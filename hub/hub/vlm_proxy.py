"""Shared AI: an OpenAI-compatible /v1/chat/completions in front of the hub's vLLM.

Sites authenticate with their device token (the hub told them to use it as the API key in `welcome`), so
usage can be metered per site and organisation; the internal vLLM key never leaves the hub. Streams are
passed through as they arrive; `stream_options.include_usage` is added so the final chunk carries token
counts. Per-site and per-org concurrency caps return 429, which a site's own circuit breaker turns into
"use the local model for a while".
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import db
from .config import settings

log = logging.getLogger("hub.vlm")
router = APIRouter()

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
    return bool(settings.vllm_url and settings.vllm_model)


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
        raise HTTPException(403, "shared AI is not enabled for this organisation")
    return site


def _sem(store: dict, key: str, n: int) -> asyncio.Semaphore:
    if key not in store:
        store[key] = asyncio.Semaphore(n)
    return store[key]


@router.get("/v1/models")
async def models(request: Request):
    _site_for(request)
    return {"object": "list", "data": [{"id": settings.vllm_model, "object": "model"}]}


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if not configured():
        raise HTTPException(503, "shared AI is not configured on this hub")
    site = _site_for(request)
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
        headers = {"Authorization": f"Bearer {settings.vllm_key}"} if settings.vllm_key else {}
        if not stream:
            r = await client().post("/chat/completions", json=body, headers=headers)
            usage = (r.json().get("usage") or {}) if r.headers.get("content-type", "").startswith("application/json") else {}
            _record(site, task, usage, time.time() - t0, r.status_code, n_images, streamed=False)
            return JSONResponse(r.json() if r.headers.get("content-type", "").startswith("application/json") else {"error": r.text[:300]},
                                status_code=r.status_code)
        req = client().build_request("POST", "/chat/completions", json=body, headers=headers)
        r = await client().send(req, stream=True)
        if r.status_code >= 400:
            text = (await r.aread()).decode()[:300]
            await r.aclose()
            _record(site, task, {}, time.time() - t0, r.status_code, n_images, streamed=True)
            return JSONResponse({"error": text}, status_code=r.status_code)

        async def gen():
            usage: dict = {}
            try:
                async for line in r.aiter_lines():
                    if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]"):
                        try:
                            u = json.loads(line[5:]).get("usage")
                            if u:
                                usage = u
                        except ValueError:
                            pass
                    yield (line + "\n").encode()
            finally:
                await r.aclose()
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
