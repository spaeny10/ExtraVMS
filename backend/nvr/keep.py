"""What to keep once footage is older than the continuous window.

Policy is site-wide (settings table) with optional per-camera partial overrides. `keep_windows` turns
events, camera rule events and operator locks overlapping a stretch of time into merged windows, each
with the reasons it's kept and an importance score (used to decide what goes first when disk runs low).
"""
from __future__ import annotations

import copy
import json

from . import zones
from .config import settings
from .db import db

DEFAULT_POLICY = {
    "continuous_days": 10,
    "pad_before_s": 30,
    "pad_after_s": 30,
    "keep": {
        "person": True,
        "qwen_analyzed": True,
        "rule_events": True,
        "feedback": True,
        "vehicles_in_detect_zones": True,
    },
    "rule_topics": ["FieldDetector", "LineDetector", "LoiteringDetector", "FieldInDetector",
                    "FieldOutDetector", "TamperDetector", "ObjectLeftDetector", "ObjectRemoveDetector"],
    "min_free_gb": 200,
}

LOCKED_SCORE = 1000.0
SCORES = {"threat:high": 90, "threat:medium": 80, "threat:low": 70, "person+synopsis": 60, "person": 50,
          "feedback": 45, "rule": 40, "qwen": 35, "vehicle_in_zone": 30}
MERGE_GAP_S = 5.0
DEFER_LIMIT_DAYS = 2  # stop waiting for unfinished AI work this long after footage ages out


def _merge(base: dict, override: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif v is not None:
            out[k] = v
    return out


def site_policy() -> dict:
    return _merge(DEFAULT_POLICY, db.get_setting("retention_policy"))


def policy_for(camera: dict) -> dict:
    """Site policy with the camera's override applied. A legacy per-camera retention_days maps to continuous_days."""
    p = _merge(site_policy(), camera.get("retention_policy"))
    if camera.get("retention_days") and not (camera.get("retention_policy") or {}).get("continuous_days"):
        p["continuous_days"] = camera["retention_days"]
    return p


def keep_windows(camera: dict, start: float, end: float, policy: dict) -> tuple[list[dict], bool]:
    """Windows to keep within [start, end] and whether to defer (AI still working on overlapping events).

    Returns ([{start, end, reasons: [...], score}], defer).
    """
    keep = policy["keep"]
    pb, pa = float(policy["pad_before_s"]), float(policy["pad_after_s"])
    items: list[tuple[float, float, str, float]] = []  # (start, end, reason, score)
    defer = False
    include_zones = [z for z in zones.normalize(camera.get("zones")) if z["type"] == "include"]

    events = db.all(
        "SELECT id, camera_class, status, start_ts, end_ts, synopsis, threat, feedback, corrected_at, path, error "
        "FROM events WHERE camera_id=? AND start_ts <= ? AND COALESCE(end_ts, start_ts) >= ? AND status != 'masked'",
        [camera["id"], end + pb, start - pa])
    noted = {r["event_id"] for r in db.all(
        "SELECT DISTINCT event_id FROM chat_messages WHERE saved=1 AND event_id IN "
        f"({','.join(str(e['id']) for e in events if e.get('id') is not None) or 'NULL'})")}
    for e in events:
        s, t = e["start_ts"] - pb, (e["end_ts"] or e["start_ts"]) + pa
        person = e["camera_class"] == "person"
        if e["status"] in ("open", "pending"):
            defer = True
        elif (person and e["status"] == "verified" and not e["synopsis"] and not e["error"]
              and "person" in settings.synopsis_labels):
            defer = True  # Qwen hasn't described it yet; its synopsis may matter for keeping
        if keep["person"] and person and e["status"] == "verified":
            items.append((s, t, "person+synopsis" if e["synopsis"] else "person",
                          SCORES["person+synopsis" if e["synopsis"] else "person"]))
        if keep["qwen_analyzed"] and e["synopsis"]:
            threat = e["threat"] if e["threat"] in ("low", "medium", "high") else None
            items.append((s, t, f"threat:{threat}" if threat else "qwen", SCORES[f"threat:{threat}" if threat else "qwen"]))
        if keep["feedback"] and (e["feedback"] or e["corrected_at"] or e["id"] in noted):
            items.append((s, t, "feedback", SCORES["feedback"]))
        if (keep["vehicles_in_detect_zones"] and include_zones and e["camera_class"] == "vehicle"
                and e["status"] == "verified" and zones.path_allowed(json.loads(e["path"] or "[]"), include_zones)):
            items.append((s, t, "vehicle_in_zone", SCORES["vehicle_in_zone"]))

    if keep["rule_events"] and policy["rule_topics"]:
        for r in db.all("SELECT ts, topic FROM rule_events WHERE camera_id=? AND state=1 AND ts BETWEEN ? AND ?",
                        [camera["id"], start - pa, end + pb]):
            topic = r["topic"].split(":")[-1]
            if any(f"/{name}/" in f"/{topic}/" for name in policy["rule_topics"]):
                items.append((r["ts"] - pb, r["ts"] + pa, f"rule:{topic.split('/')[1] if '/' in topic else topic}",
                              SCORES["rule"]))

    for lk in db.all("SELECT start_ts, end_ts FROM locks WHERE camera_id=? AND start_ts <= ? AND end_ts >= ?",
                     [camera["id"], end, start]):
        items.append((lk["start_ts"], lk["end_ts"], "locked", LOCKED_SCORE))

    # clip to the requested range and merge
    items = sorted((max(s, start), min(t, end), why, sc) for s, t, why, sc in items if t > start and s < end)
    windows: list[dict] = []
    for s, t, why, sc in items:
        if windows and s <= windows[-1]["end"] + MERGE_GAP_S:
            w = windows[-1]
            w["end"] = max(w["end"], t)
            w["score"] = max(w["score"], sc)
            if why not in w["reasons"]:
                w["reasons"].append(why)
        else:
            windows.append({"start": s, "end": t, "reasons": [why], "score": sc})
    return windows, defer


def is_locked(camera_id: str, start: float, end: float) -> bool:
    return db.one("SELECT 1 FROM locks WHERE camera_id=? AND start_ts < ? AND end_ts > ? LIMIT 1",
                  [camera_id, end, start]) is not None
