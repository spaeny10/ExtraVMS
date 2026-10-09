"""MediaMTX: config generation, process supervision, playback API client."""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import logging
import os
import re
import secrets
import threading
import urllib.parse

import httpx
import yaml

from .config import settings
from .db import db

log = logging.getLogger("nvr.mediamtx")


HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,252}$")   # IP address or hostname; anything else breaks the RTSP URL
CAMERA_ID_RE = re.compile(r"^[a-z0-9_]{1,32}$")
# An RTSP path on the camera, appended after host:port in camera_url. Must start with one "/" and may not hold
# "@" (rtsp://u:p@host:554@evil:554/x sends the password elsewhere), whitespace or "#". Covers the vendor forms
# seen so far: /main, /Streaming/Channels/101, /cam/realmonitor?channel=1&subtype=0, /h264Preview_01_main,
# /media/video1;stream=1, /axis-media/media.amp?videocodec=h264&resolution=1920x1080, /live/ch00_0, /11.
PATH_RE = re.compile(r"^/(?!/)[A-Za-z0-9_.~/\-?=&%;:,+]{0,254}$")


def valid_host(host: str | None) -> bool:
    return bool(host) and HOST_RE.fullmatch(host) is not None


def camera_problem(cam: dict, partial: bool = False) -> str | None:
    """Why this camera row may not be stored (it would produce a dangerous or broken RTSP URL), or None.
    `partial`: fields that are absent are not checked (an import or merge may leave paths to the defaults)."""
    cid = cam.get("id")
    if not isinstance(cid, str) or not CAMERA_ID_RE.fullmatch(cid):
        return f"camera id {str(cid)[:40]!r} must be 1-32 lowercase letters, digits or _"
    if not (partial and "host" not in cam) and not (isinstance(cam.get("host"), str) and valid_host(cam["host"])):
        return f"camera {cid}: address {str(cam.get('host'))[:60]!r} is not an IP address or hostname"
    for k in ("main_path", "sub_path"):
        if partial and k not in cam:
            continue
        v = cam.get(k)
        if not isinstance(v, str) or not PATH_RE.fullmatch(v):
            return (f"camera {cid}: {k} {str(v)[:60]!r} must start with a single / and use only letters, digits and "
                    "_ . ~ / - ? = & % ; : , + (no @, spaces or #)")
    for k in ("rtsp_port", "onvif_port"):
        if partial and k not in cam:
            continue
        v = cam.get(k)
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 65535:
            return f"camera {cid}: {k} must be a port number 1-65535"
    # port-forward mode (optional; empty = connect to host / the ports above): the same rules
    ph = cam.get("public_host")
    if ph not in (None, "") and not (isinstance(ph, str) and valid_host(ph)):
        return f"camera {cid}: outside address {str(ph)[:60]!r} is not an IP address or hostname"
    for k in ("public_rtsp_port", "public_onvif_port", "public_replay_port"):
        v = cam.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 65535):
            return f"camera {cid}: {k} must be a port number 1-65535 (or empty)"
    if cam.get("record_stream") not in (None, "main", "sub"):
        return f"camera {cid}: record_stream must be main or sub"
    return None


# ---- the NVR's own RTSP / WHEP reader (settings.rtsp_auth)

def reader_credentials() -> tuple[str, str] | None:
    """User and password the NVR's own readers use against MediaMTX (ingest's RTSP metadata session, the WHEP
    proxy). Generated on first use and kept in the settings table, not .env. None when rtsp_auth is off."""
    if not settings.rtsp_auth:
        return None
    # One caller at a time: at startup the config writer, every camera's metadata reader and the WHEP proxy ask at
    # once, and a check-then-create race left Qwenbot's mediamtx.yml with one password and the database with another
    # (2026-10-08: every metadata session got 401 for 4.5 h, no detections).
    with _reader_lock:
        c = db.get_setting("mediamtx_reader")
        if not (isinstance(c, dict) and c.get("user") and c.get("pass")):
            c = {"user": "nvr", "pass": secrets.token_urlsafe(24)}
            db.set_setting("mediamtx_reader", c)
            log.warning("generated a new MediaMTX reader password (none was stored)")
            c = db.get_setting("mediamtx_reader") or c
        return c["user"], c["pass"]


_reader_lock = threading.Lock()


def reader_auth_header() -> dict[str, str]:
    cred = reader_credentials()
    if not cred:
        return {}
    return {"Authorization": "Basic " + base64.b64encode(f"{cred[0]}:{cred[1]}".encode()).decode()}


LOCAL_IPS = ["127.0.0.1", "::1"]
PRIVATE_IPS = ["127.0.0.1", "::1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"]


def auth_users() -> list[dict]:
    cred = reader_credentials()
    if cred is None:   # NVR_RTSP_AUTH=0: as before, anyone on this machine or a private network may view
        return [
            {"user": "any", "pass": "", "ips": PRIVATE_IPS, "permissions": [{"action": "read"}, {"action": "playback"}]},
            {"user": "any", "pass": "", "ips": LOCAL_IPS,
             "permissions": [{"action": "publish"}, {"action": "api"}, {"action": "metrics"}]},
        ]
    return [
        # Viewing (RTSP, WebRTC) needs the NVR's generated reader password, from this machine or the local network:
        # a LAN device can no longer pull the cameras through MediaMTX anonymously.
        {"user": cred[0], "pass": cred[1], "ips": PRIVATE_IPS, "permissions": [{"action": "read"}, {"action": "playback"}]},
        # This machine only, no password: publishing, the API and metrics (127.0.0.1-bound anyway) and playback
        # (its server listens on 127.0.0.1 only; the NVR proxies it as /api/playback).
        {"user": "any", "pass": "", "ips": LOCAL_IPS,
         "permissions": [{"action": "publish"}, {"action": "api"}, {"action": "metrics"}, {"action": "playback"}]},
    ]


def atomic_write(path, text: str) -> None:
    """Replace `path` with `text` in one step (temp file in the same directory + os.replace), so a reader watching the
    file (MediaMTX's config hot reload) never sees it empty or half written."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:   # Windows: the reader has the file open this instant; it closes it right away
            if attempt == 9:
                tmp.unlink(missing_ok=True)
                raise
            import time
            time.sleep(0.05)


def camera_url(cam: dict, path: str) -> str:
    """The camera's RTSP URL: its outside address and RTSP port in port-forward mode (public_host /
    public_rtsp_port), else host / rtsp_port."""
    from .onvif_soap import outside
    user = urllib.parse.quote(cam["username"], safe="")
    pw = urllib.parse.quote(cam["password"], safe="")
    host, _, port = outside(cam)
    return f"rtsp://{user}:{pw}@{host}:{port}{path}"


def records_sub(cam: dict) -> bool:
    """The camera records its sub stream continuously (record_stream "sub": saves cellular data)."""
    return cam.get("record_stream") == "sub"


def local_url(path: str) -> str:
    """This MediaMTX's own RTSP URL for `path`, with the NVR's reader password when rtsp_auth is on."""
    base = settings.mediamtx_rtsp.rstrip("/")
    cred = reader_credentials()
    if cred:
        scheme, rest = base.split("://", 1)
        base = f"{scheme}://{urllib.parse.quote(cred[0], safe='')}:{urllib.parse.quote(cred[1], safe='')}@{rest}"
    return f"{base}/{path}"


def sub_relays_main(cam: dict) -> bool:
    """<id>_sub relays <id> from this MediaMTX instead of pulling from the camera: record_stream "sub" (<id> is the
    sub stream), or a camera without a usable low-resolution stream (streams.plan)."""
    from . import streams
    return records_sub(cam) or streams.sub_source(cam) is None


def camera_paths(cam: dict) -> list[str]:
    """MediaMTX paths whose bytes come from the camera itself (bandwidth from the site): the recorded path
    and the on-demand one pulling the other stream. A local relay (<id>_sub in sub mode or on a camera without a
    sub stream, <id>_hd on a sub-mode camera without one) is not counted."""
    from . import streams
    if records_sub(cam):
        return [cam["id"], f"{cam['id']}_hd"] if streams.sub_source(cam) else [cam["id"]]
    return [cam["id"]] if streams.sub_source(cam) is None else [cam["id"], f"{cam['id']}_sub"]


def build_config(cameras: list[dict]) -> dict:
    from . import streams
    rec_root = settings.recordings_dir.as_posix()
    paths: dict = {}
    for cam in cameras:
        problem = camera_problem(cam, partial=True)
        if problem:
            # MediaMTX refuses to start on one malformed source URL, taking every camera down with it, and a crafted
            # path could send the password elsewhere: leave this camera out (it shows as down in Settings) and keep
            # the others recording.
            log.error("[%s] %s: camera left out of MediaMTX", cam.get("id"), problem)
            continue
        on_demand = {"rtspTransport": "tcp", "sourceOnDemand": True, "sourceOnDemandCloseAfter": "30s"}
        # The camera path SD live view pulls: sub_path, a lower-resolution profile the camera lists instead of it, or
        # None when it has none (secondary stream switched off, or RTSP 404 seen): then <id>_sub relays <id>, so SD
        # always plays (streams.plan; Qwenbot's South PTZ Dome 2026-10-08).
        sub = streams.sub_source(cam)
        if sub is not None and not PATH_RE.fullmatch(sub):
            sub = None   # never put a path the camera reported into an RTSP URL unchecked
        if records_sub(cam) and sub is None:
            # record_stream "sub" on a camera with no sub stream: record the only stream there is (recording nothing
            # would be worse than the extra data); SD and HD both relay it locally
            paths[cam["id"]] = {"source": camera_url(cam, cam["main_path"]), "rtspTransport": "tcp", "record": True}
            paths[f"{cam['id']}_sub"] = {**on_demand, "source": local_url(cam["id"])}
            paths[f"{cam['id']}_hd"] = {**on_demand, "source": local_url(cam["id"])}
            continue
        if records_sub(cam):
            # record_stream "sub" (cellular sites): the path named after the camera pulls and records the SUB stream
            # 24/7, so everything keyed by the camera id keeps working unchanged on the smaller picture: playback,
            # Timeline, event clips (pipeline fetch_clip), frames, verification and the ONVIF metadata reader
            # (the camera must send metadata on its sub-stream profile). <id>_sub (SD live view) relays that same
            # stream from this MediaMTX, so watching costs no extra upload; <id>_hd pulls the main stream from the
            # camera only while someone watches in HD.
            paths[cam["id"]] = {"source": camera_url(cam, sub), "rtspTransport": "tcp", "record": True}
            paths[f"{cam['id']}_sub"] = {**on_demand, "source": local_url(cam["id"])}
            paths[f"{cam['id']}_hd"] = {**on_demand, "source": camera_url(cam, cam["main_path"])}
            continue
        # Main stream: always pulled and recorded 24/7. The NVR also reads the ONVIF
        # metadata track from this path, so the camera only serves one main session.
        paths[cam["id"]] = {
            "source": camera_url(cam, cam["main_path"]),
            "rtspTransport": "tcp",
            "record": True,
        }
        # Sub stream: H.264, used for browser live view; pulled only while watched. Without one, a relay of the
        # main stream from this MediaMTX (already pulled 24/7: no second session on the camera).
        paths[f"{cam['id']}_sub"] = {**on_demand, "source": camera_url(cam, sub) if sub is not None else local_url(cam["id"])}
    return {
        "logLevel": "info",
        "logDestinations": ["stdout", "file"],
        "logFile": (settings.runtime_dir / "mediamtx.log").as_posix(),
        "authMethod": "internal",
        # Only this machine may publish or use the API. Viewing needs this machine or the local network (so a
        # forwarded MediaMTX port never exposes the cameras to the internet; remote viewing goes through the NVR)
        # and, with rtsp_auth, the NVR's generated reader password.
        "authInternalUsers": auth_users(),
        "api": True, "apiAddress": "127.0.0.1:9997",
        "metrics": True, "metricsAddress": "127.0.0.1:9998",
        "playback": True, "playbackAddress": "127.0.0.1:9996",  # the NVR proxies playback (/api/playback)
        "rtsp": True, "rtspTransports": ["tcp"], "rtspAddress": ":8554",
        "rtmp": False, "srt": False, "moq": False,
        "hls": False,  # unused: live view is WebRTC, recordings are fMP4 through the NVR
        "webrtc": True, "webrtcAddress": f"127.0.0.1:{settings.mediamtx_webrtc_port}",  # signaling via /api/whep
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

    def write_config(self, cameras: list[dict]) -> bool:
        """Write mediamtx.yml if it differs from what the cameras and settings call for. True when it was written."""
        settings.runtime_dir.mkdir(parents=True, exist_ok=True)
        settings.recordings_dir.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(build_config(cameras), sort_keys=False)
        if not self.config_path.exists() or self.config_path.read_text() != text:
            # MediaMTX hot-reloads on change. Write a temp file and swap it in: an in-place write_text truncates first,
            # and a reload that caught the empty file ran MediaMTX on its defaults (no API, no recording, no camera
            # paths) until the next restart (Qwenbot, 2026-10-06, ~6 min of recording lost).
            atomic_write(self.config_path, text)
            log.info("wrote %s (%d cameras)", self.config_path, len(cameras))
            return True
        return False

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
