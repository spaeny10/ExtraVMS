"""A Site's Find tab (find.py): browse and search fan out to the Site's servers only, merge newest first / by priority
/ by relevance, page with a per-server cursor without skipping or repeating, and the Site's saved views are readable
by anyone who sees the Site and writable by operators and up (audited)."""
import json
from urllib.parse import parse_qs

import pytest
import sqlalchemy as sa

from hub import db, find
from hub.agents import registry
from test_access import _login, server


class FindConn:
    """A server's /api/events and /api/search over a fixed list (id DESC like the server), honoring the paging
    parameters, camera and priority sort; it remembers the queries it was sent."""

    def __init__(self, s: dict, events: list[dict]):
        self.site_id, self.site, self.last_seen = s["id"], s, 1e12
        self.events = sorted(events, key=lambda e: -e["id"])
        self.queries: list[dict] = []

    async def call(self, method, path, query, headers, body, timeout):
        q = {k: v[0] for k, v in parse_qs(query).items()}
        self.queries.append({"path": path, **q})
        evs = [e for e in self.events if "camera" not in q or e["camera_id"] == q["camera"]]
        limit = int(q.get("limit", 50))
        if path == "/api/events":
            if q.get("sort") == "priority":
                evs = sorted(evs, key=lambda e: (-find.PRIORITY_RANK[e.get("priority") or "none"], -e["id"]))
            if "before_id" in q:
                evs = [e for e in evs if e["id"] < int(q["before_id"])]
        elif path == "/api/search":
            evs = sorted(evs, key=lambda e: (-e["score"], -e["start_ts"]))
        else:
            return 404, b""
        off = int(q.get("offset", 0))
        return 200, json.dumps(evs[off:off + limit]).encode()


def ev(i: int, ts: float, cam="cam1", priority="none", score=0.0):
    return {"id": i, "camera_id": cam, "camera_class": "person", "start_ts": ts, "priority": priority, "score": score, "status": "verified"}


@pytest.fixture(scope="module")
def world(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": "Find Co", "slug": "find-co"}).json()["id"]
    yard = root.post(f"/api/orgs/{oid}/locations", json={"name": "Find Yard"}).json()
    a, b = server(oid, "Alpha", yard["id"]), server(oid, "Bravo", yard["id"])
    other = server(oid, "Elsewhere")   # its own one-server Site
    # ids overlap across servers on purpose (they are per server); times interleave
    ca = FindConn(a, [ev(i, 1000 + 10 * i, "cam1" if i % 2 else "cam2", priority="high" if i == 2 else "none", score=i / 10) for i in range(1, 8)])
    cb = FindConn(b, [ev(i, 1005 + 10 * i, priority="medium" if i == 6 else "none", score=i / 10 + 0.05) for i in range(1, 8)])
    co = FindConn(other, [ev(i, 5000 + i) for i in range(1, 4)])
    for s, c in ((a, ca), (b, cb), (other, co)):
        registry.by_site[s["id"]] = c
    root.post(f"/api/orgs/{oid}/members", json={"email": "fv@find.example", "role": "viewer", "password": "find-pass-12345",
                                              "all_sites": False, "location_ids": [yard["id"]]})
    root.post(f"/api/orgs/{oid}/members", json={"email": "fo@find.example", "role": "operator", "password": "find-pass-12345",
                                              "all_sites": False, "location_ids": [yard["id"]]})
    viewer = _login(client, "fv@find.example", "find-pass-12345")
    operator = _login(client, "fo@find.example", "find-pass-12345")
    try:
        yield {"root": root, "viewer": viewer, "operator": operator, "oid": oid, "yard": yard, "a": a, "b": b, "other": other,
               "ca": ca, "cb": cb, "co": co}
    finally:
        for s in (a, b, other):
            registry.by_site.pop(s["id"], None)


def _all_pages(c, url: str, limit: int) -> list[list[dict]]:
    pages, cursor = [], None
    path, _, query = url.partition("?")
    base = {k: v[0] for k, v in parse_qs(query).items()}
    for _ in range(20):
        r = c.get(path, params={**base, "limit": limit, **({"cursor": cursor} if cursor else {})})
        assert r.status_code == 200, r.text
        body = r.json()
        pages.append(body["events"])
        cursor = body["next"]
        if not cursor:
            return pages
    raise AssertionError("no end")


def test_browse_scoped_merged_and_paged(world):
    root, yard, a, b, other = (world[k] for k in ("root", "yard", "a", "b", "other"))
    url = f"/api/locations/{yard['id']}/find/events"
    pages = _all_pages(root, url, 4)
    flat = [e for p in pages for e in p]
    # never another Site's events; each server's events once (ids collide across servers, the server tag tells them apart)
    assert {e["server_id"] for e in flat} == {a["id"], b["id"]}
    keys = [(e["server_id"], e["id"]) for e in flat]
    assert len(keys) == len(set(keys)) == 14
    assert [e["start_ts"] for e in flat] == sorted((e["start_ts"] for e in flat), reverse=True)
    assert all(len(p) <= 4 for p in pages) and len(pages) == 4
    assert flat[0]["location_id"] == yard["id"] and flat[0]["server_name"] == "Bravo"
    # newest-first pages by before_id per server
    assert any("before_id" in q for q in world["ca"].queries)
    # the other Site's server was never asked
    assert not world["co"].queries


def test_browse_priority_and_filters_forwarded(world):
    root, yard, a = world["root"], world["yard"], world["a"]
    url = f"/api/locations/{yard['id']}/find/events"
    flat = [e for p in _all_pages(root, url + "?sort=priority&status=verified&flags=rule,unusual&attention=true&min_yolo=0.4&place=Dock", 5) for e in p]
    assert [e["priority"] for e in flat[:2]] == ["high", "medium"] and len(flat) == 14
    q = world["ca"].queries[-1]
    assert q["sort"] == "priority" and q["status"] == "verified" and q["flags"] == "rule,unusual" and q["attention"] == "true"
    assert q["min_yolo"] == "0.4" and q["place"] == "Dock" and "offset" in q
    # one camera of one server: only that server is asked
    world["cb"].queries.clear()
    r = root.get(url, params={"camera": f"{a['id']}:cam2"}).json()
    assert {e["server_id"] for e in r["events"]} == {a["id"]} and {e["camera_id"] for e in r["events"]} == {"cam2"}
    assert not world["cb"].queries
    # a camera of another Site's server: nothing, and that server isn't asked
    assert root.get(url, params={"camera": f"{world['other']['id']}:cam1"}).json()["events"] == []
    assert not world["co"].queries
    assert root.get(url, params={"flags": "bogus"}).status_code == 400
    assert root.get(url, params={"cursor": "not json"}).status_code == 400


def test_cursor_cannot_reach_another_site(world):
    root, yard, other = world["root"], world["yard"], world["other"]
    cur = json.dumps({other["id"]: {"offset": 0}})
    r = root.get(f"/api/locations/{yard['id']}/find/events", params={"cursor": cur}).json()
    assert r["events"] == [] and r["next"] is None
    assert not world["co"].queries


def test_search_merged_by_relevance(world):
    root, yard = world["root"], world["yard"]
    pages = _all_pages(root, f"/api/locations/{yard['id']}/find/search?q=person&label=person", 3)
    flat = [e for p in pages for e in p]
    assert len(flat) == 14
    assert [e["score"] for e in flat] == sorted((e["score"] for e in flat), reverse=True)
    assert world["cb"].queries[-1]["q"] == "person" and world["cb"].queries[-1]["label"] == "person"


def test_offline_server_is_reported(world):
    root, yard, b = world["root"], world["yard"], world["b"]
    conn = registry.by_site.pop(b["id"])
    try:
        r = root.get(f"/api/locations/{yard['id']}/find/events").json()
        assert r["offline"] == ["Bravo"] and {e["server_id"] for e in r["events"]} == {world["a"]["id"]}
    finally:
        registry.by_site[b["id"]] = conn


def test_access(world):
    viewer, yard, other = world["viewer"], world["yard"], world["other"]
    assert viewer.get(f"/api/locations/{yard['id']}/find/events").status_code == 200
    assert viewer.get(f"/api/locations/{other['location_id']}/find/events").status_code == 403
    assert viewer.get(f"/api/locations/{other['location_id']}/find/search?q=x").status_code == 403
    assert viewer.get(f"/api/locations/{other['location_id']}/find-views").status_code == 403


def test_saved_views(world):
    root, viewer, operator, yard = (world[k] for k in ("root", "viewer", "operator", "yard"))
    url = f"/api/locations/{yard['id']}/find-views"
    assert viewer.get(url).json() == {"views": [], "can_edit": False}
    assert operator.get(url).json()["can_edit"] is True
    view = {"id": "v1", "name": "  Night gate  ", "icon": "★", "filters": {"camera": "x/cam1", "flags": ["rule"], "hours": 12}, "mode": "events"}
    assert viewer.put(url, json={"views": [view]}).status_code == 403
    r = operator.put(url, json={"views": [view]})
    assert r.status_code == 200, r.text
    saved = r.json()["views"]
    assert saved == [{"id": "v1", "name": "Night gate", "icon": "★", "filters": view["filters"], "mode": "events", "builtin": False}]
    assert viewer.get(url).json()["views"] == saved
    assert operator.put(url, json={"views": [{**view, "name": " "}]}).status_code == 400
    assert operator.put(url, json={"views": [{**view, "id": f"v{i}"} for i in range(51)]}).status_code in (400, 422)
    audit = db.rows(sa.select(db.audit_log).where(db.audit_log.c.action.like("site find views saved%")))
    assert audit and audit[-1]["detail"]["location_id"] == yard["id"] and audit[-1]["detail"]["added"] == ["v1"]
    # per Site: another Site's views are separate
    assert root.get(f"/api/locations/{world['other']['location_id']}/find-views").json()["views"] == []


def test_merge_pages_unit():
    pages = {"a": [{"id": 9, "start_ts": 90}, {"id": 8, "start_ts": 80}], "b": [{"id": 5, "start_ts": 85}, {"id": 4, "start_ts": 10}]}
    out, nxt = find.merge_pages(pages, ["a", "b"], 3, 2, "newest", {}, "before_id")
    assert [(s, e["id"]) for s, e in out] == [("a", 9), ("b", 5), ("a", 8)]
    assert nxt == {"a": {"before_id": 8}, "b": {"before_id": 5}}
    # a short page that was fully taken means that server is done
    out, nxt = find.merge_pages({"a": [{"id": 1, "start_ts": 1}]}, ["a"], 5, 5, "newest", {"a": {"before_id": 2}}, "before_id")
    assert nxt == {}
    # offset paging adds what was taken; a server none of whose page was taken keeps its place
    out, nxt = find.merge_pages({"a": [{"id": 1, "score": 0.9}], "b": [{"id": 2, "score": 0.1}]}, ["a", "b"], 1, 1, "score",
                                {"a": {"offset": 10}, "b": {"offset": 3}}, "offset")
    assert nxt == {"a": {"offset": 11}, "b": {"offset": 3}}
    assert find.parse_cursor(json.dumps({"a": {"before_id": 3}, "zz": {"offset": 1}}), {"a"}) == {"a": {"before_id": 3}}
    with pytest.raises(ValueError):
        find.parse_cursor(json.dumps({"a": {"offset": -1}}), {"a"})
