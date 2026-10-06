"""Response headers (security.py), push endpoints (push.py), trusted proxies (__main__/config) and the site TURN
credential refresh (agents.py)."""
import asyncio
import re
import time

import pytest
import sqlalchemy as sa

from hub import agents, db, push, security, turn
from hub.config import settings
from test_access import _login, server


# ---------------------------------------------------------------- what a site may send back through the proxy

def test_proxy_headers_allow_list():
    out = security.filter_proxy_headers({
        "content-type": "video/mp4", "content-range": "bytes 0-99/1000", "accept-ranges": "bytes", "etag": '"abc"',
        "last-modified": "Mon, 05 Oct 2026 10:00:00 GMT", "cache-control": "max-age=86400", "content-disposition": "attachment; filename=a.mp4",
        "x-frame-time": "1.5", "set-cookie": "hub_session=fixated; Path=/", "access-control-allow-origin": "*",
        "access-control-allow-credentials": "true", "content-security-policy": "default-src *", "strict-transport-security": "max-age=0",
        "x-frame-options": "ALLOWALL", "refresh": "0;url=https://evil.example", "link": "<https://evil.example>; rel=preload",
        "transfer-encoding": "chunked", "content-length": "1000", "connection": "close", "www-authenticate": "Basic"}, "s_1")
    assert out["content-type"] == "video/mp4" and out["content-range"] == "bytes 0-99/1000" and out["accept-ranges"] == "bytes"
    assert out["etag"] == '"abc"' and out["x-frame-time"] == "1.5" and out["content-disposition"].startswith("attachment")
    for h in ("set-cookie", "access-control-allow-origin", "access-control-allow-credentials", "strict-transport-security", "refresh",
              "link", "transfer-encoding", "content-length", "connection", "www-authenticate"):
        assert h not in out, h
    # the hub's own, never the site's
    assert out["content-security-policy"] == security.PROXY_CSP and out["x-content-type-options"] == "nosniff"
    assert out["x-frame-options"] == "SAMEORIGIN"
    assert out["content-security-policy"].startswith("sandbox; default-src 'none'")


@pytest.mark.parametrize("ctype", ["text/html", "TEXT/HTML; charset=utf-8", "application/xhtml+xml", "image/svg+xml", "text/xml",
                                   "application/xml", "application/javascript", "text/javascript", "text/xsl", "application/x-shockwave-flash"])
def test_proxy_active_types_become_plain_text(ctype):
    assert security.filter_proxy_headers({"content-type": ctype}, "s_1")["content-type"] == "text/plain; charset=utf-8"


@pytest.mark.parametrize("ctype", ["application/json", "application/x-ndjson", "text/event-stream", "image/jpeg", "video/mp4",
                                   "application/sdp", "text/plain; charset=utf-8", "application/octet-stream"])
def test_proxy_media_and_data_types_pass(ctype):
    assert security.filter_proxy_headers({"Content-Type": ctype}, "s_1")["content-type"] == ctype


def test_proxy_missing_type_and_locations():
    assert security.filter_proxy_headers({}, "s_1")["content-type"] == "application/octet-stream"
    assert security.filter_proxy_headers([("location", "/api/whep/cam1/abc")], "s_1")["location"] == "/s/s_1/api/whep/cam1/abc"
    for bad in ("https://evil.example/", "//evil.example/x", "/\\evil.example", "javascript:alert(1)"):
        assert "location" not in security.filter_proxy_headers({"location": bad}, "s_1"), bad


# ---------------------------------------------------------------- the hub's own headers

def _csp(r) -> dict[str, str]:
    return {d.split(" ", 1)[0]: (d.split(" ", 1) + [""])[1] for d in (x.strip() for x in r.headers["content-security-policy"].split(";")) if d}


def test_hub_pages_carry_security_headers(client):
    r = client.get("/login")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in r.headers["permissions-policy"] and "microphone=(self)" in r.headers["permissions-policy"]
    assert "strict-transport-security" not in r.headers   # plain http (the test client); set on https
    csp = _csp(r)
    assert csp["frame-ancestors"] == "'none'" and csp["object-src"] == "'none'" and "'unsafe-inline'" not in csp["script-src"]
    assert "https:" in csp["connect-src"] and "wss:" in csp["connect-src"] and "blob:" in csp["media-src"]
    assert "https://tile.openstreetmap.org" in csp["img-src"] and "data:" in csp["img-src"]
    # API answers get them too
    assert client.get("/auth/me").headers["x-content-type-options"] == "nosniff"


def test_hsts_on_https(client):
    from fastapi.testclient import TestClient
    c = TestClient(client.app, base_url="https://testserver")
    r = c.get("/healthz")
    assert r.headers["strict-transport-security"] == "max-age=31536000"
    assert _csp(r)["connect-src"].startswith("'self' wss://testserver")


def test_inline_scripts_allowed_by_hash(tmp_path, monkeypatch):
    html = tmp_path / "index.html"
    html.write_text('<html><head><script>var t = 1;</script><script type="module" src="./a.js"></script></head></html>', encoding="utf-8")
    hashes = security.inline_script_hashes(html)
    assert hashes == ["'sha256-" + __import__("base64").b64encode(__import__("hashlib").sha256(b"var t = 1;").digest()).decode() + "'"]
    monkeypatch.setattr(settings, "site_ui_dir", tmp_path)
    csp = security.site_ui_csp("https", "hub.example", "s_abc")
    assert hashes[0] in csp and "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]


def test_site_console_may_only_talk_to_its_own_prefix(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": "Headers Co", "slug": "headers-co"}).json()["id"]
    s = server(oid, "Header NVR")
    r = root.get(f"/s/{s['id']}/")
    if r.status_code == 503:
        pytest.skip("site UI bundle not built")
    csp = _csp(r)
    assert csp["connect-src"] == f"http://testserver/s/{s['id']}/ ws://testserver/s/{s['id']}/"
    assert csp["frame-ancestors"] == "'self'" and r.headers["x-frame-options"] == "SAMEORIGIN"
    # hub-answered routes under a site get the proxy's sandbox
    t = root.get(f"/s/{s['id']}/api/turn")
    assert t.status_code == 200 and t.headers["content-security-policy"] == security.PROXY_CSP


def test_odd_site_ids_never_reach_the_header(client):
    r = client.get("/s/x;script-src%20*/", follow_redirects=False)
    assert re.search(r"connect-src [^;]*/s/-/", r.headers["content-security-policy"])
    assert "script-src *" not in r.headers["content-security-policy"]


# ---------------------------------------------------------------- push endpoints

@pytest.mark.parametrize("url", ["https://fcm.googleapis.com/fcm/send/abc", "https://android.googleapis.com/gcm/send/abc",
                                 "https://updates.push.services.mozilla.com/wpush/v2/abc", "https://web.push.apple.com/QAbc",
                                 "https://wns2-par02p.notify.windows.com/w/?token=abc"])
def test_real_push_services_accepted(url):
    assert push.endpoint_ok(url)


@pytest.mark.parametrize("url", ["http://fcm.googleapis.com/fcm/send/abc", "https://postgres:5432/", "http://169.254.169.254/latest",
                                 "https://evil.example/fcm.googleapis.com", "https://fcm.googleapis.com.evil.example/x",
                                 "https://fcm.googleapis.com:8443/x", "https://user:pw@fcm.googleapis.com/x", "https://notify.windows.com.evil/x",
                                 "https://127.0.0.1/x", "file:///etc/passwd", ""])
def test_other_endpoints_refused(url):
    assert not push.endpoint_ok(url)


def test_subscribe_refuses_non_push_endpoints_and_only_replaces_own_rows(client, superuser):
    a = _login(client, superuser["email"], superuser["password"])
    oid = a.post("/api/orgs", json={"name": "Push Scope Co", "slug": "push-scope-co"}).json()["id"]
    a.post(f"/api/orgs/{oid}/members", json={"email": "pa@push.example", "role": "viewer", "password": "push-a-pass-1"})
    a.post(f"/api/orgs/{oid}/members", json={"email": "pb@push.example", "role": "viewer", "password": "push-b-pass-1"})
    pa, pb = _login(client, "pa@push.example", "push-a-pass-1"), _login(client, "pb@push.example", "push-b-pass-1")
    assert pa.post("/api/push/subscribe", json={"subscription": {"endpoint": "http://postgres:5432/"}}).status_code == 400
    ep = "https://fcm.googleapis.com/fcm/send/shared-browser"
    keys_a = {"p256dh": "AAA", "auth": "aaa"}
    assert pa.post("/api/push/subscribe", json={"subscription": {"endpoint": ep, "keys": keys_a}}).status_code == 200
    t = db.push_subscriptions
    owners = lambda: sorted(r["user_id"] for r in db.rows(sa.select(t.c.user_id).where(t.c.endpoint == ep)))  # noqa: E731
    uid_a = owners()[0]
    # naming someone else's endpoint (without its keys) doesn't delete their subscription
    assert pb.post("/api/push/subscribe", json={"subscription": {"endpoint": ep, "keys": {"p256dh": "BBB", "auth": "bbb"}}}).status_code == 200
    assert uid_a in owners() and len(owners()) == 2
    pb.post("/api/push/unsubscribe", json={"endpoint": ep})
    assert owners() == [uid_a]
    # the same browser subscription (same keys) signed in as someone else takes it over
    assert pb.post("/api/push/subscribe", json={"subscription": {"endpoint": ep, "keys": keys_a}}).status_code == 200
    assert len(owners()) == 1 and owners() != [uid_a]
    pb.post("/api/push/unsubscribe", json={"endpoint": ep})


def test_old_bad_endpoint_rows_are_dropped_not_sent():
    assert push._send_one({"endpoint": "http://postgres:5432/", "sub": {"endpoint": "http://postgres:5432/"}}, {}) is False


# ---------------------------------------------------------------- trusted proxies, TURN refresh

def test_forwarded_allow_ips_default_is_loopback():
    from hub.config import Settings
    assert Settings(_env_file=None).forwarded_allow_ips == "127.0.0.1"


def test_site_turn_credential_is_short_and_refreshed(monkeypatch):
    assert settings.turn_site_ttl_s <= 86400
    monkeypatch.setattr(settings, "turn_host", "turn.example")
    monkeypatch.setattr(settings, "turn_secret", "s3cret")
    sent = []

    class Conn:
        site_id = "s_turn"
        turn_expires = time.time() + settings.turn_site_ttl_s

        async def send(self, frame):
            sent.append(frame)

    conn = Conn()
    asyncio.run(agents.registry._refresh_turn(conn))
    assert sent == []                                   # plenty left: nothing sent
    conn.turn_expires = time.time() + 60
    asyncio.run(agents.registry._refresh_turn(conn))
    assert len(sent) == 1 and sent[0]["t"] == "turn" and sent[0]["turn"]["username"].endswith(":site:s_turn")
    assert conn.turn_expires > time.time() + settings.turn_site_ttl_s - 5
    assert turn.mint("x", 1) is not None
