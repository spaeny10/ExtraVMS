"""Camera web pages through the hub (hub/hub/camweb.py): a camera's own admin page, opened from the hub without a
port forward.

The hub serves each camera's page at the root of its own name (https://<label>.cam.axiomvision.ai/) and sends every
request down this server's tunnel to /api/camweb/<camera id>, with the browser's path and query in `x-camweb-path`
and the query string. This route forwards it to the camera at its REGISTERED address only (cameras.host, or the
outside address in port-forward mode) on its web port (web_scheme / web_port; https without certificate checks:
camera certificates are self-signed) and streams the answer back. WebSockets are passed through the same way.

Tunnel-only, like /api/config/handoff: the request must come through the in-process hub bridge (hub_agent.is_tunnel)
and carry `x-hub-internal: camweb`; the hub's public /s/<site>/api proxy strips x-hub-* and refuses this path, and a
direct connection never carries the tunnel marker. Nothing here can reach another host: the path is only ever
appended to the camera's own origin (never `//host`), and redirects are passed to the browser, not followed.

What the answer carries for the hub: several Set-Cookie headers in one `x-camweb-cookies` (a JSON list: the tunnel
keeps one value per header name), other repeated headers joined with ", ", and a Location naming the camera's own
address made relative, so the browser stays on the camera's hub name. The hub rewrites the cookies (no Domain,
Secure) and adds its own headers. On the LAN none of this is needed: the server UI links to the camera directly.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import urllib.parse

import httpx
import websockets
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from . import hub_agent, onvif_soap
from .db import db

log = logging.getLogger("nvr.camweb")
router = APIRouter()

MAX_TARGET = 8192                 # longest path + query passed on
WS_MAX = 16 * 1024 * 1024         # largest WebSocket message either way
TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0)   # a firmware upload is processed for minutes
# never passed to the camera: hop-by-hop, the hub's own and the proxies' headers (the hub strips most of them already)
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding",
       "upgrade", "host", "forwarded", "via", "x-real-ip", "cdn-loop"}
DROP_PREFIX = ("x-hub-", "x-forwarded-", "x-camweb-", "sec-websocket-")
# never passed back: hop-by-hop, and the length (the body is re-streamed)
HOP_RESPONSE = {"connection", "keep-alive", "proxy-authenticate", "proxy-connection", "te", "trailer", "transfer-encoding",
                "upgrade", "content-length"}

_client: httpx.AsyncClient | None = None
_transport: httpx.AsyncBaseTransport | None = None   # tests stand a fake camera in here


def client() -> httpx.AsyncClient:
    """One client for every camera: no certificate checks (cameras' https uses self-signed certificates), no
    redirects followed, no proxy from the environment."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(verify=False, timeout=TIMEOUT, follow_redirects=False, trust_env=False, transport=_transport)
    return _client


def _insecure_ssl() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def web_address(cam: dict) -> tuple[str, str, int]:
    """(scheme, host, port) of the camera's web page as this server reaches it. Port-forward mode (an outside address,
    or a camera added by its public address): the outside web port, else the outside ONVIF port when the web page is on
    the ONVIF port (cameras serve both on 80: the same forward), else the web port itself."""
    scheme = "https" if cam.get("web_scheme") == "https" else "http"
    port = int(cam.get("web_port") or (443 if scheme == "https" else 80))
    host = onvif_soap.outside(cam)[0]
    if cam.get("public_host") or onvif_soap.auto_forward(cam):
        if cam.get("public_web_port"):
            port = int(cam["public_web_port"])
        elif cam.get("public_onvif_port") and port == int(cam.get("onvif_port") or 80):
            port = int(cam["public_onvif_port"])
    return scheme, host, port


def _hostport(scheme: str, host: str, port: int) -> str:
    h = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return h if port == (443 if scheme in ("https", "wss") else 80) else f"{h}:{port}"


def _target(raw: str | None, query: str) -> str:
    """The path and query the camera is asked for: an absolute path (one leading /, so it can never read as another
    host), printable ASCII only."""
    path = raw or "/"
    target = path + ("?" + query if query else "")
    if not path.startswith("/") or len(target) > MAX_TARGET or "#" in target or any(not 0x20 < ord(ch) < 0x7f for ch in target):
        raise HTTPException(400, "bad camera path")
    return "/" + target.lstrip("/")


def _camera(camera_id: str) -> dict:
    cam = db.one("SELECT * FROM cameras WHERE id=?", [camera_id])
    if not cam:
        raise HTTPException(404, "unknown camera")
    return cam


def _check(scope, headers) -> None:
    if not hub_agent.is_tunnel(scope) or headers.get("x-hub-internal") != "camweb":
        raise HTTPException(403, "camera web pages are only opened through the fleet hub")


def _forward_headers(headers, scheme: str, hostport: str, session_origin: str | None) -> list[tuple[str, str]]:
    """The browser's headers for the camera: its own cookies and login (Authorization) pass, Origin and a Referer from
    the hub name are pointed at the camera's own origin (cameras compare them with their address), Host is the camera's."""
    origin = f"{scheme}://{hostport}"
    out = []
    for k, v in headers.items():
        lk = k.lower()
        if lk in HOP or lk.startswith(DROP_PREFIX):
            continue
        if lk == "origin":
            v = origin
        elif lk == "referer":
            if session_origin and (v == session_origin or v.startswith(session_origin + "/")):
                v = origin + v[len(session_origin):]
            else:
                continue
        out.append((lk, v))
    out.append(("host", hostport))
    return out


def _relative_location(value: str, names: set[str]) -> str:
    """A redirect to the camera's own address (http://192.168.1.10/doc/page/login.asp) as a path on the hub name."""
    try:
        u = urllib.parse.urlsplit(value.strip())
    except ValueError:
        return value
    if u.scheme.lower() in ("http", "https") and (u.hostname or "").lower() in names:
        return urllib.parse.urlunsplit(("", "", u.path or "/", u.query, u.fragment))
    return value


def response_headers(items, names: set[str]) -> dict[str, str]:
    """The camera's response headers as the tunnel carries them (one value per name; see the module docstring)."""
    out: dict[str, str] = {}
    cookies: list[str] = []
    for k, v in items:
        lk = k.lower()
        if lk in HOP_RESPONSE or lk.startswith("x-camweb-"):
            continue
        if lk == "set-cookie":
            cookies.append(v)
            continue
        if lk == "location":
            v = _relative_location(v, names)
        out[lk] = f"{out[lk]}, {v}" if lk in out else v
    if cookies:
        out["x-camweb-cookies"] = json.dumps(cookies)
    return out


def _names(cam: dict, host: str) -> set[str]:
    """Every address the camera may name itself by in a redirect."""
    return {n.lower().strip("[]") for n in (cam.get("host"), cam.get("public_host"), host) if n}


def _failed(status: int, detail: str) -> Response:
    # x-camweb-error: the hub shows its own page ("the camera did not answer") instead of passing this on
    return Response(detail, status_code=status, media_type="text/plain", headers={"x-camweb-error": "1"})


@router.api_route("/api/camweb/{camera_id}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def camweb(camera_id: str, request: Request):
    _check(request.scope, request.headers)
    cam = _camera(camera_id)
    scheme, host, port = web_address(cam)
    target = _target(request.headers.get("x-camweb-path"), request.url.query)
    hostport = _hostport(scheme, host, port)
    try:
        url = httpx.URL(scheme=scheme, host=host.strip("[]"), port=port, raw_path=target.encode("ascii"))
    except (httpx.InvalidURL, ValueError, TypeError):
        raise HTTPException(400, "bad camera address")
    if url.host.lower() != host.strip("[]").lower():   # never anywhere but the camera
        raise HTTPException(400, "bad camera address")
    headers = _forward_headers(request.headers, scheme, hostport, request.headers.get("x-camweb-host"))
    has_body = request.method in ("POST", "PUT", "PATCH") or int(request.headers.get("content-length") or 0) > 0
    req = client().build_request(request.method, url, headers=headers, content=request.stream() if has_body else None)
    try:
        resp = await client().send(req, stream=True)
    except httpx.TimeoutException:
        return _failed(504, "the camera did not answer in time")
    except httpx.HTTPError as e:
        log.info("camera web page %s (%s): %s", camera_id, hostport, type(e).__name__)
        return _failed(502, f"the camera did not answer ({type(e).__name__})")
    out = response_headers(resp.headers.multi_items(), _names(cam, host))
    return StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, headers=out, background=BackgroundTask(resp.aclose))


async def _ws_connect(uri: str, **kw):
    """websockets.connect (tests stand a fake camera in here)."""
    return await websockets.connect(uri, **kw)


@router.websocket("/api/camweb/{camera_id}")
async def camweb_ws(ws: WebSocket, camera_id: str):
    """A WebSocket of the camera's page, passed through to ws(s)://<the camera's web address>."""
    try:
        _check(ws.scope, ws.headers)
        cam = _camera(camera_id)
        scheme, host, port = web_address(cam)
        target = _target(ws.headers.get("x-camweb-path"), ws.scope.get("query_string", b"").decode("latin-1"))
    except HTTPException as e:
        await ws.close(code=4403 if e.status_code == 403 else 4400)
        return
    wscheme = "wss" if scheme == "https" else "ws"
    hostport = _hostport(wscheme, host, port)
    uri = f"{wscheme}://{hostport}{target}"
    if (urllib.parse.urlsplit(uri).hostname or "").lower() != host.strip("[]").lower():
        await ws.close(code=4400)
        return
    headers = [(k, v) for k, v in _forward_headers(ws.headers, scheme, _hostport(scheme, host, port), ws.headers.get("x-camweb-host"))
               if k in ("cookie", "authorization", "user-agent", "origin", "accept-language")]
    kw = {"ssl": _insecure_ssl()} if wscheme == "wss" else {}
    try:
        upstream = await _ws_connect(uri, subprotocols=ws.scope.get("subprotocols") or None, additional_headers=headers,
                                     open_timeout=10, max_size=WS_MAX, ping_interval=None, user_agent_header=None, proxy=None, **kw)
    except Exception as e:
        log.info("camera web socket %s (%s): %s", camera_id, hostport, type(e).__name__)
        await ws.close(code=4502)
        return
    await ws.accept(subprotocol=upstream.subprotocol)

    async def to_camera():
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect":
                return
            if m.get("bytes") is not None:
                await upstream.send(m["bytes"])
            elif m.get("text") is not None:
                await upstream.send(m["text"])

    async def to_browser():
        async for msg in upstream:
            if isinstance(msg, (bytes, bytearray)):
                await ws.send_bytes(bytes(msg))
            else:
                await ws.send_text(msg)

    tasks = [asyncio.create_task(to_camera()), asyncio.create_task(to_browser())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, WebSocketDisconnect, websockets.ConnectionClosed, RuntimeError):
                pass
            except Exception:
                log.exception("camera web socket %s", camera_id)
        await upstream.close()
        try:
            await ws.close()
        except RuntimeError:
            pass   # already closed
