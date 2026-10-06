"""Fleet actions through real tunnels: five fake sites (Ironsight, Hailo T1, Qwenbot, Delta, Echo) behind HubAgents, the
Ask text -> plan -> confirmation card -> execute -> undo path, with the shared AI mocked and the rule parser as fallback.
Camera passwords must reach the destination and appear nowhere else (audit rows, logs, plan responses)."""
import asyncio
import base64
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
PW_YARD = "pw-yard-" + "k" * 6
PW_NEW = "pw-new-" + "m" * 6


def cam(cid, name, host, br=None, enabled=1, password="", zones=None, policies=None):
    return {"id": cid, "name": name, "host": host, "onvif_port": 80, "rtsp_port": 554, "username": "admin", "password": password,
            "main_path": "/main", "sub_path": "/sub", "enabled": enabled, "zones": zones or [], "retention_days": None, "scene_notes": "",
            "retention_policy": None, "synopsis_labels": None, "policies": policies or [], "ptz_config": None, "_br": br}


def make_site(name, cameras, yolo_device="hailo", days=14, events=None, views=None, unreachable=(), labels_default=("person",)):
    st = {"cameras": {c["id"]: c for c in cameras}, "days": days, "moved_to": {}, "puts": [], "merged": [], "handoff_calls": [],
          "yolo_device": yolo_device, "events": list(events or []), "views": list(views or []), "imported": {}, "files": {},
          "locks": {}, "purged": [], "unreachable": set(unreachable), "created": [], "history_calls": []}

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
        hub_only = scope["client"] == ("hub", 0) and headers.get("x-hub-internal") == "handoff"

        async def reply(status, obj):
            await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": json.dumps(obj).encode()})

        def public(c):
            ready = c.get("_ready", True) and c["host"] not in st["unreachable"] and bool(c.get("enabled", 1))
            return {**{k: v for k, v in c.items() if k not in ("password", "_br", "_ready")},
                    "status": {"stream_ready": ready, "health": {"bitrate_mbps": c["_br"]}}}

        if path == "/api/cameras" and method == "GET":
            await reply(200, [public(c) for c in st["cameras"].values()])
        elif path == "/api/config/handoff":
            st["handoff_calls"].append(scope["client"])
            if not hub_only:
                await reply(403, {"detail": "camera credentials are only handed to the fleet hub"})
                return
            ids = (q.get("cameras") or [""])[0].split(",")
            cams = [{k: v for k, v in c.items() if k not in ("_br", "_ready")} for c in st["cameras"].values() if c["id"] in ids]
            learned = {c["id"]: {"baseline": {"days": 12.0, "labels": {}}, "parked": [{"box": [0, 0, 1, 1]}],
                                 "corrections": [{"original": "a truck", "corrected": "a car"}]} for c in cams}
            await reply(200, {"format": 1, "partial": True, "cameras": cams, "camera_links": [], "learned": learned,
                              "identities": [{"name": "Sam", "kind": "person", "looks": [{"embedding": "AAAA", "sightings": 2}]}]})
        elif path == "/api/config/export":
            await reply(200, {"format": 1, "cameras": [{k: v for k, v in c.items() if k not in ("password", "_br", "_ready")} for c in st["cameras"].values()]})
        elif path == "/api/config/merge" and method == "POST":
            data = (await body(receive))["data"]
            ids, updated = {}, []
            by_host = {c["host"]: cid for cid, c in st["cameras"].items()}
            for c in data["cameras"]:
                if c["host"] in by_host:   # the same camera: updated in place (and switched on), as siteconfig.merge_cameras
                    new = by_host[c["host"]]
                    updated.append(new)
                    st["cameras"][new].update({k: v for k, v in copy.deepcopy(c).items() if k != "id"}, enabled=1)
                else:
                    new = c["id"] if c["id"] not in st["cameras"] else c["id"] + "_2"
                    st["cameras"][new] = {**copy.deepcopy(c), "id": new, "_br": 4.0}
                ids[c["id"]] = new
            st["merged"].append(data)
            n = len(data.get("learned") or {})
            await reply(200, {"cameras": len(ids), "updated": updated, "camera_links": 0, "identities": 1, "ids": ids,
                              "learned": {"baseline": n, "parked": n, "corrections": n}})
        elif path == "/api/config/history" and method == "GET":
            st["history_calls"].append(scope["client"])
            if not hub_only:
                await reply(403, {"detail": "tunnel only"})
                return
            cams = (q.get("cameras") or [""])[0].split(",")
            after = int((q.get("after_id") or ["0"])[0])
            evs = [e for e in st["events"] if e["camera_id"] in cams and e["src_id"] > after]
            page = evs[:1]   # one event per page here, so paging is exercised
            await reply(200, {"events": copy.deepcopy(page), "next_after_id": page[-1]["src_id"] if len(evs) > 1 else None, "total": len(evs)})
        elif path == "/api/config/history" and method == "POST":
            if not hub_only:
                await reply(403, {"detail": "tunnel only"})
                return
            b = await body(receive)
            ids = {}
            for e in b["events"]:
                key = (b["source"]["site_id"], e["src_id"])
                if key not in st["imported"]:
                    st["imported"][key] = {**e, "id": 1000 + len(st["imported"]), "camera_id": b["cameras"][e["camera_id"]],
                                           "migrated_from": {"site": b["source"]["site"], "event_id": e["src_id"]}}
                ids[str(e["src_id"])] = st["imported"][key]["id"]
            await reply(200, {"ids": ids, "added": len(b["events"]), "skipped": 0})
        elif path == "/api/config/history/files" and method == "POST":
            if not hub_only:
                await reply(403, {"detail": "tunnel only"})
                return
            files = (await body(receive))["files"]
            for f in files:
                st["files"][(f["event_id"], f["name"])] = f["data"]
            await reply(200, {"files": len(files)})
        elif path.startswith("/api/events/") and "/media/" in path:
            eid, name = path.split("/")[3], path.rsplit("/", 1)[1]
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"image/jpeg")]})
            await send({"type": "http.response.body", "body": f"jpeg-{eid}-{name}".encode()})
        elif path == "/api/find/views":
            if method == "PUT":
                st["views"] = (await body(receive))["views"]
            await reply(200, {"views": st["views"]})
        elif path.startswith("/api/cameras/") and method == "DELETE":
            cid = path.rsplit("/", 1)[1]
            if (q.get("purge") or [""])[0] == "true":
                st["cameras"].pop(cid, None)
                st["purged"].append(cid)
                await reply(200, {"ok": True, "removed": "deleted"})
                return
            st["cameras"][cid]["enabled"] = 0
            st["moved_to"][cid] = (q.get("moved_to") or [None])[0]
            await reply(200, {"ok": True, "removed": "disabled"})
        elif path.startswith("/api/cameras/") and method == "PUT":
            cid = path.rsplit("/", 1)[1]
            b = await body(receive)
            st["puts"].append({k: v for k, v in b.items() if k != "password"} | {"had_password": "password" in b})
            if cid not in st["cameras"]:
                st["created"].append(b)
                st["cameras"][cid] = {**b, "_br": 3.0, "ptz_config": None}
            else:
                st["cameras"][cid].update({k: v for k, v in b.items() if k != "password"})
            await reply(200, public(st["cameras"][cid]))
        elif path == "/api/retention/policy":
            if method == "PUT":
                st["days"] = (await body(receive))["continuous_days"]
            await reply(200, {"policy": {"continuous_days": st["days"], "min_free_gb": 50}})
        elif path == "/api/retention/stats":
            await reply(200, {"disk": {"total_gb": 2000, "free_gb": 500}, "cameras": [{"camera_id": "x", "continuous_gb": 100}]})
        elif path == "/api/system":
            await reply(200, {"yolo_device": st["yolo_device"], "yolo_frame_ms": 21.5, "recordings_disk": {"total_gb": 2000, "free_gb": 500},
                              "tz_offset_s": 0, "synopsis_labels_default": list(labels_default)})
        elif path == "/api/locks" and method == "POST":
            b = await body(receive)
            lid = len(st["locks"]) + 1
            st["locks"][lid] = b
            await reply(200, {"id": lid, **b})
        elif path.startswith("/api/locks/") and method == "DELETE":
            st["locks"].pop(int(path.rsplit("/", 1)[1]), None)
            await reply(200, {"ok": True})
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
        "Delta": make_site("Delta", [cam("d1", "Side Yard", "10.4.0.1", 3.0, password=PW_YARD), {**cam("d2", "Shed", "10.4.0.2", 2.0, password="shed-" + "p" * 4), "_ready": False}],
                           "cpu", events=[{"src_id": 501, "camera_id": "d1", "synopsis": "A person by the shed", "status": "verified", "files": ["crop_0.jpg", "snapshot.jpg"]},
                                          {"src_id": 502, "camera_id": "d1", "synopsis": "A van", "status": "verified", "files": ["crop_0.jpg"]}],
                           views=[{"id": "v1", "name": "Shed watch", "filters": {"camera": "d1"}, "mode": "events"},
                                  {"id": "v2", "name": "Shed only", "filters": {"camera": "d2"}, "mode": "events"}]),
        "Echo": make_site("Echo", [cam("d1", "Lobby", "10.5.0.1", 5.0)], "cpu", views=[{"id": "v9", "name": "Shed watch", "filters": {"camera": "d1"}, "mode": "events"}],
                          labels_default=("person", "vehicle")),
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
    monkeypatch.setattr(fleet_actions, "STREAM_CHECK_S", 1.0)
    monkeypatch.setattr(fleet_actions, "STREAM_POLL_S", 0.05)
    monkeypatch.setattr(fleet_actions, "RATE_N", 1000)
    fleet_actions._cameras_cache.clear()
    fleet_actions._busy.clear()
    fleet_actions._runs.clear()
    fleet_actions._refusals.clear()


def plan(f, text, client=None, origin="actions_page"):
    """Plans as the Customer › Actions page makes them (origin "actions_page"); origin=None is any other caller."""
    r = (client or f.owner).post(f"/api/orgs/{f.org['id']}/actions/plan", json={"text": text, **({"origin": origin} if origin else {})})
    assert r.status_code == 200, r.text
    return r.json()


def execute(f, plan_id, client=None, confirm_name=None):
    body = {"plan_id": plan_id, **({"confirm_name": confirm_name} if confirm_name is not None else {})}
    return (client or f.owner).post(f"/api/orgs/{f.org['id']}/actions/execute", json=body)


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
    # an explicit plan through the API (no plan from the Actions page) is refused, and logged
    r = fleet.owner.post(f"/api/orgs/{fleet.org['id']}/actions/execute", json={"plan": {"action": "set_retention", "source_site": "qwenbot", "days": 9}})
    assert r.status_code == 403 and fleet.apps["Qwenbot"].st["days"] == 7
    assert _last_action(fleet)["detail"]["outcome"] == "refused"
    p = plan(fleet, "Rename cam3 on Hailo T1 to Loading Dock")
    assert p["cameras"][0]["id"] == "cam3" and p["card"]["can_execute"]
    assert execute(fleet, p["id"]).json()["ok"]
    put = fleet.apps["Hailo T1"].st["puts"][-1]
    assert put["name"] == "Loading Dock" and put["host"] == "10.2.0.3" and put["had_password"] is False and "status" not in put
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
    row = db.one(sa.select(db.audit_log).where(db.audit_log.c.org_id == fleet.org["id"], db.audit_log.c.action.like("fleet action: Move%"), db.audit_log.c.status == 200))
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
    assert card["confirm_name"] == "Ironsight" and p["options"]["copy_history"] is True
    r = execute(fleet, p["id"], confirm_name="Ironsight")
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
    assert execute(fleet, p["id"], confirm_name="qwenbot").json()["ok"]
    assert db.one(sa.select(db.sites).where(db.sites.c.id == fleet.sites["Qwenbot"]))["retired_at"]
    fleet.owner.post(f"/api/sites/{fleet.sites['Qwenbot']}/retire", json={"retired": False})


# ---------------------------------------------------------------- v2: references, learned state, history, stream check, undo, new verbs

def _last_action(f):
    return db.one(sa.select(db.audit_log).where(db.audit_log.c.org_id == f.org["id"], db.audit_log.c.method == "ACTION")
                  .order_by(db.audit_log.c.id.desc()).limit(1))


def _audit(f, audit_id):
    return db.one(sa.select(db.audit_log).where(db.audit_log.c.id == audit_id))


def _site_row(f, name):
    return db.one(sa.select(db.sites).where(db.sites.c.id == f.sites[name]))


def test_move_carries_references_learned_state_and_history(fleet, caplog):
    caplog.set_level(logging.DEBUG)
    org, delta, echo = fleet.org["id"], fleet.sites["Delta"], fleet.sites["Echo"]
    dash = {"version": 1, "cols": 12, "rowH": 60, "widgets": [
        {"id": "w1", "type": "camera", "x": 0, "y": 0, "w": 4, "h": 4, "props": {"site": delta, "camera": "d1", "quality": "sd"}},
        {"id": "w2", "type": "events", "x": 4, "y": 0, "w": 4, "h": 4, "props": {"cameras": [{"site": delta, "camera": "d1"}, {"site": echo, "camera": "d1"}], "limit": 20}},
        {"id": "w3", "type": "camera", "x": 8, "y": 0, "w": 4, "h": 4, "props": {"site": echo, "camera": "d1", "quality": "sd"}}]}
    r = fleet.owner.post(f"/api/orgs/{org}/dashboards", json={"name": "Ops", "config": dash, "shared": True})
    assert r.status_code == 200, r.text
    dash_id = r.json()["id"]
    r = fleet.owner.post(f"/api/orgs/{org}/groups", json={"name": "Yards", "members": [{"site": delta, "camera": "d1"}, {"site": echo, "camera": "d1"}]})
    assert r.status_code == 200, r.text
    group_id = r.json()["id"]
    now = time.time()
    for kind, key, det in (("camera_down", "d1", {"name": "Side Yard"}), ("event_high", "501", {"id": 501, "camera_id": "d1", "priority": "high"}),
                           ("event_high", "777", {"id": 777, "camera_id": "d9", "priority": "high"})):
        db.insert(db.alerts, {"org_id": org, "site_id": delta, "kind": kind, "key": key, "opened_at": now, "closed_at": None,
                              "acked_by": None, "acked_at": None, "detail": det})

    p = plan(fleet, "Move Side Yard from Delta to Echo with history")
    assert p["action"] == "move_cameras" and p["options"]["copy_history"] is True and p["card"]["can_execute"], p
    card = p["card"]
    assert any(o["key"] == "copy_history" and o["default"] is True for o in card["options"]) and card["confirm_name"] is None
    assert any("Mbps from" in ln for ln in card["capacity"]) and any("days of continuous footage" in ln for ln in card["capacity"])
    assert any("Detection: CPU, 21.5 ms per frame" in ln for ln in card["capacity"])
    assert card["capacity_data"]["mbps"] == 8.0 and card["capacity_data"]["cameras"] == 2
    assert any("baseline" in m for m in card["moves"]) and any("Find views" in m for m in card["moves"])
    assert plan(fleet, "Move Side Yard from Delta to Echo")["options"]["copy_history"] is False      # move: off by default
    r = execute(fleet, p["id"])
    assert r.status_code == 200 and r.json()["ok"], r.text
    res = r.json()
    e, d = fleet.apps["Echo"].st, fleet.apps["Delta"].st
    assert e["cameras"]["d1_2"]["password"] == PW_YARD and e["cameras"]["d1"]["name"] == "Lobby"   # id clash: d1_2
    assert d["cameras"]["d1"]["enabled"] == 0 and d["moved_to"]["d1"] == "Echo"
    # learned state rode along in the handoff and was seeded
    assert e["merged"][-1]["learned"]["d1"]["baseline"]["days"] == 12.0 and e["merged"][-1]["source"] == "Delta"
    assert any("Learned state carried over: 1 baseline, 1 parked spot, 1 operator correction" in ln for ln in res["lines"])
    # history: both events (two pages), remapped camera, new ids, images posted, all through the tunnel only
    imported = sorted(e["imported"].values(), key=lambda x: x["src_id"])
    assert [(x["src_id"], x["camera_id"], x["id"]) for x in imported] == [(501, "d1_2", 1000), (502, "d1_2", 1001)]
    assert "files" not in imported[0] and imported[0]["synopsis"] == "A person by the shed"
    assert e["files"][(1000, "snapshot.jpg")] == base64.b64encode(b"jpeg-501-snapshot.jpg").decode() and (1001, "crop_0.jpg") in e["files"]
    assert all(c == ("hub", 0) for c in d["history_calls"])
    assert any("Copied 2 events (3 images, no clips) to Echo" in ln for ln in res["lines"])
    # references followed the camera
    cfg = db.one(sa.select(db.dashboards).where(db.dashboards.c.id == dash_id))["config"]
    assert cfg["widgets"][0]["props"]["site"] == echo and cfg["widgets"][0]["props"]["camera"] == "d1_2"
    assert cfg["widgets"][1]["props"]["cameras"] == [{"site": echo, "camera": "d1_2"}, {"site": echo, "camera": "d1"}]
    assert cfg["widgets"][2]["props"] == {"site": echo, "camera": "d1", "quality": "sd"}       # Echo's own camera untouched
    members = db.one(sa.select(db.camera_groups).where(db.camera_groups.c.id == group_id))["members"]
    assert members == [{"site_id": echo, "camera_id": "d1_2"}, {"site_id": echo, "camera_id": "d1"}]
    al = {a["key"]: a for a in db.rows(sa.select(db.alerts).where(db.alerts.c.org_id == org, db.alerts.c.kind.in_(["camera_down", "event_high"])))}
    assert al["d1"]["closed_at"] and al["1000"]["site_id"] == echo and al["1000"]["detail"]["camera_id"] == "d1_2"
    assert al["777"]["site_id"] == delta and al["777"]["closed_at"] is None                    # not a moved camera
    assert [v for v in e["views"] if v["filters"]["camera"] == "d1_2"][0]["name"] == "Shed watch (Delta)"   # name clash on Echo
    assert len(d["views"]) == 2                                                                 # the source keeps its own
    assert any("References updated to Echo: 1 dashboard, 1 camera group, 1 open alert, 1 saved Find view" in ln for ln in res["lines"])
    assert any("1 camera-down alert closed" in ln for ln in res["lines"])
    # audit row: what was done, a reverse plan for 24 h, never the password
    row = _audit(fleet, res["audit_id"])
    assert row["detail"]["references"] == {"dashboards": 1, "groups": 1, "alerts": 1, "alerts_closed": 1, "find_views": 1}
    assert row["detail"]["reverse"] == [{"action": "move_cameras", "source_site": echo, "target_site": delta, "cameras": ["d1_2"], "copy_history": False}]
    assert res["undo_until"] and res["undo_until"] - time.time() > 23 * 3600
    assert PW_YARD not in json.dumps(row) and PW_YARD not in caplog.text and PW_YARD not in r.text
    fleet.moved_audit = res["audit_id"]
    fleet.dash_id, fleet.group_id = dash_id, group_id


def test_undo_round_trip(fleet):
    org, delta, echo = fleet.org["id"], fleet.sites["Delta"], fleet.sites["Echo"]
    assert fleet.viewer.post(f"/api/orgs/{org}/actions/undo/{fleet.moved_audit}").status_code == 403
    r = fleet.owner.post(f"/api/orgs/{org}/actions/undo/{fleet.moved_audit}")
    assert r.status_code == 200 and r.json()["ok"], r.text
    e, d = fleet.apps["Echo"].st, fleet.apps["Delta"].st
    assert d["cameras"]["d1"]["enabled"] == 1 and e["cameras"]["d1_2"]["enabled"] == 0 and e["moved_to"]["d1_2"] == "Delta"
    assert any("now records on Delta (as d1)" in ln for ln in r.json()["lines"])
    cfg = db.one(sa.select(db.dashboards).where(db.dashboards.c.id == fleet.dash_id))["config"]
    assert cfg["widgets"][0]["props"]["site"] == delta and cfg["widgets"][0]["props"]["camera"] == "d1"
    row = _audit(fleet, fleet.moved_audit)
    assert row["detail"]["undone_at"] and row["detail"]["undone_by"] == "root@example.com"
    assert fleet_actions.undo_until(row) is None
    assert fleet.owner.post(f"/api/orgs/{org}/actions/undo/{fleet.moved_audit}").status_code == 409     # once
    assert fleet.owner.post(f"/api/orgs/{org}/actions/undo/999999").status_code == 404
    undo_row = _audit(fleet, row["detail"]["undo_audit"][-1])
    assert undo_row["action"].startswith("fleet action: Undo: Move") and "reverse" not in undo_row["detail"]
    # an action older than 24 h can no longer be undone
    old = {**row, "ts": time.time() - 25 * 3600, "detail": {**row["detail"], "undone_at": None}}
    assert fleet_actions.undo_until(old) is None


def test_stream_check_rolls_back(fleet, monkeypatch):
    monkeypatch.setattr(fleet_actions, "STREAM_CHECK_S", 0.6)
    e, d = fleet.apps["Echo"].st, fleet.apps["Delta"].st
    e["unreachable"].add("10.4.0.2")
    p = plan(fleet, "Move Shed from Delta to Echo")
    assert any("not streaming at Delta either" in w for w in p["card"]["warnings"]), p["card"]["warnings"]
    r = execute(fleet, p["id"])
    assert r.status_code == 200 and not r.json()["ok"]
    assert any("Echo could not reach 10.4.0.2 within 0.6 s: check VLAN/firewall" in ln for ln in r.json()["lines"]), r.json()["lines"]
    assert "d2" not in e["cameras"] and "d2" in e["purged"]                    # merged camera removed again
    assert d["cameras"]["d2"]["enabled"] == 1 and "d2" not in d["moved_to"]   # the source was never touched
    assert r.json()["undo_until"] is None
    # forced through with skip_stream_check
    p = plan(fleet, "Move Shed from Delta to Echo")
    r = fleet.owner.post(f"/api/orgs/{fleet.org['id']}/actions/execute", json={"plan_id": p["id"], "options": {"skip_stream_check": True}})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert "d2" in e["cameras"] and d["cameras"]["d2"]["enabled"] == 0
    e["unreachable"].discard("10.4.0.2")
    assert fleet.owner.post(f"/api/orgs/{fleet.org['id']}/actions/undo/{r.json()['audit_id']}").json()["ok"]
    assert d["cameras"]["d2"]["enabled"] == 1


def test_type_to_confirm(fleet):
    p = plan(fleet, "Retire Delta")
    assert p["card"]["confirm_name"] == "Delta"
    assert execute(fleet, p["id"]).status_code == 400
    r = execute(fleet, p["id"], confirm_name="Delt")
    assert r.status_code == 400 and 'type the server name "Delta"' in r.json()["detail"]
    assert _site_row(fleet, "Delta")["retired_at"] is None
    r = execute(fleet, p["id"], confirm_name="  delta ")
    assert r.status_code == 200 and r.json()["ok"]
    assert _site_row(fleet, "Delta")["retired_at"]
    # undo restores it
    assert fleet.owner.post(f"/api/orgs/{fleet.org['id']}/actions/undo/{r.json()['audit_id']}").json()["ok"]
    assert _site_row(fleet, "Delta")["retired_at"] is None
    # migrate asks for the name too
    p = plan(fleet, "Migrate Delta to Echo")
    assert p["card"]["confirm_name"] == "Delta" and p["options"]["copy_history"] is True
    assert execute(fleet, p["id"], confirm_name="Echo").status_code == 400


def test_add_camera_password_reaches_the_site_only(fleet, caplog):
    caplog.set_level(logging.DEBUG)
    p = plan(fleet, "Add 10.5.0.50 to Echo as Front Gate")
    assert p["action"] == "add_camera" and p["card"]["can_execute"]
    assert [i["key"] for i in p["card"]["inputs"]][:2] == ["password", "username"] and p["card"]["inputs"][0]["type"] == "password"
    assert any("counting 4 Mbps for the new camera" in ln for ln in p["card"]["capacity"])
    r = fleet.owner.post(f"/api/orgs/{fleet.org['id']}/actions/execute", json={"plan_id": p["id"]})
    assert r.status_code == 200 and not r.json()["ok"] and "password" in r.json()["lines"][-1]
    p = plan(fleet, "Add 10.5.0.50 to Echo as Front Gate")
    r = fleet.owner.post(f"/api/orgs/{fleet.org['id']}/actions/execute",
                         json={"plan_id": p["id"], "camera": {"password": PW_NEW, "username": "installer", "rtsp_port": "8554", "main_path": "/live/main"}})
    assert r.status_code == 200 and r.json()["ok"], r.text
    e = fleet.apps["Echo"].st
    made = e["created"][-1]
    assert made["password"] == PW_NEW and made["host"] == "10.5.0.50" and made["name"] == "Front Gate" and made["username"] == "installer"
    assert made["rtsp_port"] == 8554 and made["main_path"] == "/live/main" and made["id"] not in ("d1", "d1_2")
    assert any("is pulling its stream" in ln for ln in r.json()["lines"])
    for row in db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id == fleet.org["id"])):
        assert PW_NEW not in json.dumps(row)
    assert PW_NEW not in caplog.text and PW_NEW not in r.text and PW_NEW not in json.dumps(p)
    # a bad port is refused without echoing what was typed
    p2 = plan(fleet, "Add 10.5.0.51 to Echo as Spare")
    r2 = fleet.owner.post(f"/api/orgs/{fleet.org['id']}/actions/execute", json={"plan_id": p2["id"], "camera": {"password": PW_NEW, "onvif_port": "99999"}})
    assert not r2.json()["ok"] and PW_NEW not in r2.text
    # undo removes it again
    assert fleet.owner.post(f"/api/orgs/{fleet.org['id']}/actions/undo/{r.json()['audit_id']}").json()["ok"]
    assert made["id"] in e["purged"] and made["id"] not in e["cameras"]


def test_quiet_alerts_mutes(fleet):
    from hub import alerts
    echo, delta = _site_row(fleet, "Echo"), _site_row(fleet, "Delta")

    def event(eid):
        return {"type": "event", "event": {"id": eid, "status": "verified", "priority": "high", "camera_id": "d1", "start_ts": time.time()}}

    p = plan(fleet, "Quiet alerts at Echo for 2 hours")
    assert p["action"] == "quiet_alerts" and p["site"]["name"] == "Echo" and p["card"]["can_execute"], p
    r = execute(fleet, p["id"])
    assert r.status_code == 200 and r.json()["ok"]
    mute = fleet_actions.current_mute(fleet.org["id"])
    assert mute["sites"] == [echo["id"]] and 7100 < mute["until"] - time.time() <= 7200
    alerts.on_event(echo, event(9001))
    alerts.on_event(delta, event(9002))
    keys = {a["key"] for a in db.rows(sa.select(db.alerts).where(db.alerts.c.kind == "event_high"))}
    assert "9001" not in keys and "9002" in keys
    assert fleet.owner.post(f"/api/orgs/{fleet.org['id']}/actions/undo/{r.json()['audit_id']}").json()["ok"]
    assert fleet_actions.current_mute(fleet.org["id"]) is None
    alerts.on_event(echo, event(9003))
    assert db.one(sa.select(db.alerts).where(db.alerts.c.key == "9003"))
    assert not plan(fleet, "quiet alerts")["card"]["can_execute"]     # until when?


def test_lock_footage_and_labels(fleet):
    e = fleet.apps["Echo"].st
    p = plan(fleet, "Lock Lobby footage 3-4 pm yesterday")
    assert p["action"] == "lock_footage" and p["card"]["can_execute"], p
    r = execute(fleet, p["id"])
    assert r.status_code == 200 and r.json()["ok"], r.text
    lock = list(e["locks"].values())[-1]
    assert lock["camera_id"] == "d1" and lock["end_ts"] - lock["start_ts"] == 3600 and lock["start_ts"] % 3600 == 0   # site tz offset 0
    assert time.gmtime(lock["start_ts"]).tm_hour == 15
    assert fleet.owner.post(f"/api/orgs/{fleet.org['id']}/actions/undo/{r.json()['audit_id']}").json()["ok"] and not e["locks"]
    p = plan(fleet, "Stop describing vehicles on Lobby at Echo")
    assert p["action"] == "set_synopsis_labels" and p["card"]["can_execute"] and "Qwen describes people on \"Lobby\" (now people and vehicles)" in p["card"]["moves"][0]
    r = execute(fleet, p["id"])
    assert r.json()["ok"] and e["cameras"]["d1"]["synopsis_labels"] == ["person"] and e["puts"][-1]["had_password"] is False
    assert fleet.owner.post(f"/api/orgs/{fleet.org['id']}/actions/undo/{r.json()['audit_id']}").json()["ok"]
    assert e["cameras"]["d1"]["synopsis_labels"] is None          # back to the site default
    # set_retention undo restores the old number
    p = plan(fleet, "Set Echo to 5 days of recording")
    r = execute(fleet, p["id"])
    assert e["days"] == 5
    assert fleet.owner.post(f"/api/orgs/{fleet.org['id']}/actions/undo/{r.json()['audit_id']}").json()["ok"] and e["days"] == 14


def test_reference_lists_every_verb(fleet):
    r = fleet.owner.get(f"/api/orgs/{fleet.org['id']}/actions/reference")
    assert r.status_code == 200
    ref = r.json()
    assert [v["action"] for v in ref["verbs"]] == list(fleet_actions.ACTIONS)
    for v in ref["verbs"]:
        assert len(v["examples"]) >= 2 and v["moves"] and v["stays"] and v["undo"], v["action"]
        for ex in v["examples"]:   # the page's examples are what the planner understands: they cannot drift apart
            cleaned, maybe = fleet_actions._clean(ex)
            assert maybe and fleet_actions.parse_rules(cleaned)["action"] == v["action"], ex
    assert set(fleet_actions.SCHEMA["properties"]["action"]["enum"]) == {*fleet_actions.ACTIONS, "none"}
    assert all(f"- {a}:" in fleet_actions.SYSTEM for a in fleet_actions.ACTIONS)
    assert any("type" in s.lower() and "name" in s for s in ref["safety"]) and ref["undo_hours"] == 24 and ref["capacity"]
    recent = ref["recent"]
    assert 0 < len(recent) <= 50 and all(x["action"].startswith("fleet action:") for x in recent)
    assert any(x["undo_until"] for x in recent) and any(x["undo_until"] is None for x in recent)
    assert ref["log_scope"] == "all" and any(x["outcome"] == "refused" for x in recent)
    # a viewer's log: only their own attempts (the refused Confirm in test_viewer_cannot_execute)
    mine = fleet.viewer.get(f"/api/orgs/{fleet.org['id']}/actions/reference").json()
    assert mine["log_scope"] == "own" and mine["recent"] and all(x["user_email"] == "viewer@actions.example" for x in mine["recent"])
    assert all(not x["can_undo"] for x in mine["recent"])


def test_sites_with_several_servers(fleet, monkeypatch):
    """Tenancy v2: a Site groups servers. A two-server Site quiets all its servers, asks which server for a
    server-level verb, and finds a camera's owner among its servers; a one-server Site's name means its server."""
    from hub import alerts
    org, delta, echo, qwen = fleet.org["id"], fleet.sites["Delta"], fleet.sites["Echo"], fleet.sites["Qwenbot"]
    t = time.time()
    hq, main = db.new_id("l_"), db.new_id("l_")
    for lid, name in ((hq, "Austin HQ"), (main, "Main Street")):
        db.insert(db.locations, {"id": lid, "org_id": org, "name": name, "address": "", "timezone": None, "notes": None,
                                 "created_at": t, "updated_at": t})
    db.run(sa.update(db.sites).where(db.sites.c.id.in_([delta, echo])).values(location_id=hq))
    db.run(sa.update(db.sites).where(db.sites.c.id == qwen).values(location_id=main))
    try:
        p = plan(fleet, "Quiet alerts at Austin HQ tonight")
        assert p["action"] == "quiet_alerts" and p["site"] is None and p["location"]["name"] == "Austin HQ" and p["card"]["can_execute"], p
        assert p["summary"] == "Quiet alerts at Austin HQ (2 servers) tonight"
        r = execute(fleet, p["id"])
        assert r.status_code == 200 and r.json()["ok"], r.text
        mute = fleet_actions.current_mute(org)
        assert sorted(mute["sites"]) == sorted([delta, echo]) and mute["location"] == {"id": hq, "name": "Austin HQ"}
        assert alerts.muted(_site_row(fleet, "Delta")) and alerts.muted(_site_row(fleet, "Echo")) and not alerts.muted(_site_row(fleet, "Qwenbot"))
        assert fleet.owner.post(f"/api/orgs/{org}/actions/undo/{r.json()['audit_id']}").json()["ok"]
        assert fleet_actions.current_mute(org) is None

        p = plan(fleet, "Retire Austin HQ")
        assert not p["card"]["can_execute"] and "Austin HQ has 2 servers (Delta, Echo): which one?" in p["needs"], p["needs"]
        p = plan(fleet, "Set Main Street to 12 days of recording")
        assert p["site"]["id"] == qwen and p["card"]["can_execute"], p
        p = plan(fleet, "Rename Lobby on Austin HQ to Front Lobby")
        assert p["site"]["id"] == echo and [c["id"] for c in p["cameras"]] == ["d1"] and p["card"]["can_execute"], p

        seen = {}

        async def fake_complete(messages, max_tokens=500, temperature=0.2, schema=None):
            seen["prompt"] = messages[0]["content"]
            return json.dumps({**fleet_actions.EMPTY, "action": "none"})
        monkeypatch.setattr(fleet_actions.vlm_proxy, "configured", lambda: True)
        monkeypatch.setattr(fleet_actions.vlm_proxy, "complete", fake_complete)
        plan(fleet, "Retire Austin HQ")
        assert "- Site Austin HQ: server Delta (online): " in seen["prompt"] and "; server Echo (online): Lobby [d1]" in seen["prompt"]
        assert "- Site Main Street: server Qwenbot (online): Yard [cam1]" in seen["prompt"]
    finally:
        db.run(sa.update(db.sites).where(db.sites.c.id.in_([delta, echo, qwen])).values(location_id=None))
        db.run(sa.delete(db.locations).where(db.locations.c.id.in_([hq, main])))


# ---------------------------------------------------------------- instructions run only from Customer › Actions

def _member(f, email, role):
    uid = f.owner.post(f"/api/orgs/{f.org['id']}/members", json={"email": email, "role": role, "password": "member password 1"}).json()["id"]
    c = httpx.Client(base_url=f.base, timeout=30)
    assert c.post("/auth/login", json={"email": email, "password": "member password 1"}).status_code == 200
    return uid, c


def _set_role(f, uid, role):
    db.run(sa.update(db.memberships).where(db.memberships.c.user_id == uid, db.memberships.c.org_id == f.org["id"]).values(role=role))


def _action_count():
    return len(db.rows(sa.select(db.audit_log.c.id).where(db.audit_log.c.method == "ACTION")))


def test_fleet_ask_never_plans(fleet, monkeypatch):
    """Find's Ask (POST /api/fleet/ask) only asks the servers' assistants: an instruction is never planned or run."""
    async def boom(*a, **k):
        raise AssertionError("the fleet Ask must not plan actions")
    monkeypatch.setattr(fleet_actions, "plan_for", boom)
    monkeypatch.setattr(fleet_actions, "build", boom)
    before, rows = set(fleet_actions._plans), _action_count()
    with fleet.owner.stream("POST", "/api/fleet/ask", json={"org": fleet.org["id"], "message": "Migrate Ironsight to Hailo T1"}) as resp:
        assert resp.status_code == 200
        lines = [json.loads(x) for x in resp.iter_lines() if x.strip()]
    assert lines and lines[-1]["type"] == "done" and not any(x.get("type") == "plan" for x in lines)
    assert set(fleet_actions._plans) == before and _action_count() == rows
    assert _site_row(fleet, "Ironsight")["retired_at"] is None


def test_execute_needs_an_actions_page_plan_by_the_same_user(fleet):
    e = fleet.apps["Echo"].st
    days = e["days"]
    # a plan made anywhere else (no origin) has a card but cannot run
    p = plan(fleet, "Set Echo to 6 days of recording", origin=None)
    assert p["card"]["can_execute"] and p["parser"] == "rules"
    r = execute(fleet, p["id"])
    assert r.status_code == 403 and "Customer › Actions" in r.json()["detail"] and e["days"] == days
    row = _last_action(fleet)
    assert row["status"] == 403 and row["detail"]["outcome"] == "refused" and "Customer › Actions" in row["detail"]["reason"]
    assert row["detail"]["plan"] == p["id"] and row["detail"]["servers"] == ["Echo"] and row["user_email"] == "root@example.com"
    # someone else's plan
    uid, admin2 = _member(fleet, "admin2@actions.example", "admin")
    p = plan(fleet, "Set Echo to 6 days of recording")
    r = execute(fleet, p["id"], client=admin2)
    assert r.status_code == 403 and "someone else" in r.json()["detail"] and e["days"] == days
    assert _last_action(fleet)["user_email"] == "admin2@actions.example" and _last_action(fleet)["detail"]["outcome"] == "refused"
    # the role is checked again at Confirm: planned as admin, demoted before pressing it
    p = plan(fleet, "Set Echo to 6 days of recording", client=admin2)
    assert p["allowed"] is True
    _set_role(fleet, uid, "viewer")
    try:
        r = execute(fleet, p["id"], client=admin2)
        assert r.status_code == 403 and "needs admin" in r.json()["detail"] and e["days"] == days
        assert _last_action(fleet)["detail"]["reason"] == "needs admin in this organization"
    finally:
        _set_role(fleet, uid, "admin")
    # the owner's own Actions-page plan runs, once; the second try is refused and logged as "already ran"
    p = plan(fleet, "Set Echo to 6 days of recording")
    r = execute(fleet, p["id"])
    assert r.status_code == 200 and r.json()["ok"] and e["days"] == 6
    done = _audit(fleet, r.json()["audit_id"])
    assert done["detail"]["outcome"] == "done" and done["detail"]["origin"] == "actions_page" and done["detail"]["reverse"]
    r = execute(fleet, p["id"])
    assert r.status_code == 404 and "already ran" in r.json()["detail"] and "already ran" in _last_action(fleet)["detail"]["reason"]
    # an expired plan says so
    p = plan(fleet, "Set Echo to 7 days of recording")
    fleet_actions._plans[p["id"]]["expires_at"] = time.time() - 1
    r = execute(fleet, p["id"])
    assert r.status_code == 404 and "expired" in r.json()["detail"] and _last_action(fleet)["detail"]["outcome"] == "refused"
    # a wrong typed name is refused and logged
    p = plan(fleet, "Retire Echo")
    assert execute(fleet, p["id"], confirm_name="Ech").status_code == 400
    assert 'type the server name "Echo"' in _last_action(fleet)["detail"]["reason"] and _site_row(fleet, "Echo")["retired_at"] is None
    assert fleet.owner.post(f"/api/orgs/{fleet.org['id']}/actions/undo/{done['id']}").json()["ok"] and e["days"] == days


def test_execute_rechecks_site_scope(fleet):
    org, echo, qwen = fleet.org["id"], fleet.sites["Echo"], fleet.sites["Qwenbot"]
    t = time.time()
    north, south = db.new_id("l_"), db.new_id("l_")
    for lid, name in ((north, "North"), (south, "South")):
        db.insert(db.locations, {"id": lid, "org_id": org, "name": name, "address": "", "timezone": None, "notes": None, "created_at": t, "updated_at": t})
    db.run(sa.update(db.sites).where(db.sites.c.id == echo).values(location_id=north))
    db.run(sa.update(db.sites).where(db.sites.c.id == qwen).values(location_id=south))
    uid, radm = _member(fleet, "radm@actions.example", "admin")
    try:
        assert fleet.owner.put(f"/api/orgs/{org}/members/{uid}/access", json={"all_sites": False, "location_ids": [north]}).status_code == 200
        # servers outside their Sites do not resolve at plan time
        p = plan(fleet, "Set Qwenbot to 9 days of recording", client=radm)
        assert not p["card"]["can_execute"] and p["site"] is None
        # nor may they quiet the whole customer
        p = plan(fleet, "Quiet alerts for 2 hours", client=radm)
        r = execute(fleet, p["id"], client=radm)
        assert r.status_code == 403 and "every Site" in r.json()["detail"] and fleet_actions.current_mute(org) is None
        # planned while granted North, executed after the grant moved to South: refused at Confirm
        p = plan(fleet, "Set Echo to 8 days of recording", client=radm)
        assert p["card"]["can_execute"]
        assert fleet.owner.put(f"/api/orgs/{org}/members/{uid}/access", json={"all_sites": False, "location_ids": [south]}).status_code == 200
        r = execute(fleet, p["id"], client=radm)
        assert r.status_code == 403 and "access to Echo" in r.json()["detail"] and fleet.apps["Echo"].st["days"] != 8
        row = _last_action(fleet)
        assert row["detail"]["outcome"] == "refused" and row["site_id"] == echo and row["detail"]["location_id"] == north
    finally:
        db.run(sa.update(db.sites).where(db.sites.c.id.in_([echo, qwen])).values(location_id=None))
        db.run(sa.delete(db.location_grants).where(db.location_grants.c.user_id == uid))
        db.run(sa.delete(db.locations).where(db.locations.c.id.in_([north, south])))


def test_one_action_per_server_and_rate_limit(fleet, monkeypatch):
    echo = fleet.sites["Echo"]
    e = fleet.apps["Echo"].st
    days = e["days"]
    p = plan(fleet, "Set Echo to 4 days of recording")
    fleet_actions._busy[echo] = "p_other"
    r = execute(fleet, p["id"])
    assert r.status_code == 409 and r.json()["detail"] == "Echo is busy with another action" and e["days"] == days
    assert _last_action(fleet)["detail"]["outcome"] == "refused" and _last_action(fleet)["status"] == 409
    del fleet_actions._busy[echo]
    r = execute(fleet, p["id"])           # a refusal does not use the plan up
    assert r.status_code == 200 and r.json()["ok"] and e["days"] == 4 and not fleet_actions._busy
    # the rate limit (patched to 2 per window): the third action in the window is refused
    monkeypatch.setattr(fleet_actions, "RATE_N", 2)
    fleet_actions._runs.clear()
    assert execute(fleet, plan(fleet, "Set Echo to 5 days of recording")["id"]).status_code == 200
    assert execute(fleet, plan(fleet, "Set Echo to 6 days of recording")["id"]).status_code == 200
    r = execute(fleet, plan(fleet, "Set Echo to 7 days of recording")["id"])
    assert r.status_code == 429 and "at most 2 in 10 minutes" in r.json()["detail"] and e["days"] == 6
    assert _last_action(fleet)["status"] == 429
    fleet_actions._runs.clear()
    assert execute(fleet, plan(fleet, f"Set Echo to {days} days of recording")["id"]).json()["ok"] and e["days"] == days


def test_failed_execution_is_logged(fleet):
    p = plan(fleet, "Add 10.5.0.77 to Echo as Spare Two")
    r = execute(fleet, p["id"])       # no password typed: the site refuses
    assert r.status_code == 200 and not r.json()["ok"]
    row = _audit(fleet, r.json()["audit_id"])
    assert row["status"] == 500 and row["detail"]["outcome"] == "failed" and "password" in row["detail"]["reason"]
    assert "reverse" not in row["detail"] and fleet_actions.undo_until(row) is None
    log_row = next(x for x in fleet.owner.get(f"/api/orgs/{fleet.org['id']}/actions/reference").json()["recent"] if x["id"] == row["id"])
    assert log_row["outcome"] == "failed" and "password" in log_row["reason"] and log_row["servers"] == ["Echo"] and not log_row["can_undo"]


def test_operator_sees_and_undoes_only_their_own(fleet):
    org = fleet.org["id"]
    uid, op = _member(fleet, "operator@actions.example", "operator")
    e = fleet.apps["Echo"].st
    p = plan(fleet, "Lock Lobby footage 1-2 pm yesterday", client=op)
    assert p["allowed"] and p["parser"] == "rules"
    r = execute(fleet, p["id"], client=op)
    assert r.status_code == 200 and r.json()["ok"], r.text
    mine = op.get(f"/api/orgs/{org}/actions/reference").json()
    assert mine["log_scope"] == "own" and [x["user_email"] for x in mine["recent"]] == ["operator@actions.example"]
    assert mine["recent"][0]["can_undo"] and mine["recent"][0]["outcome"] == "done"
    # an operator cannot run an admin verb (refused and logged in their own log), nor undo someone else's action
    p2 = plan(fleet, "Set Echo to 3 days of recording", client=op)
    assert p2["allowed"] is False and execute(fleet, p2["id"], client=op).status_code == 403
    owners = fleet.owner.get(f"/api/orgs/{org}/actions/reference").json()["recent"]
    theirs = next(x for x in owners if x["can_undo"] and x["user_email"] == "root@example.com")
    assert op.post(f"/api/orgs/{org}/actions/undo/{theirs['id']}").status_code == 403
    mine = op.get(f"/api/orgs/{org}/actions/reference").json()["recent"]
    assert {x["outcome"] for x in mine} == {"done", "refused"} and all(x["user_email"] == "operator@actions.example" for x in mine)
    # their own lock they can undo; the original shows who undid it, the undo row points back to it
    r2 = op.post(f"/api/orgs/{org}/actions/undo/{r.json()['audit_id']}")
    assert r2.status_code == 200 and r2.json()["ok"] and not e["locks"]
    orig = _audit(fleet, r.json()["audit_id"])
    assert orig["detail"]["undone_by"] == "operator@actions.example" and orig["detail"]["undo_attempts"][-1]["ok"]
    assert _audit(fleet, r2.json()["audit_id"])["detail"]["undo_of"] == orig["id"]
    # an undo that can no longer run is refused and logged too
    assert op.post(f"/api/orgs/{org}/actions/undo/{orig['id']}").status_code == 409
    last = _last_action(fleet)
    assert last["detail"]["outcome"] == "refused" and last["detail"]["undo_of"] == orig["id"] and last["action"].startswith("fleet action: Undo:")


def test_monitored_site_notes(fleet):
    org, echo, delta = fleet.org["id"], fleet.sites["Echo"], fleet.sites["Delta"]
    t = time.time()
    lid = db.new_id("l_")
    db.insert(db.locations, {"id": lid, "org_id": org, "name": "Harbor", "address": "", "timezone": None, "notes": None,
                             "created_at": t, "updated_at": t, "monitored": True})
    db.run(sa.update(db.sites).where(db.sites.c.id == echo).values(location_id=lid))
    note = "Harbor is monitored by the SOC; arming, contacts and procedures stay with the Site."
    try:
        p = plan(fleet, "Quiet alerts at Echo for 2 hours")
        assert note in p["card"]["stays"], p["card"]
        p = plan(fleet, "Retire Echo")
        assert note in p["card"]["stays"] and any("Harbor is monitored by the SOC and would be left with no cameras" in w for w in p["card"]["warnings"])
        p = plan(fleet, "Migrate Echo to Delta")
        assert note in p["card"]["stays"] and any("left with no cameras" in w for w in p["card"]["warnings"])
        # a second server at the Site: retiring one is no longer a warning
        db.run(sa.update(db.sites).where(db.sites.c.id == delta).values(location_id=lid))
        p = plan(fleet, "Retire Delta")
        assert note in p["card"]["stays"] and not any("no cameras" in w for w in p["card"]["warnings"])
        # not monitored: no note
        db.run(sa.update(db.locations).where(db.locations.c.id == lid).values(monitored=False))
        assert not any("SOC" in s for s in plan(fleet, "Retire Delta")["card"]["stays"])
    finally:
        db.run(sa.update(db.sites).where(db.sites.c.id.in_([echo, delta])).values(location_id=None))
        db.run(sa.delete(db.locations).where(db.locations.c.id == lid))
