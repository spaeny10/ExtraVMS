"""PPE compliance (ppe.py) on a throwaway DB, with the PPE detector and Qwen faked.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_ppe.py   (from backend/)
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-ppe-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from nvr import advisor, policy, ppe, synopsis, verifier, zones  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402
from nvr.verifier import event_dir  # noqa: E402

T0 = 1_790_000_000.0
YARD = {"name": "Yard", "type": "ppe", "points": [[0, 0.5], [1, 0.5], [1, 1], [0, 1]], "required": ["hard_hat", "vest"]}
CAM = {"id": "cam1", "name": "Site gate", "host": "10.0.0.5", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": "p",
       "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": [YARD], "retention_days": None, "scene_notes": "",
       "retention_policy": None, "policies": []}
db.upsert_camera(CAM)
PERSON = [0.4, 0.4, 0.5, 0.8]   # normalised person box; feet at y=0.8, inside the Yard


def walk(start, secs, inside=True, step=0.5):
    """A person standing (feet in the Yard, or above it) for secs, sampled every step seconds."""
    y = 0.0 if inside else -0.45
    return [[start + i * step, PERSON[0], PERSON[1] + y, PERSON[2], PERSON[3] + y, 0.9] for i in range(int(secs / step) + 1)]


def make(path, start=T0, status="verified"):
    det = {"samples": [{"ts": p[0], "match": {"cls": "person", "conf": 0.9, "box": p[1:5]}} for p in path[::4]],
           "keyframes": [], "time_shift_s": 0.0}
    eid = db.create_event(camera_id="cam1", track_id="t", camera_class="person", camera_conf=0.9, start_ts=start,
                          end_ts=path[-1][0], path=path, status=status, detections=det, yolo_class="person", yolo_conf=0.9,
                          yolo_hits=3, clip_start=start - 5)
    (event_dir(eid) / "clip.mp4").write_bytes(b"x")
    db.update_event(eid, clip=f"events/{eid}/clip.mp4")
    return eid


def hat(conf, worn=True):
    return {"kind": ("hard_hat", worn), "conf": conf, "box": [0.43, 0.40, 0.47, 0.45]}


def vest(conf, worn=True):
    return {"kind": ("vest", worn), "conf": conf, "box": [0.41, 0.48, 0.49, 0.62]}


class FakeDetector:
    available = True

    def __init__(self, boxes):
        self.boxes, self.calls = boxes, 0

    def detect(self, images):
        self.calls += 1
        return [list(self.boxes) for _ in images]


def fake_frames(clip, clip_start, targets):
    return {t: np.zeros((90, 160, 3), np.uint8) for t in targets}


verifier.grab_frames = fake_frames   # ppe.detect_event imports it at call time
VLM_CALLS = []


def fake_vlm(answer):
    async def confirm(crops):
        VLM_CALLS.append(len(crops))
        return None if answer is None else {**ppe.parse_vlm(answer), "model": "fake-qwen"}
    return confirm


def run_check(eid, boxes, vlm_answer=None, zone_list=None):
    VLM_CALLS.clear()
    ppe.confirm_with_vlm = fake_vlm(vlm_answer)
    det = FakeDetector(boxes)

    async def go():
        loop = asyncio.get_running_loop()
        return await ppe.check_event(db.event(eid), zone_list or [YARD], lambda fn, *a: loop.run_in_executor(None, fn, *a), det, None)
    return asyncio.run(go()), det


# ---------------------------------------------------------------- zones, dwell, plan

def test_ppe_zone_normalises_and_filters_nothing():
    z = zones.normalize([{**YARD, "required": ["vest", "gloves", "hard_hat"]}, {**YARD, "required": None}, {**YARD, "required": []}])
    assert [x["type"] for x in z] == ["ppe", "ppe", "ppe"]
    assert z[0]["required"] == ["hard_hat", "vest"] and z[1]["required"] == ["hard_hat", "vest"] and z[2]["required"] == []
    assert len(ppe.ppe_zones(z)) == 2  # a zone requiring nothing checks nothing
    assert zones.allowed((0.5, 0.1), z) and zones.allowed((0.5, 0.9), z)  # not an include or exclude zone


def test_dwell_counts_continuous_time_inside():
    path = walk(T0, 4, inside=False) + walk(T0 + 4.5, 10)
    d = ppe.dwell(path, YARD["points"])
    assert d["first"] == T0 + 4.5 and d["last"] == T0 + 14.5 and abs(d["seconds"] - 10) < 1e-6
    gappy = walk(T0, 2) + walk(T0 + 10, 2)          # a 8 s hole is not continuous presence
    assert abs(ppe.dwell(gappy, YARD["points"])["seconds"] - 4) < 1e-6
    assert ppe.dwell(walk(T0, 5, inside=False), YARD["points"]) is None


def test_plan_needs_min_dwell_and_starts_after_grace():
    e = {"camera_class": "person", "status": "verified", "path": walk(T0, 12)}
    (c,) = ppe.plan(e, [YARD])
    assert c["zone"] == "Yard" and c["required"] == ["hard_hat", "vest"] and c["priority"] == "medium"
    assert c["window"] == [T0 + settings.ppe_grace_s, T0 + 12]
    passing = {"camera_class": "person", "status": "verified", "path": walk(T0, 3)}
    assert ppe.plan(passing, [YARD]) == []                          # through the zone in < min_dwell_s: no check
    assert ppe.plan({**e, "camera_class": "vehicle"}, [YARD]) == []
    assert ppe.plan(e, [{**YARD, "min_dwell_s": 20}]) == []          # per-zone dwell
    assert ppe.plan(e, [{**YARD, "type": "area"}]) == []


# ---------------------------------------------------------------- detector answers

def test_item_scores_use_head_and_torso_regions():
    s = ppe.item_scores([hat(0.8), vest(0.7, worn=False), {"kind": ("hard_hat", True), "conf": 0.9, "box": [0.43, 0.7, 0.47, 0.75]}], PERSON)
    assert s["hard_hat"] == [0.8, 0.0]          # a "hat" at knee height (a hat on the ground) doesn't count
    assert s["vest"] == [0.0, 0.7]
    assert ppe.item_scores([hat(0.9)], [0.7, 0.4, 0.8, 0.8])["hard_hat"] == [0.0, 0.0]  # someone else's hat


def test_hat_present_vest_missing_is_a_vest_violation():
    calls = ppe.assess([{"scores": {"hard_hat": [0.8, 0.0], "vest": [0.1, 0.7]}}] * 3, ["hard_hat", "vest"])
    assert calls == {"hard_hat": "present", "vest": "missing"}
    final = ppe.decide(["hard_hat", "vest"], calls, None)
    assert final["verdict"] == "violation" and final["violation"] == ["vest"]
    assert ppe.combine_frames(["present", "missing"]) == "unclear" and ppe.combine_frames(["present", "present", "missing"]) == "present"
    assert ppe.combine_frames(["unclear", "missing"]) == "missing" and ppe.combine_frames([]) == "unclear"


def test_decide_with_qwen():
    req = ["hard_hat", "vest"]
    # detector can't tell -> Qwen decides
    r = ppe.decide(req, {"hard_hat": "unclear", "vest": "present"}, {"hard_hat": "missing", "vest": "missing"})
    assert r["verdict"] == "violation" and r["violation"] == ["hard_hat"]   # detector-confident vest stands
    # detector false positive (says missing), Qwen sees the hat: no violation
    r = ppe.decide(req, {"hard_hat": "missing", "vest": "present"}, {"hard_hat": "present", "vest": "present"})
    assert r["verdict"] == "compliant" and r["overruled"] == ["hard_hat"]
    # Qwen unclear keeps the detector's answer; Qwen unavailable too
    assert ppe.decide(req, {"hard_hat": "missing", "vest": "present"}, {"hard_hat": "unclear", "vest": "unclear"})["violation"] == ["hard_hat"]
    assert ppe.decide(req, {"hard_hat": "unclear", "vest": "present"}, None)["verdict"] == "unclear"
    # ppe_vlm_all: Qwen also overrules a detector "present" (a cap taken for a hard hat)
    assert ppe.decide(req, {"hard_hat": "present", "vest": "present"}, {"hard_hat": "missing", "vest": "present"}, vlm_first=True)["violation"] == ["hard_hat"]
    assert ppe.needs_vlm({"hard_hat": "present", "vest": "unclear"}) and not ppe.needs_vlm({"hard_hat": "present", "vest": "present"})


def test_parse_vlm_schema():
    assert set(ppe.SCHEMA["required"]) == {"head", "hard_hat", "torso", "hi_vis_vest"}
    assert ppe.SCHEMA["properties"]["hard_hat"]["enum"] == ["yes", "no", "unclear"]
    p = ppe.parse_vlm({"head": "a blue baseball cap", "hard_hat": "no", "torso": "orange vest with strips", "hi_vis_vest": "YES"})
    assert p["hard_hat"] == "missing" and p["vest"] == "present" and p["head"] == "a blue baseball cap"
    bad = ppe.parse_vlm({"hard_hat": "maybe"})
    assert bad["hard_hat"] == "unclear" and bad["vest"] == "unclear" and bad["torso"] == ""
    assert "ppe" in __import__("nvr.vlmroute", fromlist=["TASKS"]).TASKS


# ---------------------------------------------------------------- the whole check (fakes)

def test_check_event_compliant_never_asks_qwen():
    eid = make(walk(T0, 12))
    res, det = run_check(eid, [hat(0.85), vest(0.8)], vlm_answer={"hard_hat": "no", "hi_vis_vest": "no"})
    assert res["verdict"] == "compliant" and res["vlm"] is None and VLM_CALLS == [] and det.calls == 1
    assert len(res["frames"]) >= 2 and all(f["ts"] >= T0 + settings.ppe_grace_s - 0.3 for f in res["frames"])
    assert "_images" in res


def test_check_event_unclear_asks_qwen_and_qwen_no_overrules():
    eid = make(walk(T0 + 100, 12, ), start=T0 + 100)
    res, _ = run_check(eid, [vest(0.8)], vlm_answer={"head": "bare head", "hard_hat": "no", "torso": "vest", "hi_vis_vest": "yes"})
    assert res["detector"] == {"hard_hat": "unclear", "vest": "present"} and VLM_CALLS and VLM_CALLS[0] <= 3
    assert res["verdict"] == "violation" and res["violation"] == ["hard_hat"] and res["vlm"]["model"] == "fake-qwen"
    # detector false positive: it sees "no helmet", Qwen sees a hard hat -> compliant
    res, _ = run_check(eid, [hat(0.7, worn=False), vest(0.8)], vlm_answer={"hard_hat": "yes", "hi_vis_vest": "yes"})
    assert res["detector"]["hard_hat"] == "missing" and res["verdict"] == "compliant" and res["overruled"] == ["hard_hat"]


def test_passing_through_is_not_checked():
    eid = make(walk(T0 + 200, 3), start=T0 + 200)
    res, det = run_check(eid, [hat(0.1)])
    assert res is None and det.calls == 0


def test_missing_model_is_reported():
    eid = make(walk(T0 + 300, 12), start=T0 + 300)

    class Missing(FakeDetector):
        available = False

    async def go():
        return await ppe.check_event(db.event(eid), [YARD], None, Missing([]), None)
    res = asyncio.run(go())
    assert res["verdict"] == "unavailable" and "PPE model missing" in res["error"]
    ctx = {"cameras": db.cameras(), "ppe_model_present": False}
    (f,) = advisor.check_ppe(ctx)
    assert f.key == "ppe:model" and "Site gate" in f.title
    assert advisor.check_ppe({**ctx, "ppe_model_present": True}) == []


# ---------------------------------------------------------------- pipeline: rule hit, priority, tags, synopsis

def test_pipeline_violation_sets_priority_tag_and_prompt():
    from nvr import pipeline as pl
    p = pl.Pipeline()
    p.cameras = {c["id"]: c for c in db.cameras()}
    p.vlm_ready = True
    p.ppe = FakeDetector([hat(0.8), vest(0.75, worn=False)])
    ppe.confirm_with_vlm = fake_vlm({"hard_hat": "yes", "hi_vis_vest": "no"})
    eid = make(walk(T0 + 400, 15), start=T0 + 400)

    async def embed(text):
        return None
    pl.vlm.embed = embed

    async def go():
        res = await p.check_ppe(eid)
        broken = policy.check(eid, p.cameras["cam1"])
        await p.reindex(eid)
        return res, broken
    res, broken = asyncio.run(go())
    assert res["verdict"] == "violation" and res["violation"] == ["vest"] and VLM_CALLS  # vest missing -> Qwen confirmed
    e = db.event(eid)
    assert e["detections"]["ppe"]["verdict"] == "violation" and "_images" not in e["detections"]["ppe"]
    assert e["detections"]["samples"]   # verification data kept alongside
    assert broken["kind"] == "ppe" and broken["priority"] == "medium"
    assert broken["text"] == "No hi-vis vest in PPE zone 'Yard' (15 s)" and broken["tags"] == ["ppe violation", "no hi-vis vest"]
    assert e["policy"]["kind"] == "ppe" and e["priority"] == "medium"
    assert (event_dir(eid) / "ppe.jpg").exists() and res["marked"] == "ppe.jpg"
    hits = db.search("no hi-vis vest", None)
    assert any(h["id"] == eid for h in hits)
    facts = synopsis.event_facts(e, p.cameras["cam1"])
    assert "PPE violation" in facts and "hi-vis vest" in facts and "'Yard'" in facts
    # the zone is removed: the rule no longer applies, and re-checking drops the stored answer
    cam = {**CAM, "zones": []}
    db.upsert_camera(cam)
    p.cameras = {c["id"]: c for c in db.cameras()}
    assert policy.check(eid, p.cameras["cam1"]) is None and db.event(eid)["priority"] != "medium"
    assert asyncio.run(p.check_ppe(eid)) is None and "ppe" not in db.event(eid)["detections"]
    db.upsert_camera(CAM)


def test_compliant_person_has_no_rule_and_a_fact():
    eid = make(walk(T0 + 500, 12), start=T0 + 500)
    res, _ = run_check(eid, [hat(0.9), vest(0.9)])
    det = db.event(eid)["detections"]
    det["ppe"] = {k: v for k, v in res.items() if k != "_images"}
    db.update_event(eid, detections=det)
    assert policy.check(eid, db.cameras()[0]) is None
    assert "wore the required hard hat and hi-vis vest" in synopsis.ppe_fact(db.event(eid))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
