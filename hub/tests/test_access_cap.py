"""An admin can grant at most the Sites they can see: a Site-restricted admin (or owner) must not be able to hand
out every Site, or Sites outside their own set, through invites, /access or adding members.
Otherwise they could invite a fresh account with everything and sign in as it."""
from hub import auth
from test_access import _login, server

MSG = auth.GRANT_SCOPE_MSG


def _setup(client, superuser, slug):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": slug, "slug": slug}).json()["id"]
    a, b = server(oid, "Mine"), server(oid, "Theirs")
    adm = root.post(f"/api/orgs/{oid}/members", json={"email": f"adm@{slug}.example", "role": "admin", "password": "admin-pass-123"}).json()
    assert root.put(f"/api/orgs/{oid}/members/{adm['id']}/access",
                    json={"all_sites": False, "location_ids": [a["location_id"]]}).status_code == 200
    v = root.post(f"/api/orgs/{oid}/members", json={"email": f"v@{slug}.example", "role": "viewer", "password": "viewer-pass-123",
                                                    "all_sites": False, "location_ids": []}).json()
    return root, _login(client, f"adm@{slug}.example", "admin-pass-123"), oid, a, b, adm, v


def _denied(r):
    assert r.status_code == 403 and r.json()["detail"] == MSG, r.text


def test_restricted_admin_invites(client, superuser):
    root, adm, oid, a, b, _, _ = _setup(client, superuser, "cap-inv")
    _denied(adm.post(f"/api/orgs/{oid}/invites", json={}))   # all_sites defaults to true
    _denied(adm.post(f"/api/orgs/{oid}/invites", json={"all_sites": True}))
    _denied(adm.post(f"/api/orgs/{oid}/invites", json={"all_sites": False, "location_ids": [a["location_id"], b["location_id"]]}))
    r = adm.post(f"/api/orgs/{oid}/invites", json={"all_sites": False, "location_ids": [a["location_id"]]})
    assert r.status_code == 200 and r.json()["location_ids"] == [a["location_id"]]
    assert adm.post(f"/api/orgs/{oid}/invites", json={"all_sites": False, "location_ids": []}).status_code == 200
    # an unknown Site is still a 422, as before
    assert adm.post(f"/api/orgs/{oid}/invites", json={"all_sites": False, "location_ids": ["l_nope"]}).status_code == 422


def test_restricted_admin_access(client, superuser):
    root, adm, oid, a, b, _, v = _setup(client, superuser, "cap-acc")
    url = f"/api/orgs/{oid}/members/{v['id']}"
    _denied(adm.put(f"{url}/access", json={"all_sites": True}))
    _denied(adm.put(f"{url}/access", json={"all_sites": False, "location_ids": [b["location_id"]]}))
    m = next(x for x in root.get(f"/api/orgs/{oid}/members").json() if x["id"] == v["id"])
    assert m["all_sites"] is False and m["location_ids"] == []           # nothing changed
    r = adm.put(f"{url}/access", json={"all_sites": False, "location_ids": [a["location_id"]]})
    assert r.status_code == 200 and r.json()["location_ids"] == [a["location_id"]]


def test_restricted_admin_add_member(client, superuser):
    root, adm, oid, a, b, _, v = _setup(client, superuser, "cap-add")
    url = f"/api/orgs/{oid}/members"
    # a new member with no access fields would see every Site
    _denied(adm.post(url, json={"email": "new1@cap-add.example", "password": "new-pass-1234"}))
    assert auth.user_by_email("new1@cap-add.example") is None             # refused before the account was made
    _denied(adm.post(url, json={"email": "new2@cap-add.example", "password": "new-pass-1234", "all_sites": True}))
    _denied(adm.post(url, json={"email": "new3@cap-add.example", "password": "new-pass-1234", "location_ids": [b["location_id"]]}))
    _denied(adm.post(url, json={"email": "new4@cap-add.example", "password": "new-pass-1234", "location_ids": []}))  # [] = all
    r = adm.post(url, json={"email": "new5@cap-add.example", "password": "new-pass-1234", "location_ids": [a["location_id"]]})
    assert r.status_code == 200 and r.json()["all_sites"] is False and r.json()["location_ids"] == [a["location_id"]]
    # role-only change of an existing member leaves access alone and needs no Site check
    r = adm.post(url, json={"email": "v@cap-add.example", "role": "operator"})
    assert r.status_code == 200
    m = next(x for x in root.get(url).json() if x["id"] == v["id"])
    assert m["role"] == "operator" and m["all_sites"] is False


def test_restricted_owner_is_capped_too(client, superuser):
    root, _, oid, a, b, adm, _ = _setup(client, superuser, "cap-own")
    root.post(f"/api/orgs/{oid}/members", json={"email": "adm@cap-own.example", "role": "owner"})
    own = _login(client, "adm@cap-own.example", "admin-pass-123")
    _denied(own.post(f"/api/orgs/{oid}/invites", json={"all_sites": True}))


def test_all_sites_admin_unchanged(client, superuser):
    root, _, oid, a, b, adm, v = _setup(client, superuser, "cap-free")
    root.put(f"/api/orgs/{oid}/members/{adm['id']}/access", json={"all_sites": True})
    free = _login(client, "adm@cap-free.example", "admin-pass-123")
    assert free.post(f"/api/orgs/{oid}/invites", json={}).status_code == 200
    assert free.put(f"/api/orgs/{oid}/members/{v['id']}/access", json={"all_sites": False, "location_ids": [b["location_id"]]}).status_code == 200
    assert free.post(f"/api/orgs/{oid}/members", json={"email": "x@cap-free.example", "password": "new-pass-1234"}).status_code == 200
