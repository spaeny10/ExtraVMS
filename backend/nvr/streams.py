"""Which streams a camera really serves, and what SD live view plays.

Every camera has a main_path and a sub_path (default "/main" and "/sub", typical Milesight). MediaMTX records
<id> from the main path and pulls <id>_sub from the sub path on demand for SD live view. A camera whose secondary
stream is switched off answers "/sub" with RTSP 404, so SD never connects (Qwenbot 2026-10-08: the South PTZ Dome,
a Milesight MS-C8164-SPD, lists one ONVIF media profile, Profile_1 H.264 3840x2160 at /main).

`probe_streams` asks the camera (read-only ONVIF: Media GetProfiles + GetStreamUri, Media2 when there is no
Media service) for its profiles. `plan` decides what <id>_sub pulls:

* the configured sub_path when the camera lists it (or nothing is known yet: as before);
* otherwise the best lower-resolution profile the camera lists (H.264 first, then the largest), as a *detected*
  path. The user's sub_path field is never rewritten: the detection is stored with the probe and shown in
  Settings, and a later probe (the camera's sub stream re-enabled, the field corrected) takes it back;
* no lower-resolution profile, or MediaMTX saw RTSP 404 on the path it would use: <id>_sub relays <id> (the
  recorded main stream, already pulled 24/7) from this MediaMTX, so SD always plays, and the camera gets the
  NO_SUB problem.

The probe runs when a camera is added or its address or credentials change (api.put_camera), on Settings'
"Check" (POST /api/cameras/{id}/streams/check), and for cameras never probed (StreamChecker, at startup and when
an import adds one). StreamChecker also tails MediaMTX's log for "[path <id>_sub] [RTSP source] bad status code:
404" so a camera that drops its sub stream falls back without waiting for a probe.

Stored on the camera row: `streams` (JSON {profiles, media, metadata_analytics, error, sub_not_found: {path, at}}) and
`streams_checked_at`. metadata_analytics is the profiles' metadata configuration Analytics flag (False on cameras
whose metadata carries no objects, e.g. the Reolink RP-PCT8MD: their detections come from ONVIF events, ruleevents.py).

When the configured main path is not one the camera lists, view() offers the paths it does list (`suggest`: the
largest profile as main, the plan's sub) for a one-click fix in Settings; nothing is ever changed automatically.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.parse

from . import onvif_soap as soap
from .db import db

log = logging.getLogger("nvr.streams")

CALL_TIMEOUT_S = 4          # per ONVIF call
PROBE_TIMEOUT_S = 10        # the whole probe, as awaited by a request
RETRY_FAILED_S = 3600       # a probe that failed (camera offline) is retried after this long
WATCH_S = 10                # MediaMTX log tail interval
LOG_TAIL_BYTES = 256 * 1024

# what changes which camera answers, or how: the stored profiles no longer apply
ADDRESS_KEYS = ("host", "onvif_port", "rtsp_port", "username", "password", "public_host", "public_rtsp_port", "public_onvif_port")

ENCODINGS = {"H264": "H.264", "H265": "H.265", "HEVC": "H.265", "JPEG": "MJPEG", "MJPEG": "MJPEG", "MPEG4": "MPEG-4"}
# query parameters some cameras put credentials in (never stored, never shown)
SECRET_PARAM = re.compile(r"^(user(name)?|pass(word|wd)?|pwd|auth|token|key)$", re.I)
# MediaMTX: "2026/10/08 19:58:14 ERR [path cam4_sub] [RTSP source] bad status code: 404 (Not Found)"
SUB_404 = re.compile(r"\[path ([a-z0-9_]+)_sub\] \[RTSPS? source\][^\n]*\b(404|453)\b")
# 453 "Not Enough Bandwidth": the camera has no stream connection left for us (Qwenbot's SW Corner PTZ 2026-10-08:
# something else held its slots). Any extra sub-stream session would be refused too: SD relays the main stream we
# already pull, whatever the profiles say.


def busy_text() -> str:
    return ("The camera refused its low-resolution stream (453 Not Enough Bandwidth: no connection left on the "
            "camera): SD plays the main stream. Check what else is connected to the camera, then press Check.")


def no_sub_text(width: int | None = None, height: int | None = None) -> str:
    size = f" ({width}×{height})" if width and height else ""
    return (f"No low-resolution stream: SD plays the main stream{size}. "
            "Enable the camera's secondary stream for faster live view.")


# --------------------------------------------------------------------------- parsing ONVIF replies

def uri_path(uri: str | None) -> str | None:
    """An RTSP URI the camera reported -> its path and query: host, port and any user:password@ dropped, query
    parameters that carry credentials removed. None when there is no path."""
    if not uri:
        return None
    try:
        parts = urllib.parse.urlsplit(uri.strip())
    except ValueError:
        return None
    path = parts.path or ""
    if not path.startswith("/"):
        return None
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in query if not SECRET_PARAM.match(k)]
    q = urllib.parse.urlencode(kept, safe="/,:;") if kept else ""
    return path + (f"?{q}" if q else "")


def _encoding(raw: str | None) -> str | None:
    if not raw:
        return None
    return ENCODINGS.get(raw.strip().upper().replace(".", "").replace("-", ""), raw.strip())


def _int(s: str | None) -> int | None:
    try:
        return int(float(s)) if s not in (None, "") else None
    except ValueError:
        return None


def parse_profiles(body) -> list[dict]:
    """GetProfiles reply (Media or Media2) -> [{token, name, encoding, width, height, fps}] for profiles with a video
    encoder, in the camera's order. Media: Profiles/VideoEncoderConfiguration; Media2: Profiles/Configurations/
    VideoEncoder (Encoding there may also be an attribute)."""
    out = []
    for prof in soap.find_all(body, "Profiles"):
        enc = soap.find(prof, "VideoEncoderConfiguration")   # (an Element without children is falsy: no `or`)
        if enc is None:
            enc = soap.find(prof, "VideoEncoder")
        if enc is None:
            continue   # an audio- or metadata-only profile
        res = soap.find(enc, "Resolution")
        fps = soap.text(soap.find(enc, "RateControl"), "FrameRateLimit")
        name = soap.children(prof, "Name")
        out.append({
            "token": prof.get("token") or "",
            "name": (name[0].text or "").strip() if name else "",
            "encoding": _encoding(soap.text(enc, "Encoding") or enc.get("Encoding")),
            "width": _int(soap.text(res, "Width")),
            "height": _int(soap.text(res, "Height")),
            "fps": _int(fps),
        })
    return out


def parse_metadata_analytics(body) -> bool | None:
    """GetProfiles reply -> the metadata configurations' Analytics flag: True if any says true, False if they all say
    false, None when no profile has one (Media: MetadataConfiguration/Analytics; Media2: Configurations/Metadata/Analytics)."""
    values = [(e.text or "").strip().lower() for e in body.iter() if soap.local(e) == "Analytics"]
    values = [v for v in values if v in ("true", "false", "1", "0")]
    if not values:
        return None
    return any(v in ("true", "1") for v in values)


def parse_stream_uri(body) -> str | None:
    return soap.text(body, "Uri")


def _area(p: dict) -> int:
    return (p.get("width") or 0) * (p.get("height") or 0)


def sort_profiles(profiles: list[dict]) -> list[dict]:
    """Largest resolution first (the main stream); ties keep the camera's order."""
    return sorted(profiles, key=lambda p: -_area(p))


# --------------------------------------------------------------------------- the probe (read-only ONVIF)

GET_URI_MEDIA1 = ("<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol>"
                  "</tt:Transport></trt:StreamSetup><trt:ProfileToken>{token}</trt:ProfileToken></trt:GetStreamUri>")
GET_URI_MEDIA2 = "<tr2:GetStreamUri><tr2:Protocol>RTSP</tr2:Protocol><tr2:ProfileToken>{token}</tr2:ProfileToken></tr2:GetStreamUri>"


def _profiles_from(client: soap.Onvif, url: str, get_profiles: str, get_uri: str, flags: dict | None = None) -> list[dict]:
    from xml.sax.saxutils import escape
    body = client.call(url, get_profiles)
    if flags is not None:
        flags["metadata_analytics"] = parse_metadata_analytics(body)
    profiles = parse_profiles(body)
    out = []
    for p in profiles:
        try:
            uri = parse_stream_uri(client.call(url, get_uri.format(token=escape(p["token"]))))
        except soap.OnvifError as e:
            log.info("GetStreamUri %s: %s", p["token"], e)
            continue
        path = uri_path(uri)
        if path:
            out.append({**p, "path": path})
    return out


def probe_streams(cam: dict, timeout: float = CALL_TIMEOUT_S) -> dict:
    """{profiles: [{token, name, encoding, width, height, fps, path}] largest first, media: "media" | "media2"}.
    Raises OnvifError when the camera can't be asked. Blocking: run it in a worker thread."""
    client = soap.Onvif.for_camera(cam, timeout=timeout)
    try:
        soap.sync_clock(client)
    except soap.OnvifError:
        pass   # some cameras refuse it unauthenticated; the digest may still be accepted
    soap.discover_services(client)
    media, media2 = client.service("media"), client.service("media2")
    if not media and not media2:
        raise soap.OnvifError("the camera reports no ONVIF media service")
    profiles: list[dict] = []
    used = None
    flags: dict = {}
    if media:
        try:
            profiles = _profiles_from(client, media, "<trt:GetProfiles/>", GET_URI_MEDIA1, flags)
            used = "media"
        except soap.OnvifError:
            if not media2:
                raise
    if not profiles and media2:
        profiles = _profiles_from(client, media2, "<tr2:GetProfiles><tr2:Type>All</tr2:Type></tr2:GetProfiles>", GET_URI_MEDIA2, flags)
        used = "media2"
    return {"profiles": sort_profiles(profiles), "media": used, "metadata_analytics": flags.get("metadata_analytics")}


# --------------------------------------------------------------------------- the decision

def state_of(cam: dict) -> dict:
    """The camera's stored probe result ({} when never probed)."""
    raw = cam.get("streams")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    return raw if isinstance(raw, dict) else {}


def same_path(configured: str | None, listed: str | None) -> bool:
    """A configured stream path names the stream the camera lists: equal paths (a trailing / aside), and every query
    parameter configured is in the listed URI (cameras append their own, e.g. ?transportmode=unicast&profile=…)."""
    if not configured or not listed:
        return False
    a, b = urllib.parse.urlsplit(configured), urllib.parse.urlsplit(listed)
    if (a.path.rstrip("/") or "/") != (b.path.rstrip("/") or "/"):
        return False
    have = set(urllib.parse.parse_qsl(b.query, keep_blank_values=True))
    return all(kv in have for kv in urllib.parse.parse_qsl(a.query, keep_blank_values=True))


def _safe_path(path: str | None) -> bool:
    from .mediamtx import PATH_RE
    return bool(path) and PATH_RE.fullmatch(path) is not None


def plan(cam: dict) -> dict:
    """What SD live view (<id>_sub) pulls for this camera, and the stream problems to show:
    {sub_path: camera path for <id>_sub, or None = relay <id> (the main stream) from this MediaMTX,
     detected: sub_path came from the camera's profiles, not the sub_path field,
     main: the main profile (or None), sub: the profile SD plays (or None), problems: [str]}."""
    st = state_of(cam)
    profiles = [p for p in st.get("profiles") or [] if isinstance(p, dict)]
    main_path, sub_path = cam.get("main_path") or "/main", cam.get("sub_path") or "/sub"
    missing = (st.get("sub_not_found") or {}).get("path")
    busy = (st.get("sub_not_found") or {}).get("code") == "453"
    out: dict = {"sub_path": sub_path, "detected": False, "main": None, "sub": None, "problems": [], "suggest": None}
    if busy:   # no connection left on the camera: relay the main stream, whatever it offers
        out.update(sub_path=None)
        out["problems"].append(busy_text())
        return out
    if not profiles:   # never probed, or the camera wouldn't say: as configured, unless MediaMTX saw a 404 there
        if missing and missing == sub_path:
            out.update(sub_path=None)
            out["problems"].append(no_sub_text())
        return out
    main = next((p for p in profiles if same_path(main_path, p.get("path"))), None)
    if main is None:
        listed = ", ".join(p["path"] for p in profiles if p.get("path"))
        out["problems"].append(f"The camera does not list the main stream path {main_path} (it serves {listed}): "
                               "check the stream paths in Settings → Cameras.")
    ref = main or profiles[0]
    out["main"] = ref

    def refused(path: str | None) -> bool:   # MediaMTX got 404 there: whatever the profiles say, don't pull it
        return bool(missing and path and (path == missing or same_path(path, missing)))

    configured = None if refused(sub_path) else next((p for p in profiles if same_path(sub_path, p.get("path"))), None)
    if configured is not None:
        chosen, profile = sub_path, configured
    else:
        lower = [p for p in profiles if _area(p) < _area(ref) and _safe_path(p.get("path")) and not refused(p.get("path"))]
        lower.sort(key=lambda p: (p.get("encoding") != "H.264", -_area(p)))   # browsers play H.264 everywhere
        profile = lower[0] if lower else None
        chosen = profile["path"] if profile else None
        out["detected"] = profile is not None
    out["sub_path"], out["sub"] = chosen, profile
    if chosen is None:
        out["problems"].append(no_sub_text(ref.get("width"), ref.get("height")))
    if main is None and _safe_path(ref.get("path")):
        # the configured main path is not one the camera serves: offer what it lists (the user clicks; never automatic)
        sub_suggest = chosen if chosen and _safe_path(chosen) else None
        out["suggest"] = {"main_path": ref["path"], "sub_path": sub_suggest or sub_path}
    return out


def sub_source(cam: dict) -> str | None:
    """The camera path <id>_sub pulls, or None: relay the main stream (mediamtx.build_config)."""
    return plan(cam)["sub_path"]


def problems(cam: dict) -> list[str]:
    return plan(cam)["problems"]


def _profile_view(p: dict | None) -> dict | None:
    if not p:
        return None
    return {k: p.get(k) for k in ("token", "encoding", "width", "height", "fps", "path")}


def view(cam: dict) -> dict:
    """What Settings → Cameras shows: {checked_at, error, profiles, main, sub, sd: {path, relay, detected}}."""
    st = state_of(cam)
    p = plan(cam)
    return {
        "checked_at": cam.get("streams_checked_at"),
        "error": st.get("error"),
        "profiles": [_profile_view(x) for x in st.get("profiles") or []],
        "main": _profile_view(p["main"]),
        "sub": _profile_view(p["sub"]),
        "sd": {"path": p["sub_path"], "relay": p["sub_path"] is None, "detected": p["detected"]},
        "sub_not_found": bool((st.get("sub_not_found") or {}).get("path")),
        "metadata_analytics": st.get("metadata_analytics"),
        # the configured main path is not listed: the paths the camera does list, for a one-click fix in Settings
        "suggest": p.get("suggest"),
    }


# --------------------------------------------------------------------------- storing results

def _camera(camera_id: str) -> dict | None:
    return next((c for c in db.cameras() if c["id"] == camera_id), None)


def save(camera_id: str, st: dict, checked_at: float | None) -> None:
    db.execute("UPDATE cameras SET streams=?, streams_checked_at=? WHERE id=?",
               [json.dumps(st) if st else None, checked_at, camera_id])


def record_probe(camera_id: str, result: dict | None, error: str | None = None, now: float | None = None,
                 clear_404: bool = False) -> dict:
    """Store a probe's result (or its error, keeping the profiles from before). Returns the new stored state."""
    cam = _camera(camera_id) or {}
    st = state_of(cam)
    now = now or time.time()
    if result is not None:
        st = {**st, "profiles": result.get("profiles") or [], "media": result.get("media"),
              "metadata_analytics": result.get("metadata_analytics"), "error": None}
        if clear_404:   # a manual check gives the sub stream another chance (MediaMTX re-marks it on the next 404)
            st.pop("sub_not_found", None)
    else:
        st = {**st, "error": (error or "check failed")[:200]}
    save(camera_id, st, now)
    return st


def forget(camera_id: str) -> None:
    """The camera's address or credentials changed: what it served before no longer applies."""
    save(camera_id, {}, None)


def clear_404(camera_id: str) -> None:
    """The stream paths were edited: the path MediaMTX was refused may not be the one pulled any more."""
    cam = _camera(camera_id)
    st = state_of(cam or {})
    if st.pop("sub_not_found", None) is not None:
        save(camera_id, st, (cam or {}).get("streams_checked_at"))


def mark_sub_not_found(camera_id: str, now: float | None = None, code: str = "404") -> bool:
    """MediaMTX got RTSP 404 (or 453, no connection left) for <id>_sub: remember the camera path it pulled and why.
    True when this changes the plan."""
    cam = _camera(camera_id)
    if not cam:
        return False
    before = plan(cam)
    path = before["sub_path"]
    if path is None:
        return False   # already relaying the main stream
    st = state_of(cam)
    st["sub_not_found"] = {"path": path, "at": now or time.time(), "code": code}
    save(camera_id, st, cam.get("streams_checked_at"))
    log.warning("[%s] the camera answered %s for its sub stream %s: SD live view relays the main stream", camera_id, code, path)
    return True


def needs_probe(cam: dict, now: float | None = None) -> bool:
    """Never probed, or the last probe failed and RETRY_FAILED_S has passed."""
    if not cam.get("enabled"):
        return False
    checked = cam.get("streams_checked_at")
    if not checked:
        return True
    st = state_of(cam)
    return bool(st.get("error")) and not st.get("profiles") and (now or time.time()) - checked >= RETRY_FAILED_S


def scan_log(text: str) -> dict[str, str]:
    """{camera id: RTSP code} for <id>_sub sources refused with 404 or 453 in this MediaMTX log text (the last wins)."""
    return {cid: code for cid, code in SUB_404.findall(text)}


# --------------------------------------------------------------------------- the background checker

class StreamChecker:
    """Probes cameras that were never probed (one at a time), runs requested checks, and tails MediaMTX's log for
    sub-stream 404s. `on_change()` is called (in the event loop) whenever a camera's SD source changes, to rewrite
    mediamtx.yml."""

    def __init__(self, on_change, log_path=None) -> None:
        self.on_change = on_change
        self.log_path = log_path
        self._offset: int | None = None
        self._busy: dict[str, asyncio.Task] = {}

    async def check(self, camera_id: str, manual: bool = False) -> dict:
        """Probe one camera now (one probe per camera at a time) and apply the result. Returns view(cam)."""
        task = self._busy.get(camera_id)
        if task is None or task.done():
            task = asyncio.ensure_future(self._check(camera_id, manual))
            self._busy[camera_id] = task
            task.add_done_callback(lambda t: self._busy.pop(camera_id, None) if self._busy.get(camera_id) is t else None)
        # shielded: a request that gives up (timeout, client gone) leaves the probe to finish and be stored
        await asyncio.shield(task)
        cam = _camera(camera_id)
        return view(cam) if cam else {}

    def check_soon(self, camera_id: str) -> None:
        """Probe in the background (after the request that asked has answered)."""
        async def run():
            try:
                await self.check(camera_id)
            except Exception:  # noqa: BLE001 - a background check must not raise into the loop
                log.exception("[%s] stream check failed", camera_id)
        asyncio.ensure_future(run())

    async def _check(self, camera_id: str, manual: bool) -> None:
        cam = _camera(camera_id)
        if not cam:
            return
        before = plan(cam)["sub_path"]
        try:
            result = await asyncio.to_thread(probe_streams, cam)
            record_probe(camera_id, result, clear_404=manual)
            log.info("[%s] streams: %s", camera_id, ", ".join(
                f"{p.get('width')}x{p.get('height')} {p.get('encoding')} {p.get('path')}" for p in result["profiles"]) or "none listed")
        except soap.OnvifError as e:
            record_probe(camera_id, None, error=str(e), clear_404=manual)
            log.info("[%s] stream check failed: %s", camera_id, e)
        except Exception as e:  # noqa: BLE001 - malformed XML etc.
            record_probe(camera_id, None, error=f"unexpected reply: {e}", clear_404=manual)
            log.warning("[%s] stream check failed: %s", camera_id, e)
        after = _camera(camera_id)
        if after and plan(after)["sub_path"] != before:
            self.on_change()

    def _read_new_log(self) -> str:
        if not self.log_path:
            return ""
        try:
            size = self.log_path.stat().st_size
        except OSError:
            return ""
        if self._offset is None or size < self._offset:
            # first look (or MediaMTX started a new file): only what comes from now on; a 404 from before a restart
            # is already stored on the camera
            self._offset = size
            return ""
        if size == self._offset:
            return ""
        start = max(self._offset, size - LOG_TAIL_BYTES)
        try:
            with open(self.log_path, "rb") as f:
                f.seek(start)
                data = f.read(size - start)
        except OSError:
            return ""
        self._offset = size
        return data.decode("utf-8", "replace")

    def watch_once(self) -> bool:
        """Apply any sub-stream 404 MediaMTX logged since the last look. True when mediamtx.yml must be rewritten."""
        changed = False
        for cid, code in scan_log(self._read_new_log()).items():
            if mark_sub_not_found(cid, code=code):
                changed = True
                if not self._busy.get(cid):
                    self.check_soon(cid)   # the camera may serve another, lower-resolution profile
        return changed

    async def run(self) -> None:
        await asyncio.sleep(20)   # after MediaMTX and the readers are up
        last_probe_pass = 0.0
        while True:
            try:
                if self.watch_once():
                    self.on_change()
                if time.monotonic() - last_probe_pass >= 60:
                    last_probe_pass = time.monotonic()
                    for cam in db.cameras(enabled_only=True):
                        if needs_probe(cam):
                            await self.check(cam["id"])
            except Exception:  # noqa: BLE001 - a bad cycle must not end the watcher
                log.exception("stream watcher cycle failed")
            await asyncio.sleep(WATCH_S)
