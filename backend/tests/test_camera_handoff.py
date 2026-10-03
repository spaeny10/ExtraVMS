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


def _post(path, client_marker, body, headers=None):
    async def go():
        transport = httpx.ASGITransport(app=app, client=client_marker)
        async with httpx.AsyncClient(transport=transport, base_url="http://site") as c:
            return await c.post(path, json=body, headers=headers or {})
    return asyncio.run(go())


def test_learned_state_moves_with_the_camera():
    from nvr import baseline, parked
    from nvr.pipeline import Pipeline
    db.upsert_camera({**CAM, "id": "learn", "name": "Learner", "host": "10.4.0.1", "policies": []})
    entry = {"first_ts": time.time() - 20 * 86400, "days": 20.0, "daytype_days": [14, 6],
             "labels": {"person": {"events": 40, "slot_days": {"0-9": {"2026-09-01": 2}}, "grid": [[0.0] * 16] * 9, "durations": [5.0, 9.0]}}}
    base = baseline.current()
    base.setdefault("cameras", {})["learn"] = entry
    db.set_setting("baseline", base)
    baseline._cache = base
    parked.save("learn", [{"box": [0.1, 0.2, 0.3, 0.4], "cls": "vehicle", "first_seen": 1.0, "last_seen": 2.0, "count": 3}])
    eid = db.create_event(camera_id="learn", track_id="c", camera_class="vehicle", camera_conf=0.9, start_ts=time.time(), end_ts=time.time() + 2,
                          path=[], status="verified", synopsis="A tow truck", synopsis_json={"summary": "A car parked", "threat_level": "low"},
                          synopsis_original={"summary": "A truck towing equipment", "threat_level": "high"}, corrected_at=time.time())
    assert eid
    data = json.loads(json.dumps(siteconfig.handoff(["learn"])))
    got = data["learned"]["learn"]
    assert got["baseline"]["days"] == 20.0 and got["parked"][0]["count"] == 3
    assert got["corrections"] == [{"original": "A truck towing equipment [threat: high]", "corrected": "A car parked [threat: low]",
                                   "label": "vehicle", "at": got["corrections"][0]["at"]}]
    # the destination: the id is taken by another camera there, so it arrives as learn_2
    db.execute("DELETE FROM events WHERE camera_id='learn'")
    db.execute("DELETE FROM cameras WHERE id='learn'")
    db.execute("DELETE FROM settings WHERE key='parked:learn'")
    db.upsert_camera({**CAM, "id": "learn", "name": "Theirs", "host": "10.4.9.9", "policies": [], "zones": []})
    data["source"] = "Ironsight"
    counts = siteconfig.merge_cameras(data)
    new = counts["ids"]["learn"]
    assert new == "learn_2" and counts["learned"] == {"baseline": 1, "parked": 1, "corrections": 1}
    assert baseline.current()["cameras"][new]["days"] == 20.0 and baseline.current()["cameras"][new]["seeded_from"] == "Ironsight"
    assert parked.load(new)[0]["box"] == [0.1, 0.2, 0.3, 0.4]
    assert Pipeline.correction_examples(new, n=3, label="vehicle") == [{"original": "A truck towing equipment [threat: high]",
                                                                        "corrected": "A car parked [threat: low]"}]
    # a rebuild keeps the seed while this site has less history of its own; a thinner seed never replaces
    baseline.rebuild()
    assert baseline.current()["cameras"][new]["days"] == 20.0
    assert baseline.seed(new, {"days": 1, "labels": {}}) is False
    # merging again adds nothing twice
    again = siteconfig.merge_cameras(data)
    assert again["learned"] == {"baseline": 0, "parked": 0, "corrections": 0} and len(parked.load(new)) == 1


def test_history_copy_remaps_ids():
    import base64
    from nvr.config import settings
    db.upsert_camera({**CAM, "id": "hsrc", "name": "History", "host": "10.5.0.1", "policies": []})
    t0 = time.time() - 3600
    e1 = db.create_event(camera_id="hsrc", track_id="a", camera_class="person", camera_conf=0.8, start_ts=t0, end_ts=t0 + 5, path=[[t0, 0, 0, 1, 1, 0.9]],
                         status="verified", synopsis="A person at the door", synopsis_json={"summary": "A person at the door", "tags": ["door"]},
                         priority="high", areas=[{"name": "Door", "from": t0, "to": t0 + 2}])
    db.update_event(e1, snapshot=f"events/{e1}/snapshot.jpg", clip=f"events/{e1}/clip.mp4")
    e2 = db.create_event(camera_id="hsrc", track_id="b", camera_class="vehicle", camera_conf=0.7, start_ts=t0 + 60, end_ts=t0 + 70, path=[], status="rejected")
    db.create_event(camera_id="hsrc", track_id="c", camera_class="person", camera_conf=0.7, start_ts=t0 + 90, path=[], status="open")   # still running: not copied
    d = settings.data_dir / "events" / str(e1)
    d.mkdir(parents=True, exist_ok=True)
    (d / "snapshot.jpg").write_bytes(b"\xff\xd8snap")
    (d / "crop_0.jpg").write_bytes(b"\xff\xd8crop")
    (d / "clip.mp4").write_bytes(b"mp4")
    v = np.random.default_rng(3).normal(size=512).astype(np.float32)
    v /= np.linalg.norm(v)
    db.set_vec("reid_vec", e1, v)

    page = json.loads(json.dumps(siteconfig.export_history(["hsrc"])))
    assert page["total"] == 2 and page["next_after_id"] is None and [e["src_id"] for e in page["events"]] == [e1, e2]
    ev = page["events"][0]
    assert ev["files"] == ["crop_0.jpg", "snapshot.jpg"] and ev["reid"] and "clip" not in ev and "id" not in ev
    p1 = siteconfig.export_history(["hsrc"], limit=1)   # paging
    assert len(p1["events"]) == 1 and p1["next_after_id"] == e1 and siteconfig.export_history(["hsrc"], after_id=e1)["events"][0]["src_id"] == e2

    db.upsert_camera({**CAM, "id": "hdst", "name": "History here", "host": "10.5.0.2", "policies": []})
    body = {"source": {"site": "Ironsight", "site_id": "s_iron"}, "cameras": {"hsrc": "hdst"}, "events": page["events"]}
    res = siteconfig.import_history(body)
    assert res["added"] == 2 and set(res["ids"]) == {str(e1), str(e2)}
    n1 = res["ids"][str(e1)]
    assert n1 not in (e1, e2)
    copied = db.event(n1)
    assert copied["camera_id"] == "hdst" and copied["synopsis"] == "A person at the door" and copied["clip"] is None
    assert copied["migrated_from"] == {"site": "Ironsight", "site_id": "s_iron", "event_id": e1, "camera_id": "hsrc"}
    assert copied["snapshot"] == f"events/{n1}/snapshot.jpg" and copied["priority"] == "high" and copied["areas"][0]["name"] == "Door"
    assert copied["synopsis_json"]["tags"] == ["door"]
    assert np.allclose(db.get_vec("reid_vec", n1), v)
    again = siteconfig.import_history(body)   # retried: nothing copied twice
    assert again["added"] == 0 and again["ids"] == res["ids"]
    files = [{"event_id": n1, "name": "snapshot.jpg", "data": base64.b64encode(b"\xff\xd8snap").decode()},
             {"event_id": n1, "name": "clip.mp4", "data": base64.b64encode(b"mp4").decode()},
             {"event_id": e1, "name": "crop_9.jpg", "data": base64.b64encode(b"x").decode()}]
    assert siteconfig.import_history_files(files) == 1   # only jpg, only for copied events
    assert (settings.data_dir / "events" / str(n1) / "snapshot.jpg").read_bytes() == b"\xff\xd8snap"
    assert not (settings.data_dir / "events" / str(e1) / "crop_9.jpg").exists()


def test_history_endpoints_are_tunnel_only():
    lan = ("192.168.1.9", 5000)
    assert get("/api/config/history?cameras=hsrc", lan, {"x-hub-internal": "handoff"}).status_code == 403
    assert get("/api/config/history?cameras=hsrc", hub_agent.IN_PROCESS_CLIENT).status_code == 403
    r = get("/api/config/history?cameras=hsrc", hub_agent.IN_PROCESS_CLIENT, {"x-hub-internal": "handoff"})
    assert r.status_code == 200 and r.json()["total"] == 2
    assert _post("/api/config/history", lan, {"source": {}, "cameras": {}, "events": []}, {"x-hub-internal": "handoff"}).status_code == 403
    assert _post("/api/config/history/files", lan, {"files": []}).status_code == 403


def test_remove_camera_purges_only_unused():
    db.upsert_camera({**CAM, "id": "fresh", "name": "Fresh", "host": "10.6.0.1", "policies": []})
    db.set_setting("parked:fresh", [{"box": [0, 0, 1, 1]}])
    assert siteconfig.remove_camera("fresh") == "deleted"
    assert not db.one("SELECT 1 FROM cameras WHERE id='fresh'") and db.get_setting("parked:fresh") is None
    assert siteconfig.remove_camera("hdst") == "disabled"          # has (copied) events: only disabled
    assert db.one("SELECT enabled FROM cameras WHERE id='hdst'")["enabled"] == 0
    assert siteconfig.remove_camera("nope") == "missing"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
