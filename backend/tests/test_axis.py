"""AXIS cameras over VAPIX (axis.py), against a fake camera on httpx's MockTransport shaped like TSS-DT's AXIS M4327-P
(2026-11-06): anonymous identity, the scene metadata producer turned on by setup, VAPIX stream paths with SD the same
fisheye circle smaller, streams.plan taking those paths, AXIS Object Analytics events named after their scenario, and
the advisor's two findings.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_axis.py   (from backend/)
"""
import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-axis-test-")
os.environ["NVR_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="nvr-axis-runtime-")
os.environ["NVR_RECORDINGS_DIR"] = tempfile.mkdtemp(prefix="nvr-axis-rec-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from nvr import advisor, axis, ingest, ruleevents, streams  # noqa: E402
from nvr.camdevice import Unsupported  # noqa: E402

CAM = {"id": "cam9", "name": "AXIS Test", "host": "192.168.105.115", "onvif_port": 80, "rtsp_port": 554, "username": "root",
       "password": "secret-pw", "main_path": "/main", "sub_path": "/sub", "enabled": 1,
       "streams": {"profiles": [
           {"token": "profile_0_h264", "encoding": "H.264", "width": 2160, "height": 2160, "source": "0",
            "path": "/onvif-media/media.amp?profile=profile_0_h264&sessiontimeout=60&streamtype=unicast"},
           {"token": "profile_2_h264", "encoding": "H.264", "width": 2560, "height": 1440, "source": "0",
            "path": "/onvif-media/media.amp?profile=profile_2_h264&sessiontimeout=60&streamtype=unicast"}]}}
RES = "root.Properties.Image.Resolution=2160x2160,1920x1920,1440x1440,1280x1280,1024x1024,800x800,640x640,1920x1080,1280x720,640x360"


class FakeAxis:
    def __init__(self, brand="AXIS", enabled=False, aoa=True):
        self.brand, self.enabled, self.aoa = brand, enabled, aoa
        self.calls: list[tuple[str, str, bool]] = []   # (path, method, authenticated)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path, authed = req.url.path, "authorization" in req.headers
        body = json.loads(req.content or b"{}") if req.method == "POST" else {}
        self.calls.append((path, body.get("method") or req.url.params.get("action", ""), authed))
        if path == "/axis-cgi/basicdeviceinfo.cgi":
            if self.brand != "AXIS":
                return httpx.Response(404)
            return httpx.Response(200, json={"apiVersion": "1.3", "data": {"propertyList": {
                "Brand": "AXIS", "ProdNbr": "M4327-P", "ProdShortName": "AXIS M4327-P", "ProdFullName": "AXIS M4327-P Panoramic Camera",
                "ProdType": "Panoramic Camera", "Version": "11.4.63", "SerialNumber": "B8A44F916D1B"}}})
        if path == "/axis-cgi/apidiscovery.cgi":
            return httpx.Response(200, json={"data": {"apiList": [{"id": "analytics-metadata-config", "version": "1.0"},
                                                                   {"id": "event-mqtt-bridge", "version": "1.2"}]}})
        if not authed:   # everything else wants the login: answer the digest challenge
            return httpx.Response(401, headers={"WWW-Authenticate": 'Digest realm="AXIS_B8A44F916D1B", nonce="abc", algorithm=MD5, qop="auth"'})
        if path == "/axis-cgi/analyticsmetadataconfig.cgi":
            if body["method"] == "listProducers":
                return httpx.Response(200, json={"data": {"producers": [
                    {"name": "VideoMotionTracker", "niceName": "Video Motion Tracker", "videochannels": [{"channel": 1, "enabled": False}]},
                    {"name": "AnalyticsSceneDescription", "niceName": "Fusion Tracker", "videochannels": [{"channel": 1, "enabled": self.enabled}]}]}})
            if body["method"] == "setEnabledProducers":
                assert body["params"] == {"producers": [{"name": "AnalyticsSceneDescription", "videochannels": [{"channel": 1, "enabled": True}]}]}
                self.enabled = True
                return httpx.Response(200, json={"data": {}})
        if path == "/local/objectanalytics/control.cgi":
            if not self.aoa:
                return httpx.Response(404)
            return httpx.Response(200, json={"data": {"scenarios": [
                {"id": 1, "name": "Back door", "type": "motion", "objectClassifications": [{"type": "human"}]},
                {"id": 2, "name": "Lot", "type": "fence", "objectClassifications": []},
                {"id": 3, "name": "Footfall", "type": "crosslineCounting", "objectClassifications": [{"type": "human"}]}]}})
        if path == "/axis-cgi/param.cgi":
            return httpx.Response(200, text=RES + "\n")
        return httpx.Response(404)


def with_camera(fake: FakeAxis):
    axis._transport = httpx.MockTransport(fake)
    axis._identity.clear()
    axis._scenarios.clear()


def test_identify_without_login():
    fake = FakeAxis()
    with_camera(fake)
    ident = axis.identify(CAM)
    assert ident["model"] == "AXIS M4327-P" and ident["firmware"] == "11.4.63" and "event-mqtt-bridge" in ident["apis"]
    assert all(not authed for _, _, authed in fake.calls)            # no login was sent
    assert axis.cached("cam9") is ident
    with_camera(FakeAxis(brand="Milesight"))
    assert axis.identify(CAM, fresh=True) is None and axis.cached("cam9") is None
    assert axis.firmware_old("11.4.63") and not axis.firmware_old("11.11.124") and not axis.firmware_old("12.6.85")
    assert not axis.firmware_old(None)


def test_sd_is_the_same_picture_smaller():
    res = [(2160, 2160), (1920, 1920), (1024, 1024), (800, 800), (1920, 1080), (1280, 720), (640, 360)]
    assert axis.sd_size((2160, 2160), res) == (1024, 1024)            # the circle, not a 16:9 panorama
    assert axis.sd_size((1920, 1080), [(1920, 1080), (1280, 720), (640, 360)]) == (640, 360)
    assert axis.sd_size(None, []) is None
    main, sub = axis.stream_paths((2160, 2160), res, analytics=True)
    assert main == "/axis-media/media.amp?camera=1&videocodec=h264&analytics=polygon"
    assert sub == "/axis-media/media.amp?camera=1&videocodec=h264&resolution=1024x1024"
    assert axis.uses_vapix(main) and axis.has_analytics(main) and not axis.has_analytics(sub)
    assert axis.stream_paths(None, [], analytics=False) == ("/axis-media/media.amp?camera=1&videocodec=h264",) * 2


def test_read_and_setup():
    fake = FakeAxis()
    with_camera(fake)
    r = axis.read_all(CAM)
    assert r["firmware"]["old"] and r["metadata"]["producer"] == "AnalyticsSceneDescription" and not r["metadata"]["enabled"]
    assert [s["name"] for s in r["analytics"]["scenarios"]] == ["Back door", "Lot", "Footfall"] and r["setup_needed"]
    done = axis.setup(CAM)
    assert fake.enabled and done["enabled_now"] and done["written"] == ["scene metadata producer AnalyticsSceneDescription on"]
    assert done["main_path"].endswith("&analytics=polygon") and done["sd"] == "1024x1024"
    after = axis.read_all({**CAM, "main_path": done["main_path"], "sub_path": done["sub_path"]})
    assert after["metadata"]["enabled"] and after["streams"] == {"main_path": done["main_path"], "sub_path": done["sub_path"],
                                                                 "vapix": True, "analytics": True}
    assert not after["setup_needed"]
    again = axis.setup({**CAM, "main_path": done["main_path"]})
    assert not again["enabled_now"] and again["written"] == []         # already on: the camera isn't written again
    with_camera(FakeAxis(brand="Hikvision"))
    try:
        axis.read_all(CAM)
        raise AssertionError("not an AXIS")
    except Unsupported:
        pass
    with_camera(FakeAxis(aoa=False))
    assert axis.read_all(CAM)["analytics"] == {"supported": False, "reason": "AXIS Object Analytics isn't installed or running"}


def test_new_camera_gets_axis_paths():
    with_camera(FakeAxis())
    assert axis.paths_for_new(CAM) == ("/axis-media/media.amp?camera=1&videocodec=h264",
                                       "/axis-media/media.amp?camera=1&videocodec=h264&resolution=1024x1024")
    with_camera(FakeAxis(brand="Milesight"))
    assert axis.paths_for_new(CAM) is None


def test_plan_takes_vapix_paths():
    cam = {**CAM, "main_path": "/axis-media/media.amp?camera=1&videocodec=h264&analytics=polygon",
           "sub_path": "/axis-media/media.amp?camera=1&videocodec=h264&resolution=1024x1024"}
    p = streams.plan(cam)
    assert p["problems"] == [] and p["suggest"] is None and not p["detected"]
    assert p["sub_path"] == cam["sub_path"] and (p["sub"]["width"], p["sub"]["height"]) == (1024, 1024)
    assert (p["main"]["width"], p["main"]["height"]) == (2160, 2160) and p["encoding"] == "H.264"
    # as before for the ONVIF paths: the listed sub is the 2560x1440 panorama
    onvif = {**CAM, "main_path": CAM["streams"]["profiles"][0]["path"], "sub_path": CAM["streams"]["profiles"][1]["path"]}
    assert streams.plan(onvif)["sub"]["width"] == 2560


def notification(topic: str, items: dict) -> ET.Element:
    data = "".join(f'<tt:SimpleItem Name="{k}" Value="{v}"/>' for k, v in items.items())
    return ET.fromstring(
        '<wsnt:NotificationMessage xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" xmlns:tt="http://www.onvif.org/ver10/schema" '
        'xmlns:tns1="http://www.onvif.org/ver10/topics" xmlns:tnsaxis="http://www.axis.com/2009/event/topics">'
        f'<wsnt:Topic Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet">{topic}</wsnt:Topic>'
        '<wsnt:Message><tt:Message UtcTime="2026-11-06T18:00:00Z" PropertyOperation="Changed"><tt:Source/>'
        f'<tt:Data>{data}</tt:Data></tt:Message></wsnt:Message></wsnt:NotificationMessage>')


def test_object_analytics_events():
    fake = FakeAxis()
    with_camera(fake)
    axis.read_all(CAM)                                                  # caches the scenarios
    puller = ingest.EventPuller(CAM, lambda ev: None)
    ev = puller._parse(notification("tnsaxis:CameraApplicationPlatform/ObjectAnalytics/Device1Scenario1", {"active": "1"}))
    assert ev.state is True and ev.rule == "Back door"
    assert ruleevents.classify(ev.topic, ev.data) == ("object", "person")
    off = puller._parse(notification("tnsaxis:CameraApplicationPlatform/ObjectAnalytics/Device1Scenario1", {"active": "0"}))
    assert off.state is False
    # a scenario for every class, with the event naming its class: the event's class wins
    lot = puller._parse(notification("tnsaxis:CameraApplicationPlatform/ObjectAnalytics/Device1Scenario2", {"active": "1", "classTypes": "vehicle"}))
    assert lot.rule == "Lot" and ruleevents.classify(lot.topic, lot.data) == ("object", "vehicle")
    # counting and the aggregate ANY open nothing
    assert puller._parse(notification("tnsaxis:CameraApplicationPlatform/ObjectAnalytics/Device1Scenario3", {"active": "1"})) is None
    assert puller._parse(notification("tnsaxis:CameraApplicationPlatform/ObjectAnalytics/Device1ScenarioANY", {"active": "1"})) is None
    # a scenario not cached yet still opens, named by its number
    new = puller._parse(notification("tnsaxis:CameraApplicationPlatform/ObjectAnalytics/Device1Scenario7", {"active": "1"}))
    assert new.rule == "Scenario 7"
    # other makers' events are untouched
    reo = puller._parse(notification("tns1:RuleEngine/MyRuleDetector/PeopleDetect", {"State": "true"}))
    assert reo.state is True and reo.rule is None


def test_advisor_findings():
    with_camera(FakeAxis())
    axis.identify(CAM)
    ctx = {"cameras": [CAM]}
    keys = {f.key for f in advisor.check_axis(ctx)}
    assert keys == {"axis:firmware:cam9", "axis:setup:cam9"}
    set_up = {**CAM, "main_path": "/axis-media/media.amp?camera=1&videocodec=h264&analytics=polygon"}
    assert {f.key for f in advisor.check_axis({"cameras": [set_up]})} == {"axis:firmware:cam9"}
    assert advisor.check_axis({"cameras": [{**CAM, "id": "other"}]}) == []          # not known as an AXIS


def test_setup_route_falls_back_to_video_only():
    """The setup route stores the paths, then wants the main stream up within AXIS_READY_S; a camera that won't send
    video and metadata in one session gets its main path back as video only, so recording never stops."""
    import asyncio
    from starlette.requests import Request
    from nvr import device_api
    stored, up = [], {"ok": False}
    real = (device_api._camera, device_api._axis_paths, device_api._stream_up, axis.setup, axis.read_all)

    async def paths(cid, main, sub):
        stored.append((main, sub))

    async def stream_up(cid, since):
        return up["ok"]
    device_api._camera = lambda cid: {**CAM, "main_path": stored[-1][0] if stored else CAM["main_path"]}
    device_api._axis_paths, device_api._stream_up = paths, stream_up
    axis.setup = lambda cam: {"producer": "AnalyticsSceneDescription", "enabled_now": True, "written": ["x"], "sd": "1024x1024",
                              "main_path": "/axis-media/media.amp?camera=1&videocodec=h264&analytics=polygon",
                              "sub_path": "/axis-media/media.amp?camera=1&videocodec=h264&resolution=1024x1024"}
    axis.read_all = lambda cam: {"supported": True, "streams": {"main_path": cam["main_path"]}}
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    try:
        r = asyncio.run(device_api.device_axis_setup("cam9", req))
        assert [m for m, _ in stored] == ["/axis-media/media.amp?camera=1&videocodec=h264&analytics=polygon",
                                          "/axis-media/media.amp?camera=1&videocodec=h264"]
        assert r["setup"]["fallback"] and r["streams"]["main_path"].endswith("videocodec=h264")
        stored.clear()
        up["ok"] = True
        r = asyncio.run(device_api.device_axis_setup("cam9", req))
        assert len(stored) == 1 and "fallback" not in r["setup"] and r["streams"]["main_path"].endswith("analytics=polygon")
    finally:
        device_api._camera, device_api._axis_paths, device_api._stream_up, axis.setup, axis.read_all = real


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
