"""Which streams a camera serves (ONVIF Media / Media2 GetProfiles + GetStreamUri, read-only), what SD live view
pulls (streams.plan), MediaMTX's <id>_sub when a camera has no sub stream (a relay of the main), the RTSP 404 seen
in MediaMTX's log, and the "Check" route. No camera, MediaMTX or network needed: ONVIF replies are captured-style
XML served by a stub.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_streams.py   (from backend/)
"""
import asyncio
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

TMP = Path(tempfile.mkdtemp(prefix="nvr-streams-test-"))
for k, sub in (("NVR_DATA_DIR", "data"), ("NVR_RECORDINGS_DIR", "recordings"), ("NVR_RUNTIME_DIR", "runtime")):
    os.environ[k] = str(TMP / sub)   # never the real server's database, recordings or mediamtx.yml
os.environ.pop("NVR_ALLOWED_HOSTS", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tunnelproto"))

from nvr import mediamtx, streams  # noqa: E402
from nvr import onvif_soap as soap  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402

for d in (settings.data_dir, settings.recordings_dir, settings.runtime_dir):
    v = str(d).replace("\\", "/").lower()
    assert "nvr-streams-test-" in v and "e:/nvr" not in v and "d:/nvr" not in v and "newvms/runtime" not in v, d

CAM = {"id": "ptz", "name": "South PTZ Dome", "host": "10.20.7.31", "username": "u", "password": "p", "main_path": "/main",
       "sub_path": "/sub", "onvif_port": 80, "rtsp_port": 554, "enabled": 1}

NS = ('xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
      'xmlns:tr2="http://www.onvif.org/ver20/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema" '
      'xmlns:tds="http://www.onvif.org/ver10/device/wsdl"')


def envelope(body: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope {NS}><s:Body>{body}</s:Body></s:Envelope>'


def body_of(xml: str) -> ET.Element:
    root = ET.fromstring(xml)
    return next(e for e in root if soap.local(e) == "Body")


def media1_profile(token: str, name: str, enc: str, w: int, h: int, fps: int) -> str:
    return (f'<trt:Profiles token="{token}" fixed="true"><tt:Name>{name}</tt:Name>'
            f'<tt:VideoSourceConfiguration token="VideoSourceToken"><tt:Name>VideoSource</tt:Name><tt:UseCount>2</tt:UseCount>'
            f'<tt:SourceToken>VideoSource_1</tt:SourceToken><tt:Bounds x="0" y="0" width="3840" height="2160"/></tt:VideoSourceConfiguration>'
            f'<tt:VideoEncoderConfiguration token="VideoEncoder_{token}"><tt:Name>VideoEncoder</tt:Name><tt:UseCount>1</tt:UseCount>'
            f'<tt:Encoding>{enc}</tt:Encoding><tt:Resolution><tt:Width>{w}</tt:Width><tt:Height>{h}</tt:Height></tt:Resolution>'
            f'<tt:Quality>4</tt:Quality><tt:RateControl><tt:FrameRateLimit>{fps}</tt:FrameRateLimit><tt:EncodingInterval>1</tt:EncodingInterval>'
            f'<tt:BitrateLimit>8192</tt:BitrateLimit></tt:RateControl></tt:VideoEncoderConfiguration>'
            f'<tt:MetadataConfiguration token="Metadata"><tt:Name>Metadata</tt:Name></tt:MetadataConfiguration></trt:Profiles>')


# Milesight MS-C8164-SPD with its secondary stream off (Qwenbot 2026-10-08): one profile, H.264 3840x2160 at /main
ONE_PROFILE = envelope("<trt:GetProfilesResponse>" + media1_profile("Profile_1", "Profile_1", "H264", 3840, 2160, 25) + "</trt:GetProfilesResponse>")
# a typical two-stream Milesight: H.265 main, H.264 sub
TWO_PROFILES = envelope("<trt:GetProfilesResponse>" + media1_profile("Profile_1", "Profile_1", "H265", 2592, 1520, 25)
                        + media1_profile("Profile_2", "Profile_2", "H264", 1280, 720, 15)
                        # an audio-only profile (no video encoder) is left out
                        + '<trt:Profiles token="Profile_A"><tt:Name>Audio</tt:Name></trt:Profiles>'
                        + "</trt:GetProfilesResponse>")
MEDIA2_PROFILES = envelope(
    '<tr2:GetProfilesResponse>'
    '<tr2:Profiles token="main" fixed="true"><tr2:Name>mainStream</tr2:Name><tr2:Configurations>'
    '<tr2:VideoEncoder token="ve0" GovLength="50" Profile="Main"><tt:Name>ve0</tt:Name><tt:UseCount>1</tt:UseCount>'
    '<tt:Encoding>H265</tt:Encoding><tt:Resolution><tt:Width>3840</tt:Width><tt:Height>2160</tt:Height></tt:Resolution>'
    '<tt:RateControl ConstantBitRate="false"><tt:FrameRateLimit>20.000000</tt:FrameRateLimit><tt:BitrateLimit>6144</tt:BitrateLimit></tt:RateControl>'
    '</tr2:VideoEncoder></tr2:Configurations></tr2:Profiles>'
    '<tr2:Profiles token="sub" fixed="true"><tr2:Name>subStream</tr2:Name><tr2:Configurations>'
    '<tr2:VideoEncoder token="ve1" GovLength="50" Profile="Main"><tt:Name>ve1</tt:Name><tt:UseCount>1</tt:UseCount>'
    '<tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>640</tt:Width><tt:Height>360</tt:Height></tt:Resolution>'
    '<tt:RateControl ConstantBitRate="false"><tt:FrameRateLimit>15</tt:FrameRateLimit></tt:RateControl>'
    '</tr2:VideoEncoder></tr2:Configurations></tr2:Profiles></tr2:GetProfilesResponse>')


def services(media1: bool, media2: bool) -> str:
    svc = '<tds:Service><tds:Namespace>http://www.onvif.org/ver10/device/wsdl</tds:Namespace><tds:XAddr>http://192.168.5.31/onvif/device_service</tds:XAddr></tds:Service>'
    if media1:
        svc += '<tds:Service><tds:Namespace>http://www.onvif.org/ver10/media/wsdl</tds:Namespace><tds:XAddr>http://192.168.5.31/onvif/Media</tds:XAddr></tds:Service>'
    if media2:
        svc += '<tds:Service><tds:Namespace>http://www.onvif.org/ver20/media/wsdl</tds:Namespace><tds:XAddr>http://192.168.5.31/onvif/Media2</tds:XAddr></tds:Service>'
    return envelope(f"<tds:GetServicesResponse>{svc}</tds:GetServicesResponse>")


def uri_reply(uri: str, media2: bool = False) -> str:
    if media2:
        return envelope(f"<tr2:GetStreamUriResponse><tr2:Uri>{uri}</tr2:Uri></tr2:GetStreamUriResponse>")
    return envelope(f"<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>{uri}</tt:Uri><tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
                    f"<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT60S</tt:Timeout></trt:MediaUri></trt:GetStreamUriResponse>")


DATE = envelope("<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:UTCDateTime><tt:Time><tt:Hour>1</tt:Hour><tt:Minute>2</tt:Minute>"
                "<tt:Second>3</tt:Second></tt:Time><tt:Date><tt:Year>2026</tt:Year><tt:Month>10</tt:Month><tt:Day>8</tt:Day></tt:Date>"
                "</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>")


class FakeCamera:
    """Answers Onvif.call like a camera would; records every request (all must be read-only Get* calls)."""

    def __init__(self, media1: bool, media2: bool, profiles: str, uris: dict[str, str], media2_profiles: str | None = None):
        self.media1, self.media2, self.profiles, self.uris, self.media2_profiles = media1, media2, profiles, uris, media2_profiles
        self.calls: list[str] = []

    def __call__(self, client, url, body, auth=True, header="", timeout=None):
        op = body.split(">", 1)[0].lstrip("<").split()[0].rstrip("/")
        self.calls.append(op)
        assert op.split(":")[1].startswith("Get"), f"not read-only: {op}"
        if op == "tds:GetSystemDateAndTime":
            return body_of(DATE)
        if op == "tds:GetServices":
            return body_of(services(self.media1, self.media2))
        if op == "trt:GetProfiles":
            return body_of(self.profiles)
        if op == "tr2:GetProfiles":
            return body_of(self.media2_profiles)
        if op in ("trt:GetStreamUri", "tr2:GetStreamUri"):
            token = body.split("ProfileToken>", 1)[1].split("<", 1)[0]
            return body_of(uri_reply(self.uris[token], media2=op.startswith("tr2")))
        raise soap.OnvifError(f"unexpected {op}")


def with_camera(fake: FakeCamera, cam: dict) -> dict:
    real = soap.Onvif.call
    soap.Onvif.call = lambda client, url, body, **kw: fake(client, url, body, **kw)
    try:
        return streams.probe_streams(cam)
    finally:
        soap.Onvif.call = real


# ------------------------------------------------------------------ parsing / probing

def test_uri_path_strips_host_and_credentials():
    assert streams.uri_path("rtsp://192.168.5.31:554/main") == "/main"
    assert streams.uri_path("rtsp://admin:secret@192.168.5.31/Streaming/Channels/102?transportmode=unicast&profile=Profile_2") == \
        "/Streaming/Channels/102?transportmode=unicast&profile=Profile_2"
    out = streams.uri_path("rtsp://10.0.0.5/cam/realmonitor?channel=1&subtype=1&user=admin&password=secret")
    assert out == "/cam/realmonitor?channel=1&subtype=1" and "secret" not in out
    assert streams.uri_path("") is None and streams.uri_path(None) is None and streams.uri_path("rtsp://host") is None


def test_probe_one_profile_camera():
    fake = FakeCamera(True, False, ONE_PROFILE, {"Profile_1": "rtsp://192.168.5.31:554/main"})
    r = with_camera(fake, CAM)
    assert r["media"] == "media"
    assert r["profiles"] == [{"token": "Profile_1", "name": "Profile_1", "encoding": "H.264", "width": 3840, "height": 2160, "fps": 25, "path": "/main"}]
    assert fake.calls == ["tds:GetSystemDateAndTime", "tds:GetServices", "trt:GetProfiles", "trt:GetStreamUri"]


def test_probe_two_profile_milesight():
    fake = FakeCamera(True, True, TWO_PROFILES, {"Profile_1": "rtsp://192.168.5.31:554/main", "Profile_2": "rtsp://192.168.5.31:554/sub"})
    r = with_camera(fake, CAM)
    assert [(p["token"], p["encoding"], p["width"], p["height"], p["path"]) for p in r["profiles"]] == [
        ("Profile_1", "H.265", 2592, 1520, "/main"), ("Profile_2", "H.264", 1280, 720, "/sub")]
    assert "tr2:GetProfiles" not in fake.calls   # Media answered: Media2 is not asked


def test_probe_media2_only():
    fake = FakeCamera(False, True, "", {"main": "rtsp://192.168.5.40/live/ch00_0", "sub": "rtsp://192.168.5.40/live/ch00_1"}, MEDIA2_PROFILES)
    r = with_camera(fake, CAM)
    assert r["media"] == "media2"
    assert [(p["token"], p["encoding"], p["width"], p["fps"], p["path"]) for p in r["profiles"]] == [
        ("main", "H.265", 3840, 20, "/live/ch00_0"), ("sub", "H.264", 640, 15, "/live/ch00_1")]


def test_probe_sorted_by_resolution():
    # a camera that lists its small stream first: the main is still first in the result
    swapped = envelope("<trt:GetProfilesResponse>" + media1_profile("P2", "sub", "H264", 640, 480, 10)
                       + media1_profile("P1", "main", "H264", 1920, 1080, 25) + "</trt:GetProfilesResponse>")
    r = with_camera(FakeCamera(True, False, swapped, {"P1": "rtsp://h/a", "P2": "rtsp://h/b"}), CAM)
    assert [p["token"] for p in r["profiles"]] == ["P1", "P2"]


def test_no_media_service():
    try:
        with_camera(FakeCamera(False, False, "", {}), CAM)
    except soap.OnvifError as e:
        assert "media" in str(e)
    else:
        raise AssertionError("expected OnvifError")


# ------------------------------------------------------------------ the decision

def prof(token, enc, w, h, path):
    return {"token": token, "encoding": enc, "width": w, "height": h, "fps": 25, "path": path}


ONE = [prof("Profile_1", "H.264", 3840, 2160, "/main")]
TWO = [prof("Profile_1", "H.265", 2592, 1520, "/main"), prof("Profile_2", "H.264", 1280, 720, "/sub")]


def cam_with(profiles=None, **kw):
    st = {"profiles": profiles} if profiles is not None else None
    return {**CAM, **kw, "streams": st}


def test_plan_never_probed_is_unchanged():
    p = streams.plan(cam_with())
    assert p["sub_path"] == "/sub" and p["problems"] == [] and not p["detected"]


def test_plan_main_only_relays_and_warns():
    p = streams.plan(cam_with(ONE))
    assert p["sub_path"] is None and p["main"]["path"] == "/main"
    assert p["problems"] == ["No low-resolution stream: SD plays the main stream (3840×2160). "
                             "Enable the camera's secondary stream for faster live view."]
    v = streams.view(cam_with(ONE))
    assert v["sd"] == {"path": None, "relay": True, "detected": False} and v["sub"] is None


def test_plan_configured_sub_listed():
    p = streams.plan(cam_with(TWO))
    assert p["sub_path"] == "/sub" and not p["detected"] and p["problems"] == [] and p["sub"]["width"] == 1280


def test_plan_detects_another_sub_path():
    # the camera serves its sub stream elsewhere: SD uses it, the user's field is left as it is
    profiles = [prof("1", "H.265", 3840, 2160, "/main"), prof("2", "H.265", 1920, 1080, "/stream2"), prof("3", "H.264", 704, 480, "/stream3")]
    p = streams.plan(cam_with(profiles))
    assert p["sub_path"] == "/stream3" and p["detected"] and p["problems"] == []   # H.264 first: browsers play it
    p = streams.plan(cam_with([profiles[0], profiles[1]]))
    assert p["sub_path"] == "/stream2" and p["detected"]


def test_plan_query_paths_match():
    profiles = [prof("1", "H.264", 1920, 1080, "/Streaming/Channels/101?transportmode=unicast&profile=Profile_1"),
                prof("2", "H.264", 640, 360, "/Streaming/Channels/102?transportmode=unicast&profile=Profile_2")]
    p = streams.plan(cam_with(profiles, main_path="/Streaming/Channels/101", sub_path="/Streaming/Channels/102"))
    assert p["sub_path"] == "/Streaming/Channels/102" and p["problems"] == [] and p["main"]["token"] == "1"
    assert streams.same_path("/cam/realmonitor?channel=1&subtype=0", "/cam/realmonitor?channel=1&subtype=0&unicast=true")
    assert not streams.same_path("/cam/realmonitor?channel=1&subtype=0", "/cam/realmonitor?channel=1&subtype=1")


def test_plan_main_not_listed_is_a_problem_not_a_change():
    p = streams.plan(cam_with(TWO, main_path="/h264"))
    assert any("does not list the main stream path /h264" in x for x in p["problems"])
    assert p["sub_path"] == "/sub"
    cfg = mediamtx.build_config([cam_with(TWO, main_path="/h264")])["paths"]
    assert cfg["ptz"]["source"].endswith("/h264")   # the configured main path is never changed silently


def test_plan_unsafe_detected_path_is_never_used():
    profiles = [prof("1", "H.264", 1920, 1080, "/main"), prof("2", "H.264", 640, 360, "/x@evil.example:554/y")]
    p = streams.plan(cam_with(profiles))
    assert p["sub_path"] is None and p["problems"]


def test_plan_404_falls_back():
    cam = {**cam_with(TWO), "streams": {"profiles": TWO, "sub_not_found": {"path": "/sub", "at": 1.0}}}
    p = streams.plan(cam)
    assert p["sub_path"] is None and p["problems"][0].startswith("No low-resolution stream")
    # never probed, MediaMTX saw the 404: relay too
    p = streams.plan({**CAM, "streams": {"sub_not_found": {"path": "/sub", "at": 1.0}}})
    assert p["sub_path"] is None and p["problems"] == [streams.no_sub_text()]
    # a 404 on a path that is no longer configured does not matter
    assert streams.plan({**CAM, "sub_path": "/sub2", "streams": {"sub_not_found": {"path": "/sub", "at": 1.0}}})["sub_path"] == "/sub2"


# ------------------------------------------------------------------ MediaMTX config

def test_build_config_main_only_relays_main():
    p = mediamtx.build_config([cam_with(ONE)])["paths"]
    assert set(p) == {"ptz", "ptz_sub"}
    assert p["ptz"] == {"source": "rtsp://u:p@10.20.7.31:554/main", "rtspTransport": "tcp", "record": True}
    relay = p["ptz_sub"]
    # the relay of the recorded main stream from this MediaMTX, still on demand (sourceOnDemand is unchanged)
    assert relay["source"] == mediamtx.local_url("ptz") and relay["source"].endswith("127.0.0.1:8554/ptz")
    assert relay["sourceOnDemand"] is True and "record" not in relay
    assert mediamtx.camera_paths(cam_with(ONE)) == ["ptz"]   # the relay costs no bandwidth from the site
    assert mediamtx.sub_relays_main(cam_with(ONE)) and not mediamtx.sub_relays_main(cam_with(TWO))


def test_build_config_unchanged_with_sub():
    for cam in (cam_with(), cam_with(TWO), CAM):
        p = mediamtx.build_config([cam])["paths"]
        assert p["ptz_sub"]["source"] == "rtsp://u:p@10.20.7.31:554/sub" and p["ptz_sub"]["sourceOnDemand"] is True
        assert mediamtx.camera_paths(cam) == ["ptz", "ptz_sub"]


def test_build_config_detected_sub():
    profiles = [prof("1", "H.265", 3840, 2160, "/main"), prof("2", "H.264", 704, 480, "/stream2")]
    p = mediamtx.build_config([cam_with(profiles)])["paths"]
    assert p["ptz_sub"]["source"] == "rtsp://u:p@10.20.7.31:554/stream2"
    # record_stream "sub" records the detected sub stream
    p = mediamtx.build_config([cam_with(profiles, record_stream="sub")])["paths"]
    assert p["ptz"]["source"] == "rtsp://u:p@10.20.7.31:554/stream2" and p["ptz_hd"]["source"].endswith("/main")


def test_build_config_record_sub_without_sub_stream():
    cam = cam_with(ONE, record_stream="sub")
    p = mediamtx.build_config([cam])["paths"]
    assert p["ptz"] == {"source": "rtsp://u:p@10.20.7.31:554/main", "rtspTransport": "tcp", "record": True}
    assert p["ptz_sub"]["source"] == mediamtx.local_url("ptz") == p["ptz_hd"]["source"]
    assert mediamtx.camera_paths(cam) == ["ptz"]


# ------------------------------------------------------------------ stored state, MediaMTX's 404, the checker

def _store(cam: dict) -> None:
    db.upsert_camera({k: v for k, v in cam.items() if k != "streams"})
    streams.save(cam["id"], {}, None)


def _get(cid: str) -> dict:
    return next(c for c in db.cameras() if c["id"] == cid)


def test_record_probe_and_404():
    _store(CAM)
    assert streams.needs_probe(_get("ptz"))
    streams.record_probe("ptz", {"profiles": TWO, "media": "media"}, now=1000.0)
    cam = _get("ptz")
    assert cam["streams_checked_at"] == 1000.0 and streams.sub_source(cam) == "/sub" and not streams.needs_probe(cam)
    assert streams.scan_log("2026/10/08 19:58:14 ERR [path ptz_sub] [RTSP source] bad status code: 404 (Not Found)\n"
                            "2026/10/08 19:58:15 INF [path ptz] [RTSP source] ready: 1 track (H264)\n"
                            "2026/10/08 19:58:16 ERR [path other_cam_sub] [RTSP source] bad status code: 453 (Not Enough Bandwidth)\n") == {"ptz": "404", "other_cam": "453"}
    assert streams.mark_sub_not_found("ptz") is True
    assert streams.sub_source(_get("ptz")) is None
    assert streams.mark_sub_not_found("ptz") is False       # already relaying
    assert streams.mark_sub_not_found("nope") is False
    # a failed probe keeps what was known
    streams.record_probe("ptz", None, error="connection failed: timed out", now=2000.0)
    st = streams.state_of(_get("ptz"))
    assert st["profiles"] == TWO and st["error"].startswith("connection failed") and st["sub_not_found"]["path"] == "/sub"
    # a manual check that succeeds gives the sub stream another chance
    streams.record_probe("ptz", {"profiles": TWO, "media": "media"}, clear_404=True)
    assert streams.sub_source(_get("ptz")) == "/sub"
    # edited paths clear the 404 too; another address forgets everything
    streams.mark_sub_not_found("ptz")
    streams.clear_404("ptz")
    assert streams.sub_source(_get("ptz")) == "/sub"
    streams.forget("ptz")
    assert _get("ptz")["streams_checked_at"] is None and streams.needs_probe(_get("ptz"))


def test_453_relays_the_main_stream_whatever_the_profiles():
    """Qwenbot's SW Corner PTZ (2026-10-08): its sub stream exists, but the camera answered 453 Not Enough Bandwidth
    because something else held its connections. Any other sub profile would be refused too: relay the main."""
    _store(CAM)
    streams.record_probe("ptz", {"profiles": TWO, "media": "media"}, now=1000.0)
    assert streams.sub_source(_get("ptz")) == "/sub"
    assert streams.mark_sub_not_found("ptz", code="453") is True
    p = streams.plan(_get("ptz"))
    assert p["sub_path"] is None and p["problems"] == [streams.busy_text()]
    streams.record_probe("ptz", {"profiles": TWO, "media": "media"}, clear_404=True)   # Check: try the sub again
    assert streams.sub_source(_get("ptz")) == "/sub"


def test_failed_probe_retried_later():
    _store({**CAM, "id": "off1"})
    streams.record_probe("off1", None, error="connection failed", now=1000.0)
    cam = _get("off1")
    assert not streams.needs_probe(cam, now=1000.0 + 60) and streams.needs_probe(cam, now=1000.0 + streams.RETRY_FAILED_S)
    assert not streams.needs_probe({**cam, "enabled": 0}, now=10 ** 10)


def test_health_problem_from_streams():
    from nvr.health import StreamHealth, stream_problems
    _store({**CAM, "id": "dome"})
    streams.record_probe("dome", {"profiles": ONE, "media": "media"})
    assert stream_problems("dome") == [streams.no_sub_text(3840, 2160)]
    h = StreamHealth().camera("dome")
    # a warning, not a problem: the hub opens camera_down for any problem, and this camera works
    assert streams.no_sub_text(3840, 2160) in h["warnings"] and streams.no_sub_text(3840, 2160) not in h["problems"]


def test_watcher_tails_the_log():
    _store({**CAM, "id": "cam4"})
    streams.record_probe("cam4", {"profiles": TWO, "media": "media"})
    log = settings.runtime_dir / "mediamtx.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("2026/10/08 19:00:00 ERR [path cam4_sub] [RTSP source] bad status code: 404 (Not Found)\n")
    checks: list[str] = []
    w = streams.StreamChecker(lambda: None, log)
    w.check_soon = checks.append
    assert w.watch_once() is False                      # what was logged before it started is not re-read
    with open(log, "a") as f:
        f.write("2026/10/08 19:58:14 INF [path cam4_sub] [RTSP source] started on demand\n"
                "2026/10/08 19:58:15 ERR [path cam4_sub] [RTSP source] bad status code: 404 (Not Found)\n")
    assert w.watch_once() is True and checks == ["cam4"]
    assert streams.sub_source(_get("cam4")) is None
    assert w.watch_once() is False


def test_checker_applies_a_probe():
    _store({**CAM, "id": "dome2"})
    changes: list[int] = []
    w = streams.StreamChecker(lambda: changes.append(1), None)
    real = streams.probe_streams
    streams.probe_streams = lambda cam, timeout=streams.CALL_TIMEOUT_S: {"profiles": ONE, "media": "media"}
    try:
        v = asyncio.run(w.check("dome2", manual=True))
    finally:
        streams.probe_streams = real
    assert v["sd"]["relay"] is True and v["main"]["width"] == 3840 and changes == [1]


def test_check_route():
    from starlette.testclient import TestClient

    from nvr import api, hub_agent
    api.state.hub = hub_agent.HubAgent(api.app, SimpleNamespace())
    api.state.pipeline = SimpleNamespace(subscribers=set())
    api.state.streams = streams.StreamChecker(lambda: None, None)
    _store({**CAM, "id": "dome3"})
    real = streams.probe_streams
    seen = []
    streams.probe_streams = lambda cam, timeout=streams.CALL_TIMEOUT_S: seen.append(cam["id"]) or {"profiles": TWO, "media": "media"}
    try:
        client = TestClient(api.app, base_url="http://localhost:8080")
        h = {"Origin": "http://localhost:8080"}
        r = client.post("/api/cameras/dome3/streams/check", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["sd"] == {"path": "/sub", "relay": False, "detected": False} and body["sub"]["width"] == 1280 and seen == ["dome3"]
        assert client.post("/api/cameras/nope/streams/check", headers=h).status_code == 404
        pub = api.public_camera(_get("dome3"))
        assert pub["streams"]["main"]["width"] == 2592 and "password" not in pub and "u:p@" not in str(pub)
    finally:
        streams.probe_streams = real


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
