"""Events whose synopsis failed while Qwen / the remote AI was down are retried when it is back (pipeline.retry_*),
on a throwaway DB. Run: ..\\.venv\\Scripts\\python.exe -m pytest tests\\test_synopsis_retry.py  (from backend/)"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-retry-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import mediamtx, vlmroute  # noqa: E402
from nvr.db import db  # noqa: E402
from nvr.pipeline import Pipeline, synopsis_attempts, synopsis_error  # noqa: E402

mediamtx.recording_spans = lambda *a, **k: []
db.upsert_camera({"id": "cam1", "name": "Yard", "host": "10.0.0.5", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": "p",
                  "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": [], "retention_days": None, "scene_notes": "", "retention_policy": None, "policies": []})
NOW = time.time()


def make(start, error=None, cls="person", status="verified", synopsis=None):
    path = [[start, 0.3, 0.35, 0.4, 0.65, 0.9], [start + 5, 0.5, 0.35, 0.6, 0.65, 0.9]]
    eid = db.create_event(camera_id="cam1", track_id=str(start), camera_class=cls, camera_conf=0.9, start_ts=start,
                          end_ts=start + 5, path=path, status=status)
    db.update_event(eid, error=error, synopsis=synopsis)
    return eid


def test_error_text_counts_attempts():
    e = {"error": None}
    e1 = synopsis_error(e, RuntimeError("remote VLM 500"))
    assert e1.startswith("synopsis: ") and synopsis_attempts({"error": e1}) == 1
    e2 = synopsis_error({"error": e1}, RuntimeError("still down"))
    assert synopsis_attempts({"error": e2}) == 2 and "still down" in e2
    assert synopsis_attempts({"error": "verify: no frames"}) == 0 and synopsis_attempts({"error": None}) == 0


def test_retry_requeues_only_failed_recent_wanted_events():
    p = Pipeline()
    failed = make(NOW - 600, error="synopsis: remote VLM 500: Internal Server Error")
    failed_twice = make(NOW - 500, error=synopsis_error({"error": "synopsis: x"}, RuntimeError("y")))
    exhausted = make(NOW - 400, error="synopsis (attempt 3): no model")
    old = make(NOW - 8 * 86400, error="synopsis: remote VLM 500")
    verify_err = make(NOW - 300, error="verify: no frames decoded", status="error")
    done = make(NOW - 200, synopsis="A person walked by.")
    vehicle = make(NOW - 100, error="synopsis: remote VLM 500", cls="vehicle")   # not a synopsis label by default here?
    n = p.retry_failed_synopses()
    queued = {p.synopsis_q.get_nowait()[2] for _ in range(p.synopsis_q.qsize())}
    assert failed in queued and failed_twice in queued
    assert exhausted not in queued and old not in queued and verify_err not in queued and done not in queued
    wants_vehicle = p.wants_synopsis(db.event(vehicle))
    assert (vehicle in queued) == wants_vehicle
    assert n == len(queued)
    # a second pass does nothing while those are still pending
    assert p.retry_failed_synopses() == 0


def test_retry_due_gates_on_ready_idle_backoff_and_breaker():
    p = Pipeline()
    p.vlm_ready = True
    assert p.retry_due()
    p.synopsis_failed_at = time.time()
    assert not p.retry_due()                          # just failed: back off
    assert p.retry_due(now=time.time() + p.RETRY_BACKOFF_S + 1)
    p.synopsis_failed_at = 0
    p.synopsis_pending.add(1)
    assert not p.retry_due()                          # live work first
    p.synopsis_pending.clear()
    p.vlm_ready = False
    assert not p.retry_due()
    p.vlm_ready = True
    old = vlmroute.router.down_until
    vlmroute.router.down_until = time.time() + 100    # remote AI circuit breaker open
    try:
        assert not p.retry_due()
    finally:
        vlmroute.router.down_until = old
    assert p.retry_due()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
