"""The situation brief for a clip Ask (synopsis.chat_stream): what else happened on this server around the event, so
Otso can reason from timing the frames can't show.

Why (Shawn, 2026-11-05): event 377, a person crossing a dark basement at 08:43, came one minute after the switch
feeding three cameras and three speakers lost power, and the same person came back at 09:12 carrying equipment and at
09:19 with a spool of cable. Otso saw four dark frames only and could know none of it; asked "is anything suspicious
here?" it said nothing, and when the operator suggested a flashlight it agreed and embellished.

Three parts, each only when it says something:
  * picture: the clip is black-and-white (the camera's night mode: the scene was dark), and which of the camera's other
    events in the hour are in color (the lights were on then);
  * recording: cameras on this server whose recording stopped or started again in the hour, or that recorded none of
    it (the recordings index, recindex.py: every camera records continuously, so a gap is the camera gone; gaps shorter
    than GAP_MIN_S are ignored);
  * this camera's other events in the hour, nearest first, one line each (`events=False` when the search evidence
    lists them already: clipask's "both" and "search" questions, which also need the room).

Everything is this server's: devices on another server at the Site (a speaker, the other NVR's cameras) aren't seen.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from pathlib import Path

from . import recindex
from .config import settings
from .db import db

log = logging.getLogger("nvr.askbrief")

WINDOW_S = 3600.0        # the hour before the event and the hour after it
GAP_MIN_S = 120.0        # a recording gap at least this long is the camera gone (MediaMTX restarts a reader in seconds)
EVENTS_EACH_SIDE = 6     # the camera's events listed before and after, nearest first
SYNOPSIS_CHARS = 200
MAX_DEVICE_LINES = 10
GRAY_COLORFUL_MAX = 0.05     # an infrared picture: under 5% of pixels with any color (measured: IR 2%, color >= 16%)
GRAY_PIXEL_SPREAD = 12       # a pixel "has color" when its channels differ by more than this


# ---------------------------------------------------------------- picture

def is_gray(path: Path) -> bool | None:
    """Is the snapshot black-and-white (night / infrared mode)? None when it can't be read. The detection box drawn on
    some snapshots is a few percent of the pixels at most, under the threshold."""
    try:
        import cv2
        import numpy as np
        img = cv2.imread(str(path)) if path.is_file() else None
        if img is None:
            return None
        img = cv2.resize(img, (160, 90), interpolation=cv2.INTER_AREA).astype(np.int16)
        spread = img.max(axis=2) - img.min(axis=2)
        return float((spread > GRAY_PIXEL_SPREAD).mean()) < GRAY_COLORFUL_MAX
    except Exception:  # noqa: BLE001 - a missing or odd file: say nothing about the picture
        return None


def _snapshot(e: dict) -> Path | None:
    return settings.data_dir / e["snapshot"] if e.get("snapshot") else None


def picture_line(e: dict, others: list[dict]) -> str | None:
    """The clip is black-and-white, and the other events (this camera, the hour) that are in color, if any."""
    snap = _snapshot(e)
    if not snap or not is_gray(snap):
        return None
    line = "this clip is black-and-white: the camera was in night (infrared) mode, so the scene was dark"
    color = [o for o in others if (p := _snapshot(o)) and is_gray(p) is False]
    if color:
        line += f". This camera's events at {_times(color)} are in color (lit)"
    return line + "."


# ---------------------------------------------------------------- recording

def recording_lines(spans: list[tuple[float, float]], lo: float, hi: float, t: float, now: float) -> list[str]:
    """What a camera's recording did in [lo, hi] (its spans, clipped to it), relative to the event at `t`: stopped,
    started again, or nothing all hour. Gaps shorter than GAP_MIN_S don't count."""
    if not spans:
        return ["recorded nothing in this hour"]
    out = []
    if spans[0][0] - lo >= GAP_MIN_S:
        out.append(f"was not recording until {_at(spans[0][0], t)}")
    for (_, end), (start, _) in zip(spans, spans[1:]):
        if start - end >= GAP_MIN_S:
            out.append(f"stopped recording at {_at(end, t)} and started again at {_at(start, t)}")
    end = spans[-1][1]
    if hi - end >= GAP_MIN_S:
        out.append(f"stopped recording at {_at(end, t)} and " + ("has not recorded since" if hi >= now - GAP_MIN_S
                                                                 else f"had not recorded again by {_clock(hi)}"))
    return out


async def recording_part(e: dict, now: float) -> list[str]:
    t = e["start_ts"]
    lo, hi = t - WINDOW_S, min(t + WINDOW_S, now)
    lines = []
    for c in db.cameras(enabled_only=True):
        if not recindex.indexed(c["id"]):
            continue          # the index isn't read yet (just started): nothing reliable to say
        said = recording_lines(await recindex.spans_ts(c["id"], lo, hi), lo, hi, t, now)
        name = "This camera" if c["id"] == e["camera_id"] else c["name"]
        lines += [f"{name} {x}" for x in said]
    return lines[:MAX_DEVICE_LINES]


# ---------------------------------------------------------------- the camera's other events

def nearby_events(e: dict) -> tuple[list[dict], list[dict]]:
    """This camera's other events in the hour before and after, nearest first (masked and rejected ones left out)."""
    t = e["start_ts"]
    cols = "id, start_ts, end_ts, status, camera_class, yolo_class, synopsis, snapshot"
    keep = "camera_id=? AND id<>? AND status NOT IN ('masked', 'rejected')"
    before = db.all(f"SELECT {cols} FROM events WHERE {keep} AND start_ts>=? AND start_ts<? ORDER BY start_ts DESC LIMIT ?",
                    [e["camera_id"], e["id"], t - WINDOW_S, t, EVENTS_EACH_SIDE])
    after = db.all(f"SELECT {cols} FROM events WHERE {keep} AND start_ts>=? AND start_ts<=? ORDER BY start_ts LIMIT ?",
                   [e["camera_id"], e["id"], t, t + WINDOW_S, EVENTS_EACH_SIDE])
    return before, after


def event_line(o: dict) -> str:
    dur = (o["end_ts"] or o["start_ts"]) - o["start_ts"]
    what = o["yolo_class"] or o["camera_class"] or "motion"
    text = " ".join((o.get("synopsis") or "").split())
    if len(text) > SYNOPSIS_CHARS:
        text = text[:SYNOPSIS_CHARS].rsplit(" ", 1)[0] + "..."
    return f"  {_clock(o['start_ts'])} {what}, {dur:.0f} s: {text or '(no description yet)'}"


# ---------------------------------------------------------------- the brief

async def build(e: dict, events: bool = True, now: float | None = None) -> str | None:
    """The brief for a chat turn on event `e`, or None when there is nothing to say. Never raises."""
    now = time.time() if now is None else now
    try:
        before, after = await asyncio.to_thread(nearby_events, e)
        parts = []
        picture = await asyncio.to_thread(picture_line, e, before + after)
        if picture:
            parts.append(f"- Picture: {picture}")
        rec = await recording_part(e, now)
        if rec:
            parts.append("- Recording on this server:\n" + "\n".join(f"  {x}" for x in rec))
        if events:
            parts.append("- This camera, before this event (nearest first):\n" + "\n".join(map(event_line, before))
                         if before else "- This camera, before this event: nothing in the hour before.")
            parts.append("- This camera, after this event (nearest first):\n" + "\n".join(map(event_line, after))
                         if after else "- This camera, after this event: nothing in the hour after.")
        if not parts:
            return None
        return "Situation brief (this server, the hour either side of the event; local times):\n" + "\n".join(parts)
    except Exception:  # noqa: BLE001 - the brief helps; an Ask must never fail for it
        log.warning("situation brief for event %s failed", e.get("id"), exc_info=True)
        return None


def _clock(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).astimezone().strftime("%H:%M")


def _at(ts: float, t: float) -> str:
    """A time and where it falls against the event: "08:42 (1 min before this event)"."""
    m = round((ts - t) / 60)
    rel = "around the time of this event" if m == 0 else f"{abs(m)} min {'before' if m < 0 else 'after'} this event"
    return f"{_clock(ts)} ({rel})"


def _times(events: list[dict]) -> str:
    ts = sorted(_clock(o["start_ts"]) for o in events)
    return ", ".join(ts[:-1]) + " and " + ts[-1] if len(ts) > 1 else ts[0]
