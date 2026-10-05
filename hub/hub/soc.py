"""The SOC layer: one internal Security Operations Center that watches customers' Sites overnight.

Customers opt in per Site (locations.monitored). SOC operators and supervisors are hub-level roles (users.soc_role,
auth.soc_level) scoped to customers with at least one monitored Site (auth.membership widens their access there).
A monitored Site is armed by a weekly schedule in the Site's timezone, holidays and a manual override (armed_now);
only armed Sites will feed the SOC queue (stage 2). Disarming never touches what customers get: their alerts, push
and timelines are unchanged whether a Site is monitored or not.

This module holds the shared vocabulary (incident states, lanes, dispositions, four-eyes rules, SLA by priority) so
the incident engine, the routes and the UI agree on one set of codes, plus two small caches: which customers have a
monitored Site (read on every membership() check of a SOC user) and each Site's armed state.
"""
from __future__ import annotations

import datetime as dt
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa

from . import db

# ---------------------------------------------------------------- vocabulary

SOC_ROLES = ("operator", "supervisor")
SOC_RANK = {"operator": 1, "supervisor": 2}

STATES = ("new", "claimed", "pending_verify", "closed")
LANES = ("ring", "quiet")          # ring: rings until claimed; quiet: low priority, swept in bulk (AI triage is a lane)
PRIORITIES = ("low", "medium", "high")
PRIORITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}

# Disposition groups in the order an operator picks from (T / F / N chords in the workstation).
DISPOSITION_GROUPS = (("true_alarm", "True alarm", "t"), ("false_alarm", "False alarm", "f"), ("not_actionable", "Not actionable", "n"))
# code -> (group, label, needs_notes, selectable). `selectable` False = set by the system (sweep, expiry), never by hand.
DISPOSITIONS: dict[str, tuple[str, str, bool, bool]] = {
    "true_alarm_deterred": ("true_alarm", "Deterred (left after warning)", True, True),
    "true_alarm_dispatched": ("true_alarm", "Police / guard dispatched", True, True),
    "customer_notified": ("true_alarm", "Customer notified", True, True),
    "false_alarm": ("false_alarm", "False alarm (nothing there)", False, True),
    "nuisance": ("false_alarm", "Nuisance (animal, weather, lights)", False, True),
    "authorized": ("false_alarm", "Authorized person", False, True),
    "test": ("false_alarm", "Test", False, True),
    "no_action": ("not_actionable", "No action needed", False, True),
    "swept": ("not_actionable", "Swept (quiet lane)", False, False),
    "expired": ("not_actionable", "Expired unhandled", False, False),
}
TRUE_ALARM = frozenset(c for c, d in DISPOSITIONS.items() if d[0] == "true_alarm")
FALSE_ALARM = frozenset(c for c, d in DISPOSITIONS.items() if d[0] == "false_alarm")
# Four-eyes: a second person (a supervisor who is not the resolver) verifies before the incident closes. Every
# true-alarm disposition, and "no action" on a high-priority incident (the riskiest thing to wave through).
FOUR_EYES: dict[str, frozenset[str]] = {**{c: frozenset(PRIORITIES) for c in TRUE_ALARM}, "no_action": frozenset({"high"})}


def needs_four_eyes(disposition: str, priority: str) -> bool:
    return priority in FOUR_EYES.get(disposition, frozenset())


def dispositions_catalogue() -> dict:
    """GET /api/soc/dispositions: the groups in picking order, each disposition with its chord digit."""
    groups = []
    for gid, label, key in DISPOSITION_GROUPS:
        items = []
        for code, (g, lbl, notes, selectable) in DISPOSITIONS.items():
            if g != gid:
                continue
            items.append({"code": code, "label": lbl, "needs_notes": notes, "selectable": selectable,
                          "four_eyes": sorted(FOUR_EYES.get(code, ()), key=lambda p: -PRIORITY_RANK[p]),
                          "key": str(sum(1 for i in items if i["selectable"]) + 1) if selectable else None})
        groups.append({"id": gid, "label": label, "key": key, "dispositions": items})
    return {"groups": groups}


# SLA by incident priority: seconds to claim, seconds from claim to resolve, and the lane. None = no clock.
SLA: dict[str, dict] = {
    "high": {"claim_s": 60, "resolve_s": 600, "lane": "ring"},
    "medium": {"claim_s": 180, "resolve_s": 1200, "lane": "ring"},
    "low": {"claim_s": None, "resolve_s": None, "lane": "quiet"},
}
SLA_KV = "soc_sla"


def sla() -> dict[str, dict]:
    """SLA with the kv `soc_sla` override merged in ({priority: {claim_s?, resolve_s?, lane?}}). A malformed override
    is ignored field by field, so a bad PUT can never leave the queue without clocks."""
    out = {p: dict(v) for p, v in SLA.items()}
    row = db.one(sa.select(db.kv.c.value).where(db.kv.c.key == SLA_KV))
    over = row["value"] if row else None
    if isinstance(over, dict):
        for p, v in over.items():
            if p not in out or not isinstance(v, dict):
                continue
            for k in ("claim_s", "resolve_s"):
                if k in v and (v[k] is None or (isinstance(v[k], (int, float)) and not isinstance(v[k], bool) and 0 < v[k] <= 86400)):
                    out[p][k] = v[k]
            if v.get("lane") in LANES:
                out[p]["lane"] = v["lane"]
    return out


# ---------------------------------------------------------------- which customers are monitored

MONITORED_TTL_S = 15.0
ARMED_TTL_S = 30.0
_monitored: tuple[float, frozenset[str]] | None = None
_armed: dict[str, tuple[float, tuple[bool, str]]] = {}


def monitored_org_ids() -> frozenset[str]:
    """Customers with at least one monitored Site. Cached for 15 s: auth.membership() asks for every request a SOC
    user makes, and opting a Site in or out calls invalidate()."""
    global _monitored
    now = time.monotonic()
    if _monitored is not None and _monitored[0] > now:
        return _monitored[1]
    ids = frozenset(r["org_id"] for r in db.rows(sa.select(db.locations.c.org_id).where(db.locations.c.monitored.is_(True)).distinct()))
    _monitored = (now + MONITORED_TTL_S, ids)
    return ids


def org_monitored(org_id: str | None) -> bool:
    return bool(org_id) and org_id in monitored_org_ids()


def invalidate() -> None:
    """A Site's monitoring, schedule or override changed (or tests wrote rows directly)."""
    global _monitored
    _monitored = None
    _armed.clear()


# ---------------------------------------------------------------- arming
# Times are wall-clock in the Site's timezone (UTC when it has none or an unknown one), so a window keeps meaning
# "18:00 to 06:00 local" across daylight-saving changes. Weekly windows: [{dow, from, to}], dow 0..6 with Monday = 0
# (Python's weekday()); `to` <= `from` is an overnight window that ends on the next day (from == to: 24 hours).
# A window covers [from, to): armed at `from`, disarmed at `to`. Precedence: an unexpired override, then a holiday
# on today's local date, then the weekly schedule; an empty schedule on a monitored Site means armed around the clock.
# A holiday governs its whole local date: armed all day, disarmed all day, or (with from/to) armed only inside that
# window on that date (an overnight from/to there means before `to` or from `from` on that date).

def tz_of(loc: dict) -> dt.tzinfo:
    try:
        return ZoneInfo(loc.get("timezone") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return dt.timezone.utc


def valid_timezone(name: str | None) -> bool:
    if not name:
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def parse_hhmm(v) -> int | None:
    """"HH:MM" -> minutes after midnight (00:00..23:59), else None."""
    if not isinstance(v, str) or len(v) != 5 or v[2] != ":" or not (v[:2].isdigit() and v[3:].isdigit()):
        return None
    h, m = int(v[:2]), int(v[3:])
    return h * 60 + m if h < 24 and m < 60 else None


def _in_window(minute: int, f: int, t: int) -> tuple[bool, bool]:
    """(armed by today's part, armed by yesterday's spill-over) for a window f..t at this minute of the day."""
    if t > f:
        return f <= minute < t, False
    return minute >= f, minute < t   # overnight (or 24 h when f == t)


def _schedule_armed(schedule: list, local: dt.datetime) -> bool:
    minute = local.hour * 60 + local.minute
    wd = local.weekday()
    for w in schedule or []:
        if not isinstance(w, dict):
            continue
        f, t = parse_hhmm(w.get("from")), parse_hhmm(w.get("to"))
        days = {d for d in (w.get("dow") or []) if isinstance(d, int)}
        if f is None or t is None:
            continue
        today, spill = _in_window(minute, f, t)
        if (today and wd in days) or (spill and (wd - 1) % 7 in days):
            return True
    return False


def _holiday(loc: dict, local: dt.datetime) -> dict | None:
    day = local.date().isoformat()
    for h in loc.get("arm_holidays") or []:
        if isinstance(h, dict) and h.get("date") == day:
            return h
    return None


def _holiday_armed(h: dict, local: dt.datetime) -> bool:
    if not h.get("armed"):
        return False
    f, t = parse_hhmm(h.get("from")), parse_hhmm(h.get("to"))
    if f is None or t is None:
        return True
    today, before = _in_window(local.hour * 60 + local.minute, f, t)
    return today or before


def active_override(loc: dict, now: float) -> dict | None:
    o = loc.get("arm_override")
    if isinstance(o, dict) and o.get("mode") in ("arm", "disarm") and float(o.get("until") or 0) > now:
        return o
    return None


def compute_armed(loc: dict, now: float) -> tuple[bool, str]:
    """Pure: (armed, reason) at `now`. reason: unmonitored | override | holiday | schedule | disarmed_schedule | always."""
    if not loc.get("monitored"):
        return False, "unmonitored"
    o = active_override(loc, now)
    if o:
        return o["mode"] == "arm", "override"
    local = dt.datetime.fromtimestamp(now, tz_of(loc))
    h = _holiday(loc, local)
    if h is not None:
        return _holiday_armed(h, local), "holiday"
    schedule = [w for w in (loc.get("arm_schedule") or []) if isinstance(w, dict)]
    if not schedule:
        return True, "always"
    return (True, "schedule") if _schedule_armed(schedule, local) else (False, "disarmed_schedule")


def armed_now(loc: dict, now: float | None = None) -> tuple[bool, str]:
    """(armed, reason) for a Site row. With `now` given it is pure (tests, next_change); without, the answer is
    cached per Site for up to 30 s, never past the next change, and invalidate() drops it when the config changes."""
    if now is not None:
        return compute_armed(loc, now)
    t = time.time()
    hit = _armed.get(loc.get("id") or "")
    if hit and hit[0] > t:
        return hit[1]
    res = compute_armed(loc, t)
    nxt = next_change(loc, t)
    until = min(t + ARMED_TTL_S, nxt["at"]) if nxt else t + ARMED_TTL_S
    if loc.get("id"):
        _armed[loc["id"]] = (until, res)
    return res


NEXT_CHANGE_DAYS = 8          # the weekly schedule repeats, so a change within 8 days or none at all
HOLIDAY_HORIZON_DAYS = 400    # holidays are dated: look further ahead for them


def _local_ts(tz: dt.tzinfo, day: dt.date, minute: int) -> float:
    return dt.datetime(day.year, day.month, day.day, minute // 60, minute % 60, tzinfo=tz).timestamp()


def next_change(loc: dict, now: float | None = None) -> dict | None:
    """The next moment the armed state flips: {"at": epoch seconds, "armed": the state from then on}, or None when
    it never changes within the horizon (unmonitored, or armed around the clock with no dated exceptions).
    Evaluates compute_armed at every boundary (window edges, local midnights of holidays and the days around them,
    the override's end) after `now`, in time order, and returns the first that differs from the current state."""
    now = time.time() if now is None else now
    if not loc.get("monitored"):
        return None
    tz = tz_of(loc)
    cur = compute_armed(loc, now)[0]
    today = dt.datetime.fromtimestamp(now, tz).date()
    cands: set[float] = set()
    o = active_override(loc, now)
    if o:
        cands.add(float(o["until"]))
    edges = set()
    for w in loc.get("arm_schedule") or []:
        if isinstance(w, dict):
            edges |= {m for m in (parse_hhmm(w.get("from")), parse_hhmm(w.get("to"))) if m is not None}
    for i in range(NEXT_CHANGE_DAYS + 1):
        day = today + dt.timedelta(days=i)
        cands.add(_local_ts(tz, day, 0))
        for m in edges:
            cands.add(_local_ts(tz, day, m))
    for h in loc.get("arm_holidays") or []:
        try:
            day = dt.date.fromisoformat(str(h.get("date")))
        except (ValueError, AttributeError):
            continue
        if not (today <= day <= today + dt.timedelta(days=HOLIDAY_HORIZON_DAYS)):
            continue
        cands |= {_local_ts(tz, day, 0), _local_ts(tz, day + dt.timedelta(days=1), 0)}
        for m in (parse_hhmm(h.get("from")), parse_hhmm(h.get("to"))):
            if m is not None:
                cands.add(_local_ts(tz, day, m))
        for m in edges:   # the weekly windows resume on the day after a holiday
            cands.add(_local_ts(tz, day + dt.timedelta(days=1), m))
    for c in sorted(x for x in cands if x > now):
        state = compute_armed(loc, c)[0]
        if state != cur:
            return {"at": c, "armed": state}
    return None
