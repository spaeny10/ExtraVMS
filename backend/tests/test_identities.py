"""People & vehicles grouping and naming on a throwaway database (synthetic fingerprints, no GPU).

Run: ..\\.venv\\Scripts\\python.exe tests\\test_identities.py   (from backend/)
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-ident-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from nvr import identities  # noqa: E402
from nvr.db import db  # noqa: E402

CAM = {"host": "127.0.0.1", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": "", "main_path": "/m",
       "sub_path": "/s", "enabled": 1, "zones": [], "retention_days": None, "scene_notes": "", "retention_policy": None}
NOW = time.time()
rng = np.random.default_rng(7)


def unit(v):
    return v / np.linalg.norm(v)


def person_vec(base, noise=0.25):
    """Same person: base direction plus noise (cosine ~0.85-0.95); different people: unrelated directions."""
    return unit(base + rng.normal(0, noise / np.sqrt(512), 512))


def add(cam, ts, cls="person", vec=None, **kw):
    f = {"camera_id": cam, "track_id": "t", "camera_class": cls, "start_ts": ts, "end_ts": ts + 8, "status": "verified",
         "created_at": ts, "yolo_conf": 0.9, "synopsis": f"a {cls}", **kw}
    eid = db.execute_insert(f"INSERT INTO events ({','.join(f)}) VALUES ({','.join('?' * len(f))})", list(f.values()))
    if vec is not None:
        db.set_vec(identities.VEC_TABLE[cls], eid, vec)
    return eid


A, B, C = (unit(rng.normal(size=512)) for _ in range(3))


def setup_module(_=None):
    db.upsert_camera({"id": "cam1", "name": "Side Yard", **CAM})
    db.upsert_camera({"id": "cam2", "name": "East Door", **CAM})
    global ids_a, ids_b, id_c, id_nofp
    ids_a = [add("cam2", NOW - 3600 + i * 300, vec=person_vec(A)) for i in range(5)]   # person A, 5 sightings
    ids_b = [add("cam1", NOW - 3000 + i * 400, vec=person_vec(B)) for i in range(3)]   # person B
    id_c = add("cam2", NOW - 500, vec=person_vec(C))                                    # person C once
    id_nofp = add("cam2", NOW - 200)                                                    # no fingerprint
    add("cam2", NOW - 100, vec=person_vec(A), feedback='{"verdict": "false_alarm"}')    # ignored


def test_average_link_groups_same_person():
    r = identities.clusters("person", NOW - 7200)
    sizes = sorted(c["sightings"] for c in r["clusters"])
    assert sizes == [1, 1, 3, 5], sizes
    big = next(c for c in r["clusters"] if c["sightings"] == 5)
    assert {e["id"] for e in big["events"]} == set(ids_a) and big["cameras"] == ["East Door"]
    lone = next(c for c in r["clusters"] if not c["fingerprinted"])
    assert lone["events"][0]["id"] == id_nofp
    assert r["sightings"] == 10 and r["clusters"][0]["key"] == "p1"   # newest cluster first


def test_journey_forces_merge():
    # two sightings with very different fingerprints but a confirmed journey between them
    x = add("cam2", NOW - 50, vec=unit(rng.normal(size=512)), journey_id=99)
    y = add("cam1", NOW - 40, vec=unit(rng.normal(size=512)), journey_id=99)
    r = identities.clusters("person", NOW - 60)
    joined = next(c for c in r["clusters"] if {e["id"] for e in c["events"]} >= {x, y})
    assert joined["sightings"] == 2 and joined["cameras"] == ["East Door", "Side Yard"]


def test_naming_and_recognition():
    ident = identities.name_cluster("person", "Alex", ids_a, notes="warehouse lead")
    assert ident["sightings"] == 5
    r = identities.clusters("person", NOW - 7200)
    named = [c for c in r["clusters"] if c["name"]]
    assert len(named) == 1 and named[0]["name"] == "Alex" and named[0]["sightings"] == 5, named
    # a brand-new sighting of A is recognised by name, B is not
    new_a = add("cam1", NOW - 10, vec=person_vec(A))
    assert "Alex" in (identities.identity_facts("person", new_a) or "")
    assert identities.identity_facts("person", ids_b[0]) is None
    # naming more sightings with the same name merges into one identity
    again = identities.name_cluster("person", "alex", [new_a])
    assert again["id"] == ident["id"] and again["sightings"] == 6
    identities.update_identity(ident["id"], notes="owner")
    assert db.one("SELECT notes FROM identities WHERE id=?", [ident["id"]])["notes"] == "owner"
    identities.delete_identity(ident["id"])
    assert not [c for c in identities.clusters("person", NOW - 7200)["clusters"] if c["name"]]


def test_watch_list_marks_matching_sightings():
    from nvr import baseline
    ident = identities.name_cluster("person", "Pink Hat", ids_b)
    assert identities.check_watch(ids_b[0]) is None                       # named but not watched
    ident, changed = identities.set_watch(ident["id"], True, "seen loitering", recheck_hours=48)
    assert ident["watch"] == 1 and set(changed) >= set(ids_b), changed
    assert db.event(ids_b[0])["watched"] == "Pink Hat" and db.event(ids_a[0])["watched"] is None
    assert baseline.priority(db.event(ids_b[0]), 0.0) == "medium"           # watch raises priority
    new_b = add("cam2", NOW - 5, vec=person_vec(B))
    assert identities.check_watch(new_b) == "Pink Hat"                    # a new sighting is flagged
    assert "watch list" in identities.identity_facts("person", new_b)
    r = identities.clusters("person", NOW - 7200)
    assert any(c["watch"] for c in r["clusters"] if c["name"] == "Pink Hat")
    _, changed = identities.set_watch(ident["id"], False, recheck_hours=48)
    assert db.event(ids_b[0])["watched"] is None and new_b in changed
    identities.delete_identity(ident["id"])


def test_vehicles_use_their_own_table():
    D = identities.VEHICLE_DIM  # CLIP + colour histogram
    v = unit(rng.normal(size=D))
    a = add("cam1", NOW - 900, cls="vehicle", vec=unit(v + rng.normal(0, 0.1 / np.sqrt(D), D)))
    b = add("cam1", NOW - 800, cls="vehicle", vec=unit(v + rng.normal(0, 0.1 / np.sqrt(D), D)))
    add("cam1", NOW - 700, cls="vehicle", vec=unit(rng.normal(size=D)))
    r = identities.clusters("vehicle", NOW - 1000)
    assert sorted(c["sightings"] for c in r["clusters"]) == [1, 2]
    assert {e["id"] for e in max(r["clusters"], key=lambda c: c["sightings"])["events"]} == {a, b}


def test_color_hist_separates_white_from_black():
    white = np.full((60, 120, 3), 235, np.uint8)
    black = np.full((60, 120, 3), 25, np.uint8)
    red = np.zeros((60, 120, 3), np.uint8); red[:, :, 2] = 200
    hw, hb, hr = identities.color_hist(white), identities.color_hist(black), identities.color_hist(red)
    assert hw @ hb < 0.2 and hw @ hr < 0.2
    assert identities.color_hist(np.full((60, 120, 3), 225, np.uint8)) @ hw > 0.9  # same colour, slightly darker


def test_second_outfit_becomes_a_new_look():
    ident = identities.name_cluster("person", "Sam", [add("cam1", NOW - 600, vec=person_vec(A))])
    assert ident["looks"] == 1
    other = add("cam1", NOW - 500, vec=person_vec(C))       # same person, different clothes (unrelated vector)
    assert identities.identity_facts("person", other) is None
    ident = identities.name_cluster("person", "Sam", [other])
    assert ident["looks"] == 2 and ident["sightings"] == 2
    assert "Sam" in (identities.identity_facts("person", add("cam2", NOW - 20, vec=person_vec(C))) or "")
    assert "Sam" in (identities.identity_facts("person", add("cam2", NOW - 10, vec=person_vec(A))) or "")
    ident = identities.name_cluster("person", "Sam", [add("cam1", NOW - 400, vec=person_vec(A))])
    assert ident["looks"] == 2 and ident["sightings"] == 3  # resembles look A: refined, not a third look
    identities.delete_identity(ident["id"])
    assert not db.all("SELECT 1 FROM identity_looks WHERE identity_id=?", [ident["id"]])


if __name__ == "__main__":
    setup_module()
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
