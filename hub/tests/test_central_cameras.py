"""Central recording, allocated on the Hosts page: camera limits and automatic camera addresses (central_cameras.py).

A real hub on a port, a fake axiom-host agent (records set_camera_network), and a fake central instance behind a real
HubAgent tunnel (its camera API: GET / PUT / DELETE /api/cameras). Customers add cameras through the console proxy or
a fleet action; the hub refuses what breaks the camera limit or the address rules, and opens / closes the cameras'
public addresses on the host by itself."""
import asyncio
import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
import uvicorn

from hub import agents, central_cameras, db, fleet_actions, hosts
from hub.api import app
from hub.config import settings
from test_central import FakeHost, _login, capacity, wait
from test_fleet_actions import make_site

PW = "camguard-pass-12345"
CAM_PW = "cam-secret-" + "x" * 6   # a stand-in camera password: must never reach a log or an audit row


@pytest.fixture(scope="module")
def hub():
    """A real hub on a port, and its event loop (hub-side coroutines run there: the tunnels and host sockets live in it)."""
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
    """Run coro_fn() in the hub's loop and wait for its result."""
    async def run():
        return await coro_fn()
    return asyncio.run_coroutine_threadsafe(run(), w.loop).result(30)


def _set_cmds(fh, ci_id):
    return [c["args"] for c in fh.cmds if c["op"] == "set_camera_network" and c["args"]["id"] == ci_id]


@pytest.fixture(scope="module")
def world(hub, superuser):
    from nvr import hub_agent
    from nvr.db import db as site_db
    base = hub.base
    old_delay = central_cameras.SYNC_DELAY_S
    central_cameras.SYNC_DELAY_S = 0.05
    root = _login(base, superuser["email"], superuser["password"])
    org = root.post("/api/orgs", json={"name": "Camguard Co", "slug": "camguard-co"}).json()
    locs = {n: root.post(f"/api/orgs/{org['id']}/locations", json={"name": f"Lot {n}"}).json() for n in "ABCD"}
    for email, role, extra in (("owner@camguard.example", "owner", {}), ("siteadmin@camguard.example", "admin", {"all_sites": False, "location_ids": [locs["A"]["id"]]}),
                               ("viewer@camguard.example", "viewer", {})):
        r = root.post(f"/api/orgs/{org['id']}/members", json={"email": email, "role": role, "password": PW, **extra})
        assert r.status_code == 200, r.text
    host = root.post("/api/hub/hosts", json={"name": "Guard host"}).json()

    def answer(cmd):
        if cmd["op"] == "create_instance":
            return {"ok": True, "detail": "created", "instance": {"id": cmd["args"]["id"], "state": "running"}}
        if cmd["op"] == "set_camera_network":
            return {"ok": True, "detail": "set", "instance": {"id": cmd["args"]["id"], "host_ips": {h: ["93.184.216.99"] for h in cmd["args"]["hosts"]}}}
        return {"ok": True, "detail": "done"}
    fh = FakeHost(base, host["token"], capacity(), answer, "guard")
    wait(lambda: hosts.registry.online(host["host"]["id"]))

    # Lot A: VPN instance with a camera limit of 2, allocated on the host (the Hosts page's Allocate form)
    r = root.post(f"/api/locations/{locs['A']['id']}/central", json={"mode": "vpn", "quota_gb": 500, "host_id": host["host"]["id"],
                                                                     "camera_limit": 2, "name": "Lot A Central"})
    assert r.status_code == 200, r.text
    ci = r.json()
    # Lot B: a port-forward instance (never enrolled) whose public address is taken
    ci_b = root.post(f"/api/locations/{locs['B']['id']}/central", json={"mode": "forward", "public_ip": "93.184.216.50", "quota_gb": 100,
                                                                        "host_id": host["host"]["id"]}).json()
    wait(lambda: hosts.get_instance(ci["id"])["ready_at"] and hosts.get_instance(ci_b["id"])["ready_at"])

    # the instance's server: a HubAgent tunnel in front of a fake server API (as if it had enrolled)
    site_app = make_site("Lot A Central", [])
    token = db.new_token()
    sid = db.new_id("s_")
    db.insert(db.sites, {"id": sid, "org_id": org["id"], "name": "Lot A Central", "location": "", "token_hash": db.token_hash(token),
                         "token_prev_hash": None, "token_rotated_at": None, "created_at": time.time(), "last_seen_at": None, "online": False,
                         "version": None, "summary": None, "clock_skew_s": None, "agent_ip": None, "hostname": None, "location_id": locs["A"]["id"]})
    db.run(sa.update(db.central_instances).where(db.central_instances.c.id == ci["id"]).values(server_id=sid, state="running"))
    site_db.set_setting("hub_url", base.replace("http://", "ws://") + "/agent")

    class Agent(hub_agent.HubAgent):
        def _auth_header(self):
            return f"Bearer {token}"

    async def summary(st, since):
        cams = site_app.st["cameras"].values()
        return {"cameras": [{"id": c["id"], "name": c["name"]} for c in cams if c.get("enabled", 1)],
                "disabled": [{"id": c["id"], "name": c["name"]} for c in cams if not c.get("enabled", 1)], "today": {}}
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    agent = Agent(site_app, SimpleNamespace(pipeline=SimpleNamespace(subscribers=set()), ingests={}, health=None), summary_fn=summary)
    fut = asyncio.run_coroutine_threadsafe(agent.run(), loop)
    wait(lambda: sid in agents.registry.by_site)
    users = {k: _login(base, f"{k}@camguard.example", PW) for k in ("owner", "siteadmin", "viewer")}
    yield SimpleNamespace(base=base, loop=hub.loop, root=root, org=org, locs=locs, host=host, fh=fh, ci=hosts.get_instance(ci["id"]), ci_b=ci_b, sid=sid,
                          site=site_app, users=users)
    fut.cancel()
    time.sleep(0.3)
    loop.call_soon_threadsafe(loop.stop)
    fh.close()
    central_cameras.SYNC_DELAY_S = old_delay


def _cam(cid, host, **kw):
    return {"id": cid, "name": kw.pop("name", cid.title()), "host": host, "username": "admin", "password": CAM_PW, "enabled": True, **kw}


def _put(client, w, cid, host, **kw):
    return client.put(f"/s/{w.sid}/api/cameras/{cid}", json=_cam(cid, host, **kw))


def _subnet(w):
    return hosts.camera_network(hosts.get_instance(w.ci["id"]))["subnets"][0]


def _ip(w, last):
    return _subnet(w).rsplit(".", 1)[0] + f".{last}"


def _limit(w, n):
    r = w.root.patch(f"/api/locations/{w.locs['A']['id']}/central/{w.ci['id']}", json={"camera_limit": n})
    assert r.status_code == 200, r.text
    return r.json()


def _forget_cameras(w):
    """Back to an instance without cameras (each test starts clean)."""
    w.site.st["cameras"].clear()
    central_cameras._pending.pop(w.sid, None)
    db.run(sa.delete(db.cameras).where(db.cameras.c.server_id == w.sid))


def test_rules_unit():
    assert central_cameras.write_kind("PUT", "/api/cameras/cam1") == "camera"
    assert central_cameras.write_kind("POST", "/api/cameras") == "camera"
    assert central_cameras.write_kind("POST", "/api/config/import") == "import" and central_cameras.write_kind("POST", "/api/config/merge") == "merge"
    for m, p in (("PUT", "/api/cameras/cam1/ptz/config"), ("POST", "/api/cameras/scan"), ("POST", "/api/cameras/cam1/relay"), ("GET", "/api/cameras")):
        assert central_cameras.write_kind(m, p) is None, (m, p)
    assert central_cameras.changes_cameras("DELETE", "/api/cameras/cam1") and not central_cameras.changes_cameras("DELETE", "/api/locks/3")
    assert central_cameras.classify("192.168.7.20") == ("private", "192.168.7.20")
    assert central_cameras.classify("100.64.1.2")[0] == "private"
    assert central_cameras.classify("93.184.216.34") == ("public_ip", "93.184.216.34")
    assert central_cameras.classify("Yard.DynDNS.example.NET.") == ("host", "yard.dyndns.example.net")
    for bad in ("127.0.0.1", "169.254.1.1", "224.0.0.1", "0.0.0.0", "::1", "camera1", "vllm", "x.localhost", "bad_name.example.net"):
        with pytest.raises(central_cameras.Refused) as e:
            central_cameras.classify(bad)
        assert e.value.status == 400, bad


def test_allocate_with_camera_limit_and_admin_only(world):
    w, root = world, world.root
    loc_a = w.locs["A"]["id"]
    got = root.get(f"/api/locations/{loc_a}/central").json()
    inst = got["instances"][0]
    assert got["can_manage"] is True and got["can_provision"] is False
    assert inst["camera_limit"] == 2 and inst["host_id"] == w.host["host"]["id"]
    audit = db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id == w.org["id"], db.audit_log.c.action.like("central recording provisioning%")))
    assert any(a["detail"].get("camera_limit") == 2 and a["detail"]["instance_id"] == w.ci["id"] for a in audit)
    # the camera limit: 1..500 or none
    for bad in (0, 501, "x"):
        assert root.post(f"/api/locations/{w.locs['C']['id']}/central", json={"quota_gb": 50, "camera_limit": bad}).status_code == 422
    url = f"/api/locations/{loc_a}/central/{w.ci['id']}"
    assert root.patch(url, json={}).status_code == 400
    assert root.patch(url, json={"camera_limit": 0}).status_code == 422
    assert _limit(w, 5)["camera_limit"] == 5
    assert _limit(w, None)["camera_limit"] is None
    assert _limit(w, 2)["camera_limit"] == 2
    rows = db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id == w.org["id"], db.audit_log.c.action.like("central recording camera limit%")))
    assert [r["action"].rsplit(": ", 1)[1] for r in rows][-3:] == ["Lot A 2 -> 5", "Lot A 5 -> no limit", "Lot A no limit -> 2"]
    # the Site page reads, never writes: every central write is for hub administrators only
    writes = [("post", f"/api/locations/{w.locs['C']['id']}/central", {"quota_gb": 50, "host_id": w.host["host"]["id"]}),
              ("patch", url, {"camera_limit": 9}), ("patch", url, {"quota_gb": 900}),
              ("put", url + "/cameras", {"subnets": ["192.168.50.0/24"]}), ("delete", url, None)]
    for who in ("owner", "siteadmin", "viewer"):
        c = w.users[who]
        for method, path, body in writes:
            r = c.request(method.upper(), path, json=body) if body is not None else c.request(method.upper(), path)
            assert r.status_code == 403, (who, method, path, r.status_code)
    assert w.users["siteadmin"].get(f"/api/locations/{loc_a}/central").json()["can_manage"] is False
    assert hosts.get_instance(w.ci["id"])["camera_limit"] == 2 and hosts.get_instance(w.ci["id"])["state"] == "running"


def test_camera_limit_through_the_proxy(world, caplog):
    caplog.set_level(logging.DEBUG)
    w = world
    _forget_cameras(w)
    _limit(w, 2)
    owner = w.users["owner"]   # a customer admin adds cameras like on any server
    assert _put(owner, w, "cam1", _ip(w, 11)).status_code == 200
    assert _put(owner, w, "cam2", _ip(w, 12)).status_code == 200
    r = _put(owner, w, "cam3", _ip(w, 13))
    assert r.status_code == 409 and r.json()["detail"] == "This Site's central recording allows 2 cameras; ask Axiom Vision to raise it."
    assert "cam3" not in w.site.st["cameras"]
    r = _put(w.root, w, "cam3", _ip(w, 13))   # hub administrators too: raise the limit instead
    assert r.status_code == 409
    # editing a camera it has is fine; the registry follows the instance's own list
    assert _put(owner, w, "cam1", _ip(w, 21), name="Gate").status_code == 200
    wait(lambda: {c["camera_id"] for c in db.rows(sa.select(db.cameras).where(db.cameras.c.server_id == w.sid, db.cameras.c.missing_since.is_(None)))} == {"cam1", "cam2"})
    assert w.root.get(f"/api/locations/{w.locs['A']['id']}/central").json()["instances"][0]["camera_count"] == 2
    # a camera switched off frees its place; switching it on again counts as new
    assert owner.delete(f"/s/{w.sid}/api/cameras/cam2").status_code == 200
    wait(lambda: not db.one(sa.select(db.cameras).where(db.cameras.c.server_id == w.sid, db.cameras.c.camera_id == "cam2"))["enabled"])
    assert _put(owner, w, "cam3", _ip(w, 13)).status_code == 200
    assert _put(owner, w, "cam2", _ip(w, 12)).status_code == 409
    # lowering the limit below the count keeps the cameras; new ones are refused
    _limit(w, 1)
    assert _put(owner, w, "cam1", _ip(w, 22)).status_code == 200
    assert _put(owner, w, "cam4", _ip(w, 14)).status_code == 409
    # a backup restore through the console counts its cameras too
    r = owner.post(f"/s/{w.sid}/api/config/import", json={"data": {"format": 1, "cameras": [_cam("cam8", _ip(w, 18)), _cam("cam9", _ip(w, 19))]}})
    assert r.status_code == 409
    _limit(w, None)
    # the camera password went to the instance and nowhere else
    assert w.site.st["cameras"]["cam1"]["password"] == CAM_PW
    assert CAM_PW not in caplog.text
    for row in db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id == w.org["id"])):
        assert CAM_PW not in json.dumps(row, default=str)


def test_private_addresses_stay_in_the_site_network(world):
    w = world
    _forget_cameras(w)
    _limit(w, None)
    owner = w.users["owner"]
    sub = _subnet(w)
    r = _put(owner, w, "cam1", "192.168.7.20")
    assert r.status_code == 400 and r.json()["detail"] == f"192.168.7.20 is outside this Site's camera network {sub}"
    assert _put(owner, w, "cam1", "10.200.0.5").status_code == 400          # the host's instance pool
    assert _put(owner, w, "cam1", "127.0.0.1").status_code == 400
    assert _put(owner, w, "cam1", "camera1").status_code == 400              # a bare name: the container's own DNS
    assert _put(owner, w, "cam1", _ip(w, 30)).status_code == 200
    assert not w.site.st["cameras"].get("cam1", {}).get("public_host")
    # a private outside address behind public_host is fine: the instance connects to the router
    assert _put(owner, w, "cam2", "192.168.7.20", public_host="93.184.216.71").status_code == 200
    # a hub administrator adds the Site's LAN as a Site network: then its cameras are allowed
    url = f"/api/locations/{w.locs['A']['id']}/central/{w.ci['id']}/cameras"
    assert w.root.put(url, json={"subnets": [sub, "192.168.7.0/24"]}).status_code == 200
    assert _put(owner, w, "cam3", "192.168.7.20").status_code == 200
    assert w.root.put(url, json={"subnets": [sub]}).status_code == 200


def test_public_addresses_open_and_close_by_themselves(world):
    w = world
    _forget_cameras(w)
    w.root.put(f"/api/locations/{w.locs['A']['id']}/central/{w.ci['id']}/cameras", json={"subnets": [_subnet(w)]})
    owner = w.users["owner"]
    sub = _subnet(w)
    before = len(_set_cmds(w.fh, w.ci["id"]))
    assert _put(owner, w, "cam1", "192.168.1.10", public_host="93.184.216.77", public_rtsp_port=5541).status_code == 200
    args = wait(lambda: next((a for a in _set_cmds(w.fh, w.ci["id"])[before:] if "93.184.216.77" in a["public_ips"]), None))
    assert args == {"id": w.ci["id"], "subnets": [sub], "public_ips": ["93.184.216.77"], "hosts": []}
    inst = w.root.get(f"/api/locations/{w.locs['A']['id']}/central").json()["instances"][0]
    assert inst["camera_network"]["auto"] == {"public_ips": ["93.184.216.77"], "hosts": []} and inst["camera_network"]["public_ips"] == []
    assert "93.184.216.77" in inst["peplink"]["forward_addresses"]
    audit = db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id == w.org["id"], db.audit_log.c.user_email == "(automatic: camera addresses)"))
    assert audit and audit[-1]["action"] == "central recording camera addresses: Lot A" and audit[-1]["detail"]["to"]["public_ips"] == ["93.184.216.77"]
    # a second camera behind the same router and a dynamic DNS name: one more firewall change (for the name)
    n = len(_set_cmds(w.fh, w.ci["id"]))
    assert _put(owner, w, "cam2", "192.168.1.11", public_host="93.184.216.77").status_code == 200
    assert _put(owner, w, "cam3", "192.168.1.12", public_host="Yard.Dyn.Example.net").status_code == 200
    wait(lambda: any("yard.dyn.example.net" in a["hosts"] for a in _set_cmds(w.fh, w.ci["id"])[n:]))
    # an update that leaves public_host out keeps the stored one (the instance still connects to the router)
    assert w.users["owner"].put(f"/s/{w.sid}/api/cameras/cam2", json={k: v for k, v in _cam("cam2", "192.168.1.11").items()}).status_code == 200
    # the last camera at an address goes: it closes again
    assert owner.delete(f"/s/{w.sid}/api/cameras/cam3").status_code == 200
    wait(lambda: (lambda a: a and "yard.dyn.example.net" not in a[-1]["hosts"])(_set_cmds(w.fh, w.ci["id"])))
    assert owner.delete(f"/s/{w.sid}/api/cameras/cam1").status_code == 200
    time.sleep(0.5)
    assert "93.184.216.77" in _set_cmds(w.fh, w.ci["id"])[-1]["public_ips"]   # cam2 still uses it
    assert owner.delete(f"/s/{w.sid}/api/cameras/cam2").status_code == 200
    wait(lambda: _set_cmds(w.fh, w.ci["id"])[-1] == {"id": w.ci["id"], "subnets": [sub], "public_ips": [], "hosts": []})
    # nothing changes, nothing is sent
    n = len(_set_cmds(w.fh, w.ci["id"]))
    assert in_hub(w, lambda: central_cameras.sync(w.ci["id"])) == "unchanged"
    assert len(_set_cmds(w.fh, w.ci["id"])) == n


def test_addresses_of_others_are_refused(world, monkeypatch):
    w = world
    _forget_cameras(w)
    owner = w.users["owner"]
    r = _put(owner, w, "cam1", "192.168.1.10", public_host="93.184.216.50")   # Lot B's router
    assert r.status_code == 409 and "another Site" in r.json()["detail"]
    monkeypatch.setattr(settings, "datacenter_ip", "198.51.100.200")
    r = _put(owner, w, "cam1", "192.168.1.10", public_host="198.51.100.200")
    assert r.status_code == 409 and "Axiom Vision's own systems" in r.json()["detail"]
    db.run(sa.update(db.hosts).where(db.hosts.c.id == w.host["host"]["id"]).values(agent_ip="93.184.216.60"))
    assert _put(owner, w, "cam1", "93.184.216.60").status_code == 409
    # the hub's own name
    monkeypatch.setattr(settings, "public_url", "https://hub.example.net")
    assert _put(owner, w, "cam1", "10.1.1.1", public_host="hub.example.net").status_code == 409
    # another Site's subnet: private, so it must be in Lot A's own networks, and isn't
    assert w.root.put(f"/api/locations/{w.locs['B']['id']}/central/{w.ci_b['id']}/cameras",
                      json={"subnets": ["10.99.0.0/24"], "public_ips": ["93.184.216.50"]}).status_code == 200
    r = _put(owner, w, "cam1", "10.99.0.5")
    assert r.status_code == 400 and "outside this Site's camera network" in r.json()["detail"]
    # an administrator cannot hand Lot A a subnet or router another Site has
    url = f"/api/locations/{w.locs['A']['id']}/central/{w.ci['id']}/cameras"
    assert w.root.put(url, json={"subnets": [_subnet(w), "10.99.0.0/25"]}).status_code == 409
    assert w.root.put(url, json={"subnets": [_subnet(w)], "public_ips": ["93.184.216.50"]}).status_code == 409
    assert not w.site.st["cameras"]


def test_fleet_add_camera_keeps_the_rules(world, monkeypatch):
    w = world
    _forget_cameras(w)
    monkeypatch.setattr(fleet_actions.vlm_proxy, "configured", lambda: False)
    monkeypatch.setattr(fleet_actions, "STREAM_CHECK_S", 0.5)
    monkeypatch.setattr(fleet_actions, "STREAM_POLL_S", 0.05)
    _limit(w, 1)

    def add(host, name):
        p = w.root.post(f"/api/orgs/{w.org['id']}/actions/plan", json={"text": f"Add {host} to Lot A Central as {name}", "origin": "actions_page"}).json()
        assert p["action"] == "add_camera", p
        return w.root.post(f"/api/orgs/{w.org['id']}/actions/execute", json={"plan_id": p["id"], "camera": {"password": CAM_PW}}).json()
    out = add(_ip(w, 41), "Gate")
    assert out["ok"], out
    out = add(_ip(w, 42), "Dock")
    assert not out["ok"] and "allows 1 camera; ask Axiom Vision to raise it" in out["lines"][-1], out
    _limit(w, None)
    out = add("192.168.9.9", "Shed")
    assert not out["ok"] and "outside this Site's camera network" in out["lines"][-1], out
    assert sorted(c["host"] for c in w.site.st["cameras"].values()) == [_ip(w, 41)]


def test_moving_a_central_server(world):
    w = world
    url = f"/api/servers/{w.sid}"
    r = w.users["owner"].patch(url, json={"location_id": w.locs["C"]["id"]})
    assert r.status_code == 403 and "hub administrators" in r.json()["detail"]
    r = w.root.patch(url, json={"location_id": w.locs["B"]["id"]})   # Lot B has its own instance
    assert r.status_code == 409 and "one instance per Site" in r.json()["detail"]
    assert hosts.get_instance(w.ci["id"])["location_id"] == w.locs["A"]["id"]
    # a hub administrator may move it to a Site without one; the instance follows
    assert w.root.patch(url, json={"location_id": w.locs["D"]["id"]}).status_code == 200
    assert hosts.get_instance(w.ci["id"])["location_id"] == w.locs["D"]["id"]
    assert w.root.patch(url, json={"location_id": w.locs["A"]["id"]}).status_code == 200
    assert hosts.get_instance(w.ci["id"])["location_id"] == w.locs["A"]["id"]
    # renaming is still the customer's business
    assert w.users["owner"].patch(url, json={"name": "Lot A Central"}).status_code == 200
    # retiring or removing it is not: removing only the server row would orphan the instance on its host
    r = w.users["owner"].post(f"{url}/retire", json={"retired": True})
    assert r.status_code == 403 and "Axiom Vision" in r.json()["detail"]
    for who in (w.users["owner"], w.root):
        r = who.delete(url)
        assert r.status_code == 409 and "Hosts page" in r.json()["detail"]
    assert hosts.get_instance(w.ci["id"])["server_id"] == w.sid


def test_legacy_row_waits_for_its_site_networks(world):
    """An instance from before automatic addresses (its host may allow more than the hub stored): automatic syncs
    never push until a hub administrator sets its Site networks (here the command line's way: stored, then the
    hub's sweep sends it)."""
    w = world
    _forget_cameras(w)
    ci_id = w.ci["id"]
    db.run(sa.update(db.central_instances).where(db.central_instances.c.id == ci_id).values(camera_network={"subnets": [], "public_ips": ["93.184.216.80"], "hosts": []}))
    n = len(_set_cmds(w.fh, ci_id))
    # a camera behind a router: allowed (public address), stored as automatic, but not sent to the host
    assert _put(w.users["owner"], w, "cam1", "192.168.105.11", public_host="93.184.216.81").status_code == 200
    wait(lambda: hosts.auto_addresses(hosts.get_instance(ci_id))["public_ips"] == ["93.184.216.81"])
    assert in_hub(w, lambda: central_cameras.sync(ci_id)) == "legacy"
    assert len(_set_cmds(w.fh, ci_id)) == n
    row = hosts.get_instance(ci_id)
    assert hosts.auto_addresses(row) == {"public_ips": ["93.184.216.81"], "hosts": []} and hosts.applied_state(row)[0] == "legacy"
    assert w.root.get(f"/api/locations/{w.locs['A']['id']}/central").json()["instances"][0]["camera_network"]["auto_on"] is False
    hosts.store_site_networks(ci_id, ["192.168.105.0/24"], [], [], {"id": None, "email": "(command line)"})
    assert hosts.applied_state(hosts.get_instance(ci_id))[0] == "pending"

    async def sweep():
        central_cameras.sweep()
    in_hub(w, sweep)   # (the hub's sweeper does this every 30 s)
    wait(lambda: hosts.applied_state(hosts.get_instance(ci_id))[0] == "ok")
    assert _set_cmds(w.fh, ci_id)[-1] == {"id": ci_id, "subnets": ["192.168.105.0/24"], "public_ips": ["93.184.216.81"], "hosts": []}
    # restore the Site network the other tests use
    sub = f"10.20.{hosts.get_instance(ci_id)['site_number']}.0/24"
    assert w.root.put(f"/api/locations/{w.locs['A']['id']}/central/{ci_id}/cameras", json={"subnets": [sub]}).status_code == 200
    _forget_cameras(w)


def test_heartbeat_change_triggers_one_sync(monkeypatch):
    calls: list[str] = []

    async def fake_sync(ci_id):
        calls.append(ci_id)
        return "unchanged"
    monkeypatch.setattr(central_cameras, "sync", fake_sync)
    monkeypatch.setattr(central_cameras, "SYNC_DELAY_S", 0.05)
    monkeypatch.setattr(central_cameras, "instance_for_server", lambda sid: {"id": "ci_hb"} if sid == "s_central" else None)
    central_cameras._seen.clear()

    async def go():
        central_cameras.on_heartbeat("s_central", {"cameras": [{"id": "cam1"}]})
        central_cameras.on_heartbeat("s_central", {"cameras": [{"id": "cam1"}]})   # same list: debounced into the first
        central_cameras.on_heartbeat("s_other", {"cameras": [{"id": "cam1"}]})     # not a central instance
        await asyncio.sleep(0.3)
        central_cameras.on_heartbeat("s_central", {"cameras": [{"id": "cam1"}]})   # unchanged: nothing
        await asyncio.sleep(0.2)
        central_cameras.on_heartbeat("s_central", {"cameras": [{"id": "cam1"}], "disabled": [{"id": "cam2"}]})
        await asyncio.sleep(0.3)
    asyncio.run(go())
    assert calls == ["ci_hb", "ci_hb"]
