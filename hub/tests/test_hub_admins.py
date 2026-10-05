"""Hub administrators (users.is_super): the setsuper helper, the /api/hub/admins routes (403 for everyone else, 409
for the last one), hub-level audit rows, and /api/hub/sites across customers."""
import sqlalchemy as sa

from hub import auth, db
from test_access import _login, server


def _hub_rows():
    return db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id.is_(None)).order_by(db.audit_log.c.id))


def test_set_super_helper(client, superuser):
    auth.create_user("promote@example.com", "promote-pass-123")
    before = len(_hub_rows())
    u, changed = auth.set_super("Promote@Example.com ", True)
    assert changed and auth.user_by_email("promote@example.com")["is_super"]
    assert auth.set_super("promote@example.com", True)[1] is False
    rows = _hub_rows()
    assert len(rows) == before + 1   # an unchanged flag writes nothing
    assert rows[-1]["action"] == "hub admin granted: promote@example.com" and rows[-1]["user_email"] == "(command line)"
    _, changed = auth.set_super("promote@example.com", False)
    assert changed and not auth.user_by_email("promote@example.com")["is_super"]
    assert _hub_rows()[-1]["action"] == "hub admin revoked: promote@example.com"
    try:
        auth.set_super("nobody@example.com", True)
        raise AssertionError("expected LookupError")
    except LookupError:
        pass


def test_hub_admin_routes(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    auth.create_user("second@example.com", "second-pass-123")
    other = _login(client, "second@example.com", "second-pass-123")

    # not a hub administrator: every hub route is refused, and the Members-style pages never see the flag
    for path in ("/api/hub/admins", "/api/hub/audit", "/api/hub/sites"):
        assert other.get(path).status_code == 403
    assert other.post("/api/hub/admins", json={"email": "second@example.com"}).status_code == 403

    assert root.post("/api/hub/admins", json={"email": "ghost@example.com"}).status_code == 404
    r = root.post("/api/hub/admins", json={"email": "second@example.com"})
    assert r.status_code == 200 and r.json()["email"] == "second@example.com"
    admins = root.get("/api/hub/admins").json()
    assert {a["email"] for a in admins} >= {superuser["email"], "second@example.com"}
    assert set(admins[0]) == {"id", "email", "totp_enabled", "last_login_at"}
    sid = next(a["id"] for a in admins if a["email"] == "second@example.com")
    assert other.get("/auth/me").json()["user"]["is_super"] is True   # takes effect without signing in again

    # leaving only one administrator: `second` revokes root, then can't revoke themselves
    assert other.delete(f"/api/hub/admins/{superuser['id']}").status_code == 200
    assert root.get("/api/hub/admins").status_code == 403
    assert [a["email"] for a in other.get("/api/hub/admins").json()] == ["second@example.com"]
    r = other.delete(f"/api/hub/admins/{sid}")
    assert r.status_code == 409 and r.json()["detail"] == auth.LAST_ADMIN_MSG
    # restore root, then `second` may step down
    assert other.post("/api/hub/admins", json={"email": superuser["email"]}).status_code == 200
    assert other.delete(f"/api/hub/admins/{sid}").status_code == 200
    assert root.delete(f"/api/hub/admins/{sid}").status_code == 404   # no longer one

    acts = [r["action"] for r in root.get("/api/hub/audit").json()]
    assert "hub admin granted: second@example.com" in acts and f"hub admin revoked: {superuser['email']}" in acts
    # hub-level rows never leak into a customer's audit
    org = root.post("/api/orgs", json={"name": "Audit Iso", "slug": "audit-iso"}).json()
    assert not [r for r in root.get(f"/api/audit?org={org['id']}").json() if r["action"].startswith("hub admin")]


def test_hub_sites_across_customers(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    a = root.post("/api/orgs", json={"name": "Alpha Hub Co", "slug": "alpha-hub-co"}).json()
    b = root.post("/api/orgs", json={"name": "Beta Hub Co", "slug": "beta-hub-co"}).json()
    sa_ = server(a["id"], "Alpha North")
    sb = server(b["id"], "Beta South")
    retired = server(b["id"], "Beta Old")
    db.run(sa.update(db.sites).where(db.sites.c.id == retired["id"]).values(retired_at=1.0))
    out = root.get("/api/hub/sites").json()
    by = {g["org"]["id"]: g for g in out}
    assert set(by[a["id"]]) == {"org", "locations", "unassigned"} and by[a["id"]]["org"] == {"id": a["id"], "name": "Alpha Hub Co"}
    assert [loc["id"] for loc in by[a["id"]]["locations"]] == [sa_["location_id"]]
    assert by[a["id"]]["locations"] == root.get(f"/api/orgs/{a['id']}/locations").json()   # same rollup code
    beta = {loc["id"]: loc for loc in by[b["id"]]["locations"]}
    assert beta[sb["location_id"]]["servers_total"] == 1 and beta[retired["location_id"]]["servers"] == []
    with_retired = {g["org"]["id"]: g for g in root.get("/api/hub/sites?include_retired=true").json()}
    assert [s["id"] for s in {loc["id"]: loc for loc in with_retired[b["id"]]["locations"]}[retired["location_id"]]["servers"]] == [retired["id"]]
    names = [g["org"]["name"] for g in out]
    assert names == sorted(names)   # customers by name, like the picker
    # /auth/me lists every customer for a hub administrator (the picker), memberships or not
    assert {a["id"], b["id"]} <= {o["id"] for o in root.get("/auth/me").json()["orgs"]}
