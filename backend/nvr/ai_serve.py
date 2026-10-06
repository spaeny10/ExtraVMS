"""Serve this site's local Qwen to the rest of the fleet.

The hub can be told (HUB_VLLM_SITE) that one site's GPU is the shared AI. Other sites still talk to the hub's
`/v1/chat/completions`; the hub forwards each request down *this* site's tunnel to `/api/ai/v1/...`, and this
module relays it to the local Ollama's OpenAI-compatible `/v1`. So the fleet gets a shared model without a GPU
at the hub and without this site opening any port.

Only requests that arrived through the tunnel are accepted (the bridge marks their ASGI scope, checked with
`hub_agent.is_tunnel`, and the hub adds `x-hub-internal: ai`); the LAN gets 403 and the hub's public
`/s/<site>/api/` proxy refuses the path before it reaches the tunnel. Ollama's own parallel slots
(`ollama_parallel`) queue these behind or beside the site's own calls; the site's `VlmGate` is not involved.

With a fallback model (NVR_FALLBACK_VLM_MODEL) these requests are routed like the site's interactive work
(vlmroute.plan): the primary, or the fallback while the primary is down; a request the primary refuses with a
connection error, a timeout or a 5xx (before any bytes were sent back) is retried on the fallback. Each request
counts in that instance's queue while it runs, so the site's synopses see the load and can spill over.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from . import hub_agent, vlmroute
from .config import settings

log = logging.getLogger("nvr.ai_serve")
router = APIRouter(prefix="/api/ai/v1")
_clients: dict[str, httpx.AsyncClient] = {}
TASK = "hub"   # not high-volume: the primary unless it is down (the hub's digests and shift reports are read by people)


def client(role: str = "primary") -> httpx.AsyncClient:
    if role not in _clients:
        url = vlmroute.router.models[role].url
        _clients[role] = httpx.AsyncClient(base_url=url + "/v1", timeout=httpx.Timeout(300, connect=10))
    return _clients[role]


def set_client(c: httpx.AsyncClient | None, role: str = "primary") -> None:
    """Tests inject a client with a mock transport (None: back to a real one)."""
    if c is None:
        _clients.pop(role, None)
    else:
        _clients[role] = c


def guard(request: Request) -> None:
    if not hub_agent.is_tunnel(request.scope) or request.headers.get("x-hub-internal") != "ai":
        raise HTTPException(403, "the shared AI is only served to the hub")
    if not settings.local_vlm_enabled:
        raise HTTPException(503, "this site has no local model to share")


@router.get("/models")
async def models(request: Request):
    guard(request)
    return {"object": "list", "data": [{"id": vlmroute.router.models[vlmroute.router.pick(TASK)].model, "object": "model"}]}


def _bad(r: httpx.Response | None, e: Exception | None) -> bool:
    return e is not None or (r is not None and r.status_code >= 500)


@router.post("/chat/completions")
async def chat_completions(request: Request):
    guard(request)
    body = await request.json()
    body.setdefault("reasoning_effort", "none")  # Qwen3.x: answer directly instead of spending the budget thinking
    rt = vlmroute.router
    order = rt.plan(TASK)
    for i, role in enumerate(order):
        m = rt.models[role]
        last = i + 1 >= len(order) or not rt.models[order[i + 1]].available()
        body["model"] = m.model                  # whatever the hub calls it, the instance's own model answers
        m.queue += 1
        r, err, handed_off = None, None, False
        try:
            if not body.get("stream"):
                try:
                    r = await client(role).post("/chat/completions", json=body)
                except (httpx.TransportError, httpx.TimeoutException) as e:
                    err = e
                if _bad(r, err):
                    rt.local_failed(role, err or httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r))
                    if not last:
                        continue
                    if err is not None:
                        raise HTTPException(502, f"local model unavailable: {type(err).__name__}")
                else:
                    rt.local_ok(role)
                return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type", "application/json"))
            try:
                req = client(role).build_request("POST", "/chat/completions", json=body)
                r = await client(role).send(req, stream=True)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                err = e
            if _bad(r, err):
                content = b""
                if r is not None:
                    content = await r.aread()
                    await r.aclose()
                rt.local_failed(role, err or httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r))
                if not last:
                    continue
                if err is not None:
                    raise HTTPException(502, f"local model unavailable: {type(err).__name__}")
                return Response(content, status_code=r.status_code, media_type=r.headers.get("content-type", "application/json"))
            if r.status_code >= 400:
                content = await r.aread()
                await r.aclose()
                return Response(content, status_code=r.status_code, media_type=r.headers.get("content-type", "application/json"))
            rt.local_ok(role)
            handed_off = True   # the generator below finishes the request (and its queue count)

            async def gen(r=r, m=m):
                try:
                    async for chunk in r.aiter_bytes():
                        yield chunk
                finally:
                    m.queue -= 1
                    await r.aclose()

            return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
        finally:
            if not handed_off:
                m.queue -= 1
    raise HTTPException(503, "no local model")   # unreachable: plan() is never empty
