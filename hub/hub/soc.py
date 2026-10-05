"""The SOC layer: one internal Security Operations Center that watches customers' Sites overnight.

Customers opt in per Site (locations.monitored). SOC operators and supervisors are hub-level roles (users.soc_role,
auth.soc_level) scoped to customers with at least one monitored Site (auth.membership widens their access there).
A monitored Site is armed by a weekly schedule in the Site's timezone, holidays and a manual override (armed_now);
only armed Sites feed the SOC queue. Disarming never touches what customers get: their alerts, push and timelines
are unchanged whether a Site is monitored or not.

This module holds the shared vocabulary (incident states, lanes, dispositions, four-eyes rules, SLA by priority) so
the incident engine, the routes and the UI agree on one set of codes, two small caches (which customers have a
monitored Site, read on every membership() check of a SOC user, and each Site's armed state), and the incident
engine: the feed from the tunnel (on_event, on_attention -> ingest), ownership (claim, release, takeover, handoff,
resolve, verify, sweep, promote) as single conditional UPDATEs so two operators can never both win, the append-only
incident log, operator presence, and the in-process broadcast that /api/soc/ws relays.

It never imports agents (agents imports this module to call the feed): the few functions that reach a site through
the tunnel import the registry lazily.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa

from . import cameras, db

log = logging.getLogger("hub.soc")

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


# ================================================================ incidents
# An incident is the SOC's record of one or more events at one Site. Events arrive from the tunnel (on_event for
# live publishes, on_attention for the heartbeat's attention list, which catches events published while a tunnel
# was down) and are grouped: an event joins the Site's newest open incident if that incident saw an event within the
# Site's grouping window, else it opens a new one. Every state change is one conditional UPDATE (WHERE state = ...),
# so two operators clicking Claim at once get one winner and one 409, and each writes an incident_log row in the
# same transaction. Escalation (stage 3) reads sla_due_at / resolve_due_at / next_escalation_at / escalation_level,
# which the functions below keep current, so a pure escalate_once(now) can be added without touching them.

GROUP_WINDOW_S = 600          # default grouping window (locations.soc_group_minutes overrides it per Site)
MAX_EVENT_AGE_S = 900         # sites re-publish old events (feedback, locks, synopsis reruns): those never open incidents
OPEN_STATES = ("new", "claimed", "pending_verify")
ACTIVE_STATES = ("new", "claimed")   # new events still join these; a resolved incident awaiting verification is done
PRESENCE = ("available", "engaged", "break", "offline")
ON_SHIFT_S = 120              # presence older than this counts as offline (the socket and the UI refresh it every 30 s)
SOUND_REPEAT_S = 10
CALL_OUTCOMES = ("spoke", "voicemail", "no_answer", "busy", "dispatched", "refused")
# Resolutions that tell the site its detection was wrong, so baseline.priority learns. "authorized" and "test" were
# real people, correctly detected: sending false_alarm for them would teach the site to ignore people.
FEEDBACK_DISPOSITIONS = frozenset({"false_alarm", "nuisance"})


class SocError(Exception):
    """Refusals the routes turn into HTTP errors with this status and message."""
    status = 400


class ConflictError(SocError):
    status = 409


class Forbidden(SocError):
    status = 403


class NotFound(SocError):
    status = 404


class Invalid(SocError):
    status = 422


class Unavailable(SocError):
    status = 503


class SiteError(SocError):
    status = 502


def norm_priority(p) -> str:
    """Site priority -> incident priority. "none" and anything unknown are low: every verified event at an armed Site
    enters the queue (AI triage decides the lane, never whether the SOC sees it)."""
    return p if p in PRIORITIES else "low"


def rank(p) -> int:
    return PRIORITY_RANK.get(p, 0)


def _ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    return f"{s // 3600} h {s % 3600 // 60} min"


def _uid(u: dict | None) -> str | None:
    return u.get("id") if u else None


def level_of(u: dict | None) -> str | None:
    """auth.soc_level without importing auth (auth imports this module)."""
    if not u:
        return None
    if u.get("is_super"):
        return "supervisor"
    return u.get("soc_role") if u.get("soc_role") in SOC_ROLES else None


def _log(c, iid: int, u: dict | None, action: str, detail: dict | None = None, now: float | None = None) -> None:
    c.execute(db.incident_log.insert().values(incident_id=iid, ts=now or time.time(), user_id=_uid(u),
                                              user_email=u.get("email") if u else None, action=action, detail=detail or {}))


def _load(c, iid: int) -> dict:
    row = c.execute(sa.select(db.incidents).where(db.incidents.c.id == iid)).mappings().first()
    if row is None:
        raise NotFound("unknown incident")
    return dict(row)


# ---------------------------------------------------------------- tags: what the queue shows about an incident

def _emails(ids) -> dict[str, str]:
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    return {r["id"]: r["email"] for r in db.rows(sa.select(db.users.c.id, db.users.c.email).where(db.users.c.id.in_(ids)))}


USER_FIELDS = ("claimed_by", "resolved_by", "four_eyes_by", "assigned_by")


def tag(rows: list[dict]) -> list[dict]:
    """Full incident rows plus what a queue needs without more lookups: customer and Site names, the Site's timezone,
    the servers and cameras involved (from incident_events), and the emails behind the user ids. Batched."""
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    orgs = {r["id"]: r["name"] for r in db.rows(sa.select(db.orgs.c.id, db.orgs.c.name).where(db.orgs.c.id.in_(list({r["org_id"] for r in rows}))))}
    locs = {r["id"]: r for r in db.rows(sa.select(db.locations.c.id, db.locations.c.name, db.locations.c.timezone)
                                        .where(db.locations.c.id.in_(list({r["location_id"] for r in rows}))))}
    ie = db.incident_events
    evs = db.rows(sa.select(ie.c.incident_id, ie.c.server_id, ie.c.camera_id).where(ie.c.incident_id.in_(ids)).order_by(ie.c.id))
    srv_ids = {e["server_id"] for e in evs}
    srv_names = {r["id"]: r["name"] for r in db.rows(sa.select(db.sites.c.id, db.sites.c.name).where(db.sites.c.id.in_(list(srv_ids))))} if srv_ids else {}
    cam_names = {(r["server_id"], r["camera_id"]): r["name"] for r in db.rows(
        sa.select(db.cameras.c.server_id, db.cameras.c.camera_id, db.cameras.c.name).where(db.cameras.c.server_id.in_(list(srv_ids))))} if srv_ids else {}
    per: dict[int, tuple[dict, dict]] = {i: ({}, {}) for i in ids}
    for e in evs:
        servers, cams = per[e["incident_id"]]
        servers.setdefault(e["server_id"], {"id": e["server_id"], "name": srv_names.get(e["server_id"], e["server_id"])})
        if e["camera_id"]:
            k = (e["server_id"], e["camera_id"])
            cams.setdefault(k, {"server_id": k[0], "camera_id": k[1], "name": cam_names.get(k) or k[1]})
    emails = _emails(r.get(k) for r in rows for k in USER_FIELDS)
    out = []
    for r in rows:
        loc = locs.get(r["location_id"]) or {}
        servers, cams = per[r["id"]]
        out.append({**r, "org_name": orgs.get(r["org_id"]), "location_name": loc.get("name"), "location_timezone": loc.get("timezone"),
                    "servers": list(servers.values()), "cameras": list(cams.values()),
                    **{f"{k}_email": emails.get(r.get(k)) for k in USER_FIELDS}})
    return out


def get(iid: int) -> dict:
    row = db.one(sa.select(db.incidents).where(db.incidents.c.id == iid))
    if row is None:
        raise NotFound("unknown incident")
    return tag([row])[0]


PRIORITY_ORDER = sa.case({"high": 3, "medium": 2, "low": 1}, value=db.incidents.c.priority, else_=0)


def query(states=OPEN_STATES, lane: str | None = None, org_id: str | None = None, location_id: str | None = None,
          since: float | None = None, limit: int = 200) -> list[dict]:
    """The queue: by priority (highest first), then oldest first, tagged."""
    t = db.incidents
    q = sa.select(t).where(t.c.state.in_(list(states))).order_by(PRIORITY_ORDER.desc(), t.c.opened_at, t.c.id).limit(limit)
    if lane:
        q = q.where(t.c.lane == lane)
    if org_id:
        q = q.where(t.c.org_id == org_id)
    if location_id:
        q = q.where(t.c.location_id == location_id)
    if since is not None:
        q = q.where(t.c.opened_at >= since)
    return tag(db.rows(q))


def events_of(iid: int) -> list[dict]:
    """The incident's events with their server and camera names (the workstation opens each in the Timeline)."""
    ie = db.incident_events
    evs = db.rows(sa.select(ie).where(ie.c.incident_id == iid).order_by(ie.c.ts, ie.c.id))
    srv_ids = {e["server_id"] for e in evs}
    if not srv_ids:
        return []
    srv_names = {r["id"]: r["name"] for r in db.rows(sa.select(db.sites.c.id, db.sites.c.name).where(db.sites.c.id.in_(list(srv_ids))))}
    cam_names = {(r["server_id"], r["camera_id"]): r["name"] for r in db.rows(
        sa.select(db.cameras.c.server_id, db.cameras.c.camera_id, db.cameras.c.name).where(db.cameras.c.server_id.in_(list(srv_ids))))}
    return [{**e, "server_name": srv_names.get(e["server_id"], e["server_id"]),
             "camera_name": cam_names.get((e["server_id"], e["camera_id"])) or e["camera_id"]} for e in evs]


def log_of(iids) -> dict[int, list[dict]]:
    ids = [iids] if isinstance(iids, int) else list(iids)
    out: dict[int, list[dict]] = {i: [] for i in ids}
    if ids:
        t = db.incident_log
        for r in db.rows(sa.select(t).where(t.c.incident_id.in_(ids)).order_by(t.c.ts, t.c.id)):
            out[r["incident_id"]].append(r)
    return out


def sop_progress(log_rows: list[dict]) -> dict[tuple[int, str], dict]:
    """Checklist state from the log: the latest `sop` row per (procedure, step) wins, so unticking is a row too."""
    out: dict[tuple[int, str], dict] = {}
    for r in log_rows:
        d = r.get("detail") or {}
        if r["action"] == "sop" and isinstance(d, dict) and d.get("procedure_id") is not None and d.get("step_id"):
            out[(int(d["procedure_id"]), str(d["step_id"]))] = {"done": bool(d.get("done")), "by": r.get("user_email"),
                                                                "at": r["ts"], "note": d.get("note")}
    return out


# ---------------------------------------------------------------- broadcast (/api/soc/ws)
# One set of queues for the whole SOC (incidents span customers; only SOC staff connect). Producers run on the event
# loop (the tunnel, routes) or, in tests, on another thread: each queue remembers its loop and is fed thread-safely.

subscribers: set[asyncio.Queue] = set()
_loops: dict[asyncio.Queue, asyncio.AbstractEventLoop] = {}


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    subscribers.add(q)
    _loops[q] = asyncio.get_running_loop()
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    subscribers.discard(q)
    _loops.pop(q, None)


def broadcast(msg: dict) -> None:
    """Slow readers are skipped, never awaited (the tunnel calls this)."""
    try:
        cur = asyncio.get_running_loop()
    except RuntimeError:
        cur = None
    for q in list(subscribers):
        if q.qsize() >= 100:
            continue
        lp = _loops.get(q)
        if lp is None or lp is cur:
            q.put_nowait(msg)
        elif not lp.is_closed():
            lp.call_soon_threadsafe(q.put_nowait, msg)


def ring_state() -> tuple[int, str | None]:
    """(unclaimed incidents in the ringing lane, their top priority)."""
    t = db.incidents
    rows = db.rows(sa.select(t.c.priority, sa.func.count().label("n")).where(t.c.state == "new", t.c.lane == "ring").group_by(t.c.priority))
    n = sum(r["n"] for r in rows)
    top = max((r["priority"] for r in rows), key=rank, default=None)
    return n, top


def sound() -> tuple[dict, int]:
    """The server decides the ringer: ring while any ringing-lane incident is unclaimed, repeat every 10 s."""
    n, top = ring_state()
    return {"ring": n > 0, "repeat_s": SOUND_REPEAT_S, "priority": top}, n


def frame(kind: str, **body) -> dict:
    snd, n = sound()
    return {"type": kind, **body, "sound": snd, "ring_count": n, "now": time.time()}


def _emit(kind: str, incident: dict, **extra) -> dict:
    broadcast(frame(kind, incident=incident, **extra))
    return incident


def _spawn(fn, *args) -> None:
    """Run a coroutine function in the background on the running loop (push). No loop (a test calling ingest from a
    plain thread): skipped. Failures are logged, never raised into the tunnel."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def run():
        try:
            await fn(*args)
        except Exception:
            log.exception("soc background task %s", getattr(fn, "__name__", fn))
    loop.create_task(run())


# ---------------------------------------------------------------- the feed

def _title(c, server_id: str, camera_id: str | None, ev: dict) -> str:
    name = None
    if camera_id:
        row = c.execute(sa.select(db.cameras.c.name).where(db.cameras.c.server_id == server_id, db.cameras.c.camera_id == camera_id)).first()
        name = (row.name if row else None) or camera_id
    head = " · ".join(x for x in (name, ev.get("label")) if x)
    syn = (ev.get("synopsis") or "").strip()
    return (f"{head}: {syn}" if head and syn else head or syn or "Event")[:200]


def _raise(c, cur: dict, prio: str, now: float, rules: dict, u: dict | None = None) -> dict:
    """Values that raise an incident to `prio` (priority only ever rises), and its log row. Quiet -> ring starts the
    claim clock and the escalation ladder afresh; a rise inside the ringing lane only ever tightens the claim clock."""
    if rank(prio) <= rank(cur["priority"]):
        return {}
    vals: dict = {"priority": prio}
    rule = rules.get(prio) or {}
    claim_s = rule.get("claim_s")
    if cur["lane"] == "quiet" and rule.get("lane") == "ring":
        due = now + claim_s if claim_s and cur["state"] == "new" else None
        vals |= {"lane": "ring", "sla_due_at": due, "escalation_level": 0, "next_escalation_at": due}
    elif cur["lane"] == "ring" and cur["state"] == "new" and claim_s:
        due = now + claim_s
        if cur.get("sla_due_at") is None or due < cur["sla_due_at"]:
            vals["sla_due_at"] = due
            if not cur.get("escalation_level"):
                vals["next_escalation_at"] = due
    _log(c, cur["id"], u, "priority_raised", {"from": cur["priority"], "to": prio, "lane": vals.get("lane", cur["lane"])}, now)
    return vals


def _event_detail(ev: dict) -> dict:
    return {"label": ev.get("label"), "synopsis": (ev.get("synopsis") or "")[:300] or None, "policy": ev.get("policy"),
            "watched": ev.get("watched"), "start_ts": ev.get("start_ts")}


def ingest(site: dict, loc: dict, ev: dict, now: float | None = None, source: str = "event") -> tuple[str, dict] | None:
    """One event at a monitored, armed Site into the queue, in one transaction. Returns (what happened, the tagged
    incident) with what = opened | event_added | updated (a re-publish raised the priority or brought the first
    synopsis), or None when nothing changed (a plain re-publish, an event of a resolved incident).

    `ev`: {id, camera_id, priority, label, start_ts, synopsis, policy, watched} (alerts.on_event's shape). Callers
    (on_event, on_attention) have already checked monitored / armed / camera enabled."""
    now = time.time() if now is None else now
    eid = str(ev.get("id") if ev.get("id") is not None else "")[:40]
    if not eid:
        return None
    prio = norm_priority(ev.get("priority"))
    cam = str(ev["camera_id"])[:64] if ev.get("camera_id") not in (None, "") else None
    detail = _event_detail(ev)
    rules = sla()
    window = (loc.get("soc_group_minutes") or 0) * 60 or GROUP_WINDOW_S
    t, ie = db.incidents, db.incident_events
    raised_high = False
    try:
        with db.engine().begin() as c:
            # select-then-insert: the common re-publish is answered by this read; the unique constraint is the backstop
            have = c.execute(sa.select(ie).where(ie.c.server_id == site["id"], ie.c.event_id == eid)).mappings().first()
            if have is not None:
                # the same event again (synopsis arrived, feedback, lock, the heartbeat's attention list): never a new
                # row, but a higher priority or a first synopsis still updates its open incident
                cur = _load(c, have["incident_id"])
                if cur["state"] not in ACTIVE_STATES:
                    return None
                iid = cur["id"]
                ev_vals: dict = {}
                if rank(prio) > rank(have["priority"]):
                    ev_vals["priority"] = prio
                old = have["detail"] if isinstance(have["detail"], dict) else {}
                if detail.get("synopsis") and not old.get("synopsis"):
                    ev_vals["detail"] = {**old, **{k: v for k, v in detail.items() if v is not None}}
                if not ev_vals:
                    return None
                c.execute(sa.update(ie).where(ie.c.id == have["id"]).values(**ev_vals))
                vals = _raise(c, cur, prio, now, rules)
                raised_high = vals.get("priority") == "high"
                first = c.execute(sa.select(ie.c.id).where(ie.c.incident_id == iid).order_by(ie.c.id).limit(1)).first()
                if "detail" in ev_vals and first and first.id == have["id"]:
                    vals["title"] = _title(c, site["id"], cam, {**ev, **ev_vals["detail"]})
                c.execute(sa.update(t).where(t.c.id == iid).values(**vals, updated_at=now))
                what = "updated"
            else:
                cur = c.execute(sa.select(t).where(t.c.location_id == loc["id"], t.c.state.in_(ACTIVE_STATES),
                                                   t.c.last_event_at >= now - window)
                                .order_by(t.c.last_event_at.desc(), t.c.id.desc()).limit(1)).mappings().first()
                log_detail = {"server_id": site["id"], "event_id": eid, "camera_id": cam, "priority": prio, "source": source}
                if cur is None:
                    rule = rules.get(prio) or {}
                    lane = rule.get("lane") or "quiet"
                    due = now + rule["claim_s"] if lane == "ring" and rule.get("claim_s") else None
                    res = c.execute(t.insert().values(
                        org_id=loc["org_id"], location_id=loc["id"], opened_at=now, last_event_at=now, updated_at=now, closed_at=None,
                        state="new", priority=prio, lane=lane, sla_due_at=due, resolve_due_at=None, escalation_level=0,
                        next_escalation_at=due, event_count=1, title=_title(c, site["id"], cam, ev)))
                    iid, what = res.inserted_primary_key[0], "opened"
                    _log(c, iid, None, "opened", log_detail, now)
                else:
                    cur = dict(cur)
                    iid, what = cur["id"], "event_added"
                    _log(c, iid, None, "event_added", log_detail, now)
                    vals = _raise(c, cur, prio, now, rules)
                    raised_high = vals.get("priority") == "high"
                    c.execute(sa.update(t).where(t.c.id == iid).values(**vals, event_count=t.c.event_count + 1, last_event_at=now, updated_at=now))
                try:
                    ts = float(ev.get("start_ts") or now)
                except (TypeError, ValueError):
                    ts = now
                c.execute(ie.insert().values(incident_id=iid, server_id=site["id"], event_id=eid, camera_id=cam, priority=prio,
                                             kind=source, ts=ts, detail=detail, feedback_state=None))
    except sa.exc.IntegrityError:
        # the unique (server, event) backstop: a concurrent ingest of the same event committed first. This whole
        # transaction rolled back, so no half-made incident is left behind.
        return None
    inc = get(iid)
    _emit({"opened": "incident_opened", "event_added": "incident_event_added"}.get(what, "incident_updated"), inc,
          event={"server_id": site["id"], "event_id": eid, "camera_id": cam, "priority": prio, **detail})
    if (what == "opened" and inc["priority"] == "high") or raised_high:
        from . import push   # lazily: push imports auth, which imports this module
        _spawn(push.notify_soc, inc, "operators")
        if what == "opened":
            _spawn(push.notify_incident_customers, inc)
    return what, inc


def _feed(conn, ev: dict, source: str) -> tuple[str, dict] | None:
    site, loc = getattr(conn, "site", None), getattr(conn, "location", None)
    if not site or not loc or not loc.get("monitored") or site.get("retired_at"):
        return None
    now = time.time()
    try:
        ts = float(ev.get("start_ts") or now)
    except (TypeError, ValueError):
        ts = now
    if ts < now - MAX_EVENT_AGE_S:
        return None
    if not armed_now(loc, min(ts, now))[0]:
        return None   # disarmed: the event still reaches the Site's Timeline and the customer's alerts, not the SOC
    if ev.get("camera_id") not in (None, "") and str(ev["camera_id"]) in cameras.disabled_ids(site["id"]):
        return None
    return ingest(site, loc, ev, now=now, source=source)


def on_event(conn, msg: dict) -> tuple[str, dict] | None:
    """A live event publish from a server (agents._loop): verified, not marked a false alarm at the site."""
    if msg.get("type") != "event" or not isinstance(msg.get("event"), dict):
        return None
    e = msg["event"]
    if e.get("status") != "verified" or e.get("id") is None:
        return None
    fb = e.get("feedback")
    if isinstance(fb, str):   # the site may send the stored JSON text rather than the parsed object
        try:
            fb = json.loads(fb)
        except ValueError:
            fb = None
    if isinstance(fb, dict) and fb.get("verdict") == "false_alarm":
        return None
    pol = e.get("policy")
    return _feed(conn, {"id": e["id"], "camera_id": e.get("camera_id"), "priority": e.get("priority"), "label": e.get("camera_class"),
                        "start_ts": e.get("start_ts"), "synopsis": e.get("synopsis"), "watched": e.get("watched"),
                        "policy": pol.get("text") if isinstance(pol, dict) else pol}, "event")


def on_attention(conn, attention) -> list[tuple[str, dict]]:
    """The heartbeat's attention list (summary.attention: verified medium/high/rule/watched events since the last
    one). Catches what a live publish missed while the tunnel was down; re-listed events are idempotent."""
    out = []
    for e in attention or []:
        if isinstance(e, dict) and e.get("id") is not None:
            r = _feed(conn, e, "attention")
            if r:
                out.append(r)
    return out


# ---------------------------------------------------------------- ownership

def _claimed_msg(cur: dict, now: float) -> str:
    who = _emails([cur.get("claimed_by")]).get(cur.get("claimed_by")) or "someone"
    return f"already claimed by {who} {_ago(now - (cur.get('claimed_at') or now))} ago"


def _conflict(cur: dict, now: float) -> ConflictError:
    if cur["state"] == "claimed":
        return ConflictError(_claimed_msg(cur, now))
    if cur["state"] == "pending_verify":
        return ConflictError("this incident is resolved and waiting for a supervisor to verify it")
    if cur["state"] == "closed":
        return ConflictError("this incident is closed")
    return ConflictError("nobody has claimed this incident: claim it first")


def _is_sup(level: str | None) -> bool:
    return level == "supervisor"


def _holder(cur: dict):
    t = db.incidents
    return t.c.claimed_by == cur["claimed_by"] if cur["claimed_by"] else t.c.claimed_by.is_(None)


def _resolve_due(cur: dict, now: float) -> float | None:
    if cur.get("resolve_due_at") is not None:
        return cur["resolve_due_at"]
    rs = (sla().get(cur["priority"]) or {}).get("resolve_s")
    return now + rs if rs else None


def _done_with(uid: str | None, iid: int) -> None:
    """Someone stopped handling `iid`: engaged on it -> available (break and offline stay as they are)."""
    if not uid:
        return
    p = db.one(sa.select(db.soc_presence).where(db.soc_presence.c.user_id == uid))
    if p and p["state"] == "engaged" and p.get("incident_id") in (iid, None):
        set_presence(uid, "available", incident_id=None)


def claim(iid: int, u: dict) -> dict:
    now = time.time()
    t = db.incidents
    with db.engine().begin() as c:
        cur = _load(c, iid)
        due = _resolve_due({**cur, "resolve_due_at": None}, now)
        res = c.execute(sa.update(t).where(t.c.id == iid, t.c.state == "new").values(
            state="claimed", claimed_by=u["id"], claimed_at=now, first_claimed_at=sa.func.coalesce(t.c.first_claimed_at, now),
            resolve_due_at=due, next_escalation_at=due, updated_at=now))
        if not res.rowcount:
            cur = _load(c, iid)
            if cur["state"] == "claimed" and cur["claimed_by"] == u["id"]:
                return get(iid)   # a double click: already mine
            raise _conflict(cur, now)
        _log(c, iid, u, "claim", {"after_s": round(now - cur["opened_at"], 1)}, now)
    inc = _emit("incident_updated", get(iid))   # the incident first: every tab stops ringing before rosters redraw
    set_presence(u["id"], "engaged", incident_id=iid)
    return inc


def release(iid: int, u: dict, level: str | None) -> dict:
    """Back to the queue, where it rings again. The claim clock is not restarted: an incident released past its SLA
    is still overdue."""
    now = time.time()
    t = db.incidents
    with db.engine().begin() as c:
        cur = _load(c, iid)
        if cur["state"] != "claimed":
            raise ConflictError("this incident is not claimed") if cur["state"] == "new" else _conflict(cur, now)
        if cur["claimed_by"] != u["id"] and not _is_sup(level):
            raise Forbidden(f"{_claimed_msg(cur, now)}: only they or a supervisor can release it")
        claim_s = (sla().get(cur["priority"]) or {}).get("claim_s")
        due = cur["sla_due_at"] if cur["sla_due_at"] is not None else (now + claim_s if cur["lane"] == "ring" and claim_s else None)
        res = c.execute(sa.update(t).where(t.c.id == iid, t.c.state == "claimed", _holder(cur)).values(
            state="new", claimed_by=None, claimed_at=None, resolve_due_at=None, sla_due_at=due, next_escalation_at=due, updated_at=now))
        if not res.rowcount:
            raise _conflict(_load(c, iid), now)
        _log(c, iid, u, "release", {"from_user_id": cur["claimed_by"]}, now)
    _done_with(cur["claimed_by"], iid)
    return _emit("incident_updated", get(iid))


def takeover(iid: int, u: dict) -> dict:
    """A supervisor takes an open (new or claimed) incident, whoever holds it. The route checks the level."""
    now = time.time()
    t = db.incidents
    with db.engine().begin() as c:
        cur = _load(c, iid)
        if cur["state"] not in ACTIVE_STATES:
            raise _conflict(cur, now)
        if cur["claimed_by"] == u["id"]:
            return get(iid)
        due = _resolve_due(cur, now)
        res = c.execute(sa.update(t).where(t.c.id == iid, t.c.state == cur["state"], _holder(cur)).values(
            state="claimed", claimed_by=u["id"], claimed_at=now, first_claimed_at=sa.func.coalesce(t.c.first_claimed_at, now),
            assigned_by=u["id"], resolve_due_at=due, next_escalation_at=due, updated_at=now))
        if not res.rowcount:
            raise ConflictError("the incident changed while you were taking it over: try again")
        _log(c, iid, u, "takeover", {"from_user_id": cur["claimed_by"], "from_email": _emails([cur["claimed_by"]]).get(cur["claimed_by"])}, now)
    inc = _emit("incident_updated", get(iid))
    _done_with(cur["claimed_by"], iid)
    set_presence(u["id"], "engaged", incident_id=iid)
    return inc


def handoff(iid: int, u: dict, level: str | None, target_id: str) -> dict:
    """The holder (or a supervisor, also for an unclaimed incident: assigning it) gives the incident to other SOC staff."""
    now = time.time()
    t = db.incidents
    target = db.one(sa.select(db.users).where(db.users.c.id == target_id))
    if not target or not level_of(target):
        raise NotFound("that person is not SOC staff")
    with db.engine().begin() as c:
        cur = _load(c, iid)
        if cur["state"] not in ACTIVE_STATES or (cur["state"] == "new" and not _is_sup(level)):
            raise _conflict(cur, now)
        if cur["claimed_by"] != u["id"] and not _is_sup(level):
            raise Forbidden(f"{_claimed_msg(cur, now)}: only they or a supervisor can hand it off")
        if cur["claimed_by"] == target_id:
            return get(iid)
        due = _resolve_due(cur, now)
        res = c.execute(sa.update(t).where(t.c.id == iid, t.c.state == cur["state"], _holder(cur)).values(
            state="claimed", claimed_by=target_id, claimed_at=now, first_claimed_at=sa.func.coalesce(t.c.first_claimed_at, now),
            assigned_by=u["id"], resolve_due_at=due, next_escalation_at=due, updated_at=now))
        if not res.rowcount:
            raise ConflictError("the incident changed while you were handing it off: try again")
        _log(c, iid, u, "handoff", {"from_user_id": cur["claimed_by"], "to_user_id": target_id, "to_email": target["email"]}, now)
    inc = _emit("incident_updated", get(iid))
    _done_with(cur["claimed_by"], iid)
    set_presence(target_id, "engaged", incident_id=iid)
    return inc


def resolve(iid: int, u: dict, level: str | None, disposition: str, notes: str | None = None) -> dict:
    """The holder (or a supervisor) closes the incident with a disposition, or sends it to pending_verify when the
    disposition needs four eyes at this priority. The route sends false-alarm feedback to the sites afterwards."""
    d = DISPOSITIONS.get(disposition)
    if d is None or not d[3]:
        raise Invalid(f"unknown disposition {disposition!r}")
    notes = (notes or "").strip() or None
    if d[2] and not notes:
        raise Invalid(f"notes are required for {d[1].lower()}")
    now = time.time()
    t = db.incidents
    with db.engine().begin() as c:
        cur = _load(c, iid)
        if cur["state"] not in ACTIVE_STATES or (cur["claimed_by"] != u["id"] and not _is_sup(level)):
            raise _conflict(cur, now)
        four = needs_four_eyes(disposition, cur["priority"])
        res = c.execute(sa.update(t).where(t.c.id == iid, t.c.state == cur["state"], _holder(cur)).values(
            state="pending_verify" if four else "closed", disposition=disposition, disposition_notes=notes, resolved_by=u["id"],
            resolved_at=now, closed_at=None if four else now, next_escalation_at=None, updated_at=now))
        if not res.rowcount:
            raise _conflict(_load(c, iid), now)
        _log(c, iid, u, "resolve", {"disposition": disposition, "notes": notes, "four_eyes": four}, now)
    _done_with(cur["claimed_by"], iid)
    if cur["claimed_by"] != u["id"]:
        _done_with(u["id"], iid)
    return _emit("incident_resolved", get(iid))


def verify(iid: int, u: dict) -> dict:
    """Four eyes: a supervisor other than the resolver closes a pending_verify incident. The route checks the level."""
    now = time.time()
    t = db.incidents
    with db.engine().begin() as c:
        res = c.execute(sa.update(t).where(t.c.id == iid, t.c.state == "pending_verify", t.c.resolved_by != u["id"]).values(
            state="closed", closed_at=now, four_eyes_by=u["id"], four_eyes_at=now, updated_at=now))
        if not res.rowcount:
            cur = _load(c, iid)
            if cur["state"] == "pending_verify":
                raise Forbidden("you resolved this incident: a different supervisor has to verify it")
            raise ConflictError("this incident is not waiting for verification")
        _log(c, iid, u, "verify", {}, now)
    return _emit("incident_resolved", get(iid))


def note(iid: int, u: dict, text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise Invalid("a note needs text")
    now = time.time()
    with db.engine().begin() as c:
        _load(c, iid)
        _log(c, iid, u, "note", {"text": text[:4000]}, now)
        c.execute(sa.update(db.incidents).where(db.incidents.c.id == iid).values(updated_at=now))
    return _emit("incident_updated", get(iid))


def sweep(u: dict, location_id: str | None = None) -> list[int]:
    """Close every unclaimed quiet-lane incident (optionally at one Site) as `swept`. Each is its own conditional
    UPDATE, so one claimed or promoted a moment ago is left alone."""
    now = time.time()
    t = db.incidents
    q = sa.select(t.c.id).where(t.c.lane == "quiet", t.c.state == "new")
    if location_id:
        q = q.where(t.c.location_id == location_id)
    done = []
    with db.engine().begin() as c:
        for r in c.execute(q).all():
            res = c.execute(sa.update(t).where(t.c.id == r.id, t.c.lane == "quiet", t.c.state == "new").values(
                state="closed", disposition="swept", resolved_by=u["id"], resolved_at=now, closed_at=now, next_escalation_at=None,
                updated_at=now))
            if res.rowcount:
                _log(c, r.id, u, "swept", {"location_id": location_id}, now)
                done.append(r.id)
    for row in tag(db.rows(sa.select(t).where(t.c.id.in_(done)))) if done else []:
        _emit("incident_resolved", row)
    return done


def promote(iid: int, u: dict) -> dict:
    """Quiet -> ringing lane: the operator says this one matters. At least medium, so it gets a claim clock; the
    escalation ladder starts afresh."""
    now = time.time()
    t = db.incidents
    with db.engine().begin() as c:
        cur = _load(c, iid)
        if cur["state"] not in ACTIVE_STATES:
            raise _conflict(cur, now)
        if cur["lane"] != "quiet":
            raise ConflictError("this incident is already in the ringing lane")
        prio = cur["priority"] if rank(cur["priority"]) >= rank("medium") else "medium"
        claim_s = (sla().get(prio) or {}).get("claim_s")
        due = now + claim_s if claim_s and cur["state"] == "new" else None
        res = c.execute(sa.update(t).where(t.c.id == iid, t.c.lane == "quiet", t.c.state == cur["state"]).values(
            lane="ring", priority=prio, sla_due_at=due, escalation_level=0, next_escalation_at=due, updated_at=now))
        if not res.rowcount:
            raise ConflictError("the incident changed while you were promoting it: try again")
        _log(c, iid, u, "promote", {"from_priority": cur["priority"], "to_priority": prio}, now)
    return _emit("incident_updated", get(iid))


def _require_handler(cur: dict, u: dict, level: str | None) -> None:
    """Calls, checklist ticks and deterrence act on the incident: its holder, or a supervisor while it is open."""
    if _is_sup(level) and cur["state"] in OPEN_STATES:
        return
    if cur["state"] == "claimed" and cur["claimed_by"] == u["id"]:
        return
    now = time.time()
    raise Forbidden(f"{_claimed_msg(cur, now)}: it is theirs to work") if cur["state"] == "claimed" else _conflict(cur, now)


def log_call(iid: int, u: dict, level: str | None, contact_id: int, outcome: str, notes: str | None = None) -> dict:
    if outcome not in CALL_OUTCOMES:
        raise Invalid(f"outcome is one of {', '.join(CALL_OUTCOMES)}")
    now = time.time()
    with db.engine().begin() as c:
        cur = _load(c, iid)
        _require_handler(cur, u, level)
        lc = db.location_contacts
        ct = c.execute(sa.select(lc).where(lc.c.id == contact_id, lc.c.location_id == cur["location_id"])).mappings().first()
        if ct is None:
            raise NotFound("not a contact of this incident's site")
        _log(c, iid, u, "call", {"contact_id": ct["id"], "name": ct["name"], "phone": ct["phone"], "outcome": outcome,
                                 "notes": (notes or "").strip()[:2000] or None}, now)
        c.execute(sa.update(db.incidents).where(db.incidents.c.id == iid).values(updated_at=now))
    return _emit("incident_updated", get(iid))


def sop_tick(iid: int, u: dict, level: str | None, procedure_id: int, step_id: str, done: bool, note_text: str | None = None) -> dict:
    now = time.time()
    with db.engine().begin() as c:
        cur = _load(c, iid)
        _require_handler(cur, u, level)
        lp = db.location_procedures
        p = c.execute(sa.select(lp).where(lp.c.id == procedure_id, lp.c.location_id == cur["location_id"])).mappings().first()
        if p is None:
            raise NotFound("not a procedure of this incident's site")
        step = next((s for s in (p["steps"] or []) if isinstance(s, dict) and s.get("id") == step_id), None)
        if step is None:
            raise NotFound("no such step in that procedure")
        _log(c, iid, u, "sop", {"procedure_id": p["id"], "title": p["title"], "step_id": step_id, "text": step.get("text"),
                                "done": bool(done), "note": (note_text or "").strip()[:2000] or None}, now)
        c.execute(sa.update(db.incidents).where(db.incidents.c.id == iid).values(updated_at=now))
    return _emit("incident_updated", get(iid))


# ---------------------------------------------------------------- through the tunnel: deterrence and feedback

async def relay(iid: int, u: dict, level: str | None, server_id: str, camera_id: str, on: bool) -> dict:
    """Switch a camera's relay output (siren, lights) at the incident's Site; logged whether or not it worked."""
    from .agents import registry          # lazily: agents imports this module
    from .fleet_actions import _call
    with db.engine().connect() as c:
        cur = _load(c, iid)
        _require_handler(cur, u, level)
        srv = c.execute(sa.select(db.sites.c.id, db.sites.c.location_id).where(db.sites.c.id == server_id)).first()
        if srv is None or srv.location_id != cur["location_id"]:
            raise NotFound("that server is not at this incident's site")
        cam = c.execute(sa.select(db.cameras.c.camera_id).where(db.cameras.c.server_id == server_id, db.cameras.c.camera_id == camera_id)).first()
        if cam is None:
            raise NotFound("unknown camera on that server")
    conn = registry.get(server_id)
    status, result, error = None, None, None
    if conn is None:
        error = "server offline"
    else:
        try:
            status, result = await _call(conn, u, "POST", f"/api/cameras/{camera_id}/relay", role="operator", body={"on": bool(on)}, timeout=15)
        except Exception as e:   # tunnel trouble: logged as a failed attempt, the operator tries another way
            error = str(e)[:200] or "site did not answer"
        if status is not None and status != 200:
            d = result.get("detail") if isinstance(result, dict) else None
            error = f"HTTP {status}{f': {str(d)[:160]}' if d else ''}"
    ok = error is None
    now = time.time()
    with db.engine().begin() as c:
        _log(c, iid, u, "relay", {"server_id": server_id, "camera_id": camera_id, "on": bool(on), "ok": ok, "status": status, "error": error}, now)
        c.execute(sa.update(db.incidents).where(db.incidents.c.id == iid).values(updated_at=now))
    inc = _emit("incident_updated", get(iid))
    if not ok:
        raise (Unavailable if conn is None else SiteError)(f"relay failed: {error}")
    return {"ok": True, "status": status, "result": result, "incident": inc}


async def send_feedback(iid: int, u: dict, note_text: str | None = None) -> dict:
    """A false-alarm resolution tells each event's server (PUT /api/events/{id}/feedback {verdict: false_alarm}) so
    the site's baseline learns. Each incident_event records sent | failed; failed rows (server offline, timeout) are
    what a retry pass (stage 3: rows with feedback_state 'failed' on false-alarm incidents) picks up."""
    from .agents import registry
    from .fleet_actions import _call
    ie = db.incident_events
    rows = db.rows(sa.select(ie).where(ie.c.incident_id == iid, sa.or_(ie.c.feedback_state.is_(None), ie.c.feedback_state == "failed")))
    sent = failed = 0
    for r in rows:
        conn = registry.get(r["server_id"])
        ok = False
        if conn is not None:
            try:
                st, _ = await _call(conn, u, "PUT", f"/api/events/{r['event_id']}/feedback", role="operator",
                                    body={"verdict": "false_alarm", "note": (note_text or "SOC: false alarm")[:500]}, timeout=15)
                ok = st == 200
            except Exception:
                ok = False
        db.run(sa.update(ie).where(ie.c.id == r["id"]).values(feedback_state="sent" if ok else "failed"))
        sent, failed = sent + ok, failed + (not ok)
    if rows:
        with db.engine().begin() as c:
            _log(c, iid, u, "feedback", {"sent": sent, "failed": failed})
        _emit("incident_updated", get(iid))
    return {"sent": sent, "failed": failed}


# ---------------------------------------------------------------- presence
# soc_presence.state holds the status (available | engaged | break | offline); last_seen_at is refreshed by the
# socket (every 30 s while it is open) and by PUT /api/soc/presence, and a row older than ON_SHIFT_S reads as
# offline, so a closed laptop drops off the roster without anyone setting anything.

_KEEP = object()
_sockets: dict[str, int] = {}   # user id -> open /api/soc/ws sockets (closing a second tab must not sign the first off)


def set_presence(uid: str, status: str, incident_id=_KEEP, now: float | None = None) -> dict | None:
    if status not in PRESENCE:
        raise Invalid(f"status is one of {', '.join(PRESENCE)}")
    now = time.time() if now is None else now
    p = db.soc_presence
    vals: dict = {"state": status, "last_seen_at": now}
    if incident_id is not _KEEP:
        vals["incident_id"] = incident_id
    elif status != "engaged":
        vals["incident_id"] = None
    with db.engine().begin() as c:
        cur = c.execute(sa.select(p).where(p.c.user_id == uid)).mappings().first()
        if cur is None:
            c.execute(p.insert().values(**{"user_id": uid, "since": now, "incident_id": None, **vals}))
        else:
            if cur["state"] != status:
                vals["since"] = now
            c.execute(sa.update(p).where(p.c.user_id == uid).values(**vals))
    entry = presence_of(uid, now)
    broadcast(frame("presence", presence=entry))
    return entry


def touch_presence(uid: str, now: float | None = None) -> None:
    db.run(sa.update(db.soc_presence).where(db.soc_presence.c.user_id == uid).values(last_seen_at=time.time() if now is None else now))


def _entry(u: dict, p: dict | None, now: float) -> dict:
    live = bool(p) and p["state"] != "offline" and p["last_seen_at"] >= now - ON_SHIFT_S
    return {"user_id": u["id"], "email": u["email"], "soc_role": level_of(u),
            "status": p["state"] if live else "offline", "since": p["since"] if p else None,
            "last_seen_at": p["last_seen_at"] if p else None, "incident_id": p.get("incident_id") if live else None, "on_shift": live}


_USER_COLS = (db.users.c.id, db.users.c.email, db.users.c.soc_role, db.users.c.is_super)


def roster(now: float | None = None) -> list[dict]:
    """SOC staff (and hub administrators who have opened the SOC) with their effective status, by email."""
    now = time.time() if now is None else now
    pres = {r["user_id"]: r for r in db.rows(sa.select(db.soc_presence))}
    us = db.rows(sa.select(*_USER_COLS).where(sa.or_(db.users.c.soc_role.in_(list(SOC_ROLES)), db.users.c.id.in_(list(pres) or [""])))
                 .order_by(db.users.c.email))
    return [_entry(u, pres.get(u["id"]), now) for u in us if level_of(u)]


def presence_of(uid: str, now: float | None = None) -> dict | None:
    now = time.time() if now is None else now
    u = db.one(sa.select(*_USER_COLS).where(db.users.c.id == uid))
    if not u:
        return None
    return _entry(u, db.one(sa.select(db.soc_presence).where(db.soc_presence.c.user_id == uid)), now)


def on_shift_ids(level: str | None = None, now: float | None = None) -> set[str]:
    """Who is on shift: status not offline and seen within ON_SHIFT_S (push and, in stage 3, escalation read this).
    `level` "supervisor" keeps supervisors (hub administrators included) only."""
    return {e["user_id"] for e in roster(now) if e["on_shift"] and (level is None or e["soc_role"] == level)}


def socket_opened(u: dict) -> None:
    """A SOC tab connected: available, unless they are mid-incident or on break and merely reconnecting."""
    _sockets[u["id"]] = _sockets.get(u["id"], 0) + 1
    cur = presence_of(u["id"])
    if cur and cur["on_shift"] and cur["status"] in ("engaged", "break"):
        touch_presence(u["id"])
        return
    set_presence(u["id"], "available", incident_id=None)


def socket_closed(u: dict) -> None:
    n = _sockets.get(u["id"], 1) - 1
    if n > 0:
        _sockets[u["id"]] = n
        return
    _sockets.pop(u["id"], None)
    set_presence(u["id"], "offline", incident_id=None)


def snapshot() -> dict:
    """The first frame on /api/soc/ws: every open incident and the roster."""
    return frame("snapshot", incidents=query(OPEN_STATES, limit=1000), presence=roster())
