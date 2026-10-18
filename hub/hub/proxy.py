"""/s/{site_id}/... : the site's own UI and API, reached through its tunnel.

Static files come from the site UI bundle the hub ships (frontend/dist), byte-identical to what the site
serves itself; API and WebSocket requests go down the tunnel with the hub user's identity attached. Hold to Talk
(/s/{site_id}/api/speakers/{id}/talk) is a WebSocket relayed through the tunnel to the site, both ways.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from tunnelproto import chunk

from . import auth, central_cameras, db, security, viewing
from .agents import TooManyStreams, WsAssembler, registry, ws_pieces
from .config import settings
from .roles import allows, required_role

log = logging.getLogger("hub.proxy")
router = APIRouter()

# talk-down plays are audited with what was played where (the site's answer names the speaker, the message and the
# words of a spoken text); a Describe & warn is audited lightly (which speaker, which camera), never what it said
TALKDOWN_PLAY = re.compile(r"^/api/speakers/[^/]+/play$")
TALKDOWN_DESCRIBE = re.compile(r"^/api/speakers/([^/]+)/describe$")
TALKDOWN_ANSWER_MAX = 8192
# a live view hanging up (WhepPlayer teardown): not audited, every tile closing would write a row (the offer already
# does). The offers themselves (retries included) make one row per viewing session of a camera (viewing.py).
WHEP_HANGUP = re.compile(r"^/api/whep/[^/]+/[^/]+$")
TALKDOWN_TEXT_MAX = 300
# Hold to Talk (backend speakers_api.talk): the browser's microphone, a WebSocket relayed to the site as a "ws" stream (as
# camera page sockets are, camweb.py), allowed as a POST of its path would be (roles.py); one audit row per live talk,
# written when it ends, from the site's own status messages (the speaker, its cameras, how long it was live), never audio
SPEAKER_ID = re.compile(r"^[a-z0-9_]{1,32}$")   # backend speakers_api.ID_RX
TALK_INFO_MAX = 4096                            # a status message of the site's larger than this is relayed, not read

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding",
       "upgrade", "cookie", "authorization", "host", "content-length"}


def _site_and_role(request: Request, site_id: str) -> tuple[dict, dict, str]:
    u = auth.require_user(request)
    site, role = auth.site_access(u, site_id)
    return u, site, role


@router.api_route("/s/{site_id}/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
async def proxy_api(site_id: str, path: str, request: Request):
    auth.csrf_check(request)
    u, site, role = _site_and_role(request, site_id)
    api_path = "/api/" + path
    if api_path == "/api/hub":
        raise HTTPException(403, "the hub link is managed here, not through the site")
    if api_path.startswith(("/api/ai/", "/api/config/history", "/api/camweb/")) or api_path == "/api/config/handoff":
        # hub-internal only: the fleet AI relay (vlm_proxy), camera credential handoff and history copy (fleet_actions),
        # camera web pages (camweb.py: only on a camera's own name, with a session)
        raise HTTPException(404, "not available through the hub")
    needed = required_role(request.method, api_path)
    if not allows(role, needed):
        raise HTTPException(403, f"needs the {needed} role")
    conn = registry.get(site_id)
    if conn is None:
        raise HTTPException(503, "site offline")
    # a central recording instance: camera limit and address rules before the change reaches it (central_cameras.py)
    central = central_cameras.instance_for_server(site_id) if central_cameras.changes_cameras(request.method, api_path) else None
    reserved: list[str] = []
    kind = central_cameras.write_kind(request.method, api_path) if central else None
    body = await request.body() if request.method in ("POST", "PUT", "PATCH") else None
    if kind:
        from . import bridges   # a camera at a LAN address the Site's Axiom Bridge carries gets its relay address here
        body = bridges.bridge_fill(central, kind, api_path, body) or body
        try:
            reserved = await central_cameras.check(central, kind, api_path, body)
        except central_cameras.Refused as e:
            raise HTTPException(e.status, e.detail)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP and not k.lower().startswith("x-hub-")}
    headers.update(_identity(u, role, site, conn, request, request.url.scheme))
    t0 = time.time()
    try:
        s = await conn.request(request.method, api_path, request.url.query, headers, body)
    except TooManyStreams:
        raise HTTPException(429, "too many requests to this site at once")
    try:
        # a subnet scan (POST /api/cameras/scan) answers only when every address was probed: up to ~72 s for a /22; "Fix
        # clocks" (POST /api/cameras/device/time) when every chosen camera has answered or timed out
        slow = api_path.rstrip("/").endswith(("/api/cameras/scan", "/api/cameras/device/time")) and request.method == "POST"
        wait = settings.scan_timeout_s if slow else settings.first_byte_timeout_s
        await asyncio.wait_for(s.head.wait(), wait)
    except asyncio.TimeoutError:
        await conn.abort(s, "timeout")
        raise HTTPException(504, "the site did not answer in time")
    if s.aborted or s.status is None:
        conn.finish(s)
        raise HTTPException(503, f"site aborted: {s.aborted or 'no response'}")
    if central:
        if 200 <= s.status < 300:
            central_cameras.after_write(site_id)   # the firewall follows the instance's cameras
        else:
            central_cameras.release(site_id, reserved)
    # allow-listed headers only, never an active content type, always nosniff + a sandboxing CSP (security.py)
    status, resp_headers = s.status, security.filter_proxy_headers(s.headers, site_id)
    answer = bytearray() if TALKDOWN_PLAY.match(api_path) and request.method == "POST" else None
    described = _describe(api_path, body, conn) if request.method == "POST" else None

    async def body_iter():
        try:
            while True:
                if await request.is_disconnected():
                    await conn.abort(s, "client left")
                    return
                c = await s.read()
                if c is None:
                    return
                if answer is not None and len(answer) < TALKDOWN_ANSWER_MAX:
                    answer.extend(c[:TALKDOWN_ANSWER_MAX - len(answer)])
                yield c
                await conn.send({"t": "credit", "id": s.id, "bytes": len(c)})
        finally:
            conn.finish(s)
            offer = viewing.WHEP_OFFER.match(api_path) if request.method == "POST" else None
            if offer:
                viewing.offer(u, site, offer.group(1), request.url.path, status, request.client.host if request.client else None,
                              round((time.time() - t0) * 1000))
            elif request.method != "GET" and not (request.method == "DELETE" and WHEP_HANGUP.match(api_path)):
                _audit(u, site, request, status, time.time() - t0, _talkdown(answer, status, conn) if answer is not None else described)

    return StreamingResponse(body_iter(), status_code=status, headers=resp_headers, media_type=resp_headers.get("content-type"))


def _identity(u: dict, role: str, site: dict, conn, request, scheme: str) -> dict:
    """What the site is told about the request: the hub user and role, the server and its Site, where it came from."""
    return {"x-hub-user": u["email"], "x-hub-role": role, "x-hub-site": site["id"],
            "x-forwarded-for": request.client.host if request.client else "", "x-forwarded-host": request.headers.get("host", ""),
            "x-forwarded-proto": scheme,
            # the Site this server belongs to (cached on the connection; the agent passes headers as UTF-8)
            "x-hub-location": (conn.location or {}).get("id") or site.get("location_id") or "",
            "x-hub-location-name": (conn.location or {}).get("name") or ""}


def _talkdown(answer: bytearray, status: int, conn) -> dict | None:
    """{action, detail} for a talk-down play: which speaker, which message, its cameras and the Site (None = the site
    refused it or the answer can't be read; the plain "POST speakers/<id>/play" row is written then)."""
    if not 200 <= status < 300:
        return None
    try:
        a = json.loads(bytes(answer))
    except ValueError:
        return None
    if not isinstance(a, dict) or not a.get("speaker_id"):
        return None
    loc = getattr(conn, "location", None) or {}
    speaker, message = str(a.get("speaker") or a["speaker_id"])[:80], str(a.get("message") or "?")[:120]
    detail = {"speaker_id": str(a["speaker_id"])[:40], "speaker": speaker, "message": message,
              "message_id": a.get("message_id"), "camera_ids": [str(c)[:40] for c in (a.get("camera_ids") or [])][:64],
              "replaced": a.get("replaced"), "location_id": loc.get("id"), "location_name": loc.get("name")}
    if a.get("text"):
        detail["text"] = str(a["text"])[:TALKDOWN_TEXT_MAX]   # the exact words spoken (a typed text, Describe & warn)
    return {"action": f"talk-down: {message} on {speaker}", "detail": {"talkdown": detail}}


def _describe(api_path: str, body: bytes | None, conn) -> dict | None:
    """{action, detail} for a Describe & warn: the speaker and the camera looked at (None = not a describe)."""
    m = TALKDOWN_DESCRIBE.match(api_path)
    if not m:
        return None
    try:
        b = json.loads(body or b"{}")
    except ValueError:
        b = None
    camera = str(b.get("camera_id") or "")[:40] if isinstance(b, dict) else ""
    loc = getattr(conn, "location", None) or {}
    return {"action": "talk-down describe",
            "detail": {"talkdown_describe": {"speaker_id": m.group(1)[:40], "camera_id": camera or None,
                                             "location_id": loc.get("id"), "location_name": loc.get("name")}}}


def _audit(u: dict, site: dict, request: Request | WebSocket, status: int, dt: float, special: dict | None = None,
           method: str | None = None) -> None:
    method = method or request.method   # a WebSocket has none: the caller names it
    db.insert(db.audit_log, {"ts": time.time(), "user_id": u["id"], "user_email": u["email"], "org_id": site["org_id"], "site_id": site["id"],
                             "action": (special or {}).get("action") or f"{method} {request.url.path.split('/api/', 1)[-1]}",
                             "method": method, "path": request.url.path, "status": status,
                             "ip": request.client.host if request.client else None,
                             "detail": {"ms": round(dt * 1000), **((special or {}).get("detail") or {})}})


@router.websocket("/s/{site_id}/api/ws")
async def proxy_ws(site_id: str, ws: WebSocket):
    u = auth.current_user(ws)  # type: ignore[arg-type]  (Request-like: cookies + headers)
    if not u:
        await ws.close(code=4401)
        return
    try:
        auth.site_access(u, site_id)
    except HTTPException:
        await ws.close(code=4403)
        return
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue()
    conn = registry.get(site_id)
    if conn is not None:
        conn.subscribers.add(q)
    try:
        while True:
            msg = await q.get()
            if msg.get("type") == "site_offline":
                await ws.close(code=1012, reason="site offline")
                return
            await ws.send_json(msg)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        c = registry.get(site_id)
        if c is not None:
            c.subscribers.discard(q)


def _talk_audit(speaker_id: str, info: dict, live_s: float | None, conn) -> dict:
    """{action, detail} for a live talk: the speaker (as the site named it), its cameras, the message it replaced, how
    long it was live and how it ended, the Site."""
    loc = getattr(conn, "location", None) or {}
    speaker = str(info.get("speaker") or speaker_id)[:80]
    detail = {"speaker_id": speaker_id, "speaker": speaker, "message": "Live talk", "live": True,
              "camera_ids": [str(c)[:40] for c in (info.get("camera_ids") or []) if isinstance(c, (str, int))][:64],
              "replaced": str(info["replaced"])[:120] if info.get("replaced") else None,
              "duration_s": round(live_s, 1) if live_s is not None else None,
              "result": str(info.get("result") or ("refused" if info.get("refused") else "ended"))[:20],
              "location_id": loc.get("id"), "location_name": loc.get("name")}
    if info.get("error"):
        detail["error"] = str(info["error"])[:200]
    return {"action": f"talk-down: Live talk on {speaker}", "detail": {"talkdown": detail}}


async def _refuse(ws: WebSocket, code: int, error: str) -> None:
    """Refuse an opened talk socket the way the site does: a message the page can read (it never sees a close code
    of a socket refused before it opened), then the code."""
    try:
        await ws.send_json({"type": "talk", "state": "refused", "error": error})
        await ws.close(code=code)
    except (WebSocketDisconnect, RuntimeError):
        pass


@router.websocket("/s/{site_id}/api/speakers/{speaker_id}/talk")
async def proxy_talk(site_id: str, speaker_id: str, ws: WebSocket):
    """Hold to Talk through the hub: the browser's audio up to the site's /api/speakers/{id}/talk, its status messages
    down. Signed in, from the hub's own pages only (a WebSocket carries no CSRF protection of its own), then the same
    role a POST to the path needs. The talk is audited when it ends."""
    u = auth.current_user(ws)  # type: ignore[arg-type]  (Request-like: cookies + headers)
    origin = (ws.headers.get("origin") or "").rstrip("/")
    own = {settings.public_url.rstrip("/"), f"{'https' if ws.url.scheme == 'wss' else 'http'}://{ws.headers.get('host', '')}"}
    if not u or (origin and origin not in own):
        await ws.close(code=4401 if not u else 4403)
        return
    await ws.accept()
    api_path = f"/api/speakers/{speaker_id}/talk"
    try:
        site, role = auth.site_access(u, site_id)
    except HTTPException:
        await _refuse(ws, 4403, "no access to this server")
        return
    needed = required_role("POST", api_path)
    if not SPEAKER_ID.match(speaker_id):
        await _refuse(ws, 4404, "unknown speaker")
        return
    if not allows(role, needed):
        await _refuse(ws, 4403, f"needs the {needed} role")
        return
    conn = registry.get(site_id)
    if conn is None:
        await _refuse(ws, 4503, "the server is offline")
        return
    if "talk" not in conn.caps():
        await _refuse(ws, 4501, "this server's software is too old to talk live: update it")
        return
    try:
        s = await conn.open("GET", api_path, "", _identity(u, role, site, conn, ws, "https" if ws.url.scheme == "wss" else "http"),
                            body=True, ws=True)
    except TooManyStreams:
        await _refuse(ws, 4429, "too many requests to this server at once")
        return
    t0 = time.time()
    info: dict = {}
    live_at: list[float] = []
    try:
        try:
            await asyncio.wait_for(s.head.wait(), settings.first_byte_timeout_s)
        except asyncio.TimeoutError:
            await conn.abort(s, "timeout")
        if s.aborted or s.status != 101:
            info["refused"], info["error"] = True, "the server did not take the call"
            await _refuse(ws, 4502, info["error"])
            return
        await _talk_relay(ws, conn, s, info, live_at)
    finally:
        await conn.abort(s, "closed")
        conn.finish(s)
        live_s = (info.get("played_s") if isinstance(info.get("played_s"), (int, float)) else None) or \
            (time.time() - live_at[0] if live_at else None)
        try:
            _audit(u, site, ws, s.status or 502, time.time() - t0, _talk_audit(speaker_id, info, live_s, conn), method="WS")
        except Exception:   # the audit must not break the socket's teardown
            log.exception("live talk audit for %s/%s", site_id, speaker_id)


async def _talk_relay(ws: WebSocket, conn, s, info: dict, live_at: list[float]) -> None:
    """Both ways until either side closes: the browser's messages up the tunnel (credit-paced), the site's down; the
    site's small JSON status messages are read on the way for the audit."""
    async def up():
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect":
                return
            for piece in ws_pieces(m["bytes"] if m.get("bytes") is not None else (m.get("text") or "")):
                await s.take_credit(len(piece))
                if s.aborted:
                    return
                await conn.send_bytes(chunk(s.id, piece))

    async def down():
        asm = WsAssembler()
        while (c := await s.read()) is not None:
            await conn.send({"t": "credit", "id": s.id, "bytes": len(c)})
            got = asm.feed(c)
            if got is None:
                continue
            binary, payload = got
            if not binary and len(payload) <= TALK_INFO_MAX:
                _talk_info(payload, info, live_at)
            await (ws.send_bytes(payload) if binary else ws.send_text(payload.decode("utf-8", "replace")))

    tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError, ValueError):
                pass
            except Exception:
                log.exception("live talk socket")
        try:
            await ws.close()
        except RuntimeError:
            pass   # the browser already went


def _talk_info(payload: bytes, info: dict, live_at: list[float]) -> None:
    try:
        m = json.loads(payload)
    except ValueError:
        return
    if not isinstance(m, dict) or m.get("type") != "talk":
        return
    state = m.get("state")
    if state == "connecting":
        info.update({k: m.get(k) for k in ("speaker", "camera_ids", "replaced")})
    elif state == "live" and not live_at:
        live_at.append(time.time())
    elif state == "ended":
        info.update({k: m.get(k) for k in ("result", "error", "played_s")})
    elif state == "refused":
        info.update(refused=True, error=m.get("error"))


# ---- the site UI bundle under /s/{site_id}/

@router.get("/s/{site_id}")
async def site_root_redirect(site_id: str):
    return RedirectResponse(f"/s/{site_id}/", status_code=307)


@router.get("/s/{site_id}/{file_path:path}")
async def site_ui(site_id: str, file_path: str, request: Request):
    u = auth.current_user(request)
    if not u:
        return RedirectResponse(f"/login?next=/s/{site_id}/", status_code=307)
    auth.site_access(u, site_id)
    dist: Path = settings.site_ui_dir
    if file_path in ("sw.js",):
        return Response(status_code=404)  # the site's service worker must not run under the hub
    target = (dist / file_path).resolve() if file_path else None
    if target and file_path and target.is_file() and str(target).startswith(str(dist.resolve())):
        fresh = file_path.startswith("assets/")
        return FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable" if fresh else "no-cache"})
    return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})
