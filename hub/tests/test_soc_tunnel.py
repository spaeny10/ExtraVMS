"""The SOC through a real tunnel: a verified event published by an enrolled server at a monitored Site opens an
incident (agents._loop -> soc.on_event), the relay reaches the site (POST /api/cameras/{id}/relay) and is logged,
and resolving as a false alarm sends PUT /api/events/{id}/feedback {verdict: false_alarm} to the site."""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

import httpx
import sqlalchemy as sa

from hub import auth, cameras, db
from test_fleet import hub_server  # noqa: F401  (module-scoped fixture: a real hub on a port)

PW = "tunnel-soc-pass-1"


def fake_site():
    """Records what the hub asks of it; answers the two SOC calls."""
    async def site_app(scope, receive, send):
        body = b""
        while True:
            m = await receive()
            body += m.get("body", b"")
            if not m.get("more_body"):
                break
        path, method = scope["path"], scope["method"]
        site_app.calls.append((method, path, json.loads(body) if body else None, dict((k.decode(), v.decode()) for k, v in scope["headers"])))
        if method == "POST" and path.startswith("/api/cameras/") and path.endswith("/relay"):
            status, out = 200, {"ok": True, "on": (json.loads(body) or {}).get("on")}
        elif method == "PUT" and path.startswith("/api/events/") and path.endswith("/feedback"):
            status, out = 200, {"id": int(path.split("/")[3]), "feedback": json.loads(body)}
        else:
            status, out = 404, {"detail": "nope"}
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": json.dumps(out).encode()})
    site_app.calls = []
    return site_app


async def _summary():
    return {"cameras": [{"id": "cam1", "name": "Gate", "stream_ready": True}], "today": {}, "queues": {"verify": 0, "synopsis": 0}}


def _login(base, email, pw):
    c = httpx.Client(base_url=base, timeout=30)
    assert c.post("/auth/login", json={"email": email, "password": pw}).status_code == 200
    return c


def test_soc_through_the_tunnel(hub_server, superuser):  # noqa: F811
    from nvr import hub_agent
    from nvr.db import db as site_db
    from hub import agents
    base = hub_server
    owner = _login(base, superuser["email"], superuser["password"])
    org = owner.post("/api/orgs", json={"name": "Tunnel SOC", "slug": "tunnel-soc"}).json()
    auth.create_user("op@tunnelsoc.example", PW)
    auth.set_soc_role("op@tunnelsoc.example", "operator")
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    app = fake_site()
    site_db.set_setting("hub_url", base.replace("http://", "ws://") + "/agent")
    site_db.set_setting("hub_token", None)
    site_db.set_setting("hub_claim", None)
    agent = hub_agent.HubAgent(app, SimpleNamespace(pipeline=SimpleNamespace(subscribers=set()), ingests={}, health=None),
                               summary_fn=lambda st, since: _summary())
    fut = asyncio.run_coroutine_threadsafe(agent.run(), loop)
    try:
        for _ in range(100):
            time.sleep(0.05)
            code = agent.status()["claim_code"]
            if code and code in agents.registry.pending:
                break
        site = owner.post(f"/api/orgs/{org['id']}/sites/claim", json={"code": agent.status()["claim_code"], "name": "Depot", "location": ""}).json()
        for _ in range(100):
            time.sleep(0.05)
            if agent.connected and agent.enrolled and agents.registry.get(site["id"]) is not None:
                break
        assert agents.registry.get(site["id"]) is not None, agent.status()
        row = db.one(sa.select(db.sites).where(db.sites.c.id == site["id"]))
        lid = row["location_id"]
        cameras.sync(row, [{"id": "cam1", "name": "Gate"}], full=False)
        assert owner.patch(f"/api/locations/{lid}", json={"timezone": "America/Chicago"}).status_code == 200
        assert owner.put(f"/api/locations/{lid}/monitoring", json={"monitored": True}).json()["armed"] is True
        op = _login(base, "op@tunnelsoc.example", PW)

        # a verified event published by the site opens an incident
        subs = agent.state.pipeline.subscribers
        for _ in range(50):
            if subs:
                break
            time.sleep(0.05)
        evt = {"type": "event", "event": {"id": 501, "camera_id": "cam1", "camera_class": "person", "start_ts": time.time(),
                                          "status": "verified", "priority": "high", "synopsis": "a person climbs the fence"}}
        for q in list(subs):
            loop.call_soon_threadsafe(q.put_nowait, evt)
        inc = None
        for _ in range(100):
            got = op.get(f"/api/soc/incidents?location={lid}").json()
            if got:
                inc = got[0]
                break
            time.sleep(0.05)
        assert inc and inc["priority"] == "high" and inc["title"] == "Gate · person: a person climbs the fence"
        base_i = f"/api/soc/incidents/{inc['id']}"
        assert op.post(f"{base_i}/claim").status_code == 200

        # deterrence: the relay call reaches the site, as the operator, and is logged
        r = op.post(f"{base_i}/relay", json={"server_id": site["id"], "camera_id": "cam1", "on": True})
        assert r.status_code == 200 and r.json()["ok"] is True and r.json()["result"] == {"ok": True, "on": True}
        method, path, body, headers = app.calls[-1]
        assert (method, path, body) == ("POST", "/api/cameras/cam1/relay", {"on": True})
        assert headers.get("x-hub-user") == "op@tunnelsoc.example" and headers.get("x-hub-role") == "operator"
        assert op.post(f"{base_i}/relay", json={"server_id": site["id"], "camera_id": "nope", "on": True}).status_code == 404
        assert op.post(f"{base_i}/relay", json={"server_id": "s_elsewhere", "camera_id": "cam1", "on": True}).status_code == 404
        relay_log = [r for r in op.get(base_i).json()["log"] if r["action"] == "relay"]
        assert len(relay_log) == 1 and relay_log[0]["detail"] == {"server_id": site["id"], "camera_id": "cam1", "on": True, "ok": True,
                                                                   "status": 200, "error": None}

        # a false alarm goes back to the site so its baseline learns
        r = op.post(f"{base_i}/resolve", json={"disposition": "false_alarm", "notes": "shadow of a tree"})
        assert r.status_code == 200 and r.json()["feedback"] == {"sent": 1, "failed": 0}
        method, path, body, _ = app.calls[-1]
        assert (method, path, body) == ("PUT", "/api/events/501/feedback", {"verdict": "false_alarm", "note": "shadow of a tree"})
        ie = db.one(sa.select(db.incident_events).where(db.incident_events.c.incident_id == inc["id"]))
        assert ie["feedback_state"] == "sent"
        # the site re-publishes the event with its new verdict: no new incident
        evt2 = {"type": "event", "event": {**evt["event"], "feedback": {"verdict": "false_alarm"}}}
        for q in list(subs):
            loop.call_soon_threadsafe(q.put_nowait, evt2)
        time.sleep(0.3)
        assert len(db.rows(sa.select(db.incidents).where(db.incidents.c.location_id == lid))) == 1
    finally:
        fut.cancel()
        time.sleep(0.3)
        loop.call_soon_threadsafe(loop.stop)
