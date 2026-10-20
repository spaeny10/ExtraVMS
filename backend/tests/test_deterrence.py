"""Deterrence (deterrence.py, deterrence_api.py, milesight.py) without a camera: discovery on the probed Milesight
MS-CQ8231's own ONVIF answers (tests/fixtures/milesight_cq8231, its address replaced by TEST-NET 192.0.2.75), the action
mapping for it and for other makers' relays, aux On -> Off timing with a newer action cancelling the old timer, monostable
pulses and bistable relays, Stop, the automatic rules (verified event -> action, cooldown, armed gating, zones, off by
default), the routes with hub roles, the old relay route on a camera without PTZ, a camera's own speaker for talk-down,
and the Milesight web API requests against a fake camera on 127.0.0.1 (digest auth, get / set bodies as its page sends).

Every ONVIF call goes to a fake (onvif_soap.Onvif.call is replaced before anything runs); nothing reaches a real camera.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_deterrence.py   (from backend/)
"""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET

TMP = Path(tempfile.mkdtemp(prefix="nvr-deterrence-test-"))
for k, sub in (("NVR_DATA_DIR", "data"), ("NVR_RECORDINGS_DIR", "recordings"), ("NVR_RUNTIME_DIR", "runtime")):
    os.environ[k] = str(TMP / sub)   # never the real server's database, recordings or mediamtx.yml
os.environ["NVR_ALLOWED_HOSTS"] = "site"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tunnelproto"))

import httpx  # noqa: E402

from nvr import onvif_soap  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "milesight_cq8231"
XML = {name: (FIX / f"{name}.xml").read_text(encoding="utf-8") for name in ("services", "dev_relays", "ptz_nodes", "info")}
EMPTY_TIME = '<GetSystemDateAndTimeResponse xmlns="http://www.onvif.org/ver10/device/wsdl"/>'
sent: list[tuple[str, str]] = []      # (operation, body) of every ONVIF call
refuse: set[str] = set()              # operations the fake camera faults


def fake_call(self, url, body, auth=True, header="", timeout=None):
    assert "192.168." not in (url or "") and "192.168." not in str(self.host), "a test must never reach a LAN camera"
    tag = body.split("<", 2)[1].split(">", 1)[0].split(" ", 1)[0].split(":", 1)[-1].rstrip("/")
    sent.append((tag, body))
    if tag in refuse:
        raise onvif_soap.OnvifError(f"{tag} refused (ter:ActionNotSupported)")
    canned = {"GetServices": XML["services"], "GetDeviceInformation": XML["info"], "GetRelayOutputs": XML["dev_relays"],
              "GetNodes": XML["ptz_nodes"], "GetSystemDateAndTime": EMPTY_TIME}
    return ET.fromstring(canned.get(tag) or f'<{tag}Response xmlns="http://www.onvif.org/ver10/device/wsdl"/>')


onvif_soap.Onvif.call = fake_call   # before the app is imported: every ONVIF call in this file hits the fake

from nvr import aischedule, api, deterrence, hub_agent, milesight, ptz  # noqa: E402
from nvr.api import app  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402

for d in (settings.data_dir, settings.recordings_dir, settings.runtime_dir):
    v = str(d).replace("\\", "/").lower()
    assert "nvr-deterrence-test-" in v and "e:/nvr" not in v and "d:/nvr" not in v, d

FAST = 200.0   # the timers run this much faster than the wall clock
deterrence.DeterrenceCamera.sleep = staticmethod(lambda s: asyncio.sleep(s / FAST))
PW = "cam-" + "q" * 9
CAM = {"id": "cam7", "name": "Q Series", "host": "192.0.2.75", "onvif_port": 80, "rtsp_port": 554, "username": "admin",
       "password": PW, "main_path": "/main", "sub_path": "/sub", "enabled": 1,
       "zones": [{"name": "Yard", "type": "area", "points": [[0, 0.5], [1, 0.5], [1, 1], [0, 1]]},
                 {"name": "Hidden", "type": "exclude", "points": [[0, 0], [0.1, 0], [0.1, 0.1]]}]}
db.upsert_camera(CAM)
LAN = ("192.0.2.9", 5000)


def ops(name: str) -> list[str]:
    return [b for t, b in sent if t == name]


def aux_sent() -> list[str]:
    return [b.split("<tds:AuxiliaryCommand>", 1)[1].split("<", 1)[0] for b in ops("SendAuxiliaryCommand")]


def relay_sent() -> list[tuple[str, str]]:
    out = []
    for b in ops("SetRelayOutputState"):
        token = b.split("<tds:RelayOutputToken>", 1)[1].split("<", 1)[0]
        out.append((token, b.split("<tds:LogicalState>", 1)[1].split("<", 1)[0]))
    return out


async def probed() -> deterrence.DeterrenceCamera:
    d = deterrence.DeterrenceCamera(dict(CAM))
    await d.probe()
    sent.clear()
    return d


def call(method, path, body=None, role=None, user="op@example"):
    async def go():
        tunnel = role is not None
        transport = httpx.ASGITransport(app=hub_agent.as_tunnel(app) if tunnel else app, client=hub_agent.IN_PROCESS_CLIENT if tunnel else LAN)
        headers = {"x-hub-role": role, "x-hub-user": user} if tunnel else {}
        async with httpx.AsyncClient(transport=transport, base_url="http://site", timeout=30) as c:
            return await c.request(method, path, json=body, headers=headers)
    return asyncio.run(go())


# ---------------------------------------------------------------- discovery and mapping

def test_discovery_from_the_probed_camera():
    services = ET.fromstring(XML["services"])
    aux = deterrence.parse_device_aux(services)
    assert aux == ["tt:Disarm|Off", "tt:Disarm|On", "tt:IRLamp|On", "tt:IRLamp|Off", "tt:IRLamp|Auto", "tt:Light|Off", "tt:Light|On",
                   "tt:Light|Auto", "tt:LightSmartSwitch|On", "tt:WarningLight|Off", "tt:WarningLight|On"]
    assert deterrence.parse_audio_outputs(services) == 2
    relays = ptz.parse_relays(ET.fromstring(XML["dev_relays"]))
    assert [(r["token"], r["mode"], r["delay_s"]) for r in relays] == [("AlarmOut_0", "bistable", 10.0), ("Warning Light", "monostable", 10.0),
                                                                      ("Sounder", "monostable", 10.0)]
    ptz_aux = ptz.parse_nodes(ET.fromstring(XML["ptz_nodes"]))["aux_commands"]
    assert "tt:Light|Auto" in ptz_aux and not any("WarningLight" in c for c in ptz_aux)
    plan = deterrence.plan(relays, aux, ptz_aux)
    # the strobe is the WarningLight aux command on the device service (not the monostable "Warning Light" relay)
    assert {k: plan["strobe"][k] for k in ("via", "service", "target", "on", "off")} == {"via": "aux", "service": "device", "target": "tt:WarningLight", "on": "On", "off": "Off"}
    assert {k: plan["siren"][k] for k in ("via", "target", "pulse_s")} == {"via": "relay", "target": "Sounder", "pulse_s": 10.0}
    # the white light goes back to Auto (its own day/night logic), not Off; IRLamp and LightSmartSwitch are not it
    assert {k: plan["light"][k] for k in ("via", "service", "target", "on", "off")} == {"via": "aux", "service": "device", "target": "tt:Light", "on": "On", "off": "Auto"}
    assert [deterrence.relay_role(r["token"], plan) for r in relays] == [None, None, "siren"]


def test_mapping_for_other_makers():
    plan = deterrence.plan([{"token": "RelayOutputToken1", "mode": "bistable", "delay_s": 0}, {"token": "Strobe_1", "mode": "bistable", "delay_s": 0},
                            {"token": "AlarmOut_Siren", "mode": "monostable", "delay_s": 5}], [], [])
    assert plan["strobe"]["target"] == "Strobe_1" and plan["strobe"]["pulse_s"] is None
    assert plan["siren"]["target"] == "AlarmOut_Siren" and plan["siren"]["pulse_s"] == 5.0
    assert plan["light"] is None
    # nothing named: only the generic relays
    assert deterrence.plan([{"token": "AlarmOut_0", "mode": "bistable", "delay_s": 0}], [], []) == {"strobe": None, "siren": None, "light": None}
    # a siren aux command; the PTZ node's light when the device lists none; an On without an Off is no output (no way back)
    plan = deterrence.plan([], ["tt:Siren|On", "tt:Siren|Off", "tt:WarningLight|On"], ["tt:Light|On", "tt:Light|Off"])
    assert plan["siren"]["via"] == "aux" and plan["strobe"] is None
    assert plan["light"]["service"] == "ptz" and plan["light"]["off"] == "Off"
    assert deterrence.pulses_for(10, 10) == 1 and deterrence.pulses_for(25, 10) == 3 and deterrence.pulses_for(3, 10) == 1
    assert deterrence.clamp_seconds(None) == 10 and deterrence.clamp_seconds(500) == 120 and deterrence.clamp_seconds(0) == 1


def test_probe_against_the_fake_camera():
    d = asyncio.run(probed())
    assert d.available and d.caps["milesight"] and d.caps["model"] == "MS-CQ8231-HYPG1" and d.caps["audio_outputs"] == 2
    assert d.brief() == {"strobe": True, "siren": True, "light": True, "relays": 3}
    pub = d.public()
    assert pub["available"] and pub["actions"]["strobe"]["target"] == "tt:WarningLight" and pub["actions"]["siren"]["pulse_s"] == 10.0
    assert [r["role"] for r in pub["relays"]] == [None, None, "siren"] and pub["state"] == {}
    assert not any(t.startswith(("Set", "Send")) for t, _ in sent)   # probing writes nothing
    # a camera that doesn't answer: not available, asked again later; nothing to show on the tile
    refuse.add("GetServices")
    refuse.add("GetCapabilities")
    try:
        d2 = deterrence.DeterrenceCamera({**CAM, "id": "dead"})
        asyncio.run(d2.probe())
        assert not d2.available and d2.caps["error"] and d2.brief() is None and d2.next_probe > time.time()
    finally:
        refuse.clear()


# ---------------------------------------------------------------- timing

def test_aux_on_then_off_after_the_time():
    async def go():
        d = await probed()
        r = await d.turn("strobe", True, seconds=10, by="op@example")
        assert r == {"action": "strobe", "on": True, "seconds": 10.0}
        assert aux_sent() == ["tt:WarningLight|On"]
        st = d.public_state()["strobe"]
        assert st["on"] and 9 <= st["remaining_s"] <= 10 and st["by"] == "op@example"
        await asyncio.sleep(10 / FAST + 0.1)
        assert aux_sent() == ["tt:WarningLight|On", "tt:WarningLight|Off"]
        assert not d.public_state()["strobe"]["on"] and "strobe" not in d.timers
    asyncio.run(go())


def test_a_newer_action_cancels_the_old_timer():
    async def go():
        d = await probed()
        await d.turn("strobe", True, seconds=10)
        await asyncio.sleep(5 / FAST)
        await d.turn("strobe", True, seconds=20)        # replaces: one Off, at the new time
        await asyncio.sleep(10 / FAST + 0.03)            # the first timer's time has passed
        assert aux_sent() == ["tt:WarningLight|On", "tt:WarningLight|On"]
        assert d.public_state()["strobe"]["on"]
        await asyncio.sleep(10 / FAST + 0.1)
        assert aux_sent() == ["tt:WarningLight|On", "tt:WarningLight|On", "tt:WarningLight|Off"]
        # turned off by hand: the Off at once, no timer left to send another
        await d.turn("light", True, seconds=30)
        await d.turn("light", False)
        await asyncio.sleep(30 / FAST + 0.1)
        assert aux_sent()[-2:] == ["tt:Light|On", "tt:Light|Auto"] and aux_sent().count("tt:Light|Auto") == 1
    asyncio.run(go())


def test_relay_pulses_and_bistable_relays():
    async def go():
        d = await probed()
        await d.turn("siren", True, seconds=10)          # one pulse: the camera ends it itself
        assert relay_sent() == [("Sounder", "active")]
        await asyncio.sleep(10 / FAST + 0.1)
        assert relay_sent() == [("Sounder", "active")] and not d.public_state()["siren"]["on"]
        sent.clear()
        await d.turn("siren", True, seconds=25)          # three pulses of 10 s cover 25 s
        assert 29 <= d.public_state()["siren"]["remaining_s"] <= 30
        await asyncio.sleep(30 / FAST + 0.15)
        assert relay_sent() == [("Sounder", "active")] * 3
        sent.clear()
        await d.turn("relay", True, token="AlarmOut_0", seconds=5)   # bistable: inactive after the time
        await asyncio.sleep(5 / FAST + 0.1)
        assert relay_sent() == [("AlarmOut_0", "active"), ("AlarmOut_0", "inactive")]
        sent.clear()
        await d.turn("relay", True, token="AlarmOut_0", latch=True)  # the old relay route: stays on
        await asyncio.sleep(10 / FAST + 0.1)
        assert relay_sent() == [("AlarmOut_0", "active")] and d.public_state()["relay:AlarmOut_0"]["on"]
        try:
            await d.turn("relay", True, token="nope")
            raise AssertionError("an unknown relay must be refused")
        except deterrence.NoOutput:
            pass
    asyncio.run(go())


def test_stop_turns_everything_off_and_cancels_the_timers():
    async def go():
        d = await probed()
        await d.turn("strobe", True, seconds=60)
        await d.turn("relay", True, token="AlarmOut_0", seconds=60)
        sent.clear()
        r = await d.stop(by="op@example")
        assert r["errors"] == []
        # strobe and siren always; the relay this server turned on; the light was never turned on: left alone (Auto)
        assert aux_sent() == ["tt:WarningLight|Off"] and sorted(relay_sent()) == [("AlarmOut_0", "inactive"), ("Sounder", "inactive")]
        assert not any(s["on"] for s in d.public_state().values()) and not d.timers
        await asyncio.sleep(60 / FAST + 0.1)
        assert aux_sent() == ["tt:WarningLight|Off"]   # the cancelled timers sent nothing more
        # Disarm is the camera's stored one-click-disarm setting: Stop never sends it
        assert not any("Disarm" in b for _, b in sent)
        # a camera that refuses: the error is passed on and logged as failed
        refuse.add("SendAuxiliaryCommand")
        try:
            await d.turn("strobe", True)
            raise AssertionError("a refused command must raise")
        except onvif_soap.OnvifError:
            pass
        finally:
            refuse.clear()
        last = db.one("SELECT * FROM deterrence_log WHERE camera_id='cam7' ORDER BY id DESC LIMIT 1")
        assert last["action"] == "strobe" and last["result"] == "failed" and "refused" in last["error"]
    asyncio.run(go())


# ---------------------------------------------------------------- automatic rules

def event(**kw) -> dict:
    e = {"id": 1, "camera_id": "cam7", "status": "verified", "camera_class": "person", "ptz_preset": None,
         "path": [[100.0, 0.4, 0.6, 0.5, 0.8, 0.9], [101.0, 0.4, 0.6, 0.5, 0.9, 0.9]]}
    return {**e, **kw}


def test_rule_gates():
    rule = deterrence.clean_rules([{"actions": ["strobe"], "when": "always"}])[0]
    assert rule["enabled"] and rule["labels"] == ["person"] and rule["seconds"] == 10 and rule["cooldown_s"] == 120 and rule["id"]
    z = CAM["zones"]
    now = 1000.0
    ok = lambda r, e=None, armed=True, last=None: deterrence.check_rule(r, e or event(), z, armed, last, now)
    assert ok(rule) == (True, "")
    assert ok({**rule, "enabled": False}) == (False, "off")
    assert ok(rule, event(status="pending")) == (False, "not verified")
    assert ok(rule, event(camera_class="vehicle")) == (False, "label")
    assert ok({**rule, "labels": ["person", "vehicle"]}, event(camera_class="vehicle"))[0]
    assert ok({**rule, "actions": []}) == (False, "nothing to do")
    # zone: the feet entered the named area; not outside it, never by an exclude zone's name, not while turned away
    assert ok({**rule, "zone": "Yard"})[0]
    assert ok({**rule, "zone": "Yard"}, event(path=[[100.0, 0.4, 0.1, 0.5, 0.3, 0.9]])) == (False, "outside the zone")
    assert ok({**rule, "zone": "Hidden"}, event(path=[[100.0, 0.0, 0.0, 0.1, 0.05, 0.9]])) == (False, "outside the zone")
    assert ok({**rule, "zone": "Yard"}, event(ptz_preset="away")) == (False, "camera turned away")
    # armed: only an armed Site; unknown (stand-alone, nothing pushed) never counts as armed
    armed = {**rule, "when": "armed"}
    assert ok(armed, armed=True)[0] and ok(armed, armed=False) == (False, "not armed") and ok(armed, armed=None) == (False, "armed unknown")
    assert ok(rule, armed=None)[0]   # "always" fires anyway
    # cooldown
    assert ok(rule, last=now - 60) == (False, "cooldown") and ok(rule, last=now - 121)[0]
    for bad in ([{"actions": ["laser"]}], [{"labels": []}], [{"when": "sometimes"}], [{"seconds": 500}], [{}] * 9):
        try:
            deterrence.clean_rules(bad)
            raise AssertionError(f"{bad} must be refused")
        except ValueError:
            pass
    assert deterrence.zone_names(z) == ["Yard"]


def push_schedule(cfg):
    aischedule.apply(cfg)


def test_verified_event_fires_the_rule_with_cooldown_and_armed_gating():
    async def go():
        d = await probed()
        deterrence.manager.cameras["cam7"] = d
        deterrence.manager.last_fired.clear()
        now = time.time()
        eid = db.create_event(camera_id="cam7", track_id="t1", camera_class="person", camera_conf=0.9, start_ts=now - 5, end_ts=now,
                              path=event()["path"], status="verified")
        # off by default: no rules, nothing sent
        db.set_deterrence_rules("cam7", [])
        assert await deterrence.manager.on_verified(eid) == [] and not sent
        rules = deterrence.clean_rules([{"name": "Night yard", "actions": ["strobe", "siren"], "seconds": 20, "when": "armed", "zone": "Yard"}])
        db.set_deterrence_rules("cam7", rules)
        # no schedule from the hub: an "armed" rule never fires
        push_schedule(None)
        assert await deterrence.manager.on_verified(eid) == [] and not sent
        # the Site monitored with no windows: armed around the clock
        push_schedule({"mode": "full", "monitored": True, "arm_schedule": []})
        assert await deterrence.manager.on_verified(eid) == [rules[0]["id"]]
        assert aux_sent() == ["tt:WarningLight|On"] and relay_sent() == [("Sounder", "active")]
        rows = db.all("SELECT * FROM deterrence_log WHERE camera_id='cam7' AND source='rule' ORDER BY id")
        assert [(r["action"], r["event_id"], r["by"], r["result"]) for r in rows[-2:]] == [("strobe", eid, "rule Night yard", "ok"), ("siren", eid, "rule Night yard", "ok")]
        # the same visit verified again (a merge) within the cooldown: nothing more
        sent.clear()
        assert await deterrence.manager.on_verified(eid) == [] and not sent
        # disarmed by an override: never, even after the cooldown
        deterrence.manager.last_fired.clear()
        push_schedule({"mode": "full", "monitored": True, "arm_schedule": [], "arm_override": {"mode": "disarm", "until": time.time() + 3600}})
        assert await deterrence.manager.on_verified(eid) == [] and not sent
        # a vehicle doesn't match a person rule; an unverified event never runs the rules
        push_schedule({"mode": "full", "monitored": True, "arm_schedule": []})
        db.update_event(eid, camera_class="vehicle")
        assert await deterrence.manager.on_verified(eid) == []
        db.update_event(eid, camera_class="person", status="rejected")
        assert await deterrence.manager.on_verified(eid) == []
        await d.stop(quiet=True)
        push_schedule(None)
    asyncio.run(go())


def test_rule_plays_the_talkdown_message_on_a_linked_speaker():
    from nvr import talkdown
    played = []
    orig = talkdown.manager.play
    talkdown.manager.play = lambda sp, msg, by: played.append((sp["id"], msg.name, by)) or {}
    try:
        db.upsert_speaker({"id": "spk7", "name": "Cam7 speaker", "host": "192.0.2.75", "camera_ids": ["cam7"], "enabled": True})
        db.upsert_speaker({"id": "spk8", "name": "Elsewhere", "host": "192.0.2.76", "camera_ids": ["cam1"], "enabled": True})
        mid = talkdown.add_text_message("Leave", "Leave now.")["id"]

        async def go():
            deterrence.manager.cameras["cam7"] = await probed()
            deterrence.manager.last_fired.clear()
            eid = db.create_event(camera_id="cam7", track_id="t2", camera_class="person", camera_conf=0.9, start_ts=time.time(),
                                  end_ts=time.time(), path=event()["path"], status="verified")
            db.set_deterrence_rules("cam7", deterrence.clean_rules([{"name": "Talk", "when": "always", "speaker_id": "spk7", "message_id": mid}]))
            assert len(await deterrence.manager.on_verified(eid)) == 1
            assert played == [("spk7", "Leave", "rule Talk")] and not sent   # no outputs asked: none set off
            # a speaker not linked to the camera is never used
            deterrence.manager.last_fired.clear()
            db.set_deterrence_rules("cam7", deterrence.clean_rules([{"name": "Talk", "when": "always", "speaker_id": "spk8", "message_id": mid}]))
            await deterrence.manager.on_verified(eid)
            assert len(played) == 1
            # verified() runs the rules in a task of their own (the verifier doesn't wait for the camera)
            deterrence.manager.last_fired.clear()
            db.set_deterrence_rules("cam7", deterrence.clean_rules([{"name": "Talk", "when": "always", "speaker_id": "spk7", "message_id": mid}]))
            deterrence.manager.verified(eid)
            await asyncio.sleep(0.05)
            assert len(played) == 2
            db.set_deterrence_rules("cam7", [])
        asyncio.run(go())
    finally:
        talkdown.manager.play = orig


# ---------------------------------------------------------------- routes

def test_routes_roles_and_errors():
    deterrence.manager.cameras.clear()
    deterrence.manager.sync(db.cameras(enabled_only=True))
    api.state.ptz = ptz.PtzManager()   # no PTZ cameras: the old relay route goes through deterrence
    sent.clear()
    assert call("GET", "/api/cameras/cam7/deterrence", role="viewer").status_code == 403
    assert call("POST", "/api/cameras/cam7/deterrence", {"action": "strobe"}, role="viewer").status_code == 403
    r = call("GET", "/api/cameras/cam7/deterrence", role="operator")   # probed on demand
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["available"] and j["milesight"] and j["audio_outputs"] == 2 and j["actions"]["light"]["target"] == "tt:Light"
    assert PW not in r.text
    r = call("POST", "/api/cameras/cam7/deterrence", {"action": "siren", "seconds": 10}, role="operator", user="op@example")
    assert r.status_code == 200, r.text
    assert r.json()["result"] == {"action": "siren", "on": True, "seconds": 10.0} and r.json()["state"]["siren"]["by"] == "op@example"
    assert call("POST", "/api/cameras/cam7/deterrence", {"action": "relay", "token": "nope"}, role="operator").status_code == 409
    assert call("POST", "/api/cameras/cam7/deterrence", {"action": "strobe", "seconds": 500}, role="operator").status_code == 422
    assert call("POST", "/api/cameras/cam7/deterrence", {"action": "laser"}, role="operator").status_code == 422
    assert call("POST", "/api/cameras/nope/deterrence", {"action": "stop"}, role="operator").status_code == 404
    assert call("POST", "/api/cameras/cam7/deterrence", {"action": "stop"}, role="operator").status_code == 200
    log = call("GET", "/api/cameras/cam7/deterrence/log", role="operator").json()
    assert log[0]["action"] == "stop" and log[1]["action"] == "siren" and log[1]["by"] == "op@example" and log[1]["on"] is True
    # the camera list carries the brief for the tile
    assert api.deterrence.manager.brief("cam7") == {"strobe": True, "siren": True, "light": True, "relays": 3}
    # the old relay route on a camera without PTZ: the first relay, latched (as on a PTZ camera)
    sent.clear()
    r = call("POST", "/api/cameras/cam7/relay", {"on": True})
    assert r.status_code == 200 and r.json() == {"state": True, "mode": "bistable"} and relay_sent() == [("AlarmOut_0", "active")]
    r = call("POST", "/api/cameras/cam7/relay", {"on": True, "token": "Sounder"})
    assert r.json()["mode"] == "monostable" and relay_sent()[-1] == ("Sounder", "active")
    # rules: an operator reads them, an admin writes them; checked against the camera's zones and linked speakers
    assert call("PUT", "/api/cameras/cam7/deterrence/rules", {"rules": []}, role="operator").status_code == 403
    r = call("GET", "/api/cameras/cam7/deterrence/rules", role="operator")
    assert r.status_code == 200 and r.json()["zones"] == ["Yard"] and r.json()["caps"] == {"strobe": True, "siren": True, "light": True}
    assert r.json()["rules"] == [] and r.json()["armed"]["known"] is False
    bad_zone = {"rules": [{"actions": ["strobe"], "zone": "Nowhere"}]}
    assert call("PUT", "/api/cameras/cam7/deterrence/rules", bad_zone, role="admin").status_code == 422
    assert call("PUT", "/api/cameras/cam7/deterrence/rules", {"rules": [{"actions": ["strobe"], "speaker_id": "spk8", "message_id": 1}]},
                role="admin").status_code == 422   # spk8 is linked to cam1, not cam7
    assert call("PUT", "/api/cameras/cam7/deterrence/rules", {"rules": [{"actions": ["laser"]}]}, role="admin").status_code == 422
    r = call("PUT", "/api/cameras/cam7/deterrence/rules", {"rules": [{"name": "Night", "actions": ["strobe"], "zone": "Yard", "seconds": 15}]}, role="admin")
    assert r.status_code == 200, r.text
    rule = r.json()["rules"][0]
    assert rule["id"] and rule["when"] == "armed" and rule["seconds"] == 15 and db.cameras()[0]["deterrence_rules"][0]["id"] == rule["id"]
    # the camera form never clears them (it doesn't know the field)
    cam = next(c for c in db.cameras() if c["id"] == "cam7")
    db.upsert_camera({k: cam[k] for k in CAM})
    assert next(c for c in db.cameras() if c["id"] == "cam7")["deterrence_rules"][0]["id"] == rule["id"]
    # a config export carries them and a restore puts them back (checked again)
    from nvr import siteconfig
    exp = siteconfig.export_config()
    db.set_deterrence_rules("cam7", [])
    siteconfig.import_config(exp)
    assert next(c for c in db.cameras() if c["id"] == "cam7")["deterrence_rules"][0]["name"] == "Night"
    db.set_deterrence_rules("cam7", [])


def test_camera_keeps_its_row_and_gets_a_speaker():
    orig = api.sync_cameras
    api.sync_cameras = lambda: None   # no MediaMTX here
    try:
        db.upsert_camera({**CAM, "id": "cam9", "name": "Q Gate", "host": "192.0.2.79", "main_path": "/main"})
        assert call("POST", "/api/cameras/cam9/to-speaker", {"disable_camera": False}, role="operator").status_code == 403
        r = call("POST", "/api/cameras/cam9/to-speaker", {"disable_camera": False, "speaker_id": "spk9"}, role="admin")
        assert r.status_code == 200, r.text
        j = r.json()
        assert PW not in r.text and j["camera_ids"] == ["cam9"] and j["host"] == "192.0.2.79" and j["path"] == "/main" and j["name"] == "Q Gate speaker"
        sp = db.speaker("spk9")
        assert sp["password"] == PW and sp["codec"] == "PCMU" and sp["rtsp_port"] == 554 and sp["username"] == "admin"
        assert db.one("SELECT enabled FROM cameras WHERE id='cam9'")["enabled"] == 1   # still a camera
    finally:
        api.sync_cameras = orig


# ---------------------------------------------------------------- the Milesight web API

CGI = {name: json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))
       for name in ("get.camera.white.led", "get.warning.light.cfg", "get.audio", "get.audio.file", "get.one.click.disarm")}


class FakeMilesight:
    """A camera's operator.cgi on 127.0.0.1: digest auth (a 401 challenge first, as the camera does), the probed get
    answers, and every set recorded; `refuse` names sets answered {"setState": "failed"}."""

    def __init__(self):
        self.posts: list[tuple[str, dict]] = []
        self.authorized: list[bool] = []
        self.refuse: set[str] = set()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, body: dict | None, extra=None):
                raw = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _auth(self) -> bool:
                h = self.headers.get("Authorization") or ""
                if not h.startswith("Digest ") or 'username="admin"' not in h:
                    self._reply(401, None, {"WWW-Authenticate": 'Digest realm="IPCamera", nonce="abc123", qop="auth", algorithm=MD5'})
                    return False
                fake.authorized.append(True)
                return True

            def _action(self):
                from urllib.parse import parse_qs, urlsplit
                u = urlsplit(self.path)
                assert u.path == "/cgi-bin/operator/operator.cgi", u.path
                q = parse_qs(u.query)
                assert q.get("format") == ["json"]
                return q["action"][0]

            def do_GET(self):
                if not self._auth():
                    return
                a = self._action()
                self._reply(200, CGI.get(a) or {"setState": "failed"})

            def do_POST(self):
                if not self._auth():
                    return
                a = self._action()
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                assert self.headers.get("Content-Type", "").startswith("application/json")
                fake.posts.append((a, body))
                self._reply(200, {"setState": "failed" if a in fake.refuse else "succeed"})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def cam(self) -> dict:
        return {**CAM, "id": "ms1", "host": "127.0.0.1", "web_port": self.port, "onvif_port": 9}


def test_milesight_reads_and_writes_as_its_page_does():
    fake = FakeMilesight()
    cam = fake.cam()
    out = milesight.read_all(cam)
    assert out["supported"] and fake.authorized
    assert out["white_light"] == {"supported": True, "mode": "auto", "sensitivity": 3, "delay_s": 5, "start": "18:00", "stop": "06:00",
                                  "brightness": 100, "writable": True}
    assert out["red_blue"] == {"supported": True, "mode": "red_blue", "frequency": "medium", "writable": True}
    assert out["siren"]["speaker_volume"] == 50 and out["siren"]["volume_writable"] and not out["siren"]["file_writable"]
    assert [f["index"] for f in out["siren"]["files"]] == [0, 1, 2, 3, 4] and not any(f["present"] for f in out["siren"]["files"])
    assert out["disarm"]["enable"] is False and set(out["disarm"]["mutes"]) == set(milesight.DISARM_KEYS)
    # writes: the whole object read back with the changes (white light, audio, disarm); the warning light's redBlueLight only
    before, after, written = milesight.write(cam, {"white_light": {"mode": "schedule", "start": "19:30", "stop": "5:15", "brightness": 80},
                                                   "red_blue": {"mode": "red", "frequency": "high"}, "speaker_volume": 70,
                                                   "disarm": {"enable": True, "mutes": {"warningLight": True, "audio": True}}})
    posts = dict(fake.posts)
    assert list(posts) == ["set.camera.white.led", "set.warning.light.cfg", "set.audio", "set.one.click.disarm"]
    assert posts["set.camera.white.led"] == {**CGI["get.camera.white.led"], "lightCtrl": 3, "startHour": 19, "startMin": 30,
                                             "stopHour": 5, "stopMin": 15, "brightness": 80}
    assert posts["set.warning.light.cfg"] == {"redBlueLight": {"mode": 1, "frequency": 2}}
    assert posts["set.audio"] == {**CGI["get.audio"], "speakerVolume": 70} and isinstance(posts["set.audio"]["bitRate"], int)
    d = CGI["get.one.click.disarm"]
    assert posts["set.one.click.disarm"] == {**d, "enable": 1, "disarm": {**d["disarm"], "warningLight": 1, "audio": 1}}
    assert posts["set.one.click.disarm"]["scheduleList"] == d["scheduleList"]
    assert before["white_light"]["mode"] == "auto" and before["speaker_volume"] == 50 and written == posts and after["supported"]
    # checked before anything is written
    fake.posts.clear()
    for bad in ({"white_light": {"brightness": 0}}, {"white_light": {"start": "25:00"}}, {"white_light": {"mode": "disco"}},
                {"red_blue": {"frequency": "max"}}, {"speaker_volume": 101}, {"disarm": {"mutes": {"laser": True}}}, {}):
        try:
            milesight.write(cam, bad)
            raise AssertionError(f"{bad} must be refused")
        except milesight.Invalid:
            pass
    assert fake.posts == []
    # the camera refuses: an error, never the password
    fake.refuse.add("set.audio")
    try:
        milesight.write(cam, {"speaker_volume": 10})
        raise AssertionError("a refusal must raise")
    except milesight.WebApiError as e:
        assert "set.audio" in str(e) and PW not in str(e)
    fake.srv.shutdown()


def test_milesight_routes():
    fake = FakeMilesight()
    db.upsert_camera(fake.cam())
    deterrence.manager.sync(db.cameras(enabled_only=True))
    d = deterrence.manager.get("ms1")
    d.caps = {"available": True, "milesight": True, "actions": {}, "relays": []}   # the probe said Milesight
    r = call("GET", "/api/cameras/ms1/device/milesight", role="operator")
    assert r.status_code == 200 and r.json()["supported"] and r.json()["white_light"]["mode"] == "auto"
    assert call("GET", "/api/cameras/ms1/device/milesight", role="viewer").status_code == 403
    assert call("PUT", "/api/cameras/ms1/device/milesight", {"speaker_volume": 40}, role="operator").status_code == 403
    r = call("PUT", "/api/cameras/ms1/device/milesight", {"speaker_volume": 40}, role="admin")
    assert r.status_code == 200, r.text
    assert r.json()["written"]["set.audio"]["speakerVolume"] == 40 and r.json()["before"] == {"speaker_volume": 50}
    assert call("PUT", "/api/cameras/ms1/device/milesight", {"red_blue": {"mode": "green"}}, role="admin").status_code == 422
    # another maker: not supported, nothing asked of its web API
    d.caps = {"available": True, "milesight": False, "actions": {}, "relays": []}
    n = len(fake.authorized)
    r = call("GET", "/api/cameras/ms1/device/milesight", role="operator")
    assert r.status_code == 200 and r.json() == {"supported": False, "reason": "not a Milesight camera"} and len(fake.authorized) == n
    fake.srv.shutdown()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            sent.clear()
            fn()
            print("ok", name)
    print("all passed")
