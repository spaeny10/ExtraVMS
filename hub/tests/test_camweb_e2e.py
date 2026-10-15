"""Camera web pages end to end: the hub under uvicorn, a real site agent (nvr.hub_agent) running the site's camweb
route (nvr.camweb) against a fake camera. Covers the one-time link, the cookie, a GET with query, a streamed download,
an upload, the camera's redirect and cookies, and a WebSocket both ways through the real tunnel framing."""
import asyncio
import json
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
import websockets
from fastapi import FastAPI

from hub import agents
from hub.api import app

CAM_HOST = "10.9.8.7"


@pytest.fixture(scope="module")
def hub_server():
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield port
    server.should_exit = True
    t.join(timeout=5)


def camera(request: httpx.Request) -> httpx.Response:
    """The camera's web server (the site reaches it through httpx; nothing listens anywhere)."""
    async def body(data: bytes):
        yield data
    if request.url.path == "/big":
        async def big():
            for i in range(5):
                yield bytes([65 + i]) * 300_000
        return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=big())
    if request.url.path == "/login":
        return httpx.Response(302, headers=[("location", f"http://{CAM_HOST}/doc/page/main.asp"),
                                            ("set-cookie", f"SID=abc; Path=/; Domain={CAM_HOST}"), ("set-cookie", "lang=en")],
                              content=body(b""))
    got = request.read()
    return httpx.Response(200, headers={"content-type": "application/json"}, content=body(json.dumps({
        "url": str(request.url), "host": request.headers.get("host"), "method": request.method, "size": len(got),
        "cookie": request.headers.get("cookie"), "hub": [k for k in request.headers if k.startswith("x-")]}).encode()))


class Upstream:
    """The camera's WebSocket: echoes."""

    def __init__(self, uri):
        self.uri, self.subprotocol, self.q = uri, None, asyncio.Queue()

    async def send(self, m):
        await self.q.put(m[::-1] if isinstance(m, str) else m)

    def __aiter__(self):
        return self

    async def __anext__(self):
        m = await self.q.get()
        if m is None:
            raise StopAsyncIteration
        return m

    async def close(self):
        self.q.put_nowait(None)


def test_camera_page_through_the_tunnel(hub_server, superuser):
    from nvr import camweb as site_camweb
    from nvr import hub_agent
    from nvr.db import db as site_db

    port = hub_server
    base = f"http://127.0.0.1:{port}"
    site_db.set_setting("hub_url", f"ws://127.0.0.1:{port}/agent")
    site_db.set_setting("hub_token", None)
    site_db.upsert_camera({"id": "cam1", "name": "Gate", "host": CAM_HOST, "onvif_port": 80, "rtsp_port": 554, "username": "u",
                           "password": "pw-stand-in", "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": []})
    site_camweb._client, site_camweb._transport = None, httpx.MockTransport(camera)
    opened = []

    async def fake_ws(uri, **kw):
        opened.append(uri)
        return Upstream(uri)
    site_camweb._ws_connect = fake_ws
    site = FastAPI()
    site.include_router(site_camweb.router)
    state = SimpleNamespace(pipeline=SimpleNamespace(subscribers=set()), ingests={}, health=None)

    async def summary(st, since):
        return {"cameras": [{"id": "cam1", "name": "Gate", "stream_ready": True}], "version": "0.9.0"}

    agent = hub_agent.HubAgent(site, state, summary_fn=summary)
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    fut = asyncio.run_coroutine_threadsafe(agent.run(), loop)
    try:
        for _ in range(100):
            time.sleep(0.05)
            if agent.status()["claim_code"] in agents.registry.pending:
                break
        code = agent.status()["claim_code"]
        owner = httpx.Client(base_url=base)
        assert owner.post("/auth/login", json={"email": superuser["email"], "password": superuser["password"]}).status_code == 200
        org = owner.post("/api/orgs", json={"name": "Cam E2E", "slug": "cam-e2e"}).json()
        srv = owner.post(f"/api/orgs/{org['id']}/sites/claim", json={"code": code, "name": "E2E NVR"}).json()
        for _ in range(100):
            time.sleep(0.05)
            c = agents.registry.get(srv["id"])
            if c is not None and "camweb" in c.caps():
                break
        r = owner.post(f"/api/servers/{srv['id']}/cameras/cam1/web-session")
        assert r.status_code == 200, r.text
        link = r.json()["url"]
        host = r.json()["host"]
        enter = "/__axiom/" + link.partition("/__axiom/")[2]
        tab = httpx.Client(base_url=base, headers={"host": host})   # cookies by hand: the jar would file them under 127.0.0.1
        r = tab.get(enter)
        assert r.status_code == 303, r.text
        token = r.headers["set-cookie"].split(";")[0]          # axcam=<token>
        cookie = {"cookie": f"{token}; SID=own"}

        r = tab.get("/cgi-bin/info.cgi?ch=1", headers=cookie)
        assert r.status_code == 200, r.text
        got = r.json()
        assert got["url"] == f"http://{CAM_HOST}/cgi-bin/info.cgi?ch=1" and got["host"] == CAM_HOST
        assert got["cookie"] == "SID=own" and got["hub"] == []

        r = tab.get("/big", headers=cookie)
        assert r.status_code == 200 and len(r.content) == 1_500_000 and r.content[-1:] == b"E"
        r = tab.post("/upload.cgi", headers=cookie, content=b"f" * 700_000)
        assert r.json()["method"] == "POST" and r.json()["size"] == 700_000
        r = tab.get("/login", headers=cookie)
        assert r.status_code == 302 and r.headers["location"] == "/doc/page/main.asp"
        assert r.headers.get_list("set-cookie") == ["SID=abc; Path=/", "lang=en"]
        assert httpx.get(f"{base}/", headers={"host": host}).status_code == 403   # no cookie, no camera

        async def socket_round_trip():
            sock = socket.create_connection(("127.0.0.1", port))
            async with websockets.connect(f"ws://{host}/ws/live?x=1", sock=sock, origin=f"http://{host}",
                                          additional_headers={"Cookie": token}) as ws:
                await ws.send("hello")
                assert await asyncio.wait_for(ws.recv(), 5) == "olleh"
                big = bytes(range(256)) * 1000   # 256 KB: several tunnel chunks each way
                await ws.send(big)
                assert await asyncio.wait_for(ws.recv(), 5) == big
        asyncio.run(socket_round_trip())
        assert opened == [f"ws://{CAM_HOST}/ws/live?x=1"]
    finally:
        fut.cancel()
        time.sleep(0.3)   # let the agent's tasks unwind before its loop stops
        loop.call_soon_threadsafe(loop.stop)
        site_camweb._client = site_camweb._transport = None
