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
    h_small = FakeHost(base, small["token"], capacity(gpus=[{"index": 0, "name": "NVIDIA A10", "mem_total_mb": 23028, "mem_used_mb": 20000}]), answer, "small")
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
                        "vlm_url": "http://vllm:8000/v1"}
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
