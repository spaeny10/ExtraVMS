"""Back-to-back fragments on one camera merge into one event before Qwen (merge.py), on a throwaway DB.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_merge.py   (from backend/)
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-merge-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from nvr import merge  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402
from nvr.verifier import event_dir  # noqa: E402

T0 = 1_790_000_000.0
db.upsert_camera({"id": "cam1", "name": "Yard", "host": "10.0.0.5", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": "p",
                  "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": [], "retention_days": None, "scene_notes": "", "retention_policy": None, "policies": []})


def box(t, cx, cy, w=0.1, h=0.3, conf=0.9):
    return [t, cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2, conf]


def make(cam="cam1", track="t1", cls="person", start=T0, dur=5.0, x0=0.3, x1=0.5, status="verified", **kw):
    path = [box(start, x0, 0.5), box(start + dur / 2, (x0 + x1) / 2, 0.5), box(start + dur, x1, 0.5)]
    return db.create_event(camera_id=cam, track_id=track, camera_class=cls, camera_conf=0.9, start_ts=start, end_ts=start + dur,
                           path=path, status=status, **kw)


def vec(seed, base=None):
    v = np.random.default_rng(seed).normal(size=512) if base is None else base + np.random.default_rng(seed).normal(size=512) * 0.02
    return v / np.linalg.norm(v)


def test_same_track_merges_and_cleans_up():
    a = make(track="77", start=T0, x1=0.5)
    b = make(track="77", start=T0 + 5 + 4, x0=0.9, x1=0.95)        # far away on screen: the track id alone is enough
    db.set_reid(b, vec(1))
    (event_dir(b)).mkdir(parents=True, exist_ok=True); (event_dir(b) / "clip.mp4").write_bytes(b"x")
    db.execute("INSERT INTO locks (camera_id, start_ts, end_ts, event_id, note, created_at) VALUES (?,?,?,?,?,?)", ["cam1", T0, T0 + 1, b, "", time.time()])
    found = merge.candidate(db.event(b))
    assert found and found[0]["id"] == a and found[1].startswith("same track 77")
    merged = merge.apply(found[0], db.event(b), found[1])
    assert merged["id"] == a and merged["status"] == "pending" and merged["end_ts"] == T0 + 14 and len(merged["path"]) == 6
    assert merged["cells"] and merged["synopsis"] is None
    assert db.event(b) is None and db.get_reid(b) is None and not (settings.data_dir / "events" / str(b)).exists()
    assert db.one("SELECT event_id FROM locks")["event_id"] == a


def test_new_track_needs_continuity_and_reid():
    base = vec(10)
    a = make(track="1", start=T0 + 100, x1=0.5); db.set_reid(a, base)
    # continuous position, same person
    b = make(track="2", start=T0 + 100 + 5 + 3, x0=0.52); db.set_reid(b, vec(11, base))
    assert np.dot(db.get_reid(a), db.get_reid(b)) > settings.merge_reid_min
    found = merge.candidate(db.event(b))
    assert found and found[0]["id"] == a and "re-ID" in found[1]
    # continuous position, different person
    c = make(track="3", start=T0 + 100 + 5 + 3, x0=0.52); db.set_reid(c, vec(99))
    assert merge.candidate(db.event(c)) is None
    # same person, but appears on the far side of the frame
    d = make(track="4", start=T0 + 100 + 5 + 3, x0=0.95, x1=0.97); db.set_reid(d, vec(12, base))
    assert merge.candidate(db.event(d)) is None


def test_refusals():
    a = make(track="1", start=T0 + 1000, x1=0.5)
    assert merge.candidate(db.event(make(track="1", start=T0 + 1000 + 5 + 25))) is None          # gap too long
    assert merge.candidate(db.event(make(track="1", cls="vehicle", start=T0 + 1000 + 5 + 2))) is None  # class differs
    assert merge.candidate(db.event(make(track="1", start=T0 + 1000 + 5 + 2, ptz_preset="away"))) is None  # different view
    db.update_event(a, feedback={"verdict": "false_alarm"})
    assert merge.candidate(db.event(make(track="1", start=T0 + 1000 + 5 + 2))) is None            # operator touched A
    long = make(track="9", start=T0 + 5000, dur=299)
    assert merge.candidate(db.event(make(track="9", start=T0 + 5000 + 299 + 2))) is None          # A already at the cap
    assert db.event(long)["status"] == "verified"
    # an unverified fragment never merges
    assert merge.candidate(db.event(make(track="5", start=T0 + 7000 + 7, status="pending"))) is None


def test_vehicles_need_no_reid():
    a = make(track="v1", cls="vehicle", start=T0 + 9000, x1=0.6)
    b = make(track="v2", cls="vehicle", start=T0 + 9000 + 5 + 1, x0=0.62)
    found = merge.candidate(db.event(b))
    assert found and found[0]["id"] == a and found[1].startswith("continuous")


def test_hold_loop_queues_once():
    from nvr import mediamtx
    from nvr.pipeline import Pipeline
    mediamtx.recording_spans = lambda *a, **k: []
    p = Pipeline()
    e = make(track="h1", start=T0 + 20000)
    p.synopsis_hold[e] = time.time() - 1       # due already
    p.synopsis_hold[999999] = time.time() - 1  # vanished (merged) event: dropped silently

    async def tick():
        task = asyncio.create_task(p.hold_loop())
        await asyncio.sleep(1.5)
        task.cancel()
    asyncio.run(tick())
    assert p.synopsis_hold == {} and e in p.synopsis_pending and p.synopsis_q.qsize() == 1
    assert p.synopsis_q.get_nowait()[2] == e


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
