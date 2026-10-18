"""Axiom Bridge (bridges.py): enrollment codes, who may manage a Site's bridges, bridged cameras and their listen ports,
set_relay to the host, the relay address rule (central_cameras.py), Apply to camera, bridge_offline, and the splice end
to end under uvicorn: a fake bridge and a fake host relay (websockets clients) with an asyncio TCP echo server as the
camera."""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from hub import agents, alerts, auth, bridges, central_cameras, db, hosts, push
from hub.api import app
from hub.config import settings
from test_central import FakeHost, _login, capacity, wait
from test_fleet_actions import make_site

PW = "bridge-pass-12345"
CAM_PW = "cam-secret-" + "b" * 6
GATEWAY = "10.200.0.1"


@pytest.fixture(scope="module")
def hub():
    """A real hub on a port, and its event loop (the sockets live there; hub coroutines are run in it)."""
    loop = asyncio.new_event_loop()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    t = threading.Thread(target=lambda: loop.run_until_complete(server.serve()), daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    yield SimpleNamespace(base=f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}", loop=loop)
    server.should_exit = True
    t.join(timeout=5)


def in_hub(w, coro_fn):
    async def run():
        return await coro_fn()
    return asyncio.run_coroutine_threadsafe(run(), w.loop).result(30)


def _ws(base: str) -> str:
    return base.replace("http://", "ws://")


@pytest.fixture(scope="module")
def world(hub, superuser):
    """Customer "Bridge Co" with Sites Farm (a bridge-mode central instance) and Barn (none), its people, another
    customer's admin, host A (a FakeHost answering create_instance and set_relay) and the instance's server: a real
    HubAgent tunnel in front of a fake server API (make_site)."""
    from nvr import hub_agent
    from nvr.db import db as site_db
    base = hub.base
    root = _login(base, superuser["email"], superuser["password"])
    org = root.post("/api/orgs", json={"name": "Bridge Co", "slug": "bridge-co"}).json()
    other = root.post("/api/orgs", json={"name": "Other Bridge Co", "slug": "other-bridge-co"}).json()
    farm = root.post(f"/api/orgs/{org['id']}/locations", json={"name": "Farm"}).json()
    barn = root.post(f"/api/orgs/{org['id']}/locations", json={"name": "Barn"}).json()
    elsewhere = root.post(f"/api/orgs/{other['id']}/locations", json={"name": "Elsewhere"}).json()
    people = (("farmadmin", org, "admin", {"all_sites": False, "location_ids": [farm["id"]]}),
              ("barnadmin", org, "admin", {"all_sites": False, "location_ids": [barn["id"]]}),
              ("viewer", org, "viewer", {}), ("operator", org, "operator", {}), ("otheradmin", other, "admin", {}))
    for name, o, role, extra in people:
        r = root.post(f"/api/orgs/{o['id']}/members", json={"email": f"{name}@bridge.example", "role": role, "password": PW, **extra})
        assert r.status_code == 200, r.text
    host = root.post("/api/hub/hosts", json={"name": "Bridge host"}).json()
    host_b = root.post("/api/hub/hosts", json={"name": "Other host"}).json()
    st = SimpleNamespace(relay_ok=True)

    def answer(cmd):
        a = cmd["args"]
        if cmd["op"] == "create_instance":
            return {"ok": True, "detail": "created", "instance": {"id": a["id"], "state": "running", "mode": a["mode"], "gateway": GATEWAY,
                                                                   "relay": {"ip": GATEWAY, "ports": [], "listening": [], "errors": {}}}}
        if cmd["op"] == "set_relay":
            if not st.relay_ok:
                return {"ok": False, "detail": "relay port 47000 is outside 47100-47999"}
            ports = [p["port"] for p in a["ports"]]
            return {"ok": True, "detail": "set", "instance": {"id": a["id"], "gateway": GATEWAY,
                                                              "relay": {"ip": GATEWAY, "ports": ports, "listening": ports, "errors": {}}}}
        return {"ok": True, "detail": "done"}
    fh = FakeHost(base, host["token"], capacity(), answer, "bridgehost")
    wait(lambda: hosts.registry.online(host["host"]["id"]))
    r = root.post(f"/api/locations/{farm['id']}/central", json={"mode": "bridge", "quota_gb": 100, "host_id": host["host"]["id"], "name": "Farm Central"})
    assert r.status_code == 200, r.text
    ci = r.json()
    wait(lambda: hosts.get_instance(ci["id"])["ready_at"])

    # the instance's server, enrolled as if it had used its token, with one camera on the farm's LAN
    site_app = make_site("Farm Central", [{"id": "gate", "name": "Gate", "host": "192.168.1.50", "onvif_port": 80, "rtsp_port": 554,
                                           "username": "admin", "password": CAM_PW, "enabled": 1, "zones": [{"name": "drive", "points": [[0, 0], [1, 1]]}],
                                           "scene_notes": "the farm gate", "main_path": "/main", "sub_path": "/sub", "web_port": None,
                                           "public_host": None, "public_rtsp_port": None, "public_onvif_port": None, "_br": 3.0}])
    token = db.new_token()
    sid = db.new_id("s_")
    db.insert(db.sites, {"id": sid, "org_id": org["id"], "name": "Farm Central", "location": "", "token_hash": db.token_hash(token),
                         "token_prev_hash": None, "token_rotated_at": None, "created_at": time.time(), "last_seen_at": None, "online": False,
                         "version": None, "summary": None, "clock_skew_s": None, "agent_ip": None, "hostname": None, "location_id": farm["id"]})
    db.run(sa.update(db.central_instances).where(db.central_instances.c.id == ci["id"]).values(server_id=sid, state="running"))
    site_db.set_setting("hub_url", _ws(base) + "/agent")

    class Agent(hub_agent.HubAgent):
        def _auth_header(self):
            return f"Bearer {token}"

    async def summary(s, since):
        return {"cameras": [{"id": c["id"], "name": c["name"]} for c in site_app.st["cameras"].values()], "today": {}}
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    agent = Agent(site_app, SimpleNamespace(pipeline=SimpleNamespace(subscribers=set()), ingests={}, health=None), summary_fn=summary)
    fut = asyncio.run_coroutine_threadsafe(agent.run(), loop)
    wait(lambda: sid in agents.registry.by_site)
    users = {name: _login(base, f"{name}@bridge.example", PW) for name, *_ in people}
    yield SimpleNamespace(base=base, loop=hub.loop, root=root, org=org, other=other, farm=farm, barn=barn, elsewhere=elsewhere, users=users,
                          host=host, host_b=host_b, fh=fh, ci=ci, sid=sid, site=site_app, st=st)
    fut.cancel()
    time.sleep(0.3)
    loop.call_soon_threadsafe(loop.stop)
    fh.close()


def _code(w, client=None, name="Farm PC", loc=None):
    r = (client or w.root).post(f"/api/locations/{(loc or w.farm)['id']}/bridges/codes", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()


HELLO = {"t": "hello", "proto": 1, "version": "0.1.0-test", "os": "windows", "arch": "amd64", "hostname": "farm-pc",
         "lan": [{"if": "Ethernet", "addr": "192.168.1.20/24"}], "caps": ["tcp", "probe"]}


async def _enroll(w, code: str) -> dict:
    async with connect(_ws(w.base) + "/bridge", additional_headers={"Authorization": f"Enroll {code}"}) as ws:
        await ws.send(json.dumps(HELLO))
        got = json.loads(await asyncio.wait_for(ws.recv(), 10))
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(ws.recv(), 10)
        return got


def _new_bridge(w, client=None, name="Farm PC"):
    """(bridge id, device token) through the real enrollment socket."""
    got = asyncio.run(_enroll(w, _code(w, client, name)["code"]))
    assert got["t"] == "enrolled" and got["bridge_id"].startswith("br_") and got["token"], got
    return got["bridge_id"], got["token"]


def _target(w, bid, host="192.168.1.50", client=None, **kw):
    return (client or w.root).post(f"/api/locations/{w.farm['id']}/bridges/{bid}/targets", json={"name": kw.pop("name", "Gate"), "host": host, **kw})


def _relay_cmds(w):
    return [c["args"] for c in w.fh.cmds if c["op"] == "set_relay"]


def _actions(org_id):
    return [r["action"] for r in db.rows(sa.select(db.audit_log.c.action).where(db.audit_log.c.org_id == org_id))]


# ---------------------------------------------------------------- the fakes for the splice

class FakeBridge:
    """The Go bridge's part: hello, welcome, targets; on `open` it dials `dial(host, port)` and pumps both ways under the
    hub's window, crediting what it wrote (unless credit=False); `on_open` "refuse" answers abort, "ignore" nothing."""

    def __init__(self, w, token, dial=None, credit=True, on_open="dial"):
        self.url, self.token = _ws(w.base) + "/bridge", token
        self.dial, self.credit, self.on_open = dial, credit, on_open
        self.frames: list[dict] = []
        self.streams: dict[int, dict] = {}
        self.aborted: dict[int, str] = {}
        self.welcome = self.targets = None
        self.changed = asyncio.Event()

    async def __aenter__(self):
        self.ws = await connect(self.url, additional_headers={"Authorization": f"Bearer {self.token}"}, max_size=2 ** 20)
        await self.send(HELLO)
        self.reader = asyncio.create_task(self._read())
        await self.until(lambda: self.welcome and self.targets)
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        await self.ws.close()
        self.reader.cancel()
        for s in self.streams.values():
            s["w"].close()

    async def send(self, frame):
        await self.ws.send(json.dumps(frame) if isinstance(frame, dict) else frame)

    async def until(self, pred, timeout=10.0):
        end = time.monotonic() + timeout
        while not pred():
            self.changed.clear()
            if time.monotonic() > end:
                raise AssertionError(f"timed out; bridge frames {self.frames[-5:]}")
            try:
                await asyncio.wait_for(self.changed.wait(), 0.2)
            except asyncio.TimeoutError:
                pass

    async def _read(self):
        try:
            async for m in self.ws:
                if isinstance(m, bytes):
                    sid, data = int.from_bytes(m[:4], "big"), m[4:]
                    s = self.streams.get(sid)
                    if s is not None:
                        s["recv"] += len(data)
                        s["w"].write(data)
                        await s["w"].drain()
                        if self.credit:
                            await self.send({"t": "credit", "id": sid, "bytes": len(data)})
                else:
                    f = json.loads(m)
                    self.frames.append(f)
                    await self._frame(f)
                self.changed.set()
        except ConnectionClosed:
            pass
        finally:
            self.changed.set()

    async def _frame(self, f):
        t = f.get("t")
        if t == "welcome":
            self.welcome = f
        elif t == "targets":
            self.targets = f
        elif t == "open":
            if self.on_open == "refuse":
                await self.send({"t": "abort", "id": f["id"], "reason": "connection refused"})
            elif self.on_open == "dial":
                asyncio.create_task(self._open(f))
        elif t == "end" and f["id"] in self.streams:
            self.streams[f["id"]]["w"].write_eof()
        elif t == "abort":
            self.aborted[f["id"]] = f.get("reason")
            s = self.streams.pop(f["id"], None)
            if s:
                s["w"].close()
        elif t == "credit" and f["id"] in self.streams:
            self.streams[f["id"]]["window"] += f["bytes"]
        elif t == "probe":
            ok = f["port"] != 555
            await self.send({"t": "probe_result", "id": f["id"], "ok": ok, "ms": 3 if ok else None, "error": None if ok else "connection refused"})
        elif t == "rotate":
            await self.send({"t": "rotated"})

    async def _open(self, f):
        r, wr = await asyncio.open_connection(*self.dial(f["host"], f["port"]))
        s = {"w": wr, "recv": 0, "window": self.welcome["window"], "open": f}
        self.streams[f["id"]] = s
        await self.send({"t": "opened", "id": f["id"]})
        while True:
            data = await r.read(65536)
            if not data:
                break
            while s["window"] < len(data):
                await self.until(lambda: s["window"] >= len(data) or f["id"] not in self.streams)
                if f["id"] not in self.streams:
                    return
            s["window"] -= len(data)
            await self.send(f["id"].to_bytes(4, "big") + data)
        if f["id"] in self.streams:
            await self.send({"t": "end", "id": f["id"]})


class FakeRelay:
    """The host agent's /host-relay: opens streams on listeners, writes under the hub's window, credits what it got."""

    def __init__(self, w, token, credit=True):
        self.url, self.token, self.credit = _ws(w.base) + "/host-relay", token, credit
        self.streams: dict[int, dict] = {}
        self._next = 1
        self.changed = asyncio.Event()

    async def __aenter__(self):
        self.ws = await connect(self.url, additional_headers={"Authorization": f"Bearer {self.token}"}, max_size=2 ** 20)
        await self.ws.send(json.dumps({"t": "hello", "proto": 1, "version": "0.3.0", "listeners": []}))
        self.reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc):
        await self.ws.close()
        self.reader.cancel()

    async def until(self, pred, timeout=10.0):
        end = time.monotonic() + timeout
        while not pred():
            self.changed.clear()
            if time.monotonic() > end:
                raise AssertionError(f"timed out; relay streams {[(k, v['answer'].done(), len(v['data']), v['aborted']) for k, v in self.streams.items()]}")
            try:
                await asyncio.wait_for(self.changed.wait(), 0.2)
            except asyncio.TimeoutError:
                pass

    async def open(self, instance, port):
        """(stream id, None once opened | the hub's reason for refusing)."""
        sid = self._next
        self._next += 2
        s = {"data": bytearray(), "eof": False, "aborted": None, "answer": asyncio.get_running_loop().create_future(), "window": 512 * 1024, "sent": 0}
        self.streams[sid] = s
        await self.ws.send(json.dumps({"t": "open", "id": sid, "instance": instance, "port": port, "peer": "10.200.0.2"}))
        return sid, await asyncio.wait_for(s["answer"], 15)

    async def write(self, sid, data, wait=True):
        s = self.streams[sid]
        for i in range(0, len(data), 65536):
            piece = data[i:i + 65536]
            if wait:
                await self.until(lambda: s["window"] >= len(piece) or s["aborted"])
            if s["aborted"]:
                return
            s["window"] -= len(piece)
            s["sent"] += len(piece)
            await self.ws.send(sid.to_bytes(4, "big") + piece)

    async def _read(self):
        try:
            async for m in self.ws:
                if isinstance(m, bytes):
                    sid, data = int.from_bytes(m[:4], "big"), m[4:]
                    s = self.streams[sid]
                    s["data"] += data
                    if self.credit:
                        await self.ws.send(json.dumps({"t": "credit", "id": sid, "bytes": len(data)}))
                else:
                    f = json.loads(m)
                    s = self.streams.get(f.get("id"))
                    if s is None:
                        continue
                    if f["t"] == "opened":
                        s["answer"].set_result(None)
                    elif f["t"] == "abort":
                        s["aborted"] = f.get("reason") or "aborted"
                        if not s["answer"].done():
                            s["answer"].set_result(s["aborted"])
                    elif f["t"] == "end":
                        s["eof"] = True
                    elif f["t"] == "credit":
                        s["window"] += f["bytes"]
                self.changed.set()
        except ConnectionClosed:
            pass


async def _echo_camera():
    """A camera that echoes every byte, and half-closes once its client did."""
    async def handle(r, wr):
        try:
            while data := await r.read(65536):
                wr.write(data)
                await wr.drain()
            wr.write_eof()
            await wr.drain()
        except (ConnectionError, OSError):
            pass
    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


# ---------------------------------------------------------------- tests

def test_bridge_mode_provisioning(world):
    ci = hosts.get_instance(world.ci["id"])
    assert (ci["mode"], ci["subnet"], ci["public_ip"]) == ("bridge", None, None)
    assert hosts.camera_network(ci) == {"subnets": [], "public_ips": [], "hosts": []}
    created = next(c["args"] for c in world.fh.cmds if c["op"] == "create_instance" and c["args"]["id"] == ci["id"])
    assert (created["mode"], created["subnet"], created["public_ip"]) == ("bridge", None, None)
    # a bridge-mode instance takes no subnet or public IP; the hub's own Allocate form sends neither
    r = world.root.post(f"/api/locations/{world.barn['id']}/central", json={"mode": "bridge", "quota_gb": 100, "subnet": "10.20.99.0/24",
                                                                              "host_id": world.host["host"]["id"]})
    assert r.status_code == 400 and "bridge mode" in r.json()["detail"]
    # the heartbeat's gateway and relay are kept (the relay address rule and the Site page read them)
    world.fh.send({"t": "heartbeat", "capacity": capacity(), "instances": [
        {"id": ci["id"], "state": "running", "mode": "bridge", "gateway": GATEWAY, "relay": {"ip": GATEWAY, "ports": [], "listening": [], "errors": {}}}]})
    wait(lambda: (hosts.get_instance(ci["id"])["info"] or {}).get("gateway") == GATEWAY)
    assert bridges.relay_address(hosts.get_instance(ci["id"])) == GATEWAY


def test_who_may_do_what(world):
    u, farm = world.users, world.farm
    path = f"/api/locations/{farm['id']}/bridges"
    # Site admins of Farm and hub administrators: codes; nobody else
    assert _code(world, u["farmadmin"])["code"]
    assert _code(world)["code"]
    for who in ("barnadmin", "viewer", "operator", "otheradmin"):
        assert u[who].post(f"{path}/codes", json={"name": "x"}).status_code == 403, who
    # read-only status for everyone who sees the Site; codes, LAN addresses and the relay for its admins only
    bid, _ = _new_bridge(world, u["farmadmin"], name="Role PC")
    for who in ("viewer", "operator"):
        r = u[who].get(path)
        assert r.status_code == 200, (who, r.text)
        v = r.json()
        assert any(b["id"] == bid for b in v["bridges"]) and not v["can_manage"]
        assert "codes" not in v and "instance" not in v and all("lan" not in b for b in v["bridges"])
    for who in ("barnadmin", "otheradmin"):
        assert u[who].get(path).status_code == 403, who
    admin_view = u["farmadmin"].get(path).json()
    assert admin_view["can_manage"] and admin_view["instance"]["id"] == world.ci["id"] and admin_view["codes"]
    assert next(b for b in admin_view["bridges"] if b["id"] == bid)["lan"] == [{"if": "Ethernet", "addr": "192.168.1.20/24"}]
    # mapping, renaming, testing, applying and retiring: the same people
    for who in ("barnadmin", "viewer", "operator", "otheradmin"):
        assert _target(world, bid, client=u[who]).status_code == 403, who
        assert u[who].patch(f"{path}/{bid}", json={"name": "nope"}).status_code == 403, who
        assert u[who].delete(f"{path}/{bid}").status_code == 403, who
    r = _target(world, bid, client=u["farmadmin"], host="192.168.1.60", name="Shed")
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    for who in ("barnadmin", "viewer", "operator", "otheradmin"):
        assert u[who].post(f"{path}/{bid}/targets/{tid}/test").status_code == 403, who
        assert u[who].post(f"{path}/{bid}/targets/{tid}/apply", json={}).status_code == 403, who
        assert u[who].delete(f"{path}/{bid}/targets/{tid}").status_code == 403, who
    viewer_t = next(t for t in u["viewer"].get(path).json()["targets"] if t["id"] == tid)
    assert "host" not in viewer_t and [p["port"] for p in viewer_t["ports"]] == [554, 80, 555]
    assert u["farmadmin"].patch(f"{path}/{bid}", json={"name": "Shed PC"}).json()["name"] == "Shed PC"
    # another Site's bridge is not found through this one
    assert world.root.patch(f"/api/locations/{world.barn['id']}/bridges/{bid}", json={"name": "x"}).status_code == 404
    assert u["farmadmin"].delete(f"{path}/{bid}").json()["ok"] is True
    assert all(b["id"] != bid for b in u["viewer"].get(path).json()["bridges"])
    assert db.rows(sa.select(db.bridge_targets).where(db.bridge_targets.c.bridge_id == bid)) == []


def test_code_lifecycle(world):
    out = _code(world, name="Lifecycle PC")
    code = out["code"]
    assert out["install"] == rf".\axiom-bridge.exe install --hub ws://testserver/bridge --code {code}"   # HUB_PUBLIC_URL in conftest
    assert out["hub_url"] == "ws://testserver/bridge" and code.startswith("AXB-") and len(bridges.normalize_code(code)) == 12
    row = db.one(sa.select(db.bridge_enroll_codes).where(db.bridge_enroll_codes.c.id == out["id"]))
    assert row["code_hash"] == bridges.code_hash(code) and code not in json.dumps(row, default=str)   # only a hash is kept
    listed = world.root.get(f"/api/locations/{world.farm['id']}/bridges").json()["codes"]
    assert any(c["id"] == out["id"] for c in listed) and all("code" not in c for c in listed)

    async def refused(header):
        with pytest.raises(InvalidStatus) as e:
            await connect(_ws(world.base) + "/bridge", additional_headers={"Authorization": header} if header else {})
        return e.value.response.status_code
    assert asyncio.run(refused(None)) == 403
    assert asyncio.run(refused("Enroll WRONG-CODE-0000-0000")) == 403
    # typed without the prefix and the dashes, in lower case: still works, once
    got = asyncio.run(_enroll(world, bridges.normalize_code(code).lower()))
    assert got["t"] == "enrolled"
    b = bridges.get_bridge(got["bridge_id"])
    assert b["name"] == "Lifecycle PC" and b["location_id"] == world.farm["id"] and b["token_hash"] == db.token_hash(got["token"])
    assert b["hello"]["hostname"] == "farm-pc" and got["token"] not in json.dumps(b, default=str)
    assert asyncio.run(refused(f"Enroll {code}")) == 403                     # reused
    assert asyncio.run(refused(f"Bearer {got['token']}x")) == 403
    # expired
    exp = _code(world, name="Late PC")
    db.run(sa.update(db.bridge_enroll_codes).where(db.bridge_enroll_codes.c.id == exp["id"]).values(expires_at=time.time() - 1))
    assert asyncio.run(refused(f"Enroll {exp['code']}")) == 403
    # revoked
    rev = _code(world, name="Revoked PC")
    assert world.users["viewer"].delete(f"/api/locations/{world.farm['id']}/bridges/codes/{rev['id']}").status_code == 403
    assert world.users["farmadmin"].delete(f"/api/locations/{world.farm['id']}/bridges/codes/{rev['id']}").json() == {"ok": True}
    assert asyncio.run(refused(f"Enroll {rev['code']}")) == 403
    # atomic: two enrollments racing with one code make one bridge
    race = _code(world, name="Race PC")["code"]
    results, start = [], threading.Barrier(4)

    def go():
        start.wait()
        results.append(bridges.enroll(race, HELLO, "203.0.113.9"))
    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(r is not None for r in results) == 1
    # the device token connects; a retired bridge's does not
    async def hello_ok(token):
        async with FakeBridge(world, token) as fb:
            return fb.welcome
    welcome = asyncio.run(hello_ok(got["token"]))
    assert welcome["bridge_id"] == got["bridge_id"] and welcome["site"] == "Farm" and welcome["max_streams"] == settings.bridge_max_streams
    assert welcome["window"] == settings.bridge_window and welcome["heartbeat_s"] == 30
    assert world.root.delete(f"/api/locations/{world.farm['id']}/bridges/{got['bridge_id']}").status_code == 200
    assert asyncio.run(refused(f"Bearer {got['token']}")) == 403
    acts = _actions(world.org["id"])
    for a in ("Axiom Bridge enrollment code created: Farm", "Axiom Bridge enrolled: Lifecycle PC into Farm",
              "Axiom Bridge enrollment code revoked: Farm", "Axiom Bridge retired: Lifecycle PC"):
        assert a in acts, a


def test_target_validation_and_listen_ports(world):
    bid, _ = _new_bridge(world, name="Ports PC")
    for host, why in (("8.8.8.8", "private LAN"), ("127.0.0.1", "private LAN"), ("100.64.1.1", "private LAN"), ("cam.example.net", "IPv4"),
                      ("192.168.1.20", "the bridge PC itself"), ("fd00::1", "private LAN")):
        r = _target(world, bid, host=host)
        assert r.status_code == 400 and why in r.json()["detail"], (host, r.text)
    for ports in ([0], [70000], [554, 554], []):
        assert _target(world, bid, ports=ports).status_code == 400, ports
    assert _target(world, bid, ports=list(range(1, 10))).status_code == 422
    n0 = len(_relay_cmds(world))
    a = _target(world, bid, host="192.168.1.70", name="Pond")
    assert a.status_code == 200, a.text
    a = a.json()
    base = min(p["listen"] for p in a["ports"])
    assert [(p["port"], p["listen"]) for p in a["ports"]] == [(554, base), (80, base + 1), (555, base + 2)]
    assert base in bridges.RELAY_PORTS
    assert _target(world, bid, host="192.168.1.70", ports=[554]).status_code == 409      # the same camera port twice
    b = _target(world, bid, host="192.168.1.71", name="Barn door", ports=[8554, 8080]).json()
    assert [p["listen"] for p in b["ports"]] == [base + 3, base + 4]
    # the host was sent every mapped port of the instance, labelled, each time
    sent = _relay_cmds(world)[n0:]
    assert len(sent) == 2 and sent[-1]["id"] == world.ci["id"]
    last = {p["port"]: p["label"] for p in sent[-1]["ports"]}
    assert last[base] == "Pond RTSP" and last[base + 4] == "Barn door web" and set(last) >= {base, base + 1, base + 2, base + 3, base + 4}
    # removing the first frees its ports for the next camera (lowest first)
    assert world.root.delete(f"/api/locations/{world.farm['id']}/bridges/{bid}/targets/{a['id']}").json()["ok"] is True
    assert base not in {p["port"] for p in _relay_cmds(world)[-1]["ports"]}
    c = _target(world, bid, host="192.168.1.72", name="Silo", ports=[554]).json()
    assert [p["listen"] for p in c["ports"]] == [base]
    # the host refuses (or is away): nothing is kept
    world.st.relay_ok = False
    try:
        r = _target(world, bid, host="192.168.1.73", name="Refused")
        assert r.status_code == 502 and "outside" in r.json()["detail"]
    finally:
        world.st.relay_ok = True
    assert not db.rows(sa.select(db.bridge_targets).where(db.bridge_targets.c.name == "Refused"))
    # a Site without a central instance has nothing to relay to
    barn_b = asyncio.run(_enroll(world, _code(world, loc=world.barn)["code"]))["bridge_id"]
    r = world.root.post(f"/api/locations/{world.barn['id']}/bridges/{barn_b}/targets", json={"name": "Hay", "host": "192.168.5.5"})
    assert r.status_code == 409 and "no central recording instance" in r.json()["detail"]
    acts = _actions(world.org["id"])
    assert "bridged camera mapped: Pond (192.168.1.70) via Ports PC" in acts and "bridged camera removed: Pond via Ports PC" in acts
    assert "Axiom Bridge relay ports: Farm" in acts
    assert world.root.delete(f"/api/locations/{world.farm['id']}/bridges/{bid}").status_code == 200
    assert {p["port"] for p in _relay_cmds(world)[-1]["ports"]}.isdisjoint({base, base + 3, base + 4})   # retired: its ports closed


def test_heartbeat_resends_relay_ports(world, monkeypatch):
    bid, _ = _new_bridge(world, name="Drift PC")
    t = _target(world, bid, host="192.168.1.80", name="Drift", ports=[554, 80]).json()
    want = sorted(p["listen"] for p in t["ports"])
    n = len(_relay_cmds(world))
    bridges._relay_tried.clear()
    # the host restarted with an older registry: its heartbeat reports none of them
    world.fh.send({"t": "heartbeat", "capacity": capacity(), "instances": [
        {"id": world.ci["id"], "state": "running", "mode": "bridge", "gateway": GATEWAY, "relay": {"ip": GATEWAY, "ports": [], "listening": [], "errors": {}}}]})
    wait(lambda: len(_relay_cmds(world)) > n)
    assert set(want) <= {p["port"] for p in _relay_cmds(world)[-1]["ports"]}
    # an agent older than 0.3.0 (no relay in its instances) is left alone
    n = len(_relay_cmds(world))
    bridges._relay_tried.clear()
    world.fh.send({"t": "heartbeat", "capacity": capacity(), "instances": [{"id": world.ci["id"], "state": "running", "mode": "bridge", "gateway": GATEWAY}]})
    time.sleep(0.5)
    assert len(_relay_cmds(world)) == n
    world.root.delete(f"/api/locations/{world.farm['id']}/bridges/{bid}")


def test_relay_address_needs_this_instances_listen_ports(world):
    bid, _ = _new_bridge(world, name="Rule PC")
    t = _target(world, bid, host="192.168.1.90", name="Rule").json()
    rtsp, onvif, replay = (p["listen"] for p in t["ports"])
    ci = hosts.get_instance(world.ci["id"])
    assert bridges.relay_address(ci) == GATEWAY

    def check(**cam):
        body = {"id": "rule", "name": "Rule", "host": "192.168.1.90", **cam}
        return in_hub(world, lambda: central_cameras.check(ci, "camera", "/api/cameras/rule", body))
    check(public_host=GATEWAY, public_rtsp_port=rtsp, public_onvif_port=onvif, public_replay_port=replay, public_web_port=None)
    for cam in ({"public_rtsp_port": rtsp, "public_onvif_port": 46000}, {"public_rtsp_port": rtsp, "public_onvif_port": onvif, "public_replay_port": 554},
                {"public_rtsp_port": rtsp, "public_onvif_port": None}, {"public_rtsp_port": None, "public_onvif_port": None, "public_replay_port": None,
                                                                         "public_web_port": None}):
        with pytest.raises(central_cameras.Refused) as e:
            check(public_host=GATEWAY, **cam)
        assert e.value.status == 400, cam
    # another instance's gateway, or the camera's LAN address itself, is a private address outside the Site networks
    for addr in ("10.200.0.17", "192.168.1.90"):
        with pytest.raises(central_cameras.Refused):
            check(public_host=addr, public_rtsp_port=rtsp, public_onvif_port=onvif)
    # through the console proxy too, and the relay address is never opened on the firewall (auto addresses)
    r = world.root.put(f"/s/{world.sid}/api/cameras/rule", json={"id": "rule", "name": "Rule", "host": "192.168.1.90", "username": "admin",
                                                                  "password": CAM_PW, "public_host": GATEWAY, "public_rtsp_port": rtsp, "public_onvif_port": 1})
    assert r.status_code == 400 and "bridged camera ports" in r.json()["detail"]
    r = world.root.put(f"/s/{world.sid}/api/cameras/rule", json={"id": "rule", "name": "Rule", "host": "192.168.1.90", "username": "admin",
                                                                  "password": CAM_PW, "public_host": GATEWAY, "public_rtsp_port": rtsp, "public_onvif_port": onvif})
    assert r.status_code == 200, r.text
    auto, skipped = central_cameras.auto_of([{"id": "rule", "host": "192.168.1.90", "public_host": GATEWAY}], ci)
    assert auto == {"public_ips": [], "hosts": []} and skipped == []
    world.site.st["cameras"].pop("rule", None)
    world.root.delete(f"/api/locations/{world.farm['id']}/bridges/{bid}")


def test_a_bridged_camera_is_added_by_its_lan_address(world):
    """Shawn, 2026-10-18: the instance's New camera dialog with the camera's LAN address (no outside address) was
    refused as "a private address and this Site has no camera network". Mapped on the bridge, the hub now fills in the
    relay address and ports on the way through; not mapped, it says to map it first."""
    bid, _ = _new_bridge(world, name="LAN add PC")
    t = _target(world, bid, host="192.168.1.70", name="Yard").json()
    rtsp, onvif, replay = (p["listen"] for p in t["ports"])
    cam = {"id": "yard", "name": "Yard", "host": "192.168.1.70", "onvif_port": 80, "rtsp_port": 554, "username": "admin",
           "password": CAM_PW, "main_path": "/main", "sub_path": "/sub", "public_host": ""}
    r = world.root.put(f"/s/{world.sid}/api/cameras/yard", json=cam)
    assert r.status_code == 200, r.text
    put = world.site.st["puts"][-1]
    assert put["host"] == "192.168.1.70" and put["had_password"] is True    # the camera itself is as typed
    assert (put["public_host"], put["public_rtsp_port"], put["public_onvif_port"], put["public_replay_port"]) == (GATEWAY, rtsp, onvif, replay)
    # an outside address set by hand is left alone (and checked as before)
    r = world.root.put(f"/s/{world.sid}/api/cameras/yard", json={**cam, "public_host": "192.168.1.70"})
    assert r.status_code == 400
    # a LAN address the bridge doesn't carry: told to map it first
    r = world.root.put(f"/s/{world.sid}/api/cameras/other", json={**cam, "id": "other", "host": "192.168.1.71"})
    assert r.status_code == 400 and "Add a camera" in r.json()["detail"]
    world.site.st["cameras"].pop("yard", None)
    world.root.delete(f"/api/locations/{world.farm['id']}/bridges/{bid}")


def test_one_step_add_maps_and_adds_the_camera(world):
    """Add a camera… with a login: mapped on the bridge and created on the instance, relay address filled in, the
    password passed through to the instance only (never in the hub's answer, database or audit)."""
    bid, _ = _new_bridge(world, name="One step PC")
    r = world.users["farmadmin"].post(f"/api/locations/{world.farm['id']}/bridges/{bid}/targets",
                                      json={"name": "North Gate", "host": "192.168.1.80", "login": {"username": "admin", "password": CAM_PW}})
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["added"] is True and t["camera_id"] == "north_gate" and CAM_PW not in r.text
    rtsp, onvif, replay = (p["listen"] for p in t["ports"])
    put = world.site.st["puts"][-1]
    assert put["had_password"] is True and put["host"] == "192.168.1.80" and put["name"] == "North Gate"
    assert (put["public_host"], put["public_rtsp_port"], put["public_onvif_port"], put["public_replay_port"]) == (GATEWAY, rtsp, onvif, replay)
    assert world.site.st["cameras"]["north_gate"]["password"] == CAM_PW
    row = db.one(sa.select(db.bridge_targets).where(db.bridge_targets.c.id == t["id"]))
    assert row["camera_id"] == "north_gate" and row["applied_at"]
    assert CAM_PW not in json.dumps(db.rows(sa.select(db.audit_log)), default=str)
    # the same address again: mapping refused (already bridged); a second camera at a new address that the instance
    # already has under another target: the add part says so and the mapping stays
    r = world.users["farmadmin"].post(f"/api/locations/{world.farm['id']}/bridges/{bid}/targets",
                                      json={"name": "Again", "host": "192.168.1.80", "login": {"username": "admin", "password": "x"}})
    assert r.status_code == 409
    world.site.st["cameras"]["preexisting"] = {**world.site.st["cameras"]["north_gate"], "id": "preexisting", "host": "192.168.1.81"}
    r = world.users["farmadmin"].post(f"/api/locations/{world.farm['id']}/bridges/{bid}/targets",
                                      json={"name": "Shed", "host": "192.168.1.81", "login": {"username": "admin", "password": "x"}})
    assert r.status_code == 200 and r.json()["added"] is False and "already has" in r.json()["add_error"]
    # a camera mapped without a login (Porch, 2026-10-18), added to recording from its row afterwards
    m = world.users["farmadmin"].post(f"/api/locations/{world.farm['id']}/bridges/{bid}/targets", json={"name": "Porch", "host": "192.168.1.82"}).json()
    assert m["camera_id"] is None and "added" not in m
    add = f"/api/locations/{world.farm['id']}/bridges/{bid}/targets/{m['id']}/add"
    assert world.users["viewer"].post(add, json={"username": "admin", "password": CAM_PW}).status_code == 403
    r = world.users["farmadmin"].post(add, json={"username": "admin", "password": CAM_PW})
    assert r.status_code == 200 and r.json() == {"camera_id": "porch"} and world.site.st["cameras"]["porch"]["public_host"] == GATEWAY
    assert world.users["farmadmin"].post(add, json={"username": "admin", "password": CAM_PW}).status_code == 409   # it's there now
    for k in ("north_gate", "preexisting", "porch"):
        world.site.st["cameras"].pop(k, None)
    world.root.delete(f"/api/locations/{world.farm['id']}/bridges/{bid}")


def test_apply_to_camera(world):
    bid, _ = _new_bridge(world, name="Apply PC")
    t = _target(world, bid, host="192.168.1.50", name="Gate", camera_id="gate").json()
    rtsp, onvif, replay = (p["listen"] for p in t["ports"])
    path = f"/api/locations/{world.farm['id']}/bridges/{bid}/targets/{t['id']}/apply"
    r = world.users["farmadmin"].post(path, json={})
    assert r.status_code == 200, r.text
    assert r.json() == {"camera_id": "gate", "public_host": GATEWAY, "public_rtsp_port": rtsp, "public_onvif_port": onvif,
                        "public_replay_port": replay, "public_web_port": None}
    put = world.site.st["puts"][-1]
    assert put["had_password"] is False   # the password is never sent: the server keeps the stored one
    assert (put["public_host"], put["public_rtsp_port"], put["public_onvif_port"], put["public_replay_port"]) == (GATEWAY, rtsp, onvif, replay)
    # everything else went back exactly as the instance had it (its PUT resets fields a body leaves out)
    assert put["zones"] == [{"name": "drive", "points": [[0, 0], [1, 1]]}] and put["scene_notes"] == "the farm gate" and put["host"] == "192.168.1.50"
    assert "status" not in put and "streams" not in put
    cam = world.site.st["cameras"]["gate"]
    assert cam["password"] == CAM_PW and cam["public_host"] == GATEWAY
    row = db.one(sa.select(db.bridge_targets).where(db.bridge_targets.c.id == t["id"]))
    assert row["applied_at"] and row["camera_id"] == "gate"
    audit = db.rows(sa.select(db.audit_log).where(db.audit_log.c.action == f"bridged camera applied: Gate -> camera gate"))
    assert audit and CAM_PW not in json.dumps(audit, default=str)
    # refused: an unknown camera, a camera whose RTSP port is not bridged, an instance whose host never said its gateway
    assert world.root.post(path, json={"camera_id": "nope"}).status_code == 404
    world.site.st["cameras"]["odd"] = {**cam, "id": "odd", "name": "Odd", "rtsp_port": 8554, "public_host": None}
    r = world.root.post(path, json={"camera_id": "odd"})
    assert r.status_code == 409 and "8554" in r.json()["detail"]
    world.site.st["cameras"].pop("odd")
    info = hosts.get_instance(world.ci["id"])["info"]
    hosts._set(world.ci["id"], info={k: v for k, v in info.items() if k != "gateway"})
    try:
        r = world.root.post(path, json={})
        assert r.status_code == 409 and "relay address" in r.json()["detail"]
    finally:
        hosts._set(world.ci["id"], info=info)
    world.root.delete(f"/api/locations/{world.farm['id']}/bridges/{bid}")


def test_bridge_offline_alert_reaches_the_customer(world, superuser, monkeypatch):
    monkeypatch.setattr(alerts, "on_open", None)
    bid, token = _new_bridge(world, name="Alert PC")
    db.run(sa.update(db.bridges).where(db.bridges.c.id == bid).values(online=False, last_seen_at=time.time() - 3600))
    monkeypatch.setattr(bridges.registry, "started_at", 0)
    asyncio.run(bridges.registry.sweep())
    asyncio.run(bridges.registry.sweep())
    rows = db.rows(sa.select(db.alerts).where(db.alerts.c.site_id == bid, db.alerts.c.closed_at.is_(None)))
    assert len(rows) == 1 and rows[0]["kind"] == "bridge_offline" and rows[0]["org_id"] == world.org["id"]
    assert rows[0]["detail"]["location_id"] == world.farm["id"]
    # listed for the customer's people who see Farm (by the bridge's name), not for those who don't
    for who, sees in (("viewer", True), ("farmadmin", True), ("barnadmin", False)):
        got = [a for a in world.users[who].get(f"/api/alerts?org={world.org['id']}").json() if a["site_id"] == bid]
        assert bool(got) == sees, who
        if sees:
            assert got[0]["site_name"] == "Alert PC" and got[0]["location_id"] == world.farm["id"] and got[0]["location_name"] == "Farm"
    assert [a["kind"] for a in world.users["viewer"].get(f"/api/locations/{world.farm['id']}/alerts").json() if a["site_id"] == bid] == ["bridge_offline"]
    assert not [a for a in world.users["viewer"].get(f"/api/locations/{world.barn['id']}/alerts").json() if a["site_id"] == bid]
    # pushed to the customer's members who see the Site and to hub administrators
    viewer, barn = auth.user_by_email("viewer@bridge.example"), auth.user_by_email("barnadmin@bridge.example")
    subs = [(superuser["id"], "root-bridge"), (viewer["id"], "viewer-bridge"), (barn["id"], "barn-bridge")]
    for uid, ep in subs:
        push.subscribe(uid, {"endpoint": f"https://fcm.googleapis.com/fcm/send/{ep}"}, [*push.DEFAULT_KINDS], "t")
    sent: list[tuple] = []
    push.set_sender(lambda sub, payload: sent.append((sub["user_id"], payload)) or True)
    try:
        asyncio.run(push.notify_alert(world.org["id"], bridges.alert_subject(bridges.get_bridge(bid)), "bridge_offline", rows[0]["detail"]))
    finally:
        push.set_sender(None)
        for uid, ep in subs:
            push.unsubscribe(uid, f"https://fcm.googleapis.com/fcm/send/{ep}")
    got = {uid for uid, _ in sent}
    assert {superuser["id"], viewer["id"]} <= got and barn["id"] not in got
    mine = next(p for uid, p in sent if uid == viewer["id"])
    assert mine["title"] == "Farm · Alert PC: Axiom Bridge offline" and mine["url"] == f"/sites/{world.farm['id']}/servers"
    # acknowledged by someone who sees the Site; not by an admin of another Site
    assert world.users["barnadmin"].post(f"/api/alerts/{rows[0]['id']}/ack").status_code == 404
    assert world.users["farmadmin"].post(f"/api/alerts/{rows[0]['id']}/ack").json() == {"ok": True}
    # it opens again while the bridge stays away, and closes by itself once it is back
    asyncio.run(bridges.registry.sweep())
    assert len(db.rows(sa.select(db.alerts).where(db.alerts.c.site_id == bid, db.alerts.c.closed_at.is_(None)))) == 1

    async def back():
        async with FakeBridge(world, token):
            pass
    asyncio.run(back())
    assert db.rows(sa.select(db.alerts).where(db.alerts.c.site_id == bid, db.alerts.c.closed_at.is_(None))) == []
    world.root.delete(f"/api/locations/{world.farm['id']}/bridges/{bid}")


def test_rotate_token(world):
    bid, token = _new_bridge(world, name="Rotate PC")
    path = f"/api/locations/{world.farm['id']}/bridges/{bid}/rotate-token"
    r = world.root.post(path)
    assert r.status_code == 409   # offline

    async def go():
        async with FakeBridge(world, token) as fb:
            assert world.users["viewer"].post(path).status_code == 403
            assert (await asyncio.to_thread(world.users["farmadmin"].post, path)).json() == {"ok": True}
            await fb.until(lambda: any(f["t"] == "rotate" for f in fb.frames))
            return next(f["token"] for f in fb.frames if f["t"] == "rotate")
    new = asyncio.run(go())
    b = bridges.get_bridge(bid)
    assert b["token_hash"] == db.token_hash(new) and b["token_prev_hash"] == db.token_hash(token)

    async def welcome(tok):
        async with FakeBridge(world, tok) as fb:
            return fb.welcome["bridge_id"]
    assert asyncio.run(welcome(new)) == bid
    assert asyncio.run(welcome(token)) == bid   # the old one, within the grace period
    db.run(sa.update(db.bridges).where(db.bridges.c.id == bid).values(token_rotated_at=time.time() - bridges.ROTATE_GRACE_S - 1))
    with pytest.raises(InvalidStatus):
        asyncio.run(welcome(token))
    assert "Axiom Bridge token rotated: Rotate PC" in _actions(world.org["id"])
    world.root.delete(f"/api/locations/{world.farm['id']}/bridges/{bid}")


# ---------------------------------------------------------------- the splice, end to end

@pytest.fixture()
def bridged(world):
    """A bridge with one mapped camera (554, 80, 555 relayed from three listen ports); unmapped afterwards."""
    bid, token = _new_bridge(world, name="Splice PC")
    t = _target(world, bid, host="192.168.1.95", name="Echo").json()
    yield SimpleNamespace(bid=bid, token=token, target=t, listen=[p["listen"] for p in t["ports"]])
    world.root.delete(f"/api/locations/{world.farm['id']}/bridges/{bid}")


def test_splice_bytes_both_ways_end_and_test(world, bridged):
    async def go():
        cam, port = await _echo_camera()
        dialed = []

        def dial(host, p):
            dialed.append((host, p))
            return "127.0.0.1", port
        async with FakeBridge(world, bridged.token, dial) as fb, FakeRelay(world, world.host["token"]) as fr:
            assert fb.targets["items"] == [{"id": bridged.target["id"], "host": "192.168.1.95", "ports": [554, 80, 555]}]
            sid, why = await fr.open(world.ci["id"], bridged.listen[1])
            assert why is None and sid % 2 == 1
            assert dialed == [("192.168.1.95", 80)]               # the listen port named the camera port
            assert any(f["t"] == "open" and f["port"] == 80 and f["target"] == bridged.target["id"] and f["host"] == "192.168.1.95" for f in fb.frames)
            payload = bytes(range(256)) * 4000                    # ~1 MB: many chunks, many credits, both windows
            await fr.write(sid, payload)
            await fr.until(lambda: len(fr.streams[sid]["data"]) >= len(payload))
            assert bytes(fr.streams[sid]["data"]) == payload
            # the instance half-closes: the camera sees EOF and closes too, the instance gets `end`
            await fr.ws.send(json.dumps({"t": "end", "id": sid}))
            await fr.until(lambda: fr.streams[sid]["eof"])
            await asyncio.sleep(0.1)
            assert not bridges.registry.relays[world.host["host"]["id"]].legs and not bridges.registry.bridges[bridged.bid].legs
            # Test: one TCP connect per port from the bridge
            res = await asyncio.to_thread(world.root.post, f"/api/locations/{world.farm['id']}/bridges/{bridged.bid}/targets/{bridged.target['id']}/test")
            assert [(p["port"], p["ok"]) for p in res.json()["ports"]] == [(554, True), (80, True), (555, False)]
            view = await asyncio.to_thread(world.users["viewer"].get, f"/api/locations/{world.farm['id']}/bridges")
            mine = next(t for t in view.json()["targets"] if t["id"] == bridged.target["id"])
            assert mine["probe"]["ports"][2]["error"] == "connection refused"
            assert next(b for b in view.json()["bridges"] if b["id"] == bridged.bid)["online"] is True
        cam.close()
    asyncio.run(go())


def test_splice_abort_both_ways_and_bridge_offline(world, bridged):
    async def go():
        cam, port = await _echo_camera()
        async with FakeRelay(world, world.host["token"]) as fr:
            async with FakeBridge(world, bridged.token, lambda h, p: ("127.0.0.1", port)) as fb:
                # the instance resets: the bridge is told
                sid, why = await fr.open(world.ci["id"], bridged.listen[0])
                assert why is None
                await fb.until(lambda: fb.streams)
                bsid = next(iter(fb.streams))
                await fr.ws.send(json.dumps({"t": "abort", "id": sid, "reason": "instance reset"}))
                await fb.until(lambda: bsid in fb.aborted)
                assert fb.aborted[bsid] == "instance reset"
                # the camera refuses: the instance is told why
                fb.on_open = "refuse"
                sid2, why = await fr.open(world.ci["id"], bridged.listen[0])
                assert why == "connection refused"
                # the bridge goes away mid-stream: the instance's stream is reset
                fb.on_open = "dial"
                sid3, why = await fr.open(world.ci["id"], bridged.listen[2])
                assert why is None
            await fr.until(lambda: fr.streams[sid3]["aborted"])
            assert "offline" in fr.streams[sid3]["aborted"]
            # and while it is offline, nothing opens
            _, why = await fr.open(world.ci["id"], bridged.listen[0])
            assert why == "the bridge is offline"
        cam.close()
    asyncio.run(go())


def test_splice_refusals(world, bridged, monkeypatch):
    async def go():
        cam, port = await _echo_camera()
        async with FakeBridge(world, bridged.token, lambda h, p: ("127.0.0.1", port)) as fb, \
                FakeRelay(world, world.host["token"]) as fr, FakeRelay(world, world.host_b["token"]) as other:
            # another host names this instance's listener: refused (a host only reaches its own instances)
            _, why = await other.open(world.ci["id"], bridged.listen[0])
            assert why == "unknown listener"
            _, why = await fr.open(world.ci["id"], 47999)                      # nothing mapped there
            assert why == "no bridged camera on that port"
            _, why = await fr.open("ci_nope", bridged.listen[0])
            assert why == "unknown listener"
            # the stream cap
            monkeypatch.setattr(settings, "bridge_max_streams", 2)
            assert (await fr.open(world.ci["id"], bridged.listen[0]))[1] is None
            assert (await fr.open(world.ci["id"], bridged.listen[1]))[1] is None
            _, why = await fr.open(world.ci["id"], bridged.listen[2])
            assert "as many streams" in why
            monkeypatch.setattr(settings, "bridge_max_streams", 16)
            # a bridge that does not answer the open in time
            monkeypatch.setattr(settings, "bridge_open_timeout_s", 0.5)
            fb.on_open = "ignore"
            _, why = await fr.open(world.ci["id"], bridged.listen[2])
            assert why == "the bridge did not answer"
            await fb.until(lambda: any(f["t"] == "abort" for f in fb.frames))   # and the bridge's half is reset too
        cam.close()
    asyncio.run(go())


def test_withheld_credit_stops_forwarding_with_bounded_memory(world, bridged):
    window = settings.bridge_window

    async def go():
        cam, port = await _echo_camera()
        async with FakeBridge(world, bridged.token, lambda h, p: ("127.0.0.1", port), credit=False) as fb, \
                FakeRelay(world, world.host["token"]) as fr:
            sid, why = await fr.open(world.ci["id"], bridged.listen[0])
            assert why is None
            writer = asyncio.create_task(fr.write(sid, b"z" * (8 * window)))
            await asyncio.sleep(1.5)
            s = fr.streams[sid]
            bstream = next(iter(fb.streams.values()))
            leg = bridges.registry.relays[world.host["host"]["id"]].legs[sid]
            assert bstream["recv"] <= window                    # the bridge never credited: one window reached it
            assert leg.held <= window                           # the hub holds at most one more
            assert s["sent"] <= 2 * window                      # the instance was stopped there
            assert not writer.done()
            writer.cancel()
            await fr.ws.send(json.dumps({"t": "abort", "id": sid, "reason": "test over"}))
        cam.close()
    asyncio.run(go())


def test_bridge_protocol_violations_close_it(world, bridged):
    async def go():
        cam, port = await _echo_camera()
        # a bridge that tries to open a stream itself
        async with FakeBridge(world, bridged.token, lambda h, p: ("127.0.0.1", port)) as fb:
            await fb.send({"t": "open", "id": 1, "target": bridged.target["id"], "host": "192.168.1.1", "port": 22})
            await fb.until(lambda: fb.reader.done())
            assert fb.ws.close_code == 4400
        # a bridge that sends more than the window without credit
        # (the instance credits nothing either, so the hub cannot pass the bytes on and credit them itself)
        async with FakeBridge(world, bridged.token, lambda h, p: ("127.0.0.1", port)) as fb, FakeRelay(world, world.host["token"], credit=False) as fr:
            sid, why = await fr.open(world.ci["id"], bridged.listen[0])
            assert why is None
            await fb.until(lambda: fb.streams)
            bsid = next(iter(fb.streams))
            for _ in range(2 * settings.bridge_window // 65536 + 2):
                await fb.send(bsid.to_bytes(4, "big") + b"q" * 65536)
            await fb.until(lambda: fb.reader.done())
            assert fb.ws.close_code == 4400
            await fr.until(lambda: fr.streams[sid]["aborted"])
        cam.close()
    asyncio.run(go())
