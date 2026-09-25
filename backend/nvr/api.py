"""FastAPI app: REST + WebSocket for the web UI, and process lifecycle."""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import mimetypes
import re
import shutil
import time
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import assistant, backup, baseline, footage, frames, health, identities, journeys, keep, mediamtx, retention, zones
from . import synopsis as vlm
from . import vlmroute
from .config import ROOT, settings
from .db import db
from .ingest import CameraIngest
from .pipeline import Pipeline
from .seed import seed_cameras_from_env

log = logging.getLogger("nvr.api")


class State:
    pipeline: Pipeline
    mtx: mediamtx.MediaMTX
    ollama: vlm.OllamaServer
    ingests: dict[str, CameraIngest] = {}
    tasks: list[asyncio.Task] = []
    footage: footage.Indexer
    health: health.StreamHealth


state = State()


def sync_cameras() -> None:
    """Apply the camera table to MediaMTX config and the ingest threads."""
    cams = db.cameras(enabled_only=True)
    state.mtx.write_config(cams)
    loop = asyncio.get_running_loop()
    wanted = {c["id"]: c for c in cams}
    for cid in list(state.ingests):
        if cid not in wanted:
            state.ingests.pop(cid).stop()
    for cid, cam in wanted.items():
        state.pipeline.tracker.set_zones(cid, cam["zones"])
        state.pipeline.cameras[cid] = cam
        if cid not in state.ingests:
            ing = CameraIngest(cam, loop, state.pipeline.frames, state.pipeline.rule_events)
            ing.start()
            state.ingests[cid] = ing


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    for d in (settings.data_dir, settings.runtime_dir, settings.recordings_dir):
        d.mkdir(parents=True, exist_ok=True)
    seed_cameras_from_env()
    state.pipeline = Pipeline()
    state.mtx = mediamtx.MediaMTX()
    state.ollama = vlm.OllamaServer()
    state.mtx.write_config(db.cameras(enabled_only=True))
    p = state.pipeline
    state.footage = footage.Indexer(p)
    state.health = health.StreamHealth()
    assistant.ctx.pipeline, assistant.ctx.footage = p, state.footage
    state.tasks = [asyncio.create_task(coro, name=name) for name, coro in [
        ("mediamtx", state.mtx.run()),
        ("ollama", state.ollama.run()),
        ("ingest", p.ingest_loop()),
        ("verify", p.verify_loop()),
        ("synopsis", p.synopsis_loop()),
        ("journeys", p.journey_loop()),
        ("journey-narratives", journeys.narrative_loop(p)),
        ("vlm-start", p.start_vlm()),
        ("retention", retention.retention_loop()),
        ("baseline", baseline.baseline_loop(p)),
        ("footage-index", state.footage.run()),
        ("briefings", assistant.briefing_loop(p)),
        ("backup", backup.backup_loop()),
        ("stream-health", state.health.run()),
    ]]
    await asyncio.sleep(1.5)  # let MediaMTX bind before readers connect
    sync_cameras()
    p.recover()
    log.info("NVR up: http://localhost:%s", settings.port)
    yield
    for ing in state.ingests.values():
        ing.stop()
    await state.mtx.stop()
    await state.ollama.stop()
    for t in state.tasks:
        t.cancel()


app = FastAPI(title="NewVMS", lifespan=lifespan)

PUBLIC_CAMERA_FIELDS = ("id", "name", "host", "onvif_port", "rtsp_port", "username", "main_path",
                        "sub_path", "enabled", "zones", "retention_days", "scene_notes", "retention_policy",
                        "synopsis_labels")


def public_camera(c: dict) -> dict:
    return {k: c[k] for k in PUBLIC_CAMERA_FIELDS}


# ---------------------------------------------------------------- cameras

class CameraIn(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_]{1,32}$")
    name: str
    host: str
    onvif_port: int = 80
    rtsp_port: int = 554
    username: str = "admin"
    password: str | None = None  # omitted on update = keep existing
    main_path: str = "/main"
    sub_path: str = "/sub"
    enabled: bool = True
    zones: list[dict] = []
    retention_days: int | None = None
    scene_notes: str = ""
    retention_policy: dict | None = None  # partial override of the site retention policy; None = inherit
    synopsis_labels: list[Literal["person", "vehicle"]] | None = None  # what Qwen describes; None = site default


@app.get("/api/cameras")
async def list_cameras():
    status = {}
    with contextlib.suppress(httpx.HTTPError):
        status = await mediamtx.path_status()
    out = []
    for c in db.cameras():
        p = status.get(c["id"], {})
        ing = state.ingests.get(c["id"])
        out.append({**public_camera(c), "status": {
            "stream_ready": bool(p.get("ready")),
            "recording": bool(p.get("ready")) and c["enabled"],
            "tracks": [t if isinstance(t, str) else t.get("codec") for t in p.get("tracks", [])],
            **(ing.status() if ing else {}),
            "health": state.health.camera(c["id"]),
        }})
    return out


@app.put("/api/cameras/{camera_id}")
async def put_camera(camera_id: str, cam: CameraIn):
    if cam.id != camera_id:
        raise HTTPException(400, "id mismatch")
    data = cam.model_dump()
    data["enabled"] = int(data["enabled"])
    if data["password"] is None:
        existing = db.one("SELECT password FROM cameras WHERE id=?", [camera_id])
        data["password"] = existing["password"] if existing else ""
    db.upsert_camera(data)
    if camera_id in state.ingests:  # restart readers with new settings
        state.ingests.pop(camera_id).stop()
    sync_cameras()
    state.pipeline.queue_missing_synopses(camera_id)  # e.g. vehicles just switched on for Qwen
    return public_camera(next(c for c in db.cameras() if c["id"] == camera_id))


@app.delete("/api/cameras/{camera_id}")
async def delete_camera(camera_id: str):
    db.execute("UPDATE cameras SET enabled=0 WHERE id=?", [camera_id])
    sync_cameras()
    return {"ok": True}


# ---------------------------------------------------------------- zones / masks

class ZonesIn(BaseModel):
    zones: list[dict]


def _mask_changes(camera_id: str, zone_list: list[dict]) -> tuple[list[int], list[tuple[int, str]]]:
    """Past events to hide (whole path in masked areas) and previously hidden ones to restore."""
    zl = zones.normalize(zone_list)
    to_mask, to_restore = [], []
    for r in db.all("SELECT id, status, status_before_mask, path FROM events WHERE camera_id=? "
                    "AND status IN ('verified','rejected','error','masked')", [camera_id]):
        ok = zones.path_allowed(json.loads(r["path"] or "[]"), zl)
        if r["status"] == "masked" and ok:
            to_restore.append((r["id"], r["status_before_mask"] or "verified"))
        elif r["status"] != "masked" and not ok:
            to_mask.append(r["id"])
    return to_mask, to_restore


@app.get("/api/cameras/{camera_id}/detections")
async def camera_detections(camera_id: str, hours: float = Query(24, le=24 * 14), limit: int = Query(4000, le=20000)):
    """Recent foot points of tracked objects, for drawing masks: [[x, y, class, event_id, status], ...]."""
    rows = db.all("SELECT id, camera_class, status, path FROM events WHERE camera_id=? AND start_ts>=? "
                  "ORDER BY start_ts DESC LIMIT ?", [camera_id, time.time() - hours * 3600, limit])
    points = []
    for r in rows:
        path = json.loads(r["path"] or "[]")
        for p in path if len(path) <= 3 else [path[0], path[len(path) // 2], path[-1]]:  # start, middle, end
            x, y = zones.foot(p[1:5])
            points.append([round(x, 4), round(y, 4), r["camera_class"], r["id"], r["status"]])
    return points


@app.post("/api/cameras/{camera_id}/zones/preview")
async def preview_zones(camera_id: str, body: ZonesIn):
    to_mask, to_restore = await asyncio.to_thread(_mask_changes, camera_id, body.zones)
    return {"mask": len(to_mask), "restore": len(to_restore)}


@app.post("/api/cameras/{camera_id}/zones/apply")
async def apply_zones(camera_id: str):
    """Hide past events that are entirely in masked areas; restore ones that no longer are."""
    cam = next((c for c in db.cameras() if c["id"] == camera_id), None)
    if not cam:
        raise HTTPException(404)
    t0 = time.time()
    to_mask, to_restore = await asyncio.to_thread(_mask_changes, camera_id, cam["zones"])

    def write():
        # One transaction: the connection is autocommit, and per-row commits made this take seconds.
        with db.lock:
            db.conn.execute("BEGIN")
            try:
                # Status only: search already skips status='masked', so the index stays and restore is instant.
                db.conn.executemany("UPDATE events SET status_before_mask=status, status='masked' WHERE id=?",
                                    [(eid,) for eid in to_mask])
                db.conn.executemany("UPDATE events SET status=?, status_before_mask=NULL WHERE id=?",
                                    [(prev, eid) for eid, prev in to_restore])
                db.conn.execute("COMMIT")
            except Exception:
                db.conn.execute("ROLLBACK")
                raise

    await asyncio.to_thread(write)
    log.info("[%s] zones applied in %.1fs: %d masked, %d restored", camera_id, time.time() - t0, len(to_mask), len(to_restore))
    return {"masked": len(to_mask), "restored": len(to_restore)}


# ---------------------------------------------------------------- events

@app.get("/api/events")
async def list_events(camera: str | None = None, status: str | None = None, label: str | None = None,
                      threat: str | None = None, since: float | None = None, until: float | None = None,
                      before_id: int | None = None, min_yolo: float = Query(0, ge=0, le=1),
                      limit: int = Query(50, le=500)):
    where, params = [], []
    if min_yolo > 0:  # events still being tracked/verified have no YOLO score yet; keep them
        where.append("(yolo_conf >= ? OR status IN ('open','pending'))"); params.append(min_yolo)
    for col, val in (("camera_id", camera), ("camera_class", label), ("threat", threat)):
        if val:
            where.append(f"{col}=?"); params.append(val)
    if status:
        statuses = status.split(",")
        where.append(f"status IN ({','.join('?' * len(statuses))})"); params += statuses
    if since:
        where.append("start_ts>=?"); params.append(since)
    if until:
        where.append("start_ts<=?"); params.append(until)
    if before_id:
        where.append("id<?"); params.append(before_id)
    sql = ("SELECT id, camera_id, track_id, camera_class, camera_conf, start_ts, end_ts, status, yolo_class, "
           "yolo_conf, yolo_hits, snapshot, clip, synopsis, threat, priority, anomaly, anomaly_json, watched, error, corrected_at, feedback, "
           "EXISTS(SELECT 1 FROM locks WHERE locks.event_id = events.id) AS locked, journey_id, "
           "(SELECT COUNT(DISTINCT je.value) FROM journeys, json_each(journeys.cameras) je WHERE journeys.id = events.journey_id) AS journey_cameras FROM events"
           + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY id DESC LIMIT ?")
    rows = db.all(sql, [*params, limit])
    for r in rows:
        r["feedback"] = json.loads(r["feedback"]) if r["feedback"] else None
        r["anomaly_json"] = json.loads(r["anomaly_json"]) if r["anomaly_json"] else None
    return rows


@app.get("/api/events/{event_id}")
async def get_event(event_id: int):
    e = db.event(event_id)
    if not e:
        raise HTTPException(404)
    e["lock"] = db.one("SELECT * FROM locks WHERE event_id=?", [event_id])
    return e


def _event_file(event_id: int, name: str) -> Path:
    if not re.fullmatch(r"[a-z_0-9]+\.(jpg|mp4)", name):
        raise HTTPException(400)
    f = settings.data_dir / "events" / str(event_id) / name
    if not f.exists():
        raise HTTPException(404)
    return f


@app.get("/api/events/{event_id}/media/{name}")
async def event_media(event_id: int, name: str):
    return FileResponse(_event_file(event_id, name), headers={"Cache-Control": "max-age=86400"})


@app.post("/api/events/{event_id}/reprocess")
async def reprocess(event_id: int):
    e = db.event(event_id)
    if not e:
        raise HTTPException(404)
    db.update_event(event_id, status="pending", synopsis=None, synopsis_json=None, synopsis_original=None,
                    corrected_at=None, threat=None, error=None)
    db.unindex_event(event_id)
    await state.pipeline.verify_q.put(event_id)
    return {"ok": True}


# ---------------------------------------------------------------- corrections & feedback

class ObjectIn(BaseModel):
    type: str
    description: str = ""


class SynopsisIn(BaseModel):
    summary: str
    activity: str = ""
    objects: list[ObjectIn] = []
    threat_level: Literal["none", "low", "medium", "high"] = "none"
    threat_reason: str = ""
    tags: list[str] = []


class FeedbackIn(BaseModel):
    rating: Literal["up", "down"] | None = None
    reasons: list[str] = []
    verdict: Literal["correct", "false_alarm", "wrong_class"] | None = None
    correct_class: str | None = None
    note: str | None = None


def _require_event(event_id: int) -> dict:
    e = db.event(event_id)
    if not e:
        raise HTTPException(404)
    return e


@app.put("/api/events/{event_id}/synopsis")
async def correct_synopsis(event_id: int, body: SynopsisIn):
    """Operator correction. The first model version is kept in synopsis_original."""
    e = _require_event(event_id)
    fields = {"synopsis": body.summary.strip(), "synopsis_json": body.model_dump(), "threat": body.threat_level,
              "corrected_at": time.time()}
    if e.get("synopsis_json") and not e.get("synopsis_original"):
        fields["synopsis_original"] = e["synopsis_json"]
    db.update_event(event_id, **fields)
    baseline.apply(event_id, rescore=False)
    await state.pipeline.reindex(event_id)
    state.pipeline.publish(event_id)
    return db.event(event_id)


@app.delete("/api/events/{event_id}/synopsis/correction")
async def revert_synopsis(event_id: int):
    e = _require_event(event_id)
    orig = e.get("synopsis_original")
    if not orig:
        raise HTTPException(400, "event has no correction")
    db.update_event(event_id, synopsis=orig.get("summary"), synopsis_json=orig, threat=orig.get("threat_level"),
                    synopsis_original=None, corrected_at=None)
    baseline.apply(event_id, rescore=False)
    await state.pipeline.reindex(event_id)
    state.pipeline.publish(event_id)
    return db.event(event_id)


@app.post("/api/events/{event_id}/synopsis/generate")
async def generate_synopsis(event_id: int):
    """(Re)generate the Qwen synopsis for any event, including labels skipped by default."""
    e = _require_event(event_id)
    if not (e.get("detections") or {}).get("keyframes"):
        raise HTTPException(400, "no keyframes for this event; reprocess it first")
    db.update_event(event_id, synopsis=None, synopsis_json=None, synopsis_original=None, corrected_at=None,
                    threat=None, error=None)
    await state.pipeline.synopsis_q.put(event_id)
    state.pipeline.publish(event_id)
    return {"ok": True}


@app.put("/api/events/{event_id}/feedback")
async def put_feedback(event_id: int, body: FeedbackIn):
    e = _require_event(event_id)
    fb = {**(e.get("feedback") or {}), **body.model_dump(exclude_unset=True), "at": time.time()}
    db.update_event(event_id, feedback=fb)
    baseline.apply(event_id, rescore=False)
    await state.pipeline.reindex(event_id)
    state.pipeline.publish(event_id)
    return db.event(event_id)


@app.get("/api/feedback/stats")
async def feedback_stats():
    rows = db.all("SELECT camera_class, status, synopsis, corrected_at, feedback FROM events")
    out = {"synopses": 0, "corrected": 0, "up": 0, "down": 0, "reasons": {}, "verdicts": {}}
    for r in rows:
        fb = json.loads(r["feedback"]) if r["feedback"] else {}
        out["synopses"] += bool(r["synopsis"])
        out["corrected"] += bool(r["corrected_at"])
        if fb.get("rating") in ("up", "down"):
            out[fb["rating"]] += 1
            for reason in fb.get("reasons") or [] if fb["rating"] == "down" else []:
                out["reasons"][reason] = out["reasons"].get(reason, 0) + 1
        if fb.get("verdict"):
            key = f"{r['camera_class']}:{r['status']}"
            out["verdicts"].setdefault(key, {}).setdefault(fb["verdict"], 0)
            out["verdicts"][key][fb["verdict"]] += 1
    return out


@app.get("/api/feedback/export")
async def feedback_export():
    """JSONL of every event with operator feedback or a correction: training / tuning data."""
    rows = db.all("SELECT * FROM events WHERE feedback IS NOT NULL OR corrected_at IS NOT NULL ORDER BY id")
    lines = []
    for r in rows:
        e = db.event(r["id"])
        lines.append(json.dumps({
            "id": e["id"], "camera_id": e["camera_id"], "start_ts": e["start_ts"], "end_ts": e["end_ts"],
            "camera_class": e["camera_class"], "camera_conf": e["camera_conf"], "status": e["status"],
            "yolo_class": e["yolo_class"], "yolo_conf": e["yolo_conf"], "yolo_hits": e["yolo_hits"],
            "feedback": e.get("feedback"), "synopsis_model": e.get("synopsis_original") or e.get("synopsis_json"),
            "synopsis_corrected": e.get("synopsis_json") if e.get("corrected_at") else None,
            "path": e.get("path"), "samples": (e.get("detections") or {}).get("samples"),
            "media": {"snapshot": e.get("snapshot"), "clip": e.get("clip"),
                      "keyframes": [k["file"] for k in (e.get("detections") or {}).get("keyframes", [])]},
        }))
    return Response("\n".join(lines) + "\n", media_type="application/x-ndjson",
                    headers={"Content-Disposition": "attachment; filename=newvms-feedback.jsonl"})


# ---------------------------------------------------------------- clip chat

class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    at: float | None = None  # clip time in seconds to focus on


@app.get("/api/events/{event_id}/chat")
async def get_chat(event_id: int):
    _require_event(event_id)
    return db.chat(event_id)


@app.delete("/api/events/{event_id}/chat")
async def clear_chat(event_id: int):
    db.execute("DELETE FROM chat_messages WHERE event_id=? AND saved=0", [event_id])
    return db.chat(event_id)


@app.post("/api/events/{event_id}/chat")
async def post_chat(event_id: int, body: ChatIn):
    """Ask Qwen about the clip. Streams NDJSON: {type:user|frames|delta|done|error}."""
    e = _require_event(event_id)
    if not state.pipeline.vlm_ready:
        raise HTTPException(503, "Qwen is not ready yet")
    p = state.pipeline
    camera = p.cameras.get(e["camera_id"], {"name": e["camera_id"]})
    history = [{"role": m["role"], "content": m["content"]} for m in db.chat(event_id)]
    user_id = db.add_chat(event_id, "user", body.message, at=body.at)
    if INSTRUCTION_RE.search(body.message.lower()):
        # The chat can't act. Say so from code (the model tends to agree to anything) and point at the real feature.
        canned = ("I can't watch for anyone or send alerts from this chat; I only see this clip. To be told about this "
                  + ("person" if e["camera_class"] == "person" else "vehicle")
                  + " in future, use **Watch this person** at the top of this event: sightings that match their appearance "
                  "are then raised to medium priority and shown under Needs attention on Home.")
        msg_id = db.add_chat(event_id, "assistant", canned, frames=[], at=body.at)

        async def canned_stream():
            yield json.dumps({"type": "user", "id": user_id}) + "\n"
            yield json.dumps({"type": "delta", "text": canned}) + "\n"
            yield json.dumps({"type": "done", "id": msg_id}) + "\n"
        return StreamingResponse(canned_stream(), media_type="application/x-ndjson")

    async def stream():
        yield json.dumps({"type": "user", "id": user_id}) + "\n"
        answer = ""
        frames_meta: list[dict] = []
        try:
            frames = await p.chat_frames(e, body.at)
            if not frames:
                raise RuntimeError("no recording available for this event")
            frames_meta = [{"file": name, "t": t} for t, _, name in frames]
            yield json.dumps({"type": "frames", "frames": frames_meta}) + "\n"
            async with p.gate.chat():
                async for chunk in vlm.chat_stream(e, camera, [(t, b) for t, b, _ in frames], history, body.message):
                    answer += chunk
                    yield json.dumps({"type": "delta", "text": chunk}) + "\n"
            msg_id = db.add_chat(event_id, "assistant", answer.strip(), frames=frames_meta, at=body.at)
            yield json.dumps({"type": "done", "id": msg_id}) + "\n"
        except Exception as ex:  # surface errors to the UI instead of a broken stream
            log.exception("chat on event %s failed", event_id)
            if answer:
                db.add_chat(event_id, "assistant", answer.strip() + " [interrupted]", frames=frames_meta, at=body.at)
            yield json.dumps({"type": "error", "error": str(ex)}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.put("/api/events/{event_id}/chat/{msg_id}/saved")
async def save_chat_note(event_id: int, msg_id: int, saved: bool = True):
    db.execute("UPDATE chat_messages SET saved=? WHERE id=? AND event_id=? AND role='assistant'",
               [int(saved), msg_id, event_id])
    await state.pipeline.reindex(event_id)
    return db.chat(event_id)


INSTRUCTION_RE = re.compile(r"\b(keep an eye|watch (out )?for|look out for|alert me|notify me|let me know if|tell me (if|when)|"
                            r"flag (him|her|them|this|that)|track (him|her|them)|remind me|add (him|her|them) to)\b")
query_label = assistant.query_label


@app.get("/api/search")
async def search(q: str, camera: str | None = None, since: float | None = None, until: float | None = None,
                 label: str | None = None, min_yolo: float = Query(0, ge=0, le=1),
                 limit: int = Query(30, le=200)):
    emb = await vlm.embed(f"search_query: {q}") if state.pipeline.vlm_ready else None
    return db.search(q, emb, limit, camera, since, until, label or query_label(q), min_yolo)


# ---------------------------------------------------------------- recordings

# ---------------------------------------------------------------- learned baseline (what's normal)

@app.get("/api/baseline")
async def baseline_status():
    return baseline.status()


@app.post("/api/baseline/rebuild")
async def baseline_rebuild():
    await asyncio.to_thread(baseline.rebuild)
    n = await asyncio.to_thread(baseline.backfill, False)
    return {"ok": True, "scored": n, "cameras": baseline.status()}


# ---------------------------------------------------------------- cross-camera journeys

class CameraLinkIn(BaseModel):
    cam_a: str
    cam_b: str
    min_s: float = Field(ge=-600, le=3600)
    max_s: float = Field(ge=-600, le=3600)
    one_way: bool = False


@app.get("/api/topology")
async def get_topology():
    return journeys.topology()


@app.put("/api/topology")
async def put_topology(links: list[CameraLinkIn]):
    known = {c["id"] for c in db.cameras()}
    if any(l.cam_a not in known or l.cam_b not in known for l in links):
        raise HTTPException(400, "unknown camera")
    journeys.set_topology([l.model_dump() for l in links])
    queued = await state.pipeline.relink(7)  # re-check recent person events against the new neighbours
    return {"links": journeys.topology(), "relinking": queued}


@app.get("/api/topology/suggestions")
async def topology_suggestions(days: float = Query(14, ge=1, le=90)):
    return await asyncio.to_thread(journeys.suggestions, days)


@app.get("/api/events/{event_id}/journey")
async def event_journey(event_id: int):
    return journeys.journey_for(event_id)


@app.post("/api/links/{link_id}/reject")
async def reject_link(link_id: int):
    affected = journeys.reject_link(link_id)
    for eid in affected:
        await state.pipeline.reindex(eid)
        state.pipeline.publish(eid)
    return {"ok": True, "events": affected}


@app.post("/api/journeys/{journey_id}/regenerate")
async def regenerate_journey(journey_id: int):
    db.execute("UPDATE journeys SET dirty=1, updated_at=0 WHERE id=?", [journey_id])
    return {"ok": True}


@app.post("/api/journeys/relink")
async def relink(days: float = Query(7, ge=0.1, le=30)):
    return {"queued": await state.pipeline.relink(days)}


# ---------------------------------------------------------------- timeline layouts

class LayoutIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    config: dict


def _layout(row: dict) -> dict:
    return {**row, "config": json.loads(row["config"])}


@app.get("/api/layouts")
async def list_layouts():
    return [_layout(r) for r in db.all("SELECT * FROM layouts ORDER BY name COLLATE NOCASE")]


@app.post("/api/layouts")
async def create_layout(body: LayoutIn):
    now = time.time()
    try:
        lid = db.execute("INSERT INTO layouts (name, config, created_at, updated_at) VALUES (?,?,?,?)",
                         [body.name.strip(), json.dumps(body.config), now, now]).lastrowid
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, "a layout with that name already exists")
        raise
    return _layout(db.one("SELECT * FROM layouts WHERE id=?", [lid]))


@app.put("/api/layouts/{layout_id}")
async def update_layout(layout_id: int, body: LayoutIn):
    if not db.one("SELECT 1 FROM layouts WHERE id=?", [layout_id]):
        raise HTTPException(404)
    try:
        db.execute("UPDATE layouts SET name=?, config=?, updated_at=? WHERE id=?",
                   [body.name.strip(), json.dumps(body.config), time.time(), layout_id])
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, "a layout with that name already exists")
        raise
    return _layout(db.one("SELECT * FROM layouts WHERE id=?", [layout_id]))


@app.delete("/api/layouts/{layout_id}")
async def delete_layout(layout_id: int):
    db.execute("DELETE FROM layouts WHERE id=?", [layout_id])
    return {"ok": True}


# ---------------------------------------------------------------- retention & locks

@app.get("/api/retention/policy")
async def get_retention_policy():
    return {"policy": keep.site_policy(), "defaults": keep.DEFAULT_POLICY}


@app.put("/api/retention/policy")
async def put_retention_policy(policy: dict):
    merged = keep._merge(keep.DEFAULT_POLICY, policy)
    if not 1 <= float(merged["continuous_days"]) <= 365:
        raise HTTPException(400, "continuous_days must be 1-365")
    db.set_setting("retention_policy", merged)
    return {"policy": keep.site_policy()}


@app.get("/api/retention/stats")
async def retention_stats():
    return await asyncio.to_thread(retention.stats)


@app.get("/api/retention/preview")
async def retention_preview(camera: str, hours: float = Query(24, ge=1, le=24 * 30)):
    return await asyncio.to_thread(retention.preview, camera, hours)


class LockIn(BaseModel):
    camera_id: str
    start_ts: float
    end_ts: float
    note: str = ""


@app.get("/api/locks")
async def list_locks(camera: str | None = None, start: float | None = None, end: float | None = None):
    where, params = ["1=1"], []
    if camera:
        where.append("camera_id=?"); params.append(camera)
    if start:
        where.append("end_ts>=?"); params.append(start)
    if end:
        where.append("start_ts<=?"); params.append(end)
    return db.all(f"SELECT * FROM locks WHERE {' AND '.join(where)} ORDER BY start_ts DESC", params)


@app.post("/api/locks")
async def create_lock(body: LockIn):
    if body.end_ts <= body.start_ts:
        raise HTTPException(400, "end must be after start")
    lock_id = db.execute("INSERT INTO locks (camera_id, start_ts, end_ts, note, created_at) VALUES (?,?,?,?,?)",
                         [body.camera_id, body.start_ts, body.end_ts, body.note, time.time()]).lastrowid
    return db.one("SELECT * FROM locks WHERE id=?", [lock_id])


@app.delete("/api/locks/{lock_id}")
async def delete_lock(lock_id: int):
    db.execute("DELETE FROM locks WHERE id=?", [lock_id])
    return {"ok": True}


class EventLockIn(BaseModel):
    note: str = ""


@app.post("/api/events/{event_id}/lock")
async def lock_event(event_id: int, body: EventLockIn):
    e = _require_event(event_id)
    cam = next((c for c in db.cameras() if c["id"] == e["camera_id"]), {"id": e["camera_id"]})
    policy = keep.policy_for(cam)
    if not db.one("SELECT 1 FROM locks WHERE event_id=?", [event_id]):
        db.execute("INSERT INTO locks (camera_id, start_ts, end_ts, event_id, note, created_at) VALUES (?,?,?,?,?,?)",
                   [e["camera_id"], e["start_ts"] - policy["pad_before_s"], (e["end_ts"] or e["start_ts"]) + policy["pad_after_s"],
                    event_id, body.note, time.time()])
    state.pipeline.publish(event_id)
    return db.one("SELECT * FROM locks WHERE event_id=?", [event_id])


@app.delete("/api/events/{event_id}/lock")
async def unlock_event(event_id: int):
    db.execute("DELETE FROM locks WHERE event_id=?", [event_id])
    state.pipeline.publish(event_id)
    return {"ok": True}


@app.get("/api/recordings/{camera_id}")
async def recordings(camera_id: str, start: float | None = None, end: float | None = None):
    spans = await mediamtx.recording_spans(camera_id, start, end)
    events = db.all("SELECT id, camera_class, yolo_class, yolo_conf, start_ts, end_ts, status, threat, priority, anomaly, journey_id, "
                    "json_extract(feedback, '$.verdict') AS verdict, synopsis IS NOT NULL AS has_synopsis FROM events "
                    "WHERE camera_id=? AND start_ts>=? AND start_ts<=? ORDER BY start_ts",
                    [camera_id, start or 0, end or time.time()])
    lo, hi = start or 0, end or time.time()
    kept = db.all("SELECT start_ts, end_ts, reasons, score FROM kept_footage WHERE camera_id=? AND end_ts>=? AND start_ts<=? "
                  "ORDER BY start_ts", [camera_id, lo, hi])
    for k in kept:
        k["reasons"] = json.loads(k["reasons"])
    locks = db.all("SELECT * FROM locks WHERE camera_id=? AND end_ts>=? AND start_ts<=? ORDER BY start_ts", [camera_id, lo, hi])
    return {"spans": spans, "events": events, "kept": kept, "locks": locks}


# ---------------------------------------------------------------- Ask the NVR + briefings

class AskIn(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    thread_id: int | None = None


@app.get("/api/assistant/threads")
async def assistant_threads(limit: int = Query(30, le=200)):
    return db.all("SELECT t.*, (SELECT COUNT(*) FROM assistant_messages m WHERE m.thread_id = t.id) AS messages "
                  "FROM assistant_threads t ORDER BY updated_at DESC LIMIT ?", [limit])


@app.get("/api/assistant/threads/{thread_id}")
async def assistant_thread(thread_id: int):
    t = assistant.thread(thread_id)
    if not t:
        raise HTTPException(404, "no such conversation")
    return t


@app.delete("/api/assistant/threads/{thread_id}")
async def delete_assistant_thread(thread_id: int):
    db.execute("DELETE FROM assistant_messages WHERE thread_id=?", [thread_id])
    db.execute("DELETE FROM assistant_threads WHERE id=?", [thread_id])
    return {"ok": True}


@app.post("/api/assistant/ask")
async def assistant_ask(body: AskIn):
    """Streams NDJSON: thread, user, calls (what was looked up + citation refs), model, delta..., done | error."""
    if not state.pipeline.vlm_ready and not vlmroute.router.configured:
        raise HTTPException(503, "Qwen is not ready yet")
    if body.thread_id and not db.one("SELECT 1 FROM assistant_threads WHERE id=?", [body.thread_id]):
        raise HTTPException(404, "no such conversation")

    async def stream():
        async for chunk in assistant.ask(body.thread_id, body.message.strip()):
            yield json.dumps(chunk) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


class BriefingSettingsIn(BaseModel):
    enabled: bool = True
    time: str = Field("07:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


@app.get("/api/briefings")
async def list_briefings(limit: int = Query(10, le=60)):
    return {"briefings": assistant.briefings(limit), "settings": assistant.briefing_settings()}


@app.post("/api/briefings/generate")
async def generate_briefing():
    if not state.pipeline.vlm_ready and not vlmroute.router.configured:
        raise HTTPException(503, "Qwen is not ready yet")
    return await assistant.generate_briefing()


@app.put("/api/briefings/settings")
async def put_briefing_settings(body: BriefingSettingsIn):
    db.set_setting("briefing", body.model_dump())
    return assistant.briefing_settings()


# ---------------------------------------------------------------- remote Qwen (optional, e.g. RunPod Serverless)

class RemoteTasksIn(BaseModel):
    tasks: list[str]


@app.get("/api/remote")
async def remote_status():
    return vlmroute.router.status()


@app.put("/api/remote/tasks")
async def remote_tasks(body: RemoteTasksIn):
    vlmroute.router.set_tasks(body.tasks)
    return vlmroute.router.status()


@app.post("/api/remote/test")
async def remote_test():
    return {**await vlmroute.router.test(), "status": vlmroute.router.status()}


@app.post("/api/remote/warm")
async def remote_warm():
    """Start a cold remote worker loading (the Ask tab calls this when it opens)."""
    return await vlmroute.router.warm()


# ---------------------------------------------------------------- search all footage

@app.get("/api/footage/status")
async def footage_status():
    return await asyncio.to_thread(state.footage.status)


@app.get("/api/footage/search")
async def footage_search(q: str = Query(min_length=2, max_length=200), camera: str | None = None,
                         since: float | None = None, until: float | None = None, limit: int = Query(30, le=100)):
    vec = await state.footage.embed_text(q)
    cams = [camera] if camera else [c["id"] for c in db.cameras()]
    hits = await asyncio.to_thread(state.footage.index.search, vec, cams, since, until, 400)
    return footage.moments(hits, limit)


class FootageVerifyIn(BaseModel):
    camera_id: str = Field(pattern=r"^[a-z0-9_]{1,32}$")
    ts: float
    tile: int = Field(0, ge=0, lt=len(footage.TILES))
    q: str = Field(min_length=2, max_length=200)


@app.post("/api/footage/verify")
async def footage_verify(body: FootageVerifyIn):
    """Qwen double-checks one footage search result (the user is waiting, so it takes chat priority)."""
    img = await asyncio.get_running_loop().run_in_executor(state.pipeline.decode, footage.tile_jpeg, body.camera_id, body.ts, body.tile)
    if not img:
        raise HTTPException(404, "no recording at that time")
    if not state.pipeline.vlm_ready:
        raise HTTPException(503, "Qwen is still starting")
    r = await vlm.footage_match(img, body.q)
    return {"matches": bool(r.get("matches")), "confidence": r.get("confidence", "low"), "seen": r.get("seen", ""),
            "model": r.get("_model")}


@app.get("/api/frame/{camera_id}")
async def frame(camera_id: str, t: float, w: int = Query(960, ge=320, le=1280), exact: bool = False):
    """Single preview frame from the recordings, for live timeline scrubbing."""
    if not re.fullmatch(r"[a-z0-9_]{1,32}", camera_id):
        raise HTTPException(400)
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(state.pipeline.decode, frames.preview_jpeg, camera_id, t, w, exact)
    if not result:
        raise HTTPException(404, "no recording at that time")
    data, frame_ts, live = result
    return Response(data, media_type="image/jpeg", headers={
        "X-Frame-Time": f"{frame_ts:.3f}",
        "Cache-Control": "no-store" if live else "max-age=86400",
    })


@app.get("/api/playback/{camera_id}")
async def playback(camera_id: str, start: float, duration: float = Query(60, le=3600)):
    """Proxy MediaMTX playback so the browser stays same-origin."""
    params = {"path": camera_id, "start": mediamtx.rfc3339(start), "duration": str(duration), "format": "mp4"}
    client = httpx.AsyncClient(timeout=None)
    req = client.build_request("GET", f"{settings.mediamtx_playback}/get", params=params)
    r = await client.send(req, stream=True)
    if r.status_code != 200:
        await r.aclose(); await client.aclose()
        raise HTTPException(r.status_code, "no recording for that range")

    async def body():
        try:
            async for chunk in r.aiter_bytes():
                yield chunk
        except httpx.HTTPError as e:  # MediaMTX went away mid-stream (restart); end the response quietly
            log.info("playback %s ended early: %s", camera_id, e)
        finally:
            await r.aclose(); await client.aclose()

    return StreamingResponse(body(), media_type="video/mp4")


# ---------------------------------------------------------------- system

@app.get("/api/system")
async def system():
    rec = shutil.disk_usage(settings.recordings_dir)
    counts = {r["status"]: r["n"] for r in db.all("SELECT status, COUNT(*) n FROM events GROUP BY status")}
    return {
        "recordings_disk": {"total_gb": round(rec.total / 1e9), "free_gb": round(rec.free / 1e9)},
        "retention_days": keep.site_policy()["continuous_days"],
        "retention_alert": retention.alert,
        "queues": {"verify": state.pipeline.verify_q.qsize(), "synopsis": state.pipeline.synopsis_q.qsize()},
        "vlm_ready": state.pipeline.vlm_ready, "vlm_model": settings.vlm_model,
        "yolo_ready": state.pipeline.verifier is not None, "yolo_model": settings.yolo_model,
        "events": counts,
        "webrtc_port": settings.mediamtx_webrtc_port,
        "backup": backup.status(),
    }


@app.post("/api/backup")
async def backup_now():
    return await asyncio.to_thread(backup.run)


# ---------------------------------------------------------------- home

@app.get("/api/home")
async def home(since: float | None = None):
    """Everything the Home tab shows in one call: what needs attention since `since`, today's activity,
    camera health, disk, the latest briefing."""
    now = time.time()
    since = since or now - 86400
    day_start = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    cols = ("id, camera_id, camera_class, start_ts, end_ts, status, yolo_class, yolo_conf, snapshot, synopsis, threat, "
            "priority, anomaly, anomaly_json, watched, feedback, corrected_at, journey_id, error")
    ok = "status='verified' AND (feedback IS NULL OR json_extract(feedback, '$.verdict') IS NOT 'false_alarm')"
    attention = db.all(f"SELECT {cols} FROM events WHERE {ok} AND start_ts >= ? AND (priority IN ('low','medium','high') "
                       f"OR COALESCE(anomaly, 0) >= ?) ORDER BY CASE priority WHEN 'high' THEN 3 WHEN 'medium' THEN 2 "
                       f"WHEN 'low' THEN 1 ELSE 0 END DESC, COALESCE(anomaly, 0) DESC, start_ts DESC LIMIT 20",
                       [since, baseline.PRIORITY_LOW])
    recent = db.all(f"SELECT {cols} FROM events WHERE {ok} ORDER BY start_ts DESC LIMIT 8")
    for e in attention + recent:
        e["anomaly_json"] = json.loads(e["anomaly_json"]) if e["anomaly_json"] else None
        e["feedback"] = json.loads(e["feedback"]) if e["feedback"] else None
    today = {}
    for r in db.all(f"SELECT camera_id, camera_class, COUNT(*) n FROM events WHERE {ok} AND start_ts >= ? GROUP BY 1, 2", [day_start]):
        today.setdefault(r["camera_id"], {})[r["camera_class"]] = r["n"]
    new_since = db.one(f"SELECT COUNT(*) n FROM events WHERE {ok} AND start_ts >= ?", [since])["n"]
    status = {}
    with contextlib.suppress(httpx.HTTPError):
        status = await mediamtx.path_status()
    cams = []
    for c in db.cameras(enabled_only=True):
        ing = state.ingests.get(c["id"])
        st = ing.status() if ing else {}
        cams.append({"id": c["id"], "name": c["name"], "stream_ready": bool(status.get(c["id"], {}).get("ready")),
                     "metadata": bool(st.get("metadata")), "metadata_last": st.get("metadata_last") or None,
                     "onvif_events": bool(st.get("onvif_events")), "today": today.get(c["id"], {}),
                     "health": state.health.camera(c["id"])})
    b = db.one("SELECT id, headline, period_start, period_end, created_at, model FROM briefings ORDER BY created_at DESC LIMIT 1")
    rec = shutil.disk_usage(settings.recordings_dir)
    p = state.pipeline
    return {"now": now, "since": since, "new_since": new_since, "attention": attention, "recent": recent, "cameras": cams,
            "briefing": b, "disk": {"free_gb": round(rec.free / 1e9), "total_gb": round(rec.total / 1e9)},
            "retention_alert": retention.alert, "yolo_ready": p.verifier is not None, "vlm_ready": p.vlm_ready,
            "queues": {"verify": p.verify_q.qsize(), "synopsis": p.synopsis_q.qsize()}, "backup": backup.status()["last"],
            "baseline": baseline.status()}


# ---------------------------------------------------------------- people & vehicles

class IdentityIn(BaseModel):
    kind: Literal["person", "vehicle"]
    name: str = Field(min_length=1, max_length=60)
    event_ids: list[int] = Field(min_length=1, max_length=500)
    notes: str = Field("", max_length=300)
    watch: bool = False
    watch_note: str = Field("", max_length=300)


class IdentityUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60)
    notes: str | None = Field(None, max_length=300)
    watch: bool | None = None
    watch_note: str | None = Field(None, max_length=300)


@app.get("/api/events/{event_id}/identity")
async def event_identity(event_id: int):
    """The named identity this sighting matches, if any (for the Watch button)."""
    e = _require_event(event_id)
    if e["camera_class"] not in identities.VEC_TABLE:
        return None
    m = identities.match_identity(e["camera_class"], event_id)
    return {**{k: v for k, v in m[0].items() if k != "vec"}, "sim": round(m[1], 3)} if m else None


async def _republish(event_ids: list[int]) -> None:
    for eid in event_ids:
        baseline.apply(eid, rescore=False)  # priority now includes the watch flag
        state.pipeline.publish(eid)


@app.get("/api/identities")
async def list_identities(kind: Literal["person", "vehicle"] = "person", since: float | None = None,
                          until: float | None = None, camera: str | None = None):
    return await asyncio.to_thread(identities.clusters, kind, since or time.time() - 86400, until, camera)


@app.post("/api/identities")
async def create_identity(body: IdentityIn):
    try:
        ident = await asyncio.to_thread(identities.name_cluster, body.kind, body.name, body.event_ids, body.notes)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if body.watch:
        ident, changed = await asyncio.to_thread(identities.set_watch, ident["id"], True, body.watch_note)
        await _republish(changed)
    return ident


@app.put("/api/identities/{iid}")
async def put_identity(iid: int, body: IdentityUpdate):
    r = identities.update_identity(iid, body.name, body.notes)
    if not r:
        raise HTTPException(404)
    if body.watch is not None:
        r, changed = await asyncio.to_thread(identities.set_watch, iid, body.watch, body.watch_note)
        await _republish(changed)
    return r


@app.delete("/api/identities/{iid}")
async def remove_identity(iid: int):
    identities.delete_identity(iid)
    return {"ok": True}


@app.websocket("/api/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    q: asyncio.Queue = asyncio.Queue()
    state.pipeline.subscribers.add(q)
    try:
        while True:
            await sock.send_json(await q.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        state.pipeline.subscribers.discard(q)


# ---------------------------------------------------------------- live view signalling

@app.post("/api/whep/{path}")
async def whep(path: str, request: Request):
    """Forward a WebRTC (WHEP) offer to MediaMTX so the browser only talks to this origin. That keeps live view
    working over HTTPS (installed app on a phone): no mixed-content request to MediaMTX's own port. The video
    itself still flows straight from MediaMTX over WebRTC."""
    if not re.fullmatch(r"[a-z0-9_]{1,40}", path):
        raise HTTPException(400)
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"http://127.0.0.1:{settings.mediamtx_webrtc_port}/{path}/whep", content=await request.body(),
                         headers={"Content-Type": "application/sdp"})
    body = r.content
    if r.status_code in (200, 201):
        hosts = [*settings.webrtc_public_hosts, request.headers.get("host", "")]
        ips = await asyncio.to_thread(public_ips, hosts)
        if ips:
            body = add_candidates(body.decode(), ips, settings.webrtc_media_port).encode()
    return Response(body, status_code=r.status_code, media_type=r.headers.get("content-type", "application/sdp"))


def public_ips(hosts: list[str]) -> list[str]:
    """Public IPv4s for the given hosts ("name:port", "1.2.3.4", "[::1]:8080"); private/loopback ones are skipped
    because MediaMTX already offers this machine's LAN addresses."""
    import ipaddress
    import socket
    out: list[str] = []
    for h in hosts:
        h = (h or "").strip()
        if not h:
            continue
        name = h[1:h.index("]")] if h.startswith("[") else h.rsplit(":", 1)[0] if h.count(":") == 1 else h
        try:
            addrs = [name] if _is_ip(name) else [a[4][0] for a in socket.getaddrinfo(name, None, socket.AF_INET)]
        except OSError:
            continue
        for a in addrs:
            ip = ipaddress.ip_address(a)
            if ip.version == 4 and ip.is_global and a not in out:
                out.append(a)
    return out


def _is_ip(s: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def add_candidates(sdp: str, ips: list[str], port: int) -> str:
    """Add UDP and TCP host candidates for public IPs to a WHEP answer, next to MediaMTX's own candidates (it
    lists them in the first, BUNDLE-owning media section), so a browser outside the LAN can reach MediaMTX
    through a forwarded port."""
    lines = sdp.replace("\r\n", "\n").split("\n")
    extra = []
    for n, ip in enumerate(ips):
        extra.append(f"a=candidate:nvrpub{n}u 1 udp 1694498815 {ip} {port} typ host")
        extra.append(f"a=candidate:nvrpub{n}t 1 tcp 1518280447 {ip} {port} typ host tcptype passive")
    # after the last candidate line of each media section that has any
    sections: list[list[str]] = [[]]
    for line in lines:
        if line.startswith("m="):
            sections.append([])
        sections[-1].append(line)
    out: list[str] = []
    for sec in sections:
        last = max((i for i, line in enumerate(sec) if line.startswith("a=candidate:")), default=None)
        out += sec if last is None else sec[:last + 1] + extra + sec[last + 1:]
    return "\r\n".join(out)


# ---------------------------------------------------------------- UI

mimetypes.add_type("application/manifest+json", ".webmanifest")
dist = ROOT / "frontend" / "dist"
if dist.exists():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        f = dist / full_path
        if full_path and f.is_file():
            # the service worker and manifest must be revalidated so app updates reach installed copies
            fresh = f.name in ("sw.js", "manifest.webmanifest")
            return FileResponse(f, headers={"Cache-Control": "no-cache"} if fresh else None)
        # index.html must always be revalidated so a rebuilt UI (new hashed assets) is picked up.
        return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})
