"""Hailo-8 YOLO backend (hailo.py) without the hardware: letterbox geometry, NMS, the device's output format (a
recorded real inference), conf/class filtering, COCO names, and the verifier choosing it.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_hailo.py   (from backend/)
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-hailo-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from nvr import advisor, hailo, verifier  # noqa: E402
from nvr.config import settings  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "hailo_raw_c4704_6.json").read_text())


def _to_input(xyxyn, scale, px, py, w, h):
    """Original normalised xyxy -> input pixels (what the model sees after letterbox)."""
    b = np.asarray(xyxyn, dtype=np.float64).copy()
    b[:, [0, 2]] = b[:, [0, 2]] * w * scale + px
    b[:, [1, 3]] = b[:, [1, 3]] * h * scale + py
    return b


def test_letterbox_keeps_aspect_and_centres():
    for h, w in [(2160, 3840), (586, 1000), (1080, 1080), (1920, 1080), (300, 400)]:
        img = np.full((h, w, 3), 200, np.uint8)
        out, scale, px, py = hailo.letterbox(img, (640, 640))
        assert out.shape == (640, 640, 3) and out.dtype == np.uint8
        nw, nh = round(w * scale), round(h * scale)
        assert max(nw, nh) == 640 and abs(scale - min(640 / h, 640 / w)) < 1e-9
        # the picture sits in the middle, grey padding around it
        assert (out[int(py):int(py) + nh, int(px):int(px) + nw] == 200).all()
        assert abs(px - (640 - nw) / 2) <= 0.5 and abs(py - (640 - nh) / 2) <= 0.5
        if py >= 1:
            assert (out[0] == hailo.PAD_VALUE).all() and (out[-1] == hailo.PAD_VALUE).all()
        if px >= 1:
            assert (out[:, 0] == hailo.PAD_VALUE).all() and (out[:, -1] == hailo.PAD_VALUE).all()


def test_box_unmapping_round_trips():
    rng = np.random.default_rng(1)
    for h, w in [(2160, 3840), (586, 1000), (1920, 1080), (640, 640)]:
        _, scale, px, py = hailo.letterbox(np.zeros((h, w, 3), np.uint8), (640, 640))
        a = rng.uniform(0, 1, (20, 2))
        b = rng.uniform(0, 1, (20, 2))
        boxes = np.stack([np.minimum(a[:, 0], b[:, 0]), np.minimum(a[:, 1], b[:, 1]), np.maximum(a[:, 0], b[:, 0]), np.maximum(a[:, 1], b[:, 1])], 1)
        back = hailo.unletterbox(_to_input(boxes, scale, px, py, w, h), scale, px, py, w, h)
        assert np.abs(back - boxes).max() < 1e-5, (h, w, np.abs(back - boxes).max())
    # a box that spills into the padding is clipped to the picture
    _, scale, px, py = hailo.letterbox(np.zeros((360, 640, 3), np.uint8), (640, 640))
    out = hailo.unletterbox(np.array([[-5, 0, 650, 640]]), scale, px, py, 640, 360)
    assert out.tolist() == [[0.0, 0.0, 1.0, 1.0]]


def test_nms_suppresses_overlaps_within_a_class_only():
    boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [50, 50, 60, 60], [0, 0, 10, 10]], np.float32)
    scores = np.array([0.9, 0.8, 0.7, 0.6], np.float32)
    assert hailo.nms(boxes, scores, 0.5).tolist() == [0, 2]          # 1 and 3 overlap 0
    assert hailo.nms(boxes, scores, 0.99).tolist() == [0, 1, 2]      # only the exact duplicate goes
    cls = np.array([0, 0, 0, 2])                                     # the duplicate is a car: kept
    assert sorted(hailo.batched_nms(boxes, scores, cls, 0.5).tolist()) == [0, 2, 3]
    assert hailo.nms(np.zeros((0, 4)), np.zeros(0)).tolist() == []


def test_parse_real_device_output():
    raw = np.asarray(FIXTURE["raw"] + [0.0] * (FIXTURE["buffer_len"] - len(FIXTURE["raw"])), np.float32)
    yxyx, score, cls = hailo.parse_nms_by_class(raw, FIXTURE["num_classes"])
    assert len(yxyx) == len(score) == len(cls) > 0
    # the device's decoder can land a hair outside [0, 1] at the frame edge (-0.0006 here): to_result clips it
    assert ((yxyx >= -0.01) & (yxyx <= 1.01)).all() and (yxyx[:, 2] >= yxyx[:, 0]).all() and (yxyx[:, 3] >= yxyx[:, 1]).all()
    assert (score >= hailo.NMS_SCORE_FLOOR - 1e-6).all() and (score <= 1).all()
    assert {2, 7} <= set(cls.tolist())  # a car and a truck on this frame
    exp = FIXTURE["expected_conf025_vehicle_person"]
    res = hailo.frame_result(raw, "nms_by_class", 80, tuple(FIXTURE["input_hw"]), FIXTURE["scale"], FIXTURE["pad_x"], FIXTURE["pad_y"],
                             tuple(FIXTURE["orig_shape"]), 0.25, sorted(verifier.PERSON | verifier.VEHICLE))
    assert res.boxes.cls.tolist() == exp["cls"]
    assert np.allclose(res.boxes.conf.tolist(), exp["conf"], atol=1e-6)
    assert np.allclose(res.boxes.xyxyn.tolist(), exp["xyxyn"], atol=1e-5)
    # the truck at the left edge of the frame (x from 0), in the road band
    truck = res.boxes.xyxyn[res.boxes.cls.tolist().index(7)]
    assert truck[0] < 0.01 and 0.25 < truck[1] < 0.3 and 0.35 < truck[3] < 0.38


def test_empty_output_parses():
    yxyx, score, cls = hailo.parse_nms_by_class(np.zeros(80 * 501, np.float32), 80)
    assert yxyx.shape == (0, 4) and score.shape == (0,) and cls.shape == (0,)
    r = hailo.frame_result(np.zeros(80 * 501, np.float32), "nms_by_class", 80, (640, 640), 1.0, 0, 0, (640, 640), 0.25, None)
    assert r.boxes.xyxyn.tolist() == [] and r.boxes.cls.tolist() == [] and r.boxes.conf.tolist() == []


def _raw(dets):
    """Build a HAILO_NMS_BY_CLASS buffer from {cls: [(ymin, xmin, ymax, xmax, score), ...]}."""
    out = []
    for c in range(80):
        out.append(len(dets.get(c, [])))
        for d in dets.get(c, []):
            out.extend(d)
    return np.asarray(out + [0.0] * (80 * 501 - len(out)), np.float32)


def test_conf_and_class_filtering():
    raw = _raw({0: [(0.1, 0.1, 0.5, 0.2, 0.9), (0.6, 0.6, 0.9, 0.7, 0.2)], 2: [(0.3, 0.3, 0.4, 0.5, 0.5)], 15: [(0.2, 0.2, 0.3, 0.3, 0.95)]})
    r = hailo.frame_result(raw, "nms_by_class", 80, (640, 640), 1.0, 0.0, 0.0, (640, 640), 0.25, [0, 1, 2, 3, 5, 7])
    assert r.boxes.cls.tolist() == [0.0, 2.0]                        # cat (15) filtered, low person dropped
    assert np.allclose(r.boxes.conf.tolist(), [0.9, 0.5])            # highest first
    assert np.allclose(r.boxes.xyxyn.tolist()[0], [0.1, 0.1, 0.2, 0.5])  # yxyx -> xyxy
    r = hailo.frame_result(raw, "nms_by_class", 80, (640, 640), 1.0, 0.0, 0.0, (640, 640), 0.1, None)
    assert sorted(r.boxes.cls.tolist()) == [0.0, 0.0, 2.0, 15.0]


def test_names_are_the_coco_ids_the_verifier_uses():
    n = hailo.HailoYOLO.names
    assert len(n) == 80
    assert {i: n[i] for i in (0, 1, 2, 3, 5, 7)} == {0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
    assert verifier.PERSON == {0} and verifier.VEHICLE == {1, 2, 3, 5, 7}


def test_dense_head_goes_through_numpy_nms():
    rows = np.zeros((3, 84), np.float32)
    rows[0, :4], rows[0, 4] = [100, 100, 40, 80], 0.9          # person
    rows[1, :4], rows[1, 4] = [102, 101, 40, 80], 0.8          # same person, suppressed
    rows[2, :4], rows[2, 4 + 2] = [400, 300, 100, 50], 0.7     # car
    xyxy, score, cls = hailo.decode_dense(rows.T, 80)          # channel-first, as many heads export it
    assert cls.tolist() == [0, 2] and np.allclose(score, [0.9, 0.7])
    assert np.allclose(xyxy[0], [80, 60, 120, 140])


class _FakeHailo(hailo.HailoYOLO):
    """HailoYOLO.predict with the device replaced by the recorded output."""
    def __init__(self, hef=None):
        self.input_hw, self.kind, self.num_classes, self.device_name = (640, 640), "nms_by_class", 80, "hailo-8"
        self.seen = []

    def infer_raw(self, rgb):
        self.seen.append(rgb.shape)
        return np.asarray(FIXTURE["raw"] + [0.0] * (FIXTURE["buffer_len"] - len(FIXTURE["raw"])), np.float32)


def test_predict_matches_the_ultralytics_shape():
    m = _FakeHailo()
    h, w = FIXTURE["orig_shape"]
    res = m.predict([np.zeros((h, w, 3), np.uint8)] * 3, imgsz=1280, conf=0.25, device="hailo", verbose=False,
                    classes=sorted(verifier.PERSON | verifier.VEHICLE))
    assert len(res) == 3 and m.seen == [(640, 640, 3)] * 3            # one result per image; imgsz is the HEF's
    for r in res:
        assert isinstance(r.boxes.xyxyn.tolist(), list) and isinstance(r.boxes.cls.tolist(), list) and isinstance(r.boxes.conf.tolist(), list)
        assert np.allclose(r.boxes.xyxyn.tolist(), FIXTURE["expected_conf025_vehicle_person"]["xyxyn"], atol=1e-5)
    assert m.to("cuda:0") is m


def test_verifier_loads_hailo_for_a_hef():
    saved = settings.yolo_device, settings.yolo_model, hailo.HailoYOLO
    try:
        settings.yolo_device, settings.yolo_model = "hailo", "yolov11s.hef"
        hailo.HailoYOLO = _FakeHailo
        v = verifier.Verifier()
        assert isinstance(v.model.model, _FakeHailo) and v.model.on_hailo and v.device == "hailo-8"   # supervised (detector.py)
        assert v.model.status() is None and v.model.names == hailo.NAMES
        assert settings.torch_device == "cpu"                         # PPE / CLIP / re-ID stay on torch, on the CPU
        settings.yolo_model = "yolo11n.pt"
        try:
            verifier.Verifier()
            raise AssertionError("a .pt on the hailo device must be refused")
        except ValueError as e:
            assert ".hef" in str(e)
    finally:
        settings.yolo_device, settings.yolo_model, hailo.HailoYOLO = saved
    assert settings.torch_device == ("cpu" if settings.yolo_device == "hailo" else settings.yolo_device)


def test_advisor_knows_the_yolo_device():
    slow_cpu = advisor.check_yolo({"yolo": {"device": "cpu", "model": "yolo11n.pt", "frame_ms": [620, 580, 700, 640, 610]}})
    assert [f.key for f in slow_cpu] == ["ai:yolo_slow"] and "CPU" in slow_cpu[0].title and "Hailo" in " ".join(slow_cpu[0].steps)
    assert advisor.check_yolo({"yolo": {"device": "hailo-8", "model": "yolov11s.hef", "frame_ms": [23, 25, 22, 24, 26]}}) == []
    slow_hailo = advisor.check_yolo({"yolo": {"device": "hailo-8", "model": "yolov11s.hef", "frame_ms": [300] * 6}})
    assert slow_hailo and "hailo-8" in slow_hailo[0].title and slow_hailo[0].impact == "low"
    assert advisor.check_yolo({"yolo": {"device": "cpu", "frame_ms": [900]}}) == []   # too few samples
    assert advisor.check_yolo({}) == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
