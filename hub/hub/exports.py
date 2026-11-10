"""Site exports across servers: one zip for cameras on several servers of a Site.

The Site Timeline exports cameras of one server straight from that server (its own /api/exports, through the tunnel).
For a selection across servers the browser asks the hub (POST /api/locations/{id}/exports), which:
  1. asks each server for its part: POST /api/exports with the range on that server's clock (sites.clock_skew_s), and
     `tiles` when a grid is wanted (640x360 copies, so the hub never decodes 4K)
  2. follows each server's job, then fetches its files through the tunnel (GET /api/exports/{id}/files/{name})
  3. builds the grid from the tiles (ffmpeg in the hub image), a report linking each server's report, checksums, a zip
Kept KEEP_S under settings.exports_dir; GET /api/exports/{id} and /download for the user who asked (or a Site admin).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import html
import json
import logging
import re
import secrets
import shutil
import subprocess
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import auth, db
from .agents import registry
from .config import settings
from .roles import allows

log = logging.getLogger("hub.exports")
router = APIRouter()

MAX_S, GRID_MAX_S, KEEP_S = 3600, 1800, 7 * 86400
GRID_W, GRID_H, GRID_FPS = 1920, 1080, 10
POLL_S = 2.0
FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf"]

_jobs: dict[str, "HubExport"] = {}
_tasks: set[asyncio.Task] = set()


@dataclass
class HubExport:
    id: str
    user_id: str
    by: str
    location_id: str
    site: str
    parts: list[dict]            # [{server, server_name, cameras: [id], role, offset, job?, state, error?}]
    start: float                 # the hub's clock (what the Timeline shows)
    end: float
    quality: str = "original"
    audio: bool = True
    grid: bool = False
    lock: bool = True
    note: str = ""
    created: float = field(default_factory=time.time)
    state: str = "queued"
    step: str = ""
    progress: float = 0.0
    files: list[dict] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)
    # each server's events in the range with Otso's descriptions (its job's `events`), on the hub's clock, by time
    events: list[dict] = field(default_factory=list)
    zip_name: str = ""
    zip_bytes: int = 0
    error: str | None = None
    finished: float | None = None

    def public(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k not in ("user_id",)}


def job_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9]{12}", job_id):
        raise HTTPException(404, "no such export")
    return Path(settings.exports_dir) / job_id


def _save(job: HubExport) -> None:
    d = job_dir(job.id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "job.json").write_text(json.dumps(asdict(job)), encoding="utf-8")


def _get(job_id: str) -> HubExport | None:
    if job_id in _jobs:
        return _jobs[job_id]
    f = job_dir(job_id) / "job.json"
    if f.is_file():
        try:
            j = HubExport(**json.loads(f.read_text(encoding="utf-8")))
        except (ValueError, TypeError):
            return None
        if j.state in ("queued", "running"):
            j.state, j.error = "failed", "the hub restarted during the export"
        _jobs[j.id] = j
        return j
    return None


def _cleanup() -> None:
    root = Path(settings.exports_dir)
    if not root.exists():
        return
    cutoff = time.time() - KEEP_S
    for d in root.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                _jobs.pop(d.name, None)
        except OSError:
            pass


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")[:60] or "server"


def stamp(t: float) -> str:
    return dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d_%H-%M-%S")


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- grid (as the servers' exports.grid_filter)

def grid_layout(n: int) -> tuple[int, int]:
    for cols, rows in ((1, 1), (2, 1), (2, 2), (3, 2), (3, 3), (4, 3), (4, 4), (5, 4), (6, 4)):
        if cols * rows >= n:
            return cols, rows
    raise ValueError("at most 24 cameras in a grid")


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\u2019").replace(":", "\\:").replace("%", "\\%")


def grid_args(ffmpeg: str, inputs: list[Path], labels: list[str], local_start: float, out: Path) -> list[str]:
    """Tiles side by side on a 1080p canvas, each named, the time along the bottom. `local_start`: the export's start as
    the Site's wall clock, in seconds (drawn with gmtime so the hub's own time zone doesn't matter)."""
    font = next((f for f in FONTS if Path(f).is_file()), None)
    n = len(inputs)
    cols, rows = grid_layout(n)
    w, h = GRID_W // cols // 2 * 2, GRID_H // rows // 2 * 2
    ff = f"fontfile='{_esc(font)}':" if font else ""
    parts = []
    for i, name in enumerate(labels):
        tile = f"[{i}:v]fps={GRID_FPS},scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
        if font:
            tile += f",drawtext={ff}text='{_esc(name)}':x=8:y=8:fontsize={max(14, h // 22)}:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=4"
        parts.append(tile + f"[t{i}]")
    last = "t0"
    if n > 1:
        layout = "|".join(f"{(i % cols) * w}_{(i // cols) * h}" for i in range(n))
        parts.append("".join(f"[t{i}]" for i in range(n)) + f"xstack=inputs={n}:layout={layout}:fill=black[g]")
        last = "g"
    if font:
        parts.append(f"[{last}]drawtext={ff}text='%{{pts\\:gmtime\\:{local_start:.3f}}}':x=(w-tw)/2:y=h-th-12:"
                     f"fontsize={GRID_H // 30}:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=6[out]")
        last = "out"
    args = [ffmpeg, "-v", "error", "-nostdin", "-y"]
    for p in inputs:
        args += ["-i", str(p)]
    return args + ["-filter_complex", ";".join(parts), "-map", f"[{last}]", "-an", "-pix_fmt", "yuv420p", "-c:v", "libx264",
                   "-preset", "veryfast", "-b:v", "6000k", "-shortest", "-movflags", "+faststart", str(out)]


# ---------------------------------------------------------------- the job

async def _call_json(conn, method: str, path: str, part: dict, by: str, body: dict | None = None, timeout: float = 30) -> dict:
    hdrs = {"x-hub-user": by, "x-hub-role": part["role"]}
    if body is not None:
        hdrs["content-type"] = "application/json"
    status, raw = await conn.call(method, path, "", hdrs, json.dumps(body).encode() if body is not None else None, timeout)
    if status != 200:
        raise RuntimeError(f"{part['server_name']}: HTTP {status} {raw[:200].decode(errors='replace')}")
    return json.loads(raw.decode() or "null")


async def _fetch(conn, path: str, dest: Path, part: dict, by: str) -> None:
    """A file from the server, streamed to disk through the tunnel."""
    s = await conn.request("GET", path, "", {"x-hub-user": by, "x-hub-role": part["role"]}, None)
    try:
        await asyncio.wait_for(s.head.wait(), 60)
        if s.aborted or s.status != 200:
            raise RuntimeError(f"{part['server_name']}: {path}: HTTP {s.status or s.aborted}")
        with open(dest, "wb") as f:
            while True:
                c = await asyncio.wait_for(s.read(), 120)
                if c is None:
                    break
                f.write(c)
                await conn.send({"t": "credit", "id": s.id, "bytes": len(c)})
    except asyncio.TimeoutError:
        await conn.abort(s, "timeout")
        raise RuntimeError(f"{part['server_name']}: {path}: the server stopped sending")
    finally:
        conn.finish(s)


def _progress(job: HubExport, p: float, step: str) -> None:
    job.progress, job.step = round(min(0.99, p), 3), step
    _save(job)


async def run(job: HubExport) -> None:
    job.state = "running"
    _save(job)
    try:
        await _build(job)
        job.state, job.step, job.progress = "done", "ready", 1.0
    except Exception as e:  # noqa: BLE001 - reported on the job
        log.exception("hub export %s failed", job.id)
        job.state, job.error = "failed", str(e)[:300]
    job.finished = time.time()
    _save(job)


async def _build(job: HubExport) -> None:
    d = job_dir(job.id)
    tmp = d / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    live = []
    # 1. each server starts its part
    for part in job.parts:
        conn = registry.get(part["server"])
        if conn is None:
            part["state"], part["error"] = "failed", "server offline"
            job.missing += [{"id": c, "name": c, "why": f"{part['server_name']} is offline"} for c in part["cameras"]]
            continue
        body = {"cameras": part["cameras"], "start": job.start + part["offset"], "end": job.end + part["offset"],
                "quality": job.quality, "audio": job.audio, "grid": False, "tiles": job.grid, "lock": job.lock,
                "note": job.note, "site": job.site}
        part["job"] = (await _call_json(conn, "POST", "/api/exports", part, job.by, body))["id"]
        part["state"] = "running"
        live.append((part, conn))
    if not live:
        raise RuntimeError("none of the servers is online")
    # 2. follow them
    while True:
        done = 0.0
        for part, conn in live:
            if part["state"] in ("done", "failed"):
                done += 1
                continue
            st = await _call_json(conn, "GET", f"/api/exports/{part['job']}", part, job.by)
            part["remote"] = st
            if st["state"] == "done":
                part["state"] = "done"
            elif st["state"] == "failed":
                part["state"], part["error"] = "failed", st.get("error")
                job.missing += [{"id": c, "name": c, "why": f"{part['server_name']}: {st.get('error')}"} for c in part["cameras"]]
            done += (st.get("progress") or 0) if part["state"] == "running" else 1
        _progress(job, 0.6 * done / len(live), "the servers are writing their files")
        if all(p["state"] in ("done", "failed") for p, _ in live):
            break
        await asyncio.sleep(POLL_S)
    ok = [(p, c) for p, c in live if p["state"] == "done"]
    if not ok:
        raise RuntimeError("every server's part failed: " + "; ".join(str(p.get("error")) for p, _ in live))
    # 3. fetch the files
    tiles: list[tuple[str, Path]] = []
    want = sum(len(p["remote"]["files"]) for p, _ in ok)
    got = 0
    for part, conn in ok:
        prefix = safe_name(part["server_name"])
        job.missing += [{**m, "name": f"{part['server_name']} · {m['name']}"} for m in part["remote"].get("missing", [])]
        job.events += part_events(part)
        for f in part["remote"]["files"]:
            if f["kind"] == "tile":
                dest = tmp / f"{prefix}_{f['name']}"
            elif f["kind"] == "report":
                dest = d / f"report_{prefix}.html"
            else:
                dest = d / f"{prefix}_{f['name']}"
            _progress(job, 0.6 + 0.3 * got / max(1, want), f"fetching {part['server_name']}: {f['name']}")
            await _fetch(conn, f"/api/exports/{part['job']}/files/{f['name']}", dest, part, job.by)
            got += 1
            if f["kind"] == "tile":
                tiles.append((f"{part['server_name']} · {part['names'].get(f.get('camera'), f.get('camera'))}", dest))
            else:
                job.files.append({"name": dest.name, "bytes": dest.stat().st_size, "sha256": await asyncio.to_thread(sha256, dest),
                                  "camera": f.get("camera"), "kind": f["kind"], "server": part["server_name"]})
    job.events.sort(key=lambda ev: ev["start_ts"])
    # 4. the grid across servers
    if job.grid and tiles:
        _progress(job, 0.92, "grid video")
        out = d / f"grid_{stamp(job.start)}.mp4"
        # the Site's wall clock: the hub runs in UTC, so take the first server's offset from UTC (its own job says)
        local = job.start + float(ok[0][0]["remote"].get("utc_offset_s") or 0.0)
        r = await asyncio.to_thread(subprocess.run, grid_args(shutil.which("ffmpeg") or "ffmpeg", [p for _, p in tiles],
                                    [n for n, _ in tiles], local, out), capture_output=True, text=True, timeout=max(300, (job.end - job.start) * 6))
        if r.returncode != 0:
            raise RuntimeError("grid video: " + ((r.stderr or "ffmpeg failed").strip().splitlines() or ["ffmpeg failed"])[-1][:200])
        job.files.append({"name": out.name, "bytes": out.stat().st_size, "sha256": await asyncio.to_thread(sha256, out), "kind": "grid"})
    # 5. report, checksums, zip
    report = d / "report.html"
    report.write_text(_report(job), encoding="utf-8")
    job.files.append({"name": report.name, "bytes": report.stat().st_size, "sha256": sha256(report), "kind": "report"})
    sums = d / "checksums.txt"
    sums.write_text("".join(f"{f['sha256']}  {f['name']}\n" for f in job.files), encoding="utf-8")
    job.zip_name = f"export_{safe_name(job.site)}_{stamp(job.start)}.zip"
    await asyncio.to_thread(_zip, d, job.zip_name, [f["name"] for f in job.files] + [sums.name])
    job.zip_bytes = (d / job.zip_name).stat().st_size
    shutil.rmtree(tmp, ignore_errors=True)


def part_events(part: dict) -> list[dict]:
    """A server's events (its export job's `events`, servers 2026-11-10 on) on the hub's clock, named with the server."""
    off = float(part.get("offset") or 0.0)
    out = []
    for ev in (part.get("remote") or {}).get("events") or []:
        end = ev.get("end_ts")
        out.append({**ev, "server": part["server_name"], "start_ts": ev["start_ts"] - off, "end_ts": end - off if end is not None else None,
                    "focus": False})
    return out


def _zip(d: Path, name: str, files: list[str]) -> None:
    with zipfile.ZipFile(d / name, "w") as z:
        for f in files:
            z.write(d / f, f, compress_type=zipfile.ZIP_STORED if f.endswith((".mp4", ".jpg")) else zipfile.ZIP_DEFLATED)


def _report(job: HubExport) -> str:
    e = html.escape
    t = lambda x: dt.datetime.fromtimestamp(x).strftime("%a %b %d %Y, %H:%M:%S")   # noqa: E731
    files = "".join(f"<tr><td>{e(f['name'])}</td><td>{e(f.get('server') or '')}</td><td>{f['bytes'] / 1e6:.1f} MB</td>"
                    f"<td class=mono>{f['sha256']}</td></tr>" for f in job.files)
    reports = "".join(f"<li><a href='{e(f['name'])}'>{e(f.get('server') or f['name'])}</a>: its cameras, files and the events in the range</li>"
                      for f in job.files if f["kind"] == "report")
    missing = "".join(f"<li>{e(m['name'])}: {e(m['why'])}</li>" for m in job.missing)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Export {e(job.id)}</title>
<style>body{{font:14px/1.45 system-ui,sans-serif;margin:32px;color:#111}}h1{{font-size:20px}}h2{{font-size:16px;margin-top:28px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:4px 6px;text-align:left;vertical-align:top}}
.mono{{font-family:ui-monospace,monospace;font-size:11px;word-break:break-all}}.muted{{color:#666}}</style></head><body>
<h1>Video export · {e(job.site)}</h1>
<p><b>{t(job.start)}</b> to <b>{t(job.end)}</b> ({job.end - job.start:.0f} s), hub time · from {len(job.parts)} servers</p>
<p>Exported by {e(job.by)} on {t(job.created)} · {'original recording' if job.quality == 'original' else '720p copy'}
{' · with audio' if job.audio else ' · no audio'}{' · footage locked against deletion on each server' if job.lock else ''}</p>
{f'<p>Note: {e(job.note)}</p>' if job.note else ''}
<h2>Each server's report</h2><ul>{reports}</ul>
<h2>Files</h2><table><tr><th>File</th><th>Server</th><th>Size</th><th>SHA-256</th></tr>{files}</table>
{f'<p>Not included:</p><ul>{missing}</ul>' if missing else ''}
<p class=muted>checksums.txt holds the same SHA-256 values; each server's report lists its own. VLC plays every file here.</p>
</body></html>"""


# ---------------------------------------------------------------- API

class CamIn(BaseModel):
    server: str = Field(max_length=32)
    camera: str = Field(max_length=40)


class SiteExportIn(BaseModel):
    cameras: list[CamIn] = Field(min_length=1, max_length=24)
    start: float
    end: float
    quality: str = Field("original", pattern="^(original|sd)$")
    audio: bool = True
    grid: bool = False
    lock: bool = True
    note: str = Field("", max_length=300)


def user(request: Request) -> dict:
    return auth.require_user(request)


@router.post("/api/locations/{location_id}/exports")
async def create(location_id: str, body: SiteExportIn, u: dict = Depends(user)):
    loc, role = auth.location_access(u, location_id)
    if not allows(role, "operator"):
        raise HTTPException(403, "needs the operator role")
    if body.end <= body.start or body.end - body.start > MAX_S:
        raise HTTPException(422, f"a range of up to {MAX_S // 60} minutes")
    if body.grid and body.end - body.start > GRID_MAX_S:
        raise HTTPException(422, f"the grid video is limited to {GRID_MAX_S // 60} minutes")
    parts: dict[str, dict] = {}
    for c in body.cameras:
        if c.server not in parts:
            s = db.one(sa.select(db.sites).where(db.sites.c.id == c.server))
            if not s or s.get("location_id") != loc["id"]:
                raise HTTPException(422, f"server {c.server} isn't part of this Site")
            parts[c.server] = {"server": c.server, "server_name": s["name"], "cameras": [], "role": role,
                               "offset": float(s.get("clock_skew_s") or 0.0), "state": "queued", "names": {}}
        if c.camera not in parts[c.server]["cameras"]:
            parts[c.server]["cameras"].append(c.camera)
            cam = db.one(sa.select(db.cameras).where(db.cameras.c.server_id == c.server, db.cameras.c.camera_id == c.camera))
            parts[c.server]["names"][c.camera] = (cam or {}).get("name") or c.camera
    _cleanup()
    job = HubExport(id=secrets.token_hex(6), user_id=u["id"], by=u.get("email") or u["id"], location_id=loc["id"],
                    site=loc.get("name") or loc["id"], parts=list(parts.values()), start=body.start, end=body.end,
                    quality=body.quality, audio=body.audio, grid=body.grid, lock=body.lock, note=body.note)
    _jobs[job.id] = job
    _save(job)
    t = asyncio.get_running_loop().create_task(run(job), name=f"hub-export-{job.id}")
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    log.info("site export %s by %s: %d cameras on %d servers, %.0f s", job.id, job.by, len(body.cameras), len(parts), body.end - body.start)
    return job.public()


def _mine(job_id: str, u: dict) -> HubExport:
    job = _get(job_id)
    if job is None:
        raise HTTPException(404, "no such export")
    if job.user_id != u["id"]:
        loc, role = auth.location_access(u, job.location_id)
        if not allows(role, "admin"):
            raise HTTPException(404, "no such export")
    return job


@router.get("/api/exports/{job_id}")
async def status(job_id: str, u: dict = Depends(user)):
    return _mine(job_id, u).public()


@router.get("/api/exports/{job_id}/download")
async def download(job_id: str, u: dict = Depends(user)):
    job = _mine(job_id, u)
    if job.state != "done":
        raise HTTPException(409, "the export isn't ready")
    return FileResponse(job_dir(job.id) / job.zip_name, media_type="application/zip", filename=job.zip_name)
