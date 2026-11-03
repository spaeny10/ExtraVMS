"""Exports: a stretch of recording from one or more cameras, as files to keep or hand over.

POST /api/exports starts a job; it runs in the background (one at a time) and leaves, under <data_dir>/exports/<id>/:
  <Camera>_<YYYY-MM-DD_HH-MM-SS>.mp4   per camera: the recording as stored (original: video copied, not re-encoded)
                                       or a 720p H.264 copy (sd); audio as AAC (G.711 converted) or none
  grid.mp4                             optional: every camera side by side, names and the time drawn on
  event_<id>_*.jpg                     an export started from an event: its snapshot and marked PPE frame
  report.html                          site, cameras, range, who exported it, the events in the range (prints to PDF)
  checksums.txt                        SHA-256 of every file, to show later that nothing was altered
  export.zip                           all of the above (what the Download button gives)
  tile_<camera>.mp4                    asked by the hub (tiles): a 640x360 copy per camera for its grid across servers;
                                       not in the zip, the report or the checksums
The range is locked on each camera (retention keeps it) unless asked not to. Jobs are kept KEEP_S, then deleted.
The hub collects these per server for a Site export across servers (hub/hub/exports.py).
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

import httpx

from . import mediamtx
from .config import settings
from .db import db

log = logging.getLogger("nvr.exports")

MAX_S = 3600            # one export: at most an hour
GRID_MAX_S = 1800       # the grid re-encodes every camera: at most 30 min
KEEP_S = 7 * 86400
GRID_W, GRID_H, GRID_FPS = 1920, 1080, 10
SD_HEIGHT, SD_KBPS = 720, 1500
TILE_W, TILE_H = 640, 360
FONTS = ["C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf"]

_jobs: dict[str, "Job"] = {}
_run_locks: dict = {}   # event loop -> its lock: one export at a time (tests run several loops)
_tasks: set[asyncio.Task] = set()


@dataclass
class Job:
    id: str
    cameras: list[dict]                  # [{id, name}]
    start: float
    end: float
    quality: str = "original"            # original | sd
    audio: bool = True
    grid: bool = False
    tiles: bool = False                  # small copies for the hub's grid across servers
    lock: bool = True
    by: str = "local"
    event_id: int | None = None
    note: str = ""
    site: str = ""
    created: float = field(default_factory=time.time)
    state: str = "queued"                # queued | running | done | failed
    step: str = ""
    progress: float = 0.0
    files: list[dict] = field(default_factory=list)    # {name, bytes, sha256, camera?, kind}
    missing: list[dict] = field(default_factory=list)  # cameras with no recording in the range: {id, name, why}
    zip_name: str = ""
    zip_bytes: int = 0
    error: str | None = None
    finished: float | None = None
    utc_offset_s: float = 0.0           # this server's local time at the start, minus UTC (the hub's grid clock)

    def public(self) -> dict:
        return asdict(self)


def root() -> Path:
    return settings.data_dir / "exports"


def job_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9]{12}", job_id):
        raise ValueError("bad export id")
    return root() / job_id


def _save(job: Job) -> None:
    d = job_dir(job.id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "job.json").write_text(json.dumps(job.public()), encoding="utf-8")


def load() -> None:
    """Jobs from disk (after a restart); one that was running when the server stopped is marked failed."""
    if not root().exists():
        return
    for f in root().glob("*/job.json"):
        try:
            j = Job(**json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            continue
        if j.state in ("queued", "running"):
            j.state, j.error = "failed", "the server restarted during the export"
        _jobs[j.id] = j


def get(job_id: str) -> Job | None:
    if job_id not in _jobs:
        load()
    return _jobs.get(job_id)


def recent(limit: int = 20) -> list[Job]:
    if not _jobs:
        load()
    return sorted(_jobs.values(), key=lambda j: j.created, reverse=True)[:limit]


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")[:60] or "camera"


def stamp(t: float) -> str:
    return dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d_%H-%M-%S")


def grid_layout(n: int) -> tuple[int, int]:
    """(columns, rows) for n tiles."""
    for cols, rows in ((1, 1), (2, 1), (2, 2), (3, 2), (3, 3), (4, 3), (4, 4), (5, 4), (6, 4)):
        if cols * rows >= n:
            return cols, rows
    raise ValueError("at most 24 cameras in a grid")


def _font() -> str | None:
    return next((f for f in FONTS if Path(f).is_file()), None)


def _esc(s: str) -> str:
    """Text for an ffmpeg filter option inside single quotes."""
    return s.replace("\\", "\\\\").replace("'", "\u2019").replace(":", "\\:").replace("%", "\\%")


def grid_filter(labels: list[str], start: float, font: str | None) -> tuple[str, str]:
    """(filter_complex, output label) laying len(labels) inputs out on a GRID_W x GRID_H canvas, each named, with the
    wall-clock time along the bottom."""
    n = len(labels)
    cols, rows = grid_layout(n)
    w, h = GRID_W // cols // 2 * 2, GRID_H // rows // 2 * 2
    ff = f"fontfile='{_esc(font)}':" if font else ""
    parts = []
    for i, name in enumerate(labels):
        tile = (f"[{i}:v]fps={GRID_FPS},scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1")
        if font:
            tile += (f",drawtext={ff}text='{_esc(name)}':x=8:y=8:fontsize={max(14, h // 22)}:fontcolor=white:"
                     "box=1:boxcolor=black@0.55:boxborderw=4")
        parts.append(tile + f"[t{i}]")
    if n == 1:
        last = "t0"
    else:
        layout = "|".join(f"{(i % cols) * w}_{(i // cols) * h}" for i in range(n))
        parts.append("".join(f"[t{i}]" for i in range(n)) + f"xstack=inputs={n}:layout={layout}:fill=black[g]")
        last = "g"
    if font:
        # the shared clock: the export's start plus the frame time, in this server's local time
        local = start + (dt.datetime.fromtimestamp(start).astimezone().utcoffset() or dt.timedelta()).total_seconds()
        parts.append(f"[{last}]drawtext={ff}text='%{{pts\\:gmtime\\:{local:.3f}}}':x=(w-tw)/2:y=h-th-12:"
                     f"fontsize={GRID_H // 30}:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=6[out]")
        last = "out"
    return ";".join(parts), last


def _video_enc(encoder: str | None, kbps: int) -> list[str]:
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-b:v", f"{kbps}k", "-maxrate", f"{kbps * 3 // 2}k"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{kbps}k", "-maxrate", f"{kbps * 3 // 2}k",
            "-bufsize", f"{kbps * 2}k"]


def camera_args(ffmpeg: str, raw: Path, out: Path, quality: str, audio: bool, encoder: str | None) -> list[str]:
    """ffmpeg argv: the raw MediaMTX MP4 -> the exported file (video copied for original, 720p H.264 for sd; audio AAC
    or none), moov at the front so it plays while it downloads."""
    args = [ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(raw), "-map", "0:v:0"]
    if audio:
        args += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "96k"]
    else:
        args += ["-an"]
    if quality == "sd":
        args += ["-vf", f"scale=w=-2:h='min({SD_HEIGHT},ih)'", "-pix_fmt", "yuv420p", *_video_enc(encoder, SD_KBPS)]
    else:
        args += ["-c:v", "copy"]
    return args + ["-movflags", "+faststart", str(out)]


def tile_args(ffmpeg: str, src: Path, out: Path, encoder: str | None) -> list[str]:
    return [ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(src), "-an", "-vf",
            f"fps={GRID_FPS},scale={TILE_W}:{TILE_H}:force_original_aspect_ratio=decrease,pad={TILE_W}:{TILE_H}:(ow-iw)/2:(oh-ih)/2",
            "-pix_fmt", "yuv420p", *_video_enc(encoder, 600), "-movflags", "+faststart", str(out)]


def grid_args(ffmpeg: str, inputs: list[Path], labels: list[str], start: float, out: Path, encoder: str | None) -> list[str]:
    fc, last = grid_filter(labels, start, _font())
    args = [ffmpeg, "-v", "error", "-nostdin", "-y"]
    for p in inputs:
        args += ["-i", str(p)]
    return args + ["-filter_complex", fc, "-map", f"[{last}]", "-an", "-pix_fmt", "yuv420p", *_video_enc(encoder, 6000),
                   "-shortest", "-movflags", "+faststart", str(out)]


def _run(args: list[str], timeout: float) -> None:
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "ffmpeg failed").strip().splitlines()[-1][:300])


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"


# ---------------------------------------------------------------- the job

def start(cameras: list[dict], start_ts: float, end_ts: float, *, encoder: str | None, **kw) -> Job:
    """Queue an export; it runs in the background on the current loop."""
    job = Job(id=secrets.token_hex(6), cameras=cameras, start=start_ts, end=end_ts,
              utc_offset_s=(dt.datetime.fromtimestamp(start_ts).astimezone().utcoffset() or dt.timedelta()).total_seconds(), **kw)
    _jobs[job.id] = job
    _save(job)
    t = asyncio.get_running_loop().create_task(_job(job, encoder), name=f"export-{job.id}")
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return job


async def _job(job: Job, encoder: str | None) -> None:
    async with _run_locks.setdefault(asyncio.get_running_loop(), asyncio.Lock()):
        job.state, job.step = "running", "starting"
        _save(job)
        try:
            await _build(job, encoder)
            job.state, job.step, job.progress = "done", "ready", 1.0
        except Exception as e:  # noqa: BLE001 - the job reports it; the server carries on
            log.exception("export %s failed", job.id)
            job.state, job.error = "failed", str(e)[:300]
        job.finished = time.time()
        _save(job)


def _progress(job: Job, done: float, step: str) -> None:
    job.progress, job.step = round(min(0.99, done), 3), step
    _save(job)


async def _build(job: Job, encoder: str | None) -> None:
    d = job_dir(job.id)
    tmp = d / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    ff = _ffmpeg()
    dur = job.end - job.start
    n = len(job.cameras)
    steps = n + (1 if job.grid else 0) + 1
    made: list[tuple[dict, Path]] = []
    for i, cam in enumerate(job.cameras):
        _progress(job, i / steps, f"{cam['name']}: reading the recording")
        raw = tmp / f"{cam['id']}.raw.mp4"
        try:
            await mediamtx.fetch_clip(cam["id"], job.start, dur, raw)
        except httpx.HTTPStatusError as e:
            job.missing.append({**cam, "why": "no recording in this range" if e.response.status_code == 404 else f"HTTP {e.response.status_code}"})
            continue
        out = d / f"{safe_name(cam['name'])}_{stamp(job.start)}.mp4"
        _progress(job, (i + 0.5) / steps, f"{cam['name']}: writing the file")
        await asyncio.to_thread(_run, camera_args(ff, raw, out, job.quality, job.audio, encoder), max(120, dur * 3))
        raw.unlink(missing_ok=True)
        made.append((cam, out))
    if not made:
        raise RuntimeError("none of the cameras has recording in this range")
    if job.grid:
        _progress(job, n / steps, "grid video")
        out = d / f"grid_{stamp(job.start)}.mp4"
        await asyncio.to_thread(_run, grid_args(ff, [p for _, p in made], [c["name"] for c, _ in made], job.start, out, encoder),
                                max(300, dur * 6))
        made.append(({"id": "grid", "name": "All cameras (grid)"}, out))
    tiles = []
    if job.tiles:
        _progress(job, (steps - 1) / steps, "tiles for the hub's grid")
        for cam, p in made:
            if cam["id"] != "grid":
                t = d / f"tile_{safe_name(cam['id'])}.mp4"
                await asyncio.to_thread(_run, tile_args(ff, p, t, encoder), max(120, dur * 3))
                tiles.append((cam, t))
    _progress(job, (steps - 1) / steps, "report")
    images = _event_images(job, d)
    for cam, p in made:
        job.files.append({"name": p.name, "bytes": p.stat().st_size, "sha256": await asyncio.to_thread(sha256, p),
                          "camera": cam["id"], "kind": "grid" if cam["id"] == "grid" else "video"})
    for p in images:
        job.files.append({"name": p.name, "bytes": p.stat().st_size, "sha256": sha256(p), "kind": "image"})
    if job.lock:
        _lock(job, [c for c, _ in made if c["id"] != "grid"])
    tile_files = [{"name": t.name, "bytes": t.stat().st_size, "sha256": "", "camera": c["id"], "kind": "tile"} for c, t in tiles]
    report = d / "report.html"
    report.write_text(report_html(job, _events(job)), encoding="utf-8")
    job.files.append({"name": report.name, "bytes": report.stat().st_size, "sha256": sha256(report), "kind": "report"})
    sums = d / "checksums.txt"
    sums.write_text("".join(f"{f['sha256']}  {f['name']}\n" for f in job.files), encoding="utf-8")
    job.zip_name = f"export_{safe_name(job.site) + '_' if job.site else ''}{stamp(job.start)}.zip"
    await asyncio.to_thread(_zip, d, job.zip_name, [f["name"] for f in job.files] + [sums.name])
    job.zip_bytes = (d / job.zip_name).stat().st_size
    job.files += tile_files   # fetched by the hub one by one; not part of the zip
    shutil.rmtree(tmp, ignore_errors=True)


def _zip(d: Path, name: str, files: list[str]) -> None:
    with zipfile.ZipFile(d / name, "w") as z:
        for f in files:
            stored = f.endswith((".mp4", ".jpg"))   # already compressed
            z.write(d / f, f, compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)


def _event_images(job: Job, d: Path) -> list[Path]:
    if not job.event_id:
        return []
    from .verifier import event_dir
    out = []
    for src, label in (("snapshot.jpg", "snapshot"), ("ppe.jpg", "ppe")):
        p = event_dir(job.event_id) / src
        if p.is_file():
            dst = d / f"event_{job.event_id}_{label}.jpg"
            shutil.copyfile(p, dst)
            out.append(dst)
    return out


def _lock(job: Job, cams: list[dict]) -> None:
    note = f"Exported by {job.by}" + (f": {job.note}" if job.note else "")
    for c in cams:
        db.execute("INSERT INTO locks (camera_id, start_ts, end_ts, note, created_at) VALUES (?,?,?,?,?)",
                   [c["id"], job.start, job.end, note[:200], time.time()])


def _events(job: Job) -> list[dict]:
    ids = [c["id"] for c in job.cameras]
    rows = db.all(f"SELECT id, camera_id, camera_class, start_ts, end_ts, status, priority, threat, synopsis, policy, "
                  f"json_extract(feedback, '$.verdict') AS verdict, yolo_conf FROM events WHERE camera_id IN ({','.join('?' * len(ids))}) "
                  "AND start_ts<=? AND COALESCE(end_ts, start_ts)>=? AND status='verified' ORDER BY start_ts",
                  [*ids, job.end, job.start])
    for r in rows:
        try:
            r["policy"] = json.loads(r["policy"]) if r.get("policy") else None
        except ValueError:
            r["policy"] = None
    return rows


def _t(t: float | None) -> str:
    return dt.datetime.fromtimestamp(t).strftime("%a %b %d %Y, %H:%M:%S") if t else ""


def report_html(job: Job, events: list[dict]) -> str:
    e = html.escape
    names = {c["id"]: c["name"] for c in job.cameras}
    tz = dt.datetime.fromtimestamp(job.start).astimezone().strftime("%Z (UTC%z)")
    rows_files = "".join(
        f"<tr><td>{e(f['name'])}</td><td>{e(names.get(f.get('camera', ''), f.get('kind', '')))}</td>"
        f"<td>{f['bytes'] / 1e6:.1f} MB</td><td class=mono>{f['sha256']}</td></tr>" for f in job.files)
    rows_missing = "".join(f"<li>{e(m['name'])}: {e(m['why'])}</li>" for m in job.missing)
    focus = next((ev for ev in events if ev["id"] == job.event_id), None)
    focus_html = ""
    if focus:
        pol = (focus.get("policy") or {}).get("text")
        imgs = "".join(f"<img src='{e(f['name'])}' alt=''>" for f in job.files if f["kind"] == "image")
        focus_html = (f"<h2>Event #{focus['id']}</h2><p><b>{e(names.get(focus['camera_id'], focus['camera_id']))}</b> · "
                      f"{e(focus['camera_class'] or '')} · {_t(focus['start_ts'])} · priority {e(focus.get('priority') or 'none')}</p>"
                      f"<p>{e(focus.get('synopsis') or '')}</p>"
                      + (f"<p class=warn>Rule: {e(pol)}</p>" if pol else "")
                      + (f"<p>Detection confidence: {focus['yolo_conf'] * 100:.0f}%</p>" if focus.get("yolo_conf") else "")
                      + (f"<p>Reviewed as: {e(focus['verdict'])}</p>" if focus.get("verdict") else "") + f"<div class=imgs>{imgs}</div>")
    rows_events = "".join(
        f"<tr><td>{_t(ev['start_ts'])[-8:]}</td><td>{e(names.get(ev['camera_id'], ev['camera_id']))}</td>"
        f"<td>{e(ev['camera_class'] or '')}</td><td>{e(ev.get('priority') or '')}</td>"
        f"<td>{e(ev.get('synopsis') or '')}{' · ' + e(ev['policy']['text']) if ev.get('policy') and ev['policy'].get('text') else ''}</td></tr>"
        for ev in events)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Export {e(job.id)}</title>
<style>body{{font:14px/1.45 system-ui,sans-serif;margin:32px;color:#111}}h1{{font-size:20px}}h2{{font-size:16px;margin-top:28px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:4px 6px;text-align:left;vertical-align:top}}
.mono{{font-family:ui-monospace,monospace;font-size:11px;word-break:break-all}}.muted{{color:#666}}.warn{{color:#b00}}
.imgs img{{max-width:48%;margin:4px;border:1px solid #ccc}}</style></head><body>
<h1>Video export{' · ' + e(job.site) if job.site else ''}</h1>
<p><b>{_t(job.start)}</b> to <b>{_t(job.end)}</b> ({(job.end - job.start):.0f} s) · times in {e(tz)}</p>
<p>Exported by {e(job.by)} on {_t(job.created)} · {'original recording (video not re-encoded)' if job.quality == 'original' else '720p copy'}
{' · with audio' if job.audio else ' · no audio'}{' · footage locked against deletion' if job.lock else ''}</p>
{f'<p>Note: {e(job.note)}</p>' if job.note else ''}
{focus_html}
<h2>Files</h2><table><tr><th>File</th><th>Camera</th><th>Size</th><th>SHA-256</th></tr>{rows_files}</table>
{f'<p>Not included:</p><ul>{rows_missing}</ul>' if rows_missing else ''}
<p class=muted>checksums.txt holds the same SHA-256 values: recompute them (for example <code>sha256sum</code> or
<code>Get-FileHash</code>) to show a file is unchanged. Some cameras record H.265; VLC plays every file here.</p>
<h2>Events in this range</h2>{'<table><tr><th>Time</th><th>Camera</th><th>What</th><th>Priority</th><th>Description</th></tr>' + rows_events + '</table>' if events else '<p class=muted>None.</p>'}
</body></html>"""


async def cleanup_loop(stop: asyncio.Event | None = None) -> None:
    """Delete exports older than KEEP_S, hourly."""
    while stop is None or not stop.is_set():
        cutoff = time.time() - KEEP_S
        for j in list(recent(10_000)):
            if j.created < cutoff and j.state in ("done", "failed"):
                shutil.rmtree(job_dir(j.id), ignore_errors=True)
                _jobs.pop(j.id, None)
        await asyncio.sleep(3600)
