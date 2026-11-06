"""AXIS cameras over VAPIX, for what ONVIF alone doesn't give (the Camera tab's AXIS section, Scan, new cameras, the
advisor, AXIS Object Analytics events). Plan and probe: docs/axis-integration-plan.md (TSS-DT cam9, an AXIS M4327-P,
AXIS OS 11.4.63, 2026-11-06).

* Who it is, without a login: POST /axis-cgi/basicdeviceinfo.cgi getAllUnrestrictedProperties (brand, model, firmware,
  serial) and /axis-cgi/apidiscovery.cgi getApiList (the VAPIX APIs it has) both answer anonymously. Scan uses it for
  the "needs auth" devices ONVIF can't name; a camera added with the default /main and /sub paths gets AXIS's own.
* Stream paths: VAPIX RTSP, /axis-media/media.amp?camera=1&videocodec=h264, instead of the ONVIF profile URIs. The
  ONVIF profiles of the M4327-P are all 1080p or larger and SD picked a 2560x1440 panorama, another picture than the
  recorded fisheye circle; with VAPIX the SD stream is the same view at a resolution we choose (`resolution=WxH`, the
  largest the camera lists with the main picture's shape at most SD_MAX_W wide). streams.plan accepts these paths
  although no ONVIF profile lists them.
* Object detection: the camera's AXIS Scene Metadata (ONVIF tt:Frame objects: Human, Vehicle, Bike...) is off until a
  metadata producer is enabled (analyticsmetadataconfig.cgi setEnabledProducers: AnalyticsSceneDescription, else
  VideoMotionTracker, on video channel 1), and is only in an RTSP session that asks for it (`analytics=polygon` on the
  main path). Then the metadata reader reads it from the main path like a Milesight's. setup() does both; it changes
  the camera, so it runs from the Camera tab's button only.
* AXIS Object Analytics scenarios (/local/objectanalytics/control.cgi getConfiguration) send ONVIF events on
  .../ObjectAnalytics/Device1ScenarioN with an "active" data item. annotate() names the rule after the scenario and
  gives the event the scenario's object classes when the event carries none; counting and occupancy scenarios (periodic
  counts, not detections) and the aggregate ScenarioANY are dropped. The scenarios are cached by read_all, setup and
  refresh_loop.

Blocking (httpx sync): call from a worker thread. Errors never carry the camera's password (camdevice.clean).
"""
from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse

import httpx

from .camdevice import Unsupported, clean, lock
from .onvif_soap import OnvifError

log = logging.getLogger("nvr.axis")

TIMEOUT_S = 6.0
IDENTIFY_TIMEOUT_S = 2.5
VAPIX_MEDIA = "/axis-media/media.amp"
SD_MAX_W = 1024
# advisory only: older than the 2024 long-term-support track of AXIS OS
FIRMWARE_BASELINE = (11, 11)
FIRMWARE_BASELINE_TEXT = "AXIS OS 11.11, the 2024 long-term support track"
PRODUCERS = ("AnalyticsSceneDescription", "VideoMotionTracker")   # the first the camera lists is enabled
COUNTING = {"crosslinecounting", "occupancyinarea"}               # AOA scenario types that count instead of detect
SCENARIO_TOPIC = re.compile(r"objectanalytics/device(\d+)scenario(\d+|any)\b", re.I)
REFRESH_S = 1800

_transport: httpx.BaseTransport | None = None   # tests stand a fake camera in here
_identity: dict[str, dict] = {}                 # camera id -> identify() (None-valued: not an AXIS)
_scenarios: dict[str, dict[int, dict]] = {}     # camera id -> {scenario id: {name, type, labels}}


class WebApiError(OnvifError):
    """The camera's VAPIX API refused or didn't answer (502, like a camera's ONVIF error)."""


# ---------------------------------------------------------------- who it is (no login)

def _url(scheme: str, host: str, port: int, path: str) -> str:
    h = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{h}:{port}{path}"


def identify_at(scheme: str, host: str, port: int, timeout: float = IDENTIFY_TIMEOUT_S) -> dict | None:
    """{brand, model, full_name, firmware, serial, apis: {id: version}} from the camera's anonymous VAPIX answers, or
    None when it isn't an AXIS (or doesn't answer)."""
    try:
        with httpx.Client(timeout=timeout, verify=False, trust_env=False, follow_redirects=False, transport=_transport) as c:
            r = c.post(_url(scheme, host, port, "/axis-cgi/basicdeviceinfo.cgi"),
                       json={"apiVersion": "1.0", "method": "getAllUnrestrictedProperties"})
            props = ((r.json() or {}).get("data") or {}).get("propertyList") if r.status_code == 200 else None
            if not isinstance(props, dict) or str(props.get("Brand", "")).upper() != "AXIS":
                return None
            apis: dict[str, str] = {}
            try:
                a = c.post(_url(scheme, host, port, "/axis-cgi/apidiscovery.cgi"), json={"apiVersion": "1.0", "method": "getApiList"})
                for x in ((a.json() or {}).get("data") or {}).get("apiList") or []:
                    if isinstance(x, dict) and x.get("id"):
                        apis[str(x["id"])] = str(x.get("version") or "")
            except (httpx.HTTPError, ValueError):
                pass
    except (httpx.HTTPError, ValueError):
        return None
    return {"brand": "AXIS", "model": props.get("ProdShortName") or props.get("ProdNbr") or "AXIS camera",
            "full_name": props.get("ProdFullName"), "number": props.get("ProdNbr"), "type": props.get("ProdType"),
            "firmware": props.get("Version"), "serial": props.get("SerialNumber"), "apis": apis}


def web(cam: dict) -> tuple[str, str, int]:
    from .camweb import web_address   # the camera's web page as this server reaches it (port-forward mode included)
    return web_address(cam)


def identify(cam: dict, fresh: bool = False) -> dict | None:
    """identify_at for a camera, cached per camera id (fresh: ask again)."""
    cid = cam.get("id") or ""
    if not fresh and cid in _identity:
        return _identity[cid]
    ident = identify_at(*web(cam))
    if cid:
        _identity[cid] = ident
    return ident


def cached(camera_id: str) -> dict | None:
    """What identify last found for this camera (None: not an AXIS, or not asked yet)."""
    return _identity.get(camera_id)


def firmware_tuple(version: str | None) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", version or "")[:3])


def firmware_old(version: str | None) -> bool:
    v = firmware_tuple(version)
    return bool(v) and v[:2] < FIRMWARE_BASELINE


# ---------------------------------------------------------------- the camera's VAPIX JSON APIs (with its login)

def _client(cam: dict) -> httpx.Client:
    return httpx.Client(auth=httpx.DigestAuth(cam.get("username") or "", cam.get("password") or ""), timeout=TIMEOUT_S,
                        verify=False, trust_env=False, follow_redirects=False, transport=_transport)


def _http(cam: dict, fn, what: str) -> httpx.Response:
    try:
        r = fn()
    except httpx.HTTPError as e:
        raise WebApiError(f"no answer from the camera's VAPIX API: {clean(str(e), cam)}") from None
    if r.status_code == 404:
        raise Unsupported(f"the camera has no {what}")
    if r.status_code in (401, 403):
        raise WebApiError(f"{what}: the camera refused the login (HTTP {r.status_code}); check the camera's user has operator rights")
    if r.status_code != 200:
        raise WebApiError(f"{what}: HTTP {r.status_code}")
    return r


def call(c: httpx.Client, cam: dict, path: str, method: str, params: dict | None = None, api_version: str = "1.0") -> dict:
    """A VAPIX JSON API method; its `data`. An `error` answer is a WebApiError with the camera's message."""
    scheme, host, port = web(cam)
    body = {"apiVersion": api_version, "context": "axiom", "method": method}
    if params is not None:
        body["params"] = params
    r = _http(cam, lambda: c.post(_url(scheme, host, port, path), json=body), f"{path} {method}")
    try:
        data = r.json()
    except ValueError:
        raise WebApiError(f"{method}: not a JSON answer") from None
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        err = data["error"]
        raise WebApiError(f"{method}: {clean(str(err.get('message') or 'refused'), cam)} (code {err.get('code')})")
    return (data or {}).get("data") or {}


def producers(c: httpx.Client, cam: dict) -> list[dict]:
    """The camera's analytics metadata producers: [{name, nice, channels: {channel: enabled}}]."""
    data = call(c, cam, "/axis-cgi/analyticsmetadataconfig.cgi", "listProducers", {})
    out = []
    for p in data.get("producers") or []:
        if isinstance(p, dict) and p.get("name"):
            out.append({"name": str(p["name"]), "nice": p.get("niceName") or p["name"],
                        "channels": {int(v.get("channel", 0)): bool(v.get("enabled")) for v in p.get("videochannels") or []
                                     if isinstance(v, dict)}})
    return out


def pick_producer(prods: list[dict]) -> dict | None:
    by = {p["name"]: p for p in prods}
    return next((by[n] for n in PRODUCERS if n in by), None)


def _labels(classes: list | None) -> list[str]:
    """A scenario's object classes as our labels (none listed: the scenario takes every class it can)."""
    out = []
    for k in classes or []:
        t = str((k or {}).get("type", "")).lower() if isinstance(k, dict) else ""
        lab = "person" if t in ("human", "person") else "vehicle" if t in ("vehicle", "car", "truck", "bus", "bike") else None
        if lab and lab not in out:
            out.append(lab)
    return out or ["person", "vehicle"]


def scenarios(c: httpx.Client, cam: dict) -> list[dict]:
    """AXIS Object Analytics scenarios [{id, name, type, labels, counting}] (Unsupported: AOA isn't installed); cached
    for annotate()."""
    data = call(c, cam, "/local/objectanalytics/control.cgi", "getConfiguration")
    out = []
    for s in data.get("scenarios") or []:
        if not isinstance(s, dict) or s.get("id") is None:
            continue
        typ = str(s.get("type") or "")
        out.append({"id": int(s["id"]), "name": str(s.get("name") or f"Scenario {s['id']}"), "type": typ,
                    "labels": _labels(s.get("objectClassifications")), "counting": typ.lower() in COUNTING})
    if cam.get("id"):
        _scenarios[cam["id"]] = {s["id"]: s for s in out}
    return out


def resolutions(c: httpx.Client, cam: dict) -> list[tuple[int, int]]:
    """The resolutions the camera lists (param.cgi Properties.Image.Resolution), largest first; [] when it won't say."""
    scheme, host, port = web(cam)
    try:
        r = _http(cam, lambda: c.get(_url(scheme, host, port, "/axis-cgi/param.cgi"),
                                     params={"action": "list", "group": "Properties.Image.Resolution"}), "param.cgi")
    except (WebApiError, Unsupported) as e:
        log.info("[%s] AXIS resolutions: %s", cam.get("id"), e)
        return []
    found = {(int(w), int(h)) for w, h in re.findall(r"(\d{3,4})x(\d{3,4})", r.text.split("=", 1)[-1])}
    return sorted(found, key=lambda wh: wh[0] * wh[1], reverse=True)


# ---------------------------------------------------------------- stream paths

def uses_vapix(path: str | None) -> bool:
    return bool(path) and urllib.parse.urlsplit(path).path == VAPIX_MEDIA


def has_analytics(path: str | None) -> bool:
    return uses_vapix(path) and ("analytics", "polygon") in urllib.parse.parse_qsl(urllib.parse.urlsplit(path).query)


def native_size(cam: dict, res: list[tuple[int, int]]) -> tuple[int, int] | None:
    """The main picture's size: the largest ONVIF profile the stream check stored, else the largest resolution listed."""
    profiles = [p for p in ((cam.get("streams") or {}).get("profiles") or []) if isinstance(p, dict) and p.get("width")]
    if profiles:
        p = max(profiles, key=lambda p: int(p["width"]) * int(p.get("height") or 0))
        return int(p["width"]), int(p["height"])
    return res[0] if res else None


def sd_size(native: tuple[int, int] | None, res: list[tuple[int, int]]) -> tuple[int, int] | None:
    """The SD resolution: the largest listed with the main picture's shape and at most SD_MAX_W wide (else the largest
    of any shape that fits)."""
    fits = [r for r in res if r[0] <= SD_MAX_W]
    if native and native[1]:
        shape = native[0] / native[1]
        same = [r for r in fits if r[1] and abs(r[0] / r[1] - shape) / shape < 0.03]
        if same:
            return max(same, key=lambda r: r[0] * r[1])
    return max(fits, key=lambda r: r[0] * r[1]) if fits else None


def stream_paths(native: tuple[int, int] | None, res: list[tuple[int, int]], analytics: bool) -> tuple[str, str]:
    """(main_path, sub_path) over VAPIX RTSP: H.264 of view 1, the main with the scene metadata when asked."""
    main = f"{VAPIX_MEDIA}?camera=1&videocodec=h264"
    sd = sd_size(native, res)
    sub = f"{main}&resolution={sd[0]}x{sd[1]}" if sd else main
    return (main + "&analytics=polygon" if analytics else main), sub


def paths_for_new(cam: dict) -> tuple[str, str] | None:
    """A camera being added with the default paths: AXIS's own if it is an AXIS (no metadata yet: that needs the
    camera's producer turned on, the Camera tab's button), else None. Never raises."""
    try:
        if not identify(cam, fresh=True):
            return None
        with _client(cam) as c:
            res = resolutions(c, cam)
        return stream_paths(native_size(cam, res), res, analytics=False)
    except Exception:  # noqa: BLE001 - adding a camera never fails for this
        log.warning("[%s] AXIS stream paths for a new camera", cam.get("id"), exc_info=True)
        return None


# ---------------------------------------------------------------- the Camera tab

def read_all(cam: dict) -> dict:
    """{supported, identity, firmware: {version, old, baseline}, metadata: {...}, scenarios, streams: {...},
    setup_needed}. Unsupported for a camera that isn't an AXIS."""
    ident = identify(cam, fresh=True)
    if not ident:
        raise Unsupported("not an AXIS camera")
    meta: dict = {"supported": False}
    scen: dict = {"supported": False}
    with _client(cam) as c:
        try:
            prods = producers(c, cam)
            p = pick_producer(prods)
            meta = {"supported": p is not None, "producer": p and p["name"], "producer_name": p and p["nice"],
                    "enabled": bool(p and p["channels"].get(1)), "producers": prods}
            if p is None:
                meta["reason"] = "the camera has no scene metadata producer (AXIS Scene Metadata isn't on this firmware)"
        except Unsupported as e:
            meta = {"supported": False, "reason": str(e)}
        except WebApiError as e:
            meta = {"supported": False, "error": True, "reason": str(e)}
        try:
            scen = {"supported": True, "scenarios": scenarios(c, cam)}
        except Unsupported:
            scen = {"supported": False, "reason": "AXIS Object Analytics isn't installed or running"}
        except WebApiError as e:
            scen = {"supported": False, "error": True, "reason": str(e)}
    main, sub = cam.get("main_path"), cam.get("sub_path")
    streams = {"main_path": main, "sub_path": sub, "vapix": uses_vapix(main) and uses_vapix(sub), "analytics": has_analytics(main)}
    needed = not streams["vapix"] or (meta.get("supported") and not (meta.get("enabled") and streams["analytics"]))
    return {"supported": True, "identity": ident,
            "firmware": {"version": ident.get("firmware"), "old": firmware_old(ident.get("firmware")), "baseline": FIRMWARE_BASELINE_TEXT},
            "metadata": meta, "analytics": scen, "streams": streams, "setup_needed": bool(needed)}


def setup(cam: dict) -> dict:
    """The Camera tab's "Set up for Axiom": turn the camera's scene metadata producer on (video channel 1), and work out
    the VAPIX stream paths (main with the metadata, SD the same view smaller). Changes the camera; the caller stores
    the paths. {producer, enabled_now, main_path, sub_path, sd: "WxH" | None, written: [...]}."""
    if not identify(cam, fresh=True):
        raise Unsupported("not an AXIS camera")
    written = []
    with lock(cam["id"]), _client(cam) as c:
        try:
            p = pick_producer(producers(c, cam))
        except Unsupported:
            p = None
        enabled_now = False
        if p is not None and not p["channels"].get(1):
            call(c, cam, "/axis-cgi/analyticsmetadataconfig.cgi", "setEnabledProducers",
                 {"producers": [{"name": p["name"], "videochannels": [{"channel": 1, "enabled": True}]}]})
            written.append(f"scene metadata producer {p['name']} on")
            enabled_now = True
        res = resolutions(c, cam)
        try:
            scenarios(c, cam)
        except (Unsupported, WebApiError):
            pass
    native = native_size(cam, res)
    main, sub = stream_paths(native, res, analytics=p is not None)
    sd = sd_size(native, res)
    return {"producer": p and p["name"], "enabled_now": enabled_now, "main_path": main, "sub_path": sub,
            "sd": f"{sd[0]}x{sd[1]}" if sd else None, "written": written}


# ---------------------------------------------------------------- AXIS Object Analytics events

def annotate(camera_id: str, topic: str, data: dict) -> bool:
    """An ONVIF event from the camera: AOA scenario events get the scenario's name as the rule and its object classes
    when the event names none (data changed in place). False: drop it (a counting scenario, or ScenarioANY, which
    repeats the per-scenario events)."""
    m = SCENARIO_TOPIC.search(topic or "")
    if not m:
        return True
    if m.group(2).lower() == "any":
        return False
    n = int(m.group(2))
    sc = (_scenarios.get(camera_id) or {}).get(n)
    if sc is None:
        data.setdefault("Rule", f"Scenario {n}")
        return True
    if sc["counting"]:
        return False
    data.setdefault("Rule", sc["name"])
    from .ruleevents import object_type
    if not object_type(data):
        data["ObjectClass"] = " ".join(sc["labels"])
    return True


async def refresh_loop(cameras) -> None:
    """Every REFRESH_S: who each enabled camera is (no login) and, for an AXIS, its AOA scenarios, so the advisor and
    annotate() have them without the Camera tab being opened. `cameras()` gives the enabled cameras."""
    await asyncio.sleep(60)
    while True:
        for cam in cameras():
            try:
                if await asyncio.to_thread(identify, cam, True):
                    def fetch(c=cam):
                        with _client(c) as cl:
                            scenarios(cl, c)
                    await asyncio.to_thread(fetch)
            except (Unsupported, WebApiError) as e:
                log.info("[%s] AXIS refresh: %s", cam.get("id"), e)
            except Exception:  # noqa: BLE001 - one camera never stops the others
                log.warning("[%s] AXIS refresh failed", cam.get("id"), exc_info=True)
        await asyncio.sleep(REFRESH_S)
