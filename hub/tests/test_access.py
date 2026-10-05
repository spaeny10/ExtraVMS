"""Who sees which servers: the all_sites flag or exactly the granted Sites (no grants = nothing), the legacy
/grants mapping, member removal and server removal cleaning up after themselves, push recipients."""
import asyncio
import time

import sqlalchemy as sa
from fastapi.testclient import TestClient

from hub import cameras, db, push


def _login(client, email, password):
    c = TestClient(client.app, base_url="http://testserver")   # its own cookie jar
    r = c.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return c


def server(org_id: str, name: str, location_id: str | None = None) -> dict:
    """A server row as enrolment would leave it (no tunnel needed), in its own one-server Site unless given one."""
    s = {"id": db.new_id("s_"), "org_id": org_id, "name": name, "location": "", "token_hash": db.token_hash(db.new_token()),
         "token_prev_hash": None, "token_rotated_at": None, "created_at": time.time(), "last_seen_at": None, "online": False,
         "version": None, "summary": None, "clock_skew_s": None, "agent_ip": None, "hostname": None, "location_id": location_id}
    with db.engine().begin() as c:
        c.execute(db.sites.insert().values(**s))
        db.location_for_server(c, s)
    return s


def _fleet_ids(c, org_id):
    o = c.get(f"/api/fleet?org={org_id}").json()["orgs"][0]
    return {s["id"] for s in o["sites"]}, {loc["id"] for loc in o["locations"]}


def test_all_sites_flag_and_grants(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    org = root.post("/api/orgs", json={"name": "Access Co", "slug": "access-co"}).json()
    oid = org["id"]
    a, b = server(oid, "North"), server(oid, "South")
    v = root.post(f"/api/orgs/{oid}/members", json={"email": "guard@access.example", "role": "viewer", "password": "guard-pass-123"}).json()
    viewer = _login(client, "guard@access.example", "guard-pass-123")

    # a new member sees every Site by default
    m = next(x for x in root.get(f"/api/orgs/{oid}/members").json() if x["id"] == v["id"])
    assert m["all_sites"] is True and m["location_ids"] == [] and m["sites"] == []
    assert _fleet_ids(viewer, oid) == ({a["id"], b["id"]}, {a["location_id"], b["location_id"]})

    # restricted to North's Site
    r = root.put(f"/api/orgs/{oid}/members/{v['id']}/access", json={"all_sites": False, "location_ids": [a["location_id"]]})
    assert r.status_code == 200 and r.json() == {"all_sites": False, "location_ids": [a["location_id"]], "sites": [a["id"]]}
    assert _fleet_ids(viewer, oid) == ({a["id"]}, {a["location_id"]})
    assert viewer.get(f"/s/{a['id']}/api/turn").status_code == 200
    assert viewer.get(f"/s/{b['id']}/api/turn").status_code == 403
    assert viewer.get(f"/s/{b['id']}/").status_code == 403
    assert viewer.get(f"/api/locations/{b['location_id']}").status_code == 403
    assert viewer.get(f"/api/locations/{a['location_id']}").json()["servers"][0]["id"] == a["id"]
    assert [loc["id"] for loc in viewer.get(f"/api/orgs/{oid}/locations").json()] == [a["location_id"]]
    # a Site of another customer is refused
    assert root.put(f"/api/orgs/{oid}/members/{v['id']}/access", json={"all_sites": False, "location_ids": ["l_nope"]}).status_code == 422

    # revoking the last Site leaves an empty fleet, not everything
    assert root.put(f"/api/orgs/{oid}/members/{v['id']}/access", json={"all_sites": False, "location_ids": []}).status_code == 200
    assert _fleet_ids(viewer, oid) == (set(), set())
    assert viewer.get(f"/s/{a['id']}/api/turn").status_code == 403
    assert viewer.get(f"/api/alerts?org={oid}").json() == []

    # back to all Sites
    root.put(f"/api/orgs/{oid}/members/{v['id']}/access", json={"all_sites": True, "location_ids": []})
    assert _fleet_ids(viewer, oid)[0] == {a["id"], b["id"]}
    # the member endpoints are admin-only
    assert viewer.put(f"/api/orgs/{oid}/members/{v['id']}/access", json={"all_sites": True}).status_code == 403


def test_legacy_grants_map_to_sites(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": "Legacy Co", "slug": "legacy-co"}).json()["id"]
    a, b = server(oid, "One"), server(oid, "Two")
    v = root.post(f"/api/orgs/{oid}/members", json={"email": "legacy@access.example", "role": "viewer", "password": "legacy-pass-123"}).json()
    assert root.put(f"/api/orgs/{oid}/members/{v['id']}/grants", json={"site_ids": [b["id"]]}).json() == {"ok": True}
    m = next(x for x in root.get(f"/api/orgs/{oid}/members").json() if x["id"] == v["id"])
    assert m["all_sites"] is False and m["location_ids"] == [b["location_id"]] and m["sites"] == [b["id"]]
    # the legacy table mirrors it, so older hub code would still restrict this member
    assert {g["site_id"] for g in db.rows(sa.select(db.site_grants).where(db.site_grants.c.user_id == v["id"]))} == {b["id"]}
    assert root.put(f"/api/orgs/{oid}/members/{v['id']}/grants", json={"site_ids": []}).status_code == 200
    m = next(x for x in root.get(f"/api/orgs/{oid}/members").json() if x["id"] == v["id"])
    assert m["all_sites"] is True and m["sites"] == []

    # removing the member leaves no grants behind
    root.put(f"/api/orgs/{oid}/members/{v['id']}/access", json={"all_sites": False, "location_ids": [a["location_id"]]})
    assert root.delete(f"/api/orgs/{oid}/members/{v['id']}").status_code == 200
    assert db.rows(sa.select(db.location_grants).where(db.location_grants.c.user_id == v["id"])) == []
    assert db.rows(sa.select(db.site_grants).where(db.site_grants.c.user_id == v["id"])) == []


def test_delete_server_cascades(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": "Cascade Co", "slug": "cascade-co"}).json()["id"]
    gone, kept = server(oid, "Gone"), server(oid, "Kept")
    cameras.sync(gone, [{"id": "cam1", "name": "Door"}])
    cameras.sync(kept, [{"id": "cam1", "name": "Door"}])
    db.insert(db.config_backups, {"site_id": gone["id"], "org_id": oid, "created_at": time.time(), "bytes": 2, "data": {}, "cameras": 1,
                                  "identities": 0, "site_version": None})
    db.insert(db.site_grants, {"user_id": "u_someone", "site_id": gone["id"]})
    g = root.post(f"/api/orgs/{oid}/groups", json={"name": "Doors", "members": [{"site": gone["id"], "camera": "cam1"},
                                                                               {"site": kept["id"], "camera": "cam1"}]}).json()
    assert root.delete(f"/api/servers/{gone['id']}").status_code == 200   # the /api/servers alias of DELETE /api/sites/{id}
    assert cameras.for_server(gone["id"]) == [] and len(cameras.for_server(kept["id"])) == 1
    assert db.rows(sa.select(db.config_backups).where(db.config_backups.c.site_id == gone["id"])) == []
    assert db.rows(sa.select(db.site_grants).where(db.site_grants.c.site_id == gone["id"])) == []
    assert root.get(f"/api/orgs/{oid}/groups").json()[0]["members"] == [{"site_id": kept["id"], "camera_id": "cam1"}]
    assert g["id"] and db.one(sa.select(db.sites).where(db.sites.c.id == gone["id"])) is None


def test_push_only_to_members_who_see_the_server(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": "Push Co", "slug": "push-co"}).json()["id"]
    a, b = server(oid, "Seen"), server(oid, "Unseen")
    v = root.post(f"/api/orgs/{oid}/members", json={"email": "push@access.example", "role": "viewer", "password": "push-pass-1234"}).json()
    root.put(f"/api/orgs/{oid}/members/{v['id']}/access", json={"all_sites": False, "location_ids": [a["location_id"]]})
    viewer = _login(client, "push@access.example", "push-pass-1234")
    assert viewer.post("/api/push/subscribe", json={"subscription": {"endpoint": "https://push.example/guard"}, "kinds": ["offline"]}).status_code == 200
    sent = []
    push.set_sender(lambda sub, payload: sent.append((sub["endpoint"], payload["site_id"])) or True)
    try:
        asyncio.run(push.notify_alert(oid, db.one(sa.select(db.sites).where(db.sites.c.id == b["id"])), "offline", {}))
        assert ("https://push.example/guard", b["id"]) not in sent
        asyncio.run(push.notify_alert(oid, db.one(sa.select(db.sites).where(db.sites.c.id == a["id"])), "offline", {}))
        assert ("https://push.example/guard", a["id"]) in sent
    finally:
        push.set_sender(None)
        viewer.post("/api/push/unsubscribe", json={"endpoint": "https://push.example/guard"})
