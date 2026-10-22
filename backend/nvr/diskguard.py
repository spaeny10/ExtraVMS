"""Event-media deletion that works without the database, so a box whose disk is full can still start.

At 0 bytes free SQLite cannot open its WAL ("disk I/O error"), `db.Database()` raises at import time and the
service restart-loops without ever reaching the retention pass that would free space (2026-10-22: a central
instance's 50 GB data dataset on fred filled with event clips, SQLite failed for ~30 h, and restarts crash-looped).
So, before SQLite opens:

- `preopen()` runs from `nvr/__main__` before anything that imports the database: it walks data_dir/events on the
  filesystem alone and deletes event media (keyframes, crops, legacy clips; never snapshot.jpg) oldest-first until
  `disk_emergency_free_gb` is free, then, if that is not enough, the oldest nightly database backups when they sit on
  the same filesystem (the newest BACKUPS_KEPT stay).
- `Database.__init__` runs the same cleanup again (cheap when there is room), and an open that still fails with a
  storage error ("disk I/O error", "database or disk is full") runs it once more, harder, and retries once.

Free space is measured with statvfs on the data dir itself (shutil.disk_usage): inside an instance container that is
its own quota'd ZFS dataset, whose statvfs reports the quota, not the pool. ZFS gives a deleted file's space back a
few seconds later (the next transaction group), so what was deleted is counted as well as measured: judging by
statvfs alone would keep deleting long after enough was gone. The ids touched are kept in `cleared` so the database
can set their `clip` column to NULL once it is open.

Deliberately imports nothing from the rest of nvr: db.py imports this module before the database exists.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

log = logging.getLogger("nvr.diskguard")

KEEP_FILES = {"snapshot.jpg"}   # small, and the only picture the event list and alerts have
cleared: list[int] = []         # event ids emptied before the database was open (db.py nulls their clip)
BACKUPS_KEPT = 2                # nightly database backups an emergency never deletes (backup.py keeps 14)
EMERGENCY_MAX_FRAC = 0.25       # never aim for more than this share of a (small) volume free


def free_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def total_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(path).total
    except OSError:
        return 0


def emergency_target(path: Path, gb: float) -> int:
    """Bytes to have free before SQLite opens: `gb` (disk_emergency_free_gb), but at most a quarter of a small volume
    (a 10 GB dataset must not lose every picture to reach 5 GB)."""
    need = int(gb * 1e9)
    total = total_bytes(path)
    return min(need, int(total * EMERGENCY_MAX_FRAC)) if total else need


def has_media(d: Path) -> bool:
    """Does this event folder hold anything besides what is always kept (the snapshot)?"""
    try:
        with os.scandir(d) as it:
            return any(f.name not in KEEP_FILES for f in it)
    except OSError:
        return False


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


def _same_fs(a: Path, b: Path) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def prune_backups(backup_dir: Path | None, data_dir: Path, enough) -> int:
    """The oldest nightly database backups (nvr-*.db), newest BACKUPS_KEPT kept, until enough(freed) says stop. Only
    when they share data_dir's filesystem (on a central instance /data/backups is on the quota'd dataset)."""
    if backup_dir is None or not Path(backup_dir).is_dir() or not _same_fs(Path(backup_dir), data_dir):
        return 0
    freed = 0
    olds = sorted(Path(backup_dir).glob("nvr-*.db"))[:-BACKUPS_KEPT] if BACKUPS_KEPT else sorted(Path(backup_dir).glob("nvr-*.db"))
    for f in olds:
        if enough(freed):
            break
        try:
            size = f.stat().st_size
            f.unlink()
            freed += size
            log.warning("disk emergency: deleted the old database backup %s (%.1f GB)", f.name, size / 1e9)
        except OSError:
            pass
    return freed


def emergency_free(data_dir: Path, target_bytes: int, min_age_s: float = 86400, backup_dir: Path | None = None) -> int:
    """Free event media until data_dir's volume has `target_bytes` free. Ids autoincrement, so id order is
    oldest-first. Folders older than `min_age_s` (the shortest continuous window) go first; only if that is not
    enough are newer ones touched, because a box that cannot start records nothing at all. Then old database backups
    on the same filesystem (backup_dir). Returns the bytes deleted."""
    start = free_bytes(data_dir)
    if start >= target_bytes:
        return 0
    freed = 0

    def enough(extra: int = 0) -> bool:
        # measured, or counted (ZFS hands the space back a few seconds after the unlink)
        return max(free_bytes(data_dir), start + freed + extra) >= target_bytes

    dirs = _event_dirs(Path(data_dir) / "events")
    cutoff = time.time() - min_age_s

    def mtime(d: Path) -> float:
        try:
            return d.stat().st_mtime
        except OSError:
            return 0.0

    old = [(i, d) for i, d in dirs if mtime(d) < cutoff]
    new = [(i, d) for i, d in dirs if mtime(d) >= cutoff]
    n = 0
    for i, d in old + new:
        if enough():
            break
        got = delete_event_media(d)
        if got:
            freed += got
            n += 1
            cleared.append(i)
    if not enough():
        freed += prune_backups(backup_dir, Path(data_dir), lambda extra: enough(extra))
    if freed:
        log.warning("disk emergency: freed %.1f GB (%d events' media%s) so the NVR can run; %.1f GB free now",
                    freed / 1e9, n, ", old backups" if not n else "", free_bytes(data_dir) / 1e9)
    if not enough():
        log.error("disk emergency: only %.1f GB free on %s after deleting everything allowed (wanted %.1f GB)",
                  free_bytes(data_dir) / 1e9, data_dir, target_bytes / 1e9)
    return freed


def wait_free(data_dir: Path, target_bytes: int, timeout: float = 15.0, poll: float = 0.5) -> int:
    """Wait (up to `timeout`) for statvfs to show `target_bytes` free: ZFS returns deleted space asynchronously, and
    SQLite retried at once would still find the disk full. Returns the free bytes seen last."""
    end = time.time() + timeout
    free = free_bytes(data_dir)
    while free < target_bytes and time.time() < end:
        time.sleep(poll)
        free = free_bytes(data_dir)
    return free


def preopen(data_dir: Path, gb: float, backup_dir: Path | None = None) -> int:
    """The start-up hook (nvr/__main__, before anything opens the database): make `gb` (capped by emergency_target)
    free on data_dir's filesystem from the filesystem alone. Never raises: a guard must not be what stops the start."""
    try:
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        target = emergency_target(data_dir, gb)
        freed = emergency_free(data_dir, target, backup_dir=backup_dir)
        if freed:
            wait_free(data_dir, target)
        return freed
    except Exception:  # noqa: BLE001
        log.exception("disk check before opening the database failed; starting anyway")
        return 0
