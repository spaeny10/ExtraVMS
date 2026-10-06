"""Direct-on-LAN: hub-minted token verification, the middleware (cookie / header / query, read-only, CORS),
the handshake cookie, the LAN certificate, the heartbeat `direct` block and the in-process HTTPS listener.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_direct.py   (from backend/)
"""
import asyncio
import hashlib
import os
import socket
import ssl
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("NVR_ALLOWED_HOSTS", "lan")  # the test client's Host (lan_guard Host allow-list)
os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-direct-test-")  # never the real DB (or its certificate)
os.environ["NVR_HUB_URL"] = "wss://hub.axiomvision.ai/agent"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tunnelproto"))

import httpx  # noqa: E402
from fastapi import Request  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402

from nvr import api, direct  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402

DEVICE_TOKEN = "dev-token-for-tests-only"
SITE = "site_test1"
HUB = "https://hub.axiomvision.ai"
db.set_setting("hub_token", DEVICE_TOKEN)
db.set_setting("hub_site_id", SITE)


def mint(**over) -> str:
    payload = {"sid": SITE, "uid": "u1", "email": "viewer@example.com", "role": "viewer", "exp": int(time.time()) + 600}
    payload.update(over)
    return direct.sign(payload, DEVICE_TOKEN)


# a probe route that shows what a handler sees (query string and identity headers), ahead of the SPA catch-all
async def _echo(request: Request):
    return {"query": request.url.query, "user": request.headers.get("x-hub-user"), "role": request.headers.get("x-hub-role"),
            "site": request.headers.get("x-hub-site"), "direct": request.headers.get("x-hub-direct"),
            "hub_role": api._hub_role(request)}


api.app.router.routes.insert(0, APIRoute("/api/_test/echo", _echo, methods=["GET"]))


def run(coro):
    return asyncio.run(coro)


async def call(method: str, url: str, **kw) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app, raise_app_exceptions=False),
                                 base_url="http://lan") as c:
        return await c.request(method, url, **kw)


# ---------------------------------------------------------------- tokens

def test_token_round_trip_and_rejections():
    good = mint()
    claims = direct.verify(good)
    assert claims and claims["email"] == "viewer@example.com" and claims["role"] == "viewer" and claims["sid"] == SITE
    assert "=" not in good and good.count(".") == 1                       # unpadded base64url, as the hub mints
    assert direct.verify(mint(exp=int(time.time()) - 1)) is None          # expired
    assert direct.verify(mint(exp=int(time.time()))) is None              # exp must be strictly in the future
    assert direct.verify(mint(sid="site_other")) is None                  # another site's token
    assert direct.verify(mint(role="superuser")) is None                  # unknown role
    assert direct.verify(direct.sign({"sid": SITE, "role": "viewer", "exp": time.time() + 60}, "other-device")) is None
    p, s = good.split(".")
    forged = direct._b64e(direct._b64d(p).replace(b"viewer", b"owner\x20"))  # tampered payload, original signature
    assert direct.verify(f"{forged}.{s}") is None
    for junk in ("", "x", "a.b.c", "!!!.???", ".", good + "x", "é.é", "a" * 5000):
        assert direct.verify(junk) is None
    assert direct.verify(None) is None


def test_token_matches_hub_reference():
    """Same bytes as hub/hub/direct.py: key = sha256(device token) hex as bytes, MAC over the base64 text."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hub"))
        from hub import direct as hub_direct  # type: ignore
    except Exception as e:  # the hub package needs its own settings; skip when it cannot be imported here
        print("  (hub reference not importable here:", type(e).__name__, "- skipped)")
        return
    th = hashlib.sha256(DEVICE_TOKEN.encode()).hexdigest()
    tok = hub_direct.mint({"id": SITE, "token_hash": th}, {"id": "u9", "email": "o@example.com"}, "owner", ttl_s=60)
    claims = direct.verify(tok)
    assert claims and claims["uid"] == "u9" and claims["role"] == "owner"
    assert hub_direct.verify(mint(), th, SITE) is not None


def test_unenrolled_site_accepts_no_token():
    tok = mint()
    db.set_setting("hub_token", None)
    try:
        assert direct.verify(tok) is None
    finally:
        db.set_setting("hub_token", DEVICE_TOKEN)


def test_token_rotation_invalidates():
    tok = mint()
    db.set_setting("hub_token", "rotated-token")
    try:
        assert direct.verify(tok) is None
    finally:
        db.set_setting("hub_token", DEVICE_TOKEN)


def test_hub_origin():
    assert direct.hub_origin("wss://hub.axiomvision.ai/agent") == HUB
    assert direct.hub_origin("ws://127.0.0.1:8000/agent") == "http://127.0.0.1:8000"
    assert direct.hub_origin("") is None and direct.hub_origin("ftp://x/y") is None
    assert direct.allowed_origins() == {HUB}                     # the dev origins only with NVR_DEV_ORIGINS=1
    settings.dev_origins = True
    try:
        assert direct.allowed_origins() == {HUB, "http://localhost:8000", "http://localhost:5174"}
    finally:
        settings.dev_origins = False


# ---------------------------------------------------------------- middleware

def test_lan_without_token_is_unchanged():
    r = run(call("GET", "/api/_test/echo?a=1", headers={"x-hub-role": "owner", "x-hub-user": "forged"}))
    assert r.status_code == 200
    assert r.json() == {"query": "a=1", "user": None, "role": None, "site": None, "direct": None, "hub_role": None}
    r = run(call("GET", "/api/direct/session"))
    assert r.status_code == 401 and "access-control-allow-origin" not in r.headers


def test_accepts_header_cookie_and_query():
    tok = mint(role="operator", email="op@example.com")
    want = {"user": "op@example.com", "role": "operator", "site": SITE, "direct": "1", "hub_role": "operator"}
    # the cookie counts only on a cross-origin request from the hub page; same-origin it is ignored (tested below)
    for kw in ({"headers": {"Authorization": f"Direct {tok}"}}, {"headers": {"Cookie": f"direct={tok}", "Origin": HUB}}):
        r = run(call("GET", "/api/_test/echo?a=1", **kw))
        assert r.status_code == 200, r.text
        assert {k: r.json()[k] for k in want} == want and r.json()["query"] == "a=1"
    r = run(call("POST", "/api/_test/echo", headers={"Cookie": f"direct={tok}"}))
    assert r.status_code != 403, "a valid direct cookie on the server's own (same-origin) UI must not make it read-only"
    r = run(call("GET", f"/api/_test/echo?a=1&direct={tok}&b=2"))
    assert r.status_code == 200 and {k: r.json()[k] for k in want} == want
    assert r.json()["query"] == "a=1&b=2"          # the token never reaches a handler (or uvicorn's access log)
    r = run(call("GET", "/api/direct/session", headers={"Authorization": f"Direct {tok}"}))
    assert r.json() == {"user": "op@example.com", "role": "operator", "exp": direct.verify(tok)["exp"], "site_id": SITE}


def test_forged_hub_headers_replaced_by_token_identity():
    r = run(call("GET", "/api/_test/echo", headers={"Authorization": f"Direct {mint()}", "x-hub-role": "owner",
                                                    "x-hub-user": "forged@example.com"}))
    assert r.json()["role"] == "viewer" and r.json()["user"] == "viewer@example.com"


def test_bad_tokens():
    expired = mint(exp=int(time.time()) - 5)
    for kw in ({"headers": {"Authorization": f"Direct {expired}"}}, {"params": {"direct": expired}},
               {"headers": {"Cookie": f"direct={expired}", "Origin": HUB}}):
        r = run(call("GET", "/api/_test/echo", **kw))
        assert r.status_code == 401 and "invalid or expired" in r.json()["detail"]
    # a stale cookie on the server's own page (no cross origin) is ignored: plain LAN access, as before
    r = run(call("GET", "/api/_test/echo", headers={"Cookie": f"direct={expired}"}))
    assert r.status_code == 200 and r.json()["role"] is None


def test_read_only():
    auth = {"Authorization": f"Direct {mint(role='owner')}"}
    for method, path in [("POST", "/api/assistant/plan"), ("PUT", "/api/retention"), ("DELETE", "/api/cameras/cam1"),
                         ("POST", "/api/cameras"), ("PATCH", "/api/events/1/feedback"), ("POST", "/api/whepx/a")]:
        r = run(call(method, path, headers=auth, json={}))
        assert r.status_code == 403 and r.json()["detail"] == "direct connection is read-only; use the hub", (method, path)
    # WHEP signalling passes the middleware: the route itself answers (400 for a bad path, before any network)
    r = run(call("POST", "/api/whep/BAD!", headers=auth, content=b"v=0"))
    assert r.status_code == 400
    # what the hub's proxy hides is hidden on a direct connection too
    for path in ("/api/hub", "/api/config/handoff", "/api/config/history", "/api/ai/chat"):
        assert run(call("GET", path, headers=auth)).status_code == 404, path


def test_probe_and_cors():
    r = run(call("GET", "/api/direct/probe", headers={"Origin": HUB}))
    assert r.status_code == 204 and r.headers["access-control-allow-origin"] == HUB
    assert r.headers["access-control-allow-credentials"] == "true"
    r = run(call("GET", "/api/direct/probe", headers={"Origin": "https://evil.example"}))
    assert r.status_code == 204 and "access-control-allow-origin" not in r.headers
    r = run(call("GET", "/api/direct/probe"))
    assert r.status_code == 204 and "access-control-allow-origin" not in r.headers
    r = run(call("GET", "/api/direct/probe", headers={"Origin": "http://localhost:5174"}))
    assert "access-control-allow-origin" not in r.headers     # dev origin, NVR_DEV_ORIGINS off
    settings.dev_origins = True
    try:
        r = run(call("GET", "/api/direct/probe", headers={"Origin": "http://localhost:5174"}))
        assert r.headers["access-control-allow-origin"] == "http://localhost:5174"
    finally:
        settings.dev_origins = False
    # preflight from the hub (incl. Chrome's Private Network Access ask); none for other origins
    pre = {"Origin": HUB, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type",
           "Access-Control-Request-Private-Network": "true"}
    r = run(call("OPTIONS", "/api/whep/cam1", headers=pre))
    assert r.status_code == 204 and r.headers["access-control-allow-origin"] == HUB
    assert "POST" in r.headers["access-control-allow-methods"] and "authorization" in r.headers["access-control-allow-headers"]
    assert r.headers["access-control-allow-private-network"] == "true"
    r = run(call("OPTIONS", "/api/whep/cam1", headers={**pre, "Origin": "https://evil.example"}))
    assert "access-control-allow-origin" not in r.headers
    # a tokenless cross-origin request to anything but probe/handshake gets no CORS (the page cannot read it)
    r = run(call("GET", "/api/_test/echo", headers={"Origin": HUB}))
    assert "access-control-allow-origin" not in r.headers
    # with a token it does, and exposes the headers the players read
    r = run(call("GET", "/api/_test/echo", headers={"Origin": HUB, "Authorization": f"Direct {mint()}"}))
    assert r.headers["access-control-allow-origin"] == HUB and "location" in r.headers["access-control-expose-headers"]
    r = run(call("POST", "/api/cameras", headers={"Origin": HUB, "Authorization": f"Direct {mint()}"}))
    assert r.status_code == 403 and r.headers["access-control-allow-origin"] == HUB   # the UI can read the refusal


def test_handshake_sets_cookie():
    tok = mint()
    r = run(call("GET", "/api/direct/handshake", params={"token": tok}, headers={"Origin": HUB}))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["user"] == "viewer@example.com" and body["role"] == "viewer"
    assert body["exp"] == direct.verify(tok)["exp"] and tok not in r.text
    cookie = r.headers["set-cookie"]
    low = cookie.lower()
    assert cookie.startswith(f"direct={tok};") and "secure" in low and "samesite=none" in low and "httponly" in low
    assert "path=/" in low and 590 <= int(low.split("max-age=")[1].split(";")[0]) <= 600
    assert r.headers["access-control-allow-origin"] == HUB and r.headers["access-control-allow-credentials"] == "true"
    r = run(call("GET", "/api/direct/handshake", params={"token": mint(sid="nope")}, headers={"Origin": HUB}))
    assert r.status_code == 401 and "set-cookie" not in r.headers


def test_tokens_never_reach_the_access_log():
    """uvicorn logs the query string from the scope dict it handed the app, after the response started: what
    the outer server sees there must not contain a token (handshake ?token= or media ?direct=)."""
    seen = []

    async def server(scope, receive, send):   # stands in for uvicorn's protocol, which keeps the same dict
        async def logged_send(msg):
            if msg["type"] == "http.response.start":
                seen.append(scope["query_string"].decode())
            await send(msg)
        await api.app(scope, receive, logged_send)

    tok = mint()

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url="http://lan") as c:
            assert (await c.get("/api/direct/handshake", params={"token": tok, "x": "1"})).status_code == 200
            assert (await c.get("/api/_test/echo", params={"direct": tok, "t": "5"})).status_code == 200
            assert (await c.get("/api/direct/handshake")).status_code == 401

    run(go())
    assert seen == ["x=1", "t=5", ""] and not any(tok in q for q in seen)


def test_direct_disabled_ignores_tokens():
    settings.direct_enabled = False
    try:
        r = run(call("GET", "/api/_test/echo", headers={"Authorization": f"Direct {mint()}"}))
        assert r.status_code == 200 and r.json()["role"] is None
        r = run(call("GET", "/api/direct/probe", headers={"Origin": HUB}))
        assert "access-control-allow-origin" not in r.headers
        assert direct.summary() is None
    finally:
        settings.direct_enabled = True


# ---------------------------------------------------------------- certificate, summary, HTTPS listener

def test_cert_and_summary():
    cert, key = direct.ensure_cert()
    assert cert.parent == settings.data_dir / "tls" and cert.exists() and key.exists()
    from cryptography import x509
    c = x509.load_pem_x509_certificate(cert.read_bytes())
    san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "localhost" in san.get_values_for_type(x509.DNSName) and socket.gethostname() in san.get_values_for_type(x509.DNSName)
    ips = {str(i) for i in san.get_values_for_type(x509.IPAddress)}
    assert set(direct.local_ipv4s()) <= ips
    assert 800 <= (c.not_valid_after_utc - c.not_valid_before_utc).days <= 825  # Apple refuses TLS certs valid > 825 days
    mtime = cert.stat().st_mtime
    assert direct.ensure_cert()[0].stat().st_mtime == mtime          # kept across restarts
    fp = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
    s = direct.summary()
    assert s["fingerprint"] == fp and len(fp) == 64
    assert s["urls"][-1] == {"url": f"http://localhost:{settings.port}", "local": True}
    assert s["urls"][:-1] == [f"https://{ip}:{settings.https_port}" for ip in direct.local_ipv4s()]
    assert all("127." not in u for u in s["urls"][:-1])


def test_https_listener_in_process():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    old = settings.https_port, settings.host
    settings.https_port, settings.host = port, "127.0.0.1"
    try:
        server = api._https_server()
        assert server is not None

        async def go():
            task = asyncio.create_task(api._serve_https(server))
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.05)
            try:
                async with httpx.AsyncClient(verify=False) as c:
                    r = await c.get(f"https://127.0.0.1:{port}/api/direct/probe", headers={"Origin": HUB})
                    assert r.status_code == 204 and r.headers["access-control-allow-origin"] == HUB
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, 10)

        run(go())
        # a busy port is logged, not fatal
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", port))
            blocker.listen()
            run(api._serve_https(api._https_server()))
    finally:
        settings.https_port, settings.host = old


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
