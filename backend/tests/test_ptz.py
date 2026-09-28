"""PTZ (ptz.py) without a camera: parsers on the real probe XML, position/home logic, SOAP bodies via a fake
Onvif.call, config persistence, and the away gating in tracker / baseline / policy.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_ptz.py   (from backend/)
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from xml.etree import ElementTree as ET

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-ptz-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import baseline, onvif_soap, policy, ptz  # noqa: E402
from nvr.db import db  # noqa: E402
from nvr.ingest import MetaFrame, DetectedObject  # noqa: E402
from nvr.tracker import Tracker  # noqa: E402

TT = 'xmlns:tt="http://www.onvif.org/ver10/schema"'
NODES = f"""<GetNodesResponse xmlns="http://www.onvif.org/ver20/ptz/wsdl" {TT}><PTZNode token="PTZNodeToken"><tt:Name>PTZNode</tt:Name>
<tt:SupportedPTZSpaces><tt:AbsolutePanTiltPositionSpace/><tt:ContinuousPanTiltVelocitySpace/><tt:ContinuousZoomVelocitySpace/><tt:RelativePanTiltTranslationSpace/></tt:SupportedPTZSpaces>
<tt:MaximumNumberOfPresets>300</tt:MaximumNumberOfPresets><tt:HomeSupported>true</tt:HomeSupported>
<tt:AuxiliaryCommands>lighton</tt:AuxiliaryCommands><tt:AuxiliaryCommands>brushon</tt:AuxiliaryCommands>
<tt:Extension><tt:SupportedPresetTour><tt:MaximumNumberOfPresetTours>8</tt:MaximumNumberOfPresetTours></tt:SupportedPresetTour></tt:Extension></PTZNode></GetNodesResponse>"""
CONFIGS = f"""<GetConfigurationsResponse xmlns="http://www.onvif.org/ver20/ptz/wsdl" {TT}><PTZConfiguration token="PTZToken"><tt:DefaultPTZTimeout>PT00H00M01S</tt:DefaultPTZTimeout></PTZConfiguration></GetConfigurationsResponse>"""
PROFILES = f"""<GetProfilesResponse xmlns="http://www.onvif.org/ver10/media/wsdl" {TT}><Profiles token="Profile_1"><tt:Name>main</tt:Name><tt:PTZConfiguration token="PTZToken"/></Profiles><Profiles token="Profile_2"><tt:Name>sub</tt:Name></Profiles></GetProfilesResponse>"""
STATUS = {"pos": (-0.47766, -0.074133, 0.020455), "moving": False}


def status_xml():
    x, y, z = STATUS["pos"]
    mv = "MOVING" if STATUS["moving"] else "IDLE"
    return (f'<GetStatusResponse xmlns="http://www.onvif.org/ver20/ptz/wsdl" {TT}><PTZStatus><tt:Position>'
            f'<tt:PanTilt x="{x}" y="{y}" space="http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace"/>'
            f'<tt:Zoom x="{z}" space="http://www.onvif.org/ver10/tptz/ZoomSpaces/PositionGenericSpace"/></tt:Position>'
            f'<tt:MoveStatus><tt:PanTilt>{mv}</tt:PanTilt><tt:Zoom>{mv}</tt:Zoom></tt:MoveStatus><tt:UtcTime>2026-09-26T21:16:35Z</tt:UtcTime></PTZStatus></GetStatusResponse>')


USER_PRESETS = [("1", "back shop"), ("2", "Ac units"), ("3", "flip pad"), ("4", "tractor supply"), ("5", "parking"), ("6", "Intersection"), ("7", "577 LEDs")]
SYSTEM_PRESETS = [("33", "Auto Flip"), ("34", "Goto Zero"), ("35", "Self Check")]
PRESETS = list(USER_PRESETS) + list(SYSTEM_PRESETS)


def presets_xml():
    items = "".join(f'<Preset token="{t}"><tt:Name>{n}</tt:Name><tt:PTZPosition><tt:PanTilt x="245.52" y="-3.39"/></tt:PTZPosition></Preset>' for t, n in PRESETS)
    return f'<GetPresetsResponse xmlns="http://www.onvif.org/ver20/ptz/wsdl" {TT}>{items}</GetPresetsResponse>'


RELAYS = f"""<GetRelayOutputsResponse xmlns="http://www.onvif.org/ver10/device/wsdl" {TT}><RelayOutputs token="AlarmOut_0"><tt:Properties><tt:Mode>Bistable</tt:Mode><tt:DelayTime>PT00H00M10S</tt:DelayTime><tt:IdleState>open</tt:IdleState></tt:Properties></RelayOutputs></GetRelayOutputsResponse>"""
INPUTS = f"""<GetDigitalInputsResponse xmlns="http://www.onvif.org/ver10/deviceIO/wsdl" {TT}><DigitalInputs token="AlarmIn_0" IdleState="open"/></GetDigitalInputsResponse>"""
SERVICES = """<GetServicesResponse xmlns="http://www.onvif.org/ver10/device/wsdl"><Service><Namespace>http://www.onvif.org/ver20/ptz/wsdl</Namespace><XAddr>http://cam/onvif/PTZ</XAddr></Service>
<Service><Namespace>http://www.onvif.org/ver10/media/wsdl</Namespace><XAddr>http://cam/onvif/Media</XAddr></Service>
<Service><Namespace>http://www.onvif.org/ver10/deviceIO/wsdl</Namespace><XAddr>http://cam/onvif/deviceIO</XAddr></Service></GetServicesResponse>"""
TIME = """<GetSystemDateAndTimeResponse xmlns="http://www.onvif.org/ver10/device/wsdl"><SystemDateAndTime><UTCDateTime><Time><Hour>1</Hour><Minute>2</Minute><Second>3</Second></Time><Date><Year>2026</Year><Month>9</Month><Day>26</Day></Date></UTCDateTime></SystemDateAndTime></GetSystemDateAndTimeResponse>"""

calls: list[tuple[str, str]] = []


def fake_call(self, url, body, auth=True, header="", timeout=None):
    calls.append((url, body))
    tag = body.split("<", 2)[1].split(">", 1)[0].split(" ", 1)[0].split(":", 1)[-1].rstrip("/")
    canned = {"GetNodes": NODES, "GetConfigurations": CONFIGS, "GetProfiles": PROFILES, "GetStatus": status_xml,
              "GetPresets": presets_xml, "GetRelayOutputs": RELAYS, "GetDigitalInputs": INPUTS, "GetServices": SERVICES,
              "GetSystemDateAndTime": TIME}
    if tag == "SetPreset":
        return ET.fromstring('<SetPresetResponse xmlns="http://www.onvif.org/ver20/ptz/wsdl"><PresetToken>8</PresetToken></SetPresetResponse>')
    if tag in canned:
        c = canned[tag]
        return ET.fromstring(c() if callable(c) else c)
    return ET.fromstring(f'<{tag}Response xmlns="http://www.onvif.org/ver20/ptz/wsdl"/>')


onvif_soap.Onvif.call = fake_call  # every test runs against the fake camera

CAM = {"id": "cam1", "name": "Side Yard", "host": "127.0.0.1", "onvif_port": 80, "rtsp_port": 554, "username": "u",
       "password": "", "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": [], "retention_days": None,
       "scene_notes": "", "retention_policy": None}


def sent(tag: str) -> list[str]:
    return [b for _, b in calls if f":{tag}>" in b or f":{tag} " in b or f":{tag}/" in b]


def test_parsers_on_probe_xml():
    ps = ptz.parse_presets(ET.fromstring(presets_xml()))
    assert len(ps) == 10 and sum(p["system"] for p in ps) == 3
    assert [p["name"] for p in ps if not p["system"]] == [n for _, n in USER_PRESETS]
    st = ptz.parse_status(ET.fromstring(status_xml()))
    assert st["position"] == {"x": -0.4777, "y": -0.0741, "zoom": 0.0205} and st["moving"] is False
    caps = ptz.parse_nodes(ET.fromstring(NODES))
    assert caps["home_supported"] and caps["max_presets"] == 300 and caps["continuous"] and caps["tours"] and "lighton" in caps["aux_commands"]
    r = ptz.parse_relays(ET.fromstring(RELAYS))[0]
    assert r == {"token": "AlarmOut_0", "mode": "bistable", "delay_s": 10.0, "idle_state": "open"}
    assert ptz.parse_digital_inputs(ET.fromstring(INPUTS)) == ["AlarmIn_0"]
    assert onvif_soap.parse_duration("PT2S") == 2 and onvif_soap.parse_duration("PT00H01M10S") == 70 and onvif_soap.parse_duration(None) == 0


def test_position_logic():
    home = {"x": 0.1, "y": -0.2, "zoom": 0.05}
    assert ptz.at_position({"x": 0.11, "y": -0.21, "zoom": 0.06}, home)
    assert not ptz.at_position({"x": 0.15, "y": -0.2, "zoom": 0.05}, home)
    assert not ptz.at_position(None, home) and not ptz.at_position(home, None)
    pp = {"1": home, "5": {"x": 0.5, "y": 0.0, "zoom": 0.0}}
    assert ptz.nearest_preset({"x": 0.505, "y": 0.0, "zoom": 0.0}, pp) == "5"
    assert ptz.nearest_preset({"x": 0.3, "y": 0.0, "zoom": 0.0}, pp) is None
    assert ptz.is_system_preset("33", "Auto Flip") and ptz.is_system_preset("9", "Self Check") and not ptz.is_system_preset("5", "parking")
    now = 1000.0
    ok = dict(at_home=False, moving=False, last_command_at=now - 400, return_home_min=5, now=now, home_token="5")
    assert ptz.should_return_home(**ok)
    assert not ptz.should_return_home(**{**ok, "return_home_min": 0})
    assert not ptz.should_return_home(**{**ok, "moving": True})
    assert not ptz.should_return_home(**{**ok, "last_command_at": now - 100})
    assert not ptz.should_return_home(**{**ok, "home_token": None})
    assert not ptz.should_return_home(**{**ok, "at_home": True})
    x, y = ptz.relative_for_click(0.25, 0.25, 0.0)
    assert x > 0 and y < 0 and abs(x) == abs(y)           # right/down click -> pan right, tilt down (ONVIF y up)
    assert abs(ptz.relative_for_click(0.25, 0, 1.0)[0]) < abs(x)  # zoomed in: smaller angle for the same offset
    moves = [(10, True, None), (20, False, "parking"), (30, True, None)]
    assert ptz.away_between(moves, 12, 18) is None
    assert ptz.away_between(moves, 22, 28) == "parking"
    assert ptz.away_between(moves, 15, 25) == "parking"     # moved away during the window
    assert ptz.away_between(moves, 32, 40) is None
    assert ptz.away_between([], 0, 1) is None


def test_probe_and_commands():
    db.upsert_camera(CAM)
    p = ptz.PtzCamera(dict(CAM))
    asyncio.run(p.probe())
    assert p.available and p.profile == "Profile_1" and len(p.presets) == 10 and p.relays[0]["mode"] == "bistable" and p.inputs == ["AlarmIn_0"]
    assert p.pan_tilt and p.public()["pan_tilt"]
    calls.clear()
    asyncio.run(p.move(0.5, -0.2, 0))
    body = sent("ContinuousMove")[0]
    assert 'x="0.500" y="-0.200"' in body and "PT2S" in body and "tt:Zoom" not in body
    asyncio.run(p.move(0, 0, 0.3))
    assert 'tt:Zoom x="0.300"' in sent("ContinuousMove")[1] and "tt:PanTilt" not in sent("ContinuousMove")[1]
    asyncio.run(p.stop())
    assert "<tptz:PanTilt>true</tptz:PanTilt>" in sent("Stop")[0]
    asyncio.run(p.move(0, 0, 0))
    assert len(sent("Stop")) == 2  # an all-zero move is a stop
    asyncio.run(p.goto_preset("5"))
    assert "<tptz:PresetToken>5</tptz:PresetToken>" in sent("GotoPreset")[0]
    asyncio.run(p.set_relay(True))
    assert "AlarmOut_0" in sent("SetRelayOutputState")[0] and "<tds:LogicalState>active</tds:LogicalState>" in sent("SetRelayOutputState")[0]
    assert p.relay_state is True
    p.on_io_event("tns1:Device/Trigger/DigitalInput", True, 5.0)
    assert p.input_state is True and p.public()["input"]["state"] is True

    async def burst():
        await asyncio.gather(*(p.move(0.1 * i, 0, 0) for i in range(1, 6)))
    calls.clear()
    asyncio.run(burst())
    moves = sent("ContinuousMove")
    assert 1 <= len(moves) <= 2 and 'x="0.500"' in moves[-1]  # a burst is coalesced; the newest velocity wins


def test_set_home_persists_and_away_labels():
    p = ptz.PtzCamera(dict(CAM))
    asyncio.run(p.probe())
    STATUS["pos"] = (0.5, 0.0, 0.0)
    asyncio.run(p.set_home("5"))
    cfg = db.cameras()[0]["ptz_config"]
    assert cfg["home_token"] == "5" and cfg["home_name"] == "parking" and cfg["preset_pos"]["5"]["x"] == 0.5
    assert p.status["at_home"] and p.away_label() is None
    STATUS["pos"] = (-0.4, 0.1, 0.0)  # turned somewhere unknown
    asyncio.run(p.refresh_status())
    assert p.away_label() == "away" and not p.status["at_home"]
    asyncio.run(p.goto_preset("6", wait=True))  # visits Intersection: its position is learned
    assert p.cfg["preset_pos"]["6"]["x"] == -0.4 and p.away_label() == "Intersection"
    assert [m[2] for m in p.moves] == [None, "away", "Intersection"]
    assert db.one("SELECT COUNT(*) AS n FROM ptz_moves WHERE camera_id='cam1'")["n"] == 3
    tok = asyncio.run(p.set_preset("Gate"))
    assert tok == "8" and p.cfg["preset_pos"]["8"]["x"] == -0.4
    STATUS["pos"] = (0.5, 0.0, 0.0)
    asyncio.run(p.refresh_status())
    assert p.away_label() is None and p.public()["home_name"] == "parking"


def test_tracker_tags_away_events():
    tr = Tracker(lambda eid: None)
    tr.set_zones("cam1", [{"name": "yard", "type": "include", "points": [[0, 0], [0.5, 0], [0.5, 0.5], [0, 0.5]]}])
    tr.away_preset = lambda cid: "parking"
    obj = DetectedObject(object_id="7", cls="human", conf=0.9, box=[0.8, 0.8, 0.9, 0.95])  # outside every include zone
    tr.on_frame(MetaFrame(camera_id="cam1", ts=100.0, objects=[obj]))
    tr.on_frame(MetaFrame(camera_id="cam1", ts=103.0, objects=[obj]))
    t = tr.tracks[("cam1", "7")]
    assert t.event_id is not None and db.event(t.event_id)["ptz_preset"] == "parking"
    tr.away_preset = lambda cid: None
    tr.away_between = lambda cid, t0, t1: "away"
    async def _noop(eid): return None
    tr2 = Tracker(_noop)
    tr2.away_between = lambda cid, t0, t1: "away"
    tr2.set_zones("cam1", [])
    inside = DetectedObject(object_id="8", cls="human", conf=0.9, box=[0.2, 0.2, 0.3, 0.4])
    tr2.on_frame(MetaFrame(camera_id="cam1", ts=200.0, objects=[inside]))
    tr2.on_frame(MetaFrame(camera_id="cam1", ts=203.0, objects=[inside]))
    eid = tr2.tracks[("cam1", "8")].event_id
    for tk in tr2.tracks.values():
        tk.last_wall = 0  # quiet: closes on sweep
    asyncio.run(tr2.sweep())
    assert db.event(eid)["ptz_preset"] == "away" and db.event(eid)["status"] == "pending"


def test_gating_helpers():
    e = {"status": "verified", "feedback": None, "ptz_preset": "parking"}
    assert not baseline._counted(e) and baseline._counted({**e, "ptz_preset": None})
    eid = db.create_event(camera_id="cam1", track_id="x", camera_class="person", camera_conf=0.9, start_ts=time.time(),
                          end_ts=time.time() + 3, path=[], status="verified", ptz_preset="away")
    assert baseline.apply(eid) is None and policy.check(eid) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
