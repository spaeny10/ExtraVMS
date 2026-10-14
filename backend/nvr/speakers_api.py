"""Talk-down routes (talkdown.py): IP speakers, the message library, play / stop / status, Describe & warn
(talkdown_describe.py).

Roles through the hub (hub/hub/roles.py): play, stop and describe need operator, everything else that writes needs admin.
Checked here too for a tunneled or direct request (the site's own page has no sign-in: the LAN is the trust
boundary). A speaker's password is never returned; omitted on PUT = kept, as for cameras.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import re
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

from . import hub_agent, talkdown, talkdown_describe
from .db import db

router = APIRouter()

RANK = {"viewer": 0, "operator": 1, "admin": 2, "owner": 3}
ID_RX = r"^[a-z0-9_]{1,32}$"
HOST_RX = r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,252}$"
PATH_RX = r"^/[A-Za-z0-9._~\-/=&?%]{0,200}$"   # appended to rtsp://host:port: no "@", spaces or a scheme


def hub_role(request: Request) -> str | None:
    """The hub user's role for a tunneled or direct request (api._hub_role); None = this server's own page."""
    if hub_agent.is_tunnel(request.scope) or request.scope.get("nvr_direct"):
        return request.headers.get("x-hub-role")
    return None


def require(request: Request, needed: str) -> str:
    """Who is asking (the hub user, or "local"), after checking their hub role."""
    role = hub_role(request)
    if role is not None and RANK.get(role, -1) < RANK[needed]:
        raise HTTPException(403, f"needs the {needed} role")
    return request.headers.get("x-hub-user") or "hub" if role is not None else "local"


def public_speaker(s: dict) -> dict:
    return {**{k: s.get(k) for k in ("id", "name", "host", "rtsp_port", "onvif_port", "path", "username", "codec", "volume_note",
                                     "camera_ids", "check", "checked_at")},
            "enabled": bool(s.get("enabled")), "status": talkdown.manager.status(s["id"])}


def _speaker(speaker_id: str) -> dict:
    s = db.speaker(speaker_id)
    if not s:
        raise HTTPException(404, "unknown speaker")
    return s


def check_cameras(ids: list[str]) -> None:
    known = {c["id"] for c in db.cameras()}
    missing = [c for c in ids if c not in known]
    if missing:
        raise HTTPException(422, f"no camera {', '.join(missing)} on this server")


# ---------------------------------------------------------------- speakers

class SpeakerIn(BaseModel):
    id: str = Field(pattern=ID_RX)
    name: str = Field(min_length=1, max_length=80)
    host: str = Field(pattern=HOST_RX, description="speaker IP address or hostname")
    rtsp_port: int = Field(554, ge=1, le=65535)
    onvif_port: int = Field(80, ge=1, le=65535)
    path: str = Field("/main", pattern=PATH_RX)
    username: str = Field("admin", max_length=64)
    password: str | None = Field(None, max_length=128)   # omitted on update = keep the stored one
    codec: Literal["PCMU", "PCMA"] = "PCMU"
    volume_note: str = Field("", max_length=200)
    camera_ids: list[str] = Field(default_factory=list, max_length=64)   # cameras on this server whose live view talks through it
    enabled: bool = True

    @model_validator(mode="after")
    def _ids(self):
        if any(not re.match(ID_RX, c) for c in self.camera_ids):
            raise ValueError("camera_ids must be camera ids")
        self.camera_ids = list(dict.fromkeys(self.camera_ids))
        return self


@router.get("/api/speakers")
async def list_speakers():
    return [public_speaker(s) for s in db.speakers()]


@router.put("/api/speakers/{speaker_id}")
async def put_speaker(speaker_id: str, body: SpeakerIn, request: Request):
    require(request, "admin")
    if body.id != speaker_id:
        raise HTTPException(400, "id mismatch")
    check_cameras(body.camera_ids)
    data = body.model_dump()
    existing = db.speaker(speaker_id)
    if data["password"] is None:
        data["password"] = existing["password"] if existing else ""
    db.upsert_speaker(data)
    if existing and any(existing.get(k) != data.get(k) for k in ("host", "rtsp_port", "path", "username", "password")):
        db.execute("UPDATE speakers SET check_json=NULL, checked_at=NULL WHERE id=?", [speaker_id])   # another speaker now
    return public_speaker(_speaker(speaker_id))


@router.delete("/api/speakers/{speaker_id}")
async def delete_speaker(speaker_id: str, request: Request):
    require(request, "admin")
    _speaker(speaker_id)
    talkdown.manager.stop(speaker_id)
    db.execute("DELETE FROM speakers WHERE id=?", [speaker_id])
    return {"ok": True}


@router.post("/api/speakers/{speaker_id}/check")
async def check_speaker(speaker_id: str, request: Request):
    """DESCRIBE with the backchannel Require header: does it answer, does it offer a sendonly audio track, which codecs."""
    require(request, "admin")
    s = _speaker(speaker_id)
    if talkdown.manager.busy(speaker_id):
        raise HTTPException(409, "the speaker is playing a message; check it when it is done")
    result = await asyncio.to_thread(talkdown.check, s)
    db.set_speaker_check(speaker_id, result)
    return {**result, "checked_at": time.time()}


# ---------------------------------------------------------------- play / stop / status

class UploadIn(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    data_b64: str = Field(min_length=1, max_length=(talkdown.MAX_UPLOAD * 4) // 3 + 8)


class PlayIn(BaseModel):
    """Exactly one of: a library message, a text to speak, a one-off audio file."""
    message_id: int | None = None
    text: str | None = Field(None, max_length=talkdown.MAX_TEXT)
    upload: UploadIn | None = None

    @model_validator(mode="after")
    def _one(self):
        if sum(x is not None and x != "" for x in (self.message_id, self.text, self.upload)) != 1:
            raise ValueError("give exactly one of message_id, text or upload")
        return self


def _decode(u: UploadIn) -> bytes:
    try:
        data = base64.b64decode(u.data_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "data_b64 is not base64")
    try:
        talkdown.check_upload(u.filename, data)
    except talkdown.TalkdownError as e:
        raise HTTPException(422, str(e))
    return data


async def _tts_ready() -> None:
    if not await asyncio.to_thread(talkdown.tts_engine):
        raise HTTPException(409, "this server has no text-to-speech engine")


@router.post("/api/speakers/{speaker_id}/play")
async def play(speaker_id: str, body: PlayIn, request: Request):
    """Play on this speaker; one playback per speaker, a new one replaces the one playing (`replaced` names it)."""
    by = require(request, "operator")
    s = _speaker(speaker_id)
    if not s["enabled"]:
        raise HTTPException(409, "the speaker is turned off in its settings")
    if body.message_id is not None:
        m = talkdown.message(body.message_id)
        if not m:
            raise HTTPException(404, "unknown message")
        if m.get("file"):
            msg = talkdown.Message(m["name"], path=talkdown.talkdown_dir() / m["file"], message_id=m["id"])
        else:
            await _tts_ready()
            msg = talkdown.Message(m["name"], text=m["text"], message_id=m["id"], cache=True)
    elif body.text:
        text = talkdown.clean_text(body.text)
        if not text:
            raise HTTPException(422, "nothing to say")
        await _tts_ready()
        msg = talkdown.Message(f'"{text[:60]}{"…" if len(text) > 60 else ""}"', text=text)
    else:
        data = _decode(body.upload)
        d = talkdown.talkdown_dir() / "oneoff"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"up-{uuid.uuid4().hex[:12]}{Path(body.upload.filename).suffix.lower()}"
        path.write_bytes(data)
        msg = talkdown.Message(Path(body.upload.filename).name[:80], path=path, cleanup=True)
    return talkdown.manager.play(s, msg, by)


@router.post("/api/speakers/{speaker_id}/stop")
async def stop(speaker_id: str, request: Request):
    by = require(request, "operator")
    _speaker(speaker_id)
    stopped = talkdown.manager.stop(speaker_id)
    if stopped:
        talkdown.log.info("talk-down on %s stopped by %s", speaker_id, by)
    return {"stopped": stopped, "status": talkdown.manager.status(speaker_id)}


class DescribeIn(BaseModel):
    camera_id: str = Field(pattern=ID_RX)
    template: Literal["leave", "police", "monitored", "custom"] = "leave"   # talkdown_describe.TEMPLATES


@router.post("/api/speakers/{speaker_id}/describe")
async def describe(speaker_id: str, body: DescribeIn, request: Request):
    """Describe & warn: how to address the person on the camera's current picture, and the picked template as an editable
    message. Never plays anything: the operator plays the reviewed text through /play. The camera must be on this
    server; the speaker may be on another one (a hub camera group): its id keys the rate limit and the log."""
    by = require(request, "operator")
    if not re.match(ID_RX, speaker_id):
        raise HTTPException(404, "unknown speaker")
    check_cameras([body.camera_id])
    if talkdown_describe.rate_limited(speaker_id, body.camera_id):
        raise HTTPException(429, f"one description every {talkdown_describe.RATE_S:.0f} s: try again in a moment")
    t0 = time.time()
    out = await talkdown_describe.describe(body.camera_id, body.template)
    talkdown.log.info("talk-down describe of %s for %s by %s: %s people, %s in %.1f s%s", body.camera_id, speaker_id, by,
                      out["people"], out["model"] or "no model", time.time() - t0, f" ({out['error']})" if out["error"] else "")
    return out


@router.get("/api/speakers/{speaker_id}/status")
async def status(speaker_id: str):
    _speaker(speaker_id)
    return talkdown.manager.status(speaker_id)


# ---------------------------------------------------------------- the message library

class MessageIn(BaseModel):
    """A library message: an uploaded audio file, or a text the server's text-to-speech engine speaks."""
    name: str = Field(min_length=1, max_length=80)
    text: str | None = Field(None, max_length=talkdown.MAX_TEXT)
    upload: UploadIn | None = None

    @model_validator(mode="after")
    def _one(self):
        if (self.text is None) == (self.upload is None):
            raise ValueError("give either text or upload")
        return self


class RenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@router.get("/api/talkdown/messages")
async def list_messages():
    """The library, whether this server can speak text (`tts`), and the limits."""
    engine = await asyncio.to_thread(talkdown.tts_engine)
    return {"messages": [talkdown.public_message(m, engine) for m in talkdown.messages()],
            "tts": {"available": engine is not None, "engine": engine}, "max_s": talkdown.MAX_S,
            "max_upload": talkdown.MAX_UPLOAD, "types": list(talkdown.AUDIO_EXT)}


@router.post("/api/talkdown/messages")
async def add_message(body: MessageIn, request: Request):
    require(request, "admin")
    try:
        if body.upload is not None:
            m = await asyncio.to_thread(talkdown.add_file_message, body.name.strip(), body.upload.filename, _decode(body.upload))
        else:
            m = talkdown.add_text_message(body.name.strip(), body.text or "")
    except talkdown.TalkdownError as e:
        raise HTTPException(422, str(e))
    return talkdown.public_message(m, await asyncio.to_thread(talkdown.tts_engine))


@router.put("/api/talkdown/messages/{message_id}")
async def rename_message(message_id: int, body: RenameIn, request: Request):
    require(request, "admin")
    if not talkdown.message(message_id):
        raise HTTPException(404, "unknown message")
    db.execute("UPDATE talkdown_messages SET name=? WHERE id=?", [body.name.strip(), message_id])
    return talkdown.public_message(talkdown.message(message_id), await asyncio.to_thread(talkdown.tts_engine))


@router.delete("/api/talkdown/messages/{message_id}")
async def delete_message(message_id: int, request: Request):
    require(request, "admin")
    if not talkdown.delete_message(message_id):
        raise HTTPException(404, "unknown message")
    return {"ok": True}


@router.get("/api/talkdown/log")
async def play_log(limit: int = Query(50, ge=1, le=500), speaker: str | None = None):
    """The latest plays: who, which message, which speaker, how it ended."""
    sql = "SELECT * FROM talkdown_log" + (" WHERE speaker_id=?" if speaker else "") + " ORDER BY id DESC LIMIT ?"
    return db.all(sql, [speaker, limit] if speaker else [limit])
