"""Talk-down "Describe & warn": Qwen looks at the camera's current picture and says how to address the person there
("you in the red shirt by the white truck"), and a template the operator picks becomes an editable message. Nothing
is ever played from here: the operator reviews the text and plays it through the typed-text path (speakers_api.play),
which logs and audits the exact words spoken.

Guardrails: the prompt asks for clothing, colours, carried items, vehicles and position only, and the answer is
filtered in code as well (SENSITIVE words: race / ethnicity / skin, age, body, gender, health, religion). A clause that
contains one is dropped; when nothing useful is left the address falls back to the position alone, else to the generic
message. The whole describe takes at most TOTAL_S; a missing picture or an unavailable Qwen still gives the generic
message, with the reason in `error`.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from . import frames, mediamtx

log = logging.getLogger("nvr.talkdown")

TASK = "talkdown"           # not a remote task: the local Qwen answers (the remote only when there is no local model)
PRIORITY = "chat"           # the operator is waiting, ahead of queued background work
TOTAL_S = 15.0              # the whole describe: picture + Qwen
FRAME_S = 6.0               # of which, at most, getting the picture
MAX_FRAME_AGE_S = 8.0       # an older last recorded frame is not "now": read one from the live stream instead
FRAME_W = 1280
RATE_S = 3.0                # one describe per speaker and camera this often
MAX_OPTIONS = 3             # per-person descriptions offered when several people are in view
DESC_WORDS, POS_WORDS = 12, 8

# (id, name, the message after the address). With an address: "You in the red shirt: this is private property ..."
TEMPLATES = (
    ("leave", "Leave now", "this is private property and you are being recorded. Leave now."),
    ("police", "Police called", "the police have been called. Leave the property immediately."),
    ("monitored", "Monitored site", "this site is monitored by security and you are being recorded. Please leave the area."),
    ("custom", "Custom", ""),
)
TEMPLATE_IDS = tuple(t[0] for t in TEMPLATES)

SYSTEM = (
    "You help a security operator speak to people on a live camera through a loudspeaker. Say how to address each "
    "visible person so they know they are the one being spoken to.\n"
    "Describe ONLY: clothing and its colours (shirt, jacket, hoodie, hat, trousers, hi-vis vest), things they carry or "
    "push (backpack, bag, ladder, bicycle, cart), a vehicle they are in or next to, and where they are relative to an "
    "obvious landmark (a vehicle, gate, door, fence, building, sign).\n"
    "NEVER mention race, ethnicity, nationality, skin, hair, face, age, body size or shape, height, gender, disability, "
    "health, religion, tattoos or anything else about the person's body or identity, and never a name. Do not use words "
    "like man, woman, guy, lady, boy, girl, he, she, old, young, tall, short, fat or thin. Say 'in the red shirt', not "
    "'the man in the red shirt'.\n"
    "Count only real people who are clearly visible (not mannequins, pictures or reflections). If nobody is visible, "
    "answer people 0 with an empty description and position. Never guess what you cannot see."
)
PROMPT = (
    "Who is visible on this camera right now? Answer as JSON. description: at most 12 words, starting with 'in' or "
    "'with' (e.g. 'in the red shirt carrying a backpack'). position: at most 8 words, starting with a preposition "
    "(e.g. 'by the white truck', 'at the gate'). With two or more people: description and position are the most "
    "visible one, persons lists up to 3 of them (most visible first), and group_position says where they are if they "
    "are together (else empty)."
)
_ONE = {"type": "object", "properties": {"description": {"type": "string"}, "position": {"type": "string"}},
        "required": ["description", "position"]}
SCHEMA = {
    "type": "object",
    "properties": {
        "people": {"type": "integer", "minimum": 0, "description": "how many people are visible"},
        "description": {"type": "string", "description": "clothing colours / items and carried objects, at most 12 words"},
        "position": {"type": "string", "description": "where, relative to an obvious landmark, at most 8 words"},
        "persons": {"type": "array", "maxItems": MAX_OPTIONS, "items": _ONE},
        "group_position": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["people", "description", "position", "confidence"],
}

# Never spoken: a clause with one of these is dropped (whole words, any case; "dark-skinned" is dark + skinned).
SENSITIVE = frozenset("""
    race racial ethnic ethnicity nationality foreign foreigner immigrant illegal colored coloured caucasian asian african
    hispanic latino latina latinx mexican arab arabic indian chinese japanese korean european aboriginal indigenous native
    skin skinned complexion tanned swarthy
    jewish muslim christian sikh hindu hijab turban yarmulke kippah burqa niqab
    old older oldest elderly aged senior young younger youngest youth youthful teen teens teenage teenaged teenager
    teenagers kid kids child children juvenile minor minors adult adults baby toddler infant
    fat fatter thin thinner skinny slim slender tall taller short shorter heavy heavyset overweight obese chubby plump
    stocky petite muscular burly lanky scrawny husky curvy built build bodied belly pregnant
    hair haired bald balding blond blonde brunette redhead ginger beard bearded mustache moustache tattoo tattoos
    tattooed freckles scar scarred
    man men woman women guy guys lady ladies gentleman gentlemen male males female females boy boys girl girls he him his
    she her hers sir madam ma'am dude bro gal fellow mister mr mrs miss ms transgender
    wheelchair disabled disability handicapped crutch crutches cane limp limping blind deaf amputee prosthetic injured
    drunk intoxicated homeless vagrant transient addict junkie
    named called
""".split())
SENSITIVE_PHRASES = re.compile(
    r"\b(?:white|black|brown|dark|light|fair|olive|yellow|red)[\s-]+(?:person|people|individual|individuals|someone|"
    r"toned|tone)\b|\bof colou?r\b|\bmiddle[\s-]+eastern\b|\bmiddle[\s-]+aged\b", re.I)
# "the man in ...", "a young person wearing ...": the subject is "you", so this lead is dropped (and filtered if it said more)
_LEAD = re.compile(
    r"^(?:you\s+)?(?:(?:the|a|an|this|that|one)\s+)?(?:[\w'-]+\s+){0,2}?(?:person|people|individual|figure|someone|"
    r"subject|intruder|trespasser|man|woman|guy|lady|male|female|boy|girl|gentleman)\b(?:\s+(?:who\s+is|who's|that\s+is|is))?\s*",
    re.I)
_CLAUSES = re.compile(r"\s*(?:,|;|\band\b|\bwhile\b)\s*", re.I)
_DESC_LEAD = ("in", "with", "wearing", "carrying", "holding", "pushing", "pulling", "riding", "on", "inside", "driving")
_POS_LEAD = ("by", "at", "near", "next", "beside", "behind", "in", "on", "inside", "outside", "under", "between", "along",
             "across", "against", "beneath", "over", "around", "past", "through", "toward", "towards", "standing", "walking",
             "sitting", "leaning", "climbing", "crouching")


class FrameError(RuntimeError):
    """No current picture of the camera (the text is shown to the operator)."""


def words_of(text: str) -> list[str]:
    return re.findall(r"[a-z]+(?:'[a-z]+)?", (text or "").lower())


def sensitive(text: str) -> bool:
    return any(w in SENSITIVE for w in words_of(text)) or bool(SENSITIVE_PHRASES.search(text or ""))


def _tidy(text: str, max_words: int) -> str:
    t = " ".join(str(text or "").replace('"', " ").split()).strip(" .,:;!-")
    return " ".join(t.split()[:max_words]).rstrip(" .,:;!-")


def _keep_clean(text: str) -> tuple[str, bool]:
    """(text without the clauses that say something sensitive, whether any were dropped)."""
    parts = [p for p in _CLAUSES.split(text) if p.strip()]
    kept = [p for p in parts if not sensitive(p)]
    if len(kept) == len(parts):
        return text, False
    return " and ".join(kept), True


def clean_description(text: str) -> tuple[str, bool]:
    """A description as "in the red shirt carrying a backpack" (follows "You"), and whether anything was filtered."""
    t = _tidy(text, 40)
    m = _LEAD.match(t)
    filtered = False
    if m:
        filtered = sensitive(m.group(0))
        t = t[m.end():]
    t, dropped = _keep_clean(t)
    t = _tidy(t, DESC_WORDS)
    if t and (not words_of(t) or sensitive(t)):
        t, dropped = "", True
    t = t[:1].lower() + t[1:]
    if t and t.split()[0] not in _DESC_LEAD:
        t = ("in " if t.split()[0] in ("the", "a", "an") else "in the ") + t
    return t, filtered or dropped


def clean_position(text: str) -> tuple[str, bool]:
    """A position as "by the white truck" (follows the description); "" when it said something sensitive."""
    t = _tidy(text, POS_WORDS + 2)
    t = re.sub(r"^(?:you\s+)?(?:(?:who\s+)?is\s+|are\s+)?", "", t, flags=re.I)
    if not t:
        return "", False
    if sensitive(t):
        return "", True
    if t.split()[0].lower() not in _POS_LEAD:
        t = "by " + t
    t = _tidy(t, POS_WORDS)
    return t[0].lower() + t[1:], False


def phrase(description: str, position: str) -> str | None:
    """How to address them: "You in the red shirt by the white truck", "You by the gate"; None = nothing usable."""
    rest = " ".join(x for x in (description, position) if x)
    return f"You {rest}" if rest else None


def fill(template: str, address: str | None) -> str:
    """The message: "<address>: <template text>", or the template text alone (capitalised) without an address."""
    body = dict((t[0], t[2]) for t in TEMPLATES).get(template, TEMPLATES[0][2])
    if address:
        return f"{address}: {body}" if body else f"{address}: "
    return body[:1].upper() + body[1:]


def judge(answer: dict) -> dict:
    """Qwen's JSON -> {people, options, phrase, group_phrase, confidence, filtered} with the guardrails applied."""
    try:
        people = max(0, min(50, int(answer.get("people"))))
    except (TypeError, ValueError):
        raise ValueError("Otso's answer had no count of people")
    confidence = answer.get("confidence") if answer.get("confidence") in ("low", "medium", "high") else "low"
    raw = [p for p in (answer.get("persons") or []) if isinstance(p, dict)][:MAX_OPTIONS]
    if not raw or people <= 1:
        raw = [{"description": answer.get("description"), "position": answer.get("position")}]
    people = max(people, len(raw) if people else 0)
    options, filtered = [], False
    if people:
        for p in raw:
            d, f1 = clean_description(p.get("description") or "")
            pos, f2 = clean_position(p.get("position") or "")
            filtered = filtered or f1 or f2
            ph = phrase(d, pos)
            if ph and all(o["phrase"] != ph for o in options):
                options.append({"description": d, "position": pos, "phrase": ph})
    group = None
    if people > 1:
        gpos, f3 = clean_position(answer.get("group_position") or "")
        filtered = filtered or f3
        group = phrase("", gpos) or "You there"
    address = group if people > 1 else (options[0]["phrase"] if options else None)
    return {"people": people, "options": options, "phrase": address, "group_phrase": group, "confidence": confidence,
            "filtered": filtered}


# ---------------------------------------------------------------- the picture

def _redact(text: str) -> str:
    return re.sub(r"//[^/@\s]+@", "//", str(text))[:160]   # a MediaMTX URL carries the reader password


def stream_frame(camera_id: str, timeout_s: float = FRAME_S) -> tuple[bytes, float]:
    """The first keyframe of the camera's recorded path, read from this MediaMTX (pulled 24/7 already: no new session
    on the camera). Blocking."""
    import av
    import cv2
    deadline = time.monotonic() + timeout_s
    with av.open(mediamtx.local_url(camera_id), options={"rtsp_transport": "tcp", "timeout": str(int(timeout_s * 1e6))},
                 timeout=timeout_s) as c:
        for fr in c.decode(c.streams.video[0]):
            if fr.key_frame:
                img = fr.to_ndarray(format="bgr24")
                scale = FRAME_W / img.shape[1]
                if scale < 1:
                    img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if ok:
                    return buf.tobytes(), time.time()
            if time.monotonic() > deadline:
                break
    raise FrameError("no keyframe from the live stream in time")


def live_frame(camera_id: str) -> tuple[bytes, float]:
    """(JPEG, its time) of the camera's freshest picture: the last frame of the recording being written (MediaMTX
    writes 1 s parts, so it is a second or two behind live), else one read from the live stream. Blocking."""
    now = time.time()
    try:
        r = frames.preview_jpeg(camera_id, now, FRAME_W, exact=True)
    except Exception as e:  # noqa: BLE001 - a half-written last part: try the live stream
        log.info("describe %s: recording frame failed (%s)", camera_id, type(e).__name__)
        r = None
    if r and now - r[1] <= MAX_FRAME_AGE_S:
        return r[0], r[1]
    try:
        return stream_frame(camera_id)
    except FrameError:
        raise
    except Exception as e:  # noqa: BLE001
        raise FrameError(f"the live stream could not be read ({type(e).__name__}: {_redact(e)})") from e


# ---------------------------------------------------------------- describe

_last: dict[tuple[str, str], float] = {}


def rate_limited(speaker_id: str, camera_id: str, now: float | None = None) -> bool:
    """True when this speaker and camera were described less than RATE_S ago; else counts this one."""
    now = time.time() if now is None else now
    key = (speaker_id, camera_id)
    if now - _last.get(key, 0.0) < RATE_S:
        return True
    _last[key] = now
    for k in [k for k, t in _last.items() if now - t > 60]:
        _last.pop(k, None)
    return False


def answer(template: str, frame_ts: float | None, model: str | None, error: str | None = None, judged: dict | None = None) -> dict:
    j = judged or {"people": None, "options": [], "phrase": None, "group_phrase": None, "confidence": None, "filtered": False}
    return {**j, "text": fill(template, j["phrase"]), "template": template,
            "templates": [{"id": i, "name": n, "text": t} for i, n, t in TEMPLATES], "frame_ts": frame_ts, "model": model,
            "error": error}


async def describe(camera_id: str, template: str = "leave") -> dict:
    """The answer for the panel (never raises for a missing picture or Qwen; `error` says what went wrong)."""
    from .vlmroute import router
    t0 = time.monotonic()
    try:
        jpeg, frame_ts = await asyncio.wait_for(asyncio.to_thread(live_frame, camera_id), FRAME_S)
    except asyncio.TimeoutError:
        return answer(template, None, None, "No current picture from this camera (it took too long).")
    except FrameError as e:
        return answer(template, None, None, f"No current picture from this camera ({e}).")
    except Exception as e:  # noqa: BLE001
        log.warning("describe %s: no frame (%s: %s)", camera_id, type(e).__name__, _redact(e))
        return answer(template, None, None, "No current picture from this camera.")
    left = max(1.0, TOTAL_S - (time.monotonic() - t0))
    try:
        r = await asyncio.wait_for(router.chat_json(TASK, SYSTEM, PROMPT, [jpeg], SCHEMA, num_predict=220, temperature=0.1,
                                                    priority=PRIORITY), left)
    except asyncio.TimeoutError:
        return answer(template, frame_ts, None, f"Otso did not answer within {TOTAL_S:.0f} s.")
    except Exception as e:  # noqa: BLE001 - model down or not configured: the generic message
        log.warning("describe %s: Qwen unavailable (%s: %s)", camera_id, type(e).__name__, str(e)[:160])
        return answer(template, frame_ts, None, "Otso is unavailable right now.")
    try:
        judged = judge(r)
    except ValueError as e:
        return answer(template, frame_ts, r.get("_model"), f"{e}.")
    return answer(template, frame_ts, r.get("_model"), None, judged)
