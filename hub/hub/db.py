"""Hub database (SQLAlchemy Core, Postgres in production, SQLite for dev and tests).

Tenancy: orgs -> memberships (users with a role) -> sites. A user with any site_grants inside an org sees
only those sites. Sites authenticate with a device token (hashed at rest). Alerts and the audit log are
per org. Small, synchronous calls: a fleet of dozens of sites is a few writes a second.
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
              Column("created_at", Float, nullable=False), Column("last_login_at", Float, nullable=True))
memberships = Table("memberships", metadata,
                    Column("user_id", String(24), primary_key=True), Column("org_id", String(24), primary_key=True),
                    Column("role", String(16), nullable=False))
site_grants = Table("site_grants", metadata,
                    Column("user_id", String(24), primary_key=True), Column("site_id", String(24), primary_key=True))
sites = Table("sites", metadata,
              Column("id", String(24), primary_key=True), Column("org_id", String(24), nullable=False, index=True),
              Column("name", String(120), nullable=False), Column("location", String(200), nullable=False, default=""),
              Column("token_hash", String(64), nullable=False, index=True), Column("token_prev_hash", String(64), nullable=True),
              Column("token_rotated_at", Float, nullable=True), Column("created_at", Float, nullable=False),
              Column("last_seen_at", Float, nullable=True), Column("online", Boolean, nullable=False, default=False),
              Column("version", String(32), nullable=True), Column("summary", sa.JSON, nullable=True),
              Column("clock_skew_s", Float, nullable=True), Column("agent_ip", String(64), nullable=True),
              Column("hostname", String(120), nullable=True),
              Column("retired_at", Float, nullable=True))   # fleet actions: hidden from Fleet/Home, tunnel left alone
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
                Column("expires_at", Float, nullable=False), Column("accepted_at", Float, nullable=True))
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

_engine: Engine | None = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        url = settings.database_url
        kw = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {"pool_pre_ping": True}
        _engine = sa.create_engine(url, future=True, **kw)
        metadata.create_all(_engine)
        upgrade(_engine)
    return _engine


# columns added after a table first shipped: create_all never alters an existing table
ADDED_COLUMNS = [("sites", "retired_at")]


def upgrade(eng: Engine) -> None:
    """Add any ADDED_COLUMNS an older database lacks (SQLite and Postgres both take ADD COLUMN)."""
    insp = sa.inspect(eng)
    for table, col in ADDED_COLUMNS:
        if table not in insp.get_table_names():
            continue
        if col not in {c["name"] for c in insp.get_columns(table)}:
            ctype = metadata.tables[table].c[col].type.compile(dialect=eng.dialect)
            with eng.begin() as c:
                c.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {col} {ctype}"))


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
