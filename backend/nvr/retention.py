"""AI-first retention.

- Every camera keeps `continuous_days` of full 24/7 recording.
- When a segment ages past that, it is curated: footage around people, Qwen-analyzed events, operator
  feedback, camera rule events, vehicles in detect-only zones and locked ranges is kept (trimmed
  losslessly with `fmp4.trim`, so MediaMTX playback and the timeline still work); the rest is deleted.
- Kept footage stays until the disk needs space; then the lowest-importance, oldest kept files go first.
  Locked footage is never deleted. Only as a last resort is continuous footage inside the window deleted,
  which raises a capacity alert.
- Event clips past the window follow the same rules (snapshots are always kept; they're tiny).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from collections import Counter
from pathlib import Path

from . import fmp4, frames, keep
from .config import settings
from .db import db
from .fmp4 import segment_start  # noqa: F401  (re-exported; older imports use retention.segment_start)

log = logging.getLogger("nvr.retention")

WHOLE_KEEP_COVERAGE = 0.8   # keep the untrimmed segment when windows cover most of it
EVENT_MEDIA_BATCH = 500

alert: dict | None = None    # set when the continuous window can't be held
last_pass: dict = {}


def free_gb() -> float:
    return shutil.disk_usage(settings.recordings_dir).free / 1e9


def camera_segments(camera_id: str) -> list[tuple[float, Path]]:
    d = settings.recordings_dir / camera_id
    if not d.exists():
        return []
    return sorted((ts, f) for f in d.glob("*.mp4") if (ts := fmp4.segment_start(f)))


def _segment_end(start: float, f: Path, next_start: float | None) -> float:
    # MediaMTX closes a segment when the next one opens; the file's mtime is its last write.
    end = f.stat().st_mtime
    return min(end, next_start) if next_start else end


def _remove(f: Path) -> int:
    frames.release(f)
    size = f.stat().st_size
    f.unlink()
    return size


def _curated_files() -> set[str]:
    return {r["file"] for r in db.all("SELECT file FROM kept_footage")}


def curate_camera(cam: dict, now: float, dry_run: bool, stats: Counter) -> None:
    policy = keep.policy_for(cam)
    horizon = now - policy["continuous_days"] * 86400
    segs = camera_segments(cam["id"])
    curated = _curated_files()
    for i, (start, f) in enumerate(segs[:-1]):  # never the newest: MediaMTX is writing it
        if str(f) in curated:
            continue
        end = _segment_end(start, f, segs[i + 1][0])
        if end > horizon:
            break  # sorted: everything after is newer
        windows, defer = keep.keep_windows(cam, start, end, policy)
        if defer and end > horizon - keep.DEFER_LIMIT_DAYS * 86400:
            stats["deferred"] += 1
            continue
        kept_s = sum(w["end"] - w["start"] for w in windows)
        coverage = kept_s / max(1.0, end - start)
        reasons = sorted({r for w in windows for r in w["reasons"]})
        if dry_run:
            stats["dry_segments"] += 1
            stats["dry_kept_s"] += kept_s
            stats["dry_deleted_s"] += (end - start) - kept_s
            log.info("[%s] dry-run %s: keep %.0fs of %.0fs %s", cam["id"], f.name, kept_s, end - start, reasons)
            continue
        try:
            if not windows:
                stats["freed_bytes"] += _remove(f)
                stats["deleted_segments"] += 1
            elif coverage >= WHOLE_KEEP_COVERAGE:
                _record(cam["id"], f, start, end, windows)
                stats["kept_whole"] += 1
            else:
                seg = fmp4.parse(f)
                before = f.stat().st_size
                outputs = []
                for w in windows:  # one call per window so each output maps to its window's reasons
                    for p, s, e in fmp4.trim(seg, start, [(w["start"], w["end"])], f.parent):
                        _record(cam["id"], p, s, e, [w])
                        outputs.append(p)
                del seg
                after = sum(p.stat().st_size for p in outputs)
                _remove(f)
                stats["trimmed"] += 1
                stats["freed_bytes"] += before - after
        except OSError as e:  # e.g. MediaMTX is serving the file right now; retry next pass
            log.warning("[%s] could not curate %s: %s", cam["id"], f.name, e)


def _record(camera_id: str, f: Path, start: float, end: float, windows: list[dict]) -> None:
    reasons = sorted({r for w in windows for r in w["reasons"]})
    score = max((w["score"] for w in windows), default=0)
    db.execute("INSERT OR REPLACE INTO kept_footage (camera_id, start_ts, end_ts, file, reasons, score, bytes, created_at) "
               "VALUES (?,?,?,?,?,?,?,?)", [camera_id, start, end, str(f), json.dumps(reasons), score,
                                             f.stat().st_size, time.time()])


def enforce_disk_floor(dry_run: bool, stats: Counter) -> None:
    """Free space by deleting the least important kept footage first, never locked; continuous only as a last resort."""
    global alert
    floor = keep.site_policy()["min_free_gb"]
    if free_gb() >= floor:
        alert = None
        return
    for r in db.all("SELECT * FROM kept_footage ORDER BY score ASC, start_ts ASC"):
        if free_gb() >= floor:
            alert = None
            return
        if keep.is_locked(r["camera_id"], r["start_ts"], r["end_ts"]):
            continue
        if dry_run:
            log.info("dry-run: would delete kept %s (score %s)", r["file"], r["score"])
            continue
        try:
            stats["freed_bytes"] += _remove(Path(r["file"])) if Path(r["file"]).exists() else 0
            db.execute("DELETE FROM kept_footage WHERE id=?", [r["id"]])
            stats["deleted_kept"] += 1
        except OSError as e:
            log.warning("could not delete %s: %s", r["file"], e)
    if free_gb() >= floor:
        alert = None
        return
    # Last resort: oldest continuous footage (breaks the continuous guarantee; say so loudly).
    curated = _curated_files()
    oldest = sorted((s, f, cam["id"]) for cam in db.cameras()
                    for s, f in camera_segments(cam["id"])[:-1] if str(f) not in curated)
    for s, f, cam_id in oldest:
        if free_gb() >= floor:
            break
        if keep.is_locked(cam_id, s, s + 600):
            continue
        if dry_run:
            log.warning("dry-run: would delete continuous %s to free space", f.name)
            continue
        try:
            stats["freed_bytes"] += _remove(f)
            stats["deleted_continuous"] += 1
        except OSError as e:
            log.warning("could not delete %s: %s", f, e)
    if stats["deleted_continuous"] or free_gb() < floor:
        alert = {"at": time.time(), "free_gb": round(free_gb(), 1), "floor_gb": floor,
                 "message": "Disk is too small to hold the continuous window for all cameras; "
                            "oldest continuous footage was deleted. Add storage, lower continuous days or the free-space floor."}
        log.error(alert["message"])


MASKED_ROW_DAYS = 30   # masked (outside-zone) events: media goes at once, the row after this long


def purge_masked(now: float, dry_run: bool, stats: Counter) -> None:
    """Events hidden by a zone mask are never kept by retention, so their clips are dead weight: delete
    the media straight away (the snapshot stays so an unmask still shows something), and the rows once old."""
    for e in db.all("SELECT id FROM events WHERE status='masked' AND clip IS NOT NULL LIMIT ?", [EVENT_MEDIA_BATCH * 4]):
        if dry_run:
            stats["dry_masked_clips"] += 1
            continue
        d = settings.data_dir / "events" / str(e["id"])
        freed = 0
        for f in d.glob("*"):
            if f.name != "snapshot.jpg":
                freed += f.stat().st_size
                f.unlink(missing_ok=True)
        db.update_event(e["id"], clip=None)
        stats["masked_clips_deleted"] += 1
        stats["freed_bytes"] += freed
    old = db.all("SELECT id FROM events WHERE status='masked' AND start_ts < ? LIMIT ?", [now - MASKED_ROW_DAYS * 86400, EVENT_MEDIA_BATCH])
    for e in old:
        if dry_run:
            stats["dry_masked_rows"] += 1
            continue
        d = settings.data_dir / "events" / str(e["id"])
        for f in d.glob("*"):
            f.unlink(missing_ok=True)
        if d.exists():
            d.rmdir()
        db.execute("DELETE FROM events WHERE id=?", [e["id"]])
        stats["masked_rows_deleted"] += 1


def prune_event_media(now: float, dry_run: bool, stats: Counter) -> None:
    """Past the continuous window, keep event clips only for events retention would keep."""
    cams = {c["id"]: c for c in db.cameras()}
    for cam_id, cam in cams.items():
        policy = keep.policy_for(cam)
        horizon = now - policy["continuous_days"] * 86400
        rows = db.all("SELECT id, start_ts, end_ts FROM events WHERE camera_id=? AND start_ts < ? AND clip IS NOT NULL "
                      "ORDER BY start_ts LIMIT ?", [cam_id, horizon, EVENT_MEDIA_BATCH])
        for e in rows:
            windows, _ = keep.keep_windows(cam, e["start_ts"], e["end_ts"] or e["start_ts"], policy)
            if windows:
                continue
            if dry_run:
                stats["dry_event_clips"] += 1
                continue
            d = settings.data_dir / "events" / str(e["id"])
            for f in d.glob("*"):
                if f.name != "snapshot.jpg":
                    f.unlink(missing_ok=True)
            db.update_event(e["id"], clip=None)
            stats["event_clips_deleted"] += 1


def enforce() -> dict:
    global last_pass
    now = time.time()
    dry_run = settings.retention_dry_run
    stats: Counter = Counter()
    for cam in db.cameras():
        curate_camera(cam, now, dry_run, stats)
    enforce_disk_floor(dry_run, stats)
    prune_event_media(now, dry_run, stats)
    purge_masked(now, dry_run, stats)
    last_pass = {"at": now, "dry_run": dry_run, **{k: round(v, 1) for k, v in stats.items()}}
    if any(v for k, v in stats.items() if k != "deferred"):
        log.info("retention pass: %s", last_pass)
    return last_pass


# ---------------------------------------------------------------- reporting

def _dir_bytes(files) -> int:
    total = 0
    for f in files:
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


def stats() -> dict:
    now = time.time()
    curated = {r["file"]: r for r in db.all("SELECT * FROM kept_footage")}
    out = []
    for cam in db.cameras():
        segs = camera_segments(cam["id"])
        cont = [(s, f) for s, f in segs if str(f) not in curated]
        kept_rows = [r for r in curated.values() if r["camera_id"] == cam["id"]]
        locked_bytes = sum(r["bytes"] for r in kept_rows if keep.is_locked(cam["id"], r["start_ts"], r["end_ts"]))
        day_start = max(now - 86400, cont[0][0]) if cont else now
        last_day_bytes = _dir_bytes(f for s, f in cont if s >= day_start)
        out.append({
            "camera_id": cam["id"], "name": cam["name"],
            "policy": keep.policy_for(cam),
            "continuous_gb": round(_dir_bytes(f for _, f in cont) / 1e9, 2),
            "kept_gb": round(sum(r["bytes"] for r in kept_rows) / 1e9, 2),
            "locked_gb": round(locked_bytes / 1e9, 2),
            "kept_files": len(kept_rows),
            "continuous_days_on_disk": round((now - cont[0][0]) / 86400, 2) if cont else 0,
            "gb_per_day": round(last_day_bytes / 1e9 * 86400 / max(3600, now - day_start), 1) if cont else 0,
        })
    du = shutil.disk_usage(settings.recordings_dir)
    return {"cameras": out, "disk": {"total_gb": round(du.total / 1e9), "free_gb": round(du.free / 1e9)},
            "alert": alert, "last_pass": last_pass, "dry_run": settings.retention_dry_run,
            "locks": db.one("SELECT COUNT(*) n FROM locks")["n"]}


def preview(camera_id: str, hours: float = 24) -> dict:
    """What the next `hours` of age-outs would keep vs delete (no changes)."""
    cam = next((c for c in db.cameras() if c["id"] == camera_id), None)
    if not cam:
        return {}
    policy = keep.policy_for(cam)
    now = time.time()
    horizon = now - policy["continuous_days"] * 86400
    segs = camera_segments(camera_id)
    curated = _curated_files()
    kept_s = total_s = freed = 0.0
    by_reason: Counter = Counter()
    n = deferred = 0
    for i, (start, f) in enumerate(segs[:-1]):
        if str(f) in curated:
            continue
        end = _segment_end(start, f, segs[i + 1][0])
        if end > horizon + hours * 3600:
            break
        windows, defer = keep.keep_windows(cam, start, end, policy)
        deferred += defer
        k = sum(w["end"] - w["start"] for w in windows)
        for w in windows:
            for r in w["reasons"]:
                by_reason[r.split(":")[0] if r.startswith("rule") else r] += w["end"] - w["start"]
        size = f.stat().st_size
        cover = k / max(1.0, end - start)
        freed += 0 if cover >= WHOLE_KEEP_COVERAGE else size * (1 - cover)
        kept_s += k
        total_s += end - start
        n += 1
    return {"camera_id": camera_id, "hours": hours, "segments": n, "deferred": deferred,
            "kept_minutes": round(kept_s / 60, 1), "deleted_minutes": round((total_s - kept_s) / 60, 1),
            "freed_gb": round(freed / 1e9, 2), "kept_minutes_by_reason": {k: round(v / 60, 1) for k, v in by_reason.items()},
            "horizon": horizon}


async def retention_loop(interval: float = 600) -> None:
    while True:
        try:
            await asyncio.to_thread(enforce)
        except Exception:
            log.exception("retention pass failed")
        await asyncio.sleep(interval)
