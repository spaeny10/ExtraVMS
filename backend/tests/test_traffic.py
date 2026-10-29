"""Traffic cameras (traffic.py, traffic_api.py) without a camera: the push as the TS4441 sends it (a real read from the
2026-10-28 capture, tests/fixtures/milesight_ts4441, with a fake plate, scrubbed serial / MAC / licence and generated
pictures), the time zone learned from the push, passes, plates on events, the plate lists and their alarms, search and
counts, health, retention, the hub roles, and the camera's setup through its own API (a fake Milesight answering with the
camera's real get.* answers).

Throwaway data, recordings and runtime dirs. Run: ..\\.venv\\Scripts\\python.exe tests\\test_traffic.py   (from backend/)
"""
import asyncio
import base64
import copy
import datetime as dt
import json
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="nvr-traffic-test-"))
for k, sub in (("NVR_DATA_DIR", "data"), ("NVR_RECORDINGS_DIR", "recordings"), ("NVR_RUNTIME_DIR", "runtime")):
    os.environ[k] = str(TMP / sub)   # never the real server's database, recordings or mediamtx.yml
os.environ["NVR_ALLOWED_HOSTS"] = "site"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tunnelproto"))

import cv2  # noqa: E402
import httpx  # noqa: E402
import numpy as np  # noqa: E402

from nvr import api, hub_agent, milesight, policy, traffic, traffic_api  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402

for d in (settings.data_dir, settings.recordings_dir, settings.runtime_dir):
    v = str(d).replace("\\", "/").lower()
    assert "nvr-traffic-test-" in v and "e:/nvr" not in v and "d:/nvr" not in v, d
    d.mkdir(parents=True, exist_ok=True)

FIX = Path(__file__).resolve().parent / "fixtures" / "milesight_ts4441"
LAN = ("192.0.2.62", 5000)
SECRET = "s3cret-test-token"
CENTRAL = -5 * 3600   # CDT


def jpg(w=64, h=32, v=128) -> str:
    ok, b = cv2.imencode(".jpg", np.full((h, w, 3), v, np.uint8))
    return base64.b64encode(b.tobytes()).decode()


def push_read(plate="ABC1234", conf="0.89", at: float | None = None, **over) -> dict:
    p = json.loads((FIX / "push_read.json").read_text())
    at = at or time.time()
    local = dt.datetime.fromtimestamp(at, dt.timezone.utc) + dt.timedelta(seconds=CENTRAL)
    p.update({"license_plate": plate, "plate_confidence": conf, "time": local.strftime("%Y-%m-%d %H:%M:%S.%f")[:23],
              "license_plate_snapshot": jpg(40, 12) if plate != "No Plates" else "-", "vehicle_snapshot": jpg(120, 90),
              "full_snapshot": jpg(320, 180)})
    p.update(over)
    return p


def cam(cid="cam8", enabled=True):
    db.upsert_camera({"id": cid, "name": "Traffic Camera", "host": "192.0.2.62", "onvif_port": 80, "rtsp_port": 554,
                      "username": "u", "password": "p", "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": []})
    traffic.set_traffic(cid, enabled=enabled, secret=SECRET, set_at=time.time())
    return traffic.camera(cid)


def call(method, path, role=None, json_body=None, content=None, headers=None):
    async def go():
        tunnel = role is not None
        transport = httpx.ASGITransport(app=hub_agent.as_tunnel(api.app) if tunnel else api.app,
                                        client=hub_agent.IN_PROCESS_CLIENT if tunnel else LAN)
        h = {"x-hub-role": role, "x-hub-user": "op@example"} if tunnel else {}
        async with httpx.AsyncClient(transport=transport, base_url="http://site", timeout=30) as c:
            return await c.request(method, path, headers={**h, **(headers or {})}, json=json_body, content=content)
    return asyncio.run(go())


def reset():
    db.execute("DELETE FROM vehicle_reads")
    db.execute("DELETE FROM plate_lists")
    db.execute("DELETE FROM events")
    db.execute("DELETE FROM recording_offsets")
    db.execute("DELETE FROM plate_days")
    traffic.passes.recent.clear()


# ---------------------------------------------------------------- parsing

def test_parse_the_real_push():
    received = 1793222884.437 + 0.6
    p = json.loads((FIX / "push_read.json").read_text())   # "2026-10-28 16:28:04.437" Central
    r = traffic.parse(p, received)
    assert r["plate"] == "ABC1234" and r["confidence"] == 0.89 and r["region"] == "KS" and r["direction"] == "away"
    assert r["vehicle_type"] == "Truck" and r["vehicle_color"] == "White" and r["kind"] == "regular"
    assert r["zone_offset"] == CENTRAL and r["clock_ok"] and abs(r["ts"] - 1793222884.437) < 0.01
    l, t, rr, b = r["box"]
    assert 0 <= l < rr <= 1 and 0 <= t < b <= 1 and r["box"] == [0.4281, 0.1519, 0.9688, 0.7741]
    pl, pt, pr, pb = r["plate_box"]   # the plate inside the vehicle
    assert l < pl < pr < rr and t < pt < pb < b
    assert "full_snapshot" not in r["extra"] and r["extra"]["device_name"] == "Traffic Sensing PTZ"
    none = traffic.parse({**p, "license_plate": "No Plates", "plate_confidence": "0.00", "country_region": "-"}, received)
    assert none["plate"] is None and none["confidence"] is None and none["region"] is None
    assert traffic.parse({**p, "direction": "-"}, received)["direction"] is None


def test_time_zone_learned_resent_and_wrong_clock():
    now = 1793222884.0
    local = dt.datetime.fromtimestamp(now + CENTRAL, dt.timezone.utc).replace(tzinfo=None)
    ts, off, ok = traffic.read_time(local, now + 0.8, None)
    assert off == CENTRAL and ok and abs(ts - now) < 1e-6
    # resent two hours later from the SD card: the known offset, its own time
    ts, off, ok = traffic.read_time(local, now + 7200, CENTRAL, resend=True)
    assert ok and off == CENTRAL and abs(ts - now) < 1e-6
    # a camera that can't resend: two hours off is its zone moving (Central -> Pacific-like), learned again
    ts, off, ok = traffic.read_time(local, now + 7200, CENTRAL)
    assert ok and off == CENTRAL - 7200 and abs(ts - (now + 7200)) < 1e-6
    # Pacific -> Central (cam8 on 2026-10-28): a fresh read two hours "ahead" of the known offset is never a resend
    ts, off, ok = traffic.read_time(local, now + 0.5, CENTRAL - 7200, resend=True)
    assert ok and off == CENTRAL and abs(ts - now) < 1e-6
    # a clock 20 minutes off with no offset known: the receive time
    ts, off, ok = traffic.read_time(local + dt.timedelta(minutes=20), now, None)
    assert not ok and ts == now
    assert traffic.read_time(None, now, CENTRAL) == (now, CENTRAL, False)


def test_near_and_common():
    assert traffic.near("ABC1234", "ABC1284") and traffic.near("3671AED", "671AED") and traffic.near("ABC123", "ABC1234")
    assert not traffic.near("ABC12", "XBC13") and not traffic.near("AB1", "AB2")   # short plates: exact only
    assert traffic.common("671AED", "3671AED") == 6 and traffic.common("1935AFR", "9535AFL") >= 4


# ---------------------------------------------------------------- the push

def test_push_stores_the_read_and_its_crops():
    reset()
    cam()
    r = call("POST", f"/api/traffic/push/cam8/{SECRET}", json_body=push_read())
    assert r.status_code == 200, r.text
    row = db.one("SELECT * FROM vehicle_reads")
    assert row["plate"] == "ABC1234" and row["best"] == 1 and row["pass_id"] == row["id"]
    imgs = json.loads(row["images"])
    assert set(imgs) == {"plate", "vehicle"}   # the full snapshot only for a list hit
    assert all((settings.data_dir / f).is_file() for f in imgs.values())
    assert traffic.camera("cam8")["traffic"]["zone_offset_s"] == CENTRAL
    got = call("GET", f"/api/traffic/reads/{row['id']}/image/vehicle")
    assert got.status_code == 200 and got.content[:2] == b"\xff\xd8"
    assert call("GET", f"/api/traffic/reads/{row['id']}/image/full").status_code == 404
    # the status report counts as alive, stores nothing
    assert call("POST", f"/api/traffic/push/cam8/{SECRET}", json_body={"device_name": "Traffic Sensing PTZ"}).status_code == 200
    assert db.one("SELECT COUNT(*) n FROM vehicle_reads")["n"] == 1 and traffic.health.camera("cam8").get("status")


def test_push_refusals():
    reset()
    cam()
    assert call("POST", "/api/traffic/push/cam8/wrong", json_body=push_read()).status_code == 404
    assert call("POST", f"/api/traffic/push/nocam/{SECRET}", json_body=push_read()).status_code == 404
    assert call("POST", f"/api/traffic/push/cam8/{SECRET}", content=b"not json",
                headers={"content-type": "application/json"}).status_code == 400
    assert call("POST", f"/api/traffic/push/cam8/{SECRET}", role="owner", json_body=push_read()).status_code == 403
    assert db.one("SELECT COUNT(*) n FROM vehicle_reads")["n"] == 0


def test_passes_keep_the_best_read():
    reset()
    c = cam()
    t0 = time.time()
    a = traffic.ingest(c, push_read("671AED", "0.62", at=t0), t0 + 0.5)
    b = traffic.ingest(c, push_read("3671AED", "0.68", at=t0 + 0.4), t0 + 0.9)
    other = traffic.ingest(c, push_read("XYZ9876", "0.70", at=t0 + 1), t0 + 1.5)
    later = traffic.ingest(c, push_read("671AED", "0.90", at=t0 + 40), t0 + 40.5)   # past PARKED_GAP_S: a new pass
    ra, rb = db.one("SELECT * FROM vehicle_reads WHERE id=?", [a["read_id"]]), db.one("SELECT * FROM vehicle_reads WHERE id=?", [b["read_id"]])
    assert ra["pass_id"] == rb["pass_id"] == a["read_id"] and ra["best"] == 0 and rb["best"] == 1
    assert db.one("SELECT pass_id FROM vehicle_reads WHERE id=?", [other["read_id"]])["pass_id"] == other["read_id"]
    assert db.one("SELECT pass_id FROM vehicle_reads WHERE id=?", [later["read_id"]])["pass_id"] == later["read_id"]
    best = traffic.search(camera="cam8")["reads"]
    assert {r["plate"] for r in best} == {"3671AED", "XYZ9876", "671AED"} and len(best) == 3
    assert len(traffic.search(camera="cam8", all_reads=True)["reads"]) == 4
    one = call("GET", f"/api/traffic/reads/{b['read_id']}").json()
    assert [x["plate"] for x in one["pass"]] == ["671AED", "3671AED"]


def test_a_parked_car_is_one_vehicle():
    """cam8 aimed at a car lot: one blue car re-read every second is one pass; a car passing the same spot later,
    another colour in that spot, or a moving car of the same look are not."""
    reset()
    c = cam()
    t0 = time.time() - 120
    lot = {"vehicle_tracking_box_x1": "474", "vehicle_tracking_box_y1": "582", "vehicle_tracking_box_x2": "604",
           "vehicle_tracking_box_y2": "638", "vehicle_type": "Car", "vehicle_color": "Blue", "direction": "-"}
    ids = [traffic.ingest(c, push_read("No Plates", "0", at=t0 + i * 1.5, **lot), t0 + i * 1.5 + 0.3)["read_id"] for i in range(8)]
    rows = db.all(f"SELECT pass_id, best FROM vehicle_reads WHERE id IN ({','.join(map(str, ids))}) ORDER BY id")
    assert {r["pass_id"] for r in rows} == {ids[0]} and [r["best"] for r in rows] == [1] + [0] * 7
    jiggle = {**lot, "vehicle_tracking_box_x1": "476", "vehicle_tracking_box_x2": "605"}   # the camera's box wobbles
    j = traffic.ingest(c, push_read("No Plates", "0", at=t0 + 13, **jiggle), t0 + 13.3)
    assert db.one("SELECT pass_id FROM vehicle_reads WHERE id=?", [j["read_id"]])["pass_id"] == ids[0]
    red = traffic.ingest(c, push_read("No Plates", "0", at=t0 + 14, **{**lot, "vehicle_color": "Red"}), t0 + 14.3)
    assert db.one("SELECT pass_id FROM vehicle_reads WHERE id=?", [red["read_id"]])["pass_id"] == red["read_id"]
    moved = {**lot, "vehicle_tracking_box_x1": "700", "vehicle_tracking_box_x2": "830"}   # a blue car further along
    m = traffic.ingest(c, push_read("No Plates", "0", at=t0 + 15, **moved), t0 + 15.3)
    assert db.one("SELECT pass_id FROM vehicle_reads WHERE id=?", [m["read_id"]])["pass_id"] == m["read_id"]
    later = traffic.ingest(c, push_read("No Plates", "0", at=t0 + 60, **lot), t0 + 60.3)   # gone, then another blue car there
    assert db.one("SELECT pass_id FROM vehicle_reads WHERE id=?", [later["read_id"]])["pass_id"] == later["read_id"]
    # its plate read later in the same spot: the pass's best becomes the plate read
    p = traffic.ingest(c, push_read("ABC1234", "0.8", at=t0 + 61, **lot), t0 + 61.3)
    assert db.one("SELECT pass_id, best FROM vehicle_reads WHERE id=?", [p["read_id"]]) == {"pass_id": later["read_id"], "best": 1}
    assert db.one("SELECT best FROM vehicle_reads WHERE id=?", [later["read_id"]])["best"] == 0
    total = sum(b["total"] for b in traffic.counts(camera="cam8", since=t0 - 10)["buckets"])
    assert total == 4   # the parked car, the red car, the moving car, the later car


# ---------------------------------------------------------------- events

def vehicle_event(at, **f):
    return db.create_event(camera_id="cam8", track_id=f"t{at}", camera_class="vehicle", camera_conf=0.9, start_ts=at - 2,
                           end_ts=at + 3, path=[], rules=[], status="verified", **f)


def test_plate_goes_on_the_event_of_that_moment():
    reset()
    c = cam()
    t0 = time.time()
    eid = vehicle_event(t0)
    low = traffic.ingest(c, push_read("LOW1234", "0.40", at=t0), t0 + 0.5)
    assert low["event_id"] is None and db.event(eid)["plate"] is None
    out = traffic.ingest(c, push_read("ABC1234", "0.85", at=t0 + 1), t0 + 1.5)
    e = db.event(eid)
    assert out["event_id"] == eid and e["plate"] == "ABC1234" and e["plate_conf"] == "high" and e["plate_region"] == "KS"
    assert e["detections"]["plate_read"]["source"] == "camera"
    # a person's own read stays
    db.update_event(eid, plate="MINE1", detections={"plate_read": {"source": "user", "result": "read"}})
    traffic.ingest(c, push_read("ZZZ9999", "0.95", at=t0 + 2), t0 + 2.5)
    assert db.event(eid)["plate"] == "MINE1"
    # an event verified after the read: attach_to_event (the pipeline calls it) finds the read in its span
    e2 = vehicle_event(t0 + 100)
    db.update_event(e2, status="pending")
    traffic.ingest(c, push_read("LATE123", "0.75", at=t0 + 100), t0 + 100.5)
    db.update_event(e2, status="verified")
    assert traffic.attach_to_event(e2)["plate"] == "LATE123" and traffic.reads_plates("cam8")


# ---------------------------------------------------------------- lists

def test_deny_list_opens_an_alarm_once():
    reset()
    c = cam()
    t0 = time.time()
    r = call("POST", "/api/traffic/lists", role="operator", json_body={"plate": "abc-1234", "list": "deny", "note": "trespassed 10/12"})
    assert r.status_code == 200 and r.json()["plate"] == "ABC1234"
    out = traffic.ingest(c, push_read("ABC1234", "0.71", at=t0), t0 + 0.5)
    e = db.event(out["alarm_id"])
    assert e["status"] == "verified" and e["priority"] == "high" and e["plate"] == "ABC1234"
    assert e["policy"]["kind"] == "plate_list" and "deny list" in e["policy"]["text"] and "trespassed" in e["policy"]["text"]
    assert e["track_id"].startswith("lpr:") and (settings.data_dir / e["snapshot"]).is_file()
    assert set(json.loads(db.one("SELECT images FROM vehicle_reads WHERE id=?", [out["read_id"]])["images"])) == {"plate", "vehicle", "full"}
    # the Qwen description's site-rule check keeps the plate rule
    assert policy.check(out["alarm_id"])["kind"] == "plate_list" and db.event(out["alarm_id"])["policy"]["kind"] == "plate_list"
    again = traffic.ingest(c, push_read("ABC1234", "0.80", at=t0 + 60), t0 + 60.5)
    assert again["alarm_id"] == out["alarm_id"]
    assert db.one("SELECT COUNT(*) n FROM events WHERE track_id LIKE 'lpr:%'")["n"] == 1
    lists = call("GET", "/api/traffic/lists").json()
    assert lists[0]["last_seen"] and lists[0]["last_camera"] == "cam8"


def test_near_watch_allow_and_low_confidence():
    reset()
    c = cam()
    t0 = time.time()
    traffic.add_entry("WAT5678", "watch")
    traffic.add_entry("OK11111", "allow")
    near = traffic.ingest(c, push_read("WAT5679", "0.66", at=t0), t0 + 0.5)
    e = db.event(near["alarm_id"])
    assert e["priority"] == "medium" and e["policy"]["match"] == "near" and "one character off" in e["policy"]["text"]
    ok = traffic.ingest(c, push_read("OK11111", "0.90", at=t0 + 20), t0 + 20.5)
    assert ok["list"] == "allow" and ok["alarm_id"] is None
    weak = traffic.ingest(c, push_read("WAT5678", "0.30", at=t0 + 40), t0 + 40.5)
    assert weak["alarm_id"] is None and weak["list"] is None
    traffic.add_entry("OLD0001", "deny", expires_at=t0 - 1)
    assert traffic.ingest(c, push_read("OLD0001", "0.9", at=t0 + 60), t0 + 60.5)["alarm_id"] is None


def test_list_roles():
    reset()
    assert call("POST", "/api/traffic/lists", role="viewer", json_body={"plate": "ABC1234", "list": "deny"}).status_code == 403
    assert call("POST", "/api/traffic/lists", role="operator", json_body={"plate": "?", "list": "deny"}).status_code == 422
    eid = call("POST", "/api/traffic/lists", json_body={"plate": "ABC1234", "list": "watch"}).json()["id"]
    assert call("PUT", f"/api/traffic/lists/{eid}", role="operator", json_body={"note": "blue van"}).json()["note"] == "blue van"
    assert call("DELETE", f"/api/traffic/lists/{eid}", role="viewer").status_code == 403
    assert call("DELETE", f"/api/traffic/lists/{eid}", role="operator").status_code == 200
    assert call("GET", "/api/traffic/lists", role="viewer").json() == []


# ---------------------------------------------------------------- search, counts, health, retention

def test_search_and_counts():
    reset()
    c = cam()
    t0 = time.time() - 600
    for i, (p, conf, d) in enumerate([("ABC1234", "0.9", "Away"), ("ABC1284", "0.7", "Away"), ("No Plates", "0", "Approach"),
                                      ("XYZ9876", "0.8", "-")]):
        traffic.ingest(c, push_read(p, conf, at=t0 + i * 40, direction=d), t0 + i * 40 + 0.5)   # apart: not one parked car
    s = call("GET", "/api/traffic/reads?q=abc-1234").json()
    assert s["near"] == ["ABC1284"] and {r["plate"] for r in s["reads"]} == {"ABC1234", "ABC1284"}
    assert {r["plate"] for r in call("GET", "/api/traffic/reads?q=123").json()["reads"]} == {"ABC1234"}
    assert len(call("GET", "/api/traffic/reads?plates=true").json()["reads"]) == 3
    assert len(call("GET", "/api/traffic/reads?direction=approach").json()["reads"]) == 1
    cnt = call("GET", f"/api/traffic/counts?camera=cam8&since={t0 - 3600}").json()
    total = sum(b["total"] for b in cnt["buckets"])
    assert total == 4 and sum(b["plates"] for b in cnt["buckets"]) == 3
    assert sum(b["direction"].get("away", 0) for b in cnt["buckets"]) == 2
    st = call("GET", "/api/traffic/status").json()["cameras"][0]
    assert st["camera_id"] == "cam8" and st["reads_today"] >= 1 and st["enabled"]


def test_health_alert_when_silent():
    reset()
    cam()
    h = traffic.Health()
    now = time.time()
    h.started = now - 2000
    cams = db.cameras()
    traffic.set_traffic("cam8", set_at=now - 2000)
    a = h.alerts(db.cameras(), now)
    assert a and a[0]["kind"] == "traffic_push_silent" and "Traffic Camera" in a[0]["text"]
    h.heard("cam8", "status", now - 60)
    assert h.alerts(db.cameras(), now) == []
    traffic.set_traffic("cam8", enabled=False)
    h2 = traffic.Health()
    h2.started = now - 2000
    assert h2.alerts(db.cameras(), now) == [] and cams


def test_prune_old_reads():
    reset()
    c = cam()
    old = time.time() - 40 * 86400
    traffic.ingest(c, push_read("OLD1234", "0.9", at=old), old + 0.5)
    traffic.ingest(c, push_read("NEW1234", "0.9"), time.time())
    old_dir = settings.data_dir / "traffic" / "cam8" / dt.datetime.fromtimestamp(old).strftime("%Y-%m-%d")
    assert old_dir.is_dir()
    assert traffic.prune(30) == 1 and not old_dir.exists()
    assert [r["plate"] for r in db.all("SELECT plate FROM vehicle_reads")] == ["NEW1234"]


# ---------------------------------------------------------------- the camera's setup (fake Milesight)

class FakeCamera:
    """The TS4441's real get.* answers; set.* stores what was posted (the camera's page posts the whole object)."""

    def __init__(self, slots_taken=0):
        self.state = {a: json.loads((FIX / f"{a}.json").read_text())
                      for a in ("get.system.information", "get.lpr.settings", "get.lpr.post", "get.storage.settings")}
        for s in self.state["get.lpr.post"]["http"]["httpList"][:slots_taken]:
            s.update({"enable": 1, "url": "http://192.0.2.200/other"})
        self.sets: list[tuple[str, dict]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        action = req.url.params.get("action")
        if req.method == "GET":
            return httpx.Response(200, json=self.state.get(action, {"setState": "failed"}))
        body = json.loads(req.content)
        self.sets.append((action, body))
        self.state["get." + action[4:]] = {k: v for k, v in body.items() if k != "postType"}
        return httpx.Response(200, json={"setState": "succeed"})


def test_setup_through_the_cameras_own_api():
    reset()
    db.upsert_camera({"id": "cam9", "name": "Gate LPR", "host": "192.0.2.63", "onvif_port": 80, "rtsp_port": 554,
                      "username": "u", "password": "p", "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": []})
    fake = FakeCamera()
    milesight._transport = httpx.MockTransport(fake)
    settings.traffic_push_base = "http://192.0.2.5:8080"
    try:
        assert call("PUT", "/api/cameras/cam9/traffic", role="operator", json_body={"enabled": True}).status_code == 403
        r = call("PUT", "/api/cameras/cam9/traffic", role="admin", json_body={"enabled": True})
        assert r.status_code == 200, r.text
        sets = dict(fake.sets)
        assert sets["set.lpr.settings"]["lprEnable"] == 1 and sets["set.lpr.settings"]["curArea"] == "United States"
        slot = sets["set.lpr.post"]["http"]["httpList"][0]
        t = traffic.camera("cam9")["traffic"]
        assert slot["url"] == f"http://192.0.2.5:8080/api/traffic/push/cam9/{t['secret']}" and slot["enable"] == 1
        assert slot["eventMask"] == traffic.PUSH_MASK and slot["statusReportEnable"] == 1 and t["enabled"]
        j = r.json()
        assert j["camera"]["lpr_enabled"] and j["camera"]["slots"][0]["ours"] and t["secret"] not in json.dumps(j)
        assert j["camera"]["slots"][0]["url"] == "http://192.0.2.5:8080/api/traffic/push/cam9/..."
        assert j["camera"]["model"] == "TS4441-X36RPE" and j["camera"]["sd_card"] is False and j["camera"]["licensed"]
        got = call("GET", "/api/cameras/cam9/traffic", role="operator").json()
        assert got["supported"] and got["server"]["enabled"] and t["secret"] not in json.dumps(got)
        # set up again: same slot, same secret
        call("PUT", "/api/cameras/cam9/traffic", json_body={"enabled": True})
        assert traffic.camera("cam9")["traffic"]["secret"] == t["secret"]
        assert sum(1 for s in fake.state["get.lpr.post"]["http"]["httpList"] if s.get("url")) == 1
        # stop: our slot emptied, plate reading left on
        call("PUT", "/api/cameras/cam9/traffic", json_body={"enabled": False})
        s0 = fake.state["get.lpr.post"]["http"]["httpList"][0]
        assert s0["enable"] == 0 and s0["url"] == "" and fake.state["get.lpr.settings"]["lprEnable"] == 1
        assert not traffic.camera("cam9")["traffic"]["enabled"]
        # every slot taken by something else: 409, nothing written
        full = FakeCamera(slots_taken=3)
        milesight._transport = httpx.MockTransport(full)
        r = call("PUT", "/api/cameras/cam9/traffic", json_body={"enabled": True})
        assert r.status_code == 409 and "slots are in use" in r.json()["detail"]
        assert all(a != "set.lpr.post" for a, _ in full.sets)
    finally:
        milesight._transport = None
        settings.traffic_push_base = ""


def test_not_a_traffic_camera():
    milesight._transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"setState": "failed"}))
    try:
        cam()
        j = call("GET", "/api/cameras/cam8/traffic").json()
        assert j["supported"] is False and "doesn't answer" in j["reason"]
    finally:
        milesight._transport = None


def test_ask_evidence():
    reset()
    c = cam()
    t0 = time.time() - 300
    traffic.ingest(c, push_read("ABC1234", "0.88", at=t0, vehicle_color="White", vehicle_type="Truck"), t0 + 0.5)
    traffic.ingest(c, push_read("XYZ9876", "0.91", at=t0 + 30, vehicle_color="Black", vehicle_type="SUV"), t0 + 30.5)
    traffic.ingest(c, push_read("LOW0000", "0.30", at=t0 + 60, vehicle_color="White", vehicle_type="Truck"), t0 + 60.5)
    named = traffic.ask_evidence("was ABC-1234 here at 10pm?", None, None)
    assert [x["plate"] for x in named] == ["ABC1234"] and named[0]["kind"] == "plate"
    assert "license plate ABC 1234 read by the camera's plate reader (88 % sure; state reported KS)" in named[0]["text"]
    assert "going away (reported)" in named[0]["text"] and named[0]["camera_name"] == "Traffic Camera"
    near = traffic.ask_evidence("plate ABC1284", None, None)
    assert [x["plate"] for x in near] == ["ABC1234"]   # one character off: the near plate
    white = traffic.ask_evidence("tag number of the white truck?", None, None)
    assert [x["plate"] for x in white] == ["ABC1234"]   # confident reads of white trucks only
    assert traffic.ask_evidence("how many people came in today?", None, None) == []
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hub"))
    from hub import site_ask
    line = site_ask.evidence_line({**named[0], "camera": "Traffic Camera"}, dt.timezone.utc, time.time())
    assert line.startswith("Traffic Camera, ") and "license plate ABC 1234" in line
    assert site_ask._key("s1", named[0]) == ("s1", "plate", named[0]["read_id"])


def test_config_export_leaves_the_push_secret_out():
    from nvr import siteconfig
    cam()
    out = json.dumps(siteconfig.export_config())
    assert SECRET not in out and '"traffic"' not in out


def test_read_plate_on_demand():
    """The camera said "No plates" on a crop that shows one: Otso reads it (fake model), kept beside the camera's read,
    searchable, list hit reported; the rate limit, the operator role, Otso not ready."""
    from nvr import plate as plate_mod
    reset()
    c = cam()
    t0 = time.time()
    # the camera sent its plate crop but read no plate (cam8: a Kansas "51121" specialty plate)
    rid = traffic.ingest(c, push_read("No Plates", "0.00", at=t0, license_plate_snapshot=jpg(90, 40)), t0 + 0.4)["read_id"]
    traffic.add_entry("407MFL", "watch", note="red Fiesta")
    asked = []

    async def fake_ask(img, priority, question=plate_mod.QUESTION, system=plate_mod.SYSTEM):
        asked.append((len(img), priority, question, system))
        return {"legible": True, "plate": "407 MFL", "region": "Kansas", "confidence": "high", "_model": "fake"}

    class P:
        vlm_ready = True
    real_ask, real_pipe = plate_mod.ask, traffic_api.pipeline
    plate_mod.ask, traffic_api.pipeline = fake_ask, P()
    try:
        assert call("POST", f"/api/traffic/reads/{rid}/plate", role="viewer").status_code == 403
        r = call("POST", f"/api/traffic/reads/{rid}/plate", role="operator")
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["result"] == "read" and j["plate"] == "407MFL" and j["confidence"] == "high" and j["region"] == "KS"
        assert j["read"]["plate"] is None and j["read"]["otso"]["plate"] == "407MFL" and j["read"]["otso_plate"] == "407MFL"
        assert j["list"] == {"list": "watch", "match": "exact", "plate": "407MFL", "note": "red Fiesta"}
        # Ask Otso's notes on the plate it read (the camera read none), kept with its answer
        assert j["about"] == "407MFL" and j["notes"] == ["The first time this plate has been seen here."]
        assert j["read"]["otso"]["about"] == "407MFL" and j["read"]["otso"]["notes"] == j["notes"]
        # the camera's plate crop first, enlarged, asked as a plate close-up, at a person's priority; a high read stops there
        assert len(asked) == 1 and asked[0][1] == "chat" and asked[0][2] == plate_mod.PLATE_QUESTION
        assert asked[0][3] == plate_mod.ON_REQUEST_SYSTEM   # a person asked: soft but readable plates are read
        # Otso's state only when it is sure (on soft crops it named a wrong one every time)
        kept = traffic.store_otso(rid, {"result": "read", "plate": "3416AFR", "region": "MA", "confidence": "medium"})
        assert kept["otso"]["plate"] == "3416AFR" and "region" not in kept["otso"] and kept["otso_plate"] == "3416AFR"
        traffic.store_otso(rid, {"result": "read", "plate": "407MFL", "plate_raw": "407 MFL", "region": "KS", "confidence": "high"})
        crops = traffic.otso_crops(rid)
        assert isinstance(crops[0], tuple) and cv2.imdecode(np.frombuffer(crops[0][0], np.uint8), cv2.IMREAD_COLOR).shape[1] == traffic.PLATE_LOOK_PX + 48
        assert crops[1] == traffic.image_path(rid, "vehicle").read_bytes()
        assert call("POST", f"/api/traffic/reads/{rid}/plate", role="operator").status_code == 429
        found = call("GET", "/api/traffic/reads?q=407MFL").json()["reads"]
        assert [x["id"] for x in found] == [rid]
        assert rid in [x["id"] for x in call("GET", "/api/traffic/reads?plates=true").json()["reads"]]
        P.vlm_ready = False
        traffic.read_limiter.last.clear()
        assert call("POST", f"/api/traffic/reads/{rid}/plate").status_code == 503
        assert call("POST", "/api/traffic/reads/999999/plate").status_code == 404
    finally:
        plate_mod.ask, traffic_api.pipeline = real_ask, real_pipe
        traffic.read_limiter.last.clear()


def _texture(seed: int, w: int = 116, h: int = 60) -> np.ndarray:
    """A plate-like picture no other one resembles (blurred noise)."""
    g = np.random.default_rng(int(seed)).integers(0, 255, (h, w), dtype=np.uint8)
    return cv2.cvtColor(cv2.GaussianBlur(g, (5, 5), 0), cv2.COLOR_GRAY2BGR)


def _b64(img: np.ndarray) -> str:
    return base64.b64encode(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()).decode()


class FakeRecording:
    """A recording of cam8 where each plate shows `offset` seconds after the camera's read time (the run's offset), in
    the plate box of the fixture push, over a noisy road; anything else is background."""

    def __init__(self, offset: float):
        self.offset, self.plates, self.calls = offset, {}, []
        self.bg = np.random.default_rng(99).integers(0, 255, (1520, 2688, 3), dtype=np.uint8)

    def show(self, ts: float, tex: np.ndarray) -> None:
        self.plates[ts + self.offset] = tex

    still: np.ndarray | None = None   # a sign in the plate box all the time (the car lot's, read as a plate at night)

    def frame(self, t: float) -> np.ndarray:
        # a passing plate is in its box for one frame (cam8: 0.98-1.00 at one frame, under 0.75 either side)
        tex = next((v for k, v in self.plates.items() if abs(k - t) < 0.005), self.still)
        if tex is None:
            return self.bg   # read only
        f = self.bg.copy()
        l, tp, r, b = 0.5833, 0.5481, 0.6438, 0.6037   # the fixture's plate box (1120,592)-(1236,652) of 1920x1080
        x1, y1, x2, y2 = int(l * 2688), int(tp * 1520), int(r * 2688), int(b * 1520)
        f[y1:y2, x1:x2] = cv2.resize(tex, (x2 - x1, y2 - y1))
        return f

    def iter_recording(self, camera_id, targets, tol=0.6):
        self.calls.append(len(targets))
        for t in sorted(targets):
            yield t, self.frame(t)

    def recorded_frame(self, camera_id, t):
        return self.frame(t)


def test_vehicle_picture_from_the_recording():
    """cam8 with optimal push: reads with only their plate crop. Their vehicle picture is cut from the recording at the
    segment's offset, learned from reads whose plate crop matches strongly (two agreeing); a read before that waits; one
    strong-looking wrong match doesn't set the offset; the next segment learns its own; nothing recorded: none."""
    from nvr import frames
    reset()
    c = cam()
    t0 = time.time()
    rec = FakeRecording(1.5)
    real = frames.iter_recording, frames.recorded_frame, traffic.recording_segment
    frames.iter_recording, frames.recorded_frame = rec.iter_recording, rec.recorded_frame
    traffic.recording_segment = lambda camera_id, t: 1000.0   # one segment
    traffic.from_recording.due.clear()
    traffic.aligner.found.clear()

    def read(plate, at, tex=None, **kw):
        if tex is not None:
            rec.show(at, tex)
        snap = _b64(tex if tex is not None else _texture(at))
        return traffic.ingest(c, push_read(plate, "0.9", at=at, vehicle_snapshot="-", full_snapshot="-", license_plate_snapshot=snap, **kw),
                              at + 0.5)["read_id"]
    try:
        a = read("AAA1111", t0, _texture(1))
        assert traffic.read(a)["images"] == ["plate"] and [x[1] for x in traffic.from_recording.due] == [a]
        hidden = read("HID2222", t0 + 20)              # its plate isn't in the recording (seen badly): no match
        assert traffic.cut_from_recording(hidden) is None   # nothing known yet: it waits
        assert traffic.cut_from_recording(a) is True    # a strong match, but one alone sets no offset
        assert traffic.aligner.offset("cam8", 1000.0) is None and rec.calls[-1] == len(traffic.WIDE)
        b = read("BBB3333", t0 + 40, _texture(2))
        assert traffic.cut_from_recording(b) is True and traffic.aligner.offset("cam8", 1000.0) == 1.5
        assert traffic.cut_from_recording(hidden) is True   # now from the run's offset
        r = traffic.read(hidden)
        assert r["images"] == ["plate", "vehicle"] and r["vehicle_source"] == "recording"
        later = read("CCC4444", t0 + 60, _texture(3))
        assert traffic.cut_from_recording(later) is True and rec.calls[-1] == len(traffic.window(1.5, traffic.NARROW_S))   # around the offset only
        # the crop is the vehicle box of the frame where the plate is: the plate's texture inside it
        crop = cv2.imread(str(traffic.image_path(later, "vehicle")))
        assert crop is not None and crop.shape[1] > crop.shape[0]
        # a wrong strong-looking match elsewhere doesn't move an agreed offset
        traffic.aligner.learn("cam8", 1000.0, 4.75)
        assert traffic.aligner.offset("cam8", 1000.0) == 1.5
        # a sign read as a plate matches at every offset: it teaches nothing (a fresh segment stays unknown)
        traffic.recording_segment = lambda camera_id, t: 1500.0
        sign = _texture(77)
        rec.still = sign
        for k in range(3):
            sid = read("656BE", t0 + 70 + k * 5, sign, vehicle_type="-", vehicle_color="-")
            traffic.cut_from_recording(sid)
        assert traffic.aligner.offset("cam8", 1500.0) is None
        rec.still = None
        traffic.recording_segment = lambda camera_id, t: 1000.0
        # the next segment: its own offset (searched first around the nearest learned one, then wide), learned again
        traffic.recording_segment = lambda camera_id, t: 2000.0
        rec.offset = -0.5
        n1 = read("NEW1111", t0 + 100, _texture(4))
        n2 = read("NEW2222", t0 + 120)
        assert traffic.cut_from_recording(n1) is True and rec.calls[-2:] == [len(traffic.window(1.5, traffic.PRIOR_S)), len(traffic.WIDE)]
        assert traffic.cut_from_recording(n2) is None
        # an older read asked for before it is cut: 404 at once, queued (once) for the cutter, which then cuts it
        n3 = read("NEW3333", t0 + 140, _texture(5))
        assert traffic.cut_from_recording(n3) is True and traffic.aligner.offset("cam8", 2000.0) == -0.5
        traffic.from_recording.due.clear()
        traffic.from_recording.queued.clear()
        got = call("GET", f"/api/traffic/reads/{n2}/image/vehicle")
        assert got.status_code == 404 and got.headers.get("retry-after") == "5" and "being cut" in got.json()["detail"]
        assert call("GET", f"/api/traffic/reads/{n2}/image/vehicle").status_code == 404
        assert [x[1] for x in traffic.from_recording.due] == [n2]   # queued once, however often it is asked for
        assert traffic.cut_from_recording(n2) is True
        got = call("GET", f"/api/traffic/reads/{n2}/image/vehicle")
        assert got.status_code == 200 and got.content[:2] == b"\xff\xd8"
        # the camera's own picture stays "camera"
        own = traffic.ingest(c, push_read("OWN1234", "0.9", at=t0 + 160), t0 + 160.5)["read_id"]
        assert traffic.read(own)["vehicle_source"] == "camera"
        # nothing recorded there: no picture
        traffic.recording_segment = lambda camera_id, t: None
        none = read("NONE123", t0 + 180)
        assert traffic.cut_from_recording(none) is False and call("GET", f"/api/traffic/reads/{none}/image/vehicle").status_code == 404
    finally:
        frames.iter_recording, frames.recorded_frame, traffic.recording_segment = real
        traffic.from_recording.due.clear()
        traffic.aligner.found.clear()


def test_reads_that_are_not_a_vehicle():
    """cam8 at night: the car lot's sign read as "556BE" (no vehicle type, conf ~0.4). Hidden from the list, counts,
    today's totals and Ask; found by a plate search and with noise=true; no picture is cut for it."""
    reset()
    c = cam()
    t0 = time.time() - 300
    traffic.from_recording.due.clear()
    sign = traffic.ingest(c, push_read("556BE", "0.40", at=t0, vehicle_type="-", vehicle_color="-", vehicle_snapshot="-"), t0 + 0.3)["read_id"]
    junk = traffic.ingest(c, push_read("No Plates", "0", at=t0 + 40, vehicle_type="-", vehicle_color="-"), t0 + 40.3)["read_id"]
    sure = traffic.ingest(c, push_read("8777AHN", "0.88", at=t0 + 80, vehicle_type="-", vehicle_color="-"), t0 + 80.3)["read_id"]
    bright = traffic.ingest(c, push_read("656BE", "0.60", at=t0 + 100, vehicle_type="-", vehicle_color="-"), t0 + 100.3)["read_id"]
    car = traffic.ingest(c, push_read("ABC1234", "0.40", at=t0 + 120), t0 + 120.3)["read_id"]   # a real truck, low confidence
    assert [x[1] for x in traffic.from_recording.due] == []   # no picture cut for the sign
    shown = {r["id"] for r in call("GET", "/api/traffic/reads").json()["reads"]}
    assert shown == {sure, car}
    assert {r["id"] for r in call("GET", "/api/traffic/reads?noise=true").json()["reads"]} == {sign, junk, sure, bright, car}
    assert sign in {r["id"] for r in call("GET", "/api/traffic/reads?q=556BE").json()["reads"]}
    assert sum(b["total"] for b in traffic.counts(camera="cam8", since=t0 - 10)["buckets"]) == 2
    st = call("GET", "/api/traffic/status").json()["cameras"][0]
    assert st["reads_today"] == 2 and st["plates_today"] == 2
    assert traffic.ask_evidence("was 556BE here?", None, None) == []   # the sign, even when named
    assert [x["read_id"] for x in traffic.ask_evidence("was 8777AHN here?", None, None)] == [sure]
    traffic.from_recording.due.clear()


def test_cuts_and_the_disk_check_have_their_own_threads():
    """A page of reads asking for pictures must not stall the server: cuts run on traffic.CUT_POOL, the disk check on
    its own thread, so a full shared pool never reads as a hung disk (TSS-DT 2026-10-28)."""
    import concurrent.futures
    import threading
    from nvr import health
    assert isinstance(traffic.CUT_POOL, concurrent.futures.ThreadPoolExecutor) and traffic.CUT_POOL._max_workers == 2
    seen = []
    w = health.StorageWatch(probe=lambda: seen.append(threading.current_thread().name))

    async def go():
        # fill the shared pool with slow work, as the pictures did; the probe still answers at once
        loop = asyncio.get_running_loop()
        stop = threading.Event()
        blockers = [loop.run_in_executor(None, stop.wait, 5) for _ in range(64)]
        t = time.time()
        ok = await w.check()
        took = time.time() - t
        stop.set()
        await asyncio.gather(*blockers)
        return ok, took
    ok, took = asyncio.run(go())
    assert ok and took < 2 and seen and seen[0].startswith("storage-probe")


def test_new_reads_are_cut_before_the_backlog():
    """Reads queued at start-up must not hold up new ones: due reads are taken newest first (TSS-DT 2026-10-29: 253
    backfilled reads at 5 s each kept the morning's new reads waiting until they were too old for a wide search)."""
    q = traffic.FromRecording()
    now = time.time()
    for rid in (5, 6, 7):
        q.add(rid, now - 10, traffic.BACKFILL_TRIES)
    q.add(900, now)
    q.add(901, now + 60)   # not due yet
    assert [x[1] for x in q.take(now)] == [900, 7, 6, 5]
    assert [x[1] for x in q.due] == [901] and q.queued == {901}


def test_backfill_queues_recent_reads_without_a_picture():
    reset()
    c = cam()
    t0 = time.time() - 600
    traffic.from_recording.due.clear()
    traffic.from_recording.queued.clear()
    old = traffic.ingest(c, push_read("OLD1111", "0.9", at=t0, vehicle_snapshot="-"), t0 + 0.3)["read_id"]
    has = traffic.ingest(c, push_read("HAS2222", "0.9", at=t0 + 30), t0 + 30.3)["read_id"]
    sign = traffic.ingest(c, push_read("556BE", "0.4", at=t0 + 60, vehicle_type="-", vehicle_color="-", vehicle_snapshot="-"), t0 + 60.3)["read_id"]
    traffic.from_recording.due.clear()
    traffic.from_recording.queued.clear()
    assert traffic.from_recording.backfill() == 1
    assert [x[1] for x in traffic.from_recording.due] == [old] and has and sign
    assert traffic.from_recording.backfill() == 0   # already queued
    traffic.from_recording.due.clear()
    traffic.from_recording.queued.clear()


def test_offsets_kept_and_older_reads_gentle():
    """A segment's offset survives a restart (recording_offsets); an older read is cut only near a known offset (its
    segment's, or the nearest learned: the camera's clock drifts slowly), never with a wide search over the recording
    disk (TSS-DT 2026-10-29: the morning's reads, none placed on a 0.25 s grid, placed near the evening's offsets)."""
    from nvr import frames
    reset()
    db.execute("DELETE FROM recording_offsets")
    c = cam()
    rec = FakeRecording(0.76)
    real = frames.iter_recording, frames.recorded_frame, traffic.recording_segment
    frames.iter_recording, frames.recorded_frame = rec.iter_recording, rec.recorded_frame
    traffic.recording_segment = lambda camera_id, t: 3000.0
    traffic.aligner.found.clear()
    try:
        t0 = time.time()
        for k in range(2):
            tex = _texture(100 + k)
            rec.show(t0 + k * 20, tex)
            rid = traffic.ingest(c, push_read(f"KEP{k}111", "0.9", at=t0 + k * 20, vehicle_snapshot="-", license_plate_snapshot=_b64(tex)), t0 + k * 20 + 0.5)["read_id"]
            assert traffic.cut_from_recording(rid) is True
        assert db.one("SELECT offset_s FROM recording_offsets WHERE camera_id='cam8' AND segment=3000.0")["offset_s"] == 0.76
        traffic.aligner.found.clear()   # a restart
        assert traffic.aligner.offset("cam8", 3000.0) == 0.76
        # an older read in a segment with no known offset: searched near the nearest learned one only, never wide
        traffic.recording_segment = lambda camera_id, t: 4000.0
        old = traffic.ingest(c, push_read("OLD9999", "0.9", at=t0 - 3600, vehicle_snapshot="-"), t0 - 3600 + 0.5)["read_id"]
        db.execute("UPDATE vehicle_reads SET received_at=? WHERE id=?", [t0 - 3600, old])
        before = len(rec.calls)
        assert traffic.cut_from_recording(old) is None and rec.calls[before:] == [len(traffic.window(0.76, traffic.PRIOR_S))]
        # ...and found there when its plate is, the clock having drifted a little since
        rec.offset = 1.26
        tex = _texture(200)
        rec.show(t0 - 3000, tex)
        old2 = traffic.ingest(c, push_read("OLD8888", "0.9", at=t0 - 3000, vehicle_snapshot="-", license_plate_snapshot=_b64(tex)), t0 - 3000 + 0.5)["read_id"]
        db.execute("UPDATE vehicle_reads SET received_at=? WHERE id=?", [t0 - 3000, old2])
        before = len(rec.calls)
        assert traffic.cut_from_recording(old2) is True and rec.calls[before:] == [len(traffic.window(0.76, traffic.PRIOR_S))]
        assert traffic.aligner.offset("cam8", 4000.0) is None   # one match alone teaches no offset
        # nothing learned within PRIOR_MAX_S: an older read waits, reading nothing
        traffic.recording_segment = lambda camera_id, t: 3000.0 + traffic.PRIOR_MAX_S + 10
        old3 = traffic.ingest(c, push_read("OLD7777", "0.9", at=t0 - 2400, vehicle_snapshot="-"), t0 - 2400 + 0.5)["read_id"]
        db.execute("UPDATE vehicle_reads SET received_at=? WHERE id=?", [t0 - 2400, old3])
        before = len(rec.calls)
        assert traffic.cut_from_recording(old3) is None and len(rec.calls) == before
    finally:
        frames.iter_recording, frames.recorded_frame, traffic.recording_segment = real
        traffic.aligner.found.clear()
        traffic.from_recording.due.clear()
        traffic.from_recording.queued.clear()


def _sighting(plate, when, cam="cam8", vtype="Truck", direction="away"):
    t = when.timestamp()
    db.execute("INSERT INTO vehicle_reads (camera_id, ts, received_at, plate, plate_raw, confidence, vehicle_type, direction) "
               "VALUES (?,?,?,?,?,?,?,?)", [cam, t, t + 0.5, plate, plate, 0.9, vtype, direction])


def test_plate_notes_from_a_plates_history():
    """Ask Otso's notes: counted from the reads (and, once they are pruned, from plate_days), worded in code. A commuter
    seen every weekday at ~8:15 going away since August, three times last November."""
    reset()
    now = dt.datetime(2026, 10, 28, 9, 0)                   # a Wednesday
    days = [dt.date(2025, 11, 3) + dt.timedelta(days=k) for k in range(3)]
    d = dt.date(2026, 8, 3)
    while d < now.date():
        if d.weekday() < 5:
            days.append(d)
        d += dt.timedelta(days=1)
    for k, day in enumerate(days):
        _sighting("7769ABL", dt.datetime.combine(day, dt.time(8, 10 + k % 11)))
    _sighting("7769ABL", dt.datetime(2026, 10, 28, 8, 14))  # this one
    this = dt.datetime(2026, 10, 28, 8, 14).timestamp()
    # older reads go (traffic_keep_days): their sightings are kept per plate and day, without the reads
    traffic.prune(keep_days=30, now=now.timestamp())
    assert db.one("SELECT COUNT(*) n FROM vehicle_reads WHERE ts < ?", [now.timestamp() - 30 * 86400])["n"] == 0
    assert db.one("SELECT COUNT(*) n FROM plate_days WHERE plate='7769ABL'")["n"] > 40
    n = len(days) + 1
    notes = traffic.plate_notes("7769ABL", this, now.timestamp())
    assert notes["sightings"] == n
    assert notes["notes"][0] == f"Seen {n} times since Nov 3, 2025, {n - 3} of them this year."
    assert notes["notes"][1] == f"Before this, last seen yesterday at 8:{10 + (len(days) - 1) % 11} AM, heading away."
    assert notes["notes"][2].startswith("Usually on weekdays around 8:15 AM (8:10-8:20 AM), heading away:"), notes["notes"]
    assert notes["notes"][3] == "About 5 times a week over the last 8 weeks, on 40 of 56 days."
    assert len(notes["notes"]) == 4
    # past traffic_history_days the summaries go too
    traffic.prune(keep_days=30, now=(now + dt.timedelta(days=500)).timestamp())
    assert db.one("SELECT COUNT(*) n FROM plate_days")["n"] == 0


def test_plate_notes_say_little_from_little():
    """No habit from one afternoon (a car circling); different kinds of vehicle on one plate are pointed out; a first
    sighting says so; the camera lot's sign (noise) is no sighting."""
    reset()
    now = dt.datetime(2026, 10, 28, 18, 0)
    for m in (10, 14, 20):
        _sighting("HLU", dt.datetime(2026, 10, 28, 17, m))
    hlu = traffic.plate_notes("HLU", dt.datetime(2026, 10, 28, 17, 20).timestamp(), now.timestamp())["notes"]
    assert hlu == ["Seen 3 times today.", "Before this, last seen today at 5:14 PM, heading away."]
    for k in range(3):
        _sighting("SWAP123", dt.datetime(2026, 10, 20 + k, 7, 0), vtype="Truck")
        _sighting("SWAP123", dt.datetime(2026, 10, 20 + k, 19, 0), vtype="Car", direction="approach")
    swap = traffic.plate_notes("SWAP123", now.timestamp(), now.timestamp())["notes"]
    assert any(x.startswith("Seen on different kinds of vehicle (Truck 3 times, Car 3 times)") or
               x.startswith("Seen on different kinds of vehicle (Car 3 times, Truck 3 times)") for x in swap), swap
    assert "Usually around 7:00 AM, heading away: 3 of 6 times, on 3 days." in swap
    assert "Usually around 7:00 PM, coming toward the camera: 3 of 6 times, on 3 days." in swap
    _sighting("NEW1", dt.datetime(2026, 10, 28, 17, 0))
    assert traffic.plate_notes("NEW1", now.timestamp(), now.timestamp())["notes"] == ["The first time this plate has been seen here."]
    t = dt.datetime(2026, 10, 27, 23, 0).timestamp()
    db.execute("INSERT INTO vehicle_reads (camera_id, ts, received_at, plate, confidence) VALUES ('cam8',?,?,?,?)", [t, t, "NEW1", 0.4])
    assert traffic.plate_notes("NEW1", now.timestamp(), now.timestamp())["sightings"] == 1


def test_hub_roles():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hub"))
    from hub import roles
    assert roles.required_role("GET", "/api/traffic/reads") == "viewer"
    assert roles.required_role("GET", "/api/traffic/reads/5/image/plate") == "viewer"
    assert roles.required_role("POST", "/api/traffic/lists") == "operator"
    assert roles.required_role("PUT", "/api/traffic/lists/3") == "operator"
    assert roles.required_role("DELETE", "/api/traffic/lists/3") == "operator"
    assert roles.required_role("GET", "/api/cameras/cam8/traffic") == "operator"
    assert roles.required_role("PUT", "/api/cameras/cam8/traffic") == "admin"
    assert roles.required_role("POST", "/api/traffic/push/cam8/x") == "owner"
    assert roles.required_role("POST", "/api/traffic/reads/12/plate") == "operator"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
