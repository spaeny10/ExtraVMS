"""Stream health from MediaMTX's Prometheus metrics, sampled every SAMPLE_S.

Per camera: live bitrate, an estimate of recording GB/day, how long since the main stream last delivered
any bytes (a frozen camera can stay "ready" in MediaMTX for a while), corrupt frames from the camera in the
last hour (packet loss: bad cable / Wi-Fi / switch), and whether the NVR's own metadata reader is attached
to the main stream (if not, recording continues but detections are silently missed); for a camera whose detections
come from its ONVIF events (ruleevents.py), whether its event subscription works instead.

Per server: bandwidth from the cameras (inbound Mbit/s over 5 min, GB today and this month, kept per day in the
settings table) for cellular sites and central recording, and "site link down": every enabled camera without
video at once means the link to the site (VPN tunnel, 5G router) is down, one alert instead of one per camera.

Storage: "storage_failing" once the database has been failing to write for STORAGE_ALERT_S (db.storage, kept by every
statement in db.py) or a write probe of the data directory (every PROBE_S) has failed that long. 2026-10-10 the SSD
holding nvr.db failed: for 6 hours no event was stored and nothing said so. None of this needs the database itself.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
import sqlite3
import time
from collections import deque
from pathlib import Path

import httpx

from . import ingest, mediamtx
from .config import settings
from .db import db

log = logging.getLogger("nvr.health")

SAMPLE_S = 10
WINDOW = 360                 # samples kept per path (1 h) for the GB/day estimate
STALL_S = 30                 # no bytes for this long = "no video"
LINK_DOWN_S = 90             # every enabled camera without bytes for this long = the site link is down
LINK_DOWN_MIN_CAMERAS = 2    # with one camera, its own camera_down alert says the same thing
LINK_DOWN_TEXT = "All cameras unreachable: the link to the site may be down"
EVENTS_DOWN_S = 180          # a camera whose detections come from its ONVIF events: subscription down this long = a problem
BANDWIDTH_SPAN_S = 300       # Mbit/s averaged over 5 min
BANDWIDTH_KEY = "bandwidth_daily"   # settings: {"YYYY-MM-DD" (site time): bytes received from the cameras}
BANDWIDTH_KEEP_DAYS = 62
BANDWIDTH_SAVE_S = 60
STORAGE_ALERT_S = 60         # the database (or the data disk's write probe) failing this long = a server problem
PROBE_S = 60                 # write probe of settings.data_dir: a failing disk shows even when no event is being stored
PROBE_TIMEOUT_S = 20         # a write that hangs this long counts as a failure (a dying disk often hangs, not errors)
PROBE_FILE = ".write-probe"
LINE = re.compile(r'^(\w+)\{([^}]*)\}\s+([0-9.eE+-]+)$')
LABEL = re.compile(r'(\w+)="([^"]*)"')


def parse_metrics(text: str) -> dict[str, dict]:
    """Prometheus text -> {path: {inbound_bytes, frames_in_error, state, readers: {readerType: n}}}."""
    out: dict[str, dict] = {}
    for line in text.splitlines():
        m = LINE.match(line.strip())
        if not m:
            continue
        name, labels, value = m.group(1), dict(LABEL.findall(m.group(2))), float(m.group(3))
        path = labels.get("name")
        if not path or not name.startswith("paths"):
            continue
        p = out.setdefault(path, {"inbound_bytes": 0.0, "frames_in_error": 0.0, "state": labels.get("state"), "readers": {}})
        if name == "paths_inbound_bytes":
            p["inbound_bytes"] = value
        elif name == "paths_inbound_frames_in_error":
            p["frames_in_error"] = value
        elif name == "paths_readers":
            p["readers"][labels.get("readerType", "?")] = p["readers"].get(labels.get("readerType", "?"), 0) + int(value)
        elif name == "paths":
            p["state"] = labels.get("state")
    return out


def stream_problems(camera_id: str) -> list[str]:
    """What the camera's ONVIF stream check found (streams.plan): no low-resolution stream, a main path it doesn't
    list. Shown with the other problems in Settings → Cameras and sent to the hub."""
    from . import streams
    try:
        cam = db.one("SELECT id, main_path, sub_path, streams FROM cameras WHERE id=?", [camera_id])
    except sqlite3.Error:   # the database is failing (storage_alert says so): the camera's live health still counts
        return []
    return streams.problems(cam) if cam else []


# ---- storage: can this server still write its database?
def data_drive(path: Path | None = None) -> str:
    """How the problem names the data disk: the drive on Windows ("E:"), the data directory elsewhere."""
    p = Path(path or settings.data_dir)
    return p.drive or str(p)


def storage_alert(now: float | None = None) -> dict | None:
    """The `storage_failing` health alert once db.storage has been failing for more than STORAGE_ALERT_S, else None.
    Reads only the in-memory state, so it works (and the heartbeat carries it) while every statement fails."""
    s = db.storage
    since = s.get("failing_since")
    now = now or db.clock()
    if s.get("ok") or not since or now - since <= STORAGE_ALERT_S:
        return None
    err = s.get("error") or "write failed"
    at = dt.datetime.fromtimestamp(since).strftime("%H:%M")
    return {"kind": "storage_failing", "since": since, "error": err,
            "text": f"Database can't be written ({err}) since {at}: events are not being stored. "
                    f"Check the data disk ({data_drive()})."}


def probe_data_dir(path: Path | None = None) -> None:
    """Write, fsync and delete a tiny file in the data directory; raises OSError when the disk won't take it."""
    f = Path(path or settings.data_dir) / PROBE_FILE
    with open(f, "wb") as fh:
        fh.write(b"ok")
        fh.flush()
        os.fsync(fh.fileno())
    f.unlink()


class StorageWatch:
    """Every PROBE_S, probe_data_dir in a worker thread; failures (and a probe that hangs PROBE_TIMEOUT_S) feed db.storage
    like a failing statement does, a success clears what an earlier probe found."""

    def __init__(self, probe=probe_data_dir) -> None:
        self.probe = probe
        self._pending: asyncio.Future | None = None   # a probe stuck on a hung disk: never start a second one beside it
        self._pending_since = 0.0

    async def check(self) -> bool:
        """One probe; True when the data directory took the write."""
        if self._pending is not None and not self._pending.done():
            db.storage_failed(f"data disk not responding: a write has hung for {time.time() - self._pending_since:.0f} s",
                              source="probe")
            return False
        self._pending, self._pending_since = asyncio.ensure_future(asyncio.to_thread(self.probe)), time.time()
        try:
            await asyncio.wait_for(asyncio.shield(self._pending), PROBE_TIMEOUT_S)
        except asyncio.TimeoutError:
            db.storage_failed(f"data disk not responding: a write hung for {PROBE_TIMEOUT_S} s", source="probe")
            return False
        except OSError as e:
            db.storage_failed(f"data disk not writable: {e.strerror or e}", source="probe")
            return False
        db.storage_ok(source="probe")
        return True

    async def run(self) -> None:
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - the watch itself must keep going
                log.warning("storage probe: %s", e)
            await asyncio.sleep(PROBE_S)


class PathStats:
    def __init__(self) -> None:
        self.samples: deque[tuple[float, float]] = deque(maxlen=WINDOW)  # (t, bytes since first sample, monotonic)
        self.last_raw: float | None = None
        self.total = 0.0                     # counter with resets removed
        self.last_increase = 0.0
        self.errors: deque[tuple[float, float]] = deque(maxlen=WINDOW)   # (t, error frames in that interval)
        self.last_err_raw: float | None = None
        self.readers: dict[str, int] = {}
        self.state: str | None = None

    def update(self, t: float, m: dict) -> float:
        """Add one sample; returns the bytes received since the previous one."""
        raw, err = m["inbound_bytes"], m["frames_in_error"]
        if self.last_raw is None or raw < self.last_raw:  # first sample or the path was recreated
            delta = 0.0 if self.last_raw is None else raw
        else:
            delta = raw - self.last_raw
        self.last_raw = raw
        self.total += delta
        if delta > 0 or not self.samples:
            self.last_increase = t if delta > 0 else self.last_increase or t
        self.samples.append((t, self.total))
        if self.last_err_raw is not None and err >= self.last_err_raw:
            self.errors.append((t, err - self.last_err_raw))
        self.last_err_raw = err
        self.readers, self.state = m.get("readers", {}), m.get("state")
        return delta

    def bitrate_mbps(self, span_s: float = 30) -> float | None:
        if len(self.samples) < 2:
            return None
        t1, b1 = self.samples[-1]
        t0, b0 = next(((t, b) for t, b in self.samples if t >= t1 - span_s), self.samples[0])
        if t1 - t0 < 1:
            t0, b0 = self.samples[-2]
        return round((b1 - b0) * 8 / (t1 - t0) / 1e6, 3)

    def gb_per_day(self) -> float | None:
        if len(self.samples) < 6:  # need a minute
            return None
        (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
        return round((b1 - b0) / max(1.0, t1 - t0) * 86400 / 1e9, 1)

    def errors_last_hour(self, now: float) -> int:
        return int(sum(n for t, n in self.errors if t >= now - 3600))


class StreamHealth:
    def __init__(self, metrics_url: str = "http://127.0.0.1:9998/metrics") -> None:
        self.url = metrics_url
        self.paths: dict[str, PathStats] = {}
        self.last_sample = 0.0
        self.first_sample = 0.0
        self.error: str | None = None
        self.sub_recorded: set[str] = set()        # cameras whose <id>_sub relays <id> locally (mediamtx.sub_relays_main)
        self.from_cameras: set[str] = set()        # MediaMTX paths pulled from the cameras (bandwidth counts these)
        self._daily: dict[str, float] | None = None   # BANDWIDTH_KEY, loaded on first use
        self._daily_saved = 0.0
        # camera id -> its detections come from its ONVIF events (ruleevents.py; set by the API): no metadata reader
        # is expected there, so "metadata reader not attached" is not a problem (the hub would raise camera_down)
        self.event_only = lambda camera_id: False
        # ...and that camera's ONVIF event subscription: since when it pulls nothing (None while it works)
        self.events_down_since = ingest.events_down_since

    async def sample(self) -> None:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(self.url)
            r.raise_for_status()
        self.ingest(r.text)

    def ingest(self, metrics_text: str, now: float | None = None) -> None:
        """One metrics scrape: per-path stats, and the bytes the cameras sent into today's total."""
        now = now or time.time()
        try:
            cams = db.cameras(enabled_only=True)
            self.sub_recorded = {c["id"] for c in cams if mediamtx.sub_relays_main(c)}
            self.from_cameras = {p for c in cams for p in mediamtx.camera_paths(c)}
        except sqlite3.Error:
            pass   # the database is failing: keep sampling with the last camera list (stalls still show)
        received = 0.0
        for path, m in parse_metrics(metrics_text).items():
            delta = self.paths.setdefault(path, PathStats()).update(now, m)
            if path in self.from_cameras:
                received += delta
        self.add_bytes(received, now)
        self.first_sample = self.first_sample or now
        self.last_sample = now

    # ---- bandwidth (cellular sites, central recording)
    def _days(self) -> dict[str, float]:
        if self._daily is None:
            try:
                v = db.get_setting(BANDWIDTH_KEY)
            except sqlite3.Error:
                return {}   # not loaded (database failing): add_bytes counts nothing rather than overwrite the saved days
            self._daily = {k: float(n) for k, n in v.items() if isinstance(n, (int, float))} if isinstance(v, dict) else {}
        return self._daily

    def add_bytes(self, n: float, now: float | None = None) -> None:
        now = now or time.time()
        days = self._days()
        if self._daily is None:
            return
        day = dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d")   # site time, like "today" on Home
        new_day = day not in days
        days[day] = days.get(day, 0.0) + max(0.0, n)
        if new_day or now - self._daily_saved >= BANDWIDTH_SAVE_S:
            for old in sorted(days)[:-BANDWIDTH_KEEP_DAYS]:
                days.pop(old)
            try:
                db.set_setting(BANDWIDTH_KEY, {k: round(v) for k, v in days.items()})
                self._daily_saved = now
            except sqlite3.Error:
                pass   # kept in memory; saved by a later sample once the database takes writes again

    def flush(self) -> None:
        if self._daily is not None:
            try:
                db.set_setting(BANDWIDTH_KEY, {k: round(v) for k, v in self._daily.items()})
            except sqlite3.Error as e:   # shutting down with the database failing: the rest of shutdown still runs
                log.warning("bandwidth totals not saved: %s", e)

    def bandwidth(self, cams: list[dict] | None = None, now: float | None = None) -> dict:
        """{mbps, today_gb, month_gb, cameras: {id: mbps}}: what the cameras send this server (every stream pulled
        from them: the recorded one and live view's on-demand one), Mbit/s averaged over 5 min."""
        cams = db.cameras(enabled_only=True) if cams is None else cams
        per: dict[str, float | None] = {}
        for c in cams:
            rates = [r for p in mediamtx.camera_paths(c) if p in self.paths
                     for r in [self.paths[p].bitrate_mbps(BANDWIDTH_SPAN_S)] if r is not None]
            per[c["id"]] = round(sum(rates), 2) if rates else None
        days = self._days()
        today = dt.datetime.fromtimestamp(now or time.time()).strftime("%Y-%m-%d")
        return {"mbps": round(sum(v or 0 for v in per.values()), 2),
                "today_gb": round(days.get(today, 0.0) / 1e9, 2),
                "month_gb": round(sum(v for k, v in days.items() if k[:7] == today[:7]) / 1e9, 1),
                "cameras": per}

    # ---- site link
    def link_down(self, cams: list[dict] | None = None, now: float | None = None) -> dict | None:
        """The `site_link_down` health alert while every enabled camera (at least two) has sent no bytes for
        LINK_DOWN_S, else None. A camera never heard from counts from the first metrics sample. None while
        MediaMTX's metrics can't be read (that is this server's problem, not the link's)."""
        cams = db.cameras(enabled_only=True) if cams is None else cams
        now = now or time.time()
        if len(cams) < LINK_DOWN_MIN_CAMERAS or not self.first_sample or self.error or now - self.last_sample > 3 * SAMPLE_S:
            return None
        last_bytes = []
        for c in cams:
            p = self.paths.get(c["id"])
            last_bytes.append(p.last_increase if p and p.last_increase else self.first_sample)
        if all(now - t > LINK_DOWN_S for t in last_bytes):
            return {"kind": "site_link_down", "text": LINK_DOWN_TEXT, "since": max(last_bytes), "cameras": len(cams)}
        return None

    async def run(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                await self.sample()
                self.error = None
            except Exception as e:  # noqa: BLE001 - MediaMTX restarting etc.
                self.error = str(e)
            await asyncio.sleep(SAMPLE_S)

    def camera(self, camera_id: str) -> dict:
        """Health of one camera's streams, plus plain-English problems (something is wrong: the hub alerts on these)
        and warnings (it works, but could be set up better: shown in Settings, never an alert)."""
        now = time.time()
        main, sub = self.paths.get(camera_id), self.paths.get(f"{camera_id}_sub")
        out: dict = {"sampled": bool(self.last_sample), "bitrate_mbps": None, "sub_bitrate_mbps": None, "gb_per_day": None,
                     "stalled_s": None, "frames_in_error_1h": 0, "metadata_reader": None, "problems": [],
                     # stream-setup notes ("no low-resolution stream: SD plays the main stream") are warnings: the camera
                     # works, and a problem would open a camera_down alert on the hub
                     "warnings": stream_problems(camera_id)}
        if not main:
            return out
        out["bitrate_mbps"] = main.bitrate_mbps()
        out["sub_bitrate_mbps"] = sub.bitrate_mbps() if sub else None
        out["gb_per_day"] = main.gb_per_day()
        stalled = now - main.last_increase if main.last_increase else None
        out["stalled_s"] = round(stalled) if stalled is not None else None
        out["frames_in_error_1h"] = main.errors_last_hour(now)
        # record_stream "sub" or no sub stream: while someone watches SD live, <id>_sub relays this path as one more RTSP reader
        relay = int(camera_id in self.sub_recorded and sub is not None and sub.state == "ready")
        out["metadata_reader"] = main.readers.get("rtspSession", 0) >= 1 + relay
        if stalled is not None and stalled >= STALL_S and main.state == "ready":
            out["problems"].append(f"no video for {int(stalled)} s (stream still open)")
        if out["metadata_reader"] is False and len(main.samples) >= 3 and not self._event_only(camera_id):
            out["problems"].append("metadata reader not attached: detections are being missed")
        down = self._events_down(camera_id, now)
        if down is not None:
            out["problems"].append(f"ONVIF event subscription down for {down // 60} min: detections are being missed")
        if out["frames_in_error_1h"]:
            out["problems"].append(f"{out['frames_in_error_1h']} corrupt frames in the last hour (packet loss?)")
        return out

    def _event_only(self, camera_id: str) -> bool:
        try:
            return bool(self.event_only(camera_id))
        except Exception:  # noqa: BLE001 - never let the lookup hide the camera's health
            return False

    def _events_down(self, camera_id: str, now: float) -> int | None:
        """Seconds a camera that relies on its ONVIF events (no metadata reader) has had no working event subscription,
        once that is EVENTS_DOWN_S or more, else None. Its metadata problem is suppressed, so this is what shows."""
        if not self._event_only(camera_id):
            return None
        try:
            since = self.events_down_since(camera_id)
        except Exception:  # noqa: BLE001
            return None
        return int(now - since) if since and now - since >= EVENTS_DOWN_S else None

    def all(self) -> dict[str, dict]:
        return {c["id"]: self.camera(c["id"]) for c in db.cameras(enabled_only=True)}
