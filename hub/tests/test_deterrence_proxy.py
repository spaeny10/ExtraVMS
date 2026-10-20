"""Deterrence through the hub proxy (/s/<server>/api/cameras/<id>/deterrence...): a camera's strobe / siren / light / relays
are an operator's act like Hold to Talk, reading what a camera has too; viewers get 403 and nothing reaches the server.
The automatic rules and a Milesight's own settings are configuration (admin). Every action is audited with the camera,
the action and its seconds."""
import json

import sqlalchemy as sa
from tunnelproto import Stream

from hub import db
from hub.agents import registry
from hub.roles import required_role
from test_access import _login, server


class DeterrenceConn:
    """A server answering the deterrence routes like backend/nvr/deterrence_api.py; remembers what it was sent."""

    def __init__(self, s: dict, location: dict):
        self.site_id, self.site, self.last_seen = s["id"], s, 1e12
        self.location = location
        self.subscribers: set = set()
        self.sent: list[tuple[str, str, dict, dict | None]] = []

    async def request(self, method, path, query, headers, body):
        self.sent.append((method, path, headers, json.loads(body) if body else None))
        s = Stream(len(self.sent))
        s.status, out = 200, {"camera_id": path.split("/")[3], "available": True, "actions": {"strobe": {"via": "aux"}}, "state": {}}
        s.headers = {"content-type": "application/json"}
        s.head.set()
        s.push(json.dumps(out).encode())
        s.push(None)
        return s

    async def send(self, frame):
        pass

    def finish(self, s):
        pass

    async def abort(self, s, reason):
        await s.abort(reason)


def test_deterrence_role_rules():
    assert required_role("GET", "/api/cameras/cam7/deterrence") == "operator"
    assert required_role("HEAD", "/api/cameras/cam7/deterrence") == "operator"
    assert required_role("GET", "/api/cameras/cam7/deterrence/log") == "operator"
    assert required_role("GET", "/api/cameras/cam7/deterrence/rules") == "operator"
    assert required_role("POST", "/api/cameras/cam7/deterrence") == "operator"
    assert required_role("PUT", "/api/cameras/cam7/deterrence/rules") == "admin"
    assert required_role("POST", "/api/cameras/cam7/deterrence/rules") == "admin"
    assert required_role("PUT", "/api/cameras/cam7/deterrence") == "admin"
    assert required_role("POST", "/api/cameras/cam7/relay") == "operator"
    assert required_role("GET", "/api/cameras/cam7/device/milesight") == "operator"
    assert required_role("PUT", "/api/cameras/cam7/device/milesight") == "admin"
    assert required_role("GET", "/api/cameras/cam7/deterrencex") == "viewer"   # nothing next to it opens up or closes


def test_viewer_403_operator_acts_and_is_audited(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": "Deter Co", "slug": "deter-co"}).json()["id"]
    yard = root.post(f"/api/orgs/{oid}/locations", json={"name": "Deter Yard"}).json()
    srv = server(oid, "Yard NVR", yard["id"])
    conn = DeterrenceConn(srv, {"id": yard["id"], "name": "Deter Yard"})
    registry.by_site[srv["id"]] = conn
    users = {}
    for role in ("viewer", "operator", "admin"):
        email = f"{role}@deter.example"
        r = root.post(f"/api/orgs/{oid}/members", json={"email": email, "role": role, "password": "deter-pass-12345", "all_sites": True})
        assert r.status_code == 200, r.text
        users[role] = _login(client, email, "deter-pass-12345")
    base = f"/s/{srv['id']}/api"
    try:
        v, op, adm = users["viewer"], users["operator"], users["admin"]
        # a viewer neither reads what the camera can set off nor sets it off: refused at the hub, never sent
        assert v.get(f"{base}/cameras/cam7/deterrence").status_code == 403
        assert v.post(f"{base}/cameras/cam7/deterrence", json={"action": "strobe", "seconds": 10}).status_code == 403
        assert v.post(f"{base}/cameras/cam7/deterrence", json={"action": "stop"}).status_code == 403
        assert v.get(f"{base}/cameras/cam7/deterrence/rules").status_code == 403
        assert conn.sent == []
        # an operator does
        assert op.get(f"{base}/cameras/cam7/deterrence").status_code == 200
        r = op.post(f"{base}/cameras/cam7/deterrence", json={"action": "siren", "seconds": 20})
        assert r.status_code == 200, r.text
        method, path, headers, body = conn.sent[-1]
        assert (method, path, body) == ("POST", "/api/cameras/cam7/deterrence", {"action": "siren", "seconds": 20})
        assert headers["x-hub-role"] == "operator" and headers["x-hub-user"] == "operator@deter.example"
        assert op.post(f"{base}/cameras/cam7/deterrence", json={"action": "stop"}).status_code == 200
        # the rules are configuration
        assert op.put(f"{base}/cameras/cam7/deterrence/rules", json={"rules": []}).status_code == 403
        assert adm.put(f"{base}/cameras/cam7/deterrence/rules", json={"rules": []}).status_code == 200
        assert op.put(f"{base}/cameras/cam7/device/milesight", json={"speaker_volume": 40}).status_code == 403

        rows = db.rows(sa.select(db.audit_log).where(db.audit_log.c.site_id == srv["id"]).order_by(db.audit_log.c.ts))
        acts = [x for x in rows if x["path"].endswith("/cameras/cam7/deterrence") and x["method"] == "POST"]
        assert [a["action"] for a in acts] == ["deterrence on cam7: siren on", "deterrence on cam7: stop"]
        assert all(a["user_email"] == "operator@deter.example" and a["status"] == 200 for a in acts)
        assert acts[0]["detail"]["deterrence"] == {"camera_id": "cam7", "action": "siren", "on": True, "seconds": 20, "token": None,
                                                   "location_id": yard["id"], "location_name": "Deter Yard"}
        assert not any(x["user_email"] == "viewer@deter.example" for x in rows)   # refused before the proxy: nothing to audit
    finally:
        registry.by_site.pop(srv["id"], None)
