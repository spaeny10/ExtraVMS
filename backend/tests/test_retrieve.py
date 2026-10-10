"""POST /api/assistant/retrieve (retrieve.py): the hub's Site Ask gets structured evidence, no answer, no thread.

Fake planner (no Qwen, no Ollama), a fixture DB of a few days, no footage index.
Run: ..\\.venv\\Scripts\\python.exe tests\\test_retrieve.py   (from backend/)
"""
import asyncio
import datetime as dt
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-retrieve-test-")        # never the real DB
os.environ["NVR_RECORDINGS_DIR"] = tempfile.mkdtemp(prefix="nvr-retrieve-rec-")   # never the real recordings
os.environ["NVR_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="nvr-retrieve-run-")
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


def run(question, history=None, tz=None, now=NOW):
    return asyncio.run(retrieve.retrieve(question, history or [], now, tz))


class PinnedTime:
    """The time module with time() fixed, patched into one module (never the real time.time)."""

    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


class FakeClock:
    """retrieve._clock stand-in: stands still unless a test moves it (e.g. the planner "took" 19.5 s)."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


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
    # the route only trusts a client "now" near the server's clock: pin the route's clock (only api's, only here)
    with mock.patch.object(api, "time", PinnedTime(NOW)):
        r = asyncio.run(api.assistant_retrieve(api.RetrieveIn(question="What happened overnight?", now=NOW)))
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


def test_slow_footage_search_never_starves_the_event_search():
    """First question after a restart: a cold footage search used the whole budget and the event search was skipped,
    so the hub answered "no white truck" while 12 events matched. Database lookups now run first and always run."""
    async def slow_footage(a, ev):
        await asyncio.sleep(5)
        return [], 0
    PLANNER.plans = [{"calls": [{"tool": "search_footage", "text": "white truck"}, {"tool": "search_events", "text": "white truck"}]}]
    old_budget, retrieve.BUDGET_S = retrieve.BUDGET_S, 0.3
    old_tool, assistant.TOOL_FUNCS["search_footage"] = assistant.TOOL_FUNCS["search_footage"], slow_footage
    try:
        r = run("White truck?")
    finally:
        retrieve.BUDGET_S = old_budget
        assistant.TOOL_FUNCS["search_footage"] = old_tool
    ids = {e["event_id"] for e in events(r)}
    assert IDS["truck"] in ids and IDS["old_truck"] in ids, r
    assert r["calls"][0]["tool"] == "search_events", r["calls"]          # the fast lookup went first
    assert r["incomplete"] and any("search_footage" in n or "footage" in n.lower() for n in r["incomplete"]), r["incomplete"]


def _planner_taking(seconds, clock, plan):
    """A planner that answers with `plan` after `seconds` on the fake clock."""
    async def plan_after(*a, **k):
        clock.t += seconds
        return {**plan, "_model": "fake-planner"}
    return plan_after


def test_slow_lookups_never_run_past_the_hard_ceiling():
    """The hub drops a server that answers after ~25 s, with ALL its findings. Once the budget was spent every fast
    lookup still got FAST_MIN_S and the fallbacks more, so three slow database lookups ran ~36 s. Now nothing runs past
    HARD_S, and a lookup that ran out of time stops the fallbacks (its emptiness proves nothing)."""
    started = []

    async def slow(a, ev):
        started.append(time.monotonic())
        await asyncio.sleep(5)
        return [], 0
    PLANNER.plans = [{"calls": [{"tool": "count_events"}, {"tool": "list_unusual"}, {"tool": "list_journeys"}]}]
    funcs = {**assistant.TOOL_FUNCS, "count_events": slow, "list_unusual": slow, "list_journeys": slow}
    with mock.patch.multiple(retrieve, BUDGET_S=0.2, HARD_S=0.6, FAST_MIN_S=0.5, MIN_RUN_S=0.05, FALLBACK_MIN_S=0.05), \
            mock.patch.object(assistant, "TOOL_FUNCS", funcs):
        t0 = time.monotonic()
        r = run("Anything unusual?")
        took = time.monotonic() - t0
    assert took < 0.6 + 0.35, took                                        # was 3 x FAST_MIN_S + a fallback
    assert len(started) == 2 and started[1] - t0 < 0.6, started          # the third had no time left
    inc = r["incomplete"]
    assert sum("took too long" in n for n in inc) == 2 and sum("skipped to answer in time" in n for n in inc) == 1, inc
    assert [c["tool"] for c in r["calls"]] == ["count_events", "list_unusual", "list_journeys"], r["calls"]   # no fallback
    assert "incomplete_cameras" not in r                                 # all cameras: none of the lookups named one


def test_out_of_time_text_search_goes_by_keywords():
    """With the budget spent, a text search doesn't wait for the embedding (Ollama, cold after a restart, 30 s
    timeout): it searches by keywords, says so, and only the cameras it covered are reported as unfinished."""
    clock = FakeClock()
    embedded = []

    async def embed(text):
        embedded.append(text)
        return None
    plan = {"calls": [{"tool": "search_events", "text": "white pickup truck", "camera": "Front Gate"}]}
    with mock.patch.object(retrieve, "_clock", clock), mock.patch.object(vlm, "embed", embed), \
            mock.patch.object(assistant.vlmroute.router, "chat_json", _planner_taking(19.5, clock, plan)):
        r = run("Was the white pickup truck at the gate?")
    assert not embedded
    assert {e["event_id"] for e in events(r)} == {IDS["truck"], IDS["old_truck"]}
    assert any("keywords only" in n for n in r["incomplete"]), r["incomplete"]
    assert r["incomplete_cameras"] == ["cam2"]
    # with time to spare the same search does use the embedding
    plan_fast = {"calls": [{"tool": "search_events", "text": "white pickup truck", "camera": "Front Gate"}]}
    with mock.patch.object(retrieve, "_clock", FakeClock()), mock.patch.object(vlm, "embed", embed), \
            mock.patch.object(assistant.vlmroute.router, "chat_json", _planner_taking(0, clock, plan_fast)):
        r = run("Was the white pickup truck at the gate?")
    assert embedded and not r["incomplete"]


def test_at_the_ceiling_everything_is_skipped_without_fallbacks():
    clock = FakeClock()
    plan = {"calls": [{"tool": "search_events", "text": "cleaning cart"}, {"tool": "count_events"}]}
    with mock.patch.object(retrieve, "_clock", clock), \
            mock.patch.object(assistant.vlmroute.router, "chat_json", _planner_taking(21.5, clock, plan)):
        r = run("Did the cleaning lady come?")
    assert [c["tool"] for c in r["calls"]] == ["search_events", "count_events"]          # no fallback was queued
    assert all(c["count"] == 0 for c in r["calls"]) and not events(r)
    assert sum("skipped to answer in time" in n for n in r["incomplete"]) == 3, r["incomplete"]   # 2 lookups + the wider search
    assert r["duration_ms"] == 21500


def test_a_skipped_footage_search_still_gets_the_fallback():
    """The skipped footage search was the last in the queue and its `continue` jumped over the "nothing found"
    fallback, so a camera-filtered search that missed was never widened."""
    clock = FakeClock()
    plan = {"calls": [{"tool": "search_events", "text": "white pickup truck", "camera": "Lobby"},
                      {"tool": "search_footage", "text": "white pickup truck", "camera": "Lobby"}]}
    with mock.patch.object(retrieve, "_clock", clock), mock.patch.object(retrieve, "FALLBACK_MIN_S", 1.0), \
            mock.patch.object(assistant.vlmroute.router, "chat_json", _planner_taking(19.5, clock, plan)):
        r = run("White pickup truck?")
    tools = [(c["tool"], c["args"].get("camera")) for c in r["calls"]]
    assert tools[:2] == [("search_events", "cam1"), ("search_footage", "cam1")] and tools[2] == ("search_events", None), tools
    assert {e["event_id"] for e in events(r)} >= {IDS["truck"], IDS["old_truck"]}
    assert any(n.startswith("search_footage") and "skipped to answer in time" in n for n in r["incomplete"]), r["incomplete"]


def test_the_sites_time_zone_sets_the_days():
    """The hub sends the Site's zone: "today", the planner's "Now" and the offset follow it, not the server's clock."""
    now = dt.datetime(2026, 10, 6, 15, 0, tzinfo=ZoneInfo("America/Chicago")).timestamp()   # 20:00 UTC, Oct 6 in both
    out = {}
    for tz in ("America/Chicago", "UTC"):
        PLANNER.plans = [{"calls": [{"tool": "search_events", "text": "cleaning cart"}]}]
        PLANNER.prompts.clear()
        out[tz] = (run("Did the cleaning lady come today?", tz=tz, now=now), PLANNER.prompts[0])
    chicago, utc = out["America/Chicago"][0], out["UTC"][0]
    assert chicago["window"]["from"] == dt.datetime(2026, 10, 6, tzinfo=ZoneInfo("America/Chicago")).timestamp()
    assert utc["window"]["from"] == dt.datetime(2026, 10, 6, tzinfo=dt.timezone.utc).timestamp()
    assert chicago["window"]["from"] - utc["window"]["from"] == 5 * 3600
    assert chicago["utc_offset"] == -5 * 3600 and utc["utc_offset"] == 0 and chicago["tz"] == "America/Chicago"
    assert "Now: Tuesday 2026-10-06 15:00" in out["America/Chicago"][1] and "Now: Tuesday 2026-10-06 20:00" in out["UTC"][1]
    assert assistant.SITE_TZ.get() is None and assistant.CAMERA_NAMES.get() == ()          # nothing leaks out of the request
    # an unknown zone (or a host without the tz database): the server's own clock, as before
    PLANNER.plans = [{"calls": [{"tool": "search_events", "text": "cleaning cart"}]}]
    assert run("Did the cleaning lady come today?", tz="Mars/Olympus")["window"]["from"] == ts(6, 0)


if __name__ == "__main__":
    setup_module()
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
