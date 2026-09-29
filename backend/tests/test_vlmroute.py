"""Routing between the local Qwen and an optional remote model, with mocked HTTP (nothing leaves the machine).

Run: ..\\.venv\\Scripts\\python.exe tests\\test_vlmroute.py   (from backend/)
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-route-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from nvr import vlmroute  # noqa: E402
from nvr.config import settings  # noqa: E402

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
calls: list = []


class FakeLocal:
    kind, model = "local", "local-7b"

    async def chat_json(self, messages, schema, num_predict, temperature, timeout):
        calls.append(("local", messages))
        return {"ok": True, "who": "local"}

    async def stream(self, messages, num_predict, temperature, first_token_timeout):
        calls.append(("local-stream", messages))
        for w in ("local ", "answer"):
            yield w


def mock_remote(handler):
    """Make the remote backend's httpx clients use an in-process handler."""
    real = httpx.AsyncClient

    class Client(real):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)
    vlmroute.httpx.AsyncClient = Client
    return lambda: setattr(vlmroute.httpx, "AsyncClient", real)


def fresh(configured=True, tasks=("journey", "assistant", "footage_verify")):
    settings.remote_vlm_url = "https://remote.example/v1" if configured else ""
    settings.remote_vlm_key = "k" if configured else ""
    settings.remote_vlm_model = "big-72b" if configured else ""
    settings.remote_rate_usd_per_s = 0.0
    settings.remote_interactive_timeout_s = 0.3
    r = vlmroute.Router()
    r.local = FakeLocal()
    r.set_tasks(list(tasks))
    vlmroute.db.set_setting("remote_usage", None)
    calls.clear()
    return r


def run(coro):
    return asyncio.run(coro)


def test_unconfigured_is_local():
    r = fresh(configured=False)
    out = run(r.chat_json("journey", "sys", "hi", [], SCHEMA))
    assert out["_model"] == "local-7b" and calls[0][0] == "local"
    assert r.state() == "off"


def test_remote_json_request_shape():
    r = fresh()
    seen = {}

    def handler(req: httpx.Request):
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})
    undo = mock_remote(handler)
    try:
        out = run(r.chat_json("journey", "sys", "look", [b"\xff\xd8jpeg"], SCHEMA))
    finally:
        undo()
    assert out == {"ok": True, "_model": "big-72b"}, out
    assert seen["url"] == "https://remote.example/v1/chat/completions" and seen["auth"] == "Bearer k"
    b = seen["body"]
    assert b["response_format"]["type"] == "json_schema" and b["response_format"]["json_schema"]["schema"] == SCHEMA
    img = b["messages"][1]["content"][1]
    assert img["type"] == "image_url" and img["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert r.state() == "warm" and r.status()["today"]["requests"] == 1


def test_task_not_routed_stays_local():
    r = fresh(tasks=("assistant",))
    undo = mock_remote(lambda req: (_ for _ in ()).throw(AssertionError("remote must not be called")))
    try:
        out = run(r.chat_json("synopsis", "sys", "x", [], SCHEMA))
    finally:
        undo()
    assert out["_model"] == "local-7b"


def test_failure_falls_back_and_breaker_opens():
    r = fresh()
    n = {"remote": 0}

    def handler(req):
        n["remote"] += 1
        return httpx.Response(500, text="boom")
    undo = mock_remote(handler)
    try:
        a = run(r.chat_json("journey", "s", "x", [], SCHEMA))
        b = run(r.chat_json("journey", "s", "x", [], SCHEMA))  # breaker open: straight to local
    finally:
        undo()
    assert a["_model"] == b["_model"] == "local-7b" and n["remote"] == 1, (a, b, n)
    assert r.state() == "down" and "500" in r.last_error


def test_budget_guard():
    r = fresh()
    settings.remote_rate_usd_per_s = 0.01
    settings.remote_daily_budget_usd = 1.0
    vlmroute.db.set_setting("remote_usage", {"date": time.strftime("%Y-%m-%d"), "billed_s": 150.0, "requests": 3})
    assert r.over_budget() and not r.use_remote("journey")
    out = run(r.chat_json("journey", "s", "x", [], SCHEMA))
    assert out["_model"] == "local-7b"


def test_idle_billing_estimate():
    r = fresh()
    settings.remote_idle_s = 300
    r._record(1000, 1010)          # 10 s request + 300 s idle tail
    r._record(1100, 1105)          # came 90 s later: 5 s + new tail, minus the unused 210 s of the old tail
    assert r._usage()["billed_s"] == 10 + 300 + 5 + 300 - 210, r._usage()


def test_stream_remote_and_cold_start_fallback():
    r = fresh()

    def sse(req):
        body = "".join(f"data: {json.dumps({'choices': [{'delta': {'content': w}}]})}\n\n" for w in ("big ", "answer"))
        return httpx.Response(200, text=body + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"})

    async def collect(router):
        return [x async for x in router.stream("assistant", [{"role": "user", "content": "q"}])]
    undo = mock_remote(sse)
    try:
        out = run(collect(r))
    finally:
        undo()
    assert out == [("model", "big-72b"), ("delta", "big "), ("delta", "answer")], out

    r = fresh()

    async def slow(req):
        await asyncio.sleep(3)  # cold worker: nothing within the interactive timeout
        return httpx.Response(200, text="data: [DONE]\n\n")
    undo = mock_remote(slow)
    try:
        t0 = time.time()
        out = run(collect(r))
    finally:
        undo()
    assert out[0] == ("fallback", "remote waking up") and ("model", "local-7b") in out, out
    assert "".join(d for k, d in out if k == "delta") == "local answer"
    assert time.time() - t0 < 2.5 and r.state() != "down"  # a cold start doesn't open the breaker


def test_remote_only_site_never_touches_local():
    """A site without Ollama: every task (synopses and chat included) goes remote; a remote failure is an error,
    not a silent fallback; nothing configured means not ready rather than a local call."""
    settings.local_vlm_enabled = False
    try:
        r = fresh(configured=True, tasks=())
        assert r.use_remote("synopsis") and r.use_remote("chat") and "synopsis" in r.tasks()
        restore = mock_remote(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"ok": True, "who": "remote"})}}]}))
        try:
            out = run(r.chat_json("synopsis", "sys", "describe", [], SCHEMA))
            assert out["who"] == "remote" and out["_model"] == "big-72b" and not calls
        finally:
            restore()
        restore = mock_remote(lambda req: httpx.Response(500, text="boom"))
        try:
            failed = False
            try:
                run(r.chat_json("synopsis", "sys", "describe", [], SCHEMA))
            except RuntimeError as e:
                failed = "local Qwen is disabled" in str(e)
            assert failed and not calls
        finally:
            restore()
        r2 = fresh(configured=False)
        assert not r2.use_remote("synopsis")
        try:
            run(r2.chat_json("synopsis", "sys", "x", [], SCHEMA)); assert False, "should not reach local"
        except RuntimeError as e:
            assert "not configured" in str(e) and not calls
    finally:
        settings.local_vlm_enabled = True


def test_truncated_json_salvage():
    assert vlmroute.parse_json('{"summary": "He went \\"out\\"", "tags": ["a') == {"summary": 'He went "out"'}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
