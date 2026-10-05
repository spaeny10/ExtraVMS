"""Event media must not be able to fill the disk: the data-volume floor, the prune catch-up, the event-rate guard
and the no-database emergency free used at startup. Disk space is faked (each byte on disk counts as 1 GB) so the
floor logic runs against a scratch data_dir. Run as a script (test.ps1) or under pytest."""
import os
import sys
import tempfile
import time
from collections import Counter, namedtuple
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
    disk["data"] = {"total": 250, "base": 0}    # separate (OS) disk: emergency floor 5 GB, target 5 + 12.5
    disk["rec"] = {"total": 8000, "base": 0}    # recordings disk has plenty of room
    ids = [make_event(now - 3 * 86400 + i, clip_gb=10, crops=0) for i in range(22)]   # 22 x 12 = 264 > 250
    assert retention.data_floor() == (5.0, 17.5)
    retention.enforce_disk_floor(False, Counter(), now)
    assert retention.data_free_gb() >= 17.5
    assert not has_clip(ids[0]) and has_clip(ids[-1])
    retention._update_alert()
    # the pass stops at the hysteresis target, which is above 2x the emergency floor: no warning afterwards
    assert not (retention.alert and retention.alert.get("kind") == "event_media"), "above 2x floor: no warning"
    disk["data"]["base"] = disk["data"]["total"] - 8   # 8 GB free on a 250 GB OS disk: under 2x the 5 GB floor
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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
