"""Event media must not be able to fill the disk: the data-volume floor, the prune catch-up, the event-rate guard,
the no-database emergency free used at startup; and the full recordings disk (kept footage first, then continuous).
Disk space is faked (each byte on disk counts as 1 GB) so the floor logic runs against a scratch data_dir. Run as a
script (test.ps1) or under pytest."""
import os
import sys
import tempfile
import time
from collections import Counter, namedtuple
from contextlib import contextmanager
from pathlib import Path

ROOT_TMP = Path(tempfile.mkdtemp(prefix="nvr-floor-test-"))
os.environ["NVR_DATA_DIR"] = str(ROOT_TMP / "data")
os.environ["NVR_RECORDINGS_DIR"] = str(ROOT_TMP / "rec")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import diskguard, keep, retention  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402

Usage = namedtuple("Usage", "total used free")
DATA = settings.data_dir
REC = settings.recordings_dir
REC.mkdir(parents=True, exist_ok=True)
GB = 1e9
# per volume: total size and how much is used by things other than the scratch files (GB)
disk = {"data": {"total": 1000, "base": 0}, "rec": {"total": 1000, "base": 0}, "shared": True}


def _tree_gb(root: Path) -> float:
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file()) if root.exists() else 0


def fake_usage(path):
    # Each byte of event media counts as 1 GB. Shared: both dirs report the data volume.
    p = Path(path).resolve()
    vol = "rec" if (not disk["shared"] and str(p).startswith(str(REC.resolve()))) else "data"
    d = disk[vol]
    used = d["base"] + (_tree_gb(DATA / "events") if vol == "data" else 0)   # not nvr.db: its bytes are real
    return Usage(int(d["total"] * GB), int(used * GB), int(max(0, d["total"] - used) * GB))


diskguard.shutil.disk_usage = fake_usage   # the same shutil module retention and keep use
retention.same_volume = lambda: disk["shared"]

db.upsert_camera({"id": "cam1", "name": "Cam 1", "host": "10.0.0.1"})
db.set_setting("retention_policy", {"continuous_days": 1, "min_free_gb": 100})


def reset() -> None:
    db.execute("DELETE FROM locks")
    db.execute("DELETE FROM events")
    for d in (DATA / "events").glob("*") if (DATA / "events").exists() else []:
        for f in d.glob("*"):
            f.unlink()
        d.rmdir()
    disk.update({"data": {"total": 1000, "base": 0}, "rec": {"total": 1000, "base": 0}, "shared": True})
    retention._prune_cursor.clear()


def make_event(start_ts: float, clip_gb: int = 40, crops: int = 2, cls: str = "vehicle", status: str = "rejected") -> int:
    eid = db.create_event(camera_id="cam1", track_id="t", camera_class=cls, start_ts=start_ts, end_ts=start_ts + 10,
                          status=status)
    d = DATA / "events" / str(eid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "snapshot.jpg").write_bytes(b"s")
    (d / "wide.jpg").write_bytes(b"w")
    for i in range(crops):
        (d / f"crop_{i}.jpg").write_bytes(b"c")
    if clip_gb:
        (d / "clip.mp4").write_bytes(b"x" * clip_gb)
        db.update_event(eid, clip=f"events/{eid}/clip.mp4")
    return eid


def has_clip(eid: int) -> bool:
    return (DATA / "events" / str(eid) / "clip.mp4").exists()


def clip_col(eid: int):
    return db.one("SELECT clip FROM events WHERE id=?", [eid])["clip"]


def test_floor_deletes_event_media_oldest_first_to_hysteresis():
    reset()
    now = time.time()
    # 20 events x 44 GB (40 clip + wide + 2 crops + snapshot) + 60 other = 940 used: 60 GB free, under the
    # floor of 100 (site policy); the target is 100 + 5% of 1000 = 150. Each event frees 43 (snapshot stays).
    ids = [make_event(now - 3 * 86400 + i * 600) for i in range(20)]
    disk["data"]["base"] = 60
    stats = Counter()
    retention.enforce_disk_floor(False, stats, now)
    gone = [i for i in ids if not has_clip(i)]
    assert gone == ids[:len(gone)], "oldest first"
    assert retention.data_free_gb() >= 150, "stops above floor + hysteresis, not at the floor"
    assert len(gone) == 3, f"no further than needed ({len(gone)})"   # 60 + 3 x 43 = 189
    for i in gone:
        d = DATA / "events" / str(i)
        assert (d / "snapshot.jpg").exists(), "snapshot kept"
        assert not (d / "wide.jpg").exists() and not list(d.glob("crop_*.jpg"))
        assert clip_col(i) is None
    assert clip_col(ids[3]) is not None
    assert stats["floor_event_clips_deleted"] == 3
    assert retention._continuous_breach is None   # shared disk: the same pass satisfied the recordings floor too


def test_floor_ignores_keep_windows_and_reaches_into_the_window():
    reset()
    now = time.time()
    old = make_event(now - 3 * 86400, cls="person", status="verified")     # kept by policy past the window
    new = [make_event(now - 3600 + i) for i in range(5)]                    # inside the continuous window
    disk["data"]["base"] = 1000 - 43 * 6 - 60   # 54 GB free: the old event is not enough
    retention.enforce_disk_floor(False, Counter(), now)
    assert not has_clip(old), "keep windows don't protect clips from the floor"
    assert not has_clip(new[0]), "in-window clips go once the old ones are not enough"
    assert has_clip(new[-1])


def test_locked_events_skipped_unless_nothing_else_left():
    reset()
    now = time.time()
    locked = make_event(now - 3 * 86400)
    db.execute("INSERT INTO locks (camera_id, start_ts, end_ts, event_id, created_at) VALUES ('cam1', ?, ?, ?, ?)",
               [now - 3 * 86400, now - 3 * 86400 + 10, locked, now])
    overlapped = make_event(now - 2 * 86400)   # no event_id on the lock, but footage around it is locked
    db.execute("INSERT INTO locks (camera_id, start_ts, end_ts, created_at) VALUES ('cam1', ?, ?, ?)",
               [now - 2 * 86400 - 5, now - 2 * 86400 + 5, now])
    other = make_event(now - 86400 * 1.5)
    disk["data"]["base"] = 1000 - 43 * 3 - 80   # 77 GB free; one event (43) reaches 120 >= floor, short of 150
    retention.enforce_disk_floor(False, Counter(), now)
    assert not has_clip(other)
    assert has_clip(locked) and has_clip(overlapped), "locked media survives while anything else is left"
    disk["data"]["base"] += 60                  # still short with only locked media left
    retention.enforce_disk_floor(False, Counter(), now)
    assert not has_clip(locked), "last resort: locked media goes when nothing else is left"


def test_separate_data_volume_has_its_own_floor():
    reset()
    now = time.time()
    disk["shared"] = False
    disk["data"] = {"total": 250, "base": 0}    # separate (OS) disk: floor 10 % capped at 20 GB, target 20 + 12.5
    disk["rec"] = {"total": 8000, "base": 0}    # recordings disk has plenty of room
    ids = [make_event(now - 3 * 86400 + i, clip_gb=10, crops=0) for i in range(22)]   # 22 x 12 = 264 > 250
    assert retention.data_floor() == (20.0, 32.5)
    retention.enforce_disk_floor(False, Counter(), now)
    assert retention.data_free_gb() >= 32.5
    assert not has_clip(ids[0]) and has_clip(ids[-1])
    retention._update_alert()
    # the pass stops at the hysteresis target, which is above 2x the emergency floor: no warning afterwards
    assert not (retention.alert and retention.alert.get("kind") == "event_media"), "above 2x floor: no warning"
    disk["data"]["base"] = disk["data"]["total"] - 8   # 8 GB free on a 250 GB OS disk: under 2x the 20 GB floor
    retention._update_alert()
    assert retention.alert and retention.alert["kind"] == "event_media", "under 2x floor: warn in Settings"


def test_floor_dry_run_deletes_nothing():
    reset()
    now = time.time()
    ids = [make_event(now - 3 * 86400 + i) for i in range(3)]
    disk["data"]["base"] = 1000 - 43 * 3 - 50
    settings.retention_dry_run = True
    try:
        stats = Counter()
        retention.enforce_disk_floor(True, stats, now)
    finally:
        settings.retention_dry_run = False
    assert all(has_clip(i) for i in ids) and stats["dry_floor_event_clips"] >= 3


def test_adaptive_batch():
    b, m = retention.EVENT_MEDIA_BATCH, retention.EVENT_MEDIA_BATCH_MAX
    assert retention.event_media_batch(100, 900, 100) == b       # small backlog, plenty of room
    assert retention.event_media_batch(5000, 900, 100) == m      # large backlog
    assert retention.event_media_batch(10, 150, 100) == m        # disk under 2x floor


def test_prune_walks_past_kept_events():
    """550 kept events at the front of the queue must not stop the prune from reaching the 50 behind them."""
    reset()
    now = time.time()
    kept = [make_event(now - 5 * 86400 + i, clip_gb=0, crops=0, cls="person", status="verified") for i in range(550)]
    for i in kept:
        db.update_event(i, clip=f"events/{i}/clip.mp4")
    drop = [make_event(now - 4 * 86400 + i, clip_gb=1, crops=1) for i in range(50)]
    retention.prune_event_media(now, False, Counter())
    assert all(clip_col(i) is None for i in drop), "unkept clips behind the kept ones are pruned"
    assert all(clip_col(i) is not None for i in kept[:5])
    assert all((DATA / "events" / str(i) / "snapshot.jpg").exists() for i in drop)


def test_rate_guard_decide():
    d = retention.EventRateGuard.decide
    assert d(600, 600, None, 0) == (False, False)          # at the limit: fine
    assert d(601, 600, None, 0) == (True, True)            # over: suppress and say so
    assert d(1440, 600, 100, 1000) == (True, False)        # said so within the hour: quiet
    assert d(1440, 600, 100, 100 + 3600) == (True, True)   # an hour later: say it again
    assert d(10_000, 0, None, 0) == (False, False)         # 0 = no limit


def test_rate_guard_check_and_alert():
    reset()
    g = retention.EventRateGuard()
    g.count = lambda cam, now: 1440
    assert g.check("cam4", 1000.0) and "cam4" in g.suppressed
    g.count = lambda cam, now: 10
    assert not g.check("cam4", 2000.0) and "cam4" not in g.suppressed
    retention.rate_guard.suppressed["cam4"] = {"since": 0, "count": 1440}
    try:
        retention._update_alert()
        assert "cam4: 1,440 events in the last hour; clips suppressed" in retention.alert["message"]
    finally:
        retention.rate_guard.suppressed.clear()
    eid = make_event(time.time())
    retention.drop_clip_media(eid)
    assert not has_clip(eid) and clip_col(eid) is None and (DATA / "events" / str(eid) / "snapshot.jpg").exists()


def test_emergency_free_without_database():
    """At ~0 bytes free: filesystem only, oldest ids first, older than a day before newer, snapshot kept; the
    database learns about it afterwards."""
    reset()
    now = time.time()
    ids = [make_event(now - 3 * 86400 + i) for i in range(4)]
    for i in ids[:2]:                       # the two oldest folders are over a day old
        os.utime(DATA / "events" / str(i), (now - 2 * 86400, now - 2 * 86400))
    disk["data"]["base"] = 1000 - 43 * 4    # 0 bytes free (176 GB of events)
    diskguard.cleared.clear()
    freed = diskguard.emergency_free(DATA, int(50 * GB))
    assert freed >= 50 and not has_clip(ids[0]) and not has_clip(ids[1]) and has_clip(ids[2])
    assert diskguard.cleared == ids[:2]
    assert (DATA / "events" / str(ids[0]) / "snapshot.jpg").exists()
    db._null_cleared_clips()                # what Database.__init__ does once it is open
    assert clip_col(ids[0]) is None and clip_col(ids[2]) is not None and not diskguard.cleared
    # nothing to do when there is room
    assert diskguard.emergency_free(DATA, int(1 * GB)) == 0


def test_enforce_prunes_early_under_pressure():
    reset()
    now = time.time()
    ids = [make_event(now - 3 * 86400 + i) for i in range(10)]   # past the 1-day window, not kept
    disk["data"]["base"] = 1000 - 440 - 130   # 130 GB free: above the floor, under 1.5x and 2x
    retention.enforce()
    assert all(clip_col(i) is None for i in ids), "expired clips cleared"
    assert retention.last_pass.get("event_media_catch_up"), "big batch under 2x floor"
    disk["data"]["base"] = 0
    retention.enforce()
    assert retention.alert is None


def test_data_floor_on_a_quota_dataset():
    """A central instance's /data: its own quota'd dataset (statvfs reports the quota). 10 %, at least 2 GB."""
    reset()
    disk["shared"] = False
    disk["data"] = {"total": 50, "base": 0}
    assert retention.data_floor() == (5.0, 7.5)
    disk["data"] = {"total": 10, "base": 0}
    assert retention.data_floor() == (2.0, 2.5)


def test_floor_frees_new_events_without_a_clip_column():
    """New events keep no clip: the floor finds their keyframes and crops from the folder, not the clip column."""
    reset()
    now = time.time()
    ids = [make_event(now - 3 * 86400 + i * 600, clip_gb=0, crops=0) for i in range(10)]
    for i in ids:
        (DATA / "events" / str(i) / "crop_0.jpg").write_bytes(b"c" * 40)   # 41 GB of pictures each (+ snapshot)
        assert clip_col(i) is None
    disk["data"]["base"] = 1000 - 42 * 10 - 30
    stats = Counter()
    retention.enforce_disk_floor(False, stats, now)
    gone = [i for i in ids if not (DATA / "events" / str(i) / "crop_0.jpg").exists()]
    assert gone == ids[:len(gone)] and 2 <= len(gone) <= 4, gone      # 30 + 3 x 41 >= 150
    assert all((DATA / "events" / str(i) / "snapshot.jpg").exists() for i in ids)
    assert stats["floor_event_clips_deleted"] == len(gone)


def test_floor_deletes_files_even_when_the_database_cannot_write():
    """Files first, rows after: a full disk makes the UPDATE fail; the space is freed anyway and the ids wait."""
    import sqlite3
    reset()
    now = time.time()
    ids = [make_event(now - 3 * 86400 + i) for i in range(6)]
    disk["data"]["base"] = 1000 - 44 * 6
    real = db.execute

    def full(sql, params=()):
        if sql.lstrip().upper().startswith("UPDATE"):
            raise sqlite3.OperationalError("database or disk is full")
        return real(sql, params)
    diskguard.cleared.clear()
    db.execute = full
    try:
        retention.enforce_disk_floor(False, Counter(), now)
    finally:
        db.execute = real
    gone = [i for i in ids if not has_clip(i)]
    assert len(gone) >= 3, "the space was freed although no row could be written"
    assert all(clip_col(i) is not None for i in gone) and sorted(diskguard.cleared) == sorted(gone)
    retention.emergency_free()   # the next pass: rows catch up once the database writes again
    assert all(clip_col(i) is None for i in gone) and not diskguard.cleared


def test_floor_falls_back_to_the_filesystem_when_the_database_cannot_be_read():
    import sqlite3
    reset()
    now = time.time()
    ids = [make_event(now - 3 * 86400 + i) for i in range(4)]
    for i in ids:
        os.utime(DATA / "events" / str(i), (now - 2 * 86400, now - 2 * 86400))
    disk["data"]["base"] = 1000 - 44 * 4
    real = db.all

    def dead(sql, params=()):
        raise sqlite3.OperationalError("disk I/O error")
    db.all = dead
    stats = Counter()
    try:
        retention.data_floor_step(150, False, stats)
    finally:
        db.all = real
    assert stats["floor_fs_only_bytes"] > 0 and not has_clip(ids[0])
    assert (DATA / "events" / str(ids[0]) / "snapshot.jpg").exists()


def test_floor_counts_what_it_deleted_when_free_space_lags():
    """ZFS hands a deleted file's space back seconds later: statvfs alone would delete every event's media."""
    reset()
    now = time.time()
    ids = [make_event(now - 3 * 86400 + i, clip_gb=0, crops=0) for i in range(8)]
    for i in ids:
        (DATA / "events" / str(i) / "crop_0.jpg").write_bytes(b"c" * 1000)
    old = diskguard.shutil.disk_usage
    diskguard.shutil.disk_usage = lambda p: Usage(int(1e12), int(1e12), 0)   # always 0 free, whatever is deleted
    try:
        n = retention.free_event_media(2500e-9, False, Counter())   # 2,500 bytes wanted: three 1,001-byte folders
    finally:
        diskguard.shutil.disk_usage = old
    assert n == 3, n
    assert [i for i in ids if (DATA / "events" / str(i) / "crop_0.jpg").exists()] == ids[3:]


def test_enforce_frees_the_data_disk_before_curating():
    reset()
    now = time.time()
    ids = [make_event(now - 3 * 86400 + i) for i in range(5)]
    disk["data"]["base"] = 1000 - 44 * 5 - 40       # 40 GB free: under the floor
    seen = []
    real = retention.curate_camera
    retention.curate_camera = lambda cam, now, dry, stats: seen.append(retention.data_free_gb())
    try:
        retention.enforce()
    finally:
        retention.curate_camera = real
    assert seen and seen[0] >= 100, "the floor ran before curation (which writes rows)"




def test_a_window_from_the_segments_start_survives_its_trim():
    """A kept window that starts at the segment's first fragment is written under the segment's own name (trims are
    named by their start), over the original; the original was then removed, taking the kept footage with it
    (fred 2026-11-07: event #326's footage, and five more files of 10-28 that each began a segment)."""
    import shutil
    from nvr import fmp4
    cam = {"id": "camtrim"}
    d = REC / cam["id"]
    d.mkdir(parents=True, exist_ok=True)
    fix = Path(__file__).parent / "fixtures" / "av_2track.mp4"
    start = time.time() - 30 * 86400
    first = d / fmp4.segment_name(start)
    shutil.copy(fix, first)
    shutil.copy(fix, d / fmp4.segment_name(start + 600))   # the newest is never curated
    real = (keep.keep_windows, keep.policy_for)
    keep.keep_windows = lambda c, s, e, p: ([{"start": s, "end": s + 2.0, "reasons": ["person"], "score": 1}], False)
    keep.policy_for = lambda c: {"continuous_days": 7}
    try:
        stats = Counter()
        retention.curate_camera(cam, time.time(), False, stats)
    finally:
        keep.keep_windows, keep.policy_for = real
    assert stats["trimmed"] == 1
    kept = db.all("SELECT file FROM kept_footage WHERE camera_id=?", [cam["id"]])
    assert [Path(r["file"]).name for r in kept] == [first.name]
    assert first.is_file() and first.stat().st_size < fix.stat().st_size   # the trimmed piece, still there
    db.execute("DELETE FROM kept_footage WHERE camera_id=?", [cam["id"]])
    shutil.rmtree(d)


# ---------------------------------------------------------------- the recordings disk: kept and continuous footage

def rec_reset() -> None:
    reset()
    db.execute("DELETE FROM kept_footage")
    d = REC / "cam1"
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("*.mp4"):
        f.unlink()
    retention._continuous_breach = None


def make_segment(age_days: float, size: int = 30) -> Path:
    """A recording file of cam1 that started `age_days` ago (continuous footage: no kept_footage row)."""
    from nvr import fmp4
    f = REC / "cam1" / fmp4.segment_name(time.time() - age_days * 86400)
    f.write_bytes(b"k" * size)
    return f


def make_kept(age_days: float, score: float, size: int = 30) -> Path:
    f = make_segment(age_days, size)
    start = time.time() - age_days * 86400
    db.execute("INSERT INTO kept_footage (camera_id, start_ts, end_ts, file, reasons, score, bytes, created_at) "
               "VALUES (?,?,?,?,?,?,?,?)", ["cam1", start, start + 60, str(f), '["person"]', score, size, time.time()])
    return f


def lock(age_days: float, seconds: float = 60, event_id: int | None = None) -> None:
    start = time.time() - age_days * 86400
    db.execute("INSERT INTO locks (camera_id, start_ts, end_ts, event_id, note, created_at) VALUES (?,?,?,?,?,?)",
               ["cam1", start - 1, start + seconds, event_id, "", time.time()])


def kept_names() -> set[str]:
    return {Path(r["file"]).name for r in db.all("SELECT file FROM kept_footage")}


@contextmanager
def rec_disk(free: float, lag: bool = False):
    """The recordings disk on a volume of its own with `free` GB free, each byte of footage counting as 1 GB.
    lag: the filesystem's figure never moves, as on ZFS in the seconds after a delete."""
    files = lambda: sum(f.stat().st_size for f in (REC / "cam1").glob("*.mp4"))   # noqa: E731
    base = files()
    real = (retention.free_gb, retention._remove)

    def remove(f: Path) -> int:
        size = f.stat().st_size
        f.unlink()
        return int(size * GB)

    retention.free_gb = lambda: free if lag else free + base - files()
    retention._remove = remove
    disk["shared"] = False
    try:
        yield
    finally:
        retention.free_gb, retention._remove = real
        disk["shared"] = True


def test_a_full_recordings_disk_gives_up_the_least_important_kept_footage_first():
    rec_reset()
    low, low_older, high = make_kept(20, score=30), make_kept(30, score=30), make_kept(40, score=90)
    continuous, newest = make_segment(0.5), make_segment(0.001)
    with rec_disk(free=75):   # floor 100: 25 GB short, one 30 GB file is enough
        stats = Counter()
        retention.enforce_disk_floor(False, stats)
        assert stats["deleted_kept"] == 1 and not low_older.exists(), "the oldest of the least important goes first"
        assert low.exists() and high.exists() and continuous.exists() and newest.exists()
        assert kept_names() == {low.name, high.name}, "its row went with it"
        assert not stats["deleted_continuous"] and retention._continuous_breach is None
    with rec_disk(free=45):   # 55 GB short: the other low one, then the important one
        stats = Counter()
        retention.enforce_disk_floor(False, stats)
        assert stats["deleted_kept"] == 2 and not low.exists() and not high.exists()
        assert continuous.exists() and not stats["deleted_continuous"] and retention._continuous_breach is None


def test_locked_kept_footage_is_never_given_up():
    rec_reset()
    locked, other = make_kept(30, score=30), make_kept(20, score=90)
    lock(30)
    make_segment(0.001)
    with rec_disk(free=75):
        stats = Counter()
        retention.enforce_disk_floor(False, stats)
    assert locked.exists() and not other.exists(), "the locked file stays although it scores lowest"
    assert kept_names() == {locked.name}


def test_a_full_recordings_disk_counts_what_it_deleted_when_free_space_lags():
    """ZFS hands a deleted file's space back seconds later. Judged by statvfs alone, a Site at its quota lost all of
    its kept footage and then its continuous footage, with a capacity alert, for the sake of two files."""
    rec_reset()
    kept = [make_kept(30 - i, score=30) for i in range(4)]
    continuous = [make_segment(0.9), make_segment(0.8)]
    make_segment(0.001)
    with rec_disk(free=60, lag=True):   # 40 GB short: two 30 GB files
        stats = Counter()
        retention.enforce_disk_floor(False, stats)
    assert stats["deleted_kept"] == 2, stats
    assert [f.exists() for f in kept] == [False, False, True, True]
    assert all(f.exists() for f in continuous) and not stats["deleted_continuous"]
    assert retention._continuous_breach is None, "no capacity alert: enough was freed"


def test_continuous_footage_goes_last_skips_locks_and_raises_the_alert():
    rec_reset()
    kept = make_kept(30, score=90)
    oldest, locked, third = make_segment(0.9), make_segment(0.8), make_segment(0.7)
    lock(0.8)
    newest = make_segment(0.001)
    try:
        with rec_disk(free=20):   # 80 GB short: the kept file (30), then two continuous files (30 each)
            stats = Counter()
            retention.enforce_disk_floor(False, stats)
            assert not kept.exists() and stats["deleted_kept"] == 1, "kept footage goes before continuous"
            assert not oldest.exists() and not third.exists() and stats["deleted_continuous"] == 2
            assert locked.exists(), "a locked stretch of continuous footage stays"
            assert newest.exists(), "never the file being written"
            assert retention._continuous_breach and "continuous" in retention._continuous_breach["message"]
            retention._update_alert()
            assert retention.alert and retention.alert["kind"] == "continuous"
    finally:
        retention._continuous_breach = None
        retention._update_alert()


def test_floor_steps_share_what_they_freed_when_free_space_lags():
    """Each data-disk step counted its own deletions, but the next step asked statvfs again: with ZFS still reporting
    the old figure, event pictures inside the continuous window went, and then a locked event's."""
    reset()
    now = time.time()

    def event(ts: float) -> int:
        eid = make_event(ts, clip_gb=0, crops=0)
        (DATA / "events" / str(eid) / "crop_0.jpg").write_bytes(b"c" * 1000)
        return eid

    locked = event(now - 4 * 86400)
    lock(4, event_id=locked)
    old = [event(now - 3 * 86400 + i) for i in range(4)]       # past the 1-day window
    new = [event(now - 3600 + i) for i in range(3)]            # inside it
    real = (diskguard.shutil.disk_usage, retention.free_gb, settings.event_media_min_free_gb)
    diskguard.shutil.disk_usage = lambda p: Usage(0, 0, 0)     # the data disk reports 0 free, whatever is deleted
    retention.free_gb = lambda: 1e6                            # the recordings disk has room
    settings.event_media_min_free_gb = 2500e-9                 # 2,500 bytes wanted: three 1,001-byte folders
    disk["shared"] = False
    try:
        stats = Counter()
        retention.enforce_disk_floor(False, stats, now)
    finally:
        diskguard.shutil.disk_usage, retention.free_gb, settings.event_media_min_free_gb = real
        disk["shared"] = True
        retention._continuous_breach = None
    has = lambda i: (DATA / "events" / str(i) / "crop_0.jpg").exists()   # noqa: E731
    assert stats["floor_event_clips_deleted"] == 3, stats
    assert [has(i) for i in old] == [False, False, False, True]
    assert all(has(i) for i in new), "nothing inside the continuous window was touched"
    assert has(locked), "the locked event keeps its pictures"


# ---------------------------------------------------------------- the keep rules' event lookup

def test_keep_windows_reads_only_the_events_that_can_reach_the_stretch():
    """The lookup had no lower bound on an event's start, so it read every earlier event of the camera on each call:
    351 ms a call at 98,640 events (fred 2026-11-10), and retention passes of 46 minutes."""
    reset()
    t = time.time() - 5 * 86400
    for i in range(50):
        db.create_event(camera_id="cam1", track_id="t", camera_class="vehicle", start_ts=t - 86400 + i * 60,
                        end_ts=t - 86400 + i * 60 + 10, status="rejected")
    db.create_event(camera_id="cam1", track_id="t", camera_class="person", start_ts=t, end_ts=t + 500, status="verified")
    cam = next(c for c in db.cameras() if c["id"] == "cam1")
    assert abs(keep.longest_event_s(db, "cam1") - 500) < 0.01
    seen = []
    real = db.all
    db.all = lambda sql, params=(): (seen.append((sql, list(params))), real(sql, params))[1]
    try:
        windows, _ = keep.keep_windows(cam, t + 400, t + 460, keep.policy_for(cam))
    finally:
        db.all = real
    assert len(windows) == 1 and windows[0]["reasons"] == ["person"], "an event that began 400 s before the stretch"
    assert (windows[0]["start"], windows[0]["end"]) == (t + 400, t + 460)
    sql, params = next((s, p) for s, p in seen if "FROM events" in s)
    plan = " ".join(r["detail"] for r in db.all("EXPLAIN QUERY PLAN " + sql, params))
    assert "start_ts>? AND start_ts<?" in plan, plan                 # both ends of the range come from the index
    plan = " ".join(r["detail"] for r in db.all(
        "EXPLAIN QUERY PLAN SELECT MAX(COALESCE(end_ts, start_ts) - start_ts) AS s FROM events WHERE camera_id=?", ["cam1"]))
    assert "events_cam_len" in plan, plan                            # the longest event is one index lookup
    assert keep.longest_event_s(db, "no-such-camera") == 0.0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
