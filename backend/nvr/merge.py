"""Merge back-to-back fragments of one visit on one camera into a single event before Qwen describes it.

A camera that loses and re-finds a person usually hands out a new ObjectId, and tracks longer than
`track_max_seconds` are split on purpose, so one visit often arrives as several short events. After a
fragment B is verified we look for the fragment A just before it on the same camera and merge B into A when
    - the gap is short (B starts within `track_merge_gap` of A ending),
    - same class, same PTZ view, A not too long already and untouched by an operator, and
    - the camera gave both the same track id, or B starts where A ended and (for people) the re-ID
      appearance embeddings agree.
A person who stands still is often dropped by the camera's analytics for a minute or two, so a longer gap
(up to `merge_long_gap`) also merges, for people only, when the recording proves they never left: YOLO is run
on a frame every `merge_gap_step_s` across the gap and must find a person where A ended / B started in every
one of them (`candidate_long` + `footage_check`).
The merged A goes back through verification as one span (clip, YOLO, keyframes, re-ID, cells) and is
described once. Every merge is logged with its reason so a wrong one is easy to spot.
"""
from __future__ import annotations

import gc
import logging
import math
import shutil

import numpy as np

from . import cells, zones
from .config import settings
from .db import db

log = logging.getLogger("nvr.merge")
OVERLAP_S = 2.5   # the tracker closes a track after `track_end_gap` quiet seconds: B may start slightly "before" A ended


def _box_centre(p: list) -> tuple[float, float]:
    _, l, t, r, b = p[:5]
    return ((l + r) / 2, (t + b) / 2)


def _overlap(p: list, q: list) -> bool:
    _, l1, t1, r1, b1 = p[:5]
    _, l2, t2, r2, b2 = q[:5]
    return not (r1 <= l2 or r2 <= l1 or b1 <= t2 or b2 <= t1)


def _last_box(path: list) -> list | None:
    """The last well-formed path point [ts, l, t, r, b, ...]; extended paths can carry shorter entries."""
    for p in reversed(path or []):
        if isinstance(p, (list, tuple)) and len(p) >= 5:
            return list(p)
    return None


def _first_box(path: list) -> list | None:
    for p in path or []:
        if isinstance(p, (list, tuple)) and len(p) >= 5:
            return list(p)
    return None


def continuous(a_path: list, b_path: list, max_dist: float | None = None) -> bool:
    """B's first box is where A's last box was (centres within max_dist, or overlapping)."""
    p, q = _last_box(a_path), _first_box(b_path)
    if p is None or q is None:
        return False
    if _overlap(p, q):
        return True
    (x1, y1), (x2, y2) = _box_centre(p), _box_centre(q)
    return float(np.hypot(x2 - x1, y2 - y1)) <= (settings.merge_max_dist if max_dist is None else max_dist)


def reid_sim(a_id: int, b_id: int) -> float | None:
    va, vb = db.get_reid(a_id), db.get_reid(b_id)
    if va is None or vb is None:
        return None
    return float(np.dot(va, vb))


def untouched(e: dict) -> bool:
    """Nobody has corrected, rated or locked this event: it is still the pipeline's to reshape."""
    if e.get("feedback") or e.get("corrected_at"):
        return False
    return not db.one("SELECT 1 FROM locks WHERE event_id=?", [e["id"]])


def candidate(b: dict) -> tuple[dict, str] | None:
    """The event A that B continues, with the reason, or None."""
    if b["status"] != "verified" or b["end_ts"] is None:
        return None
    lo = b["start_ts"] - settings.track_merge_gap
    rows = db.all("SELECT id FROM events WHERE camera_id=? AND id!=? AND camera_class=? AND status IN ('verified','pending') "
                  "AND end_ts IS NOT NULL AND end_ts>=? AND start_ts<? ORDER BY end_ts DESC LIMIT 3",
                  [b["camera_id"], b["id"], b["camera_class"], lo, b["start_ts"]])
    for r in rows:
        a = db.event(r["id"])
        if not a:
            continue
        gap = b["start_ts"] - a["end_ts"]
        if gap < -OVERLAP_S or gap > settings.track_merge_gap:
            continue
        if (a.get("ptz_preset") or None) != (b.get("ptz_preset") or None):
            continue
        if b["end_ts"] - a["start_ts"] > settings.merge_max_seconds or not untouched(a):
            continue
        if a["track_id"] == b["track_id"]:
            return a, f"same track {a['track_id']}, gap {gap:.1f}s"
        if not continuous(a.get("path") or [], b.get("path") or []):
            continue
        if b["camera_class"] == "person":
            sim = reid_sim(a["id"], b["id"])
            if sim is None or sim < settings.merge_reid_min:
                continue
            return a, f"continuous, gap {gap:.1f}s, re-ID {sim:.2f}"
        return a, f"continuous, gap {gap:.1f}s"
    return None


def candidate_long(b: dict) -> dict | None:
    """People only: the event A that B may continue across a gap too long for `candidate` (track_merge_gap <
    gap <= merge_long_gap), passing every rule except the footage check, which the caller runs on the GPU."""
    if b["status"] != "verified" or b["end_ts"] is None or b["camera_class"] != "person":
        return None
    if settings.merge_long_gap <= settings.track_merge_gap:
        return None
    rows = db.all("SELECT id FROM events WHERE camera_id=? AND id!=? AND camera_class='person' AND status IN ('verified','pending') "
                  "AND end_ts IS NOT NULL AND end_ts>=? AND end_ts<? ORDER BY end_ts DESC LIMIT 5",
                  [b["camera_id"], b["id"], b["start_ts"] - settings.merge_long_gap, b["start_ts"] - settings.track_merge_gap])
    for r in rows:
        a = db.event(r["id"])
        if not a:
            continue
        gap = b["start_ts"] - a["end_ts"]
        if gap <= settings.track_merge_gap or gap > settings.merge_long_gap:
            continue
        if (a.get("ptz_preset") or None) != (b.get("ptz_preset") or None):
            continue
        if b["end_ts"] - a["start_ts"] > settings.merge_max_seconds or not untouched(a):
            continue
        if not continuous(a.get("path") or [], b.get("path") or []):
            continue
        sim = reid_sim(a["id"], b["id"])
        if sim is None or sim < settings.merge_reid_min:
            continue
        return a
    return None


GAP_IOU = 0.2           # a YOLO person box this close to where A ended / B started / in between counts as "still there"
FRAME_TOL_S = 10.0      # the decoded (keyframe) frame must be within this of the requested time


def gap_times(t0: float, t1: float) -> list[float]:
    """Sample times from t0 (A's last point) to t1 (B's first point), both included, every merge_gap_step_s
    (the step widens so there are never more than merge_gap_max_frames)."""
    if t1 <= t0:
        return [t0]
    cap = max(2, settings.merge_gap_max_frames)
    n = max(2, math.ceil((t1 - t0) / max(settings.merge_gap_step_s, 0.1)) + 1)
    n = min(n, cap)
    step = (t1 - t0) / (n - 1)
    return [t0 + i * step for i in range(n)]


def _near(box, ref) -> bool:
    """box and ref (both [l, t, r, b]) are the same standing person: overlap enough, or one's centre in the other."""
    from .verifier import center_inside, iou
    return iou(box, ref) > GAP_IOU or center_inside(box, ref) or center_inside(ref, box)


def frame_person_boxes(camera_id: str, t: float, zone_list: list[dict] | None, model) -> list[list[float]] | None:
    """YOLO person boxes (normalised [l, t, r, b]) in the recorded frame at t, masked zones honoured as in
    `Verifier.verify`; None when there is no recording at t. Blocking: call on the GPU executor."""
    import cv2
    from . import frames
    from .verifier import PERSON
    got = frames.preview_jpeg(camera_id, t, width=1280)
    if not got:
        return None
    jpeg, frame_t, _ = got
    if abs(frame_t - t) > FRAME_TOL_S:
        return None
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    zone_list = zones.normalize(zone_list)
    res = model.predict([zones.mask_frame(img, zone_list)], imgsz=settings.yolo_imgsz, conf=settings.yolo_conf,
                        device=settings.yolo_device, verbose=False, classes=sorted(PERSON))[0]
    boxes = [[float(v) for v in b] for b in res.boxes.xyxyn.tolist()]
    return [b for b in boxes if zones.allowed(zones.foot(b), zone_list)]


def footage_check(a: dict, b: dict, zone_list: list[dict] | None = None, model=None) -> str | None:
    """The merge reason if the recording shows a person where A ended / B started (or in between) in every
    sampled frame across the gap; None otherwise (including no recording). Blocking: run on the GPU executor."""
    p, q = _last_box(a.get("path") or []), _first_box(b.get("path") or [])
    if p is None or q is None:
        return None
    t0, t1 = p[0], q[0]
    times = gap_times(t0, t1)
    try:
        for t in times:
            boxes = frame_person_boxes(a["camera_id"], t, zone_list, model)
            if boxes is None:
                log.info("no long merge of %s into %s: no recording at %.0f", b["id"], a["id"], t)
                return None
            f = 0.0 if t1 <= t0 else min(1.0, max(0.0, (t - t0) / (t1 - t0)))
            mid = [p[i] + (q[i] - p[i]) * f for i in range(1, 5)]
            if not any(_near(bx, ref) for bx in boxes for ref in (p[1:5], q[1:5], mid)):
                log.info("no long merge of %s into %s: nobody there at %+.0fs of a %.0fs gap",
                         b["id"], a["id"], t - t0, t1 - t0)
                return None
    finally:
        gc.collect()  # PyAV frames are freed late (see verifier.grab_frames)
    return f"footage: person stayed in view for {b['start_ts'] - a['end_ts']:.0f} s ({len(times)} frames checked)"


def apply(a: dict, b: dict, reason: str = "") -> dict:
    """Fold B into A: one span, one path; A goes back to `pending` for a fresh verification; B is gone."""
    path = (a.get("path") or []) + (b.get("path") or [])
    rules = (a.get("rules") or []) + (b.get("rules") or [])
    db.update_event(a["id"], end_ts=b["end_ts"], path=path, rules=rules, cells=cells.for_event(path), status="pending",
                    camera_conf=max(a.get("camera_conf") or 0, b.get("camera_conf") or 0),
                    synopsis=None, synopsis_json=None, threat=None, error=None, detections=None, snapshot=None, clip=None,
                    areas=None, anomaly=None, anomaly_json=None, priority=None, policy=None, watched=None)
    with db.lock:
        db.conn.execute("UPDATE locks SET event_id=? WHERE event_id=?", (a["id"], b["id"]))
        db.conn.execute("UPDATE chat_messages SET event_id=? WHERE event_id=?", (a["id"], b["id"]))
        db.conn.execute("DELETE FROM event_links WHERE a=? OR b=?", (b["id"], b["id"]))
    db.delete_event(b["id"])
    shutil.rmtree(settings.data_dir / "events" / str(b["id"]), ignore_errors=True)  # event_dir() would recreate it
    log.info("merged event %s into %s on %s (%s): %.0fs -> %.0fs", b["id"], a["id"], a["camera_id"], reason,
             (a["end_ts"] or a["start_ts"]) - a["start_ts"], b["end_ts"] - a["start_ts"])
    return db.event(a["id"])
