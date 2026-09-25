"""Stream health from MediaMTX's Prometheus metrics, sampled every SAMPLE_S.

Per camera: live bitrate, an estimate of recording GB/day, how long since the main stream last delivered
any bytes (a frozen camera can stay "ready" in MediaMTX for a while), corrupt frames from the camera in the
last hour (packet loss: bad cable / Wi-Fi / switch), and whether the NVR's own metadata reader is attached
to the main stream (if not, recording continues but detections are silently missed).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque

import httpx

from .db import db

log = logging.getLogger("nvr.health")

SAMPLE_S = 10
WINDOW = 360                 # samples kept per path (1 h) for the GB/day estimate
STALL_S = 30                 # no bytes for this long = "no video"
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

    def update(self, t: float, m: dict) -> None:
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
        self.error: str | None = None

    async def sample(self) -> None:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(self.url)
            r.raise_for_status()
        now = time.time()
        for path, m in parse_metrics(r.text).items():
            self.paths.setdefault(path, PathStats()).update(now, m)
        self.last_sample = now

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
        """Health of one camera's streams, plus plain-English problems."""
        now = time.time()
        main, sub = self.paths.get(camera_id), self.paths.get(f"{camera_id}_sub")
        out: dict = {"sampled": bool(self.last_sample), "bitrate_mbps": None, "sub_bitrate_mbps": None, "gb_per_day": None,
                     "stalled_s": None, "frames_in_error_1h": 0, "metadata_reader": None, "problems": []}
        if not main:
            return out
        out["bitrate_mbps"] = main.bitrate_mbps()
        out["sub_bitrate_mbps"] = sub.bitrate_mbps() if sub else None
        out["gb_per_day"] = main.gb_per_day()
        stalled = now - main.last_increase if main.last_increase else None
        out["stalled_s"] = round(stalled) if stalled is not None else None
        out["frames_in_error_1h"] = main.errors_last_hour(now)
        out["metadata_reader"] = main.readers.get("rtspSession", 0) >= 1
        if stalled is not None and stalled >= STALL_S and main.state == "ready":
            out["problems"].append(f"no video for {int(stalled)} s (stream still open)")
        if out["metadata_reader"] is False and len(main.samples) >= 3:
            out["problems"].append("metadata reader not attached: detections are being missed")
        if out["frames_in_error_1h"]:
            out["problems"].append(f"{out['frames_in_error_1h']} corrupt frames in the last hour (packet loss?)")
        return out

    def all(self) -> dict[str, dict]:
        return {c["id"]: self.camera(c["id"]) for c in db.cameras(enabled_only=True)}
