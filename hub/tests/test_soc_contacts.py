"""A Site's SOC settings: contacts and procedures (whole-list replace keeping ids, audited) and the monitoring /
arming routes. Writes to settings: customer admin or SOC supervisor; arming: customer operator+ or SOC operator;
reads: anyone who can see the Site."""
import time

import sqlalchemy as sa

from hub import db
from test_soc_roles import soc_setup

CONTACTS = [{"name": "Dana Ruiz", "role": "Owner", "phone": "+1 512 555 0100", "email": "dana@example.com", "notify_on_open": True},
            {"name": "Night manager", "phone": "+1 512 555 0101", "email": "", "notes": "call second"}]
PROCS = [{"title": "Intruder in the yard", "category": "intrusion", "priority": "medium",
          "steps": [{"text": "Check every yard camera", "required": True}, {"text": "Trigger the siren relay"}]},
         {"title": "Vehicle at the gate after hours", "steps": [{"id": "gate", "text": "Read the plate"}]}]


def test_contacts_permissions_and_ids(client, superuser):
    s = soc_setup(client, superuser, "contacts")
    url = f"/api/locations/{s['loc']['id']}/contacts"
    assert s["adm"].get(url).json() == []
    r = s["adm"].put(url, json={"contacts": CONTACTS})
    assert r.status_code == 200, r.text
    out = r.json()
    assert [(c["order"], c["name"], c["email"], c["notify_on_open"]) for c in out] == \
        [(0, "Dana Ruiz", "dana@example.com", True), (1, "Night manager", None, False)]
    assert set(out[0]) == {"id", "order", "name", "role", "phone", "email", "notify_on_open", "notes", "created_at", "updated_at"}
    # everyone who can see the Site reads it: viewer, SOC operator, SOC supervisor
    for who in ("view", "op", "sup"):
        assert s[who].get(url).json() == out
    # the SOC operator and the customer viewer can't write; the SOC supervisor can
    assert s["op"].put(url, json={"contacts": []}).status_code == 403
    assert s["view"].put(url, json={"contacts": []}).status_code == 403
    reordered = [{**out[1], "phone": "+1 512 555 0199"}, out[0], {"name": "Alarm company"}]
    r = s["sup"].put(url, json={"contacts": reordered})
    assert r.status_code == 200
    new = r.json()
    assert [(c["name"], c["order"]) for c in new] == [("Night manager", 0), ("Dana Ruiz", 1), ("Alarm company", 2)]
    assert new[0]["id"] == out[1]["id"] and new[1]["id"] == out[0]["id"] and new[0]["phone"] == "+1 512 555 0199"
    # an id from another Site is a new row there, never a write to the other Site's row
    other_loc = s["root"].post(f"/api/orgs/{s['mon']}/locations", json={"name": "Depot", "timezone": "America/Chicago"}).json()
    copied = s["root"].put(f"/api/locations/{other_loc['id']}/contacts", json={"contacts": [{"id": new[0]["id"], "name": "Copy"}]}).json()
    assert copied[0]["id"] != new[0]["id"] and s["adm"].get(url).json()[0]["name"] == "Night manager"
    # dropped rows go; bad input is a 422
    assert [c["name"] for c in s["adm"].put(url, json={"contacts": [new[2]]}).json()] == ["Alarm company"]
    assert s["adm"].put(url, json={"contacts": [{"name": ""}]}).status_code == 422
    assert s["adm"].put(url, json={"contacts": [{"name": "X", "email": "not-an-email"}]}).status_code == 422
    # customers without a monitored Site are invisible to SOC staff
    plain_loc = db.one(sa.select(db.sites.c.location_id).where(db.sites.c.id == s["other"]["id"]))["location_id"]
    assert s["sup"].get(f"/api/locations/{plain_loc}/contacts").status_code == 403
    acts = [a["action"] for a in s["root"].get(f"/api/audit?org={s['mon']}").json()]
    assert "site contacts updated: Yard (2)" in acts and "site contacts updated: Yard (3)" in acts
    # deleting the Site takes its contacts with it
    assert s["root"].delete(f"/api/locations/{other_loc['id']}").json()["ok"] is True
    assert db.rows(sa.select(db.location_contacts).where(db.location_contacts.c.location_id == other_loc["id"])) == []


def test_procedures_permissions_and_step_ids(client, superuser):
    s = soc_setup(client, superuser, "procs")
    url = f"/api/locations/{s['loc']['id']}/procedures"
    r = s["sup"].put(url, json={"procedures": PROCS})
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out[0]) == {"id", "order", "title", "category", "steps", "priority", "created_at", "updated_at"}
    assert out[0]["steps"] == [{"id": "s1", "text": "Check every yard camera", "required": True},
                               {"id": "s2", "text": "Trigger the siren relay", "required": False}]
    assert out[1]["steps"] == [{"id": "gate", "text": "Read the plate", "required": False}] and out[1]["category"] is None
    assert s["view"].get(url).json() == out and s["op"].get(url).json() == out
    assert s["op"].put(url, json={"procedures": []}).status_code == 403
    # step ids survive an edit; a new step gets the next free one
    edited = {**out[0], "steps": [out[0]["steps"][1], {"text": "Call the first contact", "required": True}, out[0]["steps"][0]]}
    new = s["adm"].put(url, json={"procedures": [edited]}).json()
    assert len(new) == 1 and new[0]["id"] == out[0]["id"]
    assert [st["id"] for st in new[0]["steps"]] == ["s2", "s3", "s1"]
    assert s["adm"].put(url, json={"procedures": [{"title": "X", "priority": "urgent"}]}).status_code == 422
    assert "site procedures updated: Yard (1)" in [a["action"] for a in s["root"].get(f"/api/audit?org={s['mon']}").json()]


def test_monitoring_routes(client, superuser):
    s = soc_setup(client, superuser, "monr")
    lid = s["loc"]["id"]
    url = f"/api/locations/{lid}/monitoring"
    g = s["view"].get(url).json()
    assert set(g) == {"location_id", "name", "timezone", "monitored", "arm_schedule", "arm_holidays", "arm_override", "override_active",
                      "soc_group_minutes", "armed", "reason", "next_change", "now", "can_configure", "can_arm"}
    assert g["monitored"] is True and g["armed"] is True and g["reason"] == "always" and g["next_change"] is None
    assert (g["can_configure"], g["can_arm"]) == (False, False)
    assert (s["op"].get(url).json()["can_configure"], s["op"].get(url).json()["can_arm"]) == (False, True)
    assert s["sup"].get(url).json()["can_configure"] is True

    body = {"arm_schedule": [{"dow": [0, 1, 2, 3, 4], "from": "18:00", "to": "06:00"}],
            "arm_holidays": [{"date": "2026-12-25", "name": "Christmas", "armed": True}], "soc_group_minutes": 10}
    assert s["view"].put(url, json=body).status_code == 403
    assert s["op"].put(url, json=body).status_code == 403
    r = s["sup"].put(url, json=body)
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["arm_schedule"] == body["arm_schedule"] and m["arm_holidays"] == [{"date": "2026-12-25", "name": "Christmas", "armed": True}]
    assert m["soc_group_minutes"] == 10 and m["reason"] in ("schedule", "disarmed_schedule", "holiday") and m["next_change"]["at"] > m["now"]
    assert s["adm"].put(url, json={"soc_group_minutes": None}).json()["arm_schedule"] == body["arm_schedule"]   # partial
    for bad in ({"arm_schedule": [{"dow": [7], "from": "18:00", "to": "06:00"}]},
                {"arm_schedule": [{"dow": [1], "from": "24:00", "to": "06:00"}]},
                {"arm_schedule": [{"dow": [], "from": "18:00", "to": "06:00"}]},
                {"arm_holidays": [{"date": "2026-02-30"}]},
                {"arm_holidays": [{"date": "2026-12-25", "from": "08:00"}]},
                {"arm_holidays": [{"date": "2026-12-25"}, {"date": "2026-12-25"}]},
                {"soc_group_minutes": 0}):
        assert s["adm"].put(url, json=bad).status_code == 422, bad
    # monitoring needs the Site's timezone
    bare = s["root"].post(f"/api/orgs/{s['mon']}/locations", json={"name": "No TZ"}).json()
    assert s["root"].put(f"/api/locations/{bare['id']}/monitoring", json={"monitored": True}).status_code == 409
    assert s["root"].post(f"/api/locations/{bare['id']}/arm", json={"mode": "arm", "until": time.time() + 60, "reason": "x"}).status_code == 409

    # arming now: SOC operator yes, customer viewer no; at most 24 h; a reason
    arm = f"/api/locations/{lid}/arm"
    soon = time.time() + 3600
    assert s["view"].post(arm, json={"mode": "disarm", "until": soon, "reason": "cleaners"}).status_code == 403
    assert s["op"].post(arm, json={"mode": "disarm", "until": time.time() + 25 * 3600, "reason": "cleaners"}).status_code == 422
    assert s["op"].post(arm, json={"mode": "disarm", "until": time.time() - 1, "reason": "cleaners"}).status_code == 422
    assert s["op"].post(arm, json={"mode": "disarm", "until": soon}).status_code == 422
    r = s["op"].post(arm, json={"mode": "disarm", "until": soon, "reason": "cleaners on site"})
    assert r.status_code == 200, r.text
    m = r.json()
    assert (m["armed"], m["reason"], m["override_active"]) == (False, "override", True)
    assert m["arm_override"]["by"] == "op@monr.example" and m["arm_override"]["reason"] == "cleaners on site"
    # re-armed when the override ends, or later if the schedule is disarmed by then
    assert m["next_change"]["armed"] is True and m["next_change"]["at"] >= m["arm_override"]["until"]
    # stored on the Site row and audited in the customer's log
    assert db.one(sa.select(db.locations).where(db.locations.c.id == lid))["arm_override"]["mode"] == "disarm"
    acts = [a["action"] for a in s["root"].get(f"/api/audit?org={s['mon']}").json()]
    assert any(a.startswith("site disarmed until ") and a.endswith("Yard (cleaners on site)") for a in acts)
    assert "site monitoring updated: Yard" in acts and "site monitoring enabled: Yard" in acts
    assert s["view"].delete(arm).status_code == 403
    m = s["adm"].delete(arm).json()
    assert m["arm_override"] is None and m["reason"] != "override"
    assert "site arm override cleared: Yard" in [a["action"] for a in s["root"].get(f"/api/audit?org={s['mon']}").json()]
    # a Site that isn't monitored can't be armed
    s["root"].put(url, json={"monitored": False})
    assert s["root"].post(arm, json={"mode": "arm", "until": soon, "reason": "x"}).status_code == 409
    assert s["root"].get(url).json()["reason"] == "unmonitored"
