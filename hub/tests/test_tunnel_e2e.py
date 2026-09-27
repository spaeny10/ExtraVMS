"""End to end: a site agent (the real nvr.hub_agent) enrols against the hub running under uvicorn, the fleet
shows it, requests are proxied with roles enforced, streaming works, and going offline raises an alert."""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa
import uvicorn

from hub import agents, db
from hub.api import app
from hub.config import settings


async def site_app(scope, receive, send):
    """Stands in for a site: /api/json (identity echo), /api/slow (stream), /api/cameras/x/ptz/move (write)."""
    path = scope["path"]
    hdrs = {k.decode(): v.decode() for k, v in scope["headers"]}
    if path == "/api/json":
        body = json.dumps({"user": hdrs.get("x-hub-user"), "role": hdrs.get("x-hub-role"), "site": hdrs.get("x-hub-site")}).encode()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})
    elif path == "/api/slow":
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/x-ndjson")]})
        for i in range(3):
            await asyncio.sleep(0.2)
            await send({"type": "http.response.body", "body": f'{{"n":{i}}}\n'.encode(), "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
    elif path == "/api/cameras/x/ptz/move":
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"ok":true}'})
    else:
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b"nope"})


@pytest.fixture(scope="module")
def hub_server():
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)


def test_enrol_proxy_roles_offline(hub_server, superuser):
    from nvr import hub_agent
    from nvr.db import db as site_db

    base = hub_server
    site_db.set_setting("hub_url", base.replace("http://", "ws://") + "/agent")
    site_db.set_setting("hub_token", None)
    state = SimpleNamespace(pipeline=SimpleNamespace(subscribers=set()), ingests={}, health=None)

    async def summary(st, since):
        return {"cameras": [{"id": "cam1", "name": "Yard", "stream_ready": True, "problems": []}], "disk": {"free_gb": 500, "total_gb": 1000},
                "queues": {"verify": 0, "synopsis": 0}, "today": {"person": 3}, "attention": [], "version": "0.9.0"}

    agent = hub_agent.HubAgent(site_app, state, summary_fn=summary)
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    fut = asyncio.run_coroutine_threadsafe(agent.run(), loop)
    try:
        for _ in range(100):
            time.sleep(0.05)
            if agent.status()["claim_code"] and agent.status()["claim_code"] in agents.registry.pending:
                break
        code = agent.status()["claim_code"]
        assert code in agents.registry.pending, agent.status()

        owner = httpx.Client(base_url=base)
        assert owner.post("/auth/login", json={"email": superuser["email"], "password": superuser["password"]}).status_code == 200
        org = owner.post("/api/orgs", json={"name": "Acme", "slug": "acme"}).json()
        preview = owner.get(f"/api/claims/{code}").json()
        assert preview["waiting"] and preview["hint"]["version"]
        site = owner.post(f"/api/orgs/{org['id']}/sites/claim", json={"code": code, "name": "Acme HQ", "location": "Austin"}).json()
        assert site["id"].startswith("s_")
        for _ in range(100):
            time.sleep(0.05)
            if agent.connected and agent.enrolled:
                break
        assert agent.enrolled and agent.connected and agent.org == "Acme"

        # fleet shows it online with the heartbeat summary
        for _ in range(60):
            time.sleep(0.1)
            f = owner.get("/api/fleet").json()
            card = next(s for o in f["orgs"] for s in o["sites"] if s["id"] == site["id"])
            if card["online"] and card["summary"].get("today"):
                break
        assert card["online"] and card["summary"]["today"] == {"person": 3}

        # proxied requests carry the hub identity; streaming arrives incrementally
        r = owner.get(f"/s/{site['id']}/api/json")
        assert r.status_code == 200 and r.json() == {"user": superuser["email"], "role": "owner", "site": site["id"]}
        t0 = time.time(); stamps = []
        with owner.stream("GET", f"/s/{site['id']}/api/slow") as resp:
            for line in resp.iter_lines():
                stamps.append(time.time() - t0)
        assert len(stamps) == 3 and stamps[2] - stamps[0] > 0.3, stamps
        assert owner.get(f"/s/{site['id']}/api/hub").status_code == 403
        assert owner.get(f"/s/{site['id']}/").status_code in (200, 503)   # the site UI shell (503 only if dist is missing)

        # a viewer can watch but not move the camera; an operator can
        owner.post(f"/api/orgs/{org['id']}/members", json={"email": "v@acme-demo.com", "role": "viewer", "password": "viewer password 1"})
        owner.post(f"/api/orgs/{org['id']}/members", json={"email": "op@acme-demo.com", "role": "operator", "password": "operator pass 1"})
        viewer = httpx.Client(base_url=base); viewer.post("/auth/login", json={"email": "v@acme-demo.com", "password": "viewer password 1"})
        assert viewer.get(f"/s/{site['id']}/api/json").status_code == 200
        assert viewer.post(f"/s/{site['id']}/api/cameras/x/ptz/move", json={"pan": 1}).status_code == 403
        op = httpx.Client(base_url=base); op.post("/auth/login", json={"email": "op@acme-demo.com", "password": "operator pass 1"})
        assert op.post(f"/s/{site['id']}/api/cameras/x/ptz/move", json={"pan": 1}).json() == {"ok": True}
        time.sleep(0.2)
        audit = owner.get(f"/api/audit?org={org['id']}").json()
        assert any(a["user_email"] == "op@acme-demo.com" and a["path"].endswith("/ptz/move") and a["status"] == 200 for a in audit)

        # the site drops: card goes offline and an alert opens; it comes back and the alert closes
        settings.offline_after_s = 0.5
        agents.registry.started_at = 0
        asyncio.run_coroutine_threadsafe(agent.reconnect(), loop).result(5)
        fut.cancel()
        time.sleep(1.0)
        asyncio.run_coroutine_threadsafe(agents.registry.sweep(), loop).result(5)  # sweep runs in the hub loop normally; call directly here
        alerts = owner.get(f"/api/alerts?org={org['id']}").json()
        assert any(a["kind"] == "offline" and a["site_id"] == site["id"] for a in alerts), alerts
        fut2 = asyncio.run_coroutine_threadsafe(agent.run(), loop)
        for _ in range(100):
            time.sleep(0.05)
            if agent.connected:
                break
        assert agent.connected
        time.sleep(0.3)
        alerts = owner.get(f"/api/alerts?org={org['id']}").json()
        assert not any(a["kind"] == "offline" for a in alerts)
        fut2.cancel()
    finally:
        fut.cancel()
        loop.call_soon_threadsafe(loop.stop)
    assert db.one(sa.select(db.sites).where(db.sites.c.id == site["id"]))["name"] == "Acme HQ"
