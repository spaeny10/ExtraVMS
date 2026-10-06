"""Site-scoped aggregates: events, alerts, Find, Ask, backups and usage per Site (and ?location= on the org-wide
routes) honour who sees which Site; the digest groups servers by Site; push titles say "Site · Server"."""
import asyncio
import json
import time

import pytest
import sqlalchemy as sa

from hub import db, digest, push
from hub.agents import registry
from test_access import _login, server


class FakeConn:
    """Stands in for a live tunnel: answers the GETs the fan-outs make; the assistant stream is refused."""

    def __init__(self, s: dict, n: int):
        self.site_id, self.site, self.n, self.last_seen = s["id"], s, n, 1e12

    async def call(self, method, path, query, headers, body, timeout):
        name = self.site["name"]
        if path == "/api/events":
            return 200, json.dumps([{"id": self.n, "camera_id": "cam1", "camera_class": "person", "start_ts": 100.0 + self.n}]).encode()
        if path == "/api/search":
            return 200, json.dumps([{"id": self.n, "synopsis": f"{name} person", "start_ts": 100.0 + self.n}]).encode()
        if path == "/api/footage/search":
            return 200, b"[]"
        if path == "/api/briefings":
            return 200, json.dumps({"briefings": [{"headline": f"All calm at {name}", "text": f"All calm at {name}."}]}).encode()
        return 404, b""

    async def request(self, *a, **kw):
        raise RuntimeError("no assistant here")


@pytest.fixture(scope="module")   # one customer for the whole file (slugs are unique)
def world(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": "Agg Co", "slug": "agg-co"}).json()["id"]
    campus = root.post(f"/api/orgs/{oid}/locations", json={"name": "Campus"}).json()
    a, b = server(oid, "Gate", campus["id"]), server(oid, "Yard", campus["id"])
    solo = server(oid, "Solo")
    other_org = root.post("/api/orgs", json={"name": "Other Agg", "slug": "agg-other"}).json()["id"]
    foreign = root.post(f"/api/orgs/{other_org}/locations", json={"name": "Foreign"}).json()
    v = root.post(f"/api/orgs/{oid}/members", json={"email": "agg@agg.example", "role": "viewer", "password": "agg-pass-12345",
                                                   "all_sites": False, "location_ids": [campus["id"]]}).json()
    viewer = _login(client, "agg@agg.example", "agg-pass-12345")
    for i, s in enumerate((a, b, solo), 1):
        registry.by_site[s["id"]] = FakeConn(s, i)
    try:
        yield {"root": root, "viewer": viewer, "oid": oid, "campus": campus, "a": a, "b": b, "solo": solo, "foreign": foreign, "viewer_id": v["id"]}
    finally:
        for s in (a, b, solo):
            registry.by_site.pop(s["id"], None)


def test_events_per_site(world):
    root, viewer, oid, campus, a, b, solo = (world[k] for k in ("root", "viewer", "oid", "campus", "a", "b", "solo"))
    ev = root.get(f"/api/locations/{campus['id']}/events").json()
    assert [e["site_id"] for e in ev["events"]] == [b["id"], a["id"]] and {e["location_name"] for e in ev["events"]} == {"Campus"}
    ev = root.get(f"/api/locations/{campus['id']}/events?cameras={solo['id']}:cam1").json()
    assert ev["events"] == []   # a camera of another Site is ignored, never shown
    ev = root.get(f"/api/fleet/events?org={oid}&location={solo['location_id']}").json()
    assert [e["site_id"] for e in ev["events"]] == [solo["id"]] and ev["events"][0]["location_id"] == solo["location_id"]
    assert root.get(f"/api/fleet/events?org={oid}").json()["events"][0]["site_id"] == solo["id"]   # unfiltered: all three
    assert root.get(f"/api/fleet/events?org={oid}&location={world['foreign']['id']}").status_code == 404
    # the restricted viewer: their Site only, everywhere
    assert {e["site_id"] for e in viewer.get(f"/api/fleet/events?org={oid}").json()["events"]} == {a["id"], b["id"]}
    assert viewer.get(f"/api/locations/{solo['location_id']}/events").status_code == 403
    assert viewer.get(f"/api/fleet/events?org={oid}&location={solo['location_id']}").status_code == 403
    assert len(viewer.get(f"/api/locations/{campus['id']}/events").json()["events"]) == 2


def test_alerts_per_site(world):
    root, viewer, oid, campus, a, solo = (world[k] for k in ("root", "viewer", "oid", "campus", "a", "solo"))
    for s in (a, solo):
        db.insert(db.alerts, {"org_id": oid, "site_id": s["id"], "kind": "disk", "key": "", "opened_at": time.time(), "closed_at": None,
                              "acked_by": None, "acked_at": None, "detail": {}})
    rows = root.get(f"/api/locations/{campus['id']}/alerts").json()
    assert [r["site_id"] for r in rows] == [a["id"]] and rows[0]["location_name"] == "Campus" and rows[0]["site_name"] == "Gate"
    assert [r["site_id"] for r in root.get(f"/api/alerts?org={oid}&location={solo['location_id']}").json()] == [solo["id"]]
    assert len(root.get(f"/api/alerts?org={oid}").json()) == 2
    assert len(root.get(f"/api/locations/{campus['id']}/alerts?open=false").json()) == 1
    assert [r["site_id"] for r in viewer.get(f"/api/alerts?org={oid}").json()] == [a["id"]]
    assert viewer.get(f"/api/locations/{solo['location_id']}/alerts").status_code == 403


def test_search_and_ask_per_site(world):
    root, viewer, oid, campus, a, b, solo = (world[k] for k in ("root", "viewer", "oid", "campus", "a", "b", "solo"))
    r = root.get(f"/api/locations/{campus['id']}/search?q=person").json()
    assert {s["server_id"] for s in r["sites"]} == {a["id"], b["id"]} and r["offline"] == []
    e = r["events"][0]
    assert e["site_id"] == e["server_id"] and e["site_name"] == e["server_name"] and e["location_id"] == campus["id"] and e["location_name"] == "Campus"
    r = root.get(f"/api/fleet/search?org={oid}&q=person&location={solo['location_id']}").json()
    assert [ev["server_name"] for ev in r["events"]] == ["Solo"]
    assert {s["site_id"] for s in root.get(f"/api/fleet/search?org={oid}&q=person").json()["sites"]} == {a["id"], b["id"], solo["id"]}
    assert {s["site_id"] for s in viewer.get(f"/api/fleet/search?org={oid}&q=person").json()["sites"]} == {a["id"], b["id"]}
    assert viewer.get(f"/api/locations/{solo['location_id']}/search?q=x").status_code == 403

    lines = [json.loads(x) for x in root.post("/api/fleet/ask", json={"org": oid, "message": "hi", "location": campus["id"]}).text.splitlines() if x.strip()]
    assert lines[0]["type"] == "sites" and {s["site"] for s in lines[0]["sites"]} == {a["id"], b["id"]}
    assert {s["location_name"] for s in lines[0]["sites"]} == {"Campus"} and lines[-1]["type"] == "done"
    errs = [x for x in lines if x.get("type") == "error"]
    assert {x["server_id"] for x in errs} == {a["id"], b["id"]}   # every chunk carries the server/Site tags
    assert root.post("/api/fleet/ask", json={"org": oid, "message": "hi", "location": world["foreign"]["id"]}).status_code == 404


def test_backups_per_site(world):
    root, viewer, oid, campus, a, b = (world[k] for k in ("root", "viewer", "oid", "campus", "a", "b"))
    for t in (1.0, 2.0):
        db.insert(db.config_backups, {"site_id": a["id"], "org_id": oid, "created_at": t, "bytes": 10, "data": {}, "cameras": 2,
                                      "identities": 0, "site_version": "1.0"})
    out = {r["server_id"]: r for r in root.get(f"/api/locations/{campus['id']}/backups").json()}
    assert set(out) == {a["id"], b["id"]}
    assert out[a["id"]]["count"] == 2 and out[a["id"]]["latest"]["created_at"] == 2.0 and out[a["id"]]["server_name"] == "Gate"
    assert out[b["id"]] == {"server_id": b["id"], "server_name": "Yard", "online": False, "latest": None, "count": 0}
    assert viewer.get(f"/api/locations/{campus['id']}/backups").status_code == 403   # backups are admin business


def test_usage_per_site(world):
    root, viewer, oid, campus, a, b, solo = (world[k] for k in ("root", "viewer", "oid", "campus", "a", "b", "solo"))
    for s, n in ((a, 1), (b, 2), (b, 2), (solo, 5)):
        db.insert(db.vlm_usage, {"ts": time.time(), "site_id": s["id"], "org_id": oid, "model": "m", "task": "t", "prompt_tokens": n * 10,
                                 "completion_tokens": n, "latency_ms": 100, "status": 500 if s is solo else 200, "images": 0, "streamed": False})
    r = root.get(f"/api/orgs/{oid}/usage").json()
    rows = {x["site_id"]: x for x in r["sites"]}
    assert rows[a["id"]]["location_name"] == "Campus" and rows[solo["id"]]["location_id"] == solo["location_id"]
    locs = {x["name"]: x for x in r["locations"]}
    assert locs["Campus"] == {"location_id": campus["id"], "name": "Campus", "requests": 3, "prompt_tokens": 50, "completion_tokens": 5, "errors": 0}
    assert locs["Solo"]["requests"] == 1 and locs["Solo"]["errors"] == 1
    v = viewer.get(f"/api/orgs/{oid}/usage").json()
    assert {x["site_id"] for x in v["sites"]} == {a["id"], b["id"]} and [x["name"] for x in v["locations"]] == ["Campus"]


def test_digest_grouped_by_site(world):
    oid, campus, a, b, solo = (world[k] for k in ("oid", "campus", "a", "b", "solo"))
    db.run(sa.update(db.alerts).where(db.alerts.c.org_id == oid).values(closed_at=time.time()))   # test_alerts_per_site's
    data = asyncio.run(digest.collect(oid))
    assert {p["site_id"]: p["location_name"] for p in data["sites"]} == {a["id"]: "Campus", b["id"]: "Campus", solo["id"]: "Solo"}
    assert data["locations"] == [{"id": campus["id"], "name": "Campus", "servers": [a["id"], b["id"]]},
                                 {"id": solo["location_id"], "name": "Solo", "servers": [solo["id"]]}]
    text = digest._plain(data["sites"])
    assert text.splitlines() == ["Campus (2 servers)", "  • Gate — quiet. All calm at Gate", "  • Yard — quiet. All calm at Yard",
                                 "• Solo — quiet. All calm at Solo"]
    facts = digest._facts(data["sites"])
    assert facts.startswith("Site: Campus (2 servers)\nServer: Gate (online)") and "\n\nSite: Solo (online)" in facts


def test_push_title_names_the_site(world):
    oid, a, solo, root = (world[k] for k in ("oid", "a", "solo", "root"))
    row = lambda s: db.one(sa.select(db.sites).where(db.sites.c.id == s["id"]))  # noqa: E731
    assert push.title_name(row(a)) == "Campus · Gate" and push.title_name(row(solo)) == "Solo"
    assert root.post("/api/push/subscribe", json={"subscription": {"endpoint": "https://fcm.googleapis.com/fcm/send/agg"}, "kinds": ["offline"]}).status_code == 200
    sent = []
    push.set_sender(lambda sub, payload: sent.append(payload) or True)
    try:
        asyncio.run(push.notify_alert(oid, row(a), "offline", {}))
        asyncio.run(push.notify_alert(oid, row(solo), "offline", {}))
    finally:
        push.set_sender(None)
        root.post("/api/push/unsubscribe", json={"endpoint": "https://fcm.googleapis.com/fcm/send/agg"})
    assert [(p["title"], p["location_id"]) for p in sent] == [("Campus · Gate is offline", a["location_id"]), ("Solo is offline", solo["location_id"])]
