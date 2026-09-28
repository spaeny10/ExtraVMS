"""Merge back-to-back fragments of one visit on one camera into a single event before Qwen describes it.

A camera that loses and re-finds a person usually hands out a new ObjectId, and tracks longer than
`track_max_seconds` are split on purpose, so one visit often arrives as several short events. After a
fragment B is verified we look for the fragment A just before it on the same camera and merge B into A when
    - the gap is short (B starts within `track_merge_gap` of A ending),
    - same class, same PTZ view, A not too long already and untouched by an operator, and
    - the camera gave both the same track id, or B starts where A ended and (for people) the re-ID
      appearance embeddings agree.
The merged A goes back through verification as one span (clip, YOLO, keyframes, re-ID, cells) and is
described once. Every merge is logged with its reason so a wrong one is easy to spot.
"""
from __future__ import annotations

import logging
import shutil

import numpy as np

from . import cells
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


def continuous(a_path: list, b_path: list, max_dist: float | None = None) -> bool:
    """B's first box is where A's last box was (centres within max_dist, or overlapping)."""
    if not a_path or not b_path:
        return False
    p, q = a_path[-1], b_path[0]
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
