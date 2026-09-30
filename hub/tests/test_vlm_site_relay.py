"""Shared AI served by a site: the hub relays /v1/chat/completions down that site's tunnel (vlm_proxy.via_site),
meters it, refuses the serving site itself, and says 503 while that site is offline."""
import asyncio
import json
import time

import sqlalchemy as sa
from tunnelproto import Stream

from hub import agents, db, vlm_proxy
from hub.config import settings


class FakeConn:
    """Stands in for AgentConn: answers the relay request like backend/nvr/ai_serve.py would."""

    def __init__(self):
        self.requests = []
        self.credits = 0
        self.finished = []

    async def request(self, method, path, query, headers, body):
        self.requests.append((method, path, headers, json.loads(body)))
        s = Stream(1)
        payload = json.loads(body)
        if payload.get("stream"):
            s.status, s.headers = 200, {"content-type": "text/event-stream"}
            s.head.set()
            lines = ['data: {"choices":[{"delta":{"content":"Hel"}}]}', 'data: {"choices":[{"delta":{"content":"lo"}}]}',
                     'data: {"choices":[],"usage":{"prompt_tokens":700,"completion_tokens":2}}', "data: [DONE]", ""]
            text = "\n".join(lines)
            s.push(text[:40].encode()); s.push(text[40:].encode()); s.push(None)     # a line straddles the chunks
        else:
            s.status, s.headers = 200, {"content-type": "application/json"}
            s.head.set()
            s.push(json.dumps({"choices": [{"message": {"content": "{\"ok\":true}"}}], "usage": {"prompt_tokens": 1200, "completion_tokens": 40}}).encode())
            s.push(None)
        return s

    async def send(self, frame):
        if frame.get("t") == "credit":
            self.credits += frame["bytes"]

    def finish(self, s):
        self.finished.append(s.id)

    async def abort(self, s, reason):
        await s.abort(reason)


def _org_and_sites(name):
    token = db.new_token()
    org = {"id": db.new_id("o_"), "name": name, "slug": name.lower().replace(" ", "-"), "created_at": time.time(), "branding": None, "ai_shared": True}
    db.insert(db.orgs, org)
    site = {"id": db.new_id("s_"), "org_id": org["id"], "name": "Lite site", "location": "", "token_hash": db.token_hash(token),
            "token_prev_hash": None, "token_rotated_at": None, "created_at": time.time(), "last_seen_at": None, "online": False,
            "version": None, "summary": None, "clock_skew_s": None, "agent_ip": None, "hostname": None}
    db.insert(db.sites, site)
    gpu_token = db.new_token()
    gpu = {**site, "id": db.new_id("s_"), "name": "GPU site", "token_hash": db.token_hash(gpu_token)}
    db.insert(db.sites, gpu)
    return org, site, token, gpu, gpu_token


def test_relay_through_serving_site(client, superuser):
    org, lite, token, gpu, gpu_token = _org_and_sites("Relay Org")
    settings.vllm_url, settings.vllm_key, settings.vllm_model, settings.vllm_site = "", "", "qwen2.5vl:7b", gpu["id"]
    fake = FakeConn()
    agents.registry.by_site[gpu["id"]] = fake
    try:
        assert vlm_proxy.configured() and vlm_proxy.via_site()
        assert vlm_proxy.status() == {"kind": "site", "site_id": gpu["id"], "site_name": "GPU site", "online": True}
        msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}]}]
        r = client.post("/v1/chat/completions", json={"messages": msgs, "model": "other"}, headers={"Authorization": f"Bearer {token}", "X-NVR-Task": "synopsis"})
        assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == '{"ok":true}'
        method, path, headers, sent = fake.requests[-1]
        assert (method, path) == ("POST", "/api/ai/v1/chat/completions") and headers["x-hub-internal"] == "ai" and sent["model"] == "qwen2.5vl:7b"
        with client.stream("POST", "/v1/chat/completions", json={"messages": msgs, "stream": True}, headers={"Authorization": f"Bearer {token}"}) as s:
            text = "".join(s.iter_text())
        assert '"content":"Hel"' in text and "[DONE]" in text and fake.credits > 0 and fake.finished == [1, 1]
        rows = db.rows(sa.select(db.vlm_usage).where(db.vlm_usage.c.org_id == org["id"]).order_by(db.vlm_usage.c.id))
        assert [(r["task"], r["prompt_tokens"], r["completion_tokens"], r["images"], r["streamed"]) for r in rows] == \
               [("synopsis", 1200, 40, 1, False), ("stream", 700, 2, 1, True)]
        # the serving site must not call itself through the hub
        assert client.post("/v1/chat/completions", json={"messages": msgs}, headers={"Authorization": f"Bearer {gpu_token}"}).status_code == 409
        # hub-side helper (digest) goes the same way
        assert asyncio.run(vlm_proxy.complete([{"role": "user", "content": "digest"}])) == '{"ok":true}'
        # the GPU site goes offline: 503, nothing metered as success
        del agents.registry.by_site[gpu["id"]]
        r = client.post("/v1/chat/completions", json={"messages": msgs}, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 503 and "offline" in r.json()["detail"]
        assert vlm_proxy.status()["online"] is False
    finally:
        agents.registry.by_site.pop(gpu["id"], None)
        settings.vllm_site = ""
