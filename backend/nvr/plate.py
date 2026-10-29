"""A vehicle's license plate, read by Qwen from a full-resolution crop of the vehicle, only when it is clearly legible.

Why: the hub's Site Ask was asked "white nissan tag number?" and could only say no plate is recorded; nothing read
plates. Now:

- With the synopsis (pipeline._synopsis): a vehicle event that is described also gets one plate question, after the
  description is stored. It follows the description's AI-schedule handling (full hours, a person's "Generate with
  Qwen", or the catch-up queue; nothing outside them in "skip" mode) and is metered as its own kind, "plate".
- On demand: POST /api/events/{id}/plate ("Read plate") asks about the sharpest crops across the event (up to
  ON_DEMAND_CROPS, one question each, stopping at the first high-confidence read) and keeps the most confident. A person
  asked, so it runs outside AI hours too (like Ask and clip chat); one run per event every settings.plate_rate_s.

The crop: the event's YOLO-matched vehicle boxes (detections.samples), largest first; for each candidate the frame is
decoded at full resolution from the event's clip (the recorded stream: the main stream unless the camera records its
sub stream) or, when the clip is gone, from the recording, with one seek per frame (frames.frames_at). Of the largest
candidates the sharpest (variance of the Laplacian) win. A vehicle shorter than settings.plate_min_px at the recorded
resolution is never asked about.

A separate small structured call rather than an extra image in the synopsis: the same question serves both paths, its
strict schema says "unreadable" instead of guessing (a plate field in the big description schema invites a made-up
plate from the 512 px crops), the synopsis prompt and its image budget stay as they are, and a vehicle too small to read
costs nothing. One image (~1,000 tokens of prefill) and ~40 tokens of answer.

Stored (db events): plate (normalized: uppercase letters and digits), plate_raw (as read), plate_region (state or region
printed on it, when readable), plate_conf (medium | high). A low-confidence or "unreadable" answer is never stored as a
plate. What was tried is detections.plate_read: {at, source, result: read | unreadable | skipped, reason, model, crops}.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Awaitable, Callable

import cv2
import numpy as np

from . import aischedule, frames
from .config import settings
from .db import db

log = logging.getLogger("nvr.plate")

TASK = "plate"            # vlmroute task: the local Qwen (the remote one only on a server without a local model)
KIND = "plate"            # aischedule meter kind
SYNOPSIS_CROPS = 1        # crops asked about with the synopsis (GPU budget: one question per described vehicle)
ON_DEMAND_CROPS = 3       # "Read plate": the top crops, the most confident read kept
CANDIDATES = 2            # decoded per crop wanted, the sharpest kept
CROP_MAX_PX, CROP_MIN_PX = 1280, 448
PAD = 0.08                # crop padding, share of the vehicle box
SHARP_K = 60.0            # Laplacian variance at which a crop counts as half sharp (score = area * s / (s + K))
TIMEOUT_S = 90.0          # all questions of one run together
CONF_RANK = {"low": 0, "medium": 1, "high": 2}
NOT_A_PLATE = {"UNREADABLE", "UNKNOWN", "NONE", "NA", "NOTVISIBLE", "NOTREADABLE", "ILLEGIBLE", "NOPLATE", "UNCLEAR",
               "NOTLEGIBLE", "BLURRY", "PLATE", "LICENSE", "LICENCE", "NOTAVAILABLE", "UNAVAILABLE", "XXXXXX"}
RAW_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .\-·•]*$")
US_STATES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA", "COLORADO": "CO",
    "CONNECTICUT": "CT", "DELAWARE": "DE", "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID",
    "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME",
    "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS", "MISSOURI": "MO",
    "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM",
    "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR",
    "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN",
    "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY", "DISTRICT OF COLUMBIA": "DC",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "legible": {"type": "boolean", "description": "true only if every character of the license plate is clearly readable"},
        "plate": {"type": "string", "description": "the plate's characters exactly as printed, or 'unreadable'"},
        "region": {"type": "string", "description": "the state or region printed on the plate when clearly readable "
                                                   "(e.g. 'TX' or 'Texas'), else ''"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["legible", "plate", "region", "confidence"],
}
SYSTEM = (
    "You read vehicle license plates in close-ups from security cameras. Report a plate only when every character is "
    "clearly legible in the picture. If there is no plate in view, or it is cut off, blurred, too small, too dark, "
    "covered by glare, or any character is uncertain, answer legible=false and plate='unreadable'. Never guess, never "
    "fill in a character you cannot see, and never complete a partial plate. Do not read dealer frames, stickers, phone "
    "numbers, fleet numbers or company names on the vehicle as a plate. region: the US state (or other region) only when "
    "it is printed on the plate and readable; otherwise ''. confidence: high only when the plate is sharp and every "
    "character is unmistakable."
)
# Read plate on a traffic read (a person asked, the camera's crop and read beside the answer): SYSTEM refused every soft
# but readable plate (cam8 2026-10-29: "3416AFR", "51121", "087 MAB" all legible=false, confidence low). This one read
# 3416AFR and 51121 right and 087 MAB as "087 MAU" (its B half hidden), all at medium, and named a wrong state every
# time; so a medium read is shown as "probably" and Otso's state is kept only when it is sure (traffic.store_otso).
ON_REQUEST_SYSTEM = (
    "You read vehicle license plates in close-ups from traffic and security cameras. Read the plate's characters as "
    "printed. legible=false and plate='unreadable' only when there is no plate in view or its characters can't be made "
    "out. Never invent characters you cannot see and never complete a plate that is cut off. Do not read dealer frames, "
    "stickers, phone numbers or company names as a plate. region: the US state (or other region) only when it is "
    "printed on the plate and readable; otherwise ''. confidence: high when every character is unmistakable, medium "
    "when the picture is soft but each character can still be made out, low when any character is a guess."
)
QUESTION = ("This is a full-resolution close-up of one vehicle from a security camera. Read its license plate if, and "
            "only if, it is clearly legible. Answer as JSON.")
# a traffic camera's own crop of the plate, enlarged (traffic.otso_crops): the plate fills the picture
PLATE_QUESTION = ("This is a close-up of one vehicle's license plate, cut out and enlarged by a traffic camera's plate "
                  "reader. Read the plate if, and only if, every character is clearly legible. Answer as JSON.")

# A traffic camera's vehicle, looked at by Otso on its own (traffic.Looker): the plate (the strict rule: nobody is there
# to check a guess) and what the vehicle is, for search by description ("white pickup with a ladder rack"). One
# question with the camera's plate crop (enlarged) and the vehicle picture. The camera's own make is often wrong (a red
# Toyota Tundra came through as a "Mercury car", cam8 2026-10-28), so Otso's is kept beside it.
LOOK_SCHEMA = {
    "type": "object",
    "properties": {
        **SCHEMA["properties"],
        "vehicle_type": {"type": "string", "description": "car, suv, pickup truck, van, box truck, semi truck, bus, "
                                                         "motorcycle, trailer or other"},
        "color": {"type": "string", "description": "the main body colour in one or two words"},
        "make": {"type": "string", "description": "the manufacturer when the badge or shape makes it clear, else ''"},
        "model": {"type": "string", "description": "the model when clear, else ''"},
        "details": {"type": "string", "description": "2 to 6 words on what would pick this vehicle out, not its "
                                                    "type, colour or make again (ladder rack, black bed cover, company "
                                                    "wrap, trailer, dented door), or ''"},
    },
    "required": [*SCHEMA["required"], "vehicle_type", "color", "make", "model", "details"],
}
LOOK_SYSTEM = SYSTEM + (
    " Also describe the vehicle in the vehicle picture: its type, main body colour, make and model only when the badge "
    "or shape makes them clear (otherwise ''), and in details 2 to 6 words on anything that would pick it out from "
    "similar vehicles, without repeating its type, colour or make. Never invent company names or lettering you cannot "
    "read.")
LOOK_QUESTION_PLATE = ("Picture 1 is a traffic camera's close-up of the vehicle's license plate, enlarged; picture 2 is "
                       "the vehicle. Read the plate if, and only if, every character is clearly legible, and describe "
                       "the vehicle. Answer as JSON.")
LOOK_QUESTION = ("This is one vehicle from a traffic camera. Read its license plate if, and only if, every character is "
                 "clearly legible, and describe the vehicle. Answer as JSON.")
VEHICLE_KEYS = ("vehicle_type", "color", "make", "model", "details")
EMPTY_WORDS = {"", "unknown", "none", "n/a", "na", "-", "unclear", "not visible", "unreadable", "other"}


# ---------------------------------------------------------------- the answer

def normalize(raw: str | None) -> str | None:
    """'abc-1234' / 'ABC 1234' / 'ABC·1234' -> 'ABC1234'; None for anything that is not a plausible plate."""
    s = str(raw or "").strip()
    if not s or not RAW_OK.match(s):
        return None   # '?', '*', '_' and the like mark a character the model was unsure of: not a plate
    n = re.sub(r"[^A-Z0-9]", "", s.upper())
    if not 2 <= len(n) <= 10 or n in NOT_A_PLATE:
        return None
    return n


def region_of(raw: str | None) -> str | None:
    s = re.sub(r"\s+", " ", str(raw or "")).strip().strip(".").upper()
    if not s or s in ("UNKNOWN", "NONE", "N/A", "NA", "UNREADABLE", "UNCLEAR", "NOT VISIBLE", "-"):
        return None
    return US_STATES.get(s, s[:20])


def parse(ans: dict | None) -> dict:
    """One answer -> {result: "read", plate, plate_raw, region, confidence} or {result: "unreadable", reason}. A plate
    the model was not sure of (legible false, confidence low, a placeholder like 'unreadable') is never a plate."""
    ans = ans if isinstance(ans, dict) else {}
    conf = str(ans.get("confidence") or "").strip().lower()
    conf = conf if conf in CONF_RANK else "low"
    raw = re.sub(r"\s+", " ", str(ans.get("plate") or "")).strip()
    plate = normalize(raw)
    model = ans.get("_model")
    if ans.get("legible") is not True or plate is None:
        return {"result": "unreadable", "reason": "not legible", "model": model}
    if conf == "low":
        return {"result": "unreadable", "reason": "not sure of every character", "model": model}
    return {"result": "read", "plate": plate, "plate_raw": raw.upper()[:20], "region": region_of(ans.get("region")),
            "confidence": conf, "model": model}


def parse_vehicle(ans: dict | None) -> dict | None:
    """The vehicle part of a look: {vehicle_type, color, make, model, details}, lowercase but the make and model; empty
    fields left out; None when nothing was said."""
    ans = ans if isinstance(ans, dict) else {}
    out = {}
    for k in VEHICLE_KEYS:
        v = re.sub(r"\s+", " ", str(ans.get(k) or "")).strip().strip(".")
        if v.lower() in EMPTY_WORDS:
            continue
        out[k] = _words(v if k in ("make", "model") else v.lower(), 60 if k == "details" else 30)
    return out or None


def _words(s: str, n: int) -> str:
    """At most n characters, cut at a word, no dangling "and" ("white with black and lime-green" -> "white with black")."""
    if len(s) <= n:
        return s
    words = s[:n].rsplit(" ", 1)[0].rstrip(",;:-").split(" ")
    while len(words) > 1 and words[-1].lower() in ("and", "with", "on", "of", "the", "a", "an", "in", "reading", "for"):
        words.pop()
    return " ".join(words).rstrip(",;:-") or s[:n]


def vehicle_words(v: dict | None) -> str:
    """"white Ford F-150 pickup truck, ladder rack": for the read's row and for search."""
    if not v:
        return ""
    head = " ".join(v[k] for k in ("color", "make", "model", "vehicle_type") if v.get(k))
    return f"{head}, {v['details']}" if head and v.get("details") else head or v.get("details", "")


def best(results: list[dict]) -> dict:
    """The most confident read (the first, i.e. the best crop's, among equals); unreadable when none was read."""
    reads = [r for r in results if r.get("result") == "read"]
    if not reads:
        return next((r for r in results if r.get("result") == "unreadable"), {"result": "unreadable", "reason": "no answer"})
    return max(reads, key=lambda r: CONF_RANK[r["confidence"]])   # max keeps the first of equals


def display(plate: str | None, region: str | None = None) -> str:
    """'ABC1234', 'TX' -> 'ABC 1234 · TX' (letters and digits apart, as the UI's formatPlate)."""
    if not plate:
        return ""
    text = re.sub(r"(?<=[A-Z])(?=\d)|(?<=\d)(?=[A-Z])", " ", plate)
    return f"{text} · {region}" if region else text


def search_terms(plate: str | None, raw: str | None = None, region: str | None = None) -> str:
    """Words for the search index: the plate, every piece of it from 3 characters (so a partial plate finds it), the
    plate as read, its region and the words people use for it ("plate ABC123", "tag number", "licence")."""
    if not plate:
        return ""
    parts = {plate[i:j] for i in range(len(plate)) for j in range(i + 3, len(plate) + 1)}
    words = [plate, *sorted(parts - {plate}, key=lambda p: (-len(p), p)), raw or "", region or "",
             "plate", "license plate", "licence", "tag", "tag number", "registration"]
    return " ".join(w for w in words if w)


# ---------------------------------------------------------------- the crops

def candidates(e: dict) -> list[dict]:
    """The event's YOLO-matched vehicle boxes, largest first: [{ts, box}] (normalized boxes, epoch times)."""
    out = []
    for d in (e.get("detections") or {}).get("samples") or []:
        m = d.get("match") if isinstance(d, dict) else None
        if not m or not isinstance(m.get("box"), list) or len(m["box"]) != 4 or d.get("ts") is None:
            continue
        l, t, r, b = (float(v) for v in m["box"])
        if r <= l or b <= t:
            continue
        out.append({"ts": float(d["ts"]), "box": [l, t, r, b], "area": (r - l) * (b - t), "h": b - t})
    return sorted(out, key=lambda c: -c["area"])


def sharpness(img: np.ndarray) -> float:
    """Variance of the Laplacian on the crop scaled to 256 px tall (so a big crop isn't 'sharper' for its size)."""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    if g.shape[0] < 4 or g.shape[1] < 4:
        return 0.0
    g = cv2.resize(g, (max(4, int(g.shape[1] * 256 / g.shape[0])), 256), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(g, cv2.CV_64F).var())


def crop(img: np.ndarray, box: list[float]) -> np.ndarray:
    """The vehicle box with a little padding, at full resolution (scaled down past CROP_MAX_PX, up below CROP_MIN_PX)."""
    h, w = img.shape[:2]
    l, t, r, b = box
    pw, ph = (r - l) * PAD, (b - t) * PAD
    x1, y1 = max(0, int((l - pw) * w)), max(0, int((t - ph) * h))
    x2, y2 = min(w, int((r + pw) * w)), min(h, int((b + ph) * h))
    c = img[y1:max(y2, y1 + 2), x1:max(x2, x1 + 2)]
    longest = max(c.shape[:2])
    s = CROP_MAX_PX / longest if longest > CROP_MAX_PX else CROP_MIN_PX / longest if longest < CROP_MIN_PX else 1.0
    if s != 1.0:
        c = cv2.resize(c, None, fx=s, fy=s, interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
    return c


def source_of(e: dict) -> tuple[Path, float] | None:
    """(the event's clip, its start epoch) when it is on disk, else None (the recording is read instead)."""
    if not e.get("clip"):
        return None
    clip = settings.data_dir / e["clip"]
    if not clip.is_file():
        return None
    return clip, float(e.get("clip_start") or e["start_ts"] - settings.clip_pre_roll)


def _decode(e: dict, times: list[float]) -> tuple[dict[float, np.ndarray], int | None]:
    """Full-resolution frames at these epoch times and the recorded height: from the clip, else the recording."""
    src = source_of(e)
    if src:
        clip, start = src
        got = frames.frames_at(clip, [t - start for t in times])
        out = {t: got[t - start] for t in times if (t - start) in got}
        if out:
            return out, next(iter(out.values())).shape[0]
    out = {}
    for t in times:
        img = frames.recorded_frame(e["camera_id"], t)
        if img is not None:
            out[t] = img
    return out, (next(iter(out.values())).shape[0] if out else None)


def recorded_height(e: dict) -> int | None:
    """The height of the recorded picture, from the clip's (or the recording's) header without decoding."""
    try:
        src = source_of(e)
        if src:
            return frames.video_height(src[0])
        c = candidates(e)
        seg = frames.find_segment(e["camera_id"], c[0]["ts"]) if c else None
        return frames.video_height(seg[0]) if seg else None
    except Exception as ex:  # noqa: BLE001 - unreadable header: the decode decides
        log.debug("event %s: no picture height (%s)", e.get("id"), ex)
        return None


def gather(e: dict, n: int, min_px: int | None = None, decode: Callable | None = None,
           height: Callable | None = None) -> dict:
    """Blocking (decode thread). Up to n JPEG crops of the vehicle, best first: {crops, px_h, reason}. `reason` says why
    there are none ("no vehicle box", "too small: 64 px < 120 px", "no footage"). decode / height: for tests."""
    min_px = settings.plate_min_px if min_px is None else min_px
    decode = decode or _decode
    height = height or recorded_height
    cands = candidates(e)
    if not cands:
        return {"crops": [], "px_h": 0, "reason": "no vehicle box"}
    h = height(e)
    if h:
        tall = [c for c in cands if c["h"] * h >= min_px]
        if not tall:
            top = round(max(c["h"] for c in cands) * h)
            return {"crops": [], "px_h": top, "reason": f"too small: {top} px < {min_px} px"}
        cands = tall
    picks = cands[:max(1, n * CANDIDATES)]
    imgs, h2 = decode(e, [c["ts"] for c in picks])
    if not imgs:
        return {"crops": [], "px_h": 0, "reason": "no footage"}
    h = h or h2
    scored = []
    for c in picks:
        img = imgs.get(c["ts"])
        if img is None:
            continue
        px_h = round(c["h"] * img.shape[0])
        if px_h < min_px:
            continue
        cr = crop(img, c["box"])
        s = sharpness(cr)
        scored.append((c["area"] * img.shape[0] * img.shape[1] * s / (s + SHARP_K), px_h, cr))
    if not scored:
        top = round(max(c["h"] for c in picks) * (h or 0))
        return {"crops": [], "px_h": top, "reason": f"too small: {top} px < {min_px} px"}
    scored.sort(key=lambda x: -x[0])
    crops = [cv2.imencode(".jpg", cr, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes() for _, _, cr in scored[:n]]
    return {"crops": crops, "px_h": max(p for _, p, _ in scored[:n]), "reason": None}


# ---------------------------------------------------------------- asking and storing

async def ask(image: bytes, priority: str, question: str = QUESTION, system: str = SYSTEM) -> dict:
    from . import vlmroute
    return await vlmroute.router.chat_json(TASK, system, question, [image], SCHEMA, 80, 0.0, priority)


async def read(crops: list, priority: str = "background", stop_at: str = "high", system: str = SYSTEM) -> dict:
    """One question per crop (best crop first), stopping at the first `stop_at` read; the most confident kept. A crop is
    JPEG bytes (a vehicle close-up) or (bytes, question)."""
    results = []
    for c in crops:
        img, question = c if isinstance(c, tuple) else (c, QUESTION)
        r = parse(await ask(img, priority, question, system))
        results.append(r)
        if r["result"] == "read" and CONF_RANK[r["confidence"]] >= CONF_RANK[stop_at]:
            break
    out = best(results)
    out["crops"] = len(results)
    return out


async def look(plate_img: bytes | None, vehicle_img: bytes, priority: str = "background") -> dict:
    """Otso on a traffic camera's vehicle (traffic.Looker): the plate (strict: parse) and the vehicle (parse_vehicle),
    one question with the enlarged plate crop first when there is one."""
    from . import vlmroute
    images = [plate_img, vehicle_img] if plate_img else [vehicle_img]
    ans = await vlmroute.router.chat_json(TASK, LOOK_SYSTEM, LOOK_QUESTION_PLATE if plate_img else LOOK_QUESTION, images,
                                          LOOK_SCHEMA, 160, 0.0, priority)
    out = parse(ans)
    out["vehicle"] = parse_vehicle(ans)
    out["crops"] = len(images)
    return out


def store(event_id: int, result: dict, source: str, keep_plate: bool = False) -> dict | None:
    """Write the outcome on the event. A read sets the plate columns; an unreadable or skipped result clears them,
    except with keep_plate (the synopsis path never undoes a plate already read)."""
    e = db.event(event_id)
    if not e:
        return None
    info = {"at": round(time.time(), 1), "source": source, "result": result["result"]}
    for k in ("reason", "model", "crops", "px_h"):
        if result.get(k) not in (None, ""):
            info[k] = result[k]
    fields: dict = {"detections": {**(e.get("detections") or {}), "plate_read": info}}
    if result["result"] == "read":
        fields.update(plate=result["plate"], plate_raw=result["plate_raw"], plate_region=result.get("region"),
                      plate_conf=result["confidence"])
    elif not (keep_plate and e.get("plate")):
        fields.update(plate=None, plate_raw=None, plate_region=None, plate_conf=None)
    db.update_event(event_id, **fields)
    return db.event(event_id)


def is_vehicle(e: dict | None) -> bool:
    from .parked import VEHICLE_LABELS
    return bool(e) and e.get("camera_class") in VEHICLE_LABELS


async def run(event_id: int, collect: Callable[[dict, int], Awaitable[dict]], source: str, n: int,
              priority: str, keep_plate: bool = False) -> dict:
    """Gather crops (collect(e, n), on a decode thread), ask, store. Returns the result: {result: read | unreadable |
    skipped, plate?, plate_raw?, region?, confidence?, reason?, model?, crops?}. Metered as "plate" whatever job it
    runs in (the synopsis, catch-up or a person's Read plate)."""
    e = db.event(event_id)
    if not is_vehicle(e):
        return {"result": "skipped", "reason": "not a vehicle"}
    got = await collect(e, n)
    if not got["crops"]:
        result = {"result": "skipped", "reason": got.get("reason") or "no crop", "px_h": got.get("px_h")}
    else:
        with aischedule.job(KIND):
            result = await asyncio.wait_for(read(got["crops"], priority), TIMEOUT_S)
        result["px_h"] = got.get("px_h")
    store(event_id, result, source, keep_plate=keep_plate)
    log.info("event %s plate (%s): %s", event_id, source,
             display(result.get("plate"), result.get("region")) or f"{result['result']} ({result.get('reason') or '-'})")
    return result


# ---------------------------------------------------------------- "Read plate": one run per event every plate_rate_s

class Limiter:
    def __init__(self) -> None:
        self.last: dict[int, float] = {}
        self.running: set[int] = set()

    def wait_s(self, event_id: int, now: float | None = None) -> float:
        """Seconds until this event may be read again (0 = now)."""
        if event_id in self.running:
            return max(1.0, settings.plate_rate_s)
        now = time.time() if now is None else now
        return max(0.0, self.last.get(event_id, -1e18) + settings.plate_rate_s - now)

    def start(self, event_id: int, now: float | None = None) -> None:
        self.last[event_id] = time.time() if now is None else now
        self.running.add(event_id)
        if len(self.last) > 2000:   # forget old ones
            cut = sorted(self.last.values())[-1000]
            self.last = {k: v for k, v in self.last.items() if v >= cut}

    def done(self, event_id: int) -> None:
        self.running.discard(event_id)


limiter = Limiter()
