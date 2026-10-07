"""Evidence for the hub's Site Ask: the assistant's plan and lookups, returned as structured results, no answer.

The hub asks every server of a Site (POST /api/assistant/retrieve), merges the evidence and writes ONE answer for the
whole Site with the shared AI (hub/hub/site_ask.py). Here a server does what its own Ask does up to the answer:
plan (its own model, with a time limit; the built-in rules if the planner is slow or down) -> check_plan + augment ->
the same lookup tools as /api/assistant/ask (assistant.TOOL_FUNCS) -> the same "nothing found" fallbacks. Instead of
the text block the answer model reads, each lookup's findings come back as items:

  {kind: "event", event_id, ts, end_ts, camera_id, camera_name, label, priority, snapshot, text, synopsis, calls, ...}
  {kind: "footage", ts, end_ts, camera_id, camera_name, text}              a footage-index moment the model confirmed
  {kind: "journey", event_id, event_ids, ts, end_ts, camera_ids, cameras, text}
  {kind: "gap", camera_id, camera_name, ts, end_ts, minutes, text}        a camera not recording
  {kind: "count", text, counts}                                             count_events (counts are structured)
  {kind: "briefing", ts, end_ts, text, background: true}
  {kind: "note", text, camera_id?}                                          e.g. "recorded continuously"

When the question names a period ("overnight") and a fallback lookup had to look wider, items outside it carry
`earlier: true` (before it) or `later: true` (after it). `text` never starts with the camera or the time (the hub formats those in the Site's time zone). Nothing is written:
no assistant thread, no message. The old text path (/api/assistant/ask) is untouched.

Follow-ups: the hub sends the last few turns. A follow-up that only changes the period ("and yesterday?") or starts
with "and / what about" is read with the previous question (contextualize) before planning, and the planner sees the
history too.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time

from . import assistant as A
from .db import db

log = logging.getLogger("nvr.retrieve")

PLAN_TIMEOUT_S = 12.0      # the planner's share; the hub waits ~25 s for the whole retrieve
BUDGET_S = 20.0            # lookups stop starting after this (a note says so)
MAX_ITEMS = 60
HISTORY_TURNS = 4

FOLLOW_UP = re.compile(r"^\s*(and|also|plus|then|ok(ay)?,?\s*(and|what about|how about)?|what about|how about|same\s+(for|with|question\s+for))\b[\s,]*",
                       re.I)


def _last_user(history: list[dict]) -> str | None:
    for m in reversed(history or []):
        if m.get("role") == "user" and str(m.get("content") or "").strip():
            return str(m["content"]).strip()
    return None


def contextualize(question: str, history: list[dict], now: float) -> str:
    """The question to plan with. "and yesterday?" after "Did the cleaning lady come today?" becomes "Did the cleaning
    lady come yesterday?"; "what about the white van?" keeps the previous period ("the white van today"). Anything
    else is returned as asked."""
    q = question.strip()
    prev = _last_user(history)
    if not prev:
        return q
    win = A.time_window(q, now)
    rest = (win["text"] if win else q).strip()
    if win and win["text"] == q:   # time_window keeps the text when only the phrase was there
        rest = ""
    follow = FOLLOW_UP.match(q)
    rest_clean = FOLLOW_UP.sub("", rest).strip(" ?.!,")
    if win and not rest_clean:
        # only the period changed: the previous question with the new period
        pm = A._TIME_PHRASES.search(prev.lower())
        phrase = win["label"]
        if pm:
            return (prev[:pm.start()] + phrase + prev[pm.end():]).strip()
        tail = "?" if prev.rstrip().endswith("?") else ""
        return f"{prev.rstrip(' ?.!')} {phrase}{tail}"
    if follow and rest_clean:
        if win:
            return q[follow.end():].strip() or q
        pw = A.time_window(prev, now)
        return f"{rest_clean} {pw['label']}?" if pw else f"{rest_clean}?"
    return q


# ---------------------------------------------------------------- collecting structured findings

class Evidence(A.Refs):
    """Refs that also keep each event row and footage moment the lookups cite, and which lookup cited them."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[int, dict] = {}
        self.moments: dict[str, dict] = {}
        self.call_events: list[int] = []
        self.call_moments: list[str] = []

    def event(self, e: dict) -> None:
        super().event(e)
        self.rows.setdefault(e["id"], e)
        self.call_events.append(e["id"])

    def moment(self, m: dict) -> str:
        key = super().moment(m)
        self.moments[key] = m
        self.call_moments.append(key)
        return key


def _event_text(e: dict) -> str:
    """The assistant's event line without its "[#id] Camera, when, " head: "person, 12 s (extras): description"."""
    line = A._event_line(e)
    head = f"[#{e['id']}] {A._cam_name(e['camera_id'])}, {A._when(e['start_ts'])}, "
    if line.startswith(head):
        return line[len(head):]
    m = re.match(r"^\[#\d+\]\s*", line)
    return line[m.end():] if m else line


def event_item(e: dict) -> dict:
    return {"kind": "event", "event_id": e["id"], "ts": e["start_ts"], "end_ts": e.get("end_ts") or e["start_ts"],
            "camera_id": e["camera_id"], "camera_name": A._cam_name(e["camera_id"]), "label": e.get("camera_class"),
            "priority": e.get("priority") or "none", "snapshot": bool(e.get("snapshot")), "text": _event_text(e),
            "synopsis": (e.get("synopsis") or "").strip()[:400], "journey_id": e.get("journey_id"), "calls": []}


def _journey_items(a: dict) -> list[dict]:
    w, p = ["synopsis IS NOT NULL"], []
    if a.get("since") is not None:
        w.append("last_ts>=?"); p.append(a["since"])
    if a.get("until") is not None:
        w.append("first_ts<=?"); p.append(a["until"])
    out = []
    for j in db.all(f"SELECT * FROM journeys WHERE {' AND '.join(w)} ORDER BY first_ts DESC LIMIT 8", p):
        members = db.all("SELECT id FROM events WHERE journey_id=? ORDER BY start_ts", [j["id"]])
        cams = json.loads(j["cameras"] or "[]")
        out.append({"kind": "journey", "event_id": members[0]["id"] if members else None, "event_ids": [m["id"] for m in members],
                    "ts": j["first_ts"], "end_ts": j.get("last_ts") or j["first_ts"], "camera_ids": cams,
                    "cameras": [A._cam_name(c) for c in cams], "camera_id": cams[0] if cams else None,
                    "camera_name": A._cam_name(cams[0]) if cams else None, "text": (j["synopsis"] or "").strip()[:400]})
    return out


def _gap_items(a: dict, now: float) -> list[dict]:
    """assistant.t_recording_gaps, structured: gaps per camera, and which cameras recorded throughout."""
    lo0, hi = a.get("since") or now - 86400, a.get("until") or now
    cams = [a["camera"]] if a.get("camera") else [c["id"] for c in db.cameras(enabled_only=True)]
    out, ok = [], []
    for cid in cams:
        lo = lo0
        spans = A._recording_spans(cid)
        prev_end, gaps = None, []
        if spans and spans[0][0] > lo + 120:
            out.append({"kind": "note", "camera_id": cid, "camera_name": A._cam_name(cid), "ts": spans[0][0],
                        "text": "recordings only start here (camera added then, or older footage deleted)"})
            lo = max(lo, spans[0][0])
        for st, en in spans:
            if prev_end is not None and st - prev_end > 120 and st >= lo and prev_end <= hi:
                gaps.append((prev_end, st))
            prev_end = en if prev_end is None else max(prev_end, en)
        if prev_end is not None and hi - prev_end > 120 and hi >= now - 60:
            gaps.append((prev_end, hi))
        for g0, g1 in gaps:
            out.append({"kind": "gap", "camera_id": cid, "camera_name": A._cam_name(cid), "ts": g0, "end_ts": g1,
                        "minutes": round((g1 - g0) / 60), "ongoing": g1 >= now - 60,
                        "text": f"not recording for {(g1 - g0) / 60:.0f} min" + (" (still not recording)" if g1 >= now - 60 else "")})
        if not spans:
            out.append({"kind": "note", "camera_id": cid, "camera_name": A._cam_name(cid), "text": "no recordings found"})
        elif not gaps:
            ok.append(cid)
    if ok:
        out.append({"kind": "note", "camera_ids": ok, "recording_ok": True, "ts": lo0, "end_ts": hi,
                    "text": "recorded continuously: " + ", ".join(A._cam_name(c) for c in ok)})
    return out


_PEOPLE = re.compile(r"Number of different people: (?:about (\d+)|between (\d+) and (\d+))")


def _counts(a: dict, lines: list[str]) -> dict:
    """count_events, structured (same filters as the tool): totals by label and camera (+ hour/day when grouped)."""
    where, p = A._where(a)
    rows = db.all(f"SELECT camera_id, camera_class, COUNT(*) AS n FROM events WHERE {where} GROUP BY camera_id, camera_class", p)
    by_label: dict[str, int] = {}
    by_camera: dict[str, int] = {}
    for r in rows:
        by_label[r["camera_class"]] = by_label.get(r["camera_class"], 0) + r["n"]
        by_camera[r["camera_id"]] = by_camera.get(r["camera_id"], 0) + r["n"]
    out: dict = {"events": sum(by_label.values()), "by_label": by_label, "by_camera": by_camera,
                 "camera_names": {c: A._cam_name(c) for c in by_camera}, "since": a.get("since"), "until": a.get("until"),
                 "label": a.get("label"), "camera": a.get("camera")}
    if a.get("group_by") in ("hour", "day"):
        expr = {"hour": "strftime('%H:00', start_ts, 'unixepoch', 'localtime')", "day": "strftime('%Y-%m-%d', start_ts, 'unixepoch', 'localtime')"}[a["group_by"]]
        out["by_" + a["group_by"]] = {r["k"]: r["n"] for r in db.all(f"SELECT {expr} AS k, COUNT(*) AS n FROM events WHERE {where} GROUP BY k ORDER BY k", p)}
    for line in lines:
        m = _PEOPLE.search(line)
        if m:
            lo, hi = (int(m[1]), int(m[1])) if m[1] else (int(m[2]), int(m[3]))
            out["people"] = [lo, hi]
    return out


def _briefing_item(a: dict) -> dict | None:
    if a.get("since") is not None:
        b = db.one("SELECT * FROM briefings WHERE period_end >= ? ORDER BY period_end ASC LIMIT 1", [a["since"]])
    else:
        b = db.one("SELECT * FROM briefings ORDER BY created_at DESC LIMIT 1")
    if not b:
        return None
    return {"kind": "briefing", "ts": b["period_start"], "end_ts": b["period_end"], "background": True,
            "text": f"{b['headline']}\n{b['text']}"[:1500]}


def _footage_text(line: str) -> str:
    m = re.search(r" - CHECKED: (.*)$", line)
    return ("CHECKED: " + m[1]) if m else line


def _public_args(a: dict) -> dict:
    return {k: v for k, v in a.items() if v not in (None, "", False)}


async def run(calls: list[dict], ev: Evidence, question: str, now: float, deadline: float) -> tuple[list[dict], list[dict], list[str]]:
    """assistant.run_calls, collecting items. Returns (summary of calls, items, notes)."""
    summary: list[dict] = []
    items: list[dict] = []
    notes: list[str] = []
    queue = list(calls)
    while queue:
        c = queue.pop(0)
        left = deadline - time.monotonic()
        if left < 1:
            notes.append("Some lookups were skipped to answer in time: " + ", ".join(A.describe_call(x) for x in [c, *queue]))
            break
        ev.call_events, ev.call_moments = [], []
        tool, a = c["tool"], c["args"]
        label = A.describe_call(c)
        lines, n, found = [], 0, []
        try:
            if tool == "recording_gaps":
                found = await asyncio.wait_for(asyncio.to_thread(_gap_items, a, now), left)
                n = sum(1 for x in found if x["kind"] == "gap")
            else:
                lines, n = await asyncio.wait_for(A.TOOL_FUNCS[tool](a, ev), left)
                if tool == "count_events":
                    counts = await asyncio.to_thread(_counts, a, lines)
                    found = [{"kind": "count", "text": "\n".join(lines), "counts": counts, "label": label}]
                elif tool == "list_journeys":
                    found = await asyncio.to_thread(_journey_items, a)
                elif tool == "get_briefing":
                    b = await asyncio.to_thread(_briefing_item, a)
                    found = [b] if b else []
                elif tool == "search_footage":
                    for key in ev.call_moments:
                        m = ev.moments[key]
                        line = next((x for x in lines if x.startswith(f"[{key}]")), "")
                        found.append({"kind": "footage", "ts": m["ts"], "end_ts": m.get("end") or m["ts"], "camera_id": m["camera_id"],
                                      "camera_name": A._cam_name(m["camera_id"]), "tile": m.get("tile"), "text": _footage_text(line)})
                    if not ev.call_moments and lines:
                        notes.append(lines[0])
        except asyncio.TimeoutError:
            notes.append(f"{label} took too long and was skipped")
        except Exception as e:  # a broken lookup shouldn't sink the others
            log.exception("retrieve tool %s failed", tool)
            notes.append(f"{label} failed: {e}")
        for eid in dict.fromkeys(ev.call_events):   # search_events, list_unusual, list_journeys' members
            it = next((x for x in items if x["kind"] == "event" and x["event_id"] == eid), None)
            if it is None:
                it = event_item(ev.rows[eid])
                if a.get("earlier"):
                    it["earlier"] = True   # before the period asked about: nothing matched in it
                items.append(it)
            it["calls"].append(len(summary))
        items.extend(found)
        summary.append({"tool": tool, "args": a, "label": label, "count": n})
        if not queue and (fb := A.fallback_call(summary, question)):
            queue.append(fb)
    return [{**s, "args": _public_args(s["args"])} for s in summary], items, notes


def _window(question: str, calls: list[dict], now: float) -> dict | None:
    win = A.time_window(question, now)
    if win:
        return {"from": win["since"], "to": win["until"] if win["until"] is not None else now, "label": win["label"]}
    sinces = [c["args"].get("since") for c in calls if c["args"].get("since") is not None]
    untils = [c["args"].get("until") for c in calls if c["args"].get("until") is not None]
    if sinces:
        return {"from": min(sinces), "to": max(untils) if untils else now, "label": None}
    return None


def _cap(items: list[dict]) -> list[dict]:
    """At most MAX_ITEMS: counts, notes and briefings always; the rest newest first (high priority kept first)."""
    fixed = [x for x in items if x["kind"] in ("count", "note", "briefing")]
    timed = [x for x in items if x["kind"] not in ("count", "note", "briefing")]
    room = max(0, MAX_ITEMS - len(fixed))
    if len(timed) > room:
        rank = {"high": 0, "medium": 1}
        keep = sorted(timed, key=lambda x: (rank.get(x.get("priority") or "", 2), -(x.get("ts") or 0)))[:room]
        ids = {id(x) for x in keep}
        timed = [x for x in timed if id(x) in ids]
    return [*fixed, *timed]


async def retrieve(question: str, history: list[dict] | None = None, now: float | None = None, tz: str | None = None) -> dict:
    t0 = time.monotonic()
    now = now or time.time()
    history = [{"role": h["role"], "content": str(h.get("content") or "")[:1500]} for h in (history or [])
               if isinstance(h, dict) and h.get("role") in ("user", "assistant")][-HISTORY_TURNS:]
    used = contextualize(question, history, now)
    notes: list[str] = []
    raw: dict = {}
    try:
        system, text = A._plan_prompt(used, history, now)
        raw = await asyncio.wait_for(A.vlmroute.router.chat_json("assistant", system, text, [], A.PLAN_SCHEMA, 300, 0.1, "chat"),
                                     PLAN_TIMEOUT_S)
        raw = raw if isinstance(raw, dict) else {}
    except asyncio.TimeoutError:
        notes.append("The planner was slow, so the built-in lookup rules chose the lookups.")
    except Exception as e:  # noqa: BLE001 - no model: the rules still pick sensible lookups
        log.warning("retrieve planner failed: %s", e)
        notes.append("The planner was unavailable, so the built-in lookup rules chose the lookups.")
    calls = A.check_plan(raw, used, now)
    ev = Evidence()
    summary, items, more = await run(calls, ev, used, now, t0 + BUDGET_S)
    notes += more
    window = _window(used, calls, now)
    if window and A.time_window(used, now):
        # a fallback lookup widens the period ("nothing overnight: the latest since then"): say which side each is on
        for x in items:
            if x["kind"] in ("event", "footage", "journey") and x.get("ts") is not None:
                if x["ts"] < window["from"] - 60:
                    x["earlier"] = True
                elif x["ts"] > window["to"] + 60:
                    x["later"] = True
    events = [x for x in items if x["kind"] == "event"]
    count_items = [x for x in items if x["kind"] == "count"]
    counts = dict(count_items[0]["counts"]) if count_items else {}
    counts["found"] = {k: sum(1 for x in items if x["kind"] == k) for k in ("event", "footage", "journey", "gap")}
    off = dt.datetime.fromtimestamp(now).astimezone()
    out = {
        "question": used, "asked": question, "window": window,
        "calls": summary, "results": _cap(items), "counts": counts, "plan_model": raw.get("_model"), "notes": notes,
        "now": now, "tz": tz, "server_tz": off.tzname(), "utc_offset": off.utcoffset().total_seconds() if off.utcoffset() else 0,
        "truncated": max(0, len(items) - MAX_ITEMS), "duration_ms": round((time.monotonic() - t0) * 1000),
    }
    log.info("retrieve: %d calls, %d events, %d items in %d ms (planner %s)", len(summary), len(events), len(items),
             out["duration_ms"], raw.get("_model") or "rules")
    return out
