"""TURN credentials for coturn's `use-auth-secret` mode: username = "<expiry>:<scope>", password =
base64(HMAC-SHA1(secret, username)). Minted per browser session (1 h) and per site (30 d); coturn checks
them itself, so the hub never talks to coturn."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

from .config import settings


def configured() -> bool:
    return bool(settings.turn_host and settings.turn_secret)


def urls() -> list[str]:
    h = settings.turn_host
    out = [f"turn:{h}:{settings.turn_port}?transport=udp", f"turn:{h}:{settings.turn_port}?transport=tcp"]
    if settings.turn_tls_port:
        out.append(f"turns:{h}:{settings.turn_tls_port}?transport=tcp")
    return out


def credential(username: str, secret: str | None = None) -> str:
    return base64.b64encode(hmac.new((secret or settings.turn_secret).encode(), username.encode(), hashlib.sha1).digest()).decode()


def mint(scope: str, ttl_s: int) -> dict | None:
    """{urls, username, credential, expires} for the browser's RTCPeerConnection iceServers (and MediaMTX)."""
    if not configured():
        return None
    expiry = int(time.time()) + ttl_s
    username = f"{expiry}:{scope}"
    return {"urls": urls(), "username": username, "credential": credential(username), "expires": expiry}


def ice_servers(scope: str, ttl_s: int) -> list[dict]:
    c = mint(scope, ttl_s)
    return [{"urls": c["urls"], "username": c["username"], "credential": c["credential"]}] if c else []
