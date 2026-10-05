"""Direct-on-LAN: the media token the hub mints for a browser to present straight to a server, and the route
and card fields the UI uses to decide whether to try it."""
import base64
import json
import time

import sqlalchemy as sa

from hub import db, direct
from test_access import _login, server

# a fixed device token, so the key derivation is pinned: key = sha256(token).hexdigest(), as text
DEVICE_TOKEN = "device-token-for-direct-tests"
TOKEN_HASH = db.token_hash(DEVICE_TOKEN)
ROW = {"id": "s_direct", "token_hash": TOKEN_HASH}
USER = {"id": "u_1", "email": "guard@example.com"}


def test_key_is_sha256_hex_of_the_device_token():
    import hashlib
    import hmac
    assert TOKEN_HASH == hashlib.sha256(DEVICE_TOKEN.encode()).hexdigest()
    tok = direct.mint(ROW, USER, "viewer", now=1000.0)
    p, s = tok.split(".")
    want = hmac.new(TOKEN_HASH.encode(), p.encode(), hashlib.sha256).digest()
    assert base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)) == want and "=" not in tok
    assert json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))) == \
        {"sid": "s_direct", "uid": "u_1", "email": "guard@example.com", "role": "viewer", "exp": 1900}


def test_round_trip_expiry_tamper_and_wrong_site():
    now = time.time()
    tok = direct.mint(ROW, USER, "operator", ttl_s=60, now=now)
    got = direct.verify(tok, TOKEN_HASH, site_id="s_direct", now=now + 1)
    assert got and got["role"] == "operator" and got["uid"] == "u_1" and got["exp"] == int(now + 60)
    assert direct.verify(tok, TOKEN_HASH, now=now + 61) is None                  # expired
    assert direct.verify(tok, TOKEN_HASH, site_id="s_other", now=now) is None    # minted for another server
    assert direct.verify(tok, db.token_hash("another-device"), now=now) is None  # another server's key
    p, s = tok.split(".")
    assert direct.verify(p + "." + ("A" if s[0] != "A" else "B") + s[1:], TOKEN_HASH, now=now) is None
    # a payload edited to claim admin keeps the old signature: refused
    forged = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))) | {"role": "admin"}
    fp = base64.urlsafe_b64encode(json.dumps(forged).encode()).rstrip(b"=").decode()
    assert direct.verify(fp + "." + s, TOKEN_HASH, now=now) is None
    for junk in ("", "x", "a.b.c", "!!.??"):
        assert direct.verify(junk, TOKEN_HASH) is None
    # default lifetime is the setting (15 min)
    assert direct.payload_of(direct.mint(ROW, USER, "viewer", now=now))["exp"] == int(now + 900)


def test_info_sanitises_urls():
    assert direct.info(None) == {"available": False, "urls": [], "fingerprint": None}
    got = direct.info({"direct": {"urls": [{"url": "https://192.168.1.5:8443"}, {"url": "javascript:alert(1)"}, "x",
                                           {"url": "http://localhost:8080", "local": True}], "fingerprint": "ab12"}})
    assert got == {"available": True, "fingerprint": "ab12",
                   "urls": [{"url": "https://192.168.1.5:8443"}, {"url": "http://localhost:8080", "local": True}]}


def test_route_and_card(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": "Direct Co", "slug": "direct-co"}).json()["id"]
    other = root.post("/api/orgs", json={"name": "Elsewhere Co", "slug": "elsewhere-co"}).json()["id"]
    s = server(oid, "Lobby")
    root.post(f"/api/orgs/{oid}/members", json={"email": "viewer@direct.example", "role": "viewer", "password": "viewer-pass-123"})
    root.post(f"/api/orgs/{other}/members", json={"email": "out@direct.example", "role": "admin", "password": "outsider-pass-1"})
    viewer = _login(client, "viewer@direct.example", "viewer-pass-123")
    outsider = _login(client, "out@direct.example", "outsider-pass-1")

    assert outsider.post(f"/api/servers/{s['id']}/direct-token").status_code == 403
    assert viewer.post("/api/servers/s_nope/direct-token").status_code == 404

    # nothing reported yet: a token anyway, but available false and no URLs
    r = viewer.post(f"/api/sites/{s['id']}/direct-token")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["available"] is False and j["urls"] == [] and j["fingerprint"] is None and j["role"] == "viewer"
    card = root.get(f"/api/fleet?org={oid}").json()["orgs"][0]["sites"][0]
    assert card["direct"] == {"available": False, "urls": [], "fingerprint": None}

    rep = {"urls": [{"url": "https://192.168.105.105:8443"}, {"url": "http://localhost:8080", "local": True}], "fingerprint": "deadbeef"}
    db.run(sa.update(db.sites).where(db.sites.c.id == s["id"]).values(summary={"version": "1", "direct": rep}))
    j = viewer.post(f"/api/servers/{s['id']}/direct-token").json()
    assert set(j) == {"token", "exp", "role", "available", "urls", "fingerprint"}
    assert j["available"] is True and j["urls"] == rep["urls"] and j["fingerprint"] == "deadbeef"
    p = direct.verify(j["token"], s["token_hash"], site_id=s["id"])
    assert p and p["email"] == "viewer@direct.example" and p["role"] == "viewer" and p["exp"] == j["exp"]
    assert abs(j["exp"] - (time.time() + 900)) < 5
    card = root.get(f"/api/fleet?org={oid}").json()["orgs"][0]["sites"][0]
    assert card["direct"] == {"available": True, "urls": rep["urls"], "fingerprint": "deadbeef"}

    # minting is lightly rate-limited per user and server
    for _ in range(direct.MINT_LIMIT):
        last = viewer.post(f"/api/servers/{s['id']}/direct-token")
    assert last.status_code == 429
    assert root.post(f"/api/servers/{s['id']}/direct-token").status_code == 200   # another user is unaffected
