"""Find filters (db.event_filters / enrich / summary, /api/events, /api/search, /api/events/summary, saved views).

Run: ..\\.venv\\Scripts\\python.exe tests\\test_find_filters.py   (from backend/)
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-find-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: E402

from nvr import api, mediamtx  # noqa: E402
from nvr.db import db, event_filters  # noqa: E402
from nvr.pipeline import Pipeline  # noqa: E402

NOW = time.time() - 3600
IDS: dict[str, int] = {}


async def _no_spans(*_a, **_k):
    return []


def setup():
    if IDS:
        return
    mediamtx.recording_spans = _no_spans
    api.state.pipeline = Pipeline()  # list_events / search annotate rows with live pipeline state
    for cid in ("cam1", "cam2"):
        db.upsert_camera({"id": cid, "name": cid.upper(), "host": "127.0.0.1"})
    jid = db.execute_insert("INSERT INTO journeys (first_ts, last_ts, cameras, updated_at) VALUES (?,?,?,?)",
                            [NOW, NOW + 60, '["cam1","cam2"]', NOW])
    base = dict(camera_id="cam1", camera_class="person", camera_conf=0.9, yolo_conf=0.8, status="verified", path=[])

    def ev(key, i, **kw):
        IDS[key] = db.create_event(**{**base, "track_id": key, "start_ts": NOW + i, "end_ts": NOW + i + 5, **kw})

    ev("plain", 0, priority="none", synopsis="a person walks past the polo shirt rack")
    ev("ppe", 1, priority="medium", synopsis="worker without a hard hat",
       policy={"kind": "ppe", "priority": "medium", "text": "No hard hat in PPE zone 'Side Yard' (12 s)"},
       detections={"ppe": {"zone": "Side Yard", "verdict": "violation", "violation": ["hard_hat"]}})
    ev("tow", 2, camera_id="cam2", camera_class="vehicle", priority="high",
       policy={"kind": "towing", "priority": "high", "text": "Trailer moved by an unknown truck"})
    ev("unusual", 3, priority="low", anomaly=0.9, anomaly_json={"score": 0.9, "reasons": ["stayed 1 min"], "parts": {}, "learning": False})
    ev("watched", 4, priority="none", watched="Bob")
    ev("journey", 5, priority="none", journey_id=jid, synopsis="person in a polo shirt by the gate",
       areas=[{"name": "Yard", "from": 0, "to": 3}])
    ev("locked", 6, priority="none")
    db.execute("INSERT INTO locks (camera_id, start_ts, end_ts, event_id, created_at) VALUES (?,?,?,?,?)",
               ["cam1", NOW, NOW + 10, IDS["locked"], NOW])
    ev("corrected", 7, priority="none", corrected_at=NOW)
    ev("false_alarm", 8, priority="none", feedback={"verdict": "false_alarm"})
    ev("rejected", 9, status="rejected", yolo_conf=None)
    for key in ("plain", "journey"):
        e = db.event(IDS[key])
        db.index_event_text(e["id"], e["synopsis"], "person", None)


def browse(**kw):
    kw.setdefault("min_yolo", 0)
    kw.setdefault("limit", 50)
    kw.setdefault("since", NOW - 1)
    return asyncio.run(api.list_events(**kw))


def ids(rows):
    return {r["id"] for r in rows}


def test_each_flag():
    setup()
    expect = {"rule": {"ppe", "tow"}, "ppe": {"ppe"}, "unusual": {"unusual"}, "watched": {"watched"},
              "multicam": {"journey"}, "locked": {"locked"}, "corrected": {"corrected"}, "false_alarm": {"false_alarm"}}
    for flag, keys in expect.items():
        assert ids(browse(flags=flag)) == {IDS[k] for k in keys}, flag
    assert ids(browse(flags="rule,ppe")) == {IDS["ppe"]}            # flags AND together
    assert ids(browse(flags=" ppe , ppe ")) == {IDS["ppe"]}         # whitespace and duplicates are fine
    try:
        browse(flags="nope")
        raise AssertionError("unknown flag accepted")
    except HTTPException as ex:
        assert ex.status_code == 400


def test_live_feed_filters():
    """Live's "Latest activity" pills fetch the latest matches: one class as label=, PPE violations as flags=ppe."""
    setup()
    live = {"status": "open,pending,verified"}
    assert ids(browse(label="vehicle", **live)) == {IDS["tow"]}
    assert IDS["tow"] not in ids(browse(label="person", **live)) and IDS["rejected"] not in ids(browse(label="person", **live))
    assert ids(browse(flags="ppe", **live)) == {IDS["ppe"]}
    assert ids(browse(flags="ppe", label="person", **live)) == {IDS["ppe"]}
    assert browse(flags="ppe", label="vehicle", **live) == []


def test_priority_and_sort():
    setup()
    assert ids(browse(priority="medium")) == {IDS["ppe"], IDS["tow"]}
    assert ids(browse(priority="low")) == {IDS["ppe"], IDS["tow"], IDS["unusual"]}
    assert len(browse(priority="none")) == len(browse())             # "none" = any
    rows = browse(sort="priority", status="verified")
    assert [r["id"] for r in rows[:3]] == [IDS["tow"], IDS["ppe"], IDS["unusual"]]
    assert rows[3]["id"] == IDS["false_alarm"]                      # then newest first
    assert [r["id"] for r in browse(sort="priority", limit=2, offset=1)] == [IDS["ppe"], IDS["unusual"]]
    try:
        browse(priority="urgent")
        raise AssertionError("unknown priority accepted")
    except HTTPException as ex:
        assert ex.status_code == 400


def test_sort_oldest():
    """sort=oldest: earliest start first (ties by id), for the event viewer's Next beyond its strip (since=, limit=);
    the other sorts are unchanged. cam3's events are before NOW - 1, outside the other tests' since."""
    setup()
    t0 = NOW - 1000
    base = dict(camera_id="cam3", camera_class="person", camera_conf=0.9, yolo_conf=0.8, status="verified", path=[])
    db.upsert_camera({"id": "cam3", "name": "CAM3", "host": "127.0.0.1"})
    late = db.create_event(**base, track_id="o1", start_ts=t0 + 50, end_ts=t0 + 55)
    early = db.create_event(**base, track_id="o2", start_ts=t0 + 10, end_ts=t0 + 15)    # created later, starts earlier
    tie = db.create_event(**base, track_id="o3", start_ts=t0 + 10, end_ts=t0 + 12)      # same start: by id
    masked = db.create_event(**{**base, "status": "masked"}, track_id="o4", start_ts=t0 + 5, end_ts=t0 + 6)
    q = dict(camera="cam3", since=t0)
    assert [r["id"] for r in browse(sort="oldest", **q)] == [masked, early, tie, late]
    assert [r["id"] for r in browse(sort="newest", **q)] == [masked, tie, early, late]          # newest = by id, as before
    assert [r["id"] for r in browse(sort="oldest", status="open,pending,verified,rejected,error", limit=2, **q)] == [early, tie]
    assert [r["id"] for r in browse(sort="oldest", camera="cam3", since=t0 + 10.001, limit=2)] == [late]
    assert browse(sort="oldest", camera="cam3", since=t0 + 50.001) == []
    # through the route: "oldest" passes the query validation; an unknown sort is still refused
    import httpx

    async def get(qs):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app, client=("192.0.2.9", 5000)),
                                     base_url="http://127.0.0.1:8000") as c:
            return await c.get(f"/api/events?{qs}")
    r = asyncio.run(get(f"camera=cam3&since={t0 + 10.001}&limit=1&sort=oldest"))
    assert r.status_code == 200 and [x["id"] for x in r.json()] == [late], r.text
    assert asyncio.run(get("camera=cam3&sort=sideways")).status_code == 422


def test_place_zone_attention_and_card_fields():
    setup()
    assert ids(browse(place="Yard")) == {IDS["journey"]}
    assert browse(place="Nowhere") == []
    assert ids(browse(ppe_zone="Side Yard")) == {IDS["ppe"]}
    assert ids(browse(attention=True)) == {IDS["ppe"], IDS["tow"], IDS["unusual"], IDS["watched"]}
    j = next(r for r in browse() if r["id"] == IDS["journey"])
    assert j["journey_cameras"] == 2 and j["areas"][0]["name"] == "Yard"
    u = next(r for r in browse() if r["id"] == IDS["unusual"])
    assert u["anomaly_json"]["reasons"] == ["stayed 1 min"]
    assert next(r for r in browse() if r["id"] == IDS["locked"])["locked"] == 1
    # min_yolo in browse keeps unverified events, in search it doesn't
    w, _ = event_filters(min_yolo=0.5)
    assert "open" in w[0]
    w, _ = event_filters(min_yolo=0.5, keep_unverified=False)
    assert w == ["yolo_conf >= ?"]


def test_summary_counts():
    setup()
    s = asyncio.run(api.events_summary(since=NOW - 1, status="verified"))
    assert s["total"] == 9
    assert {k["kind"]: k["n"] for k in s["by_kind"]} == {"ppe": 1, "towing": 1}
    assert s["by_zone"] == [{"zone": "Side Yard", "n": 1}]
    assert {c["camera_id"]: c["n"] for c in s["by_camera"]} == {"cam1": 8, "cam2": 1}
    assert sum(d["n"] for d in s["by_day"]) == 9 and all(len(d["day"]) == 10 for d in s["by_day"])
    s = asyncio.run(api.events_summary(since=NOW - 1, flags="ppe"))
    assert s["total"] == 1 and s["by_camera"] == [{"camera_id": "cam1", "n": 1}]


def test_search_parity():
    setup()
    rows = asyncio.run(api.search("polo shirt", min_yolo=0, limit=30))
    assert ids(rows) == {IDS["plain"], IDS["journey"]}
    j = next(r for r in rows if r["id"] == IDS["journey"])
    assert j["journey_cameras"] == 2 and j["locked"] == 0 and "synopsis_pending" in j
    assert ids(asyncio.run(api.search("polo shirt", min_yolo=0, limit=30, place="Yard"))) == {IDS["journey"]}
    assert ids(asyncio.run(api.search("polo shirt", min_yolo=0, limit=30, flags="multicam"))) == {IDS["journey"]}
    assert asyncio.run(api.search("polo shirt", min_yolo=0, limit=30, priority="high")) == []
    page1 = asyncio.run(api.search("polo shirt", min_yolo=0, limit=1))
    page2 = asyncio.run(api.search("polo shirt", min_yolo=0, limit=1, offset=1))
    assert len(page1) == len(page2) == 1 and page1[0]["id"] != page2[0]["id"]


def test_saved_views_round_trip():
    setup()
    assert asyncio.run(api.get_find_views()) == {"views": []}
    body = api.FindViewsIn(views=[{"id": "v1", "name": "  PPE this week ", "filters": {"flags": ["ppe"], "hours": 168},
                                   "builtin": True}])
    out = asyncio.run(api.put_find_views(body))
    assert out["views"][0]["name"] == "PPE this week" and out["views"][0]["builtin"] is False
    assert asyncio.run(api.get_find_views())["views"][0]["filters"]["flags"] == ["ppe"]
    try:
        asyncio.run(api.put_find_views(api.FindViewsIn(views=[{"name": " "}])))
        raise AssertionError("nameless view accepted")
    except HTTPException as ex:
        assert ex.status_code == 400


def test_summary_route_before_event_id():
    paths = [getattr(r, "path", "") for r in api.app.routes]
    assert paths.index("/api/events/summary") < paths.index("/api/events/{event_id}")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
