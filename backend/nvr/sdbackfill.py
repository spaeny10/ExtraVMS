"""SD card backfill: put footage the server missed (restart, link down, video-service reload) back on the Timeline
from the camera's own recording (ONVIF Profile G). See docs/sd-card-backfill.md.

Phase 1 (this module): footage only, started by hand (POST /api/sd/recover).

- SD status per camera, refreshed hourly (GetRecordingSummary + FindRecordings + GetReplayUri, read-only):
  has_recording, earliest, latest, recording_now, the replay URI.
- Gaps: holes of GAP_MIN_S or more between the server's own recordings in the last LOOKBACK_H hours (the MediaMTX
  listing the Timeline draws), clipped to what the camera's card holds.
- The worker: one replay session per camera at a time (cameras limit sessions), up to MAX_PARALLEL cameras in
  parallel on the worker's own threads, newest job first. Footage is written as MediaMTX segments (fmp4mux) into that camera's own recording folder, never
  replacing a file, so playback, the Timeline, export and retention treat it as ordinary footage. Each job is a
  row of `restored_spans`; the Timeline shades its range "Recovered from the camera's SD card".

Times: replay runs on the camera's clock. Restored frames are moved onto this server's clock with the same offset
the metadata reader measures for live detections (ingest.MetadataReader.clock_offset, arrival minus camera time),
so they land where MediaMTX would have put them (measured on cam5: within 26 ms of the live recording's frames),
when that offset agrees with the camera's ONVIF clock within CLOCK_AGREE_S; otherwise the ONVIF difference.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import functools
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from . import fmp4mux, mediamtx, sdreplay
from .config import settings
from .db import db
from .onvif_soap import Onvif, OnvifError, discover_services, find_all, sync_clock, text

log = logging.getLogger("nvr.sdbackfill")

GAP_MIN_S = 20.0              # shorter holes are ignored (a segment boundary, a 2 s keyframe wait)
LOOKBACK_H = 72               # gaps are looked for this far back
STATUS_TTL_S = 3600           # SD status is refreshed hourly
STATUS_KEY = "sd_status"      # settings table: {camera_id: status} survives a restart
RECORDING_NOW_S = 600         # the card's newest footage within this of now = the camera is recording to it
TAIL_MARGIN_S = 60            # a hole reaching "now" is only a gap once it is this old (MediaMTX may be reconnecting)
MAX_JOB_S = 12 * 3600         # one request covers at most this much (replay runs at real time)
COVERED_SLACK_S = 5.0         # a job that misses no more than this (or 5 %) counts as fully recovered
SUB_GAP_MIN_S = 2.0           # inside a job, holes shorter than this are left alone (a keyframe's worth)
MAX_PARALLEL = 3              # recoveries at once on this server (each holds a worker thread for hours); others wait
CLOCK_AGREE_S = 2.0           # the live clock offset is trusted only this close to the ONVIF clock difference
LIVE_MIN_SAMPLES = 50         # ... and resting on at least this many metadata frames (when the count is known)

STATES = ("waiting", "recovering", "recovered", "partly recovered", "not on the card", "failed")
OPEN_STATES = ("waiting", "recovering")

# the restored_spans table is in db.SCHEMA


# --------------------------------------------------------------------------- SD status (ONVIF, read-only)

NS_SEARCH = "http://www.onvif.org/ver10/search/wsdl"
NS_REPLAY = "http://www.onvif.org/ver10/replay/wsdl"
NS_RECORDING = "http://www.onvif.org/ver10/recording/wsdl"


def _iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _plausible(e: float | None, l: float | None) -> bool:
    """An empty recording reports DataFrom 2038-01-19 and DataUntil 1970-01-01 (Milesight, no card): not footage."""
    return e is not None and l is not None and 946684800 < e < l < time.time() + 86400


def query_status(cam: dict, timeout: float = 8) -> dict:
    """The camera's own recording, read-only: {supported, has_recording, earliest, latest, recording_now, replay_uri,
    recording_token, clock_offset_s, checked_at, error}. Times are on this server's clock (the camera's minus its
    clock offset). Blocking: run in a thread."""
    out: dict = {"camera_id": cam["id"], "checked_at": time.time(), "supported": False, "has_recording": False,
                 "earliest": None, "latest": None, "recording_now": False, "replay_uri": None, "recording_token": None,
                 "clock_offset_s": None, "error": None}
    try:
        o = Onvif.for_camera(cam, timeout=timeout)
        try:
            off = sync_clock(o).total_seconds()      # camera minus this server
            out["clock_offset_s"] = round(off, 1)
        except OnvifError:
            off = 0.0
        services = discover_services(o)
        search, replay = services.get(NS_SEARCH), services.get(NS_REPLAY)
        if not search or not replay:
            out["error"] = "the camera has no ONVIF recording search / replay service (Profile G)"
            return out
        out["supported"] = True
        s = o.call(search, f'<GetRecordingSummary xmlns="{NS_SEARCH}"/>')
        n = int(text(s, "NumberRecordings") or 0)
        if not n or not text(s, "DataFrom"):
            return out                                # "no recording on the camera"
        r = o.call(search, f'<FindRecordings xmlns="{NS_SEARCH}"><Scope/><KeepAliveTime>PT10S</KeepAliveTime></FindRecordings>')
        token = text(r, "SearchToken")
        res = o.call(search, f'<GetRecordingSearchResults xmlns="{NS_SEARCH}"><SearchToken>{token}</SearchToken>'
                             '<MinResults>1</MinResults><MaxResults>10</MaxResults><WaitTime>PT5S</WaitTime></GetRecordingSearchResults>')
        best = None
        for ri in find_all(res, "RecordingInformation"):
            if not any(text(t, "TrackType") == "Video" for t in find_all(ri, "Track")):
                continue
            e, l = _iso(text(ri, "EarliestRecording")), _iso(text(ri, "LatestRecording"))
            if _plausible(e, l) and (best is None or l - e > best[2] - best[1]):
                best = (text(ri, "RecordingToken"), e, l)
        if best is None:
            e, l = _iso(text(s, "DataFrom")), _iso(text(s, "DataUntil"))
            if not _plausible(e, l):
                return out                            # an empty recording: no card, or recording to it is off
            best = (None, e, l)
        token, e, l = best
        out.update(has_recording=True, recording_token=token, earliest=e - off, latest=l - off,
                   recording_now=(time.time() - (l - off)) < RECORDING_NOW_S)
        if token:
            try:
                rp = o.call(replay, f'<GetReplayUri xmlns="{NS_REPLAY}"><StreamSetup>'
                                    '<Stream xmlns="http://www.onvif.org/ver10/schema">RTP-Unicast</Stream>'
                                    '<Transport xmlns="http://www.onvif.org/ver10/schema"><Protocol>RTSP</Protocol></Transport>'
                                    f'</StreamSetup><RecordingToken>{token}</RecordingToken></GetReplayUri>')
                uri = text(rp, "Uri")
                out["replay_uri"] = sdreplay.replay_url({**cam, "public_host": None}, uri) if uri else None
            except OnvifError as ex:
                log.info("[%s] GetReplayUri: %s (using the default replay port)", cam["id"], ex)
    except OnvifError as ex:
        out["error"] = str(ex)[:200]
    except Exception as ex:  # noqa: BLE001  (a malformed reply must not take the status loop down)
        out["error"] = f"{type(ex).__name__}: {str(ex)[:160]}"
    return out


def _saved() -> dict:
    v = db.get_setting(STATUS_KEY) or {}
    return v if isinstance(v, dict) else {}


def cached_status(camera_id: str) -> dict | None:
    return _saved().get(camera_id)


def save_status(st: dict) -> None:
    allst = _saved()
    allst[st["camera_id"]] = st
    db.set_setting(STATUS_KEY, allst)


async def refresh_status(cam: dict) -> dict:
    st = await asyncio.to_thread(query_status, cam)
    save_status(st)
    return st


async def status(cam: dict, refresh: bool = False) -> dict:
    """Cached status, re-queried when older than STATUS_TTL_S or asked to (at most once a minute)."""
    st = cached_status(cam["id"])
    age = time.time() - (st or {}).get("checked_at", 0)
    if st is None or age > STATUS_TTL_S or (refresh and age > 60):
        st = await refresh_status(cam)
    return st


def card_range(st: dict | None, now: float | None = None) -> tuple[float, float] | None:
    """(earliest, latest) the camera's card holds on this server's clock; a card still recording holds up to now
    (the status is up to an hour old)."""
    if not st or not st.get("has_recording") or st.get("earliest") is None or st.get("latest") is None:
        return None
    return st["earliest"], (max(st["latest"], now or time.time()) if st.get("recording_now") else st["latest"])


def status_text(st: dict | None) -> str:
    """'SD card: recording, holds Sep 14 → now' / 'no recording on the camera' (Settings → Cameras)."""
    if not st:
        return "SD card: not checked yet"
    if st.get("error") and not st.get("supported"):
        return "SD card: can't tell (" + st["error"] + ")"
    if not st.get("has_recording") or not _plausible(st.get("earliest"), st.get("latest")):
        return "SD card: no recording on the camera"
    def fmt(t: float) -> str:
        d = dt.datetime.fromtimestamp(t)
        return f"{d:%b} {d.day}"
    until = "now" if st.get("recording_now") else fmt(st["latest"])
    return f"SD card: {'recording' if st.get('recording_now') else 'not recording'}, holds {fmt(st['earliest'])} → {until}"


async def status_loop(stop: asyncio.Event | None = None) -> None:
    """Every hour: refresh the SD status of each enabled camera (read-only ONVIF)."""
    await asyncio.sleep(30)
    while not (stop and stop.is_set()):
        for cam in db.cameras(enabled_only=True):
            st = cached_status(cam["id"])
            if st and time.time() - st.get("checked_at", 0) < STATUS_TTL_S - 60:
                continue
            try:
                await refresh_status(cam)
            except Exception:  # noqa: BLE001
                log.exception("[%s] SD status", cam["id"])
        await asyncio.sleep(300)


# --------------------------------------------------------------------------- gaps

def spans_from_listing(listing: list[dict]) -> list[tuple[float, float]]:
    """MediaMTX playback /list items ({start: RFC 3339, duration: s}) -> sorted, merged (start, end) epochs."""
    raw = []
    for it in listing or []:
        s = _iso(it.get("start"))
        if s is not None:
            raw.append((s, s + float(it.get("duration") or 0)))
    raw.sort()
    out: list[list[float]] = []
    for s, e in raw:
        if out and s <= out[-1][1] + 0.5:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def find_gaps(spans: list[tuple[float, float]], lo: float, hi: float, min_gap: float = GAP_MIN_S) -> list[tuple[float, float]]:
    """Holes of at least `min_gap` s between recorded spans, inside [lo, hi], plus the hole after the last span
    up to `hi`. Nothing before the first span (the camera may simply not have been here)."""
    spans = sorted(spans)
    out = []
    for (_, e1), (s2, _) in zip(spans, spans[1:]):
        a, b = max(e1, lo), min(s2, hi)
        if b - a >= min_gap:
            out.append((a, b))
    if spans:
        a = max(max(e for _, e in spans), lo)
        if hi - a >= min_gap:
            out.append((a, hi))
    return out


def clip(ranges: list[tuple[float, float]], lo: float | None, hi: float | None, min_len: float = 0.0) -> list[tuple[float, float]]:
    out = []
    for a, b in ranges:
        a2, b2 = max(a, lo) if lo is not None else a, min(b, hi) if hi is not None else b
        if b2 - a2 > min_len:
            out.append((a2, b2))
    return out


def subtract(ranges: list[tuple[float, float]], holes: list[tuple[float, float]], min_len: float = 0.0) -> list[tuple[float, float]]:
    """`ranges` minus `holes`."""
    out = []
    for a, b in ranges:
        parts = [(a, b)]
        for c, d in holes:
            nxt = []
            for x, y in parts:
                if d <= x or c >= y:
                    nxt.append((x, y))
                    continue
                if c > x:
                    nxt.append((x, c))
                if d < y:
                    nxt.append((d, y))
            parts = nxt
        out += [(x, y) for x, y in parts if y - x > min_len]
    return out


def jobs(camera_id: str | None = None, since: float = 0.0) -> list[dict]:
    q, p = "SELECT * FROM restored_spans WHERE to_ts >= ?", [since]
    if camera_id:
        q += " AND camera_id = ?"
        p.append(camera_id)
    return db.all(q + " ORDER BY from_ts DESC", p)


def restored_for_timeline(camera_id: str, lo: float, hi: float) -> list[dict]:
    return db.all("SELECT id, from_ts, to_ts, state, source, restored_s, done_from, done_to FROM restored_spans "
                  "WHERE camera_id=? AND to_ts>=? AND from_ts<=? ORDER BY from_ts", [camera_id, lo, hi])


async def camera_gaps(cam: dict, hours: float = LOOKBACK_H, now: float | None = None,
                      list_spans: Callable | None = None) -> dict:
    """{camera_id, sd: status, gaps: [{from, to, seconds, on_card}], restored: [rows]} for the last `hours`."""
    now = now or time.time()
    lo, hi = now - hours * 3600, now - TAIL_MARGIN_S
    lister = list_spans or mediamtx.recording_spans
    try:
        listing = await lister(cam["id"], lo - 3600, now)   # from an hour earlier: a hole at `lo` needs the span before it
        error = None
    except Exception as e:  # noqa: BLE001
        listing, error = [], f"recordings listing failed ({type(e).__name__})"
    gaps = find_gaps(spans_from_listing(listing), lo, hi)
    st = cached_status(cam["id"])
    rows = jobs(cam["id"], since=lo)
    handled = [(r["from_ts"], r["to_ts"]) for r in rows if r["state"] != "failed"]
    out = []
    for a, b in subtract(gaps, handled, min_len=GAP_MIN_S):
        card = card_range(st, now)
        on = clip([(a, b)], *card) if card else []
        cover = sum(y - x for x, y in on)
        out.append({"from": a, "to": b, "seconds": round(b - a, 1),
                    "on_card": "yes" if cover >= (b - a) - 1 else ("partly" if cover > 0 else "no"),
                    "card_from": on[0][0] if on else None, "card_to": on[-1][1] if on else None})
    out.sort(key=lambda g: -g["from"])            # newest first
    return {"camera_id": cam["id"], "sd": st, "gaps": out, "restored": rows, "error": error}


# --------------------------------------------------------------------------- the worker

def camera_folder(camera_id: str) -> Path:
    """The camera's own recording folder; refuses anything that would resolve elsewhere."""
    if not mediamtx.CAMERA_ID_RE.fullmatch(camera_id or ""):
        raise ValueError(f"bad camera id {camera_id!r}")
    root = settings.recordings_dir.resolve()
    folder = (settings.recordings_dir / camera_id).resolve()
    assert folder.parent == root and folder.name == camera_id, f"{folder} is not a camera folder under {root}"
    return folder


def clean_temp_files(camera_id: str) -> int:
    """Remove this module's own unfinished files (.<segment>.<pid>.sdpart) after a crash; nothing else."""
    n = 0
    folder = camera_folder(camera_id)
    if folder.exists():
        for f in folder.glob(".*.sdpart"):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    return n


class WriterSink(sdreplay.Sink):
    """fetch_range -> SegmentWriter: camera time onto this server's clock, writer created from the replay's SDP."""

    def __init__(self, folder: Path, lo: float, hi: float, offset: float, on_segment: Callable | None = None):
        self.folder, self.lo, self.hi, self.offset, self.on_segment = folder, lo, hi, offset, on_segment
        self.writer: fmp4mux.SegmentWriter | None = None

    def start(self, tracks: list[sdreplay.Track]) -> None:
        v = next(t for t in tracks if t.kind == "video")
        a = next((t for t in tracks if t.kind == "audio" and t.codec in ("PCMU", "PCMA", "L16")), None)
        self.writer = fmp4mux.SegmentWriter(self.folder, v.codec, params=sdreplay.parameter_sets(v),
                                            audio=(a.clock_rate or 8000, a.channels or 1) if a else None,
                                            segment_s=_segment_s(), lo=self.lo, hi=self.hi, on_segment=self.on_segment)

    def video(self, au: sdreplay.AccessUnit) -> None:
        self.writer.add_video(au.ntp + self.offset, au.nals, au.keyframe)

    def audio(self, ch: sdreplay.AudioChunk) -> None:
        self.writer.add_audio(ch.ntp + self.offset, ch.pcm, ch.samples)

    def rollback(self) -> float | None:
        t = self.writer.rollback() if self.writer else None
        return None if t is None else t - self.offset


def _segment_s() -> float:
    s = settings.segment_duration.strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600}.get(s[-1:], 1)
    try:
        return float(s[:-1] if s[-1:] in "smh" else s) * mult
    except ValueError:
        return 600.0


def restore_range(cam: dict, lo: float, hi: float, offset: float, stop: threading.Event | None = None,
                  replay_uri: str | None = None, on_segment: Callable | None = None,
                  session_factory: Callable | None = None) -> tuple[list[fmp4mux.Written], sdreplay.FetchResult]:
    """Replay [lo, hi) (this server's clock) from the camera and write it into its folder. Blocking."""
    folder = camera_folder(cam["id"])
    sink = WriterSink(folder, lo, hi, offset, on_segment)
    url = sdreplay.replay_url(cam, replay_uri)
    try:
        res = sdreplay.fetch_range(url, cam["username"], cam["password"], lo - offset, hi - offset, sink, stop=stop,
                                   session_factory=session_factory)
    except BaseException:
        # the writer failed mid-run (a segment of that name exists, a disk error, unparsable parameter sets): its open
        # segment's temp file and handle go; segments already published stay and are counted
        if sink.writer:
            sink.writer.abort()
        raise
    written = []
    if sink.writer:
        try:
            if res.reason == "stopped" or res.unsupported:
                sink.writer.abort()                 # shutting down, or B-frames: the open segment's temp file goes
                written = sink.writer.written
            else:
                written = sink.writer.close()
        except fmp4mux.SegmentExists as e:
            log.warning("[%s] not written, a segment of that name exists: %s", cam["id"], e)
            sink.writer.abort()
            written = sink.writer.written
        except BaseException:
            sink.writer.abort()
            raise
    for w in written:
        assert w.path.parent == folder, w.path   # never outside the camera's own folder
    return written, res


def clock_offset_for(camera_id: str, live_offset: float | None, status: dict | None,
                     live_samples: int | None = None) -> float:
    """Seconds to add to the camera's time. The metadata reader's live estimate (as ingest re-times detections; within
    26 ms of MediaMTX's own frames on cam5) only when it agrees with the ONVIF clock difference within CLOCK_AGREE_S
    and, when the count is known, rests on LIVE_MIN_SAMPLES: a reader just restarted or backed up can be seconds
    off. Otherwise the ONVIF difference (whole-second clocks); with no ONVIF difference the live estimate; else 0."""
    onvif = -float(status["clock_offset_s"]) if status and status.get("clock_offset_s") is not None else None
    if live_offset is not None and onvif is not None:
        enough = live_samples is None or live_samples >= LIVE_MIN_SAMPLES
        if enough and abs(live_offset - onvif) <= CLOCK_AGREE_S:
            return live_offset + settings.camera_clock_offset
        log.warning("[%s] live clock offset %+.2f s (%s samples) disagrees with the camera's ONVIF clock (%+.1f s): "
                    "using the ONVIF one", camera_id, live_offset, "?" if live_samples is None else live_samples, onvif)
        return onvif + settings.camera_clock_offset
    if onvif is not None:
        return onvif + settings.camera_clock_offset
    if live_offset is not None:
        return live_offset + settings.camera_clock_offset
    return settings.camera_clock_offset


class Backfill:
    """Runs waiting restored_spans jobs: one per camera at a time, up to MAX_PARALLEL cameras in parallel (the rest
    stay waiting), newest first. Replays run on this object's own threads, never the event loop's default executor
    the rest of the server shares (a replay holds its thread for as long as the range lasts).
    `live_offset(camera_id)`: the metadata reader's clock offset, or (offset, samples it rests on), or None."""

    def __init__(self, live_offset: Callable[[str], float | tuple | None] | None = None, list_spans: Callable | None = None,
                 session_factory: Callable | None = None):
        self.live_offset = live_offset or (lambda _cid: None)
        self.list_spans = list_spans or mediamtx.recording_spans
        self.session_factory = session_factory
        self.pool = ThreadPoolExecutor(MAX_PARALLEL, thread_name_prefix="sd-backfill")
        self.running: dict[str, asyncio.Task] = {}
        self.stops: dict[str, threading.Event] = {}
        self.progress: dict[int, float] = {}       # job id -> last restored instant
        self._wake = asyncio.Event()

    def submit(self, camera_id: str, start: float, end: float, by: str | None = None) -> dict:
        jid = db.execute_insert("INSERT INTO restored_spans (camera_id, from_ts, to_ts, source, state, created_at, updated_at, requested_by) "
                                "VALUES (?,?,?,?,?,?,?,?)", [camera_id, start, end, "sd", "waiting", time.time(), time.time(), by])
        self._wake.set()
        return db.one("SELECT * FROM restored_spans WHERE id=?", [jid])

    def recover_interrupted(self) -> None:
        """After a restart: jobs that were running go back to waiting (what they wrote is kept; the rest is redone)."""
        db.execute("UPDATE restored_spans SET state='waiting', updated_at=? WHERE state='recovering'", [time.time()])
        for cam in db.cameras():
            try:
                clean_temp_files(cam["id"])
            except (ValueError, AssertionError, OSError):
                pass

    async def run(self) -> None:
        self.recover_interrupted()
        while True:
            self._start_waiting()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), 10)
            except asyncio.TimeoutError:
                pass

    def _start_waiting(self) -> None:
        for cid in [c for c, t in self.running.items() if t.done()]:
            self.running.pop(cid)
        rows = db.all("SELECT * FROM restored_spans WHERE state='waiting' ORDER BY from_ts DESC")
        for r in rows:
            if len(self.running) >= MAX_PARALLEL:
                break                                 # the rest stay waiting until one finishes
            if r["camera_id"] in self.running:
                continue
            self.running[r["camera_id"]] = asyncio.create_task(self._job(r), name=f"sd-backfill-{r['camera_id']}")

    def stop_all(self) -> None:
        for ev in self.stops.values():
            ev.set()

    def _set(self, jid: int, **fields) -> None:
        fields["updated_at"] = time.time()
        db.execute(f"UPDATE restored_spans SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", [*fields.values(), jid])

    async def _job(self, row: dict) -> None:
        jid, cid = row["id"], row["camera_id"]
        cam = next((c for c in db.cameras() if c["id"] == cid), None)
        if cam is None:
            self._set(jid, state="failed", error="unknown camera")
            return
        self._set(jid, state="recovering", error=None)
        try:
            await self._run_job(row, cam)
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] SD recovery %s failed", cid, jid)
            self._set(jid, state="failed", error=f"{type(e).__name__}: {str(e)[:200]}")
        finally:
            self.stops.pop(cid, None)
            self.progress.pop(jid, None)
            self._wake.set()

    async def _run_job(self, row: dict, cam: dict) -> None:
        jid, cid = row["id"], cam["id"]
        lo, hi = row["from_ts"], row["to_ts"]
        if cam.get("record_stream") == "sub":
            self._set(jid, state="failed", error="this camera records its sub stream here; the card holds the main stream")
            return
        st = await status(cam)
        if not st.get("has_recording"):
            self._set(jid, state="not on the card", error=st.get("error") or "no recording on the camera")
            return
        card = card_range(st)
        want = clip([(lo, hi)], *card) if card else []
        # only what is still missing: a restart may have restored part of it already
        try:
            listing = await self.list_spans(cid, lo - 3600, hi + 3600)
        except Exception as e:  # noqa: BLE001
            self._set(jid, state="waiting", error=f"recordings listing failed ({type(e).__name__}); will retry")
            await asyncio.sleep(30)
            return
        todo = sorted(subtract(want, spans_from_listing(listing), min_len=SUB_GAP_MIN_S), reverse=True)   # newest first
        before = db.one("SELECT restored_s FROM restored_spans WHERE id=?", [jid])["restored_s"] or 0.0
        if not todo:
            self._set(jid, state=("recovered" if before or want else "not on the card"),
                      error=None if want else "the camera's card does not cover this time")
            return
        live = self.live_offset(cid)
        live, samples = live if isinstance(live, tuple) else (live, None)
        offset = clock_offset_for(cid, live, st, samples)
        stop = threading.Event()
        self.stops[cid] = stop
        reasons = []
        loop = asyncio.get_running_loop()
        for a, b in todo:
            log.info("[%s] SD recovery %d: %s -> %s (camera clock %+.2f s)", cid, jid, _clock(a), _clock(b), -offset)
            _, res = await loop.run_in_executor(self.pool, functools.partial(
                restore_range, cam, a, b, offset, stop, st.get("replay_uri"),
                lambda w: self._segment_done(jid, w), self.session_factory))
            if res.reason and res.reason != "end":
                reasons.append(res.reason)
            if stop.is_set() or res.unsupported:
                break                                 # (B-frames: every other part would fail the same way)
        if stop.is_set():
            self._set(jid, state="waiting")          # interrupted (shutdown): resumes with what is still missing
            return
        r = db.one("SELECT restored_s, bytes FROM restored_spans WHERE id=?", [jid])
        done_s = r["restored_s"] or 0.0
        need = sum(b - a for a, b in todo) + before
        if done_s <= 0:
            state = "not on the card" if any("no recording" in x for x in reasons) else "failed"
        elif need - done_s <= max(COVERED_SLACK_S, 0.05 * need):
            state = "recovered"
        else:
            state = "partly recovered"
        self._set(jid, state=state, error="; ".join(dict.fromkeys(reasons))[:300] or None)
        log.info("[%s] SD recovery %d: %s, %.0f s of footage, %.1f MB", cid, jid, state, done_s, (r["bytes"] or 0) / 1e6)

    def _segment_done(self, jid: int, w: fmp4mux.Written) -> None:
        """A restored segment was published (worker thread): count it on the job row right away."""
        self.progress[jid] = w.end
        db.execute("UPDATE restored_spans SET bytes = bytes + ?, restored_s = restored_s + ?, "
                   "done_from = MIN(COALESCE(done_from, ?), ?), done_to = MAX(COALESCE(done_to, ?), ?), updated_at=? WHERE id=?",
                   [w.bytes, round(w.end - w.start, 3), w.start, w.start, w.end, w.end, time.time(), jid])
        from . import frames
        frames.release(w.path)       # forget the cached folder listing so previews see the new file

    def status(self) -> dict:
        return {"running": sorted(c for c, t in self.running.items() if not t.done()), "progress": dict(self.progress)}


def _clock(t: float) -> str:
    return dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
