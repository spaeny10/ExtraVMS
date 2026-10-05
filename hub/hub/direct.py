"""Direct-on-LAN media tokens.

A browser on the same LAN as a server can fetch media straight from it instead of through the hub's tunnel.
The hub authorises that with a short-lived token it mints here; the server checks it offline.

Format:  base64url(payload_json) + "." + base64url(HMAC_SHA256(key, payload_b64_ascii))
         (base64url without "=" padding; the MAC covers the base64 text exactly as sent, so JSON key order
         and whitespace never matter to the verifier)
Payload: {"sid": server id, "uid": user id, "email": user email, "role": membership role, "exp": unix seconds}
Key:     sites.token_hash as its 64-char lowercase hex string, UTF-8 encoded, i.e. sha256(device_token).hexdigest().
         The server holds its device token and can derive the same key, so no new secret has to be shared or rotated.
         The hub never stores the device token itself, only this hash, which is why the hash (not the token) is the key.

Only the current token_hash is used to mint: the server only knows its current token, so a token signed with
token_prev_hash would be refused anyway. Tokens are bearer credentials: never log them.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import hmac
import json
import time

from .config import settings

DEFAULT_TTL_S = 900

# Light per-(user, server) limit on minting so a session cannot farm tokens. Generous compared to the invite/sign-in
# limiter (10 per 15 min) because a UI legitimately re-asks on reloads; tokens last 15 min so honest use is a handful.
MINT_LIMIT, MINT_WINDOW_S = 30, 15 * 60
_mints: dict[str, collections.deque] = collections.defaultdict(collections.deque)


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sig(token_hash: str, payload_b64: str) -> bytes:
    return hmac.new(token_hash.encode(), payload_b64.encode("ascii"), hashlib.sha256).digest()


def default_ttl_s() -> int:
    return int(getattr(settings, "direct_token_ttl_s", DEFAULT_TTL_S) or DEFAULT_TTL_S)


def mint(server: dict, user: dict, role: str, ttl_s: int | None = None, now: float | None = None) -> str:
    """A token for `user` acting as `role` on `server` (a sites row; its current token_hash is the key)."""
    exp = int((now if now is not None else time.time()) + (ttl_s if ttl_s is not None else default_ttl_s()))
    payload = {"sid": server["id"], "uid": user["id"], "email": user["email"], "role": role, "exp": exp}
    p = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    return f"{p}.{_b64e(_sig(server['token_hash'], p))}"


def payload_of(token: str) -> dict:
    """The (unverified) payload of a token this hub just minted, e.g. to echo `exp` back to the caller."""
    return json.loads(_b64d(token.split(".")[0]))


def verify(token: str, token_hash: str, site_id: str | None = None, now: float | None = None) -> dict | None:
    """The payload when the token is genuine, unexpired and (given `site_id`) for that server; else None.
    Mirrors the server-side check, for tests and as the reference implementation."""
    try:
        p, s = token.split(".")
        if not hmac.compare_digest(_b64d(s), _sig(token_hash, p)):
            return None
        payload = json.loads(_b64d(p))
    except (ValueError, TypeError, UnicodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("exp"), (int, float)):
        return None
    if payload["exp"] <= (now if now is not None else time.time()):
        return None
    if site_id is not None and payload.get("sid") != site_id:
        return None
    return payload


def info(summary: dict | None) -> dict:
    """What the server last reported about reaching it directly (heartbeat `summary.direct`), sanitised: only
    http(s) URLs, so an odd value from a server can never become e.g. a javascript: link in the UI."""
    d = (summary or {}).get("direct") if isinstance(summary, dict) else None
    d = d if isinstance(d, dict) else {}
    urls = []
    for u in d.get("urls") or []:
        if isinstance(u, str):  # the site sends LAN addresses as plain strings and the localhost hint as an object
            u = {"url": u}
        if isinstance(u, dict) and isinstance(u.get("url"), str) and u["url"].lower().startswith(("https://", "http://")):
            urls.append({"url": u["url"], **({"local": True} if u.get("local") else {})})
    fp = d.get("fingerprint")
    return {"available": bool(urls), "urls": urls, "fingerprint": fp if isinstance(fp, str) and fp else None}


def rate_limited(uid: str, server_id: str, now: float | None = None) -> bool:
    """True when this user has minted too many tokens for this server lately; otherwise records this mint."""
    now = now if now is not None else time.time()
    q = _mints[f"{uid}|{server_id}"]
    while q and q[0] < now - MINT_WINDOW_S:
        q.popleft()
    if len(q) >= MINT_LIMIT:
        return True
    q.append(now)
    return False
