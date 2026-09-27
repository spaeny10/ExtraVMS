"""Fleet features through a real tunnel: fan-out search, streamed Ask from every site, digest, config backup
and restore, and push subscriptions (with a fake sender)."""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa
import uvicorn

from hub import agents, db, push
from hub.api import app

EXPORT = {"format": 1, "exported_at": 1.0, "site_version": "0.9.0", "cameras": [{"id": "cam1", "name": "Yard"}], "camera_links": [],
          "identities": [{"name": "Sam"}], "layouts": [], "settings": {}}


def make_site(name: str):
    async def site_app(scope, receive, send):
        path, q = scope["path"], scope["query_string"].decode()

        async def reply(status, obj, ctype=b"application/json"):
            await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", ctype)]})
            await send({"type": "http.response.body", "body": json.dumps(obj).encode() if not isinstance(obj, bytes) else obj})
        if path == "/api/search":
            await reply(200, [{"id": 1, "camera_id": "cam1", "start_ts": 100.0, "camera_class": "person", "synopsis": f"{name}: a person", "score": 0.9}])
        elif path == "/api/footage/search":
            await reply(200, [{"camera_id": "cam1", "ts": 90.0, "score": 3.2}])
        elif path == "/api/assistant/ask":
            body = b""
            while True:
                m = await receive(); body += m.get("body", b"")
                if not m.get("more_body"): break
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/x-ndjson")]})
            for i, word in enumerate([name, "says", "yes"]):
                await asyncio.sleep(0.05)
                await send({"type": "http.response.body", "body": json.dumps({"type": "delta", "text": word + " "}).encode() + b"\n", "more_body": True})
            await send({"type": "http.response.body", "body": json.dumps({"type": "done", "id": 1}).encode() + b"\n", "more_body": False})
        elif path == "/api/briefings":
            await reply(200, {"briefings": [{"headline": f"Quiet night at {name}", "text": "Nothing much."}], "settings": {}})
        elif path == "/api/config/export":
            await reply(200, EXPORT)
        elif path == "/api/config/import":
            body = b""
            while True:
                m = await receive(); body += m.get("body", b"")
                if not m.get("more_body"): break
            site_app.imported = json.loads(body)
            await reply(200, {"cameras": 1, "camera_links": 0, "identities": 1, "layouts": 0})
        else:
            await reply(404, {"detail": "nope"})
    site_app.imported = None
    return site_app


@pytest.fixture(scope="module")
def hub_server():
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    server.should_exit = True
    t.join(timeout=5)


def _enrol(base, owner, org, agent_app, name, loop):
    """Run a HubAgent for `agent_app` in `loop`, enrol it, return (agent, site)."""
    from nvr import hub_agent
    from nvr.db import db as site_db
    site_db.set_setting("hub_url", base.replace("http://", "ws://") + "/agent")
    site_db.set_setting("hub_token", None); site_db.set_setting("hub_claim", None)
    agent = hub_agent.HubAgent(agent_app, SimpleNamespace(pipeline=SimpleNamespace(subscribers=set()), ingests={}, health=None),
                               summary_fn=lambda st, since: _summary())
    fut = asyncio.run_coroutine_threadsafe(agent.run(), loop)
    for _ in range(100):
        time.sleep(0.05)
        code = agent.status()["claim_code"]
        if code and code in agents.registry.pending:
            break
    site = owner.post(f"/api/orgs/{org['id']}/sites/claim", json={"code": agent.status()["claim_code"], "name": name, "location": ""}).json()
    for _ in range(100):
        time.sleep(0.05)
        if agent.connected and agent.enrolled:
            break
    assert agent.connected, agent.status()
    return agent, site, fut


async def _summary():
    return {"cameras": [], "today": {"person": 1}, "queues": {"verify": 0, "synopsis": 0}}


def test_fleet_features(hub_server, superuser):
    base = hub_server
    owner = httpx.Client(base_url=base, timeout=30)
    assert owner.post("/auth/login", json={"email": superuser["email"], "password": superuser["password"]}).status_code == 200
    org = owner.post("/api/orgs", json={"name": "Fleet Co", "slug": "fleet-co"}).json()
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    app_a = make_site("Alpha")
    agent_a, site_a, fut_a = _enrol(base, owner, org, app_a, "Alpha", loop)
    try:
        time.sleep(0.5)
        # fan-out search
        r = owner.get(f"/api/fleet/search?org={org['id']}&q=person").json()
        assert r["events"][0]["site_name"] == "Alpha" and r["events"][0]["synopsis"].startswith("Alpha") and r["footage"][0]["site_id"] == site_a["id"]
        assert r["sites"][0]["events"] == 1 and r["offline"] == []
        # streamed ask
        lines = []
        with owner.stream("POST", "/api/fleet/ask", json={"org": org["id"], "message": "anyone?"}) as resp:
            for line in resp.iter_lines():
                if line.strip():
                    lines.append(json.loads(line))
        assert lines[0]["type"] == "sites" and lines[-1]["type"] == "done"
        text = "".join(l["text"] for l in lines if l.get("type") == "delta" and l["site"] == site_a["id"])
        assert text.strip() == "Alpha says yes"
        # digest (no vLLM configured -> plain text from the site briefing)
        d = owner.post(f"/api/orgs/{org['id']}/digests/generate").json()
        assert "Quiet night at Alpha" in d["text"] and owner.get(f"/api/orgs/{org['id']}/digests").json()[0]["id"] == d["id"]
        # config backup and restore
        b = owner.post(f"/api/sites/{site_a['id']}/backups").json()
        assert b["cameras"] == 1 and b["identities"] == 1
        assert owner.get(f"/api/sites/{site_a['id']}/backups").json()[0]["id"] == b["id"]
        assert owner.get(f"/api/sites/{site_a['id']}/backups/{b['id']}").json()["cameras"][0]["name"] == "Yard"
        res = owner.post(f"/api/sites/{site_a['id']}/backups/{b['id']}/restore", json={"replace_identities": True}).json()
        assert res["identities"] == 1 and app_a.imported["replace_identities"] is True and app_a.imported["data"]["cameras"][0]["id"] == "cam1"
        # push: a subscription receives an alert of a chosen kind through the fake sender
        sent = []
        push.set_sender(lambda sub, payload: sent.append((sub["endpoint"], payload)) or True)
        vap = owner.get("/api/push/vapid").json()
        assert vap["public_key"] and "offline" in vap["kinds"]
        assert owner.post("/api/push/subscribe", json={"subscription": {"endpoint": "https://push.example/abc", "keys": {"p256dh": "x", "auth": "y"}}, "kinds": ["event_policy"]}).status_code == 200
        site_row = db.one(sa.select(db.sites).where(db.sites.c.id == site_a["id"]))
        asyncio.run_coroutine_threadsafe(push.notify_alert(org["id"], site_row, "event_policy", {"id": 7, "camera_id": "cam1", "text": "Unknown truck towing"}), loop).result(5)
        asyncio.run_coroutine_threadsafe(push.notify_alert(org["id"], site_row, "offline", {}), loop).result(5)
        assert len(sent) == 1 and sent[0][1]["title"].endswith("site rule broken") and "/s/" in sent[0][1]["url"]
        push.set_sender(None)
        assert owner.post("/api/push/unsubscribe", json={"endpoint": "https://push.example/abc"}).status_code == 200
        assert owner.get("/api/push/vapid").json()["subscriptions"] == []
    finally:
        fut_a.cancel()
        time.sleep(0.3)   # let the agent's cancellation unwind before the loop stops
        loop.call_soon_threadsafe(loop.stop)
