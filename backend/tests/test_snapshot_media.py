"""Event snapshots at 1280 px (snapshots.py), rejected events keeping only a 320 px thumbnail after
rejected_media_days (retention.purge_rejected_media), the media endpoint serving that thumbnail, and the one-off
`python -m nvr.snapshot_shrink` (dry run by default; a kept event whose recording is gone keeps its full-size picture).
Throwaway data / recordings / runtime dirs.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_snapshot_media.py   (from backend/), or under pytest.
"""
import asyncio
import contextlib
import io
import os
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="nvr-snapmedia-test-")   # never the real DB, recordings or runtime config
os.environ["NVR_DATA_DIR"] = str(Path(_TMP, "data"))
os.environ["NVR_RECORDINGS_DIR"] = str(Path(_TMP, "recordings"))
os.environ["NVR_RUNTIME_DIR"] = str(Path(_TMP, "runtime"))
os.environ["NVR_BACKUP_DIR"] = str(Path(_TMP, "backups"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from nvr import diskguard, ppe, retention, snapshot_shrink, snapshots  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402
from nvr.fmp4 import segment_name  # noqa: E402

DATA = settings.data_dir
REC = settings.recordings_dir
NOW = time.time()
DAY = 86400


def camera(cid):
    db.upsert_camera({"id": cid, "name": cid, "host": "10.0.0.5", "onvif_port": 80, "rtsp_port": 554, "username": "u",
                      "password": "p", "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": [],
                      "retention_days": None, "scene_notes": "", "retention_policy": None, "policies": []})


for _c in ("rj", "sh", "ep"):
    camera(_c)


def scene(w=2592, h=1520) -> np.ndarray:
    """A camera-like frame: gradients, edges and some noise (a flat picture compresses to nothing)."""
    rng = np.random.default_rng(1)
    img = np.zeros((h, w, 3), np.uint8)
    img[:] = np.linspace(40, 200, w, dtype=np.uint8)[None, :, None]
    for i in range(60):
        x, y = int(rng.integers(0, w - 200)), int(rng.integers(0, h - 200))
        cv2.rectangle(img, (x, y), (x + int(rng.integers(40, 200)), y + int(rng.integers(40, 200))),
                      tuple(int(v) for v in rng.integers(0, 255, 3)), -1)
    noise = rng.normal(0, 12, img.shape)
    return np.clip(img + noise, 0, 255).astype(np.uint8)


def full_jpeg(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), scene(), [cv2.IMWRITE_JPEG_QUALITY, 88])
    return path.stat().st_size


def event(cam, start, status="rejected", pictures=True, **cols) -> int:
    eid = db.create_event(camera_id=cam, track_id=f"t{time.time()}{start}", camera_class="person", start_ts=start,
                          end_ts=start + 5, status=status, **cols)
    d = DATA / "events" / str(eid)
    d.mkdir(parents=True, exist_ok=True)
    if pictures:
        full_jpeg(d / "snapshot.jpg")
        for name in ("wide.jpg", "crop_0.jpg", "crop_1.jpg", "thumb_480.jpg", "chat_1000_640.jpg"):
            (d / name).write_bytes(b"x" * 500)
        db.update_event(eid, snapshot=f"events/{eid}/snapshot.jpg")
    return eid


def segment(cam: str, start: float, seconds: float) -> None:
    """An (empty) recording segment: the coverage check reads only its name and last write."""
    d = REC / cam
    d.mkdir(parents=True, exist_ok=True)
    f = d / segment_name(start)
    f.write_bytes(b"")
    os.utime(f, (start + seconds, start + seconds))


# ---------------------------------------------------------------- new snapshots

def test_new_snapshots_are_at_most_1280_px_and_much_smaller():
    img = scene()
    data = snapshots.encode(img)
    small = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert small.shape[1] == 1280 and small.shape[0] == round(1520 * 1280 / 2592), small.shape
    full = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()
    assert len(data) < len(full) / 3, (len(data), len(full))
    assert snapshots.fit(np.zeros((180, 320, 3), np.uint8)).shape == (180, 320, 3)   # never upscaled
    assert snapshots.scale_for(1520, 2592) == 1280 / 2592                              # portrait: the long side


def test_verifier_and_ppe_write_snapshots_through_snapshots_py():
    src = Path(snapshots.__file__).with_name("verifier.py").read_text(encoding="utf-8")
    assert 'snapshots.write(out_dir / "snapshot.jpg"' in src and "IMWRITE_JPEG_QUALITY, 88])\n        keyframes" not in src
    eid = event("ep", NOW - 3600, status="verified", pictures=False)
    images = {1.0: scene()}
    result = {"verdict": "violation", "violation": ["hard_hat"], "frames": [{"ts": 1.0, "box": [0.4, 0.3, 0.6, 0.9]}]}
    assert ppe.save_marked(eid, result, images) == "ppe.jpg"
    d = DATA / "events" / str(eid)
    with Image.open(d / "ppe.jpg") as im:
        assert im.size == (2592, 1520)        # the check's evidence: full resolution from the recording
    with Image.open(d / "snapshot.jpg") as im:
        assert im.size == (1280, 751)         # the event's picture: snapshot size


# ---------------------------------------------------------------- rejected events: a thumbnail only after 3 days

def test_rejected_events_keep_only_a_thumbnail_after_the_setting():
    old = NOW - 4 * DAY
    plain = event("rj", old)
    recent = event("rj", NOW - 1 * DAY)
    verified = event("rj", old + 10, status="verified")
    rated = event("rj", old + 20, feedback={"verdict": "missed", "note": "a person"})
    corrected = event("rj", old + 30, corrected_at=NOW)
    locked = event("rj", old + 40)
    db.execute("INSERT INTO locks (camera_id, start_ts, end_ts, event_id, note, created_at) VALUES ('rj', ?, ?, ?, '', ?)",
               [old + 35, old + 50, locked, NOW])
    high = event("rj", old + 60, priority="high")
    noted = event("rj", old + 70)
    db.execute("INSERT INTO chat_messages (event_id, role, content, ts, saved) VALUES (?, 'user', 'note', ?, 1)",
               [noted, NOW])
    lost = event("rj", old + 80, pictures=False)   # the snapshot is already gone (a failed disk): no picture left
    (DATA / "events" / str(lost) / "crop_0.jpg").write_bytes(b"c" * 300)
    db.update_event(lost, snapshot=f"events/{lost}/snapshot.jpg")

    stats = Counter()
    retention._rejected_cursor = (float("-inf"), -1)
    retention.purge_rejected_media(NOW, True, stats)                   # dry run: counts, deletes nothing
    assert stats["dry_rejected_media"] == 2 and stats["rejected_media_kept"] == 5, stats
    assert (DATA / "events" / str(plain) / "snapshot.jpg").exists()

    stats = Counter()
    retention.purge_rejected_media(NOW, False, stats)
    assert stats["rejected_media_deleted"] == 2 and stats["rejected_media_kept"] == 5, stats
    d = DATA / "events" / str(plain)
    assert sorted(f.name for f in d.iterdir()) == ["thumb_320.jpg"], list(d.iterdir())
    with Image.open(d / "thumb_320.jpg") as im:
        assert im.width == 320
    assert db.event(plain)["snapshot"] == f"events/{plain}/thumb_320.jpg"
    assert stats["freed_bytes"] > 100_000
    assert db.event(lost)["snapshot"] is None and not any((DATA / "events" / str(lost)).iterdir())
    for keep_id in (recent, verified, rated, corrected, locked, high, noted):
        kd = DATA / "events" / str(keep_id)
        assert (kd / "snapshot.jpg").exists() and (kd / "crop_0.jpg").exists(), keep_id
        assert db.event(keep_id)["snapshot"].endswith("snapshot.jpg")
    # the next pass finds nothing new; disk-floor cleanup keeps the thumbnail
    stats = Counter()
    retention.purge_rejected_media(NOW, False, stats)
    assert not stats["rejected_media_deleted"]
    assert not diskguard.has_media(d) and diskguard.delete_event_media(d) == 0 and (d / "thumb_320.jpg").exists()


def test_rejected_media_days_zero_keeps_everything():
    eid = event("rj", NOW - 30 * DAY)
    old_days = settings.rejected_media_days
    settings.rejected_media_days = 0
    try:
        stats = Counter()
        retention._rejected_cursor = (float("-inf"), -1)
        retention.purge_rejected_media(NOW, False, stats)
        assert not stats and (DATA / "events" / str(eid) / "snapshot.jpg").exists()
    finally:
        settings.rejected_media_days = old_days
    assert settings.rejected_media_days == 3   # NVR_REJECTED_MEDIA_DAYS default


def test_rejected_purge_walks_past_kept_events_and_survives_a_failed_update():
    old = NOW - 10 * DAY
    kept_ids = [event("rj", old + i, feedback={"verdict": "correct"}, pictures=False) for i in range(5)]
    for k in kept_ids:   # a snapshot column but no file: still "kept", walked past
        db.update_event(k, snapshot=f"events/{k}/snapshot.jpg")
    target = event("rj", old + 100)
    retention._rejected_cursor = (float("-inf"), -1)
    real = db.execute
    calls = {"n": 0}

    def failing(sql, params=()):
        if sql.startswith("UPDATE events SET snapshot = CASE"):
            calls["n"] += 1
            import sqlite3
            raise sqlite3.OperationalError("database or disk is full")
        return real(sql, params)
    db.execute = failing
    try:
        for _ in range(3):   # pages of 3: the kept events fill the first page, the cursor moves on
            retention.purge_rejected_media(NOW, False, Counter(), page=3)
    finally:
        db.execute = real
    assert calls["n"] >= 1
    d = DATA / "events" / str(target)
    assert not (d / "snapshot.jpg").exists() and (d / "thumb_320.jpg").exists()
    assert db.event(target)["snapshot"].endswith("snapshot.jpg")   # the update failed: fixed on a later pass
    retention._rejected_cursor = (float("-inf"), -1)
    for _ in range(3):
        retention.purge_rejected_media(NOW, False, Counter(), page=3)
    assert db.event(target)["snapshot"] == f"events/{target}/thumb_320.jpg"


def test_media_endpoint_serves_the_stored_thumbnail_when_the_snapshot_is_gone():
    from nvr import api
    eid = event("rj", NOW - 5 * DAY)
    retention._rejected_cursor = (float("-inf"), -1)
    retention.purge_rejected_media(NOW, False, Counter())
    assert not (DATA / "events" / str(eid) / "snapshot.jpg").exists()
    resp = asyncio.run(api.event_media(eid, "snapshot.jpg", 480))
    assert Path(resp.path).name == "thumb_320.jpg"
    resp = asyncio.run(api.event_media(eid, "snapshot.jpg", None))
    assert Path(resp.path).name == "thumb_320.jpg"
    d = DATA / "events" / "777777"
    d.mkdir(parents=True)
    for w in (320, 640):
        (d / f"thumb_{w}.jpg").write_bytes(b"t")
    assert snapshots.stored_thumb(d, 480).name == "thumb_640.jpg"
    assert snapshots.stored_thumb(d, 960).name == "thumb_640.jpg"
    assert snapshots.stored_thumb(d, 200).name == "thumb_320.jpg"
    assert snapshots.stored_thumb(DATA / "events" / "nope", 320) is None


# ---------------------------------------------------------------- snapshot_shrink

def test_snapshot_shrink_dry_run_then_apply():
    base = NOW - 40 * DAY
    segment("sh", base, 600)                                     # the recording covers [base, base + 600)
    covered = event("sh", base + 100, status="verified")
    covered_kept = event("sh", base + 200, status="verified", priority="high")   # kept, but its video is there
    gone_plain = event("sh", base + 1000, status="verified")    # recording gone, nothing special: shrinks
    gone_weapon = event("sh", base + 1100, status="verified",
                        synopsis_json={"summary": "x", "weapon_check": {"verdict": "confirmed"}})
    gone_locked = event("sh", base + 1200, status="verified")
    db.execute("INSERT INTO locks (camera_id, start_ts, end_ts, event_id, note, created_at) VALUES ('sh', ?, ?, ?, '', ?)",
               [base + 1195, base + 1210, gone_locked, NOW])
    gone_feedback = event("sh", base + 1300, status="rejected", feedback={"verdict": "missed"})
    gone_clip = event("sh", base + 1400, status="verified", priority="high")      # a legacy clip is its video
    (DATA / "events" / str(gone_clip) / "clip.mp4").write_bytes(b"v" * 10)
    db.update_event(gone_clip, clip=f"events/{gone_clip}/clip.mp4")
    pending = event("sh", base + 1500, status="pending")
    small = event("sh", base + 1600, status="verified", pictures=False)
    snapshots.write(DATA / "events" / str(small) / "snapshot.jpg", scene())

    full = {i: (DATA / "events" / str(i) / "snapshot.jpg").stat().st_size
            for i in (covered, covered_kept, gone_plain, gone_weapon, gone_locked, gone_feedback, gone_clip)}
    dry = snapshot_shrink.scan(db, camera="sh", apply=False)
    assert (dry.to_shrink, dry.kept_full, dry.unfinished, dry.small) == (4, 3, 1, 1), dry
    assert dry.sampled == 4 and 0 < dry.saved() < dry.to_shrink_bytes
    assert all((DATA / "events" / str(i) / "snapshot.jpg").stat().st_size == s for i, s in full.items()), "dry run"
    text = snapshot_shrink.render(dry, False)
    assert "to shrink:" in text and "kept at full size" in text and "estimated saving" in text and "dry run" in text

    rep = snapshot_shrink.scan(db, camera="sh", apply=True)
    assert rep.shrunk == 4 and rep.saved() > 0 and sorted(rep.ids) == sorted([covered, covered_kept, gone_plain, gone_clip])
    for i in rep.ids:
        with Image.open(DATA / "events" / str(i) / "snapshot.jpg") as im:
            assert max(im.size) == 1280
    for i in (gone_weapon, gone_locked, gone_feedback):
        assert (DATA / "events" / str(i) / "snapshot.jpg").stat().st_size == full[i]
    assert not list((DATA / "events" / str(covered)).glob("*.tmp"))
    assert "shrank 4 snapshots" in snapshot_shrink.render(rep, True)
    again = snapshot_shrink.scan(db, camera="sh", apply=True)
    assert again.shrunk == 0 and again.small >= 5


def test_snapshot_shrink_cli_dry_run_reads_only():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = snapshot_shrink.main(["--camera", "sh"])
    assert code == 0 and "dry run" in buf.getvalue()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
