"""Site agent against a fake hub: claim/enrol handshake, in-process request bridge (incremental streaming,
abort), event forwarding, heartbeat, reconnect.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_hub_agent.py   (from backend/)
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-hub-test-")  # never the real DB
os.environ["NVR_HUB_URL"] = "ws://127.0.0.1:0/agent"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tunnelproto"))

import websockets  # noqa: E402

from nvr import hub_agent  # noqa: E402
from nvr.db import db  # noqa: E402
from tunnelproto import chunk, decode, encode  # noqa: E402


# a tiny ASGI app standing in for the site: JSON, a slow stream, and an echo of headers/body
async def site_app(scope, receive, send):
    path = scope["path"]
    hdrs = {k.decode(): v.decode() for k, v in scope["headers"]}
    if path == "/json":
        body = json.dumps({"ok": True, "user": hdrs.get("x-hub-user"), "client": list(scope["client"])}).encode()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})
    elif path == "/slow":
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/x-ndjson")]})
        for i in range(3):
            await asyncio.sleep(0.25)
            await send({"type": "http.response.body", "body": f'{{"n":{i}}}\n'.encode(), "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
    elif path == "/echo":
        body = b""
        while True:
            m = await receive()
            body += m.get("body", b"")
            if not m.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": body[::-1]})
    elif path == "/forever":
        await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            while True:
                await asyncio.sleep(0.1)
                await send({"type": "http.response.body", "body": b"x" * 1000, "more_body": True})
        finally:
            site_app.cancelled = True
    else:
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b"nope"})


site_app.cancelled = False
class FakeMtx:
    writes = 0
    def write_config(self, cams):
        FakeMtx.writes += 1


state = SimpleNamespace(pipeline=SimpleNamespace(subscribers=set()), ingests={}, health=None, mtx=FakeMtx())


class FakeHub:
    """Accepts one site connection at a time; records frames; lets the test drive requests."""

    def __init__(self):
        self.frames: list = []
        self.chunks: dict[int, bytearray] = {}
        self.ended: set[int] = set()
        self.heads: dict[int, dict] = {}
        self.ws = None
        self.auth: list[str] = []
        self.got = asyncio.Event()
        self.enrol_on_claim = True

    async def handler(self, ws):
        self.auth.append(ws.request.headers.get("Authorization", ""))
        self.ws = ws
        try:
            async for raw in ws:
                m = decode(raw)
                if isinstance(m, tuple):
                    self.chunks.setdefault(m[0], bytearray()).extend(m[1])
                    await ws.send(encode({"t": "credit", "id": m[0], "bytes": len(m[1])}))
                else:
                    self.frames.append(m)
                    if m["t"] == "hello":
                        if self.auth[-1].startswith("Claim") and self.enrol_on_claim:
                            await ws.send(encode({"t": "enrolled", "site_id": "s_test", "token": "device-token-1"}))
                        else:
                            await ws.send(encode({"t": "welcome", "site_id": "s_test", "org": "Jetstream", "heartbeat_s": 0.3,
                                                  "vlm": {"url": "https://hub/v1", "model": "qwen32", "key": "k"},
                                                  "turn": {"urls": ["turn:hub:3478?transport=udp"], "username": "9:site:s_test", "credential": "c", "expires": 9}}))
                    elif m["t"] == "res":
                        self.heads[m["id"]] = m
                    elif m["t"] == "end":
                        self.ended.add(m["id"])
                self.got.set()
        except websockets.ConnectionClosed:
            pass

    async def request(self, sid, method, path, headers=None, body=None):
        await self.ws.send(encode({"t": "req", "id": sid, "method": method, "path": path, "query": "", "headers": headers or {}, "body": body is not None}))
        if body is not None:
            await self.ws.send(chunk(sid, body))
            await self.ws.send(encode({"t": "end", "id": sid}))

    async def wait_end(self, sid, timeout=5):
        t0 = time.time()
        while sid not in self.ended:
            await asyncio.sleep(0.02)
            assert time.time() - t0 < timeout, f"stream {sid} never ended"


async def _run():
    hub = FakeHub()
    async with websockets.serve(hub.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        db.set_setting("hub_url", f"ws://127.0.0.1:{port}/agent")
        agent = hub_agent.HubAgent(site_app, state, summary_fn=lambda st, since: _summary())
        task = asyncio.create_task(agent.run())
        try:
            # 1. unenrolled: connects with a claim code, gets enrolled, reconnects with the token, gets welcome
            for _ in range(200):
                await asyncio.sleep(0.05)
                if agent.connected:
                    break
            assert agent.connected and agent.enrolled and agent.site_id == "s_test" and agent.org == "Jetstream", agent.status()
            assert hub.auth[0].startswith("Claim ") and hub.auth[1] == "Bearer device-token-1", hub.auth
            assert db.get_setting("hub_token") == "device-token-1"
            from nvr.config import settings
            assert settings.remote_vlm_url == "https://hub/v1" and settings.remote_vlm_model == "qwen32"
            assert db.get_setting("hub_turn")["username"] == "9:site:s_test" and FakeMtx.writes >= 1  # TURN handed to MediaMTX

            # 2. a JSON request with hub identity headers, served in-process
            await hub.request(1, "GET", "/json", {"X-Hub-User": "shawn", "X-Hub-Role": "owner"})
            await hub.wait_end(1)
            assert hub.heads[1]["status"] == 200
            body = json.loads(bytes(hub.chunks[1]))
            assert body == {"ok": True, "user": "shawn", "client": ["hub", 0]}

            # 3. a streaming response arrives incrementally, not all at the end
            await hub.request(3, "GET", "/slow")
            arrivals = []
            t0 = time.time()
            while 3 not in hub.ended:
                await asyncio.sleep(0.02)
                n = len(hub.chunks.get(3, b""))
                if not arrivals or n > arrivals[-1][1]:
                    arrivals.append((time.time() - t0, n))
                assert time.time() - t0 < 5
            assert len(arrivals) >= 3 and arrivals[-1][0] - arrivals[1][0] > 0.15, arrivals

            # 4. request body flows down, response comes back
            await hub.request(5, "POST", "/echo", body=b"abcdef")
            await hub.wait_end(5)
            assert hub.heads[5]["status"] == 201 and bytes(hub.chunks[5]) == b"fedcba"

            # 5. abort cancels the site-side task
            await hub.request(7, "GET", "/forever")
            await asyncio.sleep(0.4)
            await hub.ws.send(encode({"t": "abort", "id": 7, "reason": "browser left"}))
            for _ in range(50):
                await asyncio.sleep(0.05)
                if site_app.cancelled and 7 not in agent.streams:  # the done-callback pops the stream a tick later
                    break
            assert site_app.cancelled and 7 not in agent.streams

            # 6. live events reach the hub; heartbeats carry the summary
            await asyncio.sleep(0.4)
            state.pipeline.subscribers and [q.put_nowait({"type": "event", "event": {"id": 42}}) for q in state.pipeline.subscribers]
            for _ in range(50):
                await asyncio.sleep(0.05)
                if any(f["t"] == "event" for f in hub.frames):
                    break
            assert any(f["t"] == "event" and f["msg"]["event"]["id"] == 42 for f in hub.frames)
            assert any(f["t"] == "heartbeat" and f["summary"]["cameras"] == 3 for f in hub.frames)

            # 7. hub goes away: agent reconnects with the token
            await hub.ws.close()
            for _ in range(200):
                await asyncio.sleep(0.05)
                if agent.connected and hub.auth.count("Bearer device-token-1") >= 2:
                    break
            assert agent.connected and hub.auth[-1] == "Bearer device-token-1"

            # 8. unenrol clears the token and shows a claim code again
            hub.enrol_on_claim = False
            await agent.configure(unenrol=True)
            await asyncio.sleep(0.3)
            st = agent.status()
            assert not st["enrolled"] and st["claim_code"] and "-" in st["claim_code"] and settings.remote_vlm_url == ""
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


async def _summary():
    return {"cameras": 3, "disk": {"free_gb": 100}}


def test_agent_end_to_end():
    asyncio.run(_run())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
