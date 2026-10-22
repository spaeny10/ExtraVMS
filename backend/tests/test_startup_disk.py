"""Start-up on a full data disk (2026-10-22: a central instance's quota'd /data dataset hit 0 bytes free, SQLite said
"disk I/O error" and the NVR crash-looped): the filesystem-only cleanup runs before anything opens the database
(nvr/__main__ -> diskguard.preopen), counts what it deleted (ZFS frees asynchronously), may drop old backups on the
same filesystem, and an open that still fails on storage is cleaned up once more and retried. Plus the heartbeat's
db_errors count and the backup space check. Throwaway dirs only.
Run: ..\\.venv\\Scripts\\python.exe tests\\test_startup_disk.py   (from backend/), or under pytest."""
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import namedtuple
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="nvr-startup-disk-"))
os.environ["NVR_DATA_DIR"] = str(ROOT / "data")
os.environ["NVR_RECORDINGS_DIR"] = str(ROOT / "rec")
os.environ["NVR_RUNTIME_DIR"] = str(ROOT / "run")
os.environ["NVR_BACKUP_DIR"] = str(ROOT / "data" / "backups")
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from nvr import backup, diskguard  # noqa: E402
from nvr import db as dbmod  # noqa: E402
from nvr.config import settings  # noqa: E402

Usage = namedtuple("Usage", "total used free")


class FakeDisk:
    """shutil.disk_usage for diskguard: a fixed total, free = total - used, where `used` counts the real bytes under
    the root (scaled) unless `frozen` (ZFS: the space of deleted files comes back later)."""

    def __init__(self, root: Path, total: int, base: int = 0, frozen: bool = False):
        self.root, self.total, self.base, self.frozen = root, total, base, frozen
        self.free0 = None

    def __call__(self, path):
        used = self.base + sum(f.stat().st_size for f in self.root.rglob("*") if f.is_file())
        free = max(0, self.total - used)
        if self.frozen:
            self.free0 = free if self.free0 is None else self.free0
            free = self.free0
        return Usage(self.total, self.total - free, free)


def events(root: Path, n: int, size: int = 1000, old: bool = True) -> list[int]:
    ids = []
    for i in range(1, n + 1):
        d = root / "events" / str(i)
        d.mkdir(parents=True, exist_ok=True)
        (d / "snapshot.jpg").write_bytes(b"s")
        (d / "clip.mp4").write_bytes(b"x" * size)
        (d / "crop_0.jpg").write_bytes(b"c")
        if old:
            os.utime(d, (time.time() - 3 * 86400,) * 2)
        ids.append(i)
    return ids


def with_disk(fake):
    old = diskguard.shutil.disk_usage
    diskguard.shutil.disk_usage = fake
    return lambda: setattr(diskguard.shutil, "disk_usage", old)


def test_emergency_free_counts_deleted_bytes_when_free_space_lags():
    root = ROOT / "lag"
    ids = events(root, 10)
    undo = with_disk(FakeDisk(root, total=20_000, frozen=True))   # never shows anything freed
    diskguard.cleared.clear()
    try:
        start = diskguard.free_bytes(root)
        freed = diskguard.emergency_free(root, start + 3000)
    finally:
        undo()
    assert 3000 <= freed <= 3003 and diskguard.cleared == ids[:3], (freed, diskguard.cleared)   # not all ten
    assert (root / "events" / "1" / "snapshot.jpg").exists() and (root / "events" / "4" / "clip.mp4").exists()
    diskguard.cleared.clear()


def test_emergency_free_then_old_backups_on_the_same_filesystem():
    root = ROOT / "bak"
    events(root, 2, size=100)
    bdir = root / "backups"
    bdir.mkdir(parents=True)
    for day in range(1, 6):
        (bdir / f"nvr-202610{day:02d}-0330.db").write_bytes(b"b" * 1000)
    fake = FakeDisk(root, total=10_000, base=10_000 - 5_250)   # used: 5,250 of real bytes counted on top
    undo = with_disk(fake)
    try:
        free0 = diskguard.free_bytes(root)
        freed = diskguard.emergency_free(root, free0 + 1500, backup_dir=bdir)
        left = sorted(f.name for f in bdir.glob("nvr-*.db"))
    finally:
        undo()
    assert freed >= 1500
    assert left == ["nvr-20261003-0330.db", "nvr-20261004-0330.db", "nvr-20261005-0330.db"], left   # oldest went first
    # never the newest BACKUPS_KEPT, however short the disk is
    undo = with_disk(FakeDisk(root, total=10_000, base=10_000))
    try:
        diskguard.emergency_free(root, 10 ** 9, backup_dir=bdir)
    finally:
        undo()
    assert sorted(f.name for f in bdir.glob("nvr-*.db")) == ["nvr-20261004-0330.db", "nvr-20261005-0330.db"]
    diskguard.cleared.clear()


def test_emergency_target_is_capped_on_small_volumes():
    undo = with_disk(lambda p: Usage(int(10e9), int(10e9), 0))
    try:
        assert diskguard.emergency_target(ROOT, 5) == int(2.5e9)
    finally:
        undo()
    undo = with_disk(lambda p: Usage(int(500e9), 0, int(500e9)))
    try:
        assert diskguard.emergency_target(ROOT, 5) == int(5e9)
    finally:
        undo()


def test_preopen_frees_and_never_raises():
    root = ROOT / "pre"
    events(root, 5)
    fake = FakeDisk(root, total=6_000)   # 5 x 1,003 used: ~985 free
    undo = with_disk(fake)
    waited = []
    old_wait = diskguard.wait_free
    diskguard.wait_free = lambda d, t, **k: waited.append(t) or 0
    try:
        assert diskguard.preopen(root, 3000 / 1e9) >= 1000   # 3,000 bytes wanted, capped at 25 %: 1,500
        assert waited, "waits for ZFS to show the space before SQLite opens"
    finally:
        undo()
        diskguard.wait_free = old_wait
    undo = with_disk(lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        assert diskguard.preopen(root, 5) == 0      # a broken guard must not stop the start
    finally:
        undo()
    diskguard.cleared.clear()


def test_main_runs_preopen_before_anything_opens_the_database():
    src = (BACKEND / "nvr" / "__main__.py").read_text(encoding="utf-8")
    assert src.index("diskguard.preopen(") < src.index("uvicorn.run(")
    out = subprocess.run([sys.executable, "-c", "import sys, nvr.__main__; print('nvr.db' in sys.modules)"],
                         cwd=BACKEND, capture_output=True, text=True, env={**os.environ}, timeout=120)
    assert out.stdout.strip() == "False", out.stdout + out.stderr


class OpenFails:
    """Database._open_all that fails `n` times with `msg`, then works."""

    def __init__(self, n: int, msg: str = "disk I/O error"):
        self.n, self.msg, self.calls = n, msg, 0
        self.real = dbmod.Database._open_all

    def __call__(self, db, path):
        self.calls += 1
        if self.calls <= self.n:
            db.conn = sqlite3.connect(":memory:")
            raise sqlite3.OperationalError(self.msg)
        return self.real(db, path)


def open_with(fails: OpenFails, path: Path):
    calls = []
    real_free, real_wait = diskguard.emergency_free, diskguard.wait_free
    diskguard.emergency_free = lambda d, target, min_age_s=86400, backup_dir=None: calls.append(min_age_s) or 0
    diskguard.wait_free = lambda d, t, **k: 0
    dbmod.Database._open_all = lambda self, p: fails(self, p)
    try:
        return dbmod.Database(path), calls
    finally:
        dbmod.Database._open_all = fails.real
        diskguard.emergency_free, diskguard.wait_free = real_free, real_wait


def test_open_that_fails_on_storage_cleans_up_once_and_retries():
    d, calls = open_with(OpenFails(1), ROOT / "o1" / "nvr.db")
    assert calls == [86400, 0], calls                     # the usual pre-open pass, then a harder one (any age)
    d.execute("INSERT INTO settings (key, value) VALUES ('x', '1')")
    assert d.one("SELECT value FROM settings WHERE key='x'")["value"] == "1"
    d.conn.close()


def test_open_that_keeps_failing_raises_after_one_retry():
    fails = OpenFails(5)
    try:
        open_with(fails, ROOT / "o2" / "nvr.db")
        raise AssertionError("should have raised")
    except sqlite3.OperationalError:
        pass
    assert fails.calls == 2, "one retry, not a loop"


def test_open_error_that_is_not_storage_raises_at_once():
    fails = OpenFails(1, "database disk image is malformed")
    try:
        open_with(fails, ROOT / "o3" / "nvr.db")
        raise AssertionError("should have raised")
    except sqlite3.OperationalError as e:
        assert "malformed" in str(e)
    assert fails.calls == 1


def test_db_errors_counts_recent_storage_failures():
    d, _ = open_with(OpenFails(0), ROOT / "o4" / "nvr.db")
    now = [1000.0]
    d.clock = lambda: now[0]
    assert d.recent_errors() == 0 and d.storage["errors_total"] == 0
    d.storage_failed("disk I/O error")
    d.storage_failed("disk I/O error")
    assert d.recent_errors() == 2 and d.storage["errors_total"] == 2
    now[0] += 901
    d.storage_ok()
    assert d.recent_errors() == 0 and d.storage["errors_total"] == 2   # the window moves on; the total stays
    d.conn.close()


def test_heartbeat_reports_db_errors_without_the_database():
    from types import SimpleNamespace
    from nvr import summary
    from nvr.db import db
    db.storage_failed("database or disk is full")
    try:
        out = summary.minimal_summary(SimpleNamespace(pipeline=None), "test", "disk I/O error")
    finally:
        db.storage_ok()
    assert out["db_errors"] >= 1 and out["storage"]["errors_total"] >= 1
    assert set(out["data_disk"]) == {"free_gb", "total_gb"}


def test_backup_needs_room_for_a_copy():
    undo = with_disk(lambda p: Usage(int(50e9), int(48e9), int(2e9)))
    old = backup.shutil.disk_usage
    backup.shutil.disk_usage = diskguard.shutil.disk_usage
    try:
        assert not backup.room_for_backup(ROOT, int(1e9))      # 2 GB free: not 1.2 GB + 2 GB spare
    finally:
        backup.shutil.disk_usage = old
        undo()
    backup.shutil.disk_usage = lambda p: Usage(int(50e9), int(40e9), int(10e9))
    try:
        assert backup.room_for_backup(ROOT, int(1e9))
    finally:
        backup.shutil.disk_usage = old


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
