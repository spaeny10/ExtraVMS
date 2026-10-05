"""A database from before Sites (locations) upgrades in place: upgrade() adds the columns, backfill() gives every
server its own one-server Site, turns legacy server grants into Site grants, then drops the legacy site_grants table,
and seeds the cameras registry. Running both twice changes nothing (every hub start runs them)."""
import sqlalchemy as sa

from hub import db

OLD_SCHEMA = [
    "CREATE TABLE orgs (id VARCHAR(24) PRIMARY KEY, name VARCHAR(120) NOT NULL, slug VARCHAR(64) NOT NULL UNIQUE, created_at FLOAT NOT NULL,"
    " branding JSON, ai_shared BOOLEAN NOT NULL)",
    "CREATE TABLE users (id VARCHAR(24) PRIMARY KEY, email VARCHAR(200) NOT NULL UNIQUE, password_hash TEXT NOT NULL, totp_secret VARCHAR(64),"
    " totp_enabled BOOLEAN NOT NULL, is_super BOOLEAN NOT NULL, created_at FLOAT NOT NULL, last_login_at FLOAT)",
    "CREATE TABLE memberships (user_id VARCHAR(24) NOT NULL, org_id VARCHAR(24) NOT NULL, role VARCHAR(16) NOT NULL, PRIMARY KEY (user_id, org_id))",
    "CREATE TABLE site_grants (user_id VARCHAR(24) NOT NULL, site_id VARCHAR(24) NOT NULL, PRIMARY KEY (user_id, site_id))",
    "CREATE TABLE sites (id VARCHAR(24) PRIMARY KEY, org_id VARCHAR(24) NOT NULL, name VARCHAR(120) NOT NULL, location VARCHAR(200) NOT NULL,"
    " token_hash VARCHAR(64) NOT NULL, token_prev_hash VARCHAR(64), token_rotated_at FLOAT, created_at FLOAT NOT NULL, last_seen_at FLOAT,"
    " online BOOLEAN NOT NULL, version VARCHAR(32), summary JSON, clock_skew_s FLOAT, agent_ip VARCHAR(64), hostname VARCHAR(120), retired_at FLOAT)",
    "CREATE INDEX ix_sites_org_id ON sites (org_id)",
    "CREATE TABLE invites (code VARCHAR(48) PRIMARY KEY, org_id VARCHAR(24) NOT NULL, email VARCHAR(200) NOT NULL, role VARCHAR(16) NOT NULL,"
    " expires_at FLOAT NOT NULL, accepted_at FLOAT)",
    "CREATE TABLE kv (key VARCHAR(64) PRIMARY KEY, value JSON NOT NULL)",
]


def _old_db(path):
    eng = sa.create_engine(f"sqlite:///{path.as_posix()}", future=True)
    with eng.begin() as c:
        for ddl in OLD_SCHEMA:
            c.execute(sa.text(ddl))
        c.execute(sa.text("INSERT INTO orgs VALUES ('o_1', 'Jetstream', 'jetstream', 1, NULL, 0)"))
        for uid in ("u_owner", "u_limited", "u_plain"):
            c.execute(sa.text(f"INSERT INTO users VALUES ('{uid}', '{uid}@x.example', 'h', NULL, 0, 0, 1, NULL)"))
            c.execute(sa.text(f"INSERT INTO memberships VALUES ('{uid}', 'o_1', 'viewer')"))
        summary = '{"cameras": [{"id": "cam1", "name": "Yard", "stream_ready": true, "problems": []}, {"id": "cam2", "name": "Gate"}]}'
        for i, (sid, name, loc) in enumerate([("s_a", "HQ", "Austin"), ("s_b", "Warehouse", ""), ("s_c", "Yard", "Dallas"), ("s_d", "hq", "")]):
            c.execute(sa.text("INSERT INTO sites (id, org_id, name, location, token_hash, created_at, online, summary) "
                              "VALUES (:id, 'o_1', :name, :loc, 'x', :t, 0, :summ)"),
                      {"id": sid, "name": name, "loc": loc, "t": i, "summ": summary if sid == "s_a" else None})
        # the limited user had two servers granted; a grant on another org's server must not count
        c.execute(sa.text("INSERT INTO site_grants VALUES ('u_limited', 's_a'), ('u_limited', 's_c'), ('u_plain', 's_elsewhere')"))
    return eng


def _snapshot(eng):
    with eng.connect() as c:
        return {t: sorted(tuple(r) for r in c.execute(sa.select(db.metadata.tables[t])))
                for t in ("locations", "location_grants", "cameras", "memberships", "sites")}


def test_old_database_upgrades_and_backfills(tmp_path):
    eng = _old_db(tmp_path / "old.db")
    for _ in range(2):
        db.metadata.create_all(eng)
        db.upgrade(eng)
        db.backfill(eng)
    first = _snapshot(eng)
    db.metadata.create_all(eng)
    db.upgrade(eng)
    db.backfill(eng)
    assert _snapshot(eng) == first   # idempotent

    with eng.connect() as c:
        servers = {r.id: r for r in c.execute(sa.select(db.sites))}
        locs = {r.id: r for r in c.execute(sa.select(db.locations))}
        # one Site per server, named after it (unique, case-insensitively), address from sites.location
        assert len(locs) == 4 and all(s.location_id in locs for s in servers.values())
        by_server = {sid: locs[s.location_id] for sid, s in servers.items()}
        assert by_server["s_a"].name == "HQ" and by_server["s_a"].address == "Austin"
        assert by_server["s_d"].name == "hq 2"
        assert by_server["s_c"].address == "Dallas" and all(loc.org_id == "o_1" for loc in locs.values())
        # grants: the limited user sees the two Sites of their servers; everyone else every Site
        flags = {r.user_id: r.all_sites for r in c.execute(sa.select(db.memberships))}
        assert flags == {"u_owner": True, "u_limited": False, "u_plain": True}
        grants = {(r.user_id, r.location_id) for r in c.execute(sa.select(db.location_grants))}
        assert grants == {("u_limited", servers["s_a"].location_id), ("u_limited", servers["s_c"].location_id)}
        # cameras registry seeded from the last summary
        cams = {(r.server_id, r.camera_id): r for r in c.execute(sa.select(db.cameras))}
        assert set(cams) == {("s_a", "cam1"), ("s_a", "cam2")}
        assert cams[("s_a", "cam1")].name == "Yard" and cams[("s_a", "cam1")].stream_ready is True
        assert cams[("s_a", "cam1")].location_id == servers["s_a"].location_id and cams[("s_a", "cam1")].enabled is True
        assert c.execute(sa.select(db.kv.c.key).where(db.kv.c.key == db.TENANCY_V2)).first() is not None
        # the legacy table is gone once its rows were migrated, and the drop is recorded so it never runs again
        assert "site_grants" not in sa.inspect(c).get_table_names()
        assert c.execute(sa.select(db.kv.c.value).where(db.kv.c.key == db.TENANCY_V2_DROP_SITE_GRANTS)).scalar()["existed"] is True
        # the new NOT NULL column has a database default (older code inserts memberships without it)
        c.execute(sa.text("INSERT INTO memberships (user_id, org_id, role) VALUES ('u_new', 'o_1', 'viewer')"))
        assert c.execute(sa.text("SELECT all_sites FROM memberships WHERE user_id = 'u_new'")).scalar() in (1, True)
        # indexes added for the new column
        assert "ix_sites_location_id" in {i["name"] for i in sa.inspect(c).get_indexes("sites")}
        assert {"all_sites", "location_ids", "label"} <= {col["name"] for col in sa.inspect(c).get_columns("invites")}


def test_grants_migration_runs_once(tmp_path):
    """After the kv marker exists, a later start must not re-derive access from site_grants (admins may have
    changed it since through the new API)."""
    eng = _old_db(tmp_path / "once.db")
    db.metadata.create_all(eng)
    db.upgrade(eng)
    db.backfill(eng)
    with eng.begin() as c:
        c.execute(sa.update(db.memberships).where(db.memberships.c.user_id == "u_limited").values(all_sites=True))
        c.execute(sa.delete(db.location_grants))
    db.backfill(eng)
    with eng.connect() as c:
        assert c.execute(sa.select(db.memberships.c.all_sites).where(db.memberships.c.user_id == "u_limited")).scalar() is True
        assert c.execute(sa.select(sa.func.count()).select_from(db.location_grants)).scalar() == 0


def test_site_grants_dropped_only_after_migration(tmp_path):
    """A database whose grants were already migrated (a 0.1.x tenancy v2 hub) drops the leftover table on the next
    start without re-reading it; a fresh database never had it and still records the step once."""
    eng = _old_db(tmp_path / "migrated.db")
    with eng.begin() as c:   # as a 0.1.x tenancy v2 hub left it: marker set, legacy table still there
        c.execute(sa.text("INSERT INTO kv VALUES ('schema:tenancy_v2', '{\"at\": 1}')"))
    db.metadata.create_all(eng)
    db.upgrade(eng)
    db.backfill(eng)
    with eng.connect() as c:
        assert "site_grants" not in sa.inspect(c).get_table_names()
        assert c.execute(sa.select(sa.func.count()).select_from(db.location_grants)).scalar() == 0   # not re-migrated

    fresh = sa.create_engine(f"sqlite:///{(tmp_path / 'fresh.db').as_posix()}", future=True)
    db.metadata.create_all(fresh)
    db.upgrade(fresh)
    db.backfill(fresh)
    db.backfill(fresh)
    with fresh.connect() as c:
        assert "site_grants" not in sa.inspect(c).get_table_names()
        assert c.execute(sa.select(db.kv.c.value).where(db.kv.c.key == db.TENANCY_V2_DROP_SITE_GRANTS)).scalar()["existed"] is False


# a 0.2.0 database: users and locations as they shipped there, before the SOC columns
V020_SCHEMA = [
    "CREATE TABLE users (id VARCHAR(24) PRIMARY KEY, email VARCHAR(200) NOT NULL UNIQUE, password_hash TEXT NOT NULL, totp_secret VARCHAR(64),"
    " totp_enabled BOOLEAN NOT NULL, is_super BOOLEAN NOT NULL, created_at FLOAT NOT NULL, last_login_at FLOAT)",
    "CREATE TABLE locations (id VARCHAR(24) PRIMARY KEY, org_id VARCHAR(24) NOT NULL, name VARCHAR(120) NOT NULL, address VARCHAR(200) NOT NULL,"
    " timezone VARCHAR(64), notes TEXT, created_at FLOAT NOT NULL, updated_at FLOAT NOT NULL)",
    "CREATE INDEX ix_locations_org_id ON locations (org_id)",
    "INSERT INTO users VALUES ('u_1', 'a@x.example', 'h', NULL, 0, 1, 1, NULL)",
    "INSERT INTO locations VALUES ('l_1', 'o_1', 'HQ', '', 'America/Chicago', NULL, 1, 1)",
]
SOC_TABLES = {"location_contacts", "location_procedures", "incidents", "incident_events", "incident_log", "soc_presence", "soc_reports"}


def test_soc_schema_is_additive(tmp_path):
    """The SOC columns arrive through ADDED_COLUMNS (existing Sites unmonitored, existing users no SOC role) and the
    SOC tables through create_all; older code's inserts (without the new columns) still work."""
    eng = sa.create_engine(f"sqlite:///{(tmp_path / 'v020.db').as_posix()}", future=True)
    with eng.begin() as c:
        for ddl in V020_SCHEMA:
            c.execute(sa.text(ddl))
    for _ in range(2):   # every start runs these
        db.metadata.create_all(eng)
        db.upgrade(eng)
        db.backfill(eng)
    insp = sa.inspect(eng)
    assert SOC_TABLES <= set(insp.get_table_names())
    assert {"monitored", "arm_schedule", "arm_holidays", "arm_override", "soc_group_minutes"} <= {c["name"] for c in insp.get_columns("locations")}
    assert "soc_role" in {c["name"] for c in insp.get_columns("users")}
    with eng.begin() as c:
        loc = c.execute(sa.select(db.locations).where(db.locations.c.id == "l_1")).mappings().one()
        assert loc["monitored"] in (0, False) and loc["arm_schedule"] is None and loc["arm_override"] is None
        assert c.execute(sa.select(db.users.c.soc_role).where(db.users.c.id == "u_1")).scalar() is None
        # 0.2.0's insert (no monitored) gets the database default
        c.execute(sa.text("INSERT INTO locations (id, org_id, name, address, created_at, updated_at) VALUES ('l_2', 'o_1', 'Depot', '', 1, 1)"))
        assert c.execute(sa.text("SELECT monitored FROM locations WHERE id = 'l_2'")).scalar() in (0, False)
    # re-publishing the same site event can't add a second incident_events row
    row = {"incident_id": 1, "server_id": "s_1", "event_id": "42"}
    with eng.begin() as c:
        c.execute(db.incident_events.insert().values(**row))
    try:
        with eng.begin() as c:
            c.execute(db.incident_events.insert().values(**row, camera_id="cam2"))
        raise AssertionError("expected a unique violation")
    except sa.exc.IntegrityError:
        pass
    # the old-database path gets the column too
    old = _old_db(tmp_path / "old-soc.db")
    db.metadata.create_all(old)
    db.upgrade(old)
    assert "soc_role" in {c["name"] for c in sa.inspect(old).get_columns("users")}
