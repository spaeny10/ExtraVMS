"""Hub database (SQLAlchemy Core, Postgres in production, SQLite for dev and tests).

Glossary (code name -> what the UI calls it):
  orgs            Customer. Users belong through `memberships` (one role per customer).
  locations       Site: a physical place, ids "l_...", HTTP /api/locations/... . Groups one or more servers.
  sites           Server: one NVR box with one device token, one tunnel and one /s/<id>/ URL. The table and the
                  /api/sites/{id} routes keep their historical name (alias `servers`, routes /api/servers/{id});
                  every `site_id` in tables, the agent protocol (welcome.site_id, x-hub-site), dashboards and camera
                  group JSON means a server id.
  cameras         Camera: the hub's registry of (server_id, camera_id), synced from hello/heartbeats (cameras.py).
  incidents       SOC incident: one or more events at a monitored Site that the SOC handles (soc.py);
                  location_contacts and location_procedures are a Site's call list and SOP checklists.
Access: a membership's role plus `all_sites` (sees every Site of the customer) or, when false, only the Sites in
`location_grants` (no grants = nothing). The legacy `site_grants` table (per-server grants, "no grants = all") is
not in this metadata any more: backfill() reads it once (when an older database first meets this code) and then
drops it.
Servers authenticate with a device token (hashed at rest). Alerts and the audit log are per org (audit rows with
org_id NULL are hub-level: hub administrators and SOC staff granted/revoked, read by /api/hub/audit). Small,
synchronous calls: a fleet of dozens of servers is a few writes a second.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid

import sqlalchemy as sa
from sqlalchemy import Boolean, Column, Float, Integer, MetaData, String, Table, Text
from sqlalchemy.engine import Engine

from .config import settings

metadata = MetaData()

orgs = Table("orgs", metadata,
             Column("id", String(24), primary_key=True), Column("name", String(120), nullable=False),
             Column("slug", String(64), unique=True, nullable=False), Column("created_at", Float, nullable=False),
             Column("branding", sa.JSON, nullable=True), Column("ai_shared", Boolean, nullable=False, default=False))
users = Table("users", metadata,
              Column("id", String(24), primary_key=True), Column("email", String(200), unique=True, nullable=False),
              Column("password_hash", Text, nullable=False), Column("totp_secret", String(64), nullable=True),
              Column("totp_enabled", Boolean, nullable=False, default=False), Column("is_super", Boolean, nullable=False, default=False),
              Column("created_at", Float, nullable=False), Column("last_login_at", Float, nullable=True),
              # SOC staff (None | operator | supervisor): hub-level like is_super, scoped to customers with a monitored
              # Site (auth.membership). One internal SOC, so a column; a soc_members table only if several SOCs exist.
              Column("soc_role", String(16), nullable=True),
              # the last TOTP time step used to sign in or re-authenticate: each code works once (auth.consume_totp_step)
              Column("totp_last_step", sa.BigInteger, nullable=True))
memberships = Table("memberships", metadata,
                    Column("user_id", String(24), primary_key=True), Column("org_id", String(24), primary_key=True),
                    Column("role", String(16), nullable=False),
                    Column("all_sites", Boolean, nullable=False, default=True, server_default=sa.text("true")))
sites = Table("sites", metadata,
              Column("id", String(24), primary_key=True), Column("org_id", String(24), nullable=False, index=True),
              Column("name", String(120), nullable=False), Column("location", String(200), nullable=False, default=""),
              Column("token_hash", String(64), nullable=False, index=True), Column("token_prev_hash", String(64), nullable=True),
              Column("token_rotated_at", Float, nullable=True), Column("created_at", Float, nullable=False),
              Column("last_seen_at", Float, nullable=True), Column("online", Boolean, nullable=False, default=False),
              Column("version", String(32), nullable=True), Column("summary", sa.JSON, nullable=True),
              Column("clock_skew_s", Float, nullable=True), Column("agent_ip", String(64), nullable=True),
              Column("hostname", String(120), nullable=True),
              Column("retired_at", Float, nullable=True),   # fleet actions: hidden from Fleet/Home, tunnel left alone
              Column("location_id", String(24), nullable=True, index=True))   # the Site this server belongs to
servers = sites   # the table holds servers; new code says so
locations = Table("locations", metadata,
                  Column("id", String(24), primary_key=True), Column("org_id", String(24), nullable=False, index=True),
                  Column("name", String(120), nullable=False), Column("address", String(200), nullable=False, default=""),
                  Column("timezone", String(64), nullable=True), Column("notes", Text, nullable=True),
                  Column("created_at", Float, nullable=False), Column("updated_at", Float, nullable=False),
                  # SOC monitoring (soc.py). Nothing happens until a customer opts a Site in (monitored = true).
                  Column("monitored", Boolean, nullable=False, default=False, server_default=sa.text("false")),
                  Column("arm_schedule", sa.JSON, nullable=True),    # [{dow: [0..6, Monday=0], from: "HH:MM", to: "HH:MM"}]
                  Column("arm_holidays", sa.JSON, nullable=True),    # [{date: "YYYY-MM-DD", name, armed, from?, to?}]
                  Column("arm_override", sa.JSON, nullable=True),    # {mode: arm|disarm, until, by, by_id, reason, at}
                  Column("soc_group_minutes", Integer, nullable=True),   # events within this window join one incident
                  # where the Site is (geocode.py): `address` stays the one-line address people read; these locate it
                  Column("lat", Float, nullable=True), Column("lon", Float, nullable=True),
                  Column("address_parts", sa.JSON, nullable=True),   # {house_number, street, city, county, state, state_code, postcode, country, country_code, display_name}
                  Column("geocoded_at", Float, nullable=True), Column("geocode_source", String(32), nullable=True))   # nominatim | geocoder | marker | manual
location_grants = Table("location_grants", metadata,
                        Column("user_id", String(24), primary_key=True), Column("location_id", String(24), primary_key=True))
cameras = Table("cameras", metadata,
                Column("server_id", String(24), primary_key=True), Column("camera_id", String(64), primary_key=True),
                Column("org_id", String(24), nullable=False, index=True),
                Column("location_id", String(24), nullable=True, index=True),   # denormalized from the server (cameras.relocate)
                Column("name", String(120), nullable=False, default=""), Column("enabled", Boolean, nullable=False, default=True),
                Column("stream_ready", Boolean, nullable=True), Column("problems", sa.JSON, nullable=True),
                Column("bitrate_mbps", Float, nullable=True), Column("ptz", sa.JSON, nullable=True),
                Column("metadata", Boolean, nullable=True), Column("onvif_events", Boolean, nullable=True),
                Column("first_seen_at", Float, nullable=False), Column("last_seen_at", Float, nullable=True),
                Column("missing_since", Float, nullable=True),   # gone from the server's list (rows are never deleted while the server exists)
                Column("source", String(16), nullable=True))     # hello | heartbeat | summary (backfill)
claims = Table("claims", metadata,
               Column("code", String(16), primary_key=True), Column("hint", sa.JSON, nullable=True),
               Column("agent_ip", String(64), nullable=True), Column("first_seen_at", Float, nullable=False),
               Column("expires_at", Float, nullable=False), Column("consumed_site_id", String(24), nullable=True))
sessions = Table("sessions", metadata,
                 Column("id", String(64), primary_key=True), Column("user_id", String(24), nullable=False, index=True),
                 Column("org_id", String(24), nullable=True), Column("created_at", Float, nullable=False),
                 Column("expires_at", Float, nullable=False), Column("ip", String(64), nullable=True), Column("ua", String(300), nullable=True))
invites = Table("invites", metadata,
                Column("code", String(48), primary_key=True), Column("org_id", String(24), nullable=False),
                Column("email", String(200), nullable=False), Column("role", String(16), nullable=False),
                Column("expires_at", Float, nullable=False), Column("accepted_at", Float, nullable=True),
                Column("all_sites", Boolean, nullable=False, default=True, server_default=sa.text("true")),
                Column("location_ids", sa.JSON, nullable=True), Column("created_by", String(24), nullable=True),
                Column("created_at", Float, nullable=True), Column("accepted_user_id", String(24), nullable=True),
                Column("label", String(120), nullable=True))
alerts = Table("alerts", metadata,
               Column("id", Integer, primary_key=True, autoincrement=True), Column("org_id", String(24), nullable=False, index=True),
               Column("site_id", String(24), nullable=False, index=True), Column("kind", String(32), nullable=False),
               Column("key", String(120), nullable=False, default=""), Column("opened_at", Float, nullable=False),
               Column("closed_at", Float, nullable=True), Column("acked_by", String(24), nullable=True),
               Column("acked_at", Float, nullable=True), Column("detail", sa.JSON, nullable=True))
vlm_usage = Table("vlm_usage", metadata,
                  Column("id", Integer, primary_key=True, autoincrement=True), Column("ts", Float, nullable=False, index=True),
                  Column("site_id", String(24), nullable=False), Column("org_id", String(24), nullable=False, index=True),
                  Column("model", String(120), nullable=True), Column("task", String(40), nullable=True),
                  Column("prompt_tokens", Integer, nullable=False, default=0), Column("completion_tokens", Integer, nullable=False, default=0),
                  Column("latency_ms", Integer, nullable=True), Column("status", Integer, nullable=True),
                  Column("images", Integer, nullable=False, default=0), Column("streamed", Boolean, nullable=False, default=False))
digests = Table("digests", metadata,
                Column("id", Integer, primary_key=True, autoincrement=True), Column("org_id", String(24), nullable=False, index=True),
                Column("day", String(10), nullable=False), Column("created_at", Float, nullable=False), Column("text", Text, nullable=False),
                Column("data", sa.JSON, nullable=True), Column("model", String(120), nullable=True))
config_backups = Table("config_backups", metadata,
                       Column("id", Integer, primary_key=True, autoincrement=True), Column("site_id", String(24), nullable=False, index=True),
                       Column("org_id", String(24), nullable=False), Column("created_at", Float, nullable=False), Column("bytes", Integer, nullable=False),
                       Column("data", sa.JSON, nullable=False), Column("cameras", Integer, nullable=False, default=0),
                       Column("identities", Integer, nullable=False, default=0), Column("site_version", String(32), nullable=True))
push_subscriptions = Table("push_subscriptions", metadata,
                           Column("id", Integer, primary_key=True, autoincrement=True), Column("user_id", String(24), nullable=False, index=True),
                           Column("endpoint", Text, nullable=False), Column("sub", sa.JSON, nullable=False), Column("kinds", sa.JSON, nullable=False),
                           Column("created_at", Float, nullable=False), Column("ua", String(200), nullable=True))
kv = Table("kv", metadata, Column("key", String(64), primary_key=True), Column("value", sa.JSON, nullable=False))
dashboards = Table("dashboards", metadata,
                   Column("id", String(24), primary_key=True), Column("org_id", String(24), nullable=False, index=True),
                   Column("owner_user_id", String(24), nullable=True, index=True),   # NULL = shared by the org
                   Column("name", String(120), nullable=False), Column("config", sa.JSON, nullable=False),
                   Column("shared", Boolean, nullable=False, default=False),
                   Column("created_at", Float, nullable=False), Column("updated_at", Float, nullable=False),
                   Column("updated_by", String(24), nullable=True))
camera_groups = Table("camera_groups", metadata,
                      Column("id", String(24), primary_key=True), Column("org_id", String(24), nullable=False, index=True),
                      Column("name", String(120), nullable=False), Column("members", sa.JSON, nullable=False),   # [{site_id, camera_id}]
                      Column("created_at", Float, nullable=False), Column("updated_at", Float, nullable=False))
audit_log = Table("audit_log", metadata,
                  Column("id", Integer, primary_key=True, autoincrement=True), Column("ts", Float, nullable=False, index=True),
                  Column("user_id", String(24), nullable=True), Column("user_email", String(200), nullable=True),
                  Column("org_id", String(24), nullable=True, index=True), Column("site_id", String(24), nullable=True),
                  Column("action", String(200), nullable=False), Column("method", String(8), nullable=True),
                  Column("path", String(400), nullable=True), Column("status", Integer, nullable=True),
                  Column("ip", String(64), nullable=True), Column("detail", sa.JSON, nullable=True))

# ---- SOC (soc.py, soc_api.py). Per-Site contacts and procedures; incidents are the SOC's record of one or more
# events at a Site, keyed by the same (server, event) as customer alerts. incident_log is append-only.
location_contacts = Table("location_contacts", metadata,
                          Column("id", Integer, primary_key=True, autoincrement=True),
                          Column("location_id", String(24), nullable=False, index=True), Column("org_id", String(24), nullable=False),
                          Column("order", Integer, nullable=False, default=0), Column("name", String(120), nullable=False),
                          Column("role", String(80), nullable=True), Column("phone", String(40), nullable=True),
                          Column("email", String(200), nullable=True),
                          Column("notify_on_open", Boolean, nullable=False, default=False, server_default=sa.text("false")),
                          Column("notes", Text, nullable=True),
                          Column("created_at", Float, nullable=False), Column("updated_at", Float, nullable=False))
location_procedures = Table("location_procedures", metadata,
                            Column("id", Integer, primary_key=True, autoincrement=True),
                            Column("location_id", String(24), nullable=False, index=True), Column("org_id", String(24), nullable=False),
                            Column("title", String(200), nullable=False), Column("category", String(60), nullable=True),
                            Column("steps", sa.JSON, nullable=False),          # [{id, text, required}]
                            Column("priority", String(8), nullable=True),      # lowest incident priority it applies to; NULL = any
                            Column("order", Integer, nullable=False, default=0),
                            Column("created_at", Float, nullable=False), Column("updated_at", Float, nullable=False))
incidents = Table("incidents", metadata,
                  Column("id", Integer, primary_key=True, autoincrement=True),
                  Column("org_id", String(24), nullable=False, index=True), Column("location_id", String(24), nullable=False, index=True),
                  Column("opened_at", Float, nullable=False), Column("last_event_at", Float, nullable=True),
                  Column("updated_at", Float, nullable=False), Column("closed_at", Float, nullable=True),
                  Column("state", String(16), nullable=False, index=True),    # soc.STATES
                  Column("priority", String(8), nullable=False), Column("lane", String(8), nullable=False),   # soc.LANES
                  Column("claimed_by", String(24), nullable=True), Column("claimed_at", Float, nullable=True),
                  Column("first_claimed_at", Float, nullable=True), Column("assigned_by", String(24), nullable=True),
                  Column("sla_due_at", Float, nullable=True), Column("resolve_due_at", Float, nullable=True),
                  Column("escalation_level", Integer, nullable=False, default=0), Column("next_escalation_at", Float, nullable=True),
                  Column("disposition", String(32), nullable=True), Column("disposition_notes", Text, nullable=True),
                  Column("resolved_by", String(24), nullable=True), Column("resolved_at", Float, nullable=True),
                  Column("four_eyes_by", String(24), nullable=True), Column("four_eyes_at", Float, nullable=True),
                  Column("event_count", Integer, nullable=False, default=0), Column("title", String(200), nullable=True))
incident_events = Table("incident_events", metadata,
                        Column("id", Integer, primary_key=True, autoincrement=True),
                        Column("incident_id", Integer, nullable=False, index=True),
                        Column("server_id", String(24), nullable=False), Column("event_id", String(40), nullable=False),
                        Column("camera_id", String(64), nullable=True), Column("priority", String(8), nullable=True),
                        Column("kind", String(32), nullable=True), Column("ts", Float, nullable=True),
                        Column("detail", sa.JSON, nullable=True), Column("feedback_state", String(16), nullable=True),
                        # a site re-publishes the same event (synopsis, feedback, lock): the second insert fails, not duplicates
                        sa.UniqueConstraint("server_id", "event_id", name="uq_incident_events_server_event"))
incident_log = Table("incident_log", metadata,
                     Column("id", Integer, primary_key=True, autoincrement=True),
                     Column("incident_id", Integer, nullable=False, index=True), Column("ts", Float, nullable=False),
                     Column("user_id", String(24), nullable=True), Column("user_email", String(200), nullable=True),
                     Column("action", String(40), nullable=False), Column("detail", sa.JSON, nullable=True))
soc_presence = Table("soc_presence", metadata,
                     Column("user_id", String(24), primary_key=True),
                     Column("state", String(16), nullable=False),     # soc.PRESENCE: available | engaged | break | offline
                     Column("since", Float, nullable=False), Column("last_seen_at", Float, nullable=False),
                     Column("incident_id", Integer, nullable=True))
soc_reports = Table("soc_reports", metadata,
                    Column("id", Integer, primary_key=True, autoincrement=True), Column("kind", String(24), nullable=False, index=True),
                    Column("org_id", String(24), nullable=True, index=True),   # NULL = the whole SOC (shift reports)
                    Column("period_start", Float, nullable=False), Column("period_end", Float, nullable=False),
                    Column("created_at", Float, nullable=False), Column("created_by", String(24), nullable=True),
                    Column("text", Text, nullable=True), Column("data", sa.JSON, nullable=True), Column("model", String(120), nullable=True))

# ---- Central recording (hosts.py). A host is one datacenter machine running the axiom-host agent (it dials
# /host-agent with its own token, hashed at rest); a central instance is one isolated server container on a host,
# recording one customer Site's cameras over SpeedFusion (mode vpn, camera subnet 10.20.<site_number>.0/24) or locked
# port forwards (mode forward, the Site's public IP). The instance enrolls itself into its Site with a single-use
# token (central_enroll_tokens), after which it is an ordinary server (server_id). Hub administrators only.
hosts = Table("hosts", metadata,
              Column("id", String(24), primary_key=True), Column("name", String(120), nullable=False),
              Column("token_hash", String(64), nullable=False, index=True), Column("created_at", Float, nullable=False),
              Column("online", Boolean, nullable=False, default=False), Column("last_seen_at", Float, nullable=True),
              Column("hostname", String(120), nullable=True), Column("version", String(32), nullable=True),
              Column("capacity", sa.JSON, nullable=True),   # hello/heartbeat: {cpus, load, ram_gb, gpus, disks, instances}
              Column("notes", Text, nullable=True),
              Column("fusionhub", String(200), nullable=True),   # the FusionHub (SpeedFusion peer) address for VPN-mode Sites
              Column("agent_ip", String(64), nullable=True))
central_instances = Table("central_instances", metadata,
                          Column("id", String(24), primary_key=True), Column("host_id", String(24), nullable=False, index=True),
                          Column("location_id", String(24), nullable=False, index=True), Column("org_id", String(24), nullable=False, index=True),
                          Column("server_id", String(24), nullable=True, index=True),   # set when the instance enrolls
                          Column("name", String(120), nullable=False),
                          Column("mode", String(8), nullable=False),            # vpn | forward
                          Column("subnet", String(43), nullable=True), Column("public_ip", String(64), nullable=True),
                          Column("site_number", Integer, nullable=True, unique=True),   # 1..250; freed (NULL) once deleted
                          Column("quota_gb", Integer, nullable=False), Column("gpu", Integer, nullable=True),   # NULL = no GPU
                          Column("state", String(16), nullable=False, index=True),   # provisioning | running | failed | deleting | deleted
                          Column("last_error", Text, nullable=True),
                          Column("created_at", Float, nullable=False), Column("created_by", String(24), nullable=True),
                          Column("updated_at", Float, nullable=True),
                          Column("ready_at", Float, nullable=True),     # the host reported the instance created (then: waiting to enroll)
                          Column("info", sa.JSON, nullable=True),       # the host's last report: {state, used_gb, quota_gb, gpu, mode, ...}
                          Column("info_at", Float, nullable=True))
central_enroll_tokens = Table("central_enroll_tokens", metadata,
                              Column("token_hash", String(64), primary_key=True),
                              Column("instance_id", String(24), nullable=False, index=True),
                              Column("org_id", String(24), nullable=False), Column("location_id", String(24), nullable=False),
                              Column("created_at", Float, nullable=False), Column("expires_at", Float, nullable=False),
                              Column("used_at", Float, nullable=True), Column("server_id", String(24), nullable=True))

# ---- Cellular coverage (coverage.py): the CoverageMap API's answer for each Site (normalized + raw), and the units used
# per calendar month (budget). `python -m hub coverage purge` deletes both (and the address-check cache in kv,
# keys "coverage:..."): required once the CoverageMap subscription ends.
site_coverage = Table("site_coverage", metadata,
                      Column("location_id", String(24), primary_key=True),
                      Column("fetched_at", Float, nullable=True),          # last successful lookup (NULL: only failed attempts)
                      Column("lat", Float, nullable=True), Column("lon", Float, nullable=True),   # where it was looked up
                      Column("data", sa.JSON, nullable=True),              # {normalized, raw, basis, address, datasets, information}
                      Column("units", Integer, nullable=False, default=0),  # units that lookup cost
                      Column("plan_at_fetch", String(8), nullable=True),    # trial | paid: trial data is never shown to customers
                      Column("attempted_at", Float, nullable=True), Column("error", Text, nullable=True))   # the last attempt
coverage_usage = Table("coverage_usage", metadata,
                       Column("month", String(7), primary_key=True),       # YYYY-MM (UTC)
                       Column("units", Integer, nullable=False, default=0), Column("calls", Integer, nullable=False, default=0))

_engine: Engine | None = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        url = settings.database_url
        kw = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {"pool_pre_ping": True}
        _engine = sa.create_engine(url, future=True, **kw)
        metadata.create_all(_engine)
        upgrade(_engine)
        backfill(_engine)
    return _engine


# columns added after a table first shipped: create_all never alters an existing table
ADDED_COLUMNS = [("sites", "retired_at"), ("sites", "location_id"), ("memberships", "all_sites"),
                 ("invites", "all_sites"), ("invites", "location_ids"), ("invites", "created_by"), ("invites", "created_at"),
                 ("invites", "accepted_user_id"), ("invites", "label"),
                 ("users", "soc_role"), ("locations", "monitored"), ("locations", "arm_schedule"), ("locations", "arm_holidays"),
                 ("locations", "arm_override"), ("locations", "soc_group_minutes"),
                 ("locations", "lat"), ("locations", "lon"), ("locations", "address_parts"), ("locations", "geocoded_at"),
                 ("locations", "geocode_source"), ("users", "totp_last_step")]


def upgrade(eng: Engine) -> None:
    """Add any ADDED_COLUMNS an older database lacks (SQLite and Postgres both take ADD COLUMN), then any missing
    index. CreateColumn renders the type plus DEFAULT / NOT NULL, so a NOT NULL column fills the existing rows."""
    insp = sa.inspect(eng)
    tables = set(insp.get_table_names())
    for table, col in ADDED_COLUMNS:
        if table not in tables:
            continue
        if col not in {c["name"] for c in insp.get_columns(table)}:
            ddl = sa.schema.CreateColumn(metadata.tables[table].c[col]).compile(dialect=eng.dialect)
            with eng.begin() as c:
                c.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
    insp = sa.inspect(eng)
    for t in metadata.sorted_tables:
        if t.name not in tables:
            continue   # create_all just made it, indexes included
        have = {c["name"] for c in insp.get_columns(t.name)}
        for idx in t.indexes:   # e.g. ix_sites_location_id on a database that predates the column
            if all(c.name in have for c in idx.columns):
                idx.create(eng, checkfirst=True)


TENANCY_V2 = "schema:tenancy_v2"
# set once the legacy site_grants table is gone; the drop only ever runs after TENANCY_V2 has migrated its rows
TENANCY_V2_DROP_SITE_GRANTS = "schema:tenancy_v2_drop_site_grants"


def unique_location_name(c, org_id: str, name: str, exclude: str | None = None) -> str:
    """`name`, or `name 2`, `name 3`... so Site names stay unique (case-insensitively) inside a customer."""
    q = sa.select(locations.c.name).where(locations.c.org_id == org_id)
    if exclude:
        q = q.where(locations.c.id != exclude)
    taken = {r.name.casefold() for r in c.execute(q)}
    base = (name or "").strip()[:110] or "Site"
    out, n = base, 2
    while out.casefold() in taken:
        out, n = f"{base} {n}", n + 1
    return out


def location_for_server(c, server: dict) -> str:
    """The server's Site id; a server without one gets its own one-server Site named after it (address from
    `sites.location`). Runs on connection `c` so it joins the caller's transaction."""
    if server.get("location_id"):
        return server["location_id"]
    lid = new_id("l_")
    t = time.time()
    c.execute(locations.insert().values(id=lid, org_id=server["org_id"], name=unique_location_name(c, server["org_id"], server["name"]),
                                        address=(server.get("location") or "")[:200], timezone=None, notes=None, created_at=t, updated_at=t))
    c.execute(sa.update(sites).where(sites.c.id == server["id"]).values(location_id=lid))
    server["location_id"] = lid
    return lid


def backfill(eng: Engine) -> None:
    """Idempotent data upgrade, run at every start after upgrade():
    1. every server without a Site gets its own one-server Site, so the fleet looks exactly as before;
    2. once (kv schema:tenancy_v2): legacy per-server grants become Site grants with all_sites=false; everyone else
       gets all_sites=true (the old implicit "no grants = all" rule, made explicit);
    3. once (kv schema:tenancy_v2_drop_site_grants), after step 2 has committed: the legacy site_grants table is dropped;
    4. the cameras registry is seeded from each server's last heartbeat summary (pairs it lacks only)."""
    has_site_grants = "site_grants" in sa.inspect(eng).get_table_names()
    with eng.begin() as c:
        for s in c.execute(sa.select(sites).where(sites.c.location_id.is_(None)).order_by(sites.c.created_at)).mappings().all():
            location_for_server(c, dict(s))
    with eng.begin() as c:
        if c.execute(sa.select(kv.c.key).where(kv.c.key == TENANCY_V2)).first() is None:
            server_loc = {r.id: (r.org_id, r.location_id) for r in c.execute(sa.select(sites.c.id, sites.c.org_id, sites.c.location_id))}
            granted: dict[str, set[str]] = {}
            # raw SQL: the table left the metadata, and a database created by this code never had it
            for g in c.execute(sa.text("SELECT user_id, site_id FROM site_grants")) if has_site_grants else ():
                granted.setdefault(g.user_id, set()).add(g.site_id)
            for m in c.execute(sa.select(memberships.c.user_id, memberships.c.org_id)).all():
                locs = {server_loc[sid][1] for sid in granted.get(m.user_id, ()) if sid in server_loc and server_loc[sid][0] == m.org_id}
                locs.discard(None)
                c.execute(sa.update(memberships).where(memberships.c.user_id == m.user_id, memberships.c.org_id == m.org_id)
                          .values(all_sites=not locs))
                for lid in locs:
                    if c.execute(sa.select(location_grants.c.user_id).where(location_grants.c.user_id == m.user_id,
                                                                             location_grants.c.location_id == lid)).first() is None:
                        c.execute(location_grants.insert().values(user_id=m.user_id, location_id=lid))
            c.execute(kv.insert().values(key=TENANCY_V2, value={"at": time.time()}))
    with eng.begin() as c:
        # Separate transaction, and gated on the migration marker, so the rows are never dropped unread. Nothing reads
        # or writes site_grants since 0.2.0; code before tenancy v2 would recreate it empty (= every server) on a rollback.
        migrated = c.execute(sa.select(kv.c.key).where(kv.c.key == TENANCY_V2)).first() is not None
        dropped = c.execute(sa.select(kv.c.key).where(kv.c.key == TENANCY_V2_DROP_SITE_GRANTS)).first() is not None
        if migrated and not dropped:
            c.execute(sa.text("DROP TABLE IF EXISTS site_grants"))
            c.execute(kv.insert().values(key=TENANCY_V2_DROP_SITE_GRANTS, value={"at": time.time(), "existed": has_site_grants}))
    with eng.begin() as c:
        have = {(r.server_id, r.camera_id) for r in c.execute(sa.select(cameras.c.server_id, cameras.c.camera_id))}
        for s in c.execute(sa.select(sites.c.id, sites.c.org_id, sites.c.location_id, sites.c.summary, sites.c.last_seen_at)).all():
            summ = s.summary if isinstance(s.summary, dict) else {}
            for cam in summ.get("cameras") or []:
                cid = str(cam.get("id") or "")[:64] if isinstance(cam, dict) else ""
                if not cid or (s.id, cid) in have:
                    continue
                have.add((s.id, cid))
                c.execute(cameras.insert().values(
                    server_id=s.id, camera_id=cid, org_id=s.org_id, location_id=s.location_id, name=str(cam.get("name") or cid)[:120],
                    enabled=True, stream_ready=cam.get("stream_ready"), problems=cam.get("problems"), bitrate_mbps=cam.get("bitrate_mbps"),
                    ptz=cam.get("ptz"), metadata=cam.get("metadata"), onvif_events=cam.get("onvif_events"),
                    first_seen_at=s.last_seen_at or time.time(), last_seen_at=s.last_seen_at, missing_since=None, source="summary"))


def reset_engine() -> None:
    """Tests: point at a fresh database after changing settings.database_url."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:12]


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def rows(stmt) -> list[dict]:
    with engine().connect() as c:
        return [dict(r._mapping) for r in c.execute(stmt)]


def one(stmt) -> dict | None:
    r = rows(stmt.limit(1) if hasattr(stmt, "limit") else stmt)
    return r[0] if r else None


def run(stmt) -> None:
    with engine().begin() as c:
        c.execute(stmt)


def insert(table: Table, values: dict) -> None:
    with engine().begin() as c:
        c.execute(table.insert().values(**values))


def now() -> float:
    return time.time()


def dumps(v) -> str:
    return json.dumps(v, separators=(",", ":"))
