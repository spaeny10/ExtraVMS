"""Central recording (hosts.py): host agents on /host-agent, placement and provisioning, Enroll tokens on /agent,
who may read a Site's central instance, and the site_link_down / host_offline alerts."""
import asyncio
import json
import threading
import time

import httpx
import pytest
import sqlalchemy as sa
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect as ws_connect

from tunnelproto import decode, encode

from hub import alerts, auth, db, hosts, push
from hub.config import settings
from test_fleet import hub_server  # noqa: F401  (module-scoped fixture: a real hub on a port)

PW = "central-pass-12345"

GPUS = [{"index": 0, "name": "NVIDIA A40", "mem_total_mb": 46068, "mem_used_mb": 30000, "util": 60},
        {"index": 1, "name": "NVIDIA A10", "mem_total_mb": 23028, "mem_used_mb": 2000, "util": 10}]


def capacity(gpus=GPUS, free_gb=8000.0):
    return {"cpus": 80, "load": 3.2, "ram_gb": {"total": 768, "free": 600}, "gpus": gpus,
            "disks": [{"path": "/srv/axiom", "total_gb": 10000, "free_gb": free_gb}], "instances": 0}


def _login(base, email, pw):
    c = httpx.Client(base_url=base, timeout=30)
    assert c.post("/auth/login", json={"email": email, "password": pw}).status_code == 200
    return c


def _ws(base: str) -> str:
    return base.replace("http://", "ws://")


class FakeHost:
    """An axiom-host agent: says hello, answers each cmd with answer(cmd) (None = never answers)."""

    def __init__(self, base, token, cap=None, answer=None, hostname="g481"):
        self.ws = ws_connect(_ws(base) + "/host-agent", additional_headers={"Authorization": f"Bearer {token}"}).__enter__()
        self.cmds: list[dict] = []
        self.frames: list[dict] = []
        self.answer = answer or (lambda cmd: {"ok": True, "detail": "done"})
        self.send({"t": "hello", "proto": 1, "hostname": hostname, "version": "0.1.0", "capacity": cap or capacity()})
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def send(self, frame):
        self.ws.send(json.dumps(frame))

    def _run(self):
        try:
            for msg in self.ws:
                f = json.loads(msg)
                self.frames.append(f)
                if f.get("t") == "cmd":
                    self.cmds.append(f)
                    r = self.answer(f)
                    if r is not None:
                        self.send({"t": "result", "id": f["id"], **r})
        except (ConnectionClosed, OSError):
            pass

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def wait(pred, timeout=10.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("timed out waiting")


@pytest.fixture(scope="module")
def world(hub_server, superuser):  # noqa: F811
    base = hub_server
    root = _login(base, superuser["email"], superuser["password"])
    org = root.post("/api/orgs", json={"name": "Central Co", "slug": "central-co"}).json()
    a = root.post(f"/api/orgs/{org['id']}/locations", json={"name": "Yard A"}).json()
    b = root.post(f"/api/orgs/{org['id']}/locations", json={"name": "Yard B"}).json()
    c = root.post(f"/api/orgs/{org['id']}/locations", json={"name": "Yard C"}).json()
    # a Site admin who may see only Yard A, and a viewer of every Site
    r = root.post(f"/api/orgs/{org['id']}/members", json={"email": "siteadmin@central.example", "role": "admin", "password": PW,
                                                           "all_sites": False, "location_ids": [a["id"]]})
    assert r.status_code == 200, r.text
    r = root.post(f"/api/orgs/{org['id']}/members", json={"email": "viewer@central.example", "role": "viewer", "password": PW})
    assert r.status_code == 200, r.text
    return {"base": base, "root": root, "org": org, "a": a, "b": b, "c": c}


def test_host_token_auth_hello_heartbeat(world):
    base, root = world["base"], world["root"]
    r = root.post("/api/hub/hosts", json={"name": "G481 #1", "notes": "rack 4", "fusionhub": "203.0.113.10"})
    assert r.status_code == 200, r.text
    out = r.json()
    token, host = out["token"], out["host"]
    assert host["id"].startswith("h_") and "--hub ws://testserver/host-agent" in out["install"]
    row = db.one(sa.select(db.hosts).where(db.hosts.c.id == host["id"]))
    assert row["token_hash"] == db.token_hash(token) and token not in json.dumps(row, default=str)   # only the hash is stored
    assert all("token" not in h for h in root.get("/api/hub/hosts").json()["hosts"])

    for hdr in ({}, {"Authorization": "Bearer nope"}, {"Authorization": f"Claim {token}"}):
        with pytest.raises(InvalidStatus) as e:
            ws_connect(_ws(base) + "/host-agent", additional_headers=hdr)
        assert e.value.response.status_code == 403
    # a host token is not a server token
    with pytest.raises(InvalidStatus):
        ws_connect(_ws(base) + "/agent", additional_headers={"Authorization": f"Bearer {token}"})

    fh = FakeHost(base, token)
    try:
        h = wait(lambda: next((x for x in root.get("/api/hub/hosts").json()["hosts"] if x["id"] == host["id"] and x["online"]), None))
        assert h["hostname"] == "g481" and h["version"] == "0.1.0" and h["capacity"]["gpus"][1]["name"] == "NVIDIA A10"
        fh.send({"t": "ping"})
        wait(lambda: any(f.get("t") == "pong" for f in fh.frames))
        fh.send({"t": "heartbeat", "capacity": capacity(free_gb=7000.0), "instances": []})
        wait(lambda: (db.one(sa.select(db.hosts).where(db.hosts.c.id == host["id"]))["capacity"] or {}).get("disks", [{}])[0].get("free_gb") == 7000.0)
    finally:
        fh.close()
    wait(lambda: not db.one(sa.select(db.hosts).where(db.hosts.c.id == host["id"]))["online"])
    # rotate: the old token stops working
    new = root.post(f"/api/hub/hosts/{host['id']}/rotate-token").json()["token"]
    with pytest.raises(InvalidStatus):
        ws_connect(_ws(base) + "/host-agent", additional_headers={"Authorization": f"Bearer {token}"})
    FakeHost(base, new).close()
    assert root.delete(f"/api/hub/hosts/{host['id']}").json() == {"ok": True}
    assert db.one(sa.select(db.hosts).where(db.hosts.c.id == host["id"])) is None


def test_gpu_choice():
    assert hosts.pick_gpu(capacity()) == 1   # the A10, not the busier A40
    a100 = [{"index": 0, "name": "NVIDIA A100", "mem_total_mb": 80000, "mem_used_mb": 70000},
            {"index": 1, "name": "NVIDIA A40", "mem_total_mb": 46068, "mem_used_mb": 0},
            {"index": 2, "name": "NVIDIA RTX 4000", "mem_total_mb": 20000, "mem_used_mb": 1000}]
    assert hosts.pick_gpu({"gpus": a100}) == 2          # no A10 (an A100 is not one): least used non-A40
    two = [{"index": 0, "name": "NVIDIA A10", "mem_total_mb": 23028, "mem_used_mb": 100},
           {"index": 1, "name": "NVIDIA A10G", "mem_total_mb": 23028, "mem_used_mb": 9000}]
    assert hosts.pick_gpu({"gpus": two}) == 0
    assert hosts.pick_gpu({"gpus": two}, {0: 3, 1: 1}) == 1   # fewer instances already wins
    assert hosts.pick_gpu({"gpus": [{"index": 0, "name": "NVIDIA A40", "mem_total_mb": 1, "mem_used_mb": 0}]}) == 0   # nothing else
    assert hosts.pick_gpu({"gpus": []}) is None and hosts.pick_gpu(None) is None
    old = settings.central_gpu_prefer
    settings.central_gpu_prefer = "RTX 4000"
    try:
        assert hosts.pick_gpu({"gpus": a100}) == 2
    finally:
        settings.central_gpu_prefer = old


def test_gpu_room_counts_shares_on_instance_cards_only():
    """Room for another Site's GPU share: what the shares leave and what is free now, both less the headroom, on the
    cards instances go on (never the A40 that serves vLLM, however much it has free)."""
    cap = {"gpus": [{"index": 0, "name": "NVIDIA A40", "mem_total_mb": 49140, "mem_used_mb": 30000},
                    {"index": 1, "name": "NVIDIA A10G", "mem_total_mb": 23028, "mem_used_mb": 8000}]}
    rooms = hosts.gpu_room(cap, {1: 12288})
    assert [r["index"] for r in rooms] == [1]
    assert rooms[0]["room_mb"] == 23028 - 1024 - 12288 and rooms[0]["slots"] == 2     # the shares bind, not today's use
    assert hosts.gpu_room(cap, {1: 0})[0]["room_mb"] == 23028 - 8000 - 1024             # nothing shared: today's use binds
    assert hosts.gpu_room(cap, {1: 30000})[0]["room_mb"] == 0
    assert hosts.pick_gpu(cap, None, {1: 12288}, 4096) == 1
    with pytest.raises(hosts.NoGpuRoom):
        hosts.pick_gpu(cap, None, {1: 20000}, 4096)
    assert hosts.pick_gpu({"gpus": []}, None, {}, 4096) is None                          # no GPU at all: the CPU, as before
    two = {"gpus": [{"index": 0, "name": "NVIDIA A10", "mem_total_mb": 23028, "mem_used_mb": 100},
                    {"index": 1, "name": "NVIDIA A10", "mem_total_mb": 23028, "mem_used_mb": 100}]}
    assert hosts.pick_gpu(two, {0: 1, 1: 4}, {0: 19000, 1: 4096}, 4096) == 1             # the only card with room
    # an instance's share: its own, else the default or its peak (+ the CUDA context) when that was more
    ci = {"id": "ci_x", "info": {"gpu_mem_mb": 6144}}
    assert hosts.share_mb(ci) == 6144
    bare = {"id": "ci_y", "info": {}}
    assert hosts.share_mb(bare) == settings.central_gpu_mem_mb
    work = {"instances": {"ci_y": {"gpu_mem": {"peak_mb": 5000}}}}
    assert hosts.share_mb(bare, work) == 5000 + hosts.GPU_CONTEXT_MB
    assert hosts.share_mb(bare, {"instances": {"ci_y": {"gpu_mem": {"peak_mb": 900}}}}) == settings.central_gpu_mem_mb


def test_provision_command_round_trip_and_enroll(world):
    base, root, org, a = world["base"], world["root"], world["org"], world["a"]
    small = root.post("/api/hub/hosts", json={"name": "Small"}).json()
    big = root.post("/api/hub/hosts", json={"name": "Big", "fusionhub": "198.51.100.7"}).json()
    full = root.post("/api/hub/hosts", json={"name": "Full"}).json()
    seen_args: list[dict] = []

    def answer(cmd):
        if cmd["op"] == "create_instance":
            seen_args.append(cmd["args"])
            return {"ok": True, "detail": "created", "instance": {"id": cmd["args"]["id"], "state": "running", "used_gb": 0}}
        return {"ok": True, "detail": "done"}
    h_small = FakeHost(base, small["token"], capacity(gpus=[{"index": 0, "name": "NVIDIA A10", "mem_total_mb": 23028, "mem_used_mb": 12000}]), answer, "small")
    h_big = FakeHost(base, big["token"], capacity(), answer, "big")
    h_full = FakeHost(base, full["token"], capacity(gpus=[{"index": 0, "name": "NVIDIA A10", "mem_total_mb": 99999, "mem_used_mb": 0}], free_gb=50), answer, "full")
    try:
        wait(lambda: sum(1 for x in root.get("/api/hub/hosts").json()["hosts"] if x["online"]) >= 3)
        # validation
        assert root.post(f"/api/locations/{a['id']}/central", json={"mode": "forward", "quota_gb": 100}).status_code == 400
        assert root.post(f"/api/locations/{a['id']}/central", json={"mode": "vpn", "quota_gb": 100, "subnet": "8.8.8.0/24"}).status_code == 400
        assert root.post("/api/locations/l_nope/central", json={"quota_gb": 100}).status_code == 404
        assert root.post(f"/api/locations/{a['id']}/central", json={"quota_gb": 10_000_000 // 10}).status_code == 409   # no host has room

        # auto placement: Full has the most GPU memory but no disk room; Big has more free GPU memory than Small
        r = root.post(f"/api/locations/{a['id']}/central", json={"mode": "vpn", "quota_gb": 4000})
        assert r.status_code == 200, r.text
        ci = r.json()
        assert ci["host_id"] == big["host"]["id"] and ci["gpu"] == 1 and ci["gpu_name"] == "NVIDIA A10"
        n = ci["site_number"]
        assert 1 <= n <= 250 and ci["subnet"] == f"10.20.{n}.0/24" and ci["phase"] in ("provisioning", "waiting_enroll")
        args = wait(lambda: seen_args[-1] if seen_args else None)
        assert args == {"id": ci["id"], "location": a["id"], "name": "Central", "mode": "vpn", "subnet": f"10.20.{n}.0/24", "public_ip": None,
                        "quota_gb": 4000, "gpu": 1, "enroll_token": args["enroll_token"], "hub_url": "ws://testserver/agent",
                        "vlm_url": "http://vllm:8000/v1", "gpu_mem_mb": 4096}
        assert h_big.cmds[-1]["op"] == "create_instance" and isinstance(h_big.cmds[-1]["id"], int)
        assert not h_small.cmds and not h_full.cmds
        token = args["enroll_token"]
        assert db.one(sa.select(db.central_enroll_tokens).where(db.central_enroll_tokens.c.token_hash == db.token_hash(token)))["location_id"] == a["id"]
        got = wait(lambda: next((x for x in root.get(f"/api/locations/{a['id']}/central").json()["instances"] if x["phase"] == "waiting_enroll"), None))
        assert got["peplink"]["fusionhub"] == "198.51.100.7" and got["peplink"]["lan_gateway"] == f"10.20.{n}.1"
        # one instance per Site
        assert root.post(f"/api/locations/{a['id']}/central", json={"quota_gb": 10}).status_code == 409
        # a second Site, port-forward mode, on a picked host: the next site number
        r = root.post(f"/api/locations/{world['b']['id']}/central", json={"mode": "forward", "public_ip": "93.184.216.34", "quota_gb": 500,
                                                                           "host_id": small["host"]["id"], "name": "Yard B Central"})
        assert r.status_code == 200, r.text
        ci_b = r.json()
        assert ci_b["site_number"] not in (None, n) and ci_b["subnet"] is None and ci_b["public_ip"] == "93.184.216.34" and ci_b["gpu"] == 0
        wait(lambda: any(a2["id"] == ci_b["id"] for a2 in seen_args))

        # ---- enrollment with the token
        for bad in ("bogus-token", ""):
            with pytest.raises(InvalidStatus) as e:
                ws_connect(_ws(base) + "/agent", additional_headers={"Authorization": f"Enroll {bad}".strip()})
            assert e.value.response.status_code in (403,)
        with ws_connect(_ws(base) + "/agent", additional_headers={"Authorization": f"Enroll {token}"}) as s:
            s.send(encode({"t": "hello", "proto": 1, "site_version": "0.9.9", "hostname": "ci-yard-a", "cameras": []}))
            m = decode(s.recv(timeout=10))
            assert m["t"] == "enrolled" and m["site_id"].startswith("s_") and m["token"]
            with pytest.raises(ConnectionClosed):
                s.recv(timeout=5)
        server = db.one(sa.select(db.sites).where(db.sites.c.id == m["site_id"]))
        assert server["org_id"] == org["id"] and server["location_id"] == a["id"] and server["name"] == "Central"
        assert server["hostname"] == "ci-yard-a" and server["token_hash"] == db.token_hash(m["token"])
        row = hosts.get_instance(ci["id"])
        assert row["server_id"] == server["id"] and row["state"] == "running"
        audit = db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id == org["id"]))
        assert any(x["action"].startswith("central instance enrolled") and x["site_id"] == server["id"] for x in audit)
        assert any(x["action"].startswith("central recording provisioning") for x in audit)
        # single use
        with pytest.raises(InvalidStatus) as e:
            ws_connect(_ws(base) + "/agent", additional_headers={"Authorization": f"Enroll {token}"})
        assert e.value.response.status_code == 403
        # the device token works like any server's: welcome names the Site
        with ws_connect(_ws(base) + "/agent", additional_headers={"Authorization": f"Bearer {m['token']}"}) as s:
            s.send(encode({"t": "hello", "proto": 1, "site_version": "0.9.9", "hostname": "ci-yard-a", "cameras": []}))
            w = decode(s.recv(timeout=10))
            assert w["t"] == "welcome" and w["site_id"] == server["id"] and w["location_id"] == a["id"]
        site_a = root.get(f"/api/locations/{a['id']}").json()
        assert [x["id"] for x in site_a["servers"]] == [server["id"]]
        assert root.get(f"/api/locations/{world['b']['id']}").json()["servers"] == []   # exactly that Site

        # expiry: Yard B's token, pushed past its 24 h
        tok_b = next(x for x in seen_args if x["id"] == ci_b["id"])["enroll_token"]
        db.run(sa.update(db.central_enroll_tokens).where(db.central_enroll_tokens.c.token_hash == db.token_hash(tok_b)).values(expires_at=time.time() - 1))
        with pytest.raises(InvalidStatus) as e:
            ws_connect(_ws(base) + "/agent", additional_headers={"Authorization": f"Enroll {tok_b}"})
        assert e.value.response.status_code == 403
        assert hosts.get_instance(ci_b["id"])["server_id"] is None

        # host heartbeat: usage per instance lands on the server card (Customer › Servers)
        h_big.send({"t": "heartbeat", "capacity": capacity(), "instances": [{"id": ci["id"], "location_id": a["id"], "name": "Central", "state": "running",
                                                                              "quota_gb": 4000, "used_gb": 812.5, "gpu": 1, "mode": "vpn"},
                                                                             {"id": ci_b["id"], "used_gb": 999}]})   # not Big's: ignored
        card = wait(lambda: next((s for s in root.get(f"/api/orgs/{org['id']}/servers").json() if s["id"] == server["id"]
                                  and (s.get("central") or {}).get("used_gb") == 812.5), None))
        assert card["central"]["quota_gb"] == 4000 and card["central"]["mode"] == "vpn"
        assert (hosts.get_instance(ci_b["id"])["info"] or {}).get("used_gb") != 999

        # quota change: a set_quota round trip
        r = root.patch(f"/api/locations/{a['id']}/central/{ci['id']}", json={"quota_gb": 6000})
        assert r.status_code == 200 and r.json()["quota_gb"] == 6000, r.text
        assert h_big.cmds[-1]["op"] == "set_quota" and h_big.cmds[-1]["args"] == {"id": ci["id"], "quota_gb": 6000}
        # a host that refuses / never answers
        h_big.answer = lambda cmd: {"ok": False, "detail": "xfs quota failed"}
        r = root.patch(f"/api/locations/{a['id']}/central/{ci['id']}", json={"quota_gb": 7000})
        assert r.status_code == 502 and "xfs quota failed" in r.text
        h_big.answer = lambda cmd: None
        old = settings.host_command_timeout_s
        settings.host_command_timeout_s = 0.5
        try:
            r = root.patch(f"/api/locations/{a['id']}/central/{ci['id']}", json={"quota_gb": 7000})
            assert r.status_code == 502 and "did not answer" in r.text
        finally:
            settings.host_command_timeout_s = old
        assert hosts.get_instance(ci["id"])["quota_gb"] == 6000

        # removal: delete_instance (data kept), then the server is retired and the site number freed
        h_big.answer = lambda cmd: {"ok": True, "detail": "deleted"}
        r = root.delete(f"/api/locations/{a['id']}/central/{ci['id']}")
        assert r.status_code == 200 and r.json()["state"] == "deleted", r.text
        assert h_big.cmds[-1]["op"] == "delete_instance" and h_big.cmds[-1]["args"] == {"id": ci["id"], "purge": False}
        assert db.one(sa.select(db.sites).where(db.sites.c.id == server["id"]))["retired_at"]
        assert hosts.get_instance(ci["id"])["site_number"] is None
        # host offline: refused unless forced
        h_small.close()
        wait(lambda: not hosts.registry.online(small["host"]["id"]))
        assert root.delete(f"/api/locations/{world['b']['id']}/central/{ci_b['id']}").status_code == 502
        assert hosts.get_instance(ci_b["id"])["state"] != "deleted"
        assert root.delete(f"/api/hub/hosts/{small['host']['id']}").status_code == 409   # still has an instance
        r = root.delete(f"/api/locations/{world['b']['id']}/central/{ci_b['id']}?force=true")
        assert r.status_code == 200 and r.json()["state"] == "deleted"
        # the expired-and-now-dead token never enrolls
        assert hosts.enroll_check(tok_b) is None
    finally:
        for h in (h_small, h_big, h_full):
            h.close()


def test_create_failure_marks_failed(world):
    base, root = world["base"], world["root"]
    host = root.post("/api/hub/hosts", json={"name": "Grumpy"}).json()
    fh = FakeHost(base, host["token"], capacity(), lambda cmd: {"ok": False, "detail": "docker: image not found"} if cmd["op"] == "create_instance" else {"ok": True})
    try:
        wait(lambda: hosts.registry.online(host["host"]["id"]))
        r = root.post(f"/api/locations/{world['c']['id']}/central", json={"quota_gb": 50, "host_id": host["host"]["id"]})
        assert r.status_code == 200, r.text
        got = wait(lambda: next((x for x in root.get(f"/api/locations/{world['c']['id']}/central").json()["instances"] if x["state"] == "failed"), None))
        assert "image not found" in got["last_error"]
        assert root.delete(f"/api/locations/{world['c']['id']}/central/{got['id']}").json()["state"] == "deleted"
    finally:
        fh.close()


def test_site_scoped_access(world):
    base, root, a, b = world["base"], world["root"], world["a"], world["b"]
    sa_ = _login(base, "siteadmin@central.example", PW)
    viewer = _login(base, "viewer@central.example", PW)
    r = sa_.get(f"/api/locations/{a['id']}/central")
    assert r.status_code == 200
    body = r.json()
    assert body["can_provision"] is False and body["can_manage"] is False and "hosts" not in body
    assert all("host_id" not in x and "last_error" not in x for x in body["instances"])
    assert sa_.get(f"/api/locations/{b['id']}/central").status_code == 403    # another Site of the same customer
    assert viewer.get(f"/api/locations/{a['id']}/central").status_code == 403  # not a Site admin
    assert sa_.post(f"/api/locations/{a['id']}/central", json={"quota_gb": 10}).status_code == 403
    assert sa_.delete(f"/api/locations/{a['id']}/central/ci_x").status_code == 403
    for path in ("/api/hub/hosts", "/api/hub/central"):
        assert sa_.get(path).status_code == 403
    assert sa_.post("/api/hub/hosts", json={"name": "x"}).status_code == 403
    assert root.get("/api/locations/l_missing/central").status_code == 404


def _server(org_id, loc_id, name="Edge"):
    sid = db.new_id("s_")
    db.insert(db.sites, {"id": sid, "org_id": org_id, "name": name, "location": "", "token_hash": db.token_hash(sid), "token_prev_hash": None,
                         "token_rotated_at": None, "created_at": time.time(), "last_seen_at": time.time(), "online": True, "version": None,
                         "summary": None, "clock_skew_s": None, "agent_ip": None, "hostname": None, "location_id": loc_id})
    return db.one(sa.select(db.sites).where(db.sites.c.id == sid))


def _open(site_id, kind=None):
    q = sa.select(db.alerts).where(db.alerts.c.site_id == site_id, db.alerts.c.closed_at.is_(None))
    if kind:
        q = q.where(db.alerts.c.kind == kind)
    return db.rows(q)


def test_site_link_down_suppresses_camera_down(world, monkeypatch):
    monkeypatch.setattr(alerts, "on_open", None)
    s = _server(world["org"]["id"], world["c"]["id"])
    cams_bad = [{"id": f"cam{i}", "name": f"Cam {i}", "stream_ready": False, "problems": ["no frames"], "link_down": True} for i in range(5)]
    # one camera already counted down twice before the server decided the whole link is down
    alerts.on_heartbeat(s, {"cameras": [{**cams_bad[0], "link_down": False}]}, 0)
    alerts.on_heartbeat(s, {"cameras": [{**cams_bad[0], "link_down": False}]}, 0)
    assert len(_open(s["id"], "camera_down")) == 1
    for _ in range(3):
        alerts.on_heartbeat(s, {"cameras": cams_bad, "site_link_down": True}, 0)
    rows = _open(s["id"])
    assert [r["kind"] for r in rows] == ["site_link_down"]
    assert rows[0]["detail"]["text"] == "All cameras at Yard C are unreachable: the link to the site may be down"
    # link back: closed; a camera still broken (not flagged link_down) opens camera_down as usual
    cams_ok = [{"id": f"cam{i}", "name": f"Cam {i}", "stream_ready": True, "problems": []} for i in range(5)]
    alerts.on_heartbeat(s, {"cameras": cams_ok, "site_link_down": False}, 0)
    assert _open(s["id"]) == []
    assert "site_link_down" in alerts.KINDS and "site_link_down" in push.DEFAULT_KINDS and "host_offline" in push.DEFAULT_KINDS
    # link_down on a camera without the server-wide flag changes nothing
    for _ in range(2):
        alerts.on_heartbeat(s, {"cameras": [cams_bad[1]]}, 0)
    assert [r["kind"] for r in _open(s["id"])] == ["camera_down"]


def test_whole_site_dark_waits_for_link_down_before_camera_alerts(world, monkeypatch):
    """Every camera dark at once: no camera_down for 4 heartbeats (~2 min), so the server's 90 s site_link_down
    arrives first and one link drop doesn't push five camera alerts."""
    monkeypatch.setattr(alerts, "on_open", None)
    s = _server(world["org"]["id"], world["c"]["id"], name="Dark")
    dark = [{"id": f"cam{i}", "name": f"Cam {i}", "stream_ready": False, "problems": ["no frames"]} for i in range(5)]
    for _ in range(3):
        alerts.on_heartbeat(s, {"cameras": dark}, 0)
    assert _open(s["id"], "camera_down") == []
    alerts.on_heartbeat(s, {"cameras": dark, "site_link_down": True}, 0)   # the server noticed in time
    assert [r["kind"] for r in _open(s["id"])] == ["site_link_down"]
    # a server that never reports site_link_down still gets its camera alerts, just later
    s2 = _server(world["org"]["id"], world["c"]["id"], name="Old")
    for _ in range(4):
        alerts.on_heartbeat(s2, {"cameras": dark}, 0)
    assert len(_open(s2["id"], "camera_down")) == 5
    # one camera down among working ones keeps the quick 2-heartbeat alert
    s3 = _server(world["org"]["id"], world["c"]["id"], name="One")
    mixed = [dark[0], {"id": "cam1", "name": "Cam 1", "stream_ready": True, "problems": []}]
    for _ in range(2):
        alerts.on_heartbeat(s3, {"cameras": mixed}, 0)
    assert [r["detail"]["name"] for r in _open(s3["id"], "camera_down")] == ["Cam 0"]


def test_host_offline_alert_reaches_hub_admins_only(world, superuser, monkeypatch):
    monkeypatch.setattr(alerts, "on_open", None)
    row, _ = hosts.create_host("Gone", None, None)
    db.run(sa.update(db.hosts).where(db.hosts.c.id == row["id"]).values(online=False, last_seen_at=time.time() - 3600))
    monkeypatch.setattr(hosts.registry, "started_at", 0)
    asyncio.run(hosts.registry.sweep())
    rows = _open(row["id"], "host_offline")
    assert len(rows) == 1 and rows[0]["org_id"] == hosts.HUB_ORG and rows[0]["detail"]["name"] == "Gone"
    asyncio.run(hosts.registry.sweep())
    assert len(_open(row["id"], "host_offline")) == 1                  # one open row, not one per sweep
    listed = next(h for h in world["root"].get("/api/hub/hosts").json()["hosts"] if h["id"] == row["id"])
    assert listed["offline_since"] == rows[0]["opened_at"]
    # pushed to hub administrators, never to customer members
    viewer = auth.user_by_email("viewer@central.example")
    push.subscribe(superuser["id"], {"endpoint": "https://fcm.googleapis.com/fcm/send/root-central"}, None, "t")
    push.subscribe(viewer["id"], {"endpoint": "https://fcm.googleapis.com/fcm/send/viewer-central"}, [*push.DEFAULT_KINDS], "t")
    sent: list[tuple] = []
    push.set_sender(lambda sub, payload: sent.append((sub["user_id"], payload)) or True)
    try:
        n = asyncio.run(push.notify_alert(hosts.HUB_ORG, hosts._alert_subject(row), "host_offline", rows[0]["detail"]))
    finally:
        push.set_sender(None)
        push.unsubscribe(superuser["id"], "https://fcm.googleapis.com/fcm/send/root-central")   # later tests count pushes
        push.unsubscribe(viewer["id"], "https://fcm.googleapis.com/fcm/send/viewer-central")
    assert n >= 1 and {uid for uid, _ in sent} == {superuser["id"]}
    assert sent[0][1]["title"] == "Host Gone is offline" and sent[0][1]["url"] == "/hub/hosts"
    # the customer push picker never offers it; hub admins see it
    assert "host_offline" not in _login(world["base"], "viewer@central.example", PW).get("/api/push/vapid").json()["kinds"]
    assert "host_offline" in world["root"].get("/api/push/vapid").json()["kinds"]
    # it reconnects: closed
    alerts.close(hosts._alert_subject(row), "host_offline")
    assert _open(row["id"]) == []
    asyncio.run(hosts.delete_host(row["id"]))


def test_real_server_agent_enrolls_with_its_token(world):
    """The server's own hub agent (backend nvr.hub_agent) presenting NVR_HUB_ENROLL_TOKEN lands in exactly the Site."""
    from types import SimpleNamespace
    from nvr import hub_agent
    from nvr.config import settings as nvr_settings
    from nvr.db import db as site_db
    if not hasattr(nvr_settings, "hub_enroll_token"):
        pytest.skip("this server version has no enrollment-token support")
    base, root, org = world["base"], world["root"], world["org"]
    loc = root.post(f"/api/orgs/{org['id']}/locations", json={"name": "Yard D"}).json()
    host = root.post("/api/hub/hosts", json={"name": "E2E host"}).json()
    seen: list[dict] = []
    fh = FakeHost(base, host["token"], capacity(), lambda cmd: seen.append(cmd["args"]) or {"ok": True, "detail": "created"})
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    agent = None
    try:
        wait(lambda: hosts.registry.online(host["host"]["id"]))
        ci = root.post(f"/api/locations/{loc['id']}/central", json={"quota_gb": 100, "host_id": host["host"]["id"]}).json()
        args = wait(lambda: next((a for a in seen if a["id"] == ci["id"]), None))
        site_db.set_setting("hub_url", _ws(base) + "/agent")
        site_db.set_setting("hub_token", None)
        site_db.set_setting("hub_claim", None)
        site_db.set_setting("hub_enroll_used", None)
        nvr_settings.hub_enroll_token = args["enroll_token"]

        async def summary(st, since):
            return {"cameras": [], "today": {}, "queues": {"verify": 0, "synopsis": 0}}
        agent = hub_agent.HubAgent(lambda *a: None, SimpleNamespace(pipeline=SimpleNamespace(subscribers=set()), ingests={}, health=None),
                                   summary_fn=summary)
        asyncio.run_coroutine_threadsafe(agent.run(), loop)
        wait(lambda: agent.enrolled and agent.connected, timeout=20)
        assert agent.location_id == loc["id"]
        row = hosts.get_instance(ci["id"])
        assert row["state"] == "running" and db.one(sa.select(db.sites).where(db.sites.c.id == row["server_id"]))["location_id"] == loc["id"]
    finally:
        nvr_settings.hub_enroll_token = ""
        if agent is not None:
            asyncio.run_coroutine_threadsafe(agent.configure(unenrol=True), loop).result(10)
        site_db.set_setting("hub_token", None)
        loop.call_soon_threadsafe(loop.stop)
        fh.close()


def test_check_camera_network():
    ok = hosts.check_camera_network(["192.168.105.7/24", "10.20.7.0/24", "100.64.3.0/24", "10.20.7.0/24"], ["203.0.113.7", " 198.51.100.9 "],
                                    ["Cam1.Example.NET.", "cam1.example.net"])
    assert ok == {"subnets": ["192.168.105.0/24", "10.20.7.0/24", "100.64.3.0/24"], "public_ips": ["203.0.113.7", "198.51.100.9"],
                  "hosts": ["cam1.example.net"]}
    assert hosts.check_camera_network([], [], []) == {"subnets": [], "public_ips": [], "hosts": []}
    for subnets, ips, names in ((["0.0.0.0/0"], [], []), (["10.0.0.0/8"], [], []), (["8.8.8.0/24"], [], []), (["10.200.4.0/24"], [], []),
                                (["10.201.0.0/24"], [], []), (["127.0.0.0/16"], [], []), (["fd00::/64"], [], []), (["nope"], [], []),
                                ([], ["cam.example.net"], []), ([], ["127.0.0.1"], []), ([], ["169.254.1.1"], []), ([], ["224.0.0.1"], []),
                                ([], ["10.200.0.2"], []), ([], [], ["1.2.3.4"]), ([], [], ["localhost"]), ([], [], ["bad name.example.net"]),
                                ([], [], ["-x.example.net"]), ([f"10.30.{i}.0/24" for i in range(33)], [], []), ("10.20.7.0/24", [], [])):
        with pytest.raises(ValueError):
            hosts.check_camera_network(subnets, ips, names)
    # rows from before camera networks: what the host allowed then
    assert hosts.camera_network({"mode": "vpn", "subnet": "10.20.7.0/24", "public_ip": None}) == {"subnets": ["10.20.7.0/24"], "public_ips": [], "hosts": []}
    assert hosts.camera_network({"mode": "forward", "subnet": None, "public_ip": "93.184.216.34"}) == {"subnets": [], "public_ips": ["93.184.216.34"], "hosts": []}
    assert hosts.camera_network({"mode": "forward", "subnet": None, "public_ip": "yard.dyn.example.net"})["hosts"] == ["yard.dyn.example.net"]


def test_camera_addresses(world, superuser):
    base, root, org, a = world["base"], world["root"], world["org"], world["a"]
    e = root.post(f"/api/orgs/{org['id']}/locations", json={"name": "Yard E"}).json()
    host = root.post("/api/hub/hosts", json={"name": "Camnet host"}).json()

    def answer(cmd):
        if cmd["op"] == "create_instance":
            return {"ok": True, "detail": "created", "instance": {"id": cmd["args"]["id"], "state": "running"}}
        if cmd["op"] == "set_camera_network":
            return {"ok": True, "detail": "set", "instance": {"id": cmd["args"]["id"], "hosts": cmd["args"]["hosts"],
                                                              "host_ips": {h: ["203.0.113.21"] for h in cmd["args"]["hosts"]}}}
        return {"ok": True, "detail": "done"}
    fh = FakeHost(base, host["token"], capacity(), answer, "camnet")
    made: list[dict] = []
    try:
        wait(lambda: hosts.registry.online(host["host"]["id"]))
        ci = root.post(f"/api/locations/{a['id']}/central", json={"mode": "vpn", "quota_gb": 100, "host_id": host["host"]["id"]}).json()
        made.append(ci)
        n = ci["site_number"]
        assert ci["camera_network"] == {"subnets": [f"10.20.{n}.0/24"], "public_ips": [], "hosts": [], "auto": {"public_ips": [], "hosts": []},
                                        "auto_on": True, "resolved": {}, "pending": False, "sync_error": None}
        assert ci["peplink"]["forward_addresses"] == []
        wait(lambda: hosts.get_instance(ci["id"])["ready_at"])
        url = f"/api/locations/{a['id']}/central/{ci['id']}/cameras"
        body = {"subnets": ["192.168.105.0/24", f"10.20.{n}.0/24"], "public_ips": ["203.0.113.7"], "hosts": ["Cam1.example.net"]}

        # who may: hub administrators only (like the quota); the Site's own admins read it but cannot change it
        sa_ = _login(base, "siteadmin@central.example", PW)
        viewer = _login(base, "viewer@central.example", PW)
        assert sa_.get(f"/api/locations/{a['id']}/central").status_code == 200
        assert sa_.put(url, json=body).status_code == 403
        assert viewer.put(url, json=body).status_code == 403
        assert httpx.put(base + url, json=body).status_code == 401
        sent_before = len(fh.cmds)
        # bad input: refused, nothing sent to the host
        for bad in ({"subnets": ["0.0.0.0/0"]}, {"subnets": ["10.200.1.0/24"]}, {"subnets": ["8.8.8.0/24"]}, {"public_ips": ["cam.example.net"]},
                    {"hosts": ["bad name"]}, {"hosts": ["10.1.2.3"]}, {"subnets": [f"10.30.{i}.0/24" for i in range(20)],
                                                                      "public_ips": [f"198.51.100.{i}" for i in range(1, 14)]}):
            r = root.put(url, json=bad)
            assert r.status_code == 400, (bad, r.text)
        assert root.put(url, json={"subnets": "10.20.7.0/24"}).status_code == 422
        assert len(fh.cmds) == sent_before
        # a Site that isn't the instance's
        assert root.put(f"/api/locations/{e['id']}/central/{ci['id']}/cameras", json=body).status_code == 404

        r = root.put(url, json=body)
        assert r.status_code == 200, r.text
        out = r.json()
        want = {"subnets": ["192.168.105.0/24", f"10.20.{n}.0/24"], "public_ips": ["203.0.113.7"], "hosts": ["cam1.example.net"]}
        assert fh.cmds[-1]["op"] == "set_camera_network" and fh.cmds[-1]["args"] == {"id": ci["id"], **want}
        assert out["camera_network"] == {**want, "auto": {"public_ips": [], "hosts": []}, "auto_on": True,
                                         "resolved": {"cam1.example.net": ["203.0.113.21"]}, "pending": False, "sync_error": None}
        assert out["peplink"]["forward_addresses"] == ["203.0.113.7", "cam1.example.net"]
        assert out["mode"] == "vpn" and out["subnet"] == f"10.20.{n}.0/24"          # the BR1 LAN of the Peplink sheet stays
        assert hosts.get_instance(ci["id"])["camera_network"] == {**want, "auto": {"public_ips": [], "hosts": []}, "applied": want}
        audit = db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id == org["id"], db.audit_log.c.action.like("central recording camera addresses%")))
        assert len(audit) == 1 and audit[0]["detail"]["to"] == want and audit[0]["detail"]["from"]["subnets"] == [f"10.20.{n}.0/24"]
        assert audit[0]["user_email"] == superuser["email"]
        # the Site's admins see it
        seen = sa_.get(f"/api/locations/{a['id']}/central").json()["instances"][0]
        assert seen["camera_network"]["hosts"] == ["cam1.example.net"]

        # another Site's instance cannot take the same subnet, public IP or DNS name
        ci_e = root.post(f"/api/locations/{e['id']}/central", json={"mode": "forward", "public_ip": "93.184.216.40", "quota_gb": 100,
                                                                    "host_id": host["host"]["id"]}).json()
        made.append(ci_e)
        assert {k: ci_e["camera_network"][k] for k in ("subnets", "public_ips", "hosts")} == {"subnets": [], "public_ips": ["93.184.216.40"], "hosts": []}
        url_e = f"/api/locations/{e['id']}/central/{ci_e['id']}/cameras"
        for clash in ({"subnets": ["192.168.105.128/25"]}, {"public_ips": ["203.0.113.7"]}, {"hosts": ["cam1.example.net"]}):
            assert root.put(url_e, json=clash).status_code == 409, clash

        # the host refuses: 502, nothing stored or audited
        fh.answer = lambda cmd: {"ok": False, "detail": "subnet 192.168.106.0/24 overlaps the hub address"}
        r = root.put(url, json={"subnets": ["192.168.106.0/24"]})
        assert r.status_code == 502 and "overlaps the hub" in r.text and "nothing was changed" in r.text
        assert hosts.camera_network(hosts.get_instance(ci["id"])) == want
        # the host offline: a clear error, nothing stored
        fh.close()
        wait(lambda: not hosts.registry.online(host["host"]["id"]))
        r = root.put(url, json={"subnets": ["192.168.106.0/24"]})
        assert r.status_code == 502 and "offline" in r.text
        assert hosts.camera_network(hosts.get_instance(ci["id"])) == want
        assert len(db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id == org["id"],
                                                         db.audit_log.c.action.like("central recording camera addresses%")))) == 1
    finally:
        fh.close()
        for c in made:
            root.delete(f"/api/locations/{c['location_id']}/central/{c['id']}?force=true")


# ---------------------------------------------------------------- work queues (capacity.work) and CPU / memory

def _work(q: dict, waiting=0, running=1, at=1000.0, vok=True, iok=True):
    """A capacity.work as the agent sends it: q = {instance id: verify_q}."""
    return {"at": at, "vllm": {"ok": vok, "running": running, "waiting": waiting, "kv_cache_pct": 34.1, "gen_tps": 180.0, "gpu": 0, "at": at},
            "instances": {iid: {"verify_q": v, "synopsis_q": 0, "verify_rate_per_min": 0.5, "gpu": 1, "ok": iok,
                                "at": at if iok else at - 600} for iid, v in q.items()}}


def test_work_served_with_history(world):
    base, root = world["base"], world["root"]
    host = root.post("/api/hub/hosts", json={"name": "Worker"}).json()
    hid = host["host"]["id"]
    fh = FakeHost(base, host["token"], capacity(), None, "worker")
    try:
        wait(lambda: hosts.registry.online(hid))
        listed = next(h for h in root.get("/api/hub/hosts").json()["hosts"] if h["id"] == hid)
        assert "work" not in listed["capacity"] and listed["work_history"] == [] and listed["queue_alerts"] == []   # an older agent
        for n, w in enumerate((5, 9)):
            fh.send({"t": "heartbeat", "capacity": {**capacity(), "work": _work({"ci_x": w, "ci_y": 3}, waiting=n)}, "instances": []})
        listed = wait(lambda: next((h for h in root.get("/api/hub/hosts").json()["hosts"] if h["id"] == hid and len(h["work_history"]) == 2), None))
        assert listed["capacity"]["work"]["instances"]["ci_x"]["verify_q"] == 9 and listed["capacity"]["work"]["vllm"]["gen_tps"] == 180.0
        assert [(s["verify_q"], s["vllm_waiting"]) for s in listed["work_history"]] == [(8, 0), (12, 1)]
        assert listed["work_trend"]["instances"]["ci_x"] == {"verify_q": 9, "verify_q_before": None, "growing": False, "recovered": True}
        # a malformed work object costs nothing but itself
        fh.send({"t": "heartbeat", "capacity": {**capacity(free_gb=6000.0), "work": {"instances": "nope", "vllm": 3}}, "instances": []})
        wait(lambda: (db.one(sa.select(db.hosts).where(db.hosts.c.id == hid))["capacity"] or {}).get("disks", [{}])[0].get("free_gb") == 6000.0)
    finally:
        fh.close()
    wait(lambda: not hosts.registry.online(hid))
    asyncio.run(hosts.delete_host(hid))
    assert hosts.work_history(hid) == []


def test_queue_growing_alert(world, superuser, monkeypatch):
    monkeypatch.setattr(alerts, "on_open", None)
    org = world["org"]
    loc = world["root"].post(f"/api/orgs/{org['id']}/locations", json={"name": "Yard Q"}).json()
    row, _ = hosts.create_host("Fred", None, None)
    hid = row["id"]
    ci_id = db.new_id("ci_")
    t0 = time.time()
    db.insert(db.central_instances, {"id": ci_id, "host_id": hid, "location_id": loc["id"], "org_id": org["id"], "server_id": None, "name": "Central",
                                     "mode": "vpn", "subnet": None, "public_ip": None, "site_number": None, "quota_gb": 100, "gpu": 1,
                                     "state": "running", "last_error": None, "created_at": t0, "created_by": None, "updated_at": t0, "ready_at": t0,
                                     "info": None, "info_at": None, "camera_limit": None, "camera_network": None})
    opened = lambda: {r["key"]: r for r in _open(hid, hosts.QUEUE_KIND)}  # noqa: E731
    t = 1_000_000.0

    def beat(q, waiting=0, **kw):
        nonlocal t
        t += 30
        hosts.record_work(hid, _work(q, waiting=waiting, at=t, **kw), t)
        hosts.check_work_alerts(row, t)

    try:
        # growing from the start, but not 15 minutes of history yet: nothing
        for i in range(25):                       # 12.5 minutes, 10 -> 58
            beat({ci_id: 10 + 2 * i})
        assert opened() == {}
        for i in range(25, 32):                   # past 15 minutes: open, once
            beat({ci_id: 10 + 2 * i})
        rows = opened()
        assert list(rows) == [f"verify:{ci_id}"]
        d = rows[f"verify:{ci_id}"]["detail"]
        assert d["site"] == "Yard Q" and d["host"] == "Fred" and d["instance_id"] == ci_id and d["queue"] == "verify"
        assert d["text"].startswith("Yard Q on Fred: YOLO verify queue at ") and "up from" in d["text"] and d["location_id"] == loc["id"]
        assert rows[f"verify:{ci_id}"]["org_id"] == hosts.HUB_ORG
        # it shows on the host card
        listed = next(h for h in hosts.list_hosts() if h["id"] == hid)
        assert listed["queue_alerts"][0]["key"] == f"verify:{ci_id}" and "Yard Q" in listed["queue_alerts"][0]["text"]
        assert listed["work_trend"]["instances"][ci_id]["growing"] is True
        # steady (not growing, not recovered): stays open; unknown values (the instance stopped answering): stays open
        for _ in range(4):
            beat({ci_id: 72})
        beat({ci_id: 72}, iok=False)
        assert hosts.work_trend(hid)["instances"][ci_id]["verify_q"] is None
        assert list(opened()) == [f"verify:{ci_id}"]
        # recovers (at or below 20): closed
        beat({ci_id: 15})
        assert opened() == {}
        # a high queue that is shrinking never opens
        hosts._work.pop(hid)
        for i in range(40):
            beat({ci_id: 500 - i})
        assert opened() == {}

        # vLLM: waiting for 10 minutes without a break opens; one heartbeat at 0 starts over
        for _ in range(15):                       # 7.5 minutes
            beat({ci_id: 0}, waiting=3)
        beat({ci_id: 0}, waiting=0)
        for _ in range(20):                       # a new run: 20 heartbeats = 9.5 minutes from its first
            beat({ci_id: 0}, waiting=2)
        assert opened() == {}
        beat({ci_id: 0}, waiting=2)               # 10 minutes
        rows = opened()
        assert list(rows) == ["vllm"] and rows["vllm"]["detail"]["text"].startswith("Fred: Qwen (vLLM) has had requests waiting for 10 minutes")
        beat({ci_id: 0}, waiting=1, vok=False)    # unreadable: unchanged
        assert list(opened()) == ["vllm"]
        beat({ci_id: 0}, waiting=0)
        assert opened() == {}
        # a gap in the heartbeats (agent away) breaks the run
        for _ in range(10):
            beat({ci_id: 0}, waiting=4)
        t += 600
        for _ in range(12):
            beat({ci_id: 0}, waiting=4)
        assert opened() == {}

        # an instance that is no longer reported: its alert closes
        hosts._work.pop(hid)
        for i in range(32):
            beat({ci_id: 10 + 2 * i})
        assert list(opened()) == [f"verify:{ci_id}"]
        beat({})
        assert opened() == {}

        # pushed to hub administrators only, with its own title; offered in the push picker to them only
        assert hosts.QUEUE_KIND in alerts.KINDS and hosts.QUEUE_KIND in alerts.HUB_KINDS and hosts.QUEUE_KIND in push.DEFAULT_KINDS
        viewer = auth.user_by_email("viewer@central.example")
        push.subscribe(superuser["id"], {"endpoint": "https://fcm.googleapis.com/fcm/send/root-queue"}, None, "t")
        push.subscribe(viewer["id"], {"endpoint": "https://fcm.googleapis.com/fcm/send/viewer-queue"}, [*push.DEFAULT_KINDS], "t")
        sent: list[tuple] = []
        push.set_sender(lambda sub, payload: sent.append((sub["user_id"], payload)) or True)
        try:
            n = asyncio.run(push.notify_alert(hosts.HUB_ORG, hosts._alert_subject(row), hosts.QUEUE_KIND, d))
        finally:
            push.set_sender(None)
            push.unsubscribe(superuser["id"], "https://fcm.googleapis.com/fcm/send/root-queue")
            push.unsubscribe(viewer["id"], "https://fcm.googleapis.com/fcm/send/viewer-queue")
        assert n >= 1 and {uid for uid, _ in sent} == {superuser["id"]}
        assert sent[0][1]["title"] == "Host Fred: work queue growing" and sent[0][1]["body"].startswith("Yard Q on Fred")
        assert sent[0][1]["url"] == "/hub/hosts"
        assert hosts.QUEUE_KIND not in _login(world["base"], "viewer@central.example", PW).get("/api/push/vapid").json()["kinds"]
        assert hosts.QUEUE_KIND in world["root"].get("/api/push/vapid").json()["kinds"]
    finally:
        db.run(sa.update(db.central_instances).where(db.central_instances.c.id == ci_id).values(state="deleted"))
        asyncio.run(hosts.delete_host(hid))
    assert _open(hid) == []


def test_set_resources(world):
    base, root, org = world["base"], world["root"], world["org"]
    loc = root.post(f"/api/orgs/{org['id']}/locations", json={"name": "Yard R"}).json()
    host = root.post("/api/hub/hosts", json={"name": "Resizer"}).json()

    def answer(cmd):
        if cmd["op"] == "create_instance":
            return {"ok": True, "detail": "created", "instance": {"id": cmd["args"]["id"], "state": "running", "cpus": 4, "mem_gb": 8}}
        if cmd["op"] == "set_resources":
            a = cmd["args"]
            return {"ok": True, "detail": "recreated", "instance": {"id": a["id"], "state": "running", "cpus": a.get("cpus", 8), "mem_gb": a.get("mem_gb", 8)}}
        return {"ok": True, "detail": "done"}
    fh = FakeHost(base, host["token"], capacity(), answer, "resizer")
    ci = None
    try:
        wait(lambda: hosts.registry.online(host["host"]["id"]))
        ci = root.post(f"/api/locations/{loc['id']}/central", json={"mode": "vpn", "quota_gb": 100, "host_id": host["host"]["id"]}).json()
        wait(lambda: hosts.get_instance(ci["id"])["ready_at"])
        # the heartbeat reports its limits
        fh.send({"t": "heartbeat", "capacity": capacity(), "instances": [{"id": ci["id"], "state": "running", "cpus": 4, "mem_gb": 8}]})
        wait(lambda: (hosts.get_instance(ci["id"])["info"] or {}).get("cpus") == 4)
        assert next(c for c in root.get("/api/hub/central").json() if c["id"] == ci["id"])["cpus"] == 4
        url = f"/api/locations/{loc['id']}/central/{ci['id']}/resources"

        # hub administrators only
        sa_ = _login(base, "siteadmin@central.example", PW)
        viewer = _login(base, "viewer@central.example", PW)
        sent_before = len(fh.cmds)
        assert sa_.put(url, json={"cpus": 8}).status_code == 403
        assert viewer.put(url, json={"cpus": 8}).status_code == 403
        assert httpx.put(base + url, json={"cpus": 8}).status_code == 401
        # bad input: refused before the host hears of it
        assert root.put(url, json={}).status_code == 400
        assert root.put(url, json={"cpus": 100}).status_code == 422
        assert root.put(url, json={"mem_gb": 1}).status_code == 422
        assert root.put(f"/api/locations/{world['b']['id']}/central/{ci['id']}/resources", json={"cpus": 8}).status_code == 404
        assert len(fh.cmds) == sent_before

        r = root.put(url, json={"cpus": 8})
        assert r.status_code == 200, r.text
        assert r.json()["cpus"] == 8 and r.json()["mem_gb"] == 8
        assert fh.cmds[-1]["op"] == "set_resources" and fh.cmds[-1]["args"] == {"id": ci["id"], "cpus": 8.0}
        r = root.put(url, json={"mem_gb": 16})
        assert r.json()["mem_gb"] == 16 and fh.cmds[-1]["args"] == {"id": ci["id"], "mem_gb": 16.0}
        audit = db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id == org["id"],
                                                      db.audit_log.c.action.like("central recording CPU/memory%")).order_by(db.audit_log.c.ts))
        assert [x["action"] for x in audit] == ["central recording CPU/memory: Yard R 4 CPUs, 8 GB -> 8 CPUs, 8 GB",
                                                "central recording CPU/memory: Yard R 8 CPUs, 8 GB -> 8 CPUs, 16 GB"]
        assert audit[0]["user_email"] == "root@example.com" and audit[0]["detail"]["instance_id"] == ci["id"]

        # the host refuses: 502, nothing stored or audited
        fh.answer = lambda cmd: {"ok": False, "detail": "cpus 64 is more than this host has (48)"}
        r = root.put(url, json={"cpus": 64})
        assert r.status_code == 502 and "more than this host has" in r.text and "nothing was changed" in r.text
        assert hosts.get_instance(ci["id"])["info"]["cpus"] == 8
        assert len(db.rows(sa.select(db.audit_log).where(db.audit_log.c.action.like("central recording CPU/memory%")))) == 2
    finally:
        fh.close()
        if ci:
            root.delete(f"/api/locations/{loc['id']}/central/{ci['id']}?force=true")


def test_gpu_placement_reserves_shares(world):
    """Each Site on a card reserves its GPU share: the card takes Sites until the shares fill it, then placement says
    so (409) instead of squeezing another one on; a smaller share still fits. The Hosts list shows the room."""
    base, root, org = world["base"], world["root"], world["org"]
    host = root.post("/api/hub/hosts", json={"name": "Sharer"}).json()
    hid = host["host"]["id"]
    gpus = [{"index": 0, "name": "NVIDIA A40", "mem_total_mb": 49140, "mem_used_mb": 30000},
            {"index": 1, "name": "NVIDIA A10G", "mem_total_mb": 23028, "mem_used_mb": 1000}]
    fh = FakeHost(base, host["token"], capacity(gpus=gpus), None, "sharer")
    fh.answer = lambda cmd: ({"ok": True, "detail": "created", "instance": {"id": cmd["args"]["id"], "state": "running"}}
                             if cmd["op"] == "create_instance" else {"ok": True, "detail": "done"})
    old = settings.central_gpu_mem_mb
    settings.central_gpu_mem_mb = 8192                     # (23028 - 1024) // 8192 = 2 Sites per A10G
    made = []
    try:
        wait(lambda: hosts.registry.online(hid))
        h = next(x for x in root.get("/api/hub/hosts").json()["hosts"] if x["id"] == hid)
        assert [g["index"] for g in h["gpu_room"]] == [1] and h["gpu_slots"] == 2   # the A40's 19 GB free never counts
        locs = [root.post(f"/api/orgs/{org['id']}/locations", json={"name": f"Share {i}"}).json() for i in range(4)]
        for loc in locs[:2]:
            r = root.post(f"/api/locations/{loc['id']}/central", json={"mode": "vpn", "quota_gb": 100, "host_id": hid})
            assert r.status_code == 200, r.text
            made.append((loc, r.json()))
            assert r.json()["gpu"] == 1 and fh.cmds[-1]["args"]["gpu_mem_mb"] == 8192
        r = root.post(f"/api/locations/{locs[2]['id']}/central", json={"mode": "vpn", "quota_gb": 100, "host_id": hid})
        assert r.status_code == 409 and "no GPU has room" in r.text and "16.0 of 21.5 GB shared" in r.text, r.text
        r = root.post(f"/api/locations/{locs[2]['id']}/central", json={"mode": "vpn", "quota_gb": 100, "host_id": hid, "gpu": 1})
        assert r.status_code == 409 and "this Site's share is 8 GB" in r.text
        # a small Site's share still fits in what is left
        r = root.post(f"/api/locations/{locs[2]['id']}/central", json={"mode": "vpn", "quota_gb": 100, "host_id": hid, "gpu_mem_mb": 4096})
        assert r.status_code == 200, r.text
        made.append((locs[2], r.json()))
        assert fh.cmds[-1]["args"]["gpu_mem_mb"] == 4096
        assert root.post(f"/api/locations/{locs[3]['id']}/central", json={"quota_gb": 100, "host_id": hid, "gpu_mem_mb": 512}).status_code == 400
        h = next(x for x in root.get("/api/hub/hosts").json()["hosts"] if x["id"] == hid)
        assert h["gpu_room"][0]["reserved_mb"] == 8192 * 2 + 4096 and h["gpu_slots"] == 0
        # the A40 chosen by hand is not placed against vLLM's memory by these rules, but still needs free memory
        r = root.post(f"/api/locations/{locs[3]['id']}/central", json={"mode": "vpn", "quota_gb": 100, "host_id": hid, "gpu": 0})
        assert r.status_code == 200, r.text
        made.append((locs[3], r.json()))
    finally:
        settings.central_gpu_mem_mb = old
        fh.close()
        for loc, ci in made:
            root.delete(f"/api/locations/{loc['id']}/central/{ci['id']}?force=true")


def test_set_resources_gpu_share(world):
    """A Site's GPU share changes with a set_resources round trip (the host recreates it), within its card's room;
    a CPU instance has none to change."""
    base, root, org = world["base"], world["root"], world["org"]
    loc = root.post(f"/api/orgs/{org['id']}/locations", json={"name": "Yard GS"}).json()
    host = root.post("/api/hub/hosts", json={"name": "Shareresizer"}).json()
    hid = host["host"]["id"]

    def answer(cmd):
        a = cmd["args"]
        if cmd["op"] == "create_instance":
            return {"ok": True, "detail": "created", "instance": {"id": a["id"], "state": "running", "cpus": 4, "mem_gb": 8,
                                                                    "gpu_mem_mb": a.get("gpu_mem_mb")}}
        if cmd["op"] == "set_resources":
            return {"ok": True, "detail": "recreated", "instance": {"id": a["id"], "state": "running", "cpus": 4, "mem_gb": 8,
                                                                    "gpu_mem_mb": a.get("gpu_mem_mb") or None}}
        return {"ok": True, "detail": "done"}
    gpus = [{"index": 1, "name": "NVIDIA A10G", "mem_total_mb": 23028, "mem_used_mb": 1000}]
    fh = FakeHost(base, host["token"], capacity(gpus=gpus), answer, "shareresizer")
    ci = None
    try:
        wait(lambda: hosts.registry.online(hid))
        ci = root.post(f"/api/locations/{loc['id']}/central", json={"mode": "vpn", "quota_gb": 100, "host_id": hid}).json()
        wait(lambda: hosts.get_instance(ci["id"])["ready_at"])
        url = f"/api/locations/{loc['id']}/central/{ci['id']}/resources"
        r = root.put(url, json={"gpu_mem_mb": 6144})
        assert r.status_code == 200, r.text
        assert fh.cmds[-1]["op"] == "set_resources" and fh.cmds[-1]["args"] == {"id": ci["id"], "gpu_mem_mb": 6144}
        assert hosts.get_instance(ci["id"])["info"]["gpu_mem_mb"] == 6144
        act = db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id == org["id"],
                                                    db.audit_log.c.action.like("central recording CPU/memory: Yard GS%")))
        assert act and act[-1]["action"].endswith("GPU share 4096 MB -> 6144 MB")
        # more than the card can give: refused before the host hears of it
        sent = len(fh.cmds)
        r = root.put(url, json={"gpu_mem_mb": 30000})
        assert r.status_code == 409 and "can grow to" in r.text and len(fh.cmds) == sent
        assert root.put(url, json={"gpu_mem_mb": 512}).status_code == 400
        r = root.put(url, json={"gpu_mem_mb": 0})                     # no cap
        assert r.status_code == 200 and fh.cmds[-1]["args"] == {"id": ci["id"], "gpu_mem_mb": 0}
    finally:
        fh.close()
        if ci:
            root.delete(f"/api/locations/{loc['id']}/central/{ci['id']}?force=true")


def test_set_instance_quota(world):
    """Change data storage: a set_instance_quota round trip (the instance's data dataset: database, event media), hub
    administrators only, its new figures kept and audited; the host's refusal (below its use, no data dataset of its
    own, an older agent) changes nothing."""
    base, root, org = world["base"], world["root"], world["org"]
    loc = root.post(f"/api/orgs/{org['id']}/locations", json={"name": "Yard DQ"}).json()
    host = root.post("/api/hub/hosts", json={"name": "Datahost"}).json()

    def answer(cmd):
        if cmd["op"] == "create_instance":
            return {"ok": True, "detail": "created", "instance": {"id": cmd["args"]["id"], "state": "running"}}
        if cmd["op"] == "set_instance_quota":
            a = cmd["args"]
            return {"ok": True, "detail": f"{a['id']} data quota {a['quota_gb']} GB", "instance": {
                "id": a["id"], "state": "running", "instance_used_gb": 46.0, "instance_quota_gb": float(a["quota_gb"]),
                "instance_free_gb": a["quota_gb"] - 46.0}}
        return {"ok": True, "detail": "done"}
    fh = FakeHost(base, host["token"], capacity(), answer, "datahost")
    ci = None
    try:
        wait(lambda: hosts.registry.online(host["host"]["id"]))
        ci = root.post(f"/api/locations/{loc['id']}/central", json={"mode": "vpn", "quota_gb": 100, "host_id": host["host"]["id"]}).json()
        wait(lambda: hosts.get_instance(ci["id"])["ready_at"])
        fh.send({"t": "heartbeat", "capacity": capacity(), "instances": [{"id": ci["id"], "state": "running", "instance_used_gb": 46.0,
                                                                          "instance_quota_gb": 50.0, "instance_free_gb": 4.0}]})
        wait(lambda: (hosts.get_instance(ci["id"])["info"] or {}).get("instance_quota_gb") == 50.0)
        url = f"/api/locations/{loc['id']}/central/{ci['id']}/data-quota"

        # hub administrators only
        sa_ = _login(base, "siteadmin@central.example", PW)
        viewer = _login(base, "viewer@central.example", PW)
        sent_before = len(fh.cmds)
        assert sa_.put(url, json={"quota_gb": 150}).status_code == 403
        assert viewer.put(url, json={"quota_gb": 150}).status_code == 403
        assert httpx.put(base + url, json={"quota_gb": 150}).status_code == 401
        # bad input: refused before the host hears of it
        for bad in ({}, {"quota_gb": 0}, {"quota_gb": 10_000_001}, {"quota_gb": "lots"}, {"quota_gb": 1.5}):
            assert root.put(url, json=bad).status_code == 422, bad
        assert root.put(f"/api/locations/{world['b']['id']}/central/{ci['id']}/data-quota", json={"quota_gb": 150}).status_code == 404
        assert len(fh.cmds) == sent_before

        r = root.put(url, json={"quota_gb": 150})
        assert r.status_code == 200, r.text
        out = r.json()
        assert (out["instance_used_gb"], out["instance_quota_gb"], out["instance_free_gb"]) == (46.0, 150.0, 104.0)
        assert fh.cmds[-1]["op"] == "set_instance_quota" and fh.cmds[-1]["args"] == {"id": ci["id"], "quota_gb": 150}
        r = root.put(url, json={"quota_gb": 40, "force": True})
        assert r.status_code == 200 and fh.cmds[-1]["args"] == {"id": ci["id"], "quota_gb": 40, "force": True}
        audit = db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id == org["id"],
                                                      db.audit_log.c.action.like("central data storage%")).order_by(db.audit_log.c.ts))
        assert [x["action"] for x in audit] == ["central data storage: Yard DQ 50 -> 150 GB",
                                                "central data storage: Yard DQ 150 -> 40 GB (forced below its use)"]
        assert audit[0]["user_email"] == "root@example.com" and audit[0]["detail"]["instance_id"] == ci["id"]

        # the host refuses: 502, nothing stored or audited
        fh.answer = lambda cmd: {"ok": False, "detail": f"{ci['id']} data already uses 46.0 GB; a 20 GB quota would leave its database no room"}
        r = root.put(url, json={"quota_gb": 20})
        assert r.status_code == 502 and "already uses 46.0 GB" in r.text and "nothing was changed" in r.text
        assert hosts.get_instance(ci["id"])["info"]["instance_quota_gb"] == 40.0
        assert len(db.rows(sa.select(db.audit_log).where(db.audit_log.c.action.like("central data storage%")))) == 2
        assert "set_instance_quota" in hosts.OPS
    finally:
        fh.close()
        if ci:
            root.delete(f"/api/locations/{loc['id']}/central/{ci['id']}?force=true")


# ---------------------------------------------------------------- shared inference services (agent 0.4.0+)

def _infer(busy=1.0, p95=40.0, up=100.0, sites=None, ok=True):
    """One service's work.infer entry as the agent sends it: sites = {instance id: (requests, gpu_s)}."""
    return {"ok": ok, "uptime_s": up, "busy_pct": busy, "queues": {"verify": 0, "background": 0, "index": 0},
            "verify_p50_ms": p95 / 2, "verify_p95_ms": p95, "verify_wait_p50_ms": 0.1, "oom": 0, "reserved_mb": 1248, "peak_mb": 1300,
            "instances": {k: {"requests": r, "frames": r * 5, "gpu_s": g} for k, (r, g) in (sites or {}).items()},
            **({} if ok else {"error": "timed out"})}


def _shared_row(hid, org, loc, gpu=1, shared=True):
    ci_id = db.new_id("ci_")
    t0 = time.time()
    db.insert(db.central_instances, {"id": ci_id, "host_id": hid, "location_id": loc["id"], "org_id": org["id"], "server_id": None,
                                     "name": "Central", "mode": "vpn", "subnet": None, "public_ip": None, "site_number": None,
                                     "quota_gb": 100, "gpu": gpu, "state": "running", "last_error": None, "created_at": t0,
                                     "created_by": None, "updated_at": t0, "ready_at": t0,
                                     "info": {"inference": "shared"} if shared else {"gpu_mem_mb": 4096}, "info_at": t0,
                                     "camera_limit": None, "camera_network": None})
    return ci_id


SHARED_GPUS = [{"index": 0, "name": "NVIDIA A40", "mem_total_mb": 49140, "mem_used_mb": 42000, "infer": None},
               {"index": 1, "name": "NVIDIA A10G", "mem_total_mb": 23028, "mem_used_mb": 2000, "infer": "running"}]


def shared_capacity(mode="shared", gpus=SHARED_GPUS):
    return {**capacity(gpus=gpus), "inference": {"mode": mode, "gpus": [g["index"] for g in gpus if g.get("infer")],
                                                "image": "axiom/instance:f575d27"}}


def test_infer_trend_room_and_shares(world):
    """The hub's view of a host's services: GPU-seconds per Site over its history (a service restart starts its
    counters again), the busiest it was, and whether a card's service takes another Site; a shared Site holds no GPU
    share, the card's service is counted once."""
    org = world["org"]
    loc = world["root"].post(f"/api/orgs/{org['id']}/locations", json={"name": "Yard Infer"}).json()
    row, _ = hosts.create_host("Inferhost", None, None)
    hid = row["id"]
    a, b = _shared_row(hid, org, loc), _shared_row(hid, org, loc)
    own = _shared_row(hid, org, loc, shared=False)
    t = 2_000_000.0
    try:
        for sites, busy, up in (({a: (10, 1.0)}, 2.0, 100), ({a: (20, 3.0), b: (5, 0.5)}, 30.0, 130),
                                ({a: (25, 0.4), b: (6, 0.2)}, 4.0, 10)):    # the last: the service restarted
            t += 30
            hosts.record_work(hid, {"at": t, "instances": {}, "infer": {"1": _infer(busy=busy, up=up, sites=sites)}}, t)
        tr = hosts.infer_trend(hid)["1"]
        assert tr["gpu_s"] == {a: 2.0 + 0.4, b: 0.5 + 0.2} and tr["busy_peak"] == 30.0 and tr["busy"] == 4.0 and tr["window_s"] == 60
        assert hosts.work_history(hid)[-1]["infer_busy"] == 4.0
        cap = shared_capacity()
        assert hosts.share_mb(hosts.get_instance(a)) == 0.0 and hosts.share_mb(hosts.get_instance(own)) == 4096
        assert hosts.gpu_reserved(hid, cap) == {1: 4096 + settings.central_infer_service_mb}
        rooms = hosts.infer_room(hid, cap)
        assert [(r["index"], r["service"], r["sites"], r["ok"], r["slots"]) for r in rooms] == [
            (1, "running", 2, True, settings.central_infer_max_sites - 2)]
        assert hosts.pick_shared_gpu(rooms) == 1
        assert hosts.infer_room(hid, capacity()) == []                     # an agent before 0.4.0: no services
        old = settings.central_infer_busy_max
        settings.central_infer_busy_max = 25.0                             # 30 % lately: no new Site there
        try:
            r = hosts.infer_room(hid, cap)[0]
            assert not r["ok"] and r["why"] == "busy 30% lately (limit 25%)" and hosts.pick_shared_gpu([r]) is None
            assert "busy 30% lately" in hosts.infer_room_text([r])
        finally:
            settings.central_infer_busy_max = old
        old = settings.central_infer_max_sites
        settings.central_infer_max_sites = 2
        try:
            assert hosts.infer_room(hid, cap)[0]["why"] == "2 of 2 Sites"
        finally:
            settings.central_infer_max_sites = old
        # a card with no service and too little memory for one takes no shared Site; one with room starts one
        two = [{"index": 0, "name": "NVIDIA A10", "mem_total_mb": 23028, "mem_used_mb": 20000, "infer": None},
               {"index": 2, "name": "NVIDIA A10", "mem_total_mb": 23028, "mem_used_mb": 500, "infer": None}]
        db.run(sa.update(db.central_instances).where(db.central_instances.c.host_id == hid).values(state="deleted"))
        rooms = hosts.infer_room(hid, shared_capacity(gpus=two))
        assert [r["ok"] for r in rooms] == [False, True] and "GB free for a" in rooms[0]["why"]
        assert hosts.pick_shared_gpu(rooms) == 2
    finally:
        hosts._work.pop(hid, None)
        db.run(sa.update(db.central_instances).where(db.central_instances.c.host_id == hid).values(state="deleted"))
        asyncio.run(hosts.delete_host(hid))


def test_inference_slow_alert(world, superuser, monkeypatch):
    """inference_slow: verify p95 over the limit for 5 minutes while requests come in opens it (hub administrators
    only); answering fast again closes it. Not answering for 2 minutes while Sites use it opens it too."""
    monkeypatch.setattr(alerts, "on_open", None)
    org = world["org"]
    loc = world["root"].post(f"/api/orgs/{org['id']}/locations", json={"name": "Yard Slow"}).json()
    row, _ = hosts.create_host("Slowhost", None, None)
    hid = row["id"]
    ci = _shared_row(hid, org, loc)
    opened = lambda: {r["key"]: r for r in _open(hid, hosts.INFER_KIND)}  # noqa: E731
    t, req = 3_000_000.0, 0

    def beat(p95, more=1, ok=True):
        nonlocal t, req
        t += 30
        req += more
        w = _infer(busy=80.0, p95=p95, up=t, sites={ci: (req, req * 0.1)}, ok=ok)
        hosts.record_work(hid, {"at": t, "instances": {}, "infer": {"1": w}}, t)
        hosts.check_infer_alerts(row, t)

    try:
        for _ in range(10):                       # 4.5 minutes slow: not yet
            beat(4000.0)
        assert opened() == {}
        beat(4000.0)
        rows = opened()
        assert list(rows) == ["gpu1"] and rows["gpu1"]["org_id"] == hosts.HUB_ORG
        d = rows["gpu1"]["detail"]
        assert d["down"] is False and d["p95_ms"] == 4000.0 and "taken 4.0 s to verify" in d["text"], d
        assert next(h for h in hosts.list_hosts() if h["id"] == hid)["queue_alerts"][0]["kind"] == hosts.INFER_KIND
        beat(1500.0)                              # better, not yet half the limit: stays open
        assert list(opened()) == ["gpu1"]
        beat(300.0)
        assert opened() == {}
        # slow but idle (the p95 is of old requests): never opens
        for _ in range(15):
            beat(4000.0, more=0)
        assert opened() == {}
        # not answering for 2 minutes while a Site uses it
        for _ in range(4):
            beat(0, ok=False)
        assert opened() == {}
        beat(0, ok=False)
        d = opened()["gpu1"]["detail"]
        assert d["down"] is True and "has not answered for 2 minutes" in d["text"] and "from 1 Site wait" in d["text"]
        beat(50.0)                                # back
        assert opened() == {}
    finally:
        hosts._work.pop(hid, None)
        db.run(sa.update(db.central_instances).where(db.central_instances.c.host_id == hid).values(state="deleted"))
        asyncio.run(hosts.delete_host(hid))


def test_shared_placement_and_switch(world):
    """On a host whose Sites use its AI service: a new Site goes on the card with a service, holds no GPU share and is
    created shared; it can move back to its own models (set_inference) and has no share to change while shared; the
    services restart on a new image from the Hosts page. An older agent can't take a shared Site."""
    base, root, org = world["base"], world["root"], world["org"]
    host = root.post("/api/hub/hosts", json={"name": "Sharedhost"}).json()
    hid = host["host"]["id"]
    old_host = root.post("/api/hub/hosts", json={"name": "Oldagent"}).json()

    def answer(cmd):
        a = cmd["args"]
        if cmd["op"] == "create_instance":
            return {"ok": True, "detail": "created", "instance": {"id": a["id"], "state": "running", "inference": a.get("inference"),
                                                                    "gpu_mem_mb": None if a.get("inference") == "shared" else a.get("gpu_mem_mb")}}
        if cmd["op"] == "set_inference":
            return {"ok": True, "detail": "recreated", "instance": {"id": a["id"], "state": "running", "inference": a["inference"],
                                                                    "gpu_mem_mb": 4096 if a["inference"] == "per_instance" else None}}
        return {"ok": True, "detail": "1 inference service(s) restarted"}
    fh = FakeHost(base, host["token"], shared_capacity(), answer, "sharedhost")
    fo = FakeHost(base, old_host["token"], capacity(), answer, "oldagent")
    made = []
    try:
        wait(lambda: hosts.registry.online(hid) and hosts.registry.online(old_host["host"]["id"]))
        locs = [root.post(f"/api/orgs/{org['id']}/locations", json={"name": f"Shared {i}"}).json() for i in range(3)]
        r = root.post(f"/api/locations/{locs[0]['id']}/central", json={"mode": "vpn", "quota_gb": 100, "host_id": hid})
        assert r.status_code == 200, r.text
        ci = r.json()
        made.append((locs[0], ci))
        args = fh.cmds[-1]["args"]
        assert ci["gpu"] == 1 and args["inference"] == "shared" and args["gpu"] == 1
        wait(lambda: hosts.get_instance(ci["id"])["ready_at"])
        assert hosts.get_instance(ci["id"])["info"]["inference"] == "shared"
        listed = next(h for h in root.get("/api/hub/hosts").json()["hosts"] if h["id"] == hid)
        assert listed["inference"] == "shared" and listed["infer_room"][0]["sites"] == 1
        assert listed["infer_slots"] == settings.central_infer_max_sites - 1
        got = next(c for c in root.get("/api/hub/central").json() if c["id"] == ci["id"])
        assert got["inference"] == "shared" and got["gpu_share_mb"] == 0.0
        # its service's work on the host card, and the Site's GPU time over the hub's history
        for n in (1, 2):
            work = {"at": time.time(), "instances": {}, "infer": {"1": _infer(busy=5.0, up=100 + n, sites={ci["id"]: (n * 10, n * 2.5)})}}
            fh.send({"t": "heartbeat", "capacity": {**shared_capacity(), "work": work}, "instances": []})
        listed = wait(lambda: next((h for h in root.get("/api/hub/hosts").json()["hosts"]
                                    if h["id"] == hid and len(h["work_history"]) >= 2), None))
        panel = listed["infer"][0]
        assert panel["gpu"] == 1 and panel["state"] == "running" and panel["ok"] and panel["verify_p95_ms"] == 40.0 and panel["sites"] == 1
        assert panel["top"] == [{"instance_id": ci["id"], "site": "Shared 0", "gpu_s": 2.5}]
        got = next(c for c in root.get("/api/hub/central").json() if c["id"] == ci["id"])
        assert got["infer_gpu_s"] == 2.5
        # no share to change while shared
        url = f"/api/locations/{locs[0]['id']}/central/{ci['id']}"
        r = root.put(f"{url}/resources", json={"gpu_mem_mb": 6144})
        assert r.status_code == 400 and "shared AI service" in r.text
        # back to its own models, and onto the service again
        r = root.put(f"{url}/inference", json={"inference": "per_instance"})
        assert r.status_code == 200, r.text
        assert fh.cmds[-1]["op"] == "set_inference" and fh.cmds[-1]["args"] == {"id": ci["id"], "inference": "per_instance"}
        assert r.json()["inference"] == "per_instance" and r.json()["gpu_mem_mb"] == 4096
        sent = len(fh.cmds)
        assert root.put(f"{url}/inference", json={"inference": "per_instance"}).status_code == 200 and len(fh.cmds) == sent   # unchanged
        assert root.put(f"{url}/inference", json={"inference": "nope"}).status_code == 422
        act = db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id == org["id"],
                                                    db.audit_log.c.action.like("central recording AI: Shared 0%")))
        assert act and act[-1]["action"].endswith("the shared AI service -> its own models")
        r = root.put(f"{url}/inference", json={"inference": "shared"})
        assert r.status_code == 200 and r.json()["inference"] == "shared"
        # a per_instance Site asked for explicitly on the same host gets a share
        r = root.post(f"/api/locations/{locs[1]['id']}/central",
                      json={"mode": "vpn", "quota_gb": 100, "host_id": hid, "inference": "per_instance"})
        assert r.status_code == 200, r.text
        made.append((locs[1], r.json()))
        assert fh.cmds[-1]["args"]["inference"] == "per_instance" and fh.cmds[-1]["args"]["gpu_mem_mb"] == 4096
        # an agent before 0.4.0 runs no services: a shared Site is refused there
        r = root.post(f"/api/locations/{locs[2]['id']}/central",
                      json={"mode": "vpn", "quota_gb": 100, "host_id": old_host["host"]["id"], "inference": "shared"})
        assert r.status_code == 409 and "older than 0.4.0" in r.text
        # the services restart on a new image (upgrades); a bad image name never reaches the host
        r = root.post(f"/api/hub/hosts/{hid}/restart-infer", json={"image": "axiom/instance:abc1234"})
        assert r.status_code == 200 and fh.cmds[-1]["op"] == "restart_infer" and fh.cmds[-1]["args"] == {"image": "axiom/instance:abc1234"}
        assert root.post(f"/api/hub/hosts/{hid}/restart-infer", json={"image": "bad image; rm"}).status_code == 400
        assert root.post(f"/api/hub/hosts/{old_host['host']['id']}/restart-infer", json={}).status_code == 409
        assert {"set_inference", "restart_infer", "infer_status"} <= set(hosts.OPS)
    finally:
        fh.close()
        fo.close()
        for loc, c in made:
            root.delete(f"/api/locations/{loc['id']}/central/{c['id']}?force=true")
        hosts._work.pop(hid, None)
