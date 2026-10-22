"""What to keep once footage is older than the continuous window.

Policy is site-wide (settings table) with optional per-camera partial overrides. `keep_windows` turns
events, camera rule events and operator locks overlapping a stretch of time into merged windows, each
with the reasons it's kept and an importance score (used to decide what goes first when disk runs low).

Events keep no clip of their own (2026-10-22): an event's video IS this kept footage, so everything a clip used to
protect has a rule here. Keep rules (policy["keep"], all on by default):
  person                    verified person events (score 50, 60 with a synopsis)
  qwen_analyzed             anything with a synopsis; its threat level sets the score (35, threat:low/medium/high 70-90)
  rule_events               the camera's own rule events (intrusion, line crossing...: 40)
  feedback                  operator feedback, a corrected synopsis or a saved chat note (45)
  vehicles_in_detect_zones  verified vehicles in a camera's detect-only zones (30)
  weapon_check              the second look confirmed a weapon (weapon:confirmed 95) or needs a person to look
                            (weapon:review 92)
  high_priority             what raises a hub alert / SOC incident: priority high (90), threat high (90, also with
                            qwen_analyzed off), a broken site rule (site_rule 85), a watch-list match (watched 85).
                            The NVR does not know about the hub's SOC incidents themselves; these are what opens them.
  ppe_violations            a PPE check found a violation (75)
  plates                    a vehicle whose plate was read (55)
Operator locks are always kept (LOCKED_SCORE) and never deleted.
"""
from __future__ import annotations

import copy
import json
import shutil

from . import zones
from .config import settings


def _db():
    # Opened on first use, not on import: own_reasons and EVENT_COLS serve tools whose dry run reads the database
    # read-only (snapshot_shrink) and must not open it for writing.
    from .db import db
    return db

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
        "weapon_check": True,
        "high_priority": True,
        "ppe_violations": True,
        "plates": True,
    },
    "rule_topics": ["FieldDetector", "LineDetector", "LoiteringDetector", "FieldInDetector",
                    "FieldOutDetector", "TamperDetector", "ObjectLeftDetector", "ObjectRemoveDetector"],
    "min_free_gb": 200,
}

LOCKED_SCORE = 1000.0
SCORES = {"weapon:confirmed": 95, "weapon:review": 92, "priority:high": 90, "threat:high": 90, "site_rule": 85,
          "watched": 85, "threat:medium": 80, "ppe_violation": 75, "threat:low": 70, "person+synopsis": 60,
          "plate": 55, "person": 50, "feedback": 45, "rule": 40, "qwen": 35, "vehicle_in_zone": 30}
WEAPON_VERDICTS = {"confirmed": "weapon:confirmed", "unclear": "weapon:review", "error": "weapon:review"}
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


def default_min_free_gb() -> int:
    """The free-space floor when the site has not set one: 200 GB on a large disk, 10% of a small one (never
    under 10 GB). A 250 GB SSD cannot spare 200 GB; with the fixed default it deleted continuous footage from
    the first day and alerted on every pass."""
    try:
        total_gb = shutil.disk_usage(settings.recordings_dir).total / 1e9
    except OSError:
        return DEFAULT_POLICY["min_free_gb"]
    return int(min(DEFAULT_POLICY["min_free_gb"], max(10, total_gb * 0.10)))


def site_policy() -> dict:
    stored = _db().get_setting("retention_policy") or {}
    p = _merge(DEFAULT_POLICY, stored)
    if stored.get("min_free_gb") is None:
        p["min_free_gb"] = default_min_free_gb()
    return p


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

    db = _db()
    events = db.all(f"SELECT {EVENT_COLS} FROM events WHERE camera_id=? AND start_ts <= ? AND COALESCE(end_ts, start_ts) >= ? "
                    "AND status != 'masked'", [camera["id"], end + pb, start - pa])
    noted = saved_notes(db, [e["id"] for e in events])
    for e in events:
        s, t = e["start_ts"] - pb, (e["end_ts"] or e["start_ts"]) + pa
        person = e["camera_class"] == "person"
        if e["status"] in ("open", "pending"):
            defer = True
        elif (person and e["status"] == "verified" and not e["synopsis"] and not e["error"]
              and "person" in settings.synopsis_labels):
            defer = True  # Qwen hasn't described it yet; its synopsis may matter for keeping
        for why in own_reasons(e, keep, e["id"] in noted, include_zones):
            items.append((s, t, why, SCORES[why]))

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


# The event columns own_reasons reads (keep_windows' query; retention.purge_rejected_media and snapshot_shrink too).
EVENT_COLS = ("id, camera_class, status, start_ts, end_ts, synopsis, threat, feedback, corrected_at, path, error, "
              "priority, watched, plate, policy IS NOT NULL AS broke_rule, json_extract(policy, '$.kind') AS rule_kind, "
              "json_extract(synopsis_json, '$.weapon_check.verdict') AS weapon, "
              "json_extract(detections, '$.ppe.verdict') AS ppe_verdict, json_extract(feedback, '$.verdict') AS fb_verdict")


def saved_notes(db, ids: list[int]) -> set[int]:
    """The events among `ids` with a saved clip-chat note (the feedback keep reason)."""
    out: set[int] = set()
    ids = [int(i) for i in ids if i is not None]
    for k in range(0, len(ids), 500):
        chunk = ids[k:k + 500]
        out |= {r["event_id"] for r in db.all("SELECT DISTINCT event_id FROM chat_messages WHERE saved=1 AND event_id IN "
                                                f"({','.join('?' * len(chunk))})", chunk)}
    return out


def own_reasons(e: dict, keep: dict, noted: bool = False, include_zones: list | None = None) -> list[str]:
    """Why this one event (an EVENT_COLS row) keeps its footage, by the keep rules that are on: a verified person, a
    synopsis (and its threat), operator feedback / a correction / a saved chat note, a verified vehicle in a
    detect-only zone, and event_reasons. Locks are not here (keep_windows reads the locks table)."""
    out = []
    person = e["camera_class"] == "person"
    if keep.get("person") and person and e["status"] == "verified":
        out.append("person+synopsis" if e["synopsis"] else "person")
    if keep.get("qwen_analyzed") and e["synopsis"]:
        threat = e["threat"] if e["threat"] in ("low", "medium", "high") else None
        out.append(f"threat:{threat}" if threat else "qwen")
    if keep.get("feedback") and (e["feedback"] or e["corrected_at"] or noted):
        out.append("feedback")
    if (keep.get("vehicles_in_detect_zones") and include_zones and e["camera_class"] == "vehicle"
            and e["status"] == "verified" and zones.path_allowed(json.loads(e["path"] or "[]"), include_zones)):
        out.append("vehicle_in_zone")
    return out + event_reasons(e, keep)


def event_reasons(e: dict, keep: dict) -> list[str]:
    """The newer keep reasons of one event row (keep_windows' query): a weapon check, high priority / threat / a broken
    site rule / a watch-list match, a PPE violation, a read plate. A false alarm (operator feedback) keeps none of these;
    its feedback reason still keeps the footage."""
    out = []
    if e["status"] in ("open", "pending", "masked"):
        return out
    if keep.get("weapon_check") and e.get("weapon") in WEAPON_VERDICTS:
        out.append(WEAPON_VERDICTS[e["weapon"]])
    if keep.get("high_priority") and e.get("fb_verdict") != "false_alarm":
        if e.get("priority") == "high":
            out.append("priority:high")
        if e.get("threat") == "high":
            out.append("threat:high")
        if e.get("broke_rule"):
            out.append("site_rule")
        if e.get("watched"):
            out.append("watched")
    if keep.get("ppe_violations") and (e.get("ppe_verdict") == "violation" or e.get("rule_kind") == "ppe"):
        out.append("ppe_violation")
    if keep.get("plates") and e.get("plate"):
        out.append("plate")
    return out


def is_locked(camera_id: str, start: float, end: float) -> bool:
    return _db().one("SELECT 1 FROM locks WHERE camera_id=? AND start_ts < ? AND end_ts > ? LIMIT 1",
                  [camera_id, end, start]) is not None
