"""Fleet actions, site side: the camera handoff (passwords only down the hub tunnel) and the merge import that adds
moved cameras without touching the destination's own settings, layouts, dashboards or cameras.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_camera_handoff.py   (from backend/)
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-handoff-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import numpy as np  # noqa: E402

from nvr import hub_agent, identities, journeys, siteconfig  # noqa: E402
from nvr.api import app  # noqa: E402
from nvr.db import db  # noqa: E402

PW = "pw-" + "x" * 8   # a stand-in, never a real credential
CAM = {"id": "cam1", "name": "Front Door", "host": "10.0.0.5", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": PW,
       "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": [{"name": "Door", "type": "area", "points": [[0, 0], [0.2, 0], [0.2, 0.2]]}],
       "retention_days": None, "scene_notes": "the door", "retention_policy": None, "synopsis_labels": ["person"],
       "policies": [{"kind": "entry", "area": "Door", "allowed": ["Sam"], "priority": "high"}]}


def get(path, client_marker, headers=None):
    async def go():
        transport = httpx.ASGITransport(app=app, client=client_marker)
        async with httpx.AsyncClient(transport=transport, base_url="http://site") as c:
            return await c.get(path, headers=headers or {})
    return asyncio.run(go())


def _seed_source():
    db.upsert_camera(CAM)
    db.upsert_camera({**CAM, "id": "cam2", "name": "Lot", "host": "10.0.0.6", "policies": [], "password": "other-pw"})
    db.upsert_camera({**CAM, "id": "cam3", "name": "Shed", "host": "10.0.0.7", "policies": [], "password": "third-pw"})
    journeys.set_topology([{"cam_a": "cam1", "cam_b": "cam2", "min_s": 0, "max_s": 40, "one_way": False},
                           {"cam_a": "cam1", "cam_b": "cam3", "min_s": 0, "max_s": 20, "one_way": True}])
    v = np.random.default_rng(1).normal(size=512); v /= np.linalg.norm(v)
    eid = db.create_event(camera_id="cam1", track_id="t", camera_class="person", camera_conf=0.9, start_ts=time.time(), end_ts=time.time() + 3, path=[], status="verified")
    db.set_vec("reid_vec", eid, v)
    identities.name_cluster("person", "Sam", [eid], notes="owner")


def test_handoff_is_tunnel_only():
    _seed_source()
    lan = ("192.168.1.9", 5000)
    assert get("/api/config/handoff?cameras=cam1", lan, {"x-hub-internal": "handoff"}).status_code == 403   # LAN, even with the header
    assert get("/api/config/handoff?cameras=cam1", hub_agent.IN_PROCESS_CLIENT).status_code == 403          # tunnel, but not a handoff
    r = get("/api/config/handoff?cameras=cam1,cam2", hub_agent.IN_PROCESS_CLIENT, {"x-hub-internal": "handoff"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["partial"] is True and [c["id"] for c in d["cameras"]] == ["cam1", "cam2"] and d["cameras"][0]["password"] == PW
    assert d["camera_links"] == [{"cam_a": "cam1", "cam_b": "cam2", "min_s": 0, "max_s": 40, "one_way": 0}]   # only links among the moved
    assert d["identities"][0]["name"] == "Sam" and "layouts" not in d and "settings" not in d
    # the ordinary export (nightly backups, LAN) still never carries passwords
    r = get("/api/config/export", lan)
    assert r.status_code == 200 and PW not in r.text and "password" not in r.json()["cameras"][0]


def test_merge_keeps_destination_settings():
    data = json.loads(json.dumps(siteconfig.handoff(["cam1", "cam2"])))
    # become the destination: its own cameras (one id clashes, one is the same physical camera), settings, layouts
    for t in ("camera_links", "identity_looks", "identities", "layouts", "dashboards", "chat_messages", "rule_events", "event_links"):
        db.execute(f"DELETE FROM {t}")
    db.execute("DELETE FROM events")
    db.execute("DELETE FROM cameras")
    db.upsert_camera({**CAM, "id": "cam1", "name": "Dest Gate", "host": "10.9.9.9", "password": "dest-pw", "zones": [], "policies": []})
    db.upsert_camera({**CAM, "id": "yard", "name": "Old Lot entry", "host": "10.0.0.6", "password": "", "zones": [], "policies": []})
    journeys.set_topology([{"cam_a": "cam1", "cam_b": "yard", "min_s": 1, "max_s": 9, "one_way": False}])
    db.execute("INSERT INTO layouts (name, config, created_at, updated_at) VALUES (?,?,?,?)", ["Dest", json.dumps({"visible": ["cam1"]}), time.time(), time.time()])
    db.execute("INSERT INTO dashboards (name, config, created_at, updated_at) VALUES (?,?,?,?)", ["Dest ops", json.dumps({"widgets": []}), time.time(), time.time()])
    db.set_setting("retention_policy", {"continuous_days": 21})
    db.set_setting("briefing", {"hour": 7})

    counts = siteconfig.merge_cameras(data)
    assert counts["ids"] == {"cam1": "cam1_2", "cam2": "yard"} and counts["updated"] == ["yard"]
    assert counts["camera_links"] == 1 and counts["identities"] == 1
    cams = {c["id"]: c for c in db.cameras()}
    assert cams["cam1"]["name"] == "Dest Gate" and cams["cam1"]["password"] == "dest-pw"           # destination's camera untouched
    moved = cams["cam1_2"]
    assert moved["name"] == "Front Door" and moved["password"] == PW and moved["zones"][0]["name"] == "Door"
    assert moved["policies"][0]["allowed"] == ["Sam"] and moved["synopsis_labels"] == ["person"]
    assert cams["yard"]["name"] == "Lot" and cams["yard"]["password"] == "other-pw"                # same address: updated, not duplicated
    assert db.get_setting("retention_policy") == {"continuous_days": 21} and db.get_setting("briefing") == {"hour": 7}
    assert [r["name"] for r in db.all("SELECT name FROM layouts")] == ["Dest"]
    assert [r["name"] for r in db.all("SELECT name FROM dashboards")] == ["Dest ops"]
    links = {(l["cam_a"], l["cam_b"]) for l in journeys.topology()}
    assert links == {("cam1", "yard"), ("cam1_2", "yard")}
    # retrying the same move changes nothing new
    again = siteconfig.merge_cameras(data)
    assert again["ids"] == {"cam1": "cam1_2", "cam2": "yard"} and again["identities"] == 0 and len(db.cameras()) == 3
    # a partial export is refused by the full restore
    try:
        siteconfig.import_config(data)
        raise AssertionError("partial export restored")
    except ValueError:
        pass


def test_mark_moved():
    siteconfig.mark_moved("cam1", "Hailo T1")
    assert db.get_setting("moved_cameras")["cam1"]["to"] == "Hailo T1"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
