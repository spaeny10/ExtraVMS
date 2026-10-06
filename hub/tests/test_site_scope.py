"""Members limited to some Sites (memberships.all_sites false + location_grants) see only those Sites everywhere:
acknowledging alerts, digests, the audit log, SOC reports and false-alarm rates, fleet action history, camera groups."""
import time

import pytest
import sqlalchemy as sa

from hub import db, soc_reports
from test_access import _login, server

PW = "scope-pass-1234"


@pytest.fixture(scope="module")
def scope(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": "Scope Co", "slug": "scope-co"}).json()["id"]
    a, b = server(oid, "Alpha NVR"), server(oid, "Bravo NVR")
    ids = {}
    for who, role in (("radm", "admin"), ("rview", "viewer"), ("fview", "viewer")):
        ids[who] = root.post(f"/api/orgs/{oid}/members", json={"email": f"{who}@scope.example", "role": role, "password": PW}).json()["id"]
    for who in ("radm", "rview"):   # restricted to Alpha's Site; fview keeps every Site
        r = root.put(f"/api/orgs/{oid}/members/{ids[who]}/access", json={"all_sites": False, "location_ids": [a["location_id"]]})
        assert r.status_code == 200
    return {"root": root, "oid": oid, "a": a, "b": b, "ids": ids,
            **{who: _login(client, f"{who}@scope.example", PW) for who in ids}}


def _alert(oid: str, site_id: str) -> int:
    with db.engine().begin() as c:
        r = c.execute(db.alerts.insert().values(org_id=oid, site_id=site_id, kind="disk", key="", opened_at=time.time(), detail={}))
        return r.inserted_primary_key[0]


def test_ack_alert_only_on_visible_servers(scope):
    oid, a, b = scope["oid"], scope["a"], scope["b"]
    hidden, mine = _alert(oid, b["id"]), _alert(oid, a["id"])
    assert scope["rview"].post(f"/api/alerts/{hidden}/ack").status_code == 404
    assert db.one(sa.select(db.alerts).where(db.alerts.c.id == hidden))["acked_at"] is None
    assert scope["rview"].post(f"/api/alerts/{mine}/ack").status_code == 200          # viewers may still acknowledge
    assert scope["fview"].post(f"/api/alerts/{_alert(oid, b['id'])}/ack").status_code == 200


def test_digest_is_cut_to_visible_servers(scope, monkeypatch):
    oid, a, b = scope["oid"], scope["a"], scope["b"]
    parts = [{"site_id": s["id"], "site_name": s["name"], "location_id": s["location_id"], "location_name": s["name"], "online": True,
              "headline": f"{s['name']} headline", "text": None, "today": {}, "cameras_down": [], "open_alerts": []} for s in (a, b)]
    db.insert(db.digests, {"org_id": oid, "day": "2026-10-05", "created_at": time.time(), "model": "qwen",
                           "text": "Alpha NVR quiet. Bravo NVR: intruder at the loading dock.", "data": {"sites": parts}})
    full = scope["fview"].get(f"/api/orgs/{oid}/digests").json()[0]
    assert "Bravo" in full["text"] and len(full["data"]["sites"]) == 2
    mine = scope["rview"].get(f"/api/orgs/{oid}/digests").json()[0]
    assert mine["scoped"] is True and "Bravo" not in mine["text"] and "Alpha NVR" in mine["text"] and mine["model"] is None
    assert [p["site_id"] for p in mine["data"]["sites"]] == [a["id"]]
    assert [g["servers"] for g in mine["data"]["locations"]] == [[a["id"]]]
    # generating asks every server of the customer: admins with every Site only
    async def fake_generate(org_id):
        return {"org_id": org_id}
    monkeypatch.setattr("hub.digest.generate", fake_generate)
    assert scope["fview"].post(f"/api/orgs/{oid}/digests/generate").status_code == 403
    assert scope["radm"].post(f"/api/orgs/{oid}/digests/generate").status_code == 403
    assert scope["root"].post(f"/api/orgs/{oid}/digests/generate").status_code == 200


def test_audit_rows_of_visible_servers_and_sites_only(scope):
    oid, a, b = scope["oid"], scope["a"], scope["b"]
    rows = [("on alpha", a["id"], {}), ("on bravo", b["id"], {}), ("customer-wide", None, {}),
            ("bravo site edit", None, {"location_id": b["location_id"]}), ("alpha site edit", None, {"location_id": a["location_id"]})]
    for action, sid, detail in rows:
        db.insert(db.audit_log, {"ts": time.time(), "user_id": None, "user_email": "x@scope.example", "org_id": oid, "site_id": sid,
                                 "action": action, "method": None, "path": None, "status": None, "ip": None, "detail": detail})
    seen = {r["action"] for r in scope["radm"].get(f"/api/audit?org={oid}").json()}
    assert {"on alpha", "alpha site edit"} <= seen and not seen & {"on bravo", "customer-wide", "bravo site edit"}
    assert scope["radm"].get(f"/api/audit?org={oid}&site={b['id']}").status_code == 403
    assert [r["action"] for r in scope["radm"].get(f"/api/audit?org={oid}&site={a['id']}").json()] == ["on alpha"]
    everything = {r["action"] for r in scope["root"].get(f"/api/audit?org={oid}").json()}
    assert {"on bravo", "customer-wide", "bravo site edit"} <= everything


def test_soc_reports_and_false_alarms_only_for_visible_sites(scope, monkeypatch):
    oid, a, b = scope["oid"], scope["a"], scope["b"]
    sites = [{"location_id": s["location_id"], "name": s["name"], "timezone": None, "monitored": True, "incidents": n,
              "by_priority": {}, "median_response_s": 30.0, "dispositions": {"false_alarm": n}, "calls": n, "armed_hours": 10.0,
              "period_hours": 720.0, "coverage": 0.014} for s, n in ((a, 2), (b, 5))]
    data = {"org_id": oid, "year": 2026, "month": 9, "start": 0.0, "end": 1.0, "sites": sites,
            "totals": {"incidents": 7, "median_response_s": 30.0, "dispositions": {"false_alarm": 7}, "calls": 7, "armed_hours": 20.0}}
    db.insert(db.soc_reports, {"kind": "monthly", "org_id": oid, "period_start": 0.0, "period_end": 1.0, "created_at": time.time(),
                               "created_by": None, "text": "Bravo NVR: 5 incidents", "data": data, "model": None})
    whole = scope["root"].get(f"/api/orgs/{oid}/soc/reports").json()[0]
    assert whole["data"]["totals"]["incidents"] == 7
    mine = scope["radm"].get(f"/api/orgs/{oid}/soc/reports").json()[0]
    assert [s["location_id"] for s in mine["data"]["sites"]] == [a["location_id"]]
    assert mine["data"]["totals"]["incidents"] == 2 and "Bravo" not in mine["text"] and "Alpha NVR" in mine["text"]

    incs = [{"id": i, "org_id": oid, "location_id": loc, "state": "closed", "disposition": "false_alarm", "opened_at": 100.0 + i}
            for i, loc in enumerate([a["location_id"], b["location_id"], b["location_id"]], start=900001)]
    monkeypatch.setattr(soc_reports, "_incidents", lambda since, until, org_id=None, location_id=None: list(incs))
    fa = scope["radm"].get(f"/api/orgs/{oid}/soc/false-alarms?since=0&until=1000").json()
    assert [s["location_id"] for s in fa["sites"]] == [a["location_id"]] and fa["totals"]["closed"] == 1
    fa = scope["root"].get(f"/api/orgs/{oid}/soc/false-alarms?since=0&until=1000").json()
    assert fa["totals"]["closed"] == 3


def test_fleet_action_history_only_for_visible_servers(scope):
    oid, a, b = scope["oid"], scope["a"], scope["b"]
    ids = {}
    for name, s in (("rename alpha", a), ("rename bravo", b)):
        with db.engine().begin() as c:
            ids[name] = c.execute(db.audit_log.insert().values(
                ts=time.time(), user_id=None, user_email="x@scope.example", org_id=oid, site_id=s["id"], action=f"fleet action: {name}",
                method="ACTION", path=None, status=200, ip=None, detail={"result": [], "reverse": []})).inserted_primary_key[0]
    recent = {r["action"] for r in scope["radm"].get(f"/api/orgs/{oid}/actions/reference").json()["recent"]}
    assert "fleet action: rename alpha" in recent and "fleet action: rename bravo" not in recent
    assert {"fleet action: rename alpha", "fleet action: rename bravo"} <= {
        r["action"] for r in scope["root"].get(f"/api/orgs/{oid}/actions/reference").json()["recent"]}
    assert scope["radm"].post(f"/api/orgs/{oid}/actions/undo/{ids['rename bravo']}").status_code == 404


def test_camera_groups_hide_ungranted_servers(scope):
    oid, a, b = scope["oid"], scope["a"], scope["b"]
    root, radm, rview = scope["root"], scope["radm"], scope["rview"]
    mixed = root.post(f"/api/orgs/{oid}/groups", json={"name": "Doors", "members": [{"site": a["id"], "camera": "cam1"},
                                                                                  {"site": b["id"], "camera": "cam1"}]}).json()
    bravo_only = root.post(f"/api/orgs/{oid}/groups", json={"name": "Bravo yard", "members": [{"site": b["id"], "camera": "cam2"}]}).json()
    got = {g["id"]: g for g in rview.get(f"/api/orgs/{oid}/groups").json()}
    assert bravo_only["id"] not in got
    assert got[mixed["id"]]["members"] == [{"site_id": a["id"], "camera_id": "cam1"}]
    # a restricted admin can't add a server they can't see, and editing keeps the cameras they were never shown
    assert radm.post(f"/api/orgs/{oid}/groups", json={"name": "Sneaky", "members": [{"site": b["id"], "camera": "cam1"}]}).status_code == 403
    r = radm.put(f"/api/orgs/{oid}/groups/{mixed['id']}", json={"members": [{"site": a["id"], "camera": "cam9"}]})
    assert r.status_code == 200
    stored = db.one(sa.select(db.camera_groups).where(db.camera_groups.c.id == mixed["id"]))["members"]
    assert {(m["site_id"], m["camera_id"]) for m in stored} == {(a["id"], "cam9"), (b["id"], "cam1")}
    assert radm.put(f"/api/orgs/{oid}/groups/{bravo_only['id']}", json={"name": "Mine now"}).status_code == 404
    assert radm.delete(f"/api/orgs/{oid}/groups/{mixed['id']}").status_code == 403
    assert root.delete(f"/api/orgs/{oid}/groups/{mixed['id']}").status_code == 200
