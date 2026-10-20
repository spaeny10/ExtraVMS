"""Deterrence on a camera: its strobe, siren, white light and relay outputs, set off by an operator (the live tile, the
talk-down panel, the hub's SOC incident view) or by an automatic rule on a verified event. deterrence_api.py serves it.

Discovery (read-only ONVIF, once per camera and again after an edit; nothing is written to probe):
  GetServices with capabilities   the device service's Misc.AuxiliaryCommands ("tt:WarningLight|On tt:Light|Auto ..."),
                                  DeviceIO's AudioOutputs (a camera speaker that talk-down could use), the service addresses
  GetDeviceInformation            the maker: a Milesight answers its web API too (milesight.py: the Camera tab's extras)
  GetRelayOutputs (Device)        every relay with its token, mode and pulse length. DeviceIO's GetRelayOutputs is not
                                  asked: the Milesight Q series doesn't implement it, the Device one is the ONVIF core
  PTZ GetNodes                    the PTZ node's AuxiliaryCommands (Milesight lists the light there too, not the strobe)
Mapping (plan()):
  strobe   the WarningLight aux command, else a relay whose token says warning / strobe / flash / beacon / light
  siren    a siren / sounder aux command, else a relay whose token says siren / sounder / horn / buzzer
  light    the Light aux command (On, then back to Auto when the camera offers it: Off would leave a light that runs on its
           own day/night logic dark until someone fixed it on the camera's page)
  relay    any relay by token (cameras of other makers name them AlarmOut_0 / RelayOutputToken1 and so on)
The siren's way (resolve_siren(), again after every probe and on every action: speakers can be linked at any time):
  relay    the camera's own siren output (a relay such as Milesight's "Sounder", or a siren aux command) only when the
           camera can make a sound with it. A Milesight plays one of its alarm-sound file slots, and they ship empty (the
           MS-CQ8231 sounded nothing): its web API's get.audio.file is read with the probe (milesight.siren_sound) and the
           relay counts only with a loaded slot (path != "0"), the selected audioPlayFileIndex one when it names a slot.
           Another maker's siren output can't be checked: it is used as before.
  speaker  otherwise the built-in siren (siren.py) on a talk-down speaker linked to the camera (enabled; the camera's own
           speaker, at its address, first): exactly `seconds`, through talkdown.Talkdown.siren(), so it takes its turn
           with talk-down on that speaker (talkdown.py's module doc: Hold to Talk wins, a siren replaces a message, a
           rule's message waits for the siren). Switching it off or Stop ends it; a newer siren replaces it.
  none     otherwise the camera has no siren to offer (a Milesight with empty slots and no speaker).
  When a Milesight's files can't be read the speaker is preferred, the relay used when there is no speaker.
  The camera's settings (cameras.deterrence_config, clean_config(): PUT .../deterrence/rules with `config`, a config
  export / restore) can pin it: siren_source auto (the above, the default) | relay | speaker (only that way, or none).
  Every siren action's log row says which way it went (deterrence_log.via: relay / aux / speaker).
Durations: a monostable relay pulses by itself (its DelayTime); a longer time re-pulses it. A bistable relay or an aux
On gets its Off after `seconds` (DEFAULT_S, at most MAX_S). A newer action on the same output cancels that timer and
starts its own, so the last operator's choice wins. Stop turns the strobe and siren off whatever we think their state is,
and the light and other relays when this server turned them on (an always-on yard light is the site's business). Every
action on a camera goes through its lock, one SOAP call at a time.

Disarm: the Milesight tt:Disarm aux is its "one-click disarm" (get.one.click.disarm, the web page's Event › One-click
disarm): a stored setting that, while on, suppresses the camera's OWN alarm-linkage actions ticked there (record,
snapshot, alarm output, play audio, SIP, HTTP, warning light, auto tracking, app push, MQTT). It is not "mute what this
server just set off", and switching it would change the camera's configuration, so Stop never sends it.

Automatic rules (per camera, JSON in cameras.deterrence_rules; none = off, every camera's default): "when a VERIFIED
person (or vehicle) event happens [whose path entered zone X] [only while the Site is armed], strobe / siren / light for
N s and optionally play talk-down message M on a speaker linked to the camera", with a cooldown per rule. They run from
the pipeline after YOLO verification (Pipeline._verify -> verified()), never from the camera's own analytics. "Armed" is
the Site's SOC monitoring as the hub last pushed it (aischedule.config: the weekly windows, holidays and override, the
hub's own rules); a stand-alone server or one the hub never sent a schedule is never armed, so only an "always" rule
fires there.

Audit: every action is a deterrence_log row (who or which rule, the event, what was sent, ok / failed) and a log line;
through the hub the proxy audits the POST as well.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
import uuid
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from . import aischedule
from . import milesight
from . import onvif_soap as soap
from . import zones as zonelib
from .db import db
from .onvif_soap import Onvif, OnvifError, find_all

log = logging.getLogger("nvr.deterrence")

DEFAULT_S = 10
MAX_S = 120
CALL_TIMEOUT = 4.0
PROBE_RETRY_S = 600.0       # a camera that didn't answer the probe is asked again this much later
POLL_S = 30.0
ACTIONS = ("strobe", "siren", "light")
LABELS = ("person", "vehicle")
MAX_RULES = 8
DEFAULT_COOLDOWN_S = 120
LOG_KEEP = 20000            # deterrence_log rows kept (the oldest go)
REFRESH_MIN_S = 5.0         # GET .../deterrence?refresh=1 probes again at most this often
SIREN_SOURCES = ("auto", "relay", "speaker")

STROBE_AUX = re.compile(r"warning|strobe|flash|beacon", re.I)
SIREN_AUX = re.compile(r"siren|sounder|horn|buzzer", re.I)
STROBE_RELAY = re.compile(r"warn|strobe|flash|beacon|light", re.I)
SIREN_RELAY = re.compile(r"siren|sounder|horn|buzzer", re.I)
LABEL = {"strobe": "Strobe", "siren": "Siren", "light": "Light"}


class NoOutput(Exception):
    """The camera has no such output (answered 409)."""


class NotReady(Exception):
    """The camera hasn't been probed yet, or the probe failed (answered 503)."""


class Busy(Exception):
    """The siren's speaker is in use by someone talking on it (Hold to Talk wins; answered 409)."""


# ---------------------------------------------------------------- parsers (unit-tested on the probed camera's XML)

def parse_device_aux(body: ET.Element | None) -> list[str]:
    """GetServices (IncludeCapability) or GetServiceCapabilities reply -> the device's Misc AuxiliaryCommands, as the
    camera spells them ("tt:WarningLight|On", ...). A space-separated attribute, by the ONVIF schema."""
    if body is None:
        return []
    out: list[str] = []
    for el in find_all(body, "Misc"):
        out += [c for c in (el.get("AuxiliaryCommands") or "").split() if c]
    return list(dict.fromkeys(out))


def parse_audio_outputs(body: ET.Element | None) -> int:
    """DeviceIO's capability AudioOutputs="2" in a GetServices (IncludeCapability) reply; 0 when it isn't there."""
    if body is None:
        return 0
    for el in body.iter():
        v = el.get("AudioOutputs")
        if v is not None and v.strip().isdigit():
            return int(v)
    return 0


def aux_values(cmds: list[str]) -> dict[str, list[str]]:
    """["tt:Light|On", "tt:Light|Off"] -> {"tt:Light": ["On", "Off"]}; commands without a value are left out (there is
    nothing to turn back off)."""
    out: dict[str, list[str]] = {}
    for c in cmds:
        name, sep, value = c.partition("|")
        if sep and name and value:
            out.setdefault(name, []).append(value)
    return out


def _value(values: list[str], want: str) -> str | None:
    return next((v for v in values if v.lower() == want), None)


def _short(name: str) -> str:
    return name.rsplit(":", 1)[-1].lower()


def _aux_act(aux: dict[str, dict[str, list[str]]], match, restore: tuple[str, ...] = ("off",)) -> dict | None:
    """The first aux command (device service before PTZ) whose name matches, with an On value and a way back."""
    for service in ("device", "ptz"):
        for name, values in (aux.get(service) or {}).items():
            if not match(_short(name)):
                continue
            on = _value(values, "on")
            off = next((v for v in (_value(values, r) for r in restore) if v), None)
            if on and off:
                return {"via": "aux", "service": service, "target": name, "on": on, "off": off, "timed": True, "pulse_s": None}
    return None


def _relay_act(relay: dict) -> dict:
    mono = relay.get("mode") == "monostable"
    return {"via": "relay", "target": relay["token"], "mode": relay.get("mode") or "bistable", "timed": True,
            "pulse_s": (float(relay.get("delay_s") or 0) or None) if mono else None}


def plan(relays: list[dict], device_aux: list[str], ptz_aux: list[str]) -> dict[str, dict | None]:
    """{strobe, siren, light: the output to drive or None} from what the camera reported (see the module doc)."""
    aux = {"device": aux_values(device_aux), "ptz": aux_values(ptz_aux)}
    out: dict[str, dict | None] = {}
    out["strobe"] = _aux_act(aux, lambda n: bool(STROBE_AUX.search(n)))
    if out["strobe"] is None:
        r = next((r for r in relays if STROBE_RELAY.search(r["token"]) and not SIREN_RELAY.search(r["token"])), None)
        out["strobe"] = _relay_act(r) if r else None
    out["siren"] = _aux_act(aux, lambda n: bool(SIREN_AUX.search(n)))
    if out["siren"] is None:
        r = next((r for r in relays if SIREN_RELAY.search(r["token"])), None)
        out["siren"] = _relay_act(r) if r else None
    out["light"] = _aux_act(aux, lambda n: n == "light", restore=("auto", "off"))
    for k, v in out.items():
        if v:
            v["label"] = LABEL[k]
    return out


def relay_role(token: str, actions: dict) -> str | None:
    return next((k for k in ("strobe", "siren") if (actions.get(k) or {}).get("via") == "relay" and actions[k]["target"] == token), None)


# ---------------------------------------------------------------- the siren's way, and the camera's settings

class Config(BaseModel):
    """A camera's deterrence settings (cameras.deterrence_config)."""
    siren_source: Literal["auto", "relay", "speaker"] = "auto"


def clean_config(raw) -> dict:
    """Stored or sent settings, checked (ValueError), with the defaults filled in."""
    if raw in (None, ""):
        return Config().model_dump()
    if not isinstance(raw, dict):
        raise ValueError("the deterrence settings must be an object")
    return Config.model_validate(raw).model_dump()


def stored_config(cfg: dict) -> dict | None:
    """What cameras.deterrence_config keeps: None for the defaults."""
    return None if cfg == Config().model_dump() else cfg


def linked_speakers(camera_id: str, host: str | None = None) -> list[dict]:
    """The enabled talk-down speakers linked to the camera, its own (at the camera's address) first, then by id."""
    sps = [s for s in db.speakers(enabled_only=True) if camera_id in (s.get("camera_ids") or [])]
    return sorted(sps, key=lambda s: (not host or s.get("host") != host, s["id"]))


def resolve_siren(camera_act: dict | None, is_milesight: bool, sound: dict | None, speakers: list[dict],
                  source: str = "auto", host: str | None = None) -> dict:
    """Which way the siren goes (the module doc): {via: "relay" (the camera's own output: a relay or an aux command) |
    "speaker" | None, source, speaker_id, speaker, own_speaker, camera_sounds: True (a Milesight with a sound loaded) /
    False (a Milesight with none) / None (can't tell), reason}. Pure: the probe's answers and the linked speakers in."""
    sounds = (sound or {}).get("loaded") if is_milesight else None
    sp = speakers[0] if speakers else None
    base = {"source": source, "speaker_id": None, "speaker": None, "own_speaker": False, "camera_sounds": sounds,
            "has_output": camera_act is not None, "speakers": len(speakers)}

    def camera(reason: str) -> dict:
        return {**base, "via": "relay", "reason": reason}

    def speaker(reason: str) -> dict:
        return {**base, "via": "speaker", "speaker_id": sp["id"], "speaker": sp["name"],
                "own_speaker": bool(host) and sp.get("host") == host, "reason": reason}

    def none(reason: str) -> dict:
        return {**base, "via": None, "reason": reason}

    if source == "relay":
        return camera("set to the camera's own siren output") if camera_act else none("set to the camera's own siren output, and it has none")
    if source == "speaker":
        return speaker("set to a talk-down speaker") if sp else none("set to a talk-down speaker, and none is linked to this camera")
    if camera_act and sounds is True:
        return camera("the camera has a siren sound loaded")
    if camera_act and sounds is None and not is_milesight:
        return camera("the camera's own siren output")
    if sp:
        why = ("the camera's siren has no sound file loaded" if camera_act and sounds is False
               else "the camera's sound files couldn't be read" if camera_act else "the camera has no siren output")
        return speaker(why)
    if camera_act and sounds is None:
        return camera("the camera's sound files couldn't be read, and no speaker is linked")
    if camera_act:
        return none("the camera's siren has no sound file loaded, and no talk-down speaker is linked to it")
    return none("the camera has no siren output, and no talk-down speaker is linked to it")


def speaker_act(route: dict) -> dict:
    """The siren as an output on the route's speaker (what the camera's answer shows under actions.siren)."""
    name = "the camera's speaker" if route.get("own_speaker") else (route.get("speaker") or route["speaker_id"])
    return {"via": "speaker", "target": route["speaker_id"], "label": f"Siren through {name}", "timed": True, "pulse_s": None,
            "speaker_id": route["speaker_id"], "speaker": route.get("speaker"), "own_speaker": bool(route.get("own_speaker"))}


def clamp_seconds(seconds: float | None) -> float:
    if seconds is None:
        return float(DEFAULT_S)
    return float(min(MAX_S, max(1.0, float(seconds))))


def pulses_for(seconds: float, pulse_s: float | None) -> int:
    """How many pulses of a monostable relay cover `seconds` (at least one: the camera's own length)."""
    if not pulse_s:
        return 1
    return max(1, math.ceil(seconds / pulse_s - 1e-9))


def relay_body(token: str, on: bool) -> str:
    return (f"<tds:SetRelayOutputState><tds:RelayOutputToken>{escape(token)}</tds:RelayOutputToken>"
            f"<tds:LogicalState>{'active' if on else 'inactive'}</tds:LogicalState></tds:SetRelayOutputState>")


def aux_body(service: str, command: str, profile: str | None = None) -> str:
    if service == "ptz":
        return (f"<tptz:SendAuxiliaryCommand><tptz:ProfileToken>{escape(profile or '')}</tptz:ProfileToken>"
                f"<tptz:AuxiliaryData>{escape(command)}</tptz:AuxiliaryData></tptz:SendAuxiliaryCommand>")
    return f"<tds:SendAuxiliaryCommand><tds:AuxiliaryCommand>{escape(command)}</tds:AuxiliaryCommand></tds:SendAuxiliaryCommand>"


# ---------------------------------------------------------------- automatic rules

class Rule(BaseModel):
    """One automatic rule; what PUT .../deterrence/rules takes and cameras.deterrence_rules keeps."""
    id: str = Field("", max_length=16)
    name: str = Field("", max_length=60)
    enabled: bool = True
    labels: list[str] = Field(default_factory=lambda: ["person"], max_length=2)
    zone: str | None = Field(None, max_length=80)          # a named zone of the camera; None = anywhere in the picture
    when: str = "armed"                                    # always | armed (the Site's SOC monitoring is armed)
    actions: list[str] = Field(default_factory=list, max_length=3)
    seconds: float = Field(DEFAULT_S, ge=1, le=MAX_S)
    speaker_id: str | None = Field(None, max_length=32)
    message_id: int | None = None
    cooldown_s: float = Field(DEFAULT_COOLDOWN_S, ge=0, le=3600)

    @field_validator("labels")
    @classmethod
    def _labels(cls, v):
        v = [x for x in dict.fromkeys(v) if x in LABELS]
        if not v:
            raise ValueError(f"labels: at least one of {', '.join(LABELS)}")
        return v

    @field_validator("actions")
    @classmethod
    def _actions(cls, v):
        bad = [x for x in v if x not in ACTIONS]
        if bad:
            raise ValueError(f"actions: {', '.join(ACTIONS)} only, not {', '.join(map(str, bad))}")
        return list(dict.fromkeys(v))

    @field_validator("when")
    @classmethod
    def _when(cls, v):
        if v not in ("always", "armed"):
            raise ValueError("when: always or armed")
        return v

    @field_validator("zone")
    @classmethod
    def _zone(cls, v):
        return (v or "").strip() or None


def clean_rules(raw) -> list[dict]:
    """Stored or sent rules, checked (ValueError) and each given an id; at most MAX_RULES."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValueError("rules must be a list")
    if len(raw) > MAX_RULES:
        raise ValueError(f"at most {MAX_RULES} rules per camera")
    out, seen = [], set()
    for r in raw:
        rule = (r if isinstance(r, Rule) else Rule.model_validate(r)).model_dump()
        if not rule["id"] or rule["id"] in seen:
            rule["id"] = uuid.uuid4().hex[:8]
        seen.add(rule["id"])
        out.append(rule)
    return out


def armed_now(now: float | None = None) -> bool | None:
    """The Site's SOC monitoring armed now, as the hub last pushed it (aischedule.config); None = unknown (a stand-alone
    server, or nothing pushed yet)."""
    cfg = aischedule.config()
    if cfg is None:
        return None
    return aischedule.compute_armed(cfg, time.time() if now is None else now)[0]


def armed_state() -> dict:
    cfg = aischedule.config()
    if cfg is None:
        return {"known": False, "armed": None, "reason": None}
    armed, reason = aischedule.compute_armed(cfg, time.time())
    return {"known": True, "armed": armed, "reason": reason}


def zone_names(zones: list[dict] | None) -> list[str]:
    """The camera's named zones a rule can name (exclude zones hide things, they are never where something happens)."""
    return list(dict.fromkeys((z.get("name") or "").strip() for z in zonelib.normalize(zones)
                              if z["type"] != "exclude" and (z.get("name") or "").strip()))


def in_zone(path: list | None, zones: list[dict] | None, name: str) -> bool:
    """Did the object's feet (bottom-center of its box, as everywhere else) enter the named zone at any sample?"""
    polys = [z["points"] for z in zonelib.normalize(zones) if z["type"] != "exclude" and (z.get("name") or "").strip() == name]
    for p in path or []:
        if len(p) < 5:
            continue
        x, y = zonelib.foot(p[1:5])
        if any(zonelib.point_in_polygon(x, y, poly) for poly in polys):
            return True
    return False


def check_rule(rule: dict, event: dict, zones: list[dict] | None, armed: bool | None, last_fired: float | None,
               now: float) -> tuple[bool, str]:
    """(fires, why not): pure, so every gate is tested on its own."""
    if not rule.get("enabled"):
        return False, "off"
    if (event.get("status") or "") != "verified":
        return False, "not verified"
    if event.get("camera_class") not in (rule.get("labels") or ["person"]):
        return False, "label"
    if not rule.get("actions") and not (rule.get("speaker_id") and rule.get("message_id")):
        return False, "nothing to do"
    if rule.get("zone"):
        if event.get("ptz_preset"):
            return False, "camera turned away"   # the zones describe the home view
        if not in_zone(event.get("path"), zones, rule["zone"]):
            return False, "outside the zone"
    if rule.get("when") == "armed":
        if armed is None:
            return False, "armed unknown"
        if not armed:
            return False, "not armed"
    cooldown = float(rule.get("cooldown_s") if rule.get("cooldown_s") is not None else DEFAULT_COOLDOWN_S)
    if last_fired is not None and now - last_fired < cooldown:
        return False, "cooldown"
    return True, ""


# ---------------------------------------------------------------- one camera

def _log(camera_id: str, action: str, target: str | None, on: bool, seconds: float | None, by: str, source: str,
         event_id: int | None, error: str | None, via: str | None = None) -> None:
    try:
        db.execute("INSERT INTO deterrence_log (ts, camera_id, action, target, on_state, seconds, by, source, event_id, result, error, via) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [time.time(), camera_id, action, target, int(on), seconds, by, source,
                                                        event_id, "failed" if error else "ok", error, via])
    except Exception:  # noqa: BLE001 - the audit row must not undo an action already sent
        log.exception("deterrence log row for %s failed", camera_id)
    log.info("deterrence: %s %s %s%s on %s by %s%s%s%s", action, target or "", "on" if on else "off",
             f" {seconds:g} s" if on and seconds else "", camera_id, by, f" via {via}" if via else "",
             f" (event {event_id})" if event_id else "", f": FAILED {error}" if error else "")


class DeterrenceCamera:
    sleep = staticmethod(asyncio.sleep)   # tests run the timers on a faster clock

    def __init__(self, cam: dict) -> None:
        self.cam = cam
        self.onvif = Onvif.for_camera(cam, timeout=CALL_TIMEOUT)
        self.lock = asyncio.Lock()
        self.caps: dict | None = None        # None = not probed; {"available": False, "error"} = the probe failed
        self.next_probe = 0.0
        self.state: dict[str, dict] = {}     # output key -> {on, until, by, started_at}
        self.timers: dict[str, asyncio.Task] = {}
        self.siren_job: tuple[str, object, float] | None = None   # (speaker id, talkdown.Job, seconds) of a siren on a speaker

    # ---- discovery
    def _probe_sync(self) -> dict:
        o = self.onvif
        try:
            soap.sync_clock(o)
        except OnvifError:
            pass   # some cameras refuse it unauthenticated; the digest may still be accepted
        body = None
        try:
            body = o.call(o.device_url, "<tds:GetServices><tds:IncludeCapability>true</tds:IncludeCapability></tds:GetServices>")
            found = soap.parse_services(body)
            for k, addr in found.items():
                o.services[k] = o.rewrite(addr)
        except OnvifError as e:
            if "NotAuthorized" in str(e) or "401" in str(e):
                raise
        if not o.services:
            soap.discover_services(o)
        o.services.setdefault("device", o.device_url)
        device_aux = parse_device_aux(body)
        if not device_aux:
            try:
                device_aux = parse_device_aux(o.call(o.services["device"], "<tds:GetServiceCapabilities/>"))
            except OnvifError:
                pass
        info: dict = {}
        try:
            info = soap.parse_device_info(o.call(o.services["device"], "<tds:GetDeviceInformation/>"))
        except OnvifError:
            pass
        from .ptz import parse_nodes, parse_relays   # the same parsers the PTZ code uses
        relays: list[dict] = []
        try:
            relays = parse_relays(o.call(o.services["device"], "<tds:GetRelayOutputs/>"))
        except OnvifError:
            pass
        ptz_aux: list[str] = []
        profile = None
        if o.services.get("ptz"):
            try:
                ptz_aux = parse_nodes(o.call(o.services["ptz"], "<tptz:GetNodes/>")).get("aux_commands") or []
            except OnvifError:
                pass
        actions = plan(relays, device_aux, ptz_aux)
        if any(a and a.get("service") == "ptz" for a in actions.values()) and o.services.get("media"):
            try:
                profs = find_all(o.call(o.services["media"], "<trt:GetProfiles/>"), "Profiles")
                profile = next((p.get("token") for p in profs if any(soap.local(e) == "PTZConfiguration" for e in p.iter())),
                               profs[0].get("token") if profs else None)
            except OnvifError:
                pass
        is_ms = "milesight" in (info.get("manufacturer") or "").lower()
        # can its siren output make a sound? (a Milesight plays an alarm-sound file slot: read-only get.audio.file)
        sound = milesight.siren_sound(self.cam) if is_ms and actions.get("siren") else None
        return {"available": True, "manufacturer": info.get("manufacturer"), "model": info.get("model"),
                "milesight": is_ms, "relays": relays, "device_aux": device_aux, "ptz_aux": ptz_aux, "ptz_profile": profile,
                "audio_outputs": parse_audio_outputs(body), "actions": actions, "siren_sound": sound, "probed_at": time.time()}

    async def probe(self) -> dict:
        try:
            caps = await asyncio.to_thread(self._probe_sync)
        except (OnvifError, OSError, ET.ParseError) as e:
            self.caps = {"available": False, "error": str(e)[:300], "probed_at": time.time()}
            self.next_probe = time.time() + PROBE_RETRY_S
            log.info("[%s] deterrence probe failed: %s", self.cam["id"], e)
            return self.caps
        self.caps = caps
        acts = [k for k, v in caps["actions"].items() if v]
        if acts or caps["relays"]:
            snd = caps.get("siren_sound")
            log.info("[%s] deterrence: %s; %d relay(s)%s%s", self.cam["id"], ", ".join(f"{k} ({caps['actions'][k]['target']})" for k in acts) or "no mapped outputs",
                     len(caps["relays"]), "; Milesight web API" if caps["milesight"] else "",
                     "" if snd is None else "; siren sound loaded" if snd.get("loaded") else "; siren sound files empty" if snd.get("loaded") is False
                     else f"; siren sound files unreadable ({snd.get('error')})")
        return caps

    @property
    def available(self) -> bool:
        return bool(self.caps and self.caps.get("available"))

    def siren_route(self) -> dict | None:
        """Which way the siren goes now (resolve_siren); None before a successful probe."""
        if not self.available:
            return None
        source = (self.cam.get("deterrence_config") or {}).get("siren_source") or "auto"
        return resolve_siren(self.caps["actions"].get("siren"), bool(self.caps.get("milesight")), self.caps.get("siren_sound"),
                             linked_speakers(self.cam["id"], self.cam.get("host")), source, self.cam.get("host"))

    def actions_now(self) -> dict[str, dict | None]:
        """{strobe, siren, light}: the outputs to drive, the siren as it resolves now (the camera's or a speaker's)."""
        acts = dict(self.caps["actions"]) if self.available else {}
        route = self.siren_route()
        if route is not None:
            acts["siren"] = (acts.get("siren") if route["via"] == "relay" else speaker_act(route) if route["via"] == "speaker" else None)
        return acts

    def brief(self) -> dict | None:
        """What the camera list carries ({strobe, siren, light, relays}); None when there is nothing to drive."""
        if not self.available:
            return None
        a = self.actions_now()
        out = {"strobe": bool(a.get("strobe")), "siren": bool(a.get("siren")), "light": bool(a.get("light")),
               "relays": len(self.caps["relays"])}
        return out if out["strobe"] or out["siren"] or out["light"] or out["relays"] else None

    # ---- sending
    def _send_sync(self, out: dict, on: bool) -> None:
        o = self.onvif
        if out["via"] == "relay":
            url, body = o.services.get("device") or o.device_url, relay_body(out["target"], on)
        else:
            command = f"{out['target']}|{out['on'] if on else out['off']}"
            if out["service"] == "ptz":
                url, body = o.services.get("ptz"), aux_body("ptz", command, (self.caps or {}).get("ptz_profile"))
            else:
                url, body = o.services.get("device") or o.device_url, aux_body("device", command)
        if not url:
            raise OnvifError(f"camera has no {out.get('service') or 'device'} service")
        try:
            o.call(url, body)
        except OnvifError as e:
            if "NotAuthorized" in str(e) or "401" in str(e):   # clock drift kills the digest: resync once
                soap.sync_clock(o)
                o.call(url, body)
            else:
                raise

    async def _send(self, out: dict, on: bool) -> None:
        async with self.lock:
            await asyncio.to_thread(self._send_sync, out, on)

    def output(self, action: str, token: str | None = None) -> tuple[str, dict]:
        """(state key, output) for an action; NoOutput when the camera has none."""
        if not self.available:
            raise NotReady((self.caps or {}).get("error") or "camera not probed yet")
        if action == "relay":
            r = next((r for r in self.caps["relays"] if r["token"] == token), None)
            if r is None:
                raise NoOutput(f"the camera has no relay {token!r}" if token else "name the relay (token)")
            return f"relay:{r['token']}", {**_relay_act(r), "label": r["token"]}
        if action == "siren":
            route = self.siren_route()
            if route["via"] is None:
                raise NoOutput(f"no siren: {route['reason']}")
            return action, (speaker_act(route) if route["via"] == "speaker" else self.caps["actions"]["siren"])
        out = self.caps["actions"].get(action)
        if not out:
            raise NoOutput(f"the camera has no {action}")
        return action, out

    def _cancel(self, key: str) -> None:
        t = self.timers.pop(key, None)
        if t and not t.done() and t is not asyncio.current_task():
            t.cancel()

    async def _expire(self, key: str, out: dict, seconds: float, pulses: int) -> None:
        """The timer of one output: re-pulse a monostable relay, or send the Off when the time is up."""
        try:
            if out["via"] == "relay" and out.get("pulse_s"):
                for _ in range(pulses - 1):
                    await self.sleep(out["pulse_s"])
                    await self._send(out, True)
                await self.sleep(out["pulse_s"])   # the camera ends the last pulse itself
            else:
                await self.sleep(seconds)
                await self._send(out, False)
        except asyncio.CancelledError:
            return   # a newer action owns the output now
        except (OnvifError, OSError) as e:
            log.warning("[%s] deterrence: turning %s off after %g s failed: %s", self.cam["id"], out["target"], seconds, e)
        if self.timers.get(key) is asyncio.current_task():
            self.timers.pop(key, None)
            self.state[key] = {**self.state.get(key, {}), "on": False, "until": None}

    async def turn(self, action: str, on: bool = True, token: str | None = None, seconds: float | None = None,
                   by: str = "local", source: str = "manual", event_id: int | None = None, latch: bool = False) -> dict:
        """One output on (for `seconds`) or off. `latch`: a bistable relay stays on until turned off (the old relay route's
        behaviour); a pulse is still a pulse."""
        key, out = self.output(action, token)
        secs = clamp_seconds(seconds)
        self._cancel(key)
        if out["via"] == "speaker":
            return self._speaker_siren(out, on, secs, by, source, event_id)
        if action == "siren":
            self._stop_speaker_siren()   # the way changed (a setting, a file loaded): one siren at a time
        try:
            await self._send(out, on)
        except (OnvifError, OSError) as e:
            _log(self.cam["id"], action, out["target"], on, secs, by, source, event_id, str(e)[:300], out["via"])
            raise
        now = time.time()
        if on and latch and not out.get("pulse_s"):
            self.state[key] = {"on": True, "until": None, "by": by, "started_at": now}
        elif on:
            pulses = pulses_for(secs, out.get("pulse_s")) if out["via"] == "relay" else 1
            length = pulses * out["pulse_s"] if out["via"] == "relay" and out.get("pulse_s") else secs
            self.state[key] = {"on": True, "until": now + length, "by": by, "started_at": now}
            self.timers[key] = asyncio.create_task(self._expire(key, out, secs, pulses), name=f"deterrence-{self.cam['id']}-{key}")
        else:
            self.state[key] = {"on": False, "until": None, "by": by, "started_at": None}
        timed = on and not (latch and not out.get("pulse_s"))
        _log(self.cam["id"], action, out["target"], on, secs if timed else None, by, source, event_id, None, out["via"])
        return {"action": action, "on": on, "seconds": secs if timed else None, **({"token": token} if action == "relay" else {}),
                **({"via": out["via"]} if action == "siren" else {})}

    def _speaker_siren(self, out: dict, on: bool, secs: float, by: str, source: str, event_id: int | None) -> dict:
        """The siren through a linked speaker: the built-in sound for `secs` on it (talkdown.Talkdown.siren: it takes
        its turn with talk-down there), or off. Busy while someone holds to talk on it."""
        from . import talkdown   # late: talkdown pulls in the TTS engines
        sid = out["speaker_id"]
        if not on:
            self._stop_speaker_siren()
            self.state["siren"] = {"on": False, "until": None, "by": by, "started_at": None}
            _log(self.cam["id"], "siren", sid, False, None, by, source, event_id, None, "speaker")
            return {"action": "siren", "on": False, "seconds": None, "via": "speaker", "speaker_id": sid}
        sp = db.speaker(sid)
        try:
            if not sp or not sp.get("enabled"):
                raise talkdown.TalkdownError(f"the speaker {sid} is gone or turned off")
            job = talkdown.manager.siren(sp, secs, by)
        except talkdown.TalkdownError as e:
            _log(self.cam["id"], "siren", sid, True, secs, by, source, event_id, str(e)[:300], "speaker")
            if isinstance(e, talkdown.SpeakerBusy):
                raise Busy(str(e)) from None
            raise NoOutput(str(e)) from None
        now = time.time()
        self.siren_job = (sid, job, secs)
        self.state["siren"] = {"on": True, "until": now + secs, "by": by, "started_at": now}
        _log(self.cam["id"], "siren", sid, True, secs, by, source, event_id, None, "speaker")
        return {"action": "siren", "on": True, "seconds": secs, "via": "speaker", "speaker_id": sid}

    def _stop_speaker_siren(self) -> str | None:
        """End this camera's siren on its speaker if it is still the one playing there; the speaker's id when it was."""
        if not self.siren_job:
            return None
        from . import talkdown
        sid, job, _ = self.siren_job
        self.siren_job = None
        return sid if talkdown.manager.stop_job(sid, job) else None

    def _sync_speaker_siren(self) -> None:
        """A siren on a speaker ends on its own (played, replaced by Hold to Talk or a message, failed): the state follows;
        while it plays its end counts from when the speaker started it."""
        if not self.siren_job:
            return
        from . import talkdown
        sid, job, secs = self.siren_job
        st = self.state.get("siren") or {}
        if talkdown.manager.current(sid) is not job:
            self.siren_job = None
            self.state["siren"] = {**st, "on": False, "until": None}
        elif job.started_at:
            self.state["siren"] = {**st, "until": job.started_at + secs}

    async def stop(self, by: str = "local", source: str = "manual", event_id: int | None = None, quiet: bool = False) -> dict:
        """Everything off: the strobe and siren always, the light and other relays if this server turned them on."""
        if not self.available:
            raise NotReady((self.caps or {}).get("error") or "camera not probed yet")
        for key in list(self.timers):
            self._cancel(key)
        on_speaker = self._stop_speaker_siren()
        if on_speaker:
            self.state["siren"] = {"on": False, "until": None, "by": by, "started_at": None}
        targets: dict[str, dict] = {}
        for action in ("strobe", "siren"):
            if self.caps["actions"].get(action):
                targets[action] = self.caps["actions"][action]
        if self.caps["actions"].get("light") and self.state.get("light", {}).get("on"):
            targets["light"] = self.caps["actions"]["light"]
        for r in self.caps["relays"]:
            key = f"relay:{r['token']}"
            if self.state.get(key, {}).get("on") and not any(o["via"] == "relay" and o["target"] == r["token"] for o in targets.values()):
                targets[key] = _relay_act(r)
        errors = []
        for key, out in targets.items():
            try:
                await self._send(out, False)
            except (OnvifError, OSError) as e:
                errors.append(f"{out['target']}: {str(e)[:150]}")
            self.state[key] = {"on": False, "until": None, "by": by, "started_at": None}
        if not quiet:
            names = [o["target"] for o in targets.values()] + ([on_speaker] if on_speaker else [])
            vias = list(dict.fromkeys([o["via"] for o in targets.values()] + (["speaker"] if on_speaker else [])))
            _log(self.cam["id"], "stop", ", ".join(names) or None, False, None, by, source, event_id, "; ".join(errors) or None,
                 ",".join(vias) or None)
        if errors and len(errors) == len(targets):
            raise OnvifError("; ".join(errors))
        return {"action": "stop", "on": False, "seconds": None, "errors": errors}

    def public_state(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        self._sync_speaker_siren()
        out = {}
        for key, s in self.state.items():
            until = s.get("until")
            on = bool(s.get("on")) and (until is None or until > now)
            out[key] = {"on": on, "until": until if on else None, "remaining_s": round(max(0.0, until - now), 1) if on and until else None,
                        "by": s.get("by"), "started_at": s.get("started_at") if on else None}
        return out

    def public(self) -> dict:
        caps = self.caps or {}
        acts = caps.get("actions") or {}
        now_acts = self.actions_now()
        route = self.siren_route()
        last = db.one("SELECT ts, action, on_state, seconds, by, source, result, error, via FROM deterrence_log WHERE camera_id=? "
                      "ORDER BY id DESC LIMIT 1", [self.cam["id"]])
        return {
            "camera_id": self.cam["id"], "probed": self.caps is not None, "available": self.available and self.brief() is not None,
            "error": caps.get("error"), "milesight": bool(caps.get("milesight")), "audio_outputs": int(caps.get("audio_outputs") or 0),
            "default_s": DEFAULT_S, "max_s": MAX_S, "now": time.time(),
            "actions": {k: ({kk: a[kk] for kk in ("via", "target", "label", "timed", "pulse_s", "speaker_id", "speaker", "own_speaker") if kk in a}
                            if (a := now_acts.get(k)) else None) for k in ACTIONS},
            # which way the siren goes (resolve_siren) and the Milesight sound-file check behind it
            "siren": ({**route, "sound": caps.get("siren_sound")} if route else None),
            "relays": [{"token": r["token"], "mode": r["mode"], "delay_s": r.get("delay_s"), "idle_state": r.get("idle_state"),
                        "role": relay_role(r["token"], acts)} for r in caps.get("relays") or []],
            "state": self.public_state(),
            "last": ({"ts": last["ts"], "action": last["action"], "on": bool(last["on_state"]), "seconds": last["seconds"], "by": last["by"],
                      "source": last["source"], "result": last["result"], "error": last["error"], "via": last["via"]} if last else None),
        }


# ---------------------------------------------------------------- every camera, and the rules

class DeterrenceManager:
    def __init__(self) -> None:
        self.cameras: dict[str, DeterrenceCamera] = {}
        self.last_fired: dict[tuple[str, str], float] = {}
        self._tasks: set[asyncio.Task] = set()

    def sync(self, cams: list[dict]) -> None:
        wanted = {c["id"]: c for c in cams}
        for cid in list(self.cameras):
            if cid not in wanted:
                self.cameras.pop(cid)
        for cid, cam in wanted.items():
            if cid in self.cameras:
                self.cameras[cid].cam = cam
            else:
                self.cameras[cid] = DeterrenceCamera(cam)

    def reset(self, camera_id: str) -> None:
        """Another address or login: probe it again (what it can do may have changed with it). Timers of the old one
        are left to run: they still turn off what they set off, through the client that set it off."""
        cam = next((c for c in db.cameras() if c["id"] == camera_id), None)
        self.cameras.pop(camera_id, None)
        if cam and cam.get("enabled"):
            self.cameras[camera_id] = DeterrenceCamera(cam)

    def get(self, camera_id: str) -> DeterrenceCamera | None:
        return self.cameras.get(camera_id)

    def brief(self, camera_id: str) -> dict | None:
        d = self.cameras.get(camera_id)
        return d.brief() if d else None

    async def ready(self, camera_id: str) -> DeterrenceCamera:
        """The camera, probed (now, if it never was); KeyError when unknown."""
        d = self.cameras.get(camera_id)
        if d is None:
            raise KeyError(camera_id)
        if d.caps is None:
            await d.probe()
        return d

    async def run(self) -> None:
        """Probe each camera once (and again after a failure, every PROBE_RETRY_S); prune the log now and then."""
        n = 0
        while True:
            for d in list(self.cameras.values()):
                if d.caps is None or (not d.available and time.time() >= d.next_probe):
                    try:
                        await d.probe()
                    except Exception:  # noqa: BLE001 - one odd camera must not end the loop
                        log.exception("[%s] deterrence probe", d.cam["id"])
            n += 1
            if n % 120 == 1:
                try:
                    db.execute("DELETE FROM deterrence_log WHERE id <= (SELECT MAX(id) FROM deterrence_log) - ?", [LOG_KEEP])
                except Exception:  # noqa: BLE001
                    log.debug("deterrence log prune failed", exc_info=True)
            await asyncio.sleep(POLL_S)

    async def shutdown(self) -> None:
        """The server is stopping: nothing it set off may stay on (an aux On has no timer of its own on the camera)."""
        for d in list(self.cameras.values()):
            if d.available and any(s.get("on") for s in d.public_state().values()):
                try:
                    await asyncio.wait_for(d.stop(by="shutdown", quiet=True), 5)
                except Exception:  # noqa: BLE001
                    log.warning("[%s] deterrence: could not turn off at shutdown", d.cam["id"])

    # ---- automatic rules
    def verified(self, event_id: int) -> None:
        """Pipeline: an event was just verified. The rules run in their own task (camera calls must not hold the
        verifier)."""
        t = asyncio.create_task(self.on_verified(event_id), name=f"deterrence-rules-{event_id}")
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def on_verified(self, event_id: int, now: float | None = None) -> list[str]:
        """Run the camera's rules for a verified event; the ids of the rules that fired."""
        try:
            e = db.event(event_id)
            if not e or e.get("status") != "verified":
                return []
            row = next((c for c in db.cameras() if c["id"] == e["camera_id"]), None)
            rules = (row or {}).get("deterrence_rules") or []
            if not rules:
                return []
            now = time.time() if now is None else now
            armed = armed_now(now)
            fired = []
            for r in rules:
                key = (e["camera_id"], r.get("id") or "")
                ok, why = check_rule(r, e, row.get("zones"), armed, self.last_fired.get(key), now)
                if not ok:
                    if why not in ("off", "label"):
                        log.debug("event %s: deterrence rule %s not fired (%s)", event_id, r.get("name") or r.get("id"), why)
                    continue
                self.last_fired[key] = now   # also when a camera call fails: a dead camera is not hammered every event
                fired.append(r.get("id") or "")
                await self._fire(e, r)
            return fired
        except Exception:  # noqa: BLE001 - a rule must never break the pipeline
            log.exception("deterrence rules for event %s failed", event_id)
            return []

    async def _fire(self, e: dict, r: dict) -> None:
        cid = e["camera_id"]
        by = f"rule {r.get('name') or r.get('id')}"
        log.info("event %s (%s on %s): deterrence rule %s fires: %s%s", e["id"], e.get("camera_class"), cid, r.get("name") or r.get("id"),
                 ", ".join(r.get("actions") or []) or "no outputs", f" + message {r['message_id']} on {r['speaker_id']}" if r.get("speaker_id") and r.get("message_id") else "")
        if r.get("actions"):
            try:
                d = await self.ready(cid)
            except KeyError:
                d = None
            for action in r["actions"]:
                if d is None:
                    break
                try:
                    await d.turn(action, True, seconds=r.get("seconds"), by=by, source="rule", event_id=e["id"])
                except NoOutput:
                    pass   # the camera lost that output (another model at the address): nothing to log against it
                except (Busy, NotReady, OnvifError, OSError) as ex:   # Busy: someone holds to talk on the siren's speaker
                    log.warning("event %s: deterrence %s on %s failed: %s", e["id"], action, cid, ex)
        if r.get("speaker_id") and r.get("message_id"):
            self._talkdown(cid, r, e["id"], by)

    @staticmethod
    def _talkdown(camera_id: str, r: dict, event_id: int, by: str) -> None:
        from . import talkdown   # late: talkdown pulls in the TTS engines
        sp = db.speaker(r["speaker_id"])
        if not sp or not sp.get("enabled") or camera_id not in (sp.get("camera_ids") or []):
            log.warning("event %s: deterrence rule's speaker %s is gone, off or not linked to %s", event_id, r["speaker_id"], camera_id)
            return
        m = talkdown.message(int(r["message_id"]))
        if not m:
            log.warning("event %s: deterrence rule's message %s is gone", event_id, r["message_id"])
            return
        if m.get("file"):
            msg = talkdown.Message(m["name"], path=talkdown.talkdown_dir() / m["file"], message_id=m["id"])
        else:
            msg = talkdown.Message(m["name"], text=m["text"], message_id=m["id"], cache=True, voice=talkdown.voice_for(m))
        try:
            # after_siren: on the speaker sounding this rule's siren the message plays when the siren ends
            talkdown.manager.play(sp, msg, by, after_siren=True)
        except Exception as ex:  # noqa: BLE001
            log.warning("event %s: deterrence rule's talk-down on %s failed: %s", event_id, sp["id"], ex)


manager = DeterrenceManager()
