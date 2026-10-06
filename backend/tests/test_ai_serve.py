"""The site's fleet-AI relay (nvr/ai_serve.py): only tunnel requests marked by the hub reach the local Ollama;
the LAN gets 403; the model name is forced; streamed bodies pass through untouched."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("NVR_ALLOWED_HOSTS", "site")  # the test client's Host (lan_guard Host allow-list)
os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-ai-serve-test-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from nvr import ai_serve, hub_agent  # noqa: E402
from nvr.config import settings  # noqa: E402

app = FastAPI()
app.include_router(ai_serve.router)


def fake_ollama(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    assert request.url.path.endswith("/v1/chat/completions") and body["model"] == settings.vlm_model
    if body.get("stream"):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n')
    return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 1}})


def call(client_marker, headers, body):
    async def go():
        transport = httpx.ASGITransport(app=hub_agent.as_tunnel(app) if client_marker == hub_agent.IN_PROCESS_CLIENT else app, client=client_marker)
        async with httpx.AsyncClient(transport=transport, base_url="http://site") as c:
            return await c.post("/api/ai/v1/chat/completions", json=body, headers=headers)
    return asyncio.run(go())


def test_only_the_tunnel_may_use_the_relay():
    ai_serve.set_client(httpx.AsyncClient(base_url="http://ollama/v1", transport=httpx.MockTransport(fake_ollama)))
    body = {"model": "whatever", "messages": [{"role": "user", "content": "hi"}]}
    assert call(("192.168.1.9", 5000), {"x-hub-internal": "ai"}, body).status_code == 403     # LAN, even with the header
    assert call(hub_agent.IN_PROCESS_CLIENT, {}, body).status_code == 403                    # tunnel, but not the hub's AI relay
    r = call(hub_agent.IN_PROCESS_CLIENT, {"x-hub-internal": "ai"}, body)
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "hello"
    r = call(hub_agent.IN_PROCESS_CLIENT, {"x-hub-internal": "ai"}, {**body, "stream": True})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream") and b"[DONE]" in r.content
    settings.local_vlm_enabled = False
    try:
        assert call(hub_agent.IN_PROCESS_CLIENT, {"x-hub-internal": "ai"}, body).status_code == 503
    finally:
        settings.local_vlm_enabled = True
        ai_serve.set_client(None)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
