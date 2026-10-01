"""Parked vehicles: a still YOLO box can't confirm camera motion next to it (Side Yard JCB, 117 events a day).

Drives Verifier.verify end to end with a fake YOLO model and synthetic frames (no GPU, no clip).
Run: ..\\.venv\\Scripts\\python.exe tests\\test_parked.py   (from backend/)
"""
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-parked-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from nvr import advisor, parked, verifier  # noqa: E402
from nvr.db import db  # noqa: E402

PERSON, CAR, TRUCK = 0, 2, 7
NAMES = {PERSON: "person", CAR: "car", TRUCK: "truck"}
JCB = (0.29, 0.26, 0.74, 0.58)
T0 = 1_790_000_000.0

db.upsert_camera({"id": "cam1", "name": "Side Yard", "host": "10.0.0.5", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": "p",
                  "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": [], "retention_days": None, "scene_notes": "",
                  "retention_policy": None, "policies": []})


class _List(list):
    def tolist(self):
        return list(self)


class FakeYOLO:
    """predict() answers from scene(ts) -> [(cls_id, conf, box)], for the frames grab_frames was last asked for."""
    names = NAMES

    def __init__(self, scene):
        self.scene, self.targets = scene, []

    def predict(self, imgs, **_):
        out = []
        for ts in self.targets[:len(imgs)]:
            dets = self.scene(ts)
            out.append(SimpleNamespace(boxes=SimpleNamespace(xyxyn=_List([list(b) for _, _, b in dets]),
                                                             cls=_List([float(c) for c, _, _ in dets]),
                                                             conf=_List([p for _, p, _ in dets]))))
        return out


def run(scene, path, label="vehicle", cam="cam1"):
    """Verify one event whose camera track is `path` while YOLO sees `scene(ts)`."""
    model = FakeYOLO(scene)

    def grab(clip, clip_start, targets):
        ts = [t for t in targets if path[0][0] - 6 <= t <= path[-1][0] + 4]  # what the clip covers
        model.targets = ts
        return {t: np.zeros((36, 64, 3), np.uint8) for t in ts}

    verifier.grab_frames = grab
    v = verifier.Verifier.__new__(verifier.Verifier)
    v.model, v._reid = model, None
    v._reid_embedding = lambda frames, detections: None
    start = path[0][0]
    eid = db.create_event(camera_id=cam, track_id="t", camera_class=label, camera_conf=0.8, start_ts=start, end_ts=path[-1][0],
                          path=path, status="pending")
    event = {"id": eid, "camera_id": cam, "camera_class": label, "path": path, "start_ts": start, "end_ts": path[-1][0]}
    return v.verify(event, Path("clip.mp4"), start - 5.0, None)


def jitter(box, ts, a=0.003):
    d = a * math.sin(ts * 1.7)
    return tuple(round(v + d, 4) for v in box)


def track(start, dur, box_at, n=30, conf=0.6):
    return [[start + dur * i / (n - 1), *box_at(dur * i / (n - 1)), conf] for i in range(n)]


def shimmer(start, dur=20.0):
    """Tiny camera boxes wandering ~4% of the frame over the parked machine (strap, shadow)."""
    return track(start, dur, lambda t: (0.40 + 0.04 * t / dur, 0.40, 0.43 + 0.04 * t / dur, 0.46))


def parked_scene(ts):
    return [(TRUCK, 0.91, jitter(JCB, ts))]


def big_box(start, dur=20.0):
    """A camera box about the machine's size, sitting on it."""
    return track(start, dur, lambda t: jitter((0.30, 0.27, 0.73, 0.57), start + t, 0.002))


def reset(cam="cam1"):
    db.set_setting(parked.key(cam), [])


def test_parked_truck_with_tiny_wandering_boxes_is_rejected():
    reset()
    r = run(parked_scene, shimmer(T0))
    assert r["status"] == "rejected", r["status"]
    assert r["yolo_class"] == "truck"
    assert r["detections"]["rejected"] == parked.REASON
    assert r["detections"]["parked"]["via"] in ("clip", "static")
    assert parked.iou(r["detections"]["parked"]["box"], JCB) > 0.9


def test_moving_truck_is_verified():
    reset()
    dur = 8.0
    move = lambda t: (JCB[0] - 0.4 * t / dur, JCB[1], JCB[2] - 0.4 * t / dur, JCB[3])  # dx 0.4 of the frame
    path = track(T0, dur, move)
    r = run(lambda ts: [(TRUCK, 0.9, move(ts - T0))], path)
    assert r["status"] == "verified", r["status"]
    assert "rejected" not in r["detections"]


def test_moving_truck_with_small_camera_boxes_is_verified():
    reset()
    dur = 8.0
    move = lambda t: (JCB[0] - 0.4 * t / dur, JCB[1], JCB[2] - 0.4 * t / dur, JCB[3])
    path = track(T0, dur, lambda t: (move(t)[0] + 0.15, 0.35, move(t)[0] + 0.25, 0.45))  # the cab only
    r = run(lambda ts: [(TRUCK, 0.9, move(ts - T0))], path)
    assert r["status"] == "verified", r["status"]


def test_person_standing_still_is_never_parked():
    reset()
    still = (0.50, 0.30, 0.56, 0.62)
    path = track(T0, 20.0, lambda t: (0.51 + 0.005 * math.sin(t), 0.40, 0.53 + 0.005 * math.sin(t), 0.45))  # small: an arm
    r = run(lambda ts: [(PERSON, 0.88, still)], path, label="person")
    assert r["status"] == "verified", r["status"]
    assert "rejected" not in r["detections"]


def test_arriving_vehicle_is_verified():
    reset()
    dur = 10.0
    def arrive(t):  # drives in over the first half, then stops on the spot
        x = -0.35 * max(0.0, 1 - t / (dur / 2))
        return (JCB[0] + x, JCB[1], JCB[2] + x, JCB[3])
    path = track(T0, dur, lambda t: (arrive(t)[0] + 0.1, 0.35, arrive(t)[0] + 0.2, 0.45))  # small boxes
    r = run(lambda ts: [(TRUCK, 0.9, arrive(ts - T0))], path)
    assert r["status"] == "verified", r["status"]


def test_distant_parked_truck_matched_in_a_few_frames_is_rejected():
    """The real Side Yard case: a small parked truck in the background sits unmoved in every frame; the camera's
    boxes overlap it in only 2 of 6 frames (enough hits), while the JCB in the foreground is also parked."""
    reset()
    small = (0.053, 0.136, 0.161, 0.224)
    path = track(T0, 20.0, lambda t: (0.07, 0.10, 0.14, 0.22) if 8 < t < 14 else (0.11, 0.10, 0.14, 0.15))
    r = run(lambda ts: [(TRUCK, 0.85, jitter(JCB, ts, 0.002)), (TRUCK, 0.5, jitter(small, ts, 0.002))], path)
    assert r["status"] == "rejected", r["status"]
    assert r["detections"]["rejected"] == parked.REASON and r["detections"]["parked"]["via"] == "static"
    assert r["yolo_class"] == "truck" and r["yolo_hits"] == 0
    assert sum(1 for d in r["detections"]["samples"] if d.get("static")) >= 2


def test_several_distant_parked_vehicles_are_rejected():
    """Shimmer over three different parked vehicles far away: each match is on a box that never moves."""
    reset()
    cars = [(0.65, 0.16, 0.68, 0.18), (0.73, 0.17, 0.76, 0.19), (0.81, 0.17, 0.84, 0.20)]
    path = track(T0, 12.0, lambda t: cars[min(2, int(t / 4))])
    r = run(lambda ts: [(TRUCK, 0.6, (0.0, 0.65, 0.34, 1.0))] + [(CAR, 0.3, c) for c in cars], path)
    assert r["status"] == "rejected" and r["detections"]["rejected"] == parked.REASON


def test_car_passing_the_parked_truck_is_verified():
    reset()
    dur = 8.0
    car = lambda t: (0.32 + 0.035 * t, 0.36, 0.40 + 0.035 * t, 0.46)  # drives across in front of the JCB, inside its outline
    path = track(T0, dur, car)
    r = run(lambda ts: [(TRUCK, 0.95, jitter(JCB, ts)), (CAR, 0.7, car(ts - T0))], path)
    assert r["status"] == "verified", r["status"]
    assert r["yolo_class"] == "car", r["yolo_class"]


def test_memory_learns_rejects_and_forgets():
    reset()
    # three static sightings with the camera box the machine's size: a vehicle that never moves during the clip is
    # rejected from the first one (per-frame static rule); the spot is remembered all the same
    for minutes in (0, 5, 11):
        r = run(parked_scene, big_box(T0 + minutes * 60))
        assert r["status"] == "rejected" and r["detections"]["rejected"] == parked.REASON, (minutes, r["status"])
    spots = parked.load("cam1")
    assert len(spots) == 1 and spots[0]["count"] == 3, spots
    assert parked.active(spots, T0 + 20 * 60), spots
    # the same again later: still rejected, and the memory keeps counting
    r = run(parked_scene, big_box(T0 + 20 * 60))
    assert r["status"] == "rejected" and r["detections"]["parked"]["via"] in ("static", "memory"), r["status"]
    assert parked.load("cam1")[0]["count"] == 4
    # it drives away: the departure is kept and the spot forgotten
    dur, t0 = 8.0, T0 + 30 * 60
    leave = lambda t: (JCB[0] - 0.4 * t / dur, JCB[1], JCB[2] - 0.4 * t / dur, JCB[3])
    r = run(lambda ts: [(TRUCK, 0.9, leave(ts - t0))] if ts <= t0 + dur else [], track(t0, dur, leave))
    assert r["status"] == "verified", r["status"]
    assert parked.load("cam1") == [], parked.load("cam1")
    # back on the spot later and static for the whole clip: rejected again; the memory starts over
    r = run(parked_scene, big_box(T0 + 50 * 60))
    assert r["status"] == "rejected", r["status"]
    assert parked.load("cam1")[0]["count"] == 1


def test_memory_needs_ten_minutes_and_expires():
    e = [{"box": list(JCB), "cls": "truck", "first_seen": T0, "last_seen": T0 + 120, "count": 5}]
    assert not parked.active(e, T0 + 200)  # 5 sightings in 2 minutes: not yet a parking place
    e[0]["last_seen"] = T0 + 700
    assert parked.active(e, T0 + 800)
    assert not parked.active(e, T0 + 700 + 86400 + 1)  # not seen for a day
    assert parked.update(e, [], None, None, T0 + 700 + 86400 + 1) == []


def test_disabled_setting_keeps_old_behaviour():
    reset()
    parked.settings.parked_suppress = False
    try:
        assert run(parked_scene, shimmer(T0))["status"] == "verified"
    finally:
        parked.settings.parked_suppress = True


def test_advisor_finding_at_twenty_rejections():
    now = time.time()
    db.upsert_camera({"id": "cam9", "name": "Side Yard", "host": "10.0.0.9", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": "p",
                      "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": [], "retention_days": None, "scene_notes": "",
                      "retention_policy": None, "policies": []})
    det = {"samples": [], "rejected": parked.REASON, "parked": {"box": list(JCB), "cls": "truck", "via": "clip"}}

    def add(n):
        for i in range(n):
            db.create_event(camera_id="cam9", track_id=f"p{i}", camera_class="vehicle", camera_conf=0.7, start_ts=now - 3600 + i,
                            end_ts=now - 3590 + i, path=[], status="rejected", yolo_class="truck", detections=det)

    cams = [{"id": "cam9", "name": "Side Yard", "host": "10.0.0.9"}]
    add(19)
    facts = advisor._event_facts(now)
    assert facts["cam9"]["parked"] == 19
    assert not [f for f in advisor.check_events({"events": facts, "cameras": cams}) if f.key == "events:parked:cam9"]
    add(1)
    facts = advisor._event_facts(now)
    found = [f for f in advisor.check_events({"events": facts, "cameras": cams}) if f.key == "events:parked:cam9"]
    assert found and found[0].title == "Side Yard: 20 events were a parked vehicle", found
    assert any("minimum object size" in s for s in found[0].steps)
    # the generic "rejected by YOLO" finding doesn't fire on parked rejections
    assert not [f for f in advisor.check_events({"events": facts, "cameras": cams}) if f.key == "events:rejected:cam9"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
