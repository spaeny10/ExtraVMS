"""Deterrence routes (deterrence.py): a camera's strobe / siren / light / relays and its automatic rules.

GET  /api/cameras/{id}/deterrence          what the camera can do, what is on now (with the seconds left), the last action
POST /api/cameras/{id}/deterrence          {action: strobe | siren | light | stop | relay, on?, token?, seconds?}
GET  /api/cameras/{id}/deterrence/log      the camera's last actions (who or which rule, ok / failed)
GET  /api/cameras/{id}/deterrence/rules    its automatic rules, with the zones, linked speakers and messages to pick from
PUT  /api/cameras/{id}/deterrence/rules    replace them (admin)

Roles through the hub (hub/hub/roles.py): reading and acting need operator ("same as Hold to Talk": a viewer watches, an
operator acts), the rules are configuration (admin, under the /api/cameras rule). Checked here too for a tunneled or
direct request; the site's own page has no sign-in (the LAN is the trust boundary).
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError

from . import deterrence
from .db import db
from .deterrence import NoOutput, NotReady
from .onvif_soap import OnvifError
from .speakers_api import require

router = APIRouter()


async def _camera(camera_id: str) -> deterrence.DeterrenceCamera:
    try:
        d = await deterrence.manager.ready(camera_id)
    except KeyError:
        if any(c["id"] == camera_id for c in db.cameras()):
            raise HTTPException(409, "the camera is turned off")
        raise HTTPException(404, "unknown camera")
    return d


@router.get("/api/cameras/{camera_id}/deterrence")
async def get_deterrence(camera_id: str, request: Request):
    require(request, "operator")
    d = await _camera(camera_id)
    if d.caps is None:
        raise HTTPException(503, "camera not probed yet; try again in a few seconds")
    return d.public()


class ActIn(BaseModel):
    action: Literal["strobe", "siren", "light", "stop", "relay"]
    on: bool = True
    token: str | None = Field(None, max_length=64)                      # the relay, for action "relay"
    seconds: float | None = Field(None, ge=1, le=deterrence.MAX_S)     # default DEFAULT_S


@router.post("/api/cameras/{camera_id}/deterrence")
async def act(camera_id: str, body: ActIn, request: Request):
    """Set off (or turn off) one output, or stop everything. An action on an output that is running replaces it."""
    by = require(request, "operator")
    d = await _camera(camera_id)
    try:
        if body.action == "stop":
            result = await d.stop(by=by)
        else:
            result = await d.turn(body.action, body.on, token=body.token, seconds=body.seconds, by=by)
    except NotReady as e:
        raise HTTPException(503, f"the camera can't be reached over ONVIF: {e}")
    except NoOutput as e:
        raise HTTPException(409, str(e))
    except (OnvifError, OSError) as e:
        raise HTTPException(503, f"the camera answered: {str(e)[:300]}")
    return {**d.public(), "result": result}


@router.get("/api/cameras/{camera_id}/deterrence/log")
async def deterrence_log(camera_id: str, request: Request, limit: int = Query(50, ge=1, le=500)):
    require(request, "operator")
    rows = db.all("SELECT ts, action, target, on_state, seconds, by, source, event_id, result, error FROM deterrence_log "
                  "WHERE camera_id=? ORDER BY id DESC LIMIT ?", [camera_id, limit])
    return [{**{k: r[k] for k in r if k != "on_state"}, "on": bool(r["on_state"])} for r in rows]


def _rules_view(cam: dict) -> dict:
    d = deterrence.manager.get(cam["id"])
    acts = ((d.caps or {}).get("actions") or {}) if d else {}
    from . import talkdown
    return {"rules": cam.get("deterrence_rules") or [], "zones": deterrence.zone_names(cam.get("zones")),
            "speakers": [{"id": s["id"], "name": s["name"]} for s in db.speakers() if cam["id"] in (s.get("camera_ids") or [])],
            "messages": [{"id": m["id"], "name": m["name"]} for m in talkdown.messages()],
            "armed": deterrence.armed_state(), "caps": {k: bool(acts.get(k)) for k in deterrence.ACTIONS}}


def _cam_row(camera_id: str) -> dict:
    cam = next((c for c in db.cameras() if c["id"] == camera_id), None)
    if cam is None:
        raise HTTPException(404, "unknown camera")
    return cam


@router.get("/api/cameras/{camera_id}/deterrence/rules")
async def get_rules(camera_id: str, request: Request):
    require(request, "operator")
    return _rules_view(_cam_row(camera_id))


class RulesIn(BaseModel):
    rules: list[dict] = Field(default_factory=list, max_length=deterrence.MAX_RULES)


@router.put("/api/cameras/{camera_id}/deterrence/rules")
async def put_rules(camera_id: str, body: RulesIn, request: Request):
    who = require(request, "admin")
    cam = _cam_row(camera_id)
    try:
        rules = deterrence.clean_rules(body.rules)
    except ValidationError as e:
        raise HTTPException(422, "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())[:500])
    except ValueError as e:
        raise HTTPException(422, str(e))
    zones = set(deterrence.zone_names(cam.get("zones")))
    linked = {s["id"] for s in db.speakers() if camera_id in (s.get("camera_ids") or [])}
    for r in rules:
        if r["zone"] and r["zone"] not in zones:
            raise HTTPException(422, f"the camera has no zone named {r['zone']!r}")
        if r["speaker_id"] and r["speaker_id"] not in linked:
            raise HTTPException(422, f"speaker {r['speaker_id']!r} isn't linked to this camera")
        if bool(r["speaker_id"]) != bool(r["message_id"]):
            raise HTTPException(422, "a talk-down message needs both the speaker and the message")
    db.set_deterrence_rules(camera_id, rules)
    deterrence.log.info("deterrence rules of %s: %d (%d on), by %s", camera_id, len(rules), sum(1 for r in rules if r["enabled"]), who)
    return _rules_view(_cam_row(camera_id))
