"""Nightly configuration backups of every site (GET /api/config/export through the tunnel), kept per site;
restore pushes one back with POST /api/config/import. Recordings and events stay at the site."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time

import sqlalchemy as sa

from . import central_cameras, db
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


def latest_for(server_ids: list[str]) -> dict[str, dict]:
    """server id -> {latest: the newest backup's list_for row, count}; servers without backups are absent.
    One query for a whole Site (KEEP caps the rows per server, so this stays small)."""
    if not server_ids:
        return {}
    t = db.config_backups
    out: dict[str, dict] = {}
    for r in db.rows(sa.select(t.c.id, t.c.site_id, t.c.created_at, t.c.bytes, t.c.cameras, t.c.identities, t.c.site_version)
                     .where(t.c.site_id.in_(server_ids)).order_by(t.c.id.desc())):
        sid = r.pop("site_id")
        if sid in out:
            out[sid]["count"] += 1
        else:
            out[sid] = {"latest": r, "count": 1}
    return out


async def restore(site: dict, backup_id: int, user_email: str, replace_identities: bool = False) -> dict:
    """POST /api/config/import of a stored backup. A central recording instance's camera limit and address rules apply
    as for an import through the console (central_cameras.check): central_cameras.Refused when it breaks them."""
    b = db.one(sa.select(db.config_backups).where(db.config_backups.c.id == backup_id, db.config_backups.c.site_id == site["id"]))
    if not b:
        raise LookupError("no such backup")
    conn = registry.get(site["id"])
    if conn is None:
        raise RuntimeError("site offline")
    body = {"data": b["data"], "replace_identities": replace_identities}
    ci = central_cameras.instance_for_server(site["id"])
    reserved = await central_cameras.check(ci, "import", "/api/config/import", body) if ci else []
    try:
        status, raw = await conn.call("POST", "/api/config/import", "", {"x-hub-user": user_email, "x-hub-role": "admin", "content-type": "application/json"},
                                      json.dumps(body).encode(), 60)
    except BaseException:
        central_cameras.release(site["id"], reserved)
        raise
    if status != 200:
        central_cameras.release(site["id"], reserved)
        raise RuntimeError(f"site answered {status}: {raw.decode()[:200]}")
    if ci:
        central_cameras.after_write(site["id"])   # the firewall follows the instance's cameras
    return json.loads(raw.decode())


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
