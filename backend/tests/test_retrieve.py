"""POST /api/assistant/retrieve (retrieve.py): the hub's Site Ask gets structured evidence, no answer, no thread.

Fake planner (no Qwen, no Ollama), a fixture DB of a few days, no footage index.
Run: ..\\.venv\\Scripts\\python.exe tests\\test_retrieve.py   (from backend/)
"""
import asyncio
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-retrieve-test-")        # never the real DB
os.environ["NVR_RECORDINGS_DIR"] = tempfile.mkdtemp(prefix="nvr-retrieve-rec-")   # never the real recordings
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import api, assistant, retrieve  # noqa: E402
from nvr import synopsis as vlm  # noqa: E402
from nvr.db import db  # noqa: E402

CAM = {"host": "127.0.0.1", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": "", "main_path": "/m",
       "sub_path": "/s", "enabled": 1, "retention_days": None, "scene_notes": "", "retention_policy": None, "zones": []}
NOW = dt.datetime(2026, 10, 6, 15, 0).timestamp()
IDS: dict = {}


def ts(day, h, m=0):
    return dt.datetime(2026, 10, day, h, m).timestamp()


def add(cam, start, cls, synopsis, **kw):
    f = {"camera_id": cam, "track_id": "t", "camera_class": cls, "start_ts": start, "end_ts": start + 12, "status": "verified",
         "created_at": start, "synopsis": synopsis}
    eid = db.execute_insert(f"INSERT INTO events ({','.join(f)}) VALUES ({','.join('?' * len(f))})", list(f.values()))
    if kw:
        db.update_event(eid, **kw)
    db.index_event_text(eid, synopsis, cls, None)
    return eid


class Planner:
    """vlmroute.router.chat_json stand-in: answers with the next canned plan and remembers the prompts."""

    def __init__(self):
        self.plans: list = []
        self.prompts: list[str] = []

    async def __call__(self, role, system, text, images, schema, num_predict, temperature, priority):
        self.prompts.append(text)
        plan = self.plans.pop(0) if self.plans else {}
        if isinstance(plan, Exception):
            raise plan
        return {**plan, "_model": "fake-planner"}


PLANNER = Planner()


async def no_embed(text):
    return None


def setup_module(_=None):
    db.upsert_camera({"id": "cam1", "name": "Lobby", **CAM})
    db.upsert_camera({"id": "cam2", "name": "Front Gate", **CAM})
    IDS["today"] = add("cam1", ts(6, 10), "person", "A woman with a cleaning cart mops the lobby floor.")
    IDS["yday"] = add("cam1", ts(5, 16, 30), "person", "A woman pushes a cleaning cart down the lobby hallway.")
    IDS["truck"] = add("cam2", ts(6, 2, 10), "vehicle", "A white pickup truck stops at the front gate at night.", priority="high")
    IDS["old_truck"] = add("cam2", ts(1, 11), "vehicle", "A white pickup truck drives through the gate.")
    IDS["night"] = add("cam2", ts(5, 23, 40), "person", "A man walks along the fence line with a flashlight.", priority="medium")
    vlm.embed = no_embed
    assistant.ctx.footage = None
    assistant.vlmroute.router.chat_json = PLANNER


def run(question, history=None):
    return asyncio.run(retrieve.retrieve(question, history or [], NOW))


def events(r):
    return [x for x in r["results"] if x["kind"] == "event"]


def test_cleaning_lady_today_returns_structured_events():
    PLANNER.plans = [{"calls": [{"tool": "search_events", "text": "cleaning lady", "since": "2026-10-06 00:00"}]}]
    r = run("Did the cleaning lady come today?")
    ev = events(r)
    assert [e["event_id"] for e in ev] == [IDS["today"]], ev
    e = ev[0]
    assert e["camera_id"] == "cam1" and e["camera_name"] == "Lobby" and e["ts"] == ts(6, 10) and e["label"] == "person"
    assert e["text"].startswith("person, 12 s") and "cleaning cart" in e["text"] and not e["text"].startswith("[#"), e["text"]
    assert e["snapshot"] is False and e["priority"] == "none" and e["calls"] == [0]
    assert r["window"]["label"] == "today" and r["window"]["from"] == ts(6, 0) and r["window"]["to"] == NOW
    assert r["calls"][0]["tool"] == "search_events" and r["calls"][0]["count"] == 1
    assert r["plan_model"] == "fake-planner" and r["question"] == "Did the cleaning lady come today?"
    assert r["counts"]["found"]["event"] == 1
    # nothing was written: no assistant conversation on the server
    assert db.one("SELECT COUNT(*) AS n FROM assistant_threads")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM assistant_messages")["n"] == 0


def test_follow_up_keeps_the_subject_with_a_new_window():
    history = [{"role": "user", "content": "Did the cleaning lady come today?"},
               {"role": "assistant", "content": "Yes: the cleaning lady was in the Lobby at 10:00 [#1]."}]
    assert retrieve.contextualize("and yesterday?", history, NOW) == "Did the cleaning lady come yesterday?"
    assert retrieve.contextualize("What about last night?", history, NOW) == "Did the cleaning lady come last night?"
    assert retrieve.contextualize("and the white pickup?", history, NOW) == "the white pickup today?"
    assert retrieve.contextualize("What happened overnight?", history, NOW) == "What happened overnight?"   # a new question
    assert retrieve.contextualize("and yesterday?", [], NOW) == "and yesterday?"
    PLANNER.plans = [RuntimeError("model is loading")]   # the rules plan alone still finds it
    PLANNER.prompts.clear()
    r = run("and yesterday?", history)
    assert r["question"] == "Did the cleaning lady come yesterday?" and r["asked"] == "and yesterday?"
    ev = events(r)
    assert [e["event_id"] for e in ev] == [IDS["yday"]], ev
    assert r["window"]["label"] == "yesterday" and r["window"]["from"] == ts(5, 0) and r["window"]["to"] == ts(6, 0)
    assert any("planner was unavailable" in n for n in r["notes"]), r["notes"]
    # the planner saw the conversation
    assert "Earlier in this conversation" in PLANNER.prompts[0] and "cleaning lady come today" in PLANNER.prompts[0]


def test_last_seen_includes_events():
    PLANNER.plans = [{"calls": [{"tool": "search_footage", "text": "white pickup truck"}]}]
    r = run("When was the white pickup truck last seen?")
    ev = events(r)
    ids = [e["event_id"] for e in ev]
    assert IDS["truck"] in ids and IDS["old_truck"] in ids, ids
    assert ids.index(IDS["truck"]) < ids.index(IDS["old_truck"])        # newest first
    truck = next(e for e in ev if e["event_id"] == IDS["truck"])
    assert truck["priority"] == "high" and "priority high" in truck["text"]
    assert r["calls"][0]["tool"] == "search_events" and r["calls"][0]["args"].get("newest") is True
    assert any(n.startswith("Footage search is not available") for n in r["notes"])


def test_period_question_counts_and_lists():
    PLANNER.plans = [{"calls": [{"tool": "get_briefing", "since": "2026-10-05 18:00"}]}]
    real_time = api.time.time
    api.time.time = lambda: NOW   # the route only trusts a client "now" near the server's clock: pin the clock to the fixture day
    try:
        r = asyncio.run(api.assistant_retrieve(api.RetrieveIn(question="What happened overnight?", now=NOW)))
    finally:
        api.time.time = real_time
    assert r["window"]["label"] == "overnight"
    count = next(x for x in r["results"] if x["kind"] == "count")
    assert count["counts"]["events"] == 2 and count["counts"]["by_label"] == {"person": 1, "vehicle": 1}
    assert count["counts"]["by_camera"] == {"cam2": 2} and count["counts"]["camera_names"] == {"cam2": "Front Gate"}
    assert r["counts"]["events"] == 2
    assert {e["event_id"] for e in events(r)} == {IDS["truck"], IDS["night"]}
    tools = [c["tool"] for c in r["calls"]]
    assert tools[:2] == ["search_events", "count_events"] and "get_briefing" in tools, tools


def test_fallback_outside_the_period_is_marked():
    # nothing at the gate this morning; the fallback looks wider and finds the 02:10 truck, before the period
    PLANNER.plans = [{"calls": [{"tool": "search_events", "text": "white pickup truck", "camera": "Front Gate"}]}]
    r = run("Was the white pickup truck here this afternoon?")
    assert r["window"]["label"] == "this afternoon"
    ev = {e["event_id"]: e for e in events(r)}
    assert ev, r
    assert all(e.get("earlier") for e in ev.values()), ev                    # all before 12:00 today
    assert not any(e.get("later") for e in ev.values())


def test_offline_question_reports_gaps_structured():
    PLANNER.plans = [{}]
    r = run("Was any camera offline in the last 24 hours?")
    assert "recording_gaps" in [c["tool"] for c in r["calls"]]
    notes = [x for x in r["results"] if x["kind"] == "note"]
    assert {x.get("camera_id") for x in notes} >= {"cam1", "cam2"} and all(x["text"] == "no recordings found" for x in notes)


def test_planner_timeout_falls_back_to_rules():
    async def slow(*a, **k):
        await asyncio.sleep(5)
    old, retrieve.PLAN_TIMEOUT_S = retrieve.PLAN_TIMEOUT_S, 0.05
    assistant.vlmroute.router.chat_json = slow
    try:
        r = run("Did the cleaning lady come today?")
    finally:
        retrieve.PLAN_TIMEOUT_S = old
        assistant.vlmroute.router.chat_json = PLANNER
    assert [e["event_id"] for e in events(r)] == [IDS["today"]]
    assert any("planner was slow" in n for n in r["notes"]) and r["plan_model"] is None


if __name__ == "__main__":
    setup_module()
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
