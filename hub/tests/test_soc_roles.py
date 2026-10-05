"""SOC staff (users.soc_role): the set_soc_role helper and its hub-level audit rows, /api/hub/soc/members (hub
administrators only), and membership() widening: SOC staff act in customers with a monitored Site (operator for
operators, admin for supervisors, every Site) and nowhere else."""
import sqlalchemy as sa

from hub import auth, db, soc
from test_access import _login, server

PW = "soc-pass-12345"


def _hub_rows():
    return db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id.is_(None)).order_by(db.audit_log.c.id))


def soc_setup(client, superuser, slug):
    """Customer `mon` with a monitored Site (timezone set) holding one server, customer `plain` with none, a SOC
    operator and a SOC supervisor (no memberships), and a customer admin and viewer of `mon`."""
    root = _login(client, superuser["email"], superuser["password"])
    mon = root.post("/api/orgs", json={"name": f"{slug} Mon", "slug": f"{slug}-mon"}).json()["id"]
    plain = root.post("/api/orgs", json={"name": f"{slug} Plain", "slug": f"{slug}-plain"}).json()["id"]
    loc = root.post(f"/api/orgs/{mon}/locations", json={"name": "Yard", "timezone": "America/Chicago"}).json()
    srv = server(mon, "Yard NVR", loc["id"])
    other = server(plain, "Plain NVR")
    r = root.put(f"/api/locations/{loc['id']}/monitoring", json={"monitored": True})
    assert r.status_code == 200 and r.json()["monitored"] is True, r.text
    for who, role in (("op", "operator"), ("sup", "supervisor")):
        auth.create_user(f"{who}@{slug}.example", PW)
        auth.set_soc_role(f"{who}@{slug}.example", role)
    for who, role in (("adm", "admin"), ("view", "viewer")):
        root.post(f"/api/orgs/{mon}/members", json={"email": f"{who}@{slug}.example", "role": role, "password": PW})
    clients = {k: _login(client, f"{k}@{slug}.example", PW) for k in ("op", "sup", "adm", "view")}
    return {"root": root, "mon": mon, "plain": plain, "loc": loc, "srv": srv, "other": other, **clients}


def test_set_soc_role_helper(client, superuser):
    auth.create_user("socstaff@example.com", "socstaff-pass-1")
    before = len(_hub_rows())
    u, changed = auth.set_soc_role(" SocStaff@Example.com", "operator")
    assert changed and auth.user_by_email("socstaff@example.com")["soc_role"] == "operator"
    assert auth.set_soc_role("socstaff@example.com", "operator")[1] is False   # unchanged: no row
    auth.set_soc_role("socstaff@example.com", "supervisor")
    auth.set_soc_role("socstaff@example.com", None)
    rows = _hub_rows()[before:]
    assert [r["action"] for r in rows] == ["soc operator granted: socstaff@example.com", "soc supervisor granted: socstaff@example.com",
                                           "soc role revoked: socstaff@example.com"]
    assert all(r["org_id"] is None and r["user_email"] == "(command line)" for r in rows)
    assert rows[1]["detail"] == {"target_user_id": u["id"], "from": "operator", "to": "supervisor"}
    for bad, exc in ((("socstaff@example.com", "owner"), ValueError), (("nobody@example.com", "operator"), LookupError)):
        try:
            auth.set_soc_role(*bad)
            raise AssertionError(f"expected {exc.__name__}")
        except exc:
            pass
    assert auth.soc_level({"is_super": True}) == "supervisor" and auth.soc_level({"soc_role": "operator"}) == "operator"
    assert auth.soc_level({"soc_role": "owner"}) is None and auth.soc_level(None) is None


def test_soc_member_routes(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    auth.create_user("socroute@example.com", "socroute-pass-1")
    other = _login(client, "socroute@example.com", "socroute-pass-1")
    assert other.get("/api/hub/soc/members").status_code == 403
    assert other.post("/api/hub/soc/members", json={"email": "socroute@example.com", "role": "supervisor"}).status_code == 403
    assert root.post("/api/hub/soc/members", json={"email": "ghost@example.com", "role": "operator"}).status_code == 404
    assert root.post("/api/hub/soc/members", json={"email": "socroute@example.com", "role": "owner"}).status_code == 422
    r = root.post("/api/hub/soc/members", json={"email": "socroute@example.com", "role": "operator"})
    assert r.status_code == 200 and r.json()["soc_role"] == "operator"
    assert set(r.json()) == {"id", "email", "soc_role", "totp_enabled", "last_login_at"}
    members = root.get("/api/hub/soc/members").json()
    me = next(m for m in members if m["email"] == "socroute@example.com")
    assert set(me) == {"id", "email", "soc_role", "totp_enabled", "last_login_at"} and me["soc_role"] == "operator"
    assert other.get("/auth/me").json()["user"]["soc_role"] == "operator"   # at once, no new sign-in
    # an operator still can't manage SOC staff
    assert other.delete(f"/api/hub/soc/members/{me['id']}").status_code == 403
    assert root.post("/api/hub/soc/members", json={"email": "socroute@example.com", "role": "supervisor"}).json()["soc_role"] == "supervisor"
    assert root.delete(f"/api/hub/soc/members/{me['id']}").json() == {"ok": True}
    assert root.delete(f"/api/hub/soc/members/{me['id']}").status_code == 404   # no longer SOC staff
    assert root.delete("/api/hub/soc/members/u_nope").status_code == 404
    assert other.get("/auth/me").json()["user"]["soc_role"] is None
    acts = [r["action"] for r in root.get("/api/hub/audit").json()]
    assert {"soc operator granted: socroute@example.com", "soc supervisor granted: socroute@example.com",
            "soc role revoked: socroute@example.com"} <= set(acts)


def test_membership_widening_only_for_monitored_customers(client, superuser):
    s = soc_setup(client, superuser, "widen")
    op, sup = auth.user_by_email("op@widen.example"), auth.user_by_email("sup@widen.example")
    assert auth.membership(op, s["mon"]) == {"role": "operator", "all_sites": True, "soc": True}
    assert auth.membership(sup, s["mon"]) == {"role": "admin", "all_sites": True, "soc": True}
    assert auth.membership(op, s["plain"]) is None and auth.membership(sup, s["plain"]) is None
    # a real member of the customer keeps the wider of the two
    view = auth.user_by_email("view@widen.example")
    assert auth.membership(view, s["mon"]) == {"role": "viewer", "all_sites": True}   # not SOC staff: unchanged
    auth.set_soc_role("adm@widen.example", "operator")   # a customer admin who is also a SOC operator stays admin
    assert auth.membership(auth.user_by_email("adm@widen.example"), s["mon"]) == {"role": "admin", "all_sites": True, "soc": True}
    auth.set_soc_role("adm@widen.example", None)
    assert auth.membership(auth.user_by_email("adm@widen.example"), s["mon"]) == {"role": "admin", "all_sites": True}

    # /auth/me lists the monitored customer (with the SOC role) and not the other
    me = s["op"].get("/auth/me").json()
    orgs = {o["id"]: o for o in me["orgs"]}
    assert me["user"]["soc_role"] == "operator"
    assert orgs[s["mon"]]["role"] == "operator" and orgs[s["mon"]]["soc"] is True and s["plain"] not in orgs
    assert {o["id"]: o["role"] for o in s["sup"].get("/api/orgs").json()}[s["mon"]] == "admin"

    # the proxy: allowed through to a monitored customer's server (offline here, so 503 from past the access check),
    # refused for someone with no membership and for the customer without a monitored Site
    assert s["op"].get(f"/s/{s['srv']['id']}/api/json").status_code == 503
    assert s["op"].get(f"/s/{s['srv']['id']}/api/turn").status_code == 200
    assert s["op"].get(f"/s/{s['other']['id']}/api/json").status_code == 403
    auth.create_user("nobody@widen.example", PW)
    stranger = _login(client, "nobody@widen.example", PW)
    assert stranger.get(f"/s/{s['srv']['id']}/api/json").status_code == 403
    # operators act (PTZ), but configuring cameras needs admin: refused before the tunnel
    assert s["op"].post(f"/s/{s['srv']['id']}/api/cameras/cam1/ptz/move", json={}).status_code == 503
    assert s["op"].put(f"/s/{s['srv']['id']}/api/cameras/cam1", json={}).status_code == 403
    assert s["sup"].put(f"/s/{s['srv']['id']}/api/cameras/cam1", json={}).status_code == 503
    # the fleet and the Site show up for them
    assert [x["id"] for x in s["op"].get(f"/api/fleet?org={s['mon']}").json()["orgs"][0]["sites"]] == [s["srv"]["id"]]

    # monitoring off: the SOC's access goes with it
    assert s["root"].put(f"/api/locations/{s['loc']['id']}/monitoring", json={"monitored": False}).json()["monitored"] is False
    assert auth.membership(op, s["mon"]) is None
    assert s["op"].get(f"/s/{s['srv']['id']}/api/json").status_code == 403
    assert s["mon"] not in {o["id"] for o in s["op"].get("/auth/me").json()["orgs"]}
    # and a direct database write is picked up within the cache TTL (here: at once, after invalidate)
    db.run(sa.update(db.locations).where(db.locations.c.id == s["loc"]["id"]).values(monitored=True))
    soc.invalidate()
    assert auth.membership(op, s["mon"])["soc"] is True


def test_dispositions_catalogue(client, superuser):
    s = soc_setup(client, superuser, "dispo")
    assert s["view"].get("/api/soc/dispositions").status_code == 403
    assert s["adm"].get("/api/soc/dispositions").status_code == 403   # a customer admin is not SOC staff
    out = s["op"].get("/api/soc/dispositions").json()
    assert s["root"].get("/api/soc/dispositions").json() == out        # hub administrators are supervisors
    assert [g["id"] for g in out["groups"]] == ["true_alarm", "false_alarm", "not_actionable"]
    assert [g["key"] for g in out["groups"]] == ["t", "f", "n"]
    codes = {d["code"]: d for g in out["groups"] for d in g["dispositions"]}
    assert set(codes) == set(soc.DISPOSITIONS)
    assert codes["true_alarm_dispatched"]["needs_notes"] is True and codes["true_alarm_dispatched"]["four_eyes"] == ["high", "medium", "low"]
    assert codes["no_action"]["four_eyes"] == ["high"] and codes["false_alarm"]["four_eyes"] == []
    assert codes["swept"]["selectable"] is False and codes["swept"]["key"] is None
    assert [d["key"] for d in out["groups"][1]["dispositions"]] == ["1", "2", "3", "4"]
    assert set(codes["false_alarm"]) == {"code", "label", "needs_notes", "four_eyes", "selectable", "key"}
