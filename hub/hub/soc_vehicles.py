"""Vehicles around an SOC incident (docs/soc-plan.md §1): the workstation's "Vehicles around this" card.

When an operator opens an incident, the hub asks every server of its Site (GET /api/traffic/around through the
tunnel) for the vehicles seen from WINDOW_BEFORE_S before the incident's first event to WINDOW_AFTER_S after its last:
the traffic cameras' passes (plate, Otso's description, direction, the plate's counted notes) and the plates Qwen read
from event footage. Asked when the card is shown, not when the incident opens, so a vehicle leaving in the 5 minutes
after still appears; the last answer is kept on the incident (incidents.vehicles) and shown, marked stale, when no
server answers. Operators only (decided with Shawn 2026-10-29): never in customer-facing reports or pushes.

A server that doesn't answer within TIMEOUT_S is left out with its reason, like Site Ask; a server too old for the
route (404) says so.
"""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlencode

import sqlalchemy as sa

from . import db
from .agents import registry

WINDOW_BEFORE_S = 600.0
WINDOW_AFTER_S = 300.0
TIMEOUT_S = 10.0
PATH = "/api/traffic/around"


def window(incident: dict) -> tuple[float, float]:
    """(since, until): 10 minutes before the incident's first event to 5 minutes after its last."""
    ie = db.incident_events
    r = db.one(sa.select(sa.func.min(ie.c.ts).label("a"), sa.func.max(ie.c.ts).label("b")).where(ie.c.incident_id == incident["id"]))
    first = (r or {}).get("a") or incident["opened_at"]
    last = (r or {}).get("b") or incident.get("last_event_at") or first
    return first - WINDOW_BEFORE_S, last + WINDOW_AFTER_S


def site_servers(location_id: str) -> list[dict]:
    s = db.sites
    return db.rows(sa.select(s.c.id, s.c.name).where(s.c.location_id == location_id, s.c.retired_at.is_(None)).order_by(s.c.name))


async def _ask(server: dict, since: float, until: float, email: str) -> dict:
    base = {"server_id": server["id"], "server_name": server["name"]}
    conn = registry.get(server["id"])
    if conn is None:
        return {**base, "status": "offline"}
    headers = {"x-hub-user": email, "x-hub-role": "viewer"}
    try:
        status, body = await asyncio.wait_for(
            conn.call("GET", PATH, urlencode({"since": round(since, 3), "until": round(until, 3),
                                              "at": round(since + WINDOW_BEFORE_S, 3)}), headers, None, TIMEOUT_S),
            TIMEOUT_S + 2)
    except asyncio.TimeoutError:
        return {**base, "status": "timeout", "error": f"no answer within {TIMEOUT_S:.0f} s"}
    except Exception as e:  # tunnel aborted, too many streams: one server must not spoil the card
        return {**base, "status": "error", "error": str(e)[:200] or type(e).__name__}
    if status == 404:
        return {**base, "status": "error", "error": "this server's software is too old for the vehicle card (update it)"}
    if status != 200:
        return {**base, "status": "error", "error": f"HTTP {status}: {body[:120].decode(errors='replace')}"}
    try:
        data = json.loads(body.decode() or "null")
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("vehicles"), list):
        return {**base, "status": "error", "error": "unreadable answer"}
    vehicles = [v for v in data["vehicles"] if isinstance(v, dict)]
    total = data.get("total") if isinstance(data.get("total"), int) else len(vehicles)
    return {**base, "status": "ok", "vehicles": vehicles, "total": total}


def merge(results: list[dict], cam_names: dict[tuple[str, str], str]) -> list[dict]:
    """Every server's vehicles, oldest first, each with its server and the camera's name in the hub's registry."""
    out = []
    for r in results:
        for v in r.get("vehicles") or []:
            cam = str(v.get("camera_id") or "")
            out.append({**v, "server_id": r["server_id"], "server_name": r["server_name"],
                        "camera_name": cam_names.get((r["server_id"], cam)) or v.get("camera_name") or cam})
    return sorted(out, key=lambda v: float(v.get("ts") or 0))


async def fetch(incident: dict, email: str, now: float | None = None) -> dict:
    """{since, until, at, stale, vehicles, total, servers: [{server_id, server_name, status, error?}]}. total: every
    vehicle in the window (each server sends at most 20: those with a plate, then the nearest to the incident). Kept on
    the incident when at least one server answered; otherwise the kept answer, stale."""
    now = time.time() if now is None else now
    since, until = window(incident)
    servers = site_servers(incident["location_id"])
    results = list(await asyncio.gather(*(_ask(s, since, until, email) for s in servers)))
    status = [{k: r[k] for k in ("server_id", "server_name", "status", "error") if k in r} for r in results]
    if any(r["status"] == "ok" for r in results):
        cams = db.cameras
        ids = [s["id"] for s in servers]
        names = {(c["server_id"], c["camera_id"]): c["name"] for c in db.rows(
            sa.select(cams.c.server_id, cams.c.camera_id, cams.c.name).where(cams.c.server_id.in_(ids)))} if ids else {}
        kept = {"at": now, "since": since, "until": until, "vehicles": merge(results, names),
                "total": sum(r.get("total") or 0 for r in results if r["status"] == "ok")}
        with db.engine().begin() as c:   # not updated_at: the queue row hasn't changed
            c.execute(sa.update(db.incidents).where(db.incidents.c.id == incident["id"]).values(vehicles=kept))
        return {**kept, "stale": False, "servers": status}
    row = db.one(sa.select(db.incidents.c.vehicles).where(db.incidents.c.id == incident["id"])) or {}
    kept = row.get("vehicles") if isinstance(row.get("vehicles"), dict) else None
    if kept:
        return {**kept, "stale": True, "servers": status}
    return {"at": None, "since": since, "until": until, "vehicles": [], "total": 0, "stale": False, "servers": status}
