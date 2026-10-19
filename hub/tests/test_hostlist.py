"""The Hosts page at fleet scale (hostlist.py): problems per host and instance, GET /api/hub/hosts and /api/hub/central
filtered, sorted and paged, GET /api/hub/hosts/{id}, GET /api/hub/fleet-summary, and that only hub administrators get any
of it. Rows are written straight into the throwaway database; "online" hosts are faked in the agent registry."""
import time
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from hub import auth, db, hostlist, hosts

PW = "hostlist-pass-12345"
NEW, OLD = "sha256:" + "1" * 64, "sha256:" + "2" * 64   # Docker image IDs: what the host creates now, and an older build


def _login(client, email, password):
    c = TestClient(client.app, base_url="http://testserver")   # its own cookie jar
    assert c.post("/auth/login", json={"email": email, "password": password}).status_code == 200
    return c


def _cap(load=8.0, cpus=80, free_gb=8000.0, image_id=NEW):
    return {"cpus": cpus, "load": [load, load, load], "ram_gb": {"total": 768, "free": 600},
            "gpus": [{"index": 1, "name": "NVIDIA A10", "mem_total_mb": 23028, "mem_used_mb": 2000, "util": 10}],
            "disks": [{"path": "/srv/axiom", "total_gb": 10000, "free_gb": free_gb}], "instances": 0,
            "image": "axiom/instance:latest", **({"image_id": image_id} if image_id else {})}


def _host(name, version, cap, online, monkeypatch):
    row, _ = hosts.create_host(name, None, None)
    db.run(sa.update(db.hosts).where(db.hosts.c.id == row["id"]).values(version=version, capacity=cap, online=online, last_seen_at=time.time()))
    if online:
        monkeypatch.setitem(hosts.registry.by_host, row["id"], SimpleNamespace())   # registry.online() is all list_hosts asks
    return row["id"]


def _server(org_id, loc_id, online=True, cams=(), name="Central"):
    sid = db.new_id("s_")
    db.insert(db.sites, {"id": sid, "org_id": org_id, "name": name, "location": "", "token_hash": db.token_hash(sid), "token_prev_hash": None,
                         "token_rotated_at": None, "created_at": time.time(), "last_seen_at": time.time(), "online": online, "version": None,
                         "summary": None, "clock_skew_s": None, "agent_ip": None, "hostname": None, "location_id": loc_id})
    for i, ready in enumerate(cams):
        db.insert(db.cameras, {"server_id": sid, "camera_id": f"cam{i}", "org_id": org_id, "location_id": loc_id, "name": f"Cam {i}",
                               "enabled": True, "stream_ready": ready, "first_seen_at": time.time(), "missing_since": None})
    return sid


def _instance(host_id, org_id, loc_id, *, state="running", mode="vpn", quota=1000, used=100.0, image_id=NEW,
              host_state="running", server=None, created=None):
    ci_id = db.new_id("ci_")
    t = created or time.time()
    db.insert(db.central_instances, {"id": ci_id, "host_id": host_id, "location_id": loc_id, "org_id": org_id, "server_id": server,
                                     "name": "Central", "mode": mode, "subnet": None, "public_ip": None, "site_number": None,
                                     "quota_gb": quota, "gpu": 1, "state": state, "last_error": None, "created_at": t, "created_by": None,
                                     "updated_at": t, "ready_at": t, "info": {"used_gb": used, "state": host_state, "image": "axiom/instance:2096ed5", "image_id": image_id},
                                     "info_at": t, "camera_limit": None, "camera_network": None})
    return ci_id


def _work(host_id, queues: dict):
    cap = db.one(sa.select(db.hosts.c.capacity).where(db.hosts.c.id == host_id))["capacity"]
    cap = {**cap, "work": {"at": time.time(), "instances": {k: {"ok": True, "verify_q": v, "verify_rate_per_min": 2.0} for k, v in queues.items()}}}
    db.run(sa.update(db.hosts).where(db.hosts.c.id == host_id).values(capacity=cap))


@pytest.fixture()
def fleet(client, superuser, monkeypatch):
    """Two hosts (one online with five instances, one offline and behind) in their own customer."""
    root = _login(client, superuser["email"], superuser["password"])
    before = root.get("/api/hub/fleet-summary").json()
    org = root.post("/api/orgs", json={"name": f"HL Co {time.time_ns()}", "slug": f"hl-co-{time.time_ns()}"}).json()
    locs = {n: root.post(f"/api/orgs/{org['id']}/locations", json={"name": f"HL {n}"}).json()["id"]
            for n in ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot")}
    tag = str(time.time_ns())
    big = _host(f"HL big {tag}", "9.0.1", _cap(load=40.0, free_gb=9000), True, monkeypatch)
    old = _host(f"HL old {tag}", "9.0.0", _cap(load=4.0, free_gb=100, image_id=None), False, monkeypatch)
    o = org["id"]
    t0 = time.time() - 1000
    ci = {
        # fine: running, its server online with both cameras streaming
        "ok": _instance(big, o, locs["Alpha"], server=_server(o, locs["Alpha"], cams=(True, True)), created=t0),
        # storage 95 %, one camera of three down
        "full": _instance(big, o, locs["Bravo"], used=950.0, server=_server(o, locs["Bravo"], cams=(True, False, False)), mode="forward", created=t0 + 1),
        # failed on the host
        "failed": _instance(big, o, locs["Charlie"], state="failed", created=t0 + 2),
        # the container exited, and runs an older build than its host creates (a git-rev tag like every instance's)
        "stopped": _instance(big, o, locs["Delta"], host_state="exited", image_id=OLD,
                             server=_server(o, locs["Delta"]), mode="bridge", created=t0 + 3),
        # a backed-up YOLO queue
        "queue": _instance(big, o, locs["Echo"], used=500.0, server=_server(o, locs["Echo"], cams=(True,)), created=t0 + 4),
        # on the offline host: no "down" for it (the host is its own line); another image ID, but its host reports none
        "away": _instance(old, o, locs["Foxtrot"], used=None, image_id=OLD, server=_server(o, locs["Foxtrot"], online=False), created=t0 + 5),
    }
    _work(big, {ci["ok"]: 0, ci["queue"]: hosts.GROW_MIN + 5, ci["full"]: 3})
    return SimpleNamespace(root=root, before=before, org=org, big=big, old=old, ci=ci, tag=tag)


def test_problems_per_instance_and_host(fleet):
    out = fleet.root.get("/api/hub/central", params={"q": "HL ", "limit": 500}).json()
    by_id = {c["id"]: c for c in out["items"] if c["org_id"] == fleet.org["id"]}
    ci = fleet.ci
    assert by_id[ci["ok"]]["problems"] == []
    assert by_id[ci["full"]]["problems"] == ["storage", "cameras"]
    assert by_id[ci["full"]]["cameras_online"] == 1 and by_id[ci["full"]]["camera_count"] == 3
    assert by_id[ci["failed"]]["problems"] == ["down"]
    assert by_id[ci["stopped"]]["problems"] == ["down", "image"]
    assert by_id[ci["queue"]]["problems"] == ["queue"]
    assert by_id[ci["queue"]]["work"]["verify_q"] == hosts.GROW_MIN + 5
    assert by_id[ci["away"]]["problems"] == []
    # the tags differ for every instance (git rev vs latest): only the IDs count
    assert (by_id[ci["ok"]]["image"], by_id[ci["ok"]]["host_image"]) == ("axiom/instance:2096ed5", "axiom/instance:latest")
    assert (by_id[ci["ok"]]["image_id"], by_id[ci["ok"]]["host_image_id"], by_id[ci["ok"]]["host_cpus"]) == (NEW, NEW, 80)
    assert by_id[ci["away"]]["host_image_id"] is None
    hs = {h["id"]: h for h in fleet.root.get("/api/hub/hosts", params={"q": fleet.tag}).json()["hosts"]}
    assert hs[fleet.big]["problems"] == [] and hs[fleet.old]["problems"] == ["offline", "agent"]


def test_central_unpaged_stays_a_list(fleet):
    full = fleet.root.get("/api/hub/central").json()
    assert isinstance(full, list) and set(fleet.ci.values()) <= {c["id"] for c in full}
    assert all("problems" in c for c in full)


def test_central_filters_sort_paging(fleet):
    get = lambda **p: fleet.root.get("/api/hub/central", params={"host": fleet.big, **p})  # noqa: E731
    r = get().json()
    assert r["total"] == 5 and r["limit"] == 50 and r["offset"] == 0
    assert [c["id"] for c in r["items"]] == [fleet.ci[k] for k in ("queue", "stopped", "failed", "full", "ok")]   # newest first
    assert [c["id"] for c in get(q="hl bravo").json()["items"]] == [fleet.ci["full"]]                           # Site name, any case
    assert get(q=fleet.org["name"].upper()).json()["total"] == 5                                               # customer name
    assert fleet.root.get("/api/hub/central", params={"q": f"HL old {fleet.tag}"}).json()["total"] == 1        # host name
    assert [c["id"] for c in get(state="failed").json()["items"]] == [fleet.ci["failed"]]
    assert [c["id"] for c in get(mode="bridge").json()["items"]] == [fleet.ci["stopped"]]
    assert {c["id"] for c in get(problem="down").json()["items"]} == {fleet.ci["failed"], fleet.ci["stopped"]}
    assert get(problem="any").json()["total"] == 4
    assert [c["id"] for c in get(problem="image").json()["items"]] == [fleet.ci["stopped"]]
    # storage: the fullest first, unknown last; asc reverses the known ones only
    s = [c["id"] for c in get(sort="storage").json()["items"]]
    assert s[:2] == [fleet.ci["full"], fleet.ci["queue"]]
    assert [c["id"] for c in get(sort="queue").json()["items"]][:3] == [fleet.ci["queue"], fleet.ci["full"], fleet.ci["ok"]]
    assert [c["id"] for c in get(sort="queue", order="asc").json()["items"]][:3] == [fleet.ci["ok"], fleet.ci["full"], fleet.ci["queue"]]
    assert [c["id"] for c in get(sort="cameras").json()["items"]][:3] == [fleet.ci["full"], fleet.ci["ok"], fleet.ci["queue"]]
    # pages
    p1, p2 = get(limit=2).json(), get(limit=2, offset=4).json()
    assert len(p1["items"]) == 2 and p1["total"] == 5 and [c["id"] for c in p2["items"]] == [fleet.ci["ok"]]
    for bad in ({"limit": 501}, {"limit": 0}, {"offset": -1}, {"sort": "nope"}, {"state": "gone"}, {"problem": "x"}, {"mode": "wifi"}):
        assert get(**bad).status_code == 422, bad


def test_hosts_filters_sort_paging(fleet):
    get = lambda **p: fleet.root.get("/api/hub/hosts", params={"q": fleet.tag, **p}).json()  # noqa: E731
    r = get()
    assert r["total"] == 2 and [h["id"] for h in r["hosts"]] == [fleet.big, fleet.old] and r["install"]   # by name
    assert [h["id"] for h in get(status="offline")["hosts"]] == [fleet.old]
    assert [h["id"] for h in get(status="online")["hosts"]] == [fleet.big]
    assert [h["id"] for h in get(status="agent")["hosts"]] == [fleet.old]
    assert [h["id"] for h in get(status="attention")["hosts"]] == [fleet.old]
    assert [h["id"] for h in get(sort="load")["hosts"]] == [fleet.big, fleet.old]   # 40 / 80 cores before 4 / 80
    assert [h["id"] for h in get(sort="free")["hosts"]] == [fleet.big, fleet.old]
    assert [h["id"] for h in get(sort="free", order="asc")["hosts"]] == [fleet.old, fleet.big]
    page2 = get(limit=1, offset=1)
    assert page2["total"] == 2 and [h["id"] for h in page2["hosts"]] == [fleet.old]
    assert fleet.root.get("/api/hub/hosts", params={"status": "sleeping"}).status_code == 422
    # no parameters: every host, as before
    everyone = fleet.root.get("/api/hub/hosts").json()
    assert everyone["total"] == len(everyone["hosts"]) and {fleet.big, fleet.old} <= {h["id"] for h in everyone["hosts"]}
    one = fleet.root.get(f"/api/hub/hosts/{fleet.big}")
    assert one.status_code == 200 and one.json()["id"] == fleet.big and one.json()["instances"] == 5
    assert fleet.root.get("/api/hub/hosts/h_nope").status_code == 404


def test_fleet_summary_counts(fleet):
    after = fleet.root.get("/api/hub/fleet-summary").json()
    d = lambda part, k: after[part][k] - fleet.before[part][k]  # noqa: E731
    assert (d("hosts", "total"), d("hosts", "online"), d("hosts", "offline")) == (2, 1, 1)
    assert after["newest_agent"] == "9.0.1" and d("hosts", "agent") >= 1
    assert (d("instances", "total"), d("instances", "down"), d("instances", "storage"), d("instances", "queue"),
            d("instances", "image"), d("instances", "cameras"), d("instances", "any")) == (6, 2, 1, 1, 1, 1, 4)
    assert after["cameras_down"] - fleet.before["cameras_down"] == 2
    assert after["images_reported"] is True and not after["healthy"]
    names = [h["name"] for h in after["host_index"]]
    assert f"HL big {fleet.tag}" in names and names == sorted(names, key=hostlist.natural_key)


def test_only_hub_admins(client, fleet):
    org = fleet.org
    r = fleet.root.post(f"/api/orgs/{org['id']}/members", json={"email": f"owner{fleet.tag}@hl.example", "role": "owner", "password": PW})
    assert r.status_code == 200, r.text
    owner = _login(client, f"owner{fleet.tag}@hl.example", PW)
    anon = TestClient(client.app, base_url="http://testserver")
    for path in ("/api/hub/hosts", f"/api/hub/hosts/{fleet.big}", "/api/hub/central", "/api/hub/fleet-summary"):
        for params in ({}, {"limit": 10}):
            assert owner.get(path, params=params).status_code == 403, path
            assert anon.get(path, params=params).status_code == 401, path


def test_heartbeat_keeps_the_image(fleet):
    ci = fleet.ci["ok"]
    row = db.one(sa.select(db.hosts).where(db.hosts.c.id == fleet.big))
    conn = SimpleNamespace(host_id=fleet.big, host=row)
    hosts.registry._on_heartbeat(conn, {"t": "heartbeat", "capacity": _cap(), "instances": [
        {"id": ci, "state": "running", "used_gb": 101.0, "quota_gb": 1000, "image": "axiom/instance:3b7e1c0", "image_id": OLD}]})
    info = db.one(sa.select(db.central_instances.c.info).where(db.central_instances.c.id == ci))["info"]
    assert (info["image"], info["image_id"]) == ("axiom/instance:3b7e1c0", OLD)


def test_pure_helpers():
    assert hostlist.version_key("0.10.1") > hostlist.version_key("0.9.3") > hostlist.version_key("0.9")
    assert hostlist.newest(["0.2.0", None, "0.10.0", "0.3.1"], hostlist.version_key) == "0.10.0"
    assert hostlist.newest([], hostlist.version_key) is None
    names = ["G481 rack 10", "g481 rack 2", "G481 rack 1", "Alpha"]
    assert sorted(names, key=hostlist.natural_key) == ["Alpha", "G481 rack 1", "g481 rack 2", "G481 rack 10"]
    base = {"state": "running", "host_online": True, "server_online": True, "host_state": "running", "quota_gb": 100, "used_gb": 10,
            "camera_count": 2, "cameras_online": 2, "image": "axiom/instance:2096ed5", "host_image": "axiom/instance:latest",
            "image_id": NEW, "host_image_id": NEW}
    assert hostlist.instance_problems(base) == []                                            # tags differ, IDs agree
    assert hostlist.instance_problems({**base, "image_id": OLD}) == ["image"]
    assert hostlist.instance_problems({**base, "image_id": OLD, "host_image_id": None}) == []   # the host doesn't say: unknown
    assert hostlist.instance_problems({**base, "image_id": None}) == []                         # the instance doesn't say
    assert hostlist.instance_problems({**base, "server_online": False}) == ["down"]  # (and no camera count then)
    assert hostlist.instance_problems({**base, "server_online": False, "host_online": False}) == []
    assert hostlist.instance_problems({**base, "used_gb": 90}) == ["storage"]
    assert hostlist.instance_problems({**base, "queue_growing": True}) == ["queue"]
    assert hostlist.instance_problems({**base, "work": {"verify_q": hosts.GROW_MIN}}) == []   # at the floor: not yet
    assert hostlist.instance_problems({**base, "state": "deleting", "used_gb": 100}) == []
    rows = [{"id": "a", "location_name": "b", "used_gb": None, "quota_gb": 10}, {"id": "b", "location_name": "a", "used_gb": 5, "quota_gb": 10},
            {"id": "c", "location_name": "c", "used_gb": 5, "quota_gb": 10}]
    assert [r["id"] for r in hostlist.sort_instances(rows, "storage")] == ["b", "c", "a"]           # ties A-Z, unknown last
    assert [r["id"] for r in hostlist.sort_instances(rows, "storage", "asc")] == ["b", "c", "a"]
    assert hostlist.page(list(range(7)), 3, 6) == ([6], 7) and hostlist.page(list(range(3)), None, 1) == ([1, 2], 3)
