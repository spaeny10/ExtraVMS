"""A Site's Find tab: the server UI's Find (browse, filters, search by meaning, saved views) across every server of
one Site.

Browse fans `GET /api/events` out to each online server of the Site with the server UI's full filter set and merges
the answers newest first (or most important first with sort=priority); search fans `GET /api/search` out and merges
by relevance. Paging is per server: the response's `next` is an opaque cursor (JSON, {server id: {before_id} or
{offset}}) that the page hands back for the following page, so infinite scroll keeps going across servers without
skipping or repeating an event. Servers that are offline, failed or have nothing more are left out of `next`.

Saved Find views live per Site in the kv table (`find_views:<location id>`), in the same shape as a server's own
/api/find/views (frontend/src/findViews.ts SavedFindView).
"""
from __future__ import annotations

import asyncio
import json
from typing import Callable, Iterable

import sqlalchemy as sa

from . import db
from .agents import registry
from .fleet import _get_json, server_tags

PRIORITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}
TAG_KEYS = ("site_id", "site_name", "server_id", "server_name", "location_id", "location_name")
MAX_VIEWS = 50
MAX_VIEWS_BYTES = 64_000

Cursor = dict[str, dict]   # server id -> {"before_id": n} | {"offset": n} | {} (from the start)


# ---------------------------------------------------------------- cursors
def parse_cursor(raw: str | None, allowed: Iterable[str]) -> Cursor | None:
    """The `cursor` parameter: None = first page. Servers outside `allowed` (another Site's) are dropped; a malformed
    cursor raises ValueError."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ValueError("bad cursor") from e
    if not isinstance(data, dict):
        raise ValueError("bad cursor")
    ok = set(allowed)
    out: Cursor = {}
    for sid, c in data.items():
        if not isinstance(c, dict):
            raise ValueError("bad cursor")
        clean: dict = {}
        for k in ("before_id", "offset"):
            if k in c:
                v = c[k]
                if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                    raise ValueError("bad cursor")
                clean[k] = v
        if sid in ok:
            out[sid] = clean
    return out


def dump_cursor(c: Cursor) -> str | None:
    return json.dumps(c, separators=(",", ":"), sort_keys=True) if c else None


# ---------------------------------------------------------------- merge
def sort_key(sort: str) -> Callable[[dict], tuple]:
    """Smaller sorts first. newest: start time; priority: rank then start time; score: relevance then start time."""
    if sort == "priority":
        return lambda e: (-PRIORITY_RANK.get(e.get("priority") or "none", 0), -(e.get("start_ts") or 0))
    if sort == "score":
        return lambda e: (-(e.get("score") or 0), -(e.get("start_ts") or 0))
    return lambda e: (-(e.get("start_ts") or 0),)


def merge_pages(pages: dict[str, list[dict]], order: list[str], limit: int, per_server: int, sort: str,
                cursors: Cursor, paging: str) -> tuple[list[tuple[str, dict]], Cursor]:
    """K-way merge of each server's page (already in that server's own order) into one page of at most `limit`,
    and the cursor for the next page.

    Every server's contribution is a prefix of its own page, so its next cursor is exact: before_id = the last event
    taken (paging="before_id", newest-first by id) or offset + taken (paging="offset"). A server stays in the cursor
    while it has events left: some of this page were not taken, or it returned a full page. Correct across servers
    because an event a server didn't return sorts after everything it did return, and a server whose whole page
    was taken filled the page by itself.
    """
    key = sort_key(sort)
    rank = {sid: i for i, sid in enumerate(order)}
    heads = {sid: 0 for sid in order if sid in pages}
    out: list[tuple[str, dict]] = []
    while len(out) < limit:
        best = None
        for sid, i in heads.items():
            evs = pages[sid]
            if i >= len(evs):
                continue
            k = (key(evs[i]), rank[sid])
            if best is None or k < best[0]:
                best = (k, sid)
        if best is None:
            break
        sid = best[1]
        out.append((sid, pages[sid][heads[sid]]))
        heads[sid] += 1
    nxt: Cursor = {}
    for sid, taken in heads.items():
        got = len(pages[sid])
        if taken >= got and got < per_server:
            continue   # nothing left on this server
        prev = cursors.get(sid, {})
        if paging == "offset":
            nxt[sid] = {"offset": prev.get("offset", 0) + taken}
        elif taken:
            nxt[sid] = {"before_id": int(pages[sid][taken - 1]["id"])}
        else:
            nxt[sid] = dict(prev)
    return out, nxt


# ---------------------------------------------------------------- fan-out
async def site_find(u: dict, org_id: str, servers: list[dict], path: str, params: dict, cursor: Cursor | None,
                    limit: int, sort: str, camera: tuple[str | None, str] | None) -> dict:
    """One page of a Site's events (path=/api/events) or search hits (path=/api/search) across `servers` (the Site's
    servers the user may see, in Site order). `camera` = (server or None, camera id) narrows to one camera."""
    headers = {"x-hub-user": u["email"], "x-hub-role": "viewer"}
    tags = server_tags(org_id, servers)
    by_id = {s["id"]: s for s in servers}
    order = [s["id"] for s in servers]
    if camera and camera[0]:
        order = [sid for sid in order if sid == camera[0]]
    if cursor is not None:
        order = [sid for sid in order if sid in cursor]   # servers missing from a cursor have nothing more
    cursors = cursor or {}
    paging = "before_id" if path == "/api/events" and sort == "newest" else "offset"
    offline, errors = [], []
    pages: dict[str, list[dict]] = {}

    async def one(sid: str):
        conn = registry.get(sid)
        if conn is None:
            offline.append(by_id[sid]["name"])
            return
        p = {**params, "limit": limit, **cursors.get(sid, {})}
        if camera:
            p["camera"] = camera[1]
        try:
            status, body = await _get_json(conn, path, p, headers)
        except Exception as e:  # tunnel timeout / abort: one slow server must not spoil the page
            errors.append({"server_id": sid, "server_name": by_id[sid]["name"], "error": str(e)[:200] or type(e).__name__})
            return
        if status != 200 or not isinstance(body, list):
            errors.append({"server_id": sid, "server_name": by_id[sid]["name"], "error": f"HTTP {status}"})
            return
        pages[sid] = [e for e in body if isinstance(e, dict) and isinstance(e.get("id"), int)]

    await asyncio.gather(*(one(sid) for sid in order))
    merged, nxt = merge_pages(pages, order, limit, limit, "score" if path == "/api/search" else sort, cursors, paging)
    events = [{**e, **{k: tags[sid][k] for k in TAG_KEYS}} for sid, e in merged]
    return {"events": events, "next": dump_cursor(nxt), "offline": offline, "errors": errors}


def camera_param(camera: str | None, allowed: set[str]) -> tuple[str | None, str] | None:
    """?camera=<server>:<camera> (or a bare camera id, looked up on every server). A server outside the Site
    raises LookupError (the page then shows nothing rather than another Site's camera)."""
    if not camera:
        return None
    if ":" in camera:
        sid, cam = camera.split(":", 1)
        if sid not in allowed:
            raise LookupError(sid)
        return sid, cam
    return None, camera


# ---------------------------------------------------------------- saved views
def _views_key(location_id: str) -> str:
    return f"find_views:{location_id}"


def get_views(location_id: str) -> list[dict]:
    row = db.one(sa.select(db.kv.c.value).where(db.kv.c.key == _views_key(location_id)))
    v = (row or {}).get("value") or {}
    return list(v.get("views") or []) if isinstance(v, dict) else []


def clean_views(views: list) -> list[dict]:
    """As a server's PUT /api/find/views keeps them: a name each (60 chars), builtin false; plus id/filters checked.
    ValueError with the reason otherwise."""
    if len(views) > MAX_VIEWS:
        raise ValueError(f"at most {MAX_VIEWS} saved views")
    out = []
    for v in views:
        if not isinstance(v, dict):
            raise ValueError("every view must be an object")
        name = str(v.get("name") or "").strip()
        if not name:
            raise ValueError("every view needs a name")
        vid = str(v.get("id") or "").strip()
        if not vid or len(vid) > 40:
            raise ValueError("every view needs an id (at most 40 characters)")
        filters = v.get("filters") or {}
        if not isinstance(filters, dict):
            raise ValueError("filters must be an object")
        out.append({"id": vid, "name": name[:60], "icon": str(v.get("icon") or "★")[:8], "filters": filters,
                    "mode": "grouped" if v.get("mode") == "grouped" else "events", "builtin": False})
    if len(json.dumps(out)) > MAX_VIEWS_BYTES:
        raise ValueError("saved views are too large")
    return out


def set_views(location_id: str, views: list[dict]) -> None:
    key = _views_key(location_id)
    with db.engine().begin() as c:
        c.execute(sa.delete(db.kv).where(db.kv.c.key == key))
        c.execute(db.kv.insert().values(key=key, value={"views": views}))


def drop_views(conn, location_id: str) -> None:
    conn.execute(sa.delete(db.kv).where(db.kv.c.key == _views_key(location_id)))
