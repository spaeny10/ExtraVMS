"""Camera web pages, site side (camweb.py): the tunnel-only route forwards to the camera's registered web address only,
with the camera's Host, streams both ways, passes WebSockets through; the agent's WebSocket bridge (hub_agent._serve_ws).

Run: ..\\.venv\\Scripts\\python.exe tests\\test_camweb.py   (from backend/)
"""
import asyncio
import json
import os
import ssl
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("NVR_ALLOWED_HOSTS", "site")  # the test client's Host (lan_guard Host allow-list)
os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-camweb-test-")  # never the real DB
os.environ.setdefault("NVR_RECORDINGS_DIR", tempfile.mkdtemp(prefix="nvr-camweb-rec-"))
os.environ.setdefault("NVR_RUNTIME_DIR", tempfile.mkdtemp(prefix="nvr-camweb-run-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tunnelproto"))

import httpx  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from nvr import camweb, hub_agent  # noqa: E402
from nvr.api import app  # noqa: E402
from nvr.db import db  # noqa: E402
from tunnelproto import Stream, decode  # noqa: E402

PW = "pw-" + "x" * 8   # a stand-in, never a real credential
CAM = {"id": "cam1", "name": "Gate", "host": "10.0.0.5", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": PW,
       "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": []}
INTERNAL = {"x-hub-internal": "camweb", "x-hub-user": "admin@example.com", "x-camweb-host": "https://cabc.cam.example.com"}

seen: list[httpx.Request] = []


def streamed(status: int, headers=None, body: bytes = b"", json_body=None) -> httpx.Response:
    """A response the way a real connection delivers it (a stream; httpx loads plain bytes up front)."""
    data = json.dumps(json_body).encode() if json_body is not None else body

    async def gen():
        yield data
    return httpx.Response(status, headers=headers, content=gen())


def camera(request: httpx.Request) -> httpx.Response:
    """A camera's web server: echoes what it got; /login redirects and sets cookies; /big streams."""
    seen.append(request)
    if request.url.path == "/login":
        return streamed(302, headers=[("location", f"http://{CAM['host']}/doc/page/main.asp?x=1"),
                                            ("set-cookie", "SID=abc; Path=/; Domain=10.0.0.5; HttpOnly"),
                                            ("set-cookie", "lang=en; Path=/"),
                                            ("www-authenticate", 'Digest realm="cam"'), ("www-authenticate", 'Basic realm="cam"'),
                                            ("connection", "close")])
    if request.url.path == "/big":
        async def gen():
            for i in range(4):
                yield bytes([65 + i]) * 100_000
        return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=gen())
    body = request.read()
    return streamed(200, {"content-type": "application/json"}, json_body={"url": str(request.url), "host": request.headers.get("host"), "method": request.method,
                                     "body": body.decode(), "cookie": request.headers.get("cookie"),
                                     "origin": request.headers.get("origin"), "referer": request.headers.get("referer"),
                                     "auth": request.headers.get("authorization"),
                                     "hub": sorted(k for k in request.headers if k.startswith(("x-hub", "x-camweb", "x-forwarded")))})


def _use_fake_camera():
    camweb._client = None
    camweb._transport = httpx.MockTransport(camera)


def _seed(**over):
    db.upsert_camera({**CAM, **over})


def call(method, path, headers=None, content=None, tunnel=True):
    async def go():
        transport = httpx.ASGITransport(app=hub_agent.as_tunnel(app) if tunnel else app,
                                        client=hub_agent.IN_PROCESS_CLIENT if tunnel else ("192.168.1.50", 5000))
        async with httpx.AsyncClient(transport=transport, base_url="http://site") as c:
            return await c.request(method, path, headers=headers or {}, content=content)
    return asyncio.run(go())


def test_refuses_without_tunnel_or_header():
    _use_fake_camera()
    _seed()
    seen.clear()
    # from the LAN (no tunnel marker), even with every header the hub would send
    assert call("GET", "/api/camweb/cam1", {**INTERNAL, "x-camweb-path": "/"}, tunnel=False).status_code == 403
    # through the tunnel but without the internal header (what the hub's public /s/<server>/api proxy would send)
    assert call("GET", "/api/camweb/cam1", {"x-camweb-path": "/"}).status_code == 403
    assert call("GET", "/api/camweb/cam1", {"x-hub-internal": "handoff", "x-camweb-path": "/"}).status_code == 403
    assert call("GET", "/api/camweb/nope", {**INTERNAL, "x-camweb-path": "/"}).status_code == 404
    assert seen == []


def test_forwards_only_to_the_registered_address():
    _use_fake_camera()
    _seed()
    seen.clear()
    r = call("GET", "/api/camweb/cam1?a=1&b=%2F", {**INTERNAL, "x-camweb-path": "/cgi-bin/x.cgi", "cookie": "SID=abc",
                                                  "authorization": "Basic dTpw", "origin": "https://cabc.cam.example.com",
                                                  "referer": "https://cabc.cam.example.com/doc/page/main.asp",
                                                  "x-forwarded-for": "1.2.3.4"})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["url"] == "http://10.0.0.5/cgi-bin/x.cgi?a=1&b=%2F" and got["host"] == "10.0.0.5"
    assert got["cookie"] == "SID=abc" and got["auth"] == "Basic dTpw"   # the camera's own login passes
    assert got["origin"] == "http://10.0.0.5" and got["referer"] == "http://10.0.0.5/doc/page/main.asp"
    assert got["hub"] == []   # nothing of the hub's reaches the camera
    # a path that tries to name another host stays a path on the camera
    for evil in ("//evil.example.com/x", "/\\evil.example.com", "http://evil.example.com/"):
        seen.clear()
        r = call("GET", "/api/camweb/cam1", {**INTERNAL, "x-camweb-path": evil})
        if r.status_code == 200:
            assert r.json()["url"].startswith("http://10.0.0.5/") and r.json()["host"] == "10.0.0.5", r.json()
            assert all(q.url.host == "10.0.0.5" for q in seen)
        else:
            assert r.status_code == 400 and seen == []
    assert call("GET", "/api/camweb/cam1", {**INTERNAL, "x-camweb-path": "/a b"}).status_code == 400
    # an @ in the path or query can't become user info for another host
    r = call("GET", "/api/camweb/cam1?u=x@evil.example.com", {**INTERNAL, "x-camweb-path": "/@evil.example.com:81/"})
    assert r.status_code == 200 and r.json()["host"] == "10.0.0.5" and seen[-1].url.host == "10.0.0.5"


def test_web_port_scheme_and_port_forward():
    _use_fake_camera()
    _seed(web_scheme="https", web_port=8443)
    r = call("GET", "/api/camweb/cam1", {**INTERNAL, "x-camweb-path": "/"})
    assert r.json()["url"] == "https://10.0.0.5:8443/" and r.json()["host"] == "10.0.0.5:8443"
    _seed(web_scheme="https", web_port=None)
    assert call("GET", "/api/camweb/cam1", {**INTERNAL, "x-camweb-path": "/"}).json()["url"] == "https://10.0.0.5/"
    # port-forward mode: the outside address, the outside ONVIF forward when the page is on the ONVIF port ...
    _seed(web_scheme="http", web_port=None, public_host="203.0.113.7", public_onvif_port=8082)
    assert call("GET", "/api/camweb/cam1", {**INTERNAL, "x-camweb-path": "/"}).json()["host"] == "203.0.113.7:8082"
    # ... or its own outside web port
    _seed(public_web_port=8090)
    assert call("GET", "/api/camweb/cam1", {**INTERNAL, "x-camweb-path": "/"}).json()["host"] == "203.0.113.7:8090"
    _seed(public_host=None, public_onvif_port=None, public_web_port=None, web_scheme="http")
    # camera certificates are self-signed: the client never checks them, follows no redirects, uses no proxy
    camweb._client = None
    camweb._transport = None
    c = camweb.client()
    ctx = c._transport._pool._ssl_context
    assert ctx.verify_mode == ssl.CERT_NONE and not ctx.check_hostname and c.follow_redirects is False
    asyncio.run(c.aclose())
    camweb._client = None


def test_response_headers_location_and_cookies():
    _use_fake_camera()
    _seed()
    r = call("GET", "/api/camweb/cam1", {**INTERNAL, "x-camweb-path": "/login"})
    assert r.status_code == 302
    assert r.headers["location"] == "/doc/page/main.asp?x=1"   # the camera's own address, made relative
    assert json.loads(r.headers["x-camweb-cookies"]) == ["SID=abc; Path=/; Domain=10.0.0.5; HttpOnly", "lang=en; Path=/"]
    assert "set-cookie" not in r.headers
    assert r.headers["www-authenticate"] == 'Digest realm="cam", Basic realm="cam"'
    # a redirect elsewhere is left alone (the browser follows it, not this server)
    assert camweb._relative_location("https://cloud.example.com/x", {"10.0.0.5"}) == "https://cloud.example.com/x"


def test_body_streams_both_ways():
    _use_fake_camera()
    _seed()
    r = call("POST", "/api/camweb/cam1", {**INTERNAL, "x-camweb-path": "/upload", "content-type": "text/plain"}, content=b"hello camera")
    assert r.json()["method"] == "POST" and r.json()["body"] == "hello camera"
    r = call("GET", "/api/camweb/cam1", {**INTERNAL, "x-camweb-path": "/big"})
    assert r.status_code == 200 and len(r.content) == 400_000 and r.content[-1:] == b"D"


def test_camera_unreachable():
    camweb._client = None

    def down(request):
        raise httpx.ConnectError("refused")
    camweb._transport = httpx.MockTransport(down)
    _seed()
    r = call("GET", "/api/camweb/cam1", {**INTERNAL, "x-camweb-path": "/"})
    assert r.status_code == 502 and r.headers["x-camweb-error"] == "1"
    camweb._client = None


class FakeUpstream:
    """A camera's WebSocket: echoes with a prefix; remembers how it was opened."""
    opened: list = []

    def __init__(self, uri, kw):
        FakeUpstream.opened.append((uri, kw))
        self.subprotocol = (kw.get("subprotocols") or [None])[0]
        self.q: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, m):
        await self.q.put(("echo:" + m) if isinstance(m, str) else b"echo:" + m)

    def __aiter__(self):
        return self

    async def __anext__(self):
        m = await self.q.get()
        if m is None:
            raise StopAsyncIteration
        return m

    async def close(self):
        self.closed = True
        self.q.put_nowait(None)


async def fake_connect(uri, **kw):
    return FakeUpstream(uri, kw)


def test_websocket_passthrough():
    _seed(web_scheme="https", web_port=8443)
    camweb._ws_connect = fake_connect
    FakeUpstream.opened.clear()
    client = TestClient(hub_agent.as_tunnel(app))
    hdrs = {**INTERNAL, "x-camweb-path": "/ws/live", "cookie": "SID=abc", "origin": "https://cabc.cam.example.com",
            "sec-websocket-protocol": "jsonrpc"}
    with client.websocket_connect("/api/camweb/cam1?ch=1", headers=hdrs) as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "echo:hi"
        ws.send_bytes(b"\x00\x01")
        assert ws.receive_bytes() == b"echo:\x00\x01"
    uri, kw = FakeUpstream.opened[0]
    assert uri == "wss://10.0.0.5:8443/ws/live?ch=1" and kw["subprotocols"] == ["jsonrpc"]
    assert kw["ssl"].verify_mode == ssl.CERT_NONE
    sent = dict(kw["additional_headers"])
    assert sent["cookie"] == "SID=abc" and sent["origin"] == "https://10.0.0.5:8443" and "x-hub-user" not in sent
    # without the tunnel marker or the internal header: refused before anything is opened
    FakeUpstream.opened.clear()
    for c, h in ((TestClient(app), hdrs), (client, {k: v for k, v in hdrs.items() if k != "x-hub-internal"})):
        try:
            with c.websocket_connect("/api/camweb/cam1", headers=h):
                raise AssertionError("should have been refused")
        except Exception as e:   # starlette's WebSocketDisconnect from the refused handshake
            assert "4403" in repr(e) or getattr(e, "code", None) == 4403, repr(e)
    assert FakeUpstream.opened == []
    _seed(web_scheme="http", web_port=None)


def test_agent_websocket_bridge():
    """hub_agent._serve_ws: 101 with the subprotocol, messages framed both ways (a big one in pieces), end closes."""
    async def echo_app(scope, receive, send):
        assert scope["type"] == "websocket" and hub_agent.is_tunnel(scope) and scope["subprotocols"] == ["p1", "p2"]
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept", "subprotocol": "p2"})
        while True:
            m = await receive()
            if m["type"] == "websocket.disconnect":
                return
            await send({"type": "websocket.send", **({"text": m["text"][::-1]} if "text" in m else {"bytes": m["bytes"]})})

    class Wire:
        def __init__(self):
            self.frames: list = []

        async def send(self, raw):
            self.frames.append(decode(raw))

    async def run():
        agent = hub_agent.HubAgent(echo_app, None)
        agent._ws = wire = Wire()
        s = Stream(9)
        task = asyncio.create_task(agent._serve_ws({"id": 9, "path": "/x", "query": "", "headers": {"sec-websocket-protocol": "p1, p2"}}, s))
        await asyncio.sleep(0.05)
        assert wire.frames[0] == {"t": "res", "id": 9, "status": 101, "headers": {"sec-websocket-protocol": "p2"}}
        for piece in hub_agent.ws_pieces("hello"):
            s.push(piece)
        big = bytes(range(256)) * 600   # 150 KB: three pieces each way
        for piece in hub_agent.ws_pieces(big):
            s.push(piece)
        await asyncio.sleep(0.1)
        asm = hub_agent.WsAssembler()
        msgs = [got for f in wire.frames[1:] if isinstance(f, tuple) and (got := asm.feed(f[1])) is not None]
        assert msgs == [(False, b"olleh"), (True, big)]
        assert sum(1 for f in wire.frames if isinstance(f, tuple)) == 4
        s.push(None)   # the browser left
        await asyncio.wait_for(task, 2)
        assert wire.frames[-1] == {"t": "end", "id": 9}

        # an app that refuses before accepting: the hub gets a refusal status, then end
        async def refuse(scope, receive, send):
            await receive()
            await send({"type": "websocket.close", "code": 4403})
        agent.app = refuse
        wire.frames.clear()
        await agent._serve_ws({"id": 11, "path": "/x", "query": "", "headers": {}}, Stream(11))
        assert wire.frames == [{"t": "res", "id": 11, "status": 403, "headers": {}}, {"t": "end", "id": 11}]
    asyncio.run(run())


def test_camera_fields_stored_and_exported():
    from nvr import api as api_mod, siteconfig
    _seed(web_scheme="https", web_port=8443, public_web_port=9443)
    cam = next(c for c in db.cameras() if c["id"] == "cam1")
    pub = api_mod.public_camera(cam)
    assert (pub["web_scheme"], pub["web_port"], pub["public_web_port"]) == ("https", 8443, 9443)
    assert set(("web_scheme", "web_port", "public_web_port")) <= set(api_mod.CAMERA_KEPT_FIELDS)   # an older UI can't clear them
    assert {"web_scheme", "web_port", "public_web_port"} <= set(siteconfig.CAMERA_COLS)
    _seed(web_scheme="http", web_port=None, public_web_port=None)


def test_camera_problem_rules():
    from nvr import mediamtx
    assert mediamtx.camera_problem({**CAM, "web_port": 70000}) and "web_port" in mediamtx.camera_problem({**CAM, "web_port": 70000})
    assert mediamtx.camera_problem({**CAM, "public_web_port": 0})
    assert mediamtx.camera_problem({**CAM, "web_scheme": "ftp"})
    assert mediamtx.camera_problem({**CAM, "web_scheme": "https", "web_port": 443, "public_web_port": 8443}) is None
    from nvr.api import CameraIn
    c = CameraIn(**{**CAM, "zones": [], "web_port": "", "public_web_port": 0})
    assert c.web_port is None and c.public_web_port is None and c.web_scheme == "http"
    # a camera saved before web_scheme existed reads back null; the zones editor sends the whole row back: not a 422,
    # and "not sent" (so a PUT keeps what is stored)
    for empty in (None, ""):
        c = CameraIn(**{**CAM, "zones": [], "web_scheme": empty})
        assert c.web_scheme == "http" and "web_scheme" not in c.model_fields_set


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
