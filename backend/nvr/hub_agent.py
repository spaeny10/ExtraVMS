"""Site side of the fleet hub: one outbound WebSocket to the hub, nothing inbound.

The agent keeps a connection to `settings.hub_url` (wss://hub.axiomvision.ai/agent). Over it the site sends a
heartbeat every 30 s (summary.site_summary) and every live event message (the same payloads /api/ws gets),
and the hub sends HTTP requests which are answered by calling this process's own ASGI app in-process:
no loopback socket, streaming bodies (Ask NDJSON, fMP4 playback) flow chunk by chunk under the credit window
of tunnelproto, and a hub-side abort cancels the request task.

Enrolment: an unenrolled site connects with `Authorization: Claim <code>` and shows the code on Settings →
System; when the owner enters it at the hub, the hub answers `enrolled` with a device token, which is stored
in the settings table and used as `Authorization: Bearer` from then on. The hub also pushes shared-AI
(`vlm`) and TURN configuration, which are applied at runtime and remembered.
"""
from __future__ import annotations

import asyncio
import logging
import random
import secrets
import socket
import ssl
import string
import time
from typing import Any, Callable

import websockets

from tunnelproto import CHUNK, HEARTBEAT_S, PING_S, PROTO, WINDOW, Stream, chunk, decode, encode, split

from . import __version__
from .config import settings
from .db import db

log = logging.getLogger("nvr.hub")

CLAIM_TTL_S = 15 * 60
# remote-VLM settings the operator pinned in .env (captured before the hub ever assigns them at runtime)
_ENV_PINNED = frozenset(settings.model_fields_set) & {"remote_vlm_url", "remote_vlm_key", "remote_vlm_model"}
CLAIM_ALPHABET = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"
IN_PROCESS_CLIENT = ("hub", 0)   # scope["client"] marker: the request came down the tunnel, not from the LAN


def new_claim() -> dict:
    code = "".join(secrets.choice(CLAIM_ALPHABET) for _ in range(8))
    return {"code": f"{code[:4]}-{code[4:]}", "expires": time.time() + CLAIM_TTL_S}


class HubAgent:
    def __init__(self, app, state, summary_fn: Callable[..., Any] | None = None) -> None:
        self.app = app
        self.state = state
        self.summary_fn = summary_fn
        self.connected = False
        self.enrolled = bool(db.get_setting("hub_token"))
        self.site_id: str | None = db.get_setting("hub_site_id")
        self.org: str | None = None
        self.last_error: str | None = None
        self.last_heartbeat: float | None = None
        self.streams: dict[int, Stream] = {}
        self._ws = None
        self._tasks: list[asyncio.Task] = []
        self._send_lock = asyncio.Lock()
        self._last_summary_at = time.time()

    # ---- status for the UI
    def hub_url(self) -> str:
        return db.get_setting("hub_url") or settings.hub_url

    def claim(self) -> dict | None:
        if self.enrolled:
            return None
        c = db.get_setting("hub_claim")
        if not c or c["expires"] < time.time() + 60:
            c = new_claim()
            db.set_setting("hub_claim", c)
        return c

    def status(self) -> dict:
        c = self.claim()
        return {"enabled": settings.hub_enabled and bool(self.hub_url()), "hub_url": self.hub_url(), "connected": self.connected,
                "enrolled": self.enrolled, "site_id": self.site_id, "org": self.org, "claim_code": c["code"] if c else None,
                "claim_expires": c["expires"] if c else None, "last_error": self.last_error, "last_heartbeat": self.last_heartbeat,
                "vlm_managed": bool(db.get_setting("hub_vlm"))}

    async def configure(self, hub_url: str | None = None, unenrol: bool = False) -> None:
        if hub_url is not None:
            db.set_setting("hub_url", hub_url.strip())
        if unenrol:
            for k in ("hub_token", "hub_site_id", "hub_vlm", "hub_turn"):
                db.set_setting(k, None)
            self.enrolled, self.site_id, self.org = False, None, None
            self._apply_vlm(None)
        await self.reconnect()

    async def reconnect(self) -> None:
        if self._ws is not None:
            await self._ws.close()

    # ---- main loop
    async def run(self) -> None:
        backoff = 1.0
        self._apply_vlm(db.get_setting("hub_vlm"))  # remembered shared-AI config from the last session
        while True:
            if not settings.hub_enabled or not self.hub_url():
                await asyncio.sleep(30)
                continue
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network errors, handshake rejections, protocol surprises
                self.last_error = str(e)[:200]
                if backoff >= 30:
                    log.warning("hub: %s; retry in %.0fs", self.last_error, backoff)
            self.connected = False
            await asyncio.sleep(backoff + random.uniform(0, backoff / 2))
            backoff = min(backoff * 2, 60)

    def _auth_header(self) -> str:
        token = db.get_setting("hub_token")
        if token:
            return f"Bearer {token}"
        return f"Claim {self.claim()['code']}"

    async def _session(self) -> None:
        url = self.hub_url()
        kw: dict = {}
        if url.startswith("wss://") and settings.hub_insecure:  # dev hub with a self-signed certificate
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            kw["ssl"] = ctx
        async with websockets.connect(url, additional_headers={"Authorization": self._auth_header()},
                                      max_size=CHUNK + 1024, ping_interval=None, open_timeout=15, **kw) as ws:
            self._ws = ws
            cams = [{"id": c["id"], "name": c["name"]} for c in db.cameras(enabled_only=True)]
            await self._send({"t": "hello", "proto": PROTO, "site_version": __version__, "cameras": cams, "now": time.time(),
                              "site_id": self.site_id, "hostname": socket.gethostname(), "caps": ["events", "turn", "vlm"]})
            try:
                async for msg in ws:
                    await self._on_message(decode(msg))
            finally:
                self._ws = None
                self.connected = False
                for t in self._tasks:
                    t.cancel()
                self._tasks.clear()
                for s in list(self.streams.values()):
                    await s.abort("disconnected")
                self.streams.clear()

    async def _send(self, frame: dict) -> None:
        async with self._send_lock:
            if self._ws is not None:
                await self._ws.send(encode(frame))

    async def _send_bytes(self, data: bytes) -> None:
        async with self._send_lock:
            if self._ws is not None:
                await self._ws.send(data)

    # ---- inbound frames
    async def _on_message(self, m) -> None:
        if isinstance(m, tuple):  # request body chunk
            sid, data = m
            s = self.streams.get(sid)
            if s is not None:
                s.push(data)
                await self._send({"t": "credit", "id": sid, "bytes": len(data)})
            return
        t = m.get("t")
        if t == "welcome":
            self.connected, self.last_error = True, None
            self.site_id, self.org = m.get("site_id", self.site_id), m.get("org")
            if self.site_id:
                db.set_setting("hub_site_id", self.site_id)
            hb = float(m.get("heartbeat_s") or HEARTBEAT_S)
            self._tasks = [asyncio.create_task(self._heartbeat_loop(hb), name="hub-heartbeat"),
                           asyncio.create_task(self._event_loop(), name="hub-events"),
                           asyncio.create_task(self._ping_loop(), name="hub-ping")]
            if "vlm" in m:
                db.set_setting("hub_vlm", m["vlm"])
                self._apply_vlm(m["vlm"])
            if "turn" in m:
                self._apply_turn(m["turn"])
            log.info("hub: connected as %s (%s)", self.site_id, self.org or "no org")
        elif t == "enrolled":
            db.set_setting("hub_token", m["token"])
            db.set_setting("hub_site_id", m.get("site_id"))
            db.set_setting("hub_claim", None)
            self.enrolled, self.site_id = True, m.get("site_id")
            log.info("hub: enrolled as %s; reconnecting with the device token", self.site_id)
            await self.reconnect()
        elif t == "rotate":
            db.set_setting("hub_token", m["token"])
            await self._send({"t": "rotated"})
            await self.reconnect()
        elif t == "revoked":
            log.warning("hub: enrolment revoked (%s)", m.get("reason", ""))
            await self.configure(unenrol=True)
        elif t == "vlm":
            db.set_setting("hub_vlm", m.get("vlm"))
            self._apply_vlm(m.get("vlm"))
        elif t == "turn":
            self._apply_turn(m.get("turn"))
        elif t == "req":
            s = Stream(m["id"])
            s.headers = {k.lower(): v for k, v in (m.get("headers") or {}).items()}
            self.streams[m["id"]] = s
            if not m.get("body"):
                s.push(None)
            task = asyncio.create_task(self._serve(m, s), name=f"hub-req-{m['id']}")
            task.add_done_callback(lambda _t, sid=m["id"]: self.streams.pop(sid, None))
        elif t == "end":
            s = self.streams.get(m["id"])
            if s is not None:
                s.push(None)
        elif t == "abort":
            s = self.streams.get(m["id"])
            if s is not None:
                await s.abort(m.get("reason", "aborted"))
        elif t == "credit":
            s = self.streams.get(m["id"])
            if s is not None:
                await s.grant(int(m["bytes"]))
        elif t == "ping":
            await self._send({"t": "pong", "ts": m.get("ts")})

    # ---- serving a hub request against our own app, in-process
    async def _serve(self, req: dict, s: Stream) -> None:
        headers = [(k.lower().encode(), str(v).encode()) for k, v in (req.get("headers") or {}).items()]
        if not any(k == b"host" for k, _ in headers):
            headers.append((b"host", b"hub"))
        query = req.get("query") or ""
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                 "method": req["method"].upper(), "scheme": "http", "path": req["path"], "raw_path": req["path"].encode(),
                 "query_string": query.encode(), "root_path": "", "headers": headers,
                 "client": IN_PROCESS_CLIENT, "server": ("hub", 0), "state": {}}
        started = False

        async def receive():
            data = await s.read()
            if data is None:
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.request", "body": data, "more_body": True}

        async def send(msg):
            nonlocal started
            if s.aborted:
                raise asyncio.CancelledError
            if msg["type"] == "http.response.start":
                started = True
                hdrs = {k.decode(): v.decode() for k, v in msg.get("headers", [])}
                await self._send({"t": "res", "id": s.id, "status": msg["status"], "headers": hdrs})
            elif msg["type"] == "http.response.body":
                body = msg.get("body", b"")
                for piece in split(body):
                    await s.take_credit(len(piece))
                    if s.aborted:
                        raise asyncio.CancelledError
                    await self._send_bytes(chunk(s.id, piece))
                if not msg.get("more_body", False):
                    await self._send({"t": "end", "id": s.id})

        try:
            await self.app(scope, receive, send)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("hub request %s %s failed", req["method"], req["path"])
            if not started and not s.aborted:
                await self._send({"t": "res", "id": s.id, "status": 500, "headers": {"content-type": "text/plain"}})
                await self._send_bytes(chunk(s.id, str(e).encode()))
                await self._send({"t": "end", "id": s.id})
            elif not s.aborted:
                await self._send({"t": "abort", "id": s.id, "reason": "error"})

    # ---- background loops while connected
    async def _heartbeat_loop(self, every: float) -> None:
        while True:
            try:
                fn = self.summary_fn
                if fn is None:
                    from .summary import site_summary
                    fn = lambda st, since: site_summary(st, since, __version__)  # noqa: E731
                summary = await fn(self.state, self._last_summary_at)
                self._last_summary_at = time.time()
                await self._send({"t": "heartbeat", "now": time.time(), "summary": summary})
                self.last_heartbeat = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("hub heartbeat failed: %s", e)
            await asyncio.sleep(every)

    async def _event_loop(self) -> None:
        q: asyncio.Queue = asyncio.Queue()
        subs = self.state.pipeline.subscribers
        subs.add(q)
        try:
            while True:
                msg = await q.get()
                await self._send({"t": "event", "msg": msg})
        finally:
            subs.discard(q)

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(PING_S)
            await self._send({"t": "ping", "ts": time.time()})

    def _apply_turn(self, turn: dict | None) -> None:
        """Remember the hub's TURN credentials and hand them to MediaMTX (it re-reads its config)."""
        if db.get_setting("hub_turn") == turn:
            return
        db.set_setting("hub_turn", turn)
        mtx = getattr(self.state, "mtx", None)
        if mtx is not None:
            try:
                mtx.write_config(db.cameras(enabled_only=True))
            except Exception as e:
                log.warning("hub: could not apply TURN to MediaMTX: %s", e)

    # ---- shared AI pushed by the hub
    @staticmethod
    def _apply_vlm(vlm: dict | None) -> None:
        """Point vlmroute at the hub's model unless .env pins its own remote."""
        if _ENV_PINNED:
            return  # operator configured a remote explicitly in .env; the hub doesn't override it
        if vlm and vlm.get("url"):
            settings.remote_vlm_url, settings.remote_vlm_key, settings.remote_vlm_model = vlm["url"], vlm.get("key", ""), vlm.get("model", "")
        else:
            settings.remote_vlm_url = settings.remote_vlm_key = settings.remote_vlm_model = ""
