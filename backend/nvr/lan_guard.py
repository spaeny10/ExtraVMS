"""Browser guards for requests that reach this server over the network WITHOUT a hub tunnel or direct token.

Port 8080 (and 8443) has no sign-in: the LAN is the trust boundary (docs/roles.md). A browser on that LAN is
not, though: any web page it opens can send requests to this server (CSRF) or point a DNS name of its own at
the server's address (DNS rebinding) and then read the answers. The middleware in api.py uses these checks:

* Host allow-list (`host_allowed`): localhost, this machine's hostname(s) and local IP addresses,
  settings.webrtc_public_hosts and NVR_ALLOWED_HOSTS. A rebinding page's requests carry its own host name and
  are refused (421), so it never gets to read anything.
* `from_other_site`: a state-changing request must come from this server's own pages (Origin / Referer
  same-origin; failing those, Sec-Fetch-Site same-origin or none). Scripts and tools that send none of these
  headers (curl, the installer) still work.
* `body_type_problem`: state-changing /api requests carry JSON (or SDP for WHEP signaling). A page cannot
  send application/json cross-origin without a CORS preflight (which this server refuses), so the simple
  text/plain, form and no-content-type bodies a cross-site form or `fetch(no-cors)` can send are rejected.
* `ws_allowed`: the same Host and Origin rules for WebSockets, which CORS does not cover at all.
"""
from __future__ import annotations

import ipaddress
import socket
import time
import urllib.parse

from . import direct
from .config import settings

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
REFRESH_S = 300          # recompute the list of names this machine answers to every 5 min...
MISS_REFRESH_S = 10      # ...and at most this often when an unknown name shows up (a new DHCP address)

_cache: dict = {"at": 0.0, "hosts": frozenset(), "fqdn": None}


def _strip_port(h: str) -> str:
    h = (h or "").strip().lower()
    if h.startswith("["):   # [::1]:8080
        return h[1:h.index("]")] if "]" in h else h[1:]
    if h.count(":") == 1:   # name:port, 1.2.3.4:port (more than one colon = a bare IPv6 address)
        h = h.split(":", 1)[0]
    return h.rstrip(".")


def _compute() -> frozenset[str]:
    names = {"localhost", "127.0.0.1", "::1"}
    try:
        hn = socket.gethostname().lower()
        if hn:
            names |= {hn, f"{hn}.local"}
    except OSError:
        pass
    if _cache["fqdn"] is None:   # can do a DNS lookup: once per process
        try:
            _cache["fqdn"] = socket.getfqdn().lower()
        except OSError:
            _cache["fqdn"] = ""
    if _cache["fqdn"]:
        names.add(_cache["fqdn"])
    names |= {ip.lower() for ip in direct.local_ipv4s() + direct.local_ipv6s()}
    names |= {_strip_port(h) for h in settings.webrtc_public_hosts if h}
    names.discard("")
    return frozenset(names)


def allowed_hosts(refresh: bool = False) -> frozenset[str]:
    now = time.monotonic()
    if refresh or not _cache["hosts"] or now - _cache["at"] > REFRESH_S:
        _cache["hosts"], _cache["at"] = _compute(), now
    return _cache["hosts"]


def host_allowed(host_header: str | None) -> bool:
    """Is this Host header a name this server is really reached under? No Host header: not a browser, allowed."""
    if host_header is None:
        return True
    extra = {_strip_port(h) for h in settings.allowed_hosts.split(",") if h.strip()}
    if "*" in extra:
        return True   # NVR_ALLOWED_HOSTS=* switches the check off (not recommended)
    name = _strip_port(host_header)
    if not name:
        return False
    if name in extra:
        return True
    try:
        if ipaddress.ip_address(name).is_loopback:
            return True
    except ValueError:
        pass
    if name in allowed_hosts():
        return True
    if time.monotonic() - _cache["at"] > MISS_REFRESH_S:   # maybe the address just changed
        return name in allowed_hosts(refresh=True)
    return False


def same_origin(url: str | None, host_header: str | None) -> bool:
    """Is `url` (an Origin or Referer) on this very server, as the browser addressed it (scheme://Host)?"""
    if not url or not host_header:
        return False
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(u.netloc) and u.netloc.lower() == host_header.strip().lower()


def from_other_site(headers, host_header: str | None) -> bool:
    """True when a browser says this request comes from a page that is not this server's own."""
    origin = headers.get("origin")
    if origin is not None:
        return not same_origin(origin, host_header)   # "null" (sandboxed / opaque origins) counts as foreign
    referer = headers.get("referer")
    if referer:
        return not same_origin(referer, host_header)
    return (headers.get("sec-fetch-site") or "same-origin").lower() not in ("same-origin", "none")


def body_type_problem(method: str, path: str, headers) -> str | None:
    """Why this state-changing /api request's body is refused, or None. JSON only (SDP for WHEP signaling);
    a body without any Content-Type is refused too (FastAPI would parse it as JSON). No route takes form or
    multipart uploads (config import is JSON)."""
    if method in SAFE_METHODS or not path.startswith("/api/"):
        return None
    ct = (headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if ct:
        if ct == "application/json" or ct.endswith("+json"):
            return None
        if ct == "application/sdp" and path.startswith("/api/whep/"):
            return None
        return f"unsupported content type {ct!r}: send JSON (Content-Type: application/json)"
    length = (headers.get("content-length") or "").strip()
    if headers.get("transfer-encoding") or (length and length != "0"):
        return "a request body needs Content-Type: application/json"
    return None


def ws_allowed(headers, cookies, query) -> bool:
    """A WebSocket from a browser: allowed host, and either this server's own page or the hub's page carrying a
    valid direct token. No Origin header = not a browser (scripts, tools): host check only."""
    host = headers.get("host")
    if not host_allowed(host):
        return False
    origin = headers.get("origin")
    if origin is None or same_origin(origin, host):
        return True
    if settings.direct_enabled and origin.lower() in direct.allowed_origins():
        token, _ = direct.token_from(headers, cookies, query)
        return direct.verify(token) is not None
    return False
