"""Site tunnels: the /agent WebSocket each site keeps open, and the registry the proxy uses.

An unenrolled site connects with `Authorization: Claim XXXX-XXXX`; the connection is parked until an admin
enters the code (then it gets `enrolled` and reconnects with its token). An enrolled site connects with
`Authorization: Bearer <device token>` and gets `welcome`. Requests from the proxy become `req` streams
(odd ids); the site answers `res` + binary chunks + `end`, paced by the tunnelproto credit window.
"""
from __future__ import annotations

import asyncio
import logging
import time

import sqlalchemy as sa
from fastapi import WebSocket, WebSocketDisconnect

from tunnelproto import CHUNK, WINDOW, Stream, chunk, decode, encode, split

from . import alerts, cameras, db, soc, turn
from .config import settings

log = logging.getLogger("hub.agents")


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
        if len(self.streams) >= settings.max_streams_per_site:
            raise TooManyStreams()
        s = Stream(self.next_id())
        self.streams[s.id] = s
        await self.send({"t": "req", "id": s.id, "method": method, "path": path, "query": query, "headers": headers, "body": body is not None})
        if body is not None:
            for piece in split(body):
                await s.take_credit(len(piece))
                await self.send_bytes(chunk(s.id, piece))
            await self.send({"t": "end", "id": s.id})
        return s

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


def _soc(conn: "AgentConn", fn, arg) -> None:
    """Feed the SOC queue (soc.on_event / on_attention); SOC trouble must never break the tunnel or alerting."""
    try:
        fn(conn, arg)
    except Exception:
        log.exception("soc feed for %s", conn.site_id)


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
                conn.summary = frame.get("summary") or {}
                if conn.site:
                    skew = float(frame.get("now") or time.time()) - time.time()
                    db.run(sa.update(db.sites).where(db.sites.c.id == conn.site_id).values(
                        last_seen_at=time.time(), online=True, summary=conn.summary, clock_skew_s=round(skew, 1),
                        version=conn.summary.get("version") or conn.site.get("version")))
                    if isinstance(conn.summary.get("cameras"), list):   # an empty summary says nothing about cameras
                        _sync_cameras(conn.site, conn.summary["cameras"], conn.summary.get("disabled"), "heartbeat")
                    alerts.on_heartbeat(conn.site, conn.summary, skew)
                    _soc(conn, soc.on_attention, conn.summary.get("attention"))
                    await self._refresh_turn(conn)
            elif t == "event":
                msg = frame.get("msg") or {}
                conn.broadcast(msg)
                if conn.site:
                    alerts.on_event(conn.site, msg)
                    _soc(conn, soc.on_event, msg)
                    self.broadcast_org(conn.site["org_id"], {**msg, **conn.tag()})
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
                   "vlm": self.vlm_config(org, conn.token, conn.site_id)}
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
