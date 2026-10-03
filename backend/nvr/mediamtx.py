"""MediaMTX: config generation, process supervision, playback API client."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import urllib.parse

import httpx
import yaml

from .config import settings
from .db import db

log = logging.getLogger("nvr.mediamtx")


HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,252}$")   # IP address or hostname; anything else breaks the RTSP URL


def valid_host(host: str | None) -> bool:
    return bool(host) and HOST_RE.match(host) is not None


def camera_url(cam: dict, path: str) -> str:
    user = urllib.parse.quote(cam["username"], safe="")
    pw = urllib.parse.quote(cam["password"], safe="")
    return f"rtsp://{user}:{pw}@{cam['host']}:{cam['rtsp_port']}{path}"


def build_config(cameras: list[dict]) -> dict:
    rec_root = settings.recordings_dir.as_posix()
    paths: dict = {}
    for cam in cameras:
        if not valid_host(cam.get("host")):
            # MediaMTX refuses to start on one malformed source URL, taking every camera down with it: leave this
            # camera out (it shows as down in Settings) and keep the others recording.
            log.error("[%s] address %r is not an IP address or hostname: camera left out of MediaMTX", cam["id"], cam.get("host"))
            continue
        # Main stream: always pulled and recorded 24/7. The NVR also reads the ONVIF
        # metadata track from this path, so the camera only serves one main session.
        paths[cam["id"]] = {
            "source": camera_url(cam, cam["main_path"]),
            "rtspTransport": "tcp",
            "record": True,
        }
        # Sub stream: H.264, used for browser live view; pulled only while watched.
        paths[f"{cam['id']}_sub"] = {
            "source": camera_url(cam, cam["sub_path"]),
            "rtspTransport": "tcp",
            "sourceOnDemand": True,
            "sourceOnDemandCloseAfter": "30s",
        }
    return {
        "logLevel": "info",
        "logDestinations": ["stdout", "file"],
        "logFile": (settings.runtime_dir / "mediamtx.log").as_posix(),
        "authMethod": "internal",
        "authInternalUsers": [
            # Only this machine may publish or use the API. Viewing needs this machine or the local network, so a
            # forwarded MediaMTX port never exposes the cameras to the internet (remote viewing goes through the NVR).
            {"user": "any", "pass": "", "ips": ["127.0.0.1", "::1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"],
             "permissions": [{"action": "read"}, {"action": "playback"}]},
            {"user": "any", "pass": "", "ips": ["127.0.0.1", "::1"],
             "permissions": [{"action": "publish"}, {"action": "api"}, {"action": "metrics"}]},
        ],
        "api": True, "apiAddress": "127.0.0.1:9997",
        "metrics": True, "metricsAddress": "127.0.0.1:9998",
        "playback": True, "playbackAddress": "127.0.0.1:9996",  # the NVR proxies playback (/api/playback)
        "rtsp": True, "rtspTransports": ["tcp"], "rtspAddress": ":8554",
        "rtmp": False, "srt": False, "moq": False,
        "hls": False,  # unused: live view is WebRTC, recordings are fMP4 through the NVR
        "webrtc": True, "webrtcAddress": f"127.0.0.1:{settings.mediamtx_webrtc_port}",  # signalling via /api/whep
        # The video itself: fixed ports so a router can forward them for remote live view. The public address is
        # added to each answer by the NVR (api.whep) from the hostname the browser used.
        "webrtcLocalUDPAddress": f":{settings.webrtc_media_port}",
        "webrtcLocalTCPAddress": f":{settings.webrtc_media_port}",
        "webrtcIPsFromInterfaces": True,
        # Through the fleet hub the browser is somewhere on the internet: relay via the hub's TURN as well
        **({"webrtcICEServers2": [{"url": u, "username": turn["username"], "password": turn["credential"]} for u in turn["urls"]]}
           if (turn := db.get_setting("hub_turn")) and turn.get("urls") else {}),
        "pathDefaults": {
            "recordPath": f"{rec_root}/%path/%Y-%m-%d_%H-%M-%S-%f",
            "recordFormat": "fmp4",
            "recordPartDuration": "1s",
            "recordSegmentDuration": settings.segment_duration,
            "recordDeleteAfter": "0s",  # retention is handled by nvr.retention (days + disk quota)
        },
        "paths": paths,
    }


class MediaMTX:
    def __init__(self) -> None:
        self.config_path = settings.runtime_dir / "mediamtx.yml"
        self.proc: asyncio.subprocess.Process | None = None
        self._stop = asyncio.Event()

    def write_config(self, cameras: list[dict]) -> None:
        settings.runtime_dir.mkdir(parents=True, exist_ok=True)
        settings.recordings_dir.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(build_config(cameras), sort_keys=False)
        if not self.config_path.exists() or self.config_path.read_text() != text:
            self.config_path.write_text(text)  # MediaMTX hot-reloads on change
            log.info("wrote %s (%d cameras)", self.config_path, len(cameras))

    async def run(self) -> None:
        """Keep MediaMTX running; restart with backoff if it exits."""
        backoff = 1
        while not self._stop.is_set():
            log.info("starting MediaMTX")
            self.proc = await asyncio.create_subprocess_exec(
                str(settings.mediamtx_exe), str(self.config_path),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            code = await self.proc.wait()
            if self._stop.is_set():
                break
            log.warning("MediaMTX exited with %s; restarting in %ss", code, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def stop(self) -> None:
        self._stop.set()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()


def rfc3339(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace("+00:00", "Z")


async def path_status() -> dict[str, dict]:
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.get(f"{settings.mediamtx_api}/v3/paths/list")
        r.raise_for_status()
        return {p["name"]: p for p in r.json().get("items", [])}


async def recording_spans(path: str, start: float | None = None, end: float | None = None) -> list[dict]:
    params = {"path": path}
    if start:
        params["start"] = rfc3339(start)
    if end:
        params["end"] = rfc3339(end)
    # Listing walks the segment files on disk; a week on a busy spinning disk can take well over 10 s.
    async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=5)) as c:
        r = await c.get(f"{settings.mediamtx_playback}/list", params=params)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return r.json()


async def fetch_clip(path: str, start: float, duration: float, dest) -> None:
    """Download [start, start+duration] from the recordings as a standard MP4."""
    params = {"path": path, "start": rfc3339(start), "duration": f"{duration:.3f}", "format": "mp4"}
    async with httpx.AsyncClient(timeout=60) as c:
        async with c.stream("GET", f"{settings.mediamtx_playback}/get", params=params) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in r.aiter_bytes():
                    f.write(chunk)
