"""The latest picture of each camera: what a Site's lists and live tiles show before (or without) the tunnel.

Each server's agent sends {"t": "still", "cam", "ts", "jpeg" (base64)} for every enabled camera every 5 minutes
(NVR_HUB_STILL_INTERVAL) and when an event opens, only to a hub whose welcome says "stills". Only the newest per camera
is kept (camera_stills), so a server that goes offline leaves its cameras' last pictures behind ("Last picture hh:mm").
A still is refused unless it is a JPEG of at most MAX_BYTES for a camera the registry lists as enabled on that server;
a camera's row goes when it leaves its server's list or is disabled (cameras.sync), and all of a server's with it.
Served by GET /api/servers/{sid}/cameras/{cid}/still to whoever may view that camera (auth.camera_access).
"""
from __future__ import annotations

import base64
import binascii
import math
import re
import time

import sqlalchemy as sa

from . import db

MAX_BYTES = 256 * 1024          # decoded JPEG; the agent keeps its own under 150 KB
MAX_AGE_S = 30 * 86400          # a camera down for longer loses its last picture (purge_old, from the sweeper)
MAX_B64 = (MAX_BYTES + 2) // 3 * 4
CAM_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
JPEG_MAGIC = b"\xff\xd8\xff"


def decode(frame: dict) -> tuple[str, float, bytes] | None:
    """(camera id, ts, jpeg) from an agent's still frame, or None when it isn't a plausible one."""
    cam, ts, b64 = frame.get("cam"), frame.get("ts"), frame.get("jpeg")
    if not isinstance(cam, str) or not CAM_ID.match(cam) or not isinstance(b64, str) or len(b64) > MAX_B64:
        return None
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts) or ts <= 0:
        return None
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(data) > MAX_BYTES or not data.startswith(JPEG_MAGIC):
        return None
    return cam, float(ts), data


def accept(server_id: str, frame: dict) -> bool:
    """Store the frame's still as its camera's latest (an older one than what is kept is ignored); False = refused."""
    got = decode(frame)
    if got is None:
        return False
    cam, ts, data = got
    t, c = db.camera_stills, db.cameras
    with db.engine().begin() as conn:
        known = conn.execute(sa.select(c.c.camera_id).where(c.c.server_id == server_id, c.c.camera_id == cam, c.c.enabled == True,  # noqa: E712
                                                            c.c.missing_since.is_(None))).first()
        if known is None:
            return False   # not a camera of this server (or not one the hub shows): never stored
        have = conn.execute(sa.select(t.c.ts).where(t.c.server_id == server_id, t.c.camera_id == cam)).first()
        vals = {"ts": ts, "jpeg": data, "received_at": time.time()}
        if have is None:
            conn.execute(t.insert().values(server_id=server_id, camera_id=cam, **vals))
        elif ts > have.ts:
            conn.execute(sa.update(t).where(t.c.server_id == server_id, t.c.camera_id == cam).values(**vals))
    return True


def get(server_id: str, camera_id: str) -> dict | None:
    return db.one(sa.select(db.camera_stills).where(db.camera_stills.c.server_id == server_id, db.camera_stills.c.camera_id == camera_id))


def times(server_ids: list[str]) -> dict[str, dict[str, float]]:
    """server -> {camera: ts} of the stills kept (never the pictures themselves)."""
    out: dict[str, dict[str, float]] = {}
    if not server_ids:
        return out
    t = db.camera_stills
    for r in db.rows(sa.select(t.c.server_id, t.c.camera_id, t.c.ts).where(t.c.server_id.in_(server_ids))):
        out.setdefault(r["server_id"], {})[r["camera_id"]] = r["ts"]
    return out


def etag(ts: float) -> str:
    return f'"{ts:.3f}"'


def purge_old(now: float | None = None) -> None:
    """Forget stills taken more than MAX_AGE_S ago: the hub keeps no picture of a camera that has been down that long."""
    cutoff = (now if now is not None else time.time()) - MAX_AGE_S
    db.run(sa.delete(db.camera_stills).where(db.camera_stills.c.ts < cutoff))


def drop(conn, server_id: str, camera_ids: list[str] | None = None) -> None:
    """Forget the stills of these cameras of the server (None: all of them), on the caller's connection."""
    stmt = sa.delete(db.camera_stills).where(db.camera_stills.c.server_id == server_id)
    if camera_ids is not None:
        if not camera_ids:
            return
        stmt = stmt.where(db.camera_stills.c.camera_id.in_(camera_ids))
    conn.execute(stmt)
