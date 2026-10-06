"""Response headers.

1. What a site may send back through /s/<id>/api/* (proxy.py). A site is a box on a customer's LAN, not part of the
   hub: whatever it answers is served on the hub's origin, next to the hub_session cookie. So only an allow-list of
   response headers passes (never Set-Cookie, CORS, CSP or anything else security-related), any type a browser could
   run as a document (HTML, XML, SVG, JavaScript) is downgraded to text/plain, and every answer carries nosniff plus
   a sandboxing CSP: even opened directly in a tab it can't run script with the hub's origin.

2. The hub's own security headers on every response (api.py middleware): HSTS on https, nosniff, framing, referrer
   and permissions policies, and a CSP. Two CSPs: the hub's pages, and the site UI bundle served at /s/<id>/ (code
   the hub ships, frontend/dist), which may only talk to its own /s/<id>/ prefix, not the hub's /api or /auth.
   Inline scripts in either index.html are allowed by their sha256, computed from the file being served.
"""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit

from .config import settings

# ---------------------------------------------------------------- 1. proxied site responses

# What the site UI needs: JSON, NDJSON and SSE streams (type, cache-control), JPEG frames (X-Frame-Time), MP4 and fMP4
# playback with range requests (content-range, accept-ranges, etag, last-modified), downloads (content-disposition),
# WHEP answers (content-type application/sdp, location), 429/503 retry hints.
PROXY_PASS_HEADERS = frozenset({
    "content-type", "content-range", "accept-ranges", "content-encoding", "content-language", "content-disposition",
    "cache-control", "etag", "last-modified", "expires", "vary", "retry-after", "location", "x-frame-time",
})
# a browser may execute (or render as an active document) any of these
ACTIVE_TYPE = re.compile(r"html|xml|svg|javascript|ecmascript|jscript|xsl|x-shockwave|cache-manifest|vnd\.wap", re.I)
SAFE_TEXT = "text/plain; charset=utf-8"
# Opened directly (a frame, a clip, an error page) the answer is a sandboxed, opaque-origin document: no script,
# nothing loaded except the media itself.
PROXY_CSP = "sandbox; default-src 'none'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'unsafe-inline'; frame-ancestors 'self'"


def _relative_location(value: str, site_id: str) -> str | None:
    v = value.strip()
    if not v.startswith("/") or v.startswith("//") or "\\" in v or any(ord(ch) < 0x20 for ch in v):
        return None   # absolute or scheme-relative: a site must not bounce hub users elsewhere
    return f"/s/{site_id}{v}" if v.startswith("/api/") else v


def filter_proxy_headers(headers, site_id: str) -> dict[str, str]:
    """The response headers a site's answer may carry on the hub's origin (lower-case names). `headers`: a dict or
    an iterable of (name, value) pairs as the tunnel delivered them."""
    items = headers.items() if hasattr(headers, "items") else headers
    out: dict[str, str] = {}
    for k, v in items:
        lk = str(k).lower()
        if lk not in PROXY_PASS_HEADERS:
            continue
        v = str(v)
        if lk == "location":
            v = _relative_location(v, site_id)
            if v is None:
                continue
        out[lk] = v
    ct = out.get("content-type", "").strip()
    if not ct:
        out["content-type"] = "application/octet-stream"
    elif ACTIVE_TYPE.search(ct.split(";", 1)[0]):
        out["content-type"] = SAFE_TEXT
    out["x-content-type-options"] = "nosniff"
    out["content-security-policy"] = PROXY_CSP
    out["x-frame-options"] = "SAMEORIGIN"
    return out


# ---------------------------------------------------------------- 2. the hub's own headers

PERMISSIONS_POLICY = "camera=(), microphone=(self), geolocation=(), payment=(), usb=(), serial=(), hid=(), bluetooth=()"
_hash_cache: dict[str, tuple[float, list[str]]] = {}
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>", re.S | re.I)


def inline_script_hashes(index: Path) -> list[str]:
    """'sha256-…' for each inline <script> in this index.html (cached by mtime; [] when the file is missing)."""
    try:
        mtime = index.stat().st_mtime
    except OSError:
        return []
    key = str(index)
    hit = _hash_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    text = index.read_text(encoding="utf-8", errors="replace")
    hashes = [f"'sha256-{base64.b64encode(hashlib.sha256(m.encode('utf-8')).digest()).decode()}'" for m in _INLINE_SCRIPT.findall(text)]
    _hash_cache[key] = (mtime, hashes)
    return hashes


def _origin_of_template(url: str) -> str | None:
    """https://{s}.tile.example.org/{z}/{x}/{y}.png -> https://*.tile.example.org"""
    try:
        p = urlsplit(url.replace("{s}.", "a."))
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    host = p.netloc
    if "{s}." in url:
        host = "*." + host.split(".", 1)[1]
    return f"{p.scheme}://{host}"


def hub_csp(scheme: str, host: str) -> str:
    """The hub's pages: its own bundle; images and video from itself, blob:, data:, map tiles and Direct-on-LAN
    servers (https://<LAN ip>:8443, any host, so https: broadly); fetches and sockets to itself and to those servers."""
    from .geocode import map_config
    tiles = _origin_of_template(map_config()["tiles"]) or ""
    ws = f"{'wss' if scheme == 'https' else 'ws'}://{host}"
    dev = " http: ws:" if scheme == "http" else ""   # a plain-http dev hub reaches plain-http dev servers
    scripts = " ".join(["'self'", *inline_script_hashes(settings.ui_dir / "index.html")])
    return ("default-src 'self'; "
            f"script-src {scripts}; "
            "style-src 'self' 'unsafe-inline'; "
            f"img-src 'self' data: blob: https: {tiles}".rstrip() + "; "
            "media-src 'self' blob: https:; "
            f"connect-src 'self' {ws} https: wss:{dev}; "
            "font-src 'self' data:; worker-src 'self'; manifest-src 'self'; frame-src 'none'; object-src 'none'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'none'")


def site_ui_csp(scheme: str, host: str, site_id: str) -> str:
    """The site UI bundle at /s/<id>/. Its code is the hub's own (frontend/dist), but it renders data a site sends,
    so it gets the tightest policy that keeps the server console working: scripts only from the hub (no inline
    except the theme pre-paint snippet, by hash), and network access only under its own /s/<id>/ prefix: a page
    that somehow ran foreign script still couldn't call the hub's /api or /auth with the user's session."""
    base = f"{scheme}://{host}/s/{site_id}/"
    wsbase = f"{'wss' if scheme == 'https' else 'ws'}://{host}/s/{site_id}/"
    scripts = " ".join(["'self'", *inline_script_hashes(settings.site_ui_dir / "index.html")])
    return ("default-src 'self'; "
            f"script-src {scripts}; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; "
            f"connect-src {base} {wsbase}; "
            "font-src 'self' data:; worker-src 'none'; manifest-src 'self'; frame-src 'none'; object-src 'none'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'self'")


_SITE_PATH = re.compile(r"^/s/([^/]+)(/.*)?$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_HOST = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:.]+\](:\d{1,5})?$")


def _host(request) -> str:
    """The Host the browser used, if it is a plain host[:port] (it goes into a header); else the public URL's."""
    host = request.headers.get("host") or ""
    return host if _SAFE_HOST.match(host) else (urlsplit(settings.public_url).netloc or "localhost")


def apply(request, response) -> None:
    """Add the hub's headers to a response (never overriding one a route set, e.g. the proxy's sandbox CSP)."""
    h = response.headers
    scheme = request.url.scheme
    host = _host(request)
    path = request.url.path
    if scheme == "https":
        h.setdefault("strict-transport-security", "max-age=31536000")
    h.setdefault("x-content-type-options", "nosniff")
    h.setdefault("referrer-policy", "strict-origin-when-cross-origin")
    h.setdefault("permissions-policy", PERMISSIONS_POLICY)
    m = _SITE_PATH.match(path)
    if m and (m.group(2) or "").startswith("/api/"):
        # hub-answered routes under a site (turn, errors raised before the tunnel): same rules as the site's answers
        h.setdefault("content-security-policy", PROXY_CSP)
        h.setdefault("x-frame-options", "SAMEORIGIN")
    elif m:
        sid = m.group(1) if _SAFE_ID.match(m.group(1)) else "-"   # never a raw path segment inside a header
        h.setdefault("content-security-policy", site_ui_csp(scheme, host, sid))
        h.setdefault("x-frame-options", "SAMEORIGIN")
    else:
        h.setdefault("content-security-policy", hub_csp(scheme, host))
        h.setdefault("x-frame-options", "DENY")
