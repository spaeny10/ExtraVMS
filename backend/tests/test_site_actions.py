"""Site actions (site_actions.py): the site's Ask box takes "Rename cam1 to Loading Dock", "Set retention to 9 days",
"Stop describing vehicles on cam1" and "Lock Front Door footage 3-4 pm yesterday" as instructions with a confirmation
card, through POST /api/assistant/plan and /execute, with the hub's role checked per verb.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_site_actions.py   (from backend/)
"""
import asyncio
import datetime as dt
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-site-actions-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from nvr import hub_agent, keep, site_actions  # noqa: E402
from nvr.api import app  # noqa: E402
from nvr.db import db  # noqa: E402

LAN = ("192.168.1.9", 5000)
CAM = {"id": "cam1", "name": "Front Door", "host": "10.0.0.5", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": "pw-" + "y" * 6,
       "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": [], "retention_days": None, "scene_notes": "",
       "retention_policy": None, "synopsis_labels": ["person", "vehicle"], "policies": []}


def post(path, body, client=LAN, headers=None):
    async def go():
        transport = httpx.ASGITransport(app=app, client=client)
        async with httpx.AsyncClient(transport=transport, base_url="http://site") as c:
            return await c.post(path, json=body, headers=headers or {})
    return asyncio.run(go())


def plan(text, **kw):
    r = post("/api/assistant/plan", {"text": text}, **kw)
    assert r.status_code == 200, r.text
    return r.json()


def test_questions_are_not_actions():
    db.upsert_camera(CAM)
    db.upsert_camera({**CAM, "id": "cam2", "name": "Side Yard", "host": "10.0.0.6"})
    for q in ("how many people today?", "Did anyone lock the gate?", "show me the front door", "what happened overnight"):
        assert plan(q) == {"action": "none"}, q
    assert plan("hello there") == {"action": "none"}


def test_rename_and_retention():
    p = plan("Rename cam1 to Loading Dock")
    assert p["action"] == "rename_camera" and p["allowed"] and p["card"]["can_execute"] and "becomes \"Loading Dock\"" in p["card"]["moves"][0]
    assert db.one("SELECT name FROM cameras WHERE id='cam1'")["name"] == "Front Door"   # a plan changes nothing
    r = post("/api/assistant/execute", {"plan_id": p["id"]})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert db.one("SELECT name FROM cameras WHERE id='cam1'")["name"] == "Loading Dock"
    assert post("/api/assistant/execute", {"plan_id": p["id"]}).status_code == 404   # runs once
    p = plan("please set retention to 9 days")
    assert p["action"] == "set_retention" and p["card"]["can_execute"]
    assert post("/api/assistant/execute", {"plan_id": p["id"]}).json()["ok"]
    assert keep.site_policy()["continuous_days"] == 9
    p = plan("Rename the porch swing to Gate")
    assert not p["card"]["can_execute"] and "no camera called" in p["card"]["needs"][0]
    assert post("/api/assistant/execute", {"plan_id": p["id"]}).status_code == 409


def test_synopsis_labels():
    p = plan("Stop describing vehicles on Loading Dock")
    assert p["action"] == "set_synopsis_labels" and p["labels"] == ["person"] and p["card"]["can_execute"]
    assert post("/api/assistant/execute", {"plan_id": p["id"]}).json()["ok"]
    assert db.cameras()[0]["synopsis_labels"] == ["person"]
    p = plan("describe only people on cam1")   # already so
    assert not p["card"]["can_execute"] and "already" in p["card"]["needs"][0]


def test_lock_footage():
    p = plan("Lock Side Yard footage 3-4 pm yesterday")
    assert p["action"] == "lock_footage" and p["card"]["can_execute"], p
    y = dt.date.today() - dt.timedelta(days=1)
    assert p["start_ts"] == dt.datetime.combine(y, dt.time(15, 0)).timestamp() and p["end_ts"] - p["start_ts"] == 3600
    res = post("/api/assistant/execute", {"plan_id": p["id"]}).json()
    assert res["ok"]
    lock = db.one("SELECT * FROM locks WHERE camera_id='cam2'")
    assert lock and lock["start_ts"] == p["start_ts"] and lock["end_ts"] == p["end_ts"]
    p = plan("lock Side Yard footage 11pm-11:30pm today")
    if dt.datetime.now().hour < 23:
        assert "hasn't happened" in " ".join(p["card"]["needs"])
    assert site_actions.parse_range("11-1 pm today")[2] == "11:00-13:00 today"


def test_roles_from_the_hub():
    hub = hub_agent.IN_PROCESS_CLIENT
    p = plan("Lock Side Yard footage 1-2 pm yesterday", client=hub, headers={"x-hub-role": "viewer", "x-hub-user": "v@example"})
    assert p["allowed"] is False
    assert post("/api/assistant/execute", {"plan_id": p["id"]}, client=hub, headers={"x-hub-role": "viewer"}).status_code == 403
    r = post("/api/assistant/execute", {"plan_id": p["id"]}, client=hub, headers={"x-hub-role": "operator", "x-hub-user": "op@example"})
    assert r.status_code == 200 and "op@example" in db.all("SELECT note FROM locks ORDER BY id DESC")[0]["note"]
    # an operator may lock footage but not rename cameras (that needs admin, as PUT /api/cameras does)
    p = plan("Rename cam2 to Yard", client=hub, headers={"x-hub-role": "operator"})
    assert p["allowed"] is False
    assert post("/api/assistant/execute", {"plan_id": p["id"]}, client=hub, headers={"x-hub-role": "operator"}).status_code == 403
    # the LAN cannot claim a hub role: the middleware strips x-hub-* (no role = the site's own page)
    p = plan("Rename cam2 to Yard", headers={"x-hub-role": "viewer"})
    assert p["allowed"] is True


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
