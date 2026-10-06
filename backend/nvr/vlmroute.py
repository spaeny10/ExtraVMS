"""Where each Qwen request runs: the local model (Ollama; qwen3.5:9b by default) or an optional larger remote model.

The remote is any OpenAI-compatible endpoint, typically a RunPod Serverless vLLM worker running
Qwen2.5-VL-32B/72B. Only the reasoning-heavy tasks listed in `remote_tasks` go there. Anything else
(routine synopses, clip chat) and every failure or timeout falls back to the local model, so the NVR
works the same with no remote configured, no internet, or a remote that is still cold-starting.

- Local calls take turns through the pipeline's VlmGate (chat ahead of background work).
- Remote calls run on a different GPU, so they skip the gate; at most REMOTE_CONCURRENCY at a time.
- Circuit breaker: after a remote failure, remote is skipped for DOWN_S.
- Spend guard: an estimate of billed seconds (request time plus the worker's idle-before-scale-down time)
  times `remote_rate_usd_per_s`; once today's estimate reaches `remote_daily_budget_usd`, tasks go local.

Two local models (optional, NVR_FALLBACK_VLM_MODEL): the primary (vlm_model, e.g. qwen3.8:27b on GPU 1) and a
smaller fallback (e.g. qwen3.5:9b on GPU 0), each in its own managed `ollama serve` (synopsis.OllamaServer) with
its own gate. Every local request goes to the primary, except:
- the primary is not ready (starting, restarting, or skipped for SKIP_S after a connection / 5xx / timeout
  failure): the fallback answers, and a request whose primary call fails that way is retried on the fallback;
- high-volume background work (HIGH_VOLUME: synopses, journeys, PPE checks) while the primary's queue (requests in
  flight plus waiting for their turn, the hub's shared-AI requests included) is over `fallback_when_queue_over`.
Interactive work (Ask, clip chat, briefings, footage search, the hub's digests) stays on the primary unless it is
down. Each instance is always called with its own loaded num_ctx, so a request never makes Ollama reload a model.
With no fallback configured everything goes to the primary exactly as before.
"""
from __future__ import annotations

import asyncio
from collections import Counter, deque
import base64
import contextlib
import datetime as dt
import json
import logging
import re
import time
from typing import AsyncIterator, Callable

import httpx

from .config import settings
from .db import db

log = logging.getLogger("nvr.vlmroute")

TASKS = ("assistant", "briefing", "journey", "unusual_review", "footage_verify", "synopsis", "chat", "ppe")
REMOTE_CONCURRENCY = 2
DOWN_S = 300
# local primary / fallback routing
HIGH_VOLUME = frozenset({"synopsis", "unusual_review", "journey", "ppe"})   # may spill over to the fallback when busy
SKIP_S = 60             # after a local connection / 5xx / timeout failure, that instance is skipped this long
FAIL_RESTART = 2        # consecutive such failures before that instance's Ollama is restarted (pipeline.restart_vlm)
FALLBACK_ALERT_S = 120  # vlm_fallback_active once the primary has been down this long with the fallback serving
ROLES = ("primary", "fallback")


class LocalHTTPError(RuntimeError):
    """A local Ollama answered with an HTTP error (status kept so 5xx can fall back and 4xx doesn't)."""

    def __init__(self, status: int, text: str) -> None:
        super().__init__(f"Qwen request failed ({status}): {text[:300]}")
        self.status = status


def retryable(e: BaseException) -> bool:
    """A local failure that says "this instance is unwell" (try the other one), not "this request is bad"."""
    if isinstance(e, (httpx.TransportError, asyncio.TimeoutError)):   # connect refused, reset, read/connect timeout
        return True
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code >= 500
    if isinstance(e, LocalHTTPError):
        return e.status >= 500
    return False


def size_label(model: str) -> str:
    """'qwen3.8:27b' -> '27B' (the parameter count from the tag), else the model name."""
    m = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)b\b", model or "", re.I)
    return f"{m.group(1)}B" if m else (model or "?")


class LocalModel:
    """One managed `ollama serve` and its model: the primary (vlm_model) or the fallback (fallback_vlm_model).
    Settings are read live (the hub / tests may change them). The pipeline moves `state` (start_vlm / restart_vlm);
    the router counts the queue and skips an instance for SKIP_S after it failed."""

    def __init__(self, role: str) -> None:
        self.role = role
        self.state = "starting"                  # starting | ready | unresponsive
        self.down_since: float | None = None     # stopped answering (or failed to start) at
        self.skip_until = 0.0
        self.fails = 0                           # consecutive connection / 5xx / timeout failures
        self.queue = 0                           # requests in flight + waiting for their turn (router + shared AI)
        self.last_error = ""
        self.vram: dict | None = None            # {"size", "size_vram"} from /api/ps after warm-up

    @property
    def primary(self) -> bool:
        return self.role == "primary"

    @property
    def configured(self) -> bool:
        return settings.local_vlm_enabled if self.primary else settings.fallback_vlm_enabled

    @property
    def model(self) -> str:
        return settings.vlm_model if self.primary else settings.fallback_vlm_model

    @property
    def url(self) -> str:
        return (settings.ollama_url if self.primary else settings.fallback_ollama_url).rstrip("/")

    @property
    def gpu(self) -> str:
        return settings.ollama_gpu if self.primary else settings.fallback_ollama_gpu

    @property
    def num_ctx(self) -> int:
        return settings.vlm_num_ctx if self.primary else settings.fallback_num_ctx

    @property
    def parallel(self) -> int:
        return settings.ollama_parallel if self.primary else settings.fallback_ollama_parallel

    def available(self, now: float | None = None) -> bool:
        return self.configured and self.state == "ready" and (now or time.time()) >= self.skip_until

    def set_state(self, state: str) -> None:
        if state == "ready":
            self.down_since, self.fails, self.skip_until = None, 0, 0.0
        elif self.down_since is None and state == "unresponsive":
            self.down_since = time.time()
        self.state = state

    def failed_start(self) -> None:
        """Start-up could not load the model (Ollama didn't start, pull / warm-up failed): down from now."""
        if self.down_since is None:
            self.down_since = time.time()

    def snapshot(self) -> dict:
        return {"model": self.model, "gpu": self.gpu, "state": self.state, "queue": self.queue, "url": self.url,
                "num_ctx": self.num_ctx, "parallel": self.parallel, "down_since": self.down_since,
                "skipped_until": self.skip_until if time.time() < self.skip_until else None,
                "last_error": self.last_error or None, "vram": self.vram}


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


def no_think(body: dict) -> dict:
    """Ask a remote OpenAI-compatible model to answer directly (Qwen3.x otherwise spends the budget thinking).
    NVR_REMOTE_VLM_NO_THINK picks how: "reasoning_effort" (default; Ollama /v1 and most hosted APIs),
    "chat_template" (vLLM: chat_template_kwargs.enable_thinking=false, since some vLLM builds reject
    reasoning_effort "none"), or "off" (send nothing)."""
    how = (settings.remote_vlm_no_think or "reasoning_effort").strip().lower()
    if how == "reasoning_effort":
        body["reasoning_effort"] = "none"
    elif how == "chat_template":
        body["chat_template_kwargs"] = {"enable_thinking": False}
    return body

class OllamaBackend:
    """Native /api/chat on one local instance. Always sends that instance's own num_ctx (a different one would
    make Ollama reload the model) and nothing else that changes how the model is loaded."""
    kind = "local"

    def __init__(self, inst: LocalModel | None = None) -> None:
        self.inst = inst or LocalModel("primary")

    @property
    def model(self) -> str:
        return self.inst.model

    def options(self, temperature: float, num_predict: int) -> dict:
        return {"temperature": temperature, "num_ctx": self.inst.num_ctx, "num_predict": num_predict}

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
                "options": self.options(temperature, num_predict)}
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10)) as c:
            r = await c.post(f"{self.inst.url}/api/chat", json=body)
            if r.status_code >= 400:
                raise LocalHTTPError(r.status_code, r.text)
            return parse_json(r.json()["message"]["content"])

    async def stream(self, messages: list[dict], num_predict: int, temperature: float,
                     first_token_timeout: float) -> AsyncIterator[str]:
        body = {"model": self.model, "messages": self._messages(messages), "stream": True, "think": False,
                "options": self.options(temperature, num_predict)}
        async with httpx.AsyncClient(timeout=httpx.Timeout(240, connect=10)) as c:
            async with c.stream("POST", f"{self.inst.url}/api/chat", json=body) as r:
                if r.status_code >= 400:
                    raise LocalHTTPError(r.status_code, (await r.aread()).decode())
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
        body = no_think({"model": self.model, "messages": self._messages(messages), "max_tokens": num_predict,
                         "temperature": temperature,
                         "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}})
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15)) as c:
            r = await c.post(self._url(), json=body, headers=self._headers())
            if r.status_code >= 400:
                raise RuntimeError(f"remote VLM {r.status_code}: {r.text[:300]}")
            return parse_json(r.json()["choices"][0]["message"]["content"] or "")

    async def stream(self, messages: list[dict], num_predict: int, temperature: float,
                     first_token_timeout: float) -> AsyncIterator[str]:
        body = no_think({"model": self.model, "messages": self._messages(messages), "max_tokens": num_predict,
                         "temperature": temperature, "stream": True})
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
        self.models = {role: LocalModel(role) for role in ROLES}
        self.local = OllamaBackend(self.models["primary"])
        self.fallback = OllamaBackend(self.models["fallback"])
        self.remote = OpenAIBackend()
        self.gate = None                  # the pipeline's VlmGate for the primary, set by Pipeline()
        self.fallback_gate = None         # ...and the fallback's own (it is another GPU: the two run side by side)
        self.on_unresponsive: Callable[[str], None] | None = None   # pipeline: restart that instance's Ollama
        self.fallback_served: deque[float] = deque(maxlen=5000)     # when the fallback answered (last hour count)
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

    # -- local primary / fallback
    def plan(self, task: str) -> list[str]:
        """Which local instance(s) to try, in order (see the module docstring)."""
        if not settings.fallback_vlm_enabled:
            return ["primary"]                       # one local model: today's behavior
        p, f = self.models["primary"], self.models["fallback"]
        now = time.time()
        if not f.available(now):
            return ["primary"]
        if not p.available(now):
            return ["fallback"]
        if task in HIGH_VOLUME and p.queue > settings.fallback_when_queue_over:
            return ["fallback", "primary"]
        return ["primary", "fallback"]

    def pick(self, task: str) -> str:
        """The instance a request would go to now (the hub's shared AI asks this)."""
        return self.plan(task)[0]

    def backend(self, role: str):
        return self.local if role == "primary" else self.fallback

    def local_ok(self, role: str) -> None:
        m = self.models[role]
        m.fails, m.skip_until = 0, 0.0
        if role == "fallback":
            self.fallback_served.append(time.time())

    def local_failed(self, role: str, e: BaseException) -> None:
        """A connection / 5xx / timeout failure: skip this instance for SKIP_S, and after FAIL_RESTART in a row
        have the pipeline restart its Ollama (with no fallback configured the pipeline's own watchdog does that,
        as before)."""
        m = self.models[role]
        m.fails += 1
        m.last_error = f"{type(e).__name__}: {str(e)[:200]}"
        if not settings.fallback_vlm_enabled:
            return
        m.skip_until = time.time() + SKIP_S
        log.warning("local %s model %s failed (%s, %d in a row); %s", role, m.model, m.last_error, m.fails,
                    "using the other local model" if role == "primary" else "using the primary")
        if m.fails >= FAIL_RESTART and m.state == "ready" and self.on_unresponsive is not None:
            self.on_unresponsive(role)

    def routed_to_fallback_last_hour(self) -> int:
        cutoff = time.time() - 3600
        return sum(1 for t in self.fallback_served if t >= cutoff)

    def vlm_status(self) -> dict:
        """/api/system `vlm`: both local instances in words and numbers."""
        def one(role):
            m = self.models[role]
            return {k: v for k, v in m.snapshot().items() if k in ("model", "gpu", "state", "queue", "down_since", "vram")}
        return {"primary": one("primary"), "fallback": one("fallback") if settings.fallback_vlm_enabled else None,
                "routed_to_fallback_last_hour": self.routed_to_fallback_last_hour()}

    def health_alerts(self, now: float | None = None) -> list[dict]:
        """`vlm_fallback_active` while the primary has been down > FALLBACK_ALERT_S and the fallback is serving."""
        if not settings.fallback_vlm_enabled:
            return []
        now = now or time.time()
        p, f = self.models["primary"], self.models["fallback"]
        if p.state == "ready" or p.down_since is None or now - p.down_since < FALLBACK_ALERT_S or f.state != "ready":
            return []
        name = size_label(p.model)
        return [{"kind": "vlm_fallback_active",
                 "text": f"{'Qwen ' if 'qwen' in p.model.lower() and name != p.model else ''}{name} is down; "
                         f"descriptions are using the {size_label(f.model)}",
                 "since": p.down_since, "error": p.last_error or None, "model": p.model, "fallback_model": f.model}]

    def embed_url(self) -> str:
        """Embeddings (one small model, same vectors anywhere): the primary, or the fallback while it is down."""
        p, f = self.models["primary"], self.models["fallback"]
        return f.url if settings.fallback_vlm_enabled and not p.available() and f.available() else p.url

    @contextlib.asynccontextmanager
    async def _local_turn(self, priority: str, role: str = "primary"):
        """Count the request in the instance's queue while it waits for its turn and runs."""
        m = self.models[role]
        gate = self.gate if role == "primary" else self.fallback_gate
        m.queue += 1
        try:
            if gate is None:
                yield
            else:
                async with (gate.chat() if priority == "chat" else gate.background()):
                    yield
        finally:
            m.queue -= 1

    async def _local_json(self, task: str, messages: list[dict], schema: dict, num_predict: int, temperature: float,
                          priority: str) -> dict:
        order = self.plan(task)
        for i, role in enumerate(order):
            try:
                async with self._local_turn(priority, role):
                    r = await self.backend(role).chat_json(messages, schema, num_predict, temperature, 180)
            except Exception as e:
                if not retryable(e):
                    raise
                self.local_failed(role, e)
                if i + 1 < len(order) and self.models[order[i + 1]].available():
                    continue
                raise
            self.local_ok(role)
            return {**r, "_model": self.backend(role).model}
        raise RuntimeError("no local model")   # unreachable: plan() is never empty

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
        return await self._local_json(task, messages, schema, num_predict, temperature, priority)

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
        order = self.plan(task)
        for i, role in enumerate(order):
            backend, started = self.backend(role), False
            try:
                async with self._local_turn(priority, role):
                    if len(order) == 1:   # nothing to fall back to: name the model up front, as before
                        started = True
                        yield ("model", backend.model)
                    async for chunk in backend.stream(messages, num_predict, temperature, 240):
                        if not started:   # the model is named once it has answered, so a fallback can still rename it
                            started = True
                            yield ("model", backend.model)
                        yield ("delta", chunk)
                    if not started:
                        yield ("model", backend.model)
            except Exception as e:
                if not retryable(e):
                    raise
                self.local_failed(role, e)
                if started or i + 1 >= len(order) or not self.models[order[i + 1]].available():
                    raise   # failed mid-answer (can't restart silently) or nowhere else to go
                yield ("fallback", "primary model unavailable" if role == "primary" else "fallback model unavailable")
                continue
            self.local_ok(role)
            return

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
