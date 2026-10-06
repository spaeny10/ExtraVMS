"""Security fixes of the site server: SPA path traversal, hub URL change un-enrolls, browser guards (Host allow-list,
cross-site writes, body types, WebSocket origin), the unforgeable tunnel marker, TURN credentials, camera
path validation and the MediaMTX reader password. No MediaMTX, models or network needed (one test starts a
uvicorn listener on 127.0.0.1).

Run: ..\\.venv\\Scripts\\python.exe tests\\test_security.py   (from backend/)
"""
import asyncio
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-security-test-")  # never the real DB
os.environ["NVR_HUB_URL"] = "wss://hub.axiomvision.ai/agent"
os.environ.pop("NVR_ALLOWED_HOSTS", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tunnelproto"))

import httpx  # noqa: E402
import pydantic  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from nvr import api, direct, hub_agent, lan_guard, mediamtx, siteconfig  # noqa: E402
from nvr.config import ROOT, settings  # noqa: E402
from nvr.db import db  # noqa: E402

HOST = "localhost:8080"
SAME = "http://localhost:8080"
api.state.hub = hub_agent.HubAgent(api.app, SimpleNamespace())
api.state.pipeline = SimpleNamespace(subscribers=set())


def run(coro):
    return asyncio.run(coro)


async def _call(app, method, url, client=("192.168.1.50", 50000), **kw):
    transport = httpx.ASGITransport(app=app, client=client, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=f"http://{HOST}") as c:
        return await c.request(method, url, **kw)


def call(method, url, **kw):
    return run(_call(api.app, method, url, **kw))


def raw_get(path: str, headers: dict | None = None) -> tuple[int, bytes, dict]:
    """GET with exactly this (already percent-decoded, as uvicorn passes it) scope path: no client-side URL
    normalization in the way."""
    hdrs = [(b"host", HOST.encode())] + [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": path, "raw_path": path.encode("latin-1", "replace"), "query_string": b"", "root_path": "",
             "headers": hdrs, "client": ("192.168.1.50", 50000), "server": ("localhost", 8080)}
    out: dict = {"status": None, "body": b"", "headers": {}}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = {k.decode().lower(): v.decode() for k, v in msg.get("headers", [])}
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    run(api.app(scope, receive, send))
    return out["status"], out["body"], out["headers"]


# ---------------------------------------------------------------- 1. path traversal

DIST = ROOT / "frontend" / "dist"
SECRET_MARKERS = (b"[extensions]", b"root:", b"Axiom", b"SQLite format", b"PRIVATE KEY")


def test_spa_traversal_is_404():
    assert DIST.is_dir(), "frontend/dist missing: build the UI first (npm run build)"
    readme = (ROOT / "README.md").read_bytes()[:200]
    for path in ("/../../README.md", "/../README.md", "/..%2F..%2FREADME.md".replace("%2F", "/"),
                 "/%2E%2E/%2E%2E/README.md".replace("%2E", "."), "/..\\..\\README.md", "/assets\\..\\..\\..\\README.md",
                 "/C:/Windows/win.ini", "/c:\\windows\\win.ini", "//etc/passwd", "//C:/Windows/win.ini",
                 "/api/../../../README.md", "/api/../../data/nvr.db", "/../../data/nvr.db", "/../../data/tls/key.pem",
                 "/index.html::$DATA", "/index.html:Zone.Identifier", "/icons/../../../README.md", "/nope.js",
                 "/data/nvr.db", "/\x00index.html"):
        status, body, _ = raw_get(path)
        assert status == 404, (path, status, body[:80])
        assert readme not in body and b"SQLite format" not in body and b"PRIVATE KEY" not in body, path


def test_spa_over_http_client_too():
    # the same through a real client (it percent-encodes / normalizes as it likes): never a repo file
    readme = (ROOT / "README.md").read_bytes()[:200]
    for url in ("/..%2F..%2FREADME.md", "/%2E%2E%2F%2E%2E%2FREADME.md", "/..%5C..%5CREADME.md", "/C:%2FWindows%2Fwin.ini",
                "/%2F%2Fetc%2Fpasswd", "/api/..%2F..%2F..%2FREADME.md", "/..%2F..%2Fdata%2Fnvr.db"):
        r = call("GET", url)
        assert r.status_code == 404 and readme not in r.content and b"SQLite format" not in r.content, (url, r.status_code)


def test_unknown_api_path_is_json_404():
    for path in ("/api/nonexistent", "/api", "/api/", "/api/events/x/y/z"):
        status, body, hdrs = raw_get(path)
        assert status == 404 and hdrs["content-type"].startswith("application/json"), (path, status)
        assert json.loads(body) == {"detail": "Not Found"}


def test_ui_still_served():
    status, body, hdrs = raw_get("/")
    assert status == 200 and b"<html" in body.lower() and hdrs.get("cache-control") == "no-cache"
    for route in ("/settings", "/events/123", "/find"):          # client-side routes: index.html
        status, body2, _ = raw_get(route)
        assert status == 200 and body2 == body, route
    for name in ("index.html", "sw.js", "manifest.webmanifest", "axiom.webp"):
        if (DIST / name).is_file():
            status, b, hdrs = raw_get(f"/{name}")
            assert status == 200 and b == (DIST / name).read_bytes(), name
    asset = next((DIST / "assets").iterdir())
    r = call("GET", f"/assets/{asset.name}")
    assert r.status_code == 200 and r.content == asset.read_bytes()
    icons = DIST / "icons"
    if icons.is_dir() and any(icons.iterdir()):
        icon = next(f for f in icons.iterdir() if f.is_file())
        status, b, _ = raw_get(f"/icons/{icon.name}")
        assert status == 200 and b == icon.read_bytes()


def test_contained_helper():
    base = Path(tempfile.mkdtemp(prefix="nvr-contain-"))
    (base / "sub").mkdir()
    (base / "sub" / "a.jpg").write_bytes(b"x")
    (base.parent / "outside.txt").write_text("secret")
    assert api.contained(base, "sub/a.jpg") == (base / "sub" / "a.jpg").resolve()
    for rel in ("", "sub", "../outside.txt", "sub/../../outside.txt", "/etc/passwd", "\\x", "sub\\a.jpg",
                "C:/Windows/win.ini", "c:x", "sub/a.jpg::$DATA", "sub/a.jpg\x00", str(base.parent / "outside.txt"), "sub/missing.jpg"):
        assert api.contained(base, rel) is None, rel


def test_event_media_names():
    d = settings.data_dir / "events" / "7"
    d.mkdir(parents=True, exist_ok=True)
    (d / "snapshot.jpg").write_bytes(b"\xff\xd8jpeg")
    r = call("GET", "/api/events/7/media/snapshot.jpg")
    assert r.status_code == 200 and r.content == b"\xff\xd8jpeg"
    assert call("GET", "/api/events/7/media/missing.jpg").status_code == 404
    for bad in ("..%2F..%2Fnvr.db", "x.db", "..", "snapshot.jpg%00", "SNAP.JPG"):
        assert call("GET", f"/api/events/7/media/{bad}").status_code in (400, 404), bad
    assert call("GET", "/api/events/..%2F..%2F/media/snapshot.jpg").status_code in (404, 422)


def test_media_paths_validated():
    for url in ("/api/playback/..%2Fv3%2Fconfig?start=1", "/api/playback/Cam1?start=1", "/api/recordings/a%2Fb"):
        assert call("GET", url).status_code in (400, 404, 422), url


# ---------------------------------------------------------------- 2. hub URL

def _enrol():
    db.set_setting("hub_url", "wss://hub.axiomvision.ai/agent")
    for k, v in (("hub_token", "device-token"), ("hub_site_id", "site_1"), ("hub_vlm", {"url": "https://x"}),
                 ("hub_turn", {"urls": ["turn:x"], "username": "u", "credential": "c"})):
        db.set_setting(k, v)
    api.state.hub.enrolled, api.state.hub.site_id = True, "site_1"


def test_hub_url_change_unenrols():
    _enrol()
    r = call("PUT", "/api/hub", json={"hub_url": "wss://hub.axiomvision.ai/agent"})   # same address: nothing changes
    assert r.status_code == 200 and db.get_setting("hub_token") == "device-token" and r.json()["enrolled"] is True
    r = call("PUT", "/api/hub", json={"hub_url": "wss://evil.example/agent"})
    assert r.status_code == 200, r.text
    for k in ("hub_token", "hub_site_id", "hub_vlm", "hub_turn"):
        assert db.get_setting(k) is None, k
    body = r.json()
    assert body["enrolled"] is False and body["site_id"] is None and body["claim_code"] and body["hub_url"] == "wss://evil.example/agent"
    assert api.state.hub._auth_header().startswith("Claim ")   # the old device token is never offered to the new hub


def test_hub_url_rules():
    _enrol()
    for bad in ("ws://evil.example/agent", "http://evil.example/agent", "https://hub.axiomvision.ai/agent", "evil", "wss://"):
        r = call("PUT", "/api/hub", json={"hub_url": bad})
        assert r.status_code == 422, (bad, r.status_code)
    assert db.get_setting("hub_token") == "device-token" and db.get_setting("hub_url") == "wss://hub.axiomvision.ai/agent"
    for ok in ("ws://localhost:8000/agent", "ws://127.0.0.1:8000/agent", "ws://[::1]:8000/agent"):
        assert call("PUT", "/api/hub", json={"hub_url": ok}).status_code == 200, ok
    settings.hub_allow_insecure = True
    try:
        assert call("PUT", "/api/hub", json={"hub_url": "ws://192.168.1.5:8000/agent"}).status_code == 200
    finally:
        settings.hub_allow_insecure = False
    try:
        hub_agent.check_hub_url("ws://192.168.1.5:8000/agent")
        raise AssertionError("ws:// to the LAN accepted without NVR_HUB_ALLOW_INSECURE")
    except ValueError:
        pass
    _enrol()
    r = call("PUT", "/api/hub", json={"unenrol": True})
    assert r.status_code == 200 and db.get_setting("hub_token") is None and r.json()["enrolled"] is False
    db.set_setting("hub_url", None)


# ---------------------------------------------------------------- 3. browser guards

def test_host_allow_list():
    assert lan_guard.host_allowed("localhost:8080") and lan_guard.host_allowed("127.0.0.1") and lan_guard.host_allowed("[::1]:8443")
    assert lan_guard.host_allowed(socket.gethostname() + ":8080")
    for ip in direct.local_ipv4s():
        assert lan_guard.host_allowed(f"{ip}:8443"), ip
    assert lan_guard.host_allowed(None)                       # not a browser
    for bad in ("evil.example", "evil.example:8080", "attacker.rebind.network:8080", "", "localhost.evil.example"):
        assert not lan_guard.host_allowed(bad), bad
    r = call("GET", "/api/hub", headers={"Host": "rebind.evil.example:8080"})
    assert r.status_code == 421 and "NVR_ALLOWED_HOSTS" in r.json()["detail"]
    r = call("POST", "/api/advisor/dismiss", headers={"Host": "rebind.evil.example:8080", "Origin": "http://rebind.evil.example:8080"},
             json={"key": "k"})
    assert r.status_code == 421
    settings.allowed_hosts = "nvr.example.com, yard.lan"
    try:
        assert lan_guard.host_allowed("nvr.example.com") and lan_guard.host_allowed("YARD.LAN:8080")
        assert call("GET", "/api/hub", headers={"Host": "nvr.example.com"}).status_code == 200
    finally:
        settings.allowed_hosts = ""
        lan_guard.allowed_hosts(refresh=True)


def test_cross_site_writes_refused():
    body = {"key": "csrf-test"}
    for hdrs in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Origin": "http://localhost:9999"},
                 {"Referer": "https://evil.example/page"}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                 {"Origin": "https://hub.axiomvision.ai"}):   # the hub page without a direct token cannot write either
        r = call("POST", "/api/advisor/dismiss", headers=hdrs, json=body)
        assert r.status_code == 403 and "cross-site" in r.json()["detail"], (hdrs, r.status_code)
    for method, url in (("PUT", "/api/hub"), ("DELETE", "/api/layouts/1"), ("POST", "/api/backup"), ("POST", "/api/config/import")):
        assert call(method, url, headers={"Origin": "https://evil.example"}, json={}).status_code == 403, url
    # this server's own page, and tools that send no browser headers
    for hdrs in ({"Origin": SAME}, {"Referer": f"{SAME}/settings"}, {"Sec-Fetch-Site": "same-origin"}, {}):
        r = call("POST", "/api/advisor/dismiss", headers=hdrs, json=body)
        assert r.status_code == 200, (hdrs, r.status_code, r.text)
    # the tunnel is not subject to any of this (the hub authenticated the user)
    r = run(_call(hub_agent.as_tunnel(api.app), "POST", "/api/advisor/dismiss", headers={"Origin": "https://evil.example", "Host": "hub"}, json=body))
    assert r.status_code == 200


def test_body_types():
    # what a cross-site form or fetch(no-cors) can send, even if it passed the origin check
    for hdrs, content in (({"Content-Type": "text/plain"}, b'{"key":"x"}'),
                          ({"Content-Type": "application/x-www-form-urlencoded"}, b"key=x"),
                          ({"Content-Type": "multipart/form-data; boundary=x"}, b"--x--"),
                          ({}, b'{"key":"x"}')):                       # a Blob without a type: no Content-Type at all
        r = call("POST", "/api/advisor/dismiss", headers={"Origin": SAME, **hdrs}, content=content)
        assert r.status_code == 415, (hdrs, r.status_code)
    assert call("POST", "/api/advisor/dismiss", headers={"Origin": SAME, "Content-Type": "application/json; charset=utf-8"},
                content=b'{"key":"x"}').status_code == 200
    # bodyless POSTs (Back up now, Rebuild...) and WHEP's SDP still work
    assert lan_guard.body_type_problem("POST", "/api/backup", {"content-length": "0"}) is None
    assert lan_guard.body_type_problem("POST", "/api/whep/cam1_sub", {"content-type": "application/sdp"}) is None
    assert lan_guard.body_type_problem("POST", "/api/cameras/x/relay", {"content-type": "application/sdp"}) is not None
    r = call("POST", "/api/whep/BAD!", headers={"Origin": SAME, "Content-Type": "application/sdp"}, content=b"v=0")
    assert r.status_code == 400                                   # reached the route (bad path), not refused by the guard


def test_websocket_origin():
    client = TestClient(api.app, base_url=f"http://{HOST}")
    for hdrs in ({"origin": "https://evil.example"}, {"origin": "http://localhost:8080", "host": "rebind.evil.example:8080"},
                 {"origin": "https://hub.axiomvision.ai"}):        # hub page without a valid direct token
        try:
            with client.websocket_connect("/api/ws", headers=hdrs):
                raise AssertionError(f"WebSocket accepted for {hdrs}")
        except WebSocketDisconnect as e:
            assert e.code == 1008, hdrs
    H = lambda **kw: {k.replace("_", "-"): v for k, v in kw.items()}  # noqa: E731
    assert lan_guard.ws_allowed(H(host=HOST, origin=SAME), {}, {})
    assert lan_guard.ws_allowed(H(host=HOST), {}, {})                # no Origin: not a browser
    assert not lan_guard.ws_allowed(H(host=HOST, origin="https://evil.example"), {}, {})
    db.set_setting("hub_token", "device-token")
    db.set_setting("hub_site_id", "site_1")
    db.set_setting("hub_url", "wss://hub.axiomvision.ai/agent")
    try:
        LAN = f"{(direct.local_ipv4s() or ['localhost'])[0]}:8443"   # this box as the hub page addresses it
        tok = direct.sign({"sid": "site_1", "uid": "u", "role": "viewer", "exp": int(time.time()) + 60}, "device-token")
        assert lan_guard.ws_allowed(H(host=LAN, origin="https://hub.axiomvision.ai"), {}, {"direct": tok})
        assert not lan_guard.ws_allowed(H(host=LAN, origin="https://hub.axiomvision.ai"), {}, {"direct": tok + "x"})
    finally:
        for k in ("hub_token", "hub_site_id", "hub_url"):
            db.set_setting(k, None)


def test_dev_origins_need_the_setting():
    assert "http://localhost:8000" not in direct.allowed_origins() and "http://localhost:5174" not in direct.allowed_origins()
    settings.dev_origins = True
    try:
        assert {"http://localhost:8000", "http://localhost:5174"} <= direct.allowed_origins()
    finally:
        settings.dev_origins = False


# ---------------------------------------------------------------- 4. the tunnel marker

def test_client_tuple_is_not_the_tunnel():
    # what X-Forwarded-For: hub used to produce through uvicorn's proxy headers: scope client ("hub", 0)
    r = run(_call(api.app, "GET", "/api/config/handoff", client=hub_agent.IN_PROCESS_CLIENT, headers={"x-hub-internal": "handoff"}))
    assert r.status_code == 403
    r = run(_call(api.app, "POST", "/api/assistant/plan", client=hub_agent.IN_PROCESS_CLIENT,
                  headers={"x-hub-role": "viewer", "x-hub-user": "forged"}, json={"text": "hello"}))
    assert r.status_code == 200          # treated as the LAN: forged x-hub-* stripped, no hub role
    # the real tunnel (scope marked by hub_agent) still passes
    r = run(_call(hub_agent.as_tunnel(api.app), "GET", "/api/config/handoff", headers={"x-hub-internal": "handoff", "Host": "hub"}))
    assert r.status_code == 200
    assert not hub_agent.is_tunnel({"client": ("hub", 0), "nvr.hub_tunnel": True})   # a lookalike value is not the marker


def test_uvicorn_ignores_x_forwarded_for():
    import uvicorn
    assert "proxy_headers=False" in (Path(api.__file__).parent / "__main__.py").read_text()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg = uvicorn.Config(api.app, host="127.0.0.1", port=port, lifespan="off", log_level="warning", proxy_headers=False)
    server = uvicorn.Server(cfg)

    async def go():
        task = asyncio.create_task(server.serve())
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"http://127.0.0.1:{port}/api/config/handoff",
                                headers={"X-Forwarded-For": "hub", "x-hub-internal": "handoff"})
                assert r.status_code == 403, r.text
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 10)

    run(go())
    old = settings.https_port, settings.host
    settings.https_port, settings.host = port, "127.0.0.1"
    try:
        https = api._https_server()
        assert https is not None and https.config.proxy_headers is False
    finally:
        settings.https_port, settings.host = old


# ---------------------------------------------------------------- 5. TURN credentials

def test_turn_credentials():
    db.set_setting("hub_turn", {"urls": ["turn:turn.example:3478"], "username": "u", "credential": "secret-cred"})
    try:
        r = call("GET", "/api/turn", headers={"Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 200 and r.json()["iceServers"][0]["credential"] == "secret-cred" and r.headers["cache-control"] == "no-store"
        assert call("GET", "/api/turn").status_code == 200                       # this server's page over plain http
        for hdrs in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": "https://evil.example"}, {"Referer": "https://evil.example/"}):
            r = call("GET", "/api/turn", headers=hdrs)
            assert r.status_code == 403 and "secret-cred" not in r.text, hdrs
        r = run(_call(hub_agent.as_tunnel(api.app), "GET", "/api/turn", headers={"Host": "hub", "Sec-Fetch-Site": "cross-site"}))
        assert r.status_code == 200
        db.set_setting("hub_token", "device-token")
        db.set_setting("hub_site_id", "site_1")
        tok = direct.sign({"sid": "site_1", "uid": "u", "role": "owner", "exp": int(time.time()) + 60}, "device-token")
        r = call("GET", "/api/turn", headers={"Authorization": f"Direct {tok}"})
        assert r.status_code == 404 and "secret-cred" not in r.text                # hidden on a direct connection
    finally:
        for k in ("hub_turn", "hub_token", "hub_site_id"):
            db.set_setting(k, None)


# ---------------------------------------------------------------- 6. camera validation

CAM = {"id": "cam9", "name": "Yard", "host": "192.168.1.10", "username": "u", "password": "p", "main_path": "/main",
       "sub_path": "/sub", "onvif_port": 80, "rtsp_port": 554, "enabled": 1}
GOOD_PATHS = ("/main", "/sub", "/Streaming/Channels/101", "/cam/realmonitor?channel=1&subtype=0", "/h264Preview_01_main",
              "/media/video1;stream=1", "/axis-media/media.amp?videocodec=h264&resolution=1920x1080", "/live/ch00_0", "/11",
              "/user=admin_password=_channel=1_stream=0.sdp?real_stream", "/", "/onvif-media/media.amp?profile=profile_1_h264")
BAD_PATHS = ("@evil:554/x", "/x@evil:554/y", "//evil/x", "main", "/a b", "/x#y", "/x\n", "/x\\y", "", "/" + "a" * 300)


def test_camera_path_rules():
    for p in GOOD_PATHS:
        assert mediamtx.PATH_RE.fullmatch(p), p
        api.CameraIn(**{**CAM, "main_path": p, "sub_path": p})
    for p in BAD_PATHS:
        assert not mediamtx.PATH_RE.fullmatch(p), p
        for field in ("main_path", "sub_path"):
            try:
                api.CameraIn(**{**CAM, field: p})
                raise AssertionError(f"{field} {p!r} accepted")
            except pydantic.ValidationError as e:
                assert field in str(e), str(e)
    for bad in ({"id": "Cam 1"}, {"id": "../x"}, {"host": "evil:554@x"}, {"rtsp_port": 0}, {"rtsp_port": 70000}):
        try:
            api.CameraIn(**{**CAM, **bad})
            raise AssertionError(f"{bad} accepted")
        except pydantic.ValidationError:
            pass


def test_camera_api_rejects_bad_path():
    r = call("PUT", "/api/cameras/cam9", headers={"Origin": SAME}, json={**CAM, "main_path": "@evil.example:554/x"})
    assert r.status_code == 422 and "main_path" in r.text


def test_import_and_merge_validate():
    data = {"format": 1, "cameras": [{**CAM, "id": "cam8"}, {**CAM, "id": "cam9", "sub_path": "@evil:554/x"}]}
    for fn in (lambda: siteconfig.import_config(data), lambda: siteconfig.merge_cameras(data)):
        try:
            fn()
            raise AssertionError("bad camera stored")
        except siteconfig.InvalidCamera as e:
            assert "sub_path" in str(e)
    assert not db.one("SELECT 1 FROM cameras WHERE id IN ('cam8','cam9')")   # nothing stored, not even the good one
    r = call("POST", "/api/config/import", json={"data": data})
    assert r.status_code == 422 and "sub_path" in r.json()["detail"]
    r = call("POST", "/api/config/merge", json={"data": {**data, "partial": True}})
    assert r.status_code == 422
    try:
        siteconfig.import_config({"format": 1, "cameras": [{**CAM, "id": "BAD ID"}]})
        raise AssertionError("bad id imported")
    except siteconfig.InvalidCamera:
        pass
    # an export of this site's cameras imports again (fields as stored)
    siteconfig.import_config({"format": 1, "cameras": [{**CAM, "id": "cam7"}]})
    assert db.one("SELECT 1 FROM cameras WHERE id='cam7'")
    db.execute("DELETE FROM cameras WHERE id='cam7'")


def test_mediamtx_skips_a_stored_bad_path():
    cfg = mediamtx.build_config([{**CAM, "id": "ok1"}, {**CAM, "id": "bad1", "main_path": "@evil:554/x"}])
    assert "ok1" in cfg["paths"] and "bad1" not in cfg["paths"] and "bad1_sub" not in cfg["paths"]


# ---------------------------------------------------------------- 7. MediaMTX reader password

def test_rtsp_reader_auth():
    assert settings.rtsp_auth is True
    user, pw = mediamtx.reader_credentials()
    assert user and len(pw) >= 24 and mediamtx.reader_credentials() == (user, pw)       # generated once, kept
    assert db.get_setting("mediamtx_reader") == {"user": user, "pass": pw}
    users = mediamtx.build_config([])["authInternalUsers"]
    readers = [u for u in users if {"action": "read"} in u["permissions"]]
    assert readers and all(u["user"] == user and u["pass"] == pw for u in readers)      # no anonymous viewing at all
    anon = [u for u in users if u["user"] == "any"]
    assert all(set(u["ips"]) <= {"127.0.0.1", "::1"} for u in anon)                       # anonymous = this machine only
    assert {"action": "playback"} in anon[0]["permissions"] and {"action": "api"} in anon[0]["permissions"]
    import base64
    h = mediamtx.reader_auth_header()["Authorization"]
    assert base64.b64decode(h.split()[1]).decode() == f"{user}:{pw}"
    settings.rtsp_auth = False
    try:
        users = mediamtx.build_config([])["authInternalUsers"]
        assert users[0]["user"] == "any" and "192.168.0.0/16" in users[0]["ips"] and mediamtx.reader_auth_header() == {}
    finally:
        settings.rtsp_auth = True


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
