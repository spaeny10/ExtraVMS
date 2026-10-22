"""The structured facts the hub's customer digest is written from (summary.digest_facts, GET /api/digest/facts) and the
briefing's weapon wording: a weapon reads as confirmed only under today's rule (a HIGH-confidence, consistent second
look), so event 22909's medium "holstered handgun held in a hand" is a possible weapon that needs review.

No Qwen: events are written straight into a throwaway database.
Run: ..\\.venv\\Scripts\\python.exe tests\\test_digest_facts.py   (from backend/)
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-digest-test-")        # never the real DB
os.environ["NVR_RECORDINGS_DIR"] = tempfile.mkdtemp(prefix="nvr-digest-rec-")   # never the real recordings
os.environ["NVR_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="nvr-digest-run-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import assistant, summary  # noqa: E402
from nvr.db import db  # noqa: E402

CAM = {"host": "127.0.0.1", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": "", "main_path": "/m",
       "sub_path": "/s", "enabled": 1, "retention_days": None, "scene_notes": "", "retention_policy": None, "zones": []}
NOW = time.time()
HOLSTERED = {"object": "holstered handgun", "held": ["holstered handgun"], "weapon": "yes", "confidence": "medium",
             "held_by": "hand", "resembles_weapon": "yes"}
GUN = {"object": "black pistol", "held": ["black pistol"], "weapon": "yes", "confidence": "high", "held_by": "hand",
       "resembles_weapon": "yes"}
IDS = {}


def add(cam, start, cls="person", synopsis="a person", **kw):
    f = {"camera_id": cam, "track_id": "t", "camera_class": cls, "start_ts": start, "end_ts": start + 10, "status": "verified",
         "created_at": start, "synopsis": synopsis}
    eid = db.execute_insert(f"INSERT INTO events ({','.join(f)}) VALUES ({','.join('?' * len(f))})", list(f.values()))
    if kw:
        db.update_event(eid, **kw)
    return eid


def wc(verdict, persons):
    return {"weapon_check": {"verdict": verdict, "confirmed": verdict == "confirmed", "needs_review": verdict != "confirmed",
                             "kind": "firearm", "persons": persons,
                             "reason": "Weapon confirmed by a second look at full resolution: holstered handgun (medium confidence)."}}


def setup_module(_=None):
    db.upsert_camera({"id": "door", "name": "South Exterior Door", **CAM})
    db.upsert_camera({"id": "yard", "name": "Side Yard", **CAM})
    # event 22909 as stored before the rule changed: "confirmed" on a medium, self-contradicting answer
    IDS["22909"] = add("door", NOW - 3600, synopsis="A man entered. Another person holding a black handgun. Weapon confirmed.",
                       priority="high", synopsis_json=wc("confirmed", [HOLSTERED]))
    IDS["real"] = add("door", NOW - 3000, synopsis="A man holding a pistol.", priority="high", synopsis_json=wc("confirmed", [GUN]))
    IDS["cleared"] = add("door", NOW - 2900, priority="low", synopsis_json=wc("not_confirmed", []))
    IDS["reviewed"] = add("door", NOW - 2800, synopsis="A van backed up to the dock.", priority="high",
                          feedback={"verdict": "correct", "at": NOW})
    IDS["high"] = add("yard", NOW - 2700, synopsis="A man climbed the fence.", priority="high")
    IDS["ppe_high"] = add("yard", NOW - 2600, priority="high", policy={"kind": "ppe", "text": "No hard hat", "priority": "high"},
                          detections={"ppe": {"verdict": "violation", "zone": "Side Yard", "violation": ["hard_hat"]}})
    for i in range(3):
        add("door", NOW - 2500 + i, detections={"ppe": {"verdict": "violation", "zone": "East Door", "violation": ["hard_hat", "vest"]}})
    IDS["long"] = add("yard", NOW - 2000, priority="medium", anomaly=0.97,
                      anomaly_json={"score": 0.97, "reasons": ["Stayed 45 min, longer than 100% of visits"]})
    IDS["rule"] = add("yard", NOW - 1900, priority="medium", policy={"kind": "after_hours", "text": "Person after hours", "priority": "medium"})
    IDS["old"] = add("yard", NOW - 3 * 86400, synopsis="Long ago.", priority="high")


def test_weapons_read_under_the_new_rule():
    f = summary.digest_facts(NOW - 86400, NOW)
    w = {x["id"]: x for x in f["weapons"]}
    assert IDS["cleared"] not in w                                         # a cleared claim is no weapon
    old = w[IDS["22909"]]
    assert old["verdict"] == "unclear" and not old["confirmed"] and old["needs_review"]
    assert old["confidence"] == "medium" and old["object"] == "holstered handgun" and old["camera"] == "South Exterior Door"
    real = w[IDS["real"]]
    assert real["verdict"] == "confirmed" and real["confirmed"] and real["confidence"] == "high"


def test_attention_is_unreviewed_high_priority_without_ppe():
    f = summary.digest_facts(NOW - 86400, NOW)
    ids = [a["id"] for a in f["attention"]]
    assert IDS["22909"] in ids and IDS["real"] in ids and IDS["high"] in ids
    assert IDS["reviewed"] not in ids and IDS["ppe_high"] not in ids and IDS["old"] not in ids
    assert ids == sorted(ids, key=lambda i: -db.event(i)["start_ts"])      # newest first
    a = next(a for a in f["attention"] if a["id"] == IDS["22909"])
    # no description for a weapon event: the model's "Weapon confirmed" text never reaches the digest
    assert a["synopsis"] is None and a["weapon"]["verdict"] == "unclear"
    assert next(a for a in f["attention"] if a["id"] == IDS["high"])["synopsis"] == "A man climbed the fence."


def test_unusual_rules_ppe_counts():
    f = summary.digest_facts(NOW - 86400, NOW)
    u = next(x for x in f["unusual"] if x["id"] == IDS["long"])
    assert u["reasons"] == ["Stayed 45 min, the longest visit on record"]
    assert [r["kind"] for r in f["rules"]] == ["after_hours"] and f["rules"][0]["examples"] == [IDS["rule"]]
    assert f["ppe"]["count"] == 4 and [z["zone"] for z in f["ppe"]["zones"]] == ["East Door", "Side Yard"]
    assert f["ppe"]["zones"][0]["count"] == 3 and f["ppe"]["missing"]["hard hat"] == 4
    assert f["counts"]["person"] >= 9


def test_briefing_line_says_unconfirmed_for_the_old_medium_confirmation():
    line = assistant._event_line(db.event(IDS["22909"]))
    assert "WEAPON CONFIRMED" not in line and "possible weapon, UNCONFIRMED" in line
    assert "(holstered handgun, medium confidence)" in line and "medium confidence)." not in line   # the old reason left out
    assert "WEAPON CONFIRMED (black pistol, high confidence)" in assistant._event_line(db.event(IDS["real"]))
    assert "the longest visit on record" in assistant._event_line(db.event(IDS["long"]))


if __name__ == "__main__":
    setup_module()
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
