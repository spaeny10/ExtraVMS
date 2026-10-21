"""License plates (plate.py): parsing and normalization, "unreadable" never stored as a plate, the minimum-size skip,
full-resolution crops from a real (tiny, generated) clip, the read with the synopsis and "Read plate" on demand
(POST /api/events/{id}/plate) with a fake VLM: the rate limit, outside AI hours, the hub roles, the meter kind. Also the
search index (plate, partial plate, "tag number"), the Ask's class-intent detector and its evidence (assistant.py,
retrieve.py), and a merge keeping a vehicle's plate.

Nothing reaches Qwen or Ollama (the router's model call, the embeddings and the synopsis are fakes); throwaway data,
recordings and runtime dirs. Run: ..\\.venv\\Scripts\\python.exe tests\\test_plate.py   (from backend/)
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="nvr-plate-test-"))
for k, sub in (("NVR_DATA_DIR", "data"), ("NVR_RECORDINGS_DIR", "recordings"), ("NVR_RUNTIME_DIR", "runtime")):
    os.environ[k] = str(TMP / sub)   # never the real server's database, recordings or mediamtx.yml
os.environ["NVR_ALLOWED_HOSTS"] = "site"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tunnelproto"))

import av  # noqa: E402
import cv2  # noqa: E402
import httpx  # noqa: E402
import numpy as np  # noqa: E402

from nvr import aischedule, api, assistant, frames, hub_agent, mediamtx, merge, plate, retrieve, vlmroute  # noqa: E402
from nvr import synopsis as vlm  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402
from nvr.pipeline import Pipeline  # noqa: E402

for d in (settings.data_dir, settings.recordings_dir, settings.runtime_dir):
    v = str(d).replace("\\", "/").lower()
    assert "nvr-plate-test-" in v and "e:/nvr" not in v and "d:/nvr" not in v, d
    d.mkdir(parents=True, exist_ok=True)

mediamtx.recording_spans = lambda *a, **k: []


async def no_embed(text):
    return None


vlm.embed = no_embed   # reindex: keyword index only, never the real Ollama
db.upsert_camera({"id": "cam1", "name": "Gate", "host": "192.0.2.5", "onvif_port": 80, "rtsp_port": 554, "username": "u",
                  "password": "p", "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": []})
LAN = ("192.0.2.9", 5000)
NOW = time.time()


class FakeVlm:
    """vlmroute.router._chat_json stand-in (the router's chat_json still meters around it): canned answers in order."""

    def __init__(self):
        self.answers: list = []
        self.calls: list[tuple[str, str, int, str]] = []

    async def __call__(self, task, system, text, images, schema, num_predict, temperature, priority):
        self.calls.append((task, text, len(images), priority))
        a = self.answers.pop(0) if self.answers else {"legible": False, "plate": "unreadable", "region": "", "confidence": "low"}
        if isinstance(a, Exception):
            raise a
        return {**a, "_model": "fake-qwen"}


FAKE = FakeVlm()
vlmroute.router._chat_json = FAKE
METERED: list[tuple] = []
aischedule.meter.record = lambda kind, inside, *a, **k: METERED.append((kind, inside))


def vehicle(boxes=((0.2, 0.3, 0.6, 0.8),), cls="vehicle", start=None, **kw) -> int:
    start = start or NOW - 600
    samples = [{"ts": start + i, "cam_box": list(b), "yolo": [], "match": {"cls": "car", "conf": 0.9, "box": list(b)}, "iou": 0.8}
               for i, b in enumerate(boxes)]
    eid = db.create_event(camera_id="cam1", track_id=str(time.time()), camera_class=cls, camera_conf=0.9, start_ts=start,
                          end_ts=start + 10, path=[[start, *boxes[0], 0.9]], status="verified",
                          detections={"samples": samples, "keyframes": []}, **kw)
    return eid


def jpeg_ok(b: bytes) -> bool:
    return b[:2] == b"\xff\xd8"


# ---------------------------------------------------------------- the answer

def test_normalize_and_parse():
    assert plate.normalize("abc-1234") == "ABC1234" and plate.normalize(" 7ABC 123 ") == "7ABC123"
    assert plate.normalize("ABC·123") == "ABC123" and plate.normalize("AB.12") == "AB12"
    for bad in ("unreadable", "Unreadable", "UNKNOWN", "n/a", "none", "", None, "AB?123", "A*C123", "AB_123", "X", "ABCDEFGHIJK1"):
        assert plate.normalize(bad) is None, bad
    r = plate.parse({"legible": True, "plate": "abc-1234", "region": "Texas", "confidence": "high", "_model": "m"})
    assert r == {"result": "read", "plate": "ABC1234", "plate_raw": "ABC-1234", "region": "TX", "confidence": "high", "model": "m"}
    assert plate.parse({"legible": True, "plate": "KX 4471", "region": "ca", "confidence": "medium"})["region"] == "CA"
    assert plate.parse({"legible": True, "plate": "KX 4471", "region": "unknown", "confidence": "medium"})["region"] is None
    # never a plate: not legible, a placeholder, unsure characters, low confidence, junk
    for ans in ({"legible": False, "plate": "ABC123", "confidence": "high"},
                {"legible": True, "plate": "unreadable", "confidence": "high"},
                {"legible": True, "plate": "AB?123", "confidence": "high"},
                {"legible": True, "plate": "ABC123", "confidence": "low"},
                {"legible": "yes", "plate": "ABC123", "confidence": "high"},
                {}, None, "not json"):
        assert plate.parse(ans)["result"] == "unreadable", ans
    # the most confident read wins, the best crop's among equals; none read: unreadable
    reads = [plate.parse({"legible": True, "plate": p, "confidence": c}) for p, c in (("AAA111", "medium"), ("BBB222", "high"), ("CCC333", "high"))]
    assert plate.best(reads)["plate"] == "BBB222"
    assert plate.best([plate.parse({}), plate.parse(None)])["result"] == "unreadable"
    assert plate.display("ABC1234", "TX") == "ABC 1234 · TX" and plate.display("7ABC123") == "7 ABC 123" and plate.display(None) == ""


def test_unreadable_is_never_stored_as_a_plate():
    eid = vehicle()
    plate.store(eid, plate.parse({"legible": True, "plate": "unreadable", "confidence": "high"}), "user")
    e = db.event(eid)
    assert e["plate"] is None and e["plate_raw"] is None and e["detections"]["plate_read"]["result"] == "unreadable"
    plate.store(eid, plate.parse({"legible": True, "plate": "TX-4471", "region": "Texas", "confidence": "high"}), "user")
    e = db.event(eid)
    assert (e["plate"], e["plate_raw"], e["plate_region"], e["plate_conf"]) == ("TX4471", "TX-4471", "TX", "high")
    # the synopsis path never undoes a plate read before; a person's new read replaces it
    plate.store(eid, {"result": "unreadable", "reason": "not legible"}, "synopsis", keep_plate=True)
    assert db.event(eid)["plate"] == "TX4471"
    plate.store(eid, {"result": "unreadable", "reason": "not legible"}, "user")
    assert db.event(eid)["plate"] is None and db.event(eid)["detections"]["plate_read"]["source"] == "user"


# ---------------------------------------------------------------- the crops

def fake_decode(h=1080, w=1920):
    def decode(e, times):
        rng = np.random.default_rng(1)
        return {t: rng.integers(0, 255, (h, w, 3), dtype=np.uint8) for t in times}, h
    return decode


def test_min_size_skip_and_best_crops():
    tiny = db.event(vehicle(boxes=((0.4, 0.45, 0.45, 0.5),)))       # 5% of 1080 = 54 px tall
    asked = []
    got = plate.gather(tiny, 1, min_px=120, decode=lambda e, t: asked.append(t) or ({}, None), height=lambda e: 1080)
    assert got["crops"] == [] and got["reason"].startswith("too small: 54 px < 120 px") and asked == []   # nothing decoded
    # without a known height the decoded frame decides
    got = plate.gather(tiny, 1, min_px=120, decode=fake_decode(), height=lambda e: None)
    assert got["crops"] == [] and got["reason"].startswith("too small")
    big = db.event(vehicle(boxes=((0.1, 0.1, 0.3, 0.3), (0.2, 0.2, 0.7, 0.9), (0.4, 0.4, 0.5, 0.45))))
    got = plate.gather(big, 2, min_px=120, decode=fake_decode(), height=lambda e: 1080)
    assert len(got["crops"]) == 2 and all(jpeg_ok(c) for c in got["crops"]) and got["reason"] is None
    first = cv2.imdecode(np.frombuffer(got["crops"][0], np.uint8), cv2.IMREAD_COLOR)
    # the largest box (0.5 x 0.7 of 1080p, padded) first, at full resolution: ~1114 x 877, not scaled
    assert 1100 <= first.shape[1] <= 1120 and 860 <= first.shape[0] <= 890
    assert got["px_h"] == 756
    none = db.event(vehicle())
    db.update_event(none["id"], detections={"samples": [{"ts": NOW, "cam_box": [0, 0, 1, 1], "yolo": [], "match": None}]})
    assert plate.gather(db.event(none["id"]), 1, decode=fake_decode(), height=lambda e: 1080)["reason"] == "no vehicle box"


def test_sharpest_of_the_largest_wins():
    sharp = np.zeros((400, 400, 3), np.uint8)
    sharp[::4] = 255                                  # stripes: a high Laplacian variance
    blurry = cv2.GaussianBlur(sharp, (51, 51), 20)
    assert plate.sharpness(sharp) > 10 * plate.sharpness(blurry)
    eid = vehicle(boxes=((0.0, 0.0, 1.0, 1.0), (0.0, 0.0, 0.95, 0.95)))   # the larger one is blurry
    e = db.event(eid)
    t0, t1 = (s["ts"] for s in e["detections"]["samples"])
    got = plate.gather(e, 1, min_px=50, decode=lambda e, t: ({t0: blurry, t1: sharp}, 400), height=lambda e: 400)
    img = cv2.imdecode(np.frombuffer(got["crops"][0], np.uint8), cv2.IMREAD_GRAYSCALE)
    assert plate.sharpness(img) > plate.sharpness(cv2.cvtColor(blurry, cv2.COLOR_BGR2GRAY))


def make_clip(path: Path, w=640, h=480, n=30, fps=10):
    c = av.open(str(path), "w")
    s = c.add_stream("libx264", rate=fps)
    s.width, s.height, s.pix_fmt = w, h, "yuv420p"
    for i in range(n):
        img = np.full((h, w, 3), 40, np.uint8)
        cv2.rectangle(img, (100 + i * 4, 150), (300 + i * 4, 350), (255, 255, 255), -1)
        cv2.putText(img, f"{i:02d}", (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
        for p in s.encode(av.VideoFrame.from_ndarray(img, format="bgr24")):
            c.mux(p)
    for p in s.encode():
        c.mux(p)
    c.close()


def test_full_resolution_frames_from_the_clip():
    eid = vehicle(boxes=((0.15, 0.3, 0.5, 0.75), (0.2, 0.3, 0.55, 0.75)))
    e = db.event(eid)
    d = settings.data_dir / "events" / str(eid)
    d.mkdir(parents=True, exist_ok=True)
    make_clip(d / "clip.mp4")
    clip_start = e["detections"]["samples"][0]["ts"] - 1.0   # the samples are 1 s and 2 s into the clip
    db.update_event(eid, clip=f"events/{eid}/clip.mp4", clip_start=clip_start)
    e = db.event(eid)
    assert frames.video_height(d / "clip.mp4") == 480
    got = frames.frames_at(d / "clip.mp4", [1.0, 2.0, 99.0])
    assert set(got) == {1.0, 2.0} and got[1.0].shape == (480, 640, 3)   # full resolution; past the end: left out
    imgs, h = plate._decode(e, [s["ts"] for s in e["detections"]["samples"]])
    assert h == 480 and len(imgs) == 2
    out = plate.gather(e, 1, min_px=120)
    assert len(out["crops"]) == 1 and out["px_h"] == 216
    assert plate.gather(e, 1, min_px=400)["reason"] == "too small: 216 px < 400 px"
    # no clip: the recording (one seek in its segment)
    rec = settings.recordings_dir / "cam1"
    rec.mkdir(parents=True, exist_ok=True)
    seg_start, name = clip_start, "segment.mp4"
    make_clip(rec / name)
    old = frames.find_segment
    frames.find_segment = lambda cam, t: (rec / name, seg_start, False)
    try:
        db.update_event(eid, clip=None)
        imgs, h = plate._decode(db.event(eid), [s["ts"] for s in e["detections"]["samples"]])
        assert h == 480 and len(imgs) == 2
    finally:
        frames.find_segment = old


# ---------------------------------------------------------------- with the synopsis

def keyframes(eid):
    d = settings.data_dir / "events" / str(eid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "wide.jpg").write_bytes(cv2.imencode(".jpg", np.zeros((8, 8, 3), np.uint8))[1].tobytes())
    e = db.event(eid)
    db.update_event(eid, detections={**e["detections"], "keyframes": [{"file": "wide.jpg", "ts": e["start_ts"], "kind": "wide"}]})


def test_the_synopsis_reads_a_vehicles_plate_and_only_a_vehicles():
    from nvr import weaponcheck
    p = Pipeline()
    crops_asked = []

    async def crops(e, n):
        crops_asked.append((e["id"], n))
        return {"crops": [b"\xff\xd8crop"], "px_h": 300, "reason": None}
    p.plate_crops = crops

    async def fake_synopsis(event, camera, images, examples):
        return {"summary": "A white pickup, described as likely a Nissan Titan, parks by the gate.", "threat_level": "none",
                "tags": ["pickup"], "objects": []}

    async def review(e, result, evidence):
        return result, None
    old = (vlm.synopsis, weaponcheck.review)
    vlm.synopsis, weaponcheck.review = fake_synopsis, review
    try:
        car = vehicle()
        keyframes(car)
        FAKE.calls.clear()
        METERED.clear()
        FAKE.answers = [{"legible": True, "plate": "7ABC-123", "region": "California", "confidence": "medium"}]
        with aischedule.job("catchup"):   # a caught-up description: its plate is metered as "plate" all the same
            asyncio.run(p._synopsis(car))
        e = db.event(car)
        assert e["synopsis"].startswith("A white pickup") and e["plate"] == "7ABC123" and e["plate_region"] == "CA"
        assert crops_asked == [(car, plate.SYNOPSIS_CROPS)] and FAKE.calls[-1][0] == "plate" and FAKE.calls[-1][3] == "background"
        assert ("plate", aischedule.full()) in METERED and e["detections"]["plate_read"]["source"] == "synopsis"
        assert db.search("7ABC123", None)[0]["id"] == car
        # a person: no plate question
        crops_asked.clear()
        person = vehicle(cls="person")
        keyframes(person)
        asyncio.run(p._synopsis(person))
        assert crops_asked == [] and db.event(person)["synopsis"]
        # the setting off: none either
        settings.plate_read = False
        try:
            car2 = vehicle()
            keyframes(car2)
            asyncio.run(p._synopsis(car2))
            assert crops_asked == [] and db.event(car2)["plate"] is None
        finally:
            settings.plate_read = True
        # a failing plate question never fails the description
        car3 = vehicle()
        keyframes(car3)
        FAKE.answers = [RuntimeError("model down")]
        asyncio.run(p._synopsis(car3))
        assert db.event(car3)["synopsis"] and db.event(car3)["error"] is None and db.event(car3)["plate"] is None
    finally:
        vlm.synopsis, weaponcheck.review = old


# ---------------------------------------------------------------- "Read plate"

def call(method, path, role=None, user="op@example"):
    async def go():
        tunnel = role is not None
        transport = httpx.ASGITransport(app=hub_agent.as_tunnel(api.app) if tunnel else api.app,
                                        client=hub_agent.IN_PROCESS_CLIENT if tunnel else LAN)
        headers = {"x-hub-role": role, "x-hub-user": user} if tunnel else {}
        async with httpx.AsyncClient(transport=transport, base_url="http://site", timeout=30) as c:
            return await c.request(method, path, headers=headers)
    return asyncio.run(go())


def test_read_plate_on_demand():
    p = Pipeline()
    p.vlm_ready = True
    api.state.pipeline = p
    asked = []

    async def crops(e, n):
        asked.append(n)
        return {"crops": [b"\xff\xd8a", b"\xff\xd8b", b"\xff\xd8c"][:n], "px_h": 420, "reason": None}
    p.plate_crops = crops
    old_full = aischedule.full
    aischedule.full = lambda now=None: False   # outside the Site's AI hours: a person asked, so it runs
    try:
        car = vehicle()
        FAKE.calls.clear()
        METERED.clear()
        FAKE.answers = [{"legible": False, "plate": "unreadable", "region": "", "confidence": "low"},
                        {"legible": True, "plate": "ABC 1234", "region": "TX", "confidence": "medium"},
                        {"legible": True, "plate": "ABC 1284", "region": "TX", "confidence": "high"}]
        r = call("POST", f"/api/events/{car}/plate")
        assert r.status_code == 200, r.text
        j = r.json()
        assert asked == [plate.ON_DEMAND_CROPS] and len(FAKE.calls) == 3 and all(c[3] == "chat" for c in FAKE.calls)
        assert j["result"] == "read" and j["plate"] == "ABC1284" and j["confidence"] == "high" and j["region"] == "TX"
        assert j["event"]["plate"] == "ABC1284" and j["event"]["detections"]["plate_read"]["source"] == "user"
        assert METERED == [("plate", False)] * 3          # metered as plate reads, outside the schedule
        # found by search, including a partial plate and the words people use
        for q in ("plate ABC1284", "abc-1284", "1284", "ABC", "tag number", "license plate"):
            assert car in [x["id"] for x in db.search(q, None)], q
        # one run per event every plate_rate_s
        r = call("POST", f"/api/events/{car}/plate")
        assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
        plate.limiter.last[car] = time.time() - settings.plate_rate_s - 1
        FAKE.answers = [{"legible": True, "plate": "ABC 1284", "region": "TX", "confidence": "high"}]
        FAKE.calls.clear()
        r = call("POST", f"/api/events/{car}/plate")
        assert r.status_code == 200 and len(FAKE.calls) == 1    # a high-confidence read on the first crop: no more questions
        # nothing legible: "Not readable", no plate stored
        car2 = vehicle()
        FAKE.answers = []
        r = call("POST", f"/api/events/{car2}/plate")
        assert r.status_code == 200 and r.json()["result"] == "unreadable" and r.json()["plate"] is None
        assert db.event(car2)["plate"] is None
        # too small: skipped without a question
        async def small(e, n):
            return {"crops": [], "px_h": 40, "reason": "too small: 40 px < 120 px"}
        p.plate_crops = small
        car3 = vehicle()
        FAKE.calls.clear()
        r = call("POST", f"/api/events/{car3}/plate")
        assert r.status_code == 200 and r.json()["result"] == "skipped" and r.json()["reason"].startswith("too small") and not FAKE.calls
        # people have no plates; unknown events; Qwen not ready
        person = vehicle(cls="person")
        assert call("POST", f"/api/events/{person}/plate").status_code == 400
        assert call("POST", "/api/events/999999/plate").status_code == 404
        p.vlm_ready = False
        assert call("POST", f"/api/events/{vehicle()}/plate").status_code == 503
        p.vlm_ready = True
        # through the hub: an operator's act, a viewer's never (refused before anything is read)
        p.plate_crops = crops
        car4 = vehicle()
        asked.clear()
        assert call("POST", f"/api/events/{car4}/plate", role="viewer").status_code == 403 and asked == []
        FAKE.answers = [{"legible": True, "plate": "XYZ987", "region": "", "confidence": "high"}]
        r = call("POST", f"/api/events/{car4}/plate", role="operator")
        assert r.status_code == 200 and r.json()["plate"] == "XYZ987" and r.json()["region"] is None
    finally:
        aischedule.full = old_full


def test_list_carries_the_plate():
    car = vehicle()
    plate.store(car, plate.parse({"legible": True, "plate": "LST 100", "region": "OK", "confidence": "high"}), "user")
    api.state.pipeline = Pipeline()
    r = call("GET", "/api/events?label=vehicle&limit=500")
    row = next(x for x in r.json() if x["id"] == car)
    assert (row["plate"], row["plate_region"], row["plate_conf"]) == ("LST100", "OK", "high")


# ---------------------------------------------------------------- Ask: class intent, evidence, references

def test_class_intent():
    vehicle_qs = ["white nissan tag number?", "What was the plate on the truck?", "any cars today", "licence plate of the van",
                  "Did a Ford F-150 come by?", "silverado at the gate", "the white SUV's license", "who parked the Tacoma"]
    person_qs = ["Did the cleaning lady come today?", "anyone at the door last night", "how many people came in",
                 "a man in a red jacket", "were there customers in the lobby"]
    mixed = ["a man next to the truck", "what happened overnight?", "anything unusual today?", "show me the gate",
             "the driver of the pickup and the woman with him", "people near the cars"]
    for q in vehicle_qs:
        assert assistant.class_intent(q) == "vehicle", q
    for q in person_qs:
        assert assistant.class_intent(q) == "person", q
    for q in mixed:
        assert assistant.class_intent(q) is None, q
    assert assistant.query_label("white nissan tag number?") == "vehicle"


def test_evidence_lines_and_class_filter():
    car = vehicle(synopsis="A white pickup, possibly a Nissan Titan, parked by the gate.")
    plate.store(car, plate.parse({"legible": True, "plate": "TTN 404", "region": "TX", "confidence": "high"}), "user")
    line = assistant._event_line({**db.event(car), "plate_region": "TX"})
    assert "license plate TTN404 (TX), high confidence" in line
    it = retrieve.event_item(db.event(car))
    assert it["plate"] == "TTN404" and it["plate_region"] == "TX" and "license plate TTN404" in it["text"]
    assert "plate" not in retrieve.event_item(db.event(vehicle()))
    items = [{"kind": "event", "label": "person", "event_id": 1}, {"kind": "event", "label": "vehicle", "event_id": 2},
             {"kind": "journey", "event_id": 3}, {"kind": "footage", "ts": 1}, {"kind": "count", "text": ""},
             {"kind": "event", "label": "motion", "event_id": 4}]
    ids = lambda xs: [x.get("event_id", x["kind"]) for x in xs]  # noqa: E731
    assert ids(retrieve.keep_class(items, "vehicle")) == [2, "footage", "count", 4]
    assert ids(retrieve.keep_class(items, "person")) == [1, 3, "footage", "count", 4]
    assert retrieve.keep_class(items, None) == items
    assert "described as likely a Nissan Titan" in assistant.ANSWER_SYSTEM and "never guess or complete a plate" in assistant.ANSWER_SYSTEM
    # the Ask's citation refs carry the plate for the answer's chips
    refs = assistant.Refs()
    refs.event(db.event(car))
    assert refs.as_dict()["events"][str(car)]["plate"] == "TTN404"


def test_retrieve_leaves_the_other_class_out():
    db.index_event_text(vehicle(synopsis="A white Nissan pickup at the gate."), "A white Nissan pickup at the gate.", "vehicle", None)
    for i in range(3):
        pid = vehicle(cls="person", synopsis=f"A man in white walks to the gate {i}.")
        db.index_event_text(pid, f"A man in white walks to the gate {i}.", "person white", None)

    async def planner(task, system, text, images, schema, num_predict, temperature, priority):
        return {"calls": [{"tool": "search_events", "text": "white nissan"}], "_model": "fake-planner"}
    old = vlmroute.router.chat_json
    vlmroute.router.chat_json = planner
    try:
        out = asyncio.run(retrieve.retrieve("white nissan tag number?", now=time.time()))
    finally:
        vlmroute.router.chat_json = old
    events = [x for x in out["results"] if x["kind"] == "event"]
    assert events and all(x["label"] == "vehicle" for x in events)
    assert all(c["args"].get("label") == "vehicle" for c in out["calls"] if c["tool"] == "search_events")


def test_merge_keeps_a_vehicles_plate():
    a, b = db.event(vehicle()), db.event(vehicle())
    db.update_event(b["id"], plate="KEEP1", plate_raw="KEEP 1", plate_region="TX", plate_conf="high")
    assert merge._plate_of(a, db.event(b["id"])) == {"plate": "KEEP1", "plate_raw": "KEEP 1", "plate_region": "TX", "plate_conf": "high"}
    a2 = {**a, "plate": "MINE1", "plate_conf": "high"}
    assert merge._plate_of(a2, db.event(b["id"])) == {}
    assert merge._plate_of({**a, "plate": "MINE1", "plate_conf": "medium"}, db.event(b["id"]))["plate"] == "KEEP1"
    merge.apply(a, db.event(b["id"]), "test")
    assert db.event(a["id"])["plate"] == "KEEP1" and db.event(b["id"]) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
