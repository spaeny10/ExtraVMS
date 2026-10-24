"""Ask about this clip, beyond the clip: route a clip-chat question to the clip's frames, the server's event search, or
both, and gather the search evidence the answer is written from (POST /api/events/{id}/chat, api.py).

Routing is cheap and deterministic (route(), no extra Qwen call):
  clip     questions about what the clip shows ("what is he carrying", "what color", "is the door open"): the frames only,
           as before.
  search   questions that reach beyond the clip, with nothing to look at in it: counts and times ("how many times",
           "when did they first / last…", "today", "this week"), recurrence ("did this truck come back"), other places
           ("where else did they go", "other cameras", a camera named in the question) and before / after ("what
           happened before this"). No frames: the evidence only.
  both     a question with both kinds of words ("what is he carrying, and was he here earlier?"), or one with only a weak
           hint of reaching beyond the clip ("before", "later", "when did he leave"): the frames plus the search as extra
           context, since leaving it out could make the answer wrong and including it costs no Qwen call.

The search is anchored to the event: its camera (scope "camera"; "anywhere" / "other cameras" = all of this server's
cameras; a camera named in the question = that camera), its time (the clip's local day in the Site's time zone, or the
period the question names; "ever" / "before today" = the week before the clip's day to a week after it, up to now) and
its subject:
  same     the same person / vehicle (assistant tool same_subject -> identities.same_subject, appearance fingerprints
           and license plates), for questions about "him", "this truck", "how many times", "where else"...
  general  the assistant's own lookups (count_events, search_events listing) on the same camera and window, for
           questions about other people or vehicles ("anyone else", "how many people came today") and for before /
           after (the CONTEXT_S before the clip started or after it ended). Also when the same-subject search can't
           run (a group of people, no fingerprint), so the answer still has the camera's sightings.

evidence() returns the text block Qwen reads (no [#id] handles: the answer names times and cameras, the chips under it
link the events) and the structured refs the UI renders as chips:
  [{event_id, camera_id, start_ts, match: this | same | possible | found, score, via, label, snapshot}]
("found" = a sighting the general lookups listed, no similarity score).
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time

from . import aischedule
from . import assistant as A
from .config import settings
from .db import db

log = logging.getLogger("nvr.clipask")

CONTEXT_S = 1800            # "what happened before / after": this long before the clip started or after it ended
EVER_DAYS = 6               # "ever", "before today": this many days before the clip's day, and up to a week after it
MAX_EVIDENCE_CHARS = 5000   # ~1,250 tokens: with 2 frames (~2,100), the prompt and history it fits vlm_num_ctx 6144
BOTH_FRAMES = 2             # frames sent with search evidence ("both"): the evidence needs the room
MAX_FOUND_REFS = 12         # sightings the general lookups listed, as chips

# what the clip itself answers: appearance, objects, actions, the scene
CLIP_WORDS = re.compile(
    r"\b(wear(s|ing)?|wore|carr(y|ies|ying|ied)|hold(s|ing)?|held|colou?rs?|look(s|ed)? like|doing|describe|description|"
    r"cloth(es|ing)|shirt|jacket|coat|hoodie|pants|shorts|hat|cap|helmet|vest|mask|gloves|shoes|bag|backpack|box|package|"
    r"phone|weapon|gun|knife|tool|ladder|face|hair|beard|tall|height|age|build|door|gate|window|open|closed|light|"
    r"which way|direction|in (his|her|their) hands?|license plate|plate number|make|model|logo|damage|parked)\b")
# the subject of the clip: him, them, this truck, the same guy
SUBJECT = re.compile(r"\b(he|him|his|she|her|they|them|their|it|its|same|this (person|guy|man|woman|lady|kid|car|truck|van|"
                     r"vehicle|pickup|suv|one|driver|worker)|that (person|guy|man|woman|lady|kid|car|truck|van|vehicle|pickup|"
                     r"suv|one|driver|worker)|the (person|guy|man|woman|lady|car|truck|van|vehicle|driver))\b")
# a strong sign the question reaches beyond the clip
STRONG = {
    "count": re.compile(r"\bhow (many|often)\b|\bnumber of\b|\bcount\b"),
    "first_last": re.compile(r"\b(first|last|earliest|latest|next|previous) (time|seen|sighting|visit|appearance|one)\b|"
                             r"\bwhen did .{0,60}\b(first|last)\b|\b(first|last) (show(ed|s)? up|arriv\w*|appear\w*|came|come|left|leave)\b"),
    "recur": re.compile(r"\b(again|come back|came back|comes back|coming back|return(s|ed|ing)?|back (here|again)|"
                        r"show(s|ed|n)? up|turn(s|ed)? up|visit(s|ed|ing)?|same (person|guy|man|woman|lady|people|driver|"
                        r"vehicle|car|truck|van|pickup|suv)|(was|were|is|are|has|have|had) (he|she|they|it|this \w+|that \w+) "
                        r"(been )?(here|around|back|seen)|been here|seen (him|her|them|it|this \w+|that \w+)\b)"),
    "places": re.compile(r"\b(where else|anywhere|elsewhere|other cameras?|another camera|any (other )?cameras?|"
                         r"all (the )?cameras|every camera|which cameras?|around the (site|property|building)|on site|"
                         r"rest of the (site|property))\b"),
    "ever": re.compile(r"\b(ever|before today|other days|previous days|in the past|this month|regular(ly)?|usual(ly)?|"
                       r"every day|daily|recently)\b"),
}
PLACES = STRONG["places"]
EVER = STRONG["ever"]
CONTEXT_BEFORE = re.compile(r"\bwhat happened (before|earlier|prior)\b|\b(before|prior to) (this|that|the (clip|event|video))\b|"
                            r"\bleading up\b|\bbefore (he|she|they|it) (arrived|came|got here|showed up)\b")
CONTEXT_AFTER = re.compile(r"\bwhat happened (after|next|later|afterwards)\b|\bafter (this|that|the (clip|event|video))\b|"
                           r"\bafterwards\b|\bafter (he|she|they|it) (left|went|drove)\b")
# other people or vehicles than the clip's subject: the camera's events, not the same-subject search
OTHERS = re.compile(r"\b(anyone|anybody|someone|somebody|anything|something) else\b|\bother (people|persons|visitors|workers|"
                    r"cars|vehicles|trucks|vans)\b|\bany(one| other)? (people|persons|cars|vehicles|trucks)\b|\bhow many (people|"
                    r"persons|visitors|workers|cars|vehicles|trucks|vans)\b|\bwho else\b")
# a weak hint: may be about the clip ("when did he leave" the frame) or beyond it; unsure = both
WEAK = re.compile(r"\b(before|after|earlier|later|previously|afterwards|then)\b|\bwhen did .{0,60}\b(arriv\w*|leave|left|come|came|"
                  r"get here|got here)\b|\bwhere did .{0,40}\bgo\b|\bwho is\b")


def route(question: str, cameras: list[tuple[str, str]] | tuple = (), anchor_camera: str | None = None) -> dict:
    """How to answer a clip-chat question: {mode: clip | search | both, same, general, context: before | after | None,
    scope: camera | all | named, cameras (named), ever, signals}. `cameras`: (id, name) of this server's cameras, so a
    camera named in the question scopes the search to it."""
    q = re.sub(r"\s+", " ", question.lower()).strip()
    signals = [name for name, rx in STRONG.items() if rx.search(q)]
    if A.time_phrase(question, [n for _, n in cameras]):
        signals.append("period")
    named = [cid for cid, name in cameras if cid != anchor_camera and name and len(name.strip()) >= 3
             and re.search(r"\b" + re.escape(name.lower().strip()) + r"\b", q)]
    if named:
        signals.append("camera")
    context = "before" if CONTEXT_BEFORE.search(q) else "after" if CONTEXT_AFTER.search(q) else None
    if context:
        signals.append(context)
    others = bool(OTHERS.search(q))
    if others:
        signals.append("others")
    subject = bool(SUBJECT.search(q))
    bare = q
    for _, name in cameras:   # "East Door" names a camera, not a door to look at
        if name and name.strip():
            bare = re.sub(r"\b" + re.escape(name.lower().strip()) + r"\b", " ", bare)
    clip_words = bool(CLIP_WORDS.search(bare))
    beyond = any(s != "others" for s in signals) or others
    if beyond:
        mode = "both" if clip_words else "search"
    elif WEAK.search(q):
        mode = "both"   # unsure: the frames, and the search as extra context
        signals.append("weak")
    else:
        mode = "clip"
    same = general = False
    if mode != "clip":
        about_subject = subject or any(s in signals for s in ("recur", "first_last", "places", "camera", "ever", "weak"))
        count_others = "count" in signals and not subject and "recur" not in signals
        general = others or count_others or bool(context) or ("period" in signals and not about_subject)
        same = about_subject and not (others and not subject) and not count_others
        if not same and not general:
            same = True
    return {"mode": mode, "same": same, "general": general, "context": context,
            "scope": "all" if "places" in signals and not named else "named" if named else "camera",
            "cameras": named, "ever": "ever" in signals, "signals": signals}


def _day(ts: float, zone) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, zone).replace(hour=0, minute=0, second=0, microsecond=0)


def window(e: dict, question: str, intent: dict, now: float, zone=None) -> dict:
    """The period the search covers: {since, until, label}. The period the question names, else the clip's local day
    (the Site's time zone; the server's clock without one); "ever" widens it to the week before that day and the week
    after (never past now)."""
    win = A.time_window(question, now, zone)
    f = lambda t: dt.datetime.fromtimestamp(t, zone).strftime("%a %b %d %H:%M")
    if win:
        until = win["until"] if win["until"] is not None else now
        return {"since": win["since"], "until": until, "label": f"{win['label']} ({f(win['since'])} to {f(until)})"}
    day = _day(e["start_ts"], zone)
    if intent.get("ever"):
        since = (day - dt.timedelta(days=EVER_DAYS)).timestamp()
        until = min(now, (day + dt.timedelta(days=8)).timestamp())
        return {"since": since, "until": until, "label": f"{f(since)} to {f(until)}"}
    until = min(now, (day + dt.timedelta(days=1)).timestamp())
    return {"since": day.timestamp(), "until": max(until, e.get("end_ts") or e["start_ts"]),
            "label": f"the clip's day, {day:%a %b %d}"}


HANDLE = re.compile(r"\[#\d+\]\s*")


def _ref(eid: int, info: dict, match: str, score: float | None, via: str | None) -> dict:
    return {"event_id": eid, "camera_id": info.get("camera_id"), "start_ts": info.get("start_ts"), "match": match,
            "score": score, "via": via, "label": info.get("label"), "snapshot": bool(info.get("snapshot"))}


def site_zone():
    """The Site's time zone the hub pushed with its AI schedule (aischedule.config), else None: the server's clock."""
    try:
        return A.zone_for((aischedule.config() or {}).get("timezone"))
    except Exception:  # noqa: BLE001 - no schedule pushed or unreadable: the server's own clock
        return None


async def evidence(e: dict, question: str, intent: dict, now: float | None = None, zone=None) -> dict:
    """The search evidence for a clip question routed beyond the clip: {text, refs, calls, window, same}. `zone`: the
    Site's time zone (default site_zone()); days, "today" and the times in the text follow it."""
    now = now or time.time()
    zone = site_zone() if zone is None else zone
    tokens = (A.SITE_TZ.set(zone), A.CAMERA_NAMES.set(tuple(c["name"] for c in db.cameras() if c.get("name"))))
    try:
        return await _evidence(e, question, intent, now, zone)
    finally:
        A.SITE_TZ.reset(tokens[0])
        A.CAMERA_NAMES.reset(tokens[1])


async def _evidence(e: dict, question: str, intent: dict, now: float, zone) -> dict:
    win = window(e, question, intent, now, zone)
    cam = e["camera_id"]
    scope_cams = None if intent["scope"] == "all" else intent["cameras"] if intent["scope"] == "named" else [cam]
    scope_label = ("all cameras" if scope_cams is None
                   else ", ".join(A._cam_name(c) for c in scope_cams) + (" (this clip's camera)" if scope_cams == [cam] else ""))
    refs = A.Refs()
    blocks = [f"Search anchored to this clip ({A._cam_name(cam)}, {A._when(e['start_ts'], now)}, {e['camera_class']}). "
              f"Period searched: {win['label']}. Cameras: {scope_label}. Times are local."]
    calls: list[dict] = []
    same = None
    general = intent["general"]
    if intent["same"]:
        a = {"event_id": e["id"], "cameras": scope_cams, "since": win["since"], "until": win["until"],
             "scope": f"{scope_label}, {win['label']}", "limit": settings.same_subject_max}
        lines, n = await A.t_same_subject(a, refs)
        same = a.pop("_result")
        calls.append({"tool": "same_subject", "args": a, "count": n})
        blocks.append("\n".join(lines))
        if same.get("reason"):
            general = True   # no appearance search possible: the camera's sightings in the window still help
    if general:
        label = A.class_intent(question) or (e["camera_class"] if e["camera_class"] in ("person", "vehicle") else None)
        base = {"text": "", "label": label, "min_priority": None, "group_by": None}
        one = scope_cams[0] if scope_cams and len(scope_cams) == 1 else None
        if intent.get("context"):
            start, end = e["start_ts"], e.get("end_ts") or e["start_ts"]
            since, until = (start - CONTEXT_S, start) if intent["context"] == "before" else (end, min(now, end + CONTEXT_S))
            gcalls = [{"tool": "search_events", "args": {**base, "camera": one, "since": since, "until": until}}]
        else:
            common = {**base, "camera": one, "since": win["since"], "until": win["until"]}
            gcalls = [{"tool": "count_events", "args": dict(common)}, {"tool": "search_events", "args": dict(common)}]
        text, summary = await A.run_calls(gcalls, refs)
        blocks.append(text)
        calls += summary
    text = HANDLE.sub("", "\n\n".join(b for b in blocks if b))
    if len(text) > MAX_EVIDENCE_CHARS:
        text = text[:MAX_EVIDENCE_CHARS].rsplit("\n", 1)[0] + "\n(search results truncated)"
    out_refs: list[dict] = []
    seen: set[int] = set()
    for m in (same or {}).get("matches") or []:
        info = refs.events.get(m["event_id"]) or {"camera_id": m["camera_id"], "start_ts": m["start_ts"]}
        out_refs.append(_ref(m["event_id"], info, m["match"], m["score"], m.get("via")))
        seen.add(m["event_id"])
    found = 0
    for eid, info in refs.events.items():
        if eid in seen or found >= MAX_FOUND_REFS:
            continue
        seen.add(eid)
        if eid == e["id"]:
            out_refs.append(_ref(eid, info, "this", None, None))
            continue
        out_refs.append(_ref(eid, info, "found", None, None))
        found += 1
    out_refs.sort(key=lambda r: r["start_ts"] or 0)
    log.info("clip chat on event %s: %s, %d refs (%s)", e["id"], intent["mode"], len(out_refs), ", ".join(intent["signals"]))
    return {"text": text, "refs": out_refs, "calls": calls, "window": win, "same": same}
