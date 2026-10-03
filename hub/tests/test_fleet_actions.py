"""Fleet actions through real tunnels: three fake sites (Ironsight, Hailo T1, Qwenbot) behind HubAgents, the Ask
text -> plan -> confirmation card -> execute path, with the shared AI mocked and the rule parser as fallback.
Camera passwords must reach the destination and appear nowhere else (audit rows, logs, plan responses)."""
import asyncio
import copy
import json
import logging
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
import sqlalchemy as sa
import uvicorn

from hub import agents, db, fleet_actions
from hub.api import app

PW_FRONT = "pw-front-" + "q" * 6     # stand-ins, never real credentials
PW_LOT = "pw-lot-" + "z" * 6


def cam(cid, name, host, br=None, enabled=1, password="", zones=None, policies=None):
    return {"id": cid, "name": name, "host": host, "onvif_port": 80, "rtsp_port": 554, "username": "admin", "password": password,
            "main_path": "/main", "sub_path": "/sub", "enabled": enabled, "zones": zones or [], "retention_days": None, "scene_notes": "",
            "retention_policy": None, "synopsis_labels": None, "policies": policies or [], "ptz_config": None, "_br": br}


def make_site(name, cameras, yolo_device="hailo", days=14):
    st = {"cameras": {c["id"]: c for c in cameras}, "days": days, "moved_to": {}, "puts": [], "merged": [], "handoff_calls": [],
          "yolo_device": yolo_device}

    async def body(receive):
        b = b""
        while True:
            m = await receive()
            b += m.get("body", b"")
            if not m.get("more_body"):
                return json.loads(b or b"null")

    async def app_(scope, receive, send):
        path, method = scope["path"], scope["method"]
        q = parse_qs(scope["query_string"].decode())
        headers = {k.decode(): v.decode() for k, v in scope["headers"]}

        async def reply(status, obj):
            await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": json.dumps(obj).encode()})

        def public(c):
            return {**{k: v for k, v in c.items() if k not in ("password", "_br")}, "status": {"health": {"bitrate_mbps": c["_br"]}}}

        if path == "/api/cameras" and method == "GET":
            await reply(200, [public(c) for c in st["cameras"].values()])
        elif path == "/api/config/handoff":
            st["handoff_calls"].append(scope["client"])
            if scope["client"] != ("hub", 0) or headers.get("x-hub-internal") != "handoff":
                await reply(403, {"detail": "camera credentials are only handed to the fleet hub"})
                return
            ids = (q.get("cameras") or [""])[0].split(",")
            cams = [{k: v for k, v in c.items() if k != "_br"} for c in st["cameras"].values() if c["id"] in ids]
            await reply(200, {"format": 1, "partial": True, "cameras": cams, "camera_links": [], "identities": [{"name": "Sam", "kind": "person", "looks": []}]})
        elif path == "/api/config/export":
            await reply(200, {"format": 1, "cameras": [{k: v for k, v in c.items() if k not in ("password", "_br")} for c in st["cameras"].values()]})
        elif path == "/api/config/merge" and method == "POST":
            data = (await body(receive))["data"]
            ids = {}
            for c in data["cameras"]:
                new = c["id"] if c["id"] not in st["cameras"] else c["id"] + "_2"
                ids[c["id"]] = new
                st["cameras"][new] = {**copy.deepcopy(c), "id": new, "_br": 4.0}
            st["merged"].append(data)
            await reply(200, {"cameras": len(ids), "updated": [], "camera_links": 0, "identities": 1, "ids": ids})
        elif path.startswith("/api/cameras/") and method == "DELETE":
            cid = path.rsplit("/", 1)[1]
            st["cameras"][cid]["enabled"] = 0
            st["moved_to"][cid] = (q.get("moved_to") or [None])[0]
            await reply(200, {"ok": True})
        elif path.startswith("/api/cameras/") and method == "PUT":
            cid = path.rsplit("/", 1)[1]
            b = await body(receive)
            st["puts"].append(b)
            st["cameras"][cid].update({k: v for k, v in b.items() if k != "password"})
            await reply(200, public(st["cameras"][cid]))
        elif path == "/api/retention/policy":
            if method == "PUT":
                st["days"] = (await body(receive))["continuous_days"]
            await reply(200, {"policy": {"continuous_days": st["days"]}})
        elif path == "/api/system":
            await reply(200, {"yolo_device": st["yolo_device"]})
        elif path == "/api/topology":
            await reply(200, [])
        elif path == "/api/assistant/ask":
            await body(receive)
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/x-ndjson")]})
            await send({"type": "http.response.body", "body": json.dumps({"type": "delta", "text": f"{name} answers"}).encode() + b"\n"})
        else:
            await reply(404, {"detail": "nope"})

    app_.st = st
    return app_


@pytest.fixture(scope="module")
def fleet(superuser):
    from nvr import hub_agent

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    owner = httpx.Client(base_url=base, timeout=60)
    assert owner.post("/auth/login", json={"email": superuser["email"], "password": superuser["password"]}).status_code == 200
    org = owner.post("/api/orgs", json={"name": "Actions Co", "slug": "actions-co"}).json()
    assert owner.post(f"/api/orgs/{org['id']}/members", json={"email": "viewer@actions.example", "role": "viewer", "password": "viewer password 1"}).status_code == 200
    viewer = httpx.Client(base_url=base, timeout=30)
    assert viewer.post("/auth/login", json={"email": "viewer@actions.example", "password": "viewer password 1"}).status_code == 200

    front_zones = [{"name": "Door", "type": "area", "points": []}, {"name": "Walk", "type": "include", "points": []}]
    apps = {
        "Ironsight": make_site("Ironsight", [cam("cam1", "Front Door Inside sys 2", "192.168.105.19", 4.1, password=PW_FRONT, zones=front_zones,
                                                 policies=[{"kind": "entry", "area": "Door", "allowed": ["Sam"], "priority": "high"}]),
                                             cam("cam2", "Back Lot", "192.168.105.20", 3.0, password=PW_LOT),
                                             cam("cam9", "Old porch", "192.168.105.21", None, enabled=0, password="unused-pw")], "cpu"),
        "Hailo T1": make_site("Hailo T1", [cam("cam1", "Gate", "10.2.0.1", 15), cam("cam2", "Dock", "10.2.0.2", 15),
                                           cam("cam3", "Side", "10.2.0.3", 15), cam("cam4", "Roof", "10.2.0.4", 15)], "cpu"),
        "Qwenbot": make_site("Qwenbot", [cam("cam1", "Yard", "10.3.0.1", 2.0)], "cpu", days=30),
    }
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    sites, futs = {}, []
    base_ws = base.replace("http://", "ws://") + "/agent"
    from nvr.db import db as site_db
    site_db.set_setting("hub_url", base_ws)
    for name, site_app in apps.items():
        token = db.new_token()
        sid = db.new_id("s_")
        db.insert(db.sites, {"id": sid, "org_id": org["id"], "name": name, "location": "", "token_hash": db.token_hash(token), "token_prev_hash": None,
                             "token_rotated_at": None, "created_at": time.time(), "last_seen_at": None, "online": False, "version": None,
                             "summary": None, "clock_skew_s": None, "agent_ip": None, "hostname": None})

        class Agent(hub_agent.HubAgent):
            def _auth_header(self, token=token):   # one process, several sites: each its own token
                return f"Bearer {token}"

        async def summary(st, since):
            return {"cameras": [], "today": {}}

        agent = Agent(site_app, SimpleNamespace(pipeline=SimpleNamespace(subscribers=set()), ingests={}, health=None), summary_fn=summary)
        futs.append(asyncio.run_coroutine_threadsafe(agent.run(), loop))
        sites[name] = sid
    for _ in range(200):
        if all(s in agents.registry.by_site for s in sites.values()):
            break
        time.sleep(0.05)
    assert all(s in agents.registry.by_site for s in sites.values())
    yield SimpleNamespace(base=base, owner=owner, viewer=viewer, org=org, sites=sites, apps=apps)
    for f in futs:
        f.cancel()
    time.sleep(0.3)
    loop.call_soon_threadsafe(loop.stop)
    server.should_exit = True


@pytest.fixture(autouse=True)
def rules_only(monkeypatch):
    """Default: no shared AI (the rule parser); tests that want the AI patch it in."""
    monkeypatch.setattr(fleet_actions.vlm_proxy, "configured", lambda: False)
    fleet_actions._cameras_cache.clear()


def plan(f, text, client=None):
    r = (client or f.owner).post(f"/api/orgs/{f.org['id']}/actions/plan", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()


def execute(f, plan_id, client=None):
    return (client or f.owner).post(f"/api/orgs/{f.org['id']}/actions/execute", json={"plan_id": plan_id})


def test_rule_parser():
    p = fleet_actions.parse_rules
    assert p("Migrate Ironsight to Hailo T1")["action"] == "migrate_site"
    m = p("Move the front door camera from Ironsight to Qwenbot")
    assert (m["action"], m["cameras"], m["source_site"], m["target_site"]) == ("move_cameras", ["front door"], "Ironsight", "Qwenbot")
    assert p("Retire Ironsight")["source_site"] == "Ironsight"
    r = p("Set Qwenbot to 7 days of recording")
    assert (r["action"], r["source_site"], r["days"]) == ("set_retention", "Qwenbot", 7)
    assert p("keep 10 days at Hailo T1")["days"] == 10
    n = p("Rename cam3 on Hailo T1 to Loading Dock")
    assert (n["action"], n["cameras"], n["source_site"], n["new_name"]) == ("rename_camera", ["cam3"], "Hailo T1", "Loading Dock")
    assert p("move gate and dock cameras to Qwenbot")["cameras"] == ["gate", "dock"]
    # questions are never instructions, even with an action verb in them
    for q in ("how many people today?", "Did anyone move the ladder?", "Which cameras moved last week", "show me the front door"):
        assert fleet_actions._clean(q)[1] is False, q
    assert fleet_actions._clean("Can you move the back lot camera to Qwenbot?") == ("move the back lot camera to Qwenbot", True)


def test_question_is_not_an_action(fleet, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("questions must not reach the model")
    monkeypatch.setattr(fleet_actions.vlm_proxy, "configured", lambda: True)
    monkeypatch.setattr(fleet_actions.vlm_proxy, "complete", boom)
    assert plan(fleet, "how many people today?") == {"action": "none"}
    assert plan(fleet, "anyone at the front door last night?") == {"action": "none"}


def test_ai_plan_uses_real_names(fleet, monkeypatch):
    seen = {}

    async def fake_complete(messages, max_tokens=500, temperature=0.2, schema=None):
        seen["messages"], seen["schema"] = messages, schema
        if "kinda" in messages[-1]["content"]:
            return json.dumps({"action": "move_cameras", "source_site": "Ironsight", "target_site": "Qwenbot", "cameras": ["Back Lot"],
                               "days": 0, "new_name": "", "confidence": "medium"})
        if "weather" in messages[-1]["content"]:
            return json.dumps({"action": "none", "source_site": "", "target_site": "", "cameras": [], "days": 0, "new_name": "", "confidence": "high"})
        return "```json\n" + json.dumps({"action": "move_cameras", "source_site": "ironsight", "target_site": "qwen bot", "cameras": ["front door"],
                                          "days": 0, "new_name": "", "confidence": "high"}) + "\n```"
    monkeypatch.setattr(fleet_actions.vlm_proxy, "configured", lambda: True)
    monkeypatch.setattr(fleet_actions.vlm_proxy, "complete", fake_complete)
    p = plan(fleet, "Move the front door camera from Ironsight over to the qwen box")
    assert p["action"] == "move_cameras" and p["parser"] == "ai" and p["needs"] == []
    assert p["source"]["name"] == "Ironsight" and p["target"]["name"] == "Qwenbot" and [c["id"] for c in p["cameras"]] == ["cam1"]
    prompt = seen["messages"][0]["content"]
    assert "Front Door Inside sys 2 [cam1]" in prompt and "Hailo T1" in prompt and seen["schema"]["properties"]["action"]["enum"][-1] == "none"
    assert p["card"]["can_execute"] is True and p["allowed"] is True
    # an unsure reading becomes a question, not an action
    p = plan(fleet, "kinda shift the lot thing to qwen, move it")
    assert p["card"]["can_execute"] is False and "not sure" in p["needs"][0]
    # the model's "none" wins
    assert plan(fleet, "set the weather to sunny") == {"action": "none"}


def test_unresolved_names_ask_questions(fleet):
    p = plan(fleet, "Move the porch swing camera from Ironsight to Qwenbot")
    assert p["card"]["can_execute"] is False and any('no camera on Ironsight called "porch swing"' in n for n in p["needs"])
    assert execute(fleet, p["id"]).status_code == 409
    p = plan(fleet, "Migrate Ironside to Atlantis")
    assert any('no site called "Atlantis"' in n for n in p["needs"]) and p["card"]["can_execute"] is False


def test_viewer_cannot_execute(fleet):
    p = plan(fleet, "Set Qwenbot to 7 days of recording", client=fleet.viewer)
    assert p["allowed"] is False and p["card"]["can_execute"] is True
    assert execute(fleet, p["id"], client=fleet.viewer).status_code == 403
    assert fleet.apps["Qwenbot"].st["days"] == 30
    # the handoff is never reachable through the public proxy
    assert fleet.owner.get(f"/s/{fleet.sites['Ironsight']}/api/config/handoff").status_code == 404


def test_set_retention_and_rename(fleet):
    p = plan(fleet, "Set Qwenbot to 7 days of recording")
    assert p["action"] == "set_retention" and "now 30" in p["card"]["moves"][0]
    r = execute(fleet, p["id"])
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert fleet.apps["Qwenbot"].st["days"] == 7 and "7 days" in r.json()["lines"][0]
    assert execute(fleet, p["id"]).status_code == 404   # a plan runs once
    # an explicit plan through the API, no Ask text
    r = fleet.owner.post(f"/api/orgs/{fleet.org['id']}/actions/execute", json={"plan": {"action": "set_retention", "source_site": "qwenbot", "days": 9}})
    assert r.status_code == 200 and fleet.apps["Qwenbot"].st["days"] == 9
    p = plan(fleet, "Rename cam3 on Hailo T1 to Loading Dock")
    assert p["cameras"][0]["id"] == "cam3" and p["card"]["can_execute"]
    assert execute(fleet, p["id"]).json()["ok"]
    put = fleet.apps["Hailo T1"].st["puts"][-1]
    assert put["name"] == "Loading Dock" and put["host"] == "10.2.0.3" and "password" not in put and "status" not in put
    assert fleet.apps["Hailo T1"].st["cameras"]["cam3"]["name"] == "Loading Dock"


def test_move_one_camera(fleet, caplog):
    caplog.set_level(logging.DEBUG)
    p = plan(fleet, "Move the back lot camera from Ironsight to Qwenbot")
    assert p["action"] == "move_cameras" and [c["name"] for c in p["cameras"]] == ["Back Lot"] and p["card"]["can_execute"]
    assert any("Recordings and event clips stay on Ironsight" in s for s in p["card"]["stays"])
    assert PW_LOT not in json.dumps(p)
    r = execute(fleet, p["id"])
    assert r.status_code == 200 and r.json()["ok"], r.text
    q, src = fleet.apps["Qwenbot"].st, fleet.apps["Ironsight"].st
    assert q["cameras"]["cam2"]["password"] == PW_LOT and q["cameras"]["cam2"]["name"] == "Back Lot"
    assert src["cameras"]["cam2"]["enabled"] == 0 and src["moved_to"] == {"cam2": "Qwenbot"}
    assert src["handoff_calls"][-1] == ("hub", 0)
    row = db.one(sa.select(db.audit_log).where(db.audit_log.c.org_id == fleet.org["id"], db.audit_log.c.action.like("fleet action: Move%")))
    assert row["status"] == 200 and row["detail"]["cameras"] == [{"id": "cam2", "name": "Back Lot", "new_id": "cam2"}]
    assert PW_LOT not in json.dumps(row) and PW_LOT not in caplog.text and PW_LOT not in r.text
    # moving it back would land on a site that already has that address: the card says so
    p = plan(fleet, "move back lot from qwenbot to ironsight")
    assert any("already has \"Back Lot\" at 192.168.105.20" in w for w in p["card"]["warnings"])


def test_migrate_site(fleet, caplog):
    caplog.set_level(logging.DEBUG)
    p = plan(fleet, "Migrate Ironsight to Hailo T1")
    assert p["action"] == "migrate_site" and [c["name"] for c in p["cameras"]] == ["Front Door Inside sys 2"]
    card = p["card"]
    assert card["can_execute"] and "192.168.105.19, 4.1 Mbps" in card["moves"][0] and "1 zone, 1 place, 1 site rule" in card["moves"][0]
    assert any("Old porch" in s for s in card["stays"]) and any("retired" in s for s in card["stays"])
    assert any("64 Mbps" in w for w in card["warnings"]) and any("CPU" in w and "5 cameras" in w for w in card["warnings"])
    r = execute(fleet, p["id"])
    assert r.status_code == 200 and r.json()["ok"], r.text
    h = fleet.apps["Hailo T1"].st
    assert h["cameras"]["cam1_2"]["password"] == PW_FRONT and h["cameras"]["cam1"]["name"] == "Gate"   # id clash: new id, Gate untouched
    assert fleet.apps["Ironsight"].st["moved_to"]["cam1"] == "Hailo T1"
    assert any("as cam1_2" in line for line in r.json()["lines"]) and any("retired" in line for line in r.json()["lines"])
    assert PW_FRONT not in caplog.text and PW_FRONT not in r.text
    for row in db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id == fleet.org["id"])):
        assert PW_FRONT not in json.dumps(row)
    iron = fleet.sites["Ironsight"]
    assert db.one(sa.select(db.sites).where(db.sites.c.id == iron))["retired_at"]
    # hidden from Fleet (and so Home), shown with the toggle; Ask no longer fans out to it
    g = fleet.owner.get(f"/api/fleet?org={fleet.org['id']}").json()["orgs"][0]
    assert iron not in [s["id"] for s in g["sites"]] and g["retired"] == 1
    g = fleet.owner.get(f"/api/fleet?org={fleet.org['id']}&include_retired=true").json()["orgs"][0]
    assert any(s["id"] == iron and s["retired_at"] for s in g["sites"])
    with fleet.owner.stream("POST", "/api/fleet/ask", json={"org": fleet.org["id"], "message": "anything?"}) as resp:
        first = json.loads(next(resp.iter_lines()))
    assert iron not in [s["site"] for s in first["sites"]]
    # a retired site is no longer a target or a source
    p = plan(fleet, "Move the yard camera from Qwenbot to Ironsight")
    assert any('no site called "Ironsight"' in n for n in p["needs"])
    # and it can be brought back
    assert fleet.owner.post(f"/api/sites/{iron}/retire", json={"retired": False}).json()["retired_at"] is None


def test_retire_site(fleet):
    p = plan(fleet, "Retire Qwenbot")
    assert p["action"] == "retire_site" and any("still has 2 enabled cameras" in w for w in p["card"]["warnings"])
    assert execute(fleet, p["id"]).json()["ok"]
    assert db.one(sa.select(db.sites).where(db.sites.c.id == fleet.sites["Qwenbot"]))["retired_at"]
    fleet.owner.post(f"/api/sites/{fleet.sites['Qwenbot']}/retire", json={"retired": False})
