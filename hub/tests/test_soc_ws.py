"""/api/soc/ws: SOC staff only; a snapshot first (open incidents, roster with the user now available), then live
frames carrying the incident and the server's sound policy; arming changes broadcast too; the user goes offline
when their last socket closes."""
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from hub import soc
from test_soc_incidents import ctx, ev
from test_soc_roles import soc_setup


def _until(sock, kind, n=20):
    for _ in range(n):
        f = sock.receive_json()
        if f["type"] == kind:
            return f
    raise AssertionError(f"no {kind} frame")


def test_soc_socket(client, superuser):
    s = soc_setup(client, superuser, "sock")
    with pytest.raises(WebSocketDisconnect) as e:
        with s["adm"].websocket_connect("/api/soc/ws") as sock:   # a customer admin is not SOC staff
            sock.receive_json()
    assert e.value.code == 4403
    srv, loc = ctx(s)
    with s["op"].websocket_connect("/api/soc/ws") as sock:
        snap = sock.receive_json()
        assert snap["type"] == "snapshot" and isinstance(snap["incidents"], list)
        assert set(snap) >= {"incidents", "presence", "sound", "ring_count", "now"}
        me = next(p for p in snap["presence"] if p["email"] == "op@sock.example")
        assert me["status"] == "available" and me["on_shift"] is True
        # an event at the armed Site (ingested on this thread: frames cross to the socket's loop thread-safely)
        what, inc = soc.ingest(srv, loc, ev(1, "high"))
        f = _until(sock, "incident_opened")
        assert f["incident"]["id"] == inc["id"] and f["incident"]["priority"] == "high" and f["incident"]["location_name"] == "Yard"
        assert f["sound"] == {"ring": True, "repeat_s": 10, "priority": "high"} and f["ring_count"] >= 1
        assert f["event"]["event_id"] == "1" and f["event"]["camera_id"] == "cam1"
        soc.ingest(srv, loc, ev(2, "medium"))
        f = _until(sock, "incident_event_added")
        assert f["incident"]["event_count"] == 2
        s["op"].post(f"/api/soc/incidents/{inc['id']}/claim")
        f = _until(sock, "incident_updated")
        assert f["incident"]["state"] == "claimed" and f["incident"]["claimed_by_email"] == "op@sock.example"
        f = _until(sock, "presence")
        assert f["presence"]["status"] == "engaged" and f["presence"]["incident_id"] == inc["id"]
        s["op"].post(f"/api/soc/incidents/{inc['id']}/resolve", json={"disposition": "authorized"})
        assert _until(sock, "incident_resolved")["incident"]["state"] == "closed"
        # arming changes reach the SOC
        s["root"].post(f"/api/locations/{loc['id']}/arm", json={"mode": "disarm", "until": time.time() + 600, "reason": "open late"})
        f = _until(sock, "arming")
        assert f["site"]["id"] == loc["id"] and f["site"]["armed"] is False and f["site"]["reason"] == "override"
    for _ in range(50):   # the server notices the close on its own loop
        pres = {p["email"]: p for p in s["sup"].get("/api/soc/presence").json()}
        if pres["op@sock.example"]["status"] == "offline":
            break
        time.sleep(0.05)
    assert pres["op@sock.example"]["status"] == "offline" and pres["op@sock.example"]["on_shift"] is False
