"""People overlapping on one camera merge into one event per activity before Qwen describes it (merge.py groups), on a
throwaway DB: T-304 cam1's crew of 2026-10-20 15:37-15:45 (13 overlapping person events) as one event with "4 people",
the cap splitting a longer activity, separate visits, operator-touched events, zones, PTZ views, vehicles and PPE
violations staying apart, the pipeline holding descriptions (and the group's verification) while people are still
arriving, the AI schedule, and the one-off backfill tool (dry run by default).

Run: ..\\.venv\\Scripts\\python.exe tests\\test_group_merge.py   (from backend/)
"""
import asyncio
import datetime as dt
import io
import os
import re
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="nvr-group-test-")   # never the real DB, recordings or runtime config
os.environ["NVR_DATA_DIR"] = str(Path(_TMP, "data"))
os.environ["NVR_RECORDINGS_DIR"] = str(Path(_TMP, "recordings"))
os.environ["NVR_RUNTIME_DIR"] = str(Path(_TMP, "runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from nvr import aischedule, mediamtx, merge, merge_groups, vlmroute  # noqa: E402
from nvr import pipeline as pl  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402
from nvr.pipeline import Pipeline  # noqa: E402
from nvr.verifier import sample_path  # noqa: E402

mediamtx.recording_spans = lambda *a, **k: []
LEFT = [[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]]
RIGHT = [[0.5, 0.0], [1.0, 0.0], [1.0, 1.0], [0.5, 1.0]]


def camera(cid, zones=()):
    db.upsert_camera({"id": cid, "name": f"Cam {cid}", "host": "10.0.0.5", "onvif_port": 80, "rtsp_port": 554, "username": "u",
                      "password": "p", "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": list(zones),
                      "retention_days": None, "scene_notes": "", "retention_policy": None, "policies": []})


for c in ("w1", "w2", "w3", "z1", "v1", "p1", "h1", "h2", "s1", "ai", "syn"):
    camera(c)
camera("z1", [{"name": "Dock", "type": "include", "points": LEFT}, {"name": "Aisle", "type": "include", "points": RIGHT}])
camera("p1", [{"name": "Floor", "type": "ppe", "points": [[0, 0], [1, 0], [1, 1], [0, 1]], "required": ["hard_hat"]}])

# T-304 cam1 "Warehouse Walk Way", 2026-10-20 (event id, start, end) as reported: 13 events, 3-4 people at once
T304 = [(20107, "15:37:07", "15:41:47"), (20112, "15:37:44", "15:37:48"), (20113, "15:37:56", "15:38:07"),
        (20114, "15:37:58", "15:38:43"), (20122, "15:39:11", "15:43:04"), (20126, "15:40:04", "15:40:06"),
        (20138, "15:41:43", "15:42:25"), (20148, "15:42:58", "15:44:38"), (20154, "15:43:52", "15:44:41"),
        (20156, "15:43:59", "15:44:59"), (20158, "15:44:10", "15:44:41"), (20165, "15:44:56", "15:45:02"),
        (20166, "15:44:58", "15:45:04")]


def secs(hms):
    h, m, s = map(int, hms.split(":"))
    return h * 3600 + m * 60 + s


T304_OFF = [(i, secs(a) - secs(T304[0][1]), secs(b) - secs(T304[0][1])) for i, a, b in T304]   # seconds from 15:37:07


def path_for(start, end, x, n=6):
    """A person walking at x across the aisle (feet at y 0.65)."""
    return [[start + (end - start) * k / (n - 1), x - 0.04, 0.35, x + 0.04, 0.65, 0.9] for k in range(n)]


def make(cam, start, end, x=0.3, cls="person", status="verified", track=None, **kw):
    path = path_for(start, end, x)
    eid = db.create_event(camera_id=cam, track_id=track or f"t{start:.0f}{x}", camera_class=cls, camera_conf=0.9,
                          start_ts=start, end_ts=end, path=path, status=status, **kw)
    if status == "verified":
        db.update_event(eid, yolo_class=cls, yolo_conf=0.9, yolo_hits=3, detections=detections(path, cls))
    return eid


def detections(path, cls="person"):
    return {"samples": [{"ts": p[0], "cam_box": p[1:5], "yolo": [], "match": {"cls": cls, "conf": 0.9, "box": p[1:5]}, "iou": 1.0}
                        for p in sample_path(path, 6)], "keyframes": [], "needed": 3}


def t304(cam, base):
    """The T-304 events on `cam`, 15:37:07 at `base`; {real event id: id here}."""
    return {i: make(cam, base + a, base + b, x=0.1 + 0.06 * (k % 13)) for k, (i, a, b) in enumerate(T304_OFF)}


def alive(ids):
    return [i for i in ids if db.event(i)]


def merge_all(groups):
    for g in groups:
        for j, why in g["joins"]:
            merge.apply(db.event(g["target"]["id"]), db.event(j["id"]), why)


# ---------------------------------------------------------------- the plan

def test_t304_sequence_is_one_event_of_four_people():
    base = time.time() - 30 * 86400
    ids = t304("w1", base)
    groups = merge.plan_groups(merge.group_events("w1", base - 1, base + 1000), None)
    assert len(groups) == 1
    g = groups[0]
    assert g["target"]["id"] == ids[20107] and len(g["joins"]) == 12 and g["people"] == 4
    assert g["end"] - g["start"] == secs("15:45:04") - secs("15:37:07") == 477
    whys = {j["id"]: why for j, why in g["joins"]}
    assert whys[ids[20112]] == f"group: overlaps {ids[20107]} by 4 s"            # inside 20107
    assert whys[ids[20166]].startswith("group: overlaps ")
    merge_all(groups)
    assert alive(ids.values()) == [ids[20107]]
    e = db.event(ids[20107])
    assert e["status"] == "pending" and e["people"] == 4 and len(e["tracks"]) == 13
    assert (e["start_ts"], e["end_ts"]) == (base, base + 477) and e["synopsis"] is None
    assert [p[0] for p in e["path"]] == sorted(p[0] for p in e["path"]) and len(e["path"]) == 13 * 6
    assert {m["event"] for m in e["tracks"]} == set(ids.values())
    assert all(m.get("why") for m in e["tracks"] if m["event"] != ids[20107])
    assert merge.people_at_once(e["tracks"]) == 4


def test_cap_splits_a_longer_activity():
    base = time.time() - 29 * 86400
    ids = t304("w2", base)
    groups = merge.plan_groups(merge.group_events("w2", base - 1, base + 1000), None, cap=400)
    assert len(groups) == 2
    first, second = groups
    assert first["target"]["id"] == ids[20107] and second["target"]["id"] == ids[20148]
    assert {j["id"] for j, _ in first["joins"]} == {ids[i] for i in (20112, 20113, 20114, 20122, 20126, 20138)}
    assert {j["id"] for j, _ in second["joins"]} == {ids[i] for i in (20154, 20156, 20158, 20165, 20166)}
    assert all(g["end"] - g["start"] <= 400 for g in groups)
    # a whole day of people on a busy camera: one event per cap of activity, never a day-long one
    day = time.time() - 28 * 86400
    for k in range(0, 3600, 30):
        make("w3", day + k, day + k + 45, x=0.2 + (k % 7) * 0.1)
    groups = merge.plan_groups(merge.group_events("w3", day - 1, day + 4000), None)
    assert len(groups) == 7 and all(g["end"] - g["start"] <= settings.group_merge_max_s for g in groups)


def test_separate_visits_stay_separate():
    base = time.time() - 27 * 86400
    a = make("s1", base, base + 30)
    b = make("s1", base + 30 + settings.track_merge_gap + 20, base + 90)   # a real gap
    c = make("s1", base + 90 + settings.track_merge_gap - 2, base + 120)   # follows b within the gap: same activity
    assert merge.group_for(db.event(b), None) is None or merge.group_for(db.event(b), None)[0]["id"] == b
    target, joins = merge.group_for(db.event(c), None)
    assert target["id"] == b and [j["id"] for j, _ in joins] == [c] and joins[0][1] == "group: starts 8 s after %d ended" % b
    assert len(merge.plan_groups([db.event(a), db.event(b), db.event(c)], None)) == 2


def test_operator_touched_events_are_never_merged():
    base = time.time() - 26 * 86400
    a = make("s1", base, base + 60)
    b = make("s1", base + 20, base + 40)
    db.execute("INSERT INTO locks (camera_id, start_ts, end_ts, event_id, note, created_at) VALUES (?,?,?,?,?,?)",
               ["s1", base, base + 60, a, "", time.time()])
    assert merge.group_for(db.event(b), None) is None                   # A locked: B stays alone
    c = make("s1", base + 200, base + 260)
    d = make("s1", base + 210, base + 230)
    db.update_event(d, feedback={"verdict": "false_alarm"})
    assert merge.group_for(db.event(c), None) is None                   # B rated: neither joins
    assert merge.group_for(db.event(d), None) is None
    e = make("s1", base + 400, base + 460)
    f = make("s1", base + 410, base + 430)
    db.update_event(e, corrected_at=time.time())
    assert merge.group_for(db.event(f), None) is None and db.event(e)["status"] == "verified"


def test_zones_and_ptz_view_are_respected():
    zl = db.cameras()[[c["id"] for c in db.cameras()].index("z1")]["zones"]
    base = time.time() - 25 * 86400
    a = make("z1", base, base + 60, x=0.2)            # in the Dock (left)
    b = make("z1", base + 10, base + 50, x=0.8)       # at the same time in the Aisle (right): another activity
    c = make("z1", base + 20, base + 40, x=0.3)       # in the Dock with A
    assert merge.zone_set(db.event(a), zl) == {0} and merge.zone_set(db.event(b), zl) == {1}
    assert merge.group_for(db.event(b), zl) is None
    target, joins = merge.group_for(db.event(c), zl)
    assert target["id"] == a and [j["id"] for j, _ in joins] == [c]
    # PTZ: the camera turned away is another view (and the zones don't apply to it)
    d = make("s1", base, base + 60)
    e = make("s1", base + 10, base + 30, ptz_preset="away")
    assert merge.group_for(db.event(e), None) is None and merge.zone_set(db.event(e), zl) is None
    f = make("s1", base + 15, base + 25)
    assert merge.group_for(db.event(f), None)[0]["id"] == d
    # two groups side by side in different zones, both waiting to be verified again, don't wait for each other
    now = time.time()
    two = [{"event": 1, "track": "a", "from": now - 60, "to": now - 30}, {"event": 2, "track": "b", "from": now - 50, "to": now - 20}]
    g1 = make("z1", now - 60, now - 20, x=0.2, status="pending", tracks=two)
    g2 = make("z1", now - 55, now - 25, x=0.8, status="pending", tracks=two)
    assert not merge.group_waiting(db.event(g1), now, zl) and not merge.group_waiting(db.event(g2), now, zl)
    g3 = make("z1", now - 50, now - 30, x=0.3, status="pending")   # being verified in the same zone: worth waiting for
    assert merge.group_waiting(db.event(g1), now, zl) and not merge.group_waiting(db.event(g2), now, zl)
    db.update_event(g3, status="rejected")


def test_a_vehicle_passing_is_not_merged_into_the_people():
    base = time.time() - 24 * 86400
    a = make("v1", base, base + 120)
    v = make("v1", base + 30, base + 50, cls="vehicle")
    b = make("v1", base + 60, base + 80)
    assert merge.group_for(db.event(v), None) is None and not merge.group_ok(db.event(v))
    target, joins = merge.group_for(db.event(b), None)
    assert target["id"] == a and [j["id"] for j, _ in joins] == [b]
    w = make("v1", base + 35, base + 55, cls="vehicle")    # two vehicles at once stay two events too
    assert merge.group_for(db.event(w), None) is None


def test_people_at_once_counts_handoffs_once():
    m = lambda f, t: {"event": 0, "track": "x", "from": f, "to": t}  # noqa: E731
    assert merge.people_at_once([m(0, 10), m(8, 20)]) == 1            # the camera re-found the same person
    assert merge.people_at_once([m(0, 10), m(5, 20)]) == 2
    assert merge.people_at_once([m(0, 100), m(10, 12), m(50, 51)]) == 2   # short tracks inside still count
    assert merge.people_at_once([m(0, 10), m(20, 30)]) == 1


def test_description_prompt_and_search_say_a_group():
    from nvr import synopsis as vlm
    base = time.time() - 23 * 86400
    a = make("s1", base, base + 100)
    b = make("s1", base + 10, base + 60, x=0.6)
    merge.apply(db.event(a), db.event(b), "group: overlaps 1 by 50 s")
    e = db.event(a)
    prompt = vlm.build_prompt(e, {"name": "Aisle"}, [])
    assert "a group of 2 people were in view at the same time (2 camera tracks" in prompt
    assert "movement across the frame: several people" in prompt
    one = vlm.build_prompt(db.event(make("s1", base + 500, base + 510)), {"name": "Aisle"}, [])
    assert "group of" not in one


# ---------------------------------------------------------------- the pipeline (live)

class FakeVerifier:
    model = "M"

    def __init__(self):
        self.calls = []

    def verify(self, e, clip, clip_start, zl):
        self.calls.append(e["id"])
        v = np.random.default_rng(e["id"]).normal(size=512)   # everyone looks different: no re-ID fragment merges
        return {"status": "verified", "yolo_class": "person", "yolo_conf": 0.9, "yolo_hits": 3, "reid": list(v / np.linalg.norm(v)),
                "detections": detections(e["path"]), "clip_start": clip_start}


def live_pipeline():
    p = Pipeline()
    p.verifier = FakeVerifier()
    p.cameras = {c["id"]: c for c in db.cameras()}
    p.vlm_ready = True
    return p


class Live:
    """Clip fetches, the recording lag and the post-roll out of the way, so _verify runs at once."""
    def __enter__(self):
        self.old = (pl.fetch_clip, settings.recording_lag, settings.clip_post_roll)

        async def no_fetch(*a, **k):
            return None
        pl.fetch_clip = no_fetch
        settings.recording_lag, settings.clip_post_roll = -1e12, 0
        return self

    def __exit__(self, *exc):
        pl.fetch_clip, settings.recording_lag, settings.clip_post_roll = self.old


def drain(p):
    """Verify everything queued, as the verify worker would."""
    async def go():
        while not p.verify_q.empty():
            await p._verify(p.verify_q.get_nowait())
    asyncio.run(go())


def test_live_t304_holds_merges_and_describes_once():
    """The crew as the pipeline sees it: each track opens an event, closes, is verified; descriptions and the group's
    verification wait while people are still being tracked; one event of 4 people is described once."""
    base = time.time() - 490          # ends 5 s ago, so "now" is inside the activity's cap
    p = live_pipeline()
    ids: dict[int, int] = {}
    order = sorted(T304_OFF, key=lambda r: (r[2], r[0]))
    with Live():
        for rid, a, b in order:
            for oid, oa, ob in T304_OFF:   # tracks started by now are open events
                if oid not in ids and oa <= b:
                    ids[oid] = db.create_event(camera_id="h1", track_id=f"tr{oid}", camera_class="person", camera_conf=0.9,
                                               start_ts=base + oa, path=path_for(base + oa, base + ob, 0.1 + 0.06 * (oid % 13)),
                                               status="open")
            db.update_event(ids[rid], end_ts=base + b, status="pending")
            p.verify_q.put_nowait(ids[rid])
            drain(p)
            if rid != order[-1][0]:
                p.hold_tick(time.time())
                drain(p)
                assert p.synopsis_q.empty() and not p.synopsis_pending, f"described before the activity ended (after {rid})"
    left = alive(ids.values())
    assert left == [ids[20107]]
    e = db.event(ids[20107])
    assert e["status"] == "verified" and e["people"] == 4 and len(e["tracks"]) == 13
    assert db.get_reid(e["id"]) is None                       # several people: no one fingerprint
    assert p.verifier.calls.count(ids[20107]) <= 2            # verified once alone, once as the whole group
    assert p.synopsis_hold and not p.verify_hold
    p.hold_tick(time.time())
    assert [p.synopsis_q.get_nowait()[2] for _ in range(p.synopsis_q.qsize())] == [ids[20107]]


def test_description_is_not_held_for_ever():
    now = time.time()
    p = live_pipeline()
    a = make("h2", now - 100, now - 80)
    o = db.create_event(camera_id="h2", track_id="still-there", camera_class="person", camera_conf=0.9, start_ts=now - 90,
                        path=path_for(now - 90, now - 89, 0.5), status="open")
    p.synopsis_hold[a] = now - 1
    p.hold_tick(now)
    assert a in p.synopsis_hold and p.synopsis_q.empty()     # someone overlapping it is still being tracked
    p.hold_tick(now - 100 + settings.group_merge_max_s + 1)
    assert a not in p.synopsis_hold and p.synopsis_q.get_nowait()[2] == a   # the activity reached the cap
    b = make("h2", now - 70, now - 60)
    p.synopsis_hold[b] = now - 1
    p.hold_tick(now)
    assert b in p.synopsis_hold                                # the person is still there...
    db.update_event(o, status="rejected", end_ts=now - 75)
    p.hold_tick(now + p.HOLD_STEP_S)
    assert p.synopsis_q.get_nowait()[2] == b                   # ...and gone: the activity ended, described


def test_ppe_violation_keeps_its_own_event():
    base = time.time() - 200
    p = live_pipeline()
    a = make("p1", base, base + 100)
    b = make("p1", base + 20, base + 40, status="pending")

    async def fake_ppe(event_id):
        e = db.event(event_id)
        db.update_event(event_id, detections={**(e.get("detections") or {}), "ppe": {"verdict": "violation", "violation": ["hard_hat"]}})
    p.check_ppe = fake_ppe
    with Live():
        p.verify_q.put_nowait(b)
        drain(p)
    assert db.event(b) and db.event(b)["status"] == "verified" and db.event(a)["status"] == "verified"
    c = make("p1", base + 50, base + 60, status="pending")

    async def compliant(event_id):
        e = db.event(event_id)
        db.update_event(event_id, detections={**(e.get("detections") or {}), "ppe": {"verdict": "compliant", "violation": []}})
    p.check_ppe = compliant
    with Live():
        p.verify_q.put_nowait(c)
        drain(p)
    assert db.event(c) is None and db.event(a)["people"] == 2   # compliant: joins the group


def test_ai_schedule_outside_hours_defers_the_merged_event_once():
    base = time.time() - 300
    p = live_pipeline()
    aischedule.apply({"mode": "soc", "outside": "catchup", "monitored": False, "timezone": "UTC"})
    try:
        vlmroute.router.models["primary"].queue = vlmroute.router.models["fallback"].queue = 0
        a = make("ai", base, base + 60)
        p.queue_synopsis(a)

        async def worker():
            task = asyncio.create_task(p._synopsis_worker())
            await asyncio.sleep(0.2)
            task.cancel()
        asyncio.run(worker())
        assert a in p.synopsis_deferred                                  # outside the hours: waits for catch-up
        b = make("ai", base + 30, base + 50, status="pending")
        with Live():
            p.verify_q.put_nowait(b)
            drain(p)
        assert db.event(b) is None and a not in p.synopsis_deferred    # merged: the old deferral is gone
        e = db.event(a)
        assert e["status"] == "verified" and e["people"] == 2 and a in p.synopsis_hold
        p.hold_tick(time.time() + 60)
        asyncio.run(worker())
        assert a in p.synopsis_deferred and p.synopsis_q.empty()        # deferred again, as one event
    finally:
        aischedule.apply(None)


def test_synopsis_written_for_a_merged_part_is_dropped():
    base = time.time() - 400
    p = live_pipeline()
    a = make("syn", base, base + 60)
    b = make("syn", base + 10, base + 30)
    from nvr import synopsis as vlm, weaponcheck
    old = (vlm.synopsis, weaponcheck.review)
    d = Path(settings.data_dir, "events", str(a))
    d.mkdir(parents=True, exist_ok=True)
    (d / "wide.jpg").write_bytes(b"jpg")
    db.update_event(a, detections={**db.event(a)["detections"], "keyframes": [{"file": "wide.jpg", "ts": base, "kind": "wide"}]})

    async def slow(event, camera, images, examples):
        merge.apply(db.event(a), db.event(b), "group: test")             # merged while Qwen was writing
        return {"summary": "A person walks.", "threat_level": "none"}

    async def review(e, result, evidence):
        return result, None
    vlm.synopsis, weaponcheck.review = slow, review
    try:
        asyncio.run(p._synopsis(a))
    finally:
        vlm.synopsis, weaponcheck.review = old
    assert db.event(a)["synopsis"] is None and db.event(a)["status"] == "pending"


# ---------------------------------------------------------------- the backfill tool

def snapshot_rows(ids):
    return [(r["id"], r["status"], r["end_ts"], r["synopsis"], r["tracks"])
            for r in db.all(f"SELECT * FROM events WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id", list(ids))]


def test_backfill_dry_run_changes_nothing_and_apply_merges():
    day = dt.datetime.now().replace(hour=15, minute=37, second=7, microsecond=0) - dt.timedelta(days=3)
    base = day.timestamp()
    camera("bf")
    ids = t304("bf", base)
    for i in ids.values():
        db.update_event(i, synopsis=f"described {i}")
    locked = make("bf", base + 600, base + 700)
    near = make("bf", base + 620, base + 640)
    db.execute("INSERT INTO locks (camera_id, start_ts, end_ts, event_id, note, created_at) VALUES (?,?,?,?,?,?)",
               ["bf", base + 600, base + 700, locked, "", time.time()])
    masked = make("bf", base + 100, base + 120, status="masked")
    before = snapshot_rows([*ids.values(), locked, near, masked])
    since = day.strftime("%Y-%m-%d")
    out = io.StringIO()
    with redirect_stdout(out):
        assert merge_groups.main(["--since", since, "--camera", "bf"]) == 0          # dry run is the default
        assert merge_groups.main(["--since", since, "--camera", "bf", "--dry-run"]) == 0
    text = out.getvalue()
    assert "dry run: nothing changed" in text and f"  {ids[20107]}  15:37:07-15:45:04 (477 s), 4 people at once, 13 events" in text
    assert not re.search(rf"({locked}|{near}|{masked})", text)
    assert snapshot_rows([*ids.values(), locked, near, masked]) == before and db.get_setting(merge.REVERIFY_KEY) is None
    with redirect_stdout(io.StringIO()):
        assert merge_groups.main(["--since", since, "--camera", "bf", "--apply"]) == 0
    assert alive(ids.values()) == [ids[20107]]
    e = db.event(ids[20107])
    assert e["status"] == "pending" and e["people"] == 4 and e["synopsis"] is None
    assert db.event(locked)["status"] == "verified" and db.event(near) and db.event(masked)["status"] == "masked"
    # the running NVR picks the request up and verifies the group again (then holds and describes it as usual)
    p = live_pipeline()
    assert db.get_setting(merge.REVERIFY_KEY) == [ids[20107]]
    p.hold_tick(time.time())
    assert db.get_setting(merge.REVERIFY_KEY) is None and p.verify_q.get_nowait() == ids[20107]
    # run again: nothing left to merge
    out = io.StringIO()
    with redirect_stdout(out):
        merge_groups.main(["--since", since, "--camera", "bf", "--apply"])
    assert "merged 0 events into 0" in out.getvalue()


def test_reverify_requests_survive_a_concurrent_take():
    merge.request_reverify([1, 2])
    row = db.one("SELECT value FROM settings WHERE key=?", [merge.REVERIFY_KEY])
    merge.request_reverify([3])                        # the tool adds one between the NVR's read and its delete
    cur = db.execute("DELETE FROM settings WHERE key=? AND value=?", [merge.REVERIFY_KEY, row["value"]])
    assert cur.rowcount == 0 and merge.take_reverify_requests() == [1, 2, 3] and merge.take_reverify_requests() == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
