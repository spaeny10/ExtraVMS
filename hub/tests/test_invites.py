"""Invites: admins make links (owner invites need an owner), list and revoke them; the public preview and accept
routes (new account, existing account with its password, a signed-in browser), single use, expiry, the email
lock, never narrowing an existing member, and the per-IP rate limit."""
import time

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from hub import auth, db
from test_access import _login, server

IP_KEY = "invite|testclient"   # TestClient's client host


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    auth._failures.pop(IP_KEY, None)
    yield
    auth._failures.pop(IP_KEY, None)


def _anon(client):
    return TestClient(client.app, base_url="http://testserver")


def _setup(root, slug):
    oid = root.post("/api/orgs", json={"name": slug.title(), "slug": slug}).json()["id"]
    a, b = server(oid, "North"), server(oid, "South")
    return oid, a, b


def test_create_list_revoke(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid, a, b = _setup(root, "inv-co")
    root.post(f"/api/orgs/{oid}/members", json={"email": "adm@inv.example", "role": "admin", "password": "adm-pass-12345"})
    root.post(f"/api/orgs/{oid}/members", json={"email": "view@inv.example", "role": "viewer", "password": "view-pass-12345"})
    adm, viewer = _login(client, "adm@inv.example", "adm-pass-12345"), _login(client, "view@inv.example", "view-pass-12345")

    assert viewer.post(f"/api/orgs/{oid}/invites", json={}).status_code == 403
    assert adm.post(f"/api/orgs/{oid}/invites", json={"role": "owner"}).status_code == 403   # owners invite owners
    assert root.post(f"/api/orgs/{oid}/invites", json={"role": "owner"}).status_code == 200
    assert adm.post(f"/api/orgs/{oid}/invites", json={"all_sites": False, "location_ids": ["l_nope"]}).status_code == 422
    assert adm.post(f"/api/orgs/{oid}/invites", json={"email": "not an email"}).status_code == 422

    r = adm.post(f"/api/orgs/{oid}/invites", json={"email": "New@Inv.example", "role": "operator", "all_sites": False,
                                                  "location_ids": [a["location_id"]], "label": "night guard", "expires_days": 3}).json()
    assert r["url"] == f"http://testserver/invite/{r['code']}" and len(r["code"]) <= 48
    assert r["email"] == "new@inv.example" and r["role"] == "operator" and r["all_sites"] is False
    assert r["location_ids"] == [a["location_id"]] and r["label"] == "night guard"
    assert 2.9 * 86400 < r["expires_at"] - time.time() <= 3 * 86400
    # an anyone-with-the-link invite stores "" (the column is NOT NULL)
    open_inv = adm.post(f"/api/orgs/{oid}/invites", json={}).json()
    assert open_inv["email"] == "" and open_inv["all_sites"] is True and open_inv["location_ids"] == []

    pending = {i["code"]: i for i in adm.get(f"/api/orgs/{oid}/invites").json()}
    assert r["code"] in pending and open_inv["code"] in pending
    assert pending[r["code"]]["locations"] == [{"id": a["location_id"], "name": "North"}]
    assert pending[r["code"]]["created_by_email"] == "adm@inv.example"
    assert viewer.get(f"/api/orgs/{oid}/invites").status_code == 403

    assert adm.delete(f"/api/orgs/{oid}/invites/{open_inv['code']}").json() == {"ok": True}
    assert adm.delete(f"/api/orgs/{oid}/invites/{open_inv['code']}").status_code == 404
    assert open_inv["code"] not in {i["code"] for i in adm.get(f"/api/orgs/{oid}/invites").json()}
    assert _anon(client).get(f"/api/invites/{open_inv['code']}").status_code == 404
    # the code is a secret: the audit trail names the invite, never the code
    audit = root.get(f"/api/audit?org={oid}").json()
    assert any(x["action"].startswith("invite created: new@inv.example") for x in audit)
    assert not any(r["code"] in str(x) for x in audit)


def test_preview_and_accept_as_new_user(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid, a, b = _setup(root, "inv-new")
    inv = root.post(f"/api/orgs/{oid}/invites", json={"email": "fresh@inv.example", "role": "viewer", "all_sites": False,
                                                     "location_ids": [b["location_id"]]}).json()
    anon = _anon(client)
    p = anon.get(f"/api/invites/{inv['code']}").json()
    assert p["org_name"] == "Inv-New" and p["role"] == "viewer" and p["all_sites"] is False
    assert p["locations"] == [{"id": b["location_id"], "name": "South"}] and p["email_hint"] == "f***@inv.example"
    assert p["expires_at"] == inv["expires_at"]

    assert anon.post(f"/api/invites/{inv['code']}/accept", json={"email": "fresh@inv.example"}).status_code == 422
    assert anon.post(f"/api/invites/{inv['code']}/accept", json={"email": "fresh@inv.example", "password": "short"}).status_code == 422
    # locked to its email: nobody else can use it (and no account is made for them)
    assert anon.post(f"/api/invites/{inv['code']}/accept", json={"email": "other@inv.example", "password": "other-pass-12345"}).status_code == 403
    assert auth.user_by_email("other@inv.example") is None

    r = anon.post(f"/api/invites/{inv['code']}/accept", json={"email": "fresh@inv.example", "password": "fresh-pass-12345"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["role"] == "viewer" and out["org_id"] == oid and out["all_sites"] is False and out["location_ids"] == [b["location_id"]]
    assert anon.cookies.get(auth.COOKIE)
    me = anon.get("/auth/me").json()
    assert me["user"]["email"] == "fresh@inv.example" and me["active_org"] == oid
    o = anon.get(f"/api/fleet?org={oid}").json()["orgs"][0]
    assert [s["id"] for s in o["sites"]] == [b["id"]]
    row = db.one(sa.select(db.invites).where(db.invites.c.code == inv["code"]))
    assert row["accepted_at"] and row["accepted_user_id"] == out["user"]["id"]
    assert any(x["action"] == "invite accepted: fresh@inv.example -> viewer" and x["detail"]["new_user"]
               for x in root.get(f"/api/audit?org={oid}").json())

    # single use
    assert _anon(client).get(f"/api/invites/{inv['code']}").status_code == 404
    assert _anon(client).post(f"/api/invites/{inv['code']}/accept",
                              json={"email": "fresh@inv.example", "password": "fresh-pass-12345"}).status_code == 404
    assert inv["code"] not in {i["code"] for i in root.get(f"/api/orgs/{oid}/invites").json()}


def test_accept_as_existing_user(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid, a, b = _setup(root, "inv-old")
    other = root.post("/api/orgs", json={"name": "Elsewhere", "slug": "inv-elsewhere"}).json()["id"]
    root.post(f"/api/orgs/{other}/members", json={"email": "known@inv.example", "role": "viewer", "password": "known-pass-12345"})
    inv = root.post(f"/api/orgs/{oid}/invites", json={"role": "operator", "all_sites": False, "location_ids": [a["location_id"]]}).json()
    anon = _anon(client)
    assert anon.post(f"/api/invites/{inv['code']}/accept", json={"email": "known@inv.example", "password": "wrong-pass-12345"}).status_code == 401
    r = anon.post(f"/api/invites/{inv['code']}/accept", json={"email": "known@inv.example", "password": "known-pass-12345"}).json()
    assert r["role"] == "operator" and r["location_ids"] == [a["location_id"]] and {o["id"] for o in r["orgs"]} == {oid, other}
    assert anon.get("/auth/me").json()["active_org"] == oid

    # an existing member is never narrowed: higher role and all-Sites access survive a smaller invite
    root.post(f"/api/orgs/{oid}/members", json={"email": "boss@inv.example", "role": "admin", "password": "boss-pass-12345"})
    small = root.post(f"/api/orgs/{oid}/invites", json={"role": "viewer", "all_sites": False, "location_ids": [b["location_id"]]}).json()
    r = _anon(client).post(f"/api/invites/{small['code']}/accept", json={"email": "boss@inv.example", "password": "boss-pass-12345"}).json()
    assert r["role"] == "admin" and r["all_sites"] is True
    # ...but an upgrade applies, and Site grants add up
    up = root.post(f"/api/orgs/{oid}/invites", json={"role": "admin", "all_sites": False, "location_ids": [b["location_id"]]}).json()
    r = _anon(client).post(f"/api/invites/{up['code']}/accept", json={"email": "known@inv.example", "password": "known-pass-12345"}).json()
    assert r["role"] == "admin" and r["all_sites"] is False and set(r["location_ids"]) == {a["location_id"], b["location_id"]}


def test_accept_while_signed_in(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid, a, b = _setup(root, "inv-session")
    other = root.post("/api/orgs", json={"name": "Home Co", "slug": "inv-home"}).json()["id"]
    root.post(f"/api/orgs/{other}/members", json={"email": "signed@inv.example", "role": "viewer", "password": "signed-pass-1234"})
    me = _login(client, "signed@inv.example", "signed-pass-1234")
    sid = me.cookies.get(auth.COOKIE)
    locked = root.post(f"/api/orgs/{oid}/invites", json={"email": "someone@inv.example"}).json()
    assert me.post(f"/api/invites/{locked['code']}/accept", json={}).status_code == 403   # for another address
    inv = root.post(f"/api/orgs/{oid}/invites", json={"role": "viewer"}).json()
    # body credentials are ignored: the signed-in account joins
    r = me.post(f"/api/invites/{inv['code']}/accept", json={"email": "ignored@inv.example", "password": "whatever-123456"}).json()
    assert r["user"]["email"] == "signed@inv.example" and r["all_sites"] is True
    assert auth.user_by_email("ignored@inv.example") is None
    assert me.cookies.get(auth.COOKIE) == sid and me.get("/auth/me").json()["active_org"] == oid
    assert {s["id"] for s in me.get(f"/api/fleet?org={oid}").json()["orgs"][0]["sites"]} == {a["id"], b["id"]}


def test_expired_and_rate_limit(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid, a, b = _setup(root, "inv-limit")
    code = db.new_token()
    db.insert(db.invites, {"code": code, "org_id": oid, "email": "", "role": "viewer", "expires_at": time.time() - 1, "accepted_at": None,
                           "all_sites": True, "location_ids": [], "created_by": None, "created_at": time.time() - 86400,
                           "accepted_user_id": None, "label": None})
    anon = _anon(client)
    assert anon.get(f"/api/invites/{code}").status_code == 404
    assert anon.post(f"/api/invites/{code}/accept", json={"email": "late@inv.example", "password": "late-pass-12345"}).status_code == 404
    assert code not in {i["code"] for i in root.get(f"/api/orgs/{oid}/invites").json()}

    live = root.post(f"/api/orgs/{oid}/invites", json={}).json()
    for _ in range(auth.FAIL_LIMIT - 3):   # the two expired-invite 404s above count too
        assert anon.get(f"/api/invites/guess-{_}").status_code == 404
    assert anon.get(f"/api/invites/{live['code']}").status_code == 200   # successes don't count
    assert anon.get(f"/api/invites/{live['code']}").status_code == 200
    assert anon.get("/api/invites/guess-final").status_code == 404     # the limit-th failure...
    assert anon.get(f"/api/invites/{live['code']}").status_code == 429  # ...locks this IP out, valid code or not
    assert anon.post(f"/api/invites/{live['code']}/accept", json={"email": "x@inv.example", "password": "x-pass-123456"}).status_code == 429
