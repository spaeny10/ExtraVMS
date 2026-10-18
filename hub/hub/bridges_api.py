"""Axiom Bridge routes (bridges.py): a Site's bridges, their enrollment codes and its bridged cameras.

Everyone who sees the Site reads its bridges' status (GET). Its admins (customer admin and up who see the Site, hub
administrators; auth.location_access + require_role admin, as for the Site's camera groups) create and revoke
enrollment codes, rename, rotate and retire its bridges, map its cameras to its central instance (allocated by hub
administrators on the Hosts page), test them and apply the relay address to the instance's camera. Every change is
audited (bridges.py). The two sockets, /bridge and /host-relay, are in api.py next to /agent and /host-agent.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import auth, bridges, hosts
from .roles import allows

router = APIRouter()


def user(request: Request) -> dict:
    return auth.require_user(request)


def _site_admin(u: dict, location_id: str) -> dict:
    loc, _ = auth.location_access(u, location_id)
    auth.require_role(u, loc["org_id"], "admin")
    return loc


def _err(e: Exception) -> HTTPException:
    if isinstance(e, bridges.Refused):
        return HTTPException(e.status, e.detail)
    if isinstance(e, hosts.HostError):
        return HTTPException(502, f"{e}: nothing was changed")
    return HTTPException(502, str(e))


class CodeIn(BaseModel):
    name: str | None = Field(None, max_length=120)   # the bridge's name; empty = the PC's hostname


class BridgePatch(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class TargetIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    host: str = Field(min_length=1, max_length=64)                      # the camera's LAN IPv4 address
    ports: list[int] | None = Field(None, max_length=bridges.MAX_PORTS)  # None = 554, 80, 555
    camera_id: str | None = Field(None, max_length=64)                  # the instance's camera (Apply to camera)


class TargetPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    host: str | None = Field(None, min_length=1, max_length=64)
    camera_id: str | None = Field(None, max_length=64)
    enabled: bool | None = None


class ApplyIn(BaseModel):
    camera_id: str | None = Field(None, max_length=64)   # None = the camera the target is mapped to


@router.get("/api/locations/{location_id}/bridges")
async def site_bridges(location_id: str, u: dict = Depends(user)):
    loc, role = auth.location_access(u, location_id)
    return bridges.site_view(loc, allows(role, "admin"))


@router.post("/api/locations/{location_id}/bridges/codes")
async def create_code(location_id: str, body: CodeIn, u: dict = Depends(user)):
    """A single-use enrollment code (in this answer only: the hub keeps a hash) and the command that installs the bridge."""
    loc = _site_admin(u, location_id)
    row, code = bridges.create_code(loc, body.name, u)
    return {**bridges.code_out(row), "code": code, "install": bridges.install_command(code), "hub_url": bridges.bridge_url()}


@router.delete("/api/locations/{location_id}/bridges/codes/{code_id}")
async def revoke_code(location_id: str, code_id: str, u: dict = Depends(user)):
    loc = _site_admin(u, location_id)
    try:
        bridges.revoke_code(loc, code_id, u)
    except bridges.Refused as e:
        raise _err(e)
    return {"ok": True}


@router.patch("/api/locations/{location_id}/bridges/{bridge_id}")
async def rename_bridge(location_id: str, bridge_id: str, body: BridgePatch, u: dict = Depends(user)):
    loc = _site_admin(u, location_id)
    try:
        b = bridges.rename_bridge(loc, bridges.site_bridge(location_id, bridge_id), body.name, u)
    except bridges.Refused as e:
        raise _err(e)
    return bridges.bridge_out(b, True)


@router.post("/api/locations/{location_id}/bridges/{bridge_id}/rotate-token")
async def rotate_bridge(location_id: str, bridge_id: str, u: dict = Depends(user)):
    """A new device token, sent to the connected bridge (the old one works 10 more minutes)."""
    loc = _site_admin(u, location_id)
    try:
        await bridges.rotate_token(loc, bridges.site_bridge(location_id, bridge_id), u)
    except (bridges.Refused, bridges.BridgeError) as e:
        raise _err(e)
    return {"ok": True}


@router.delete("/api/locations/{location_id}/bridges/{bridge_id}")
async def retire_bridge(location_id: str, bridge_id: str, u: dict = Depends(user)):
    """Retire: the bridge is disconnected for good and its cameras unmapped (their recordings stop)."""
    loc = _site_admin(u, location_id)
    try:
        failed = await bridges.retire(loc, bridges.site_bridge(location_id, bridge_id), u)
    except bridges.Refused as e:
        raise _err(e)
    return {"ok": True, "relay_pending": failed}


@router.post("/api/locations/{location_id}/bridges/{bridge_id}/targets")
async def create_target(location_id: str, bridge_id: str, body: TargetIn, u: dict = Depends(user)):
    """Map a camera behind the bridge to the Site's central instance (listen ports on its host; 502 if the host can't)."""
    loc = _site_admin(u, location_id)
    try:
        b = bridges.site_bridge(location_id, bridge_id)
        t = await bridges.create_target(loc, b, body.name, body.host, body.ports, body.camera_id, u)
    except (bridges.Refused, hosts.HostError) as e:
        raise _err(e)
    return bridges.target_out(t, b, True)


@router.patch("/api/locations/{location_id}/bridges/{bridge_id}/targets/{target_id}")
async def update_target(location_id: str, bridge_id: str, target_id: str, body: TargetPatch, u: dict = Depends(user)):
    loc = _site_admin(u, location_id)
    try:
        b = bridges.site_bridge(location_id, bridge_id)
        t = await bridges.update_target(loc, b, bridges.site_target(location_id, bridge_id, target_id), body.model_dump(exclude_unset=True), u)
    except bridges.Refused as e:
        raise _err(e)
    return bridges.target_out(t, b, True)


@router.delete("/api/locations/{location_id}/bridges/{bridge_id}/targets/{target_id}")
async def delete_target(location_id: str, bridge_id: str, target_id: str, u: dict = Depends(user)):
    loc = _site_admin(u, location_id)
    try:
        b = bridges.site_bridge(location_id, bridge_id)
        err = await bridges.delete_target(loc, b, bridges.site_target(location_id, bridge_id, target_id), u)
    except bridges.Refused as e:
        raise _err(e)
    return {"ok": True, "relay_pending": err}


@router.post("/api/locations/{location_id}/bridges/{bridge_id}/targets/{target_id}/test")
async def test_target(location_id: str, bridge_id: str, target_id: str, u: dict = Depends(user)):
    """Test: the bridge opens (and closes) a TCP connection to each of the camera's ports."""
    _site_admin(u, location_id)
    try:
        bridges.site_bridge(location_id, bridge_id)
        return await bridges.registry.test_target(bridges.site_target(location_id, bridge_id, target_id))
    except (bridges.Refused, bridges.BridgeError) as e:
        raise _err(e)


@router.post("/api/locations/{location_id}/bridges/{bridge_id}/targets/{target_id}/apply")
async def apply_target(location_id: str, bridge_id: str, target_id: str, body: ApplyIn, u: dict = Depends(user)):
    """Apply to camera: the instance's camera gets the relay address and ports (GET then PUT through its tunnel; its
    password and every other setting stay as they are)."""
    loc = _site_admin(u, location_id)
    try:
        b = bridges.site_bridge(location_id, bridge_id)
        return await bridges.apply_to_camera(loc, b, bridges.site_target(location_id, bridge_id, target_id), body.camera_id, u)
    except (bridges.Refused, bridges.BridgeError) as e:
        raise _err(e)
