import pyotp

from conftest import login
from hub import auth


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


def test_totp_flow(client):
    auth.create_user("totp@example.com", "another decent one")
    login(client, "totp@example.com", "another decent one")
    secret = client.post("/auth/totp/setup").json()["secret"]
    assert client.post("/auth/totp/enable", json={"code": "000000"}).status_code == 400
    assert client.post("/auth/totp/enable", json={"code": pyotp.TOTP(secret).now()}).status_code == 200
    client.post("/auth/logout")
    r = client.post("/auth/login", json={"email": "totp@example.com", "password": "another decent one"})
    assert r.status_code == 200 and r.json() == {"totp_required": True}
    assert client.post("/auth/login", json={"email": "totp@example.com", "password": "another decent one", "totp": "123456"}).status_code == 401
    login(client, "totp@example.com", "another decent one", totp=pyotp.TOTP(secret).now())
    client.post("/auth/logout")


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
