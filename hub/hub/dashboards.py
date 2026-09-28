"""Home dashboards and camera groups.

A dashboard is a widget grid (see frontend/src/dashboard/types.ts): camera tiles, event feeds, briefings,
alerts, site health and an Ask box, placed on a 12-column grid. Each user has their own; admins can publish
org-shared ones (owner_user_id NULL). Groups are named sets of (site, camera) pairs an org reuses in widgets.
Fleet events fan `GET /api/events` out through the tunnels like fleet.search does.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Literal

import sqlalchemy as sa
from pydantic import BaseModel, Field, ValidationError

from . import auth, db
from .agents import registry
from .fleet import FANOUT_TIMEOUT_S, _get_json
from .roles import allows

log = logging.getLogger("hub.dashboards")
COLS = 12
MAX_WIDGETS = 60
MAX_TILES_DEFAULT = 9


# ---------------------------------------------------------------- config validation
class CameraProps(BaseModel):
    site: str = Field(max_length=24)
    camera: str = Field(max_length=64)
    quality: Literal["sd", "hd"] = "sd"


class CamRef(BaseModel):
    site: str = Field(max_length=24)
    camera: str = Field(max_length=64)


class EventsProps(BaseModel):
    sites: list[str] | None = None
    cameras: list[CamRef] | None = None
    group: str | None = Field(default=None, max_length=24)
    classes: list[Literal["person", "vehicle"]] | None = None
    limit: int = Field(default=20, ge=1, le=100)


class BriefingProps(BaseModel):
    source: Literal["digest", "site"] = "digest"
    site: str | None = Field(default=None, max_length=24)


class AlertsProps(BaseModel):
    kinds: list[str] | None = None
    limit: int = Field(default=20, ge=1, le=100)


class HealthProps(BaseModel):
    sites: list[str] | None = None


class AskProps(BaseModel):
    placeholder: str | None = Field(default=None, max_length=120)


PROPS = {"camera": CameraProps, "events": EventsProps, "briefing": BriefingProps, "alerts": AlertsProps, "health": HealthProps, "ask": AskProps}


class Widget(BaseModel):
    id: str = Field(min_length=1, max_length=24)
    type: Literal["camera", "events", "briefing", "alerts", "health", "ask"]
    x: int = Field(ge=0, lt=COLS)
    y: int = Field(ge=0, le=500)
    w: int = Field(ge=1, le=COLS)
    h: int = Field(ge=1, le=60)
    props: dict = {}


class DashboardConfig(BaseModel):
    version: Literal[1] = 1
    cols: Literal[12] = 12
    rowH: int = Field(default=60, ge=30, le=200)
    widgets: list[Widget] = Field(default_factory=list, max_length=MAX_WIDGETS)


def validate_config(cfg: dict) -> dict:
    """The config as stored: known widget types only, in-bounds, props checked per type. ValueError otherwise."""
    try:
        c = DashboardConfig.model_validate(cfg)
        out = c.model_dump()
        seen = set()
        for w in out["widgets"]:
            if w["id"] in seen:
                raise ValueError(f"duplicate widget id {w['id']}")
            seen.add(w["id"])
            if w["x"] + w["w"] > COLS:
                raise ValueError(f"widget {w['id']} overflows the grid")
            w["props"] = PROPS[w["type"]].model_validate(w["props"]).model_dump(exclude_none=True)
        return out
    except ValidationError as e:
        raise ValueError(e.errors()[0].get("msg", "invalid dashboard")) from e


# ---------------------------------------------------------------- dashboards
def _public(row: dict, with_config: bool = True) -> dict:
    d = {k: row[k] for k in ("id", "org_id", "owner_user_id", "name", "shared", "created_at", "updated_at", "updated_by")}
    if with_config:
        d["config"] = row["config"]
    return d


def list_for(u: dict, org_id: str) -> list[dict]:
    """The user's own dashboards plus the org's shared ones, shared first, then by name."""
    rows = db.rows(sa.select(db.dashboards).where(db.dashboards.c.org_id == org_id,
                                                  sa.or_(db.dashboards.c.owner_user_id == u["id"], db.dashboards.c.shared == True)))  # noqa: E712
    rows.sort(key=lambda r: (not r["shared"], r["name"].lower()))
    return [_public(r, with_config=False) for r in rows]


def get(u: dict, org_id: str, dash_id: str) -> dict | None:
    r = db.one(sa.select(db.dashboards).where(db.dashboards.c.id == dash_id, db.dashboards.c.org_id == org_id))
    if not r or not (r["shared"] or r["owner_user_id"] == u["id"]):
        return None
    return r


def can_edit(u: dict, org_id: str, row: dict) -> bool:
    if row["owner_user_id"] == u["id"] and not row["shared"]:
        return True
    role = auth.role_in(u, org_id)
    return bool(row["shared"] and role and allows(role, "admin"))


def create(u: dict, org_id: str, name: str, config: dict, shared: bool) -> dict:
    row = {"id": db.new_id("d"), "org_id": org_id, "owner_user_id": None if shared else u["id"], "name": name.strip()[:120],
           "config": validate_config(config), "shared": shared, "created_at": time.time(), "updated_at": time.time(), "updated_by": u["id"]}
    db.insert(db.dashboards, row)
    return _public(row)


def update(u: dict, org_id: str, row: dict, name: str | None, config: dict | None, shared: bool | None) -> dict:
    values: dict = {"updated_at": time.time(), "updated_by": u["id"]}
    if name is not None:
        values["name"] = name.strip()[:120]
    if config is not None:
        values["config"] = validate_config(config)
    if shared is not None and shared != row["shared"]:
        values["shared"] = shared
        values["owner_user_id"] = None if shared else u["id"]
    db.run(sa.update(db.dashboards).where(db.dashboards.c.id == row["id"]).values(**values))
    return _public(db.one(sa.select(db.dashboards).where(db.dashboards.c.id == row["id"])))


def delete(row: dict) -> None:
    db.run(sa.delete(db.dashboards).where(db.dashboards.c.id == row["id"]))


def _default_key(u: dict, org_id: str) -> str:
    key = f"dash_default:{u['id']}:{org_id}"
    assert len(key) <= 64
    return key


def default_id(u: dict, org_id: str) -> str | None:
    r = db.one(sa.select(db.kv).where(db.kv.c.key == _default_key(u, org_id)))
    return (r["value"] or {}).get("id") if r else None


def set_default(u: dict, org_id: str, dash_id: str | None) -> None:
    key = _default_key(u, org_id)
    db.run(sa.delete(db.kv).where(db.kv.c.key == key))
    if dash_id:
        db.insert(db.kv, {"key": key, "value": {"id": dash_id}})


def default_config(u: dict, org_id: str) -> dict:
    """A generated starting point: the first cameras of the sites this user can see, an events feed, alerts,
    health, the org digest and an Ask box. Never stored until the user saves it under a name."""
    widgets: list[dict] = []
    n = 0
    tiles: list[tuple[str, str]] = []
    for s in auth.visible_sites(u, org_id):
        for c in (s.get("summary") or {}).get("cameras") or []:
            if len(tiles) < MAX_TILES_DEFAULT:
                tiles.append((s["id"], c["id"]))
    for i, (site, cam) in enumerate(tiles):
        widgets.append({"id": f"w_cam{i}", "type": "camera", "x": (i % 3) * 4, "y": (i // 3) * 4, "w": 4, "h": 4, "props": {"site": site, "camera": cam, "quality": "sd"}})
        n = (i // 3 + 1) * 4
    widgets += [
        {"id": "w_events", "type": "events", "x": 0, "y": n, "w": 8, "h": 7, "props": {"limit": 20}},
        {"id": "w_alerts", "type": "alerts", "x": 8, "y": n, "w": 4, "h": 4, "props": {"limit": 20}},
        {"id": "w_health", "type": "health", "x": 8, "y": n + 4, "w": 4, "h": 3, "props": {}},
        {"id": "w_brief", "type": "briefing", "x": 0, "y": n + 7, "w": 8, "h": 4, "props": {"source": "digest"}},
        {"id": "w_ask", "type": "ask", "x": 8, "y": n + 7, "w": 4, "h": 2, "props": {}},
    ]
    return {"version": 1, "cols": COLS, "rowH": 60, "widgets": widgets}


# ---------------------------------------------------------------- groups
def groups_for(org_id: str) -> list[dict]:
    return db.rows(sa.select(db.camera_groups).where(db.camera_groups.c.org_id == org_id).order_by(db.camera_groups.c.name))


def _clean_members(members: list) -> list[dict]:
    out, seen = [], set()
    for m in members or []:
        try:
            ref = CamRef.model_validate(m)
        except ValidationError as e:
            raise ValueError("members must be {site, camera}") from e
        key = (ref.site, ref.camera)
        if key not in seen:
            seen.add(key)
            out.append({"site_id": ref.site, "camera_id": ref.camera})
    if len(out) > 500:
        raise ValueError("too many cameras in one group")
    return out


def create_group(u: dict, org_id: str, name: str, members: list) -> dict:
    row = {"id": db.new_id("g"), "org_id": org_id, "name": name.strip()[:120], "members": _clean_members(members),
           "created_at": time.time(), "updated_at": time.time()}
    db.insert(db.camera_groups, row)
    return row


def update_group(org_id: str, group_id: str, name: str | None, members: list | None) -> dict | None:
    row = db.one(sa.select(db.camera_groups).where(db.camera_groups.c.id == group_id, db.camera_groups.c.org_id == org_id))
    if not row:
        return None
    values: dict = {"updated_at": time.time()}
    if name is not None:
        values["name"] = name.strip()[:120]
    if members is not None:
        values["members"] = _clean_members(members)
    db.run(sa.update(db.camera_groups).where(db.camera_groups.c.id == group_id).values(**values))
    return db.one(sa.select(db.camera_groups).where(db.camera_groups.c.id == group_id))


def delete_group(org_id: str, group_id: str) -> bool:
    row = db.one(sa.select(db.camera_groups.c.id).where(db.camera_groups.c.id == group_id, db.camera_groups.c.org_id == org_id))
    if not row:
        return False
    db.run(sa.delete(db.camera_groups).where(db.camera_groups.c.id == group_id))
    return True


def resolve_group(org_id: str, group_id: str) -> list[tuple[str, str]] | None:
    row = db.one(sa.select(db.camera_groups).where(db.camera_groups.c.id == group_id, db.camera_groups.c.org_id == org_id))
    return [(m["site_id"], m["camera_id"]) for m in row["members"]] if row else None


# ---------------------------------------------------------------- fleet events
async def fleet_events(u: dict, org_id: str, sites: list[str] | None, cameras: list[tuple[str, str]] | None, group: str | None,
                       classes: list[str] | None, limit: int, since: float | None) -> dict:
    """Latest events across the sites the user may see, newest first, each tagged with its site."""
    visible = {s["id"]: s for s in auth.visible_sites(u, org_id)}
    wanted: dict[str, set[str] | None] = {}          # site -> camera ids (None = all)
    members = resolve_group(org_id, group) if group else None
    if group and members is None:
        return {"events": [], "offline": [], "errors": [{"site_id": None, "error": "group not found"}]}
    if members is not None:
        for site, cam in members:
            wanted.setdefault(site, set())
            if wanted[site] is not None:
                wanted[site].add(cam)  # type: ignore[union-attr]
    if cameras:
        for site, cam in cameras:
            wanted.setdefault(site, set())
            if wanted[site] is not None:
                wanted[site].add(cam)  # type: ignore[union-attr]
    for site in sites or []:
        wanted[site] = None
    if not wanted:
        wanted = {sid: None for sid in visible}
    wanted = {sid: cams for sid, cams in wanted.items() if sid in visible}
    headers = {"x-hub-user": u["email"], "x-hub-role": "viewer"}
    offline, errors, events = [], [], []

    async def one(site_id: str, cams: set[str] | None):
        conn = registry.get(site_id)
        if conn is None:
            offline.append(site_id)
            return
        params = {"limit": min(limit, 100), "status": "verified,open,pending", "since": since}
        if cams and len(cams) == 1:
            params["camera"] = next(iter(cams))
        if classes and len(classes) == 1:
            params["label"] = classes[0]
        try:
            status, body = await _get_json(conn, "/api/events", params, headers)
        except Exception as e:  # tunnel timeout / abort
            errors.append({"site_id": site_id, "error": str(e)})
            return
        if status != 200 or not isinstance(body, list):
            errors.append({"site_id": site_id, "error": f"HTTP {status}"})
            return
        name = visible[site_id]["name"]
        for e in body:
            if cams and e.get("camera_id") not in cams:
                continue
            if classes and e.get("camera_class") not in classes:
                continue
            events.append({**e, "site_id": site_id, "site_name": name})

    await asyncio.gather(*(one(sid, cams) for sid, cams in wanted.items()))
    events.sort(key=lambda e: -(e.get("start_ts") or 0))
    return {"events": events[:limit], "offline": offline, "errors": errors}


def event_matches(evt: dict, site_id: str, wanted: dict[str, set[str] | None]) -> bool:
    """For the live socket: does an event from `site_id` fall inside a widget's (site, cameras) selection?"""
    if site_id not in wanted:
        return False
    cams = wanted[site_id]
    return cams is None or evt.get("camera_id") in cams
