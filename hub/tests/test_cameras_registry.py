"""The cameras registry follows what a server reports: hello and heartbeats go through the real agent loop
(registry._loop) on a fake WebSocket, so the hooks in agents.py are what's tested. Old agents (no `disabled`,
empty summaries) must keep working."""
import asyncio
import json
import time

import sqlalchemy as sa

from hub import agents, cameras, db
from test_access import _login, server


class FakeWS:
    """Just enough of a Starlette WebSocket for AgentConn and the registry loop."""

    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.client = type("C", (), {"host": "10.1.2.3"})()

    async def receive(self):
        return await self.inbox.get()

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def send_bytes(self, data):
        pass

    async def close(self, code=1000, reason=""):
        self.inbox.put_nowait({"type": "websocket.disconnect"})

    def feed(self, frame: dict):
        self.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(frame)})


async def _settle(ws: FakeWS):
    """Let the loop drain what was fed."""
    for _ in range(100):
        if ws.inbox.empty():
            await asyncio.sleep(0.01)
            return
        await asyncio.sleep(0.005)


def _cams(server_id):
    return {c["camera_id"]: c for c in cameras.for_server(server_id)}


def _open_alerts(server_id):
    return db.rows(sa.select(db.alerts).where(db.alerts.c.site_id == server_id, db.alerts.c.closed_at.is_(None)))


def test_hello_and_heartbeats_sync_the_registry(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": "Cam Co", "slug": "cam-co"}).json()["id"]
    s = server(oid, "Gatehouse")
    lid = s["location_id"]
    row = db.one(sa.select(db.sites).where(db.sites.c.id == s["id"]))

    async def main():
        ws = FakeWS()
        conn = agents.AgentConn(ws, row, None, "token")
        heard: asyncio.Queue = asyncio.Queue()
        agents.registry.org_subscribers.setdefault(oid, set()).add(heard)
        loop = asyncio.create_task(agents.registry._loop(conn))
        try:
            # hello: ids and names only
            ws.feed({"t": "hello", "hostname": "gate", "site_version": "1.0", "cameras": [{"id": "cam1", "name": "Gate"}, {"id": "cam2", "name": "Lane"}]})
            await _settle(ws)
            welcome = next(f for f in ws.sent if f["t"] == "welcome")
            assert welcome["location"] == "Gatehouse" and welcome["site_id"] == s["id"]
            online = heard.get_nowait()
            assert online["type"] == "site_online" and online["location_id"] == lid and online["location_name"] == "Gatehouse"
            c = _cams(s["id"])
            assert set(c) == {"cam1", "cam2"} and c["cam1"]["name"] == "Gate" and c["cam1"]["location_id"] == lid
            assert c["cam1"]["stream_ready"] is None and c["cam1"]["source"] == "hello"

            # heartbeat (old agent: no `disabled`): status fields arrive
            hb = {"cameras": [{"id": "cam1", "name": "Gate", "stream_ready": True, "problems": [], "bitrate_mbps": 2.5, "metadata": True},
                              {"id": "cam2", "name": "Lane", "stream_ready": False, "problems": ["no frames"]}]}
            ws.feed({"t": "heartbeat", "now": time.time(), "summary": hb})
            ws.feed({"t": "heartbeat", "now": time.time(), "summary": hb})
            await _settle(ws)
            c = _cams(s["id"])
            assert c["cam1"]["stream_ready"] is True and c["cam1"]["bitrate_mbps"] == 2.5 and c["cam1"]["metadata"] is True
            assert c["cam2"]["problems"] == ["no frames"] and c["cam2"]["first_seen_at"] <= c["cam2"]["last_seen_at"]
            assert [a["key"] for a in _open_alerts(s["id"]) if a["kind"] == "camera_down"] == ["cam2"]

            # the Site's camera list says which are live right now (the server is connected)
            got = {x["camera_id"]: x for x in root.get(f"/api/locations/{lid}/cameras").json()}
            assert got["cam1"]["online"] is True and got["cam2"]["online"] is False and got["cam1"]["server_name"] == "Gatehouse"
            card = root.get(f"/api/fleet?org={oid}").json()["orgs"][0]["sites"][0]
            assert card["cameras_total"] == 2 and card["cameras_online"] == 1

            # an empty summary (agent hiccup) says nothing about cameras
            ws.feed({"t": "heartbeat", "now": time.time(), "summary": {}})
            await _settle(ws)
            assert all(not x["missing_since"] for x in _cams(s["id"]).values())

            # cam2 is switched off at the server (new agent: `disabled`), cam1 disappears
            ws.feed({"t": "heartbeat", "now": time.time(), "summary": {"cameras": [], "disabled": [{"id": "cam2", "name": "Lane"}]}})
            await _settle(ws)
            c = _cams(s["id"])
            assert c["cam2"]["enabled"] is False and c["cam2"]["missing_since"] is None
            assert c["cam1"]["missing_since"] is not None and c["cam1"]["enabled"] is True   # missing, never deleted
            assert not [a for a in _open_alerts(s["id"]) if a["kind"] == "camera_down"]   # a disabled camera is not "down"
            # an event on a disabled camera raises nothing
            ws.feed({"t": "event", "msg": {"type": "event", "event": {"id": 501, "camera_id": "cam2", "status": "verified", "priority": "high"}}})
            await _settle(ws)
            assert not [a for a in _open_alerts(s["id"]) if a["kind"] == "event_high"]
            evt = heard.get_nowait()
            assert evt["site_id"] == s["id"] and evt["location_name"] == "Gatehouse"

            # cam1 comes back and cam2 is enabled again
            ws.feed({"t": "heartbeat", "now": time.time(), "summary": {"cameras": [{"id": "cam1", "stream_ready": True}, {"id": "cam2", "name": "Lane 2"}], "disabled": []}})
            await _settle(ws)
            c = _cams(s["id"])
            assert c["cam1"]["missing_since"] is None and c["cam1"]["name"] == "Gate"   # a status-only entry keeps the name
            assert c["cam2"]["enabled"] is True and c["cam2"]["name"] == "Lane 2"
            assert root.get(f"/api/fleet?org={oid}").json()["orgs"][0]["sites"][0]["cameras_total"] == 2
        finally:
            agents.registry.org_subscribers.get(oid, set()).discard(heard)
            await ws.close()
            await loop
            await agents.registry._detach(conn)

    asyncio.run(main())
    assert agents.registry.get(s["id"]) is None


def test_sync_directly():
    srv = {"id": "s_direct", "org_id": "o_direct", "location_id": "l_direct"}
    cameras.sync(srv, [{"id": "a", "name": "A"}, "b", {"name": "no id"}], source="hello")
    assert set(_cams("s_direct")) == {"a", "b"} and _cams("s_direct")["b"]["name"] == "b"
    cameras.sync(srv, [{"id": "a"}], full=False)   # partial lists never mark anything missing
    assert _cams("s_direct")["b"]["missing_since"] is None
    cameras.relocate("s_direct", "l_other")
    assert {c["location_id"] for c in cameras.for_location("l_other")} == {"l_other"} and len(cameras.for_org("o_direct")) == 2
    cameras.delete_server("s_direct")
    assert cameras.for_server("s_direct") == []
