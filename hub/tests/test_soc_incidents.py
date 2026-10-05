"""The SOC incident model (soc.ingest and the feed) and ownership through the routes: grouping within the Site's
window, priority only rising (quiet -> ring restarts the claim clock), idempotent re-publishes, nothing from a
disarmed or unmonitored Site or a disabled camera, and claim / release / takeover / handoff / resolve / verify /
sweep / promote as conditional updates with the 409 a lost race gets."""
import time
from types import SimpleNamespace

import sqlalchemy as sa

from hub import auth, cameras, db, soc
from test_access import _login, server
from test_soc_roles import PW, soc_setup


def rows(loc_id):
    return db.rows(sa.select(db.incidents).where(db.incidents.c.location_id == loc_id).order_by(db.incidents.c.id))


def ctx(s):
    srv = db.one(sa.select(db.sites).where(db.sites.c.id == s["srv"]["id"]))
    loc = db.one(sa.select(db.locations).where(db.locations.c.id == s["loc"]["id"]))
    cameras.sync(srv, [{"id": "cam1", "name": "Gate"}, {"id": "cam2", "name": "Dock"}], full=True)
    return srv, loc


def ev(i, prio="medium", cam="cam1", **kw):
    return {"id": i, "camera_id": cam, "priority": prio, "label": "person", "start_ts": time.time(), "synopsis": f"person {i}", **kw}


def actions(iid):
    return [r["action"] for r in soc.log_of(iid)[iid]]


def test_grouping_window_and_priority_only_rises(client, superuser):
    s = soc_setup(client, superuser, "group")
    srv, loc = ctx(s)
    t0 = time.time()
    what, inc = soc.ingest(srv, loc, ev(1, "medium"), now=t0)
    assert what == "opened" and inc["state"] == "new" and inc["lane"] == "ring" and inc["priority"] == "medium"
    assert inc["sla_due_at"] == t0 + 180 and inc["next_escalation_at"] == t0 + 180 and inc["escalation_level"] == 0
    assert inc["event_count"] == 1 and inc["title"] == "Gate · person: person 1"
    assert inc["org_name"] == "group Mon" and inc["location_name"] == "Yard" and inc["location_timezone"] == "America/Chicago"
    assert inc["servers"] == [{"id": srv["id"], "name": "Yard NVR"}]
    assert inc["cameras"] == [{"server_id": srv["id"], "camera_id": "cam1", "name": "Gate"}]

    what, inc2 = soc.ingest(srv, loc, ev(2, "low", cam="cam2"), now=t0 + 300)   # within 10 min of the last event
    assert what == "event_added" and inc2["id"] == inc["id"] and inc2["priority"] == "medium" and inc2["event_count"] == 2
    assert {c["camera_id"] for c in inc2["cameras"]} == {"cam1", "cam2"}
    what, inc3 = soc.ingest(srv, loc, ev(3, "high"), now=t0 + 400)
    assert what == "event_added" and inc3["priority"] == "high" and inc3["event_count"] == 3
    assert inc3["sla_due_at"] == t0 + 180   # a rise inside the ringing lane only tightens the clock (t0+460 is later)
    assert actions(inc["id"]) == ["opened", "event_added", "event_added", "priority_raised"]
    pr = soc.log_of(inc["id"])[inc["id"]][-1]
    assert pr["detail"] == {"from": "medium", "to": "high", "lane": "ring"} and pr["user_id"] is None

    # the window counts from the incident's last event: 10 min after t0+400 is a new incident
    what, inc4 = soc.ingest(srv, loc, ev(4, "medium"), now=t0 + 400 + 601)
    assert what == "opened" and inc4["id"] != inc["id"]
    # a Site's own window (soc_group_minutes) wins over the default
    what, inc5 = soc.ingest(srv, {**loc, "soc_group_minutes": 30}, ev(5, None), now=t0 + 400 + 601 + 1500)
    assert what == "event_added" and inc5["id"] == inc4["id"]
    assert len(rows(loc["id"])) == 2


def test_quiet_lane_and_promotion_by_priority(client, superuser):
    s = soc_setup(client, superuser, "quiet")
    srv, loc = ctx(s)
    t0 = time.time()
    what, inc = soc.ingest(srv, loc, ev(1, "none"), now=t0)   # "none" from the site is low: the quiet lane, no clock
    assert inc["priority"] == "low" and inc["lane"] == "quiet" and inc["sla_due_at"] is None
    db.run(sa.update(db.incidents).where(db.incidents.c.id == inc["id"]).values(escalation_level=2))
    what, inc = soc.ingest(srv, loc, ev(2, "high"), now=t0 + 30)
    assert inc["lane"] == "ring" and inc["priority"] == "high" and inc["sla_due_at"] == t0 + 30 + 60
    assert inc["escalation_level"] == 0 and inc["next_escalation_at"] == t0 + 90


def test_idempotent_republish(client, superuser):
    s = soc_setup(client, superuser, "repub")
    srv, loc = ctx(s)
    t0 = time.time()
    e = ev(7, "low", synopsis="")
    soc.ingest(srv, loc, e, now=t0)
    assert soc.ingest(srv, loc, e, now=t0 + 5) is None   # the same event again: nothing
    inc = rows(loc["id"])[0]
    assert inc["event_count"] == 1 and inc["title"] == "Gate · person"
    # a re-publish that brings the synopsis and a higher priority updates the open incident, still one event row
    what, inc = soc.ingest(srv, loc, {**e, "priority": "medium", "synopsis": "a man at the gate"}, now=t0 + 10)
    assert what == "updated" and inc["event_count"] == 1 and inc["priority"] == "medium" and inc["lane"] == "ring"
    assert inc["title"] == "Gate · person: a man at the gate"
    evs = db.rows(sa.select(db.incident_events).where(db.incident_events.c.incident_id == inc["id"]))
    assert len(evs) == 1 and evs[0]["priority"] == "medium" and evs[0]["event_id"] == "7"
    # once resolved, re-publishes of its events change nothing and open nothing
    db.run(sa.update(db.incidents).where(db.incidents.c.id == inc["id"]).values(state="closed"))
    assert soc.ingest(srv, loc, {**e, "priority": "high"}, now=t0 + 20) is None
    assert len(rows(loc["id"])) == 1


def test_feed_filters(client, superuser):
    s = soc_setup(client, superuser, "feed")
    srv, loc = ctx(s)
    conn = SimpleNamespace(site=srv, location=loc, site_id=srv["id"])

    def msg(i, **kw):
        return {"type": "event", "event": {"id": i, "camera_id": "cam1", "camera_class": "person", "start_ts": time.time(),
                                           "status": "verified", "priority": "high", "synopsis": "x", **kw}}
    assert soc.on_event(conn, msg(1, status="open")) is None                         # not verified yet
    assert soc.on_event(conn, msg(2, feedback='{"verdict": "false_alarm"}')) is None  # marked a false alarm at the site
    assert soc.on_event(conn, msg(3, start_ts=time.time() - 3600)) is None            # an old event re-published
    assert soc.on_event(SimpleNamespace(site=srv, location={**loc, "monitored": False}, site_id=srv["id"]), msg(4)) is None
    disarmed = {**loc, "arm_override": {"mode": "disarm", "until": time.time() + 600}}
    assert soc.on_event(SimpleNamespace(site=srv, location=disarmed, site_id=srv["id"]), msg(5)) is None
    cameras.sync(srv, [{"id": "cam1", "name": "Gate"}], disabled=[{"id": "cam2", "name": "Dock"}])
    assert soc.on_event(conn, msg(6, camera_id="cam2")) is None                       # camera switched off at the server
    assert rows(loc["id"]) == []
    what, inc = soc.on_event(conn, msg(8, policy={"text": "nobody after 22:00"}))
    assert what == "opened" and inc["priority"] == "high"
    # the heartbeat's attention list feeds the same way (and re-lists are idempotent)
    att = [{"id": 8, "camera_id": "cam1", "priority": "high", "start_ts": time.time()},
           {"id": 9, "camera_id": "cam1", "label": "person", "priority": "medium", "start_ts": time.time()},
           {"id": 10, "camera_id": "cam2", "priority": "high", "start_ts": time.time()}]
    got = soc.on_attention(conn, att)
    assert [w for w, _ in got] == ["event_added"] and got[0][1]["event_count"] == 2
    ie = db.rows(sa.select(db.incident_events).where(db.incident_events.c.incident_id == inc["id"]).order_by(db.incident_events.c.id))
    assert [(e["event_id"], e["kind"]) for e in ie] == [("8", "event"), ("9", "attention")]
    assert ie[0]["detail"]["policy"] == "nobody after 22:00"


def _incident(s, i=1, prio="high"):
    srv, loc = ctx(s)
    return soc.ingest(srv, loc, ev(i, prio))[1]


def test_claim_release_takeover_handoff(client, superuser):
    s = soc_setup(client, superuser, "own")
    auth.create_user("op2@own.example", PW)
    auth.set_soc_role("op2@own.example", "operator")
    op2 = _login(client, "op2@own.example", PW)
    inc = _incident(s)
    base = f"/api/soc/incidents/{inc['id']}"
    assert s["view"].post(f"{base}/claim").status_code == 403   # customers are not SOC staff
    r = s["op"].post(f"{base}/claim")
    assert r.status_code == 200 and r.json()["incident"]["state"] == "claimed"
    got = r.json()["incident"]
    assert got["claimed_by_email"] == "op@own.example" and got["first_claimed_at"] == got["claimed_at"] and got["resolve_due_at"]
    assert s["op"].post(f"{base}/claim").status_code == 200    # a double click is fine
    r = op2.post(f"{base}/claim")
    assert r.status_code == 409 and r.json()["detail"].startswith("already claimed by op@own.example ") and r.json()["detail"].endswith(" s ago")
    assert op2.post(f"{base}/release").status_code == 403
    r = s["op"].post(f"{base}/release")
    assert r.status_code == 200 and r.json()["incident"]["state"] == "new" and r.json()["incident"]["claimed_by"] is None
    assert r.json()["incident"]["sla_due_at"] == inc["sla_due_at"]   # the claim clock keeps running
    assert s["op"].post(f"{base}/release").status_code == 409
    assert op2.post(f"{base}/claim").status_code == 200
    first = db.one(sa.select(db.incidents).where(db.incidents.c.id == inc["id"]))["first_claimed_at"]
    assert s["op"].post(f"{base}/takeover").status_code == 403   # supervisors only
    r = s["sup"].post(f"{base}/takeover")
    assert r.status_code == 200 and r.json()["incident"]["claimed_by_email"] == "sup@own.example"
    assert r.json()["incident"]["assigned_by_email"] == "sup@own.example" and r.json()["incident"]["first_claimed_at"] == first
    op_id, op2_id = auth.user_by_email("op@own.example")["id"], auth.user_by_email("op2@own.example")["id"]
    assert s["sup"].post(f"{base}/handoff", json={"user_id": auth.user_by_email("view@own.example")["id"]}).status_code == 404
    r = s["sup"].post(f"{base}/handoff", json={"user_id": op_id})
    assert r.status_code == 200 and r.json()["incident"]["claimed_by"] == op_id
    assert op2.post(f"{base}/handoff", json={"user_id": op2_id}).status_code == 403   # not theirs to give
    assert s["op"].post(f"{base}/handoff", json={"user_id": op2_id}).json()["incident"]["claimed_by"] == op2_id
    assert actions(inc["id"]) == ["opened", "claim", "release", "claim", "takeover", "handoff", "handoff"]
    # presence follows the work: op2 holds it (engaged), op handed it on (available)
    pres = {p["email"]: p for p in s["op"].get("/api/soc/presence").json()}
    assert pres["op2@own.example"]["status"] == "engaged" and pres["op2@own.example"]["incident_id"] == inc["id"]
    assert pres["op@own.example"]["status"] == "available" and pres["op@own.example"]["on_shift"] is True
    assert set(pres["op@own.example"]) == {"user_id", "email", "soc_role", "status", "since", "last_seen_at", "incident_id", "on_shift"}
    assert s["op"].put("/api/soc/presence", json={"status": "break"}).json()["status"] == "break"
    assert s["op"].put("/api/soc/presence", json={"status": "asleep"}).status_code == 422
    assert soc.on_shift_ids() >= {op2_id, op_id}
    assert op_id not in soc.on_shift_ids(now=time.time() + soc.ON_SHIFT_S + 5)   # not seen for 2 minutes: off shift
    assert s["op"].post("/api/soc/incidents/999999/claim").status_code == 404


def test_resolve_and_four_eyes(client, superuser):
    s = soc_setup(client, superuser, "four")
    inc = _incident(s, 1, "high")
    base = f"/api/soc/incidents/{inc['id']}"
    assert s["op"].post(f"{base}/resolve", json={"disposition": "false_alarm"}).status_code == 409   # claim it first
    s["op"].post(f"{base}/claim")
    assert s["op"].post(f"{base}/resolve", json={"disposition": "true_alarm_dispatched"}).status_code == 422   # notes required
    assert s["op"].post(f"{base}/resolve", json={"disposition": "swept"}).status_code == 422   # set by the system only
    assert s["op"].post(f"{base}/resolve", json={"disposition": "bogus"}).status_code == 422
    r = s["op"].post(f"{base}/resolve", json={"disposition": "true_alarm_dispatched", "notes": "police called"})
    assert r.status_code == 200 and r.json()["incident"]["state"] == "pending_verify" and r.json()["incident"]["closed_at"] is None
    assert r.json()["feedback"] is None
    assert s["op"].post(f"{base}/verify").status_code == 403    # supervisors only
    r = s["sup"].post(f"{base}/verify")
    assert r.status_code == 200 and r.json()["incident"]["state"] == "closed" and r.json()["incident"]["four_eyes_by_email"] == "sup@four.example"
    assert s["sup"].post(f"{base}/verify").status_code == 409

    # a supervisor resolving needs another supervisor to verify
    inc2 = _incident(s, 2, "high")
    b2 = f"/api/soc/incidents/{inc2['id']}"
    r = s["sup"].post(f"{b2}/resolve", json={"disposition": "no_action"})   # unclaimed: supervisors may; no_action on high = four eyes
    assert r.json()["incident"]["state"] == "pending_verify"
    r = s["sup"].post(f"{b2}/verify")
    assert r.status_code == 403 and r.json()["detail"] == "you resolved this incident: a different supervisor has to verify it"
    assert s["root"].post(f"{b2}/verify").json()["incident"]["state"] == "closed"   # a hub administrator is a supervisor

    # a false alarm closes at once and is reported to the site (offline here: recorded as failed for a retry)
    soc.ingest(*ctx(s), ev(3, "medium"))   # the others are resolved: a new incident
    inc3 = rows(s["loc"]["id"])[-1]
    s["op"].post(f"/api/soc/incidents/{inc3['id']}/claim")
    r = s["op"].post(f"/api/soc/incidents/{inc3['id']}/resolve", json={"disposition": "false_alarm", "notes": "a fox"})
    assert r.json()["incident"]["state"] == "closed" and r.json()["incident"]["disposition"] == "false_alarm"
    assert r.json()["feedback"] == {"sent": 0, "failed": 1}
    assert db.one(sa.select(db.incident_events).where(db.incident_events.c.incident_id == inc3["id"]))["feedback_state"] == "failed"
    assert actions(inc3["id"])[-2:] == ["resolve", "feedback"]
    # the log of the first: who did what
    lg = soc.log_of(inc["id"])[inc["id"]]
    assert [(r["action"], r["user_email"]) for r in lg] == [("opened", None), ("claim", "op@four.example"), ("resolve", "op@four.example"),
                                                             ("verify", "sup@four.example")]
    assert lg[2]["detail"] == {"disposition": "true_alarm_dispatched", "notes": "police called", "four_eyes": True}


def test_sweep_and_promote(client, superuser):
    s = soc_setup(client, superuser, "sweep")
    srv, loc = ctx(s)
    t0 = time.time()
    a = soc.ingest(srv, loc, ev(1, "low"), now=t0 - 3000)[1]
    b = soc.ingest(srv, loc, ev(2, "low"), now=t0 - 2000)[1]
    c = soc.ingest(srv, loc, ev(3, "low"), now=t0 - 1000)[1]
    other = server(s["mon"], "Other NVR")   # another monitored-customer Site: left alone by a Site sweep
    oloc = db.one(sa.select(db.locations).where(db.locations.c.id == other["location_id"]))
    d = soc.ingest(other, {**oloc, "monitored": True}, ev(4, "low"), now=t0)[1]
    r = s["op"].post(f"/api/soc/incidents/{a['id']}/promote")
    assert r.status_code == 200
    p = r.json()["incident"]
    assert p["lane"] == "ring" and p["priority"] == "medium" and p["sla_due_at"] and p["escalation_level"] == 0
    assert s["op"].post(f"/api/soc/incidents/{a['id']}/promote").status_code == 409
    s["op"].post(f"/api/soc/incidents/{b['id']}/claim")   # claimed: not swept
    r = s["op"].post("/api/soc/incidents/sweep", json={"location_id": loc["id"]})
    assert r.status_code == 200 and r.json() == {"swept": [c["id"]], "count": 1}
    got = {i["id"]: i for i in rows(loc["id"])}
    assert got[c["id"]]["state"] == "closed" and got[c["id"]]["disposition"] == "swept" and got[a["id"]]["state"] == "new"
    assert db.one(sa.select(db.incidents).where(db.incidents.c.id == d["id"]))["state"] == "new"
    assert actions(c["id"]) == ["opened", "swept"] and actions(a["id"]) == ["opened", "promote"]


def test_queue_detail_calls_and_sop(client, superuser):
    s = soc_setup(client, superuser, "detail")
    srv, loc = ctx(s)
    t0 = time.time()
    med = soc.ingest(srv, loc, ev(1, "medium"), now=t0 - 2000)[1]
    high = soc.ingest(srv, loc, ev(2, "high"), now=t0)[1]
    queue = s["op"].get(f"/api/soc/incidents?location={loc['id']}").json()
    assert [i["id"] for i in queue] == [high["id"], med["id"]]   # priority first, then oldest
    assert s["op"].get(f"/api/soc/incidents?location={loc['id']}&state=closed").json() == []
    assert s["op"].get("/api/soc/incidents?state=bogus").status_code == 422
    assert s["view"].get("/api/soc/incidents").status_code == 403
    root = s["root"]
    cts = root.put(f"/api/locations/{loc['id']}/contacts", json={"contacts": [{"name": "Pat", "phone": "555-0100"}]}).json()
    procs = root.put(f"/api/locations/{loc['id']}/procedures", json={"procedures": [
        {"title": "Intruder", "steps": [{"text": "Check all cameras", "required": True}, {"text": "Call the keyholder"}], "priority": "high"},
        {"title": "Any alarm", "steps": [{"text": "Look"}]}, {"title": "Never", "steps": [{"text": "x"}], "priority": "high"}]}).json()
    base = f"/api/soc/incidents/{high['id']}"
    assert s["op"].post(f"{base}/calls", json={"contact_id": cts[0]["id"], "outcome": "spoke"}).status_code == 409   # unclaimed
    s["op"].post(f"{base}/claim")
    r = s["op"].post(f"{base}/calls", json={"contact_id": cts[0]["id"], "outcome": "no_answer", "notes": "rang out"})
    assert r.status_code == 200
    assert s["op"].post(f"{base}/calls", json={"contact_id": cts[0]["id"], "outcome": "maybe"}).status_code == 422
    assert s["op"].post(f"{base}/calls", json={"contact_id": 999999, "outcome": "spoke"}).status_code == 404
    r = s["op"].post(f"{base}/sop", json={"procedure_id": procs[0]["id"], "step_id": "s1", "done": True})
    assert r.status_code == 200 and r.json()["procedures"][0]["steps"][0]["done"] is True and r.json()["procedures"][0]["complete"] is True
    assert s["op"].post(f"{base}/sop", json={"procedure_id": procs[0]["id"], "step_id": "s9"}).status_code == 404
    s["op"].post(f"{base}/note", json={"text": "subject left on foot"})
    d = s["op"].get(base).json()
    assert set(d) == {"incident", "events", "log", "contacts", "procedures", "calls"}
    assert d["incident"]["id"] == high["id"] and d["contacts"][0]["name"] == "Pat"
    assert [p["title"] for p in d["procedures"]] == ["Intruder", "Any alarm", "Never"]   # all apply at high
    assert d["procedures"][0]["done_count"] == 1 and d["procedures"][1]["complete"] is False
    assert d["calls"][0]["detail"] == {"contact_id": cts[0]["id"], "name": "Pat", "phone": "555-0100", "outcome": "no_answer", "notes": "rang out"}
    assert d["events"][0]["camera_name"] == "Gate" and d["events"][0]["server_name"] == "Yard NVR"
    assert [r["action"] for r in d["log"]] == ["opened", "claim", "call", "sop", "note"]
    # at medium only the procedures without a priority (or a lower one) apply
    assert [p["title"] for p in s["op"].get(f"/api/soc/incidents/{med['id']}").json()["procedures"]] == ["Any alarm"]

    # overview, Sites board, SLA
    ov = s["op"].get("/api/soc/overview").json()
    assert ov["by_state"]["claimed"] >= 1 and ov["ring_count"] >= 1 and ov["sound"]["ring"] is True
    me = next(o for o in ov["operators"] if o["email"] == "op@detail.example")
    assert me["claimed"] == 1 and me["status"] == "engaged"
    sites = {x["id"]: x for x in s["op"].get("/api/soc/sites").json()}
    assert sites[loc["id"]]["armed"] is True and sites[loc["id"]]["reason"] == "always" and sites[loc["id"]]["open_incidents"] == 2
    assert sites[loc["id"]]["org_name"] == "detail Mon" and sites[loc["id"]]["ringing"] == 1   # med is ringing, high claimed
    assert s["op"].get("/api/soc/sla").json()["sla"]["high"]["claim_s"] == 60
    assert s["op"].put("/api/soc/sla", json={"high": {"claim_s": 45}}).status_code == 403
    r = s["sup"].put("/api/soc/sla", json={"high": {"claim_s": 45}, "low": {"lane": "ring", "claim_s": 900}})
    assert r.status_code == 200 and r.json()["sla"]["high"] == {"claim_s": 45, "resolve_s": 600, "lane": "ring"}
    assert r.json()["sla"]["low"]["lane"] == "ring" and r.json()["defaults"]["high"]["claim_s"] == 60
    assert s["sup"].put("/api/soc/sla", json={"high": None, "low": None}).json()["sla"] == soc.SLA


def test_customer_incident_list(client, superuser):
    s = soc_setup(client, superuser, "cust")
    inc = _incident(s, 1, "high")
    s["op"].post(f"/api/soc/incidents/{inc['id']}/claim")
    s["op"].post(f"/api/soc/incidents/{inc['id']}/resolve", json={"disposition": "authorized"})
    out = s["view"].get(f"/api/locations/{s['loc']['id']}/incidents").json()
    assert [i["id"] for i in out] == [inc["id"]] and out[0]["state"] == "closed" and out[0]["disposition"] == "authorized"
    assert out[0]["claimed_by_email"] == "SOC" and out[0]["resolved_by_email"] == "SOC"   # the rota stays the SOC's
    assert [(r["action"], r["by"]) for r in out[0]["log"]] == [("opened", None), ("claim", "SOC"), ("resolve", "SOC")]
    assert s["op"].get(f"/api/locations/{s['loc']['id']}/incidents").status_code == 200
    auth.create_user("outsider@cust.example", PW)
    assert _login(client, "outsider@cust.example", PW).get(f"/api/locations/{s['loc']['id']}/incidents").status_code == 403
    # a member restricted to another Site doesn't see this one
    other = server(s["mon"], "Elsewhere")
    s["root"].post(f"/api/orgs/{s['mon']}/members", json={"email": "narrow@cust.example", "role": "viewer", "password": PW,
                                                           "all_sites": False, "location_ids": [other["location_id"]]})
    assert _login(client, "narrow@cust.example", PW).get(f"/api/locations/{s['loc']['id']}/incidents").status_code == 403


def test_push_to_soc_and_customers(client, superuser):
    import asyncio
    from hub import push
    s = soc_setup(client, superuser, "pushsoc")
    sent = []
    push.set_sender(lambda sub, payload: sent.append((sub["user_id"], payload)) or True)
    try:
        ids = {k: auth.user_by_email(f"{k}@pushsoc.example")["id"] for k in ("op", "sup", "view", "adm")}
        for k in ids:
            push.subscribe(ids[k], {"endpoint": f"https://push.example/{k}-pushsoc"}, ["offline"] if k == "adm" else None, "test")
        soc.set_presence(ids["op"], "available")
        inc = _incident(s, 1, "high")
        assert asyncio.run(push.notify_soc(inc, "operators")) == 1   # on shift: op; sup has no presence
        assert sent[-1][0] == ids["op"] and sent[-1][1]["url"] == f"/soc/incidents/{inc['id']}" and sent[-1][1]["kind"] == "soc"
        sent.clear()
        assert asyncio.run(push.notify_soc(inc, "supervisors")) >= 1
        assert ids["sup"] in {u for u, _ in sent} and ids["op"] not in {u for u, _ in sent}
        sent.clear()
        # customers: only subscriptions that opted into soc_incident (the default kinds' event_high already pushes the
        # same event, so view on the defaults gets nothing extra; adm chose offline only)
        assert asyncio.run(push.notify_incident_customers(inc)) == 0
        push.subscribe(ids["view"], {"endpoint": "https://push.example/view-pushsoc"}, [*push.DEFAULT_KINDS, "soc_incident"], "test")
        assert asyncio.run(push.notify_incident_customers(inc)) == 1
        assert sent[0][0] == ids["view"] and sent[0][1]["kind"] == "soc_incident" and sent[0][1]["url"] == f"/sites/{s['loc']['id']}/alerts"
        assert "soc_incident" in s["view"].get("/api/push/vapid").json()["kinds"]   # the PushCard can offer it
    finally:
        push.set_sender(None)
