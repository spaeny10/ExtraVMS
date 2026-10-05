"""The hub's camera registry: one row per (server, camera), kept in step with what each server reports.

Servers say which cameras they have in `hello` ({id, name}) and in every heartbeat summary (status fields:
stream_ready, problems, bitrate, ptz...). sync() upserts those; a camera that drops out of a full list gets
`missing_since` but is never deleted while its server exists (dashboards and groups keep pointing at it, and it
comes back cleanly if re-added). Newer agents also send `disabled: [{id, name}]`; those rows get enabled=false and
alerts ignore them. Everything is read with .get so old agents work unchanged.
"""
from __future__ import annotations

import time

import sqlalchemy as sa

from . import db

STATUS = ("stream_ready", "problems", "bitrate_mbps", "ptz", "metadata", "onvif_events")


def _ids(items) -> list[tuple[str, dict]]:
    """[(camera_id, item)] from a list of dicts ({id, name, ...}) or bare ids; junk is skipped."""
    out = []
    for it in items or []:
        d = it if isinstance(it, dict) else {"id": it}
        cid = str(d.get("id") or "")[:64]
        if cid:
            out.append((cid, d))
    return out


def sync(server: dict, cams: list | None, full: bool = True, disabled: list | None = None, source: str = "heartbeat") -> None:
    """Upsert `cams` (enabled) and `disabled`; with `full`, rows in neither list are marked missing.
    `disabled=None` means the agent doesn't report them (old agent): enabled flags are left alone except for
    cameras listed in `cams`, which are enabled by definition."""
    now = time.time()
    t = db.cameras
    listed = _ids(cams)
    off = _ids(disabled)
    with db.engine().begin() as c:
        have = {r["camera_id"]: dict(r) for r in c.execute(sa.select(t).where(t.c.server_id == server["id"])).mappings()}
        base = {"org_id": server["org_id"], "location_id": server.get("location_id"), "last_seen_at": now, "missing_since": None, "source": source}

        def put(cid: str, d: dict, enabled: bool) -> None:
            vals = dict(base, enabled=enabled)
            old = have.get(cid)
            name = d.get("name")
            if name or not old:
                vals["name"] = str(name or cid)[:120]
            for k in STATUS:          # only what this message carries: a hello must not wipe heartbeat status
                if k in d:
                    vals[k] = d[k]
            if old is None:
                c.execute(t.insert().values(server_id=server["id"], camera_id=cid, first_seen_at=now, **vals))
                have[cid] = vals
            else:
                c.execute(sa.update(t).where(t.c.server_id == server["id"], t.c.camera_id == cid).values(**vals))

        for cid, d in listed:
            put(cid, d, True)
        for cid, d in off:
            put(cid, d, False)
        if full:
            seen = {cid for cid, _ in listed} | {cid for cid, _ in off}
            gone = [cid for cid, r in have.items() if cid not in seen and not r.get("missing_since")]
            if gone:
                c.execute(sa.update(t).where(t.c.server_id == server["id"], t.c.camera_id.in_(gone)).values(missing_since=now))


def _q():
    return sa.select(db.cameras).order_by(db.cameras.c.server_id, db.cameras.c.name, db.cameras.c.camera_id)


def for_server(server_id: str) -> list[dict]:
    return db.rows(_q().where(db.cameras.c.server_id == server_id))


def for_location(location_id: str) -> list[dict]:
    return db.rows(_q().where(db.cameras.c.location_id == location_id))


def for_org(org_id: str) -> list[dict]:
    return db.rows(_q().where(db.cameras.c.org_id == org_id))


def disabled_ids(server_id: str) -> set[str]:
    return {r["camera_id"] for r in db.rows(sa.select(db.cameras.c.camera_id).where(db.cameras.c.server_id == server_id,
                                                                                   db.cameras.c.enabled == False))}  # noqa: E712


def relocate(server_id: str, location_id: str | None, conn=None) -> None:
    """The server moved to another Site: its cameras' denormalised location follows."""
    stmt = sa.update(db.cameras).where(db.cameras.c.server_id == server_id).values(location_id=location_id)
    if conn is not None:
        conn.execute(stmt)
    else:
        db.run(stmt)


def delete_server(server_id: str, conn=None) -> None:
    stmt = sa.delete(db.cameras).where(db.cameras.c.server_id == server_id)
    if conn is not None:
        conn.execute(stmt)
    else:
        db.run(stmt)


def counts(server_ids: list[str], online_ids: set[str]) -> dict[str, tuple[int, int]]:
    """server -> (cameras_total, cameras_online). Total = enabled and not missing; online = also stream-ready on an
    online server (an offline server's last reported state says nothing about now)."""
    out = {sid: (0, 0) for sid in server_ids}
    if not server_ids:
        return out
    t = db.cameras
    for r in db.rows(sa.select(t.c.server_id, t.c.stream_ready).where(t.c.server_id.in_(server_ids), t.c.enabled == True,  # noqa: E712
                                                                      t.c.missing_since.is_(None))):
        total, online = out[r["server_id"]]
        out[r["server_id"]] = (total + 1, online + (1 if r["stream_ready"] and r["server_id"] in online_ids else 0))
    return out
