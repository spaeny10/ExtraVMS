"""YOLO verification of camera (edge) detections.

For a closed event: fetch the clip from MediaMTX recordings, decode frames at sampled track
timestamps, run YOLO, and check that YOLO sees the same class where the camera said it was.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")  # GPU indices match nvidia-smi / Ollama

import av
import cv2
import numpy as np

from . import zones
from .config import ROOT, settings

log = logging.getLogger("nvr.verifier")

PERSON = {0}
VEHICLE = {1, 2, 3, 5, 7}  # bicycle, car, motorcycle, bus, truck
LABEL_CLASSES = {"person": PERSON, "vehicle": VEHICLE}


def event_dir(event_id: int) -> Path:
    d = settings.data_dir / "events" / str(event_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def center_inside(a, b) -> bool:
    cx, cy = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2
    return b[0] <= cx <= b[2] and b[1] <= cy <= b[3]


MAX_SHIFT_S = 5.0     # clock error the verifier tolerates between camera metadata and the recording
SHIFT_STEP_S = 0.25


def box_at(path: list, t: float, tol: float = 0.4):
    """Camera box at time t, from the nearest path point (None if the track has no point near t)."""
    best = min(path, key=lambda p: abs(p[0] - t), default=None)
    return tuple(best[1:5]) if best is not None and abs(best[0] - t) <= tol else None


def _matches(cam_box, boxes: list, allowed: set) -> tuple[dict | None, float]:
    match, match_iou = None, 0.0
    for b in boxes:
        if b["cls_id"] not in allowed:
            continue
        score = iou(cam_box, b["box"])
        if score >= settings.verify_iou or center_inside(cam_box, b["box"]) or center_inside(b["box"], cam_box):
            if match is None or b["conf"] > match["conf"]:
                match, match_iou = b, score
    return match, match_iou


def _centre(b) -> tuple[float, float]:
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def _moves_together(pairs: list[tuple[tuple[float, float], tuple[float, float]]]) -> bool:
    """If the camera track moved between the first and last matched frames, the YOLO boxes must have moved
    the same way by at least half as far; a parked object can't follow a moving track."""
    (t0, y0), (t1, y1) = pairs[0], pairs[-1]
    tdx, tdy = t1[0] - t0[0], t1[1] - t0[1]
    track_move = (tdx * tdx + tdy * tdy) ** 0.5
    if track_move < 0.08:  # the track barely moved: nothing to check
        return True
    ydx, ydy = y1[0] - y0[0], y1[1] - y0[1]
    along = (ydx * tdx + ydy * tdy) / track_move  # YOLO movement in the track's direction
    return along >= 0.5 * track_move


def best_shift(path: list, frames_boxes: list[tuple[float, list]], allowed: set) -> tuple[float, int]:
    """The single time shift (s) that lines the camera track up with YOLO in the most frames.
    One shift for the whole event means the matches must follow the track's motion, so a parked car
    that happens to sit on the path can't produce matches in several frames."""
    best, best_key = (0.0, 0), (0, 0.0)
    steps = int(MAX_SHIFT_S / SHIFT_STEP_S)
    for k in sorted(range(-steps, steps + 1), key=abs):  # smallest shift wins exact ties
        shift = k * SHIFT_STEP_S
        hits, overlap, pairs = 0, 0.0, []
        for ts, boxes in frames_boxes:
            cb = box_at(path, ts + shift)
            m, m_iou = _matches(cb, boxes, allowed) if cb is not None else (None, 0.0)
            if m is not None:
                hits, overlap = hits + 1, overlap + m_iou
                pairs.append((_centre(cb), _centre(m["box"])))
        if hits >= 2 and not _moves_together(pairs):
            continue
        # most matching frames first; among those, the tightest overlap is the real clock offset
        if (hits, round(overlap, 3)) > best_key:
            best, best_key = (shift, hits), (hits, round(overlap, 3))
    return best


def sample_path(path: list, n: int) -> list:
    """Pick n path entries spread over the track, preferring confident ones."""
    if len(path) <= n:
        return list(path)
    step = len(path) / n
    out = []
    for i in range(n):
        window = path[int(i * step):int((i + 1) * step)] or [path[int(i * step)]]
        out.append(max(window, key=lambda p: p[5]))
    return out


def grab_frames(clip: Path, clip_start: float, targets: list[float]) -> dict[float, np.ndarray]:
    """Decode the clip once and return the frame nearest each target epoch timestamp."""
    wanted = sorted(targets)
    best: dict[float, tuple[float, np.ndarray]] = {}
    with av.open(str(clip)) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        last_target = wanted[-1] - clip_start
        for frame in container.decode(stream):
            if frame.time is None:
                continue
            t = frame.time
            near = [w for w in wanted if abs((w - clip_start) - t) < 0.6]
            if near:
                img = None
                for w in near:
                    d = abs((w - clip_start) - t)
                    if w not in best or d < best[w][0]:
                        img = img if img is not None else frame.to_ndarray(format="bgr24")
                        best[w] = (d, img)
            if t > last_target + 0.6:
                break
    return {w: v[1] for w, v in best.items()}


class Verifier:
    def __init__(self) -> None:
        from ultralytics import YOLO  # heavy import; keep module import cheap

        weights = ROOT / "models" / settings.yolo_model
        weights.parent.mkdir(exist_ok=True)
        if not weights.exists():
            from ultralytics.utils.downloads import attempt_download_asset
            attempt_download_asset(str(weights))
        self.model = YOLO(str(weights))
        self.model.to(settings.yolo_device)
        self._reid = None  # person re-ID (loaded on first person event)
        log.info("YOLO %s loaded on %s", settings.yolo_model, settings.yolo_device)

    def _reid_embedding(self, frames: dict, detections: list) -> list[float] | None:
        """Appearance fingerprint of the verified person (tight crops of YOLO's matched boxes)."""
        from .reid import ReID, person_crop
        try:
            if self._reid is None:
                self._reid = ReID()
            crops = [person_crop(frames[d["ts"]], d["match"]["box"]) for d in _largest_matches(detections) if d["ts"] in frames]
            vec = self._reid.embed(crops)
            return vec.tolist() if vec is not None else None
        except Exception:
            log.exception("re-ID embedding failed")
            return None

    def verify(self, event: dict, clip: Path, clip_start: float, zone_list: list[dict] | None = None) -> dict:
        zone_list = zones.normalize(zone_list)
        label = event["camera_class"]
        allowed = LABEL_CLASSES.get(label, set())
        samples = sample_path(event["path"], settings.verify_frames)
        frames = grab_frames(clip, clip_start, [s[0] for s in samples])
        if not frames:
            return {"status": "error", "error": "no frames decoded from recording"}

        ts_list = [s[0] for s in samples if s[0] in frames]
        # YOLO only sees allowed areas: masked regions are painted grey before inference.
        results = self.model.predict([zones.mask_frame(frames[t], zone_list) for t in ts_list], imgsz=settings.yolo_imgsz,
                                     conf=settings.yolo_conf, device=settings.yolo_device, verbose=False,
                                     classes=sorted(PERSON | VEHICLE))
        names = self.model.names
        detections, hits, best = [], 0, None
        sample_by_ts = {s[0]: s for s in samples}
        for ts, res in zip(ts_list, results):
            cam_box = tuple(sample_by_ts[ts][1:5])
            boxes = [
                {"cls": names[int(c)], "cls_id": int(c), "conf": round(float(p), 3),
                 "box": [round(float(v), 4) for v in b]}
                for b, c, p in zip(res.boxes.xyxyn.tolist(), res.boxes.cls.tolist(), res.boxes.conf.tolist())
            ]
            # Drop anything standing in a masked area (e.g. a box that straddles the mask edge).
            boxes = [b for b in boxes if zones.allowed(zones.foot(b["box"]), zone_list)]
            match, match_iou = _matches(cam_box, boxes, allowed)
            if match:
                hits += 1
                if best is None or match["conf"] > best[1]["conf"]:
                    best = (ts, match, cam_box)
            detections.append({"ts": ts, "cam_box": list(cam_box), "yolo": boxes,
                               "match": match, "iou": round(match_iou, 3)})

        need = min(settings.verify_min_hits, max(1, len(ts_list) // 2))
        shift = 0.0
        if hits < need:
            # The camera's metadata clock may be off by a few seconds (fast vehicles then never overlap):
            # try one consistent time shift of the camera track against the frames YOLO looked at.
            shift, shifted_hits = best_shift(event["path"], [(d["ts"], d["yolo"]) for d in detections], allowed)
            if shift and shifted_hits >= need:
                hits, best = 0, None
                for d in detections:
                    cb = box_at(event["path"], d["ts"] + shift)
                    m, m_iou = _matches(cb, d["yolo"], allowed) if cb else (None, 0.0)
                    if cb:
                        d["cam_box"] = list(cb)
                    d["match"], d["iou"] = m, round(m_iou, 3)
                    if m:
                        hits += 1
                        if best is None or m["conf"] > best[1]["conf"]:
                            best = (d["ts"], m, tuple(cb))
            else:
                shift = 0.0
        verified = hits >= need
        out_dir = event_dir(event["id"])
        # Snapshot: best matching frame, or the middle sample if nothing matched.
        snap_ts, snap_match, snap_cam = best if best else (ts_list[len(ts_list) // 2], None,
                                                           tuple(sample_by_ts[ts_list[len(ts_list) // 2]][1:5]))
        snapshot = out_dir / "snapshot.jpg"
        snap_img = zones.draw_outlines(annotate(frames[snap_ts], snap_cam, snap_match), zone_list)
        cv2.imwrite(str(snapshot), snap_img, [cv2.IMWRITE_JPEG_QUALITY, 88])
        keyframes = save_keyframes(out_dir, frames, detections)
        reid_vec = self._reid_embedding(frames, detections) if verified and label == "person" else None
        return {
            "reid": reid_vec,
            "status": "verified" if verified else "rejected",
            "yolo_class": best[1]["cls"] if best else None,
            "yolo_conf": best[1]["conf"] if best else None,
            "yolo_hits": hits,
            "detections": {"samples": detections, "keyframes": keyframes, "needed": need, "time_shift_s": shift},
            "snapshot": str(snapshot.relative_to(settings.data_dir)),
            "clip_start": clip_start,
        }


def _largest_matches(detections: list, n: int = 4) -> list:
    matched = [d for d in detections if d["match"]]
    area = lambda b: (b[2] - b[0]) * (b[3] - b[1])
    return sorted(matched, key=lambda d: area(d["match"]["box"]), reverse=True)[:n]


def annotate(img: np.ndarray, cam_box, match: dict | None) -> np.ndarray:
    out = img.copy()
    h, w = out.shape[:2]
    px = lambda b: (int(b[0] * w), int(b[1] * h), int(b[2] * w), int(b[3] * h))
    x1, y1, x2, y2 = px(cam_box)
    cv2.rectangle(out, (x1, y1), (x2, y2), (0, 200, 255), 2)  # camera: amber
    if match:
        x1, y1, x2, y2 = px(match["box"])
        cv2.rectangle(out, (x1, y1), (x2, y2), (80, 220, 80), 3)  # YOLO: green
        cv2.putText(out, f"{match['cls']} {match['conf']:.2f}", (x1, max(20, y1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (80, 220, 80), 2)
    return out


def save_keyframes(out_dir: Path, frames: dict, detections: list) -> list[dict]:
    """Images for the VLM: one annotated wide shot plus object crops across the track."""
    # Prefer frames where YOLO confirmed the object; otherwise fall back to the camera's boxes
    # so rejected / unverified events can still be reviewed or discussed with the VLM.
    matched = [d for d in detections if d["match"]] or detections
    if not matched:
        return []
    picks = [matched[int(i * (len(matched) - 1) / max(1, settings.synopsis_images - 2))]
             for i in range(min(len(matched), settings.synopsis_images - 1))]
    keyframes = []
    mid = matched[len(matched) // 2]
    wide = annotate(frames[mid["ts"]], mid["cam_box"], mid["match"])
    scale = 1280 / wide.shape[1]
    wide = cv2.resize(wide, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(out_dir / "wide.jpg"), wide, [cv2.IMWRITE_JPEG_QUALITY, 85])
    keyframes.append({"file": "wide.jpg", "ts": mid["ts"], "kind": "wide"})
    seen = set()
    for i, d in enumerate(picks):
        if d["ts"] in seen:
            continue
        seen.add(d["ts"])
        img = frames[d["ts"]]
        h, w = img.shape[:2]
        l, t, r, b = d["match"]["box"] if d["match"] else d["cam_box"]
        # pad the crop so the VLM sees context around the object
        pw, ph = (r - l) * 0.6 + 0.03, (b - t) * 0.4 + 0.03
        x1, y1 = int(max(0, l - pw) * w), int(max(0, t - ph) * h)
        x2, y2 = int(min(1, r + pw) * w), int(min(1, b + ph) * h)
        crop = img[y1:y2, x1:x2]
        if crop.size == 0:
            continue
        s = 512 / max(crop.shape[:2])
        if s < 1:
            crop = cv2.resize(crop, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        name = f"crop_{i}.jpg"
        cv2.imwrite(str(out_dir / name), crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
        keyframes.append({"file": name, "ts": d["ts"], "kind": "crop"})
    return keyframes
