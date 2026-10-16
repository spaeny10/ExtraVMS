"""Live viewing in the audit log (viewing.py): WHEP offers through the proxy, retries and SD/HD switches included, make one
"Viewed <camera>" row per session; a gap of more than 10 minutes starts a new one; the index survives a restart through
the database; the teardown beacon adds the viewing time; a Direct-on-LAN view gets a client-reported row from the
beacons alone; hang-ups and other writes are audited as before."""
import json
import time

import pytest
import sqlalchemy as sa
from tunnelproto import Stream

from hub import cameras, db, telemetry, viewing
from hub.agents import registry
from test_access import _login, server


class WhepConn:
    """A server answering WHEP offers (201 + an SDP answer) and hang-ups (200)."""

    def __init__(self, s: dict):
        self.site_id, self.site, self.last_seen, self.location = s["id"], s, 1e12, None
        self.subscribers: set = set()
        self.sent: list[tuple[str, str]] = []

    async def request(self, method, path, query, headers, body):
        self.sent.append((method, path))
        s = Stream(len(self.sent))
        s.status = 201 if method == "POST" and "/whep/" in path else 200
        s.headers = {"content-type": "application/sdp" if s.status == 201 else "application/json"}
        s.head.set()
        s.push(b"v=0\r\n" if s.status == 201 else b"{}")
        s.push(None)
        return s

    async def send(self, frame):
        pass

    def finish(self, s):
        pass

    async def abort(self, s, reason):
        await s.abort(reason)


@pytest.fixture()
def world(client, superuser):
    viewing.reset()
    telemetry.reset()
    root = _login(client, superuser["email"], superuser["password"])
    slug = f"view-{time.time_ns()}"
    oid = root.post("/api/orgs", json={"name": f"View Co {slug}", "slug": slug}).json()["id"]
    srv = server(oid, "Gatehouse")
    cameras.sync(srv, [{"id": "cam1", "name": "Front gate"}, {"id": "cam2", "name": "Lane"}])
    email = f"watcher@{slug}.example"
    root.post(f"/api/orgs/{oid}/members", json={"email": email, "role": "viewer", "password": "watcher-pass-12"})
    conn = WhepConn(srv)
    registry.by_site[srv["id"]] = conn
    yield {"oid": oid, "sid": srv["id"], "root": root, "v": _login(client, email, "watcher-pass-12"), "conn": conn}
    registry.by_site.pop(srv["id"], None)
    viewing.reset()


def offer(w, path: str):
    r = w["v"].post(f"/s/{w['sid']}/api/whep/{path}", content=b"v=0\r\n", headers={"Content-Type": "application/sdp"})
    assert r.status_code == 201, r.text
    return r


def rows(w) -> list[dict]:
    return db.rows(sa.select(db.audit_log).where(db.audit_log.c.site_id == w["sid"]).order_by(db.audit_log.c.id))


def views(w) -> list[dict]:
    return [r for r in rows(w) if (r["detail"] or {}).get("kind") == "live_view"]


def beacon(w, **over):
    b = {"event": "end", "server": w["sid"], "camera": "cam1", "path": "cam1_sub", "quality": "sd", "route": "hub", "attempt": 1,
         "stages": {}, "ttff_ms": 900, "duration_s": 125, "ice_timed_out": False} | over
    r = w["v"].post("/api/telemetry/live", content=json.dumps(b), headers={"Content-Type": "application/json"})
    assert r.status_code == 204, r.text


def test_offers_merge_into_one_session(world):
    w = world
    for p in ("cam1_sub", "cam1_sub", "cam1_sub", "cam1"):   # retries, then the seamless switch to HD
        offer(w, p)
    offer(w, "cam2_sub")
    assert w["v"].delete(f"/s/{w['sid']}/api/whep/cam1/0b1c2d3e-aaaa-bbbb-cccc-0123456789ab").status_code == 200   # hang-up: no row
    v = views(w)
    assert [r["action"] for r in v] == ["Viewed Front gate", "Viewed Lane"]
    d = v[0]["detail"]
    assert d["sessions"] == 4 and d["paths"] == ["cam1", "cam1_sub"] and d["quality"] == "hd" and d["route"] == "tunnel"
    assert d["camera"] == "cam1" and d["first"] <= d["last"] and v[0]["method"] == "POST" and v[0]["status"] == 201
    assert len(rows(w)) == 2   # no "POST whep/..." rows, no hang-up rows
    # the Audit page (customer admins) reads it as written
    assert any(r["action"] == "Viewed Front gate" for r in w["root"].get(f"/api/audit?org={w['oid']}").json())


def test_gap_restart_and_duration(world):
    w = world
    offer(w, "cam1_sub")
    # the teardown beacon of an attempt that played adds its viewing time
    beacon(w, duration_s=125)
    v = views(w)
    assert len(v) == 1 and v[0]["detail"]["duration_s"] == 125 and v[0]["action"] == "Viewed Front gate for 2 min"
    # a restart forgets the index: the next offer finds the open row in the database
    viewing.reset()
    offer(w, "cam1_sub")
    assert len(views(w)) == 1 and views(w)[0]["detail"]["sessions"] == 2
    # more than 10 minutes since the last activity: a new session
    key = next(iter(viewing._open))
    viewing._open[key]["detail"]["last"] -= viewing.VIEW_GAP_S + 1
    offer(w, "cam1_sub")
    v = views(w)
    assert len(v) == 2 and v[1]["detail"]["sessions"] == 1 and v[1]["detail"]["duration_s"] == 0
    # an hour-long view ends: its beacon belongs to the session that was active when the attempt began
    viewing._open[key]["detail"]["last"] -= 3000
    beacon(w, duration_s=3700)
    assert views(w)[1]["detail"]["duration_s"] == 3700 and views(w)[1]["action"] == "Viewed Front gate for 62 min"
    # an attempt that never showed a picture adds nothing
    beacon(w, ttff_ms=None, duration_s=50)
    assert views(w)[1]["detail"]["duration_s"] == 3700


def test_direct_view_is_a_client_row(world):
    w = world
    beacon(w, event="first_frame", route="direct", path="cam2", quality="hd", duration_s=None)
    beacon(w, event="first_frame", route="direct", path="cam2", quality="hd", duration_s=None, camera="cam2", attempt=2)
    beacon(w, event="end", route="direct", path="cam2", quality="hd", duration_s=40, camera="cam2")
    # the first report named cam1 (default camera): a separate camera, so its own row
    v = {r["detail"]["camera"]: r for r in views(w)}
    d = v["cam2"]["detail"]
    assert d["route"] == "direct" and d["source"] == "client" and d["sessions"] == 1 and d["duration_s"] == 40
    assert v["cam2"]["action"] == "Viewed Lane for 40 s" and v["cam2"]["method"] is None and v["cam2"]["status"] is None
    assert w["conn"].sent == []   # the hub never saw it
    # a first picture through the hub writes nothing by itself (its offer did)
    before = len(views(w))
    beacon(w, event="first_frame", route="hub", camera="cam2", path="cam2_sub", duration_s=None)
    assert len(views(w)) == before


def test_other_writes_still_audited(world):
    w = world
    assert w["v"].post(f"/s/{w['sid']}/api/whep/cam1_sub/extra", content=b"x").status_code == 201   # not an offer: as any POST
    offer(w, "cam1_sub")
    assert [r["action"] for r in rows(w)] == ["POST whep/cam1_sub/extra", "Viewed Front gate"]
