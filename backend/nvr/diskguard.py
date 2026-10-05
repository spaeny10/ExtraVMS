"""Event-media deletion that works without the database, so a box whose disk is full can still start.

At 0 bytes free SQLite cannot open its WAL ("disk I/O error"), `db.Database()` raises at import time and the
service restart-loops without ever reaching the retention pass that would free space. `emergency_free()` runs
from `Database.__init__` before the connection is opened: it walks data_dir/events on the filesystem alone and
deletes clips and keyframes (never snapshot.jpg) oldest-first until there is room to start. The ids it touched
are kept in `cleared` so the database can set their `clip` column to NULL once it is open.

Deliberately imports nothing from the rest of nvr: db.py imports this module before the database exists.
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

log = logging.getLogger("nvr.diskguard")

KEEP_FILES = {"snapshot.jpg"}   # tiny, and the only picture the event list and alerts have
cleared: list[int] = []         # event ids emptied before the database was open (db.py nulls their clip)


def free_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def delete_event_media(d: Path) -> int:
    """Delete everything in one event folder except the snapshot; returns the bytes freed."""
    freed = 0
    try:
        files = list(d.iterdir())
    except OSError:
        return 0
    for f in files:
        if f.name in KEEP_FILES:
            continue
        try:
            if not f.is_file():
                continue
            size = f.stat().st_size
            f.unlink()
            freed += size
        except OSError:  # vanished or in use: the next pass gets it
            pass
    return freed


def _event_dirs(events: Path) -> list[tuple[int, Path]]:
    try:
        return sorted((int(d.name), d) for d in events.iterdir() if d.name.isdigit())
    except OSError:
        return []


def emergency_free(data_dir: Path, target_bytes: int, min_age_s: float = 86400) -> int:
    """Free event media until data_dir's volume has `target_bytes` free. Ids autoincrement, so id order is
    oldest-first. Folders older than `min_age_s` (the shortest continuous window) go first; only if that is not
    enough are newer ones touched, because a box that cannot start records nothing at all."""
    if free_bytes(data_dir) >= target_bytes:
        return 0
    dirs = _event_dirs(data_dir / "events")
    cutoff = time.time() - min_age_s

    def mtime(d: Path) -> float:
        try:
            return d.stat().st_mtime
        except OSError:
            return 0.0

    old = [(i, d) for i, d in dirs if mtime(d) < cutoff]
    new = [(i, d) for i, d in dirs if mtime(d) >= cutoff]
    freed, n = 0, 0
    for i, d in old + new:
        if free_bytes(data_dir) >= target_bytes:
            break
        got = delete_event_media(d)
        if got:
            freed += got
            n += 1
            cleared.append(i)
    if n:
        log.warning("disk emergency: freed %.1f GB of event media (%d events) so the NVR can start; %.1f GB free now",
                    freed / 1e9, n, free_bytes(data_dir) / 1e9)
    return freed
