"""Sites (locations): CRUD, moving servers between them, enrolling into one, and the fleet's additive shape."""
import time
from types import SimpleNamespace

import sqlalchemy as sa

from hub import agents, cameras, db
from test_access import _login, server

CARD_KEYS = {"id", "org_id", "name", "location", "online", "last_seen_at", "version", "hostname", "clock_skew_s", "summary",
             "open_alerts", "retired_at"}   # what the current UI reads; must all still be there


def _org(root, slug):
    return root.post("/api/orgs", json={"name": slug.title(), "slug": slug}).json()["id"]


def test_location_crud_and_moves(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = _org(root, "loc-co")
    hq = root.post(f"/api/orgs/{oid}/locations", json={"name": "HQ", "address": "1 Main St", "timezone": "America/Chicago"}).json()
    assert hq["id"].startswith("l_") and hq["servers"] == [] and hq["servers_total"] == 0 and hq["timezone"] == "America/Chicago"
    assert root.post(f"/api/orgs/{oid}/locations", json={"name": " hq "}).status_code == 409
    depot = root.post(f"/api/orgs/{oid}/locations", json={"name": "Depot"}).json()
    assert root.patch(f"/api/locations/{depot['id']}", json={"name": "HQ"}).status_code == 409
    assert root.patch(f"/api/locations/{depot['id']}", json={"address": "Dock 4", "notes": "gate code at desk"}).json()["address"] == "Dock 4"

    # a server enrolled elsewhere moves into HQ; its cameras follow
    s = server(oid, "NVR 1")
    own = s["location_id"]
    cameras.sync(s, [{"id": "cam1", "name": "Lobby", "stream_ready": True}, {"id": "cam2", "name": "Dock"}])
    card = root.patch(f"/api/sites/{s['id']}", json={"location_id": hq["id"]}).json()
    assert card["location_id"] == hq["id"] and card["location_name"] == "HQ" and card["cameras_total"] == 2
    assert {c["location_id"] for c in cameras.for_server(s["id"])} == {hq["id"]}
    assert root.patch(f"/api/servers/{s['id']}", json={"location_id": "l_nope"}).status_code == 404
    audit = root.get(f"/api/audit?org={oid}").json()
    moved = next(a for a in audit if a["action"].startswith("server moved"))
    assert moved["location_id"] == hq["id"] and moved["location_name"] == "HQ"
    created = next(a for a in audit if a["action"] == "site created: Depot")
    assert created["location_id"] == depot["id"]

    # the Site's cameras
    cams = root.get(f"/api/locations/{hq['id']}/cameras").json()
    assert [(c["camera_id"], c["name"], c["server_name"], c["online"]) for c in cams] == [("cam2", "Dock", "NVR 1", False), ("cam1", "Lobby", "NVR 1", False)]

    # deleting a Site with servers needs move_to; grants on it are dropped, not carried over
    assert root.delete(f"/api/locations/{hq['id']}").status_code == 409
    assert root.delete(f"/api/locations/{hq['id']}?move_to=l_nope").status_code == 404
    v = root.post(f"/api/orgs/{oid}/members", json={"email": "loc@loc.example", "role": "viewer", "password": "loc-pass-12345",
                                                   "all_sites": False, "location_ids": [hq["id"]]}).json()
    assert v["all_sites"] is False and v["location_ids"] == [hq["id"]]
    r = root.delete(f"/api/locations/{hq['id']}?move_to={depot['id']}").json()
    assert r == {"ok": True, "moved": 1}
    assert db.one(sa.select(db.sites).where(db.sites.c.id == s["id"]))["location_id"] == depot["id"]
    assert {c["location_id"] for c in cameras.for_server(s["id"])} == {depot["id"]}
    assert db.rows(sa.select(db.location_grants).where(db.location_grants.c.location_id == hq["id"])) == []
    assert root.get(f"/api/locations/{hq['id']}").status_code == 404
    # an empty Site deletes directly (the server's old one-server Site)
    assert root.delete(f"/api/locations/{own}").json() == {"ok": True, "moved": 0}


def _pending_claim(code: str) -> None:
    """A server waiting to be enrolled: the claims row plus a parked connection (enroll tolerates a dummy)."""
    db.insert(db.claims, {"code": code, "hint": {"hostname": "box", "cameras": [], "version": "1.0"}, "agent_ip": "10.0.0.9",
                          "first_seen_at": time.time(), "expires_at": time.time() + 900, "consumed_site_id": None})
    agents.registry.pending[code] = SimpleNamespace()


def test_claim_with_and_without_location(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = _org(root, "claim-co")
    _pending_claim("AAAA-1111")
    card = root.post(f"/api/orgs/{oid}/sites/claim", json={"code": "AAAA-1111", "name": "Front Office", "location": "Austin"}).json()
    loc = root.get(f"/api/locations/{card['location_id']}").json()
    assert loc["name"] == "Front Office" and loc["address"] == "Austin" and [s["id"] for s in loc["servers"]] == [card["id"]]

    _pending_claim("BBBB-2222")
    assert root.post(f"/api/orgs/{oid}/servers/claim", json={"code": "BBBB-2222", "name": "Back", "location_id": "l_nope"}).status_code == 404
    card2 = root.post(f"/api/orgs/{oid}/servers/claim", json={"code": "BBBB-2222", "name": "Back Office", "location_id": card["location_id"]}).json()
    assert card2["location_id"] == card["location_id"] and card2["location_name"] == "Front Office"
    loc = root.get(f"/api/locations/{card['location_id']}").json()
    assert loc["servers_total"] == 2 and {s["name"] for s in loc["servers"]} == {"Front Office", "Back Office"}
    # a second server without a Site whose name is taken gets a suffixed Site
    _pending_claim("CCCC-3333")
    card3 = root.post(f"/api/orgs/{oid}/sites/claim", json={"code": "CCCC-3333", "name": "front office"}).json()
    assert card3["location_name"] == "front office 2"


def test_fleet_locations_rollups(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = _org(root, "roll-co")
    site = root.post(f"/api/orgs/{oid}/locations", json={"name": "Campus"}).json()
    a = server(oid, "A", site["id"])
    b = server(oid, "B", site["id"])
    r = server(oid, "Old", site["id"])
    other = server(oid, "Solo")
    db.run(sa.update(db.sites).where(db.sites.c.id == r["id"]).values(retired_at=time.time()))
    cameras.sync(a, [{"id": "c1"}, {"id": "c2"}], disabled=[{"id": "c3", "name": "Off"}])
    cameras.sync(b, [{"id": "c1"}])
    cameras.sync(b, [], full=True)   # b's camera went missing: not counted
    db.insert(db.alerts, {"org_id": oid, "site_id": a["id"], "kind": "disk", "key": "", "opened_at": time.time(), "closed_at": None,
                          "acked_by": None, "acked_at": None, "detail": {}})
    # a server row written without a Site (older code paths) lands in `unassigned`
    loose = dict(server(oid, "Loose"), location_id=None)
    db.run(sa.update(db.sites).where(db.sites.c.id == loose["id"]).values(location_id=None))

    o = root.get(f"/api/fleet?org={oid}").json()["orgs"][0]
    assert {s["name"] for s in o["sites"]} == {"A", "B", "Solo", "Loose"} and o["retired"] == 1 and o["open_alerts"] == 1
    assert all(CARD_KEYS <= set(s) for s in o["sites"])
    by_name = {loc["name"]: loc for loc in o["locations"]}
    campus = by_name["Campus"]
    assert {k: campus[k] for k in ("servers_total", "servers_online", "cameras_total", "cameras_online", "open_alerts", "retired_servers")} == \
        {"servers_total": 2, "servers_online": 0, "cameras_total": 2, "cameras_online": 0, "open_alerts": 1, "retired_servers": 1}
    assert {s["name"] for s in campus["servers"]} == {"A", "B"}
    assert [s["name"] for s in by_name["Solo"]["servers"]] == ["Solo"] and other["location_id"] == by_name["Solo"]["id"]
    assert [s["name"] for s in o["unassigned"]] == ["Loose"]
    card_a = next(s for s in o["sites"] if s["name"] == "A")
    assert card_a["location_name"] == "Campus" and card_a["cameras_total"] == 2 and card_a["open_alerts"] == 1
    # alerts rows say which Site
    al = root.get(f"/api/alerts?org={oid}").json()
    assert al[0]["location_id"] == site["id"] and al[0]["location_name"] == "Campus"
    # the Sites list endpoint has the same rollups; include_retired shows the retired server too
    lst = {loc["name"]: loc for loc in root.get(f"/api/orgs/{oid}/locations").json()}
    assert lst["Campus"]["servers_total"] == 2
    full = root.get(f"/api/orgs/{oid}/locations?include_retired=true").json()
    assert {s["name"] for s in next(loc for loc in full if loc["name"] == "Campus")["servers"]} == {"A", "B", "Old"}
