"""PTZ, relay output and digital input over ONVIF, and where a PTZ camera is pointing.

Analytics on a PTZ camera only make sense at one view: the operator marks a preset as *home*, and zones,
named places, painted regions and the learned baseline apply there. Events while the camera is turned away
are still recorded and verified, but tagged with the preset they were captured at (or "away").

"Where is it": preset positions reported by GetPresets are in degrees while GetStatus is normalized, so they
can't be compared. Instead, after each GotoPreset completes we capture the normalized position into
ptz_config.preset_pos[token]; the live position within POS_TOL of a stored one means "at that preset".

Move protocol: the UI re-sends ContinuousMove (camera timeout PT2S) every 0.5 s while a control is held and
sends Stop on release; here a per-camera lock + sequence number coalesce a burst so only the newest velocity
is sent, and a watchdog stops the camera if the re-sends stop arriving (tab closed mid-drag).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from . import onvif_soap as soap
from .db import db
from .onvif_soap import Onvif, OnvifError, find, find_all, text

log = logging.getLogger("nvr.ptz")

POS_TOL = 0.02          # normalized pan/tilt/zoom distance that still counts as "at" a preset
MOVE_TIMEOUT = "PT2S"   # the camera keeps moving this long per ContinuousMove; the UI re-sends every 0.5 s
WATCHDOG_S = 3.0        # no re-send for this long -> Stop (lost pointerup / tab closed)
POLL_S = 5.0            # GetStatus cadence
CALL_TIMEOUT = 3.0
PROBE_RETRY_S = 60.0
WAIT_IDLE_S = 12.0      # longest we wait for a GotoPreset to finish
SYSTEM_PRESET_MIN_TOKEN = 33
SYSTEM_PRESET_NAMES = ("auto flip", "goto zero", "self check", "auto scan", "pattern", "tour", "day mode",
                       "night mode", "ir on", "ir off", "wiper", "reboot", "manual limit", "scan limit")
DEFAULT_CFG = {"home_token": None, "home_name": None, "home_pos": None, "return_home_min": 5,
               "relay_label": "Relay", "input_label": "Input", "preset_pos": {}}

SP = "http://www.onvif.org/ver10/tptz"
VEL_PT, VEL_Z = f"{SP}/PanTiltSpaces/VelocityGenericSpace", f"{SP}/ZoomSpaces/VelocityGenericSpace"
TR_PT, TR_Z = f"{SP}/PanTiltSpaces/TranslationGenericSpace", f"{SP}/ZoomSpaces/TranslationGenericSpace"


# ---------------------------------------------------------------- pure helpers (unit-tested)

def at_position(pos: dict | None, ref: dict | None, tol: float = POS_TOL) -> bool:
    if not pos or not ref:
        return False
    return all(abs(float(pos.get(k, 0)) - float(ref.get(k, 0))) <= tol for k in ("x", "y", "zoom"))


def nearest_preset(pos: dict | None, preset_pos: dict[str, dict], tol: float = POS_TOL) -> str | None:
    """Token of the stored preset position the camera is at (closest if several), else None."""
    best, best_d = None, None
    for token, ref in (preset_pos or {}).items():
        if at_position(pos, ref, tol):
            d = max(abs(float(pos[k]) - float(ref.get(k, 0))) for k in ("x", "y", "zoom"))
            if best_d is None or d < best_d:
                best, best_d = token, d
    return best


def is_system_preset(token: str, name: str | None) -> bool:
    try:
        if int(token) >= SYSTEM_PRESET_MIN_TOKEN:
            return True
    except (TypeError, ValueError):
        pass
    return (name or "").strip().lower().startswith(SYSTEM_PRESET_NAMES)


def should_return_home(*, at_home: bool, moving: bool, last_command_at: float | None, return_home_min: int,
                       now: float, home_token: str | None) -> bool:
    if not home_token or not return_home_min or at_home or moving:
        return False
    return now - (last_command_at or 0) >= return_home_min * 60


def relative_for_click(dx: float, dy: float, zoom: float | None) -> tuple[float, float]:
    """Pan/tilt translation for a click dx,dy from the picture center (fractions of the content rect,
    right/down positive). The gain shrinks with zoom (a click near the edge means a smaller angle when
    zoomed in); ONVIF tilt is up-positive so dy is flipped. Tune the sign here if a camera pans the wrong way."""
    z = min(max(float(zoom or 0.0), 0.0), 1.0)
    gain = 0.5 * (1 - 0.8 * z)
    return round(2 * dx * gain, 4), round(-2 * dy * gain, 4)


def away_between(moves: list[tuple[float, bool, str | None]], t0: float, t1: float) -> str | None:
    """From a log of (ts, at_home, label) changes: the first non-home label the camera showed during
    [t0, t1], or None if it stayed home (or the log says nothing about that time)."""
    state = None
    for ts, at_home, label in moves:
        if ts <= t0:
            state = (at_home, label)
        elif ts <= t1:
            if state and not state[0]:
                return state[1] or "away"
            state = (at_home, label)
        else:
            break
    if state and not state[0]:
        return state[1] or "away"
    return None


# ---------------------------------------------------------------- SOAP parsers

def parse_status(body: ET.Element) -> dict:
    pt, z = find(body, "PanTilt"), find(body, "Zoom")
    pos = None
    if pt is not None and pt.get("x") is not None:
        pos = {"x": round(float(pt.get("x")), 4), "y": round(float(pt.get("y", 0)), 4),
               "zoom": round(float(z.get("x")), 4) if z is not None and z.get("x") is not None else 0.0}
    ms = find(body, "MoveStatus")
    moving = any((c.text or "").strip().upper() == "MOVING" for c in (list(ms) if ms is not None else []))
    return {"position": pos, "moving": moving, "utc": text(body, "UtcTime")}


def parse_presets(body: ET.Element) -> list[dict]:
    out = []
    for p in find_all(body, "Preset"):
        token = p.get("token") or ""
        name = text(p, "Name") or token
        out.append({"token": token, "name": name, "system": is_system_preset(token, name)})
    return out


def parse_nodes(body: ET.Element) -> dict:
    node = find(body, "PTZNode")
    if node is None:
        return {}
    return {
        "home_supported": (text(node, "HomeSupported") or "").lower() == "true",
        "max_presets": int(text(node, "MaximumNumberOfPresets") or 0),
        "aux_commands": [a.text.strip() for a in find_all(node, "AuxiliaryCommands") if a.text],
        "continuous": find(node, "ContinuousPanTiltVelocitySpace") is not None,
        "relative": find(node, "RelativePanTiltTranslationSpace") is not None,
        "absolute": find(node, "AbsolutePanTiltPositionSpace") is not None,
        "zoom": find(node, "ContinuousZoomVelocitySpace") is not None or find(node, "AbsoluteZoomPositionSpace") is not None,
        "tours": find(node, "SupportedPresetTour") is not None,
    }


def parse_relays(body: ET.Element) -> list[dict]:
    return [{"token": r.get("token") or "", "mode": (text(r, "Mode") or "Bistable").lower(),
             "delay_s": soap.parse_duration(text(r, "DelayTime")), "idle_state": text(r, "IdleState")}
            for r in find_all(body, "RelayOutputs")]


def parse_digital_inputs(body: ET.Element) -> list[str]:
    return [d.get("token") or "" for d in find_all(body, "DigitalInputs")]


def _f(v: float) -> str:
    return f"{max(-1.0, min(1.0, float(v))):.3f}"


# ---------------------------------------------------------------- one camera

class PtzCamera:
    def __init__(self, cam: dict) -> None:
        self.cam = cam
        self.onvif = Onvif(cam["host"], cam["onvif_port"], cam["username"], cam["password"], timeout=CALL_TIMEOUT)
        self.lock = asyncio.Lock()
        self.caps: dict | None = None          # None = not probed yet; {"available": False} = no PTZ
        self.profile: str | None = None
        self.presets: list[dict] = []
        self.relays: list[dict] = []
        self.inputs: list[str] = []
        self.status: dict = {"position": None, "moving": False, "at_home": False, "preset": None, "preset_name": None,
                             "last_command_at": None, "last_error": None, "polled_at": None}
        self.relay_state: bool | None = None
        self.relay_changed_at: float | None = None
        self.input_state: bool | None = None
        self.input_changed_at: float | None = None
        self.moves: deque = deque(maxlen=2000)   # (ts, at_home, label)
        self._seq = 0
        self._move_deadline: float | None = None
        self._next_probe = 0.0
        self._last_state: tuple | None = None

    # ---- config
    @property
    def cfg(self) -> dict:
        c = self.cam.get("ptz_config") or {}
        out = {**DEFAULT_CFG, **c}
        out["preset_pos"] = dict(out.get("preset_pos") or {})
        return out

    def save_cfg(self, cfg: dict) -> None:
        self.cam["ptz_config"] = cfg
        db.set_ptz_config(self.cam["id"], cfg)

    @property
    def available(self) -> bool:
        return bool(self.caps and self.caps.get("available"))

    def preset_name(self, token: str | None) -> str | None:
        return next((p["name"] for p in self.presets if p["token"] == token), None) if token else None

    # ---- transport
    def _url(self, key: str) -> str:
        url = self.onvif.device_url if key == "device" else self.onvif.services.get(key)
        if not url:
            raise OnvifError(f"camera has no {key} service")
        return url

    def _send_sync(self, key: str, body: str) -> ET.Element:
        try:
            r = self.onvif.call(self._url(key), body)
        except OnvifError as e:
            msg = str(e)
            if "NotAuthorized" in msg or "Sender" in msg or "401" in msg:  # clock drift kills the digest: resync once
                soap.sync_clock(self.onvif)
                r = self.onvif.call(self._url(key), body)
            else:
                self.status["last_error"] = msg
                raise
        self.status["last_error"] = None
        return r

    async def _call(self, key: str, body: str) -> ET.Element:
        async with self.lock:
            return await asyncio.to_thread(self._send_sync, key, body)

    def _touch(self) -> None:
        self.status["last_command_at"] = time.time()

    # ---- discovery
    async def probe(self) -> None:
        def work() -> dict:
            soap.sync_clock(self.onvif)
            soap.discover_services(self.onvif)
            if "ptz" not in self.onvif.services:
                return {"available": False}
            profiles = self.onvif.call(self._url("media"), "<trt:GetProfiles/>")
            profs = find_all(profiles, "Profiles")
            profile = next((p.get("token") for p in profs if find(p, "PTZConfiguration") is not None),
                           profs[0].get("token") if profs else None)
            if not profile:
                return {"available": False, "error": "no media profile"}
            caps = parse_nodes(self.onvif.call(self._url("ptz"), "<tptz:GetNodes/>"))
            cfgs = self.onvif.call(self._url("ptz"), "<tptz:GetConfigurations/>")
            caps["default_timeout_s"] = soap.parse_duration(text(cfgs, "DefaultPTZTimeout"))
            presets = parse_presets(self.onvif.call(self._url("ptz"), f"<tptz:GetPresets><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken></tptz:GetPresets>"))
            # A fixed camera with a motorized lens also answers the PTZ service (zoom + focus only). Without pan/tilt
            # or presets there is nothing to steer: no PTZ mode, no home view, never "away".
            caps["pan_tilt"] = bool(caps["continuous"] or caps["relative"] or caps["absolute"] or presets)
            relays, inputs = [], []
            try:
                relays = parse_relays(self.onvif.call(self._url("device"), "<tds:GetRelayOutputs/>"))
            except OnvifError:
                pass
            try:
                if "deviceio" in self.onvif.services:
                    inputs = parse_digital_inputs(self.onvif.call(self._url("deviceio"), "<tmd:GetDigitalInputs/>"))
            except OnvifError:
                pass
            return {"available": True, "profile": profile, "caps": caps, "presets": presets, "relays": relays, "inputs": inputs}
        try:
            r = await asyncio.to_thread(work)
        except (OnvifError, OSError) as e:
            self.caps = {"available": False, "error": str(e)}
            self._next_probe = time.time() + PROBE_RETRY_S
            self.status["last_error"] = str(e)
            log.warning("[%s] PTZ probe failed: %s", self.cam["id"], e)
            return
        if not r.get("available"):
            self.caps = {"available": False, **({"error": r["error"]} if r.get("error") else {})}
            self._next_probe = time.time() + PROBE_RETRY_S
            return
        self.profile, self.presets, self.relays, self.inputs = r["profile"], r["presets"], r["relays"], r["inputs"]
        self.caps = {"available": True, **r["caps"], "relays": len(self.relays), "inputs": len(self.inputs)}
        self.status["last_error"] = None
        log.info("[%s] PTZ: %d presets, home %s, %d relay(s), %d input(s)", self.cam["id"], len(self.presets),
                 self.cfg.get("home_name") or "not set", len(self.relays), len(self.inputs))

    # ---- movement
    def _pt(self) -> str:
        return f"<tptz:ProfileToken>{escape(self.profile or '')}</tptz:ProfileToken>"

    async def move(self, pan: float, tilt: float, zoom: float) -> dict:
        if not pan and not tilt and not zoom:
            return await self.stop()
        self._seq += 1
        my = self._seq
        async with self.lock:
            if my != self._seq:
                return {"coalesced": True}  # a newer velocity is already waiting
            vel = ""
            if pan or tilt:
                vel += f'<tt:PanTilt x="{_f(pan)}" y="{_f(tilt)}" space="{VEL_PT}"/>'
            if zoom:
                vel += f'<tt:Zoom x="{_f(zoom)}" space="{VEL_Z}"/>'
            body = (f"<tptz:ContinuousMove>{self._pt()}<tptz:Velocity>{vel}</tptz:Velocity>"
                    f"<tptz:Timeout>{MOVE_TIMEOUT}</tptz:Timeout></tptz:ContinuousMove>")
            await asyncio.to_thread(self._send_sync, "ptz", body)
        self._move_deadline = time.time() + WATCHDOG_S
        self.status["moving"] = True
        self._touch()
        return {"ok": True}

    async def stop(self) -> dict:
        self._seq += 1
        self._move_deadline = None
        await self._call("ptz", f"<tptz:Stop>{self._pt()}<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>")
        self._touch()
        return {"ok": True}

    async def relative(self, dx: float = 0.0, dy: float = 0.0, zoom: float = 0.0) -> dict:
        pos = self.status.get("position") or {}
        x, y = relative_for_click(dx, dy, pos.get("zoom"))
        tr = ""
        if x or y:
            tr += f'<tt:PanTilt x="{_f(x)}" y="{_f(y)}" space="{TR_PT}"/>'
        if zoom:
            tr += f'<tt:Zoom x="{_f(zoom)}" space="{TR_Z}"/>'
        if not tr:
            return {"ok": True}
        await self._call("ptz", f"<tptz:RelativeMove>{self._pt()}<tptz:Translation>{tr}</tptz:Translation></tptz:RelativeMove>")
        self._touch()
        return {"ok": True}

    async def _wait_idle(self) -> None:
        deadline = time.time() + WAIT_IDLE_S
        await asyncio.sleep(0.6)  # let the move start before believing an IDLE
        while time.time() < deadline:
            await self.refresh_status(log_moves=False)
            if not self.status["moving"]:
                break
            await asyncio.sleep(0.3)
        # Milesight reports a transient position right after a preset move: read again once settled
        await asyncio.sleep(1.0)
        await self.refresh_status(log_moves=False)

    async def goto_preset(self, token: str, wait: bool = False) -> dict:
        await self._call("ptz", f"<tptz:GotoPreset>{self._pt()}<tptz:PresetToken>{escape(token)}</tptz:PresetToken></tptz:GotoPreset>")
        self._touch()
        if wait:
            await self._wait_idle()
            if self.status.get("position"):
                cfg = self.cfg
                cfg["preset_pos"][token] = self.status["position"]
                self.save_cfg(cfg)
            await self.refresh_status()
        return {"ok": True}

    async def home(self) -> dict:
        token = self.cfg.get("home_token")
        if token:
            return await self.goto_preset(token, wait=True)
        await self._call("ptz", f"<tptz:GotoHomePosition>{self._pt()}</tptz:GotoHomePosition>")
        self._touch()
        return {"ok": True}

    async def refresh_presets(self) -> list[dict]:
        self.presets = parse_presets(await self._call("ptz", f"<tptz:GetPresets>{self._pt()}</tptz:GetPresets>"))
        return self.presets

    async def set_preset(self, name: str, token: str | None = None) -> str:
        body = (f"<tptz:SetPreset>{self._pt()}<tptz:PresetName>{escape(name)}</tptz:PresetName>"
                + (f"<tptz:PresetToken>{escape(token)}</tptz:PresetToken>" if token else "") + "</tptz:SetPreset>")
        r = await self._call("ptz", body)
        new_token = text(r, "PresetToken") or token or ""
        await self.refresh_presets()
        await self.refresh_status(log_moves=False)
        if self.status.get("position") and new_token:
            cfg = self.cfg
            cfg["preset_pos"][new_token] = self.status["position"]
            if cfg.get("home_token") == new_token:
                cfg["home_name"], cfg["home_pos"] = name, self.status["position"]
            self.save_cfg(cfg)
        await self.refresh_status()
        return new_token

    async def rename_preset(self, token: str, name: str) -> None:
        await self.goto_preset(token, wait=True)  # SetPreset overwrites the position with the current one
        await self.set_preset(name, token)

    async def remove_preset(self, token: str) -> None:
        await self._call("ptz", f"<tptz:RemovePreset>{self._pt()}<tptz:PresetToken>{escape(token)}</tptz:PresetToken></tptz:RemovePreset>")
        await self.refresh_presets()
        cfg = self.cfg
        cfg["preset_pos"].pop(token, None)
        if cfg.get("home_token") == token:
            cfg["home_token"] = cfg["home_name"] = cfg["home_pos"] = None
        self.save_cfg(cfg)
        await self.refresh_status()

    async def set_home(self, token: str | None) -> None:
        cfg = self.cfg
        if not token:
            cfg["home_token"] = cfg["home_name"] = cfg["home_pos"] = None
            self.save_cfg(cfg)
            await self.refresh_status()
            return
        await self.goto_preset(token, wait=True)  # also captures preset_pos[token]
        cfg = self.cfg
        cfg["home_token"], cfg["home_name"], cfg["home_pos"] = token, self.preset_name(token) or token, cfg["preset_pos"].get(token)
        self.save_cfg(cfg)
        await self.refresh_status()

    def set_config(self, **fields) -> dict:
        cfg = self.cfg
        for k in ("return_home_min", "relay_label", "input_label"):
            if fields.get(k) is not None:
                cfg[k] = fields[k]
        self.save_cfg(cfg)
        return cfg

    # ---- relay / input
    async def set_relay(self, on: bool) -> dict:
        if not self.relays:
            raise OnvifError("camera has no relay output")
        relay = self.relays[0]
        if relay["mode"] == "monostable":
            on = True  # a pulse: the camera returns it to idle after delay_s
        state = "active" if on else "inactive"
        await self._call("device", f"<tds:SetRelayOutputState><tds:RelayOutputToken>{escape(relay['token'])}</tds:RelayOutputToken>"
                                   f"<tds:LogicalState>{state}</tds:LogicalState></tds:SetRelayOutputState>")
        self.relay_state, self.relay_changed_at = on, time.time()
        return {"state": on, "mode": relay["mode"]}

    def on_io_event(self, topic: str, state: bool | None, ts: float) -> None:
        if state is None:
            return
        if "DigitalInput" in topic:
            if self.input_state != state:
                self.input_state, self.input_changed_at = state, ts
        elif "Relay" in topic:
            self.relay_state, self.relay_changed_at = state, ts

    # ---- status
    async def refresh_status(self, log_moves: bool = True) -> dict:
        st = parse_status(await self._call("ptz", f"<tptz:GetStatus>{self._pt()}</tptz:GetStatus>"))
        cfg = self.cfg
        preset = nearest_preset(st["position"], cfg["preset_pos"])
        home = cfg.get("home_token")
        at_home = bool(home) and preset == home
        self.status.update(position=st["position"], moving=st["moving"], at_home=at_home, preset=preset,
                           preset_name=self.preset_name(preset), polled_at=time.time())
        if home and not st["moving"] and log_moves:
            label = None if at_home else (self.preset_name(preset) or "away")
            key = (at_home, label)
            if key != self._last_state:
                self._last_state = key
                now = time.time()
                self.moves.append((now, at_home, label))
                db.execute("INSERT INTO ptz_moves (camera_id, ts, at_home, preset) VALUES (?,?,?,?)",
                           [self.cam["id"], now, int(at_home), label])
        return self.status

    @property
    def pan_tilt(self) -> bool:
        return bool(self.caps and self.caps.get("pan_tilt", True))

    def away_label(self) -> str | None:
        """None while at home (or when we can't tell); else the preset name or 'away'."""
        if not self.available or not self.pan_tilt or not self.cfg.get("home_token") or self.status.get("position") is None:
            return None
        if self.status["at_home"]:
            return None
        return self.status.get("preset_name") or "away"

    def public(self) -> dict:
        cfg = self.cfg
        relay = self.relays[0] if self.relays else None
        return {
            "available": self.available, "pan_tilt": self.pan_tilt, "at_home": self.status["at_home"], "moving": self.status["moving"],
            "preset": self.status["preset"], "preset_name": self.status["preset_name"], "position": self.status["position"],
            "home_token": cfg.get("home_token"), "home_name": cfg.get("home_name"), "last_error": self.status["last_error"],
            "relay": {"label": cfg["relay_label"], "state": self.relay_state, "mode": relay["mode"], "changed_at": self.relay_changed_at} if relay else None,
            "input": {"label": cfg["input_label"], "state": self.input_state, "changed_at": self.input_changed_at} if self.inputs else None,
        }

    def info(self) -> dict:
        cfg = self.cfg
        return {"caps": self.caps, "status": self.public(),
                "presets": [{**p, "is_home": p["token"] == cfg.get("home_token"), "known": p["token"] in cfg["preset_pos"]} for p in self.presets],
                "config": {k: cfg[k] for k in ("home_token", "home_name", "return_home_min", "relay_label", "input_label")}}


# ---------------------------------------------------------------- all cameras

class PtzManager:
    def __init__(self) -> None:
        self.cameras: dict[str, PtzCamera] = {}

    def sync(self, cams: list[dict]) -> None:
        wanted = {c["id"]: c for c in cams}
        for cid in list(self.cameras):
            if cid not in wanted:
                del self.cameras[cid]
        for cid, cam in wanted.items():
            if cid not in self.cameras:
                self.cameras[cid] = PtzCamera(cam)
            else:
                self.cameras[cid].cam = cam  # ptz_config and names may have changed

    def reset(self, cid: str) -> None:
        cam = next((c for c in db.cameras() if c["id"] == cid), None)
        if cam:
            self.cameras[cid] = PtzCamera(cam)

    def get(self, cid: str) -> PtzCamera | None:
        return self.cameras.get(cid)

    def status(self, cid: str) -> dict | None:
        p = self.cameras.get(cid)
        return p.public() if p and p.available else None

    def away_preset(self, cid: str) -> str | None:
        p = self.cameras.get(cid)
        return p.away_label() if p else None

    def away_between(self, cid: str, t0: float, t1: float) -> str | None:
        p = self.cameras.get(cid)
        return away_between(list(p.moves), t0, t1) if p else None

    def io_event(self, cid: str, topic: str, state: bool | None, ts: float) -> None:
        p = self.cameras.get(cid)
        if p:
            p.on_io_event(topic, state, ts)

    async def run(self) -> None:
        while True:
            for p in list(self.cameras.values()):
                try:
                    await self._tick(p)
                except (OnvifError, OSError) as e:
                    p.status["last_error"] = str(e)
                except Exception:
                    log.exception("[%s] PTZ poll failed", p.cam["id"])
            await asyncio.sleep(POLL_S)

    async def _tick(self, p: PtzCamera) -> None:
        if p.caps is None or (not p.available and p.caps.get("error") and time.time() >= p._next_probe):
            await p.probe()
        if not p.available:
            return
        if p._move_deadline and time.time() > p._move_deadline:
            log.info("[%s] PTZ watchdog: stopping a move nobody renewed", p.cam["id"])
            await p.stop()
        await p.refresh_status()
        cfg = p.cfg
        if should_return_home(at_home=p.status["at_home"], moving=p.status["moving"], last_command_at=p.status["last_command_at"],
                              return_home_min=int(cfg.get("return_home_min") or 0), now=time.time(), home_token=cfg.get("home_token")):
            log.info("[%s] PTZ idle for %s min: returning home", p.cam["id"], cfg["return_home_min"])
            await p.goto_preset(cfg["home_token"], wait=True)
