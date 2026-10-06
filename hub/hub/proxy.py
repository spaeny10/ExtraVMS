"""/s/{site_id}/... : the site's own UI and API, reached through its tunnel.

Static files come from the site UI bundle the hub ships (frontend/dist), byte-identical to what the site
serves itself; API and WebSocket requests go down the tunnel with the hub user's identity attached.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse

from . import auth, db, security
from .agents import TooManyStreams, registry
from .config import settings
from .roles import allows, required_role

log = logging.getLogger("hub.proxy")
router = APIRouter()

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
    if api_path.startswith(("/api/ai/", "/api/config/history")) or api_path == "/api/config/handoff":
        # hub-internal only: the fleet AI relay (vlm_proxy), camera credential handoff and history copy (fleet_actions)
        raise HTTPException(404, "not available through the hub")
    needed = required_role(request.method, api_path)
    if not allows(role, needed):
        raise HTTPException(403, f"needs the {needed} role")
    conn = registry.get(site_id)
    if conn is None:
        raise HTTPException(503, "site offline")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP and not k.lower().startswith("x-hub-")}
    headers.update({"x-hub-user": u["email"], "x-hub-role": role, "x-hub-site": site_id,
                    "x-forwarded-for": request.client.host if request.client else "", "x-forwarded-host": request.headers.get("host", ""),
                    "x-forwarded-proto": request.url.scheme,
                    # the Site this server belongs to (cached on the connection; the agent passes headers as UTF-8)
                    "x-hub-location": (conn.location or {}).get("id") or site.get("location_id") or "",
                    "x-hub-location-name": (conn.location or {}).get("name") or ""})
    body = await request.body() if request.method in ("POST", "PUT", "PATCH") else None
    t0 = time.time()
    try:
        s = await conn.request(request.method, api_path, request.url.query, headers, body)
    except TooManyStreams:
        raise HTTPException(429, "too many requests to this site at once")
    try:
        # a subnet scan (POST /api/cameras/scan) answers only when every address was probed: up to ~72 s for a /22
        wait = settings.scan_timeout_s if api_path.rstrip("/").endswith("/api/cameras/scan") else settings.first_byte_timeout_s
        await asyncio.wait_for(s.head.wait(), wait)
    except asyncio.TimeoutError:
        await conn.abort(s, "timeout")
        raise HTTPException(504, "the site did not answer in time")
    if s.aborted or s.status is None:
        conn.finish(s)
        raise HTTPException(503, f"site aborted: {s.aborted or 'no response'}")
    # allow-listed headers only, never an active content type, always nosniff + a sandboxing CSP (security.py)
    status, resp_headers = s.status, security.filter_proxy_headers(s.headers, site_id)

    async def body_iter():
        try:
            while True:
                if await request.is_disconnected():
                    await conn.abort(s, "client left")
                    return
                c = await s.read()
                if c is None:
                    return
                yield c
                await conn.send({"t": "credit", "id": s.id, "bytes": len(c)})
        finally:
            conn.finish(s)
            if request.method != "GET":
                _audit(u, site, request, status, time.time() - t0)

    return StreamingResponse(body_iter(), status_code=status, headers=resp_headers, media_type=resp_headers.get("content-type"))


def _audit(u: dict, site: dict, request: Request, status: int, dt: float) -> None:
    db.insert(db.audit_log, {"ts": time.time(), "user_id": u["id"], "user_email": u["email"], "org_id": site["org_id"], "site_id": site["id"],
                             "action": f"{request.method} {request.url.path.split('/api/', 1)[-1]}", "method": request.method,
                             "path": request.url.path, "status": status, "ip": request.client.host if request.client else None,
                             "detail": {"ms": round(dt * 1000)}})


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
