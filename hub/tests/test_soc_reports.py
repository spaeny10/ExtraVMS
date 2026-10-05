"""SOC reports (soc_reports.py): operator p50/p95 against hand-computed values, false-alarm rates per Site and
camera, the shift report without vLLM (plain text), the monthly customer summary with its armed-hours estimate, the
shift boundaries and the loop's idempotent run, and who may read which report route. The incidents are written at a
fixed time in November 2023, so incidents other tests open now never fall into these windows."""
import asyncio
import datetime as dt

import pytest
import sqlalchemy as sa

from hub import auth, db, soc, soc_reports
from test_access import _login
from test_soc_incidents import ctx, ev
from test_soc_roles import PW, soc_setup

T0 = dt.datetime(2023, 11, 14, 12, 0).timestamp()   # hub-local, mid-month
SINCE, UNTIL = T0 - 10, T0 + 20000


def _make(srv, loc, i, prio, cam, holder=None, claim_after=None, resolve_after=None, disposition=None, pre=(), mid=(), calls=0):
    """An incident opened at T0 + 1000*i (apart, so none group), worked by `holder` as given, closed."""
    opened = T0 + 1000 * i
    inc = soc.ingest(srv, loc, ev(i, prio, cam=cam, start_ts=opened), now=opened)[1]
    t, lg = db.incidents, db.incident_log
    vals = {"state": "closed", "closed_at": opened + (resolve_after or 1), "disposition": disposition}
    with db.engine().begin() as c:
        for action, detail in pre:   # before the claim (escalations while unclaimed)
            c.execute(lg.insert().values(incident_id=inc["id"], ts=opened + 1, user_id=None, action=action, detail=detail))
        if holder:
            c.execute(lg.insert().values(incident_id=inc["id"], ts=opened + claim_after, user_id=holder, action="claim", detail={}))
            vals |= {"first_claimed_at": opened + claim_after, "claimed_by": holder}
        for action, detail in mid:   # while held
            c.execute(lg.insert().values(incident_id=inc["id"], ts=opened + claim_after + 1, user_id=None, action=action, detail=detail))
        for _ in range(calls):
            c.execute(lg.insert().values(incident_id=inc["id"], ts=opened + 5, user_id=holder, action="call",
                                         detail={"contact_id": 1, "outcome": "spoke"}))
        if resolve_after is not None and holder:
            c.execute(lg.insert().values(incident_id=inc["id"], ts=opened + resolve_after, user_id=holder, action="resolve",
                                         detail={"disposition": disposition}))
            vals |= {"resolved_by": holder, "resolved_at": opened + resolve_after}
        if pre:
            vals["escalation_level"] = max(d["level"] for a, d in pre if a == "escalated")
        c.execute(sa.update(t).where(t.c.id == inc["id"]).values(**vals))
    return inc["id"]


@pytest.fixture(scope="module")
def rep(client, superuser):
    s = soc_setup(client, superuser, "reports")
    srv, loc = ctx(s)
    auth.create_user("op2@reports.example", PW)
    auth.set_soc_role("op2@reports.example", "operator")
    ids = {k: auth.user_by_email(f"{k}@reports.example")["id"] for k in ("op", "op2", "sup")}
    # op: claims after 10/20/30/40 s, resolves after 100..400 s
    for i, (claim, res, disp) in enumerate(((10, 100, "false_alarm"), (20, 200, "nuisance"), (30, 300, "authorized"),
                                            (40, 400, "true_alarm_dispatched")), start=1):
        _make(srv, loc, i, "medium", "cam1", ids["op"], claim, res, disp)
    # sup: a high incident, overdue while they held it, one call made
    _make(srv, loc, 5, "high", "cam2", ids["sup"], 50, 500, "no_action", mid=(("overdue", {"level": 1}),), calls=1)
    # op2: picked up after it escalated to level 1
    _make(srv, loc, 6, "medium", "cam2", ids["op2"], 70, 600, "false_alarm", pre=(("escalated", {"level": 1}),))
    # nobody: a quiet one that expired
    _make(srv, loc, 7, "low", "cam2", disposition="expired")
    return {**s, "ids": ids, "srv": srv, "loc_row": loc}


def test_percentile_helper():
    assert soc_reports.percentile([], 0.5) is None
    assert soc_reports.percentile([7], 0.95) == 7
    assert soc_reports.percentile([40, 10, 30, 20], 0.5) == 25.0     # linear between ranks: (20 + 30) / 2
    assert soc_reports.percentile([10, 20, 30, 40], 0.95) == 38.5    # rank 2.85: 30 + 0.85 * 10


def test_operator_stats(rep):
    out = soc_reports.operator_stats(SINCE, UNTIL, rep["mon"])
    ops = {o["email"]: o for o in out["operators"]}
    assert set(ops) == {"op@reports.example", "op2@reports.example", "sup@reports.example"}
    op = ops["op@reports.example"]
    assert op["claimed"] == 4 and op["resolved"] == 4 and op["soc_role"] == "operator"
    assert op["time_to_claim"] == {"n": 4, "p50": 25.0, "p95": 38.5}
    assert op["time_to_resolve"] == {"n": 4, "p50": 250.0, "p95": 385.0}
    assert op["dispositions"] == {"false_alarm": 1, "nuisance": 1, "authorized": 1, "true_alarm_dispatched": 1}
    assert op["false_alarm_share"] == 0.5 and op["escalations_received"] == 0 and op["escalated_claimed"] == 0
    sup = ops["sup@reports.example"]
    assert sup["escalations_received"] == 1 and sup["time_to_claim"] == {"n": 1, "p50": 50, "p95": 50} and sup["false_alarm_share"] == 0.0
    op2 = ops["op2@reports.example"]
    assert op2["escalated_claimed"] == 1 and op2["escalations_received"] == 0 and op2["false_alarm_share"] == 1.0
    tot = out["totals"]
    assert tot["incidents"] == 7 and tot["claimed"] == 6 and tot["resolved"] == 6   # the expired one has no resolver
    assert tot["time_to_claim"] == {"n": 6, "p50": 35.0, "p95": 65.0}   # [10..50, 70]: rank 4.75 = 50 + 0.75 * 20
    assert soc_reports.operator_stats(SINCE, UNTIL, rep["plain"])["operators"] == []   # another customer: nothing


def test_false_alarm_rate(rep):
    out = soc_reports.false_alarm_rate(SINCE, UNTIL, rep["mon"])
    assert out["totals"]["closed"] == 7 and out["totals"]["judged"] == 6 and out["totals"]["false_alarms"] == 3
    assert out["totals"]["rate"] == 0.5
    (site,) = out["sites"]
    assert site["location_id"] == rep["loc"]["id"] and site["location_name"] == "Yard" and site["org_name"] == "reports Mon"
    assert site["top_disposition"] == "false_alarm" and site["last_incident_at"] == T0 + 7000
    cams = {c["camera_id"]: c for c in out["cameras"]}
    assert [c["camera_id"] for c in out["cameras"]] == ["cam1", "cam2"]   # most false alarms first
    assert cams["cam1"]["camera_name"] == "Gate" and cams["cam1"]["server_name"] == "Yard NVR"
    assert (cams["cam1"]["closed"], cams["cam1"]["false_alarms"], cams["cam1"]["rate"]) == (4, 2, 0.5)
    assert cams["cam1"]["top_disposition"] == "authorized"   # a four-way tie: alphabetical
    assert (cams["cam2"]["closed"], cams["cam2"]["judged"], cams["cam2"]["false_alarms"], cams["cam2"]["rate"]) == (3, 2, 1, 0.5)


def test_shift_report_plain_and_routes(rep):
    r = asyncio.run(soc_reports.shift_report(SINCE, UNTIL))
    assert r["kind"] == "shift" and r["org_id"] is None and r["model"] is None   # no vLLM in tests: the plain text
    d = r["data"]
    assert d["counts"]["incidents"] == 7 and d["counts"]["by_priority"] == {"medium": 5, "high": 1, "low": 1}
    assert d["counts"]["by_lane"] == {"ring": 6, "quiet": 1} and d["counts"]["by_escalation"] == {"0": 6, "1": 1}
    assert d["counts"]["overdue"] == 1 and d["counts"]["open_now"] == 0
    assert d["calls"] == {"total": 1, "by_outcome": {"spoke": 1}}
    assert [n["priority"] for n in d["notable"]] == ["high", "medium"] and d["notable"][1]["escalation_level"] == 1
    assert d["notable"][0]["location_name"] == "Yard" and d["notable"][0]["disposition"] == "no_action"
    assert {o["email"] for o in d["operators"]} >= {"op@reports.example", "sup@reports.example"}
    assert r["text"].startswith("Shift 2023-11-14 ") and "7 incident(s) (1 high, 5 medium, 1 low)" in r["text"]
    assert "Notable:" in r["text"] and "Calls: 1 (spoke 1)" in r["text"]

    assert rep["op"].get("/api/soc/reports/shifts").status_code == 403   # supervisors only
    lst = rep["sup"].get("/api/soc/reports/shifts?limit=200").json()
    assert r["id"] in [x["id"] for x in lst]
    one = rep["sup"].get(f"/api/soc/reports/shifts/{r['id']}").json()
    assert set(one) == {"id", "kind", "org_id", "period_start", "period_end", "created_at", "created_by", "text", "data", "model"}
    assert rep["sup"].get("/api/soc/reports/shifts/999999").status_code == 404
    g = rep["sup"].post("/api/soc/reports/shifts/generate", json={"start": SINCE, "end": UNTIL})
    assert g.status_code == 200 and g.json()["created_by"] == rep["ids"]["sup"] and g.json()["data"]["counts"]["incidents"] == 7
    assert rep["sup"].post("/api/soc/reports/shifts/generate", json={"start": UNTIL, "end": SINCE}).status_code == 422
    assert rep["sup"].post("/api/soc/reports/shifts/generate").status_code == 200   # default: the last completed shift
    assert rep["op"].post("/api/soc/reports/shifts/generate").status_code == 403

    # the SOC-side computed reports
    assert rep["op"].get("/api/soc/reports/operators").status_code == 403
    ops = rep["sup"].get(f"/api/soc/reports/operators?since={SINCE}&until={UNTIL}&org={rep['mon']}").json()
    assert ops == soc_reports.operator_stats(SINCE, UNTIL, rep["mon"])
    fa = rep["sup"].get(f"/api/soc/reports/false-alarms?since={SINCE}&until={UNTIL}&org={rep['mon']}").json()
    assert fa["totals"]["false_alarms"] == 3 and len(fa["cameras"]) == 2
    assert rep["sup"].get(f"/api/soc/reports/operators?since={UNTIL}&until={SINCE}").status_code == 422


def test_monthly_summary_and_customer_routes(rep):
    r = soc_reports.monthly_customer_summary(rep["mon"], 2023, 11)
    d = r["data"]
    assert r["kind"] == "monthly" and r["org_id"] == rep["mon"] and (d["year"], d["month"]) == (2023, 11)
    (site,) = d["sites"]
    assert site["name"] == "Yard" and site["incidents"] == 7 and site["calls"] == 1
    assert site["median_response_s"] == 35.0 and site["dispositions"]["false_alarm"] == 2
    assert site["coverage"] == pytest.approx(1.0) and site["armed_hours"] == site["period_hours"]   # armed around the clock
    assert d["totals"]["incidents"] == 7 and "reports Mon: SOC monitoring summary for November 2023" in r["text"]
    assert "@" not in r["text"] and "op@" not in str(d)   # customer-facing: no SOC staff
    # the armed-hours estimate follows the schedule (overnight 18:00-06:00 = 12 h a day)
    day = dt.datetime(2023, 11, 14, tzinfo=dt.timezone.utc).timestamp()
    loc = {"monitored": True, "timezone": "UTC", "arm_schedule": [{"dow": list(range(7)), "from": "18:00", "to": "06:00"}]}
    assert soc_reports.armed_hours(loc, day, day + 86400) == 12.0
    assert soc_reports.armed_hours({**loc, "monitored": False}, day, day + 86400) == 0.0

    # SOC supervisors read (or generate) any customer's month
    assert rep["op"].get(f"/api/soc/reports/customers/{rep['mon']}?year=2023&month=11").status_code == 403
    got = rep["sup"].get(f"/api/soc/reports/customers/{rep['mon']}?year=2023&month=11").json()
    assert got["id"] == r["id"]   # the stored one, not a new one
    gen = rep["sup"].get(f"/api/soc/reports/customers/{rep['mon']}?year=2023&month=10").json()
    assert gen["id"] != r["id"] and gen["data"]["totals"]["incidents"] == 0 and gen["created_by"] == rep["ids"]["sup"]
    assert rep["sup"].get(f"/api/soc/reports/customers/{rep['mon']}?year=2023").status_code == 422
    assert rep["sup"].get("/api/soc/reports/customers/o_nope?year=2023&month=11").status_code == 404
    # the customer's own admin sees their summaries (without who generated them) and false-alarm rates
    mine = rep["adm"].get(f"/api/orgs/{rep['mon']}/soc/reports").json()
    assert {x["id"] for x in mine} >= {r["id"], gen["id"]} and all("created_by" not in x for x in mine)
    assert all(x["kind"] == "monthly" and x["org_id"] == rep["mon"] for x in mine)
    fa = rep["adm"].get(f"/api/orgs/{rep['mon']}/soc/false-alarms?since={SINCE}&until={UNTIL}").json()
    assert fa["totals"]["false_alarms"] == 3 and fa["org_id"] == rep["mon"]
    for who in ("view", "sup", "op"):   # viewers, and SOC staff widened into the customer, are not its admins
        assert rep[who].get(f"/api/orgs/{rep['mon']}/soc/reports").status_code == 403, who
        assert rep[who].get(f"/api/orgs/{rep['mon']}/soc/false-alarms").status_code == 403, who
    assert rep["root"].get(f"/api/orgs/{rep['mon']}/soc/reports").status_code == 200   # hub administrators
    auth.create_user("stranger@reports.example", PW)
    assert _login(rep["root"], "stranger@reports.example", PW).get(f"/api/orgs/{rep['mon']}/soc/reports").status_code in (403, 404)


def test_shift_boundaries_and_loop(rep, monkeypatch):
    from hub.config import settings
    monkeypatch.setattr(settings, "soc_shift_ends", "06:00,14:00,22:00")
    D = dt.datetime
    assert soc_reports.last_shift(D(2026, 10, 4, 3, 0)) == (D(2026, 10, 3, 14, 0), D(2026, 10, 3, 22, 0))
    assert soc_reports.last_shift(D(2026, 10, 4, 14, 0)) == (D(2026, 10, 4, 6, 0), D(2026, 10, 4, 14, 0))
    assert soc_reports.next_shift_end(D(2026, 10, 4, 3, 0)) == D(2026, 10, 4, 6, 0)
    assert soc_reports.next_shift_end(D(2026, 10, 4, 22, 0)) == D(2026, 10, 5, 6, 0)
    monkeypatch.setattr(settings, "soc_shift_ends", "07:30, bogus")
    assert soc_reports.shift_ends() == [450] and soc_reports.last_shift(D(2026, 10, 4, 8, 0)) == (D(2026, 10, 3, 7, 30), D(2026, 10, 4, 7, 30))
    monkeypatch.setattr(settings, "soc_shift_ends", "")
    assert soc_reports.shift_ends() == [360]
    monkeypatch.setattr(settings, "soc_shift_ends", "06:00,14:00,22:00")
    # the loop's work at a boundary: the shift that ended, and on the 1st last month for each monitored customer
    made = asyncio.run(soc_reports.run_due(D(2023, 12, 1, 6, 0)))
    shift = [m for m in made if m["kind"] == "shift"]
    assert len(shift) == 1 and shift[0]["period_start"] == D(2023, 11, 30, 22, 0).timestamp()
    assert shift[0]["period_end"] == D(2023, 12, 1, 6, 0).timestamp()
    t = db.soc_reports
    nov = soc_reports.month_bounds(2023, 11)
    assert len(db.rows(sa.select(t).where(t.c.kind == "monthly", t.c.org_id == rep["mon"], t.c.period_start == nov[0]))) == 1   # kept
    assert all(m["org_id"] in soc.monitored_org_ids() for m in made if m["kind"] == "monthly")
    assert asyncio.run(soc_reports.run_due(D(2023, 12, 1, 6, 0))) == []   # idempotent
