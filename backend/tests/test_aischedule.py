"""The Site's AI schedule on the server (aischedule.py) and what follows it, on a throwaway DB with mocked HTTP:
evaluating the pushed schedule (timezones, windows across midnight, DST, holidays, the override, unmonitored Sites,
no schedule / a stand-alone server = full time), keeping it across restarts, the gating of each job kind outside the
schedule (descriptions deferred to catch-up or skipped and never retried, journeys, retries, PPE ppe_vlm_all, a person's
Generate with Qwen, interactive tasks), the catch-up line's order and its wait for an idle Qwen (and a busy shared vLLM),
the hub agent applying pushes, and the meter (requests, tokens, seconds per kind and in/out of schedule).

Run: ..\\.venv\\Scripts\\python.exe tests\\test_aischedule.py   (from backend/)
"""
import asyncio
import datetime as dt
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

_TMP = tempfile.mkdtemp(prefix="nvr-aisched-test-")   # never the real DB, recordings or runtime config
os.environ["NVR_DATA_DIR"] = str(Path(_TMP, "data"))
os.environ["NVR_RECORDINGS_DIR"] = str(Path(_TMP, "recordings"))
os.environ["NVR_RUNTIME_DIR"] = str(Path(_TMP, "runtime"))
os.environ["NVR_HUB_URL"] = "ws://127.0.0.1:0/agent"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tunnelproto"))

import httpx  # noqa: E402

from nvr import aischedule, hub_agent, mediamtx, ppe, vlmroute  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402
from nvr.pipeline import Pipeline  # noqa: E402

mediamtx.recording_spans = lambda *a, **k: []
db.upsert_camera({"id": "cam1", "name": "Yard", "host": "10.0.0.5", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": "p",
                  "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": [], "retention_days": None, "scene_notes": "",
                  "retention_policy": None, "policies": []})
CHI = ZoneInfo("America/Chicago")
WEEKNIGHTS = [{"dow": [0, 1, 2, 3, 4], "from": "18:00", "to": "06:00"}]   # Monday..Friday evenings to the next morning
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
settings.catchup_poll_s = 0.01


def at(y, m, d, h=0, mi=0, tz=CHI):
    return dt.datetime(y, m, d, h, mi, tzinfo=tz).timestamp()


def cfg(**kw):
    return aischedule.clean({"mode": "soc", "outside": "catchup", "monitored": True, "timezone": "America/Chicago",
                             "arm_schedule": WEEKNIGHTS, "arm_holidays": [], "arm_override": None, **kw})


def run(coro):
    return asyncio.run(coro)


# full Qwen (monitored, no windows = armed around the clock) / limited (never armed) / no schedule at all
INSIDE = {"mode": "soc", "outside": "catchup", "monitored": True, "timezone": "UTC", "arm_schedule": []}
OUTSIDE_CATCHUP = {"mode": "soc", "outside": "catchup", "monitored": False, "timezone": "UTC"}
OUTSIDE_SKIP = {"mode": "soc", "outside": "skip", "monitored": False, "timezone": "UTC"}


def make(start, cls="person", status="verified", synopsis=None):
    path = [[start, 0.3, 0.35, 0.4, 0.65, 0.9], [start + 5, 0.5, 0.35, 0.6, 0.65, 0.9]]
    eid = db.create_event(camera_id="cam1", track_id=str(start) + cls, camera_class=cls, camera_conf=0.9, start_ts=start,
                          end_ts=start + 5, path=path, status=status)
    db.update_event(eid, synopsis=synopsis)
    return eid


# ---------------------------------------------------------------- evaluating the schedule

def test_overnight_window_across_midnight_in_the_sites_timezone():
    c = cfg()
    assert aischedule.compute_armed(c, at(2026, 10, 5, 17, 59)) == (False, "disarmed_schedule")
    assert aischedule.compute_armed(c, at(2026, 10, 5, 18, 0)) == (True, "schedule")       # from is inclusive
    assert aischedule.compute_armed(c, at(2026, 10, 6, 3, 0)) == (True, "schedule")        # Monday's window after midnight
    assert aischedule.compute_armed(c, at(2026, 10, 6, 6, 0)) == (False, "disarmed_schedule")   # to is exclusive
    assert aischedule.compute_armed(c, at(2026, 10, 10, 3, 0)) == (True, "schedule")       # Saturday morning: Friday's
    assert aischedule.compute_armed(c, at(2026, 10, 11, 3, 0)) == (False, "disarmed_schedule")  # Saturday night: not listed
    # the same instant is another wall-clock time in another zone; an unknown zone counts as UTC
    assert aischedule.compute_armed({**c, "timezone": "UTC"}, at(2026, 10, 5, 12, 0)) == (False, "disarmed_schedule")
    assert aischedule.compute_armed({**c, "timezone": "UTC"}, at(2026, 10, 5, 13, 0)) == (True, "schedule")
    assert aischedule.compute_armed({**c, "timezone": "Mars/Olympus"}, at(2026, 10, 5, 13, 0)) == (True, "schedule")
    # a Sunday window spills into Monday (dow wraps)
    sun = cfg(arm_schedule=[{"dow": [6], "from": "22:00", "to": "02:00"}])
    assert aischedule.compute_armed(sun, at(2026, 10, 12, 1, 0)) == (True, "schedule")
    assert aischedule.compute_armed(sun, at(2026, 10, 12, 2, 0)) == (False, "disarmed_schedule")
    # state(): on until 06:00, then limited until 18:00, in local wall-clock time
    s = aischedule.evaluate(c, at(2026, 10, 5, 23, 0))
    assert s["qwen"] == "on" and s["until"] == at(2026, 10, 6, 6, 0) and s["mode"] == "soc"
    s = aischedule.evaluate(c, at(2026, 10, 6, 12, 0))
    assert s["qwen"] == "limited" and s["until"] == at(2026, 10, 6, 18, 0) and s["outside"] == "catchup"


def test_daylight_saving_keeps_wall_clock_times():
    c = cfg(arm_schedule=[{"dow": [5], "from": "22:00", "to": "06:00"}])   # Saturday night
    # US DST ends 2026-11-01 02:00 (that night is 25 hours long): the window still ends at 06:00 on the wall clock
    assert aischedule.compute_armed(c, at(2026, 11, 1, 1, 30)) == (True, "schedule")
    assert aischedule.compute_armed(c, at(2026, 11, 1, 5, 59)) == (True, "schedule")
    assert aischedule.compute_armed(c, at(2026, 11, 1, 6, 0)) == (False, "disarmed_schedule")
    assert aischedule.next_change(c, at(2026, 10, 31, 23, 0)) == {"at": at(2026, 11, 1, 6, 0), "armed": False}
    assert at(2026, 11, 1, 6, 0) - at(2026, 10, 31, 22, 0) == 9 * 3600   # 8 wall-clock hours, 9 real ones
    # DST starts 2027-03-14 02:00 (02:00-03:00 does not exist): a window 01:30 -> 03:30 is 1 real hour
    spring = cfg(arm_schedule=[{"dow": [6], "from": "01:30", "to": "03:30"}])
    assert aischedule.compute_armed(spring, at(2027, 3, 14, 1, 45)) == (True, "schedule")
    assert aischedule.compute_armed(spring, at(2027, 3, 14, 1, 45) + 1800) == (True, "schedule")    # 03:15 CDT
    assert aischedule.compute_armed(spring, at(2027, 3, 14, 3, 30)) == (False, "disarmed_schedule")
    assert aischedule.next_change(spring, at(2027, 3, 14, 1, 45)) == {"at": at(2027, 3, 14, 3, 30), "armed": False}


def test_holidays_override_and_unmonitored():
    xmas = {"date": "2026-12-25", "name": "Christmas", "armed": True}
    assert aischedule.compute_armed(cfg(arm_holidays=[xmas]), at(2026, 12, 25, 12)) == (True, "holiday")
    assert aischedule.compute_armed(cfg(arm_holidays=[{**xmas, "armed": False}]), at(2026, 12, 25, 23)) == (False, "holiday")
    part = cfg(arm_holidays=[{**xmas, "from": "08:00", "to": "20:00"}])
    assert aischedule.compute_armed(part, at(2026, 12, 25, 10)) == (True, "holiday")
    assert aischedule.compute_armed(part, at(2026, 12, 25, 21)) == (False, "holiday")
    o = cfg(arm_override={"mode": "arm", "until": at(2026, 10, 6, 14, 0)})
    assert aischedule.compute_armed(o, at(2026, 10, 6, 12, 0)) == (True, "override")
    assert aischedule.compute_armed(o, at(2026, 10, 6, 14, 0)) == (False, "disarmed_schedule")   # expired
    assert aischedule.next_change(o, at(2026, 10, 6, 12, 0)) == {"at": at(2026, 10, 6, 14, 0), "armed": False}
    # an unmonitored Site is never armed: on "SOC hours" its Qwen is always limited, with no change ahead
    s = aischedule.evaluate(cfg(monitored=False), at(2026, 10, 5, 23))
    assert s["qwen"] == "limited" and s["reason"] == "unmonitored" and s["until"] is None
    # a monitored Site with no windows is armed around the clock
    assert aischedule.evaluate(cfg(arm_schedule=[]), at(2026, 10, 6, 12))["qwen"] == "on"


def test_no_schedule_full_mode_and_standalone_are_full_time():
    assert aischedule.evaluate(None, time.time()) == {"mode": "full", "qwen": "on", "reason": "default", "until": None,
                                                       "outside": "catchup", "key": None}
    full = aischedule.clean({"mode": "full", "monitored": False})
    assert aischedule.evaluate(full, time.time())["qwen"] == "on"
    assert aischedule.clean({"mode": "always-on?"})["mode"] == "full" and aischedule.clean({"outside": "x"})["outside"] == "catchup"
    assert aischedule.clean("nonsense") is None
    aischedule.apply(OUTSIDE_SKIP)
    assert not aischedule.full()
    settings.hub_enabled = False   # a stand-alone server: no hub decides anything
    try:
        assert aischedule.config() is None and aischedule.full(now=time.time())
    finally:
        settings.hub_enabled = True
    aischedule.apply(None)
    assert aischedule.full()


def test_pushed_schedule_is_kept_across_restarts():
    aischedule.apply({**OUTSIDE_SKIP, "key": "abc123"})
    assert db.get_setting(aischedule.SETTING)["outside"] == "skip"
    aischedule.reload()                     # a restart: read back from the database
    assert aischedule.config()["key"] == "abc123" and aischedule.outside() == "skip" and not aischedule.full()
    assert aischedule.summary()["key"] == "abc123" and aischedule.summary()["qwen"] == "limited"
    aischedule.apply(None)
    aischedule.reload()
    assert aischedule.config() is None and aischedule.full()


# ---------------------------------------------------------------- what runs outside the schedule

def _pipeline():
    p = Pipeline()
    p.vlm_ready = True
    vlmroute.router.models["primary"].queue = vlmroute.router.models["fallback"].queue = 0
    vlmroute.router.remote_queue = 0
    return p


async def _drain_worker(p: Pipeline, seconds: float = 0.2):
    task = asyncio.create_task(p._synopsis_worker())
    await asyncio.sleep(seconds)
    task.cancel()


def test_outside_catchup_defers_and_skip_marks():
    described = []

    async def fake(event_id):
        described.append(event_id)
        db.update_event(event_id, synopsis=f"described {event_id}")

    # inside the schedule: described as before
    aischedule.apply(INSIDE)
    p = _pipeline()
    p._synopsis = fake
    a = make(time.time() - 100)
    p.queue_synopsis(a)
    run(_drain_worker(p))
    assert described == [a]

    # outside, catch up: deferred into the catch-up line, the card says so
    aischedule.apply(OUTSIDE_CATCHUP)
    p = _pipeline()
    p._synopsis = fake
    b = make(time.time() - 90)
    p.queue_synopsis(b)
    run(_drain_worker(p))
    assert described == [a] and b in p.synopsis_deferred and p.catchup_q.qsize() == 1 and b not in p.synopsis_pending
    assert p.annotate(db.event(b))["synopsis_deferred"] is True

    # outside, skip: marked, never described, never retried, not requeued at start-up
    aischedule.apply(OUTSIDE_SKIP)
    p = _pipeline()
    p._synopsis = fake
    c = make(time.time() - 80)
    p.queue_synopsis(c)
    run(_drain_worker(p))
    e = db.event(c)
    assert e["detections"]["synopsis_skipped"] == "No description (outside AI hours)" and e["detections"]["ai_hours"] == "skipped"
    assert e["error"] is None and e["synopsis"] is None and c not in p.synopsis_deferred
    assert not p.wants_synopsis(e)
    aischedule.apply(INSIDE)   # back inside the hours: still not retried or recovered
    p2 = _pipeline()
    p2.retry_failed_synopses()
    p2.recover()
    queued = {p2.synopsis_q.get_nowait()[2] for _ in range(p2.synopsis_q.qsize())}
    assert c not in queued and b in queued   # b was only deferred: a restart queues it again

    # a person's Generate with Qwen is written whatever the schedule says
    aischedule.apply(OUTSIDE_SKIP)
    p = _pipeline()
    p._synopsis = fake
    p.queue_synopsis(c, user=True)
    assert p.synopsis_q._queue[0][0] == -1   # ahead of everything
    run(_drain_worker(p))
    assert described[-1] == c and c not in p.synopsis_user
    aischedule.apply(None)


def test_retries_journeys_and_ppe_follow_the_schedule():
    p = _pipeline()
    aischedule.apply(INSIDE)
    assert p.retry_due() and run(p.journey_ok())
    aischedule.apply(OUTSIDE_CATCHUP)
    assert not p.retry_due()                    # retries wait for the schedule
    assert run(p.journey_ok())                  # journeys at idle with catch-up...
    p.synopsis_pending.add(1)
    assert not run(p.journey_ok())              # ...not while a description is being written
    p.synopsis_pending.clear()
    vlmroute.router.models["primary"].queue = 1
    assert not run(p.journey_ok())              # ...nor while Qwen answers someone (an Ask, the hub's shared AI)
    vlmroute.router.models["primary"].queue = 0
    aischedule.apply(OUTSIDE_SKIP)
    assert not run(p.journey_ok())              # with skip they wait for the schedule
    # PPE: second looks always; "Qwen checks every person" only inside the schedule
    settings.ppe_vlm_all = True
    try:
        assert not ppe.needs_vlm({"hard_hat": "present", "vest": "present"}) and not ppe.vlm_all()
        assert ppe.needs_vlm({"hard_hat": "unclear", "vest": "present"})
        aischedule.apply(INSIDE)
        assert ppe.needs_vlm({"hard_hat": "present", "vest": "present"}) and ppe.vlm_all()
    finally:
        settings.ppe_vlm_all = False
        aischedule.apply(None)


def test_catchup_order_and_waiting_for_an_idle_qwen():
    aischedule.apply(OUTSIDE_CATCHUP)
    p = _pipeline()
    order, kinds = [], []

    async def fake(event_id):
        order.append(event_id)
        kinds.append(aischedule.kind_of("synopsis"))
        db.update_event(event_id, synopsis="x")

    p._synopsis = fake
    t = time.time() - 50
    v1, pe1, v2, pe2 = make(t, "vehicle"), make(t + 1), make(t + 2, "vehicle"), make(t + 3)
    for eid, prio in ((v1, 1), (pe1, 0), (v2, 1), (pe2, 0)):
        p.defer_synopsis(eid, prio)
    busy = {"vllm": True}

    async def shared_busy():
        return busy["vllm"]
    real_busy = aischedule.shared.busy
    aischedule.shared.busy = shared_busy

    async def go():
        task = asyncio.create_task(p.catchup_loop())
        await asyncio.sleep(0.1)
        assert order == []                      # the shared vLLM is busy: nothing yet
        busy["vllm"] = False
        p.synopsis_q.put_nowait((0, 999, 12345))   # a live description is queued: still waiting
        await asyncio.sleep(0.1)
        assert order == []
        p.synopsis_q.get_nowait()
        await asyncio.sleep(0.2)
        task.cancel()
    try:
        run(go())
    finally:
        aischedule.shared.busy = real_busy
    assert order == [pe1, pe2, v1, v2]          # people first, then oldest first
    assert kinds == ["catchup"] * 4 and not p.synopsis_deferred
    assert [p.journey_q.get_nowait() for _ in range(p.journey_q.qsize())] == order   # linked afterwards, like the workers'
    # described meanwhile by a person (Generate with Qwen): dropped from the line; skip switched on: marked instead
    p = _pipeline()
    p._synopsis = fake
    order.clear()
    x, y = make(t + 4), make(t + 5)
    p.defer_synopsis(x)
    p.defer_synopsis(y)
    p.queue_synopsis(x, user=True)             # leaves the catch-up line
    p.synopsis_q.get_nowait()
    p.synopsis_pending.clear()
    vlmroute.router.models["primary"].queue = 1   # Qwen busy, then the Site switches to skip
    aischedule.apply(OUTSIDE_SKIP)

    async def go2():
        task = asyncio.create_task(p.catchup_loop())
        await asyncio.sleep(0.15)
        task.cancel()
    run(go2())
    vlmroute.router.models["primary"].queue = 0
    assert order == [] and db.event(y)["detections"]["ai_hours"] == "skipped" and not p.synopsis_deferred
    aischedule.apply(None)


def test_shared_vllm_metrics():
    sm = aischedule.SharedModel()
    old = (settings.local_vlm_enabled, settings.remote_vlm_url, settings.catchup_vllm_metrics_url)
    try:
        settings.local_vlm_enabled, settings.remote_vlm_url = False, "http://vllm:8000/v1"
        assert sm.url() == "http://vllm:8000/metrics"
        db.set_setting("hub_vlm", {"url": "https://hub.example/v1"})
        assert sm.url() is None                     # the hub's shared AI has no /metrics
        db.set_setting("hub_vlm", None)
        settings.catchup_vllm_metrics_url = "off"
        assert sm.url() is None
        settings.catchup_vllm_metrics_url = ""
        settings.local_vlm_enabled = True
        assert sm.url() is None                     # a local Qwen: this server's own queue says it all
        settings.local_vlm_enabled = False
        text = {"body": 'vllm:num_requests_running{model_name="q"} 1.0\nvllm:num_requests_waiting{model_name="q"} 0.0\n'
                        'vllm:num_requests_running_total 9\n'}
        real = httpx.AsyncClient

        class Client(real):
            def __init__(self, *a, **kw):
                kw["transport"] = httpx.MockTransport(lambda req: httpx.Response(200, text=text["body"]) if text["body"]
                                                      else httpx.Response(503))
                super().__init__(*a, **kw)
        aischedule.httpx.AsyncClient = Client
        try:
            assert run(sm.busy(now=1000.0)) is False
            text["body"] = 'vllm:num_requests_running{model_name="q"} 1.0\nvllm:num_requests_waiting{model_name="q"} 2.0\n'
            assert run(sm.busy(now=1001.0)) is False          # cached for TTL_S
            assert run(sm.busy(now=1010.0)) is True           # requests waiting there
            text["body"] = 'vllm:num_requests_running{a="1"} 1\nvllm:num_requests_running{a="2"} 1\nvllm:num_requests_waiting 0\n'
            assert run(sm.busy(now=1020.0)) is True           # 2 running (summed over label sets) >= catchup_vllm_busy_running
            text["body"] = ""
            assert run(sm.busy(now=1030.0)) is False          # can't read it: this server's queue decides
            text["body"] = "vllm:num_requests_waiting 5\n"
            assert run(sm.busy(now=1040.0)) is False          # ...and it isn't asked again for FAIL_S
            assert run(sm.busy(now=1030.0 + sm.FAIL_S + 1)) is True
        finally:
            aischedule.httpx.AsyncClient = real
        assert aischedule.metric("x 1\nvllm:a 2\n", "vllm:b") is None
    finally:
        settings.local_vlm_enabled, settings.remote_vlm_url, settings.catchup_vllm_metrics_url = old


# ---------------------------------------------------------------- the hub agent

def test_hub_agent_applies_pushes_and_unenrol_clears():
    agent = hub_agent.HubAgent(None, SimpleNamespace(pipeline=SimpleNamespace(subscribers=set()), mtx=None))
    run(agent._on_message({"t": "ai_schedule", "ai_schedule": {**OUTSIDE_SKIP, "key": "k1"}}))
    assert aischedule.config()["key"] == "k1" and not aischedule.full()

    async def welcome():
        async def summary(state, since):
            return {}
        agent.summary_fn = summary
        await agent._on_message({"t": "welcome", "site_id": "s_x", "heartbeat_s": 60, "ai_schedule": {**INSIDE, "key": "k2"}})
        for t in agent._tasks:
            t.cancel()
    run(welcome())
    assert aischedule.config()["key"] == "k2" and aischedule.full()
    assert db.get_setting(aischedule.SETTING)["key"] == "k2"
    run(agent.configure(unenrol=True))
    assert aischedule.config() is None and db.get_setting(aischedule.SETTING) is None and aischedule.full()


# ---------------------------------------------------------------- metering

class FakeLocal:
    kind, model = "local", "local-9b"

    async def chat_json(self, messages, schema, num_predict, temperature, timeout):
        vlmroute.note_usage(time.time() - 0.5, 100, 10)
        return {"ok": True}

    async def stream(self, messages, num_predict, temperature, first_token_timeout):
        t0 = time.time() - 1.0
        for w in ("a ", "b"):
            yield w
        vlmroute.note_usage(t0, 50, 2)


def test_meter_counts_persists_and_reports():
    m = aischedule.Meter()
    now = at(2026, 10, 18, 12, tz=dt.timezone.utc)
    m.record("synopsis", True, 2.5, 1000, 80, now=now)
    m.record("synopsis", True, 1.5, 900, 70, now=now + 1)
    m.record("synopsis", False, 3.0, None, None, now=now + 2)
    m.record("ppe", False, 0.75, 400, 20, now=now + 86400)     # the next UTC day
    m.flush(now + 86400)
    again = aischedule.Meter()                                   # a restart: read back from the settings table
    rows = again.rows(now=now + 86400)
    by = {(r["day"], r["kind"], r["sched"]): r for r in rows}
    assert by[("2026-10-18", "synopsis", "in")] == {"day": "2026-10-18", "kind": "synopsis", "sched": "in", "requests": 2,
                                                    "prompt_tokens": 1900, "completion_tokens": 150, "seconds": 4.0}
    assert by[("2026-10-18", "synopsis", "out")]["requests"] == 1 and by[("2026-10-18", "synopsis", "out")]["prompt_tokens"] == 0
    assert by[("2026-10-19", "ppe", "out")]["seconds"] == 0.75
    assert [r["day"] for r in again.rows(days=1, now=now + 86400)] == ["2026-10-19"]
    again.flush(now + (aischedule.KEEP_DAYS + 2) * 86400)        # old days are dropped
    assert aischedule.Meter().rows(days=100, now=now + 86400) == []


def test_router_meters_kind_schedule_tokens_and_seconds():
    old_meter = aischedule.meter
    aischedule.meter = m = aischedule.Meter()
    settings.remote_vlm_url = settings.remote_vlm_key = settings.remote_vlm_model = ""
    r = vlmroute.Router()
    r.local = FakeLocal()
    try:
        aischedule.apply(OUTSIDE_CATCHUP)
        run(r.chat_json("talkdown", "s", "x", [b"jpg"], SCHEMA, priority="chat"))   # Describe & warn: always, metered
        run(r.chat_json("ppe", "s", "x", [], SCHEMA))
        with aischedule.job("catchup"):
            run(r.chat_json("synopsis", "s", "x", [], SCHEMA))

        async def chat():
            return [x async for x in r.stream("chat", [{"role": "user", "content": "q"}])]
        aischedule.apply(INSIDE)
        assert run(chat())[0] == ("model", "local-9b")
        run(r.chat_json("assistant", "s", "x", [], SCHEMA, priority="chat"))
        by = {(x["kind"], x["sched"]): x for x in m.rows()}
        assert by[("talkdown", "out")]["requests"] == 1 and by[("talkdown", "out")]["prompt_tokens"] == 100
        assert by[("ppe", "out")]["completion_tokens"] == 10 and by[("catchup", "out")]["requests"] == 1
        assert ("synopsis", "out") not in by
        assert by[("ask", "in")]["requests"] == 2 and by[("ask", "in")]["prompt_tokens"] == 150   # clip chat + Ask
        assert 0.4 < by[("talkdown", "out")]["seconds"] < 1.0 and by[("ask", "in")]["seconds"] >= 1.4
        assert r.idle()
    finally:
        aischedule.meter = old_meter
        aischedule.apply(None)


def test_backends_report_their_token_counts():
    """The real backends: Ollama's eval counts (JSON and the last stream line), OpenAI's usage (JSON and the
    include_usage chunk), all into the request's meter."""
    real = httpx.AsyncClient
    seen = {}

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        seen.setdefault("bodies", []).append(body)
        if req.url.path.endswith("/api/chat"):
            if body.get("stream"):
                lines = [{"message": {"content": "hi"}, "done": False},
                         {"message": {"content": ""}, "done": True, "prompt_eval_count": 33, "eval_count": 4}]
                return httpx.Response(200, text="\n".join(json.dumps(x) for x in lines))
            return httpx.Response(200, json={"message": {"content": '{"ok": true}'}, "prompt_eval_count": 120, "eval_count": 7})
        if body.get("stream"):
            chunks = [{"choices": [{"delta": {"content": "hey"}}]}, {"choices": [], "usage": {"prompt_tokens": 61, "completion_tokens": 5}}]
            return httpx.Response(200, text="".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}],
                                         "usage": {"prompt_tokens": 200, "completion_tokens": 9}})

    class Client(real):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)
    vlmroute.httpx.AsyncClient = Client

    async def one(fn):
        u = {"seconds": 0.0, "prompt": 0, "completion": 0}
        token = vlmroute.USAGE.set(u)
        try:
            await fn()
        finally:
            vlmroute.USAGE.reset(token)
        return u["prompt"], u["completion"]

    async def drain(gen):
        return [x async for x in gen]
    try:
        local, remote = vlmroute.OllamaBackend(), vlmroute.OpenAIBackend()
        settings.remote_vlm_url, settings.remote_vlm_key, settings.remote_vlm_model = "https://remote.example/v1", "k", "big"
        assert run(one(lambda: local.chat_json([{"role": "user", "content": "x"}], SCHEMA, 10, 0.1, 5))) == (120, 7)
        assert run(one(lambda: drain(local.stream([{"role": "user", "content": "x"}], 10, 0.1, 5)))) == (33, 4)
        assert run(one(lambda: remote.chat_json([{"role": "user", "content": "x"}], SCHEMA, 10, 0.1, 5))) == (200, 9)
        assert run(one(lambda: drain(remote.stream([{"role": "user", "content": "x"}], 10, 0.1, 5)))) == (61, 5)
        assert seen["bodies"][-1]["stream_options"] == {"include_usage": True}
    finally:
        vlmroute.httpx.AsyncClient = real
        settings.remote_vlm_url = settings.remote_vlm_key = settings.remote_vlm_model = ""


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
