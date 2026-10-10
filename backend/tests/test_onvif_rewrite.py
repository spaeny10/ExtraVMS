"""Port-forward mode: a camera reached through the site router's forwards reports its internal address in every
URL it returns (XAddrs, the PullPoint subscription manager, stream URIs). onvif_soap.rewrite maps them to the
outside address; with the public fields empty nothing changes. Replies are captured-style fixtures
(tests/fixtures/onvif); the end-to-end tests run a fake camera on 127.0.0.1 that hands out 192.168.105.12 URLs.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_onvif_rewrite.py   (from backend/)
"""
import asyncio
import os
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-rewrite-test-")  # never the real DB
os.environ["NVR_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="nvr-rewrite-runtime-")      # nor the running server's
os.environ["NVR_RECORDINGS_DIR"] = tempfile.mkdtemp(prefix="nvr-rewrite-rec-")       # MediaMTX config / recordings
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pydantic  # noqa: E402

from nvr import ingest, mediamtx, onvif_soap, ptz  # noqa: E402
from nvr.onvif_soap import find, rewrite, text  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "onvif"
INTERNAL = {"id": "gate", "name": "Gate", "host": "192.168.105.12", "onvif_port": 80, "rtsp_port": 554,
            "username": "admin", "password": "pw", "main_path": "/Streaming/Channels/101", "sub_path": "/Streaming/Channels/102"}
FORWARDED = {**INTERNAL, "public_host": "203.0.113.50", "public_onvif_port": 8081, "public_rtsp_port": 5541}


def body(name: str) -> ET.Element:
    root = ET.fromstring((FIX / name).read_bytes())
    return next(e for e in root if onvif_soap.local(e) == "Body")


def test_rewrite_rules():
    sub = "http://192.168.105.12:80/onvif/Events/SubManager_1"
    assert rewrite(sub, FORWARDED) == "http://203.0.113.50:8081/onvif/Events/SubManager_1"
    assert rewrite("http://192.168.105.12/onvif/PTZ", FORWARDED) == "http://203.0.113.50:8081/onvif/PTZ"   # default port 80
    assert rewrite("rtsp://192.168.105.12:554/Streaming/Channels/101?transportmode=unicast&profile=Profile_1", FORWARDED) == \
        "rtsp://203.0.113.50:5541/Streaming/Channels/101?transportmode=unicast&profile=Profile_1"
    assert rewrite("rtsp://192.168.105.12/live", FORWARDED) == "rtsp://203.0.113.50:5541/live"            # default 554
    assert rewrite("rtsp://u:p@192.168.105.12:554/x", FORWARDED) == "rtsp://u:p@203.0.113.50:5541/x"     # user info kept
    # outside ports left empty: same port numbers outside as inside
    same_ports = {**INTERNAL, "public_host": "cam5.example.net"}
    assert rewrite("http://192.168.105.12:8000/onvif/event_service", same_ports) == "http://cam5.example.net:8000/onvif/event_service"
    assert rewrite("http://192.168.105.12/onvif/Media", same_ports) == "http://cam5.example.net/onvif/Media"
    # VPN mode / same LAN (no public_host): untouched, as are other schemes and empty values
    for cam in (INTERNAL, {**INTERNAL, "public_host": None}, {**INTERNAL, "public_host": ""}, None):
        assert rewrite(sub, cam) == sub
    assert rewrite("urn:uuid:1234", FORWARDED) == "urn:uuid:1234" and rewrite(None, FORWARDED) is None and rewrite("", FORWARDED) == ""


def test_added_by_its_public_address():
    """Dave's camera (2026-10-08): added as 5001.bigview.ai, RTSP 556, ONVIF 8082 with no outside fields; it answered
    on 8082 and handed back http://192.168.50.37:80/... for every service, so events and PTZ timed out."""
    dave = {**INTERNAL, "host": "5001.bigview.ai", "onvif_port": 8082, "rtsp_port": 556}
    assert onvif_soap.auto_forward(dave)
    assert rewrite("http://192.168.50.37:80/onvif/Events", dave) == "http://5001.bigview.ai:8082/onvif/Events"
    assert rewrite("http://192.168.50.37/onvif/PTZ", dave) == "http://5001.bigview.ai:8082/onvif/PTZ"
    assert rewrite("rtsp://192.168.50.37:554/main", dave) == "rtsp://5001.bigview.ai:556/main"
    assert rewrite("rtsp://192.168.50.37:555/onvifreplay", dave) == "rtsp://5001.bigview.ai:555/onvifreplay"   # replay keeps its port
    assert rewrite("rtsp://192.168.50.37:555/onvifreplay", {**dave, "public_replay_port": 5555}) == "rtsp://5001.bigview.ai:5555/onvifreplay"
    # a public IP works the same; a public URL it reports, or its own name, is left alone
    by_ip = {**dave, "host": "162.190.144.15"}
    assert rewrite("http://192.168.50.37/onvif/Media", by_ip) == "http://162.190.144.15:8082/onvif/Media"
    assert rewrite("http://5001.bigview.ai:8082/onvif/Media", dave) == "http://5001.bigview.ai:8082/onvif/Media"
    assert rewrite("http://8.8.4.4/onvif/Media", dave) == "http://8.8.4.4/onvif/Media"
    # cameras added by a private address (same LAN, VPN) and explicit outside fields behave as before
    assert not onvif_soap.auto_forward(INTERNAL) and not onvif_soap.auto_forward(FORWARDED)
    assert rewrite("http://10.0.0.7/onvif/Events", INTERNAL) == "http://10.0.0.7/onvif/Events"
    assert onvif_soap.outside(dave) == ("5001.bigview.ai", 8082, 556)
    # outside ports set without an outside host: the URLs take the ports we connect to (outside()), as camera_url does
    ports = {**dave, "public_onvif_port": 18082, "public_rtsp_port": 1556}
    assert onvif_soap.outside(ports) == ("5001.bigview.ai", 18082, 1556)
    assert rewrite("http://192.168.50.37:80/onvif/Events", ports) == "http://5001.bigview.ai:18082/onvif/Events"
    assert rewrite("rtsp://192.168.50.37:554/main", ports) == "rtsp://5001.bigview.ai:1556/main"
    assert rewrite("rtsp://192.168.50.37:555/onvifreplay", ports) == "rtsp://5001.bigview.ai:555/onvifreplay"


def test_lan_names_and_vpn_addresses_are_not_rewritten():
    """Only a camera added by a public IP or a public-looking DNS name is taken to be behind a port forward: a LAN name,
    a single label, a CGNAT / VPN address (100.64/10) or loopback reaches the camera's own reported address directly."""
    reported = "http://192.168.50.37/onvif/Events"
    for host in ("cam1.lan", "nvr.local", "gate.home.arpa", "cam.internal", "camera7", "CAM7.LOCAL.", "100.72.1.5",
                 "100.64.0.1", "127.0.0.1", "localhost", "169.254.3.4", "10.1.2.3"):
        cam = {**INTERNAL, "host": host}
        assert not onvif_soap.auto_forward(cam), host
        assert rewrite(reported, cam) == reported, host
    for host in ("5001.bigview.ai", "162.190.144.15", "cam5.example.net"):
        assert onvif_soap.auto_forward({**INTERNAL, "host": host}), host
        assert rewrite(reported, {**INTERNAL, "host": host}).startswith(f"http://{host}:80/"), host
    # forward mode (an outside host set) rewrites whatever the camera was added by
    assert rewrite(reported, {**INTERNAL, "host": "100.72.1.5", "public_host": "203.0.113.50"}) == \
        "http://203.0.113.50/onvif/Events"


def test_outside_and_camera_url():
    assert onvif_soap.outside(INTERNAL) == ("192.168.105.12", 80, 554)
    assert onvif_soap.outside(FORWARDED) == ("203.0.113.50", 8081, 5541)
    assert onvif_soap.outside({**INTERNAL, "public_host": "203.0.113.50"}) == ("203.0.113.50", 80, 554)
    assert mediamtx.camera_url(INTERNAL, "/Streaming/Channels/101") == "rtsp://admin:pw@192.168.105.12:554/Streaming/Channels/101"
    assert mediamtx.camera_url(FORWARDED, "/Streaming/Channels/101") == "rtsp://admin:pw@203.0.113.50:5541/Streaming/Channels/101"
    cfg = mediamtx.build_config([FORWARDED])
    assert cfg["paths"]["gate"]["source"] == "rtsp://admin:pw@203.0.113.50:5541/Streaming/Channels/101"
    assert cfg["paths"]["gate_sub"]["source"] == "rtsp://admin:pw@203.0.113.50:5541/Streaming/Channels/102"
    c = onvif_soap.Onvif.for_camera(FORWARDED)
    assert c.device_url == "http://203.0.113.50:8081/onvif/device_service"
    assert onvif_soap.Onvif.for_camera(INTERNAL).device_url == "http://192.168.105.12:80/onvif/device_service"


def test_captured_replies_rewritten():
    svc = onvif_soap.parse_services(body("GetServicesResponse.xml"))
    assert svc["events"] == "http://192.168.105.12:80/onvif/Events" and svc["deviceio"] == "http://192.168.105.12/onvif/DeviceIO"
    out = {k: rewrite(v, FORWARDED) for k, v in svc.items()}
    assert set(out) >= {"device", "media", "media2", "events", "ptz", "imaging", "deviceio", "analytics"}
    assert all(v.startswith("http://203.0.113.50:8081/onvif/") for v in out.values()), out
    caps = onvif_soap.parse_capabilities(body("GetCapabilitiesResponse.xml"))
    assert caps["deviceio"] == "http://192.168.1.64:8000/onvif/deviceIO_service" and caps["ptz"].endswith("/onvif/ptz_service")
    cam8000 = {**INTERNAL, "host": "192.168.1.64", "onvif_port": 8000, "public_host": "198.51.100.7", "public_onvif_port": 8085}
    assert {k: rewrite(v, cam8000) for k, v in caps.items()}["events"] == "http://198.51.100.7:8085/onvif/event_service"
    sub = text(find(body("CreatePullPointSubscriptionResponse.xml"), "SubscriptionReference"), "Address")
    assert rewrite(sub, FORWARDED) == "http://203.0.113.50:8081/onvif/Events/SubManager_1"
    uri = text(body("GetStreamUriResponse.xml"), "Uri")
    assert rewrite(uri, FORWARDED) == "rtsp://203.0.113.50:5541/Streaming/Channels/101?transportmode=unicast&profile=Profile_1"


# ---------------------------------------------------------------- a fake camera behind a port forward

NODES = """<?xml version="1.0"?><env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"
 xmlns:tt="http://www.onvif.org/ver10/schema"><env:Body><tptz:GetNodesResponse><tptz:PTZNode token="PTZNODETOKEN"><tt:Name>PTZ</tt:Name>
<tt:SupportedPTZSpaces><tt:ContinuousPanTiltVelocitySpace><tt:URI>http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace</tt:URI>
</tt:ContinuousPanTiltVelocitySpace></tt:SupportedPTZSpaces><tt:MaximumNumberOfPresets>255</tt:MaximumNumberOfPresets><tt:HomeSupported>true</tt:HomeSupported>
</tptz:PTZNode></tptz:GetNodesResponse></env:Body></env:Envelope>"""
EMPTY = """<?xml version="1.0"?><env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope"><env:Body><Ok/></env:Body></env:Envelope>"""
REPLIES = {"GetSystemDateAndTime": "GetSystemDateAndTimeResponse.xml", "GetServices": "GetServicesResponse.xml",
           "CreatePullPointSubscription": "CreatePullPointSubscriptionResponse.xml", "PullMessages": "PullMessagesResponse.xml",
           "GetProfiles": "GetProfilesResponse.xml", "GetDeviceInformation": "GetDeviceInformationResponse.xml"}


class FakeCamera(BaseHTTPRequestHandler):
    seen: list[tuple[str, str, str]] = []   # (path, operation, wsa:To)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        root = ET.fromstring(raw)
        op = onvif_soap.local(next(iter(next(e for e in root if onvif_soap.local(e) == "Body"))))
        FakeCamera.seen.append((self.path, op, text(root, "To") or ""))
        reply = (FIX / REPLIES[op]).read_bytes() if op in REPLIES else (NODES if op == "GetNodes" else EMPTY).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/soap+xml")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *a):
        pass


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeCamera)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_event_subscription_through_the_forward():
    """The camera hands out http://192.168.105.12:80/... for its events service and subscription manager; every
    request must still reach the forward (here 127.0.0.1:<port>), with wsa:To naming the camera's own address."""
    srv = _serve()
    FakeCamera.seen = []
    cam = {**INTERNAL, "public_host": "127.0.0.1", "public_onvif_port": srv.server_address[1]}
    got = []
    puller = ingest.EventPuller(cam, lambda ev: (got.append(ev), puller.stop_event.set()))
    try:
        puller._session()
    finally:
        srv.shutdown()
    assert got and got[0].rule == "Gate" and got[0].state is True and got[0].topic == "RuleEngine/FieldDetector/ObjectsInside"
    ops = [(p, op) for p, op, _ in FakeCamera.seen]
    assert ("/onvif/device_service", "GetServices") in ops and ("/onvif/Events", "CreatePullPointSubscription") in ops
    pull = next(s for s in FakeCamera.seen if s[1] == "PullMessages")
    assert pull[0] == "/onvif/Events/SubManager_1" and pull[2] == "http://192.168.105.12:80/onvif/Events/SubManager_1"


def test_ptz_probe_through_the_forward():
    srv = _serve()
    FakeCamera.seen = []
    cam = {**INTERNAL, "public_host": "127.0.0.1", "public_onvif_port": srv.server_address[1]}
    p = ptz.PtzCamera(cam)
    try:
        asyncio.run(p.probe())
    finally:
        srv.shutdown()
    assert p.available and p.profile == "Profile_1", p.caps
    assert all(v.startswith(f"http://127.0.0.1:{srv.server_address[1]}/onvif/") for v in p.onvif.services.values()), p.onvif.services
    paths = {path for path, _, _ in FakeCamera.seen}
    assert {"/onvif/Media", "/onvif/PTZ", "/onvif/DeviceIO"} <= paths, paths


def test_validation():
    from nvr.api import CameraIn
    assert mediamtx.camera_problem(FORWARDED) is None and mediamtx.camera_problem(INTERNAL) is None
    assert "outside address" in mediamtx.camera_problem({**INTERNAL, "public_host": "evil@host"})
    assert "public_rtsp_port" in mediamtx.camera_problem({**INTERNAL, "public_host": "1.2.3.4", "public_rtsp_port": 70000})
    assert "record_stream" in mediamtx.camera_problem({**INTERNAL, "record_stream": "both"})
    c = CameraIn(id="gate", name="Gate", host="192.168.105.12", public_host="", public_rtsp_port=0, public_onvif_port=None)
    assert c.public_host is None and c.public_rtsp_port is None and c.record_stream == "main"
    c = CameraIn(id="gate", name="Gate", host="192.168.105.12", public_host="203.0.113.50", public_rtsp_port=5541, record_stream="sub")
    assert c.public_host == "203.0.113.50" and c.public_rtsp_port == 5541 and c.record_stream == "sub"
    for bad in ({"public_host": "a b"}, {"public_host": "x", "public_onvif_port": 0.5}, {"public_onvif_port": 65536}, {"record_stream": "hd"}):
        try:
            CameraIn(id="gate", name="Gate", host="192.168.105.12", **bad)
        except pydantic.ValidationError:
            continue
        raise AssertionError(f"accepted {bad}")


def test_port_forward_fields_stored_and_kept():
    """PUT without the new fields (an older client) keeps them; export / handoff / merge carry them."""
    from nvr import siteconfig
    from nvr.api import CameraIn, put_camera, state  # noqa: F401
    from nvr.db import db
    db.upsert_camera({**FORWARDED, "record_stream": "sub"})
    row = next(c for c in db.cameras() if c["id"] == "gate")
    assert (row["public_host"], row["public_rtsp_port"], row["public_onvif_port"], row["record_stream"]) == ("203.0.113.50", 5541, 8081, "sub")
    exported = next(c for c in siteconfig.export_config()["cameras"] if c["id"] == "gate")
    assert exported["public_host"] == "203.0.113.50" and exported["record_stream"] == "sub" and "password" not in exported
    hand = siteconfig.handoff(["gate"])
    assert hand["cameras"][0]["public_rtsp_port"] == 5541
    db.execute("DELETE FROM cameras WHERE id='gate'")
    siteconfig.merge_cameras(hand)
    row = next(c for c in db.cameras() if c["id"] == "gate")
    assert row["public_onvif_port"] == 8081 and row["record_stream"] == "sub"
    # the PUT route: fields the client didn't send are kept, sent ones replace them ("" clears)
    import nvr.api as api

    class Ptz:
        def reset(self, cid): pass

    class Pipe:
        def queue_missing_synopses(self, cid): pass
    saved = {k: getattr(api.state, k, None) for k in ("ptz", "pipeline")}
    api.state.ptz, api.state.pipeline = Ptz(), Pipe()
    orig_sync = api.sync_cameras
    api.sync_cameras = lambda: None
    try:
        body = {k: v for k, v in INTERNAL.items() if k != "password"}
        out = asyncio.run(put_camera("gate", CameraIn(**body)))
        assert out["public_host"] == "203.0.113.50" and out["public_rtsp_port"] == 5541 and out["record_stream"] == "sub", out
        out = asyncio.run(put_camera("gate", CameraIn(**body, public_host="", record_stream="main")))
        assert out["public_host"] is None and out["record_stream"] == "main" and out["public_rtsp_port"] == 5541, out
    finally:
        api.sync_cameras = orig_sync
        for k, v in saved.items():
            setattr(api.state, k, v)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
