"""`python -m nvr.reverify`: events that failed verification during a storage outage go back to the running NVR, which
verifies them one at a time in the background (Pipeline.background_reverify_tick) without holding up live events.
Throwaway data / recordings / runtime dirs.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_reverify.py   (from backend/), or under pytest.
"""
import contextlib
import io
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="nvr-reverify-test-")   # never the real DB, recordings or runtime config
os.environ["NVR_DATA_DIR"] = str(Path(_TMP, "data"))
os.environ["NVR_RECORDINGS_DIR"] = str(Path(_TMP, "recordings"))
os.environ["NVR_RUNTIME_DIR"] = str(Path(_TMP, "runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import mediamtx, reverify  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402
from nvr.fmp4 import segment_name  # noqa: E402
from nvr.pipeline import Pipeline  # noqa: E402

mediamtx.recording_spans = lambda *a, **k: []
REC = settings.recordings_dir
NOW = time.time()
BASE = NOW - 2 * 86400
FULL = "verify: [Errno 28] No space left on device"


def camera(cid):
    db.upsert_camera({"id": cid, "name": cid, "host": "10.0.0.5", "onvif_port": 80, "rtsp_port": 554, "username": "u",
                      "password": "p", "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": [],
                      "retention_days": None, "scene_notes": "", "retention_policy": None, "policies": []})


for _c in ("cam1", "cam2", "cam3"):
    camera(_c)


def segment(cam: str, start: float, seconds: float) -> None:
    d = REC / cam
    d.mkdir(parents=True, exist_ok=True)
    f = d / segment_name(start)
    f.write_bytes(b"")
    os.utime(f, (start + seconds, start + seconds))


def event(cam, start, status="error", error=FULL, end=5.0) -> int:
    return db.create_event(camera_id=cam, track_id=f"t{time.time()}{start}", camera_class="person", start_ts=start,
                           end_ts=None if end is None else start + end, status=status, error=error)


segment("cam1", BASE, 3600)
segment("cam2", BASE, 3600)
E = {
    "full": event("cam1", BASE + 100),
    "io": event("cam1", BASE + 200, error="verify: disk I/O error"),
    "db_full": event("cam2", BASE + 300, error="verify: database or disk is full"),
    "oom": event("cam1", BASE + 400, error="verify: [Errno 12] Cannot allocate memory"),
    "gone": event("cam1", BASE + 7200),                 # past the recording's end
    "cam3": event("cam3", BASE + 100),                  # nothing recorded for cam3
    "open": event("cam1", BASE + 500, end=None),
    "fine": event("cam1", BASE + 600, status="verified", error=None),
    "rejected": event("cam1", BASE + 700, status="rejected", error=None),
    "old": event("cam1", BASE - 86400),                 # before --since below, and not recorded
}


def test_storage_errors_are_recognised():
    for text in (FULL, "verify: disk I/O error", "verify: database or disk is full", "OSError: [Errno 5] Input/output error",
                 "attempt to write a readonly database", "database is locked"):
        assert reverify.is_storage_error(text), text
    for text in ("verify: [Errno 12] Cannot allocate memory", "verify: Client error '404 Not Found'", None, ""):
        assert not reverify.is_storage_error(text), text


def test_dry_run_counts_and_changes_nothing():
    rep = reverify.scan(db)
    assert rep.matched == 8 and rep.eligible == 3 and rep.recording_missing == 3 and rep.other_error == 1, rep
    assert rep.unfinished == 1 and rep.already_fine == 2 and rep.already_queued == 0
    assert rep.ids == [E["full"], E["io"], E["db_full"]]
    assert reverify.queued(db) == [] and db.event(E["full"])["status"] == "error"
    text = reverify.render(rep, False, False)
    for line in ("eligible (recording covers the event):", "recording missing", "already fine", "other errors", "dry run"):
        assert line in text, text
    anyerr = reverify.scan(db, any_error=True)
    assert anyerr.eligible == 4 and E["oom"] in anyerr.ids and anyerr.other_error == 0
    cam = reverify.scan(db, camera="cam2")
    assert cam.ids == [E["db_full"]] and cam.already_fine == 0
    since = reverify.scan(db, since=BASE - 10)
    assert since.recording_missing == 2 and since.matched == 7
    limited = reverify.scan(db, limit=2)
    assert limited.ids == [E["full"], E["io"]] and limited.over_limit == 1


def test_cli_dry_run_reads_only():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = reverify.main(["--status", "error", "--since", "2020-01-01", "--camera", "cam1", "--limit", "5"])
    assert code == 0 and "dry run" in buf.getvalue() and "eligible" in buf.getvalue()
    assert reverify.queued(db) == []


def test_queue_requests_merge_and_takes_never_lose_ids():
    reverify.request(db, [5, 3])
    reverify.request(db, [3, 9, 1])
    assert reverify.queued(db) == [1, 3, 5, 9]
    assert reverify.take_next(db) == 1
    # a request between the read and the swap of a take: the take gives up this time, nothing is lost
    row = db.one("SELECT value FROM settings WHERE key=?", [reverify.QUEUE_KEY])
    real_one = db.one

    def one(sql, params=()):
        out = real_one(sql, params)
        if "FROM settings WHERE key=?" in sql:
            reverify.request(db, [2])
        return out
    db.one = one
    try:
        assert reverify.take_next(db) is None
    finally:
        db.one = real_one
    assert row and reverify.queued(db) == [2, 3, 5, 9]
    while reverify.take_next(db) is not None:
        pass
    assert reverify.queued(db) == [] and db.one("SELECT 1 FROM settings WHERE key=?", [reverify.QUEUE_KEY]) is None


def test_apply_hands_them_to_the_pipeline_one_at_a_time_in_the_background():
    rep = reverify.scan(db, apply=True)
    assert rep.eligible == 3 and reverify.queued(db) == [E["full"], E["io"], E["db_full"]]
    assert db.event(E["full"])["status"] == "error", "they stay in error until the NVR takes them (no restart flood)"
    again = reverify.scan(db, apply=True)
    assert again.eligible == 0 and again.already_queued == 3

    p = Pipeline()
    p.verifier = object()   # loaded (the tick waits for the verifier otherwise)
    t = 1000.0
    p.verify_q.put_nowait(424242)   # a live event waiting: the background one waits too
    assert p.background_reverify_tick(t) is None and reverify.queued(db)[0] == E["full"]
    p.verify_q.get_nowait()
    t += p.BG_REVERIFY_S
    for i in range(p.BG_REVERIFY_MAX_SYNOPSIS):   # Qwen busy: wait as well
        p.synopsis_q.put_nowait((1, i, 900000 + i))
    assert p.background_reverify_tick(t) is None
    while not p.synopsis_q.empty():
        p.synopsis_q.get_nowait()
    assert p.background_reverify_tick(t + 0.5) is None, "polled at most every BG_REVERIFY_S"
    t += p.BG_REVERIFY_S
    assert p.background_reverify_tick(t) == E["full"]
    e = db.event(E["full"])
    assert e["status"] == "pending" and e["error"] is None and p.verify_q.get_nowait() == E["full"]
    t += p.BG_REVERIFY_S
    assert p.background_reverify_tick(t) is None, "one at a time: the previous one is still being verified"
    db.update_event(E["full"], status="verified")
    t += p.BG_REVERIFY_S
    # an operator reprocessed this one meanwhile: it is not touched, the next tick takes the next
    db.update_event(E["io"], status="verified", error=None)
    assert p.background_reverify_tick(t) is None and db.event(E["io"])["status"] == "verified"
    t += p.BG_REVERIFY_S
    assert p.background_reverify_tick(t) == E["db_full"] and p.verify_q.get_nowait() == E["db_full"]
    # a verification that never finishes doesn't block the queue for ever
    reverify.request(db, [E["gone"]])
    t += p.BG_REVERIFY_S
    assert p.background_reverify_tick(t) is None
    t += p.BG_REVERIFY_STUCK_S
    assert p.background_reverify_tick(t) == E["gone"]
    assert reverify.queued(db) == []


def test_hold_tick_runs_the_background_step():
    p = Pipeline()
    p.verifier = object()
    eid = event("cam1", BASE + 900)
    reverify.request(db, [eid])
    p.hold_tick(time.time())
    assert db.event(eid)["status"] == "pending" and p.verify_q.get_nowait() == eid


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
