"""Traffic camera routes (traffic.py): the cameras' push, plate reads, counts, plate lists, and a camera's setup.

POST /api/traffic/push/{camera}/{secret}   the camera's push (a read or a status report). LAN only: refused through the
                                           hub or a direct connection; an unknown camera or secret is a 404
GET  /api/traffic/reads                    newest first: ?camera, since, until, q (a plate or part of one; near plates
                                           come back as `near`), type, color, direction, list (allow | deny | watch |
                                           any), min_conf, plates (only reads with a plate), all (every read of a pass,
                                           not just its best), noise (reads that aren't a vehicle too: hidden
                                           by default, except in a plate search), look (a description: "white pickup
                                           ladder rack", every word in Otso's or the camera's description), before_id,
                                           limit
GET  /api/traffic/reads/{id}               one read
GET  /api/traffic/reads/{id}/image/{kind}  plate | vehicle | full | violation; a vehicle picture not cut from the
                                           recording yet: 404 at once (Retry-After), the read queued for it
POST /api/traffic/reads/{id}/plate         Read plate: Otso reads the vehicle crop now (operator; metered "plate"; one
                                           run per read every plate_rate_s, 429 with Retry-After); kept beside the
                                           camera's read: {result, plate, plate_raw, region, confidence, reason, read, list}
GET  /api/traffic/around                   the vehicles seen in a window (?since, until, at; at most 6 h): the traffic
                                           cameras' passes and the plates read from event footage, with each plate's
                                           counted notes (traffic.around; the hub's SOC incident card)
GET  /api/traffic/counts                   vehicles per hour or day (?camera, since, until, bucket hour | day)
GET  /api/traffic/status                   each traffic camera: set up, last push, reads and plates today
GET  /api/traffic/lists                    the plate lists, each with last seen, passes in 7 / 30 days, Otso's
                                           description, other entries for its plate and which applies, in force now
GET  /api/traffic/lists/{id}               one entry with its sightings, notes, alarms and the vehicle Otso saw last
POST /api/traffic/lists                    {plate (? and * for a partial plate), list: allow | deny | watch, note?,
                                           expires_at?, category?, schedule? {when: always | soc | hours, days, from,
                                           to}, action? alarm | mark, priority?, cameras?, expect? {vehicle_type, color,
                                           make, model}} (operator)
POST /api/traffic/lists/import             {entries: [{plate, list, note?, category?, expires_at?}]} (operator)
PUT  /api/traffic/lists/{id}               any of the above but plate and list; null clears (operator)
DELETE /api/traffic/lists/{id}             (operator)
GET  /api/cameras/{id}/traffic             what the camera says (model, licence, plate reading, push slots, SD card),
                                           this server's setup and the camera's push health (operator)
PUT  /api/cameras/{id}/traffic             {enabled, plate_reading?}: set the camera up / stop its push (admin)
PUT  /api/cameras/{id}/traffic/alerts      {circling?, new?, unusual_time?, when?: armed | always}: the camera's plate
                                           pattern alerts (admin); GET /api/cameras/{id}/traffic shows them (server.alerts)

Roles through the hub (hub/hub/roles.py): reads, images, counts and lists are a viewer's (like events and their
plates); list changes an operator's (like watching a vehicle); a camera's traffic settings operator to read, admin to
change (the /api/cameras rule). Checked here too for a tunneled or direct request.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import httpx

from . import aischedule, plate, traffic
from .config import settings
from .db import db
from .onvif_soap import OnvifError
from .speakers_api import hub_role, require

log = logging.getLogger("nvr.traffic")
router = APIRouter()
pipeline = None   # api.lifespan sets it: alarm events are published and described like any verified event


async def _after(out: dict) -> None:
    """A new alarm event: publish it (the hub and the SOC take it from there) and describe it; an event that got a plate:
    re-index it so its plate finds it."""
    p = pipeline
    if p is None:
        return
    try:
        if out.get("alarm_id"):
            eid = out["alarm_id"]
            p.publish(eid)
            if not p.plan_synopsis(eid):
                await p.reindex(eid)
        if out.get("event_id") and out["event_id"] != out.get("alarm_id"):
            e = db.event(out["event_id"])
            if e and e["status"] == "verified":
                await p.reindex(out["event_id"])
            p.publish(out["event_id"])
    except Exception:  # noqa: BLE001 - the read is stored; a publish or index problem must not fail the camera's push
        log.exception("traffic: after read %s", out.get("read_id"))


@router.post("/api/traffic/push/{camera_id}/{secret}")
async def push(camera_id: str, secret: str, request: Request):
    if hub_role(request) is not None:
        raise HTTPException(403, "cameras push on the LAN")
    cam = traffic.camera(camera_id)
    if not traffic.check_secret(cam, secret):
        raise HTTPException(404, "not found")
    size = int(request.headers.get("content-length") or 0)
    if size > traffic.BODY_MAX:
        raise HTTPException(413, "too large")
    body = await request.body()
    if len(body) > traffic.BODY_MAX:
        raise HTTPException(413, "too large")
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(400, "not JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "not a JSON object")
    if traffic.is_status(payload):
        traffic.health.heard(camera_id, "status")
        return {"result": "ok"}
    out = await asyncio.to_thread(traffic.ingest, cam, payload, time.time())
    await _after(out)
    return {"result": "ok"}


@router.get("/api/traffic/reads")
async def reads(camera: str | None = None, since: float | None = None, until: float | None = None,
                q: str | None = Query(None, max_length=20), type: str | None = Query(None, max_length=30),
                color: str | None = Query(None, max_length=30), direction: Literal["approach", "away"] | None = None,
                list: Literal["allow", "deny", "watch", "any"] | None = None, min_conf: float = Query(0, ge=0, le=1),
                plates: bool = False, all: bool = False, before_id: int | None = None, limit: int = Query(100, ge=1, le=500),
                noise: bool = False, look: str | None = Query(None, max_length=80)):
    return await asyncio.to_thread(traffic.search, camera, since, until, q, type, color, direction, list, min_conf,
                                   plates, all, before_id, limit, noise, look)


@router.get("/api/traffic/reads/{read_id}")
async def one_read(read_id: int):
    r = traffic.read(read_id)
    if not r:
        raise HTTPException(404, "unknown read")
    if r["pass_id"]:
        r["pass"] = [traffic.public(x) for x in db.all("SELECT * FROM vehicle_reads WHERE pass_id=? ORDER BY ts", [r["pass_id"]])]
    return r


@router.get("/api/traffic/reads/{read_id}/image/{kind}")
async def read_image(read_id: int, kind: Literal["plate", "vehicle", "full", "violation"]):
    f = traffic.image_path(read_id, kind)
    if f is None and kind == "vehicle" and (r := traffic.read(read_id)) and not traffic.is_noise(r):
        # sent without one: queued for a cut from the recording (one at a time, traffic.CUT_POOL), never cut here: a
        # page of reads cutting their pictures at once filled the shared pool and stalled the whole server
        traffic.from_recording.add(read_id, time.time())
        raise HTTPException(404, "the picture is being cut from the recording", headers={"Retry-After": "5"})
    if f is None:
        raise HTTPException(404, "no such picture")
    return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/api/traffic/reads/{read_id}/plate")
async def read_plate(read_id: int, request: Request):
    """Ask Otso on a read: Otso reads the plate from the camera's crops (the camera's reader misses plates they show),
    then notes on the plate's history (traffic.plate_notes: counted, not the model's). A person asked, so it runs
    outside the Site's AI hours too, metered as "plate" (as Read plate on an event)."""
    require(request, "operator")
    first = traffic.read(read_id)
    if not first:
        raise HTTPException(404, "unknown read")
    wait = traffic.read_limiter.wait_s(read_id)
    if wait > 0:
        raise HTTPException(429, f"this plate was just read; try again in {wait:.0f} s", headers={"Retry-After": str(int(wait) + 1)})
    # no vehicle picture yet: from the recording (on the cutters' own pool)
    await asyncio.get_running_loop().run_in_executor(traffic.CUT_POOL, traffic.cut_from_recording, read_id)
    crops = await asyncio.to_thread(traffic.otso_crops, read_id)
    if not crops and not first["plate"]:
        raise HTTPException(400, "no picture of the vehicle was kept for this read")
    if crops and (pipeline is None or not pipeline.vlm_ready):
        raise HTTPException(503, "Otso is not ready yet")
    traffic.read_limiter.start(read_id)
    try:
        if crops:
            with aischedule.job(plate.KIND):
                result = await asyncio.wait_for(plate.read(crops, "chat", system=plate.ON_REQUEST_SYSTEM), plate.TIMEOUT_S)
        else:
            result = {"result": "skipped", "reason": "no picture of the vehicle"}
    except (asyncio.TimeoutError, httpx.TimeoutException):
        raise HTTPException(504, "Otso took too long to read the plate; try again in a moment")
    except Exception as ex:  # noqa: BLE001 - the model is down or misconfigured
        log.warning("traffic read %s: plate read failed: %s", read_id, ex)
        raise HTTPException(502, f"the plate could not be read: {str(ex)[:200]}")
    finally:
        traffic.read_limiter.done(read_id)
    about = first["plate"] or (result.get("plate") if result.get("result") == "read" else None)
    notes = await asyncio.to_thread(traffic.plate_notes, about, first["ts"]) if about else None
    r = await asyncio.to_thread(traffic.store_otso, read_id, result, notes)
    hit = traffic.match_list(result.get("plate")) if result.get("result") == "read" else None
    out = {k: result.get(k) for k in ("result", "plate", "plate_raw", "region", "confidence", "reason", "model", "crops")}
    return {**out, "read": r, "list": {k: hit[k] for k in ("list", "match", "plate", "note")} if hit else None,
            "about": notes["plate"] if notes else None, "notes": notes["notes"] if notes else []}


@router.get("/api/traffic/around")
async def vehicles_around(since: float, until: float, at: float | None = None):
    """The vehicles seen in a window (traffic.around): the hub's SOC incident card, 10 minutes before an incident to 5
    after it; `at` (the incident's first event) picks which ones when there are more than fit. Viewer, like the reads."""
    if until <= since or until - since > traffic.AROUND_MAX_S:
        raise HTTPException(422, f"the window must be positive and at most {traffic.AROUND_MAX_S // 3600} hours")
    return {"since": since, "until": until, **await asyncio.to_thread(traffic.around, since, until, at)}


@router.get("/api/traffic/counts")
async def counts(camera: str | None = None, since: float | None = None, until: float | None = None,
                 bucket: Literal["hour", "day"] = "hour"):
    return await asyncio.to_thread(traffic.counts, camera, since, until, bucket)


@router.get("/api/traffic/status")
async def status():
    day = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    out = []
    for c in db.cameras():
        t = c.get("traffic") or {}
        if not t:
            continue
        sql, p = traffic.noise_sql()
        n = db.one("SELECT COUNT(*) AS reads, SUM(plate IS NOT NULL) AS plates FROM vehicle_reads "
                   f"WHERE camera_id=? AND ts>=? AND best=1 AND NOT {sql}", [c["id"], day, *p]) or {}
        h = traffic.health.camera(c["id"])
        out.append({"camera_id": c["id"], "name": c["name"], "enabled": bool(t.get("enabled")), "last_push": h.get("push"),
                    "last_read": h.get("read"), "last_status": h.get("status"), "reads_today": n.get("reads") or 0,
                    "plates_today": n.get("plates") or 0, "zone_offset_s": t.get("zone_offset_s")})
    return {"cameras": out, "silent_after_s": settings.traffic_silent_s}


class Schedule(BaseModel):
    when: Literal["always", "soc", "hours"]
    days: list[int] | None = None
    from_: str | None = Field(None, alias="from", max_length=5)
    to: str | None = Field(None, max_length=5)

    def plain(self) -> dict:
        return {"when": self.when, "days": self.days, "from": self.from_, "to": self.to}


class Expect(BaseModel):
    vehicle_type: str | None = Field(None, max_length=40)
    color: str | None = Field(None, max_length=40)
    make: str | None = Field(None, max_length=40)
    model: str | None = Field(None, max_length=40)


Category = Literal["employee", "contractor", "visitor", "resident", "delivery", "trespass", "police", "other"]


class EntryIn(BaseModel):
    plate: str = Field(..., min_length=2, max_length=20)
    list: Literal["allow", "deny", "watch"]
    note: str = Field("", max_length=200)
    expires_at: float | None = None
    category: Category | None = None
    schedule: Schedule | None = None
    action: Literal["alarm", "mark"] | None = None
    priority: Literal["low", "medium", "high"] | None = None
    cameras: list[str] | None = Field(None, max_length=50)
    expect: Expect | None = None


class EntryEdit(BaseModel):
    """Only the fields sent change; a field sent as null clears it (clear_expiry: the older way to clear expires_at)."""
    note: str | None = Field(None, max_length=200)
    expires_at: float | None = None
    clear_expiry: bool = False
    category: Category | None = None
    schedule: Schedule | None = None
    action: Literal["alarm", "mark"] | None = None
    priority: Literal["low", "medium", "high"] | None = None
    cameras: list[str] | None = Field(None, max_length=50)
    expect: Expect | None = None


class ImportIn(BaseModel):
    entries: list[dict] = Field(..., max_length=traffic.IMPORT_MAX)


def _options(body: BaseModel, sent: set[str]) -> dict:
    out = {}
    for k in ("category", "action", "priority", "cameras"):
        if k in sent:
            out[k] = getattr(body, k)
    if "schedule" in sent:
        out["schedule"] = body.schedule.plain() if body.schedule else None
    if "expect" in sent:
        out["expect"] = body.expect.model_dump() if body.expect else None
    return out


@router.get("/api/traffic/lists")
async def lists():
    """Every entry with what the Lists page shows (traffic.lists_overview): last seen, passes in 7 and 30 days, Otso's
    description of its vehicle, other entries for the same plate and which list applies, in force now."""
    return await asyncio.to_thread(traffic.lists_overview)


@router.get("/api/traffic/lists/{entry_id}")
async def list_entry(entry_id: int):
    """One entry opened (traffic.entry_detail): its latest sightings, the plate's notes, its alarms, the vehicle Otso saw last."""
    d = await asyncio.to_thread(traffic.entry_detail, entry_id)
    if d is None:
        raise HTTPException(404, "unknown entry")
    return d


@router.post("/api/traffic/lists")
async def add_entry(body: EntryIn, request: Request):
    who = require(request, "operator")
    try:
        return await asyncio.to_thread(traffic.add_entry, body.plate, body.list, body.note, body.expires_at, who,
                                       **_options(body, body.model_fields_set))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/api/traffic/lists/import")
async def import_entries(body: ImportIn, request: Request):
    """CSV rows the page parsed: {plate, list, note?, category?, expires_at?} each; per row ok or why not (operator)."""
    who = require(request, "operator")
    results = await asyncio.to_thread(traffic.import_entries, body.entries, who)
    log.info("plate lists: %d of %d rows imported by %s", sum(r["ok"] for r in results), len(results), who)
    return {"results": results, "added": sum(r["ok"] for r in results)}


@router.put("/api/traffic/lists/{entry_id}")
async def edit_entry(entry_id: int, body: EntryEdit, request: Request):
    who = require(request, "operator")
    sent = set(body.model_fields_set)
    changes = _options(body, sent)
    if "note" in sent:
        changes["note"] = body.note or ""
    if body.clear_expiry or "expires_at" in sent:
        changes["expires_at"] = None if body.clear_expiry else body.expires_at
    try:
        e = await asyncio.to_thread(traffic.edit_entry, entry_id, who, **changes)
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    if e is None:
        raise HTTPException(404, "unknown entry")
    return e


@router.delete("/api/traffic/lists/{entry_id}")
async def delete_entry(entry_id: int, request: Request):
    require(request, "operator")
    db.execute("DELETE FROM plate_lists WHERE id=?", [entry_id])
    return {"ok": True}


def _cam(camera_id: str) -> dict:
    cam = traffic.camera(camera_id)
    if not cam:
        raise HTTPException(404, "unknown camera")
    return cam


def _ours(cam: dict) -> dict:
    t = cam.get("traffic") or {}
    h = traffic.health.camera(cam["id"])
    return {"enabled": bool(t.get("enabled")), "set_at": t.get("set_at"), "last_push": h.get("push"),
            "last_read": h.get("read"), "last_status": h.get("status"), "zone_offset_s": t.get("zone_offset_s"),
            "silent_after_s": settings.traffic_silent_s, "alerts": traffic.alerts_config(cam),
            "learn_days": settings.traffic_learn_days, "circling": {"passes": settings.traffic_circling_passes,
                                                                    "minutes": round(settings.traffic_circling_s / 60)}}


@router.get("/api/cameras/{camera_id}/traffic")
async def camera_traffic(camera_id: str, request: Request):
    require(request, "operator")
    cam = _cam(camera_id)
    try:
        cs = await asyncio.to_thread(traffic.camera_state, cam)
    except OnvifError as e:   # milesight.WebApiError / Unsupported: not a traffic camera, or it doesn't answer
        return {"supported": False, "reason": str(e), "server": _ours(cam)}
    return {"supported": True, "camera": cs, "server": _ours(cam)}


class SetupIn(BaseModel):
    enabled: bool
    plate_reading: bool | None = None


class AlertsIn(BaseModel):
    circling: bool | None = None
    new: bool | None = None
    unusual_time: bool | None = None
    when: Literal["armed", "always"] | None = None


@router.put("/api/cameras/{camera_id}/traffic/alerts")
async def set_traffic_alerts(camera_id: str, body: AlertsIn, request: Request):
    """The camera's plate pattern alerts (traffic.check_patterns): circling, new plates, regulars at unusual times, and
    whether new / unusual alert only while the Site's SOC monitoring is armed (admin: the camera's configuration)."""
    who = require(request, "admin")
    _cam(camera_id)
    alerts = await asyncio.to_thread(traffic.set_alerts, camera_id, **body.model_dump())
    log.info("[%s] plate pattern alerts %s by %s", camera_id, alerts, who)
    return _ours(_cam(camera_id))


@router.put("/api/cameras/{camera_id}/traffic")
async def set_camera_traffic(camera_id: str, body: SetupIn, request: Request):
    who = require(request, "admin")
    cam = _cam(camera_id)
    try:
        cs = await asyncio.to_thread(traffic.configure, cam, body.enabled, body.plate_reading)
    except traffic.SetupError as e:
        raise HTTPException(409, str(e))
    except OnvifError as e:
        raise HTTPException(502, str(e))
    log.info("[%s] traffic push %s by %s", camera_id, "set up" if body.enabled else "stopped", who)
    return {"supported": True, "camera": cs, "server": _ours(_cam(camera_id))}
