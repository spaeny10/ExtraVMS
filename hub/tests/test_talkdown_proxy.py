"""Talk-down through the hub proxy (/s/<server>/api/speakers/...): operators and up may play and stop, configuring speakers
and the message library needs admin, viewers may only watch; every play is audited with who, the Site, the speaker and
the message (from the server's answer)."""
import json

import sqlalchemy as sa
from tunnelproto import Stream

from hub import db
from hub.agents import registry
from test_access import _login, server


class SpeakerConn:
    """A server answering the talk-down routes like backend/nvr/speakers_api.py; remembers what it was sent."""

    def __init__(self, s: dict, location: dict):
        self.site_id, self.site, self.last_seen = s["id"], s, 1e12
        self.location = location
        self.subscribers: set = set()
        self.sent: list[tuple[str, str, dict, dict | None]] = []
        self.refuse = False

    async def request(self, method, path, query, headers, body):
        self.sent.append((method, path, headers, json.loads(body) if body else None))
        s = Stream(len(self.sent))
        if path.endswith("/play") and self.refuse:
            s.status, out = 409, {"detail": "the speaker is turned off in its settings"}
        elif path.endswith("/play"):
            sid = path.split("/")[3]
            s.status, out = 200, {"speaker_id": sid, "speaker": "Back wall", "camera_ids": ["cam1", "cam2"], "message": "Leave now",
                                  "message_id": 1, "replaced": None, "status": {"state": "connecting"}}
        elif path.endswith("/stop"):
            s.status, out = 200, {"stopped": True, "status": {"state": "idle"}}
        else:
            s.status, out = 200, {"ok": True}
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


def test_talkdown_roles_and_audit(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": "Talk Co", "slug": "talk-co"}).json()["id"]
    yard = root.post(f"/api/orgs/{oid}/locations", json={"name": "Talk Yard"}).json()
    srv = server(oid, "Yard NVR", yard["id"])
    conn = SpeakerConn(srv, {"id": yard["id"], "name": "Talk Yard"})
    registry.by_site[srv["id"]] = conn
    users = {}
    for role in ("viewer", "operator", "admin"):
        email = f"{role}@talk.example"
        r = root.post(f"/api/orgs/{oid}/members", json={"email": email, "role": role, "password": "talk-pass-12345", "all_sites": True})
        assert r.status_code == 200, r.text
        users[role] = _login(client, email, "talk-pass-12345")
    base = f"/s/{srv['id']}/api"
    try:
        v, op, adm = users["viewer"], users["operator"], users["admin"]
        assert v.get(f"{base}/speakers").status_code == 200                       # watching: the list and status
        assert v.post(f"{base}/speakers/spk1/play", json={"message_id": 1}).status_code == 403
        assert v.post(f"{base}/speakers/spk1/stop").status_code == 403
        # an operator plays and stops
        r = op.post(f"{base}/speakers/spk1/play", json={"message_id": 1})
        assert r.status_code == 200, r.text
        method, path, headers, body = conn.sent[-1]
        assert (method, path, body) == ("POST", "/api/speakers/spk1/play", {"message_id": 1})
        assert headers["x-hub-role"] == "operator" and headers["x-hub-user"] == "operator@talk.example"
        assert op.post(f"{base}/speakers/spk1/stop").status_code == 200
        # configuring needs admin
        sp = {"id": "spk1", "name": "Back wall", "host": "192.168.105.101", "camera_ids": ["cam1"]}
        assert op.put(f"{base}/speakers/spk1", json=sp).status_code == 403
        assert op.post(f"{base}/speakers/spk1/check").status_code == 403
        assert op.delete(f"{base}/speakers/spk1").status_code == 403
        assert op.post(f"{base}/talkdown/messages", json={"name": "x", "text": "y"}).status_code == 403
        assert op.delete(f"{base}/talkdown/messages/1").status_code == 403
        assert op.post(f"{base}/cameras/cam7/to-speaker", json={}).status_code == 403
        assert adm.put(f"{base}/speakers/spk1", json=sp).status_code == 200
        assert adm.post(f"{base}/talkdown/messages", json={"name": "x", "text": "y"}).status_code == 200

        rows = db.rows(sa.select(db.audit_log).where(db.audit_log.c.site_id == srv["id"]).order_by(db.audit_log.c.ts))
        play = [x for x in rows if x["path"].endswith("/speakers/spk1/play")]
        assert len(play) == 1   # the viewer's refused attempt never reached the server
        a = play[0]
        assert a["action"] == "talk-down: Leave now on Back wall" and a["user_email"] == "operator@talk.example" and a["status"] == 200
        t = a["detail"]["talkdown"]
        assert t == {"speaker_id": "spk1", "speaker": "Back wall", "message": "Leave now", "message_id": 1, "camera_ids": ["cam1", "cam2"],
                     "replaced": None, "location_id": yard["id"], "location_name": "Talk Yard"}
        assert any(x["action"] == "POST speakers/spk1/stop" for x in rows)
        # the server refuses: audited as the plain request, with its status
        conn.refuse = True
        assert op.post(f"{base}/speakers/spk1/play", json={"message_id": 1}).status_code == 409
        last = db.rows(sa.select(db.audit_log).where(db.audit_log.c.site_id == srv["id"]).order_by(db.audit_log.c.ts.desc()).limit(1))[0]
        assert last["action"] == "POST speakers/spk1/play" and last["status"] == 409 and "talkdown" not in last["detail"]
    finally:
        registry.by_site.pop(srv["id"], None)
