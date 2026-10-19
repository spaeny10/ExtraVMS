"""Site tunnels: the /agent WebSocket each site keeps open, and the registry the proxy uses.

An unenrolled site connects with `Authorization: Claim XXXX-XXXX`; the connection is parked until an admin
enters the code (then it gets `enrolled` and reconnects with its token). A central instance (hosts.py) connects once
with `Authorization: Enroll <token>`: a valid single-use token makes it a server of the token's Site at once (the same
`enrolled` frame); an unknown, used or expired one is refused at the handshake (403). An enrolled site connects with
`Authorization: Bearer <device token>` and gets `welcome`. Requests from the proxy become `req` streams
(odd ids); the site answers `res` + binary chunks + `end`, paced by the tunnelproto credit window.
A heartbeat whose cameras stream differently from the last one is pushed to /api/fleet/ws as `server_status` (the
pages refetch on it instead of polling); `still` frames carry each camera's latest picture (stills.py). The Site's AI
schedule goes down in `welcome` and `ai_schedule` frames, the server's Qwen use comes up in `ai_usage` (ai_schedule.py).
"""
from __future__ import annotations

import asyncio
import logging
import time

import sqlalchemy as sa
from fastapi import WebSocket, WebSocketDisconnect

from tunnelproto import CHUNK, WINDOW, Stream, chunk, decode, encode, split

from . import ai_schedule, alerts, cameras, central_cameras, db, hosts, lan_certs, soc, stills, talkdown_voice, turn
from .config import settings

log = logging.getLogger("hub.agents")

STILL_MIN_GAP_S = 5.0   # a camera's stills are stored at most this often (the agent sends one every 5 min, plus events)


class AgentConn:
    def __init__(self, ws: WebSocket, site: dict | None, claim_code: str | None = None, token: str | None = None) -> None:
        self.ws = ws
        self.site = site
        self.token = token                    # the device token this connection authenticated with                      # None while unenrolled (claim)
        self.site_id = site["id"] if site else None
        self.claim_code = claim_code
        self.hello: dict = {}
        self.streams: dict[int, Stream] = {}
        self._next_id = 1                     # hub-initiated streams are odd
        self._lock = asyncio.Lock()
        self.last_seen = time.time()
        self.turn_expires = 0.0                # the TURN credential this site holds (welcome, then refreshed on heartbeats)
        self.summary: dict | None = None
        self.subscribers: set[asyncio.Queue] = set()   # browsers on /s/{id}/api/ws
        self.closed = asyncio.Event()
        self.location: dict | None = _location_of(site)   # the Site (locations row) this server belongs to
        self.still_at: dict[str, float] = {}               # camera -> when its last still was taken in

    def tag(self) -> dict:
        """What org-wide messages say about where they came from: the server and its Site."""
        site = self.site or {}
        loc = self.location or {}
        return {"site_id": self.site_id, "site_name": site.get("name"), "location_id": loc.get("id"), "location_name": loc.get("name")}

    @property
    def ip(self) -> str | None:
        return self.ws.client.host if self.ws.client else None

    async def send(self, frame: dict) -> None:
        async with self._lock:
            await self.ws.send_text(encode(frame))

    async def send_bytes(self, data: bytes) -> None:
        async with self._lock:
            await self.ws.send_bytes(data)

    def next_id(self) -> int:
        sid = self._next_id
        self._next_id = (self._next_id + 2) & 0xFFFFFFFF or 1
        return sid

    async def request(self, method: str, path: str, query: str, headers: dict, body: bytes | None) -> Stream:
        """Send a request; the returned Stream yields the response (head then chunks)."""
        s = await self.open(method, path, query, headers, body=body is not None)
        if body is not None:
            await self.send_body(s, body)
            await self.send({"t": "end", "id": s.id})
        return s

    async def open(self, method: str, path: str, query: str, headers: dict, body: bool = True, ws: bool = False) -> Stream:
        """Start a request whose body the caller streams (send_body, then an `end` frame), or a WebSocket (`ws`: the
        site answers 101, then messages flow as ws_pieces both ways; sites whose hello caps list "camweb")."""
        if len(self.streams) >= settings.max_streams_per_site:
            raise TooManyStreams()
        s = Stream(self.next_id())
        self.streams[s.id] = s
        frame = {"t": "req", "id": s.id, "method": method, "path": path, "query": query, "headers": headers, "body": body}
        if ws:
            frame["ws"] = True
        await self.send(frame)
        return s

    async def send_body(self, s: Stream, data: bytes) -> None:
        """Request body (or WebSocket) bytes under the stream's credit window; stops quietly once it is aborted."""
        for piece in split(data):
            await s.take_credit(len(piece))
            if s.aborted:
                return
            await self.send_bytes(chunk(s.id, piece))

    def caps(self) -> list:
        return list(self.hello.get("caps") or [])

    async def call(self, method: str, path: str, query: str = "", headers: dict | None = None, body: bytes | None = None,
                   timeout: float = 15.0) -> tuple[int, bytes]:
        """A whole request/response through the tunnel, for hub-side features (fleet search, digests, backups)."""
        s = await self.request(method, path, query, headers or {}, body)
        try:
            await asyncio.wait_for(s.head.wait(), timeout)
            if s.aborted or s.status is None:
                raise RuntimeError(s.aborted or "no response")
            out = bytearray()
            while True:
                c = await asyncio.wait_for(s.read(), timeout)
                if c is None:
                    break
                out += c
                await self.send({"t": "credit", "id": s.id, "bytes": len(c)})
            return s.status, bytes(out)
        except asyncio.TimeoutError:
            await self.abort(s, "timeout")
            raise RuntimeError("site did not answer in time")
        finally:
            self.finish(s)

    async def abort(self, s: Stream, reason: str) -> None:
        if self.streams.pop(s.id, None) is not None and not s.done.is_set():
            await s.abort(reason)
            try:
                await self.send({"t": "abort", "id": s.id, "reason": reason})
            except Exception:
                pass

    def finish(self, s: Stream) -> None:
        self.streams.pop(s.id, None)

    def broadcast(self, msg: dict) -> None:
        for q in list(self.subscribers):
            if q.qsize() < 100:
                q.put_nowait(msg)


class TooManyStreams(Exception):
    pass


# A WebSocket through the tunnel (camera web pages, camweb.py): each body chunk is one flag byte and up to CHUNK - 1
# bytes of a message; bit 0 = binary (else UTF-8 text), bit 1 = more of this message follows. The site's agent
# (backend/nvr/hub_agent.py) has the same two helpers.
WS_BINARY, WS_MORE = 1, 2


def ws_pieces(data: bytes | str) -> list[bytes]:
    binary = isinstance(data, (bytes, bytearray, memoryview))
    raw = bytes(data) if binary else data.encode()
    flag = WS_BINARY if binary else 0
    parts = [raw[i:i + CHUNK - 1] for i in range(0, len(raw), CHUNK - 1)] or [b""]
    return [bytes([flag | (WS_MORE if i < len(parts) - 1 else 0)]) + p for i, p in enumerate(parts)]


class WsAssembler:
    """Body chunks back into messages: feed() returns (binary, payload) once a message is whole, else None."""

    def __init__(self, limit: int = 16 * 1024 * 1024) -> None:
        self.buf = bytearray()
        self.limit = limit

    def feed(self, data: bytes) -> tuple[bool, bytes] | None:
        if not data:
            return None
        self.buf += data[1:]
        if len(self.buf) > self.limit:
            raise ValueError("WebSocket message too large")
        if data[0] & WS_MORE:
            return None
        out = bytes(self.buf)
        self.buf.clear()
        return bool(data[0] & WS_BINARY), out


def _location_of(site: dict | None) -> dict | None:
    if not site or not site.get("location_id"):
        return None
    try:
        return db.one(sa.select(db.locations).where(db.locations.c.id == site["location_id"]))
    except Exception:   # the tunnel must come up even if this lookup fails
        log.exception("location lookup for %s", site.get("id"))
        return None


def _sync_cameras(site: dict, cams: list, disabled, source: str) -> None:
    """Keep the cameras registry in step; registry trouble must never break the tunnel or alerting."""
    try:
        cameras.sync(site, cams, full=True, disabled=disabled if isinstance(disabled, list) else None, source=source)
    except Exception:
        log.exception("cameras registry sync for %s", site.get("id"))


def _central_cameras(conn: "AgentConn") -> None:
    """A central instance's camera list changed: its firewall follows (central_cameras.py); never breaks the tunnel."""
    try:
        central_cameras.on_heartbeat(conn.site_id, conn.summary or {})
    except Exception:
        log.exception("central camera addresses for %s", conn.site_id)


def _soc(conn: "AgentConn", fn, arg) -> None:
    """Feed the SOC queue (soc.on_event / on_attention); SOC trouble must never break the tunnel or alerting."""
    try:
        fn(conn, arg)
    except Exception:
        log.exception("soc feed for %s", conn.site_id)


def camera_status(summary: dict | None) -> tuple[frozenset, int] | None:
    """(ids of the cameras streaming, cameras listed) from a heartbeat summary; None when it lists no cameras."""
    cams = (summary or {}).get("cameras")
    if not isinstance(cams, list):
        return None
    up = frozenset(str(c.get("id")) for c in cams if isinstance(c, dict) and c.get("stream_ready"))
    return up, sum(1 for c in cams if isinstance(c, dict))


def _still(conn: "AgentConn", frame: dict) -> None:
    """A camera's latest picture (stills.py); a refused one is dropped, trouble never breaks the tunnel."""
    cam = frame.get("cam")
    now = time.time()
    if not isinstance(cam, str) or now - conn.still_at.get(cam, 0.0) < STILL_MIN_GAP_S:
        return
    try:
        if stills.accept(conn.site_id, frame):
            conn.still_at[cam] = now
            if len(conn.still_at) > 1000:   # ids the server made up: a bounded memory, not a map of every one
                conn.still_at.clear()
        else:
            log.debug("site %s: still for %r refused", conn.site_id, cam)
    except Exception:
        log.exception("still from %s", conn.site_id)


def _ai_schedule(conn: "AgentConn") -> None:
    """The server follows another AI schedule than its Site's: push the Site's (ai_schedule.py); never breaks the tunnel."""
    try:
        ai_schedule.on_heartbeat(conn)
    except Exception:
        log.exception("AI schedule for %s", conn.site_id)


def _ai_usage(conn: "AgentConn", frame: dict) -> None:
    """The server's Qwen use per day (ai_schedule.on_usage); trouble storing it never breaks the tunnel."""
    try:
        ai_schedule.on_usage(conn, frame)
    except Exception:
        log.exception("Qwen usage from %s", conn.site_id)


def _talkdown_voice(conn: "AgentConn") -> None:
    """The server reports another talk-down voice than its Site's: push the Site's (talkdown_voice.py); never breaks the tunnel."""
    try:
        talkdown_voice.on_heartbeat(conn)
    except Exception:
        log.exception("talk-down voice for %s", conn.site_id)


class AgentRegistry:
    def __init__(self) -> None:
        self.by_site: dict[str, AgentConn] = {}
        self.pending: dict[str, AgentConn] = {}      # claim code -> parked connection
        self.started_at = time.time()
        self.org_subscribers: dict[str, set[asyncio.Queue]] = {}   # org -> browsers on /api/fleet/ws

    def get(self, site_id: str) -> AgentConn | None:
        return self.by_site.get(site_id)

    # ---- the /agent socket
    async def serve(self, ws: WebSocket) -> None:
        auth = ws.headers.get("authorization", "")
        scheme, _, cred = auth.partition(" ")
        site = None
        claim_code = None
        if scheme == "Bearer" and cred:
            h = db.token_hash(cred)
            site = db.one(sa.select(db.sites).where(db.sites.c.token_hash == h))
            if not site:
                prev = db.one(sa.select(db.sites).where(db.sites.c.token_prev_hash == h))
                if prev and prev["token_rotated_at"] and time.time() - prev["token_rotated_at"] < 600:
                    site = prev
            if not site:
                await ws.close(code=4401, reason="unknown token")
                return
        elif scheme == "Enroll" and cred:
            ip = ws.client.host if ws.client else None
            if hosts.enroll_check(cred.strip()) is None:
                hosts.log_bad_enroll(cred.strip(), ip)
                await ws.close(code=4403, reason="invalid enrollment token")   # before accept: the handshake gets 403
                return
            await ws.accept()
            await self._enroll(ws, cred.strip(), ip)
            return
        elif scheme == "Claim" and cred:
            claim_code = cred.strip().upper()
            if len(claim_code) > 16:
                await ws.close(code=4400, reason="bad claim")
                return
        else:
            await ws.close(code=4401, reason="authorization required")
            return
        await ws.accept()
        conn = AgentConn(ws, site, claim_code, cred if scheme == "Bearer" else None)
        try:
            await self._loop(conn)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("agent %s: connection failed", conn.site_id or claim_code)
        finally:
            await self._detach(conn)

    async def _enroll(self, ws: WebSocket, token: str, ip: str | None) -> None:
        """A central instance with a valid enrollment token: wait for its hello (hostname, version), create the server
        in the token's Site and hand it a device token exactly like a claim does, then close; it reconnects with Bearer."""
        hello: dict = {}
        try:
            m = await asyncio.wait_for(ws.receive(), 15)
            if m["type"] == "websocket.disconnect":
                return
            frame = decode(m["text"]) if m.get("text") is not None else None
            if isinstance(frame, dict) and frame.get("t") == "hello":
                hello = frame
        except asyncio.TimeoutError:
            pass
        except Exception:
            log.exception("enrollment: reading hello")
        got = hosts.enroll(token, hello, ip)
        try:
            if got is None:
                hosts.log_bad_enroll(token, ip)
                await ws.close(code=4403, reason="invalid enrollment token")
                return
            site, device = got
        except Exception:
            return
        try:
            await ws.send_text(encode({"t": "enrolled", "site_id": site["id"], "token": device}))
        except Exception:
            hosts.enroll_undo(token, site["id"])   # it never got its device token: the same token may try again
            return
        try:
            await ws.close(code=1000, reason="enrolled")
        except Exception:
            pass

    async def _loop(self, conn: AgentConn) -> None:
        while True:
            m = await conn.ws.receive()
            if m["type"] == "websocket.disconnect":
                return
            frame = decode(m["bytes"]) if m.get("bytes") is not None else decode(m["text"])
            if isinstance(frame, tuple):
                sid, data = frame
                s = conn.streams.get(sid)
                if s is not None:
                    s.push(data)
                continue
            t = frame.get("t")
            if t == "hello":
                await self._on_hello(conn, frame)
            elif t == "heartbeat":
                conn.last_seen = time.time()
                prev, summary = conn.summary or (conn.site or {}).get("summary") or {}, frame.get("summary") or {}
                if summary and "cameras" not in summary and isinstance(prev.get("cameras"), list):
                    # a minimal heartbeat (the server's database is failing: summary.minimal_summary) has no camera
                    # data: keep the last known cameras for the site card and camera alerts, with its problems on top
                    summary = {**prev, **summary, "minimal": True}
                conn.summary = summary
                before, after = camera_status(prev), camera_status(summary)
                if conn.site:
                    skew = float(frame.get("now") or time.time()) - time.time()
                    db.run(sa.update(db.sites).where(db.sites.c.id == conn.site_id).values(
                        last_seen_at=time.time(), online=True, summary=conn.summary, clock_skew_s=round(skew, 1),
                        version=conn.summary.get("version") or conn.site.get("version")))
                    if isinstance(conn.summary.get("cameras"), list):   # an empty summary says nothing about cameras
                        _sync_cameras(conn.site, conn.summary["cameras"], conn.summary.get("disabled"), "heartbeat")
                        _central_cameras(conn)
                    alerts.on_heartbeat(conn.site, conn.summary, skew)
                    _soc(conn, soc.on_attention, conn.summary.get("attention"))
                    _talkdown_voice(conn)
                    _ai_schedule(conn)
                    await self._refresh_turn(conn)
                    if after is not None and after != before:
                        # site and server pages refetch on this instead of polling (hub/ui useFleetRefresh)
                        self.broadcast_org(conn.site["org_id"], {"type": "server_status", "cameras_up": len(after[0]),
                                                                 "cameras_total": after[1], **conn.tag()})
            elif t == "event":
                msg = frame.get("msg") or {}
                conn.broadcast(msg)
                if conn.site:
                    alerts.on_event(conn.site, msg)
                    _soc(conn, soc.on_event, msg)
                    self.broadcast_org(conn.site["org_id"], {**msg, **conn.tag()})
            elif t == "still":
                if conn.site:
                    _still(conn, frame)
            elif t == "ai_usage":
                if conn.site:
                    _ai_usage(conn, frame)
            elif t == "lan_csr":
                if conn.site:   # a certificate for its Direct-on-LAN names (lan_certs.py); answered with a lan_cert frame
                    await lan_certs.on_csr(conn, frame)
            elif t == "res":
                s = conn.streams.get(frame["id"])
                if s is not None:
                    s.status, s.headers = int(frame["status"]), {k.lower(): v for k, v in (frame.get("headers") or {}).items()}
                    s.head.set()
            elif t == "end":
                s = conn.streams.get(frame["id"])
                if s is not None:
                    s.push(None)
            elif t == "abort":
                s = conn.streams.get(frame["id"])
                if s is not None:
                    await s.abort(frame.get("reason", "site aborted"))
            elif t == "credit":
                s = conn.streams.get(frame["id"])
                if s is not None:
                    await s.grant(int(frame["bytes"]))
            elif t == "ping":
                await conn.send({"t": "pong", "ts": frame.get("ts")})
            elif t == "rotated":
                log.info("site %s confirmed token rotation", conn.site_id)

    async def _on_hello(self, conn: AgentConn, frame: dict) -> None:
        conn.hello = frame
        if conn.site is None:  # unenrolled: park the connection and record the claim for the UI
            code = conn.claim_code or ""
            hint = {"hostname": frame.get("hostname"), "cameras": frame.get("cameras") or [], "version": frame.get("site_version")}
            existing = db.one(sa.select(db.claims).where(db.claims.c.code == code))
            if existing:
                db.run(sa.update(db.claims).where(db.claims.c.code == code).values(hint=hint, agent_ip=conn.ip, expires_at=time.time() + 900))
            else:
                db.insert(db.claims, {"code": code, "hint": hint, "agent_ip": conn.ip, "first_seen_at": time.time(),
                                      "expires_at": time.time() + 900, "consumed_site_id": None})
            self.pending[code] = conn
            log.info("claim %s waiting (%s, %d cameras)", code, frame.get("hostname"), len(hint["cameras"]))
            return
        old = self.by_site.get(conn.site_id)
        if old is not None and old is not conn:
            await old.ws.close(code=4409, reason="replaced by a newer connection")
        self.by_site[conn.site_id] = conn
        conn.last_seen = time.time()
        db.run(sa.update(db.sites).where(db.sites.c.id == conn.site_id).values(
            online=True, last_seen_at=time.time(), agent_ip=conn.ip, hostname=frame.get("hostname"),
            version=frame.get("site_version")))
        if isinstance(frame.get("cameras"), list):
            _sync_cameras(conn.site, frame["cameras"], frame.get("disabled"), "hello")
        org = db.one(sa.select(db.orgs).where(db.orgs.c.id == conn.site["org_id"]))
        cred = turn.mint(f"site:{conn.site_id}", settings.turn_site_ttl_s)
        conn.turn_expires = float(cred["expires"]) if cred else 0.0
        welcome = {"t": "welcome", "site_id": conn.site_id, "org": org["name"] if org else None,
                   "location": (conn.location or {}).get("name"),   # the Site's name (additive; old agents ignore it)
                   "location_id": (conn.location or {}).get("id"),  # for the server UI's link back to /sites/<id>/servers
                   "heartbeat_s": settings.heartbeat_s, "now": time.time(), "max_streams": settings.max_streams_per_site,
                   "turn": cred,
                   "stills": True,   # send each camera's latest picture (stills.py); older agents ignore it
                   "vlm": self.vlm_config(org, conn.token, conn.site_id),
                   # the Site's AI schedule (ai_schedule.py) with its SOC windows and timezone; older agents ignore it
                   "ai_schedule": ai_schedule.config(conn.location),
                   **lan_certs.welcome(conn)}   # {"lan_cert": {"name"}}: this server may ask for its LAN certificate
        await conn.send(welcome)
        alerts.close(conn.site, "offline")
        log.info("site %s (%s) connected from %s", conn.site_id, conn.site["name"], conn.ip)
        self.broadcast_org(conn.site["org_id"], {"type": "site_online", **conn.tag()})

    async def _refresh_turn(self, conn: AgentConn) -> None:
        """Site TURN credentials are short-lived (settings.turn_site_ttl_s): a fresh one goes down the tunnel
        ({"t": "turn"}, which every agent version handles) once the current one is within turn_site_refresh_s of
        expiring, so a long-lived connection never ends up relaying with an expired credential."""
        if not conn.turn_expires or conn.turn_expires - time.time() > settings.turn_site_refresh_s:
            return
        cred = turn.mint(f"site:{conn.site_id}", settings.turn_site_ttl_s)
        if not cred:
            return
        conn.turn_expires = float(cred["expires"])
        try:
            await conn.send({"t": "turn", "turn": cred})
        except Exception as e:
            log.warning("TURN refresh for %s failed: %s", conn.site_id, e)

    def broadcast_org(self, org_id: str, msg: dict) -> None:
        """Dashboards and other org-wide pages listen on /api/fleet/ws; slow readers are skipped, not blocked."""
        for q in list(self.org_subscribers.get(org_id, ())):
            if q.qsize() < 100:
                q.put_nowait(msg)

    async def _detach(self, conn: AgentConn) -> None:
        conn.closed.set()
        for s in list(conn.streams.values()):
            await s.abort("site disconnected")
        conn.streams.clear()
        for q in list(conn.subscribers):
            q.put_nowait({"type": "site_offline"})
        if conn.claim_code and self.pending.get(conn.claim_code) is conn:
            del self.pending[conn.claim_code]
        if conn.site_id and self.by_site.get(conn.site_id) is conn:
            del self.by_site[conn.site_id]
            db.run(sa.update(db.sites).where(db.sites.c.id == conn.site_id).values(online=False, last_seen_at=time.time()))
            log.info("site %s disconnected", conn.site_id)
            if conn.site:
                self.broadcast_org(conn.site["org_id"], {"type": "site_offline", **conn.tag()})

    def refresh(self, server_id: str) -> None:
        """A server row or its Site changed at the hub (rename, move): re-read them for the live connection."""
        conn = self.by_site.get(server_id)
        if conn is None:
            return
        row = db.one(sa.select(db.sites).where(db.sites.c.id == server_id))
        if row:
            conn.site = row
            conn.location = _location_of(row)

    def refresh_location(self, location_id: str) -> None:
        for conn in list(self.by_site.values()):
            if conn.site and conn.site.get("location_id") == location_id:
                self.refresh(conn.site_id)

    @staticmethod
    def vlm_config(org: dict | None, token: str | None, site_id: str | None = None) -> dict | None:
        """What a site should put in its remote-VLM settings: the hub's /v1 with its own device token as key.
        The site that serves the shared AI (HUB_VLLM_SITE) gets nothing: handing it the hub's URL would relay its
        own requests straight back to it and /v1 answers 409 — it already runs the model locally."""
        if not (org and org.get("ai_shared") and (settings.vllm_url or settings.vllm_site) and settings.vllm_model and token):
            return None
        if settings.vllm_site and site_id == settings.vllm_site:
            return None
        return {"url": settings.public_url.rstrip("/") + "/v1", "model": settings.vllm_model, "key": token}

    async def push_vlm(self, org: dict) -> None:
        """The org's sharing flag changed: tell its connected sites."""
        for conn in list(self.by_site.values()):
            if conn.site and conn.site["org_id"] == org["id"]:
                await conn.send({"t": "vlm", "vlm": self.vlm_config(org, conn.token, conn.site_id)})

    # ---- enrollment from the UI
    async def enrol(self, code: str, site: dict, token: str) -> bool:
        """Hand the device token to the parked connection (if it's still there) and close it."""
        conn = self.pending.pop(code.upper(), None)
        if conn is None:
            return False
        try:
            await conn.send({"t": "enrolled", "site_id": site["id"], "token": token})
            await conn.ws.close(code=1000, reason="enrolled")
        except Exception:
            pass
        return True

    async def push(self, site_id: str, frame: dict) -> bool:
        conn = self.by_site.get(site_id)
        if conn is None:
            return False
        await conn.send(frame)
        return True

    async def sweep(self) -> None:
        """Mark sites offline that stopped heartbeating (the socket may linger behind a NAT)."""
        cutoff = time.time() - settings.offline_after_s
        for conn in list(self.by_site.values()):
            if conn.last_seen < cutoff:
                try:
                    await conn.ws.close(code=4408, reason="no heartbeat")
                except Exception:
                    pass
        if time.time() - self.started_at > 120:  # not in the first two minutes after a restart
            for s in db.rows(sa.select(db.sites).where(db.sites.c.online == False, db.sites.c.retired_at.is_(None))):  # noqa: E712
                if s["last_seen_at"] and s["last_seen_at"] < cutoff:
                    alerts.open(s, "offline", "", {"last_seen_at": s["last_seen_at"]})


registry = AgentRegistry()
