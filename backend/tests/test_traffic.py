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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
