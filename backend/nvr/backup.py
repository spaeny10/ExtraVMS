"""Nightly copy of the database (the part of the NVR that can't be re-recorded: synopses, feedback,
journeys, named identities, settings). `VACUUM INTO` writes a consistent snapshot while the NVR runs."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

from .config import settings
from .db import db

log = logging.getLogger("nvr.backup")

KEEP = 14


def run() -> dict:
    out_dir = settings.backup_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"nvr-{dt.datetime.now():%Y%m%d-%H%M}.db"
    t0 = time.time()
    with db.lock:
        db.conn.execute("VACUUM INTO ?", [path.as_posix()])
    for old in sorted(out_dir.glob("nvr-*.db"))[:-KEEP]:
        old.unlink(missing_ok=True)
    info = {"at": time.time(), "path": str(path), "bytes": path.stat().st_size, "seconds": round(time.time() - t0, 1),
            "count": len(list(out_dir.glob("nvr-*.db")))}
    db.set_setting("last_backup", info)
    log.info("database backed up to %s (%.1f MB)", path, info["bytes"] / 1e6)
    return info


def status() -> dict:
    return {"dir": str(settings.backup_dir), "last": db.get_setting("last_backup")}


async def backup_loop() -> None:
    """At 03:30 daily, and at startup if the last backup is more than a day old."""
    last = (db.get_setting("last_backup") or {}).get("at", 0)
    if time.time() - last > 86400:
        await asyncio.sleep(120)  # let startup finish first
        try:
            await asyncio.to_thread(run)
        except Exception:
            log.exception("backup failed")
    while True:
        now = dt.datetime.now()
        nxt = now.replace(hour=3, minute=30, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            await asyncio.to_thread(run)
        except Exception:
            log.exception("backup failed")
