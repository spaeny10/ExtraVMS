"""Site configuration export / import round trip on a throwaway database (no passwords in the export).

Run: ..\\.venv\\Scripts\\python.exe tests\\test_siteconfig.py   (from backend/)
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-cfg-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from nvr import identities, journeys, siteconfig  # noqa: E402
from nvr.db import db  # noqa: E402

CAM = {"id": "cam1", "name": "Yard", "host": "10.0.0.5", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": "secret-pw",
       "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": [{"name": "Door", "type": "area", "points": [[0, 0], [0.2, 0], [0.2, 0.2]]}],
       "retention_days": None, "scene_notes": "the yard", "retention_policy": None,
       "policies": [{"kind": "entry", "area": "Door", "allowed": ["Sam"], "priority": "high"}]}


def test_round_trip():
    db.upsert_camera(CAM)
    db.upsert_camera({**CAM, "id": "cam2", "name": "Lot", "policies": []})
    db.set_ptz_config("cam1", {"home_token": "5", "home_name": "parking", "return_home_min": 3})
    journeys.set_topology([{"cam_a": "cam1", "cam_b": "cam2", "min_s": 0, "max_s": 40, "one_way": False}])
    v = np.random.default_rng(1).normal(size=512); v /= np.linalg.norm(v)
    eid = db.create_event(camera_id="cam1", track_id="t", camera_class="person", camera_conf=0.9, start_ts=time.time(), end_ts=time.time() + 3, path=[], status="verified")
    db.set_vec("reid_vec", eid, v)
    identities.name_cluster("person", "Sam", [eid], notes="owner")
    db.execute("INSERT INTO layouts (name, config, created_at, updated_at) VALUES (?,?,?,?)", ["Front", json.dumps({"visible": ["cam1"], "solo": None}), time.time(), time.time()])
    db.set_setting("retention_policy", {"continuous_days": 9})
    db.execute("INSERT INTO dashboards (name, config, created_at, updated_at) VALUES (?,?,?,?)", ["Ops", json.dumps({"version": 1, "cols": 12, "rowH": 60, "widgets": []}), time.time(), time.time()])

    data = siteconfig.export_config()
    assert data["format"] == 1 and len(data["cameras"]) == 2 and "password" not in data["cameras"][0]
    assert data["cameras"][0]["ptz_config"]["home_name"] == "parking" and data["cameras"][0]["policies"][0]["area"] == "Door"
    assert data["camera_links"][0]["cam_b"] == "cam2" and data["identities"][0]["name"] == "Sam" and data["identities"][0]["looks"]
    assert data["layouts"][0]["name"] == "Front" and data["settings"]["retention_policy"] == {"continuous_days": 9}
    assert data["dashboards"][0]["name"] == "Ops"
    text = json.dumps(data)   # JSON-clean (bytes are base64)

    # wipe and restore
    db.execute("DELETE FROM camera_links"); db.execute("DELETE FROM layouts"); db.execute("DELETE FROM dashboards"); db.execute("DELETE FROM identity_looks"); db.execute("DELETE FROM identities")
    db.upsert_camera({**CAM, "name": "renamed", "zones": [], "policies": []})
    db.set_setting("retention_policy", None)
    counts = siteconfig.import_config(json.loads(text))
    assert counts == {"cameras": 2, "camera_links": 1, "identities": 1, "layouts": 1, "dashboards": 1}
    cams = {c["id"]: c for c in db.cameras()}
    assert cams["cam1"]["name"] == "Yard" and cams["cam1"]["password"] == "secret-pw" and cams["cam1"]["zones"][0]["name"] == "Door"
    assert cams["cam1"]["ptz_config"]["home_name"] == "parking"
    assert journeys.topology()[0]["max_s"] == 40
    assert [n["name"] for n in identities.named("person")] == ["Sam"] and identities.named("person")[0]["looks"] == 1
    assert db.one("SELECT name FROM layouts")["name"] == "Front" and db.get_setting("retention_policy") == {"continuous_days": 9}
    # importing again merges identities by name (no duplicates)
    assert siteconfig.import_config(json.loads(text))["identities"] == 0 and len(identities.named("person")) == 1


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
