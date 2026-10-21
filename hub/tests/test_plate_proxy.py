""""Read plate" through the hub proxy (/s/<server>/api/events/<id>/plate): one Qwen look at a vehicle's plate is an
operator's act, like naming the vehicle; viewers get 403 and nothing reaches the server. The Site Ask keeps a source's
plate and gives it to the answer model."""
import datetime as dt
import json

from tunnelproto import Stream

from hub import site_ask
from hub.agents import registry
from hub.roles import required_role
from test_access import _login, server


class PlateConn:
    """A server answering POST /api/events/<id>/plate like backend/nvr/api.py read_plate; remembers what it was sent."""

    def __init__(self, s: dict):
        self.site_id, self.site, self.last_seen = s["id"], s, 1e12
        self.location = None
        self.subscribers: set = set()
        self.sent: list[tuple[str, str, dict]] = []

    async def request(self, method, path, query, headers, body):
        self.sent.append((method, path, headers))
        s = Stream(len(self.sent))
        s.status, out = 200, {"result": "read", "plate": "ABC1234", "plate_raw": "ABC-1234", "region": "TX", "confidence": "high"}
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


def test_plate_role_rule():
    assert required_role("POST", "/api/events/12/plate") == "operator"
    assert required_role("GET", "/api/events/12") == "viewer"
    assert required_role("POST", "/api/events/12/platex") == "admin"     # nothing next to it opens up
    assert required_role("PUT", "/api/events/12/plate") == "admin"
    assert required_role("POST", "/api/events/abc/plate") == "admin"


def test_viewer_403_operator_reads(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": "Plate Co", "slug": "plate-co"}).json()["id"]
    yard = root.post(f"/api/orgs/{oid}/locations", json={"name": "Plate Yard"}).json()
    srv = server(oid, "Gate NVR", yard["id"])
    conn = PlateConn(srv)
    registry.by_site[srv["id"]] = conn
    users = {}
    for role in ("viewer", "operator"):
        email = f"{role}@plate.example"
        r = root.post(f"/api/orgs/{oid}/members", json={"email": email, "role": role, "password": "plate-pass-12345", "all_sites": True})
        assert r.status_code == 200, r.text
        users[role] = _login(client, email, "plate-pass-12345")
    base = f"/s/{srv['id']}/api"
    try:
        assert users["viewer"].post(f"{base}/events/41/plate").status_code == 403
        assert conn.sent == []          # refused at the hub, never sent
        r = users["operator"].post(f"{base}/events/41/plate")
        assert r.status_code == 200, r.text
        assert r.json()["plate"] == "ABC1234"
        method, path, headers = conn.sent[-1]
        assert (method, path) == ("POST", "/api/events/41/plate")
        assert headers["x-hub-role"] == "operator" and headers["x-hub-user"] == "operator@plate.example"
    finally:
        registry.by_site.pop(srv["id"], None)


def test_site_ask_keeps_the_plate_and_tells_the_model():
    results = [{"server_id": "s1", "server_name": "Gate NVR", "status": "ok", "data": {"results": [
        {"kind": "event", "event_id": 7, "ts": 1000.0, "camera_id": "c1", "label": "vehicle", "text": "vehicle, 12 s: A white pickup.",
         "plate": "ABC1234", "plate_region": "TX", "plate_conf": "high"},
        {"kind": "event", "event_id": 8, "ts": 900.0, "camera_id": "c1", "label": "vehicle",
         "text": "vehicle, 12 s (license plate XYZ9, high confidence): A van."}]}}]
    merged = site_ask.merge(results, {("s1", "c1"): "Gate"})
    src = site_ask._sources(merged)["items"]
    assert src[0]["plate"] == "ABC1234" and src[0]["plate_region"] == "TX" and src[0]["plate_conf"] == "high"
    tz = dt.timezone.utc
    line = site_ask.evidence_line(merged["items"][0], tz, 2000.0)
    assert "license plate ABC1234 (TX)" in line
    line2 = site_ask.evidence_line(merged["items"][1], tz, 2000.0)
    assert line2.count("license plate") == 1          # the server's line already says it
    assert "described as likely a Nissan Titan" in site_ask.ANSWER_SYSTEM and "never guess or complete a plate" in site_ask.ANSWER_SYSTEM
