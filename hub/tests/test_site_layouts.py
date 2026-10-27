"""A Site's named layouts (layouts.py): private to the user who saved them, per Site, one list shared by Live and
Timeline, only on Sites the user can see; configs are the union of both pages' fields, validated and bounded, and an
update keeps the fields the other page owns; names are unique per user and Site (409 like a server's /api/layouts);
deleting the Site deletes its layouts."""
import pytest
import sqlalchemy as sa

from hub import db, layouts
from test_access import _login, server

LIVE = {"visible": ["a/cam1", "b/cam2"], "order": ["b/cam2", "a/cam1", "a/cam3"], "quality": {"b/cam2": "hd"}, "cols": 3, "activity": False}


@pytest.fixture(scope="module")
def world(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": "Layout Co", "slug": "layout-co"}).json()["id"]
    yard = root.post(f"/api/orgs/{oid}/locations", json={"name": "Layout Yard"}).json()
    server(oid, "Alpha", yard["id"])
    other = server(oid, "Elsewhere")   # its own one-server Site, not granted to the viewer
    for email in ("lv@layout.example", "lv2@layout.example"):
        root.post(f"/api/orgs/{oid}/members", json={"email": email, "role": "viewer", "password": "layout-pass-123",
                                                  "all_sites": False, "location_ids": [yard["id"]]})
    foreign_oid = root.post("/api/orgs", json={"name": "Other Layout Co", "slug": "other-layout-co"}).json()["id"]
    foreign = server(foreign_oid, "Foreign")
    root.post(f"/api/orgs/{foreign_oid}/members", json={"email": "fx@layout.example", "role": "admin", "password": "layout-pass-123"})
    return {"root": root, "oid": oid, "yard": yard, "other": other, "foreign": foreign,
            "viewer": _login(client, "lv@layout.example", "layout-pass-123"),
            "viewer2": _login(client, "lv2@layout.example", "layout-pass-123"),
            "outsider": _login(client, "fx@layout.example", "layout-pass-123")}


def test_crud_private_per_user_shared_by_views(world):
    v, v2 = world["viewer"], world["viewer2"]
    url = f"/api/locations/{world['yard']['id']}/layouts"
    assert v.get(url).json() == []
    r = v.post(url, json={"name": "  Doors  ", "config": LIVE})
    assert r.status_code == 200, r.text
    doors = r.json()
    assert doors["name"] == "Doors" and doors["config"] == LIVE and isinstance(doors["id"], int)
    # one list for Live and Timeline: `view` is accepted from older UIs and ignored; only the fields sent are kept
    assert v.post(url + "?view=timeline", json={"name": "Doors", "config": {}}).status_code == 409
    t = v.post(url + "?view=timeline", json={"name": "Yard", "config": {"visible": None, "solo": "a/cam1", "order": None}}).json()
    assert t["config"] == {"visible": None, "solo": "a/cam1", "order": None}
    assert [x["name"] for x in v.get(url).json()] == ["Doors", "Yard"]
    assert v.get(url + "?view=timeline").json() == v.get(url).json()
    assert db.one(sa.select(db.site_layouts.c.view).where(db.site_layouts.c.id == t["id"]))["view"] == "live"
    assert v.get(url + "?view=dashboards").status_code == 422
    assert v.delete(f"{url}/{t['id']}").json() == {"ok": True}
    # names unique per user/Site/view, case-insensitively; another user may use the same name
    assert v.post(url, json={"name": "doors", "config": {}}).status_code == 409
    assert v2.post(url, json={"name": "Doors", "config": {}}).status_code == 200
    # personal: another member of the same Site neither lists, edits nor deletes them
    assert [x["config"] for x in v2.get(url).json()] == [{}]
    assert v2.put(f"{url}/{doors['id']}", json={"name": "Mine", "config": {}}).status_code == 404
    assert v2.delete(f"{url}/{doors['id']}").status_code == 404
    # update and rename
    gate = v.post(url, json={"name": "Gate", "config": {"visible": ["a/cam1"]}}).json()
    assert v.put(f"{url}/{gate['id']}", json={"name": "DOORS", "config": {}}).status_code == 409
    r = v.put(f"{url}/{doors['id']}", json={"name": "Front doors", "config": {**LIVE, "cols": None, "activity": True}})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Front doors" and r.json()["config"]["cols"] is None and r.json()["updated_at"] >= doors["updated_at"]
    assert [x["name"] for x in v.get(url).json()] == ["Front doors", "Gate"]
    assert v.delete(f"{url}/{gate['id']}").json() == {"ok": True}
    assert v.delete(f"{url}/{gate['id']}").status_code == 404
    assert [x["name"] for x in v.get(url).json()] == ["Front doors"]


def test_access_scoped_to_visible_sites(world):
    v, outsider = world["viewer"], world["outsider"]
    yard = f"/api/locations/{world['yard']['id']}/layouts"
    mine = v.post(yard, json={"name": "Scoped", "config": {}}).json()
    # a Site of the same customer the viewer isn't granted
    other = f"/api/locations/{world['other']['location_id']}/layouts"
    assert v.get(other).status_code == 403
    assert v.post(other, json={"name": "X", "config": {}}).status_code == 403
    # another customer's user: no access to this customer's Site at all
    assert outsider.get(yard).status_code == 403
    assert outsider.post(yard, json={"name": "X", "config": {}}).status_code == 403
    assert outsider.put(f"{yard}/{mine['id']}", json={"name": "X", "config": {}}).status_code == 403
    assert outsider.delete(f"{yard}/{mine['id']}").status_code == 403
    # ...and a layout id reached through the outsider's own Site is still not theirs
    foreign = f"/api/locations/{world['foreign']['location_id']}/layouts"
    assert outsider.get(foreign).json() == []
    assert outsider.put(f"{foreign}/{mine['id']}", json={"name": "X", "config": {}}).status_code == 404
    assert outsider.delete(f"{foreign}/{mine['id']}").status_code == 404
    assert v.get("/api/locations/l_nope/layouts").status_code == 404


def test_save_keeps_the_other_pages_fields(world):
    """Live sends {visible, order, quality, cols, activity}, the Timeline {visible, solo, order}: a save from either
    page replaces its own fields and keeps the rest (the stored config is the union)."""
    v = world["viewer2"]
    url = f"/api/locations/{world['yard']['id']}/layouts"
    lid = v.post(url, json={"name": "Shared", "config": LIVE}).json()["id"]
    # saved on the Timeline: the shared camera set and order change, a solo is added, Live's own fields stay
    tl = {"visible": ["a/cam1"], "solo": "a/cam1", "order": ["a/cam1", "b/cam2"]}
    r = v.put(f"{url}/{lid}", json={"name": "Shared", "config": tl})
    assert r.status_code == 200, r.text
    assert r.json()["config"] == {**tl, "quality": {"b/cam2": "hd"}, "cols": 3, "activity": False}
    # saved on Live again: the Timeline's solo stays; a null sent (cols = Auto) is a value, not "unchanged"
    live = {"visible": ["a/cam1", "b/cam2"], "order": ["b/cam2", "a/cam1"], "quality": {}, "cols": None, "activity": True}
    r = v.put(f"{url}/{lid}", json={"name": "Shared", "config": live})
    assert r.json()["config"] == {**live, "solo": "a/cam1"}
    # a rename that sends nothing changes nothing
    r = v.put(f"{url}/{lid}", json={"name": "Shared 2", "config": {}})
    assert r.json()["name"] == "Shared 2" and r.json()["config"] == {**live, "solo": "a/cam1"}
    assert [x["config"] for x in v.get(url).json() if x["id"] == lid] == [{**live, "solo": "a/cam1"}]
    # an invalid field is still refused, and the stored config is left alone
    assert v.put(f"{url}/{lid}", json={"name": "Shared 2", "config": {"cols": 9}}).status_code == 422
    assert [x["config"] for x in v.get(url).json() if x["id"] == lid] == [{**live, "solo": "a/cam1"}]
    v.delete(f"{url}/{lid}")


def test_union_config_cleaned():
    both = {**LIVE, "solo": "b/cam2", "extra": "dropped"}
    assert layouts.clean_config(both) == {**LIVE, "solo": "b/cam2"}
    assert layouts.clean_config({"solo": None, "cols": None}) == {"solo": None, "cols": None}
    assert layouts.merged_config({"solo": "a/1", "cols": 2}, {"cols": None}) == {"solo": "a/1", "cols": None}
    assert layouts.merged_config(None, {"visible": ["a/1", "a/1"]}) == {"visible": ["a/1"]}
    with pytest.raises(ValueError):
        layouts.merged_config({"solo": "a/1"}, {"solo": "x" * 121})


@pytest.mark.parametrize("config", [
    {"cols": 5}, {"cols": 0}, {"quality": {"a/cam1": "4k"}}, {"visible": "a/cam1"}, {"visible": [""]},
    {"order": ["x" * 121]}, {"visible": [f"a/c{i}" for i in range(501)]}, {"quality": {f"a/c{i}": "sd" for i in range(501)}},
    {"activity": "maybe"}, {"solo": ["a/cam1"]}, {"solo": "x" * 121},
    {"mode": "grid"}, {"spans": {"a/cam1": [13, 4]}}, {"spans": {"a/cam1": [0, 4]}}, {"spans": {"a/cam1": [4]}}, {"spans": {"a/cam1": [4, 4, 4]}},
    {"spans": {"": [4, 4]}}, {"spans": {f"a/c{i}": [4, 4] for i in range(501)}}, {"spans": ["a/cam1"]}, {"focus": ""}, {"focus": "x" * 121},
])
def test_config_validated(world, config):
    r = world["viewer"].post(f"/api/locations/{world['yard']['id']}/layouts", json={"name": "Bad", "config": config})
    assert r.status_code == 422, r.text


def test_names_and_limits(world, monkeypatch):
    v = world["viewer2"]
    url = f"/api/locations/{world['yard']['id']}/layouts"
    assert v.post(url, json={"name": "   ", "config": {}}).status_code == 422
    assert v.post(url, json={"name": "x" * 61, "config": {}}).status_code == 422
    # unknown fields are dropped, duplicate keys collapsed
    r = v.post(url, json={"name": "Clean", "config": {"order": ["a/1", "a/2", "a/1"], "extra": 1}}).json()
    assert r["config"] == {"order": ["a/1", "a/2"]}
    monkeypatch.setattr(layouts, "MAX_LAYOUTS", len(v.get(url).json()))
    assert v.post(url, json={"name": "One too many", "config": {}}).status_code == 422


def test_modes_spans_and_focus(world):
    """Live's layout mode, Freeform sizes and Focus camera are stored as sent ([w, h] lists), and a Timeline save keeps them."""
    v = world["viewer"]
    url = f"/api/locations/{world['yard']['id']}/layouts"
    free = {**LIVE, "mode": "free", "spans": {"a/cam1": [8, 6], "b/cam2": [4, 3]}, "focus": "b/cam2"}
    r = v.post(url, json={"name": "Freeform", "config": free})
    assert r.status_code == 200, r.text
    assert r.json()["config"] == free
    tl = {"visible": ["a/cam1"], "solo": None, "order": ["a/cam1"]}
    r = v.put(f"{url}/{r.json()['id']}", json={"name": "Freeform", "config": tl})
    assert r.json()["config"] == {**free, **tl}
    assert layouts.clean_config({"mode": None, "spans": None, "focus": None}) == {"mode": None, "spans": None, "focus": None}
    assert layouts.clean_config({"spans": {"a/1": (12, 1)}}) == {"spans": {"a/1": [12, 1]}}
    v.delete(f"{url}/{r.json()['id']}")


def test_deleted_site_takes_its_layouts(world):
    root, oid = world["root"], world["oid"]
    loc = root.post(f"/api/orgs/{oid}/locations", json={"name": "Short-lived"}).json()
    url = f"/api/locations/{loc['id']}/layouts"
    assert root.post(url, json={"name": "Temp", "config": LIVE}).status_code == 200
    assert root.delete(f"/api/locations/{loc['id']}").status_code == 200
    assert not db.rows(sa.select(db.site_layouts).where(db.site_layouts.c.location_id == loc["id"]))
