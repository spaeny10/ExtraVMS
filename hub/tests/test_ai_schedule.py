"""A Site's AI schedule (ai_schedule.py): full time by default for every Site; only hub administrators change it (customer
admins read it, nobody else does), every change is audited and pushed at once to the Site's connected servers as an
`ai_schedule` frame (with the SOC windows and the timezone); `welcome` carries it; a heartbeat reporting another key gets
it again (at most once per REPUSH_S); monitoring changes push too. The servers' `ai_usage` reports are stored as daily
rows per server (a repeat replaces, bad rows are dropped, old ones pruned), summed per Site for this month and last, and
billed per Site at GET /api/hub/ai-usage (JSON and CSV, hub administrators only). The server's copy of the schedule rules
(backend nvr/aischedule.py) agrees with soc.compute_armed and the hub's key."""
import asyncio
import csv
import datetime as dt
import io
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from hub import agents, ai_schedule, db, hosts, soc
from hub.agents import AgentConn, registry
from test_access import _login, server

PASS = "ai-sched-pass-123"
URL = "/api/locations/{}/ai"
CHI = ZoneInfo("America/Chicago")
WEEKNIGHTS = [{"dow": [0, 1, 2, 3, 4], "from": "18:00", "to": "06:00"}]


class FrameConn:
    """A connected server: remembers every frame the hub sends it."""

    def __init__(self, s: dict, summary: dict | None = None):
        self.site_id, self.site, self.summary = s["id"], s, summary
        self.location = db.one(sa.select(db.locations).where(db.locations.c.id == s["location_id"])) if s.get("location_id") else None
        self.frames: list[dict] = []

    async def send(self, frame: dict) -> None:
        self.frames.append(frame)

    def pushed(self) -> list[dict]:
        return [f["ai_schedule"] for f in self.frames if f.get("t") == "ai_schedule"]


def _summary(s: dict, summary: dict) -> None:
    db.run(sa.update(db.sites).where(db.sites.c.id == s["id"]).values(summary=summary))


def _reported(key: str | None = "old") -> dict:
    return {"ai_schedule": {"key": key, "mode": "full", "qwen": "on", "reason": "default", "until": None, "outside": "catchup"}}


def _wait(cond, timeout: float = 3.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


@pytest.fixture(scope="module")
def world(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": "AI Sched Co", "slug": "ai-sched-co"}).json()["id"]
    yard = root.post(f"/api/orgs/{oid}/locations", json={"name": "AI Yard"}).json()
    db.run(sa.update(db.locations).where(db.locations.c.id == yard["id"]).values(timezone="America/Chicago"))
    a, b = server(oid, "Alpha", yard["id"]), server(oid, "Bravo (old)", yard["id"])
    other = server(oid, "Elsewhere")
    _summary(a, _reported())
    _summary(b, {"speakers": []})   # an older server: no ai_schedule in its summary
    for s in (a, b, other):
        s.update(db.one(sa.select(db.sites).where(db.sites.c.id == s["id"])))
    conns = {"a": FrameConn(a, _reported()), "b": FrameConn(b, {"speakers": []}), "other": FrameConn(other, _reported())}
    for conn in conns.values():
        registry.by_site[conn.site_id] = conn
    users = {}
    for role in ("viewer", "operator", "admin"):
        email = f"{role}@ai-sched.example"
        r = root.post(f"/api/orgs/{oid}/members", json={"email": email, "role": role, "password": PASS})
        assert r.status_code == 200, r.text
        users[role] = _login(client, email, PASS)
    foreign = root.post("/api/orgs", json={"name": "Other AI Co", "slug": "other-ai-co"}).json()["id"]
    root.post(f"/api/orgs/{foreign}/members", json={"email": "fx@ai-sched.example", "role": "admin", "password": PASS})
    yield {"root": root, "oid": oid, "yard": yard, "a": a, "b": b, "other": other, "conns": conns, **users,
           "outsider": _login(client, "fx@ai-sched.example", PASS)}
    for conn in conns.values():
        registry.by_site.pop(conn.site_id, None)


def _audits(oid: str) -> list[dict]:
    return db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id == oid, db.audit_log.c.action.like("AI schedule%"))
                   .order_by(db.audit_log.c.id))


def test_default_is_full_time_and_who_reads_it(world):
    url = URL.format(world["yard"]["id"])
    loc = db.one(sa.select(db.locations).where(db.locations.c.id == world["yard"]["id"]))
    assert loc["ai_mode"] is None and loc["ai_outside"] is None   # an existing Site: nothing stored, full time
    j = world["admin"].get(url).json()
    assert j["mode"] == "full" and j["outside"] == "catchup" and j["can_edit"] is False
    assert j["effective"] == {"mode": "full", "outside": "catchup", "qwen": "on", "reason": "full_time", "until": None}
    by = {s["server_name"]: s for s in j["servers"]}
    assert list(by) == ["Alpha", "Bravo (old)"] and by["Alpha"]["supported"] and not by["Bravo (old)"]["supported"]
    assert not by["Alpha"]["in_sync"]   # it reported another key
    assert j["usage"]["this_month"]["total"]["seconds"] == 0 and j["usage"]["last_month"]["kinds"] == []
    assert world["root"].get(url).json()["can_edit"] is True
    for c in (world["viewer"], world["operator"], world["outsider"]):
        assert c.get(url).status_code == 403
    # the welcome's schedule for a server without a Site is full time too
    assert ai_schedule.config(None)["mode"] == "full"


def test_customer_admin_cannot_change_the_mode(world):
    url = URL.format(world["yard"]["id"])
    for c in (world["admin"], world["operator"], world["viewer"], world["outsider"]):
        assert c.put(url, json={"mode": "soc", "outside": "skip"}).status_code == 403
    loc = db.one(sa.select(db.locations).where(db.locations.c.id == world["yard"]["id"]))
    assert loc["ai_mode"] is None and not _audits(world["oid"]) and not world["conns"]["a"].pushed()
    assert world["root"].put(url, json={"mode": "sometimes"}).status_code == 422
    assert world["root"].put(url, json={"mode": "soc", "outside": "later"}).status_code == 422


def test_hub_admin_sets_it_pushes_and_audits(world):
    url = URL.format(world["yard"]["id"])
    db.run(sa.update(db.locations).where(db.locations.c.id == world["yard"]["id"]).values(monitored=True, arm_schedule=WEEKNIGHTS))
    r = world["root"].put(url, json={"mode": "soc", "outside": "skip"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["mode"] == "soc" and j["outside"] == "skip" and j["effective"]["mode"] == "soc"
    assert j["effective"]["qwen"] in ("on", "limited") and j["effective"]["until"] is not None
    conns = world["conns"]
    assert sorted(j["pushed"]) == sorted([world["a"]["id"], world["b"]["id"]])   # every connected server of the Site
    assert not conns["other"].frames                                               # another Site's server: nothing
    cfg = conns["a"].pushed()[-1]
    assert cfg["mode"] == "soc" and cfg["outside"] == "skip" and cfg["timezone"] == "America/Chicago" and cfg["monitored"]
    assert cfg["arm_schedule"] == WEEKNIGHTS and cfg["key"] == ai_schedule.config(db.one(sa.select(db.locations).where(
        db.locations.c.id == world["yard"]["id"])))["key"]
    rows = _audits(world["oid"])
    assert [x["action"] for x in rows] == ["AI schedule set: SOC hours (skip descriptions outside them) (AI Yard)"]
    assert rows[0]["detail"]["before"] == {"mode": "full", "outside": "catchup"} and rows[0]["user_email"] == "root@example.com"
    # the same again: pushed again, not audited again; the customer admin now reads it
    assert world["root"].put(url, json={"mode": "soc", "outside": "skip"}).status_code == 200
    assert len(_audits(world["oid"])) == 1
    seen = world["admin"].get(url).json()
    assert seen["mode"] == "soc" and seen["outside"] == "skip" and seen["can_edit"] is False
    # outside left out keeps the option; back to catch-up, then full time (stored as NULL again)
    assert world["root"].put(url, json={"mode": "soc"}).json()["outside"] == "skip"
    assert world["root"].put(url, json={"mode": "soc", "outside": "catchup"}).json()["outside"] == "catchup"
    assert len(_audits(world["oid"])) == 2


def test_effective_state_follows_the_soc_schedule():
    loc = {"id": "l_x", "ai_mode": "soc", "monitored": True, "timezone": "America/Chicago", "arm_schedule": WEEKNIGHTS,
           "arm_holidays": [], "arm_override": None}
    night = dt.datetime(2026, 10, 5, 23, 0, tzinfo=CHI).timestamp()
    noon = dt.datetime(2026, 10, 6, 12, 0, tzinfo=CHI).timestamp()
    on = ai_schedule.effective(loc, night)
    assert on["qwen"] == "on" and on["until"] == dt.datetime(2026, 10, 6, 6, 0, tzinfo=CHI).timestamp()
    off = ai_schedule.effective(loc, noon)
    assert off["qwen"] == "limited" and off["until"] == dt.datetime(2026, 10, 6, 18, 0, tzinfo=CHI).timestamp()
    assert ai_schedule.effective({**loc, "monitored": False}, night)["qwen"] == "limited"   # never armed: never full
    assert ai_schedule.effective({**loc, "ai_mode": None}, noon)["qwen"] == "on"


def test_welcome_carries_the_schedule(world):
    sent: list = []
    ws = SimpleNamespace(client=SimpleNamespace(host="203.0.113.9"), send_text=lambda t: _append(sent, t), close=_noop)
    conn = AgentConn(ws, world["a"], token="t")
    registry.by_site.pop(world["a"]["id"], None)   # else the "replaced by a newer connection" close hits the fake
    try:
        asyncio.run(registry._on_hello(conn, {"t": "hello", "hostname": "alpha", "site_version": "x"}))
    finally:
        registry.by_site[world["a"]["id"]] = world["conns"]["a"]
    from tunnelproto import decode
    welcome = decode(sent[0])
    assert welcome["t"] == "welcome"
    loc = db.one(sa.select(db.locations).where(db.locations.c.id == world["yard"]["id"]))
    assert welcome["ai_schedule"] == ai_schedule.config(loc) and welcome["ai_schedule"]["mode"] == "soc"


async def _append(lst, t):
    lst.append(t)


async def _noop(*a, **k):
    return None


def _beat(conn, summary: dict) -> None:
    async def go():
        conn.summary = summary
        agents._ai_schedule(conn)
        if ai_schedule._tasks:
            await asyncio.gather(*list(ai_schedule._tasks))
    asyncio.run(go())


def test_heartbeat_repush(world, monkeypatch):
    a = FrameConn(world["a"])
    registry.by_site[a.site_id] = a
    world["conns"]["a"] = a
    ai_schedule._last.pop(a.site_id, None)
    want = ai_schedule.config(a.location)["key"]
    _beat(a, _reported("stale"))
    assert len(a.pushed()) == 1 and a.pushed()[0]["key"] == want
    _beat(a, _reported("stale"))   # not again within REPUSH_S
    assert len(a.pushed()) == 1
    t0 = ai_schedule._last[a.site_id][1]
    monkeypatch.setattr(ai_schedule.time, "time", lambda: t0 + ai_schedule.REPUSH_S + 1)
    _beat(a, _reported("stale"))
    assert len(a.pushed()) == 2
    monkeypatch.undo()
    _beat(a, _reported(want))      # in step: nothing
    assert len(a.pushed()) == 2
    b = world["conns"]["b"]
    n = len(b.frames)
    _beat(b, {"speakers": []})     # too old to report one: left alone
    assert len(b.frames) == n
    # the Site's monitoring changes (a SOC supervisor or customer admin): its servers get the new windows at once
    url = f"/api/locations/{world['yard']['id']}/monitoring"
    r = world["admin"].put(url, json={"arm_schedule": [{"dow": [5, 6], "from": "00:00", "to": "00:00"}]})
    assert r.status_code == 200, r.text
    assert _wait(lambda: any(p["arm_schedule"] == [{"dow": [5, 6], "from": "00:00", "to": "00:00"}] for p in a.pushed()))
    # ...and its timezone
    world["admin"].patch(f"/api/locations/{world['yard']['id']}", json={"timezone": "America/Denver"})
    assert _wait(lambda: a.pushed()[-1]["timezone"] == "America/Denver")
    world["admin"].patch(f"/api/locations/{world['yard']['id']}", json={"timezone": "America/Chicago"})


def _rows(day: str, *items) -> list[dict]:
    return [{"day": day, "kind": k, "sched": s, "requests": n, "prompt_tokens": n * 1000, "completion_tokens": n * 100, "seconds": sec}
            for k, s, n, sec in items]


def test_usage_rollup_per_site(world, monkeypatch):
    t = db.ai_usage_daily
    db.run(sa.delete(t))
    today = ai_schedule.utc_today()
    this = today.isoformat()
    last_month_day = (today.replace(day=1) - dt.timedelta(days=1)).isoformat()
    a, b, other = world["conns"]["a"], world["conns"]["b"], world["conns"]["other"]
    report = _rows(this, ("synopsis", "in", 10, 50.0), ("catchup", "out", 4, 20.0), ("ppe", "out", 3, 3.0)) \
        + _rows(last_month_day, ("ask", "in", 2, 8.0))
    bad = [{"day": "2026-13-99", "kind": "synopsis", "sched": "in", "requests": 1, "seconds": 1},
           {"day": this, "kind": "DROP TABLE", "sched": "in", "requests": 1, "seconds": 1},
           {"day": this, "kind": "ask", "sched": "maybe", "requests": 1, "seconds": 1},
           {"day": this, "kind": "ask", "sched": "in", "requests": -1, "seconds": 1},
           {"day": "1999-01-01", "kind": "ask", "sched": "in", "requests": 1, "seconds": 1}, "nonsense"]
    assert ai_schedule.on_usage(a, {"t": "ai_usage", "rows": report + bad}) == 4
    assert ai_schedule.on_usage(a, {"t": "ai_usage", "rows": report}) == 4    # a repeat replaces: no double counting
    assert ai_schedule.on_usage(b, {"t": "ai_usage", "rows": _rows(this, ("synopsis", "in", 1, 5.0))}) == 1
    assert ai_schedule.on_usage(other, {"t": "ai_usage", "rows": _rows(this, ("synopsis", "in", 7, 70.0))}) == 1
    assert ai_schedule.on_usage(SimpleNamespace(site=None), {"rows": report}) == 0   # unenrolled: nothing
    assert len(db.rows(sa.select(t).where(t.c.server_id == a.site_id))) == 4
    # a later report with a bigger total for the day replaces it
    ai_schedule.on_usage(a, {"rows": _rows(this, ("synopsis", "in", 12, 60.0))})
    u = world["admin"].get(URL.format(world["yard"]["id"])).json()["usage"]
    cur = u["this_month"]
    assert cur["total"]["seconds"] == 60.0 + 20.0 + 3.0 + 5.0 and cur["total"]["requests"] == 12 + 4 + 3 + 1
    assert cur["out"]["seconds"] == 23.0 and cur["in"]["seconds"] == 65.0
    kinds = {k["kind"]: k for k in cur["kinds"]}
    assert [k["kind"] for k in cur["kinds"]] == ["synopsis", "catchup", "ppe"]   # the UI's order
    assert kinds["synopsis"]["seconds"] == 65.0 and kinds["catchup"]["out_seconds"] == 20.0
    assert kinds["synopsis"]["prompt_tokens"] == 13000
    if last_month_day[:7] != this[:7]:
        assert u["last_month"]["total"]["seconds"] == 8.0 and u["last_month"]["kinds"][0]["kind"] == "ask"
    # the Hosts page's series: zeros filled in, oldest first, today last
    days = ai_schedule.server_days([a.site_id, b.site_id], 7)
    assert len(days[a.site_id]["days"]) == 7 and days[a.site_id]["days"][-1]["day"] == this
    assert days[a.site_id]["today_s"] == 83.0 and days[b.site_id]["today_s"] == 5.0
    assert ai_schedule.sum_days([days[a.site_id], days[b.site_id], None])["today_s"] == 88.0 and ai_schedule.sum_days([]) is None
    # 13 months are kept, older rows pruned
    old = (today - dt.timedelta(days=ai_schedule.KEEP_DAYS + 5)).isoformat()
    keep = (today - dt.timedelta(days=396)).isoformat()
    db.insert(t, {"server_id": a.site_id, "day": old, "kind": "synopsis", "sched": "in", "location_id": world["yard"]["id"],
                  "org_id": world["oid"], "requests": 1, "prompt_tokens": 0, "completion_tokens": 0, "seconds": 1.0, "updated_at": 0})
    db.insert(t, {"server_id": a.site_id, "day": keep, "kind": "synopsis", "sched": "in", "location_id": world["yard"]["id"],
                  "org_id": world["oid"], "requests": 1, "prompt_tokens": 0, "completion_tokens": 0, "seconds": 1.0, "updated_at": 0})
    assert ai_schedule.prune(force=True) == 1
    assert {r["day"] for r in db.rows(sa.select(t.c.day).where(t.c.server_id == a.site_id))} >= {keep}


def test_billing_json_and_csv(world):
    month = ai_schedule.month_of()
    for c in (world["admin"], world["viewer"], world["outsider"]):
        assert c.get(f"/api/hub/ai-usage?month={month}").status_code == 403
    assert world["root"].get("/api/hub/ai-usage?month=2026-13").status_code == 422
    j = world["root"].get(f"/api/hub/ai-usage?month={month}").json()
    assert j["month"] == month
    mine = [s for s in j["sites"] if s["org_id"] == world["oid"]]
    by = {s["location_name"]: s for s in mine}
    yard = by["AI Yard"]
    assert yard["seconds"] == 88.0 and yard["seconds_out"] == 23.0 and yard["ai_mode"] == "soc" and yard["ai_outside"] == "catchup"
    assert yard["kinds"]["synopsis"] == 65.0 and by["Elsewhere"]["seconds"] == 70.0 and by["Elsewhere"]["ai_mode"] == "full"
    assert world["root"].get("/api/hub/ai-usage").json()["month"] == month   # default: this month
    r = world["root"].get(f"/api/hub/ai-usage?month={month}&format=csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert f"axiom-ai-usage-{month}.csv" in r.headers["content-disposition"]
    table = list(csv.DictReader(io.StringIO(r.text)))
    row = next(x for x in table if x["location_name"] == "AI Yard")
    assert row["seconds"] == "88.0" and row["seconds_catchup"] == "20.0" and row["ai_mode"] == "soc" and row["requests"] == "20"
    assert world["root"].get("/api/hub/ai-usage?month=1990-01").json()["sites"] == []


def test_hosts_page_carries_qwen_seconds(world):
    # an instance row for Alpha's server: its Qwen series rides along for hub administrators only
    now = time.time()
    hid = db.new_id("h_")
    db.insert(db.hosts, {"id": hid, "name": "ai-host", "token_hash": db.token_hash(db.new_token()), "created_at": now,
                         "online": False, "capacity": None})
    ci = {"id": db.new_id("ci_"), "host_id": hid, "location_id": world["yard"]["id"], "org_id": world["oid"],
          "server_id": world["a"]["id"], "name": "Central", "mode": "vpn", "subnet": "10.20.250.0/24", "public_ip": None,
          "site_number": None, "quota_gb": 10, "gpu": None, "state": "running", "created_at": now, "info": None}
    db.insert(db.central_instances, {k: v for k, v in ci.items() if k in db.central_instances.c})
    try:
        row = next(i for i in hosts.instances() if i["id"] == ci["id"])
        assert row["qwen"]["today_s"] == 83.0 and len(row["qwen"]["days"]) == hosts.QWEN_DAYS
        assert "qwen" not in next(i for i in hosts.instances(hub_admin=False) if i["id"] == ci["id"])
        h = next(x for x in hosts.list_hosts() if x["id"] == hid)
        assert h["qwen"]["today_s"] == 83.0
    finally:
        db.run(sa.delete(db.central_instances).where(db.central_instances.c.id == ci["id"]))
        db.run(sa.delete(db.hosts).where(db.hosts.c.id == hid))


def test_server_copy_of_the_rules_agrees_with_the_hub():
    from nvr import aischedule as nvr_ai
    loc = {"id": "l_x", "ai_mode": "soc", "ai_outside": "skip", "monitored": True, "timezone": "America/Chicago",
           "arm_schedule": WEEKNIGHTS + [{"dow": [5], "from": "22:00", "to": "02:00"}],
           "arm_holidays": [{"date": "2026-11-26", "name": "Thanksgiving", "armed": True},
                            {"date": "2026-12-24", "name": "Eve", "armed": True, "from": "15:00", "to": "23:00"}],
           "arm_override": {"mode": "disarm", "until": dt.datetime(2026, 10, 6, 1, 0, tzinfo=CHI).timestamp(), "by": "x"}}
    cfg = ai_schedule.config(loc)
    mine = nvr_ai.clean(cfg)
    assert mine["key"] == cfg["key"] == nvr_ai.key(cfg)   # the server reports back exactly the hub's key
    start = dt.datetime(2026, 10, 5, 0, 0, tzinfo=CHI).timestamp()
    for step in range(0, 70 * 24 * 4):                     # every 15 minutes over ten weeks: DST end and both holidays
        now = start + step * 900
        assert nvr_ai.compute_armed(mine, now) == soc.compute_armed(loc, now), now
    for now in (start, start + 3 * 86400 + 3600, dt.datetime(2026, 11, 25, 12, 0, tzinfo=CHI).timestamp()):
        assert nvr_ai.next_change(mine, now) == soc.next_change(loc, now)
