"""Learn what's normal per camera, and score how unusual each event is.

The baseline is built from the last HISTORY_DAYS of verified events (false alarms excluded), per
(camera, label):
- time: for each local hour on weekdays / weekend days, on how many observed days that label appeared;
- place: a GX x GY grid of where tracks go (box bottom-centre, i.e. roughly where they stand), as the
  fraction of events that touched each cell;
- dwell: the sorted visit durations.

Moved cameras (fleet actions) bring their baseline with them: `seed()` stores the source site's entry under the
camera's new id (setting `baseline_seeds`) and every rebuild keeps the seed for that camera until this site's own
history for it covers as many days, so a moved camera is not "learning" again for a week.

score(event) -> {score, parts, reasons, learning}. Each part is 0..1 (1 = never seen like this) and only
counts once there is enough history. priority() combines the unusualness with Qwen's threat level; an
operator's correction to threat "none" (or a false-alarm verdict) always wins.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from collections import defaultdict

import numpy as np

from .db import db

log = logging.getLogger("nvr.baseline")

HISTORY_DAYS = 28
MIN_DAYS = 7            # the time profile needs a week of history
MIN_DAYTYPE_DAYS = 2    # ... and at least 2 days of that type (weekday/weekend)
MIN_EVENTS = 20         # place and dwell need this many past events
GX, GY = 16, 9
RARE_CELL = 0.02        # a cell touched by fewer than 2% of past events is a rare spot
MIN_DWELL_S = 60        # short visits are never unusual for their length
REASON_MIN = 0.8
LEVELS = ["none", "low", "medium", "high"]
RANK = {k: i for i, k in enumerate(LEVELS)}
PRIORITY_LOW, PRIORITY_MEDIUM = 0.75, 0.9
PLURAL = {"person": "People", "vehicle": "Vehicles"}

_cache: dict | None = None


def _local(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts)


def _daytype(d: dt.date) -> int:
    return 1 if d.weekday() >= 5 else 0


def _cells(path: list) -> set[tuple[int, int]]:
    """Grid cells a track touched, using each box's bottom-centre (normalised coordinates)."""
    out = set()
    for p in path or []:
        cx, bottom = (p[1] + p[3]) / 2, p[4]
        out.add((min(GY - 1, max(0, int(bottom * GY))), min(GX - 1, max(0, int(cx * GX)))))
    return out


def _counted(e: dict) -> bool:
    """Does this event belong in the baseline (verified, not a false alarm)?"""
    fb = e.get("feedback") or {}
    if isinstance(fb, str):
        fb = json.loads(fb)
    return e.get("status") == "verified" and fb.get("verdict") != "false_alarm" and not e.get("ptz_preset")


def rebuild(now: float | None = None) -> dict:
    global _cache
    now = now or time.time()
    since = now - HISTORY_DAYS * 86400
    firsts = {r["camera_id"]: r["t"] for r in db.all("SELECT camera_id, MIN(start_ts) AS t FROM events GROUP BY camera_id")}
    rows = db.all("SELECT camera_id, camera_class, status, start_ts, end_ts, path, feedback, ptz_preset FROM events "
                  "WHERE status='verified' AND start_ts >= ? AND start_ts < ?", [since, now])
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        if _counted(r):
            groups[(r["camera_id"], r["camera_class"])].append(r)

    cams: dict[str, dict] = {}
    for cam, first in firsts.items():
        start = max(first, since)
        days = [(_local(start).date() + dt.timedelta(days=i)) for i in range((_local(now).date() - _local(start).date()).days + 1)]
        cams[cam] = {"first_ts": start, "days": round((now - start) / 86400, 2),
                     "daytype_days": [sum(1 for d in days if _daytype(d) == k) for k in (0, 1)], "labels": {}}

    for (cam, cls), evs in groups.items():
        slot_days: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))  # "type-hour" -> {date: n}
        grid = np.zeros((GY, GX))
        durations = []
        for e in evs:
            t = _local(e["start_ts"])
            slot_days[f"{_daytype(t.date())}-{t.hour}"][t.date().isoformat()] += 1
            for (y, x) in _cells(json.loads(e["path"]) if e["path"] else []):
                grid[y, x] += 1
            durations.append(max(0.0, (e["end_ts"] or e["start_ts"]) - e["start_ts"]))
        cams.setdefault(cam, {"first_ts": since, "days": 0, "daytype_days": [0, 0], "labels": {}})
        cams[cam]["labels"][cls] = {"events": len(evs), "slot_days": {k: dict(v) for k, v in slot_days.items()},
                                    "grid": grid.tolist(), "durations": sorted(durations)}
    seeds = db.get_setting("baseline_seeds") or {}
    for cam, sd in list(seeds.items()):
        if now - float(sd.get("seeded_at") or 0) > HISTORY_DAYS * 86400:
            seeds.pop(cam)   # by now this site has a full window of its own
            continue
        if cam not in cams or cams[cam]["days"] < float(sd["entry"].get("days") or 0):
            cams[cam] = {**sd["entry"], "seeded_from": sd.get("source")}
    db.set_setting("baseline_seeds", seeds)
    _cache = {"built_at": now, "since": since, "cameras": cams}
    db.set_setting("baseline", _cache)
    log.info("baseline rebuilt: %s", {c: {l: v["events"] for l, v in d["labels"].items()} for c, d in cams.items()})
    return _cache


def current() -> dict:
    global _cache
    if _cache is None:
        _cache = db.get_setting("baseline") or rebuild()
    return _cache


def entry(camera_id: str) -> dict | None:
    """This camera's learned "what's normal" (for handing it to another site with the camera)."""
    return (current().get("cameras") or {}).get(camera_id)


def seed(camera_id: str, learned: dict, source: str = "") -> bool:
    """Adopt another site's baseline for a camera moved here, unless this site already knows it better
    (more days of its own history). Non-destructive: the site's own entries are never replaced by a thinner one."""
    global _cache
    if not isinstance(learned, dict) or not isinstance(learned.get("labels"), dict):
        return False
    base = current()
    own = (base.get("cameras") or {}).get(camera_id)
    if own and float(own.get("days") or 0) >= float(learned.get("days") or 0):
        return False
    seeds = db.get_setting("baseline_seeds") or {}
    seeds[camera_id] = {"entry": learned, "seeded_at": time.time(), "source": source[:120]}
    db.set_setting("baseline_seeds", seeds)
    base.setdefault("cameras", {})[camera_id] = {**learned, "seeded_from": source[:120]}
    _cache = base
    db.set_setting("baseline", base)
    return True


def _hour_label(h: int) -> str:
    return "midnight" if h == 0 else "noon" if h == 12 else f"{h % 12} {'am' if h < 12 else 'pm'}"


def score(e: dict, base: dict | None = None) -> dict:
    """How unusual is this event for its camera and label? Excludes the event itself from the baseline."""
    base = base or current()
    cam = base["cameras"].get(e["camera_id"])
    cls = e.get("camera_class") or ""
    lab = (cam or {}).get("labels", {}).get(cls)
    t = _local(e["start_ts"])
    dur = max(0.0, (e.get("end_ts") or e["start_ts"]) - e["start_ts"])
    path = e.get("path") or []
    if isinstance(path, str):
        path = json.loads(path)
    cells = _cells(path)
    # is this event part of the baseline it's being scored against? then leave it out
    self_in = _counted(e) and base["since"] <= e["start_ts"] < base["built_at"] and lab is not None
    parts, reasons = {}, []
    noun = PLURAL.get(cls, f"{cls.capitalize()}s")

    if cam and cam["days"] >= MIN_DAYS:
        dtype = _daytype(t.date())
        ndays = cam["daytype_days"][dtype]
        if ndays >= MIN_DAYTYPE_DAYS:
            sd = (lab or {}).get("slot_days", {})
            def seen(h: int) -> float:
                days = dict(sd.get(f"{dtype}-{h % 24}", {}))
                if self_in and h % 24 == t.hour:
                    d = t.date().isoformat()
                    days[d] = days.get(d, 0) - 1
                    if days[d] <= 0:
                        days.pop(d)
                return len(days)
            here = seen(t.hour)
            blended = min(ndays, here + 0.5 * (seen(t.hour - 1) + seen(t.hour + 1)))
            p = (blended + 0.5) / (ndays + 1)
            parts["time"] = round(max(0.0, 1 - min(1.0, p)), 3)
            if parts["time"] >= REASON_MIN:
                kind = "weekend days" if dtype else "weekdays"
                reasons.append(f"{noun} are seen on this camera around {_hour_label(t.hour)} on {int(here)} of the last {ndays} {kind}")

    n = (lab or {}).get("events", 0) - (1 if self_in else 0)
    if lab and n >= MIN_EVENTS:
        grid = np.array(lab["grid"])
        if self_in:
            for (y, x) in cells:
                grid[y, x] -= 1
        if cells:
            frac = np.clip(grid, 0, None) / n
            rare = sum(1 for (y, x) in cells if frac[y, x] < RARE_CELL)
            parts["place"] = round(rare / len(cells), 3)
            if parts["place"] >= REASON_MIN:
                reasons.append(f"Went where {noun.lower()} rarely go on this camera")
        durs = list(lab["durations"])
        if self_in and dur in durs:
            durs.remove(dur)
        if dur >= MIN_DWELL_S and durs:
            shorter = int(np.searchsorted(durs, dur, side="left"))
            parts["dwell"] = round(shorter / len(durs), 3)
            if parts["dwell"] >= REASON_MIN:
                reasons.append(f"Stayed {dur / 60:.0f} min, longer than {parts['dwell'] * 100:.0f}% of visits")

    return {"score": max(parts.values(), default=0.0), "parts": parts, "reasons": reasons,
            "learning": not parts, "baseline_at": base["built_at"]}


def priority(e: dict, anomaly: float | None) -> str | None:
    """max(Qwen/operator threat, level from unusualness); an operator's 'none' or a false alarm caps it."""
    if e.get("status") != "verified":
        return None
    fb = e.get("feedback") or {}
    if isinstance(fb, str):
        fb = json.loads(fb)
    if fb.get("verdict") == "false_alarm" or (e.get("corrected_at") and e.get("threat") == "none"):
        return "none"
    a = anomaly or 0.0
    level = RANK["medium"] if a >= PRIORITY_MEDIUM else RANK["low"] if a >= PRIORITY_LOW else RANK["none"]
    if e.get("watched"):  # a person/vehicle the operator asked to be told about
        level = max(level, RANK["medium"])
    pol = e.get("policy")
    if isinstance(pol, str):
        pol = json.loads(pol)
    if pol:  # a broken site rule (policy.py) carries its own priority
        level = max(level, RANK.get(pol.get("priority") or "high", RANK["high"]))
    return LEVELS[max(level, RANK.get(e.get("threat") or "", 0))]


def apply(event_id: int, rescore: bool = True) -> dict | None:
    """Score the event (unless rescore=False and it already has a score) and store score + priority."""
    e = db.event(event_id)
    if not e or e["status"] != "verified" or e.get("ptz_preset"):
        return None  # a PTZ camera turned away: the profile describes the home view
    a = e.get("anomaly_json")
    if rescore or not a:
        a = score(e)
    db.update_event(event_id, anomaly=a["score"], anomaly_json=a, priority=priority(e, a["score"]))
    return a


def backfill(only_missing: bool = True) -> int:
    """Score verified events with no score yet, plus ones scored while their camera was still learning."""
    rows = db.all("SELECT id, anomaly_json FROM events WHERE status='verified' AND ptz_preset IS NULL AND start_ts >= ?",
                  [time.time() - HISTORY_DAYS * 86400])
    n = 0
    for r in rows:
        a = json.loads(r["anomaly_json"]) if r["anomaly_json"] else None
        if only_missing and a and not a.get("learning"):
            continue
        apply(r["id"])
        n += 1
    log.info("baseline: scored %d events", n)
    return n


def status() -> list[dict]:
    base = current()
    out = []
    for cam, d in base["cameras"].items():
        labels = {k: v["events"] for k, v in d["labels"].items()}
        active = d["days"] >= MIN_DAYS or any(n >= MIN_EVENTS for n in labels.values())
        out.append({"camera_id": cam, "days": d["days"], "events": labels, "learning": not active,
                    "time_active": d["days"] >= MIN_DAYS, "built_at": base["built_at"]})
    return out


async def baseline_loop(pipeline) -> None:
    """Rebuild at startup and nightly at 03:00, then (re)score events that need it."""
    import asyncio
    await asyncio.to_thread(rebuild)
    await asyncio.to_thread(backfill)
    while True:
        now = _local(time.time())
        nxt = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            await asyncio.to_thread(rebuild)
            await asyncio.to_thread(backfill)
        except Exception:
            log.exception("baseline rebuild failed")
