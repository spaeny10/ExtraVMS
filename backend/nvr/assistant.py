"""Ask the NVR: site-wide questions answered from the NVR's own data, plus morning briefings.

One round of plan -> run -> answer, which is dependable with a 7B model:
1. Plan: Qwen reads the question (text only) and picks up to MAX_CALLS lookups from TOOLS, as JSON.
   The plan is checked in code: camera names matched to ids, times parsed and clamped, unknown tools dropped.
2. Run: each lookup is plain code (SQL, the hybrid event search, the footage index, recordings on disk).
   Results are compact text lines with citation handles: [#123] = event 123, [F1] = footage moment 1.
3. Answer: Qwen streams a reply from those results only, citing the handles; the UI turns them into links.

Briefings: at `briefing.time` each day the code gathers the facts for the period since the last briefing
(counts, busiest hours, the highest-priority events and why, journeys, recording gaps, disk) and Qwen turns
them into a headline and a few bullets with citations.
"""
from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
import difflib
import json
import logging
import re
import shutil
import time
from typing import AsyncIterator
from zoneinfo import ZoneInfo

from . import baseline, retention, vlmroute
from . import synopsis as vlm
from .config import settings
from .db import db

log = logging.getLogger("nvr.assistant")

MAX_CALLS = 3
MAX_RESULT_CHARS = 9000        # ~2,300 tokens of lookup results in the answer prompt
HISTORY_TURNS = 6
TOOLS = ("search_events", "count_events", "list_unusual", "list_journeys", "search_footage", "recording_gaps", "get_briefing")
FOOTAGE_VERIFY = 3             # footage matches Qwen checks before the answer may use them
PRIORITY_RANK = {"any": -1, "none": 0, "low": 1, "medium": 2, "high": 3}
FALSE_ALARM = "(feedback IS NULL OR json_extract(feedback, '$.verdict') IS NOT 'false_alarm')"


PERSON_WORDS = {"person", "people", "man", "men", "woman", "women", "guy", "someone", "somebody", "worker",
                "workers", "pedestrian", "human", "kid", "child", "children", "boy", "girl", "he", "she", "intruder",
                "anyone", "anybody", "staff", "visitor", "visitors"}
VEHICLE_WORDS = {"car", "cars", "truck", "trucks", "pickup", "pickups", "van", "vans", "suv", "vehicle", "vehicles", "bus",
                 "motorcycle", "bike", "bicycle", "sedan", "trailer", "trailers", "semi", "jeep", "delivery", "ups", "fedex"}
PRIORITY_WORDS = re.compile(r"\b(unusual|odd|strange|weird|suspicious|threat|threats|priority|important|alarming|"
                            r"concerning|dangerous|intruder|break[- ]?in|trespass\w*)\b")
LISTING_WORDS = re.compile(r"^(list|show|what were|what are|latest|recent|last \d+|give me)\b")


def query_label(q: str) -> str | None:
    """Restrict to one class when the text clearly names only people or only vehicles."""
    words = {w.strip(".,!?'\"").lower() for w in q.split()}
    person, vehicle = bool(words & PERSON_WORDS), bool(words & VEHICLE_WORDS)
    return "person" if person and not vehicle else "vehicle" if vehicle and not person else None


class Context:
    """Set at startup: the pipeline (Qwen readiness) and the footage indexer."""
    pipeline = None
    footage = None


ctx = Context()

# The hub's Site Ask (retrieve.py) sets these for one request: the Site's time zone (day boundaries, "today", the
# planner's local times, labels) and the camera names (a day name inside one, "Saturday Market", is not a time).
# Unset (the server's own Ask, Find, briefings): the server's local clock and no camera names, as before.
SITE_TZ: contextvars.ContextVar[dt.tzinfo | None] = contextvars.ContextVar("site_tz", default=None)
CAMERA_NAMES: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar("camera_names", default=())


def zone_for(tz: str | dt.tzinfo | None) -> dt.tzinfo | None:
    """An IANA zone ("America/Chicago") as a tzinfo; None (the server's local clock) when absent or unknown."""
    if tz is None or isinstance(tz, dt.tzinfo):
        return tz
    try:
        return ZoneInfo(str(tz)) if str(tz).strip() else None
    except Exception:  # noqa: BLE001 - unknown name, or no tz database on this host: the local clock
        log.warning("unknown time zone %r: using the server's local time", tz)
        return None


def _zone(tz: str | dt.tzinfo | None = None) -> dt.tzinfo | None:
    return zone_for(tz) if tz is not None else SITE_TZ.get()


def _local(ts: float, tz: str | dt.tzinfo | None = None) -> dt.datetime:
    """ts as a wall-clock time in the Site's zone (naive local time when there is none)."""
    return dt.datetime.fromtimestamp(ts, _zone(tz))


def sql_local(now: float | None = None) -> str:
    """The SQLite strftime modifier for local time: 'localtime', or the Site zone's offset at `now`."""
    z = SITE_TZ.get()
    if z is None:
        return "'localtime'"
    off = dt.datetime.fromtimestamp(now or time.time(), z).utcoffset() or dt.timedelta(0)
    return f"'{int(off.total_seconds()):+d} seconds'"


# ---------------------------------------------------------------- formatting helpers

def _cameras() -> dict[str, dict]:
    return {c["id"]: c for c in db.cameras()}


def _cam_name(cid: str) -> str:
    return (_cameras().get(cid) or {}).get("name", cid)


def _when(ts: float, now: float | None = None) -> str:
    t = _local(ts)
    today = _local(now or time.time()).date()
    if t.date() == today:
        return f"today {t:%H:%M:%S}"
    if t.date() == today - dt.timedelta(days=1):
        return f"yesterday {t:%H:%M:%S}"
    return f"{t:%a %d %b %H:%M:%S}"


def _event_line(e: dict) -> str:
    an = e.get("anomaly_json")
    if isinstance(an, str):
        an = json.loads(an)
    extra = []
    areas = e.get("areas")
    if isinstance(areas, str):
        areas = json.loads(areas)
    if areas:
        extra.append("went to: " + ", ".join(a["name"] for a in areas))
    if e.get("watched"):
        extra.append(f"WATCH LIST: {e['watched']}")
    if e.get("priority") and e["priority"] != "none":
        extra.append(f"priority {e['priority']}")
    pol = e.get("policy")
    if isinstance(pol, str):
        pol = json.loads(pol)
    if pol:  # a broken site rule, e.g. "No hard hat in PPE zone 'Yard' (40 s)"
        extra.append(f"SITE RULE BROKEN: {pol['text']}")
    tc = e.get("towing_check")
    if isinstance(tc, str):
        tc = json.loads(tc)
    if tc is None and isinstance(e.get("synopsis_json"), dict):
        tc = e["synopsis_json"].get("towing_check")
    if tc and tc.get("reason"):  # the description said towing; the second look decided (policy.confirm_towing)
        extra.append(("towing confirmed: " if tc.get("confirmed") else "towing NOT confirmed: ") + tc["reason"][:160])
    wc = e.get("weapon_check")
    if isinstance(wc, str):
        wc = json.loads(wc)
    if wc is None and isinstance(e.get("synopsis_json"), dict):
        wc = e["synopsis_json"].get("weapon_check")
    if wc and wc.get("verdict"):  # the description mentioned a weapon; the full-resolution second look decided (weaponcheck)
        extra.append({"confirmed": "WEAPON CONFIRMED", "not_confirmed": "weapon NOT confirmed"}.get(
            wc["verdict"], "possible weapon, UNCONFIRMED, needs a person to look") + ": " + (wc.get("reason") or "")[:160])
    if an and an.get("reasons"):
        extra.append("unusual: " + "; ".join(an["reasons"]))
    fb = e.get("feedback")
    if isinstance(fb, str):
        fb = json.loads(fb)
    if fb and fb.get("verdict") == "false_alarm":
        extra.append("marked false alarm")
    desc = (e.get("synopsis") or f"{e.get('yolo_class') or e['camera_class']} (no description)").strip()
    if len(desc) > 200:
        desc = desc[:197] + "..."
    dur = max(0, (e.get("end_ts") or e["start_ts"]) - e["start_ts"])
    return (f"[#{e['id']}] {_cam_name(e['camera_id'])}, {_when(e['start_ts'])}, {e['camera_class']}, {dur:.0f} s"
            + (f" ({', '.join(extra)})" if extra else "") + f": {desc}")


class Refs:
    """Citation handles used in results, so the UI can render [#id] and [F1] as links."""

    def __init__(self) -> None:
        self.events: dict[int, dict] = {}
        self.footage: dict[str, dict] = {}

    def event(self, e: dict) -> None:
        self.events[e["id"]] = {"camera_id": e["camera_id"], "camera": _cam_name(e["camera_id"]), "start_ts": e["start_ts"],
                                "label": e["camera_class"], "snapshot": bool(e.get("snapshot"))}

    def moment(self, m: dict) -> str:
        key = f"F{len(self.footage) + 1}"
        self.footage[key] = {"camera_id": m["camera_id"], "camera": _cam_name(m["camera_id"]), "ts": m["ts"], "tile": m["tile"]}
        return key

    def as_dict(self) -> dict:
        return {"events": {str(k): v for k, v in self.events.items()}, "footage": self.footage}


# ---------------------------------------------------------------- plan

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "calls": {"type": "array", "maxItems": MAX_CALLS, "items": {"type": "object", "properties": {
            "tool": {"type": "string", "enum": list(TOOLS)},
            "text": {"type": "string", "description": "what to look for, for search_events / search_footage"},
            "camera": {"type": "string", "description": "camera id or name, or empty for all cameras"},
            "since": {"type": "string", "description": "local time YYYY-MM-DD HH:MM, or empty"},
            "until": {"type": "string", "description": "local time YYYY-MM-DD HH:MM, or empty"},
            "label": {"type": "string", "enum": ["any", "person", "vehicle"]},
            "min_priority": {"type": "string", "enum": ["any", "low", "medium", "high"]},
            "group_by": {"type": "string", "enum": ["none", "hour", "day", "camera", "label"]},
        }, "required": ["tool"]}},
    },
    "required": ["calls"],
}

TOOL_HELP = """Tools (pick 1-3; fill only the fields that tool uses):
- search_events: find camera events by meaning. text = what to look for, in the operator's words (e.g. "BigView truck with a trailer"). Filters: camera, since, until. Only set min_priority if the question asks about unusual or suspicious activity. Leave text empty only to list the latest events.
- count_events: how many events. Filters: camera, since, until, label. group_by: hour | day | camera | label | none.
- list_unusual: events that were unusual for their camera (odd time or place, stayed long) or high priority. Filters: camera, since, until.
- list_journeys: people followed across several cameras (e.g. went outside and came back). Filters: since, until.
- search_footage: search ALL recorded video by what it looks like, including moments no camera event covered (e.g. "white pickup truck", "open gate", "ladder"). text + camera, since, until.
- recording_gaps: when a camera was not recording (offline). camera, since, until.
- get_briefing: the written summary of a period ("what happened overnight"). since = start of the period."""


def _plan_prompt(question: str, history: list[dict], now: float) -> tuple[str, str]:
    n = _local(now)
    y = n - dt.timedelta(days=1)
    cams = "\n".join(f'- "{c["id"]}" = {c["name"]}' + (f" ({c['scene_notes'].splitlines()[0][:120]})" if (c.get("scene_notes") or "").strip() else "")
                     for c in db.cameras())
    system = ("You plan lookups for a security camera system's assistant. Pick the tools and filters that answer the "
              "operator's question. Use camera ids from the list. Use exact local times.")
    hist = ""
    if history:
        hist = "Earlier in this conversation:\n" + "\n".join(f"{m['role']}: {m['content'][:300]}" for m in history[-4:]) + "\n\n"
    text = (f"Now: {n:%A %Y-%m-%d %H:%M} (local). Today = {n:%Y-%m-%d}, yesterday = {y:%Y-%m-%d}.\n"
            f'"today" = since {n:%Y-%m-%d} 00:00. "last night"/"overnight" = {y:%Y-%m-%d} 18:00 to {n:%Y-%m-%d} 07:00. '
            f'"this morning" = {n:%Y-%m-%d} 05:00-12:00. "this afternoon" = {n:%Y-%m-%d} 12:00-18:00. '
            f'"after 6pm" (today) = since {n:%Y-%m-%d} 18:00. No time mentioned = leave since/until empty.\n'
            f"Cameras:\n{cams}\n\n{TOOL_HELP}\n\n{hist}Question: {question}\nAnswer as JSON.")
    return system, text


def match_camera(s: str | None) -> str | None:
    """Camera id from an id, a name or part of a name ('side yard', 'east door'); None = all cameras."""
    s = (s or "").strip().lower()
    if not s or s in ("any", "all", "none", "all cameras"):
        return None
    cams = db.cameras()
    for c in cams:
        if s == c["id"].lower() or s == c["name"].lower():
            return c["id"]
    for c in cams:
        name = c["name"].lower()
        if s in name or name in s:
            return c["id"]
    words = set(re.findall(r"[a-z0-9]+", s))
    best = max(cams, key=lambda c: len(words & set(re.findall(r"[a-z0-9]+", c["name"].lower()))), default=None)
    if best and words & set(re.findall(r"[a-z0-9]+", best["name"].lower())):
        return best["id"]
    close = difflib.get_close_matches(s, [c["name"].lower() for c in cams], n=1, cutoff=0.6)
    return next((c["id"] for c in cams if close and c["name"].lower() == close[0]), None)


def parse_time(s: str | None, now: float) -> float | None:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s[:19], fmt).replace(tzinfo=_zone()).timestamp()
        except ValueError:
            continue
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)  # bare time = today
    if m:
        return _local(now).replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0).timestamp()
    return None


def check_plan(raw: dict, question: str, now: float) -> list[dict]:
    """Validate Qwen's plan: known tools only, real camera ids, sane times. Falls back to a plain event search."""
    out = []
    oldest = now - 90 * 86400
    for c in (raw.get("calls") or [])[:MAX_CALLS * 3]:
        tool = c.get("tool") if isinstance(c, dict) else None
        if tool not in TOOLS:
            continue
        if len(out) >= MAX_CALLS:
            break
        since, until = parse_time(c.get("since"), now), parse_time(c.get("until"), now)
        if since is not None:
            since = min(max(since, oldest), now)
        if until is not None:
            until = min(max(until, oldest), now + 60)
        if since is not None and until is not None and since > until:
            since, until = until, since
        # The question decides the label and whether priority matters; the planner's guesses are ignored.
        label = query_label(question)
        q_low = question.lower()
        args = {"text": (c.get("text") or "").strip()[:200], "camera": match_camera(c.get("camera")), "since": since,
                "until": until, "label": label,
                "min_priority": c.get("min_priority") if c.get("min_priority") in ("low", "medium", "high") and PRIORITY_WORDS.search(q_low) else None,
                "group_by": c.get("group_by") if c.get("group_by") in ("hour", "day", "camera", "label") else None}
        if tool == "search_footage" and not args["text"]:
            args["text"] = question[:200]
        if tool == "search_events" and not args["text"] and not LISTING_WORDS.search(q_low.strip()):
            args["text"] = question[:200]  # a real question: search by meaning, don't just list the latest
        if args["text"] and (w := time_window(args["text"], now)):
            args["text"] = w["text"][:200]  # the time phrase is a filter, not something to search for
        key = (tool, json.dumps(args, sort_keys=True))
        if key not in {(o["tool"], json.dumps(o["args"], sort_keys=True)) for o in out}:
            out.append({"tool": tool, "args": args})
    if not out:
        out = [{"tool": "search_events", "args": {"text": question[:200], "camera": None, "since": None, "until": None,
                                                  "label": None, "min_priority": None, "group_by": None}}]
    win = time_window(question, now)
    if win:  # a time phrase in the question ("today", "last night", "past 3 hours") beats the planner's guess
        for c in out:
            c["args"]["since"], c["args"]["until"] = win["since"], win["until"]
    return augment(out, question, now)


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WD = "|".join(WEEKDAYS)
_DAY = r"(?:" + _WD + r")"
_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
# A label read back ("Friday, Oct 2", "Saturday or Sunday, Oct 3–4"): the date belongs to the day name, so it never
# ends up in the text that is searched for.
_DATE = r"(?:,?\s+" + _MON + r"\s+\d{1,2}(?:\s*[–-]\s*(?:" + _MON + r"\s+)?\d{1,2})?)?"
_PART = r"(?:\s+(?:night|evening|morning|afternoon))?"
# Day names come first so "Tuesday last week" is one phrase, not "last week" with a stray "Tuesday".
_TIME_PHRASES = re.compile(r"\b((?:since\s+)?(?:on\s+)?" + _DAY + r"\s+(?:of\s+)?(?:last|the\s+previous|previous)\s+week" + _DATE + "|"
                           r"(?:since\s+)?(?:last|previous)\s+week(?:'?s)?\s+(?:on\s+)?" + _DAY + _DATE + "|"
                           r"(?:on\s+)?" + _DAY + r"\s+(?:or|and)\s+(?:on\s+)?" + _DAY + _DATE + "|"
                           r"(?:since\s+)?(?:on\s+)?(?:last|this|past|previous)\s+" + _DAY + _PART + _DATE + "|"
                           r"(?:since\s+)?(?:on\s+)?" + _DAY + _PART + _DATE + "|"
                           r"today|this morning|this afternoon|this evening|tonight|last night|overnight|yesterday|this week|"
                           r"(?:(?:in|over|during|within)\s+the\s+)?(?:last|past|previous)\s+"
                           r"(?:\d+(?:\.\d+)?|one|two|three|four|five|six|seven|twelve|a|an)?\s*(?:minute|hour|day|week)s?)\b")
# a day name followed by one of these, or by a capitalized word that isn't a time word, is part of a name
# ("Saturday Market", "the Tuesday Crew"), not a day
_NAME_WORDS = {"market", "camera", "cameras", "cam", "street", "st", "road", "rd", "avenue", "ave"}
_TIME_WORDS = {"night", "morning", "afternoon", "evening", "last", "this", "previous", "week", "or", "and", "of", "on",
               "since", "i", "we"}
_PARTS = {"morning": (5, 12), "afternoon": (12, 18), "evening": (18, 24), "night": (18, 30)}   # hours from that day's 00:00


def time_phrase(text: str, cameras: list[str] | tuple[str, ...] | None = None) -> re.Match | None:
    """The first time phrase in the text that really names a period. Skipped: "every Tuesday" (not one day), a day name
    inside a camera's name (`cameras`, default CAMERA_NAMES) or followed by a name word ("Saturday Market")."""
    q = text.lower()
    names = [c.lower() for c in (CAMERA_NAMES.get() if cameras is None else cameras) if c and c.strip()]
    inside = [(x.start(), x.end()) for c in names for x in re.finditer(re.escape(c), q)]
    for m in _TIME_PHRASES.finditer(q):
        if any(a < m.end() and m.start() < b for a, b in inside):
            continue
        if re.search(r"\b(?:every|each)\s+$", q[:m.start()]):
            continue
        if any(w in m.group(1) for w in WEEKDAYS):
            nxt = re.match(r"\s+([A-Za-z]+)", text[m.end():])
            if nxt and (nxt[1].lower() in _NAME_WORDS or (nxt[1][0].isupper() and nxt[1].lower() not in _TIME_WORDS)):
                continue
        return m
    return None


def _one_day(core: str, day: dt.datetime) -> tuple[dt.datetime, str] | None:
    """The day a day name means, and its label: "Tuesday" = the most recent Tuesday (today included), "last Tuesday" /
    "previous Tuesday" = the one before today, "Tuesday last week" = the Tuesday of the previous calendar week (weeks
    start Monday), "this Tuesday" = this week's (None when that is still to come)."""
    name = next((w for w in WEEKDAYS if w in core), None)
    if name is None:
        return None
    target = WEEKDAYS.index(name)
    monday = day - dt.timedelta(days=day.weekday())
    if "week" in core:
        return monday - dt.timedelta(days=7) + dt.timedelta(days=target), f"{name.capitalize()} last week"
    if re.match(r"(?:on\s+)?this\b", core):
        d = monday + dt.timedelta(days=target)
        return (d, f"this {name.capitalize()}") if d <= day else None   # later this week: nothing recorded yet
    if re.match(r"(?:on\s+)?(?:last|past|previous)\b", core):
        return day - dt.timedelta(days=(day.weekday() - target) % 7 or 7), f"last {name.capitalize()}"
    return day - dt.timedelta(days=(day.weekday() - target) % 7), name.capitalize()


def _weekday_window(phrase: str, day: dt.datetime) -> tuple[float, float | None, str] | None:
    """A day-name phrase: one day (00:00 to 00:00), "Tuesday night" (18:00 to 06:00), "Saturday or Sunday" (both),
    "since Monday" (Monday 00:00 to now). None when it isn't one, or names no day yet ("this Friday" on a Thursday)."""
    if not any(w in phrase for w in WEEKDAYS):
        return None
    core = re.sub(_DATE + r"$", "", phrase).strip()
    since = re.match(r"since\s+", core)
    core = core[since.end():] if since else core
    part = re.search(r"\s+(night|evening|morning|afternoon)$", core)
    core = core[:part.start()] if part else core
    days = []
    for piece in re.split(r"\s+(?:or|and)\s+", core):
        one = _one_day(piece, day)
        if one is None:
            return None
        days.append(one)
    days.sort(key=lambda x: x[0])
    # labels keep their own wording so a follow-up that reuses one ("and Friday?" -> "...Friday, Oct 2?") reads back
    # as the same day
    (d, label), (last, last_label) = days[0], days[-1]
    if len(days) > 1:
        end = f"{last.day}" if last.month == d.month else f"{last:%b} {last.day}"
        return d.timestamp(), (last + dt.timedelta(days=1)).timestamp(), f"{label} or {last_label}, {d:%b} {d.day}–{end}"
    h0, h1 = _PARTS[part[1]] if part else (0, 24)
    label = f"{label} {part[1]}" if part else label
    if since:
        return (d + dt.timedelta(hours=h0)).timestamp(), None, f"since {label}, {d:%b} {d.day}"
    return (d + dt.timedelta(hours=h0)).timestamp(), (d + dt.timedelta(hours=h1)).timestamp(), f"{label}, {d:%b} {d.day}"


def time_window(text: str, now: float | None = None, tz: str | dt.tzinfo | None = None,
                cameras: list[str] | tuple[str, ...] | None = None) -> dict | None:
    """The time window a phrase in the text refers to: {since, until, label, text (phrase removed)} or None.
    Days start at midnight in `tz` (default: the Site's zone while Site Ask retrieves, else the server's clock)."""
    now = now or time.time()
    m = time_phrase(text, cameras)
    if not m:
        return None
    phrase = m.group(1)
    n = _local(now, tz)
    day = n.replace(hour=0, minute=0, second=0, microsecond=0)
    at = lambda d, h: d.replace(hour=h).timestamp()
    yday = day - dt.timedelta(days=1)
    windows = {
        "today": (day.timestamp(), None), "this morning": (at(day, 5), at(day, 12)),
        "this afternoon": (at(day, 12), at(day, 18)), "this evening": (at(day, 18), None), "tonight": (at(day, 18), None),
        "last night": (at(yday, 18), at(day, 7)), "overnight": (at(yday, 18), at(day, 7)),
        "yesterday": (yday.timestamp(), day.timestamp()),
        "this week": ((day - dt.timedelta(days=day.weekday())).timestamp(), None),
    }
    wd = _weekday_window(phrase, day)
    if wd:
        since, until, phrase = wd
    elif re.fullmatch(r"(?:last|previous)\s+week", phrase):
        # bare "last week" is the previous calendar week (Monday to Sunday); "in the last week", "over the last week",
        # "past week" and "last 7 days" are the rolling days up to now
        monday = day - dt.timedelta(days=day.weekday())
        since, until, phrase = (monday - dt.timedelta(days=7)).timestamp(), monday.timestamp(), "last week"
    elif phrase in windows:
        since, until = windows[phrase]
    else:
        lm = LAST_N.search(phrase)
        if not lm:
            return None
        k = float(lm.group(1) or 1) if (lm.group(1) or "").replace(".", "").isdigit() else WORD_NUM.get(lm.group(1) or "", 1)
        since, until = now - k * {"minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400}[lm.group(2)], None
        phrase = lm.group(0)
        if re.fullmatch(r"(?:last|previous)\s+week", phrase):
            phrase = "past week"   # "in the last week": the label reads back as the rolling week, not the calendar one
    if until is not None and until > now:
        until = None
    cleaned = re.sub(r"\s+", " ", (text[:m.start()] + text[m.end():])).strip(" ?.,!") or text
    return {"since": since, "until": until, "label": phrase, "text": cleaned}


def parse_query(text: str, now: float | None = None) -> dict:
    """For Find: the time window, the text to search events with, and what (if anything) to look for in footage."""
    win = time_window(text, now) or {}
    cleaned = win.get("text", text)
    q = text.lower().strip()
    is_question = q.endswith("?") or bool(re.match(r"(did|was|were|is|are|has|have|how|when|what|who|where|which|why|any)\b", q))
    obj = footage_text(cleaned) if is_question else cleaned
    # questions about people in general ("did anyone use the bathroom") are answered by events, not by what frames look like
    # ... and so are questions with nothing to picture ("what happened overnight?"): the image index needs a visual noun
    if is_question and (GENERIC_SUBJECT.search(q) or len(obj) < 3 or not content_words(obj)):
        obj = None
    return {"since": win.get("since"), "until": win.get("until"), "time_label": win.get("label"), "text": cleaned,
            "footage_text": obj, "question": is_question,
            # nothing to search by meaning ("what happened overnight?"): the page should list the period's events instead
            "listing": not content_words(cleaned)}


LAST_N = re.compile(r"\b(?:last|past|previous)\s+(\d+(?:\.\d+)?|one|two|three|four|five|six|seven|twelve|a|an)?\s*(minute|hour|day|week)s?\b")
WORD_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "twelve": 12}


# Lookups the question obviously needs, added in code because a small planner often misses them.
KEYWORD_TOOLS = [
    (r"\b(outside|went out|go(es)? out|came (back )?in|come (back )?in|left the building|walk(ed)? (from|between|to)|"
     r"followed|journey|across cameras|moved between)\b", "list_journeys"),
    (r"\b(unusual|odd|strange|weird|suspicious|out of the ordinary|abnormal|anything wrong)\b", "list_unusual"),
    (r"\b(offline|not recording|stopped recording|recording gap|camera (was )?down|lost (the )?feed)\b", "recording_gaps"),
    (r"\b(how many|count|number of)\b", "count_events"),
    (r"\b(overnight|last night|briefing|summary of)\b", "get_briefing"),
]


LOOK_FOR = re.compile(r"^(was|were|is|are|has|have|had|did (you|anyone|anybody|someone|we|it|the cameras?) (see|spot|catch)|"
                      r"any sign of|find|show me|look for|where('s| is| was| were)|when (was|were|did))\b")
GENERIC_SUBJECT = re.compile(r"\b(anyone|anybody|someone|somebody|people|person|a man|a woman|activity|movement|motion)\b")
FILLER = re.compile(r"\b(on|in|at) (any|the|a|our|my) (camera|cameras|footage|video|recordings?)\b|\b(today|yesterday|tonight|"
                    r"last night|this (morning|afternoon|evening|week)|overnight|recently|ever|there|any)\b|[?.!,]")


NON_VISUAL = re.compile(r"\b(what|who|when|where|why|how|happened|happen|happening|happens|going on|went on|anything|something|"
                        r"everything|nothing|there|did|do|does|was|were|is|are|be|been|the|a|an|it|that|this|these|those|new|else|"
                        r"up|about|around|tell me|show me|give me|report|summary|summarize|status|update|events?|activity|alerts?|"
                        r"anyone|anybody|someone|somebody|all|any|of|on|in|at|to|for|with|and|or|from|since|while|i|we|you|me|us|"
                        r"please|can|could|would|should|see|saw|seen|notice|noticed|observe|observed|record|recorded|camera|cameras|"
                        r"last|past|night|overnight|today|yesterday|morning|afternoon|evening|week|hour|hours|day|days)\b")


def content_words(text: str) -> str:
    """What is left of a phrase once question words and filler are removed: empty for 'what happened overnight'."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", NON_VISUAL.sub(" ", text.lower()))).strip()


WHAT_HAPPENED = re.compile(r"\b(what happened|what('s| is| was| has been) (going on|happening|new|up)|anything (happen|going on|new)|"
                           r"any activity|how was|quiet|busy|summary|briefing|report)\b")


def footage_text(question: str) -> str:
    """'Was a boat on any camera today?' -> 'a boat': the thing to look for, for the image-text index."""
    q = LOOK_FOR.sub("", question.lower().strip())
    q = FILLER.sub(" ", q)
    return re.sub(r"\s+", " ", q).strip()[:120]


# "When was a white pickup last seen?", "last time the BigView truck came by", "when did the mower come?": the most
# recent sighting, from the event records AND the footage index (a footage hit alone once answered Sep 27 when an
# event had the same truck on Oct 5).
LAST_SEEN = re.compile(r"\b(last (seen|spotted|sighted|sighting|time|visit|came|come|showed up|arrived|here|on site|by)|"
                       r"(seen|spotted|here|came|come|visited|showed up|arrived|by) (last|most recently)\b|"
                       r"(most recent|latest) (time|sighting|visit)|when did .{1,80}?\b(come|came|arrive|show up|visit|leave|drive|pull)|"
                       r"when was .{1,80}?\b(seen|spotted|here|on site|around|by))")
LAST_SEEN_FILLER = re.compile(r"\b(last|most recent(ly)?|latest|seen|spotted|sighted|sighting|time|visit(ed)?|came|come|"
                              r"showed up|show up|arrived?|here|on site|around|by|leave|drive|pull|in|out|the|a|an|was|were|is|did)\b")


def last_seen_subject(question: str) -> str:
    """'When was a white pickup truck last seen?' -> 'white pickup truck' (what to search the records for)."""
    q = (time_window(question) or {}).get("text", question)   # the time phrase is a filter, not part of the subject
    return re.sub(r"\s+", " ", LAST_SEEN_FILLER.sub(" ", footage_text(q))).strip()[:120]


def augment(calls: list[dict], question: str, now: float | None = None) -> list[dict]:
    q = question.lower()
    have = {c["tool"] for c in calls}
    base = calls[0]["args"] if calls else {}
    if LAST_SEEN.search(q) and content_words(subject := last_seen_subject(question)):
        # Always search the event records, newest match first, over all history unless the question names a period;
        # it replaces the planner's own event search for the same thing (often with a guessed, narrow window).
        win = time_window(question, now)
        calls[:] = [c for c in calls if not (c["tool"] == "search_events" and c["args"].get("text"))]
        calls.insert(0, {"tool": "search_events", "args": {
            "text": subject, "camera": base.get("camera"), "since": win["since"] if win else None,
            "until": win["until"] if win else None, "label": query_label(question), "min_priority": None,
            "group_by": None, "newest": True}})
        for c in calls:
            if c["tool"] == "search_footage":
                c["args"]["newest"] = True
                if not win:   # no period asked about: all indexed footage, like the event search
                    c["args"]["since"] = c["args"]["until"] = None
        if "search_footage" not in {c["tool"] for c in calls} and not GENERIC_SUBJECT.search(q):
            calls.insert(1, {"tool": "search_footage", "args": {
                "text": subject, "camera": base.get("camera"), "since": win["since"] if win else None,
                "until": win["until"] if win else None, "label": None, "min_priority": None, "group_by": None, "newest": True}})
        have = {c["tool"] for c in calls}
        base = calls[0]["args"]
    about_records = any(re.search(pat, q) for pat, _ in KEYWORD_TOOLS)
    if "search_footage" not in have and LOOK_FOR.search(q.strip()) and not GENERIC_SUBJECT.search(q) and not about_records:
        text = footage_text(next((c["args"]["text"] for c in calls if c["tool"] == "search_events" and c["args"].get("text")), "") or question)
        if len(text) >= 3:
            calls.append({"tool": "search_footage", "args": {"text": text, "camera": base.get("camera"), "since": base.get("since"),
                                                             "until": base.get("until"), "label": None, "min_priority": None, "group_by": None}})
            have.add("search_footage")
    for pattern, tool in KEYWORD_TOOLS:
        if tool not in have and re.search(pattern, q):
            args = {"text": "", "camera": None if tool == "list_journeys" else base.get("camera"),
                    "since": base.get("since"), "until": base.get("until"), "label": base.get("label"),
                    "min_priority": None, "group_by": None}
            calls.append({"tool": tool, "args": args})
            have.add(tool)
    # A question about a period ("what happened overnight?", "was it quiet today?") is answered from that period's
    # events: list the latest ones and count them. A briefing alone spans a different period and reads as hearsay.
    win = time_window(question, now)
    if win and (WHAT_HAPPENED.search(q) or not content_words(win["text"])):
        common = {"camera": base.get("camera"), "since": win["since"], "until": win["until"], "label": base.get("label"),
                  "min_priority": None}
        if not any(c["tool"] == "search_events" and not c["args"].get("text") for c in calls):
            calls.insert(0, {"tool": "search_events", "args": {"text": "", "group_by": None, **common}})
        if "count_events" not in have:
            calls.insert(1, {"tool": "count_events", "args": {"text": "", "group_by": "camera", **common}})
    return calls[:MAX_CALLS + 3]


def describe_call(c: dict) -> str:
    a = c["args"]
    bits = [f'"{a["text"]}"'] if a.get("text") else []
    if a.get("camera"):
        bits.append(_cam_name(a["camera"]))
    if a.get("label"):
        bits.append(a["label"])
    if a.get("since") or a.get("until"):
        s = _when(a["since"]) if a.get("since") else "…"
        u = _when(a["until"]) if a.get("until") else "now"
        bits.append(f"{s} – {u}")
    if a.get("min_priority"):
        bits.append(f"priority {a['min_priority']}+")
    if a.get("group_by"):
        bits.append(f"by {a['group_by']}")
    if a.get("newest"):
        bits.append("newest first")
    return f"{c['tool']}({', '.join(bits)})"


# ---------------------------------------------------------------- tools

def _where(a: dict, extra: list[str] | None = None) -> tuple[str, list]:
    w, p = ["status='verified'", FALSE_ALARM, *(extra or [])], []
    if a.get("camera"):
        w.append("camera_id=?"); p.append(a["camera"])
    if a.get("label"):
        w.append("camera_class=?"); p.append(a["label"])
    if a.get("since") is not None:
        w.append("start_ts>=?"); p.append(a["since"])
    if a.get("until") is not None:
        w.append("start_ts<=?"); p.append(a["until"])
    return " AND ".join(w), p


EVENT_COLS = ("id, camera_id, camera_class, yolo_class, start_ts, end_ts, synopsis, snapshot, priority, anomaly, "
              "anomaly_json, feedback, journey_id, watched, areas, policy, "
              "json_extract(CASE WHEN json_valid(synopsis_json) THEN synopsis_json END, '$.towing_check') AS towing_check, "
              "json_extract(CASE WHEN json_valid(synopsis_json) THEN synopsis_json END, '$.weapon_check') AS weapon_check")


def _coverage(words: list[str], r: dict) -> float:
    """Share of the searched words found in the event's description (plurals count: 'truck' finds 'trucks')."""
    if not words:
        return 0.0
    s = r.get("synopsis_json") if isinstance(r.get("synopsis_json"), dict) else {}
    doc = " ".join(filter(None, [r.get("synopsis"), s.get("activity"), *(s.get("tags") or []),
                                 *(o.get("description", "") for o in s.get("objects") or [] if isinstance(o, dict))])).lower()
    return sum(w in doc for w in words) / len(words)


def newest_first(rows: list[dict], text: str) -> list[dict]:
    """For 'when was X last seen': the best-covering matches, newest first (relevance alone put an older, wordier
    description of the same truck above the latest sighting)."""
    from .db import STOPWORDS
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS and len(w) > 1]
    return sorted(rows, key=lambda r: (-round(_coverage(words, r), 2), -r["start_ts"]))


async def _embed_query(a: dict) -> list[float] | None:
    """The query's embedding. Site Ask (retrieve.py) passes `_embed_s`, the time the embedding may take (the text
    model runs in Ollama and is cold after a restart); 0 or slower = search by keywords only, and `_keyword_only` says so."""
    if "_embed_s" not in a:
        return await vlm.embed(f"search_query: {a['text']}")
    if a["_embed_s"] > 0:
        try:
            return await asyncio.wait_for(vlm.embed(f"search_query: {a['text']}"), a["_embed_s"])
        except asyncio.TimeoutError:
            pass
    a["_keyword_only"] = True
    return None


async def t_search_events(a: dict, refs: Refs) -> tuple[list[str], int]:
    if a.get("text"):
        emb = await _embed_query(a)
        rows = await asyncio.to_thread(db.search, a["text"], emb, 100 if a.get("newest") else 25, a.get("camera"),
                                       a.get("since"), a.get("until"), a.get("label"))
        rows = [r for r in rows if r.get("status") == "verified"]
        if a.get("newest"):
            rows = newest_first(rows, a["text"])
    else:
        where, p = _where(a)
        rows = await asyncio.to_thread(db.all, f"SELECT {EVENT_COLS} FROM events WHERE {where} ORDER BY start_ts DESC LIMIT 25", p)
    if a.get("min_priority"):
        rows = [r for r in rows if PRIORITY_RANK.get(r.get("priority") or "none", 0) >= PRIORITY_RANK[a["min_priority"]]]
    rows = rows[:3 if a.get("earlier") else 12]
    for r in rows:
        refs.event(r)
    if a.get("earlier"):  # the most recent matches before the period asked about
        rows.sort(key=lambda r: -r["start_ts"])
        return (["EARLIER (before the period asked about; nothing matched in that period):"] + [_event_line(r) for r in rows]
                if rows else ["No earlier matches either."]), 0
    if a.get("newest") and rows:
        return (["NEWEST FIRST: the first line is the most recent event matching the description."]
                + [_event_line(r) for r in rows]), len(rows)
    return [_event_line(r) for r in rows] or ["No matching events."], len(rows)


async def t_count_events(a: dict, refs: Refs) -> tuple[list[str], int]:
    loc = sql_local()
    expr = {"hour": f"strftime('%H:00', start_ts, 'unixepoch', {loc})", "day": f"strftime('%Y-%m-%d', start_ts, 'unixepoch', {loc})",
            "camera": "camera_id", "label": "camera_class"}.get(a.get("group_by") or "", "'all'")
    where, p = _where(a)
    rows = await asyncio.to_thread(db.all, f"SELECT {expr} AS k, camera_class, COUNT(*) AS n FROM events WHERE {where} "
                                            f"GROUP BY k, camera_class ORDER BY k", p)
    total = sum(r["n"] for r in rows)
    groups: dict[str, list[str]] = {}
    for r in rows:
        k = _cam_name(r["k"]) if a.get("group_by") == "camera" else r["k"]
        groups.setdefault(k, []).append(f"{r['n']} {r['camera_class']} sightings")
    lines = []
    if a.get("label") in (None, "person"):
        where_p, pp = _where({**a, "label": "person"})
        ids = [r["id"] for r in await asyncio.to_thread(db.all, f"SELECT id FROM events WHERE {where_p} ORDER BY start_ts LIMIT 500", pp)]
        est = None
        if "_people_s" not in a:
            est = await asyncio.to_thread(distinct_people, ids)
        elif ids and a["_people_s"] > 0:   # Site Ask: the estimate gets what time is left (retrieve.py)
            try:
                est = await asyncio.wait_for(asyncio.to_thread(distinct_people, ids), a["_people_s"])
            except asyncio.TimeoutError:
                a["_people_skipped"] = True
        elif ids:
            a["_people_skipped"] = True
        if est:
            lo, hi, n = est
            span = f"about {lo}" if lo == hi else f"between {lo} and {hi}"
            lines.append(f"Number of different people: {span} (matched by appearance). Answer 'how many people' with this.")
    lines.append(f"Sightings: {total} verified camera events in total (one person can make several; false alarms excluded).")
    if a.get("group_by"):
        lines += [f"{k}: {', '.join(v)}" for k, v in groups.items()]
    elif len(rows) > 1:  # several types: say how the sightings split
        lines.append("By type: " + ", ".join(x for v in groups.values() for x in v))
    return lines, total


# re-ID cosine thresholds for "same person" (measured here: same person ~0.8-0.9, different people median ~0.65).
# The fingerprint also depends on the camera view, so the answer is a range from a strict and a loose threshold.
PEOPLE_STRICT, PEOPLE_LOOSE = 0.80, 0.72


def _average_link_counts(S, thresholds: list[float]) -> dict[float, int]:
    """Number of clusters when average-linkage merging stops at each threshold (Lance-Williams updates)."""
    import numpy as np
    M = S.astype(np.float64).copy()
    n = len(M)
    size = np.ones(n)
    alive = np.ones(n, bool)
    np.fill_diagonal(M, -np.inf)
    out, clusters = {}, n
    for th in sorted(thresholds, reverse=True):
        while clusters > 1:
            i, j = divmod(int(np.argmax(M)), n)
            if M[i, j] < th:
                break
            merged = (size[i] * M[i] + size[j] * M[j]) / (size[i] + size[j])
            M[i], M[:, i] = merged, merged
            M[i, i] = -np.inf
            M[j], M[:, j] = -np.inf, -np.inf
            size[i] += size[j]
            alive[j] = False
            clusters -= 1
        out[th] = clusters
    return out


def distinct_people(event_ids: list[int]) -> tuple[int, int, int] | None:
    """Rough number of different people among person sightings, from their re-ID fingerprints, with confirmed
    cross-camera journeys counted as one person. Returns (fewest, most, sightings used) or None."""
    import numpy as np
    pairs = [(i, v) for i in event_ids if (v := db.get_reid(i)) is not None]
    if len(pairs) < max(2, len(event_ids) // 2):
        return None
    ids = [i for i, _ in pairs]
    V = np.stack([v for _, v in pairs]).astype(np.float64)
    V /= np.linalg.norm(V, axis=1, keepdims=True)
    S = V @ V.T
    pos = {e: k for k, e in enumerate(ids)}
    marks = ",".join("?" * len(ids))
    for grp in db.all(f"SELECT journey_id, GROUP_CONCAT(id) AS ids FROM events WHERE id IN ({marks}) AND journey_id IS NOT NULL "
                      "GROUP BY journey_id", ids):
        idx = [pos[int(x)] for x in grp["ids"].split(",")]
        for a in idx:
            for b in idx:
                if a != b:
                    S[a, b] = 1.0  # same journey = same person
    c = _average_link_counts(S, [PEOPLE_STRICT, PEOPLE_LOOSE])
    return c[PEOPLE_LOOSE], c[PEOPLE_STRICT], len(ids)


async def t_list_unusual(a: dict, refs: Refs) -> tuple[list[str], int]:
    where, p = _where(a, [f"(COALESCE(anomaly, 0) >= {baseline.PRIORITY_LOW} OR priority IN ('medium', 'high'))"])
    rows = await asyncio.to_thread(db.all, f"SELECT {EVENT_COLS} FROM events WHERE {where} "
                                            "ORDER BY COALESCE(anomaly, 0) DESC, start_ts DESC LIMIT 12", p)
    for r in rows:
        refs.event(r)
    learning = [s["camera_id"] for s in baseline.status() if s["learning"]]
    note = [f"(Still learning what's normal for: {', '.join(_cam_name(c) for c in learning)}.)"] if learning else []
    return ([_event_line(r) for r in rows] or ["No unusual events."]) + note, len(rows)


async def t_list_journeys(a: dict, refs: Refs) -> tuple[list[str], int]:
    w, p = ["synopsis IS NOT NULL"], []
    if a.get("since") is not None:
        w.append("last_ts>=?"); p.append(a["since"])
    if a.get("until") is not None:
        w.append("first_ts<=?"); p.append(a["until"])
    js = await asyncio.to_thread(db.all, f"SELECT * FROM journeys WHERE {' AND '.join(w)} ORDER BY first_ts DESC LIMIT 8", p)
    lines = []
    for j in js:
        members = db.all(f"SELECT {EVENT_COLS} FROM events WHERE journey_id=? ORDER BY start_ts", [j["id"]])
        for m in members:
            refs.event(m)
        cams = " → ".join(_cam_name(c) for c in json.loads(j["cameras"] or "[]"))
        cites = " ".join(f"[#{m['id']}]" for m in members[1:])
        head = f"[#{members[0]['id']}] " if members else ""
        lines.append(f"{head}Journey {_when(j['first_ts'])} across {cams} (also {cites}): {j['synopsis']}")
    return lines or ["No cross-camera journeys."], len(js)


async def t_search_footage(a: dict, refs: Refs) -> tuple[list[str], int]:
    if ctx.footage is None:
        return ["Footage search is not available."], 0
    from .footage import moments
    vec = await ctx.footage.embed_text(a.get("text") or "")
    cams = [a["camera"]] if a.get("camera") else [c["id"] for c in db.cameras()]
    hits = await asyncio.to_thread(ctx.footage.index.search, vec, cams, a.get("since"), a.get("until"), 300)
    ms = moments(hits, 6)
    if not ms:
        return ["No footage indexed for that range."], 0
    from .footage import tile_jpeg
    # Visual similarity alone is often wrong: Qwen looks at the best few, and only confirmed ones are passed on.
    lines, checked = [], 0
    for m in ms[:FOOTAGE_VERIFY]:
        img = await asyncio.to_thread(tile_jpeg, m["camera_id"], m["ts"], m["tile"])
        if not img:
            continue
        try:
            r = await vlm.footage_match(img, a.get("text") or "")
        except Exception as e:  # noqa: BLE001
            log.warning("footage check failed: %s", e)
            continue
        checked += 1
        if r.get("matches") and r.get("confidence") in ("medium", "high"):
            lines.append((m["ts"], f"[{refs.moment(m)}] {_cam_name(m['camera_id'])}, {_when(m['ts'])}"
                                   + (f" (for {m['end'] - m['start']:.0f} s)" if m["end"] - m["start"] >= 5 else "")
                                   + f" - CHECKED: yes, {r.get('seen') or 'matches'}"))
    if a.get("newest"):  # "last seen": the latest confirmed moment first
        lines.sort(key=lambda x: -x[0])
    head = (f"Footage search: Qwen checked the best {checked} visual matches; {len(lines)} actually show it."
            + (" Newest first." if a.get("newest") and lines else "")
            if checked else "Footage search: the matches could not be checked.")
    return [head, *(l for _, l in lines)], len(lines)


def _recording_spans(camera_id: str) -> list[tuple[float, float]]:
    segs = retention.camera_segments(camera_id)
    spans = []
    for i, (st, f) in enumerate(segs):
        nxt = segs[i + 1][0] if i + 1 < len(segs) else None
        try:
            end = min(f.stat().st_mtime, nxt) if nxt else f.stat().st_mtime
        except OSError:
            continue
        spans.append((st, end))
    return spans


async def t_recording_gaps(a: dict, refs: Refs) -> tuple[list[str], int]:
    now = time.time()
    lo, hi = a.get("since") or now - 86400, a.get("until") or now
    cams = [a["camera"]] if a.get("camera") else [c["id"] for c in db.cameras(enabled_only=True)]
    lines = []
    for cid in cams:
        spans = await asyncio.to_thread(_recording_spans, cid)
        prev_end, gaps = None, []
        if spans and spans[0][0] > lo + 120:
            lines.append(f"{_cam_name(cid)}: recordings only start {_when(spans[0][0])} (camera added then, or older footage deleted)")
            lo = max(lo, spans[0][0])
        for st, en in spans:
            if prev_end is not None and st - prev_end > 120 and st >= lo and prev_end <= hi:
                gaps.append((prev_end, st))
            prev_end = en if prev_end is None else max(prev_end, en)
        if prev_end is not None and hi - prev_end > 120 and hi >= now - 60:
            gaps.append((prev_end, hi))
        for g0, g1 in gaps:
            lines.append(f"{_cam_name(cid)} not recording from {_when(g0)} to {_when(g1)} ({(g1 - g0) / 60:.0f} min)")
        if not gaps:
            lines.append(f"{_cam_name(cid)}: recorded continuously since {_when(max(lo, spans[0][0]))}" if spans
                         else f"{_cam_name(cid)}: no recordings found")
    return lines, len(lines)


async def t_get_briefing(a: dict, refs: Refs) -> tuple[list[str], int]:
    if a.get("since") is not None:
        b = db.one("SELECT * FROM briefings WHERE period_end >= ? ORDER BY period_end ASC LIMIT 1", [a["since"]])
    else:
        b = db.one("SELECT * FROM briefings ORDER BY created_at DESC LIMIT 1")
    if not b:
        return ["No briefing has been written for that period yet."], 0
    stats = json.loads(b["stats"] or "{}")
    for eid, r in (stats.get("refs", {}).get("events") or {}).items():
        refs.events[int(eid)] = r
    # Count 0: a briefing is background. It spans its own period (often a whole day), so its times and sightings
    # must not be presented as the answer for the period asked about; the event lookups are.
    return [f"BACKGROUND ONLY: a briefing written for {_when(b['period_start'])} to {_when(b['period_end'])}, a different span "
            f"than the question may ask about. Do not report its times or sightings as events of the asked period. {b['headline']}",
            b["text"]], 0


TOOL_FUNCS = {"search_events": t_search_events, "count_events": t_count_events, "list_unusual": t_list_unusual,
              "list_journeys": t_list_journeys, "search_footage": t_search_footage, "recording_gaps": t_recording_gaps,
              "get_briefing": t_get_briefing}


def fallback_call(calls: list[dict], question: str) -> dict | None:
    """When every lookup came back empty: first one plain search of all cameras with the question text (keeping
    the time window), which usually finds what an over-filtered plan missed; then, if the question was about a
    period, the most recent matches before it, so the answer can say "not today; last time was yesterday 16:53"."""
    if any(c["count"] for c in calls):
        return None
    text = (time_window(question) or {}).get("text", question)[:200]
    if not content_words(text):
        text = ""  # "what happened overnight": nothing to search by meaning; list the latest events instead
    since = min((c["args"].get("since") for c in calls if c["args"].get("since") is not None), default=None)
    args = {"text": text, "camera": None, "since": since, "until": None, "label": query_label(question),
            "min_priority": None, "group_by": None}
    tried = {json.dumps(c["args"], sort_keys=True) for c in calls if c["tool"] == "search_events"}
    if json.dumps(args, sort_keys=True) not in tried:
        return {"tool": "search_events", "args": args}
    if since is not None and not any(c["args"].get("earlier") for c in calls):
        return {"tool": "search_events", "args": {**args, "since": None, "until": since, "earlier": True}}
    return None


async def run_calls(calls: list[dict], refs: Refs, question: str | None = None) -> tuple[str, list[dict]]:
    blocks, summary = [], []
    queue = list(calls)
    while queue:
        c = queue.pop(0)
        try:
            lines, n = await TOOL_FUNCS[c["tool"]](c["args"], refs)
        except Exception as e:  # a broken lookup shouldn't sink the answer
            log.exception("assistant tool %s failed", c["tool"])
            lines, n = [f"(lookup failed: {e})"], 0
        blocks.append(f"### {describe_call(c)}\n" + "\n".join(lines))
        summary.append({"tool": c["tool"], "args": c["args"], "label": describe_call(c), "count": n})
        if not queue and question and (fb := fallback_call(summary, question)):
            queue.append(fb)
    if any(s["count"] for s in summary):  # a later lookup found something: drop the empty ones so they aren't parroted
        blocks = [b for b, s in zip(blocks, summary) if s["count"] or not b.rstrip().endswith("No matching events.")]
    text = "\n\n".join(blocks)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n(results truncated)"
    return text, summary


# ---------------------------------------------------------------- answer

ANSWER_SYSTEM = (
    "You are the assistant of an AI security camera system (NVR) for one site. Answer the operator's question using "
    "ONLY the lookup results provided. Each result line starts with a handle in square brackets; when you mention "
    "that sighting, copy its handle exactly (only handles from the list you are given). Use the local times shown. "
    "Be brief and concrete: a direct answer first, then the supporting sightings. If the results don't answer the "
    "question, say so plainly and say what was checked. Never invent events, times or counts. Footage matches only "
    "count if they say CHECKED: yes. If the only matches are marked EARLIER, say nothing matched in the period asked "
    "about and mention the most recent earlier one with its date. Counts are sightings, not different people. Cite only the few sightings that "
    "support your answer (at most 5). Don't guess identities. Write plain sentences in American English spelling; don't repeat these instructions. "
    "When a 'Period asked about' is given, open with that period and how many events the lookups found in it "
    "(for example 'Oct 2 18:00 to Oct 3 07:00: no events.'). A result marked BACKGROUND ONLY is a briefing that "
    "covers a different span: never present its times, people or activity as happening in the period asked about, "
    "and never take a 'most recent sighting' from it; only event lines and EARLIER lines are sightings. "
    "For 'when was X last seen' questions, compare the dates of ALL matching event lines and CHECKED footage lines and "
    "answer with the most recent one (results marked NEWEST FIRST list their latest match first); mention an older "
    "match only as extra context."
)


def _period_note(question: str, now: float) -> str:
    win = time_window(question, now)
    if not win:
        return ""
    until = win["until"] if win["until"] is not None else now
    f = lambda t: _local(t).strftime("%a %b %d %H:%M")
    return f"Period asked about ({win['label']}): {f(win['since'])} to {f(until)}.\n"


def _handles_note(refs: "Refs") -> str:
    hs = [f"[#{i}]" for i in refs.events] + [f"[{k}]" for k in refs.footage]
    if not hs:
        return "There are no citable sightings in these results; don't use any [..] handles."
    return "Handles you may cite: " + ", ".join(hs[:40]) + "."


async def ask(thread_id: int | None, question: str) -> AsyncIterator[dict]:
    """Streams NDJSON-able chunks: thread, user, plan, calls, model, fallback?, delta..., done | error."""
    now = time.time()
    if not thread_id:
        thread_id = db.execute_insert("INSERT INTO assistant_threads (title, created_at, updated_at) VALUES (?,?,?)",
                                      [question[:80], now, now])
    history = [{"role": m["role"], "content": m["content"]} for m in
               db.all("SELECT role, content FROM assistant_messages WHERE thread_id=? ORDER BY id", [thread_id])][-HISTORY_TURNS:]
    user_id = db.execute_insert("INSERT INTO assistant_messages (thread_id, role, content, ts) VALUES (?,?,?,?)",
                                [thread_id, "user", question, now])
    db.execute("UPDATE assistant_threads SET updated_at=? WHERE id=?", [now, thread_id])
    yield {"type": "thread", "thread_id": thread_id}
    yield {"type": "user", "id": user_id}
    answer, model, calls_meta = "", None, {}
    try:
        system, text = _plan_prompt(question, history, now)
        raw = await vlmroute.router.chat_json("assistant", system, text, [], PLAN_SCHEMA, 300, 0.1, "chat")
        calls = check_plan(raw, question, now)
        refs = Refs()
        results, summary = await run_calls(calls, refs, question)
        calls_meta = {"calls": summary, "refs": refs.as_dict(), "planner": raw.get("_model")}
        yield {"type": "calls", **calls_meta}
        messages = [{"role": "system", "content": ANSWER_SYSTEM + " " + _handles_note(refs)},
                    *({"role": m["role"], "content": m["content"][:600]} for m in history),
                    {"role": "user", "content": f"{_period_note(question, now)}Lookup results:\n{results}\n\nQuestion: {question}"}]
        fallback = None
        async for kind, data in vlmroute.router.stream("assistant", messages, 500, 0.2, "chat"):
            if kind == "model":
                model = data
                yield {"type": "model", "model": data}
            elif kind == "fallback":
                fallback = data
                yield {"type": "fallback", "reason": data}
            else:
                answer += data
                yield {"type": "delta", "text": data}
        if fallback:
            calls_meta["fallback"] = fallback
        msg_id = db.execute_insert("INSERT INTO assistant_messages (thread_id, role, content, calls, model, ts) VALUES (?,?,?,?,?,?)",
                                   [thread_id, "assistant", answer.strip(), json.dumps(calls_meta), model, time.time()])
        yield {"type": "done", "id": msg_id}
    except Exception as ex:  # surface errors in the UI instead of a broken stream
        log.exception("assistant question failed")
        if answer:
            db.execute_insert("INSERT INTO assistant_messages (thread_id, role, content, calls, model, ts) VALUES (?,?,?,?,?,?)",
                              [thread_id, "assistant", answer.strip() + " [interrupted]", json.dumps(calls_meta), model, time.time()])
        yield {"type": "error", "error": str(ex)}


def thread(thread_id: int) -> dict | None:
    t = db.one("SELECT * FROM assistant_threads WHERE id=?", [thread_id])
    if not t:
        return None
    msgs = db.all("SELECT * FROM assistant_messages WHERE thread_id=? ORDER BY id", [thread_id])
    for m in msgs:
        m["calls"] = json.loads(m["calls"]) if m["calls"] else None
    return {**t, "messages": msgs}


# ---------------------------------------------------------------- briefings

BRIEFING_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "one line, at most 15 words"},
        "bullets": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8,
                    "description": "short factual bullets; cite events as [#id]"},
    },
    "required": ["headline", "bullets"],
}
BRIEFING_SYSTEM = (
    "You write the morning briefing for the operator of an AI security camera system. From the facts given, write a "
    "one-line headline and 1-4 short bullets about what needs attention (broken site rules, PPE violations, high "
    "priority or unusual events, cameras offline, disk problems) and one bullet summing up the period in plain words. "
    "When the facts have a 'PPE:' line with violations, always give PPE its own bullet: how many violations, in which "
    "zone and when, citing one or two of the examples; workplace safety breaches matter as much as security events. "
    "Activity counts, journeys, recording and disk status are listed separately below your bullets, so don't repeat "
    "those numbers. Cite events as [#id] exactly as given. Only use the facts provided; if it was quiet, say so. "
    "Use American English spelling."
)

ATTENTION_MAX = 8      # lines in the briefing's "Needs attention" list
ATTENTION_EACH = 3     # reserved per category, so one kind (e.g. ten long stays) can't crowd out the others
PPE_VERDICT_SQL = "json_extract(CASE WHEN json_valid(detections) THEN detections END, '$.ppe.verdict')"
PPE_FIELD_SQL = "json_extract(CASE WHEN json_valid(detections) THEN detections END, '$.ppe.{}')"


def _span(t0: float, t1: float) -> str:
    a, b = dt.datetime.fromtimestamp(t0), dt.datetime.fromtimestamp(t1)
    return f"{a:%H:%M}–{b:%H:%M}" if a.date() == b.date() else f"{_when(t0)} – {_when(t1)}"


def _spread(rows: list, n: int = 3) -> list:
    """n examples spread over the list (first, middle, last)."""
    if len(rows) <= n:
        return list(rows)
    return [rows[round(i * (len(rows) - 1) / (n - 1))] for i in range(n)]


def ppe_summary(where: str, p: list, refs: Refs) -> list[dict]:
    """PPE violations in the period, one entry per camera and zone, counted from the events' PPE check."""
    from . import ppe
    rows = db.all(f"SELECT id, camera_id, camera_class, start_ts, snapshot, {PPE_FIELD_SQL.format('zone')} AS zone, "
                  f"{PPE_FIELD_SQL.format('violation')} AS missing FROM events WHERE {where} AND {PPE_VERDICT_SQL}='violation' "
                  f"ORDER BY start_ts", p)
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["camera_id"], r["zone"] or "PPE zone"), []).append(r)
    out = []
    for (cam, zone), rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        missing: dict[str, int] = {}
        for r in rs:
            for item in json.loads(r["missing"] or "[]"):
                missing[item] = missing.get(item, 0) + 1
        examples = _spread(rs)
        for r in examples:
            refs.event(r)
        out.append({"camera_id": cam, "zone": zone, "count": len(rs), "first": rs[0]["start_ts"], "last": rs[-1]["start_ts"],
                    "missing": {ppe.ITEM_WORDS.get(k, k): v for k, v in missing.items()},
                    "examples": [r["id"] for r in examples]})
    return out


def _has_ppe_zones() -> bool:
    from . import ppe
    return any(ppe.ppe_zones(c.get("zones")) for c in db.cameras(enabled_only=True))


def ppe_line(s: dict) -> str:
    items = " or ".join(s["missing"]) or "required PPE"
    detail = ", ".join(f"{v} without a {k}" for k, v in s["missing"].items())
    return (f"PPE: {s['count']} violation{'s' if s['count'] != 1 else ''} (people without a {items}) in PPE zone "
            f"'{s['zone']}' ({_cam_name(s['camera_id'])}), {_span(s['first'], s['last'])}"
            + (f": {detail}" if len(s["missing"]) > 1 else "")
            + "; examples " + " ".join(f"[#{i}]" for i in s["examples"]))


def rules_summary(where: str, p: list, refs: Refs) -> list[dict]:
    """Broken site rules in the period by kind: count, highest priority, a few examples and one rule text."""
    rows = db.all(f"SELECT id, camera_id, camera_class, start_ts, snapshot, policy FROM events WHERE {where} "
                  f"AND policy IS NOT NULL ORDER BY start_ts", p)
    kinds: dict[str, list] = {}
    for r in rows:
        pol = json.loads(r["policy"]) if isinstance(r["policy"], str) else r["policy"]
        if pol:
            kinds.setdefault(pol.get("kind") or "rule", []).append((r, pol))
    out = []
    for kind, items in sorted(kinds.items(), key=lambda kv: -max(PRIORITY_RANK.get(p_.get("priority") or "none", 0) for _, p_ in kv[1])):
        examples = _spread([r for r, _ in items])
        for r in examples:
            refs.event(r)
        top = max(items, key=lambda x: PRIORITY_RANK.get(x[1].get("priority") or "none", 0))
        out.append({"kind": kind, "count": len(items), "priority": top[1].get("priority") or "none",
                    "text": top[1].get("text", ""), "examples": [r["id"] for r in examples]})
    return out


def rules_line(rs: list[dict]) -> str:
    parts = []
    for s in rs:
        name = "PPE" if s["kind"] == "ppe" else s["kind"]
        cites = " ".join(f"[#{i}]" for i in s["examples"])
        what = " (see the PPE line)" if s["kind"] == "ppe" else (f": {s['text'][:160]}" if s["text"] else "")
        parts.append(f"{name} {s['count']} ({s['priority']} priority) {cites}{what}")
    return "Site rules broken: " + "; ".join(parts)


def attention(where: str, p: list) -> list[dict]:
    """The 'Needs attention' events, with room reserved per category: broken rules (other than PPE, which has its
    own summary line), high priority, unusual for the camera; then the rest by priority. PPE violations only fill
    left-over room: they are summarized above the list."""
    rows = db.all(f"SELECT {EVENT_COLS}, {PPE_VERDICT_SQL} AS ppe_verdict FROM events WHERE {where} "
                  f"AND (priority IN ('low','medium','high') OR COALESCE(anomaly,0) >= ? OR policy IS NOT NULL) "
                  f"ORDER BY CASE priority WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END DESC, "
                  f"COALESCE(anomaly, 0) DESC, start_ts DESC LIMIT 500", [*p, baseline.PRIORITY_LOW])
    pol = lambda r: (json.loads(r["policy"]) if isinstance(r["policy"], str) else r["policy"]) or {}
    is_ppe = lambda r: r.get("ppe_verdict") == "violation" or pol(r).get("kind") == "ppe"
    buckets = [
        [r for r in rows if r["policy"] and pol(r).get("kind") != "ppe"],
        [r for r in rows if r["priority"] == "high"],
        sorted((r for r in rows if (r["anomaly"] or 0) >= baseline.PRIORITY_LOW), key=lambda r: -(r["anomaly"] or 0)),
    ]
    picked: dict[int, dict] = {}
    for b in buckets:
        for r in [r for r in b if r["id"] not in picked][:ATTENTION_EACH]:
            picked[r["id"]] = r
    for r in [r for r in rows if not is_ppe(r)] + [r for r in rows if is_ppe(r)]:
        if len(picked) >= ATTENTION_MAX:
            break
        picked.setdefault(r["id"], r)
    return list(picked.values())[:ATTENTION_MAX]


def briefing_settings() -> dict:
    return {"enabled": True, "time": "07:00", **(db.get_setting("briefing") or {})}


def gather_facts(start: float, end: float) -> tuple[str, dict, Refs]:
    refs = Refs()
    a = {"since": start, "until": end}
    where, p = _where(a)
    counts = db.all(f"SELECT camera_id, camera_class, COUNT(*) AS n FROM events WHERE {where} GROUP BY camera_id, camera_class", p)
    fa = db.one("SELECT COUNT(*) AS n FROM events WHERE status='verified' AND json_extract(feedback, '$.verdict')='false_alarm' "
                "AND start_ts BETWEEN ? AND ?", [start, end])["n"]
    hours = db.all(f"SELECT camera_id, strftime('%H', start_ts, 'unixepoch', 'localtime') AS h, COUNT(*) AS n FROM events "
                   f"WHERE {where} GROUP BY camera_id, h ORDER BY n DESC", p)
    busiest: dict[str, tuple[str, int]] = {}
    for r in hours:
        busiest.setdefault(r["camera_id"], (r["h"], r["n"]))
    ppe_s = ppe_summary(where, p, refs)
    rules_s = rules_summary(where, p, refs)
    top = attention(where, p)
    for e in top:
        refs.event(e)
    journeys = db.all("SELECT * FROM journeys WHERE synopsis IS NOT NULL AND first_ts BETWEEN ? AND ? ORDER BY first_ts LIMIT 5", [start, end])
    for j in journeys:
        for m in db.all(f"SELECT {EVENT_COLS} FROM events WHERE journey_id=? ORDER BY start_ts LIMIT 1", [j["id"]]):
            refs.event(m)
            j["first_event"] = m["id"]
    gaps = []
    for c in db.cameras(enabled_only=True):
        prev = None
        spans = _recording_spans(c["id"])
        if spans and spans[0][0] > start + 300:
            gaps.append(f"{c['name']} recordings start {_when(spans[0][0])}")
        for st, en in spans:
            if prev is not None and st - prev > 300 and st >= start and prev <= end:
                gaps.append(f"{c['name']} not recording {_when(prev)} – {_when(st)} ({(st - prev) / 60:.0f} min)")
            prev = en if prev is None else max(prev, en)
    disk = shutil.disk_usage(settings.recordings_dir)
    free_gb = disk.free / 1e9
    lines = [f"Period: {_when(start)} to {_when(end)}."]
    by_cam: dict[str, list[str]] = {}
    for r in counts:
        by_cam.setdefault(r["camera_id"], []).append(f"{r['n']} {r['camera_class']}")
    lines.append("Activity: " + ("; ".join(f"{_cam_name(c)}: {', '.join(v)}" + (f" (busiest around {busiest[c][0]}:00)" if c in busiest else "")
                                           for c, v in by_cam.items()) or "no verified people or vehicles"))
    if fa:
        lines.append(f"Operator-marked false alarms: {fa}.")
    # PPE and broken rules are summarized before the capped list, so they can't be crowded out of it
    lines += [ppe_line(s) for s in ppe_s]
    if not ppe_s and _has_ppe_zones():
        lines.append("PPE: no violations in the PPE zones.")
    if rules_s:
        lines.append(rules_line(rules_s))
    lines.append("Needs attention:" if top else "Needs attention: nothing flagged (no unusual or elevated-priority events).")
    lines += ["- " + _event_line(e) for e in top]
    if journeys:
        lines.append("Cross-camera journeys:")
        lines += [f"- [#{j.get('first_event', '?')}] {_when(j['first_ts'])}: {j['synopsis'][:220]}" for j in journeys]
    lines.append("Recording: " + ("; ".join(gaps) if gaps else "all cameras recorded continuously."))
    lines.append(f"Recording disk: {free_gb:.0f} GB free." + (" Retention alert: the continuous window can't be held." if retention.alert else ""))
    fixed = []
    for c, v in by_cam.items():
        people = ""
        pids = [r["id"] for r in db.all(f"SELECT id FROM events WHERE {where} AND camera_id=? AND camera_class='person'", [*p, c])]
        if (est := distinct_people(pids)):
            people = f" (~{est[0]} people)" if est[0] == est[1] else f" (~{est[0]}-{est[1]} people)"
        fixed.append(f"{_cam_name(c)}: " + ", ".join(x + (" sightings" + people if x.endswith("person") else "") for x in v)
                     + (f"; busiest around {busiest[c][0]}:00" if c in busiest else ""))
    for s in ppe_s:
        fixed.append(f"PPE: {s['count']} violation{'s' if s['count'] != 1 else ''} in '{s['zone']}' ({_cam_name(s['camera_id'])}), "
                     f"{_span(s['first'], s['last'])} " + " ".join(f"[#{i}]" for i in s["examples"]))
    if top:
        fixed.append("Flagged for review: " + " ".join(f"[#{e['id']}]" for e in top))
    for j in journeys:
        route = " → ".join(_cam_name(x) for x in json.loads(j["cameras"] or "[]"))
        fixed.append(f"[#{j.get('first_event', '?')}] Journey {_when(j['first_ts'])}: {route}")
    fixed.append("Recording: " + ("; ".join(gaps) if gaps else "all cameras recorded continuously"))
    fixed.append(f"Disk: {free_gb:,.0f} GB free" + (" - retention alert" if retention.alert else ""))
    stats = {"counts": counts, "false_alarms": fa, "top": [e["id"] for e in top], "journeys": [j["id"] for j in journeys],
             "gaps": gaps, "free_gb": round(free_gb, 1), "fixed": fixed, "ppe": ppe_s,
             "rules": {s["kind"]: s["count"] for s in rules_s}}
    return "\n".join(lines), stats, refs


async def generate_briefing(start: float | None = None, end: float | None = None) -> dict:
    end = end or time.time()
    if start is None:
        last = db.one("SELECT period_end FROM briefings ORDER BY period_end DESC LIMIT 1")
        start = max(last["period_end"] if last else 0, end - 86400)
    facts, stats, refs = await asyncio.to_thread(gather_facts, start, end)
    r = await vlmroute.router.chat_json("briefing", BRIEFING_SYSTEM, facts + "\n\nWrite the briefing as JSON.", [],
                                        BRIEFING_SCHEMA, 500, 0.2, "background")
    headline = (r.get("headline") or "Briefing").strip()
    bullets = [b.strip().lstrip("-• ").strip() for b in (r.get("bullets") or []) if b and b.strip()]
    text = "\n".join(f"- {b}" for b in [*bullets, *stats["fixed"]])
    stats["refs"] = refs.as_dict()
    bid = db.execute_insert("INSERT INTO briefings (period_start, period_end, headline, text, stats, model, created_at) "
                            "VALUES (?,?,?,?,?,?,?)", [start, end, headline, text, json.dumps(stats), r.get("_model"), time.time()])
    log.info("briefing %s (%s): %s", bid, r.get("_model"), headline)
    b = briefing(bid)
    if ctx.pipeline:
        ctx.pipeline.publish_msg({"type": "briefing", "briefing": b})
    return b


def briefing(bid: int) -> dict | None:
    b = db.one("SELECT * FROM briefings WHERE id=?", [bid])
    if b:
        b["stats"] = json.loads(b["stats"] or "{}")
    return b


def briefings(limit: int = 10) -> list[dict]:
    rows = db.all("SELECT id FROM briefings ORDER BY created_at DESC LIMIT ?", [limit])
    return [briefing(r["id"]) for r in rows]


async def briefing_loop(pipeline) -> None:
    """Write the day's briefing at the configured local time (checked every minute)."""
    while True:
        await asyncio.sleep(60)
        try:
            cfg = briefing_settings()
            if not cfg["enabled"] or not pipeline.vlm_ready:
                continue
            hh, mm = (int(x) for x in cfg["time"].split(":"))
            now = dt.datetime.now()
            due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now < due:
                continue
            done = db.one("SELECT 1 FROM briefings WHERE created_at >= ?", [due.timestamp()])
            if not done:
                await generate_briefing()
        except Exception:
            log.exception("briefing failed")
            await asyncio.sleep(600)
