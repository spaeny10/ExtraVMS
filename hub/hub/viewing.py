"""Live viewing in the audit log: one row per viewing session of a camera, not one per WHEP offer.

A session is (user, server, camera). Every WHEP offer through the hub's proxy (proxy.py) joins the user's open row
for that camera, one whose detail.last is under VIEW_GAP_S old, or starts a new one; the hub UI's teardown beacon
(POST /api/telemetry/live, "end" of an attempt that showed a picture) adds that attempt's viewing time. A Direct-on-LAN
view never passes the proxy, so its rows come from the beacons alone (route "direct", source "client": what the
browser said, not something the hub saw). The Audit page shows the action as written: "Viewed <camera>" and, once
known, how long.

detail: {kind: "live_view", camera, camera_name, paths, quality (the latest), route, sessions, first, last,
duration_s, source?}. The open rows are indexed in memory ((user, server, camera) -> id and detail); after a restart
the user's latest rows on that server are read back once.
"""
from __future__ import annotations

import re
import time

import sqlalchemy as sa

from . import db

VIEW_GAP_S = 600.0                 # a new offer or picture within this long of the last one is the same session
MAX_DURATION_S = 30 * 86400        # as telemetry.MAX_DURATION_S: a wall display may stay on for weeks
INDEX_KEEP_S = 86400.0             # index entries older than this are dropped (the database still has the row)
INDEX_MAX = 5000
WHEP_OFFER = re.compile(r"^/api/whep/([a-z0-9_]{1,40})$")
KIND = "live_view"

_open: dict[tuple[str, str, str], dict] = {}   # (user id, server id, camera id) -> {"id", "detail", "name"}


def camera_of(path: str) -> str:
    """The camera a MediaMTX live path shows: <id>, <id>_sub and <id>_hd are all <id> (frontend cameraOfPath)."""
    return re.sub(r"_(sub|hd)$", "", path)


def quality_of(path: str) -> str:
    return "sd" if path.endswith("_sub") else "hd"


def _name(server_id: str, camera_id: str) -> str:
    r = db.one(sa.select(db.cameras.c.name).where(db.cameras.c.server_id == server_id, db.cameras.c.camera_id == camera_id))
    return (r or {}).get("name") or camera_id


def _action(name: str, d: dict) -> str:
    secs = d.get("duration_s") or 0
    if secs < 60:
        return f"Viewed {name}" + (f" for {round(secs)} s" if secs else "")
    mins = round(secs / 60)
    return f"Viewed {name} for {mins} min" if mins < 120 else f"Viewed {name} for {secs / 3600:.1f} h"


def _find(key: tuple[str, str, str], since: float) -> dict | None:
    """The open session row for `key` whose last activity is at or after `since`: the index, else the user's latest
    rows on that server (after a restart)."""
    hit = _open.get(key)
    if hit and hit["detail"].get("last", 0) >= since:
        return hit
    if hit:
        return None   # the indexed row is the newest one; it is closed
    uid, server_id, camera_id = key
    t = db.audit_log
    for r in db.rows(sa.select(t.c.id, t.c.detail, t.c.action).where(t.c.user_id == uid, t.c.site_id == server_id, t.c.action.like("Viewed %"))
                     .order_by(t.c.id.desc()).limit(50)):
        d = r["detail"] if isinstance(r["detail"], dict) else {}
        if d.get("kind") == KIND and d.get("camera") == camera_id:
            if d.get("last", 0) < since:
                return None
            hit = {"id": r["id"], "detail": d, "name": d.get("camera_name") or camera_id}
            _open[key] = hit
            return hit
    return None


def _prune(now: float) -> None:
    if len(_open) <= INDEX_MAX:
        return
    for k in [k for k, v in _open.items() if v["detail"].get("last", 0) < now - INDEX_KEEP_S]:
        del _open[k]
    while len(_open) > INDEX_MAX:
        del _open[next(iter(_open))]


def _write(hit: dict, status: int | None = None) -> None:
    vals: dict = {"detail": hit["detail"], "action": _action(hit["name"], hit["detail"])}
    if status is not None:
        vals["status"] = status
    db.run(sa.update(db.audit_log).where(db.audit_log.c.id == hit["id"]).values(**vals))


def _start(key: tuple[str, str, str], u: dict, site: dict, detail: dict, method: str | None, path: str | None,
           status: int | None, ip: str | None, now: float) -> dict:
    name = _name(site["id"], key[2])
    detail = {"kind": KIND, "camera": key[2], "camera_name": name, **detail}
    with db.engine().begin() as c:
        rid = c.execute(db.audit_log.insert().values(
            ts=now, user_id=u["id"], user_email=u["email"], org_id=site["org_id"], site_id=site["id"], action=_action(name, detail),
            method=method, path=path, status=status, ip=ip, detail=detail)).inserted_primary_key[0]
    hit = {"id": rid, "detail": detail, "name": name}
    _open[key] = hit
    _prune(now)
    return hit


def offer(u: dict, site: dict, mtx_path: str, request_path: str, status: int, ip: str | None, ms: int, now: float | None = None) -> None:
    """A WHEP offer through the proxy (retries included): joins the open session for its camera or starts one."""
    now = time.time() if now is None else now
    camera = camera_of(mtx_path)
    key = (u["id"], site["id"], camera)
    hit = _find(key, now - VIEW_GAP_S)
    if hit is None:
        _start(key, u, site, {"paths": [mtx_path], "quality": quality_of(mtx_path), "route": "tunnel", "sessions": 1,
                              "first": now, "last": now, "duration_s": 0, "ms": ms}, "POST", request_path, status, ip, now)
        return
    d = hit["detail"]
    d["paths"] = sorted(set(d.get("paths") or []) | {mtx_path})[:8]
    d.update(quality=quality_of(mtx_path), sessions=int(d.get("sessions") or 0) + 1, last=now)
    if d.get("route") != "tunnel":
        d["route"] = "tunnel"   # a client-reported session that went through the proxy as well: the hub saw it
    _write(hit, status)


def beacon(u: dict, site: dict, body, ip: str | None, now: float | None = None) -> None:
    """A telemetry report (telemetry.LiveIn) of an attempt that showed a picture: an "end" adds its viewing time to
    the session; a Direct-on-LAN attempt's reports also open and count the session (the proxy never saw it)."""
    if body.ttff_ms is None:
        return   # never showed a picture: nothing was viewed
    now = time.time() if now is None else now
    key = (u["id"], site["id"], body.camera)
    direct = body.route == "direct"
    duration = min(body.duration_s or 0.0, MAX_DURATION_S) if body.event == "end" else 0.0
    # the attempt began `duration` ago: its session is the one active since then (an hour-long view had no offer since)
    hit = _find(key, now - duration - VIEW_GAP_S)
    if hit is None:
        if not direct:
            return   # through the hub, but no session (an offer more than VIEW_GAP_S before the attempt began)
        _start(key, u, site, {"paths": [body.path], "quality": body.quality, "route": "direct", "source": "client",
                              "sessions": 1, "first": now - duration, "last": now, "duration_s": round(duration)},
               None, None, None, ip, now)
        return
    d = hit["detail"]
    d["last"] = max(float(d.get("last") or 0), now)
    if body.event == "end":
        d["duration_s"] = round(min(float(d.get("duration_s") or 0) + duration, MAX_DURATION_S))
    elif direct:
        d["sessions"] = int(d.get("sessions") or 0) + 1   # a direct attempt's first picture: the client's count of offers
        d["paths"] = sorted(set(d.get("paths") or []) | {body.path})[:8]
        d["quality"] = body.quality
    else:
        return   # a first picture through the hub: its offer already counted
    _write(hit)


def reset() -> None:
    """Tests: forget the in-memory index."""
    _open.clear()
