"""Events from ONVIF rule events, for cameras without object metadata (ruleevents.py; Reolink RP-PCT8MD, 2026-10-09).

Tracker lifecycle (PeopleDetect true/false, repeats, a missing false, too-long split, Vehicle, motion topics), auto
mode (Analytics=false, no metadata track, no objects for the window, a metadata camera stays as it is), the verifier on
an event without camera boxes (fake YOLO, zones from YOLO's boxes, motion -> vehicle, parked check), the stream probe's
Analytics flag and path suggestion, the metadata reader's back-off when the stream has no metadata track, and the
health problem that must not fire for event-only cameras. No camera, MediaMTX, GPU or network.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_rule_events.py   (from backend/)
"""
import asyncio
import os
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

TMP = Path(tempfile.mkdtemp(prefix="nvr-ruleevents-test-"))
for k, sub in (("NVR_DATA_DIR", "data"), ("NVR_RECORDINGS_DIR", "recordings"), ("NVR_RUNTIME_DIR", "runtime")):
    os.environ[k] = str(TMP / sub)   # never the real server's database, recordings or mediamtx.yml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from nvr import cells, ingest, merge, parked, ruleevents, streams, verifier  # noqa: E402
from nvr import onvif_soap as soap  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402
from nvr.health import PathStats, StreamHealth  # noqa: E402
from nvr.ingest import DetectedObject, MetaFrame, RuleEvent  # noqa: E402
from nvr.rtsp_client import NoTrack  # noqa: E402
from nvr.tracker import Tracker  # noqa: E402

for d in (settings.data_dir, settings.recordings_dir, settings.runtime_dir):
    v = str(d).replace("\\", "/").lower()
    assert "nvr-ruleevents-test-" in v and "e:/nvr" not in v and "d:/nvr" not in v and "newvms/runtime" not in v, d

PEOPLE = "RuleEngine/MyRuleDetector/PeopleDetect"
VEHICLE = "RuleEngine/MyRuleDetector/VehicleDetect"
CELL = "RuleEngine/CellMotionDetector/Motion"
ALARM = "VideoSource/MotionAlarm"
REOLINK = {"id": "cam6", "name": "Panoramic", "enabled": 1, "event_source": "auto", "motion_events": 0,
           "synopsis_labels": None, "zones": [], "streams": {"metadata_analytics": False}}
METADATA_CAM = {**REOLINK, "id": "cam1", "streams": {"metadata_analytics": True}}
for _cid in ("cam1", "cam2", "cam3", "cam4"):   # events reference a camera row
    db.upsert_camera({"id": _cid, "name": _cid, "host": "10.0.0.9", "zones": []})


def rule(topic, state, at, cam="cam6", data=None):
    """A Reolink-style notification as EventPuller._parse makes it, received at `at` (this PC's clock)."""
    if data is None:
        data = {"Source": "000", "State": "true" if state else "false"} if state is not None else {"Source": "000"}
    return RuleEvent(camera_id=cam, ts=at, topic=topic, rule=None, state=state, data=data, received=at)


class Closed(list):
    async def __call__(self, eid):
        self.append(eid)


def tracker(cams=None):
    closed = Closed()
    tr = Tracker(closed)
    rows = {c["id"]: c for c in (cams or [REOLINK, METADATA_CAM])}
    tr.camera = lambda cid: rows.get(cid)
    opened = []
    tr.on_opened = opened.append
    return tr, closed, opened


def sweep(tr):
    asyncio.run(tr.sweep())


# --------------------------------------------------------------------------- topics


def test_classify_topics():
    assert ruleevents.classify(PEOPLE, {"State": "true"}) == ("object", "person")
    assert ruleevents.classify(VEHICLE, {}) == ("object", "vehicle")
    assert ruleevents.classify("RuleEngine/MyRuleDetector/FaceDetect", {}) == ("object", "person")
    assert ruleevents.classify("RuleEngine/MyRuleDetector/DogCatDetect", {}) == ("object", "animal")
    assert ruleevents.classify(CELL, {"IsMotion": "true"}) == ("motion", "motion")
    assert ruleevents.classify(ALARM, {"State": "true"}) == ("motion", "motion")
    # Hikvision / Dahua field and line detectors: the label from the object type, else it's motion
    assert ruleevents.classify("RuleEngine/FieldDetector/ObjectsInside", {"IsInside": "true", "ObjectType": "Human"}) == ("object", "person")
    assert ruleevents.classify("RuleEngine/LineDetector/Crossed", {"ObjectType": "Vehicle"}) == ("object", "vehicle")
    assert ruleevents.classify("RuleEngine/FieldDetector/ObjectsInside", {"IsInside": "true"}) == ("motion", "motion")
    assert ruleevents.classify("RuleEngine/SomeVendor/Intrusion", {"ObjectType": "Human"}) == ("object", "person")
    assert ruleevents.classify("Device/Trigger/DigitalInput", {"LogicalState": "true"}) is None
    assert ruleevents.classify("VideoSource/ImageTooBlurry", {"State": "true"}) is None
    # what opens: person / vehicle / motion; animals and packages only where synopsis_labels name them
    assert ruleevents.wanted("person", REOLINK) and ruleevents.wanted("motion", REOLINK)
    assert not ruleevents.wanted("animal", REOLINK) and ruleevents.wanted("animal", {"synopsis_labels": ["animal"]})


def test_reolink_people_detect_opens_and_closes_one_event():
    tr, closed, opened = tracker()
    now = time.time()
    tr.on_rule_event(rule(PEOPLE, True, now - 10))
    tr.on_rule_event(rule(CELL, True, now - 10, data={"IsMotion": "true", "Rule": "000"}))   # motion: no second event
    tr.on_rule_event(rule(ALARM, True, now - 10))
    assert len(opened) == 1
    e = db.event(opened[0])
    assert e["status"] == "open" and e["camera_class"] == "person" and e["camera_id"] == "cam6"
    assert e["track_id"].startswith(ruleevents.TRACK_PREFIX) and ruleevents.is_rule_event(e)
    assert e["detections"]["source"] == ruleevents.SOURCE and e["cells"] is None
    assert ruleevents.whole_frame(e["path"][0][1:5])
    sweep(tr)
    assert db.event(opened[0])["status"] == "open"   # still on: the camera has not said false
    tr.on_rule_event(rule(PEOPLE, False, now - 4))
    sweep(tr)   # 4 s after the false (> track_end_gap): closed
    e = db.event(opened[0])
    assert e["status"] == "pending" and abs(e["end_ts"] - (now - 4)) < 0.01 and closed == [opened[0]], (e, closed)
    assert e["cells"] is None   # whole-frame points cross no known cells
    assert not tr.tracks
    rows = db.all("SELECT topic, state FROM rule_events WHERE camera_id='cam6'")
    assert any(r["topic"] == PEOPLE for r in rows)   # still stored like every rule event


def test_quick_false_true_and_repeats_extend_the_same_event():
    tr, closed, opened = tracker()
    now = time.time()
    tr.on_rule_event(rule(PEOPLE, True, now - 30))
    tr.on_rule_event(rule(PEOPLE, False, now - 20))
    tr.on_rule_event(rule(PEOPLE, True, now - 19.5))   # back within track_end_gap: the same visit
    tr.on_rule_event(rule(PEOPLE, True, now - 3))      # repeated true while open
    sweep(tr)
    assert len(opened) == 1 and not closed
    t = tr.tracks[("cam6", "onvif:person")]
    assert t.on and t.last_on == now - 3 and len(t.path) == 4
    tr.on_rule_event(rule(PEOPLE, False, now - 2.5))
    sweep(tr)
    e = db.event(opened[0])
    assert e["status"] == "pending" and closed == opened and e["start_ts"] == now - 30
    assert e["path"][0][0] == round(now - 30, 3) and e["path"][-1][0] == round(now - 2.5, 3)


def test_missing_false_closes_by_timeout():
    tr, closed, opened = tracker()
    now = time.time()
    tr.on_rule_event(rule(PEOPLE, True, now - settings.rule_event_on_timeout - 5))
    sweep(tr)
    assert len(opened) == 1 and closed == opened and not tr.tracks   # timed out: no follow-on event
    assert db.event(opened[0])["status"] == "pending"


def test_long_presence_splits_like_a_long_track():
    tr, closed, opened = tracker()
    now = time.time()
    tr.on_rule_event(rule(PEOPLE, True, now - settings.track_max_seconds - 5))
    sweep(tr)   # still on and under the timeout, but longer than track_max_seconds: closed, the next one opens
    assert len(closed) == 1 and len(opened) == 2 and closed[0] == opened[0]
    assert db.event(opened[1])["status"] == "open" and ("cam6", "onvif:person") in tr.tracks
    tr.on_rule_event(rule(PEOPLE, False, now - 3))
    sweep(tr)
    assert closed == opened


def test_two_topics_for_one_label_keep_the_event_open():
    # PeopleDetect and FaceDetect both say person: FaceDetect ending must not end the person event
    tr, closed, opened = tracker()
    now = time.time()
    face = "RuleEngine/MyRuleDetector/FaceDetect"
    tr.on_rule_event(rule(PEOPLE, True, now - 10))
    tr.on_rule_event(rule(face, True, now - 9))
    tr.on_rule_event(rule(face, False, now - 7))
    sweep(tr)   # 7 s after FaceDetect's false (> track_end_gap), PeopleDetect still true
    assert len(opened) == 1 and not closed and tr.tracks[("cam6", "onvif:person")].on
    tr.on_rule_event(rule(PEOPLE, False, now - 4))
    sweep(tr)
    e = db.event(opened[0])
    assert closed == opened and abs(e["end_ts"] - (now - 4)) < 0.01 and not tr.tracks


def test_false_before_the_split_opens_no_new_event():
    # the camera says "gone" just before the track_max_seconds split: closed, nothing follows
    tr, closed, opened = tracker()
    now = time.time()
    tr.on_rule_event(rule(PEOPLE, True, now - settings.track_max_seconds - 1))
    tr.on_rule_event(rule(PEOPLE, False, now - 0.5))
    sweep(tr)
    assert closed == opened and len(opened) == 1 and not tr.tracks


def test_motion_and_person_are_one_event():
    cam = {**REOLINK, "motion_events": 1}
    now = time.time()
    # motion first, then the camera names it: the motion event becomes the person event
    tr, closed, opened = tracker([cam])
    tr.on_rule_event(rule(CELL, True, now - 10, data={"IsMotion": "true", "Rule": "000"}))
    tr.on_rule_event(rule(PEOPLE, True, now - 9))
    assert len(set(opened)) == 1 and list(tr.tracks) == [("cam6", "onvif:person")]
    e = db.event(opened[0])
    assert e["camera_class"] == "person" and e["track_id"].startswith("onvif:person-") and e["start_ts"] == now - 10
    tr.on_rule_event(rule(CELL, True, now - 8, data={"IsMotion": "true", "Rule": "000"}))   # more motion: no new event
    tr.on_rule_event(rule(CELL, False, now - 7, data={"IsMotion": "false", "Rule": "000"}))
    sweep(tr)
    assert len(set(opened)) == 1 and not closed                   # motion ending does not end the person
    tr.on_rule_event(rule(PEOPLE, False, now - 4))
    sweep(tr)
    assert closed == [opened[0]] and db.event(opened[0])["status"] == "pending" and not tr.tracks
    # the person first: motion opens nothing of its own, it is recorded on the person event
    tr, closed, opened = tracker([cam])
    tr.on_rule_event(rule(PEOPLE, True, now - 10))
    tr.on_rule_event(rule(CELL, True, now - 9, data={"IsMotion": "true", "Rule": "000"}))
    assert len(opened) == 1 and list(tr.tracks) == [("cam6", "onvif:person")]
    assert any(r["topic"] == CELL for r in tr.tracks[("cam6", "onvif:person")].rules)
    tr.on_rule_event(rule(PEOPLE, False, now - 4))
    sweep(tr)
    assert closed == opened and db.event(opened[0])["camera_class"] == "person"


def test_no_metadata_event_while_an_onvif_event_is_open():
    # Analytics=false, no objects yet: the PeopleDetect event opens; objects then arrive in the metadata. Never both.
    cam = {**REOLINK, "id": "cam2"}
    tr, closed, opened = tracker([cam])
    now = time.time()
    tr.on_rule_event(rule(PEOPLE, True, now - 20, cam="cam2"))
    assert len(opened) == 1
    obj = DetectedObject(object_id="5", cls="Human", conf=0.9, box=(0.2, 0.2, 0.3, 0.5))
    for ts in (now - 16, now - 15, now - 14, now - 13):
        tr.on_frame(MetaFrame(camera_id="cam2", ts=ts, objects=[obj]))
    assert db.one("SELECT COUNT(*) AS n FROM events WHERE camera_id='cam2'")["n"] == 1
    assert tr.tracks[("cam2", "5")].event_id is None
    tr.on_rule_event(rule(PEOPLE, False, now - 10, cam="cam2"))
    sweep(tr)
    assert closed == opened
    # still there after the ONVIF event ended: its own event, starting no earlier than the last suppressed frame
    for ts in (now - 9, now - 7):
        tr.on_frame(MetaFrame(camera_id="cam2", ts=ts, objects=[obj]))
    t = tr.tracks[("cam2", "5")]
    assert t.event_id is not None and db.event(t.event_id)["start_ts"] >= now - 13


def test_vehicle_detect_is_a_vehicle_event():
    tr, closed, opened = tracker()
    now = time.time()
    tr.on_rule_event(rule(VEHICLE, True, now - 8))
    tr.on_rule_event(rule(VEHICLE, False, now - 5))
    sweep(tr)
    assert len(opened) == 1 and db.event(opened[0])["camera_class"] == "vehicle" and closed == opened


def test_line_crossing_pulse_opens_and_closes():
    tr, closed, opened = tracker()
    now = time.time()
    tr.on_rule_event(rule("RuleEngine/LineDetector/Crossed", None, now - 5, data={"ObjectId": "3", "ObjectType": "Human"}))
    sweep(tr)
    assert len(opened) == 1 and closed == opened and db.event(opened[0])["camera_class"] == "person"


def test_motion_topics_only_with_the_flag():
    tr, closed, opened = tracker()
    now = time.time()
    tr.on_rule_event(rule(CELL, True, now - 5, data={"IsMotion": "true", "Rule": "000"}))
    tr.on_rule_event(rule(ALARM, True, now - 5))
    assert opened == []
    tr2, closed2, opened2 = tracker([{**REOLINK, "motion_events": 1}])
    tr2.on_rule_event(rule(CELL, True, now - 5, data={"IsMotion": "true", "Rule": "000"}))
    tr2.on_rule_event(rule(ALARM, True, now - 4))
    assert len(opened2) == 1 and db.event(opened2[0])["camera_class"] == "motion"
    tr2.on_rule_event(rule(CELL, False, now - 3, data={"IsMotion": "false", "Rule": "000"}))
    sweep(tr2)
    assert closed2 == []   # MotionAlarm still says motion: on while any of its detectors is
    tr2.on_rule_event(rule(ALARM, False, now - 3))
    sweep(tr2)
    assert closed2 == opened2


def test_disabled_dogcat_and_initial_dump_open_nothing():
    tr, closed, opened = tracker([{**REOLINK, "enabled": 0}])
    now = time.time()
    tr.on_rule_event(rule(PEOPLE, True, now))
    tr2, _, opened2 = tracker()
    tr2.on_rule_event(rule("RuleEngine/MyRuleDetector/DogCatDetect", True, now))   # no animal label here
    init = rule(PEOPLE, True, now)
    init.initial = True
    tr2.on_rule_event(init)
    assert opened == [] and opened2 == []


def test_ptz_away_is_recorded():
    tr, closed, opened = tracker()
    tr.away_preset = lambda cid: "gate"
    now = time.time()
    tr.on_rule_event(rule(PEOPLE, True, now - 6))
    tr.on_rule_event(rule(PEOPLE, False, now - 5))
    sweep(tr)
    assert db.event(opened[0])["ptz_preset"] == "gate"


# --------------------------------------------------------------------------- auto mode


def test_auto_mode_analytics_false_switches_on_at_once():
    tr, _, _ = tracker()
    on, why = tr.uses_rule_events("cam6")
    assert on and "Analytics=false" in why
    assert tr.detection_status("cam6")["source"] == "onvif_events"


def test_auto_mode_metadata_camera_stays_off():
    tr, _, opened = tracker()
    now = time.time()
    obj = DetectedObject(object_id="7", cls="Human", conf=0.9, box=(0.2, 0.2, 0.3, 0.5))
    tr.on_frame(MetaFrame(camera_id="cam1", ts=now, objects=[obj]))
    tr.on_rule_event(rule(PEOPLE, True, now, cam="cam1"))
    assert not tr.uses_rule_events("cam1")[0]
    assert all(t.source == "metadata" for t in tr.tracks.values())
    assert [e for e in opened] == []   # the metadata track opens the event (after track_min_seconds), never the rule
    # even a camera whose Analytics flag says false keeps its metadata events while it sends objects
    tr2, _, _ = tracker([{**REOLINK, "id": "cam2"}])
    tr2.on_frame(MetaFrame(camera_id="cam2", ts=now, objects=[obj]))
    assert not tr2.uses_rule_events("cam2")[0]


def test_auto_mode_no_objects_for_the_window():
    cam = {**REOLINK, "id": "cam3", "streams": None}   # never probed: no Analytics flag
    tr, _, opened = tracker([cam])
    now = time.time()
    tr.on_rule_event(rule(PEOPLE, True, now, cam="cam3"))
    assert opened == [] and not tr.uses_rule_events("cam3", now)[0]   # watching: too early to tell
    w = tr.watch
    w.first_heard["cam3"] = now - ruleevents.AUTO_WINDOW_S - 60
    w.detect_at["cam3"] = now - 120   # a detection event two minutes ago, no object since
    on, why = tr.uses_rule_events("cam3", now)
    assert on and "10 minutes" in why
    tr.on_rule_event(rule(PEOPLE, False, now, cam="cam3"))
    tr.on_rule_event(rule(PEOPLE, True, now + 1, cam="cam3"))
    assert len(opened) == 1
    # a fresh detection (metadata objects may still be on their way) is not evidence on its own
    tr2, _, _ = tracker([cam])
    tr2.watch.first_heard["cam3"] = now - ruleevents.AUTO_WINDOW_S - 60
    tr2.watch.detect_at["cam3"] = now - 5
    assert not tr2.uses_rule_events("cam3", now)[0]
    # objects arrive: back to the metadata
    obj = DetectedObject(object_id="1", cls="person", conf=0.8, box=(0.1, 0.1, 0.2, 0.4))
    tr.on_frame(MetaFrame(camera_id="cam3", ts=now, objects=[obj]))
    assert not tr.uses_rule_events("cam3")[0]


def test_auto_mode_no_metadata_track_switches_on():
    cam = {**REOLINK, "id": "cam4", "streams": None}
    tr, _, opened = tracker([cam])
    assert not tr.uses_rule_events("cam4")[0]
    tr.metadata_missing = lambda cid: cid == "cam4"   # "no application track in SDP"
    on, why = tr.uses_rule_events("cam4")
    assert on and "no metadata track" in why
    tr.on_rule_event(rule(PEOPLE, True, time.time(), cam="cam4"))
    assert len(opened) == 1


def test_explicit_settings_win():
    tr, _, opened = tracker([{**REOLINK, "event_source": "metadata"}, {**METADATA_CAM, "event_source": "onvif_events", "streams": None}])
    assert not tr.uses_rule_events("cam6")[0] and tr.uses_rule_events("cam1")[0]
    now = time.time()
    tr.on_rule_event(rule(PEOPLE, True, now))
    tr.on_rule_event(rule(PEOPLE, True, now, cam="cam1"))
    assert len(opened) == 1 and db.event(opened[0])["camera_id"] == "cam1"


# --------------------------------------------------------------------------- verifier (no camera boxes)

PERSON_CLS, CAR, TRUCK = 0, 2, 7
NAMES = {PERSON_CLS: "person", CAR: "car", TRUCK: "truck"}


class _List(list):
    def tolist(self):
        return list(self)


class FakeYOLO:
    """predict() answers from scene(ts) -> [(cls_id, conf, box)], for the frames grab_frames was last asked for."""
    names = NAMES

    def __init__(self, scene):
        self.scene, self.targets = scene, []

    def predict(self, imgs, **_):
        out = []
        for ts in self.targets[:len(imgs)]:
            dets = self.scene(ts)
            out.append(SimpleNamespace(boxes=SimpleNamespace(xyxyn=_List([list(b) for _, _, b in dets]),
                                                             cls=_List([float(c) for c, _, _ in dets]),
                                                             conf=_List([p for _, p, _ in dets]))))
        return out


db.upsert_camera({"id": "cam6", "name": "Panoramic", "host": "10.0.0.6", "onvif_port": 8000, "rtsp_port": 554, "username": "u",
                  "password": "p", "main_path": "/Preview_01_main", "sub_path": "/Preview_01_sub", "enabled": 1, "zones": [],
                  "event_source": "auto", "motion_events": 0})


def verify(scene, label="person", start=None, dur=12.0, zone_list=None, track_id=None):
    start = start or time.time() - 100
    model = FakeYOLO(scene)

    def grab(clip, clip_start, targets):
        ts = [t for t in targets if clip_start <= t <= start + dur + settings.clip_post_roll]
        model.targets = ts
        return {t: np.zeros((36, 64, 3), np.uint8) for t in ts}

    old = verifier.grab_frames
    verifier.grab_frames = grab
    try:
        v = verifier.Verifier.__new__(verifier.Verifier)
        v.model, v._reid = model, None
        v._reid_embedding = lambda frames, detections: None
        path = [ruleevents.point(start), ruleevents.point(start + dur)]
        tid = track_id or ruleevents.track_id(label, start)
        eid = db.create_event(camera_id="cam6", track_id=tid, camera_class=label, camera_conf=0.0, start_ts=start,
                              end_ts=start + dur, path=path, status="pending", detections={"source": ruleevents.SOURCE})
        event = {"id": eid, "camera_id": "cam6", "track_id": tid, "camera_class": label, "path": path,
                 "start_ts": start, "end_ts": start + dur}
        return v.verify(event, Path("clip.mp4"), start - settings.clip_pre_roll, zone_list)
    finally:
        verifier.grab_frames = old


def walker(t0, x0=0.1, v=0.03):
    """A person walking left to right."""
    def scene(ts):
        x = x0 + v * (ts - t0)
        return [(PERSON_CLS, 0.82, (x, 0.3, x + 0.06, 0.7))]
    return scene


def test_no_box_person_is_verified_by_yolo_and_gets_a_path():
    start = time.time() - 100
    r = verify(walker(start), start=start)
    assert r["status"] == "verified" and r["yolo_class"] == "person" and r["yolo_hits"] >= 2, r
    assert r["detections"]["source"] == ruleevents.SOURCE and r["detections"]["time_shift_s"] == 0.0
    assert r["path"] and not any(ruleevents.whole_frame(p[1:5]) for p in r["path"])
    xs = [p[1] for p in r["path"]]
    assert xs == sorted(xs)   # the walk, left to right, from YOLO's boxes
    assert cells.for_event(r["path"])   # region cells from YOLO's path
    # the samples span the event (not one instant)
    ts = [d["ts"] for d in r["detections"]["samples"]]
    assert max(ts) - min(ts) >= 10


def test_no_box_event_with_nobody_is_rejected():
    r = verify(lambda ts: [])
    assert r["status"] == "rejected" and "path" not in r and r["yolo_hits"] == 0


def test_zones_apply_to_yolo_boxes():
    left = [{"name": "yard", "type": "include", "points": [[0, 0], [0.5, 0], [0.5, 1], [0, 1]]}]
    start = time.time() - 100
    # a person only on the right half: outside the include zone, rejected
    r = verify(lambda ts: [(PERSON_CLS, 0.9, (0.7, 0.3, 0.76, 0.7))], start=start, zone_list=left)
    assert r["status"] == "rejected", r
    # a person on the left half (and one on the right): verified, the path only from the one inside
    both = lambda ts: [(PERSON_CLS, 0.95, (0.7, 0.3, 0.76, 0.7)), (PERSON_CLS, 0.6, (0.2, 0.3, 0.26, 0.7))]
    r = verify(both, start=start, zone_list=left)
    assert r["status"] == "verified" and all(p[1] < 0.5 for p in r["path"]), r.get("path")
    # an exclude zone over the only person: rejected
    r = verify(lambda ts: [(PERSON_CLS, 0.9, (0.2, 0.3, 0.26, 0.7))], start=start,
               zone_list=[{"name": "street", "type": "exclude", "points": [[0, 0], [0.5, 0], [0.5, 1], [0, 1]]}])
    assert r["status"] == "rejected"


def test_wrong_class_does_not_verify():
    r = verify(lambda ts: [(CAR, 0.9, (0.2, 0.4, 0.5, 0.7))], label="person")
    assert r["status"] == "rejected"


def test_motion_event_takes_the_class_yolo_sees():
    start = time.time() - 100

    def car(ts):
        x = 0.1 + 0.04 * (ts - start)
        return [(CAR, 0.88, (x, 0.4, x + 0.2, 0.7))]
    r = verify(car, label="motion", start=start)
    assert r["status"] == "verified" and r["camera_class"] == "vehicle", r
    r = verify(walker(start), label="motion", start=start)
    assert r["status"] == "verified" and r["camera_class"] == "person"
    r = verify(lambda ts: [], label="motion", start=start)
    assert r["status"] == "rejected" and "camera_class" not in r


def test_parked_vehicle_without_camera_box_is_rejected():
    parked.save("cam6", [])
    jcb = (0.29, 0.26, 0.74, 0.58)
    r = verify(lambda ts: [(TRUCK, 0.91, jcb)], label="vehicle", dur=20.0)
    assert r["status"] == "rejected" and r["detections"].get("rejected") == parked.REASON, r["detections"].get("rejected")
    # a car driving past the parked machine: the moving one verifies it
    start = time.time() - 100

    def passing(ts):
        x = 0.05 + 0.03 * (ts - start)
        return [(TRUCK, 0.91, jcb), (CAR, 0.7, (x, 0.62, x + 0.15, 0.8))]
    parked.save("cam6", [])
    r = verify(passing, label="vehicle", start=start, dur=20.0)
    assert r["status"] == "verified" and r["yolo_class"] == "car", (r["status"], r["detections"].get("rejected"))


def test_merge_never_joins_on_whole_frame_points():
    a = [ruleevents.point(1.0)]
    b = [[3.0, 0.4, 0.3, 0.5, 0.7, 0.9]]
    assert not merge.continuous(a, b) and not merge.continuous(b, a)
    assert merge.continuous([[1.0, 0.4, 0.3, 0.5, 0.7, 0.9]], b)


def test_sample_points_widen_a_pulse():
    pts = ruleevents.sample_points({"start_ts": 100.0, "end_ts": 100.0}, 6, 5.0, 3.0)
    assert len(pts) == 6 and pts[0][0] == 98.0 and pts[-1][0] == 102.0
    pts = ruleevents.sample_points({"start_ts": 100.0, "end_ts": 130.0}, 6, 5.0, 3.0)
    assert pts[0][0] == 100.0 and pts[-1][0] == 130.0


# --------------------------------------------------------------------------- stream probe: Analytics flag, path fix

NS = ('xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
      'xmlns:tr2="http://www.onvif.org/ver20/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema"')


def body_of(inner: str):
    root = ET.fromstring(f'<s:Envelope {NS}><s:Body>{inner}</s:Body></s:Envelope>')
    return next(e for e in root if soap.local(e) == "Body")


def test_metadata_analytics_flag():
    reolink = ('<trt:GetProfilesResponse><trt:Profiles token="000"><tt:Name>mainStream</tt:Name>'
               '<tt:MetadataConfiguration token="000"><tt:Name>metaData</tt:Name><tt:UseCount>2</tt:UseCount>'
               '<tt:Analytics>false</tt:Analytics></tt:MetadataConfiguration></trt:Profiles>'
               '<trt:Profiles token="001"><tt:MetadataConfiguration token="000"><tt:Analytics>false</tt:Analytics>'
               '</tt:MetadataConfiguration></trt:Profiles></trt:GetProfilesResponse>')
    assert streams.parse_metadata_analytics(body_of(reolink)) is False
    milesight = reolink.replace("<tt:Analytics>false</tt:Analytics></tt:MetadataConfiguration></trt:Profiles>"
                                "<trt:Profiles", "<tt:Analytics>true</tt:Analytics></tt:MetadataConfiguration></trt:Profiles><trt:Profiles", 1)
    assert streams.parse_metadata_analytics(body_of(milesight)) is True
    media2 = ('<tr2:GetProfilesResponse><tr2:Profiles token="p"><tr2:Configurations><tr2:Analytics token="a"><tt:Name>x</tt:Name>'
              '</tr2:Analytics><tr2:Metadata token="m"><tt:Analytics>false</tt:Analytics></tr2:Metadata></tr2:Configurations>'
              '</tr2:Profiles></tr2:GetProfilesResponse>')
    assert streams.parse_metadata_analytics(body_of(media2)) is False
    assert streams.parse_metadata_analytics(body_of("<trt:GetProfilesResponse/>")) is None
    # stored with the probe and read back for the tracker
    db.upsert_camera({"id": "cam7", "name": "R", "host": "10.0.0.7", "zones": []})
    streams.record_probe("cam7", {"profiles": [], "media": "media", "metadata_analytics": False})
    cam = next(c for c in db.cameras() if c["id"] == "cam7")
    assert ruleevents.metadata_analytics(cam) is False


REOLINK_PROFILES = [
    {"token": "000", "name": "mainStream", "encoding": "H.264", "width": 5120, "height": 1552, "fps": 20, "path": "/Preview_01_main"},
    {"token": "001", "name": "subStream", "encoding": "H.264", "width": 1536, "height": 576, "fps": 10, "path": "/Preview_01_sub"},
]


def test_wrong_paths_get_a_one_click_suggestion():
    cam = {"id": "cam6", "main_path": "/main", "sub_path": "/sub", "streams": {"profiles": REOLINK_PROFILES, "media": "media"}}
    p = streams.plan(cam)
    assert any("does not list the main stream path /main" in x for x in p["problems"])
    assert p["suggest"] == {"main_path": "/Preview_01_main", "sub_path": "/Preview_01_sub"}
    assert streams.view(cam)["suggest"] == p["suggest"]
    # the right paths: nothing to suggest
    ok = {**cam, "main_path": "/Preview_01_main", "sub_path": "/Preview_01_sub"}
    assert streams.plan(ok)["suggest"] is None and streams.plan(ok)["problems"] == []
    # never probed: nothing to suggest
    assert streams.plan({"id": "x", "main_path": "/main", "sub_path": "/sub"})["suggest"] is None


# --------------------------------------------------------------------------- metadata reader back-off, health


class FakeRtsp:
    def __init__(self, *a, **k):
        self.sock = SimpleNamespace(close=lambda: None, settimeout=lambda t: None)


def test_no_metadata_track_backs_off_for_half_an_hour():
    old = (ingest.Rtsp, ingest.play_track, ingest.mediamtx.reader_credentials)
    ingest.Rtsp = FakeRtsp
    ingest.mediamtx.reader_credentials = lambda: ("u", "p")

    def no_track(cam, media="application"):
        raise NoTrack("no application track in SDP")
    ingest.play_track = no_track
    try:
        r = ingest.MetadataReader("cam6", lambda f: None)
        waits = []

        def wait(t):
            waits.append(t)
            if len(waits) >= 4:
                r.stop_event.set()
            return r.stop_event.is_set()
        r.stop_event.wait = wait
        r.run()
        # one DESCRIBE without the track can be a camera restarting: two quick looks, then every half hour
        assert r.no_track and waits == [*ingest.NO_TRACK_QUICK_S, ingest.NO_TRACK_RETRY_S, ingest.NO_TRACK_RETRY_S], waits
        assert ingest.NO_TRACK_QUICK_S == (30, 60)
    finally:
        ingest.Rtsp, ingest.play_track, ingest.mediamtx.reader_credentials = old


def test_ingest_set_to_onvif_events_starts_no_metadata_reader():
    loop = asyncio.new_event_loop()
    try:
        q = asyncio.Queue()
        ing = ingest.CameraIngest({**REOLINK, "event_source": "onvif_events", "host": "10.0.0.6"}, loop, q, q)
        ing.events.start = lambda: None   # no ONVIF session in a test
        ing.start()
        assert not ing.meta.is_alive() and ing.status()["metadata_off"] is True
        assert ingest.metadata_since("cam6") is None   # no reader to ask
        ing.stop()
        ing2 = ingest.CameraIngest({**REOLINK, "host": "10.0.0.6"}, loop, q, q)
        assert ing2.meta_wanted and ing2.status()["metadata_missing"] is False
    finally:
        loop.close()


def test_ingest_registry_reports_reader_and_subscription_state():
    loop = asyncio.new_event_loop()
    try:
        q = asyncio.Queue()
        ing = ingest.CameraIngest({**REOLINK, "id": "cam9", "host": "10.0.0.6"}, loop, q, q)
        ing.meta.start = ing.events.start = lambda: None   # no threads, no camera
        assert ingest.metadata_since("cam9") is None and ingest.events_down_since("cam9") is None   # not running
        ing.start()
        assert ingest.metadata_since("cam9") == 0.0                      # not connected
        ing.meta.connected, ing.meta.connected_at = True, 123.0
        assert ingest.metadata_since("cam9") == 123.0
        assert ingest.events_down_since("cam9") == ing.events.down_since > 0   # never pulled yet
        assert ing.status()["onvif_events_down_since"] == ing.events.down_since
        ing.events.down_since = None
        assert ingest.events_down_since("cam9") is None
        ing.stop()
        assert ingest.metadata_since("cam9") is None and "cam9" not in ingest.RUNNING
    finally:
        loop.close()


def test_event_puller_failing_stays_down():
    p = ingest.EventPuller({**REOLINK, "host": "10.0.0.6"}, lambda e: None)
    first = p.down_since
    n = []

    def fail():
        n.append(1)
        if len(n) >= 3:
            p.stop_event.set()
        raise soap.OnvifError("401 Unauthorized")
    p._session = fail
    p.stop_event.wait = lambda t: p.stop_event.is_set()
    ingest.log.disabled = True
    try:
        p.run()
    finally:
        ingest.log.disabled = False
    assert p.down_since == first and not p.connected   # down since it started, not reset by each retry


def sample(bytes_, readers):
    return {"inbound_bytes": float(bytes_), "frames_in_error": 0.0, "state": "ready", "readers": readers}


def test_health_problem_only_for_metadata_cameras():
    h = StreamHealth()
    h.last_sample = 1.0
    now = time.time()
    for cid in ("cam6", "cam1"):
        p = h.paths.setdefault(cid, PathStats())
        for i in range(4):
            p.update(now - 40 + i * 10, sample(1000 * (i + 1), {}))   # flowing, no metadata reader attached
    tr, _, _ = tracker()
    h.event_only = lambda cid: tr.uses_rule_events(cid)[0]
    assert not any("metadata reader" in x for x in h.camera("cam6")["problems"]), h.camera("cam6")
    assert any("metadata reader" in x for x in h.camera("cam1")["problems"])
    h.event_only = lambda cid: 1 / 0   # a failing lookup never hides a problem
    assert any("metadata reader" in x for x in h.camera("cam6")["problems"])


def test_health_event_subscription_down_for_event_only_cameras():
    h = StreamHealth()
    h.last_sample = 1.0
    now = time.time()
    for cid in ("cam6", "cam1"):
        p = h.paths.setdefault(cid, PathStats())
        for i in range(4):
            p.update(now - 40 + i * 10, sample(1000 * (i + 1), {}))
    tr, _, _ = tracker()
    h.event_only = lambda cid: tr.uses_rule_events(cid)[0]
    down = {"cam6": now - 600, "cam1": now - 600}
    h.events_down_since = lambda cid: down.get(cid)
    probs = h.camera("cam6")["problems"]
    assert any("ONVIF event subscription down for 10 min" in x for x in probs), probs
    assert not any("ONVIF event" in x for x in h.camera("cam1")["problems"])   # metadata camera: its reader is what counts
    down["cam6"] = now - 60     # a short outage (reconnecting): not yet a problem
    assert not any("ONVIF event" in x for x in h.camera("cam6")["problems"])
    down["cam6"] = None         # working
    assert not h.camera("cam6")["problems"]
    h.events_down_since = lambda cid: 1 / 0
    assert not any("ONVIF event" in x for x in h.camera("cam6")["problems"])


def test_auto_mode_window_needs_a_connected_metadata_reader():
    cam = {**REOLINK, "id": "cam3", "streams": None}   # never probed
    now = time.time()

    def watched(reader):
        tr, _, _ = tracker([cam])
        tr.watch.first_heard["cam3"] = now - 4 * ruleevents.AUTO_WINDOW_S
        tr.watch.detect_at["cam3"] = now - 120
        tr.metadata_reader = lambda cid: reader
        return tr
    # the reader can't connect (a 401 for hours): no switch, so "metadata reader not attached" still shows
    tr = watched(0.0)
    on, why = tr.uses_rule_events("cam3", now)
    assert not on and "not connected" in why
    h = StreamHealth()
    h.last_sample = 1.0
    p = h.paths.setdefault("cam3", PathStats())
    for i in range(4):
        p.update(now - 40 + i * 10, sample(1000 * (i + 1), {}))
    h.event_only = lambda cid: tr.uses_rule_events(cid)[0]
    assert any("metadata reader not attached" in x for x in h.camera("cam3")["problems"])
    # connected a minute ago: the window counts from then
    assert not watched(now - 60).uses_rule_events("cam3", now)[0]
    # connected all along and genuinely no objects: ONVIF events
    assert watched(now - 2 * ruleevents.AUTO_WINDOW_S).uses_rule_events("cam3", now)[0]
    assert watched(None).uses_rule_events("cam3", now)[0]   # no reader known (as before)
    # Analytics=false still switches at once, whatever the reader does
    tr = watched(0.0)
    tr.camera = lambda cid: REOLINK
    assert tr.uses_rule_events("cam3", now)[0]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
