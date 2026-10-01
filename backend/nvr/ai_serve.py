"""Serve this site's local Qwen to the rest of the fleet.

The hub can be told (HUB_VLLM_SITE) that one site's GPU is the shared AI. Other sites still talk to the hub's
`/v1/chat/completions`; the hub forwards each request down *this* site's tunnel to `/api/ai/v1/...`, and this
module relays it to the local Ollama's OpenAI-compatible `/v1`. So the fleet gets a shared model without a GPU
at the hub and without this site opening any port.

Only requests that arrived through the tunnel are accepted (the bridge marks them with scope client
`hub_agent.IN_PROCESS_CLIENT`, and the hub adds `x-hub-internal: ai`); the LAN gets 403 and the hub's public
`/s/<site>/api/` proxy refuses the path before it reaches the tunnel. Ollama's own parallel slots
(`ollama_parallel`) queue these behind or beside the site's own calls; the site's `VlmGate` is not involved.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from . import hub_agent
from .config import settings

log = logging.getLogger("nvr.ai_serve")
router = APIRouter(prefix="/api/ai/v1")
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=settings.ollama_url.rstrip("/") + "/v1", timeout=httpx.Timeout(300, connect=10))
    return _client


def set_client(c: httpx.AsyncClient | None) -> None:
    """Tests inject a client with a mock transport."""
    global _client
    _client = c


def guard(request: Request) -> None:
    if request.scope.get("client") != hub_agent.IN_PROCESS_CLIENT or request.headers.get("x-hub-internal") != "ai":
        raise HTTPException(403, "the shared AI is only served to the hub")
    if not settings.local_vlm_enabled:
        raise HTTPException(503, "this site has no local model to share")


@router.get("/models")
async def models(request: Request):
    guard(request)
    return {"object": "list", "data": [{"id": settings.vlm_model, "object": "model"}]}


@router.post("/chat/completions")
async def chat_completions(request: Request):
    guard(request)
    body = await request.json()
    body["model"] = settings.vlm_model           # whatever the hub calls it, this site has one model
    body.setdefault("reasoning_effort", "none")  # Qwen3.x: answer directly instead of spending the budget thinking
    if not body.get("stream"):
        r = await client().post("/chat/completions", json=body)
        return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type", "application/json"))
    req = client().build_request("POST", "/chat/completions", json=body)
    r = await client().send(req, stream=True)
    if r.status_code >= 400:
        content = await r.aread()
        await r.aclose()
        return Response(content, status_code=r.status_code, media_type=r.headers.get("content-type", "application/json"))

    async def gen():
        try:
            async for chunk in r.aiter_bytes():
                yield chunk
        finally:
            await r.aclose()

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
