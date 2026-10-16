"""Live-view timing (telemetry.py): the hub UI's beacon, who may send and read it, validation, the rate limit, the
per-server sample cap and the percentile math."""
import json

import pytest
from fastapi.testclient import TestClient

from hub import telemetry
from test_access import _login, server


@pytest.fixture(autouse=True)
def _fresh():
    telemetry.reset()
    yield
    telemetry.reset()


def report(sid: str, **over) -> dict:
    r = {"event": "first_frame", "server": sid, "camera": "cam1", "path": "cam1_sub", "quality": "sd", "route": "hub", "attempt": 1,
         "stages": {"offer": 4, "ice": 1500.4, "post": 120, "answer": 3, "connected": 200, "first_frame": 150}, "ttff_ms": 1977.6,
         "duration_s": None, "ice_timed_out": False}
    return r | over


def post(c, body, **headers):
    return c.post("/api/telemetry/live", content=json.dumps(body), headers={"Content-Type": "application/json", **headers})


@pytest.fixture(scope="module")
def world(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": "Telemetry Co", "slug": "telemetry-co"}).json()["id"]
    other = root.post("/api/orgs", json={"name": "Other Telemetry Co", "slug": "other-telemetry-co"}).json()["id"]
    s = server(oid, "Front")
    s2 = server(other, "Elsewhere")
    root.post(f"/api/orgs/{oid}/members", json={"email": "viewer@telemetry.example", "role": "viewer", "password": "viewer-pass-123"})
    root.post(f"/api/orgs/{oid}/members", json={"email": "admin@telemetry.example", "role": "admin", "password": "admin-pass-1234"})
    root.post(f"/api/orgs/{other}/members", json={"email": "out@telemetry.example", "role": "admin", "password": "outsider-pass-1"})
    return {
        "sid": s["id"], "other_sid": s2["id"], "root": root, "anon": TestClient(client.app, base_url="http://testserver"),
        "viewer": _login(client, "viewer@telemetry.example", "viewer-pass-123"),
        "admin": _login(client, "admin@telemetry.example", "admin-pass-1234"),
        "outsider": _login(client, "out@telemetry.example", "outsider-pass-1"),
    }


def test_sign_in_and_access_required(world):
    sid = world["sid"]
    assert post(world["anon"], report(sid)).status_code == 401
    assert world["anon"].get(f"/api/telemetry/live?server={sid}").status_code == 401
    # another customer's admin may neither report on this server nor read it
    assert post(world["outsider"], report(sid)).status_code == 403
    assert world["outsider"].get(f"/api/telemetry/live?server={sid}").status_code == 403
    assert post(world["viewer"], report("s_nope")).status_code == 404
    assert telemetry.summary(sid)["n"] == 0


def test_viewer_reports_admins_read(world):
    sid = world["sid"]
    r = post(world["viewer"], report(sid), Origin="http://testserver")   # what sendBeacon sends: same-origin, cookie, Origin
    assert r.status_code == 204, r.text
    assert post(world["viewer"], report(sid, route="direct", ttff_ms=800)).status_code == 204
    # a viewer may report but not read the numbers
    assert world["viewer"].get(f"/api/telemetry/live?server={sid}").status_code == 403
    for who in ("admin", "root"):
        j = world[who].get(f"/api/telemetry/live?server={sid}").json()
        assert j["n"] == 2 and j["by_route"]["hub"]["n"] == 1 and j["by_route"]["direct"]["p50"] == 800
        assert j["stages"]["ice"] == {"p50": 1500, "p95": 1500} and j["ice_timeouts"] == 0 and j["no_frame"] == 0
    # the hub's CSRF check still refuses a foreign page driving the session
    assert post(world["viewer"], report(sid), Origin="https://evil.example").status_code == 403


def test_validation_and_clamping(world):
    sid, v = world["sid"], world["viewer"]
    assert v.post("/api/telemetry/live", content=json.dumps(report(sid)), headers={"Content-Type": "text/plain"}).status_code == 415
    assert post(v, report(sid, ttff_ms=-5)).status_code == 422
    assert post(v, report(sid, ttff_ms=10_000_000)).status_code == 422                          # absurd
    assert post(v, report(sid, stages={"ice": 999_999})).status_code == 422
    assert post(v, report(sid, route="carrier-pigeon")).status_code == 422
    assert post(v, report(sid, camera="../etc")).status_code == 422
    assert post(v, report(sid, quality="4k")).status_code == 422
    assert v.post("/api/telemetry/live", content=b"{not json", headers={"Content-Type": "application/json"}).status_code == 422
    assert post(v, report(sid, pad="x" * 5000)).status_code == 413                             # body cap
    assert telemetry.summary(sid)["n"] == 0
    # end of an attempt that never showed a frame: kept as no_frame; one that did play isn't a second sample
    assert post(v, report(sid, event="end", ttff_ms=None, duration_s=9.5, ice_timed_out=True, stages={"offer": 3})).status_code == 204
    assert post(v, report(sid, event="end", duration_s=10**9)).status_code == 204
    s = telemetry.summary(sid)
    assert s["n"] == 0 and s["no_frame"] == 1 and s["ice_timeouts"] == 1
    # values are rounded and a huge viewing time is clamped
    telemetry.record(telemetry.LiveIn.model_validate(report(sid, event="end", ttff_ms=None, duration_s=10**9)))
    kept = list(telemetry._samples[sid])
    assert kept[-1]["duration_s"] == telemetry.MAX_DURATION_S and kept[0]["stages"] == {"offer": 3}


def test_rate_limit_per_user(world, monkeypatch):
    sid = world["sid"]
    monkeypatch.setattr(telemetry, "RATE_LIMIT", 3)
    for _ in range(3):
        assert post(world["viewer"], report(sid)).status_code == 204
    assert post(world["viewer"], report(sid)).status_code == 429
    assert post(world["admin"], report(sid)).status_code == 204   # per user
    assert telemetry.rate_limited("u_x", now=1000) is False
    assert [telemetry.rate_limited("u_x", now=1001) for _ in range(2)] == [False, False]
    assert telemetry.rate_limited("u_x", now=1002) is True
    assert telemetry.rate_limited("u_x", now=1000 + telemetry.RATE_WINDOW_S + 0.5) is False   # the window moved on


def test_percentiles():
    assert telemetry.percentile([], 50) is None
    assert telemetry.percentile([7], 95) == 7
    vals = list(range(1, 11))
    assert telemetry.percentile(vals, 50) == 5.5
    assert telemetry.percentile(vals, 95) == pytest.approx(9.55)
    assert telemetry.percentile([10, 1, 5], 50) == 5   # sorted first
    for i, ms in enumerate([1000, 1200, 900, 3000, 1100]):
        telemetry.record(telemetry.LiveIn.model_validate(report("s_math", ttff_ms=ms, route="hub" if i % 2 else "direct")))
    s = telemetry.summary("s_math")
    assert s["n"] == 5 and s["ttff"] == {"p50": 1100, "p95": 2640}
    assert s["by_route"]["hub"] == {"n": 2, "p50": 2100, "p95": 2910} and s["by_route"]["direct"]["n"] == 3
    assert telemetry.summary("s_none") == {"n": 0, "ttff": {"p50": None, "p95": None},
                                           "by_route": {"hub": {"n": 0, "p50": None, "p95": None}, "direct": {"n": 0, "p50": None, "p95": None}},
                                           "stages": {k: {"p50": None, "p95": None} for k in telemetry.STAGES}, "ice_timeouts": 0, "no_frame": 0}


def test_deque_cap():
    for i in range(telemetry.KEEP + 25):
        telemetry.record(telemetry.LiveIn.model_validate(report("s_cap", ttff_ms=i)), now=i)
    kept = telemetry._samples["s_cap"]
    assert len(kept) == telemetry.KEEP and kept[0]["ttff_ms"] == 25   # the oldest went first
    assert telemetry.summary("s_cap")["n"] == telemetry.KEEP
