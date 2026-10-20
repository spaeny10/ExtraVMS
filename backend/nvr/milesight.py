"""A Milesight camera's own web API, for what ONVIF doesn't reach (the Camera tab's Milesight section): its white light's
mode, brightness and schedule, its red/blue warning light, the siren's audio files and speaker volume, and one-click
disarm. device_api.py serves it; deterrence.py tells which cameras are Milesight (GetDeviceInformation).

The API: http(s)://<camera>/cgi-bin/operator/operator.cgi?action=<get.x | set.x>&format=json with HTTP digest (the
camera's login). A get is a GET and answers JSON; a set is a POST of a JSON body (Content-Type application/json) and
answers {"setState": "succeed"} (anything else is a refusal; a get the camera doesn't know answers {"setState": "failed"}).

The set payloads, as the camera's own web page builds them (firmware CQ_63.8.0.6-r2, MS-CQ8231-HYPG1: the Vue chunks of
its Image, Warning light, Audio and One-click disarm pages; their request helper posts JSON to operator.cgi):
  set.camera.white.led     the whole get.camera.white.led object back, with the fields changed (Image › White light:
                           saveWhiteLedParam spreads the object it read and sets startHour/startMin/stopHour/stopMin).
                           lightCtrl 0 auto (sensitivity 1-5, delayTime 1-60 s), 1 always on, 2 off, 3 customize (the
                           start-stop schedule); brightness 1-100 (not shown while off)
  set.warning.light.cfg    {"redBlueLight": {"mode", "frequency"}} only (Event › Warning light). mode 0 red & blue,
                           1 red, 2 blue; frequency 0 low, 1 medium, 2 high
  set.audio                the whole get.audio object back with bitRate as an integer (Audio › Audio: submit()); here only
                           speakerVolume is changed (codec / sample rate changes restart the camera's audio stream)
  set.one.click.disarm     the whole get.one.click.disarm object back: enable, disarm{...} and scheduleList (Event ›
                           One-click disarm: save() sends what it read with the schedule rebuilt)
Not written (left to the camera's page): the siren's audio file slot. The page uploads files with dataloader.cgi and
set.audio.file {"file": {index, name, path}}, and never sets audioPlayFileIndex; which file an alarm plays is chosen per
event in set.event.playaudio (admin.cgi, keyed by the event's list name). set.strobeled.flash is named in the page's
code but no page in this firmware calls it, so its payload is unknown.

Blocking (httpx sync): call from a worker thread. Errors never carry the camera's password (camdevice.clean).
"""
from __future__ import annotations

import copy
import logging
import re

import httpx

from .camdevice import Invalid, Unsupported, clean, lock
from .onvif_soap import OnvifError

log = logging.getLogger("nvr.milesight")

TIMEOUT_S = 6.0
LIGHT_MODES = {0: "auto", 1: "on", 2: "off", 3: "schedule"}
RED_BLUE_MODES = {0: "red_blue", 1: "red", 2: "blue"}
FREQUENCIES = {0: "low", 1: "medium", 2: "high"}
DISARM_KEYS = ("record", "snapshot", "output", "audio", "sip", "http", "warningLight", "autoTracking", "appPush", "mqtt")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

_transport: httpx.BaseTransport | None = None   # tests stand a fake camera in here (or run one on 127.0.0.1)


class WebApiError(OnvifError):
    """The camera's web API refused or didn't answer (502, like a camera's ONVIF error)."""


def base_url(cam: dict) -> str:
    from .camweb import web_address   # the camera's web page as this server reaches it (port-forward mode included)
    scheme, host, port = web_address(cam)
    h = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{h}:{port}/cgi-bin/operator/operator.cgi"


def _client(cam: dict) -> httpx.Client:
    return httpx.Client(auth=httpx.DigestAuth(cam.get("username") or "", cam.get("password") or ""), timeout=TIMEOUT_S,
                        verify=False, trust_env=False, follow_redirects=False, transport=_transport)


def get(c: httpx.Client, cam: dict, action: str) -> dict:
    try:
        r = c.get(base_url(cam), params={"action": action, "format": "json"})
    except httpx.HTTPError as e:
        raise WebApiError(f"no answer from the camera's web API: {clean(str(e), cam)}") from None
    if r.status_code != 200:
        raise WebApiError(f"{action}: HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise WebApiError(f"{action}: not a JSON answer") from None
    if not isinstance(data, dict) or data.get("setState") == "failed":
        raise Unsupported(f"the camera doesn't answer {action}")
    return data


def post(c: httpx.Client, cam: dict, action: str, body: dict) -> None:
    try:
        r = c.post(base_url(cam), params={"action": action, "format": "json"}, json=body)
    except httpx.HTTPError as e:
        raise WebApiError(f"no answer from the camera's web API: {clean(str(e), cam)}") from None
    try:
        data = r.json() if r.status_code == 200 else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("setState") != "succeed":
        raise WebApiError(f"the camera refused {action} (HTTP {r.status_code}{', ' + str(data.get('setState')) if isinstance(data, dict) else ''})")


# ---------------------------------------------------------------- the camera's answers -> what the UI shows

def white_light_view(raw: dict) -> dict:
    return {"supported": True, "mode": LIGHT_MODES.get(int(raw.get("lightCtrl", 0)), "auto"), "sensitivity": raw.get("sensitivity"),
            "delay_s": raw.get("delayTime"), "start": f"{int(raw.get('startHour', 0)):02d}:{int(raw.get('startMin', 0)):02d}",
            "stop": f"{int(raw.get('stopHour', 0)):02d}:{int(raw.get('stopMin', 0)):02d}", "brightness": raw.get("brightness"),
            "writable": True}


def red_blue_view(raw: dict) -> dict:
    if not raw.get("isSupportRedBlueFlashLight"):
        return {"supported": False, "reason": "the camera has no red/blue warning light"}
    rb = raw.get("redBlueLight") or {}
    return {"supported": True, "mode": RED_BLUE_MODES.get(int(rb.get("mode", 0)), "red_blue"),
            "frequency": FREQUENCIES.get(int(rb.get("frequency", 0)), "low"), "writable": True}


def siren_view(files: dict | None, audio: dict | None) -> dict:
    out: dict = {"supported": bool(files or audio), "files": [], "play_index": None, "speaker_volume": None,
                 "volume_writable": False, "file_writable": False}
    if files:
        out["files"] = [{"index": f.get("index"), "name": f.get("name") or "", "present": str(f.get("path")) not in ("0", "")}
                        for f in files.get("fileList") or [] if isinstance(f, dict)]
        out["play_index"] = files.get("audioPlayFileIndex")
    if audio:
        out["speaker_volume"] = audio.get("speakerVolume")
        out["volume_writable"] = "speakerVolume" in audio
    return out


def disarm_view(raw: dict) -> dict:
    d = raw.get("disarm") or {}
    return {"supported": True, "enable": bool(raw.get("enable")), "mutes": {k: bool(d[k]) for k in d if k in DISARM_KEYS}, "writable": True}


def _section(fn) -> dict:
    try:
        return fn()
    except Unsupported as e:
        return {"supported": False, "reason": str(e)}
    except (WebApiError, ValueError, TypeError) as e:
        return {"supported": False, "error": True, "reason": str(e)}


def read_all(cam: dict) -> dict:
    """{supported, white_light, red_blue, siren, disarm}: each section on its own ({supported: false, reason} where the
    camera can't). A camera whose web API doesn't answer at all: supported false."""
    with _client(cam) as c:
        try:
            led = get(c, cam, "get.camera.white.led")
        except Unsupported:
            led = None
        except WebApiError as e:
            return {"supported": False, "reason": clean(str(e), cam)}
        out = {"supported": True,
               "white_light": white_light_view(led) if led is not None else {"supported": False, "reason": "the camera has no white light"},
               "red_blue": _section(lambda: red_blue_view(get(c, cam, "get.warning.light.cfg"))),
               "disarm": _section(lambda: disarm_view(get(c, cam, "get.one.click.disarm")))}
        files = audio = None
        try:
            files = get(c, cam, "get.audio.file")
        except (Unsupported, WebApiError):
            pass
        try:
            audio = get(c, cam, "get.audio")
        except (Unsupported, WebApiError):
            pass
        out["siren"] = siren_view(files, audio) if files or audio else {"supported": False, "reason": "the camera has no audio output settings"}
    return out


# ---------------------------------------------------------------- writes (read-modify-write, as the camera's page does)

def _int(v, lo: int, hi: int, what: str) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise Invalid(f"{what} must be a whole number") from None
    if not lo <= n <= hi:
        raise Invalid(f"{what} must be {lo}-{hi}")
    return n


def _key(table: dict, value, what: str) -> int:
    k = next((k for k, v in table.items() if v == value), None)
    if k is None:
        raise Invalid(f"{what} must be one of {', '.join(table.values())}")
    return k


def plan_white_light(raw: dict, ch: dict) -> dict:
    """The set.camera.white.led body: the camera's object with the changes."""
    new = copy.deepcopy(raw)
    if ch.get("mode") is not None:
        new["lightCtrl"] = _key(LIGHT_MODES, ch["mode"], "mode")
    if ch.get("sensitivity") is not None:
        new["sensitivity"] = _int(ch["sensitivity"], 1, 5, "sensitivity")
    if ch.get("delay_s") is not None:
        new["delayTime"] = _int(ch["delay_s"], 1, 60, "delay")
    if ch.get("brightness") is not None:
        new["brightness"] = _int(ch["brightness"], 1, 100, "brightness")
    for field, h, m in (("start", "startHour", "startMin"), ("stop", "stopHour", "stopMin")):
        if ch.get(field) is not None:
            mt = HHMM.match(str(ch[field]).strip())
            if not mt:
                raise Invalid(f"{field} must be a time like 18:00")
            new[h], new[m] = int(mt.group(1)), int(mt.group(2))
    if (new.get("startHour"), new.get("startMin")) == (new.get("stopHour"), new.get("stopMin")) and new.get("lightCtrl") == 3:
        raise Invalid("the schedule's start and stop must differ")
    return new


def plan_red_blue(raw: dict, ch: dict) -> dict:
    if not raw.get("isSupportRedBlueFlashLight"):
        raise Unsupported("the camera has no red/blue warning light")
    rb = dict(raw.get("redBlueLight") or {})
    if ch.get("mode") is not None:
        rb["mode"] = _key(RED_BLUE_MODES, ch["mode"], "mode")
    if ch.get("frequency") is not None:
        rb["frequency"] = _key(FREQUENCIES, ch["frequency"], "frequency")
    return {"redBlueLight": {"mode": int(rb.get("mode", 0)), "frequency": int(rb.get("frequency", 0))}}


def plan_audio(raw: dict, volume) -> dict:
    if "speakerVolume" not in raw:
        raise Unsupported("the camera has no speaker volume")
    new = copy.deepcopy(raw)
    new["speakerVolume"] = _int(volume, 0, 100, "speaker volume")
    if "bitRate" in new:
        new["bitRate"] = int(new["bitRate"])   # the page sends it as an integer (parseInt)
    return new


def plan_disarm(raw: dict, ch: dict) -> dict:
    new = copy.deepcopy(raw)
    if ch.get("enable") is not None:
        new["enable"] = 1 if ch["enable"] else 0
    for k, v in (ch.get("mutes") or {}).items():
        if k not in (new.get("disarm") or {}):
            raise Invalid(f"the camera's one-click disarm has no {k!r}")
        new["disarm"][k] = 1 if v else 0
    return new


def write(cam: dict, changes: dict) -> tuple[dict, dict, dict]:
    """Apply {white_light?, red_blue?, speaker_volume?, disarm?}. Every value is checked before anything is written; each
    section is read, changed and sent back whole (the camera keeps whatever else it holds there). Returns (before, after:
    read_all, written: what was sent per action)."""
    with lock(cam["id"]), _client(cam) as c:
        steps: list[tuple[str, dict]] = []
        before: dict = {}
        if changes.get("white_light"):
            raw = get(c, cam, "get.camera.white.led")
            before["white_light"] = white_light_view(raw)
            steps.append(("set.camera.white.led", plan_white_light(raw, changes["white_light"])))
        if changes.get("red_blue"):
            raw = get(c, cam, "get.warning.light.cfg")
            before["red_blue"] = red_blue_view(raw)
            steps.append(("set.warning.light.cfg", plan_red_blue(raw, changes["red_blue"])))
        if changes.get("speaker_volume") is not None:
            raw = get(c, cam, "get.audio")
            before["speaker_volume"] = raw.get("speakerVolume")
            steps.append(("set.audio", plan_audio(raw, changes["speaker_volume"])))
        if changes.get("disarm"):
            raw = get(c, cam, "get.one.click.disarm")
            before["disarm"] = disarm_view(raw)
            steps.append(("set.one.click.disarm", plan_disarm(raw, changes["disarm"])))
        if not steps:
            raise Invalid("nothing to change")
        for action, body in steps:
            post(c, cam, action, body)
    return before, read_all(cam), dict(steps)
