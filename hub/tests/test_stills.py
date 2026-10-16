"""Each camera's latest picture (stills.py): what a server's agent may store (a JPEG of at most 256 KB, for an enabled
camera of that server), who may fetch it (whoever may view the camera), its caching (ETag, 304), where the UI learns one
exists, and that it goes with the camera and the server. Also the server_status push (agents.py): only when which
cameras stream changes, and only to the users who see that server (/api/fleet/ws)."""
import asyncio
import base64
import time

import pytest
import sqlalchemy as sa

from hub import agents, api, auth, cameras, db, stills
from test_access import _login, server
from test_cameras_registry import FakeWS, _settle

JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 8


def still(cam: str, ts: float, data: bytes = JPEG) -> dict:
    return {"t": "still", "cam": cam, "ts": ts, "jpeg": base64.b64encode(data).decode()}


@pytest.fixture(scope="module")
def world(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": "Stills Co", "slug": "stills-co"}).json()["id"]
    other = root.post("/api/orgs", json={"name": "Other Stills Co", "slug": "other-stills-co"}).json()["id"]
    a, b = server(oid, "Yard"), server(oid, "Office")
    elsewhere = server(other, "Elsewhere")
    cameras.sync(a, [{"id": "cam1", "name": "Gate"}, {"id": "cam2", "name": "Lane"}])
    cameras.sync(b, [{"id": "cam9", "name": "Desk"}])
    cameras.sync(elsewhere, [{"id": "cam1", "name": "Theirs"}])
    root.post(f"/api/orgs/{oid}/members", json={"email": "viewer@stills.example", "role": "viewer", "password": "viewer-pass-123"})
    r = root.post(f"/api/orgs/{oid}/members", json={"email": "office@stills.example", "role": "viewer", "password": "office-pass-123"}).json()
    root.put(f"/api/orgs/{oid}/members/{r['id']}/access", json={"all_sites": False, "location_ids": [b["location_id"]]})
    root.post(f"/api/orgs/{other}/members", json={"email": "out@stills.example", "role": "admin", "password": "outsider-pass-1"})
    return {"oid": oid, "a": a, "b": b, "elsewhere": elsewhere, "root": root,
            "viewer": _login(client, "viewer@stills.example", "viewer-pass-123"),
            "office": _login(client, "office@stills.example", "office-pass-123"),
            "outsider": _login(client, "out@stills.example", "outsider-pass-1")}


def _row(server_id: str):
    return db.one(sa.select(db.sites).where(db.sites.c.id == server_id))


def _kept(server_id: str) -> dict:
    return stills.times([server_id]).get(server_id, {})


def _run_agent(server_id: str, frames: list[dict], gap: float = 0.0) -> FakeWS:
    """Feed `frames` through the real agent loop (registry._loop) of a connected server; returns its socket."""
    async def main():
        ws = FakeWS()
        conn = agents.AgentConn(ws, _row(server_id), None, "token")
        task = asyncio.create_task(agents.registry._loop(conn))
        try:
            ws.feed({"t": "hello", "hostname": "h", "site_version": "1.0"})
            await _settle(ws)
            for f in frames:
                ws.feed(f)
                await _settle(ws)
        finally:
            await ws.close()
            await task
            agents.registry.by_site.pop(server_id, None)
        return ws
    old = agents.STILL_MIN_GAP_S
    agents.STILL_MIN_GAP_S = gap
    try:
        return asyncio.run(main())
    finally:
        agents.STILL_MIN_GAP_S = old


def test_upload_validation(world):
    sid, t = world["a"]["id"], time.time()
    ws = _run_agent(sid, [
        still("cam1", t),
        still("cam2", t, b"\x89PNG\r\n\x1a\n" + b"x" * 100),                       # not a JPEG
        still("cam2", t, b"\xff\xd8\xff" + b"x" * (stills.MAX_BYTES + 1)),          # over 256 KB
        {"t": "still", "cam": "cam2", "ts": t, "jpeg": "not base64!"},
        {"t": "still", "cam": "../cam2", "ts": t, "jpeg": base64.b64encode(JPEG).decode()},
        still("cam9", t),                                                           # a camera of another server
        still("nope", t),                                                           # no such camera
        {"t": "still", "cam": "cam2", "ts": "soon", "jpeg": base64.b64encode(JPEG).decode()},
    ])
    assert next(f for f in ws.sent if f["t"] == "welcome")["stills"] is True   # agents only send to a hub that asks
    assert _kept(sid) == {"cam1": pytest.approx(t)} and _kept(world["b"]["id"]) == {}
    assert stills.get(sid, "cam1")["jpeg"] == JPEG
    # newer replaces, older is ignored; one camera's stills are taken in at most every STILL_MIN_GAP_S
    _run_agent(sid, [still("cam1", t + 60, JPEG + b"new"), still("cam1", t + 30, JPEG + b"old")])
    assert _kept(sid)["cam1"] == pytest.approx(t + 60) and stills.get(sid, "cam1")["jpeg"] == JPEG + b"new"
    _run_agent(sid, [still("cam1", t + 120), still("cam1", t + 180)], gap=60)
    assert _kept(sid)["cam1"] == pytest.approx(t + 120)


def test_old_stills_purged_after_30_days(world):
    sid, t = world["a"]["id"], time.time()
    with db.engine().begin() as conn:
        stills.drop(conn, sid)   # an earlier test's newer cam1 still would make this older one ignored
    _run_agent(sid, [still("cam1", t - stills.MAX_AGE_S - 60), still("cam2", t - 3600)])
    stills.purge_old(t)
    assert set(_kept(sid)) == {"cam2"}   # a camera down for over 30 days has no last picture any more


def test_access_and_caching(world):
    sid, t = world["a"]["id"], time.time()
    stills.accept(sid, still("cam2", t))
    url = f"/api/servers/{sid}/cameras/cam2/still"
    r = world["viewer"].get(url)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content == JPEG
    assert r.headers["cache-control"] == "private, max-age=30" and r.headers["etag"] == stills.etag(t)
    r2 = world["viewer"].get(url, headers={"If-None-Match": r.headers["etag"]})
    assert r2.status_code == 304 and r2.content == b"" and r2.headers["etag"] == r.headers["etag"]
    assert world["viewer"].get(url, headers={"If-None-Match": '"1.000"'}).status_code == 200
    # another customer's admin, a member not granted that Site, nobody signed in
    assert world["outsider"].get(url).status_code == 403
    assert world["office"].get(url).status_code == 403
    assert world["office"].get(f"/api/servers/{world['b']['id']}/cameras/cam9/still").status_code == 404   # granted, no picture yet
    assert world["outsider"].get(f"/api/servers/{world['elsewhere']['id']}/cameras/cam2/still").status_code == 404   # not that server's camera
    assert world["viewer"].get(f"/api/servers/s_nope/cameras/cam2/still").status_code == 404
    from fastapi.testclient import TestClient
    assert TestClient(api.app, base_url="http://testserver").get(url).status_code == 401
    # the lists say a picture exists (never the picture itself)
    lid = world["a"]["location_id"]
    got = {c["camera_id"]: c for c in world["viewer"].get(f"/api/locations/{lid}/cameras").json()}
    assert got["cam2"]["still_ts"] == pytest.approx(t)
    card = next(s for s in world["viewer"].get(f"/api/fleet?org={world['oid']}").json()["orgs"][0]["sites"] if s["id"] == sid)
    assert card["stills"]["cam2"] == pytest.approx(t)


def test_deleted_with_the_camera_and_the_server(client, world):
    root, oid = world["root"], world["oid"]
    s = server(oid, "Shed")
    sid, t = s["id"], time.time()
    cameras.sync(s, [{"id": "c1", "name": "One"}, {"id": "c2", "name": "Two"}, {"id": "c3", "name": "Three"}])
    for c in ("c1", "c2", "c3"):
        assert stills.accept(sid, still(c, t))
    hb = lambda cams, disabled: {"t": "heartbeat", "now": time.time(), "summary": {"cameras": cams, "disabled": disabled}}  # noqa: E731
    # c3 is removed at the server, c2 disabled: their pictures go; a still for either is refused from then on
    _run_agent(sid, [hb([{"id": "c1", "name": "One"}], [{"id": "c2", "name": "Two"}]), still("c2", t + 60), still("c3", t + 60)])
    assert _kept(sid) == {"c1": pytest.approx(t)}
    assert root.delete(f"/api/servers/{sid}").status_code == 200
    assert db.rows(sa.select(db.camera_stills).where(db.camera_stills.c.server_id == sid)) == []


class Browser:
    """A /api/fleet/ws client on the test's own event loop (the route function called directly)."""

    def __init__(self, cookie: str):
        self.cookies = {auth.COOKIE: cookie}
        self.headers: dict = {}
        self.got: list[dict] = []

    async def accept(self):
        pass

    async def send_json(self, m):
        self.got.append(m)

    async def close(self, code=1000, reason=""):
        raise AssertionError(f"refused: {code}")


def test_server_status_on_change_and_only_to_who_sees_it(world):
    a, b, oid = world["a"], world["b"], world["oid"]
    ready = lambda *up: {"t": "heartbeat", "now": time.time(), "summary": {"cameras": [  # noqa: E731
        {"id": c, "name": c, "stream_ready": c in up} for c in ("cam1", "cam2")]}}

    async def main():
        everyone = Browser(world["viewer"].cookies.get(auth.COOKIE))
        office = Browser(world["office"].cookies.get(auth.COOKIE))   # granted b's Site only
        socks = [asyncio.create_task(api.fleet_ws(x, oid)) for x in (everyone, office)]
        await asyncio.sleep(0.05)
        ws = FakeWS()
        conn = agents.AgentConn(ws, _row(a["id"]), None, "token")
        conn2 = agents.AgentConn(FakeWS(), _row(b["id"]), None, "token")
        loops = [asyncio.create_task(agents.registry._loop(conn)), asyncio.create_task(agents.registry._loop(conn2))]
        try:
            ws.feed({"t": "hello", "hostname": "h", "site_version": "1.0"})
            for f in (ready("cam1"), ready("cam1"), ready("cam1", "cam2"), ready("cam1", "cam2"), ready("cam2"),
                      {"t": "heartbeat", "now": time.time(), "summary": {}}):   # an empty summary says nothing
                ws.feed(f)
            await _settle(ws)
            conn2.ws.feed({"t": "hello", "hostname": "h2", "site_version": "1.0"})
            conn2.ws.feed({"t": "heartbeat", "now": time.time(), "summary": {"cameras": [{"id": "cam9", "stream_ready": True}]}})
            await _settle(conn2.ws)
            await asyncio.sleep(0.05)
        finally:
            for w in (ws, conn2.ws):
                await w.close()
            await asyncio.gather(*loops)
            for t in socks:
                t.cancel()
            await asyncio.gather(*socks, return_exceptions=True)
            for sid in (a["id"], b["id"]):
                agents.registry.by_site.pop(sid, None)
        return everyone.got, office.got

    everyone, office = asyncio.run(main())
    mine = [(m["cameras_up"], m["cameras_total"]) for m in everyone if m["type"] == "server_status" and m["site_id"] == a["id"]]
    assert mine == [(1, 2), (2, 2), (1, 2)]   # the first heartbeat, then only the changes (cam2 up; cam1 down, cam2 up)
    st = next(m for m in everyone if m["type"] == "server_status" and m["site_id"] == a["id"])
    assert st["location_id"] == a["location_id"] and st["site_name"] == "Yard"
    assert any(m["type"] == "server_status" and m["site_id"] == b["id"] for m in everyone)
    # the member granted only the Office Site hears the Office server and nothing of the Yard's
    assert {m["site_id"] for m in office} == {b["id"]} and any(m["type"] == "server_status" for m in office)
