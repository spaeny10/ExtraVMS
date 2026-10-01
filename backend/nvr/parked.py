"""Parked vehicles: the camera's own analytics fire on shimmer, shadows or a flapping strap next to a parked machine,
and the large, confident YOLO box around the machine then "confirms" every one of those detections (a small camera
box is centre-inside the YOLO box). The result was a hundred verified "vehicle" events a day about a telehandler
that never moved.

Two checks, both only for vehicle events (a person standing still is never "parked"):
  * in the clip: the matched YOLO boxes sat still across the sampled frames while the camera's boxes were much
    smaller than the vehicle (or wandered about inside it): the motion was something else.
  * memory: a spot where a vehicle has been seen static in several events over at least 10 minutes is remembered
    per camera (setting `parked:<camera_id>`); a later event whose YOLO box sat still on that spot is rejected even
    if the camera box is large. The spot is forgotten once an event shows nothing parked there any more (it drove
    away), so the departure and the next arrival are kept: both have a YOLO box that moves during the clip.

The pure functions here take the verifier's per-frame `detections` ([{ts, cam_box, yolo, match, iou}, ...]).
"""
from __future__ import annotations

import statistics

from .config import settings

REASON = "parked vehicle, motion elsewhere"
VEHICLE_LABELS = {"vehicle"}
VEHICLE_CLS = {1, 2, 3, 5, 7}   # bicycle, car, motorcycle, bus, truck (verifier.VEHICLE)
MIN_FRAMES = 3                  # matched frames needed before "it didn't move" means anything
MIN_SPAN_S = 2.0                # ...spread over at least this long
SIZE_TOL = 0.1                  # YOLO box width/height may also vary by this share of its size (detector jitter)
WANDER_MIN = 0.03               # camera track centre moved this far while the YOLO box stayed put
MEMORY_IOU = 0.8                # a new static box is the remembered parked one
PRESENT_IOU = 0.5               # a vehicle box still occupies the remembered spot
MEMORY_SPAN_S = 600.0           # sightings must span this long before the spot is remembered
EXPIRE_S = 86400.0              # forget spots not seen for a day
STATIC_IOU = 0.75               # the same vehicle box, unmoved, in another sampled frame
STATIC_FRAC = 0.8               # ...in at least this share of the other frames: it never moved during the clip
STATIC_FRAC_PRE = 0.5           # ...or half of them, when it was already there in the pre-roll before the motion began


def _present(boxes: list | None, ref) -> bool:
    return any(b.get("cls_id") in VEHICLE_CLS and iou(b["box"], ref) >= STATIC_IOU for b in boxes or [])


def static_matches(detections: list, pre_boxes: list | None = None, spots: list | None = None) -> list[int]:
    """Indices of matched frames whose matched YOLO vehicle box never moved: present, unmoved, in (almost) every
    other sampled frame of the clip, or in half of them plus the pre-roll frame from before the camera saw any
    motion (a distant parked truck flickers in and out of YOLO's detections, but an arriving vehicle is never
    in the pre-roll at its final spot), or sitting on a remembered parking spot (`spots`, see memory below).
    Such a vehicle cannot be what moved. This catches the real Side Yard
    case: tiny camera boxes on a distant parked truck matched in 2 of 6 frames (enough hits)."""
    n = len(detections)
    if n < 3:
        return []
    out = []
    for i, d in enumerate(detections):
        m = d.get("match")
        if not m or m.get("cls_id") not in VEHICLE_CLS:
            continue
        present = sum(1 for j, o in enumerate(detections) if j != i and _present(o.get("yolo"), m["box"]))
        remembered = any(iou(m["box"], sp) >= MEMORY_IOU for sp in spots or [])   # a known parking spot (memory)
        if present >= STATIC_FRAC * (n - 1) or remembered or (pre_boxes is not None and _present(pre_boxes, m["box"])
                                                              and present >= STATIC_FRAC_PRE * (n - 1)):
            out.append(i)
    return out


def _area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _centre(b) -> tuple[float, float]:
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = _area(a) + _area(b) - inter
    return inter / union if union > 0 else 0.0


def _inside(pt, b) -> bool:
    return b[0] <= pt[0] <= b[2] and b[1] <= pt[1] <= b[3]


def _spread(values) -> float:
    return max(values) - min(values) if values else 0.0


def static_box(detections: list, max_move: float | None = None) -> list | None:
    """The median matched YOLO box if the matched boxes sat still across the clip, else None."""
    max_move = settings.parked_max_move if max_move is None else max_move
    matched = [d for d in detections if d.get("match")]
    if len(matched) < MIN_FRAMES or matched[-1]["ts"] - matched[0]["ts"] < MIN_SPAN_S:
        return None
    boxes = [d["match"]["box"] for d in matched]
    cx, cy = zip(*(_centre(b) for b in boxes))
    ws, hs = [b[2] - b[0] for b in boxes], [b[3] - b[1] for b in boxes]
    if _spread(cx) > max_move or _spread(cy) > max_move:
        return None
    if _spread(ws) > max(max_move, SIZE_TOL * statistics.mean(ws)) or _spread(hs) > max(max_move, SIZE_TOL * statistics.mean(hs)):
        return None
    return [round(statistics.median(b[i] for b in boxes), 4) for i in range(4)]


def active(entries: list, now: float) -> list:
    """Remembered spots that count: seen often enough, over long enough, and recently."""
    return [e for e in entries if e["count"] >= settings.parked_memory_min_events
            and e["last_seen"] - e["first_seen"] >= MEMORY_SPAN_S and now - e["last_seen"] <= EXPIRE_S]


def judge(label: str, detections: list, path: list, entries: list | None = None, now: float = 0.0) -> dict | None:
    """Is this vehicle event a parked vehicle with the motion somewhere else? None if not, else
    {"box": static YOLO box, "cls": its class, "via": "clip" | "memory"}."""
    if not settings.parked_suppress or label not in VEHICLE_LABELS:
        return None
    ref = static_box(detections)
    if ref is None:
        return None
    matched = [d for d in detections if d.get("match")]
    cls = statistics.mode(d["match"].get("cls") for d in matched)
    ratio = statistics.median(_area(d["cam_box"]) / max(_area(d["match"]["box"]), 1e-6) for d in matched)
    small = ratio < settings.parked_cam_box_ratio
    # the camera's box drifts about inside a vehicle that stays put (and isn't the vehicle's size)
    centres = [_centre(p[1:5]) for p in path] or [_centre(d["cam_box"]) for d in matched]
    wander = (ratio < 2 * settings.parked_cam_box_ratio
              and max(_spread([c[0] for c in centres]), _spread([c[1] for c in centres])) >= WANDER_MIN
              and all(_inside(_centre(d["cam_box"]), ref) for d in matched))
    if small or wander:
        return {"box": ref, "cls": cls, "via": "clip"}
    if any(iou(ref, e["box"]) >= MEMORY_IOU for e in active(entries or [], now)):
        return {"box": ref, "cls": cls, "via": "memory"}
    return None


def without(boxes: list, ref) -> list:
    """YOLO boxes other than the parked one (another vehicle passing it may be what the camera saw)."""
    return [b for b in boxes if iou(b["box"], ref) < PRESENT_IOU]


def update(entries: list, detections: list, ref: list | None, cls: str | None, now: float) -> list:
    """The camera's remembered spots after this event. `ref` is the event's static YOLO box (None if it moved or
    there were too few matches); only verified or parked-rejected events are passed in."""
    out = []
    frames = sorted(detections, key=lambda d: d["ts"])[-2:]  # the end of the clip: is it still there?
    for e in entries:
        if now - e["last_seen"] > EXPIRE_S:
            continue
        there = frames and any(b["cls_id"] in VEHICLE_CLS and iou(b["box"], e["box"]) >= PRESENT_IOU
                               for d in frames for b in d.get("yolo") or [])
        if frames and not there:
            continue  # nothing parked on the spot any more: it drove away
        out.append(e)
    if ref is not None:
        hit = max(out, key=lambda e: iou(ref, e["box"]), default=None)
        if hit is not None and iou(ref, hit["box"]) >= MEMORY_IOU:
            hit.update(box=ref, last_seen=max(hit["last_seen"], now), count=hit["count"] + 1)
            hit["first_seen"] = min(hit["first_seen"], now)
        else:
            out.append({"box": ref, "cls": cls, "first_seen": now, "last_seen": now, "count": 1})
    return out


def key(camera_id: str) -> str:
    return f"parked:{camera_id}"


def load(camera_id: str) -> list:
    from .db import db
    try:
        return list(db.get_setting(key(camera_id), []) or [])
    except Exception:
        return []


def save(camera_id: str, entries: list) -> None:
    from .db import db
    db.set_setting(key(camera_id), entries)
