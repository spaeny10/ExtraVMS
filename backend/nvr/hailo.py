"""YOLO on a Hailo-8 PCIe accelerator: a drop-in for the slice of ultralytics.YOLO the verifier uses.

A lite site (no NVIDIA GPU) with a Hailo-8 sets NVR_YOLO_DEVICE=hailo and NVR_YOLO_MODEL=<file>.hef (a Hailo
Model Zoo COCO detector compiled for Hailo-8, e.g. yolov11s.hef; tools/hailo_setup.sh installs the driver,
HailoRT and the HEF). HailoYOLO then answers

    model.names                                  {class id: COCO name}, the ids ultralytics uses (person=0, car=2...)
    model.predict(images, imgsz=, conf=, device=, verbose=, classes=)
        -> [result.boxes.xyxyn / .cls / .conf]   one per image, each an ndarray (so .tolist() works)

with boxes normalised to the ORIGINAL image: frames are letterboxed to the HEF's fixed input (imgsz is ignored),
run one at a time, and the boxes mapped back. The Model Zoo HEFs end in Hailo's on-chip NMS, whose output
(HAILO_NMS_BY_CLASS, float32) is parsed here; a HEF with a plain decoded head ([cx, cy, w, h, class scores...]
rows) goes through numpy NMS instead.

The device and the configured model stay open for the life of the object; calls are serialised with a lock (the
verifier runs everything on one executor thread anyway). Only the parsing/geometry is importable without the
hardware: hailo_platform is imported in HailoYOLO.__init__.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("nvr.hailo")

COCO_NAMES = ["person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
              "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
              "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
              "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
              "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
              "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
              "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard",
              "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
              "scissors", "teddy bear", "hair drier", "toothbrush"]
NAMES = dict(enumerate(COCO_NAMES))

PAD_VALUE = 114          # ultralytics' letterbox grey
NMS_SCORE_FLOOR = 0.1    # on-chip NMS keeps everything above this; predict(conf=) filters further
NMS_IOU = 0.7            # ultralytics' default IoU for NMS
MAX_DET = 300            # ultralytics' default max detections per image


# ---------------------------------------------------------------- geometry

def letterbox(img: np.ndarray, size: tuple[int, int]) -> tuple[np.ndarray, float, float, float]:
    """Resize keeping the aspect ratio and pad to size=(h, w) with grey, centred (as ultralytics does).
    Returns (padded image, scale, pad_x, pad_y): input pixel = original pixel * scale + pad."""
    h, w = img.shape[:2]
    th, tw = size
    scale = min(th / h, tw / w)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    resized = img if (nw, nh) == (w, h) else cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    pad_x, pad_y = (tw - nw) / 2, (th - nh) / 2
    left, top = int(round(pad_x - 0.1)), int(round(pad_y - 0.1))
    out = np.full((th, tw, 3), PAD_VALUE, dtype=np.uint8)
    out[top:top + nh, left:left + nw] = resized
    return out, scale, float(left), float(top)


def unletterbox(xyxy_in: np.ndarray, scale: float, pad_x: float, pad_y: float, orig_w: int, orig_h: int) -> np.ndarray:
    """Boxes in input pixels (N, 4 x1 y1 x2 y2) -> normalised x1 y1 x2 y2 of the original image, clipped to [0, 1]."""
    b = np.asarray(xyxy_in, dtype=np.float32).reshape(-1, 4).copy()
    b[:, [0, 2]] = (b[:, [0, 2]] - pad_x) / scale / orig_w
    b[:, [1, 3]] = (b[:, [1, 3]] - pad_y) / scale / orig_h
    return np.clip(b, 0.0, 1.0)


# ---------------------------------------------------------------- output parsing

def parse_nms_by_class(raw: np.ndarray, num_classes: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Hailo HAILO_NMS_BY_CLASS float32 buffer for one frame: for each class, a count followed by that many
    [y_min, x_min, y_max, x_max, score] (normalised to the model input). Returns (yxyx (N, 4), score (N,), cls (N,))."""
    flat = np.asarray(raw, dtype=np.float32).reshape(-1)
    boxes, scores, classes = [], [], []
    off = 0
    for c in range(num_classes):
        if off >= flat.size:
            break
        n = int(flat[off])
        off += 1
        if n:
            d = flat[off:off + 5 * n].reshape(n, 5)
            off += 5 * n
            boxes.append(d[:, :4])
            scores.append(d[:, 4])
            classes.append(np.full(n, c, dtype=np.int64))
    if not boxes:
        return np.zeros((0, 4), np.float32), np.zeros(0, np.float32), np.zeros(0, np.int64)
    return np.concatenate(boxes), np.concatenate(scores), np.concatenate(classes)


def nms(boxes: np.ndarray, scores: np.ndarray, iou_thr: float = NMS_IOU) -> np.ndarray:
    """Greedy non-maximum suppression on xyxy boxes; indices kept, highest score first."""
    boxes = np.asarray(boxes, dtype=np.float32).reshape(-1, 4)
    order = np.argsort(-np.asarray(scores, dtype=np.float32), kind="stable")
    area = np.clip(boxes[:, 2] - boxes[:, 0], 0, None) * np.clip(boxes[:, 3] - boxes[:, 1], 0, None)
    keep = []
    while order.size:
        i = order[0]
        keep.append(int(i))
        rest = order[1:]
        ix = np.clip(np.minimum(boxes[i, 2], boxes[rest, 2]) - np.maximum(boxes[i, 0], boxes[rest, 0]), 0, None)
        iy = np.clip(np.minimum(boxes[i, 3], boxes[rest, 3]) - np.maximum(boxes[i, 1], boxes[rest, 1]), 0, None)
        inter = ix * iy
        union = area[i] + area[rest] - inter
        iou = np.where(union > 0, inter / np.maximum(union, 1e-12), 0.0)
        order = rest[iou <= iou_thr]
    return np.asarray(keep, dtype=np.int64)


def batched_nms(boxes: np.ndarray, scores: np.ndarray, classes: np.ndarray, iou_thr: float = NMS_IOU) -> np.ndarray:
    """Per-class NMS (what ultralytics and Hailo's on-chip NMS do): boxes of different classes never suppress each other."""
    keep = [np.flatnonzero(classes == c)[nms(boxes[classes == c], scores[classes == c], iou_thr)] for c in np.unique(classes)]
    keep = np.concatenate(keep) if keep else np.zeros(0, np.int64)
    return keep[np.argsort(-scores[keep], kind="stable")]


def decode_dense(raw: np.ndarray, num_classes: int, score_floor: float = NMS_SCORE_FLOOR) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """A plain decoded head (no on-chip NMS): rows of [cx, cy, w, h, score_0..score_{C-1}] in input pixels, either
    (N, 4+C) or channel-first (4+C, N). Returns (xyxy input pixels, score, cls) after per-class NMS."""
    a = np.asarray(raw, dtype=np.float32)
    a = a.reshape(-1, a.shape[-1]) if a.ndim > 2 else a
    if a.shape[-1] != 4 + num_classes and a.shape[0] == 4 + num_classes:
        a = a.T
    cls = a[:, 4:].argmax(1)
    score = a[np.arange(len(a)), 4 + cls]
    m = score >= score_floor
    a, cls, score = a[m], cls[m], score[m]
    xyxy = np.stack([a[:, 0] - a[:, 2] / 2, a[:, 1] - a[:, 3] / 2, a[:, 0] + a[:, 2] / 2, a[:, 1] + a[:, 3] / 2], 1)
    k = batched_nms(xyxy, score, cls)
    return xyxy[k], score[k], cls[k].astype(np.int64)


# ---------------------------------------------------------------- results (the ultralytics shape the callers read)

class Boxes:
    def __init__(self, xyxyn: np.ndarray, cls: np.ndarray, conf: np.ndarray) -> None:
        self.xyxyn = np.asarray(xyxyn, dtype=np.float32).reshape(-1, 4)
        self.cls = np.asarray(cls, dtype=np.float32).reshape(-1)
        self.conf = np.asarray(conf, dtype=np.float32).reshape(-1)

    def __len__(self) -> int:
        return len(self.conf)


class Result:
    def __init__(self, boxes: Boxes, orig_shape: tuple[int, int]) -> None:
        self.boxes = boxes
        self.orig_shape = orig_shape


def to_result(xyxy_in: np.ndarray, score: np.ndarray, cls: np.ndarray, scale: float, pad_x: float, pad_y: float,
              orig_shape: tuple[int, int], conf: float, classes) -> Result:
    """Filter by conf / classes, map back to the original image, sort by confidence, cap at MAX_DET."""
    h, w = orig_shape
    m = np.asarray(score) >= conf
    if classes is not None:
        m &= np.isin(cls, list(classes))
    xyxy_in, score, cls = np.asarray(xyxy_in).reshape(-1, 4)[m], np.asarray(score)[m], np.asarray(cls)[m]
    order = np.argsort(-score, kind="stable")[:MAX_DET]
    return Result(Boxes(unletterbox(xyxy_in[order], scale, pad_x, pad_y, w, h), cls[order], score[order]), (h, w))


def frame_result(raw: np.ndarray, kind: str, num_classes: int, input_hw: tuple[int, int], scale: float, pad_x: float,
                 pad_y: float, orig_shape: tuple[int, int], conf: float, classes) -> Result:
    """One frame's raw device output -> Result. kind: "nms_by_class" (normalised yxyx) or "dense" (input pixels)."""
    ih, iw = input_hw
    if kind == "nms_by_class":
        yxyx, score, cls = parse_nms_by_class(raw, num_classes)
        xyxy = np.stack([yxyx[:, 1] * iw, yxyx[:, 0] * ih, yxyx[:, 3] * iw, yxyx[:, 2] * ih], 1) if len(yxyx) else np.zeros((0, 4), np.float32)
    else:
        xyxy, score, cls = decode_dense(raw, num_classes)
    return to_result(xyxy, score, cls, scale, pad_x, pad_y, orig_shape, conf, classes)


# ---------------------------------------------------------------- device

class HailoYOLO:
    """ultralytics.YOLO stand-in backed by a Hailo-8 (see the module docstring)."""

    names = NAMES

    def __init__(self, hef: str | Path, timeout_ms: int = 10000) -> None:
        from hailo_platform import FormatType, HailoSchedulingAlgorithm, VDevice  # HailoRT's pyhailort

        self.hef = Path(hef)
        if not self.hef.exists():
            raise FileNotFoundError(f"{self.hef} not found (tools/hailo_setup.sh downloads the Model Zoo HEF)")
        self.timeout_ms = timeout_ms
        self._lock = threading.Lock()
        params = VDevice.create_params()
        params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN  # the InferModel API activates the network through the scheduler
        self._vdevice = VDevice(params)
        self.device_name = self._identify()
        self._model = self._vdevice.create_infer_model(str(self.hef))
        self._model.set_batch_size(1)
        self._model.input().set_format_type(FormatType.UINT8)
        out = self._model.output()
        out.set_format_type(FormatType.FLOAT32)
        self.output_name = out.name
        self.kind = "nms_by_class" if out.is_nms else "dense"
        if out.is_nms:
            try:  # keep low-confidence boxes on chip; predict(conf=) decides
                out.set_nms_score_threshold(NMS_SCORE_FLOOR)
                out.set_nms_iou_threshold(NMS_IOU)
            except Exception as e:  # older HEFs may not allow it
                log.info("HEF NMS thresholds left at the compiled defaults: %s", e)
        shape = tuple(self._model.input().shape)  # (h, w, 3)
        self.input_hw = (int(shape[0]), int(shape[1]))
        self.num_classes = len(NAMES)
        self._out_shape = tuple(out.shape)
        self._configured = self._model.configure()
        self._bindings = self._configured.create_bindings()
        self._in_buf = np.empty(shape, dtype=np.uint8)
        self._out_buf = np.empty(self._out_shape, dtype=np.float32)
        self._bindings.input().set_buffer(self._in_buf)
        self._bindings.output().set_buffer(self._out_buf)
        log.info("Hailo %s: %s, input %sx%s, output %s %s (%s)", self.device_name, self.hef.name, self.input_hw[1],
                 self.input_hw[0], self.output_name, self._out_shape, self.kind)

    def _identify(self) -> str:
        try:
            dev = self._vdevice.get_physical_devices()[0]
            arch = str(dev.control.identify().device_architecture)
            return {"HAILO8": "hailo-8", "HAILO8L": "hailo-8l", "HAILO8R": "hailo-8r"}.get(arch.split(".")[-1].upper(), arch.lower())
        except Exception:
            return "hailo-8"

    def to(self, device) -> "HailoYOLO":  # ultralytics API; the HEF lives on the Hailo
        return self

    def infer_raw(self, rgb: np.ndarray) -> np.ndarray:
        """One letterboxed RGB uint8 frame (input size) -> a copy of the raw output buffer."""
        with self._lock:
            np.copyto(self._in_buf, rgb)
            self._configured.run([self._bindings], self.timeout_ms)
            return self._bindings.output().get_buffer(tf_format=None).copy() if self.kind == "nms_by_class" else self._out_buf.copy()

    def predict(self, images, imgsz=None, conf: float = 0.25, device=None, verbose: bool = False, classes=None, **_) -> list[Result]:
        if isinstance(images, np.ndarray):
            images = [images]
        out = []
        for img in images:  # BGR, any size (as ultralytics takes them)
            boxed, scale, px, py = letterbox(img, self.input_hw)
            raw = self.infer_raw(cv2.cvtColor(boxed, cv2.COLOR_BGR2RGB))
            out.append(frame_result(raw, self.kind, self.num_classes, self.input_hw, scale, px, py, img.shape[:2], conf, classes))
        return out

    __call__ = predict

    def close(self) -> None:
        with self._lock:
            for name in ("_configured", "_vdevice"):
                obj = getattr(self, name, None)
                try:
                    if obj is not None and hasattr(obj, "release"):
                        obj.release()
                except Exception:
                    pass
