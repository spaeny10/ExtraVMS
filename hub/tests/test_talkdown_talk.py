"""Hold to Talk through the hub (WebSocket /s/<server>/api/speakers/<id>/talk): the browser's audio relayed up the tunnel
as a "ws" stream in order, the site's status messages relayed down, the hub identity on the stream, the role a POST
to the path needs, refusals the page can read (foreign pages, offline or old servers), one audit row per live talk."""
import json

import pytest
import sqlalchemy as sa
from starlette.websockets import WebSocketDisconnect
from tunnelproto import Stream

from hub import db
from hub.agents import WsAssembler, registry, ws_pieces
from hub.roles import required_role
from test_access import _login, server


class TalkConn:
    """A server answering /api/speakers/<id>/talk like backend/nvr/speakers_api.talk, over the tunnel's ws framing."""

    def __init__(self, s: dict, location: dict, caps=("events", "camweb", "talk")):
        self.site_id, self.site, self.last_seen = s["id"], s, 1e12
        self.location = location
        self.subscribers: set = set()
        self.hello = {"caps": list(caps)}
        self.opened: list[tuple[str, str, dict, bool]] = []
        self.up: list[tuple[bool, bytes]] = []   # what the browser sent, as the site got it
        self.stream: Stream | None = None
        self.asm = WsAssembler()
        self.aborted: list[str] = []

    def caps(self):
        return self.hello["caps"]

    def _say(self, msg: dict) -> None:
        for piece in ws_pieces(json.dumps(msg)):
            self.stream.push(piece)

    async def open(self, method, path, query, headers, body=True, ws=False):
        self.opened.append((method, path, headers, ws))
        s = self.stream = Stream(len(self.opened) * 2 + 1)
        self.audio = 0
        s.status, s.headers = 101, {}
        s.head.set()
        self._say({"type": "talk", "state": "connecting", "speaker_id": "spk1", "speaker": "Back wall", "camera_ids": ["cam1", "cam2"],
                   "replaced": "Leave now"})
        return s

    async def send_bytes(self, data: bytes):
        got = self.asm.feed(data[4:])
        if got is None:
            return
        self.up.append(got)
        binary, payload = got
        self.audio += binary
        if binary and self.audio == 1:   # the speaker is playing it once audio comes
            self._say({"type": "talk", "state": "live"})
        elif not binary and json.loads(payload).get("type") == "end":
            self._say({"type": "talk", "state": "ended", "result": "played", "reason": "released", "error": None, "played_s": 2.5})
            self.stream.push(None)

    async def send(self, frame):
        pass

    def finish(self, s):
        pass

    async def abort(self, s, reason):
        self.aborted.append(reason)
        await s.abort(reason)


def setup_site(client, superuser, slug: str, caps=("events", "camweb", "talk")):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": f"Talk {slug}", "slug": f"talk-{slug}"}).json()["id"]
    yard = root.post(f"/api/orgs/{oid}/locations", json={"name": f"{slug} Yard"}).json()
    srv = server(oid, f"{slug} NVR", yard["id"])
    conn = TalkConn(srv, {"id": yard["id"], "name": f"{slug} Yard"}, caps)
    registry.by_site[srv["id"]] = conn
    users = {}
    for role in ("viewer", "operator", "admin"):
        email = f"{role}@{slug}.example"
        r = root.post(f"/api/orgs/{oid}/members", json={"email": email, "role": role, "password": "talk-pass-12345", "all_sites": True})
        assert r.status_code == 200, r.text
        users[role] = _login(client, email, "talk-pass-12345")
    return srv, yard, conn, users


def refused(c, path, **kw) -> tuple[str, int]:
    with c.websocket_connect(path, **kw) as sock:
        m = sock.receive_json()
        assert m["type"] == "talk" and m["state"] == "refused", m
        with pytest.raises(WebSocketDisconnect) as e:
            sock.receive_json()
        return m["error"], e.value.code


def talk_rows(srv):
    return db.rows(sa.select(db.audit_log).where(db.audit_log.c.site_id == srv["id"], db.audit_log.c.method == "WS").order_by(db.audit_log.c.ts))


def frames(n: int) -> list[bytes]:
    return [bytes([i]) * 640 for i in range(n)]   # 40 ms of 8 kHz 16-bit PCM each


def test_live_talk_relayed_and_audited(client, superuser):
    srv, yard, conn, users = setup_site(client, superuser, "relay")
    path = f"/s/{srv['id']}/api/speakers/spk1/talk"
    try:
        with users["admin"].websocket_connect(path) as sock:
            hello = sock.receive_json()
            assert hello["state"] == "connecting" and hello["speaker"] == "Back wall"
            for f in frames(4):
                sock.send_bytes(f)
            assert sock.receive_json() == {"type": "talk", "state": "live"}
            sock.send_text(json.dumps({"type": "end"}))
            end = sock.receive_json()
            assert end["state"] == "ended" and end["result"] == "played" and end["played_s"] == 2.5
        method, api_path, headers, ws = conn.opened[0]
        assert (method, api_path, ws) == ("GET", "/api/speakers/spk1/talk", True)
        assert headers["x-hub-role"] == "admin" and headers["x-hub-user"] == "admin@relay.example" and headers["x-hub-site"] == srv["id"]
        assert headers["x-hub-location"] == yard["id"] and "cookie" not in headers
        assert [p for b, p in conn.up if b] == frames(4)   # every frame, in order
        rows = talk_rows(srv)
        assert len(rows) == 1   # one row per live talk
        a = rows[0]
        assert a["action"] == "talk-down: Live talk on Back wall" and a["user_email"] == "admin@relay.example" and a["status"] == 101
        assert a["path"] == path
        assert a["detail"]["talkdown"] == {"speaker_id": "spk1", "speaker": "Back wall", "message": "Live talk", "live": True,
                                           "camera_ids": ["cam1", "cam2"], "replaced": "Leave now", "duration_s": 2.5, "result": "played",
                                           "location_id": yard["id"], "location_name": "relay Yard"}
        # the page goes without letting go: still one row, the time live measured at the hub
        with users["admin"].websocket_connect(path) as sock:
            sock.receive_json()
            sock.send_bytes(frames(1)[0])
            assert sock.receive_json()["state"] == "live"
        rows = talk_rows(srv)
        assert len(rows) == 2 and rows[1]["detail"]["talkdown"]["result"] == "ended"
        assert 0 <= rows[1]["detail"]["talkdown"]["duration_s"] < 5 and conn.aborted   # the stream is closed at the site
    finally:
        registry.by_site.pop(srv["id"], None)


def test_live_talk_refusals(client, superuser):
    srv, _, conn, users = setup_site(client, superuser, "refuse")
    path = f"/s/{srv['id']}/api/speakers/spk1/talk"
    try:
        # a viewer only watches; nothing reaches the server and nothing is audited
        why, code = refused(users["viewer"], path)
        assert code == 4403 and why.startswith("needs the ")
        # another page driving the user's session: refused before the socket opens
        with pytest.raises(WebSocketDisconnect) as e:
            with users["admin"].websocket_connect(path, headers={"origin": "https://evil.example"}) as sock:
                sock.receive_json()
        assert e.value.code == 4403
        # not signed in
        from fastapi.testclient import TestClient
        with pytest.raises(WebSocketDisconnect) as e:
            with TestClient(client.app).websocket_connect(path) as sock:
                sock.receive_json()
        assert e.value.code == 4401
        assert refused(users["admin"], f"/s/{srv['id']}/api/speakers/Bad.Id/talk") == ("unknown speaker", 4404)
        assert conn.opened == [] and talk_rows(srv) == []
        # an old server without live talk, and an offline one
        conn.hello["caps"] = ["events", "camweb"]
        why, code = refused(users["admin"], path)
        assert code == 4501 and "update" in why
        registry.by_site.pop(srv["id"], None)
        assert refused(users["admin"], path) == ("the server is offline", 4503)
    finally:
        registry.by_site.pop(srv["id"], None)


def test_live_talk_is_an_operator_act(client, superuser):
    assert required_role("POST", "/api/speakers/spk1/talk") == "operator"
    """Passes once roles.py lists talk with play / stop / describe (operator); expected to fail until then."""
    srv, _, conn, users = setup_site(client, superuser, "oper")
    try:
        with users["operator"].websocket_connect(f"/s/{srv['id']}/api/speakers/spk1/talk") as sock:
            assert sock.receive_json()["state"] == "connecting"
        assert conn.opened and conn.opened[0][2]["x-hub-role"] == "operator"
    finally:
        registry.by_site.pop(srv["id"], None)
