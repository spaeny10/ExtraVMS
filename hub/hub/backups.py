"""Nightly configuration backups of every site (GET /api/config/export through the tunnel), kept per site;
restore pushes one back with POST /api/config/import. Recordings and events stay at the site."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time

import sqlalchemy as sa

from . import db
from .agents import registry
from .config import settings

log = logging.getLogger("hub.backups")
KEEP = 30


async def backup_site(site: dict, user_email: str = "hub-backup") -> dict | None:
    conn = registry.get(site["id"])
    if conn is None:
        return None
    status, body = await conn.call("GET", "/api/config/export", "", {"x-hub-user": user_email, "x-hub-role": "admin"}, None, 30)
    if status != 200:
        raise RuntimeError(f"site answered {status}")
    data = json.loads(body.decode())
    row = {"site_id": site["id"], "org_id": site["org_id"], "created_at": time.time(), "bytes": len(body), "data": data,
           "cameras": len(data.get("cameras") or []), "identities": len(data.get("identities") or []), "site_version": data.get("site_version")}
    db.insert(db.config_backups, row)
    old = db.rows(sa.select(db.config_backups.c.id).where(db.config_backups.c.site_id == site["id"]).order_by(db.config_backups.c.id.desc()).offset(KEEP))
    if old:
        db.run(sa.delete(db.config_backups).where(db.config_backups.c.id.in_([o["id"] for o in old])))
    return db.one(sa.select(db.config_backups).where(db.config_backups.c.site_id == site["id"]).order_by(db.config_backups.c.id.desc()))


def list_for(site_id: str) -> list[dict]:
    rows = db.rows(sa.select(db.config_backups.c.id, db.config_backups.c.created_at, db.config_backups.c.bytes, db.config_backups.c.cameras,
                             db.config_backups.c.identities, db.config_backups.c.site_version)
                   .where(db.config_backups.c.site_id == site_id).order_by(db.config_backups.c.id.desc()))
    return rows


async def restore(site: dict, backup_id: int, user_email: str, replace_identities: bool = False) -> dict:
    b = db.one(sa.select(db.config_backups).where(db.config_backups.c.id == backup_id, db.config_backups.c.site_id == site["id"]))
    if not b:
        raise LookupError("no such backup")
    conn = registry.get(site["id"])
    if conn is None:
        raise RuntimeError("site offline")
    payload = json.dumps({"data": b["data"], "replace_identities": replace_identities}).encode()
    status, body = await conn.call("POST", "/api/config/import", "", {"x-hub-user": user_email, "x-hub-role": "admin", "content-type": "application/json"}, payload, 60)
    if status != 200:
        raise RuntimeError(f"site answered {status}: {body.decode()[:200]}")
    return json.loads(body.decode())


async def nightly_loop() -> None:
    while True:
        now = dt.datetime.now()
        target = now.replace(hour=settings.backup_hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        await asyncio.sleep(max(1.0, (target - now).total_seconds()))
        for s in db.rows(sa.select(db.sites)):
            try:
                if await backup_site(s):
                    log.info("config backup of %s stored", s["name"])
            except Exception as e:
                log.warning("config backup of %s failed: %s", s["name"], e)
