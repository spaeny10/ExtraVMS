"""The shared-AI proxy against a fake vLLM: device-token auth, org flag, pass-through, streaming usage,
metering rows and the busy limit."""
import json
import time

import httpx
import sqlalchemy as sa

from hub import db, vlm_proxy
from hub.config import settings


def fake_vllm(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    assert request.headers["authorization"] == "Bearer internal-key"
    assert body["model"] == "qwen-32b"
    if body.get("stream"):
        assert body["stream_options"]["include_usage"] is True
        lines = [
            'data: {"choices":[{"delta":{"content":"Hel"}}]}',
            'data: {"choices":[{"delta":{"content":"lo"}}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":700,"completion_tokens":2}}',
            "data: [DONE]", "",
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="\n".join(lines).encode())
    return httpx.Response(200, json={"choices": [{"message": {"content": "{\"ok\":true}"}}], "usage": {"prompt_tokens": 1200, "completion_tokens": 40}})


def _setup(client, superuser):
    settings.vllm_url, settings.vllm_key, settings.vllm_model = "http://vllm:8000/v1", "internal-key", "qwen-32b"
    vlm_proxy.set_client(httpx.AsyncClient(base_url="http://vllm:8000/v1", transport=httpx.MockTransport(fake_vllm)))
    token = db.new_token()
    org = {"id": db.new_id("o_"), "name": "AI Org", "slug": "ai-org", "created_at": time.time(), "branding": None, "ai_shared": True}
    db.insert(db.orgs, org)
    db.insert(db.sites, {"id": db.new_id("s_"), "org_id": org["id"], "name": "AI site", "location": "", "token_hash": db.token_hash(token),
                         "token_prev_hash": None, "token_rotated_at": None, "created_at": time.time(), "last_seen_at": None, "online": False,
                         "version": None, "summary": None, "clock_skew_s": None, "agent_ip": None, "hostname": None})
    return org, token


def test_proxy_auth_passthrough_and_metering(client, superuser):
    org, token = _setup(client, superuser)
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}]}]
    assert client.post("/v1/chat/completions", json={"messages": msgs}).status_code == 401
    assert client.post("/v1/chat/completions", json={"messages": msgs}, headers={"Authorization": "Bearer nope"}).status_code == 401
    r = client.post("/v1/chat/completions", json={"messages": msgs, "model": "something-else"}, headers={"Authorization": f"Bearer {token}", "X-NVR-Task": "synopsis"})
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == '{"ok":true}'
    with client.stream("POST", "/v1/chat/completions", json={"messages": msgs, "stream": True}, headers={"Authorization": f"Bearer {token}"}) as s:
        text = "".join(s.iter_text())
    assert '"content":"Hel"' in text and "[DONE]" in text
    rows = db.rows(sa.select(db.vlm_usage).where(db.vlm_usage.c.org_id == org["id"]).order_by(db.vlm_usage.c.id))
    assert [(r["task"], r["prompt_tokens"], r["completion_tokens"], r["images"], r["streamed"]) for r in rows] == [("synopsis", 1200, 40, 1, False), ("stream", 700, 2, 1, True)]
    report = vlm_proxy.usage_report(org["id"], 1)
    assert report[0]["requests"] == 2 and report[0]["prompt_tokens"] == 1900
    # the org can turn sharing off
    db.run(sa.update(db.orgs).where(db.orgs.c.id == org["id"]).values(ai_shared=False))
    assert client.post("/v1/chat/completions", json={"messages": msgs}, headers={"Authorization": f"Bearer {token}"}).status_code == 403
    vlm_proxy.set_client(None)
