"""Parked vehicles, stuck open events and "Open events for" in the tracker (2026-10-19).

T-304's NW PTZ opened 644 rejected vehicle events in 6 h (parked cars, one a minute each: the track_max_seconds split
reopened them at once) and the desktop's Side Yard truck 142; T-304 also had 40 events stuck "open" for hours; cam6's
Reolink VehicleDetect opened 186 vehicle events on nothing. Here: a held vehicle track (stationary -> no reopen, moved
-> reopen, timeout -> reopen, new track id -> opens, people never held), the rule-event hold, the label-flip that left
events open, the orphan sweep and its start-up pass, and the per-camera labels for metadata tracks and ONVIF events.
No camera, MediaMTX, GPU or network.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_event_holds.py   (from backend/)
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="nvr-holds-test-"))
for k, sub in (("NVR_DATA_DIR", "data"), ("NVR_RECORDINGS_DIR", "recordings"), ("NVR_RUNTIME_DIR", "runtime")):
    os.environ[k] = str(TMP / sub)   # never the real server's database, recordings or mediamtx.yml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import ruleevents  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402
from nvr.ingest import DetectedObject, MetaFrame, RuleEvent  # noqa: E402
from nvr.tracker import Tracker, moved  # noqa: E402

for d in (settings.data_dir, settings.recordings_dir, settings.runtime_dir):
    v = str(d).replace("\\", "/").lower()
    assert "nvr-holds-test-" in v and "e:/nvr" not in v and "d:/nvr" not in v and "newvms/runtime" not in v, d

VEHICLE = "RuleEngine/MyRuleDetector/VehicleDetect"
PEOPLE = "RuleEngine/MyRuleDetector/PeopleDetect"
CELL = "RuleEngine/CellMotionDetector/Motion"
REOLINK = {"id": "cam6", "name": "REOPANO Back Driveway", "enabled": 1, "event_source": "auto", "motion_events": 0,
           "synopsis_labels": None, "zones": [], "streams": {"metadata_analytics": False}}
for _cid in ("cam1", "cam4", "cam6"):   # events reference a camera row
    db.upsert_camera({"id": _cid, "name": _cid, "host": "10.0.0.9", "zones": []})
PARKED = (0.40, 0.50, 0.60, 0.70)


class Closed(list):
    async def __call__(self, eid):
        self.append(eid)


def tracker(cams=None):
    closed = Closed()
    tr = Tracker(closed)
    rows = {c["id"]: c for c in (cams or [])}
    tr.camera = lambda cid: rows.get(cid)
    opened = []
    tr.on_opened = opened.append
    return tr, closed


def sweep(tr):
    asyncio.run(tr.sweep())


def frames(tr, cam, oid, cls, box, t0, n, step=1.0):
    """n metadata frames of one object, `step` s apart from t0 (camera time); returns the time of the last one."""
    for i in range(n):
        tr.on_frame(MetaFrame(camera_id=cam, ts=t0 + i * step, objects=[DetectedObject(object_id=oid, cls=cls, conf=0.8, box=box)]))
    return t0 + (n - 1) * step


def shift(box, dx):
    return (box[0] + dx, box[1], box[2] + dx, box[3])


def gone(tr):
    """Every track went quiet (> track_end_gap): the next sweep closes them."""
    for t in tr.tracks.values():
        t.last_wall = 0.0


def count(cam):
    return db.one("SELECT COUNT(*) AS n FROM events WHERE camera_id=?", [cam])["n"]


def rule(topic, state, at, cam="cam6"):
    data = {"Source": "000", "State": "true" if state else "false"}
    return RuleEvent(camera_id=cam, ts=at, topic=topic, rule=None, state=state, data=data, received=at)


# --------------------------------------------------------------------------- parked vehicle tracks


def test_moved_thresholds():
    assert not moved(PARKED, PARKED)
    assert not moved(PARKED, shift(PARKED, 0.01))                    # camera box jitter around a parked car
    assert moved(PARKED, shift(PARKED, settings.parked_track_move + 0.01))   # driving off
    assert moved((0.40, 0.50, 0.43, 0.53), (0.42, 0.50, 0.45, 0.53))  # a small box: overlap gone before 5 % of the frame


def test_split_vehicle_stays_put_no_new_event_until_it_moves():
    # T-304 cam4 / Side Yard: the 60 s split closed the parked car's event and the same track reopened one at once
    tr, closed = tracker()
    t = frames(tr, "cam4", "1", "car", PARKED, 1000.0, int(settings.track_max_seconds) + 3)
    first = tr.tracks[("cam4", "1")].event_id
    assert first is not None and count("cam4") == 1
    sweep(tr)   # too long: closed, the same car followed on, held where it is
    assert closed == [first] and ("cam4", "1") in tr.holds and tr.holds[("cam4", "1")].why == "split"
    t = frames(tr, "cam4", "1", "car", shift(PARKED, 0.01), t + 1, 120)   # two more minutes, parked (a little jitter)
    sweep(tr)
    assert tr.tracks[("cam4", "1")].event_id is None and count("cam4") == 1 and closed == [first]
    # it drives off: an event again, starting when it moved (not when it parked)
    t = frames(tr, "cam4", "1", "car", shift(PARKED, 0.2), t + 1, 3)
    nt = tr.tracks[("cam4", "1")]
    assert nt.event_id is not None and count("cam4") == 2 and ("cam4", "1") not in tr.holds
    assert db.event(nt.event_id)["start_ts"] >= t - 3


def test_rejected_track_stays_held_new_track_id_opens():
    # Side Yard: camera track 115067 (a parked truck) reopened after every rejection
    tr, closed = tracker()
    frames(tr, "cam1", "115067", "truck", PARKED, 2000.0, 5)
    eid = tr.tracks[("cam1", "115067")].event_id
    gone(tr)
    sweep(tr)
    assert closed == [eid]
    db.update_event(eid, status="rejected")
    tr.rejected(db.event(eid))   # what Pipeline._verify does on any rejection
    assert tr.holds[("cam1", "115067")].why == "rejected"
    frames(tr, "cam1", "115067", "truck", PARKED, 2010.0, 10)   # the same track id comes back to the same spot
    assert tr.tracks[("cam1", "115067")].event_id is None and count("cam1") == 1
    frames(tr, "cam1", "115068", "truck", PARKED, 2010.0, 3)    # a new track id: not held
    assert tr.tracks[("cam1", "115068")].event_id is not None and count("cam1") == 2


def test_rejected_track_moves_reopens():
    tr, closed = tracker()
    frames(tr, "cam1", "7", "car", PARKED, 3000.0, 5)
    eid = tr.tracks[("cam1", "7")].event_id
    gone(tr)
    sweep(tr)
    tr.rejected({**db.event(eid), "status": "rejected"})
    n = count("cam1")
    frames(tr, "cam1", "7", "car", PARKED, 3010.0, 5)
    assert count("cam1") == n
    frames(tr, "cam1", "7", "car", (0.40, 0.30, 0.60, 0.50), 3015.0, 3)   # pulled forward 20 % of the frame
    assert tr.tracks[("cam1", "7")].event_id is not None and count("cam1") == n + 1


def test_hold_times_out():
    tr, closed = tracker()
    frames(tr, "cam1", "9", "car", PARKED, 4000.0, 5)
    eid = tr.tracks[("cam1", "9")].event_id
    gone(tr)
    sweep(tr)
    tr.rejected({**db.event(eid), "status": "rejected"})
    n = count("cam1")
    frames(tr, "cam1", "9", "car", PARKED, 4010.0, 5)
    assert count("cam1") == n
    tr.holds[("cam1", "9")].since -= settings.parked_track_hold_s + 1   # half an hour later, still parked
    frames(tr, "cam1", "9", "car", PARKED, 4015.0, 3)
    assert tr.tracks[("cam1", "9")].event_id is not None and count("cam1") == n + 1
    assert ("cam1", "9") not in tr.holds


def test_people_are_never_held():
    tr, closed = tracker()
    frames(tr, "cam1", "50", "human", PARKED, 5000.0, int(settings.track_max_seconds) + 3)
    eid = tr.tracks[("cam1", "50")].event_id
    sweep(tr)   # a person standing still: split as before, the next event opens as before
    assert closed == [eid] and not tr.holds
    frames(tr, "cam1", "50", "human", PARKED, 5070.0, 3)
    assert tr.tracks[("cam1", "50")].event_id not in (None, eid)
    tr.rejected({**db.event(eid), "status": "rejected"})
    assert not tr.holds


def test_rule_event_vehicle_held_while_the_camera_stays_on():
    # a VehicleDetect stuck on: one rejected event, then no new one a minute while it stays on
    cam = {**REOLINK}
    tr, closed = tracker([cam])
    now = time.time()
    tr.on_rule_event(rule(VEHICLE, True, now - settings.track_max_seconds - 5))
    first = tr.tracks[("cam6", "onvif:vehicle")].event_id
    sweep(tr)   # still on, too long: split, the next event opens now (nothing rejected yet)
    second = tr.tracks[("cam6", "onvif:vehicle")].event_id
    assert closed == [first] and second not in (None, first)
    db.update_event(first, status="rejected")
    tr.rejected(db.event(first))
    t = tr.tracks[("cam6", "onvif:vehicle")]
    assert t.hold_since > 0
    t.first_ts = now - settings.track_max_seconds - 1
    sweep(tr)   # the second one closes; the camera still says vehicle: followed without a new event
    t = tr.tracks[("cam6", "onvif:vehicle")]
    assert closed == [first, second] and t.event_id is None and t.on
    t.first_ts = now - settings.track_max_seconds - 1
    sweep(tr)
    assert tr.tracks[("cam6", "onvif:vehicle")].event_id is None and count("cam6") == 2
    # the camera says it's gone, then fires again: a new detection, a new event
    tr.on_rule_event(rule(VEHICLE, False, now - 5))
    sweep(tr)
    assert ("cam6", "onvif:vehicle") not in tr.tracks
    tr.on_rule_event(rule(VEHICLE, True, time.time()))
    assert tr.tracks[("cam6", "onvif:vehicle")].event_id is not None and count("cam6") == 3
    # held again, and the hold runs out while it stays on: one event again
    third = tr.tracks[("cam6", "onvif:vehicle")].event_id
    db.update_event(third, status="rejected")
    tr.rejected(db.event(third))
    t = tr.tracks[("cam6", "onvif:vehicle")]
    t.first_ts = time.time() - settings.track_max_seconds - 1
    sweep(tr)
    t = tr.tracks[("cam6", "onvif:vehicle")]
    assert t.event_id is None and count("cam6") == 3
    t.hold_since -= settings.parked_track_hold_s + 1
    t.first_ts = time.time() - settings.track_max_seconds - 1
    sweep(tr)
    assert tr.tracks[("cam6", "onvif:vehicle")].event_id is not None and count("cam6") == 4
    tr.on_rule_event(rule(VEHICLE, False, time.time() - 5))
    sweep(tr)


# --------------------------------------------------------------------------- stuck open events


def test_object_id_reused_for_another_label_closes_the_old_event():
    # T-304's NW PTZ gives one object id to a car and then a person: the car's event used to stay "open" for ever
    tr, closed = tracker()
    frames(tr, "cam4", "0", "car", PARKED, 6000.0, 4)
    car = tr.tracks[("cam4", "0")].event_id
    frames(tr, "cam4", "0", "human", (0.1, 0.2, 0.2, 0.6), 6004.0, 3)
    person = tr.tracks[("cam4", "0")].event_id
    assert person not in (None, car)
    sweep(tr)
    e = db.event(car)
    assert closed == [car] and e["status"] == "pending" and e["end_ts"] == 6003.0
    assert db.event(person)["status"] == "open"   # the person's track is alive


def test_orphan_sweep_closes_open_events_no_track_owns():
    tr, closed = tracker()
    now = time.time()
    path = [[now - 400, *PARKED, 0.8], [now - 300, *PARKED, 0.8]]
    stale = db.create_event(camera_id="cam4", track_id="2", camera_class="vehicle", camera_conf=0.8, start_ts=now - 400,
                            path=path, status="open", created_at=now - 400)
    young = db.create_event(camera_id="cam4", track_id="3", camera_class="person", camera_conf=0.8, start_ts=now - 20,
                            path=[[now - 20, *PARKED, 0.8]], status="open", created_at=now - 20)
    ahead = db.create_event(camera_id="cam4", track_id="4", camera_class="person", camera_conf=0.8, start_ts=now + 3600,
                            path=[[now + 3600, *PARKED, 0.8]], status="open", created_at=now - 400)   # camera clock ahead
    frames(tr, "cam4", "5", "human", PARKED, now - 3, 4)   # a live track owns its event
    owned = tr.tracks[("cam4", "5")].event_id
    sweep(tr)
    assert db.event(stale)["status"] == "open"            # the first check waits a round (start-up does its own)
    tr._orphans_at = 0.0
    sweep(tr)
    e = db.event(stale)
    assert e["status"] == "pending" and abs(e["end_ts"] - (now - 300)) < 0.01 and stale in closed
    assert db.event(ahead)["status"] == "pending" and ahead in closed and db.event(ahead)["end_ts"] == now + 3600
    assert db.event(young)["status"] == "open" and young not in closed     # not quiet long enough yet
    assert db.event(owned)["status"] == "open" and owned not in closed


def test_start_up_closes_open_events_left_from_before():
    from nvr.pipeline import Pipeline
    now = time.time()
    path = [[now - 900, *PARKED, 0.8], [now - 880, *PARKED, 0.8]]
    left = db.create_event(camera_id="cam4", track_id="8", camera_class="person", camera_conf=0.8, start_ts=now - 900,
                           path=path, status="open", created_at=now - 900)
    recent = db.create_event(camera_id="cam4", track_id="9", camera_class="person", camera_conf=0.8, start_ts=now - 5,
                             path=[[now - 5, *PARKED, 0.8]], status="open", created_at=now - 5)
    p = Pipeline()
    p.recover()
    queued = []
    while not p.verify_q.empty():
        queued.append(p.verify_q.get_nowait())
    for eid, end in ((left, now - 880), (recent, now - 5)):   # no track survives a restart: all of them close
        e = db.event(eid)
        assert e["status"] == "pending" and abs(e["end_ts"] - end) < 0.01 and eid in queued, e
    assert not db.all("SELECT id FROM events WHERE status='open'")


# --------------------------------------------------------------------------- "Open events for"


def test_event_labels_normalized():
    assert ruleevents.event_labels(None) is None
    assert ruleevents.event_labels("person") is None                       # not a list: all (never silence a camera)
    assert ruleevents.event_labels(["vehicle", "person", "motion"]) is None  # all of them
    assert ruleevents.event_labels(["vehicle", "bogus", "person", "person"]) == ["person", "vehicle"]
    assert ruleevents.event_labels([]) == []
    assert ruleevents.opens("vehicle", None) and ruleevents.opens("vehicle", {"event_labels": None})
    assert not ruleevents.opens("vehicle", {"event_labels": ["person"]}) and ruleevents.opens("person", {"event_labels": ["person"]})
    assert ruleevents.opens("animal", {"event_labels": ["person"]})   # not covered by the choice: its own rule applies
    db.upsert_camera({"id": "cam9", "name": "cam9", "host": "10.0.0.9", "zones": [], "event_labels": ["vehicle", "bogus", "person"]})
    row = next(c for c in db.cameras() if c["id"] == "cam9")
    assert row["event_labels"] == ["person", "vehicle"]
    db.upsert_camera({"id": "cam9", "name": "cam9", "host": "10.0.0.9", "zones": [], "event_labels": ["person"]})
    assert next(c for c in db.cameras() if c["id"] == "cam9")["event_labels"] == ["person"]
    db.upsert_camera({"id": "cam9", "name": "cam9", "host": "10.0.0.9", "zones": []})   # not sent: kept
    assert next(c for c in db.cameras() if c["id"] == "cam9")["event_labels"] == ["person"]
    db.upsert_camera({"id": "cam9", "name": "cam9", "host": "10.0.0.9", "zones": [], "event_labels": ["person", "vehicle", "motion"]})
    assert next(c for c in db.cameras() if c["id"] == "cam9")["event_labels"] is None
    assert db.one("SELECT event_labels FROM cameras WHERE id='cam9'")["event_labels"] is None


def test_event_labels_api_validation():
    from pydantic import ValidationError
    from nvr import api, siteconfig
    ok = api.CameraIn(id="cam6", name="REOPANO", host="192.168.105.20", event_labels=["person"])
    assert ok.event_labels == ["person"] and api.CameraIn(id="cam6", name="x", host="10.0.0.1").event_labels is None
    for bad in (["bogus"], ["person", "vehicle", "motion", "person"], "person"):
        try:
            api.CameraIn(id="cam6", name="REOPANO", host="192.168.105.20", event_labels=bad)
        except ValidationError:
            continue
        raise AssertionError(f"accepted {bad!r}")
    # an older client that doesn't send it keeps what is stored; it is shown and exported like the other fields
    assert "event_labels" in api.CAMERA_KEPT_FIELDS and "event_labels" in api.PUBLIC_CAMERA_FIELDS
    assert "event_labels" in siteconfig.CAMERA_COLS


def test_metadata_tracks_follow_event_labels():
    cam = {"id": "cam1", "event_labels": ["person"]}
    tr, closed = tracker([cam])
    n = count("cam1")
    frames(tr, "cam1", "60", "car", PARKED, 7000.0, 5)
    assert ("cam1", "60") not in tr.tracks and count("cam1") == n       # vehicles open nothing here
    frames(tr, "cam1", "61", "human", (0.1, 0.2, 0.2, 0.6), 7000.0, 3)
    assert tr.tracks[("cam1", "61")].event_id is not None and count("cam1") == n + 1
    cam["event_labels"] = ["vehicle"]
    frames(tr, "cam1", "62", "truck", PARKED, 7100.0, 3)
    assert tr.tracks[("cam1", "62")].event_id is not None
    frames(tr, "cam1", "63", "person", (0.1, 0.2, 0.2, 0.6), 7100.0, 3)
    assert ("cam1", "63") not in tr.tracks


def test_rule_events_follow_event_labels():
    # the REOPANO camera set to people only: its VehicleDetect opens nothing, PeopleDetect as before
    cam = {**REOLINK, "event_labels": ["person"], "motion_events": 1}
    tr, closed = tracker([cam])
    n = count("cam6")
    now = time.time()
    tr.on_rule_event(rule(VEHICLE, True, now - 10))
    assert ("cam6", "onvif:vehicle") not in tr.tracks and count("cam6") == n
    tr.on_rule_event(RuleEvent(camera_id="cam6", ts=now - 9, topic=CELL, rule=None, state=True,
                               data={"IsMotion": "true", "Rule": "000"}, received=now - 9))
    assert ("cam6", "onvif:motion") not in tr.tracks and count("cam6") == n    # motion is left out too
    tr.on_rule_event(rule(PEOPLE, True, now - 8))
    assert tr.tracks[("cam6", "onvif:person")].event_id is not None and count("cam6") == n + 1
    tr.on_rule_event(rule(VEHICLE, False, now - 7))   # a false for a label that opened nothing: no harm
    tr.on_rule_event(rule(PEOPLE, False, now - 5))
    sweep(tr)
    assert not tr.tracks
    # motion back in the choice (and motion_events on): motion opens its event
    cam["event_labels"] = ["person", "motion"]
    tr.on_rule_event(RuleEvent(camera_id="cam6", ts=now - 4, topic=CELL, rule=None, state=True,
                               data={"IsMotion": "true", "Rule": "000"}, received=now - 4))
    assert tr.tracks[("cam6", "onvif:motion")].event_id is not None
    # a vehicle the camera names after the motion doesn't turn it into a vehicle event here
    tr.on_rule_event(rule(VEHICLE, True, now - 3))
    assert ("cam6", "onvif:vehicle") not in tr.tracks and tr.tracks[("cam6", "onvif:motion")].label == "motion"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
