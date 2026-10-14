"""A Site's camera groups (camera_groups.py): listed for everyone who sees the Site, created / renamed / edited / deleted
by its admins and hub administrators; members are "<server>/<camera>" keys of any of the Site's servers (a malformed key
or another Site's server is a 400, a camera the registry never saw is dropped); names are unique per Site
case-insensitively; changes are audited; deleting the Site deletes its groups. Talk-down speakers (from the heartbeat
summaries) can be bound to groups, and a camera's speakers are the union of direct and group bindings."""
import pytest
import sqlalchemy as sa

from hub import camera_groups, cameras, db
from test_access import _login, server

PASS = "groups-pass-123"


def _summary(s: dict, speakers: list[dict]) -> None:
    db.run(sa.update(db.sites).where(db.sites.c.id == s["id"]).values(summary={"speakers": speakers}))


@pytest.fixture(scope="module")
def world(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": "Group Co", "slug": "group-co"}).json()["id"]
    yard = root.post(f"/api/orgs/{oid}/locations", json={"name": "Group Yard"}).json()
    a, b = server(oid, "Alpha", yard["id"]), server(oid, "Bravo", yard["id"])
    other = server(oid, "Elsewhere")   # its own Site
    cameras.sync(a, [{"id": "cam1", "name": "Gate"}, {"id": "cam2", "name": "Dock"}])
    cameras.sync(b, [{"id": "cam1", "name": "North fence"}, {"id": "cam3", "name": "Lot"}])
    cameras.sync(other, [{"id": "cam9", "name": "Far"}])
    users = {}
    for role in ("viewer", "operator", "admin"):
        email = f"{role}@groups.example"
        r = root.post(f"/api/orgs/{oid}/members", json={"email": email, "role": role, "password": PASS, "all_sites": False,
                                                       "location_ids": [yard["id"]]})
        assert r.status_code == 200, r.text
        users[role] = _login(client, email, PASS)
    foreign_oid = root.post("/api/orgs", json={"name": "Other Group Co", "slug": "other-group-co"}).json()["id"]
    root.post(f"/api/orgs/{foreign_oid}/members", json={"email": "fx@groups.example", "role": "admin", "password": PASS})
    return {"root": root, "oid": oid, "yard": yard, "a": a, "b": b, "other": other, **users,
            "outsider": _login(client, "fx@groups.example", PASS)}


def _url(world, *rest):
    return "/".join([f"/api/locations/{world['yard']['id']}/camera-groups", *map(str, rest)])


def test_crud_permissions_and_audit(world):
    v, op, adm, root = world["viewer"], world["operator"], world["admin"], world["root"]
    a, b = world["a"]["id"], world["b"]["id"]
    assert v.get(_url(world)).json() == {"groups": [], "can_edit": False}
    assert adm.get(_url(world)).json()["can_edit"] is True
    # viewers and operators read only
    for c in (v, op):
        assert c.post(_url(world), json={"name": "North yard", "cameras": [f"{a}/cam1"]}).status_code == 403
    r = adm.post(_url(world), json={"name": "  North   yard ", "cameras": [f"{a}/cam1", f"{b}/cam1", f"{a}/cam1"]})
    assert r.status_code == 200, r.text
    north = r.json()
    assert north["name"] == "North yard" and north["cameras"] == [f"{a}/cam1", f"{b}/cam1"] and north["speakers"] == []
    assert north["created_by"] == "admin@groups.example"
    # unique per Site, case-insensitively
    assert adm.post(_url(world), json={"name": "NORTH YARD", "cameras": []}).status_code == 409
    # a hub administrator edits too
    peri = root.post(_url(world), json={"name": "Perimeter", "cameras": [f"{b}/cam3"]}).json()
    assert [g["name"] for g in v.get(_url(world)).json()["groups"]] == ["North yard", "Perimeter"]
    # rename and replace members; an operator may not
    assert op.put(_url(world, north["id"]), json={"name": "X"}).status_code == 403
    assert adm.put(_url(world, north["id"]), json={"name": "perimeter"}).status_code == 409
    r = adm.put(_url(world, north["id"]), json={"name": "North side"})
    assert r.status_code == 200 and r.json()["name"] == "North side" and r.json()["cameras"] == [f"{a}/cam1", f"{b}/cam1"]
    r = adm.put(_url(world, north["id"]), json={"cameras": [f"{b}/cam1", f"{a}/cam2"]})
    assert r.json()["name"] == "North side" and r.json()["cameras"] == [f"{b}/cam1", f"{a}/cam2"]
    assert adm.put(_url(world, north["id"]), json={"name": "Same"}).json()["name"] == "Same"
    assert adm.put(_url(world, north["id"]), json={"name": "same"}).json()["name"] == "same"   # its own name, other case
    # delete
    assert op.delete(_url(world, peri["id"])).status_code == 403
    assert adm.delete(_url(world, peri["id"])).json() == {"ok": True}
    assert adm.delete(_url(world, peri["id"])).status_code == 404
    assert adm.put(_url(world, 999999), json={"name": "x"}).status_code == 404

    rows = db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id == world["oid"], db.audit_log.c.action.like("camera group%")))
    acts = [x["action"] for x in rows]
    assert "camera group created: North yard (Group Yard)" in acts and "camera group deleted: Perimeter (Group Yard)" in acts
    upd = [x for x in rows if x["action"] == "camera group updated: North side (Group Yard)"]
    assert upd and upd[0]["detail"]["renamed_from"] == "North yard"
    moved = [x for x in rows if x["detail"].get("added") == [f"{a}/cam2"]]
    assert moved and moved[0]["detail"]["removed"] == [f"{a}/cam1"]
    adm.delete(_url(world, north["id"]))


def test_access_scoped_to_visible_sites(world):
    other = f"/api/locations/{world['other']['location_id']}/camera-groups"
    assert world["admin"].get(other).status_code == 403   # a Site of the customer they aren't granted
    assert world["admin"].post(other, json={"name": "X", "cameras": []}).status_code == 403
    assert world["outsider"].get(_url(world)).status_code == 403
    assert world["outsider"].post(_url(world), json={"name": "X", "cameras": []}).status_code == 403
    # another Site's group isn't reachable through this Site
    g = world["root"].post(other, json={"name": "Far", "cameras": [f"{world['other']['id']}/cam9"]}).json()
    assert world["admin"].put(_url(world, g["id"]), json={"name": "Mine"}).status_code == 404
    assert world["admin"].delete(_url(world, g["id"])).status_code == 404


def test_validation(world):
    adm, a = world["admin"], world["a"]["id"]
    bad = [["nokey"], [f"{a}/"], [f"/cam1"], [f"{a}/cam1/x"], [""], ["x" * 130], [f"{world['other']['id']}/cam9"], ["s_nope/cam1"]]
    for keys in bad:
        r = adm.post(_url(world), json={"name": "Bad", "cameras": keys})
        assert r.status_code == 400, (keys, r.text)
    assert adm.post(_url(world), json={"name": "   ", "cameras": []}).status_code == 400
    assert adm.post(_url(world), json={"name": "x" * 61, "cameras": []}).status_code == 422
    assert adm.post(_url(world), json={"name": "Big", "cameras": [f"{a}/cam1"] * 501}).status_code == 422
    # a camera the registry never saw on a Site server is dropped, not an error
    r = adm.post(_url(world), json={"name": "Ghosts", "cameras": [f"{a}/cam1", f"{a}/gone"]})
    assert r.status_code == 200 and r.json()["cameras"] == [f"{a}/cam1"]
    assert adm.put(_url(world, r.json()["id"]), json={"cameras": ["junk"]}).status_code == 400
    adm.delete(_url(world, r.json()["id"]))


def test_at_most_100_groups(world, monkeypatch):
    monkeypatch.setattr(camera_groups, "MAX_GROUPS", 2)
    adm = world["admin"]
    ids = [adm.post(_url(world), json={"name": f"G{i}", "cameras": []}).json()["id"] for i in range(2)]
    r = adm.post(_url(world), json={"name": "G3", "cameras": []})
    assert r.status_code == 409 and "at most 2" in r.text
    for i in ids:
        adm.delete(_url(world, i))
    assert camera_groups.MAX_KEYS == 500


def test_speaker_group_binding_and_union(world):
    adm, op, v = world["admin"], world["operator"], world["viewer"]
    a, b = world["a"]["id"], world["b"]["id"]
    _summary(world["a"], [{"id": "spk1", "name": "Back wall", "camera_ids": ["cam2"], "online": True},
                          {"id": "spk2", "name": "Gate horn", "camera_ids": [], "online": None}])
    _summary(world["b"], [{"id": "spk1", "name": "Lot speaker", "camera_ids": ["cam3"], "online": False}])
    north = adm.post(_url(world), json={"name": "North yard", "cameras": [f"{a}/cam1", f"{b}/cam1"]}).json()
    lot = adm.post(_url(world), json={"name": "Lot", "cameras": [f"{b}/cam3"]}).json()
    spk = f"/api/locations/{world['yard']['id']}/speakers"

    out = v.get(spk).json()
    assert out["can_edit"] is False
    assert [(s["server_name"], s["id"]) for s in out["speakers"]] == [("Alpha", "spk1"), ("Alpha", "spk2"), ("Bravo", "spk1")]
    assert out["by_camera"] == {f"{a}/cam2": [{"server_id": a, "speaker_id": "spk1", "name": "Back wall", "online": True, "via": ["camera"]}],
                                f"{b}/cam3": [{"server_id": b, "speaker_id": "spk1", "name": "Lot speaker", "online": False, "via": ["camera"]}]}
    # binding: Site admins only; the speaker must be one its server reports; groups must be this Site's
    assert op.put(f"{spk}/{a}/spk2/groups", json={"group_ids": [north["id"]]}).status_code == 403
    assert adm.put(f"{spk}/{a}/nope/groups", json={"group_ids": [north["id"]]}).status_code == 404
    assert adm.put(f"{spk}/{world['other']['id']}/spk1/groups", json={"group_ids": []}).status_code == 404
    assert adm.put(f"{spk}/{a}/spk2/groups", json={"group_ids": [999999]}).status_code == 400
    r = adm.put(f"{spk}/{a}/spk2/groups", json={"group_ids": [north["id"], lot["id"]]})
    assert r.status_code == 200, r.text
    assert r.json()["group_ids"] == sorted([north["id"], lot["id"]])
    # a speaker on server A covers a camera of server B through a group
    assert sorted(r.json()["cameras"]) == sorted([f"{a}/cam1", f"{b}/cam1", f"{b}/cam3"])
    by = v.get(spk).json()["by_camera"]
    assert [(x["server_id"], x["speaker_id"], x["via"]) for x in by[f"{b}/cam3"]] == [(a, "spk2", [lot["id"]]), (b, "spk1", ["camera"])]
    assert [x["speaker_id"] for x in by[f"{b}/cam1"]] == ["spk2"]
    # the group list names its speakers
    gl = {g["name"]: g for g in v.get(_url(world)).json()["groups"]}
    assert gl["North yard"]["speakers"] == [{"server_id": a, "speaker_id": "spk2"}]
    # membership changes follow at once
    adm.put(_url(world, north["id"]), json={"cameras": [f"{a}/cam2"]})
    by = v.get(spk).json()["by_camera"]
    assert f"{b}/cam1" not in by and f"{a}/cam1" not in by
    assert [(x["speaker_id"], x["via"]) for x in by[f"{a}/cam2"]] == [("spk1", ["camera"]), ("spk2", [north["id"]])]
    # deleting a group unbinds it
    adm.delete(_url(world, lot["id"]))
    assert v.get(spk).json()["speakers"][1]["group_ids"] == [north["id"]]
    acts = [x["action"] for x in db.rows(sa.select(db.audit_log.c.action).where(db.audit_log.c.org_id == world["oid"]))]
    assert "speaker groups set: Gate horn (Group Yard)" in acts
    # a speaker gone from its server: clearing still works, binding doesn't
    _summary(world["a"], [{"id": "spk1", "name": "Back wall", "camera_ids": ["cam2"]}])
    assert adm.put(f"{spk}/{a}/spk2/groups", json={"group_ids": [north["id"]]}).status_code == 404
    assert adm.put(f"{spk}/{a}/spk2/groups", json={"group_ids": []}).status_code == 200
    assert not db.rows(sa.select(db.site_speaker_groups).where(db.site_speaker_groups.c.speaker_id == "spk2"))
    adm.delete(_url(world, north["id"]))


def test_union_helper():
    speakers = [
        {"server_id": "A", "id": "s1", "name": "Wall", "camera_ids": ["c1", "c2"], "group_ids": [1], "online": True},
        {"server_id": "B", "id": "s1", "name": "Pole", "camera_ids": ["c1"], "group_ids": [1, 2]},
        {"server_id": "A", "id": "s2", "name": "Idle", "camera_ids": [], "group_ids": []},
    ]
    groups = [{"id": 1, "cameras": ["A/c1", "B/c9"]}, {"id": 2, "cameras": ["A/c2"]}, {"id": 3, "cameras": ["A/c3"]}]
    m = camera_groups.speakers_by_camera(speakers, groups)
    assert set(m) == {"A/c1", "A/c2", "B/c1", "B/c9"}
    # once per speaker, with every way it got there
    assert [(x["server_id"], x["speaker_id"], x["via"]) for x in m["A/c1"]] == [("A", "s1", ["camera", 1]), ("B", "s1", [1])]
    assert [(x["server_id"], x["via"]) for x in m["A/c2"]] == [("A", ["camera"]), ("B", [2])]
    assert [(x["server_id"], x["via"]) for x in m["B/c9"]] == [("A", [1]), ("B", [1])]
    assert m["B/c1"] == [{"server_id": "B", "speaker_id": "s1", "name": "Pole", "online": None, "via": ["camera"]}]
    assert camera_groups.speakers_by_camera([], groups) == {}


def test_site_delete_drops_groups(world):
    root, oid = world["root"], world["oid"]
    gone = root.post(f"/api/orgs/{oid}/locations", json={"name": "Short lived"}).json()
    s = server(oid, "Temp", gone["id"])
    cameras.sync(s, [{"id": "cam1", "name": "Temp cam"}])
    _summary(s, [{"id": "spk1", "name": "Temp horn", "camera_ids": []}])
    url = f"/api/locations/{gone['id']}/camera-groups"
    g = root.post(url, json={"name": "All", "cameras": [f"{s['id']}/cam1"]}).json()
    assert root.put(f"/api/locations/{gone['id']}/speakers/{s['id']}/spk1/groups", json={"group_ids": [g["id"]]}).status_code == 200
    # its servers move to another Site; the groups describe the deleted place only and go with it
    assert root.delete(f"/api/locations/{gone['id']}?move_to={world['yard']['id']}").status_code == 200
    assert not db.rows(sa.select(db.site_camera_groups).where(db.site_camera_groups.c.location_id == gone["id"]))
    assert not db.rows(sa.select(db.site_speaker_groups).where(db.site_speaker_groups.c.location_id == gone["id"]))
    with db.engine().begin() as c:
        c.execute(sa.update(db.sites).where(db.sites.c.id == s["id"]).values(retired_at=1.0))
