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
* no lower-resolution profile, or the camera answered RTSP 404 / 453 on the path it would use: <id>_sub relays <id>
  (the recorded main stream, already pulled 24/7) from this MediaMTX, so SD always plays, and the camera gets the
  NO_SUB problem. plan()["encoding"] is what SD then plays: H.265 (a relayed H.265 main) is not playable over
  WebRTC in most browsers, and view() says so (sd.encoding, sd.h265).

The probe runs when a camera is added or its address or credentials change (api.put_camera; a probe still running
for the old address is discarded and asked again), on Settings' "Check" (POST /api/cameras/{id}/streams/check), and
for cameras never probed (StreamChecker, at startup and when an import adds one). StreamChecker also tails MediaMTX's
log for "[path <name>] [RTSP source] bad status code: 404" on the path that pulls the camera's sub stream (<id>_sub;
<id> on a camera recording its sub stream, whose <id>_sub is a relay of our own <id>, so a 404 there is this
MediaMTX's own answer, not the camera's) so a camera that drops its sub stream falls back without waiting for a
probe. The fallback is not for ever: after RETRY_SUB_S (doubling with each refusal, up to RETRY_SUB_MAX_S) a 453 (no
connection left on the camera, usually for a while) expires and SD tries the sub again, and a 404 is cleared when
the camera answers DESCRIBE on that path again. A manual Check clears it at once.

Stored on the camera row: `streams` (JSON {profiles, media, metadata_analytics, error, sub_not_found: {path, at, code,
tries, retry_at}, sub_retried: {path, code, tries, at}}) and `streams_checked_at`. metadata_analytics is the profiles'
metadata configuration Analytics flag (False on cameras whose metadata carries no objects, e.g. the Reolink
RP-PCT8MD: their detections come from ONVIF events, ruleevents.py).

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
RETRY_SUB_S = 3600          # a sub stream the camera refused is tried again after this long, doubling each time ...
RETRY_SUB_MAX_S = 86400     # ... up to a day
WATCH_S = 10                # MediaMTX log tail interval
LOG_TAIL_BYTES = 256 * 1024

# what changes which camera answers, or how: the stored profiles no longer apply
ADDRESS_KEYS = ("host", "onvif_port", "rtsp_port", "username", "password", "public_host", "public_rtsp_port", "public_onvif_port")

ENCODINGS = {"H264": "H.264", "H265": "H.265", "HEVC": "H.265", "JPEG": "MJPEG", "MJPEG": "MJPEG", "MPEG4": "MPEG-4"}
# query parameters some cameras put credentials in (never stored, never shown)
SECRET_PARAM = re.compile(r"^(user(name)?|pass(word|wd)?|pwd|auth|token|key)$", re.I)
# MediaMTX: "2026/10/08 19:58:14 ERR [path cam4_sub] [RTSP source] bad status code: 404 (Not Found)"
REFUSED = re.compile(r"\[path ([a-z0-9_]+)\] \[RTSPS? source\][^\n]*\b(404|453)\b")
# 453 "Not Enough Bandwidth": the camera has no stream connection left for us (Qwenbot's SW Corner PTZ 2026-10-08:
# something else held its slots). Any extra sub-stream session would be refused too: SD relays the main stream we
# already pull, whatever the profiles say.


def busy_text(h265: bool = False) -> str:
    return ("The camera refused its low-resolution stream (453 Not Enough Bandwidth: no connection left on the "
            "camera): SD plays the main stream. Check what else is connected to the camera, then press Check."
            + (H265_NOTE if h265 else ""))


def no_sub_text(width: int | None = None, height: int | None = None, h265: bool = False) -> str:
    size = f" ({width}×{height})" if width and height else ""
    return (f"No low-resolution stream: SD plays the main stream{size}. "
            "Enable the camera's secondary stream for faster live view." + (H265_NOTE if h265 else ""))


H265_NOTE = " The main stream is H.265, which most browsers can't play live."


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
    # kept as the camera wrote them (a bare "?ch1" stays "ch1", not "ch1="): only the credential parameters go
    kept = [kv for kv in parts.query.split("&")
            if kv and not SECRET_PARAM.match(urllib.parse.unquote_plus(kv.split("=", 1)[0]))]
    q = "&".join(kept)
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
     main: the main profile (or None), sub: the profile SD plays (or None), problems: [str],
     encoding: what SD plays ("H.264", "H.265"...; None when unknown)}."""
    st = state_of(cam)
    profiles = [p for p in st.get("profiles") or [] if isinstance(p, dict)]
    main_path, sub_path = cam.get("main_path") or "/main", cam.get("sub_path") or "/sub"
    missing = (st.get("sub_not_found") or {}).get("path")
    busy = (st.get("sub_not_found") or {}).get("code") == "453"
    out: dict = {"sub_path": sub_path, "detected": False, "main": None, "sub": None, "problems": [], "suggest": None,
                 "encoding": None}
    main = next((p for p in profiles if same_path(main_path, p.get("path"))), None)
    main_enc = (main or (profiles[0] if profiles else {})).get("encoding")
    if busy:   # no connection left on the camera: relay the main stream, whatever it offers (retried: StreamChecker)
        out.update(sub_path=None, encoding=main_enc)
        out["problems"].append(busy_text(main_enc == "H.265"))
        return out
    if not profiles:   # never probed, or the camera wouldn't say: as configured, unless MediaMTX saw a 404 there
        if missing and missing == sub_path:
            out.update(sub_path=None)
            out["problems"].append(no_sub_text())
        return out
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
    out["encoding"] = (profile or ref).get("encoding")      # the sub stream, or the main stream relayed
    if chosen is None:
        out["problems"].append(no_sub_text(ref.get("width"), ref.get("height"), ref.get("encoding") == "H.265"))
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
    nf = st.get("sub_not_found") or {}
    return {
        "checked_at": cam.get("streams_checked_at"),
        "error": st.get("error"),
        "profiles": [_profile_view(x) for x in st.get("profiles") or []],
        "main": _profile_view(p["main"]),
        "sub": _profile_view(p["sub"]),
        # encoding: what SD plays; h265: browsers can't play it over WebRTC (SD relays an H.265 main stream)
        "sd": {"path": p["sub_path"], "relay": p["sub_path"] is None, "detected": p["detected"],
               "encoding": p["encoding"], "h265": p["encoding"] == "H.265"},
        "sub_not_found": bool(nf.get("path")),
        "sub_retry_at": nf.get("retry_at") if nf.get("path") else None,   # when the refused sub stream is tried again
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
            st.pop("sub_retried", None)
    else:
        st = {**st, "error": (error or "check failed")[:200]}
    save(camera_id, st, now)
    return st


# bumped by forget(): a probe that started before the camera was re-addressed is stale (StreamChecker._check)
_generation: dict[str, int] = {}


def generation(camera_id: str) -> int:
    return _generation.get(camera_id, 0)


def forget(camera_id: str) -> None:
    """The camera's address or credentials changed: what it served before no longer applies."""
    _generation[camera_id] = generation(camera_id) + 1
    save(camera_id, {}, None)


def clear_404(camera_id: str) -> None:
    """The stream paths were edited: the path MediaMTX was refused may not be the one pulled any more."""
    cam = _camera(camera_id)
    st = state_of(cam or {})
    if st.pop("sub_not_found", None) is not None:
        st.pop("sub_retried", None)
        save(camera_id, st, (cam or {}).get("streams_checked_at"))


def _retry_wait(tries: int) -> float:
    return min(RETRY_SUB_S * 2 ** max(0, tries - 1), RETRY_SUB_MAX_S)


def mark_sub_not_found(camera_id: str, now: float | None = None, code: str = "404") -> bool:
    """The camera answered RTSP 404 (or 453, no connection left) for its sub stream: remember the camera path and
    why, and when to try it again (RETRY_SUB_S, doubled for each refusal in a row of the same path, at most
    RETRY_SUB_MAX_S). True when this changes the plan."""
    cam = _camera(camera_id)
    if not cam:
        return False
    before = plan(cam)
    path = before["sub_path"]
    if path is None:
        return False   # already relaying the main stream
    now = now or time.time()
    st = state_of(cam)
    prev = st.pop("sub_retried", None) or {}
    again = prev.get("path") == path and now - (prev.get("at") or 0) < 2 * RETRY_SUB_MAX_S
    tries = (prev.get("tries") or 0) + 1 if again else 1
    st["sub_not_found"] = {"path": path, "at": now, "code": code, "tries": tries, "retry_at": now + _retry_wait(tries)}
    save(camera_id, st, cam.get("streams_checked_at"))
    log.warning("[%s] the camera answered %s for its sub stream %s: SD live view relays the main stream (tried again "
                "in %.0f h)", camera_id, code, path, _retry_wait(tries) / 3600)
    return True


def retry_due(cam: dict, now: float | None = None) -> bool:
    """The camera's sub stream fallback is due to be tried again."""
    nf = state_of(cam).get("sub_not_found") or {}
    if not nf.get("path") or not cam.get("enabled"):
        return False
    due = nf.get("retry_at") or (nf.get("at") or 0) + RETRY_SUB_S
    return (now or time.time()) >= due


def end_fallback(camera_id: str, now: float | None = None) -> bool:
    """Try the refused sub stream again: drop the fallback, remembering how often it was refused (a refusal soon
    after waits longer). True when there was one."""
    cam = _camera(camera_id)
    st = state_of(cam or {})
    nf = st.pop("sub_not_found", None)
    if not cam or not nf:
        return False
    st["sub_retried"] = {"path": nf.get("path"), "code": nf.get("code"), "tries": nf.get("tries") or 1, "at": now or time.time()}
    save(camera_id, st, cam.get("streams_checked_at"))
    return True


def postpone_retry(camera_id: str, now: float | None = None) -> None:
    """The sub stream still isn't served: keep relaying and try again after a longer wait."""
    cam = _camera(camera_id)
    st = state_of(cam or {})
    nf = st.get("sub_not_found")
    if not cam or not nf:
        return
    nf["tries"] = (nf.get("tries") or 1) + 1
    nf["retry_at"] = (now or time.time()) + _retry_wait(nf["tries"])
    save(camera_id, st, cam.get("streams_checked_at"))


def sub_answers(cam: dict, path: str, timeout: float = CALL_TIMEOUT_S) -> int | None:
    """RTSP DESCRIBE of `path` on the camera (read-only: no session is set up). Its status code, None when the camera
    can't be reached. Blocking: run it in a worker thread."""
    from .rtsp_client import Rtsp
    host, _, port = soap.outside(cam)
    url = f"rtsp://{host}:{port}{path}"
    try:
        r = Rtsp(url, cam.get("username") or "", cam.get("password") or "", timeout=timeout)
    except OSError:
        return None
    try:
        return r.request("DESCRIBE", url, {"Accept": "application/sdp"})[0]
    except (OSError, ConnectionError, ValueError, IndexError):
        return None
    finally:
        try:
            r.sock.close()
        except OSError:
            pass


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
    """{MediaMTX path: RTSP code} for path sources refused with 404 or 453 in this MediaMTX log text (the last wins)."""
    return {path: code for path, code in REFUSED.findall(text)}


def sub_refusals(refused: dict[str, str], cameras: list[dict]) -> dict[str, str]:
    """{camera id: code} for the refusals that are the camera's own answer about its sub stream: on the path that pulls
    it from the camera. That is <id>_sub, except on a camera recording its sub stream: there <id> pulls it and
    <id>_sub relays <id> from this MediaMTX, so a 404 on <id>_sub is our own MediaMTX's (the camera rebooting, the
    server just restarted, <id> not ready yet), never a reason to record the main stream instead."""
    from .mediamtx import records_sub
    out = {}
    for cam in cameras:
        name = cam["id"] if records_sub(cam) else f"{cam['id']}_sub"
        if name in refused:
            out[cam["id"]] = refused[name]
    return out


# --------------------------------------------------------------------------- the background checker

class StreamChecker:
    """Probes cameras that were never probed (one at a time), runs requested checks, tails MediaMTX's log for
    sub-stream 404s / 453s and tries fallen-back sub streams again when due (retry_subs). `on_change()` is called (in the event loop) whenever a camera's SD source changes, to rewrite
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
        for _ in range(5):
            cam = _camera(camera_id)
            if not cam:
                return
            gen = generation(camera_id)
            result, error = None, None
            try:
                result = await asyncio.to_thread(probe_streams, cam)
            except soap.OnvifError as e:
                error = str(e)
                log.info("[%s] stream check failed: %s", camera_id, e)
            except Exception as e:  # noqa: BLE001 - malformed XML etc.
                error = f"unexpected reply: {e}"
                log.warning("[%s] stream check failed: %s", camera_id, e)
            if generation(camera_id) != gen:
                # the camera was re-addressed (api.put_camera -> forget) while this asked the old address: ask again
                log.info("[%s] the camera's address changed during its stream check: checking again", camera_id)
                continue
            record_probe(camera_id, result, error=error, clear_404=manual)
            if result is not None:
                log.info("[%s] streams: %s", camera_id, ", ".join(
                    f"{p.get('width')}x{p.get('height')} {p.get('encoding')} {p.get('path')}" for p in result["profiles"]) or "none listed")
            break
        after = _camera(camera_id)
        if after and plan(after)["sub_path"] != before:
            self.on_change()

    async def retry_subs(self, now: float | None = None) -> bool:
        """Cameras whose sub-stream fallback is due (retry_due): a 453 (no connection left on the camera, usually for a
        while) expires: SD pulls the sub again, and the next 453 in MediaMTX's log brings the relay back with a longer
        wait. A 404 is cleared when the camera answers DESCRIBE on that path again; otherwise the wait doubles.
        True when a camera's SD source changed (mediamtx.yml must be rewritten)."""
        changed = False
        for cam in db.cameras(enabled_only=True):
            if not retry_due(cam, now):
                continue
            cid, nf = cam["id"], state_of(cam)["sub_not_found"]
            if nf.get("code") == "453":
                ok = True
            else:
                gen = generation(cid)
                status = await asyncio.to_thread(sub_answers, cam, nf["path"])
                if generation(cid) != gen:
                    continue          # re-addressed meanwhile: forget() dropped the fallback already
                ok = status == 200
            if ok and end_fallback(cid, now):
                log.info("[%s] trying its sub stream %s again (refused with %s)", cid, nf["path"], nf.get("code"))
                changed = True
            elif not ok:
                postpone_retry(cid, now)
        return changed

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
        """Apply any sub-stream 404 / 453 MediaMTX logged since the last look (sub_refusals: only the camera's own
        answers). True when mediamtx.yml must be rewritten."""
        changed = False
        refused = scan_log(self._read_new_log())
        if not refused:
            return False
        for cid, code in sub_refusals(refused, db.cameras()).items():
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
                    if await self.retry_subs():
                        self.on_change()
            except Exception:  # noqa: BLE001 - a bad cycle must not end the watcher
                log.exception("stream watcher cycle failed")
            await asyncio.sleep(WATCH_S)
