"""Ask-the-NVR plan checking and lookups on a throwaway database (no Qwen needed).

Run: ..\\.venv\\Scripts\\python.exe tests\\test_assistant.py   (from backend/)
"""
import asyncio
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-assist-test-")        # never the real DB
os.environ["NVR_RECORDINGS_DIR"] = tempfile.mkdtemp(prefix="nvr-assist-rec-")   # never the real recordings
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import assistant  # noqa: E402
from nvr.db import db  # noqa: E402

CAM = {"host": "127.0.0.1", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": "", "main_path": "/m",
       "sub_path": "/s", "enabled": 1, "zones": [], "retention_days": None, "scene_notes": "", "retention_policy": None}
NOW = dt.datetime.now().replace(hour=15, minute=0, second=0, microsecond=0).timestamp()


def at(h, m=0, days_ago=0):
    return (dt.datetime.fromtimestamp(NOW) - dt.timedelta(days=days_ago)).replace(hour=h, minute=m).timestamp()


def add(cam, ts, cls="person", **kw):
    f = {"camera_id": cam, "track_id": "t", "camera_class": cls, "start_ts": ts, "end_ts": ts + 10, "status": "verified",
         "created_at": ts, "synopsis": f"a {cls} on {cam}", **kw}
    return db.execute_insert(f"INSERT INTO events ({','.join(f)}) VALUES ({','.join('?' * len(f))})", list(f.values()))


def setup_module(_=None):
    db.upsert_camera({"id": "cam1", "name": "Side Yard", **CAM})
    db.upsert_camera({"id": "cam2", "name": "East Door Exit & Bathrooms", **CAM})
    add("cam1", at(9)); add("cam1", at(9, 30)); add("cam1", at(13), cls="vehicle")
    add("cam2", at(13, 10)); add("cam2", at(14), priority="medium", anomaly=0.93,
                                  anomaly_json=json.dumps({"score": 0.93, "reasons": ["Stayed 9 min, longer than 97% of visits"]}))
    add("cam2", at(22, days_ago=1), feedback=json.dumps({"verdict": "false_alarm"}))


def test_match_camera():
    m = assistant.match_camera
    assert m("cam2") == "cam2" and m("Side Yard") == "cam1" and m("side yard") == "cam1"
    assert m("the east door") == "cam2" and m("bathrooms") == "cam2" and m("Side yrd") == "cam1"
    assert m("") is None and m("all cameras") is None and m("garage") is None


def test_parse_time():
    t = assistant.parse_time("2026-09-24 18:00", NOW)
    assert dt.datetime.fromtimestamp(t).hour == 18
    assert dt.datetime.fromtimestamp(assistant.parse_time("06:30", NOW)).minute == 30
    assert assistant.parse_time("yesterday-ish", NOW) is None and assistant.parse_time("", NOW) is None


def test_check_plan():
    day = dt.datetime.fromtimestamp(NOW).strftime("%Y-%m-%d")
    raw = {"calls": [
        {"tool": "search_events", "text": "person", "camera": "east door", "since": f"{day} 18:00", "until": f"{day} 12:00"},
        {"tool": "delete_everything"},                                   # unknown: dropped
        {"tool": "search_footage", "camera": "Side Yard"},               # no text: uses the question
        {"tool": "count_events", "label": "person", "group_by": "hour", "min_priority": "bogus"},
    ]}
    calls = assistant.check_plan(raw, "white truck in the yard", NOW)
    assert [c["tool"] for c in calls] == ["search_events", "search_footage", "count_events"], calls
    a = calls[0]["args"]
    assert a["camera"] == "cam2" and a["since"] < a["until"], a                   # swapped times fixed
    assert calls[1]["args"]["text"] == "white truck in the yard" and calls[1]["args"]["camera"] == "cam1"
    assert calls[2]["args"]["group_by"] == "hour" and calls[2]["args"]["min_priority"] is None
    fallback = assistant.check_plan({"calls": []}, "anything odd?", NOW)
    assert fallback[0]["tool"] == "search_events" and fallback[0]["args"]["text"] == "anything odd?"
    future = assistant.check_plan({"calls": [{"tool": "count_events", "until": "2099-01-01 00:00"}]}, "q", NOW)
    assert future[0]["args"]["until"] <= NOW + 60


def test_keyword_rules_add_lookups():
    plan = assistant.check_plan({"calls": [{"tool": "search_events", "camera": "cam2"}]}, "When did someone go outside?", NOW)
    assert [c["tool"] for c in plan] == ["search_events", "list_journeys"], plan
    assert plan[1]["args"]["camera"] is None                        # journeys span cameras
    plan = assistant.check_plan({"calls": []}, "Anything unusual or was a camera offline?", NOW)
    assert {"list_unusual", "recording_gaps"} <= {c["tool"] for c in plan}, plan
    plan = assistant.check_plan({"calls": [{"tool": "count_events"}]}, "How many cars?", NOW)
    assert [c["tool"] for c in plan] == ["count_events"], plan      # not added twice


def test_object_questions_search_footage():
    assert assistant.footage_text("Was a boat on any camera today?") == "a boat"
    assert assistant.footage_text("Did anyone see a ladder in the side yard last night?") == "a ladder in the side yard"
    plan = assistant.check_plan({"calls": [{"tool": "search_events", "min_priority": "low"}]}, "Was a boat on any camera today?", NOW)
    assert plan[-1]["tool"] == "search_footage" and plan[-1]["args"]["text"] == "a boat", plan
    plan = assistant.check_plan({"calls": [{"tool": "search_events", "camera": "cam1"}]}, "Was anyone in the side yard today?", NOW)
    assert "search_footage" not in {c["tool"] for c in plan}, plan   # people questions: events answer them


def test_last_n_window_and_no_stray_footage_search():
    plan = assistant.check_plan({"calls": [{"tool": "recording_gaps", "since": "2020-01-01 18:00", "until": "2020-01-02 07:00"}]},
                                "Was any camera offline in the last 24 hours?", NOW)
    assert all(abs(c["args"]["since"] - (NOW - 86400)) < 5 and c["args"]["until"] is None for c in plan), plan
    assert "search_footage" not in {c["tool"] for c in plan}, plan
    plan = assistant.check_plan({"calls": [{"tool": "count_events"}]}, "how many vehicles in the past two days", NOW)
    assert abs(plan[0]["args"]["since"] - (NOW - 2 * 86400)) < 5


def test_average_link_counts():
    import numpy as np
    # two tight groups (0.9 inside) that are 0.6 apart, plus one loner at 0.4
    S = np.array([[1, .9, .9, .6, .6, .4], [.9, 1, .9, .6, .6, .4], [.9, .9, 1, .6, .6, .4],
                  [.6, .6, .6, 1, .9, .4], [.6, .6, .6, .9, 1, .4], [.4, .4, .4, .4, .4, 1]])
    assert assistant._average_link_counts(S, [0.8, 0.5]) == {0.8: 3, 0.5: 2}


def run(tool, **args):
    base = {"text": "", "camera": None, "since": None, "until": None, "label": None, "min_priority": None, "group_by": None}
    refs = assistant.Refs()
    lines, n = asyncio.run(assistant.TOOL_FUNCS[tool]({**base, **args}, refs))
    return lines, n, refs


def test_count_events():
    lines, n, _ = run("count_events", since=at(0))
    assert n == 5 and lines[0].startswith("Sightings: 5"), lines                   # false alarm excluded
    lines, n, _ = run("count_events", since=at(0), camera="cam1", group_by="label")
    assert n == 3 and "person: 2 person sightings" in lines and "vehicle: 1 vehicle sightings" in lines, lines
    lines, n, _ = run("count_events", since=at(12), until=at(18), label="person", group_by="camera")
    assert n == 2 and any(l.startswith("East Door Exit & Bathrooms: 2 person sightings") for l in lines), lines


def test_list_unusual_and_refs():
    lines, n, refs = run("list_unusual", since=at(0))
    assert n == 1 and "Stayed 9 min" in lines[0] and lines[0].startswith("[#"), lines
    eid = int(lines[0][2:lines[0].index("]")])
    assert refs.as_dict()["events"][str(eid)]["camera"] == "East Door Exit & Bathrooms"


def test_list_events_by_filter():
    lines, n, _ = run("search_events", camera="cam2", min_priority="medium")
    assert n == 1 and "priority medium" in lines[0], lines


def test_recording_gaps_no_recordings():
    lines, _, _ = run("recording_gaps", camera="cam1", since=at(0))
    assert lines == ["Side Yard: no recordings found"], lines


if __name__ == "__main__":
    setup_module()
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
