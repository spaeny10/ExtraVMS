"""Cellular coverage (coverage.py): the CoverageMap request, unit counting per the vendor's billing rules, normalization,
storage and refresh (age, a pin moved > 100 m, trial data on a paid plan), the monthly budget and its alert, who sees and
who fetches (trial = hub administrators only), the per-Site rate limit, the address check cache, the upload fit and the
purge CLI. Always a fake httpx transport and a made-up key: never the real API."""
import asyncio
import json
import sys
import time

import httpx
import pytest
import sqlalchemy as sa

from hub import coverage, db, push
from hub.config import settings
from hub.hosts import HUB_ORG
from test_access import _login

FAKE_KEY = "fake-key-for-tests-0000"
PW = "coverage-pass-123"


# ---------------------------------------------------------------- a fake CoverageMap

def metric(med, count=12, failed=1, radii=None):
    """A speed-test metric; `radii` overrides the per-radius (count, med) pairs."""
    def stats(c, m, f=failed):
        return {"accuracy": "exact" if c else ("low" if f else "none"), "min": (m * 0.4 if m is not None else None), "med": m,
                "avg": m, "max": (m * 1.8 if m is not None else None), "count": c, "failedCount": f}
    r = radii or {"halfKilometer": (count, med), "oneKilometer": (count * 3, med * 0.9 if med else med), "twoKilometers": (count * 5, med * 0.8 if med else med)}
    return {**stats(count, med), "distance": 0.06, **{k: stats(c, m) for k, (c, m) in r.items()}}


def entry(code, tech, overall, *, up=20.0, speed=True, fcc=True, summary=True, cov=(1, 0.98, 0.9), signal=-80.0):
    names = {"ATT": "AT&T Mobility", "VZW": "Verizon Wireless", "TMO": "T-Mobile US"}
    e = {"provider": {"code": code, "name": names[code]}, "technology": {"code": tech, "name": "LTE" if tech == "lte" else "5GNR"}}
    if summary:
        e["summary"] = {"overall": overall, "performance": overall - 0.5, "coverage": overall + 0.5, "reliability": overall - 1,
                        "isFullyCovered": cov[0] == 1, "source": "measured" if speed else "estimated", "accuracy": "exact" if speed else None}
    if fcc:
        e["fccCoverage"] = {"signal": {"signal": signal, "halfKilometer": signal - 1, "oneKilometer": signal - 2, "twoKilometers": signal - 4},
                            "coverage": {"halfKilometer": cov[0], "oneKilometer": cov[1], "twoKilometers": cov[2]}}
    e["speedTest"] = ({"latency": metric(30.0), "downloadSpeed": metric(100.0), "uploadSpeed": metric(up)} if speed else None)
    return e


def location_answer(i, loc, speed=True):
    out = {"index": i, "id": loc.get("id"), "latitude": loc.get("latitude", 37.68), "longitude": loc.get("longitude", -96.97), "coverage": [
        entry("VZW", "lte", 8.6, up=17.4, speed=speed), entry("VZW", "5g", 7.1, up=40.0, speed=speed),
        entry("ATT", "lte", 6.2, up=8.0, speed=speed), entry("ATT", "5g", 0, speed=False, fcc=False, cov=(0, 0, 0)),
        entry("TMO", "lte", 7.5, up=30.0, speed=speed), entry("TMO", "5g", 9.0, up=60.0, speed=speed)]}
    if "address" in loc:
        out.update(address=loc["address"], confidence="high")
    return out


class FakeAPI:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.mode = "ok"            # ok | down | refuse
        self.speed = True           # speed tests near every location
        self.bad_addresses: set[str] = set()

    def bodies(self):
        return [json.loads(r.content) for r in self.requests]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.mode == "down":
            raise httpx.ConnectError(f"connection refused (key {FAKE_KEY})")
        if self.mode == "refuse":
            return httpx.Response(400, json={"status": 400, "messages": {"errors": ["Unknown dataset: speedtests"]}, "data": None})
        body = json.loads(req.content)
        locs = []
        for i, loc in enumerate(body["locations"]):
            if loc.get("address") in self.bad_addresses:
                locs.append({"index": i, "id": loc.get("id"), "address": loc["address"], "error": "Address could not be found", "coverage": []})
            else:
                locs.append(location_answer(i, loc, self.speed))
        return httpx.Response(200, json={"status": 200, "messages": {}, "data": {"country": "US", "datasets": body["datasets"], "locations": locs}})


@pytest.fixture
def fake(monkeypatch):
    f = FakeAPI()
    clock = [time.time()]
    monkeypatch.setattr(settings, "coveragemap_key", FAKE_KEY)
    monkeypatch.setattr(settings, "coveragemap_plan", "trial")
    monkeypatch.setattr(settings, "coveragemap_monthly_units", 400)
    monkeypatch.setattr(settings, "coveragemap_refresh_days", 30)
    monkeypatch.setattr(settings, "coveragemap_datasets", "summary,fcc-coverage,speed-tests")
    monkeypatch.setattr(coverage, "transport", httpx.MockTransport(f))
    monkeypatch.setattr(coverage, "_clock", lambda: clock[0])
    monkeypatch.setattr(push, "_sender", lambda sub, payload: True)   # the budget alert pushes to hub administrators: never for real
    f.clock = clock
    coverage.purge()
    yield f
    coverage.purge()


def _world(client, superuser, slug, *, lat=37.6866, lon=-96.9767, address="1200 Main St, Augusta, KS 67010"):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": slug.title(), "slug": slug}).json()["id"]
    body = {"name": "Yard", "address": address}
    if lat is not None:
        body.update(lat=lat, lon=lon, geocode_source="manual")
    loc = root.post(f"/api/orgs/{oid}/locations", json=body).json()
    users = {}
    for role in ("viewer", "admin"):
        email = f"{role}@{slug}.example"
        root.post(f"/api/orgs/{oid}/members", json={"email": email, "role": role, "password": PW})
        users[role] = _login(client, email, PW)
    return {"root": root, "org": oid, "loc": loc, **users}


# ---------------------------------------------------------------- pure parts

def test_count_units_billing_rules():
    all3 = ["summary", "fcc-coverage", "speed-tests"]
    with_tests = location_answer(0, {"id": "a"})
    no_tests = location_answer(1, {"id": "b"}, speed=False)
    err = {"index": 2, "id": "c", "error": "Address could not be found", "coverage": []}
    assert coverage.count_units({"locations": [with_tests]}, all3) == (4, [4])
    assert coverage.count_units({"locations": [no_tests]}, all3) == (2, [2])          # speed tests only when found
    assert coverage.count_units({"locations": [err]}, all3) == (0, [0])               # a location error is never billed
    assert coverage.count_units({"locations": [with_tests, no_tests, err]}, all3) == (6, [4, 2, 0])
    # the vendor's example: 10 locations, all three datasets, 6 with speed tests = 32 units
    ten = [location_answer(i, {"id": str(i)}, speed=i < 6) for i in range(10)]
    assert coverage.count_units({"locations": ten}, all3)[0] == 32
    # a dataset null on every coverage entry of the response was unavailable: not billed
    gone = location_answer(0, {"id": "a"})
    for e in gone["coverage"]:
        e["summary"] = None
    assert coverage.count_units({"locations": [gone]}, all3) == (3, [3])
    # ... but a summary null for some entries only is still billed
    part = location_answer(0, {"id": "a"})
    part["coverage"][0]["summary"] = None
    assert coverage.count_units({"locations": [part]}, all3) == (4, [4])
    # only requested datasets count
    assert coverage.count_units({"locations": [with_tests]}, ["summary"]) == (1, [1])
    assert coverage.count_units({"locations": [with_tests]}, ["speed-tests"]) == (2, [2])


def test_normalize_location():
    raw = location_answer(0, {"id": "x", "latitude": 37.1, "longitude": -97.2})
    # VZW LTE: nothing successful within 0.5 km, so 1 km is used
    raw["coverage"][0]["speedTest"]["uploadSpeed"] = metric(17.4, radii={"halfKilometer": (0, None), "oneKilometer": (9, 15.9), "twoKilometers": (30, 14.6)})
    n = coverage.normalize_location(raw)
    assert [c["code"] for c in n["carriers"]] == ["TMO", "VZW", "ATT"]   # best overall first (9.0, 8.6, 6.2)
    tmo, vzw, att = n["carriers"]
    assert tmo["best"] == 9.0 and tmo["best_technology"] == "5g" and set(tmo["tech"]) == {"lte", "5g"}
    lte = vzw["tech"]["lte"]
    assert lte["summary"] == {"overall": 8.6, "performance": 8.1, "coverage": 9.1, "reliability": 7.6, "is_fully_covered": True, "source": "measured", "accuracy": "exact"}
    assert lte["fcc"]["signal"] == {"point": -80.0, "r05": -81.0, "r1": -82.0, "r2": -84.0}
    assert lte["fcc"]["coverage"] == {"r05": 1, "r1": 0.98, "r2": 0.9}
    up = lte["speed"]["upload"]
    assert up["radius"] == "1km" and up["med"] == 15.9 and up["count"] == 9 and up["failed"] == 1
    assert lte["speed"]["download"]["radius"] == "0.5km"
    assert att["tech"]["5g"]["fcc"] is None and att["tech"]["5g"]["speed"] is None
    # tests only farther than 2 km: the closest tested area, with its distance
    far = metric(5.0, radii={"halfKilometer": (0, None), "oneKilometer": (0, None), "twoKilometers": (0, None)})
    far["distance"] = 4.2
    m = coverage.speed_metric(far)
    assert m["radius"] == "closest" and m["distance_km"] == 4.2 and m["med"] == 5.0
    # only failed tests nearby
    failed = {"accuracy": "low", "min": None, "med": None, "avg": None, "max": None, "count": 0, "failedCount": 3, "distance": 0.1,
              "halfKilometer": {"accuracy": "low", "min": None, "med": None, "avg": None, "max": None, "count": 0, "failedCount": 3}}
    m = coverage.speed_metric(failed)
    assert m["count"] == 0 and m["failed"] == 3 and m["med"] is None
    assert coverage.speed_metric(None) is None


def test_camera_need_and_upload_fit():
    servers = [{"summary": {"cameras": [{"id": "a"}, {"id": "b"}, {"id": "c", "record_stream": "sub"}, {"id": "d"}],
                            "bandwidth": {"mbps": 9.5, "cameras": {"a": 4.5, "b": 5.0, "c": None, "d": 0}}}},
               {"retired_at": 1.0, "summary": {"cameras": [{"id": "z"}]}}]
    need = coverage.camera_need(servers)
    assert need == {"mbps": 16.5, "cameras": 4, "measured": 2, "assumed": 2, "typical": False}   # 4.5 + 5 + 1 (sub) + 6
    # no summaries: the registry's enabled cameras at the assumption; nothing at all: the typical 5-camera site
    reg = [{"camera_id": "x", "enabled": True, "missing_since": None}, {"camera_id": "y", "enabled": False, "missing_since": None}]
    assert coverage.camera_need([], reg)["mbps"] == 6.0
    assert coverage.camera_need([]) == {"mbps": 30.0, "cameras": 5, "measured": 0, "assumed": 5, "typical": True}

    up = lambda med, count=10, failed=0, mn=None: {"med": med, "count": count, "failed": failed, "min": mn}  # noqa: E731
    assert coverage.upload_fit(20, up(30))["fit"] == "fits"
    assert coverage.upload_fit(20, up(29.9))["fit"] == "tight"
    assert coverage.upload_fit(20, up(20))["fit"] == "tight"
    assert coverage.upload_fit(20, up(19.9))["fit"] == "wont_fit"
    assert coverage.upload_fit(20, None)["fit"] == "unknown"
    assert coverage.upload_fit(20, up(None, count=0, failed=0))["fit"] == "unknown"
    assert coverage.upload_fit(20, up(None, count=0, failed=4))["fit"] == "wont_fit"      # people tried and could not connect
    assert coverage.upload_fit(20, up(40, count=6, failed=2))["fit"] == "tight"           # a quarter of the tests failed
    assert "slowest test 5" in coverage.upload_fit(20, up(40, mn=5))["reason"]
    assert coverage.upload_fit(0, up(40))["fit"] == "unknown"


def test_due_reasons(fake):
    loc = {"id": "l_x", "lat": 37.0, "lon": -97.0, "address": "1 Main St"}
    now = fake.clock[0]
    row = {"fetched_at": now - 86400, "lat": 37.0, "lon": -97.0, "plan_at_fetch": "trial", "data": {"basis": "point", "address": "1 Main St"}}
    assert coverage.due_reason(loc, None, now) == "never"
    assert coverage.due_reason(loc, row, now) is None
    assert coverage.due_reason(loc, {**row, "fetched_at": now - 31 * 86400}, now) == "age"
    assert coverage.due_reason({**loc, "lat": 37.0005}, row, now) is None             # ~55 m
    assert coverage.due_reason({**loc, "lat": 37.0015}, row, now) == "moved"          # ~167 m
    assert coverage.due_reason({**loc, "lat": None, "lon": None, "address": ""}, row, now) is None   # nothing to look up
    settings.coveragemap_plan = "paid"
    assert coverage.due_reason(loc, row, now) == "plan"                               # trial data on a paid plan
    assert coverage.due_reason(loc, {**row, "plan_at_fetch": "paid"}, now) is None


# ---------------------------------------------------------------- through the API

def test_request_shape_refresh_and_trial_gating(client, superuser, fake):
    w = _world(client, superuser, "cov-trial")
    lid = w["loc"]["id"]
    me = w["root"].get("/auth/me").json()["coverage"]
    assert me == {"enabled": True, "plan": "trial", "visible": True, "evaluation": True, "cost_per_lookup": 4}
    assert w["admin"].get("/auth/me").json()["coverage"]["visible"] is False

    r = w["root"].post(f"/api/locations/{lid}/coverage/refresh")
    assert r.status_code == 200, r.text
    req = fake.requests[-1]
    assert req.method == "POST" and req.url.path == "/api/v1/coverage" and req.url.host == "enterprise.coveragemap.com"
    assert req.headers["authorization"] == f"Bearer {FAKE_KEY}" and "apiKey" not in str(req.url)
    body = json.loads(req.content)
    assert body["datasets"] == ["summary", "fcc-coverage", "speed-tests"] and body["technologies"] == ["lte", "5g"]
    assert "providers" not in body                                  # the default carriers
    assert body["locations"] == [{"id": lid, "latitude": 37.6866, "longitude": -96.9767}]   # the point wins over the address
    p = r.json()
    assert p["evaluation"] is True and p["units"] == 4 and p["data"]["carriers"][0]["code"] == "TMO" and p["can_refresh"] is True
    assert p["fits"]["VZW"]["lte"]["fit"] == "wont_fit" and p["fits"]["TMO"]["5g"]["fit"] == "fits"   # need: the typical 30 Mbit/s
    assert p["need"]["typical"] is True and p["source"].startswith("Data: CoverageMap")
    assert coverage.usage()["units"] == 4 and coverage.usage()["calls"] == 1
    row = coverage.row_of(lid)
    assert row["plan_at_fetch"] == "trial" and row["data"]["raw"]["coverage"] and row["lat"] == 37.6866

    # trial: evaluation only, for hub administrators; enforced by the API, not just hidden in the UI
    for who in ("viewer", "admin"):
        assert w[who].get(f"/api/locations/{lid}/coverage").status_code == 403
        assert w[who].post(f"/api/locations/{lid}/coverage/refresh").status_code == 403
        assert w[who].post("/api/coverage/check", json={"lat": 37.1, "lon": -97.1, "org_id": w["org"]}).status_code == 403
        assert w[who].get("/api/hub/coverage").status_code == 403
    assert w["root"].get(f"/api/locations/{lid}/coverage").json()["data"]["carriers"]

    # once per Site per 10 minutes
    r = w["root"].post(f"/api/locations/{lid}/coverage/refresh")
    assert r.status_code == 429 and "Retry-After" in r.headers
    fake.clock[0] += 601
    assert w["root"].post(f"/api/locations/{lid}/coverage/refresh").status_code == 200
    assert coverage.usage() == {"month": coverage.month_of(), "units": 8, "calls": 2}
    assert len(fake.requests) == 2

    # an address-only Site is looked up by its address
    loc2 = w["root"].post(f"/api/orgs/{w['org']}/locations", json={"name": "No pin", "address": "5 Elm St, Augusta, KS"}).json()
    assert w["root"].post(f"/api/locations/{loc2['id']}/coverage/refresh").status_code == 200
    assert fake.bodies()[-1]["locations"] == [{"id": loc2["id"], "address": "5 Elm St, Augusta, KS"}]
    assert coverage.row_of(loc2["id"])["data"]["basis"] == "address"


def test_paid_plan_gating_and_trial_rows_hidden(client, superuser, fake):
    w = _world(client, superuser, "cov-paid")
    lid = w["loc"]["id"]
    assert w["root"].post(f"/api/locations/{lid}/coverage/refresh").status_code == 200   # fetched during the trial
    settings.coveragemap_plan = "paid"
    fake.clock[0] += 601
    # customers never see trial-era data, even after switching to paid; the hub administrator still does
    p = w["viewer"].get(f"/api/locations/{lid}/coverage").json()
    assert p["data"] is None and p["hidden"] is True and p["can_refresh"] is False and p["error"] is None
    assert w["root"].get(f"/api/locations/{lid}/coverage").json()["data"] is not None
    assert w["viewer"].post(f"/api/locations/{lid}/coverage/refresh").status_code == 403   # viewers see, admins fetch
    r = w["admin"].post(f"/api/locations/{lid}/coverage/refresh")
    assert r.status_code == 200 and r.json()["plan_at_fetch"] == "paid"
    p = w["viewer"].get(f"/api/locations/{lid}/coverage").json()
    assert p["data"]["carriers"][0]["code"] == "TMO" and p["hidden"] is False and p["evaluation"] is False
    assert w["viewer"].get("/auth/me").json()["coverage"]["visible"] is True
    # someone of another customer
    other = _world(client, superuser, "cov-other")
    assert other["admin"].get(f"/api/locations/{lid}/coverage").status_code == 403
    assert other["admin"].post(f"/api/locations/{lid}/coverage/refresh").status_code == 403
    # a viewer granted only another Site of the customer
    loc2 = w["root"].post(f"/api/orgs/{w['org']}/locations", json={"name": "Other yard"}).json()
    vid = next(m["id"] for m in w["root"].get(f"/api/orgs/{w['org']}/members").json() if m["email"] == "viewer@cov-paid.example")
    w["root"].put(f"/api/orgs/{w['org']}/members/{vid}/access", json={"all_sites": False, "location_ids": [loc2["id"]]})
    assert w["viewer"].get(f"/api/locations/{lid}/coverage").status_code == 403


def test_failures_keep_old_data_and_never_leak_the_key(client, superuser, fake):
    w = _world(client, superuser, "cov-fail")
    lid = w["loc"]["id"]
    assert w["root"].post(f"/api/locations/{lid}/coverage/refresh").status_code == 200
    before = coverage.row_of(lid)
    fake.mode = "down"
    fake.clock[0] += 601
    r = w["root"].post(f"/api/locations/{lid}/coverage/refresh")
    assert r.status_code == 502 and "previous data is kept" in r.json()["detail"] and FAKE_KEY not in r.text
    row = coverage.row_of(lid)
    assert row["fetched_at"] == before["fetched_at"] and row["data"] == before["data"] and FAKE_KEY not in row["error"]
    p = w["root"].get(f"/api/locations/{lid}/coverage").json()
    assert p["data"] is not None and p["error"] and FAKE_KEY not in json.dumps(p)
    # the API's own refusal (400): its messages reach the admin; nothing billed
    fake.mode = "refuse"
    fake.clock[0] += 601
    r = w["root"].post(f"/api/locations/{lid}/coverage/refresh")
    assert r.status_code == 502 and "Unknown dataset" in r.json()["detail"]
    assert coverage.usage()["units"] == 4
    # an address it cannot find: 422, no units
    fake.mode = "ok"
    fake.bad_addresses.add("Nowhere 1")
    loc2 = w["root"].post(f"/api/orgs/{w['org']}/locations", json={"name": "Lost", "address": "Nowhere 1"}).json()
    r = w["root"].post(f"/api/locations/{loc2['id']}/coverage/refresh")
    assert r.status_code == 422 and "could not be found" in r.json()["detail"]
    assert coverage.usage()["units"] == 4 and coverage.row_of(loc2["id"])["fetched_at"] is None


def test_budget_cap_stops_calls_and_alerts(client, superuser, fake):
    settings.coveragemap_monthly_units = 8   # two lookups at the worst case (4 each)
    settings.coveragemap_plan = "paid"
    w = _world(client, superuser, "cov-budget")
    ids = [w["loc"]["id"]] + [w["root"].post(f"/api/orgs/{w['org']}/locations", json={"name": f"S{i}", "lat": 37 + i / 10, "lon": -97.0}).json()["id"]
                              for i in range(1, 4)]
    assert w["root"].post(f"/api/locations/{ids[0]}/coverage/refresh").status_code == 200
    st = asyncio.run(coverage.refresh_due(ids=ids))   # 3 due, room for 1 more
    assert st["fetched"] == 1 and st["budget_stop"] is True and st["units"] == 4
    assert coverage.usage()["units"] == 8 and len(fake.requests) == 2
    assert len(fake.bodies()[-1]["locations"]) == 1   # the batch was cut to what the budget allows
    alert = db.one(sa.select(db.alerts).where(db.alerts.c.org_id == HUB_ORG, db.alerts.c.kind == "coverage_budget", db.alerts.c.closed_at.is_(None)))
    assert alert and alert["key"] == coverage.month_of() and "8 of 8" in alert["detail"]["text"]
    # manual refreshes and checks stop too, without calling the API
    left = next(i for i in ids if coverage.row_of(i) is None)
    r = w["root"].post(f"/api/locations/{left}/coverage/refresh")
    assert r.status_code == 409 and "budget" in r.json()["detail"]
    assert w["root"].post("/api/coverage/check", json={"lat": 30.0, "lon": -90.0}).status_code == 409
    assert len(fake.requests) == 2
    rep = w["root"].get("/api/hub/coverage").json()
    assert rep["budget_reached"] is True and rep["alert_open"] is True and rep["remaining"] == 0 and rep["plan"] == "paid"
    # no cap: calls go through again
    settings.coveragemap_monthly_units = 0
    assert w["root"].post(f"/api/locations/{left}/coverage/refresh").status_code == 200


def test_refresh_due_age_move_and_plan(client, superuser, fake):
    w = _world(client, superuser, "cov-due")
    lid = w["loc"]["id"]
    never = w["root"].post(f"/api/orgs/{w['org']}/locations", json={"name": "Fresh", "lat": 38.0, "lon": -97.5}).json()["id"]
    assert w["root"].post(f"/api/locations/{lid}/coverage/refresh").status_code == 200
    ids = [lid, never]
    # trial: only Sites someone looked up by hand; nothing is due yet
    assert asyncio.run(coverage.refresh_due(ids=ids))["due"] == 0
    fake.clock[0] += 31 * 86400
    st = asyncio.run(coverage.refresh_due(ids=ids))
    assert st["due"] == 1 and st["fetched"] == 1 and fake.bodies()[-1]["locations"][0]["id"] == lid
    # the pin moved more than 100 m: due; a few meters: not
    with db.engine().begin() as c:
        c.execute(sa.update(db.locations).where(db.locations.c.id == lid).values(lat=37.6869))   # ~33 m
    assert asyncio.run(coverage.refresh_due(ids=[lid]))["due"] == 0
    with db.engine().begin() as c:
        c.execute(sa.update(db.locations).where(db.locations.c.id == lid).values(lat=37.6890))   # ~270 m
    assert asyncio.run(coverage.refresh_due(ids=[lid]))["fetched"] == 1
    assert coverage.row_of(lid)["lat"] == 37.689
    # paid: trial rows are refetched, and every located Site without data is looked up (one request for both)
    settings.coveragemap_plan = "paid"
    n = len(fake.requests)
    st = asyncio.run(coverage.refresh_due(ids=ids))
    assert st["fetched"] == 2 and len(fake.requests) == n + 1 and {x["id"] for x in fake.bodies()[-1]["locations"]} == set(ids)
    assert all(coverage.row_of(i)["plan_at_fetch"] == "paid" for i in ids)


def test_moving_the_pin_kicks_a_refresh(client, superuser, fake, monkeypatch):
    w = _world(client, superuser, "cov-move")
    lid = w["loc"]["id"]
    assert w["root"].post(f"/api/locations/{lid}/coverage/refresh").status_code == 200
    kicked: list = []

    async def fake_due(ids=None, now=None):
        kicked.append(ids)
        return {}
    monkeypatch.setattr(coverage, "refresh_due", fake_due)
    assert w["admin"].patch(f"/api/locations/{lid}", json={"notes": "gate code 1234"}).status_code == 200
    assert w["admin"].patch(f"/api/locations/{lid}", json={"lat": 37.68665, "lon": -96.9767}).status_code == 200   # ~6 m
    time.sleep(0.2)
    assert kicked == []
    assert w["admin"].patch(f"/api/locations/{lid}", json={"lat": 37.70, "lon": -96.9767}).status_code == 200      # ~1.5 km
    for _ in range(50):
        if kicked:
            break
        time.sleep(0.02)
    assert kicked == [[lid]]
    # deleting the Site deletes its coverage
    w["root"].delete(f"/api/locations/{lid}")
    assert coverage.row_of(lid) is None


def test_address_check_cache_and_gating(client, superuser, fake):
    settings.coveragemap_plan = "paid"
    w = _world(client, superuser, "cov-check")
    r = w["admin"].post("/api/coverage/check", json={"lat": 37.123456, "lon": -97.654321, "org_id": w["org"]})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["cached"] is False and p["units"] == 4 and p["data"]["carriers"][0]["code"] == "TMO" and p["need"]["cameras"] == 5
    assert fake.bodies()[-1]["locations"] == [{"id": "check", "latitude": 37.123456, "longitude": -97.654321}]
    # within ~11 m (4 decimals): from the cache, not billed again
    p = w["admin"].post("/api/coverage/check", json={"lat": 37.12349, "lon": -97.65428, "org_id": w["org"]}).json()
    assert p["cached"] is True and p["units"] == 0 and len(fake.requests) == 1
    assert coverage.usage()["units"] == 4
    # an address
    assert w["root"].post("/api/coverage/check", json={"address": "10187 SW US Hwy 54, Augusta, KS"}).json()["cached"] is False
    assert fake.bodies()[-1]["locations"] == [{"id": "check", "address": "10187 SW US Hwy 54, Augusta, KS"}]
    assert w["root"].post("/api/coverage/check", json={"address": "10187 sw us hwy 54  augusta ks"}).json()["cached"] is True
    # after REFRESH_DAYS it is looked up again
    fake.clock[0] += 31 * 86400
    assert w["admin"].post("/api/coverage/check", json={"lat": 37.1235, "lon": -97.6543, "org_id": w["org"]}).json()["cached"] is False
    # who: customer admins (naming their customer) and hub administrators; not viewers, not another customer's admin
    assert w["viewer"].post("/api/coverage/check", json={"lat": 37.1, "lon": -97.1, "org_id": w["org"]}).status_code == 403
    assert w["admin"].post("/api/coverage/check", json={"lat": 37.1, "lon": -97.1}).status_code == 403
    other = _world(client, superuser, "cov-check-other")
    assert other["admin"].post("/api/coverage/check", json={"lat": 37.1, "lon": -97.1, "org_id": w["org"]}).status_code == 403
    assert w["admin"].post("/api/coverage/check", json={"lat": 37.1, "org_id": w["org"]}).status_code == 422
    # trial-era cache entries are not reused once paid
    settings.coveragemap_plan = "trial"
    w["root"].post("/api/coverage/check", json={"lat": 40.0, "lon": -100.0})
    settings.coveragemap_plan = "paid"
    n = len(fake.requests)
    assert w["admin"].post("/api/coverage/check", json={"lat": 40.0, "lon": -100.0, "org_id": w["org"]}).json()["cached"] is False
    assert len(fake.requests) == n + 1


def test_purge_cli_and_switched_off(client, superuser, fake, monkeypatch, capsys):
    w = _world(client, superuser, "cov-purge")
    lid = w["loc"]["id"]
    w["root"].post(f"/api/locations/{lid}/coverage/refresh")
    w["root"].post("/api/coverage/check", json={"lat": 41.0, "lon": -99.0})
    # switching the key off hides everything but deletes nothing
    settings.coveragemap_key = ""
    assert w["root"].get(f"/api/locations/{lid}/coverage").status_code == 404
    assert w["root"].post("/api/coverage/check", json={"lat": 41.0, "lon": -99.0}).status_code == 404
    assert w["root"].get("/auth/me").json()["coverage"] == {"enabled": False, "plan": None, "visible": False, "evaluation": False, "cost_per_lookup": 0}
    rep = w["root"].get("/api/hub/coverage").json()
    assert rep["enabled"] is False and rep["stored_sites"] == 1 and rep["units"] == 8
    assert coverage.row_of(lid) is not None
    # python -m hub coverage purge
    from hub import __main__ as cli
    monkeypatch.setattr(sys, "argv", ["hub", "coverage", "purge"])
    cli.main()
    out = capsys.readouterr().out
    assert "1 site(s)" in out and "1 cached address check(s)" in out
    assert db.rows(sa.select(db.site_coverage)) == [] and db.rows(sa.select(db.coverage_usage)) == []
    assert db.rows(sa.select(db.kv).where(db.kv.c.key.like(coverage.CHECK_PREFIX + "%"))) == []
