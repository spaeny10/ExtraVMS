"""Home dashboards and camera groups: ownership, sharing, roles, validation, defaults (no tunnel needed)."""
import sqlalchemy as sa
from fastapi.testclient import TestClient

from hub import db

CFG = {"version": 1, "cols": 12, "rowH": 60, "widgets": [
    {"id": "w_a", "type": "camera", "x": 0, "y": 0, "w": 4, "h": 4, "props": {"site": "s_x", "camera": "cam1"}},
    {"id": "w_b", "type": "events", "x": 4, "y": 0, "w": 8, "h": 6, "props": {"limit": 10, "classes": ["person"]}},
    {"id": "w_c", "type": "ask", "x": 0, "y": 4, "w": 4, "h": 2, "props": {}},
]}


def _login(client, email, password):
    c = TestClient(client.app, base_url="http://testserver")   # its own cookie jar
    r = c.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return c


def test_dashboards_and_groups(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    org = root.post("/api/orgs", json={"name": "Dash Co", "slug": "dash-co"}).json()
    oid = org["id"]
    root.post(f"/api/orgs/{oid}/members", json={"email": "viewer@dash.example", "role": "viewer", "password": "viewer-pass-123"})
    root.post(f"/api/orgs/{oid}/members", json={"email": "admin@dash.example", "role": "admin", "password": "admin-pass-1234"})
    viewer = _login(client, "viewer@dash.example", "viewer-pass-123")
    admin = _login(client, "admin@dash.example", "admin-pass-1234")

    # a fresh user gets a generated default and nothing saved
    r = viewer.get(f"/api/orgs/{oid}/dashboards").json()
    assert r["dashboards"] == [] and r["default_id"] is None
    assert {w["type"] for w in r["generated"]["widgets"]} >= {"events", "alerts", "health", "briefing", "ask"}

    # own dashboard: create, read, update, default
    mine = viewer.post(f"/api/orgs/{oid}/dashboards", json={"name": "My yard", "config": CFG}).json()
    assert mine["owner_user_id"] and not mine["shared"] and mine["can_edit"]
    assert mine["config"]["widgets"][1]["props"] == {"limit": 10, "classes": ["person"]}
    assert viewer.put(f"/api/orgs/{oid}/dashboards/{mine['id']}", json={"name": "Yard"}).json()["name"] == "Yard"
    assert viewer.put(f"/api/orgs/{oid}/dashboards/default", json={"id": mine["id"]}).status_code == 200
    assert viewer.get(f"/api/orgs/{oid}/dashboards").json()["default_id"] == mine["id"]
    # nobody else sees it
    assert admin.get(f"/api/orgs/{oid}/dashboards/{mine['id']}").status_code == 404
    assert [d["id"] for d in admin.get(f"/api/orgs/{oid}/dashboards").json()["dashboards"]] == []

    # a viewer cannot publish; an admin can, and the viewer then sees it read-only
    assert viewer.post(f"/api/orgs/{oid}/dashboards", json={"name": "Ops", "config": CFG, "shared": True}).status_code == 403
    assert viewer.put(f"/api/orgs/{oid}/dashboards/{mine['id']}", json={"shared": True}).status_code == 403
    ops = admin.post(f"/api/orgs/{oid}/dashboards", json={"name": "Ops", "config": CFG, "shared": True}).json()
    assert ops["shared"] and ops["owner_user_id"] is None
    lst = viewer.get(f"/api/orgs/{oid}/dashboards").json()["dashboards"]
    assert [d["name"] for d in lst] == ["Ops", "Yard"]   # shared first
    got = viewer.get(f"/api/orgs/{oid}/dashboards/{ops['id']}").json()
    assert got["can_edit"] is False
    assert viewer.put(f"/api/orgs/{oid}/dashboards/{ops['id']}", json={"name": "Hijacked"}).status_code == 403
    assert viewer.delete(f"/api/orgs/{oid}/dashboards/{ops['id']}").status_code == 403
    assert admin.put(f"/api/orgs/{oid}/dashboards/{ops['id']}", json={"name": "Ops 2"}).json()["name"] == "Ops 2"
    actions = [a["action"] for a in db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id == oid))]
    assert "dashboard.publish Ops" in actions and "dashboard.update Ops 2" in actions

    # validation: unknown widget type, overflow, bad props
    bad = dict(CFG, widgets=[{"id": "w", "type": "weather", "x": 0, "y": 0, "w": 2, "h": 2, "props": {}}])
    assert viewer.post(f"/api/orgs/{oid}/dashboards", json={"name": "Bad", "config": bad}).status_code == 422
    bad = dict(CFG, widgets=[{"id": "w", "type": "ask", "x": 10, "y": 0, "w": 4, "h": 2, "props": {}}])
    assert viewer.post(f"/api/orgs/{oid}/dashboards", json={"name": "Bad", "config": bad}).status_code == 422
    bad = dict(CFG, widgets=[{"id": "w", "type": "camera", "x": 0, "y": 0, "w": 4, "h": 2, "props": {"site": "s"}}])
    assert viewer.post(f"/api/orgs/{oid}/dashboards", json={"name": "Bad", "config": bad}).status_code == 422

    # groups: viewers read, admins write
    assert viewer.post(f"/api/orgs/{oid}/groups", json={"name": "Yards", "members": []}).status_code == 403
    g = admin.post(f"/api/orgs/{oid}/groups", json={"name": "Yards", "members": [{"site": "s_a", "camera": "cam1"}, {"site": "s_a", "camera": "cam1"}, {"site": "s_b", "camera": "cam2"}]}).json()
    assert g["members"] == [{"site_id": "s_a", "camera_id": "cam1"}, {"site_id": "s_b", "camera_id": "cam2"}]
    assert viewer.get(f"/api/orgs/{oid}/groups").json()[0]["name"] == "Yards"
    assert admin.put(f"/api/orgs/{oid}/groups/{g['id']}", json={"members": [{"site": "s_b", "camera": "cam2"}]}).json()["members"] == [{"site_id": "s_b", "camera_id": "cam2"}]
    assert admin.post(f"/api/orgs/{oid}/groups", json={"name": "Bad", "members": [{"nope": 1}]}).status_code == 422
    assert admin.delete(f"/api/orgs/{oid}/groups/{g['id']}").status_code == 200
    assert admin.delete(f"/api/orgs/{oid}/groups/{g['id']}").status_code == 404

    # another org's ids are invisible
    other = root.post("/api/orgs", json={"name": "Other", "slug": "other-co"}).json()
    assert root.get(f"/api/orgs/{other['id']}/dashboards/{ops['id']}").status_code == 404

    # delete own; default clears itself on next read
    assert viewer.delete(f"/api/orgs/{oid}/dashboards/{mine['id']}").status_code == 200
    assert viewer.get(f"/api/orgs/{oid}/dashboards").json()["dashboards"][0]["id"] == ops["id"]
    assert viewer.put(f"/api/orgs/{oid}/dashboards/default", json={"id": None}).status_code == 200
