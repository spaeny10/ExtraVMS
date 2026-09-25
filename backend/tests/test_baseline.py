"""Baseline / unusualness / priority tests on a throwaway database.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_baseline.py   (from backend/; also works under pytest)
"""
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import baseline  # noqa: E402
from nvr.db import db  # noqa: E402

CAM = {"name": "Test", "host": "127.0.0.1", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": "",
       "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": [], "retention_days": None,
       "scene_notes": "", "retention_policy": None}
NORMAL_PATH = [[0, 0.10, 0.60, 0.20, 0.90, 0.9], [0, 0.30, 0.60, 0.40, 0.92, 0.9]]  # bottom-left walkway
ODD_PATH = [[0, 0.85, 0.05, 0.95, 0.20, 0.9]]                                     # top-right corner


def add(cam, ts, dur=20, path=NORMAL_PATH, status="verified", **kw):
    fields = {"camera_id": cam, "track_id": "t", "camera_class": "person", "start_ts": ts, "end_ts": ts + dur,
              "path": json.dumps(path), "status": status, "created_at": ts, **kw}
    db.execute(f"INSERT INTO events ({','.join(fields)}) VALUES ({','.join('?' * len(fields))})", list(fields.values()))
    return db.one("SELECT last_insert_rowid() AS id")["id"]


def setup_module(_=None):
    for cam in ("busy", "new"):
        db.upsert_camera({"id": cam, **CAM})
    # 21 days of history on "busy": people every weekday and weekend 08:00-17:00, 3 per hour, on the walkway
    global NOW
    today = dt.datetime.now().replace(hour=20, minute=0, second=0, microsecond=0)
    NOW = today.timestamp()
    db.execute("BEGIN")
    for d in range(21, 0, -1):
        day = today - dt.timedelta(days=d)
        for h in range(8, 17):
            for m in (5, 25, 45):
                add("busy", day.replace(hour=h, minute=m).timestamp())
    db.execute("COMMIT")
    # "new" camera: only 3 days
    for d in (3, 2, 1):
        for h in (9, 10, 11):
            add("new", (today - dt.timedelta(days=d)).replace(hour=h).timestamp())
    baseline.rebuild(NOW)


def event(cam, when, dur=20, path=NORMAL_PATH, **kw):
    return {"camera_id": cam, "camera_class": "person", "status": "verified", "start_ts": when.timestamp(),
            "end_ts": when.timestamp() + dur, "path": path, **kw}


def today_at(h, m=0):
    return dt.datetime.fromtimestamp(NOW).replace(hour=h, minute=m)


def test_time_of_day():
    night = baseline.score(event("busy", today_at(3)))
    noon = baseline.score(event("busy", today_at(12, 10)))
    assert night["parts"]["time"] >= 0.9, night
    assert any("3 am" in r for r in night["reasons"]), night["reasons"]
    assert noon["parts"]["time"] < 0.3, noon
    assert not noon["reasons"], noon["reasons"]


def test_place_and_dwell():
    odd = baseline.score(event("busy", today_at(12, 10), path=ODD_PATH))
    assert odd["parts"]["place"] >= 0.9 and any("rarely go" in r for r in odd["reasons"]), odd
    long = baseline.score(event("busy", today_at(12, 10), dur=600))
    assert long["parts"]["dwell"] >= 0.95 and any("Stayed 10 min" in r for r in long["reasons"]), long
    short = baseline.score(event("busy", today_at(12, 10), dur=30))
    assert "dwell" not in short["parts"], short


def test_learning():
    s = baseline.score(event("new", today_at(3)))
    assert s["learning"] and s["score"] == 0 and not s["reasons"], s
    st = {c["camera_id"]: c for c in baseline.status()}
    assert st["new"]["learning"] and not st["busy"]["learning"], st


def test_self_excluded():
    """A stored event scored against a baseline that contains it must not count itself as 'normal'."""
    when = (dt.datetime.fromtimestamp(NOW) - dt.timedelta(days=2)).replace(hour=3)
    eid = add("busy", when.timestamp())
    baseline.rebuild(NOW)
    e = db.event(eid)
    s = baseline.score(e)
    assert s["parts"]["time"] >= 0.9, s
    db.execute("DELETE FROM events WHERE id=?", [eid])
    baseline.rebuild(NOW)


def test_priority():
    p = baseline.priority
    assert p({"status": "verified", "threat": "low"}, 0.95) == "medium"      # unusualness raises it
    assert p({"status": "verified", "threat": "high"}, 0.1) == "high"        # never lowers Qwen's threat
    assert p({"status": "verified", "threat": None}, 0.8) == "low"           # vehicles/unrated get a level
    assert p({"status": "verified", "threat": None}, 0.2) == "none"
    assert p({"status": "verified", "threat": "none", "corrected_at": 1.0}, 0.99) == "none"  # operator wins
    assert p({"status": "verified", "threat": "medium", "feedback": {"verdict": "false_alarm"}}, 0.99) == "none"
    assert p({"status": "rejected", "threat": None}, 0.99) is None


def test_apply_stores_fields():
    eid = add("busy", today_at(3).timestamp() - 86400 * 0.0 + 60)
    a = baseline.apply(eid)
    e = db.event(eid)
    assert e["anomaly"] == a["score"] and e["anomaly_json"]["reasons"] and e["priority"] in ("low", "medium"), e


if __name__ == "__main__":
    setup_module()
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
