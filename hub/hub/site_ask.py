"""A Site's Ask tab: one answer for the whole Site (all of its servers), in conversations private to each user.

POST /api/locations/{id}/ask streams NDJSON:
1. Instructions ("Quiet alerts tonight") are not answered: Customer › Actions plans and runs those. Text that reads as
   one (looks_like_instruction, the twin of the hub UI's looksLikeInstruction) gets {"type": "instruction", href}
   linking there with the text prefilled; nothing is stored.
2. The user's thread (new, or one of their own: anyone else's is a 404, hub administrators included) gets the
   question. The last HISTORY_TURNS messages go along as context.
3. Every ONLINE server of the Site gets POST /api/assistant/retrieve through its tunnel, in parallel
   (RETRIEVE_TIMEOUT_S each). The server plans and looks things up with its own model and data and returns
   structured evidence without writing an answer (backend/nvr/retrieve.py).
4. merge(): items tagged with their server and the camera's registry name, deduped, newest first, capped at
   MAX_SOURCES (high and medium priority kept first); counts summed exactly across servers; offline, failed and slow
   servers noted.
5. ONE answer by the shared AI (vlm_proxy.stream_complete), streamed as it is written. Server names stay out of it
   unless a server could not be checked. When the shared AI can't answer, fallback_answer() lists what was found, so
   Ask still answers.
6. The answer is stored with `sources` = the merged evidence (the page's Sources disclosure and citation chips).

Chunks: thread, user, status, sources, model, delta..., fallback?, done | instruction | error.
Citations: [#<ref>], ref = the event id ("123"), or "123a" / "123b" when two servers' events share an id in one answer
(ids are per server); [F<n>] footage moments numbered across the Site. Each source item carries ref, server_id and
event_id, so the page opens the right server's event.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time
from collections import defaultdict, deque
from typing import AsyncIterator
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa

from . import cameras, db, vlm_proxy
from .agents import registry
from .config import settings

log = logging.getLogger("hub.site_ask")

RETRIEVE_PATH = "/api/assistant/retrieve"
RETRIEVE_TIMEOUT_S = 25.0
HISTORY_TURNS = 4            # messages sent to the servers' planners
ANSWER_HISTORY = 6           # messages the answer model reads
MAX_SOURCES = 60
PROMPT_ITEMS = 45            # evidence lines in the answer prompt (newest first; sources keep up to MAX_SOURCES)
RATE_LIMIT, RATE_WINDOW_S = 20, 60.0
MAX_THREADS = 100
ACTIONS_PATH = "/customer/actions"
TIMED = ("event", "footage", "journey", "gap")


# ---------------------------------------------------------------- instructions go to Customer › Actions

POLITE = re.compile(r"^\s*(please|pls|kindly|ok|okay|now|go ahead and|can you|could you|would you|will you|i want to|i'd like to|"
                    r"i would like to|we need to|let's|lets)\b[\s,]*", re.I)
QUESTION = re.compile(r"^\s*(how|what|what's|whats|when|where|who|whom|whose|why|which|did|does|do(?!\s+not\b)|is|are|was|were|has|"
                      r"have|had|show|list|find|search|any|anyone|anybody|count|tell|give|should|shall|may|might)\b", re.I)
VERB_FIRST = re.compile(r"^(migrate|move|transfer|relocate|retire|decommission|rename|set|keep|retain|add|lock|protect|quiet|mute|"
                        r"silence|snooze|hush|unmute|stop\s+describing|start\s+describing|describe\s+only)\b", re.I)


def looks_like_instruction(text: str) -> bool:
    """Does this read as a fleet instruction ("Migrate Ironsight to Hailo T1", "please quiet alerts tonight")? Mirrors
    hub/ui/src/customer/fleetActions.ts looksLikeInstruction: polite padding stripped, questions never count."""
    t, polite = text.strip(), False
    while True:
        m = POLITE.match(t)
        if not m or not t[m.end():]:
            break
        t, polite = t[m.end():], True
    t = t.strip()
    if not t or QUESTION.match(t) or (t.endswith("?") and not polite):
        return False
    return bool(VERB_FIRST.match(t))


def actions_href(text: str) -> str:
    """Customer › Actions with the instruction prefilled (encoded like JavaScript's encodeURIComponent)."""
    t = text.strip()[:500]
    return f"{ACTIONS_PATH}?text={quote(t, safe=chr(39) + '!()*-._~')}" if t else ACTIONS_PATH


# ---------------------------------------------------------------- rate limit (per user, in memory)

_asks: dict[str, deque] = defaultdict(deque)


def rate_limited(uid: str, now: float | None = None) -> bool:
    """True when this user asked RATE_LIMIT questions in the last minute; otherwise records this one."""
    now = now if now is not None else time.time()
    q = _asks[uid]
    while q and q[0] < now - RATE_WINDOW_S:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(now)
    return False


# ---------------------------------------------------------------- threads (private to their user)

def _own(u: dict, location_id: str, tid: int) -> dict | None:
    t = db.site_ask_threads.c
    return db.one(sa.select(db.site_ask_threads).where(t.id == tid, t.user_id == u["id"], t.location_id == location_id))


def list_threads(u: dict, location_id: str, limit: int = 50) -> list[dict]:
    t, m = db.site_ask_threads.c, db.site_ask_messages.c
    rows = db.rows(sa.select(db.site_ask_threads).where(t.user_id == u["id"], t.location_id == location_id)
                   .order_by(t.updated_at.desc(), t.id.desc()).limit(limit))
    ids = [r["id"] for r in rows]
    n = {r["thread_id"]: r["n"] for r in db.rows(sa.select(m.thread_id, sa.func.count().label("n")).where(m.thread_id.in_(ids))
                                                  .group_by(m.thread_id))} if ids else {}
    return [{k: r[k] for k in ("id", "title", "created_at", "updated_at")} | {"messages": n.get(r["id"], 0)} for r in rows]


def get_thread(u: dict, location_id: str, tid: int) -> dict | None:
    t = _own(u, location_id, tid)
    if not t:
        return None
    m = db.site_ask_messages.c
    msgs = db.rows(sa.select(db.site_ask_messages).where(m.thread_id == tid).order_by(m.id))
    return {k: t[k] for k in ("id", "title", "created_at", "updated_at", "location_id")} | {"messages": msgs}


def rename_thread(u: dict, location_id: str, tid: int, title: str) -> dict | None:
    if not _own(u, location_id, tid):
        return None
    db.run(sa.update(db.site_ask_threads).where(db.site_ask_threads.c.id == tid).values(title=title.strip()[:200] or "Conversation"))
    return get_thread(u, location_id, tid)


def delete_thread(u: dict, location_id: str, tid: int) -> bool:
    if not _own(u, location_id, tid):
        return False
    with db.engine().begin() as c:
        c.execute(sa.delete(db.site_ask_messages).where(db.site_ask_messages.c.thread_id == tid))
        c.execute(sa.delete(db.site_ask_threads).where(db.site_ask_threads.c.id == tid))
    return True


def drop_location(conn, location_id: str) -> None:
    """The Site is deleted: its conversations go with it."""
    ids = sa.select(db.site_ask_threads.c.id).where(db.site_ask_threads.c.location_id == location_id)
    conn.execute(sa.delete(db.site_ask_messages).where(db.site_ask_messages.c.thread_id.in_(ids)))
    conn.execute(sa.delete(db.site_ask_threads).where(db.site_ask_threads.c.location_id == location_id))


def _insert(table, values: dict) -> int:
    with db.engine().begin() as c:
        return int(c.execute(table.insert().values(**values)).inserted_primary_key[0])


def _new_thread(u: dict, loc: dict, question: str, now: float) -> int:
    t = db.site_ask_threads.c
    old = [r["id"] for r in db.rows(sa.select(t.id).where(t.user_id == u["id"], t.location_id == loc["id"])
                                    .order_by(t.updated_at.desc(), t.id.desc()).offset(MAX_THREADS - 1))]
    for tid in old:   # keep the newest MAX_THREADS per user and Site
        delete_thread(u, loc["id"], tid)
    title = re.sub(r"\s+", " ", question).strip()
    return _insert(db.site_ask_threads, {"location_id": loc["id"], "org_id": loc["org_id"], "user_id": u["id"],
                                         "title": title[:80] + ("…" if len(title) > 80 else ""), "created_at": now, "updated_at": now})


def _add_message(tid: int, role: str, content: str, sources=None, model: str | None = None, duration_ms: int | None = None) -> int:
    now = time.time()
    mid = _insert(db.site_ask_messages, {"thread_id": tid, "role": role, "content": content, "sources": sources, "model": model,
                                         "created_at": now, "duration_ms": duration_ms})
    db.run(sa.update(db.site_ask_threads).where(db.site_ask_threads.c.id == tid).values(updated_at=now))
    return mid


# ---------------------------------------------------------------- fan-out

async def _retrieve(server: dict, conn, payload: bytes, headers: dict) -> dict:
    base = {"server_id": server["id"], "server_name": server["name"]}
    t0 = time.monotonic()
    try:
        status, body = await asyncio.wait_for(conn.call("POST", RETRIEVE_PATH, "", headers, payload, RETRIEVE_TIMEOUT_S),
                                              RETRIEVE_TIMEOUT_S + 2)
    except asyncio.TimeoutError:
        return {**base, "status": "timeout", "error": f"no answer within {RETRIEVE_TIMEOUT_S:.0f} s"}
    except Exception as e:  # tunnel aborted, too many streams: one server must not spoil the answer
        return {**base, "status": "error", "error": str(e)[:200] or type(e).__name__}
    ms = round((time.monotonic() - t0) * 1000)
    if status == 404:
        return {**base, "status": "error", "error": "this server's software is too old for Site Ask (update it)", "duration_ms": ms}
    if status != 200:
        return {**base, "status": "error", "error": f"HTTP {status}: {body[:120].decode(errors='replace')}", "duration_ms": ms}
    try:
        data = json.loads(body.decode() or "null")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {**base, "status": "error", "error": "unreadable answer", "duration_ms": ms}
    return {**base, "status": "ok", "data": data, "duration_ms": ms}


async def fan_out(servers: list[dict], u: dict, question: str, history: list[dict], tz: str | None) -> list[dict]:
    """Every online server's evidence, in Site order; offline servers as {status: "offline"}."""
    headers = {"x-hub-user": u["email"], "x-hub-role": "viewer", "content-type": "application/json"}
    payload = json.dumps({"question": question, "history": history, "now": time.time(), "tz": tz}).encode()

    async def one(s: dict) -> dict:
        conn = registry.get(s["id"])
        if conn is None:
            return {"server_id": s["id"], "server_name": s["name"], "status": "offline", "last_seen_at": s.get("last_seen_at")}
        return await _retrieve(s, conn, payload, headers)

    return list(await asyncio.gather(*(one(s) for s in servers)))


# ---------------------------------------------------------------- merge

def _key(sid: str, it: dict) -> tuple:
    k = it.get("kind")
    if k == "event":
        return (sid, k, it.get("event_id"))
    if k == "footage":
        return (sid, k, it.get("camera_id"), round(float(it.get("ts") or 0) / 10))
    if k == "journey":
        return (sid, k, it.get("event_id"), it.get("ts"))
    if k == "gap":
        return (sid, k, it.get("camera_id"), it.get("ts"))
    return (sid, k, it.get("camera_id"), it.get("text"))


def _add(into: dict, more: dict | None) -> None:
    for k, v in (more or {}).items():
        try:
            into[k] = into.get(k, 0) + int(v)
        except (TypeError, ValueError):
            continue


def merge(results: list[dict], cam_names: dict[tuple[str, str], str]) -> dict:
    """{items, servers, counts, window, question, dropped}: see the module docstring."""
    items: list[dict] = []
    seen: set = set()
    servers: list[dict] = []
    counts: dict = {"events": 0, "by_label": {}, "by_camera": {}}
    have_counts, people = False, None
    window, used = None, None
    by_hour: dict = {}
    by_day: dict = {}
    for r in results:
        sid, sname = r["server_id"], r["server_name"]
        d = r.get("data") or {}
        entry = {k: r.get(k) for k in ("server_id", "server_name", "status", "error", "duration_ms", "last_seen_at") if r.get(k) is not None}
        if r["status"] == "ok":
            entry["notes"] = [str(n)[:200] for n in (d.get("notes") or [])][:5]
            entry["found"] = (d.get("counts") or {}).get("found")
            if d.get("utc_offset") is not None:
                entry["utc_offset"] = d.get("utc_offset")
        servers.append(entry)
        if r["status"] != "ok":
            continue
        name = lambda cid: cam_names.get((sid, cid)) or cid   # noqa: E731
        for it in d.get("results") or []:
            if not isinstance(it, dict) or it.get("kind") not in (*TIMED, "count", "note", "briefing"):
                continue
            k = _key(sid, it)
            if k in seen:
                continue
            seen.add(k)
            cid = it.get("camera_id")
            x = {k2: v for k2, v in it.items() if k2 not in ("calls", "journey_id", "tile", "counts")}
            x.update(server_id=sid, server_name=sname,
                     camera=(cam_names.get((sid, cid)) or it.get("camera_name") or cid) if cid else None)
            if it.get("kind") == "journey":
                x["cameras"] = [cam_names.get((sid, c)) or n for c, n in zip(it.get("camera_ids") or [], it.get("cameras") or [])]
            if it.get("kind") == "note" and it.get("camera_ids"):
                x["text"] = "recorded continuously: " + ", ".join(name(c) for c in it["camera_ids"])
            if it.get("kind") == "count":
                continue   # summed below (the servers' count lines use their own names)
            items.append(x)
        c = d.get("counts") or {}
        if "events" in c:
            have_counts = True
            counts["events"] += int(c.get("events") or 0)
            _add(counts["by_label"], c.get("by_label"))
            for cid, n in (c.get("by_camera") or {}).items():
                nm = cam_names.get((sid, cid)) or (c.get("camera_names") or {}).get(cid) or cid
                _add(counts["by_camera"], {nm: n})
            _add(by_hour, c.get("by_hour"))
            _add(by_day, c.get("by_day"))
            if isinstance(c.get("people"), list) and len(c["people"]) == 2:
                people = [(people or [0, 0])[0] + int(c["people"][0]), (people or [0, 0])[1] + int(c["people"][1])]
        w = d.get("window")
        if isinstance(w, dict) and w.get("from") is not None:
            window = w if window is None else {"from": min(window["from"], w["from"]), "to": max(window.get("to") or 0, w.get("to") or 0),
                                                "label": window.get("label") or w.get("label")}
        used = used or d.get("question")
    if not have_counts:
        counts = {}
    else:
        if people:
            counts["people"] = people
        if by_hour:
            counts["by_hour"] = dict(sorted(by_hour.items()))
        if by_day:
            counts["by_day"] = dict(sorted(by_day.items()))
    timed = sorted((x for x in items if x["kind"] in TIMED), key=lambda x: -float(x.get("ts") or 0))
    other = [x for x in items if x["kind"] not in TIMED]
    dropped = 0
    if len(timed) > MAX_SOURCES:
        rank = {"high": 0, "medium": 1}
        keep = {id(x) for x in sorted(timed, key=lambda x: (rank.get(x.get("priority") or "", 2), -float(x.get("ts") or 0)))[:MAX_SOURCES]}
        dropped = len(timed) - MAX_SOURCES
        timed = [x for x in timed if id(x) in keep]
    _assign_refs(timed, [s["server_id"] for s in servers])
    return {"items": timed + other, "servers": servers, "counts": counts, "window": window, "question": used, "dropped": dropped}


def _assign_refs(timed: list[dict], order: list[str]) -> None:
    """[#123] per event id; "123a"/"123b" (in Site server order) when servers share an id; [F1].. oldest first."""
    rank = {sid: i for i, sid in enumerate(order)}
    owners: dict[int, list[str]] = {}
    for x in timed:
        if x["kind"] in ("event", "journey") and x.get("event_id") is not None:
            sids = owners.setdefault(x["event_id"], [])
            if x["server_id"] not in sids:
                sids.append(x["server_id"])
    for sids in owners.values():
        sids.sort(key=lambda sid: rank.get(sid, len(rank)))
    f = 0
    for x in reversed(timed):   # oldest footage is F1
        if x["kind"] == "footage":
            f += 1
            x["ref"] = f"F{f}"
    for x in timed:
        if x["kind"] in ("event", "journey") and x.get("event_id") is not None:
            sids = owners[x["event_id"]]
            x["ref"] = str(x["event_id"]) if len(sids) == 1 else f"{x['event_id']}{chr(97 + sids.index(x['server_id']))}"


# ---------------------------------------------------------------- the answer

def site_zone(loc: dict, merged: dict) -> dt.tzinfo:
    """The Site's time zone; else the first answering server's clock; else UTC."""
    try:
        if loc.get("timezone"):
            return ZoneInfo(loc["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        pass
    off = next((s.get("utc_offset") for s in merged.get("servers") or [] if s.get("utc_offset") is not None), None)
    return dt.timezone(dt.timedelta(seconds=float(off))) if off is not None else dt.timezone.utc


def fmt_time(ts: float, tz: dt.tzinfo, now: float, date: bool = True) -> str:
    d = dt.datetime.fromtimestamp(ts, tz)
    clock = f"{d.hour % 12 or 12}:{d:%M} {'AM' if d.hour < 12 else 'PM'}"
    if not date:
        return clock
    today = dt.datetime.fromtimestamp(now, tz).date()
    day = "today" if d.date() == today else "yesterday" if d.date() == today - dt.timedelta(days=1) else f"{d:%a %b} {d.day}"
    return f"{day} {clock}"


def _span(a: float, b: float | None, tz: dt.tzinfo, now: float) -> str:
    if b is None:
        return f"{fmt_time(a, tz, now)} to now"
    same = dt.datetime.fromtimestamp(a, tz).date() == dt.datetime.fromtimestamp(b, tz).date()
    return f"{fmt_time(a, tz, now)} to {fmt_time(b, tz, now, date=not same)}"


def evidence_line(x: dict, tz: dt.tzinfo, now: float) -> str:
    k, cam, ts = x["kind"], x.get("camera") or "", x.get("ts")
    if k == "event":
        return (f"[#{x['ref']}] {cam}, {fmt_time(ts, tz, now)}: {x.get('text') or ''}"
                + ("  (EARLIER: before the period asked about)" if x.get("earlier") else "")
                + ("  (AFTER the period asked about: not part of the answer for it)" if x.get("later") else ""))
    if k == "footage":
        return f"[{x['ref']}] {cam}, {fmt_time(ts, tz, now)}: video footage match, {x.get('text') or 'CHECKED'}"
    if k == "journey":
        return f"[#{x['ref']}] Journey {_span(ts, x.get('end_ts'), tz, now)} across {' → '.join(x.get('cameras') or [])}: {x.get('text') or ''}"
    if k == "gap":
        return f"{cam} was not recording {_span(ts, x.get('end_ts'), tz, now)} ({x.get('minutes')} min)"
    if k == "briefing":
        return (f"BACKGROUND ONLY: a briefing written for {_span(ts, x.get('end_ts'), tz, now)}, a different span than the "
                f"question may ask about: {x.get('text') or ''}")
    if k == "note":
        return f"{cam}: {x.get('text')}" if cam else str(x.get("text") or "")
    return str(x.get("text") or "")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def counts_line(c: dict) -> str:
    if not c:
        return ""
    parts = [_plural(c.get("events", 0), "event")]
    if c.get("by_label"):
        parts[0] += " (" + ", ".join(f"{n} {k}" for k, n in sorted(c["by_label"].items(), key=lambda kv: -kv[1])) + ")"
    if c.get("by_camera"):
        parts.append("by camera: " + ", ".join(f"{k} {n}" for k, n in sorted(c["by_camera"].items(), key=lambda kv: -kv[1])))
    if c.get("by_hour"):
        parts.append("by hour: " + ", ".join(f"{k} {n}" for k, n in c["by_hour"].items()))
    if c.get("by_day"):
        parts.append("by day: " + ", ".join(f"{k} {n}" for k, n in c["by_day"].items()))
    if c.get("people"):
        lo, hi = c["people"]
        parts.append(f"different people (estimated by appearance): {'about ' + str(lo) if lo == hi else f'{lo} to {hi}'}")
    return "; ".join(parts)


def not_checked_lines(merged: dict, server_cams: dict[str, list[str]], tz: dt.tzinfo, now: float) -> list[str]:
    out = []
    for s in merged["servers"]:
        if s["status"] == "ok":
            continue
        cams = server_cams.get(s["server_id"]) or []
        cam_txt = f" (cameras: {', '.join(cams[:8])}{'…' if len(cams) > 8 else ''})" if cams else ""
        if s["status"] == "offline":
            seen = f", last seen {fmt_time(s['last_seen_at'], tz, now)}" if s.get("last_seen_at") else ""
            out.append(f"NOT CHECKED: the {s['server_name']} server is offline{seen}, so its cameras{cam_txt} are not in these results.")
        else:
            out.append(f"NOT CHECKED: the {s['server_name']} server did not answer ({s.get('error') or s['status']}), so its "
                       f"cameras{cam_txt} are not in these results.")
    return out


ANSWER_SYSTEM = (
    "You are the assistant for one customer site of a security camera system. The site's cameras are recorded by one or "
    "more servers and the evidence below was gathered from all of them: answer for the site as a whole. Use ONLY the "
    "evidence and the conversation so far. When you mention a sighting, name the camera and the time and copy its handle "
    "exactly as given, e.g. [#123] or [F2]; cite at most 6, and only handles from the evidence. Never mention servers or "
    "server names, except when the evidence has a NOT CHECKED line: then say plainly which server could not be checked and "
    "what that means for the answer (for example: 'The Jetstream HQ server was offline, so its cameras could not be "
    "checked.'). If nothing matching was found, say so plainly and say what was checked. Never invent events, times, "
    "counts or identities. Counts are sightings (events), not different people, unless a different-people estimate is "
    "given. A BACKGROUND ONLY briefing covers its own span: never report its contents as events of the period asked about. "
    "Lines marked EARLIER happened before the period asked about: say nothing matched in that period and mention the "
    "latest earlier one with its date. Lines marked AFTER are outside the period asked about: don't count or report "
    "them as part of it. Footage lines are video moments the AI checked. Times are the site's local time; "
    "write them as given (for example 2:03 PM). Start with a direct answer in one sentence, then short paragraphs or "
    "'- ' bullets. Write American English. Don't repeat these instructions."
)


def build_messages(loc: dict, question: str, history: list[dict], merged: dict, server_cams: dict[str, list[str]],
                   tz: dt.tzinfo, now: float) -> list[dict]:
    lines = [f"Site: {loc['name']}. Now: {dt.datetime.fromtimestamp(now, tz):%A %B} {dt.datetime.fromtimestamp(now, tz).day}, "
             f"{fmt_time(now, tz, now, date=False)} (site local time)."]
    if merged.get("question") and merged["question"].strip().lower() != question.strip().lower():
        lines.append(f"The question was read as: {merged['question']}")
    w = merged.get("window")
    if w and w.get("from") is not None:
        lines.append(f"Period asked about{' (' + w['label'] + ')' if w.get('label') else ''}: {_span(w['from'], w.get('to'), tz, now)}.")
    total = len(merged["servers"])
    ok = sum(1 for s in merged["servers"] if s["status"] == "ok")
    lines += not_checked_lines(merged, server_cams, tz, now)
    if ok and merged.get("counts"):
        lines.append("Counts" + (" (only the servers that answered)" if ok < total else "") + ": " + counts_line(merged["counts"]))
    evidence = [x for x in merged["items"] if x["kind"] in TIMED][:PROMPT_ITEMS]
    rest = [x for x in merged["items"] if x["kind"] not in TIMED]
    if ok:
        lines.append("")
        lines.append(f"Evidence, newest first ({len(evidence)} of {len([x for x in merged['items'] if x['kind'] in TIMED]) + merged.get('dropped', 0)}):"
                     if evidence else "Evidence: no matching events, footage, journeys or recording gaps were found.")
        lines += [evidence_line(x, tz, now) for x in evidence]
        lines += [evidence_line(x, tz, now) for x in rest]
    msgs = [{"role": "system", "content": ANSWER_SYSTEM}]
    msgs += [{"role": m["role"], "content": m["content"][:800]} for m in history[-ANSWER_HISTORY:]]
    msgs.append({"role": "user", "content": "\n".join(lines) + f"\n\nQuestion: {question}"})
    return msgs


def fallback_answer(merged: dict, server_cams: dict[str, list[str]], tz: dt.tzinfo, now: float, reason: str | None = None) -> str:
    """What the lookups found, in plain words, when the shared AI can't write the answer."""
    out: list[str] = []
    w = merged.get("window")
    period = f" ({w['label']})" if w and w.get("label") else ""
    ok = any(s["status"] == "ok" for s in merged["servers"])
    evs = [x for x in merged["items"] if x["kind"] == "event" and not x.get("earlier") and not x.get("later")]
    earlier = [x for x in merged["items"] if x["kind"] == "event" and x.get("earlier")]
    other = [x for x in merged["items"] if x["kind"] in ("footage", "journey", "gap")]
    if not ok:
        out.append("None of this site's servers could be checked right now, so there is nothing to report.")
    elif not evs and not other:
        out.append(f"Nothing matched{period}: no events were found." if not earlier else f"Nothing matched{period}.")
    else:
        c = merged.get("counts") or {}
        out.append(f"Found{period}: " + (counts_line(c) if c else _plural(len(evs), "matching event")) + ".")
    bullets: list[str] = []
    for x in evs[:8]:
        desc = (x.get("synopsis") or x.get("text") or "").strip()
        bullets.append(f"- [#{x['ref']}] {x.get('camera')}, {fmt_time(x['ts'], tz, now)}: {desc[:160]}")
    if len(evs) > 8:
        bullets.append(f"- …and {len(evs) - 8} more (see Sources).")
    for x in other[:5]:
        bullets.append("- " + evidence_line(x, tz, now))
    if earlier:
        x = earlier[0]
        bullets.append(f"- The latest earlier match: [#{x['ref']}] {x.get('camera')}, {fmt_time(x['ts'], tz, now)}.")
    for line in not_checked_lines(merged, server_cams, tz, now):
        bullets.append("- " + line.replace("NOT CHECKED: the", "Not checked: the"))
    parts = [" ".join(out), "\n".join(bullets)]
    if reason is not None:   # the AI was asked and failed (not when there was nothing for it to read)
        parts.append("(The AI that writes answers could not be reached, so this is a plain list of what the lookups found.)")
    return "\n\n".join(p for p in parts if p).strip()


async def _no_think(pieces: AsyncIterator[str]) -> AsyncIterator[str]:
    """Drop <think>…</think> (a reasoning model's notes) from a stream of text pieces."""
    buf, inside = "", False
    async for piece in pieces:
        buf += piece
        out = ""
        while buf:
            if inside:
                i = buf.find("</think>")
                if i < 0:
                    buf = buf[-8:]
                    break
                buf, inside = buf[i + 8:], False
            else:
                i = buf.find("<think>")
                if i < 0:
                    k = buf.rfind("<")
                    if k >= 0 and "<think>".startswith(buf[k:]):
                        out, buf = out + buf[:k], buf[k:]
                    else:
                        out, buf = out + buf, ""
                    break
                out, buf, inside = out + buf[:i], buf[i + 7:], True
        if out:
            yield out
    if buf and not inside:
        yield buf


def _sources(merged: dict) -> dict:
    keep = ("ref", "kind", "server_id", "server_name", "event_id", "event_ids", "camera_id", "camera", "cameras", "ts", "end_ts",
            "label", "priority", "snapshot", "text", "synopsis", "earlier", "later", "minutes", "ongoing", "background")
    return {"items": [{k: x[k] for k in keep if x.get(k) not in (None, "", [], False)} for x in merged["items"]],
            "servers": merged["servers"], "counts": merged["counts"], "window": merged["window"],
            "question": merged["question"], "dropped": merged["dropped"]}


def site_servers(visible: list[dict], location_id: str) -> list[dict]:
    """The Site's (not retired) servers this user sees, in Site order (name, then id)."""
    return sorted((s for s in visible if s.get("location_id") == location_id and not s.get("retired_at")),
                  key=lambda s: (s["name"].casefold(), s["id"]))


async def ask(u: dict, loc: dict, servers: list[dict], thread_id: int | None, question: str) -> AsyncIterator[str]:
    """The NDJSON stream of POST /api/locations/{id}/ask. The caller checked access, the thread's owner and the rate."""
    def line(obj: dict) -> str:
        return json.dumps(obj) + "\n"

    t0 = time.monotonic()
    q = question.strip()
    if looks_like_instruction(q):
        yield line({"type": "instruction", "text": q, "href": actions_href(q),
                    "message": "That reads as an instruction. Instructions run from Customer › Actions."})
        yield line({"type": "done", "id": None})
        return
    now = time.time()
    m = db.site_ask_messages.c
    history = [] if not thread_id else [{"role": r["role"], "content": r["content"]} for r in db.rows(
        sa.select(m.role, m.content).where(m.thread_id == thread_id).order_by(m.id.desc()).limit(ANSWER_HISTORY))][::-1]
    tid = thread_id or _new_thread(u, loc, q, now)
    uid = _add_message(tid, "user", q)
    yield line({"type": "thread", "thread_id": tid})
    yield line({"type": "user", "id": uid})
    online = [s for s in servers if registry.get(s["id"]) is not None]
    yield line({"type": "status", "text": "Looking through the site's cameras…" if online else "No server of this site is online.",
                "servers": len(servers), "online": len(online)})
    results = await fan_out(servers, u, q, history[-HISTORY_TURNS:], loc.get("timezone")) if servers else []
    names = {(c["server_id"], c["camera_id"]): c["name"] or c["camera_id"] for c in cameras.for_location(loc["id"])}
    server_cams: dict[str, list[str]] = {}
    for (sid, _), nm in names.items():
        server_cams.setdefault(sid, []).append(nm)
    merged = merge(results, names)
    tz = site_zone(loc, merged)
    sources = _sources(merged)
    yield line({"type": "sources", **sources})
    answer, model, fallback, stored = "", None, None, False
    try:
        # nothing for the AI to read: say so without it
        if not servers:
            answer = "This site has no servers yet, so there is nothing to look through."
            yield line({"type": "delta", "text": answer})
        elif not any(s["status"] == "ok" for s in merged["servers"]):
            answer = fallback_answer(merged, server_cams, tz, now)
            yield line({"type": "delta", "text": answer})
        else:
            messages = build_messages(loc, q, history, merged, server_cams, tz, now)
            try:
                async for piece in _no_think(vlm_proxy.stream_complete(messages, 700, 0.2)):
                    if not answer:
                        piece = piece.lstrip()
                        if not piece:
                            continue
                        model = settings.vllm_model or None
                        yield line({"type": "model", "model": model})
                    answer += piece
                    yield line({"type": "delta", "text": piece})
            except Exception as e:  # noqa: BLE001 - the shared AI is down, busy or misconfigured
                reason = getattr(e, "detail", None) or str(e) or type(e).__name__
                log.warning("site ask: shared AI unavailable (%s)", str(reason)[:200])
                if answer:
                    answer += " [interrupted]"
                    yield line({"type": "delta", "text": " [interrupted]"})
                else:
                    fallback = str(reason)[:200]
                    answer = fallback_answer(merged, server_cams, tz, now, fallback)
                    yield line({"type": "fallback", "reason": fallback})
                    yield line({"type": "delta", "text": answer})
            if not answer.strip():
                fallback = "the AI wrote nothing"
                answer = fallback_answer(merged, server_cams, tz, now, fallback)
                yield line({"type": "fallback", "reason": fallback})
                yield line({"type": "delta", "text": answer})
        ms = round((time.monotonic() - t0) * 1000)
        mid = _add_message(tid, "assistant", answer.strip(), sources | ({"fallback": fallback} if fallback else {}), model, ms)
        stored = True
        log.info("site ask %s: %d/%d servers answered, %d sources, %s, %d ms", loc["id"],
                 sum(1 for s in merged["servers"] if s["status"] == "ok"), len(servers), len(merged["items"]),
                 model or f"fallback ({fallback})", ms)
        yield line({"type": "done", "id": mid, "duration_ms": ms})
    except Exception as e:  # surface it in the page instead of a broken stream
        log.exception("site ask failed")
        yield line({"type": "error", "error": str(e)[:300] or type(e).__name__})
    finally:
        if not stored and answer.strip():   # the page went away mid-answer: keep what was written
            _add_message(tid, "assistant", answer.strip() + " [interrupted]", sources, model, round((time.monotonic() - t0) * 1000))
