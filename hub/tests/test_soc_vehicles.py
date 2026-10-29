"""Vehicles around an SOC incident (soc_vehicles.py): the window (10 minutes before the first event, 5 after the
last), every server of the Site asked through the tunnel (a fake connection here), the answers merged with the hub's
camera names, the last answer kept on the incident and shown stale when nobody answers, a slow or old server left
out with its reason, kept out of queue rows, and operators only."""
import asyncio
import json
import time

import sqlalchemy as sa

from hub import agents, db, soc, soc_vehicles
from test_soc_incidents import ctx, ev
from test_soc_roles import soc_setup


class FakeConn:
    def __init__(self, answer=None, status=200, delay=0.0):
        self.answer, self.status, self.delay, self.calls = answer, status, delay, []

    async def call(self, method, path, query="", headers=None, body=None, timeout=15.0):
        self.calls.append((method, path, query, headers))
        await asyncio.sleep(self.delay)
        return self.status, json.dumps(self.answer).encode()


def test_vehicles_around_an_incident(client, superuser, monkeypatch):
    s = soc_setup(client, superuser, "veh")
    srv, loc = ctx(s)
    t0 = time.time() - 600
    _, inc = soc.ingest(srv, loc, {**ev(1, "medium"), "start_ts": t0}, now=t0)
    _, inc = soc.ingest(srv, loc, {**ev(2, "medium"), "start_ts": t0 + 120}, now=t0 + 120)
    assert soc_vehicles.window(soc.get(inc["id"])) == (t0 - 600, t0 + 120 + 300)
    van = {"source": "traffic", "ts": t0 - 60, "camera_id": "cam2", "camera_name": "cam two", "read_id": 7, "plate": "ABC1234",
           "plate_by": "camera", "vehicle": "silver Chevrolet Silverado pickup truck", "vehicle_by": "otso", "direction": "approach",
           "notes": ["Seen 4 times since Oct 3."], "images": ["plate", "vehicle"]}
    gate = {"source": "footage", "ts": t0 + 30, "camera_id": "cam1", "event_id": 9, "plate": "FTG4321", "plate_by": "footage", "notes": []}
    conn = FakeConn({"since": 0, "until": 0, "vehicles": [gate, van], "total": 41})
    monkeypatch.setattr(agents.registry, "get", lambda sid: conn if sid == srv["id"] else None)

    r = s["op"].get(f"/api/soc/incidents/{inc['id']}/vehicles")
    assert r.status_code == 200, r.text
    j = r.json()
    assert [v["plate"] for v in j["vehicles"]] == ["ABC1234", "FTG4321"] and not j["stale"] and j["total"] == 41
    assert j["vehicles"][0]["camera_name"] == "Dock" and j["vehicles"][0]["server_id"] == srv["id"]   # the hub's name
    assert j["servers"] == [{"server_id": srv["id"], "server_name": "Yard NVR", "status": "ok"}]
    method, path, query, headers = conn.calls[0]
    assert (method, path) == ("GET", "/api/traffic/around") and headers["x-hub-role"] == "viewer"
    assert query == f"since={round(t0 - 600, 3)}&until={round(t0 + 420, 3)}&at={round(t0, 3)}"   # the first event
    # kept on the incident, never in its queue row
    kept = db.one(sa.select(db.incidents.c.vehicles).where(db.incidents.c.id == inc["id"]))["vehicles"]
    assert [v["plate"] for v in kept["vehicles"]] == ["ABC1234", "FTG4321"]
    assert "vehicles" not in soc.get(inc["id"]) and "vehicles" not in s["op"].get(f"/api/soc/incidents/{inc['id']}").json()["incident"]

    # the Site goes offline: the kept answer, marked stale
    monkeypatch.setattr(agents.registry, "get", lambda sid: None)
    j = s["op"].get(f"/api/soc/incidents/{inc['id']}/vehicles").json()
    assert j["stale"] and len(j["vehicles"]) == 2 and j["servers"][0]["status"] == "offline"
    # a server too old for the route, and one too slow
    monkeypatch.setattr(agents.registry, "get", lambda sid: FakeConn({"detail": "Not Found"}, status=404))
    assert "too old" in s["op"].get(f"/api/soc/incidents/{inc['id']}/vehicles").json()["servers"][0]["error"]
    monkeypatch.setattr(soc_vehicles, "TIMEOUT_S", 0.05)
    monkeypatch.setattr(agents.registry, "get", lambda sid: FakeConn({"vehicles": []}, delay=3))
    j = s["op"].get(f"/api/soc/incidents/{inc['id']}/vehicles").json()
    assert j["servers"][0]["status"] == "timeout" and j["stale"]
    # operators only: a customer's admin and viewer can't
    assert s["adm"].get(f"/api/soc/incidents/{inc['id']}/vehicles").status_code == 403
    assert s["view"].get(f"/api/soc/incidents/{inc['id']}/vehicles").status_code == 403
