"""Site rules (policy.py) on a throwaway database: who may tow what.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_policy.py   (from backend/)
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-policy-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from nvr import baseline, identities, policy  # noqa: E402
from nvr.db import db  # noqa: E402

RULE = {"kind": "towing", "asset": "solar light tower", "allowed": ["BIGView truck"], "priority": "high"}
CAM = {"id": "cam1", "name": "Side Yard", "host": "127.0.0.1", "onvif_port": 80, "rtsp_port": 554, "username": "u",
       "password": "", "main_path": "/m", "sub_path": "/s", "enabled": 1, "zones": [], "retention_days": None,
       "scene_notes": "", "retention_policy": None, "policies": [RULE]}
NOW = time.time()
rng = np.random.default_rng(3)
D = identities.VEHICLE_DIM


def unit(v):
    return v / np.linalg.norm(v)


BIGVIEW = unit(rng.normal(size=D))


def add(ts, vec, synopsis_json, synopsis="a truck"):
    f = {"camera_id": "cam1", "track_id": "t", "camera_class": "vehicle", "start_ts": ts, "end_ts": ts + 8,
         "status": "verified", "created_at": ts, "yolo_conf": 0.9, "synopsis": synopsis}
    eid = db.execute_insert(f"INSERT INTO events ({','.join(f)}) VALUES ({','.join('?' * len(f))})", list(f.values()))
    db.update_event(eid, synopsis_json=synopsis_json)
    db.set_vec("vehicle_vec", eid, vec)
    return eid


def setup_module():
    db.upsert_camera(CAM)


def test_camera_round_trip_and_prompt():
    cam = db.cameras()[0]
    assert cam["policies"] == [RULE]
    assert "BIGView truck" in policy.prompt_lines(cam) and "solar light tower" in policy.prompt_lines(cam)
    assert policy.labels_needed(cam) == {"vehicle"}
    assert policy.labels_needed({"policies": []}) == set()


def test_unknown_vehicle_towing_breaks_the_rule():
    e = add(NOW - 100, unit(rng.normal(size=D)), {"towing": True, "towed": "solar light tower"})
    broken = policy.check(e)
    assert broken and broken["kind"] == "towing" and "not a recognised vehicle" in broken["text"]
    ev = db.event(e)
    assert ev["policy"]["priority"] == "high"
    assert ev["priority"] == "high" and baseline.priority(ev, 0.0) == "high"  # stored, not just computable


def test_known_allowed_vehicle_is_fine():
    a = add(NOW - 300, unit(BIGVIEW + rng.normal(0, 0.05 / np.sqrt(D), D)), {"towing": True, "towed": "tower"})
    identities.name_cluster("vehicle", "BIGView truck", [a])
    b = add(NOW - 90, unit(BIGVIEW + rng.normal(0, 0.05 / np.sqrt(D), D)), {"towing": True, "towed": "tower"})
    assert policy.check(b) is None
    assert db.event(b)["policy"] is None


def test_not_towing_is_ignored_and_text_fallback_works():
    quiet = add(NOW - 80, unit(rng.normal(size=D)), {"towing": False}, synopsis="A pickup drives past the parked trailers.")
    assert policy.check(quiet) is None
    old = add(NOW - 70, unit(rng.normal(size=D)), {"summary": "x"}, synopsis="A white truck is towing a trailer with a tower.")
    assert policy.check(old) is not None  # older synopses have no towing flag: the wording decides


def test_recheck_clears_when_rule_removed():
    assert policy.recheck("cam1") >= 2
    db.upsert_camera({**CAM, "policies": []})
    assert policy.recheck("cam1") == 0
    assert all(r["policy"] is None for r in (db.event(x["id"]) for x in db.all("SELECT id FROM events")))


if __name__ == "__main__":
    setup_module()
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
