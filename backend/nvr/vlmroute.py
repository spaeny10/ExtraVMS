"""Where each Qwen request runs: the local 7B (Ollama, GPU 1) or an optional larger remote model.

The remote is any OpenAI-compatible endpoint, typically a RunPod Serverless vLLM worker running
Qwen2.5-VL-32B/72B. Only the reasoning-heavy tasks listed in `remote_tasks` go there. Anything else
(routine synopses, clip chat) and every failure or timeout falls back to the local model, so the NVR
works the same with no remote configured, no internet, or a remote that is still cold-starting.

- Local calls take turns through the pipeline's VlmGate (chat ahead of background work).
- Remote calls run on a different GPU, so they skip the gate; at most REMOTE_CONCURRENCY at a time.
- Circuit breaker: after a remote failure, remote is skipped for DOWN_S.
- Spend guard: an estimate of billed seconds (request time plus the worker's idle-before-scale-down time)
  times `remote_rate_usd_per_s`; once today's estimate reaches `remote_daily_budget_usd`, tasks go local.
"""
from __future__ import annotations

import asyncio
from collections import Counter
import base64
import contextlib
import datetime as dt
import json
import logging
import re
import time
from typing import AsyncIterator

import httpx

from .config import settings
from .db import db

log = logging.getLogger("nvr.vlmroute")

TASKS = ("assistant", "briefing", "journey", "unusual_review", "footage_verify", "synopsis", "chat")
REMOTE_CONCURRENCY = 2
DOWN_S = 300


def parse_json(content: str) -> dict:
    """JSON from a model; if it was cut off at the token limit, salvage the complete top-level fields."""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        log.warning("truncated JSON from VLM (%d chars)", len(content))
        out = {}
        for key in ("summary", "reason", "same_person", "confidence", "matches", "seen", "overall", "headline"):
            if m := re.search(rf'"{key}"\s*:\s*("(?:[^"\\]|\\.)*"|true|false)', content):
                out[key] = json.loads(m.group(1))
        return out


# ---------------------------------------------------------------- backends
# messages: [{"role": "system"|"user"|"assistant", "content": str, "images": [jpeg bytes]}]

class OllamaBackend:
    kind = "local"

    @property
    def model(self) -> str:
        return settings.vlm_model

    def _messages(self, messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            mm = {"role": m["role"], "content": m["content"]}
            if m.get("images"):
                mm["images"] = [base64.b64encode(i).decode() for i in m["images"]]
            out.append(mm)
        return out

    async def chat_json(self, messages: list[dict], schema: dict, num_predict: int, temperature: float,
                        timeout: float) -> dict:
        # think=False: Qwen3.x reason by default and would spend the whole token budget thinking (empty JSON);
        # Ollama ignores the flag for models without a thinking mode (Qwen2.5-VL)
        body = {"model": self.model, "messages": self._messages(messages), "format": schema, "stream": False, "think": False,
                "options": {"temperature": temperature, "num_ctx": settings.vlm_num_ctx, "num_predict": num_predict}}
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"{settings.ollama_url}/api/chat", json=body)
            r.raise_for_status()
            return parse_json(r.json()["message"]["content"])

    async def stream(self, messages: list[dict], num_predict: int, temperature: float,
                     first_token_timeout: float) -> AsyncIterator[str]:
        body = {"model": self.model, "messages": self._messages(messages), "stream": True, "think": False,
                "options": {"temperature": temperature, "num_ctx": settings.vlm_num_ctx, "num_predict": num_predict}}
        async with httpx.AsyncClient(timeout=httpx.Timeout(240, connect=10)) as c:
            async with c.stream("POST", f"{settings.ollama_url}/api/chat", json=body) as r:
                if r.status_code >= 400:
                    raise RuntimeError(f"Qwen request failed ({r.status_code}): {(await r.aread()).decode()[:300]}")
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    msg = json.loads(line)
                    if msg.get("error"):
                        raise RuntimeError(msg["error"])
                    if chunk := msg.get("message", {}).get("content", ""):
                        yield chunk
                    if msg.get("done"):
                        break


class OpenAIBackend:
    """OpenAI-compatible /chat/completions (vLLM on RunPod Serverless, or any other host)."""
    kind = "remote"

    @property
    def model(self) -> str:
        return settings.remote_vlm_model

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {settings.remote_vlm_key}"}

    def _url(self) -> str:
        return settings.remote_vlm_url.rstrip("/") + "/chat/completions"

    @staticmethod
    def _messages(messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            if m.get("images"):
                parts = [{"type": "text", "text": m["content"]}] + [
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(i).decode()}}
                    for i in m["images"]]
                out.append({"role": m["role"], "content": parts})
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return out

    async def chat_json(self, messages: list[dict], schema: dict, num_predict: int, temperature: float,
                        timeout: float) -> dict:
        body = {"model": self.model, "messages": self._messages(messages), "max_tokens": num_predict,
                "temperature": temperature, "reasoning_effort": "none",   # no hidden thinking: the answer, not the budget
                "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}}
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15)) as c:
            r = await c.post(self._url(), json=body, headers=self._headers())
            if r.status_code >= 400:
                raise RuntimeError(f"remote VLM {r.status_code}: {r.text[:300]}")
            return parse_json(r.json()["choices"][0]["message"]["content"] or "")

    async def stream(self, messages: list[dict], num_predict: int, temperature: float,
                     first_token_timeout: float) -> AsyncIterator[str]:
        body = {"model": self.model, "messages": self._messages(messages), "max_tokens": num_predict,
                "temperature": temperature, "stream": True, "reasoning_effort": "none"}
        timeout = httpx.Timeout(connect=15, read=first_token_timeout, write=30, pool=15)
        async with httpx.AsyncClient(timeout=timeout) as c:
            async with c.stream("POST", self._url(), json=body, headers=self._headers()) as r:
                if r.status_code >= 400:
                    raise RuntimeError(f"remote VLM {r.status_code}: {(await r.aread()).decode()[:300]}")
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    delta = (json.loads(data).get("choices") or [{}])[0].get("delta", {}).get("content")
                    if delta:
                        yield delta


class TimeToFirstToken(Exception):
    pass


# ---------------------------------------------------------------- router

class Router:
    def __init__(self) -> None:
        self.local = OllamaBackend()
        self.remote = OpenAIBackend()
        self.gate = None                  # the pipeline's VlmGate, set by Pipeline()
        self.down_until = 0.0
        self.last_error = ""
        self.last_ok_end = 0.0            # end of the last successful remote request
        self.last_end = 0.0               # end of the last remote request (for idle billing)
        self._sem: asyncio.Semaphore | None = None
        self.last_latency: float | None = None
        self.task_calls: Counter = Counter()   # calls per task since start-up (Optimize my system)

    # -- configuration
    @property
    def configured(self) -> bool:
        return bool(settings.remote_vlm_url and settings.remote_vlm_key and settings.remote_vlm_model)

    def tasks(self) -> list[str]:
        if not settings.local_vlm_enabled:
            return list(TASKS)   # no local model: everything is remote
        return [t for t in db.get_setting("remote_tasks", settings.remote_tasks) if t in TASKS]

    def set_tasks(self, tasks: list[str]) -> None:
        db.set_setting("remote_tasks", [t for t in tasks if t in TASKS])

    # -- spend guard
    def _usage(self) -> dict:
        u = db.get_setting("remote_usage") or {}
        today = dt.date.today().isoformat()
        return u if u.get("date") == today else {"date": today, "billed_s": 0.0, "requests": 0}

    def _record(self, start: float, end: float) -> None:
        u = self._usage()
        idle = settings.remote_idle_s
        # The worker stays up (and billed) for `idle` after each request: count that tail now, and give back the
        # part of the previous tail this request cut short.
        billed = (end - start) + idle
        if self.last_end and start - self.last_end < idle:
            billed -= idle - max(0.0, start - self.last_end)
        u["billed_s"] = round(u["billed_s"] + billed, 1)
        u["requests"] = u.get("requests", 0) + 1
        db.set_setting("remote_usage", u)
        self.last_end = end

    def spent_usd(self) -> float:
        return self._usage()["billed_s"] * settings.remote_rate_usd_per_s

    def over_budget(self) -> bool:
        return settings.remote_rate_usd_per_s > 0 and self.spent_usd() >= settings.remote_daily_budget_usd

    def use_remote(self, task: str) -> bool:
        if not settings.local_vlm_enabled:
            return self.configured   # no local model to fall back to: always try the remote
        return (self.configured and task in self.tasks() and time.time() >= self.down_until
                and not self.over_budget())

    def _no_local(self, task: str, e: Exception | None = None) -> RuntimeError:
        return RuntimeError(f"no model for {task}: local Qwen is disabled and the remote "
                            + (f"failed ({type(e).__name__}: {str(e)[:120]})" if e else "is not configured"))

    def state(self) -> str:
        if not self.configured:
            return "off"
        if time.time() < self.down_until:
            return "down"
        return "warm" if time.time() - self.last_ok_end < settings.remote_idle_s else "cold"

    def status(self) -> dict:
        u = self._usage()
        return {"configured": self.configured, "model": settings.remote_vlm_model if self.configured else None,
                "local_model": settings.vlm_model, "state": self.state(), "tasks": self.tasks(), "all_tasks": list(TASKS),
                "down_until": self.down_until if time.time() < self.down_until else None, "last_error": self.last_error,
                "today": {"billed_s": u["billed_s"], "requests": u.get("requests", 0), "usd": round(self.spent_usd(), 2)},
                "budget_usd": settings.remote_daily_budget_usd, "rate_usd_per_s": settings.remote_rate_usd_per_s,
                "last_latency_s": self.last_latency}

    def _fail(self, task: str, e: Exception, mark_down: bool = True) -> None:
        self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
        if mark_down:
            self.down_until = time.time() + DOWN_S
        log.warning("remote VLM failed for %s (%s); using the local model%s", task, self.last_error,
                    f" for the next {DOWN_S // 60} min" if mark_down else "")

    @property
    def sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(REMOTE_CONCURRENCY)
        return self._sem

    @contextlib.asynccontextmanager
    async def _local_turn(self, priority: str):
        if self.gate is None:
            yield
        else:
            async with (self.gate.chat() if priority == "chat" else self.gate.background()):
                yield

    # -- calls
    async def chat_json(self, task: str, system: str, text: str, images: list[bytes], schema: dict,
                        num_predict: int = 300, temperature: float = 0.1, priority: str = "background") -> dict:
        """Structured answer; the result carries "_model" (which model wrote it)."""
        self.task_calls[task] += 1
        messages = [{"role": "system", "content": system}, {"role": "user", "content": text, "images": images}]
        if self.use_remote(task):
            timeout = settings.remote_interactive_timeout_s * 2 if priority == "chat" else settings.remote_background_timeout_s
            t0 = time.time()
            try:
                async with self.sem:
                    t0 = time.time()
                    r = await self.remote.chat_json(messages, schema, num_predict, temperature, timeout)
                self._record(t0, time.time())
                self.last_ok_end, self.last_latency = time.time(), round(time.time() - t0, 2)
                return {**r, "_model": self.remote.model}
            except Exception as e:  # noqa: BLE001 - any remote problem falls back to local
                self._record(t0, time.time())
                # a user-facing call that timed out is probably a cold start: fall back now, don't mark down
                self._fail(task, e, mark_down=not (priority == "chat" and isinstance(e, httpx.TimeoutException)))
                if not settings.local_vlm_enabled:
                    raise self._no_local(task, e) from e
        if not settings.local_vlm_enabled:
            raise self._no_local(task)
        async with self._local_turn(priority):
            r = await self.local.chat_json(messages, schema, num_predict, temperature, 180)
        return {**r, "_model": self.local.model}

    async def stream(self, task: str, messages: list[dict], num_predict: int = 500, temperature: float = 0.3,
                     priority: str = "chat") -> AsyncIterator[tuple[str, str]]:
        """Yields ("model", name) once, then ("delta", text) chunks. If the remote hasn't produced its first
        token within remote_interactive_timeout_s (cold start), the local model answers instead."""
        self.task_calls[task] += 1
        if self.use_remote(task):
            t0 = time.time()
            gen = self.remote.stream(messages, num_predict, temperature, settings.remote_interactive_timeout_s)
            first = None
            try:
                async with self.sem:
                    t0 = time.time()
                    first = await asyncio.wait_for(gen.__anext__(), settings.remote_interactive_timeout_s + 1)
                    yield ("model", self.remote.model)
                    yield ("delta", first)
                    async for chunk in gen:
                        yield ("delta", chunk)
                self._record(t0, time.time())
                self.last_ok_end, self.last_latency = time.time(), round(time.time() - t0, 2)
                return
            except (asyncio.TimeoutError, httpx.TimeoutException, StopAsyncIteration, httpx.HTTPError, RuntimeError) as e:
                self._record(t0, time.time())
                with contextlib.suppress(Exception):
                    await gen.aclose()
                if first is not None:  # failed mid-answer: can't restart silently
                    self._fail(task, e)
                    raise
                cold = isinstance(e, (asyncio.TimeoutError, httpx.TimeoutException))
                self._fail(task, e if not cold else TimeToFirstToken("remote still waking up"), mark_down=not cold)
                if not settings.local_vlm_enabled:
                    raise self._no_local(task, e) from e
                yield ("fallback", "remote waking up" if cold else "remote unavailable")
        if not settings.local_vlm_enabled:
            raise self._no_local(task)
        async with self._local_turn(priority):
            yield ("model", self.local.model)
            async for chunk in self.local.stream(messages, num_predict, temperature, 240):
                yield ("delta", chunk)

    async def warm(self) -> dict:
        """Tiny remote request so a cold worker starts loading (e.g. when the Ask tab opens)."""
        if not self.configured or self.state() == "warm":
            return {"state": self.state()}
        asyncio.create_task(self._warm())
        return {"state": "waking"}

    async def _warm(self) -> None:
        t0 = time.time()
        try:
            await self.remote.chat_json([{"role": "user", "content": "Reply with {\"ok\": true}"}],
                                        {"type": "object", "properties": {"ok": {"type": "boolean"}}}, 5, 0.0,
                                        settings.remote_background_timeout_s)
            self._record(t0, time.time())
            self.last_ok_end, self.last_latency = time.time(), round(time.time() - t0, 2)
            log.info("remote VLM warm after %.1fs", time.time() - t0)
        except Exception as e:  # noqa: BLE001
            self._record(t0, time.time())
            self._fail("warm", e)

    async def test(self) -> dict:
        """One small remote request, timed (System page Test button)."""
        if not self.configured:
            return {"ok": False, "error": "No remote model configured (NVR_REMOTE_VLM_URL / _KEY / _MODEL in .env)."}
        was = self.state()
        t0 = time.time()
        try:
            r = await self.remote.chat_json([{"role": "user", "content": "Say hello in three words as JSON."}],
                                            {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                                            20, 0.0, settings.remote_background_timeout_s)
            self._record(t0, time.time())
            self.last_ok_end, self.last_latency = time.time(), round(time.time() - t0, 2)
            self.down_until = 0
            return {"ok": True, "seconds": self.last_latency, "was": was, "reply": r.get("text", ""), "model": self.remote.model}
        except Exception as e:  # noqa: BLE001
            self._record(t0, time.time())
            self._fail("test", e)
            return {"ok": False, "seconds": round(time.time() - t0, 2), "was": was, "error": self.last_error}


router = Router()
