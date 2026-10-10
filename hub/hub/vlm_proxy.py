"""Shared AI: an OpenAI-compatible /v1/chat/completions the hub fronts for every site whose organization
has `ai_shared`.

Two upstreams, chosen by settings:
  * `vllm_site`: one site's local Qwen serves the fleet. The request goes down that site's tunnel to its
    `/api/ai/v1/chat/completions` (backend/nvr/ai_serve.py), so the hub needs no GPU and the site no open port.
  * `vllm_url`: a vLLM/Ollama the hub reaches directly (the `ai` compose profile, RunPod, ...).

Sites authenticate with their device token (the hub told them to use it as the API key in `welcome`), so
usage is metered per site and organization; the upstream key never leaves the hub. Streams are passed through
as they arrive; `stream_options.include_usage` is added so the final chunk carries token counts. Per-site and
per-org concurrency caps return 429, which a site's own circuit breaker turns into "use the local model for a
while".
"""
from __future__ import annotations

import asyncio
import codecs
import json
import logging
import time
from typing import AsyncIterator

import httpx
import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import db
from .agents import TooManyStreams, registry
from .config import settings

log = logging.getLogger("hub.vlm")
router = APIRouter()

RELAY_PATH = "/api/ai/v1/chat/completions"
RELAY_HEADERS = {"content-type": "application/json", "x-hub-internal": "ai", "x-hub-user": "hub", "x-hub-role": "system"}
FIRST_BYTE_S = 300.0     # a non-streamed answer arrives whole, after generation
STREAM_IDLE_S = 60.0     # stream_complete: a streamed answer with nothing new for this long has stalled

_site_sem: dict[str, asyncio.Semaphore] = {}
_org_sem: dict[str, asyncio.Semaphore] = {}
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=settings.vllm_url.rstrip("/"), timeout=httpx.Timeout(300, connect=10))
    return _client


def set_client(c: httpx.AsyncClient | None) -> None:
    """Tests inject a client with a mock transport."""
    global _client
    _client = c


def configured() -> bool:
    return bool(settings.vllm_model and (settings.vllm_site or settings.vllm_url))


def via_site() -> bool:
    return bool(settings.vllm_site)


def status() -> dict:
    """For the org AI page: what serves the fleet and whether it is reachable right now."""
    if via_site():
        conn = registry.get(settings.vllm_site)
        site = db.one(sa.select(db.sites).where(db.sites.c.id == settings.vllm_site))
        return {"kind": "site", "site_id": settings.vllm_site, "site_name": site["name"] if site else None, "online": conn is not None}
    return {"kind": "url" if settings.vllm_url else "none", "online": bool(settings.vllm_url)}


def _site_for(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme != "Bearer" or not token:
        raise HTTPException(401, "device token required")
    site = db.one(sa.select(db.sites).where(db.sites.c.token_hash == db.token_hash(token)))
    if not site:
        raise HTTPException(401, "unknown device token")
    org = db.one(sa.select(db.orgs).where(db.orgs.c.id == site["org_id"]))
    if not org or not org.get("ai_shared"):
        raise HTTPException(403, "shared AI is not enabled for this organization")
    return site


def _sem(store: dict, key: str, n: int) -> asyncio.Semaphore:
    if key not in store:
        store[key] = asyncio.Semaphore(n)
    return store[key]


def _usage_from_sse(text: str) -> dict:
    usage: dict = {}
    for line in text.splitlines():
        if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]"):
            try:
                u = json.loads(line[5:]).get("usage")
                if u:
                    usage = u
            except ValueError:
                pass
    return usage


# ---------------------------------------------------------------- upstream: a site's tunnel

class Upstream:
    """One upstream answer: status, headers, and a body iterator. Same shape for both upstreams."""

    def __init__(self, status_code: int, content_type: str, body: AsyncIterator[bytes], close) -> None:
        self.status_code, self.content_type, self.body, self.close = status_code, content_type, body, close

    async def read_all(self) -> bytes:
        out = bytearray()
        async for c in self.body:
            out += c
        return bytes(out)


async def _open_site(body: dict, stream: bool) -> Upstream:
    conn = registry.get(settings.vllm_site)
    if conn is None:
        raise HTTPException(503, "the site serving the shared AI is offline")
    try:
        s = await conn.request("POST", RELAY_PATH, "", RELAY_HEADERS, json.dumps(body).encode())
    except TooManyStreams:
        raise HTTPException(429, "shared AI is busy; use the local model")
    try:
        await asyncio.wait_for(s.head.wait(), settings.first_byte_timeout_s if stream else FIRST_BYTE_S)
    except asyncio.TimeoutError:
        await conn.abort(s, "timeout")
        raise HTTPException(504, "the shared AI did not answer in time")
    if s.aborted or s.status is None:
        conn.finish(s)
        raise HTTPException(503, f"shared AI aborted: {s.aborted or 'no response'}")

    async def body_iter():
        try:
            while True:
                c = await s.read()
                if c is None:
                    return
                yield c
                await conn.send({"t": "credit", "id": s.id, "bytes": len(c)})
        finally:
            conn.finish(s)

    async def close():
        await conn.abort(s, "client left")

    return Upstream(s.status, s.headers.get("content-type", ""), body_iter(), close)


async def _open_url(body: dict, stream: bool) -> Upstream:
    headers = {"Authorization": f"Bearer {settings.vllm_key}"} if settings.vllm_key else {}
    req = client().build_request("POST", "/chat/completions", json=body, headers=headers)
    r = await client().send(req, stream=True)

    async def body_iter():
        try:
            async for c in r.aiter_bytes():
                yield c
        finally:
            await r.aclose()

    return Upstream(r.status_code, r.headers.get("content-type", ""), body_iter(), r.aclose)


async def _open(body: dict, stream: bool) -> Upstream:
    return await (_open_site(body, stream) if via_site() else _open_url(body, stream))


async def complete(messages: list[dict], max_tokens: int = 500, temperature: float = 0.2, schema: dict | None = None) -> str | None:
    """A whole (non-streamed) answer for hub-side features such as the digest; None when unconfigured.
    `schema`: a JSON schema the answer must follow (OpenAI `response_format` json_schema, strict)."""
    if not configured():
        return None
    body = {"model": settings.vllm_model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    if schema is not None:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}}
    up = await _open(body, False)
    raw = await up.read_all()
    if up.status_code != 200:
        log.warning("shared AI %s: %s", up.status_code, raw[:200])
        return None
    return json.loads(raw)["choices"][0]["message"]["content"].strip()


def _sse_delta(line: str) -> str:
    """The text of one `data:` line of a streamed chat completion ("" for usage, [DONE] and anything else)."""
    if not line.startswith("data:"):
        return ""
    data = line[5:].strip()
    if not data or data == "[DONE]":
        return ""
    try:
        choices = json.loads(data).get("choices") or []
    except ValueError:
        return ""
    return str(((choices[0] if choices else {}).get("delta") or {}).get("content") or "")


async def _idle_limited(up: Upstream, idle_s: float) -> AsyncIterator[bytes]:
    """The upstream body, ended with an error when nothing arrives for `idle_s` (a stalled tunnel or model): the
    upstream is closed (the site's stream aborted) and RuntimeError raised, so the caller can end its own stream."""
    it = up.body.__aiter__()
    while True:
        nxt = asyncio.ensure_future(it.__anext__())
        try:
            done, _ = await asyncio.wait({nxt}, timeout=idle_s)
            if not done:
                await up.close()   # abort first (that ends the pending read), then stop waiting for it
                nxt.cancel()
                await asyncio.wait({nxt}, timeout=5)
                if nxt.done() and not nxt.cancelled():
                    nxt.exception()   # retrieved: StopAsyncIteration or the closed stream's error
                raise RuntimeError(f"the shared AI stopped writing (nothing for {idle_s:.0f} s)")
        finally:
            if not nxt.done():   # the caller went away while waiting
                nxt.cancel()
        try:
            c = nxt.result()
        except StopAsyncIteration:
            return
        yield c


async def stream_complete(messages: list[dict], max_tokens: int = 700, temperature: float = 0.2) -> AsyncIterator[str]:
    """The answer's text as it is written, for hub-side features that stream (a Site's Ask). Raises (RuntimeError or
    the HTTPException of an offline / busy upstream) before the first piece when the shared AI can't answer."""
    if not configured():
        raise RuntimeError("the shared AI is not configured on this hub")
    body = {"model": settings.vllm_model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature, "stream": True}
    up = await _open(body, True)
    if up.status_code != 200:
        raw = await up.read_all()
        raise RuntimeError(f"shared AI answered {up.status_code}: {raw[:200].decode(errors='replace')}")
    dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
    tail = ""
    async for c in _idle_limited(up, STREAM_IDLE_S):
        lines = (tail + dec.decode(c)).split("\n")
        tail = lines.pop()                     # a line may straddle two chunks
        for line in lines:
            if d := _sse_delta(line.strip()):
                yield d
    if d := _sse_delta((tail + dec.decode(b"", final=True)).strip()):
        yield d


# ---------------------------------------------------------------- the /v1 sites call

@router.get("/v1/models")
async def models(request: Request):
    _site_for(request)
    return {"object": "list", "data": [{"id": settings.vllm_model, "object": "model"}]}


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if not configured():
        raise HTTPException(503, "shared AI is not configured on this hub")
    site = _site_for(request)
    if via_site() and site["id"] == settings.vllm_site:
        raise HTTPException(409, "this site serves the shared AI itself; use its local model")
    body = await request.json()
    body["model"] = settings.vllm_model                      # sites can't pick other models
    stream = bool(body.get("stream"))
    if stream:
        body["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}
    n_images = sum(1 for m in body.get("messages", []) if isinstance(m.get("content"), list)
                   for p in m["content"] if isinstance(p, dict) and p.get("type") == "image_url")
    task = request.headers.get("x-nvr-task") or ("stream" if stream else "json")
    ssem, osem = _sem(_site_sem, site["id"], settings.vllm_per_site), _sem(_org_sem, site["org_id"], settings.vllm_per_org)
    if ssem.locked() or osem.locked():
        raise HTTPException(429, "shared AI is busy; use the local model")
    async with ssem, osem:
        t0 = time.time()
        up = await _open(body, stream)
        if not stream or up.status_code >= 400:
            raw = await up.read_all()
            is_json = up.content_type.startswith("application/json")
            usage = (json.loads(raw).get("usage") or {}) if is_json and up.status_code == 200 else {}
            _record(site, task, usage, time.time() - t0, up.status_code, n_images, streamed=stream)
            if is_json:
                return JSONResponse(json.loads(raw), status_code=up.status_code)
            return JSONResponse({"error": raw.decode(errors="replace")[:300]}, status_code=up.status_code)

        async def gen():
            tail = ""
            usage: dict = {}
            try:
                async for c in up.body:
                    text = tail + c.decode(errors="replace")
                    lines = text.split("\n")
                    tail = lines.pop()                     # a line may straddle two chunks
                    u = _usage_from_sse("\n".join(lines))
                    if u:
                        usage = u
                    yield c
                if tail:
                    usage = _usage_from_sse(tail) or usage
            finally:
                _record(site, task, usage, time.time() - t0, 200, n_images, streamed=True)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def _record(site: dict, task: str, usage: dict, dt: float, status: int, n_images: int, streamed: bool) -> None:
    db.insert(db.vlm_usage, {"ts": time.time(), "site_id": site["id"], "org_id": site["org_id"], "model": settings.vllm_model, "task": task,
                             "prompt_tokens": int(usage.get("prompt_tokens") or 0), "completion_tokens": int(usage.get("completion_tokens") or 0),
                             "latency_ms": round(dt * 1000), "status": status, "images": n_images, "streamed": streamed})


def usage_report(org_id: str, days: int) -> list[dict]:
    """Per site and day: requests, tokens, median-ish latency (mean; fine for a dashboard)."""
    since = time.time() - days * 86400
    q = sa.select(db.vlm_usage.c.site_id, sa.func.count().label("requests"), sa.func.sum(db.vlm_usage.c.prompt_tokens).label("prompt_tokens"),
                  sa.func.sum(db.vlm_usage.c.completion_tokens).label("completion_tokens"), sa.func.avg(db.vlm_usage.c.latency_ms).label("latency_ms"),
                  sa.func.sum(sa.case((db.vlm_usage.c.status >= 400, 1), else_=0)).label("errors")) \
        .where(db.vlm_usage.c.org_id == org_id, db.vlm_usage.c.ts >= since).group_by(db.vlm_usage.c.site_id)
    return [{**r, "latency_ms": round(r["latency_ms"] or 0)} for r in db.rows(q)]
