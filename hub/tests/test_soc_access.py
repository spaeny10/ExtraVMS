"""SOC supervisors are widened to admin in monitored customers so they can configure the Site's monitoring,
contacts, procedures and servers, but not manage the customer itself: members, invites, the audit log and deleting
Sites need a real membership. /auth/me tells the UI who is SOC-only and where a user is a real member, and Site
rollups say which Sites are monitored."""
from hub import auth
from test_soc_roles import PW, soc_setup

REFUSED = "SOC staff cannot manage customer members"


def test_soc_supervisor_cannot_manage_customer(client, superuser):
    s = soc_setup(client, superuser, "mgmt")
    sup, mon, loc = s["sup"], s["mon"], s["loc"]
    for r in (sup.post(f"/api/orgs/{mon}/members", json={"email": "friend@mgmt.example", "role": "admin", "password": PW}),
              sup.get(f"/api/orgs/{mon}/members"),
              sup.post(f"/api/orgs/{mon}/invites", json={"email": "friend@mgmt.example", "role": "viewer"}),
              sup.get(f"/api/orgs/{mon}/invites"),
              sup.get(f"/api/audit?org={mon}"),
              sup.delete(f"/api/locations/{loc['id']}")):
        assert r.status_code == 403 and r.json()["detail"] == REFUSED, r.text
    adm_id = auth.user_by_email("adm@mgmt.example")["id"]
    assert sup.put(f"/api/orgs/{mon}/members/{adm_id}/access", json={"all_sites": False, "location_ids": []}).status_code == 403
    assert sup.delete(f"/api/orgs/{mon}/members/{adm_id}").status_code == 403
    assert auth.user_by_email("friend@mgmt.example") is None   # refused before anything was created

    # Site configuration stays theirs
    r = sup.put(f"/api/locations/{loc['id']}/monitoring", json={"arm_schedule": [{"dow": [0, 1, 2, 3, 4], "from": "18:00", "to": "06:00"}]})
    assert r.status_code == 200 and r.json()["arm_schedule"][0]["from"] == "18:00"
    assert sup.put(f"/api/locations/{loc['id']}/contacts", json={"contacts": [{"name": "Keyholder"}]}).status_code == 200

    # the customer's own admin is unaffected, and a SOC supervisor who is also a real admin of the customer may
    assert s["adm"].get(f"/api/orgs/{mon}/members").status_code == 200
    s["root"].post(f"/api/orgs/{mon}/members", json={"email": "sup@mgmt.example", "role": "admin"})
    assert sup.get(f"/api/orgs/{mon}/members").status_code == 200
    # ... but a real viewer membership doesn't let the SOC widening reach member management
    s["root"].post(f"/api/orgs/{mon}/members", json={"email": "sup@mgmt.example", "role": "viewer"})
    assert sup.get(f"/api/orgs/{mon}/members").json()["detail"] == REFUSED
    assert s["root"].get(f"/api/orgs/{mon}/members").status_code == 200   # hub administrators: unchanged


def test_me_soc_only_member_and_monitored(client, superuser):
    s = soc_setup(client, superuser, "meflags")
    me = s["op"].get("/auth/me").json()
    assert me["user"]["soc_only"] is True
    org = next(o for o in me["orgs"] if o["id"] == s["mon"])
    assert org["member"] is False and org["soc"] is True
    me = s["adm"].get("/auth/me").json()
    assert me["user"]["soc_only"] is False and next(o for o in me["orgs"] if o["id"] == s["mon"])["member"] is True
    root = s["root"].get("/auth/me").json()
    assert root["user"]["soc_only"] is False and next(o for o in root["orgs"] if o["id"] == s["mon"])["member"] is True   # created it
    assert next(o for o in root["orgs"] if o["id"] == s["plain"])["member"] is True
    # Site rollups carry `monitored`
    locs = {l["id"]: l for l in s["adm"].get(f"/api/orgs/{s['mon']}/locations").json()}
    assert locs[s["loc"]["id"]]["monitored"] is True
    assert s["adm"].get(f"/api/locations/{s['loc']['id']}").json()["monitored"] is True
    fleet = s["root"].get(f"/api/fleet?org={s['plain']}").json()["orgs"][0]
    assert all(l["monitored"] is False for l in fleet["locations"])
