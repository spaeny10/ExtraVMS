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
import datetime as dt
import difflib
import json
import logging
import re
import shutil
import time
from typing import AsyncIterator

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


# ---------------------------------------------------------------- formatting helpers

def _cameras() -> dict[str, dict]:
    return {c["id"]: c for c in db.cameras()}


def _cam_name(cid: str) -> str:
    return (_cameras().get(cid) or {}).get("name", cid)


def _when(ts: float, now: float | None = None) -> str:
    t = dt.datetime.fromtimestamp(ts)
    today = dt.datetime.fromtimestamp(now or time.time()).date()
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
    if e.get("watched"):
        extra.append(f"WATCH LIST: {e['watched']}")
    if e.get("priority") and e["priority"] != "none":
        extra.append(f"priority {e['priority']}")
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
    n = dt.datetime.fromtimestamp(now)
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
            return dt.datetime.strptime(s[:19], fmt).timestamp()
        except ValueError:
            continue
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)  # bare time = today
    if m:
        return dt.datetime.fromtimestamp(now).replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0).timestamp()
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
        key = (tool, json.dumps(args, sort_keys=True))
        if key not in {(o["tool"], json.dumps(o["args"], sort_keys=True)) for o in out}:
            out.append({"tool": tool, "args": args})
    if not out:
        out = [{"tool": "search_events", "args": {"text": question[:200], "camera": None, "since": None, "until": None,
                                                  "label": None, "min_priority": None, "group_by": None}}]
    m = LAST_N.search(question.lower())
    if m:  # an explicit "last N hours/days" beats whatever window the planner picked
        n = float(m.group(1) or 1) if (m.group(1) or "").replace(".", "").isdigit() else WORD_NUM.get(m.group(1) or "", 1)
        since = now - n * {"minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400}[m.group(2)]
        for c in out:
            c["args"]["since"], c["args"]["until"] = since, None
    return augment(out, question)


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


def footage_text(question: str) -> str:
    """'Was a boat on any camera today?' -> 'a boat': the thing to look for, for the image-text index."""
    q = LOOK_FOR.sub("", question.lower().strip())
    q = FILLER.sub(" ", q)
    return re.sub(r"\s+", " ", q).strip()[:120]


def augment(calls: list[dict], question: str) -> list[dict]:
    q = question.lower()
    have = {c["tool"] for c in calls}
    base = calls[0]["args"] if calls else {}
    about_records = any(re.search(pat, q) for pat, _ in KEYWORD_TOOLS)
    if "search_footage" not in have and LOOK_FOR.search(q.strip()) and not GENERIC_SUBJECT.search(q) and not about_records:
        text = next((c["args"]["text"] for c in calls if c["tool"] == "search_events" and c["args"].get("text") and c["args"]["text"] != question[:200]), "") or footage_text(question)
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
    return calls[:MAX_CALLS + 2]


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
              "anomaly_json, feedback, journey_id, watched")


async def t_search_events(a: dict, refs: Refs) -> tuple[list[str], int]:
    if a.get("text"):
        emb = await vlm.embed(f"search_query: {a['text']}")
        rows = await asyncio.to_thread(db.search, a["text"], emb, 25, a.get("camera"), a.get("since"), a.get("until"), a.get("label"))
        rows = [r for r in rows if r.get("status") == "verified"]
    else:
        where, p = _where(a)
        rows = await asyncio.to_thread(db.all, f"SELECT {EVENT_COLS} FROM events WHERE {where} ORDER BY start_ts DESC LIMIT 25", p)
    if a.get("min_priority"):
        rows = [r for r in rows if PRIORITY_RANK.get(r.get("priority") or "none", 0) >= PRIORITY_RANK[a["min_priority"]]]
    rows = rows[:12]
    for r in rows:
        refs.event(r)
    return [_event_line(r) for r in rows] or ["No matching events."], len(rows)


async def t_count_events(a: dict, refs: Refs) -> tuple[list[str], int]:
    expr = {"hour": "strftime('%H:00', start_ts, 'unixepoch', 'localtime')", "day": "strftime('%Y-%m-%d', start_ts, 'unixepoch', 'localtime')",
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
        est = await asyncio.to_thread(distinct_people, ids)
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
            lines.append(f"[{refs.moment(m)}] {_cam_name(m['camera_id'])}, {_when(m['ts'])}"
                         + (f" (for {m['end'] - m['start']:.0f} s)" if m["end"] - m["start"] >= 5 else "")
                         + f" - CHECKED: yes, {r.get('seen') or 'matches'}")
    head = (f"Footage search: Qwen checked the best {checked} visual matches; {len(lines)} actually show it."
            if checked else "Footage search: the matches could not be checked.")
    return [head, *lines], len(lines)


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
    return [f"Briefing for {_when(b['period_start'])} – {_when(b['period_end'])}: {b['headline']}", b["text"]], 1


TOOL_FUNCS = {"search_events": t_search_events, "count_events": t_count_events, "list_unusual": t_list_unusual,
              "list_journeys": t_list_journeys, "search_footage": t_search_footage, "recording_gaps": t_recording_gaps,
              "get_briefing": t_get_briefing}


def fallback_call(calls: list[dict], question: str) -> dict | None:
    """When every lookup came back empty, one plain search of all cameras with the question text (keeping
    only the time window) usually finds what an over-filtered plan missed."""
    if any(c["count"] for c in calls):
        return None
    since = min((c["args"].get("since") for c in calls if c["args"].get("since") is not None), default=None)
    args = {"text": question[:200], "camera": None, "since": since, "until": None, "label": query_label(question),
            "min_priority": None, "group_by": None}
    key = json.dumps(args, sort_keys=True)
    if any(c["tool"] == "search_events" and json.dumps(c["args"], sort_keys=True) == key for c in calls):
        return None
    return {"tool": "search_events", "args": args}


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
    "count if they say CHECKED: yes. Counts are sightings, not different people. Cite only the few sightings that "
    "support your answer (at most 5). Don't guess identities. Write plain sentences; don't repeat these instructions."
)


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
                    {"role": "user", "content": f"Lookup results:\n{results}\n\nQuestion: {question}"}]
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
    "one-line headline and 1-4 short bullets about what needs attention (high priority or unusual events, cameras "
    "offline, disk problems) and one bullet summing up the period in plain words. Activity counts, journeys, recording "
    "and disk status are listed separately below your bullets, so don't repeat those numbers. Cite events as [#id] "
    "exactly as given. Only use the facts provided; if it was quiet, say so."
)


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
    top = db.all(f"SELECT {EVENT_COLS} FROM events WHERE {where} AND (priority IN ('low','medium','high') OR COALESCE(anomaly,0) >= ?) "
                 f"ORDER BY CASE priority WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END DESC, "
                 f"COALESCE(anomaly, 0) DESC LIMIT 8", [*p, baseline.PRIORITY_LOW])
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
    if top:
        fixed.append("Flagged for review: " + " ".join(f"[#{e['id']}]" for e in top))
    for j in journeys:
        route = " → ".join(_cam_name(x) for x in json.loads(j["cameras"] or "[]"))
        fixed.append(f"[#{j.get('first_event', '?')}] Journey {_when(j['first_ts'])}: {route}")
    fixed.append("Recording: " + ("; ".join(gaps) if gaps else "all cameras recorded continuously"))
    fixed.append(f"Disk: {free_gb:,.0f} GB free" + (" - retention alert" if retention.alert else ""))
    stats = {"counts": counts, "false_alarms": fa, "top": [e["id"] for e in top], "journeys": [j["id"] for j in journeys],
             "gaps": gaps, "free_gb": round(free_gb, 1), "fixed": fixed}
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
