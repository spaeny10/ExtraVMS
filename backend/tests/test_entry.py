"""Where a track came from: pre-roll box chaining, place/edge facts for Qwen, and the 'entry' site rule.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_entry.py   (from backend/)
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-entry-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from nvr import identities, policy, zones  # noqa: E402
from nvr.db import db  # noqa: E402
from nvr.synopsis import _track_fact  # noqa: E402
from nvr.verifier import chain_boxes  # noqa: E402

DOOR = {"name": "South Exterior Door", "type": "area", "points": [[0.46, 0.07], [0.46, 0.32], [0.6, 0.5], [0.65, 0.12]]}
CAM = {"id": "cam3", "name": "Kitchen", "host": "127.0.0.1", "onvif_port": 80, "rtsp_port": 554, "username": "u",
       "password": "", "main_path": "/m", "sub_path": "/s", "enabled": 1, "zones": [DOOR], "retention_days": None,
       "scene_notes": "", "retention_policy": None,
       "policies": [{"kind": "entry", "area": "South Exterior Door", "allowed": ["Shawn"], "priority": "high"}]}
NOW = time.time()
rng = np.random.default_rng(5)


def box(cx, bottom, w=0.1, h=0.3):
    return [cx - w / 2, bottom - h, cx + w / 2, bottom]


def yolo(b, conf=0.9, cls=0):
    return {"cls_id": cls, "conf": conf, "box": b}


def test_chain_follows_the_person_backwards_and_stops_at_a_gap():
    start = box(0.55, 0.47)
    frames = [(10.0, [yolo(box(0.56, 0.42))]), (9.5, [yolo(box(0.57, 0.38)), yolo(box(0.2, 0.9))]),
              (9.0, [yolo(box(0.2, 0.9))]), (8.5, [yolo(box(0.58, 0.35))])]
    got = chain_boxes(start, frames, {0})
    assert [g[0] for g in got] == [10.0, 9.5]          # stops at 9.0 where only the far-away box exists
    assert abs(got[1][4] - 0.38) < 1e-6
    assert chain_boxes(start, [(10.0, [yolo(box(0.56, 0.42), cls=2)])], {0}) == []  # wrong class


def test_place_and_edge_facts():
    assert zones.place_of(0.55, 0.42, [DOOR]) == "South Exterior Door"          # inside
    assert zones.place_of(0.55, 0.47, [DOOR]) == "South Exterior Door"          # just below the polygon (feet on the mat)
    assert zones.place_of(0.2, 0.9, [DOOR]) is None
    assert zones.edge_of(0.3, 0.97).startswith("bottom") and zones.edge_of(0.5, 0.5) is None
    e = {"path": [[0.0, *box(0.55, 0.42)], [3.0, *box(0.3, 0.99)]], "camera_class": "person"}
    fact = _track_fact(e, {"zones": [DOOR]})
    assert "first seen in 'South Exterior Door'" in fact and "bottom edge" in fact and "entered through" in fact
    assert "left through South" not in fact and "came back" not in fact


def add(ts, path, areas, vec):
    f = {"camera_id": "cam3", "track_id": "t", "camera_class": "person", "start_ts": ts, "end_ts": ts + 3,
         "status": "verified", "created_at": ts, "yolo_conf": 0.9, "synopsis": "a person"}
    eid = db.execute_insert(f"INSERT INTO events ({','.join(f)}) VALUES ({','.join('?' * len(f))})", list(f.values()))
    db.update_event(eid, path=path, areas=areas)
    db.set_vec("reid_vec", eid, vec)
    return eid


def test_entry_rule():
    db.upsert_camera(CAM)
    shawn = rng.normal(size=512); shawn /= np.linalg.norm(shawn)
    p_in = [[NOW, *box(0.55, 0.42)], [NOW + 1, *box(0.5, 0.6)], [NOW + 3, *box(0.3, 0.99)]]
    at_door = [{"name": "South Exterior Door", "from": NOW, "to": NOW + 0.6}]
    stranger = add(NOW - 100, p_in, at_door, rng.normal(size=512))
    b = policy.check(stranger)
    assert b and b["kind"] == "entry" and "not a recognized person" in b["text"]
    assert db.event(stranger)["priority"] == "high"
    known = add(NOW - 90, p_in, at_door, shawn)
    identities.name_cluster("person", "Shawn", [known])
    assert policy.check(known) is None
    # walked to the door later (leaving), not entering through it
    leaving = add(NOW - 80, [[NOW, *box(0.3, 0.99)], [NOW + 4, *box(0.55, 0.42)]],
                  [{"name": "South Exterior Door", "from": NOW + 4, "to": NOW + 4.5}], rng.normal(size=512))
    assert policy.check(leaving) is None
    assert policy.recheck("cam3") == 1


def test_door_facts_fix_the_summary():
    NOWT = 1000.0
    e = {"path": [[NOWT, *box(0.55, 0.42)], [NOWT + 3, *box(0.3, 0.99)]],
         "areas": [{"name": "South Exterior Door", "from": NOWT, "to": NOWT + 0.6}]}
    assert zones.door_facts(e) == ("South Exterior Door", None)
    fixed = zones.apply_door_facts("A person walks toward the camera. They then walk out of the building through the South Exterior Door.", e)
    assert fixed.startswith("Came in through the South Exterior Door.") and "out of the building" not in fixed
    ok = "A bearded man came in through the South Exterior Door carrying a bag."
    assert zones.apply_door_facts(ok, e) == ok
    leaving = {"path": [[NOWT, *box(0.3, 0.99)], [NOWT + 4, *box(0.55, 0.42)]],
               "areas": [{"name": "South Exterior Door", "from": NOWT + 3.6, "to": NOWT + 4}]}
    assert zones.door_facts(leaving) == (None, "South Exterior Door")
    assert zones.apply_door_facts("A person walks to the door.", leaving).endswith("Left through the South Exterior Door.")
    assert zones.apply_door_facts("Someone at the sink.", {"path": e["path"], "areas": [{"name": "Espresso Machine", "from": NOWT, "to": NOWT + 1}]}) == "Someone at the sink."


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
