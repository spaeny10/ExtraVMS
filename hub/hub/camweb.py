"""Camera web pages: a camera's own admin page (the settings only its page has) opened from the hub, without a port
forward.

Names. A camera page works unmodified only at the root of its own origin (absolute links, its own cookies, sometimes
WebSockets), so each camera gets a host name: https://<label>.<camweb domain>/ (cam.axiomvision.ai, a wildcard DNS
record to the hub). The label is "c" + base32(sha256(server/camera + salt))[:12]: stable, so Caddy issues at most one
certificate per camera (Let's Encrypt: 50 new certificates per registered domain per week), and not predictable across
installs (the salt). It is not a secret: Caddy asks GET /api/camweb/tls-ok whether a name is a camera before it gets a
certificate for it.

Access is a session, not the name. POST /api/servers/{server}/cameras/{camera}/web-session (hub administrators, and
admins or owners of the camera's Site through a real membership: not operators, viewers or SOC staff unless they are
that) answers a link with a one-time key (single use, camweb_key_ttl_s): https://<label>.../__axiom/enter?k=<key>.
Entering it sets the camera host's cookie (`__Host-axcam` on https: HttpOnly, Secure, SameSite=Lax, Path=/, no Domain)
holding a fresh session token and redirects to /. Every other request on that name needs a live session for THAT
camera (camweb_idle_s without a request ends it, camweb_max_s after opening at the latest; permissions re-checked every
few minutes); otherwise a small 403 page. /__axiom/close ends it. Nothing is ever injected into the camera's pages.

Routing. CamwebMiddleware is the outermost middleware: a request whose Host is a camera name is handled here (every
path, method and WebSocket) and never reaches the hub's routes, pages or security headers. The other way round, a
request to the hub whose Origin is a camera name is refused: a camera page's script must not drive the hub (the hub's
cookie is host-only on the hub's own name, so it is never sent to camera names, but camera names are "same-site" with
it). Requests go down the server's tunnel to /api/camweb/<camera> (backend/nvr/camweb.py: the camera at its registered
address only), bodies streamed both ways without buffering; WebSockets ride the tunnel as "ws" streams
(agents.ws_pieces).

Responses: the camera's cookies lose any Domain (host-only on its own name) and gain Secure, and may not take our
cookie names; hop-by-hop, CORS and HSTS headers of the camera are dropped; a Location naming the camera's own address
arrives relative (the server rewrites it). State-changing requests and WebSockets must come from the camera name's own
origin (another camera's page is "same-site" too). Audit: opened, entered, closed, expired (who, server, camera, Site,
IP, how long), in the customer's audit log.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import re
import secrets
import time
from urllib.parse import parse_qs, quote, urlsplit

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Request
from starlette.datastructures import Headers
from starlette.websockets import WebSocket, WebSocketDisconnect

from tunnelproto import chunk

from . import auth, db
from .agents import TooManyStreams, WsAssembler, registry, ws_pieces
from .config import settings
from .roles import allows

log = logging.getLogger("hub.camweb")
router = APIRouter()

LABEL = re.compile(r"^c[a-z2-7]{12}$")
ENTER, CLOSE = "/__axiom/enter", "/__axiom/close"
SALT_KEY = "camweb:salt"
RECHECK_S = 300                 # a session's user is re-checked (still a Site admin?) this often
MAX_OPEN_PER_USER = 20
# cookie names a camera may never set or receive: ours on the camera's name, and the hub's own
OUR_COOKIES = frozenset(n.lower() for n in ("axcam", "__Host-axcam", auth.COOKIE, "__Host-" + auth.COOKIE))
DROP_REQUEST = {"connection", "keep-alive", "proxy-authorization", "proxy-connection", "te", "trailer", "transfer-encoding",
                "upgrade", "host", "cookie", "forwarded", "via", "x-real-ip", "cdn-loop",
                "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions", "sec-websocket-accept"}
DROP_REQUEST_PREFIX = ("x-hub-", "x-forwarded-", "x-camweb-")
DROP_RESPONSE = {"connection", "keep-alive", "proxy-authenticate", "proxy-connection", "te", "trailer", "transfer-encoding",
                 "upgrade", "content-length", "strict-transport-security", "alt-svc", "public-key-pins",
                 "public-key-pins-report-only", "set-cookie", "x-camweb-cookies", "x-camweb-error"}
PAGE_CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"

_salt: str | None = None
_labels: dict[str, tuple[str, str]] = {}      # label -> (server, camera), for tls-ok
_labels_at = 0.0
_checked: dict[str, float] = {}               # session id -> last permission check
_slots: dict[str, asyncio.Semaphore] = {}     # server id -> camera page requests at once
_sockets: dict[str, int] = {}                 # server id -> camera page WebSockets open


# ---------------------------------------------------------------- names

def domain() -> str:
    """cam.axiomvision.ai: HUB_CAMWEB_DOMAIN, else "cam." + the public URL's parent domain (hub.axiomvision.ai ->
    cam.axiomvision.ai; a one-label dev host such as localhost -> cam.localhost)."""
    d = (settings.camweb_domain or "").strip().strip(".").lower()
    if d:
        return d
    host = (urlsplit(settings.public_url).hostname or "localhost").lower()
    return "cam." + (host.split(".", 1)[1] if host.count(".") >= 2 else host)


def _salt_value() -> str:
    global _salt
    if settings.camweb_salt:
        return settings.camweb_salt
    if _salt is None:
        row = db.one(sa.select(db.kv).where(db.kv.c.key == SALT_KEY))
        if row is None:
            try:
                db.insert(db.kv, {"key": SALT_KEY, "value": {"salt": secrets.token_hex(16), "at": time.time()}})
            except sa.exc.IntegrityError:
                pass   # another process made it first: use theirs
            row = db.one(sa.select(db.kv).where(db.kv.c.key == SALT_KEY))
        _salt = str(row["value"]["salt"])
    return _salt


def label_for(server_id: str, camera_id: str) -> str:
    h = hashlib.sha256(f"{server_id}/{camera_id}{_salt_value()}".encode()).digest()
    label = "c" + base64.b32encode(h).decode().lower()[:12]
    _labels[label] = (server_id, camera_id)
    return label


def origin_for(label: str) -> str:
    """https://<label>.cam.axiomvision.ai (the public URL's scheme, and its port on a dev hub)."""
    u = urlsplit(settings.public_url)
    return f"{u.scheme or 'https'}://{label}.{domain()}{f':{u.port}' if u.port else ''}"


def label_of(host: str) -> str | None:
    """For a Host header (or a bare name): its label when it is a camera name ("" when it is under the camera domain
    but no valid label), None for any other host."""
    name = (host or "").strip().lower()
    if not name.startswith("["):
        name = name.rsplit(":", 1)[0] if name.count(":") == 1 else name
    name = name.rstrip(".")
    d = domain()
    if name == d:
        return ""
    if not name.endswith("." + d):
        return None
    lab = name[: -len(d) - 1]
    return lab if LABEL.match(lab) else ""


def camera_for_label(label: str) -> tuple[str, str] | None:
    """(server, camera) whose name this is: an existing camera of a server that isn't retired."""
    global _labels_at
    hit = _labels.get(label)
    if hit is None and time.time() - _labels_at > 60:   # at most once a minute, however many unknown names are asked
        _labels_at = time.time()
        q = sa.select(db.cameras.c.server_id, db.cameras.c.camera_id).join(db.sites, db.sites.c.id == db.cameras.c.server_id) \
            .where(db.sites.c.retired_at.is_(None))
        for r in db.rows(q):
            label_for(r["server_id"], r["camera_id"])
        hit = _labels.get(label)
    if hit is None:
        return None
    ok = db.one(sa.select(db.cameras.c.camera_id).join(db.sites, db.sites.c.id == db.cameras.c.server_id)
                .where(db.cameras.c.server_id == hit[0], db.cameras.c.camera_id == hit[1], db.sites.c.retired_at.is_(None)))
    return hit if ok else None


def cookie_name() -> str:
    return "__Host-axcam" if settings.cookie_secure else "axcam"


# ---------------------------------------------------------------- who may open a camera's page

def require_may_open(u: dict, server_id: str, camera_id: str) -> tuple[dict, dict]:
    """(server, camera) or 403/404: hub administrators, and admins / owners of the camera's Site by a real membership
    (SOC staff are widened to a customer's Sites for monitoring, which doesn't count here)."""
    server, cam, _ = auth.camera_access(u, server_id, camera_id)
    if u.get("is_super"):
        return server, cam
    row = auth.real_membership(u, server["org_id"])
    if not row or not allows(row["role"], "admin"):
        raise HTTPException(403, "only the Site's admins open camera web pages")
    if row.get("all_sites") is False and server.get("location_id") not in auth.granted_location_ids(u["id"], server["org_id"]):
        raise HTTPException(403, "only the Site's admins open camera web pages")
    return server, cam


def _still_allowed(row: dict) -> bool:
    u = auth.user_by_id(row["user_id"])
    if not u:
        return False
    try:
        require_may_open(u, row["server_id"], row["camera_id"])
    except HTTPException:
        return False
    return True


# ---------------------------------------------------------------- sessions

def _deadline(row: dict, now: float) -> float:
    return min(now + settings.camweb_idle_s, row["created_at"] + settings.camweb_max_s)


def _expired(row: dict, now: float) -> bool:
    return now >= row["expires_at"] or now >= row["created_at"] + settings.camweb_max_s


def _names(row: dict) -> dict:
    srv = db.one(sa.select(db.sites.c.name).where(db.sites.c.id == row["server_id"])) or {}
    cam = db.one(sa.select(db.cameras.c.name).where(db.cameras.c.server_id == row["server_id"], db.cameras.c.camera_id == row["camera_id"])) or {}
    loc = db.one(sa.select(db.locations.c.name).where(db.locations.c.id == row["location_id"])) if row.get("location_id") else None
    return {"server_name": srv.get("name"), "camera_name": cam.get("name") or row["camera_id"], "location_name": (loc or {}).get("name")}


def _audit(event: str, row: dict, ip: str | None, extra: dict | None = None, actor: dict | None = None, now: float | None = None) -> None:
    n = _names(row)
    detail = {"location_id": row.get("location_id"),   # Site-restricted members see the rows of their Sites (auth.scope_filter)
              "camweb": {"session_id": row["id"], "server_id": row["server_id"], "server_name": n["server_name"],
                         "camera_id": row["camera_id"], "camera_name": n["camera_name"], "location_name": n["location_name"],
                         "host": f"{row['label']}.{domain()}", **(extra or {})}}
    if actor and actor.get("id") != row["user_id"]:
        detail["camweb"]["session_user"] = row.get("user_email")
    db.insert(db.audit_log, {"ts": now or time.time(), "user_id": (actor or {}).get("id") or row["user_id"],
                             "user_email": (actor or {}).get("email") or row.get("user_email"), "org_id": row["org_id"],
                             "site_id": row["server_id"], "action": f"camera web page {event}: {n['camera_name']}"[:200],
                             "method": None, "path": None, "status": None, "ip": ip, "detail": detail})


def end(row: dict, reason: str, ip: str | None = None, actor: dict | None = None, now: float | None = None) -> bool:
    """Close a session once (closed | replaced | revoked | expired); audited as closed or expired, with how long it ran."""
    now = now or time.time()
    with db.engine().begin() as c:
        n = c.execute(sa.update(db.cam_web_sessions).where(db.cam_web_sessions.c.id == row["id"], db.cam_web_sessions.c.closed_at.is_(None))
                      .values(closed_at=now, close_reason=reason, key_hash=None)).rowcount
    if n != 1:
        return False
    _checked.pop(row["id"], None)
    began = row.get("entered_at") or row["created_at"]
    end_ts = min(now, row["expires_at"]) if reason == "expired" else now
    _audit("expired" if reason == "expired" else "closed", row, ip,
           {"reason": reason, "entered": bool(row.get("entered_at")), "duration_s": max(0, round(end_ts - began))}, actor=actor, now=now)
    return True


def sweep(now: float | None = None) -> int:
    """Expire sessions past their idle or hard limit (the sweeper, every 30 s); forget ended ones after a week."""
    now = now or time.time()
    t = db.cam_web_sessions
    stale = db.rows(sa.select(t).where(t.c.closed_at.is_(None), sa.or_(t.c.expires_at <= now, t.c.created_at <= now - settings.camweb_max_s)))
    for row in stale:
        end(row, "expired", now=now)
    db.run(sa.delete(t).where(t.c.closed_at.is_not(None), t.c.closed_at < now - 7 * 86400))
    return len(stale)


def _session(token: str | None, label: str, now: float) -> tuple[dict | None, str]:
    """(session, "") for a live session of this camera name, else (None, why)."""
    if not token or len(token) > 128:
        return None, "none"
    return _session_by_hash(db.token_hash(token), label, now)


def _session_by_hash(token_hash: str | None, label: str, now: float) -> tuple[dict | None, str]:
    row = db.one(sa.select(db.cam_web_sessions).where(db.cam_web_sessions.c.token_hash == token_hash)) if token_hash else None
    if row is None or row["label"] != label:
        return None, "none"
    if row["closed_at"]:
        return None, "closed"
    if _expired(row, now):
        end(row, "expired", now=now)
        return None, "expired"
    if now - _checked.get(row["id"], 0) > RECHECK_S:
        if not _still_allowed(row):
            end(row, "revoked", now=now)
            return None, "revoked"
        _checked[row["id"]] = now
    if row["expires_at"] - now < settings.camweb_idle_s - 60:   # extended by use, written at most once a minute
        row["expires_at"] = _deadline(row, now)
        db.run(sa.update(db.cam_web_sessions).where(db.cam_web_sessions.c.id == row["id"]).values(expires_at=row["expires_at"], last_seen_at=now))
    return row, ""


def _public(row: dict) -> dict:
    n = _names(row)
    return {"id": row["id"], "server_id": row["server_id"], "camera_id": row["camera_id"], "location_id": row["location_id"],
            "host": f"{row['label']}.{domain()}", "created_at": row["created_at"], "entered_at": row["entered_at"],
            "last_seen_at": row["last_seen_at"], "expires_at": min(row["expires_at"], row["created_at"] + settings.camweb_max_s), **n}


# ---------------------------------------------------------------- hub API

def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/api/servers/{server_id}/cameras/{camera_id}/web-session")
@router.post("/api/sites/{server_id}/cameras/{camera_id}/web-session")
async def open_session(server_id: str, camera_id: str, request: Request):
    """Open a camera's own web page: a session and the one-time link the browser opens in a new tab."""
    u = auth.require_user(request)
    server, cam = require_may_open(u, server_id, camera_id)
    if server.get("retired_at"):
        raise HTTPException(409, "the server is retired")
    if cam.get("missing_since"):
        raise HTTPException(404, "that camera is no longer on its server")
    conn = registry.get(server_id)
    if conn is None:
        raise HTTPException(503, "the server is offline")
    if "camweb" not in conn.caps():
        raise HTTPException(409, "this server's software is too old to open camera web pages: update it first")
    now = time.time()
    t = db.cam_web_sessions
    mine = db.rows(sa.select(t).where(t.c.user_id == u["id"], t.c.closed_at.is_(None)))
    for old in [r for r in mine if r["server_id"] == server_id and r["camera_id"] == camera_id]:
        end(old, "replaced", _ip(request), now=now)   # the new link's cookie replaces the old one in the browser anyway
    if sum(1 for r in mine if not (r["server_id"] == server_id and r["camera_id"] == camera_id)) >= MAX_OPEN_PER_USER:
        raise HTTPException(429, "too many camera pages open: close some first")
    key = db.new_token()
    label = label_for(server_id, camera_id)
    row = {"id": db.new_id("cw_"), "token_hash": None, "key_hash": db.token_hash(key), "key_expires_at": now + settings.camweb_key_ttl_s,
           "server_id": server_id, "camera_id": camera_id, "location_id": server.get("location_id"), "org_id": server["org_id"],
           "user_id": u["id"], "user_email": u["email"], "label": label, "created_at": now, "expires_at": now + settings.camweb_idle_s,
           "entered_at": None, "last_seen_at": None, "closed_at": None, "close_reason": None, "ip": _ip(request)}
    db.insert(t, row)
    _audit("opened", row, _ip(request), now=now)
    return {"id": row["id"], "url": f"{origin_for(label)}{ENTER}?k={key}", "host": f"{label}.{domain()}",
            "expires_at": row["expires_at"], "server_id": server_id, "camera_id": camera_id, "camera_name": cam.get("name") or camera_id}


@router.get("/api/camweb/sessions")
async def my_sessions(request: Request):
    """The signed-in user's open camera pages, newest first."""
    u = auth.require_user(request)
    now = time.time()
    t = db.cam_web_sessions
    rows = db.rows(sa.select(t).where(t.c.user_id == u["id"], t.c.closed_at.is_(None)).order_by(t.c.created_at.desc()))
    return [_public(r) for r in rows if not _expired(r, now)]


@router.post("/api/camweb/sessions/{session_id}/close")
async def close_session(session_id: str, request: Request):
    u = auth.require_user(request)
    row = db.one(sa.select(db.cam_web_sessions).where(db.cam_web_sessions.c.id == session_id))
    if row is None or (row["user_id"] != u["id"] and not u.get("is_super")):
        raise HTTPException(404, "no such camera page")
    end(row, "closed", _ip(request), actor=u)
    return {"ok": True}


@router.get("/api/camweb/tls-ok")
async def tls_ok(domain: str = ""):
    """Caddy's on-demand TLS `ask`: 200 only for the name of an existing camera, so certificates are issued for
    nothing else (an unknown name gets no certificate and no Let's Encrypt request)."""
    lab = label_of(domain)
    if not lab or camera_for_label(lab) is None:
        raise HTTPException(404, "not a camera name")
    return {"ok": True}


# ---------------------------------------------------------------- the camera's name: every request

def _header(scope, name: bytes) -> str:
    for k, v in scope.get("headers") or []:
        if k == name:
            return v.decode("latin-1")
    return ""


def _own_origin(scope) -> str:
    scheme = "https" if scope.get("scheme") in ("https", "wss") else "http"
    return f"{scheme}://{_header(scope, b'host').lower()}"


def _client_ip(scope) -> str | None:
    c = scope.get("client")
    return c[0] if c else None


def _cookie(headers: Headers, name: str) -> str | None:
    for raw in headers.getlist("cookie"):
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
    return None


def _camera_cookies(headers: Headers) -> str:
    """The browser's cookies for the camera: everything but ours (the session token stays at the hub)."""
    keep = []
    for raw in headers.getlist("cookie"):
        for part in raw.split(";"):
            p = part.strip()
            if p and p.partition("=")[0].strip().lower() not in OUR_COOKIES:
                keep.append(p)
    return "; ".join(keep)


def forward_headers(headers: Headers, row: dict, own_origin: str) -> dict[str, str]:
    """The browser's headers for the camera: its own login (Authorization) and cookies pass; ours, the proxies' and
    anything x-hub-* don't; the server is told who is asking and the name it is served under (for Referer)."""
    out: dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk in DROP_REQUEST or lk.startswith(DROP_REQUEST_PREFIX):
            continue
        out[lk] = f"{out[lk]}, {v}" if lk in out else v
    cookie = _camera_cookies(headers)
    if cookie:
        out["cookie"] = cookie
    out.update({"x-hub-internal": "camweb", "x-hub-user": row.get("user_email") or "", "x-hub-site": row["server_id"],
                "x-camweb-host": own_origin})
    return out


def rewrite_set_cookie(value: str, secure: bool) -> str | None:
    """A camera's Set-Cookie for its hub name: no Domain (host-only: never shared with the hub or other cameras), Secure
    on https; None for our own cookie names or anything malformed."""
    if any(ch in value for ch in "\r\n\0"):
        return None
    parts = [p.strip() for p in value.split(";")]
    name, eq, _ = parts[0].partition("=")
    if not eq or not name.strip() or name.strip().lower() in OUR_COOKIES:
        return None
    attrs = [p for p in parts[1:] if p and p.partition("=")[0].strip().lower() != "domain"]
    if secure and not any(p.lower() == "secure" for p in attrs):
        attrs.append("Secure")
    return "; ".join([parts[0], *attrs])


def response_headers(h: dict, secure: bool) -> list[tuple[bytes, bytes]]:
    """The camera's answer headers as the tunnel delivered them (lower-case, one value per name; cookies as the
    server's x-camweb-cookies list), filtered and rewritten for the browser."""
    out: list[tuple[str, str]] = []
    for k, v in h.items():
        lk = str(k).lower()
        v = str(v)
        if lk in DROP_RESPONSE or lk.startswith("access-control-") or any(ch in v for ch in "\r\n\0"):
            continue
        out.append((lk, v))
    cookies: list[str] = []
    try:
        listed = json.loads(h.get("x-camweb-cookies") or "[]")
        cookies = [c for c in listed if isinstance(c, str)] if isinstance(listed, list) else []
    except ValueError:
        pass
    if h.get("set-cookie"):
        cookies.append(str(h["set-cookie"]))
    for c in cookies[:50]:
        r = rewrite_set_cookie(c, secure)
        if r:
            out.append(("set-cookie", r))
    have = {k for k, _ in out}
    if "x-frame-options" not in have:
        out.append(("x-frame-options", "SAMEORIGIN"))
    if "referrer-policy" not in have:
        out.append(("referrer-policy", "same-origin"))
    out.append(("x-robots-tag", "noindex, nofollow"))
    if secure:
        out.append(("strict-transport-security", "max-age=31536000"))
    return [(k.encode("latin-1"), v.encode("utf-8")) for k, v in out]


async def _page(send, status: int, title: str, text: str, headers: list[tuple[bytes, bytes]] | None = None) -> None:
    """A small page of our own on the camera's name (no script)."""
    hub = html.escape(settings.public_url.rstrip("/") + "/")
    body = (f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>body{{font:16px/1.5 system-ui,sans-serif;margin:0;display:grid;place-items:center;"
            "min-height:100vh;background:#f5f6f8;color:#1d2330}main{max-width:28rem;padding:1.5rem;margin:1rem;background:#fff;border-radius:12px;"
            "box-shadow:0 1px 4px #0002}h1{font-size:1.2rem;margin:0 0 .5rem}a{color:#2563eb}@media(prefers-color-scheme:dark){body{background:#12151c;"
            "color:#e6e8ee}main{background:#1c212b}a{color:#7aa7ff}}</style>"
            f"<main><h1>{html.escape(title)}</h1><p>{html.escape(text)}</p><p><a href='{hub}'>Open the hub</a></p></main></html>").encode()
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"text/html; charset=utf-8"), (b"cache-control", b"no-store"), (b"content-security-policy", PAGE_CSP.encode()),
        (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY"),
        (b"x-robots-tag", b"noindex, nofollow"), *(headers or [])]})
    await send({"type": "http.response.body", "body": body})


EXPIRED_TITLE = "Session expired — open it again from the hub"
WHY = {"closed": "This camera page was closed.", "expired": "Camera pages close after 30 minutes without use, and 2 hours after opening.",
       "revoked": "You no longer manage this camera's site.", "none": "Open the camera's web page from the hub: Site › Settings › Cameras."}


def _session_cookie(token: str | None) -> bytes:
    attrs = "; Path=/; HttpOnly; SameSite=Lax" + ("; Secure" if settings.cookie_secure else "")
    return f"{cookie_name()}={token or ''}{attrs}{'' if token else '; Max-Age=0'}".encode()


async def _enter(scope, send, label: str) -> None:
    q = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    key = (q.get("k") or [""])[0]
    now = time.time()
    ip = _client_ip(scope)
    row = db.one(sa.select(db.cam_web_sessions).where(db.cam_web_sessions.c.key_hash == db.token_hash(key))) if 0 < len(key) <= 128 else None
    if row is None or row["label"] != label or row["closed_at"] or (row["key_expires_at"] or 0) < now or _expired(row, now):
        await _page(send, 403, "This link has expired or was already used", "Open the camera's web page again from the hub.")
        return
    if not _still_allowed(row):
        end(row, "revoked", ip, now=now)
        await _page(send, 403, EXPIRED_TITLE, WHY["revoked"])
        return
    token = db.new_token()
    t = db.cam_web_sessions
    with db.engine().begin() as c:   # single use: only the first request with this key gets past this update
        n = c.execute(sa.update(t).where(t.c.id == row["id"], t.c.key_hash == row["key_hash"], t.c.closed_at.is_(None))
                      .values(key_hash=None, key_expires_at=None, token_hash=db.token_hash(token), entered_at=now, last_seen_at=now,
                              expires_at=_deadline(row, now))).rowcount
    if n != 1:
        await _page(send, 403, "This link has expired or was already used", "Open the camera's web page again from the hub.")
        return
    _checked[row["id"]] = now
    _audit("entered", row, ip, now=now)
    await send({"type": "http.response.start", "status": 303, "headers": [
        (b"location", b"/"), (b"set-cookie", _session_cookie(token)), (b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer")]})
    await send({"type": "http.response.body", "body": b""})


async def _close(scope, send, label: str) -> None:
    token = _cookie(Headers(scope=scope), cookie_name())
    row = db.one(sa.select(db.cam_web_sessions).where(db.cam_web_sessions.c.token_hash == db.token_hash(token))) if token and len(token) <= 128 else None
    if row is not None and row["label"] == label:
        end(row, "closed", _client_ip(scope))
    await _page(send, 200, "Camera page closed", "You can close this tab.", [(b"set-cookie", _session_cookie(None))])


def _slot(server_id: str) -> asyncio.Semaphore:
    sem = _slots.get(server_id)
    if sem is None:
        sem = _slots[server_id] = asyncio.Semaphore(settings.camweb_parallel)
    return sem


def _has_body(headers: Headers) -> bool:
    try:
        return int(headers.get("content-length") or 0) > 0 or "transfer-encoding" in headers
    except ValueError:
        return True


async def _wait_head(s, body_done: asyncio.Event, timeout: float) -> bool:
    """True once the response head is in (or the stream was aborted); the timeout counts from the end of the upload."""
    head = asyncio.ensure_future(s.head.wait())
    try:
        while not s.head.is_set():
            if body_done.is_set():
                await asyncio.wait_for(asyncio.shield(head), timeout)
            else:
                b = asyncio.ensure_future(body_done.wait())
                await asyncio.wait({head, b}, return_when=asyncio.FIRST_COMPLETED)
                b.cancel()
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        head.cancel()


async def _http(scope, receive, send, label: str) -> None:
    path = scope.get("path") or "/"
    if path == ENTER:
        await _enter(scope, send, label)
        return
    if path == CLOSE:
        await _close(scope, send, label)
        return
    headers = Headers(scope=scope)
    method = scope["method"]
    own = _own_origin(scope)
    origin = headers.get("origin")
    if method not in ("GET", "HEAD", "OPTIONS") and origin and origin.rstrip("/").lower() != own:
        await _page(send, 403, "Request refused", "A page on another name tried to change this camera's settings.")
        return
    row, why = _session(_cookie(headers, cookie_name()), label, time.time()) if label else (None, "none")
    if row is None:
        await _page(send, 403, EXPIRED_TITLE, WHY.get(why, WHY["none"]))
        return
    conn = registry.get(row["server_id"])
    if conn is None:
        await _page(send, 503, "The camera's server is offline", "Try again once the server is back online.")
        return
    if "camweb" not in conn.caps():
        await _page(send, 501, "Update the server", "This server's software is too old to open camera web pages.")
        return
    fwd = forward_headers(headers, row, own)
    fwd["x-camweb-path"] = (scope.get("raw_path") or path.encode()).decode("latin-1")
    query = scope.get("query_string", b"").decode("latin-1")
    has_body = _has_body(headers)
    sem = _slot(row["server_id"])
    try:
        await asyncio.wait_for(sem.acquire(), 30)
    except asyncio.TimeoutError:
        await _page(send, 503, "The camera's server is busy", "Too many requests to this server at once; reload in a moment.")
        return
    try:
        try:
            s = await conn.open(method, f"/api/camweb/{quote(row['camera_id'], safe='')}", query, fwd, body=has_body)
        except TooManyStreams:
            await _page(send, 503, "The camera's server is busy", "Too many requests to this server at once; reload in a moment.")
            return
        await _relay(conn, s, receive, send, has_body)
    finally:
        sem.release()


async def _relay(conn, s, receive, send, has_body: bool) -> None:
    """Upload the browser's body (credit-paced, never held whole) while waiting for the answer, then stream it back."""
    body_done = asyncio.Event()
    state = {"too_big": False}
    limit = settings.camweb_max_body_mb * 1024 * 1024

    async def pump():
        total, more = 0, has_body
        while more:
            m = await receive()
            if m["type"] == "http.disconnect":
                await conn.abort(s, "browser left")
                return
            data = m.get("body", b"")
            total += len(data)
            if total > limit:
                state["too_big"] = True
                await conn.abort(s, "upload too large")
                return
            if data:
                await conn.send_body(s, data)
            more = m.get("more_body", False)
        if has_body and not s.aborted:
            await conn.send({"t": "end", "id": s.id})
        body_done.set()
        while (await receive())["type"] != "http.disconnect":
            pass
        await conn.abort(s, "browser left")   # no-op once the answer is complete (finish() dropped the stream)

    pumping = asyncio.create_task(pump())
    try:
        if not has_body:
            body_done.set()
        if not await _wait_head(s, body_done, settings.camweb_first_byte_s):
            await conn.abort(s, "timeout")
            await _page(send, 504, "The camera did not answer in time", "Reload to try again.")
            return
        if state["too_big"]:
            await _page(send, 413, "Upload too large", f"Uploads through the hub are limited to {settings.camweb_max_body_mb} MB.")
            return
        if s.aborted or s.status is None:
            await _page(send, 502, "The camera's server did not answer", "Reload to try again.")
            return
        if s.headers.get("x-camweb-error"):   # the server couldn't reach the camera: say so on a page of ours
            detail = bytearray()
            while (c := await s.read()) is not None and len(detail) < 300:
                detail += c
            await _page(send, s.status, "The camera did not answer", bytes(detail[:300]).decode("utf-8", "replace") or "Reload to try again.")
            return
        await send({"type": "http.response.start", "status": s.status, "headers": response_headers(s.headers, settings.cookie_secure)})
        while (c := await s.read()) is not None:
            await send({"type": "http.response.body", "body": c, "more_body": True})
            await conn.send({"t": "credit", "id": s.id, "bytes": len(c)})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
    finally:
        pumping.cancel()
        conn.finish(s)


async def _websocket(scope, receive, send, label: str) -> None:
    ws = WebSocket(scope, receive, send)
    own = _own_origin(scope)
    origin = (ws.headers.get("origin") or "").rstrip("/").lower()
    if not label or origin != own:   # only the camera name's own page may open its sockets
        await ws.close(code=4403)
        return
    row, _ = _session(_cookie(ws.headers, cookie_name()), label, time.time())
    conn = registry.get(row["server_id"]) if row else None
    if row is None or conn is None or "camweb" not in conn.caps():
        await ws.close(code=4401 if row is None else 4503)
        return
    sid = row["server_id"]
    if _sockets.get(sid, 0) >= settings.camweb_ws_per_server:
        await ws.close(code=4429)
        return
    fwd = forward_headers(ws.headers, row, own)
    fwd["x-camweb-path"] = (scope.get("raw_path") or (scope.get("path") or "/").encode()).decode("latin-1")
    _sockets[sid] = _sockets.get(sid, 0) + 1
    try:
        try:
            s = await conn.open("GET", f"/api/camweb/{quote(row['camera_id'], safe='')}", scope.get("query_string", b"").decode("latin-1"),
                                fwd, body=True, ws=True)
        except TooManyStreams:
            await ws.close(code=4503)
            return
        try:
            try:
                await asyncio.wait_for(s.head.wait(), 20)
            except asyncio.TimeoutError:
                await conn.abort(s, "timeout")
            if s.aborted or s.status != 101:
                await ws.close(code=4502)
                return
            await ws.accept(subprotocol=s.headers.get("sec-websocket-protocol") or None)
            await _ws_relay(ws, conn, s, row, label)
        finally:
            await conn.abort(s, "closed")
            conn.finish(s)
    finally:
        _sockets[sid] = max(0, _sockets.get(sid, 1) - 1)


async def _ws_relay(ws: WebSocket, conn, s, row: dict, label: str) -> None:
    async def up():   # browser -> camera
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect":
                return
            for piece in ws_pieces(m["bytes"] if m.get("bytes") is not None else (m.get("text") or "")):
                await s.take_credit(len(piece))
                if s.aborted:
                    return
                await conn.send_bytes(chunk(s.id, piece))

    async def down():   # camera -> browser
        asm = WsAssembler()
        while (c := await s.read()) is not None:
            await conn.send({"t": "credit", "id": s.id, "bytes": len(c)})
            got = asm.feed(c)
            if got is not None:
                binary, payload = got
                await (ws.send_bytes(payload) if binary else ws.send_text(payload.decode("utf-8", "replace")))

    async def alive():   # an open socket keeps the session alive; a closed or expired session closes the socket
        while True:
            await asyncio.sleep(30)
            live, _ = _session_by_hash(row["token_hash"], label, time.time())
            if live is None:
                return

    tasks = [asyncio.create_task(up()), asyncio.create_task(down()), asyncio.create_task(alive())]
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
                log.exception("camera page socket")
        try:
            await ws.close()
        except RuntimeError:
            pass   # the browser already went


# ---------------------------------------------------------------- the middleware

def _is_camera_origin(origin: str) -> bool:
    try:
        host = urlsplit(origin).hostname or ""
    except ValueError:
        return False
    return bool(host) and label_of(host) is not None


class CamwebMiddleware:
    """Outermost: camera names are handled here and nothing else; the hub refuses requests from camera pages."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            label = label_of(_header(scope, b"host"))
            if label is not None:
                if scope["type"] == "http":
                    await _http(scope, receive, send, label)
                else:
                    await _websocket(scope, receive, send, label)
                return
            origin = _header(scope, b"origin")
            if origin and _is_camera_origin(origin):
                # a camera page's script calling the hub (fetch, a form, a WebSocket) with the user's hub session
                if scope["type"] == "http":
                    await _page(send, 403, "Request refused", "Camera pages can't call the hub.")
                else:
                    await WebSocket(scope, receive, send).close(code=4403)
                return
        await self.app(scope, receive, send)
