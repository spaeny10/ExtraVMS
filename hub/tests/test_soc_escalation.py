"""The escalation engine (soc.escalate_once with an injected `now`) and four-eyes rejection: an unclaimed ringing
incident past its claim clock pages on-shift operators (level 1), then supervisors and hub administrators (level 2),
then logs the pending customer contact and calls soc.on_level3 (level 3) and stops; a claimed incident past its
resolve clock is flagged overdue once; the quiet lane expires after a day of silence; failed false-alarm feedback is
retried once the server is back. Times are in the past so incidents other tests open at the real time are left alone."""
import asyncio
import time

import sqlalchemy as sa

from hub import auth, db, push, soc
from hub.agents import registry
from test_soc_incidents import actions, ctx, ev
from test_soc_roles import soc_setup


def run(now):
    return asyncio.run(soc.escalate_once(now))


def mine(acts, iid):
    return [a for a in acts if a["incident_id"] == iid]


def _ids(slug):
    return {k: auth.user_by_email(f"{k}@{slug}.example")["id"] for k in ("op", "sup", "view")}


def test_ladder_levels_and_hook(client, superuser, monkeypatch):
    s = soc_setup(client, superuser, "ladder")
    srv, loc = ctx(s)
    ids = _ids("ladder")
    sent = []
    push.set_sender(lambda sub, payload: sent.append((sub["user_id"], payload)) or True)
    hooked = []
    monkeypatch.setattr(soc, "on_level3", lambda inc: hooked.append(inc["id"]))
    try:
        for k in ("op", "sup", "view"):
            push.subscribe(ids[k], {"endpoint": f"https://push.example/{k}-ladder"}, None, "test")
        soc.set_presence(ids["op"], "available")   # on shift (the supervisor is not)
        t0 = time.time() - 1000
        inc = soc.ingest(srv, loc, ev(1, "high"), now=t0)[1]
        sent.clear()   # the open push (high) is not what this test is about
        assert mine(run(t0 + 59), inc["id"]) == []   # inside the claim clock
        got = mine(run(t0 + 61), inc["id"])
        assert [(a["action"], a["level"]) for a in got] == [("escalated", 1)] and got[0]["pushed"] >= 1
        who = {u for u, p in sent if p.get("incident_id") == inc["id"]}
        assert ids["op"] in who and ids["sup"] not in who and ids["view"] not in who   # level 1: SOC staff on shift
        row = db.one(sa.select(db.incidents).where(db.incidents.c.id == inc["id"]))
        assert row["escalation_level"] == 1 and row["next_escalation_at"] == t0 + 61 + 120
        assert mine(run(t0 + 61 + 60), inc["id"]) == []   # the next step is 120 s away
        sent.clear()
        got = mine(run(t0 + 181), inc["id"])
        assert [(a["action"], a["level"]) for a in got] == [("escalated", 2)]
        who = {u for u, p in sent if p.get("incident_id") == inc["id"]}
        assert ids["sup"] in who and ids["op"] not in who   # level 2: supervisors (and hub administrators), on shift or not
        assert all(p["audience"] == "supervisors" for u, p in sent if p.get("incident_id") == inc["id"])
        sent.clear()
        got = mine(run(t0 + 301), inc["id"])
        assert [(a["action"], a["level"]) for a in got] == [("escalated", 3)] and hooked == [inc["id"]]
        assert [p for _, p in sent if p.get("incident_id") == inc["id"]] == []   # level 3 pages nobody: the hook's job
        assert mine(run(t0 + 900), inc["id"]) == []   # and it stops there
        lg = [r for r in soc.log_of(inc["id"])[inc["id"]] if r["action"] == "escalated"]
        assert [r["detail"]["level"] for r in lg] == [1, 2, 3] and lg[-1]["detail"]["pending"] == "customer contact"
        assert all(r["user_id"] is None for r in lg)
        row = db.one(sa.select(db.incidents).where(db.incidents.c.id == inc["id"]))
        assert row["escalation_level"] == 3 and row["next_escalation_at"] is None and row["state"] == "new"
        ov = s["sup"].get("/api/soc/overview").json()
        assert set(ov["escalations"]) == {"level1", "level2", "level3"} and ov["escalations"]["level3"] >= 1
        assert isinstance(ov["overdue"], int)
        # a claim stops the ladder: a claimed incident never escalates as unclaimed
        inc2 = soc.ingest(srv, loc, ev(2, "medium"), now=t0 + 2000)[1]   # past the grouping window: its own incident
        assert inc2["id"] != inc["id"]
        s["op"].post(f"/api/soc/incidents/{inc2['id']}/claim")
        assert [a for a in mine(run(t0 + 2000 + 181), inc2["id"]) if a["action"] == "escalated"] == []
    finally:
        push.set_sender(None)
        for k in ("op", "sup", "view"):   # later tests count pushes to everyone on shift: leave no subscriptions behind
            push.unsubscribe(ids[k], f"https://push.example/{k}-ladder")


def test_overdue_once_and_quiet_expiry(client, superuser):
    s = soc_setup(client, superuser, "overdue")
    srv, loc = ctx(s)
    ids = _ids("overdue")
    sent = []
    push.set_sender(lambda sub, payload: sent.append((sub["user_id"], payload)) or True)
    try:
        push.subscribe(ids["sup"], {"endpoint": "https://push.example/sup-overdue"}, None, "test")
        inc = soc.ingest(srv, loc, ev(1, "high"))[1]
        s["op"].post(f"/api/soc/incidents/{inc['id']}/claim")
        now = time.time()
        # the resolve clock ran out 10 s ago (claim set next_escalation_at = resolve_due_at)
        db.run(sa.update(db.incidents).where(db.incidents.c.id == inc["id"]).values(resolve_due_at=now - 10, next_escalation_at=now - 10))
        sent.clear()
        got = mine(run(now), inc["id"])
        assert [(a["action"], a["level"]) for a in got] == [("overdue", 1)]
        assert ids["sup"] in {u for u, p in sent if p.get("incident_id") == inc["id"]}
        row = db.one(sa.select(db.incidents).where(db.incidents.c.id == inc["id"]))
        assert row["escalation_level"] == 1 and row["next_escalation_at"] is None and row["state"] == "claimed"
        assert mine(run(now + 60), inc["id"]) == []   # once
        # a handoff sets the clock again (still past due): cleared without a second overdue row or page
        s["sup"].post(f"/api/soc/incidents/{inc['id']}/handoff", json={"user_id": ids["sup"]})
        sent.clear()
        assert mine(run(now + 120), inc["id"]) == []
        assert actions(inc["id"]).count("overdue") == 1 and sent == []
        lg = next(r for r in soc.log_of(inc["id"])[inc["id"]] if r["action"] == "overdue")
        assert lg["detail"]["claimed_by"] == ids["op"] and lg["detail"]["overdue_s"] == 10.0
        assert s["op"].get("/api/soc/overview").json()["overdue"] >= 1

        s["sup"].post(f"/api/soc/incidents/{inc['id']}/resolve", json={"disposition": "authorized"})   # new events open anew

        # the quiet lane: silent for a day -> expired; a newer one stays
        t0 = time.time()
        old = soc.ingest(srv, loc, ev(10, "low"), now=t0 - 90000)[1]
        fresh = soc.ingest(srv, loc, ev(11, "low"), now=t0 - 3600)[1]
        assert old["lane"] == fresh["lane"] == "quiet" and old["id"] != fresh["id"]
        got = run(t0)
        assert mine(got, old["id"]) == [{"incident_id": old["id"], "action": "expired"}] and mine(got, fresh["id"]) == []
        row = db.one(sa.select(db.incidents).where(db.incidents.c.id == old["id"]))
        assert row["state"] == "closed" and row["disposition"] == "expired" and row["resolved_by"] is None and row["closed_at"] == t0
        assert actions(old["id"]) == ["opened", "expired"]
    finally:
        push.set_sender(None)
        push.unsubscribe(ids["sup"], "https://push.example/sup-overdue")


def test_feedback_retry_when_back_online(client, superuser, monkeypatch):
    from hub import fleet_actions
    s = soc_setup(client, superuser, "retry")
    srv, loc = ctx(s)
    inc = soc.ingest(srv, loc, ev(1, "medium"))[1]
    s["op"].post(f"/api/soc/incidents/{inc['id']}/claim")
    r = s["op"].post(f"/api/soc/incidents/{inc['id']}/resolve", json={"disposition": "nuisance", "notes": "a cat"})
    assert r.json()["feedback"] == {"sent": 0, "failed": 1}   # the server is offline
    assert mine(run(time.time()), inc["id"]) == []             # still offline: left as failed, not re-marked

    calls = []

    async def fake_call(conn, u, method, path, role="viewer", query="", body=None, extra=None, timeout=30):
        calls.append((u["email"], method, path, body))
        return 200, {}
    fake_conn = object()
    orig = registry.get
    monkeypatch.setattr(registry, "get", lambda sid: fake_conn if sid == srv["id"] else orig(sid))
    monkeypatch.setattr(fleet_actions, "_call", fake_call)
    got = mine(run(time.time()), inc["id"])
    assert got == [{"incident_id": inc["id"], "action": "feedback", "sent": 1, "failed": 0}]
    # in the resolver's name, with their notes, to the event the incident holds
    assert calls == [("op@retry.example", "PUT", "/api/events/1/feedback", {"verdict": "false_alarm", "note": "a cat"})]
    assert db.one(sa.select(db.incident_events).where(db.incident_events.c.incident_id == inc["id"]))["feedback_state"] == "sent"
    fb = [r for r in soc.log_of(inc["id"])[inc["id"]] if r["action"] == "feedback"]
    assert fb[-1]["user_id"] is None and fb[-1]["detail"] == {"sent": 1, "failed": 0, "retry": True}
    assert mine(run(time.time()), inc["id"]) == []   # nothing failed any more


def test_reject(client, superuser):
    s = soc_setup(client, superuser, "reject")
    srv, loc = ctx(s)
    ids = _ids("reject")
    inc = soc.ingest(srv, loc, ev(1, "high"))[1]
    base = f"/api/soc/incidents/{inc['id']}"
    s["op"].post(f"{base}/claim")
    s["op"].post(f"{base}/resolve", json={"disposition": "true_alarm_deterred", "notes": "left after the siren"})
    assert s["op"].post(f"{base}/reject", json={"note": "no"}).status_code == 403            # supervisors only
    assert s["sup"].post(f"{base}/reject", json={"note": ""}).status_code == 422             # say why
    r = s["sup"].post(f"{base}/reject", json={"note": "check the other camera first"})
    assert r.status_code == 200, r.text
    got = r.json()["incident"]
    # the resolver is on shift (resolving left them available): back to them, with a fresh resolve clock
    assert got["state"] == "claimed" and got["claimed_by"] == ids["op"] and got["disposition"] is None and got["resolved_by"] is None
    assert got["resolve_due_at"] and got["resolve_due_at"] > time.time() + 500 and got["assigned_by"] == ids["sup"]
    lg = soc.log_of(inc["id"])[inc["id"]][-1]
    assert lg["action"] == "rejected" and lg["user_id"] == ids["sup"]
    assert lg["detail"] == {"note": "check the other camera first", "to": "claimed", "resolver_id": ids["op"],
                            "disposition": "true_alarm_deterred", "disposition_notes": "left after the siren"}
    assert s["sup"].post(f"{base}/reject", json={"note": "again"}).status_code == 409        # not pending any more
    # resolved again, and the resolver goes off shift: back to the queue with a fresh claim clock and ladder
    s["op"].post(f"{base}/resolve", json={"disposition": "true_alarm_deterred", "notes": "really left"})
    s["op"].put("/api/soc/presence", json={"status": "offline"})
    db.run(sa.update(db.incidents).where(db.incidents.c.id == inc["id"]).values(escalation_level=2))
    got = s["sup"].post(f"{base}/reject", json={"note": "needs a call to the keyholder"}).json()["incident"]
    assert got["state"] == "new" and got["claimed_by"] is None and got["escalation_level"] == 0
    assert got["sla_due_at"] and got["next_escalation_at"] == got["sla_due_at"]
    assert soc.log_of(inc["id"])[inc["id"]][-1]["detail"]["to"] == "new"
    # a supervisor can't reject their own resolution (the four eyes are someone else's)
    s["sup"].post(f"{base}/resolve", json={"disposition": "true_alarm_deterred", "notes": "done"})
    assert s["sup"].post(f"{base}/reject", json={"note": "hm"}).status_code == 403
    assert s["root"].post(f"{base}/reject", json={"note": "not convinced"}).json()["incident"]["state"] in ("claimed", "new")
