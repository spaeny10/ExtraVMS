"""Two local Qwen models: the primary (e.g. qwen3.8:27b on GPU 1) and a fallback (e.g. qwen3.5:9b on GPU 0).
Routing, the two managed `ollama serve` environments, no-reload request options, the shared AI relay, the health
alert and the pipeline's per-instance start / restart; plus the synopsis prompt's door wording. Fake HTTP only
(httpx.MockTransport): nothing talks to a real Ollama.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_vlm_fallback.py   (from backend/)
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("NVR_ALLOWED_HOSTS", "site")  # the test client's Host (lan_guard Host allow-list)
os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-vlm-fallback-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from nvr import ai_serve, hub_agent, synopsis, vlmroute, zones  # noqa: E402
from nvr.config import settings  # noqa: E402

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
calls: list = []


class Fake:
    """Stands in for one instance's OllamaBackend. fail: an exception to raise instead of answering."""
    kind = "local"

    def __init__(self, model, fail=None):
        self.model, self.fail = model, fail

    async def chat_json(self, messages, schema, num_predict, temperature, timeout):
        calls.append(self.model)
        if self.fail:
            raise self.fail
        return {"ok": True}

    async def stream(self, messages, num_predict, temperature, first_token_timeout):
        calls.append(self.model)
        if self.fail:
            raise self.fail
        for w in ("an ", "answer"):
            yield w


def configure(fallback=True):
    settings.local_vlm_enabled = True
    settings.remote_vlm_url = settings.remote_vlm_key = settings.remote_vlm_model = ""
    settings.vlm_model, settings.ollama_url, settings.ollama_gpu, settings.vlm_num_ctx, settings.ollama_parallel = \
        "qwen3.8:27b", "http://127.0.0.1:11435", "1", 8192, 2
    settings.fallback_vlm_model = "qwen3.5:9b" if fallback else ""
    settings.fallback_ollama_url, settings.fallback_ollama_gpu, settings.fallback_num_ctx = "http://127.0.0.1:11437", "0", 6144
    settings.fallback_ollama_parallel, settings.fallback_when_queue_over = 1, 4


def fresh(fallback=True, primary="ready", secondary="ready", fail_primary=None):
    configure(fallback)
    r = vlmroute.Router()
    r.local = Fake("qwen3.8:27b", fail_primary)
    r.fallback = Fake("qwen3.5:9b")
    r.models["primary"].set_state(primary)
    r.models["fallback"].set_state(secondary)
    calls.clear()
    return r


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- routing

def test_primary_up_answers_everything():
    r = fresh()
    for task in ("synopsis", "journey", "assistant", "chat", "briefing"):
        assert run(r.chat_json(task, "s", "x", [], SCHEMA))["_model"] == "qwen3.8:27b"
    assert set(calls) == {"qwen3.8:27b"} and r.routed_to_fallback_last_hour() == 0


def test_primary_down_falls_back():
    for state in ("unresponsive", "starting"):
        r = fresh(primary=state)
        assert run(r.chat_json("assistant", "s", "x", [], SCHEMA))["_model"] == "qwen3.5:9b"
        assert run(r.chat_json("synopsis", "s", "x", [], SCHEMA))["_model"] == "qwen3.5:9b"
        assert calls == ["qwen3.5:9b", "qwen3.5:9b"] and r.routed_to_fallback_last_hour() == 2


def test_queue_over_threshold_spills_synopses_but_not_ask():
    r = fresh()
    r.models["primary"].queue = 5          # over fallback_when_queue_over (4)
    assert run(r.chat_json("synopsis", "s", "x", [], SCHEMA))["_model"] == "qwen3.5:9b"
    assert run(r.chat_json("journey", "s", "x", [], SCHEMA))["_model"] == "qwen3.5:9b"
    assert run(r.chat_json("assistant", "s", "x", [], SCHEMA, priority="chat"))["_model"] == "qwen3.8:27b"
    assert run(r.chat_json("briefing", "s", "x", [], SCHEMA))["_model"] == "qwen3.8:27b"
    r.models["primary"].queue = 4          # at the threshold: not over
    assert run(r.chat_json("synopsis", "s", "x", [], SCHEMA))["_model"] == "qwen3.8:27b"


def test_no_fallback_configured_is_todays_behavior():
    r = fresh(fallback=False, primary="unresponsive")
    r.models["primary"].queue = 50
    assert r.plan("synopsis") == ["primary"] and r.plan("assistant") == ["primary"]
    assert run(r.chat_json("synopsis", "s", "x", [], SCHEMA))["_model"] == "qwen3.8:27b"
    assert r.vlm_status()["fallback"] is None and r.health_alerts() == []
    # a failure is not retried anywhere and doesn't skip the primary
    r = fresh(fallback=False, fail_primary=httpx.ConnectError("refused"))
    try:
        run(r.chat_json("synopsis", "s", "x", [], SCHEMA)); assert False
    except httpx.ConnectError:
        pass
    assert r.models["primary"].skip_until == 0 and r.plan("synopsis") == ["primary"]


def test_fallback_down_keeps_primary():
    r = fresh(secondary="unresponsive")
    r.models["primary"].queue = 9
    assert r.plan("synopsis") == ["primary"]


def test_primary_failure_retries_on_fallback_and_restarts_after_two():
    restarted = []
    r = fresh(fail_primary=httpx.ConnectError("refused"))
    r.on_unresponsive = restarted.append
    out = run(r.chat_json("assistant", "s", "x", [], SCHEMA, priority="chat"))
    assert out["_model"] == "qwen3.5:9b" and calls == ["qwen3.8:27b", "qwen3.5:9b"]
    assert r.plan("assistant") == ["fallback"] and not restarted     # skipped for SKIP_S after the failure
    r.models["primary"].skip_until = 0
    run(r.chat_json("synopsis", "s", "x", [], SCHEMA))
    assert restarted == ["primary"]                                   # second failure in a row
    # 5xx and timeouts count; a 4xx (a bad request) is not the instance's fault and is not retried
    for e, retry in ((vlmroute.LocalHTTPError(500, "boom"), True), (httpx.ReadTimeout("slow"), True),
                     (vlmroute.LocalHTTPError(400, "bad"), False), (ValueError("x"), False)):
        assert vlmroute.retryable(e) == retry, e
    r = fresh(fail_primary=vlmroute.LocalHTTPError(400, "bad request"))
    try:
        run(r.chat_json("synopsis", "s", "x", [], SCHEMA)); assert False
    except vlmroute.LocalHTTPError:
        assert calls == ["qwen3.8:27b"]


def test_stream_falls_back_before_the_first_token():
    r = fresh(fail_primary=httpx.ConnectError("refused"))

    async def collect():
        return [x async for x in r.stream("chat", [{"role": "user", "content": "q"}])]
    out = run(collect())
    assert out[0][0] == "fallback" and ("model", "qwen3.5:9b") in out and ("model", "qwen3.8:27b") not in out, out
    assert "".join(d for k, d in out if k == "delta") == "an answer"
    r = fresh()
    out = run(collect())
    assert out == [("model", "qwen3.8:27b"), ("delta", "an "), ("delta", "answer")], out


def test_queue_counts_requests_waiting_and_in_flight():
    r = fresh()
    seen = []

    class Slow(Fake):
        async def chat_json(self, *a):
            seen.append(r.models["primary"].queue)
            await asyncio.sleep(0.05)
            return {"ok": True}
    r.local = Slow("qwen3.8:27b")

    async def go():
        await asyncio.gather(*(r.chat_json("synopsis", "s", "x", [], SCHEMA) for _ in range(3)))
    run(go())
    assert max(seen) == 3 and r.models["primary"].queue == 0, seen


# ---------------------------------------------------------------- the two Ollama servers and their requests

def test_two_ollama_envs():
    configure()
    rt = vlmroute.router
    p = synopsis.ollama_env(rt.models["primary"], base={"PATH": "x"})
    f = synopsis.ollama_env(rt.models["fallback"], base={"PATH": "x"})
    assert p["OLLAMA_HOST"] == "127.0.0.1:11435" and f["OLLAMA_HOST"] == "127.0.0.1:11437"
    assert p["CUDA_VISIBLE_DEVICES"] == "1" and f["CUDA_VISIBLE_DEVICES"] == "0"
    for env in (p, f):
        assert env["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID" and env["OLLAMA_VULKAN"] == "0" and env["GGML_VK_VISIBLE_DEVICES"] == ""
        assert env["OLLAMA_KEEP_ALIVE"] == "-1" and env["PATH"] == "x" and "OLLAMA_MODELS" not in env  # one shared store
    assert p["OLLAMA_NUM_PARALLEL"] == "2" and f["OLLAMA_NUM_PARALLEL"] == "1"
    assert p["OLLAMA_CONTEXT_LENGTH"] == "8192" and f["OLLAMA_CONTEXT_LENGTH"] == "6144"
    assert synopsis.OllamaServer().roles() == ["primary", "fallback"]
    configure(fallback=False)
    assert synopsis.OllamaServer().roles() == ["primary"]


def test_requests_never_force_a_reload():
    """Each instance gets its own model and its own loaded num_ctx (warm-up included); nothing else that changes how
    the model is loaded (num_gpu, main_gpu, num_batch, keep_alive...) is sent."""
    configure()
    seen = []

    def handler(req):
        seen.append((str(req.url), json.loads(req.content)))
        return httpx.Response(200, json={"message": {"content": '{"ok": true}'}})
    real = httpx.AsyncClient

    class Client(real):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)
    vlmroute.httpx.AsyncClient = Client
    try:
        r = vlmroute.Router()
        for role in ("primary", "fallback"):
            r.models[role].set_state("ready")
        run(r.chat_json("synopsis", "s", "x", [b"jpg"], SCHEMA))
        r.models["primary"].set_state("unresponsive")
        run(r.chat_json("synopsis", "s", "x", [b"jpg"], SCHEMA))
    finally:
        vlmroute.httpx.AsyncClient = real
    (u1, b1), (u2, b2) = seen
    assert u1 == "http://127.0.0.1:11435/api/chat" and b1["model"] == "qwen3.8:27b" and b1["options"]["num_ctx"] == 8192
    assert u2 == "http://127.0.0.1:11437/api/chat" and b2["model"] == "qwen3.5:9b" and b2["options"]["num_ctx"] == 6144
    for b in (b1, b2):
        assert set(b["options"]) == {"temperature", "num_ctx", "num_predict"} and "keep_alive" not in b
    w = synopsis.warm_body(vlmroute.router.models["fallback"], b"jpg")
    assert w["model"] == "qwen3.5:9b" and w["options"]["num_ctx"] == 6144 and w["keep_alive"] == -1


def test_vram_check():
    ps = {"models": [{"name": "qwen3.8:27b", "size": 18_500_000_000, "size_vram": 18_500_000_000},
                     {"name": "qwen3.5:9b", "size": 9_000_000_000, "size_vram": 7_000_000_000}]}
    assert synopsis.vram_verdict(ps, "qwen3.8:27b")["on_gpu"] is True
    assert synopsis.vram_verdict(ps, "qwen3.5:9b")["on_gpu"] is False
    assert synopsis.vram_verdict(ps, "nomic-embed-text") is None


# ---------------------------------------------------------------- health

def test_fallback_alert_after_two_minutes():
    r = fresh(primary="unresponsive")
    p = r.models["primary"]
    assert r.health_alerts() == []                                   # just went down
    p.down_since = time.time() - 121
    (a,) = r.health_alerts()
    assert a["kind"] == "vlm_fallback_active" and a["text"] == "Qwen 27B is down; descriptions are using the 9B", a
    assert a["since"] == p.down_since
    r.models["fallback"].set_state("unresponsive")
    assert r.health_alerts() == []                                   # the fallback isn't serving either
    r.models["fallback"].set_state("ready")
    p.set_state("ready")
    assert r.health_alerts() == [] and p.down_since is None
    # a primary that never came up at start-up counts from the failed start
    r = fresh(primary="starting")
    r.models["primary"].failed_start()
    r.models["primary"].down_since -= 200
    assert [x["kind"] for x in r.health_alerts()] == ["vlm_fallback_active"]
    s = r.vlm_status()
    assert s["primary"]["model"] == "qwen3.8:27b" and s["primary"]["gpu"] == "1" and s["primary"]["state"] == "starting"
    assert s["fallback"]["model"] == "qwen3.5:9b" and s["fallback"]["queue"] == 0 and s["routed_to_fallback_last_hour"] == 0


def test_pipeline_start_and_restart_per_instance():
    from nvr import pipeline as pl
    configure()
    vlmroute.router = vlmroute.Router()   # the module-level router the pipeline uses
    pl.vlmroute.router = synopsis.vlmroute.router = vlmroute.router
    p = pl.Pipeline()
    started, stopped = [], []

    async def wait_ready(timeout=60, role="primary"):
        return role == "fallback"         # the primary's Ollama doesn't come up

    async def ok(role="primary"):
        started.append(role)
        return 1.0

    class Server:
        async def stop(self, role=None):
            stopped.append(role)
    real = (pl.vlm.wait_ready, pl.vlm.ensure_models, pl.vlm.warm_up, pl.vlm.check_vram)
    real_sleep = asyncio.sleep
    pl.vlm.wait_ready, pl.vlm.ensure_models, pl.vlm.warm_up, pl.vlm.check_vram = wait_ready, ok, ok, ok
    p.ollama = Server()
    restarts = []
    p.restart_vlm = lambda role="primary": restarts.append(role) or asyncio.sleep(0)
    try:
        run(p.start_vlm())
        m = vlmroute.router.models
        assert m["fallback"].state == "ready" and m["primary"].state == "starting" and m["primary"].down_since
        assert p.vlm_ready and p.vlm_state == "starting" and restarts == ["primary"]
        assert vlmroute.router.plan("assistant") == ["fallback"]
        del p.restart_vlm
        pl.vlm.wait_ready = lambda timeout=60, role="primary": real_sleep(0, True)
        pl.asyncio.sleep = lambda s, result=None: real_sleep(0, result)   # skip the 8 s pause after stop()
        try:
            run(p.restart_vlm("primary"))   # comes back on the first try
        finally:
            pl.asyncio.sleep = real_sleep
        assert stopped == ["primary"] and m["primary"].state == "ready" and p.vlm_state == "ready" and p.vlm_down_since is None
        assert vlmroute.router.plan("assistant") == ["primary", "fallback"]
        assert p.synopsis_workers() == settings.fallback_when_queue_over + 2
        configure(fallback=False)
        assert p.synopsis_workers() == 1
    finally:
        pl.vlm.wait_ready, pl.vlm.ensure_models, pl.vlm.warm_up, pl.vlm.check_vram = real
        configure()


# ---------------------------------------------------------------- the hub's shared AI

app = FastAPI()
app.include_router(ai_serve.router)


def relay(body):
    async def go():
        transport = httpx.ASGITransport(app=hub_agent.as_tunnel(app), client=hub_agent.IN_PROCESS_CLIENT)
        async with httpx.AsyncClient(transport=transport, base_url="http://site") as c:
            return await c.post("/api/ai/v1/chat/completions", json=body, headers={"x-hub-internal": "ai"})
    return asyncio.run(go())


def test_shared_ai_routes_like_interactive_work():
    configure()
    vlmroute.router = vlmroute.Router()
    ai_serve.vlmroute.router = vlmroute.router
    hits = []

    def ollama(name, status=200):
        def handler(req):
            body = json.loads(req.content)
            hits.append((name, body["model"]))
            if status >= 500:
                return httpx.Response(status, text="boom")
            return httpx.Response(200, json={"model": body["model"], "choices": [{"message": {"content": name}}]})
        return httpx.AsyncClient(base_url=f"http://{name}/v1", transport=httpx.MockTransport(handler))
    rt = vlmroute.router
    body = {"model": "whatever", "messages": [{"role": "user", "content": "hi"}]}
    try:
        for role in ("primary", "fallback"):
            rt.models[role].set_state("ready")
        ai_serve.set_client(ollama("p"), "primary")
        ai_serve.set_client(ollama("f"), "fallback")
        rt.models["primary"].queue = 9      # busy, but the hub's requests are interactive: still the primary
        r = relay(body)
        assert r.status_code == 200 and r.json()["model"] == "qwen3.8:27b" and hits == [("p", "qwen3.8:27b")]
        assert rt.models["primary"].queue == 9 and rt.models["fallback"].queue == 0
        rt.models["primary"].queue = 0
        hits.clear()
        rt.models["primary"].set_state("unresponsive")
        r = relay(body)
        assert r.json()["model"] == "qwen3.5:9b" and hits == [("f", "qwen3.5:9b")]
        hits.clear()
        rt.models["primary"].set_state("ready")
        ai_serve.set_client(ollama("p", 500), "primary")   # the primary errors: retried on the fallback
        r = relay(body)
        assert r.status_code == 200 and hits == [("p", "qwen3.8:27b"), ("f", "qwen3.5:9b")], hits
        assert rt.models["primary"].skip_until > time.time()
    finally:
        ai_serve.set_client(None, "primary")
        ai_serve.set_client(None, "fallback")


# ---------------------------------------------------------------- door wording in the synopsis prompt

DOOR = {"name": "South Exterior Door", "type": "area", "points": [[0.45, 0.10], [0.45, 0.55], [0.62, 0.55], [0.62, 0.10]]}
CAM = {"name": "Kitchen", "zones": [DOOR]}


def track(points, t0=1_791_291_485.0, dt=0.5):
    """Feet positions -> a path of [ts, l, t, r, b, conf] boxes standing on those points."""
    return [[t0 + i * dt, x - 0.05, y - 0.3, x + 0.05, y, 0.9] for i, (x, y) in enumerate(points)]


def event(points):
    path = track(points)
    return {"path": path, "areas": zones.areas_visited(path, CAM["zones"]), "start_ts": path[0][0]}


def test_track_starting_at_the_door_entered_through_it():
    e = event([(0.53, 0.5)] * 3 + [(0.42, 0.6), (0.4, 0.65), (0.35, 0.7), (0.3, 0.75), (0.25, 0.8), (0.2, 0.85),
                                   (0.15, 0.9), (0.1, 0.95)])
    assert zones.door_facts(e) == ("South Exterior Door", None)
    line = synopsis._track_fact(e, CAM)
    assert "first seen in 'South Exterior Door'" in line and "entered through South Exterior Door" in line, line
    assert "Say it left" not in line and "came back" not in line


def test_track_from_inside_to_the_door_and_back_never_entered():
    """Event 8377: walked up to a tablet by the door from inside the room and back."""
    inside = [(0.15, 0.9), (0.2, 0.85), (0.25, 0.8), (0.3, 0.75), (0.35, 0.7), (0.4, 0.65), (0.42, 0.6)]
    e = event(inside + [(0.5, 0.5)] * 6 + list(reversed(inside)))
    assert zones.door_facts(e) == (None, None)
    line = synopsis._track_fact(e, CAM)
    assert "went to South Exterior Door and came back" in line, line
    assert "did NOT come in or go out" in line and "Say it entered" not in line and "it came in through" not in line
    facts = synopsis.event_facts({**e, "id": None, "camera_id": "c", "end_ts": e["path"][-1][0], "camera_class": "person",
                                  "camera_conf": 0.9, "yolo_class": "person", "yolo_conf": 0.9, "yolo_hits": 3}, CAM)
    assert "came in through" not in facts.replace("went to South Exterior Door and came back", ""), facts
    assert "'went to South Exterior Door'" in facts


def test_track_ending_at_the_door_left_through_it_and_beside_is_near():
    e = event([(0.1, 0.95), (0.15, 0.9), (0.2, 0.85), (0.25, 0.8), (0.3, 0.75), (0.35, 0.7), (0.4, 0.65)]
              + [(0.53, 0.5)] * 3)
    assert zones.door_facts(e) == (None, "South Exterior Door")
    line = synopsis._track_fact(e, CAM)
    assert "Say it left through South Exterior Door" in line and "entered through" not in line, line
    # started and ended just beside the door (never in it): only "near"
    e = event([(0.66, 0.5), (0.67, 0.5), (0.66, 0.52), (0.66, 0.5)])
    line = synopsis._track_fact(e, CAM)
    assert "beside 'South Exterior Door'" in line and "was near South Exterior Door" in line, line
    assert "Say it entered" not in line and "Say it left" not in line


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
