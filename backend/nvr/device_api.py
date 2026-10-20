"""Camera settings over ONVIF (camdevice.py): Settings › Cameras › Edit › Camera, and "Fix clocks…" for several.

GET /api/cameras/{id}/device reads everything (each section on its own: {supported: false, reason} where the camera
can't); PUT .../device/time | video/{token} | audio/{token} | imaging/{source} | analytics and POST
.../device/time/sync-now | reboot change the camera and return what it reads back. GET / PUT .../device/milesight: a
Milesight camera's own web API (milesight.py: white light, red/blue light, siren volume, one-click disarm). POST /api/cameras/device/time sets
the clocks of several cameras at once; GET /api/cameras/device/time gives the defaults (NTP server, time zone).

Roles through the hub (hub/hub/roles.py): reading needs operator (the camera's NTP servers, firmware), every write
admin. Checked here too for a tunneled or direct request (the site's own page has no sign-in: the LAN is the trust
boundary). Every write is logged with who asked and what changed. Camera errors are passed on verbatim, without
credentials. Nothing here stores the camera's settings: the camera is asked each time.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import camdevice, deterrence, milesight
from .camdevice import Invalid, Unsupported
from .db import db
from .onvif_soap import OnvifError
from .speakers_api import require

log = logging.getLogger("nvr.device")
router = APIRouter()

SETTING = "camera_time"            # {ntp_server, timezone}: what "Fix clocks…" and the Camera tab start from
DEFAULT_NTP = "pool.ntp.org"
BULK_AT_ONCE = 6


def _camera(camera_id: str) -> dict:
    cam = next((c for c in db.cameras() if c["id"] == camera_id), None)
    if cam is None:
        raise HTTPException(404, "unknown camera")
    return cam


def _site():
    from . import api   # the app's state (stream checks, ingest readers, the playback audio cache); imported late: api imports this
    return api


def time_defaults() -> dict:
    """The NTP server and time zone to start from: what was last applied with "remember", else pool.ntp.org and this
    server's own zone. suggestions: the gateway (only useful if the router answers NTP) and public pools."""
    saved = db.get_setting(SETTING, {}) or {}
    zone = camdevice.server_zone()
    gw = camdevice.default_gateway()
    tz = saved.get("timezone") or zone
    return {"ntp_server": saved.get("ntp_server") or DEFAULT_NTP, "timezone": tz, "posix": camdevice.posix_tz(tz),
            "server_zone": zone, "gateway": gw, "saved": bool(saved),
            "suggestions": [s for s in dict.fromkeys([saved.get("ntp_server"), DEFAULT_NTP, gw, "time.google.com", "time.windows.com"]) if s]}


async def _run(cam: dict, fn, *args):
    """fn(cam, *args) in a worker thread; camera errors as HTTP answers (422 a value the camera doesn't offer, 409 not
    supported, 502 the camera refused or didn't answer)."""
    try:
        return await asyncio.to_thread(fn, cam, *args)
    except Invalid as e:
        raise HTTPException(422, camdevice.clean(str(e), cam))
    except Unsupported as e:
        raise HTTPException(409, camdevice.clean(str(e), cam))
    except OnvifError as e:
        raise HTTPException(502, f"the camera answered: {camdevice.clean(str(e), cam)}")


def _audit(cam: dict, what: str, who: str) -> None:
    log.info("camera settings: %s (%s) %s, by %s", cam["id"], cam.get("name") or "", what, who)


def _clock_moved(camera_id: str) -> None:
    ing = getattr(_site().state, "ingests", {}).get(camera_id)
    if ing is not None:
        ing.meta.reset_clock()


def _zone_tz(timezone: str | None) -> tuple[str | None, str | None]:
    """(POSIX TZ to send, IANA zone to check against): None, None keeps the camera's own zone."""
    if not timezone or not timezone.strip():
        return None, None
    tz = camdevice.posix_tz(timezone)
    if tz is None:
        raise HTTPException(422, f"unknown time zone {timezone[:64]!r}: use a name like America/Chicago or a POSIX rule like CST6CDT,M3.2.0,M11.1.0")
    zone = timezone.strip() if camdevice.utc_offset_s(timezone.strip()) is not None else None
    return tz, zone


# ---------------------------------------------------------------- read

@router.get("/api/cameras/device/time")
async def device_time_defaults(request: Request):
    require(request, "operator")
    return await asyncio.to_thread(time_defaults)


@router.get("/api/cameras/{camera_id}/device")
async def device_read(camera_id: str, request: Request, zone: str | None = None):
    """Everything the camera says about itself over ONVIF (camdevice.read_all), with the clock defaults. `zone`: the
    IANA zone to check the camera's local time against (default: the saved or this server's)."""
    require(request, "operator")
    cam = _camera(camera_id)
    defaults = await asyncio.to_thread(time_defaults)
    out = await asyncio.to_thread(camdevice.read_all, cam, zone or defaults["timezone"])
    return {**out, "camera_id": camera_id, "defaults": defaults}


# ---------------------------------------------------------------- time

class TimeIn(BaseModel):
    mode: Literal["ntp", "manual_sync"] = "ntp"
    ntp_servers: list[str] = Field(default_factory=list, max_length=4)
    timezone: str | None = Field(None, max_length=64)   # IANA name or POSIX TZ; empty = keep the camera's own zone


@router.put("/api/cameras/{camera_id}/device/time")
async def device_set_time(camera_id: str, body: TimeIn, request: Request):
    who = require(request, "admin")
    cam = _camera(camera_id)
    tz, zone = _zone_tz(body.timezone)
    servers = [s.strip() for s in body.ntp_servers if s.strip()]
    out = await _run(cam, camdevice.set_time, body.mode, servers, tz, zone)
    _clock_moved(camera_id)
    _audit(cam, f"time {body.mode} servers={servers} tz={tz or 'kept'} -> offset {out.get('offset_s')} s", who)
    return out


@router.post("/api/cameras/{camera_id}/device/time/sync-now")
async def device_sync_now(camera_id: str, request: Request, zone: str | None = None):
    who = require(request, "admin")
    cam = _camera(camera_id)
    out = await _run(cam, camdevice.sync_now, zone or (await asyncio.to_thread(time_defaults))["timezone"])
    _clock_moved(camera_id)
    _audit(cam, f"time set from this server's clock -> offset {out.get('offset_s')} s", who)
    return out


class BulkTimeIn(TimeIn):
    camera_ids: list[str] | None = Field(None, max_length=200)   # None = every enabled camera
    remember: bool = True   # the NTP server and zone become the defaults


@router.post("/api/cameras/device/time")
async def device_fix_clocks(body: BulkTimeIn, request: Request):
    """"Fix clocks…": the same time settings on several cameras, BULK_AT_ONCE at a time. {results: [{camera_id, name,
    ok, offset_s, mode, timezone, warnings, error}]} in the order asked."""
    who = require(request, "admin")
    tz, zone = _zone_tz(body.timezone)
    servers = [s.strip() for s in body.ntp_servers if s.strip()]
    if body.mode == "ntp" and not servers:
        raise HTTPException(422, "name at least one NTP server")
    cams = db.cameras()
    if body.camera_ids is None:
        chosen = [c for c in cams if c.get("enabled")]
    else:
        known = {c["id"]: c for c in cams}
        missing = [i for i in body.camera_ids if i not in known]
        if missing:
            raise HTTPException(422, f"no camera {', '.join(missing[:5])} on this server")
        chosen = [known[i] for i in dict.fromkeys(body.camera_ids)]
    if body.remember and body.mode == "ntp":
        db.set_setting(SETTING, {"ntp_server": servers[0], "timezone": (body.timezone or "").strip() or None})
    sem = asyncio.Semaphore(BULK_AT_ONCE)

    async def one(cam: dict) -> dict:
        base = {"camera_id": cam["id"], "name": cam.get("name") or cam["id"]}
        async with sem:
            try:
                if body.mode == "ntp":
                    out = await asyncio.to_thread(camdevice.set_time, cam, "ntp", servers, tz, zone)
                else:
                    out = await asyncio.to_thread(camdevice.set_time, cam, "manual_sync", [], tz, zone)
            except (OnvifError, Invalid) as e:
                return {**base, "ok": False, "error": camdevice.clean(str(e), cam)}
            except Exception as e:  # noqa: BLE001 - one camera's odd reply must not lose the others' results
                log.warning("clock fix %s failed", cam["id"], exc_info=True)
                return {**base, "ok": False, "error": f"unexpected reply: {camdevice.clean(str(e), cam)}"}
        _clock_moved(cam["id"])
        return {**base, "ok": True, "offset_s": out.get("offset_s"), "mode": out.get("mode"), "timezone": out.get("timezone"),
                "zone_check": out.get("zone_check"), "warnings": out.get("warnings") or []}

    results = await asyncio.gather(*(one(c) for c in chosen))
    log.info("camera settings: clocks of %d camera(s) %s servers=%s tz=%s, by %s: %s", len(results), body.mode, servers, tz or "kept",
             who, ", ".join(f"{r['camera_id']} {'ok' if r['ok'] else 'failed'}" for r in results))
    return {"results": results}


# ---------------------------------------------------------------- video, audio, imaging

class VideoIn(BaseModel):
    fps: float | None = Field(None, gt=0, le=240)
    bitrate_kbps: int | None = Field(None, ge=8, le=200_000)
    gov_seconds: float | None = Field(None, ge=0.1, le=60)   # keyframe interval in seconds (frames = round(s x fps))
    gov_length: int | None = Field(None, ge=1, le=10_000)    # or in frames
    resolution: str | None = Field(None, max_length=20)      # "1920x1080"
    encoding: str | None = Field(None, max_length=16)        # H264 / H265 / JPEG (Media2 cameras only)


@router.put("/api/cameras/{camera_id}/device/video/{token}")
async def device_set_video(camera_id: str, token: str, body: VideoIn, request: Request):
    who = require(request, "admin")
    cam = _camera(camera_id)
    out, written = await _run(cam, camdevice.set_video, token, body.model_dump(exclude_none=True))
    streams = getattr(_site().state, "streams", None)
    if streams is not None:
        streams.check_soon(camera_id)   # resolution / encoding / profile changes: the SD plan and the header follow
    _audit(cam, f"video encoder {token}: {written}", who)
    return out


class AudioIn(BaseModel):
    encoding: str | None = Field(None, max_length=16)       # AAC / G711 / G726
    bitrate: int | None = Field(None, ge=1, le=1024)        # kbps
    sample_rate: int | None = Field(None, ge=1, le=192)     # kHz


@router.put("/api/cameras/{camera_id}/device/audio/{token}")
async def device_set_audio(camera_id: str, token: str, body: AudioIn, request: Request):
    who = require(request, "admin")
    cam = _camera(camera_id)
    out, written = await _run(cam, camdevice.set_audio, token, body.model_dump(exclude_none=True))
    _site()._audio_tracks.pop(camera_id, None)   # playback asks MediaMTX again whether the audio needs converting
    _audit(cam, f"audio encoder {token}: {written}", who)
    return out


class ImagingIn(BaseModel):
    brightness: float | None = None
    contrast: float | None = None
    saturation: float | None = None
    sharpness: float | None = None
    ir_cut: Literal["ON", "OFF", "AUTO"] | None = None   # day / night: ON = day (filter in), OFF = night


@router.put("/api/cameras/{camera_id}/device/imaging/{source}")
async def device_set_imaging(camera_id: str, source: str, body: ImagingIn, request: Request):
    who = require(request, "admin")
    cam = _camera(camera_id)
    out, written = await _run(cam, camdevice.set_imaging, source, body.model_dump(exclude_none=True))
    _audit(cam, f"imaging {source}: {written}", who)
    return out


# ---------------------------------------------------------------- analytics, reboot

class RuleIn(BaseModel):
    configuration: str = Field(min_length=1, max_length=128)
    rule: str = Field(min_length=1, max_length=128)
    enabled: bool


@router.put("/api/cameras/{camera_id}/device/analytics")
async def device_set_rule(camera_id: str, body: RuleIn, request: Request):
    who = require(request, "admin")
    cam = _camera(camera_id)
    out = await _run(cam, camdevice.set_rule, body.configuration, body.rule, body.enabled)
    _audit(cam, f"analytics rule {body.rule!r} {'on' if body.enabled else 'off'}", who)
    return out


@router.post("/api/cameras/{camera_id}/device/reboot")
async def device_reboot(camera_id: str, request: Request):
    who = require(request, "admin")
    cam = _camera(camera_id)
    msg = await _run(cam, camdevice.reboot)
    _audit(cam, "reboot", who)
    return {"ok": True, "message": msg}


# ---------------------------------------------------------------- Milesight extras (its web API)

def _is_milesight(cam: dict) -> bool:
    """From the deterrence probe when it has run (GetDeviceInformation), else asked now."""
    d = deterrence.manager.get(cam["id"])
    if d is not None and d.available:
        return bool(d.caps.get("milesight"))
    c = camdevice.connect(cam)
    return "milesight" in (camdevice.read_info(c).get("manufacturer") or "").lower()


def _milesight_read(cam: dict) -> dict:
    if not _is_milesight(cam):
        raise Unsupported("not a Milesight camera")
    return milesight.read_all(cam)


@router.get("/api/cameras/{camera_id}/device/milesight")
async def device_milesight(camera_id: str, request: Request):
    """{supported, white_light, red_blue, siren, disarm}: {supported: false, reason} for a camera of another maker or one
    whose web API doesn't answer."""
    require(request, "operator")
    cam = _camera(camera_id)
    try:
        return await _run(cam, _milesight_read)
    except HTTPException as e:
        if e.status_code in (409, 502):
            return {"supported": False, "reason": e.detail}
        raise


class WhiteLightIn(BaseModel):
    mode: Literal["auto", "on", "off", "schedule"] | None = None
    sensitivity: int | None = Field(None, ge=1, le=5)
    delay_s: int | None = Field(None, ge=1, le=60)
    start: str | None = Field(None, max_length=5)
    stop: str | None = Field(None, max_length=5)
    brightness: int | None = Field(None, ge=1, le=100)


class RedBlueIn(BaseModel):
    mode: Literal["red_blue", "red", "blue"] | None = None
    frequency: Literal["low", "medium", "high"] | None = None


class DisarmIn(BaseModel):
    enable: bool | None = None
    mutes: dict[str, bool] | None = None


class MilesightIn(BaseModel):
    white_light: WhiteLightIn | None = None
    red_blue: RedBlueIn | None = None
    speaker_volume: int | None = Field(None, ge=0, le=100)
    disarm: DisarmIn | None = None


@router.put("/api/cameras/{camera_id}/device/milesight")
async def device_set_milesight(camera_id: str, body: MilesightIn, request: Request):
    """Change the Milesight settings asked for (each read, changed and sent back whole, as the camera's own page does);
    returns everything read back, with `before` (the sections changed, as they were) and `written` (what was sent)."""
    who = require(request, "admin")
    cam = _camera(camera_id)

    def work(c: dict) -> tuple[dict, dict, dict]:
        if not _is_milesight(c):
            raise Unsupported("not a Milesight camera")
        return milesight.write(c, body.model_dump(exclude_none=True))

    before, after, written = await _run(cam, work)
    _audit(cam, f"Milesight {', '.join(written)}: {body.model_dump(exclude_none=True)}", who)
    return {**after, "before": before, "written": written}
