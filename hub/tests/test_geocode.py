"""Site addresses: the two geocoders (Census for US street addresses, Nominatim with rewrites), caching and the hub-wide
1 request/s spacing (fake httpx transports, never the network), the time zone at a point, the lookup routes'
permissions, saving a located Site, the backfill and its trust guard."""
import asyncio

import httpx
import pytest
import sqlalchemy as sa

from hub import db, geocode
from test_access import _login

AUGUSTA = {
    "lat": "37.6866800", "lon": "-96.9767000", "display_name": "1200, Main Street, Augusta, Butler County, Kansas, 67010, United States",
    "address": {"house_number": "1200", "road": "Main Street", "city": "Augusta", "county": "Butler County", "state": "Kansas",
                "ISO3166-2-lvl4": "US-KS", "postcode": "67010", "country": "United States", "country_code": "us"},
}
US54 = {   # what OSM has for "10187 US-54, Augusta, KS": the road, no house number
    "lat": "37.6650000", "lon": "-96.9300000", "display_name": "US 54, Augusta, Butler County, Kansas, 67010, United States",
    "address": {"road": "US 54", "city": "Augusta", "state": "Kansas", "ISO3166-2-lvl4": "US-KS", "postcode": "67010", "country": "United States", "country_code": "us"},
}
VAGUE = {   # "Building 2" matched some building somewhere
    "lat": "40.0", "lon": "-75.0", "display_name": "Building 2, Somewhere, Pennsylvania, 19000, United States",
    "address": {"building": "Building 2", "city": "Somewhere", "state": "Pennsylvania", "postcode": "19000", "country": "United States", "country_code": "us"},
}
CENSUS_HIT = {
    "matchedAddress": "10187 US HWY 54, AUGUSTA, KS, 67010", "coordinates": {"x": -96.9312, "y": 37.6641},
    "addressComponents": {"fromAddress": "10001", "toAddress": "10299", "preType": "US HWY", "streetName": "54", "suffixType": "",
                          "city": "AUGUSTA", "state": "KS", "zip": "67010"},
}
OWNER = "10187 SW US HWY 54 Augusta KS 67010"


class Fake:
    """Nominatim and the Census geocoder: records every request (and when, on the fake clock); `fail` makes both 503."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.nom_at: list[float] = []
        self.fail = False
        self.clock = [1000.0]

    def census(self) -> list[httpx.Request]:
        return [r for r in self.calls if r.url.host == "geocoding.geo.census.gov"]

    def nominatim(self) -> list[str]:
        return [r.url.params.get("q", "") for r in self.calls if r.url.host != "geocoding.geo.census.gov" and r.url.path.endswith("/search")]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if self.fail:
            return httpx.Response(503)
        if req.url.host == "geocoding.geo.census.gov":
            a = req.url.params.get("address", "")
            return httpx.Response(200, json={"result": {"input": {"address": {"address": a}}, "addressMatches": [CENSUS_HIT] if a.startswith("10187") else []}})
        self.nom_at.append(self.clock[0])
        if req.url.path.endswith("/reverse"):
            return httpx.Response(200, json=AUGUSTA)
        q = req.url.params.get("q", "").lower()
        if "us-54" in q:
            return httpx.Response(200, json=[US54])
        if "building" in q:
            return httpx.Response(200, json=[VAGUE])
        if "10187" in q:
            return httpx.Response(200, json=[])   # OSM can't place the owner's address as typed
        return httpx.Response(200, json=[AUGUSTA] if "augusta" in q or "main" in q else [])


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    sleeps: list[float] = []

    async def sleep(s):
        sleeps.append(s)
        f.clock[0] += s

    monkeypatch.setattr(geocode, "transport", httpx.MockTransport(f))
    monkeypatch.setattr(geocode, "_sleep", sleep)
    monkeypatch.setattr(geocode, "_clock", lambda: f.clock[0])
    monkeypatch.setattr(geocode, "_last_call", -1e9)
    monkeypatch.setattr(geocode, "_last_census", -1e9)
    f.sleeps = sleeps
    with db.engine().begin() as c:   # every test starts with a cold cache (and the one-off heal not yet run)
        c.execute(sa.delete(db.kv).where(db.kv.c.key.like("geo:%")))
    geocode._asks.clear()
    return f


def _spaced(times: list[float], gap: float) -> bool:
    return all(b - a >= gap - 1e-9 for a, b in zip(times, times[1:]))


def test_tz_for():
    assert geocode.tz_for(37.69, -96.97) == "America/Chicago"   # Augusta, Kansas
    assert geocode.tz_for(51.5, -0.12) == "Europe/London"
    assert geocode.tz_for(0.0, -30.0) is None                   # open Atlantic: only an Etc/ zone
    assert geocode.tz_for(None, 1.0) is None


def test_format_address():
    p = geocode.parts_from(AUGUSTA)
    assert p["street"] == "Main Street" and p["city"] == "Augusta" and p["state_code"] == "KS" and p["country_code"] == "US"
    assert geocode.format_address(p) == "1200 Main Street, Augusta, KS 67010, United States"
    de = geocode.parts_from({"address": {"road": "Unter den Linden", "house_number": "77", "city": "Berlin", "postcode": "10117",
                                         "state": "Berlin", "country": "Germany", "country_code": "de"}})
    assert geocode.format_address(de) == "Unter den Linden 77, Berlin, Berlin 10117, Germany"
    c = geocode.census_result_from(CENSUS_HIT)
    assert c["display"] == "10187 US Hwy 54, Augusta, KS 67010, United States" and c["source"] == "census"
    assert {k: c["address_parts"][k] for k in ("house_number", "street", "city", "state", "state_code", "postcode", "country", "country_code")} == {
        "house_number": "10187", "street": "US Hwy 54", "city": "Augusta", "state": "Kansas", "state_code": "KS", "postcode": "67010",
        "country": "United States", "country_code": "US"}
    assert c["lat"] == pytest.approx(37.6641) and c["lon"] == pytest.approx(-96.9312) and c["timezone"] == "America/Chicago"
    assert geocode.title_case("1ST ST NE") == "1st St NE"


def test_query_shapes(monkeypatch):
    assert geocode.house_number_of(OWNER) == "10187" and geocode.zip_of(OWNER) == "67010" and geocode.has_us_state(OWNER)
    assert geocode.house_number_of("Building 2") is None and geocode.zip_of("Building 2") is None
    assert geocode.zip_of("12345 Main St") is None   # a house number is not a ZIP
    assert geocode.provider_order(OWNER) == ["census", "nominatim"]
    assert geocode.provider_order("Building 2") == ["nominatim"]
    assert geocode.provider_order("Augusta, Kansas") == ["nominatim"]
    assert geocode.provider_order("12 High St, Leeds") == ["census", "nominatim"]       # a US hub: every street address
    monkeypatch.setattr(geocode.settings, "geocoder_country", "GB")
    assert geocode.provider_order("12 High St, Leeds") == ["nominatim", "census"]
    assert geocode.provider_order("1200 Main St, Augusta KS") == ["census", "nominatim"]   # names a US state
    assert geocode.nominatim_variants(OWNER) == [OWNER, "10187 US-54 Augusta KS", "10187 US Highway 54 Augusta KS", "10187 US HWY 54 Augusta KS"]
    assert geocode.nominatim_variants("10187 SW US Highway 54, Augusta, KS 67010")[:2] == ["10187 SW US Highway 54, Augusta, KS 67010", "10187 US-54, Augusta, KS"]
    assert geocode.nominatim_variants("Building 2") == ["Building 2"]


async def test_census_first_for_a_us_street_address(fake):
    r = await geocode.search(OWNER)
    assert r[0]["display"] == "10187 US Hwy 54, Augusta, KS 67010, United States" and r[0]["source"] == "census"
    req = fake.census()[0]
    assert req.url.params["address"] == OWNER and req.url.params["benchmark"] == "Public_AR_Current" and req.url.params["format"] == "json"
    assert req.headers["user-agent"] == geocode.USER_AGENT
    assert fake.nominatim() == []
    assert await geocode.search(OWNER.lower()) == r and len(fake.calls) == 1   # cached


async def test_nominatim_rewrites_when_census_is_off(fake, monkeypatch):
    monkeypatch.setattr(geocode.settings, "census_url", "off")
    r = await geocode.search(OWNER)
    assert fake.nominatim() == [OWNER, "10187 US-54 Augusta KS"] and fake.census() == []
    assert r[0]["source"] == "nominatim" and r[0]["address_parts"]["street"] == "US 54"
    assert _spaced(fake.nom_at, geocode.MIN_INTERVAL_S)


async def test_census_as_fallback_after_nominatim(fake, monkeypatch):
    monkeypatch.setattr(geocode.settings, "geocoder_country", "GB")
    r = await geocode.search("10187 Prairie Road")   # no state, no ZIP: Nominatim first
    hosts = [c.url.host for c in fake.calls]
    assert hosts[0] == "nominatim.openstreetmap.org" and hosts[-1] == "geocoding.geo.census.gov"
    assert r[0]["source"] == "census"


async def test_search_caches_and_spaces_requests(fake):
    r = await geocode.search("1200 Main St, Augusta KS")   # Census has nothing, Nominatim does
    assert r[0]["display"] == "1200 Main Street, Augusta, KS 67010, United States" and r[0]["source"] == "nominatim"
    assert r[0]["lat"] == pytest.approx(37.68668) and r[0]["timezone"] == "America/Chicago"
    req = next(c for c in fake.calls if c.url.host == "nominatim.openstreetmap.org")
    assert req.headers["user-agent"] == geocode.USER_AGENT
    assert req.url.params["format"] == "jsonv2" and req.url.params["addressdetails"] == "1"
    n = len(fake.calls)
    # the same query, differently spaced and cased: the cache answers
    assert await geocode.search("  1200 main st,augusta   ks ") == r
    assert len(fake.calls) == n
    # new queries back to back: Nominatim is never asked twice within a second
    await geocode.search("Augusta Kansas")
    await geocode.search("Main Street Wichita")
    assert len(fake.nominatim()) == 3 and _spaced(fake.nom_at, geocode.MIN_INTERVAL_S)


async def test_concurrent_lookups_are_serialised(fake):
    await asyncio.gather(*(geocode.search(f"Main Street {n}") for n in "abc"))
    assert len(fake.nominatim()) == 3 and _spaced(fake.nom_at, geocode.MIN_INTERVAL_S)


async def test_failures_degrade_and_are_not_cached(fake):
    fake.fail = True
    assert await geocode.search("Augusta Kansas") == []
    assert await geocode.search(OWNER) == []
    assert await geocode.reverse(37.68, -96.97) is None
    fake.fail = False
    assert (await geocode.search("Augusta Kansas"))[0]["address_parts"]["city"] == "Augusta"
    assert (await geocode.search(OWNER))[0]["source"] == "census"
    assert (await geocode.reverse(37.68, -96.97))["timezone"] == "America/Chicago"
    n = len(fake.calls)
    await geocode.reverse(37.680001, -96.970001)   # same point to ~1 m: cached
    assert len(fake.calls) == n


async def test_custom_endpoint_and_key(fake, monkeypatch):
    monkeypatch.setattr(geocode.settings, "geocoder_url", "https://geo.example.com/nominatim/")
    monkeypatch.setattr(geocode.settings, "geocoder_key", "k123")
    await geocode.search("Augusta Kansas")
    req = fake.calls[-1]
    assert str(req.url).startswith("https://geo.example.com/nominatim/search?") and req.url.params["key"] == "k123"
    assert geocode.source() == "geocoder"


def _org(root, slug):
    return root.post("/api/orgs", json={"name": slug.title(), "slug": slug}).json()["id"]


def test_routes_need_sign_in_and_rate_limit(client, superuser, fake):
    from fastapi.testclient import TestClient
    from hub.api import app
    anon = TestClient(app, base_url="http://testserver")
    assert anon.get("/api/geocode?q=Augusta Kansas").status_code == 401
    assert anon.get("/api/geocode/reverse?lat=37&lon=-97").status_code == 401
    root = _login(client, superuser["email"], superuser["password"])
    hits = root.get("/api/geocode", params={"q": "Augusta Kansas"}).json()
    assert hits[0]["lat"] == pytest.approx(37.68668) and hits[0]["timezone"] == "America/Chicago" and hits[0]["address_parts"]["postcode"] == "67010"
    assert root.get("/api/geocode", params={"q": "ab"}).json() == []
    assert root.get("/api/geocode/reverse", params={"lat": 37.68, "lon": -96.97}).json()["display"].startswith("1200 Main Street")
    assert root.get("/api/geocode/reverse", params={"lat": 91, "lon": 0}).status_code == 422
    assert root.get("/api/geocode/timezone", params={"lat": 37.69, "lon": -96.97}).json() == {"timezone": "America/Chicago"}
    for _ in range(geocode.USER_LIMIT):
        root.get("/api/geocode", params={"q": "Augusta Kansas"})
    assert root.get("/api/geocode", params={"q": "Augusta Kansas"}).status_code == 429
    me = root.get("/auth/me").json()
    assert me["map"]["tiles"] == geocode.MAP_TILES and "OpenStreetMap" in me["map"]["attribution"]


def test_saving_a_located_site(client, superuser, fake):
    root = _login(client, superuser["email"], superuser["password"])
    oid = _org(root, "geo-co")
    hit = root.get("/api/geocode", params={"q": "1200 Main Street Augusta"}).json()[0]
    loc = root.post(f"/api/orgs/{oid}/locations", json={"name": "Augusta yard", "address": hit["display"], "lat": hit["lat"], "lon": hit["lon"],
                                                        "address_parts": hit["address_parts"], "geocode_source": "nominatim"}).json()
    assert loc["lat"] == pytest.approx(37.68668) and loc["timezone"] == "America/Chicago"   # filled from the point
    assert loc["address_parts"]["city"] == "Augusta" and loc["geocode_source"] == "nominatim" and loc["geocoded_at"]
    lid = loc["id"]
    # a dragged marker: new point, the explicit time zone stays
    root.patch(f"/api/locations/{lid}", json={"timezone": "America/Denver"})
    moved = root.patch(f"/api/locations/{lid}", json={"lat": 37.7, "lon": -96.98, "geocode_source": "marker"}).json()
    assert moved["lat"] == 37.7 and moved["timezone"] == "America/Denver" and moved["geocode_source"] == "marker"
    assert moved["address_parts"]["city"] == "Augusta"   # parts not sent: kept
    # emptying the time zone of a located Site refills it from the point
    assert root.patch(f"/api/locations/{lid}", json={"timezone": None}).json()["timezone"] == "America/Chicago"
    # lat without lon, out of range
    assert root.patch(f"/api/locations/{lid}", json={"lat": 37.7}).status_code == 422
    assert root.patch(f"/api/locations/{lid}", json={"lat": 100, "lon": 0}).status_code == 422
    # junk in address_parts is dropped
    junk = root.patch(f"/api/locations/{lid}", json={"lat": 37.7, "lon": -96.98, "address_parts": {"city": "X" * 500, "evil": "<script>"}}).json()
    assert junk["address_parts"]["city"] == "X" * 120 and "evil" not in junk["address_parts"]
    # a new address text without a point: the old point no longer describes it
    re = root.patch(f"/api/locations/{lid}", json={"address": "somewhere else entirely"}).json()
    assert re["lat"] is None and re["address_parts"] is None and re["timezone"] == "America/Chicago"
    # the Sites list and the fleet carry the place
    assert {"lat", "lon", "address_parts"} <= set(root.get(f"/api/orgs/{oid}/locations").json()[0])


def test_viewer_cannot_set_a_point(client, superuser, fake):
    root = _login(client, superuser["email"], superuser["password"])
    oid = _org(root, "geo-view")
    lid = root.post(f"/api/orgs/{oid}/locations", json={"name": "Shop"}).json()["id"]
    root.post(f"/api/orgs/{oid}/members", json={"email": "geo-viewer@example.com", "role": "viewer", "password": "geo-viewer-pass-1", "all_sites": True})
    v = _login(client, "geo-viewer@example.com", "geo-viewer-pass-1")
    assert v.patch(f"/api/locations/{lid}", json={"lat": 37.7, "lon": -96.98}).status_code == 403
    assert v.get("/api/geocode", params={"q": "Augusta Kansas"}).status_code == 200   # looking up is fine for anyone signed in


def _st(s: dict) -> tuple:
    return s["located"], s["failed"], s["skipped"]


def test_backfill_guard():
    acc = geocode.backfill_accepts
    assert not acc("Building 2", geocode.parts_from(VAGUE))
    assert not acc("5 Building Row", geocode.parts_from(VAGUE))              # no house number in the hit, no ZIP to match
    assert acc(OWNER, geocode.parts_from(US54))                              # no house number, but the ZIP matches
    assert not acc("10187 SW US HWY 54 Augusta KS", geocode.parts_from(US54))  # ... and without a ZIP to compare, no
    assert acc("1200 Main Street Augusta", geocode.parts_from(AUGUSTA))
    assert acc(OWNER, geocode.census_result_from(CENSUS_HIT)["address_parts"])


async def test_backfill_is_idempotent_and_trusts_only_street_hits(client, superuser, fake):
    root = _login(client, superuser["email"], superuser["password"])
    oid = _org(root, "geo-fill")
    mk = lambda name, address, tz=None: root.post(f"/api/orgs/{oid}/locations", json={"name": name, "address": address, **({"timezone": tz} if tz else {})}).json()["id"]
    a = mk("A", "Augusta, Kansas")                                 # no house number: not even asked
    b = mk("Ironsight", "Building 2", "Europe/Paris")              # likewise ("Building 2" once landed somewhere arbitrary)
    c = mk("C", "1200 Main Street Augusta", "America/New_York")    # Nominatim, with a house number
    d = mk("Owner", OWNER)                                         # Census
    e = mk("E", "5 Building Row")                                  # a vague hit: refused
    ids = [a, b, c, d, e]
    assert _st(await geocode.backfill(ids=ids, now=5000.0)) == (2, 3, 0)
    rows = {r["id"]: r for r in db.rows(sa.select(db.locations).where(db.locations.c.id.in_(ids)))}
    assert rows[a]["lat"] is None and rows[b]["lat"] is None and rows[e]["lat"] is None
    assert not any("building 2" in q.lower() or "augusta, kansas" in q.lower() for q in fake.nominatim())
    assert rows[c]["lat"] == pytest.approx(37.68668) and rows[c]["timezone"] == "America/New_York"   # an existing time zone is kept
    assert rows[c]["address"] == "1200 Main Street Augusta"                                           # the typed address too
    assert rows[d]["geocode_source"] == "census" and rows[d]["lat"] == pytest.approx(37.6641) and rows[d]["timezone"] == "America/Chicago"
    assert rows[d]["address_parts"]["house_number"] == "10187"
    calls = len(fake.calls)
    # again: nothing to locate, the failed ones wait 24 h
    assert _st(await geocode.backfill(ids=ids, now=5000.0 + 3600)) == (0, 0, 3)
    assert len(fake.calls) == calls
    # a day later they are tried again (and fail again)
    assert _st(await geocode.backfill(ids=ids, now=5000.0 + geocode.RETRY_AFTER_S + 1)) == (0, 3, 0)
    # a changed address is tried at once
    root.patch(f"/api/locations/{a}", json={"address": "1200 Main St, Augusta KS"})
    assert _st(await geocode.backfill(ids=ids, now=5000.0 + geocode.RETRY_AFTER_S + 2))[0] == 1


async def test_backfill_heals_untrustworthy_pins_once(client, superuser, fake):
    root = _login(client, superuser["email"], superuser["password"])
    oid = _org(root, "geo-heal")
    t = 1.0
    def put(name, address, source, parts=None):
        lid = db.new_id("l_")
        db.insert(db.locations, {"id": lid, "org_id": oid, "name": name, "address": address, "timezone": "America/Chicago", "created_at": t, "updated_at": t,
                                 "lat": 40.0, "lon": -75.0, "address_parts": parts, "geocoded_at": t, "geocode_source": source})
        return lid
    bogus = put("Ironsight", "Building 2", "nominatim", geocode.parts_from(VAGUE))
    dragged = put("Dragged", "Building 3", "marker")
    good = put("Good", "1200 Main Street, Augusta, KS 67010", "nominatim", geocode.parts_from(AUGUSTA))
    st = await geocode.backfill(ids=[bogus, dragged, good], now=9000.0)
    assert st["cleared"] >= 1
    rows = {r["id"]: r for r in db.rows(sa.select(db.locations).where(db.locations.c.id.in_([bogus, dragged, good])))}
    assert rows[bogus]["lat"] is None and rows[bogus]["address_parts"] is None and rows[bogus]["geocode_source"] is None
    assert rows[dragged]["lat"] == 40.0 and rows[good]["lat"] == 40.0   # a person's pin, and a trustworthy one, stay
    # once: a later pin from a person's pick of a vague place is theirs to keep
    db.run(sa.update(db.locations).where(db.locations.c.id == bogus).values(lat=41.0, lon=-75.0, geocode_source="nominatim"))
    assert (await geocode.backfill(ids=[bogus], now=9001.0))["cleared"] == 0
    assert db.one(sa.select(db.locations).where(db.locations.c.id == bogus))["lat"] == 41.0


def test_soc_payloads_carry_the_place(client, superuser, fake):
    from hub import soc_api
    root = _login(client, superuser["email"], superuser["password"])
    oid = _org(root, "geo-soc")
    loc = root.post(f"/api/orgs/{oid}/locations", json={"name": "Yard", "address": "1200 Main Street, Augusta, KS 67010", "lat": 37.68668, "lon": -96.9767,
                                                        "address_parts": {"city": "Augusta", "state_code": "KS"}}).json()
    row = db.one(sa.select(db.locations).where(db.locations.c.id == loc["id"]))
    s = soc_api.soc_site(row, {}, {}, [])
    assert s["lat"] == pytest.approx(37.68668) and s["address"].startswith("1200 Main") and s["address_parts"]["city"] == "Augusta"
