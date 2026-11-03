"""The recordings index: every segment file's start and end, kept in the database, so the Timeline's listing is a
query instead of a walk of the recording disk.

MediaMTX's playback /list opens every file in the window to read its length. On a busy spinning disk that took 4-26 s
per camera for a 24 h window and 20-46 s for a week (TSS-DT, 2026-11-03), and again after every MediaMTX restart.

A finished segment carries its length in moov/mvhd: MediaMTX patches it when it closes the file, and so do
fmp4.trim (retention) and fmp4mux (SD card backfill). Its start is in moov/udta/mtxi (wall-clock ns, what MediaMTX
lists by), not the file name: names are local time, and the hour repeated when daylight saving ends has every name
twice, an hour apart. So a segment costs one small header read, once: the scan here lists each camera's folder every
SCAN_S (names, sizes, modification times) and reads the header only of files it hasn't seen or that changed; deleted
files drop out.

The file still being written (the newest, its mvhd still 0) isn't stored: it counts as recorded from its start to
now (LIVE_LAG_S). Asking MediaMTX for just that one file still cost ~220 ms with a folder of 1-minute files. When it
closes (or the camera stops and MediaMTX closes it) it gets its length and is indexed like the rest. Until a camera's
first scan is done, its whole listing comes from MediaMTX as before.
"""
from __future__ import annotations

import asyncio
import logging
import os
import struct
import time
from pathlib import Path

from . import mediamtx
from .config import settings
from .db import db
from .fmp4 import segment_start

log = logging.getLogger("nvr.recindex")

SCAN_S = 15.0
HEAD_BYTES = 64 * 1024        # ftyp + moov of a MediaMTX segment: ~1-2 KB; read a little more for odd cameras
LONGEST_S = 6 * 3600          # no segment is longer (MediaMTX's recordSegmentDuration, SD restores: 10 min)

LIVE_LAG_S = 2.0              # the file being written is listed up to this long ago (MediaMTX writes 1 s parts)
LIVE_MAX_S = 2 * 3600         # ...and only while it started this recently: one older without a length was abandoned
MERGE_S = 0.5                 # back-to-back files closer than this are listed as one span (as sdbackfill merges)

_ready: set[str] = set()      # paths whose first scan is done: their listing comes from the index
_live: dict[str, float] = {}  # path -> start of the file being written, as of the last scan


def header_info(path: Path) -> tuple[float | None, float | None]:
    """(start epoch from moov/udta/mtxi, seconds from moov/mvhd); None where the header doesn't say (mvhd is 0 while
    the file is being written; files from before mtxi have none)."""
    try:
        with open(path, "rb") as f:
            head = f.read(HEAD_BYTES)
    except OSError:
        return None, None
    start = dur = None
    off = 0
    while off + 8 <= len(head):
        size, typ = struct.unpack(">I4s", head[off:off + 8])
        if size < 8:
            break
        if typ == b"moov":
            end = min(off + size, len(head))
            o = off + 8
            while o + 8 <= end:
                s2, t2 = struct.unpack(">I4s", head[o:o + 8])
                if s2 < 8:
                    break
                if t2 == b"mvhd":
                    v, b = head[o + 8], o + 12
                    if v == 0 and b + 16 <= end:
                        ts, units = struct.unpack(">II", head[b + 8:b + 16])
                    elif v == 1 and b + 28 <= end:
                        ts, units = struct.unpack(">IQ", head[b + 16:b + 28])
                    else:
                        ts = units = 0
                    dur = units / ts if ts and units else None
                elif t2 == b"udta":
                    u = o + 8
                    while u + 8 <= min(o + s2, end):
                        s3, t3 = struct.unpack(">I4s", head[u:u + 8])
                        if s3 < 8:
                            break
                        if t3 == b"mtxi" and u + 8 + 44 <= end:
                            # version/flags(4) + stream id(16), then segment number, stream DTS, wall clock (ns)
                            ntp = struct.unpack(">Q", head[u + 8 + 36:u + 8 + 44])[0]
                            if ntp > 10**17:   # after 1973: a real wall clock
                                start = ntp / 1e9
                        u += s3
                o += s2
            break
        off += size
    return start, dur


def header_duration(path: Path) -> float | None:
    return header_info(path)[1]


def _listdir(folder: Path) -> dict[str, tuple[float, int, float]]:
    """name -> (start epoch, size, mtime) for the segment files in a path's folder."""
    out = {}
    try:
        with os.scandir(folder) as it:
            for e in it:
                if not e.name.endswith(".mp4") or (start := segment_start(Path(e.name))) is None:
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue   # deleted by retention meanwhile
                out[e.name] = (start, st.st_size, st.st_mtime)
    except FileNotFoundError:
        pass
    return out


def scan_path(path: str) -> int:
    """Bring one path's rows in line with its folder. Returns how many rows changed."""
    folder = settings.recordings_dir / path
    files = _listdir(folder)
    newest = max(files, key=lambda n: files[n][0]) if files else None
    known = {r["name"]: (r["size"], r["mtime"]) for r in
             db.all("SELECT name, size, mtime FROM rec_segments WHERE camera_id=?", [path])}
    starts = sorted(s for s, _, _ in files.values())
    upserts = []
    live = None
    for name, (start, size, mtime) in files.items():
        if known.get(name) == (size, mtime):
            continue
        head_start, dur = header_info(folder / name)
        start = head_start or start
        if dur is None:
            if name == newest and start > time.time() - LIVE_MAX_S:
                live = start   # being written: listed up to now, stored once it closes
                continue
            # a finished file without its length (MediaMTX stopped mid-segment): it ends at its last write, or
            # where the next one starts
            nxt = next((s for s in starts if s > start), None)
            dur = max(0.0, (min(mtime, nxt) if nxt else mtime) - start)
        upserts.append((path, name, start, start + dur, size, mtime))
    if live is None:
        _live.pop(path, None)
    else:
        _live[path] = live
    gone = [n for n in known if n not in files]
    if upserts or gone:
        db.replace_rec_segments(path, upserts, gone)
    return len(upserts) + len(gone)


def scan_all() -> int:
    root = settings.recordings_dir
    try:
        paths = sorted(e.name for e in os.scandir(root) if e.is_dir())
    except FileNotFoundError:
        return 0
    changed = 0
    for p in paths:
        try:
            changed += scan_path(p)
            _ready.add(p)
        except Exception:  # noqa: BLE001 - one bad folder must not stop the others
            log.exception("recordings index: scanning %s failed", p)
    return changed


async def run(stop: asyncio.Event | None = None) -> None:
    first = True
    while stop is None or not stop.is_set():
        t0 = time.monotonic()
        n = await asyncio.to_thread(scan_all)
        if first:
            log.info("recordings index: %d segments indexed or updated in %.0f s", n, time.monotonic() - t0)
            first = False
        await asyncio.sleep(SCAN_S)


def _rows(path: str, lo: float, hi: float) -> list[tuple[float, float]]:
    rows = db.all("SELECT start_ts, end_ts FROM rec_segments WHERE camera_id=? AND start_ts>=? AND start_ts<? AND end_ts>? "
                  "ORDER BY start_ts", [path, lo - LONGEST_S, hi, lo])
    return [(r["start_ts"], r["end_ts"]) for r in rows]


async def recording_spans(path: str, start: float | None = None, end: float | None = None) -> list[dict]:
    """MediaMTX /list's answer ({start: RFC 3339, duration}, clipped to the window) from the index, the file being
    written included; back-to-back files are one span (a week of 1-minute files was 1,000 entries)."""
    if path not in _ready:
        return await mediamtx.recording_spans(path, start, end)
    lo, hi = start or 0.0, end or float("inf")
    spans = await asyncio.to_thread(_rows, path, lo, hi)
    if (live := _live.get(path)) is not None:
        spans.append((live, time.time() - LIVE_LAG_S))
    merged: list[list[float]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1] + MERGE_S:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out = []
    for s, e in merged:
        s, e = max(s, lo), min(e, hi)
        if e > s:
            out.append({"start": mediamtx.rfc3339(s), "duration": round(e - s, 3)})
    return out
