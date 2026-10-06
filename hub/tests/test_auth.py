import hashlib
import time

import pyotp
import sqlalchemy as sa
from fastapi.testclient import TestClient

from conftest import login
from hub import auth, db


def test_login_me_logout(client, superuser):
    r = client.post("/auth/login", json={"email": superuser["email"], "password": "nope"})
    assert r.status_code == 401
    body = login(client, superuser["email"], superuser["password"])
    assert body["user"]["email"] == superuser["email"] and body["user"]["is_super"]
    assert client.get("/auth/me").json()["user"]["id"] == superuser["id"]
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/auth/me").status_code == 401


def test_rate_limit_after_ten_failures(client):
    auth.create_user("limited@example.com", "a decent password")
    for _ in range(10):
        client.post("/auth/login", json={"email": "limited@example.com", "password": "wrong password"})
    r = client.post("/auth/login", json={"email": "limited@example.com", "password": "a decent password"})
    assert r.status_code == 429


def _code(secret: str, steps_ahead: int = 0) -> str:
    """A code for the current 30 s step, or one step ahead (the hub accepts one either side for clock drift; tests use
    the next step to sign in again inside the same window, since each code works only once)."""
    return pyotp.TOTP(secret).at(time.time() + 30 * steps_ahead)


def test_totp_flow(client):
    auth.create_user("totp@example.com", "another decent one")
    login(client, "totp@example.com", "another decent one")
    assert client.post("/auth/totp/setup").status_code == 422                      # needs the password now
    assert client.post("/auth/totp/setup", json={"password": "wrong"}).status_code == 401
    secret = client.post("/auth/totp/setup", json={"password": "another decent one"}).json()["secret"]
    assert client.post("/auth/totp/enable", json={"code": "000000"}).status_code == 400
    assert client.post("/auth/totp/enable", json={"code": _code(secret)}).status_code == 200
    client.post("/auth/logout")
    r = client.post("/auth/login", json={"email": "totp@example.com", "password": "another decent one"})
    assert r.status_code == 200 and r.json() == {"totp_required": True}
    assert client.post("/auth/login", json={"email": "totp@example.com", "password": "another decent one", "totp": "123456"}).status_code == 401
    # the code that just enabled two-factor is used up: replaying it inside its window is refused
    assert client.post("/auth/login", json={"email": "totp@example.com", "password": "another decent one", "totp": _code(secret)}).status_code == 401
    login(client, "totp@example.com", "another decent one", totp=_code(secret, 1))
    client.post("/auth/logout")


def test_totp_changes_need_password_and_code(client):
    auth.create_user("totp2@example.com", "yet another decent one")
    c = TestClient(client.app, base_url="http://testserver")
    login(c, "totp2@example.com", "yet another decent one")
    old = c.post("/auth/totp/setup", json={"password": "yet another decent one"}).json()["secret"]
    assert c.post("/auth/totp/enable", json={"code": _code(old, -1)}).status_code == 200
    uid = auth.user_by_email("totp2@example.com")["id"]
    # a session alone can no longer turn it off, nor the password alone, nor a wrong code
    assert c.post("/auth/totp/disable").status_code == 422
    assert c.post("/auth/totp/disable", json={"password": "yet another decent one"}).status_code == 401
    assert c.post("/auth/totp/disable", json={"password": "yet another decent one", "code": "000000"}).status_code == 401
    assert c.post("/auth/totp/disable", json={"password": "wrong password", "code": _code(old)}).status_code == 401
    # re-setup (a new phone) needs both too, and leaves the old secret working until a code from the new one is confirmed
    assert c.post("/auth/totp/setup", json={"password": "yet another decent one"}).status_code == 401
    new = c.post("/auth/totp/setup", json={"password": "yet another decent one", "code": _code(old)}).json()["secret"]
    u = auth.user_by_id(uid)
    assert u["totp_enabled"] and u["totp_secret"] == old
    assert c.post("/auth/totp/enable", json={"code": _code(new, 1)}).status_code == 200
    u = auth.user_by_id(uid)
    assert u["totp_enabled"] and u["totp_secret"] == new
    # off: password and a fresh code from the current secret (the step after the last one used)
    with db.engine().begin() as conn:   # forget the last used step so this test needn't wait for the next window
        conn.execute(sa.update(db.users).where(db.users.c.id == uid).values(totp_last_step=None))
    assert c.post("/auth/totp/disable", json={"password": "yet another decent one", "code": _code(new)}).status_code == 200
    u = auth.user_by_id(uid)
    assert not u["totp_enabled"] and u["totp_secret"] is None


def test_totp_step_is_used_once():
    u = auth.create_user("totp3@example.com", "a third decent one")
    secret = pyotp.random_base32()
    with db.engine().begin() as conn:
        conn.execute(sa.update(db.users).where(db.users.c.id == u["id"]).values(totp_secret=secret, totp_enabled=True))
    u = auth.user_by_id(u["id"])
    code = _code(secret)
    assert auth.totp_ok(u, code) is True
    assert auth.totp_ok(auth.user_by_id(u["id"]), code) is False          # same code again: replay
    assert auth.totp_ok(auth.user_by_id(u["id"]), _code(secret, -1)) is False   # an older step is refused too
    assert auth.totp_ok(auth.user_by_id(u["id"]), _code(secret, 1)) is True


def test_password_change_ends_other_sessions(client):
    auth.create_user("pwchange@example.com", "first password here")
    a = TestClient(client.app, base_url="http://testserver")
    b = TestClient(client.app, base_url="http://testserver")
    login(a, "pwchange@example.com", "first password here")
    login(b, "pwchange@example.com", "first password here")
    assert a.post("/auth/password", json={"current": "nope", "new": "second password here"}).status_code == 401
    assert a.post("/auth/password", json={"current": "first password here", "new": "second password here"}).status_code == 200
    assert a.get("/auth/me").status_code == 200        # this session stays
    assert b.get("/auth/me").status_code == 401        # the other device is signed out
    login(b, "pwchange@example.com", "second password here")


def test_sessions_are_stored_hashed_and_legacy_rows_upgrade(client):
    u = auth.create_user("hashed@example.com", "hashed session pw")
    c = TestClient(client.app, base_url="http://testserver")
    login(c, "hashed@example.com", "hashed session pw")
    sid = c.cookies.get(auth.COOKIE)
    ids = [r["id"] for r in db.rows(sa.select(db.sessions.c.id).where(db.sessions.c.user_id == u["id"]))]
    assert ids == [hashlib.sha256(sid.encode()).hexdigest()] and sid not in ids
    # a session written before hashing (plain id) still works and is rehashed in place on first use
    legacy = "legacy-plain-session-id-0123456789abcdef"
    with db.engine().begin() as conn:
        conn.execute(db.sessions.insert().values(id=legacy, user_id=u["id"], org_id=None, created_at=time.time(),
                                                 expires_at=time.time() + 3600, ip="", ua=""))
    d = TestClient(client.app, base_url="http://testserver")
    d.cookies.set(auth.COOKIE, legacy)
    assert d.get("/auth/me").json()["user"]["email"] == "hashed@example.com"
    ids = {r["id"] for r in db.rows(sa.select(db.sessions.c.id).where(db.sessions.c.user_id == u["id"]))}
    assert legacy not in ids and hashlib.sha256(legacy.encode()).hexdigest() in ids
    assert d.get("/auth/me").status_code == 200
    assert d.post("/auth/logout").status_code == 200 and d.get("/auth/me").status_code == 401


def test_unknown_email_costs_a_password_check(client, monkeypatch):
    burned = []
    monkeypatch.setattr(auth, "burn_password_check", lambda pw: burned.append(pw))
    r = client.post("/auth/login", json={"email": "nobody-here@example.com", "password": "whatever it is"})
    assert r.status_code == 401 and burned == ["whatever it is"]


def test_dummy_hash_really_verifies():
    t0 = time.perf_counter()
    auth.burn_password_check("x")   # builds the dummy hash once, then verifies against it
    t1 = time.perf_counter()
    auth.burn_password_check("x")
    assert auth._dummy_hash and time.perf_counter() - t1 > 0.001 and t1 - t0 > 0.001


def test_cross_site_origin_refused(client, superuser):
    r = client.post("/auth/login", json={"email": superuser["email"], "password": superuser["password"]}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_orgs_and_members(client, superuser):
    login(client, superuser["email"], superuser["password"])
    o = client.post("/api/orgs", json={"name": "Jetstream", "slug": "jetstream"}).json()
    assert o["slug"] == "jetstream"
    r = client.post(f"/api/orgs/{o['id']}/members", json={"email": "viewer@example.com", "role": "viewer", "password": "viewer password 1"})
    assert r.status_code == 200
    members = client.get(f"/api/orgs/{o['id']}/members").json()
    assert {m["email"]: m["role"] for m in members} == {"root@example.com": "owner", "viewer@example.com": "viewer"}
    client.post("/auth/logout")
    login(client, "viewer@example.com", "viewer password 1")
    assert client.get(f"/api/orgs/{o['id']}/members").status_code == 403   # viewers don't manage members
    assert client.post("/api/orgs", json={"name": "x", "slug": "x-org"}).status_code == 403
    assert [x["slug"] for x in client.get("/api/orgs").json()] == ["jetstream"]
    client.post("/auth/logout")
