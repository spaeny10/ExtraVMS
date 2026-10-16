"""The Qwen confidence gate (pipeline.synopsis_gate): no synopsis for events YOLO is less sure of than synopsis_min_conf,
a note on the card instead; site rules, PPE checks and unusual events are still described. Also the synopsis prompt's
motion-evidence instruction (event 15205: parked pickups described as driving). Throwaway DB, no Qwen.
Run: ..\\.venv\\Scripts\\python.exe tests\\test_synopsis_gate.py   (from backend/)
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-gate-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import mediamtx, synopsis  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402
from nvr.pipeline import Pipeline, synopsis_min_conf  # noqa: E402

mediamtx.recording_spans = lambda *a, **k: []
assert "nvr-gate-test-" in str(settings.data_dir)
for cid in ("cam1", "cam6"):
    db.upsert_camera({"id": cid, "name": cid, "host": "10.0.0.5", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": "p",
                      "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": [], "retention_days": None, "scene_notes": "",
                      "retention_policy": None, "policies": []})
TOWING = [{"kind": "towing", "asset": "solar light tower", "allowed": ["BIGView truck"], "priority": "high"}]
NOW = time.time()


def pipeline() -> Pipeline:
    p = Pipeline()
    p.cameras = {"cam1": {"id": "cam1", "synopsis_labels": ["person", "vehicle"], "policies": TOWING},
                 "cam6": {"id": "cam6", "synopsis_labels": ["person", "vehicle"], "policies": []}}
    return p


def make(conf, cls="vehicle", cam="cam6", **cols):
    path = [[NOW - 60, 0.3, 0.35, 0.4, 0.65, conf], [NOW - 55, 0.5, 0.35, 0.6, 0.65, conf]]
    eid = db.create_event(camera_id=cam, track_id=f"t{time.monotonic_ns()}", camera_class=cls, camera_conf=0.0,
                          start_ts=NOW - 60, end_ts=NOW - 55, path=path, status="verified")
    db.update_event(eid, yolo_class="car" if cls == "vehicle" else "person", yolo_conf=conf, yolo_hits=3,
                    detections={"samples": [], "keyframes": [], "needed": 2, **cols.pop("detections", {})}, **cols)
    return eid


def test_below_the_gate_gets_a_note_and_no_qwen():
    db.set_setting("synopsis_min_conf", None)
    assert synopsis_min_conf() == settings.synopsis_min_conf == 0.60
    p = pipeline()
    low = make(0.45)
    assert p.synopsis_gate(db.event(low)) == "No synopsis: YOLO 45% < 60%"
    assert not p.wants_synopsis(db.event(low)) and p.wants_synopsis(db.event(low), gate=False)
    assert p.plan_synopsis(low) is False and low not in p.synopsis_hold
    e = db.event(low)
    assert e["status"] == "verified" and e["detections"]["synopsis_skipped"] == "No synopsis: YOLO 45% < 60%"
    # at / above the gate: held for Qwen as before, no note
    ok = make(0.60, cls="person")
    assert p.synopsis_gate(db.event(ok)) is None and p.plan_synopsis(ok) is True and ok in p.synopsis_hold
    assert "synopsis_skipped" not in db.event(ok)["detections"]
    # a label the camera doesn't describe gets no note either (the card keeps "car confirmed by YOLO")
    p.cameras["cam6"]["synopsis_labels"] = ["person"]
    plain = make(0.45)
    assert p.plan_synopsis(plain) is False and "synopsis_skipped" not in db.event(plain)["detections"]
    p.cameras["cam6"]["synopsis_labels"] = ["person", "vehicle"]


def test_restart_retry_and_relabel_do_not_queue_gated_events():
    p = pipeline()
    low = make(0.40, cls="person")
    failed = make(0.40, cls="person", error="synopsis: remote VLM 500")
    p.recover()
    p.retry_failed_synopses()
    p.queue_missing_synopses("cam6")
    queued = {p.synopsis_q.get_nowait()[2] for _ in range(p.synopsis_q.qsize())}
    assert low not in queued and failed not in queued, queued
    # "Generate with Qwen" (the API queues directly) still describes it
    assert p.queue_synopsis(low)


def test_exceptions_still_get_qwen():
    p = pipeline()
    rule = make(0.40, cls="person", policy={"kind": "entry", "priority": "high", "text": "Entered through South Door"})
    ppe = make(0.40, cls="person", detections={"ppe": {"verdict": "compliant", "zone": "Yard"}})
    unusual = make(0.40, anomaly=0.9)
    towing = make(0.40, cam="cam1")   # a towing rule can only break once Qwen has looked at the vehicle
    for eid in (rule, ppe, unusual, towing):
        e = db.event(eid)
        assert p.synopsis_gate(e) is None and p.wants_synopsis(e), eid
        assert p.plan_synopsis(eid) is True
    # an unusual event of a label the camera doesn't describe is still held (the anomaly path, unchanged)
    p.cameras["cam6"]["synopsis_labels"] = ["person"]
    odd = make(0.40)
    assert p.plan_synopsis(odd, anomaly=0.9) is True
    p.cameras["cam6"]["synopsis_labels"] = ["person", "vehicle"]


def test_site_setting_wins_and_zero_turns_it_off():
    p = pipeline()
    e = db.event(make(0.55))
    db.set_setting("synopsis_min_conf", 0.5)
    assert synopsis_min_conf() == 0.5 and p.synopsis_gate(e) is None
    db.set_setting("synopsis_min_conf", 0.7)
    assert p.synopsis_gate(e) == "No synopsis: YOLO 55% < 70%"
    db.set_setting("synopsis_min_conf", 0)
    assert p.synopsis_gate(db.event(make(0.26))) is None
    db.set_setting("synopsis_min_conf", None)


def test_prompt_requires_evidence_for_motion():
    s = synopsis.SYSTEM
    assert "Motion needs evidence" in s and "position clearly changes across the frames" in s
    assert "parked or stationary" in s
    # the JSON schema is unchanged
    assert synopsis.SCHEMA["required"] == ["summary", "objects", "activity", "threat_level", "tags", "towing"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
