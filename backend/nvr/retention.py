"""AI-first retention.

- Every camera keeps `continuous_days` of full 24/7 recording.
- When a segment ages past that, it is curated: footage around people, Qwen-analyzed events, operator
  feedback, camera rule events, vehicles in detect-only zones and locked ranges is kept (trimmed
  losslessly with `fmp4.trim`, so MediaMTX playback and the timeline still work); the rest is deleted.
- Kept footage stays until the disk needs space; then the lowest-importance, oldest kept files go first.
  Locked footage is never deleted. Only as a last resort is continuous footage inside the window deleted,
  which raises a capacity alert.
- Event clips past the window follow the same rules (snapshots are always kept; they're tiny).
- Event media lives on the data_dir volume, which has its own floor: below it clips go oldest-first whatever
  the keep rules say (see enforce_disk_floor), and a camera opening too many events keeps snapshots only.
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

from . import diskguard, fmp4, frames, keep
from .config import settings
from .db import LOCKED_SQL, db
from .fmp4 import segment_start  # noqa: F401  (re-exported; older imports use retention.segment_start)

log = logging.getLogger("nvr.retention")

WHOLE_KEEP_COVERAGE = 0.8   # keep the untrimmed segment when windows cover most of it
EVENT_MEDIA_BATCH = 500
EVENT_MEDIA_BATCH_MAX = 5000  # catch-up batch: a large backlog, or the data disk under 2x its floor
FLOOR_PAGE = 500              # rows read per query while the disk floor frees event media

alert: dict | None = None    # set when the continuous window can't be held, or event media is short of space
last_pass: dict = {}
_continuous_breach: dict | None = None   # set by enforce_disk_floor; one input to `alert`
_prune_cursor: dict[str, tuple[float, int]] = {}   # camera -> last (start_ts, id) prune_event_media looked at


def free_gb() -> float:
    return shutil.disk_usage(settings.recordings_dir).free / 1e9


def data_free_gb() -> float:
    """Free space on the volume holding data_dir (sqlite, snapshots, event clips): not always the recordings disk."""
    return diskguard.free_bytes(settings.data_dir) / 1e9


def same_volume() -> bool:
    try:
        return os.stat(settings.recordings_dir).st_dev == os.stat(settings.data_dir).st_dev
    except OSError:
        return False


def data_floor() -> tuple[float, float]:
    """(floor, target) in GB for the data_dir volume. Pruning event media starts below the floor and runs to the
    target (floor + a hysteresis margin), so one pass buys real headroom instead of hovering at the floor."""
    try:
        total_gb = shutil.disk_usage(settings.data_dir).total / 1e9
    except OSError:
        total_gb = 0.0
    if settings.event_media_min_free_gb:
        floor = float(settings.event_media_min_free_gb)
    elif same_volume():
        floor = float(keep.site_policy()["min_free_gb"])   # one disk, one floor
    else:
        # A separate data volume is usually the OS drive, which legitimately runs with little free space; a
        # "10 % of the disk" floor there deleted every clip on the desktop (Oct 5 2026: C: had 8 GB free of 1 TB).
        # Event media is evidence, so this floor is only an emergency guard: 5 GB, up to 20 GB on big disks.
        floor = float(min(20.0, max(5.0, total_gb * 0.02)))
    return floor, floor + total_gb * settings.disk_floor_hysteresis_pct / 100


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
        except Exception:  # one odd segment must not stop the pass: the rest of the disk still needs curating
            log.exception("[%s] curating %s failed; skipped", cam["id"], f.name)


def _record(camera_id: str, f: Path, start: float, end: float, windows: list[dict]) -> None:
    reasons = sorted({r for w in windows for r in w["reasons"]})
    score = max((w["score"] for w in windows), default=0)
    db.execute("INSERT OR REPLACE INTO kept_footage (camera_id, start_ts, end_ts, file, reasons, score, bytes, created_at) "
               "VALUES (?,?,?,?,?,?,?,?)", [camera_id, start, end, str(f), json.dumps(reasons), score,
                                             f.stat().st_size, time.time()])


def _horizons(now: float) -> dict[str, float]:
    return {c["id"]: now - keep.policy_for(c)["continuous_days"] * 86400 for c in db.cameras()}


def free_event_media(target_gb: float, dry_run: bool, stats: Counter, *, horizons: dict[str, float] | None = None,
                     locked: bool = False) -> int:
    """Delete event media (clip.mp4, wide.jpg, crop_*.jpg; never snapshot.jpg) oldest-first, ignoring keep windows,
    until the data volume has `target_gb` free. A clip duplicates footage that is in the recordings (or in
    kept_footage), so it is the cheapest thing to lose when the disk is full.

    horizons: only events older than their camera's continuous window. locked: False skips events under a footage
    lock (or overlapped by one); True deletes only those, the very last resort. Returns the events emptied.
    """
    n = freed = 0
    last = (float("-inf"), -1)
    stop_at = max(horizons.values(), default=float("-inf")) if horizons is not None else float("inf")
    while data_free_gb() < target_gb:
        # Keyset paging: rows skipped (locked, or inside their camera's window) don't come back on the next page.
        # status='error' rows: verify failed after the clip was fetched, so clip.mp4 is on disk but not in the row.
        rows = db.all(f"SELECT id, camera_id, start_ts, end_ts, clip, {LOCKED_SQL} AS locked FROM events "
                      "WHERE (clip IS NOT NULL OR status='error') AND status NOT IN ('open','pending') "
                      "AND (start_ts > ? OR (start_ts = ? AND id > ?)) AND start_ts < ? ORDER BY start_ts, id LIMIT ?",
                      [last[0], last[0], last[1], stop_at, FLOOR_PAGE])
        if not rows:
            break
        for r in rows:
            last = (r["start_ts"], r["id"])
            if data_free_gb() >= target_gb:
                break
            if horizons is not None and r["start_ts"] >= horizons.get(r["camera_id"], float("-inf")):
                continue
            is_locked = bool(r["locked"]) or keep.is_locked(r["camera_id"], r["start_ts"], r["end_ts"] or r["start_ts"])
            if is_locked != locked:
                continue
            if dry_run:
                stats["dry_floor_event_clips"] += 1
                continue
            got = diskguard.delete_event_media(settings.data_dir / "events" / str(r["id"]))
            if r["clip"] is not None:
                db.update_event(r["id"], clip=None)
            if got or r["clip"] is not None:
                n += 1
                freed += got
    if n:
        stats["floor_event_clips_deleted"] += n
        stats["freed_bytes"] += freed
        log.warning("disk floor: freed %.1f GB of event media (%d events%s); %.1f GB free on %s",
                    freed / 1e9, n, ", locked" if locked else "", data_free_gb(), settings.data_dir)
    return n


def enforce_disk_floor(dry_run: bool, stats: Counter, now: float | None = None) -> None:
    """Hold the free-space floor on both volumes, the recordings disk and the data_dir disk. They are often the
    same disk; then free space is shared and each step sees what the previous one freed. Cheapest loss first:

    1. data disk: event media past the continuous window (that footage is in the recordings or kept_footage)
    2. recordings: the least important kept footage, never locked
    3. data disk: any unlocked event media, oldest first
    4. recordings: oldest continuous footage (breaks the continuous guarantee; alerts)
    5. data disk: locked events' media, only when nothing else is left
    """
    global _continuous_breach
    now = now or time.time()
    floor = keep.site_policy()["min_free_gb"]
    dfloor, dtarget = data_floor()
    if data_free_gb() < dfloor:
        free_event_media(dtarget, dry_run, stats, horizons=_horizons(now))
    if free_gb() < floor:
        _free_kept(floor, dry_run, stats)
    if data_free_gb() < dfloor:
        free_event_media(dtarget, dry_run, stats)
    if free_gb() < floor:
        _free_continuous(floor, dry_run, stats)
    if data_free_gb() < dfloor:
        free_event_media(dtarget, dry_run, stats, locked=True)
    if stats["deleted_continuous"] or free_gb() < floor:
        _continuous_breach = {"at": time.time(), "free_gb": round(free_gb(), 1), "floor_gb": round(floor, 1),
                              "message": "Disk is too small to hold the continuous window for all cameras; "
                                         "oldest continuous footage was deleted. Add storage, lower continuous days or the free-space floor."}
        log.error(_continuous_breach["message"])
    else:
        _continuous_breach = None


def _free_kept(floor: float, dry_run: bool, stats: Counter) -> None:
    """The least important kept footage first, never locked."""
    for r in db.all("SELECT * FROM kept_footage ORDER BY score ASC, start_ts ASC"):
        if free_gb() >= floor:
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


def _free_continuous(floor: float, dry_run: bool, stats: Counter) -> None:
    """Last resort: oldest continuous footage (enforce_disk_floor raises the alert)."""
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


MASKED_ROW_DAYS = 30   # masked (outside-zone) events: media goes at once, the row after this long


def purge_masked(now: float, dry_run: bool, stats: Counter) -> None:
    """Events hidden by a zone mask are never kept by retention, so their clips are dead weight: delete
    the media straight away (the snapshot stays so an unmask still shows something), and the rows once old."""
    for e in db.all("SELECT id FROM events WHERE status='masked' AND clip IS NOT NULL LIMIT ?", [EVENT_MEDIA_BATCH * 4]):
        if dry_run:
            stats["dry_masked_clips"] += 1
            continue
        freed = diskguard.delete_event_media(settings.data_dir / "events" / str(e["id"]))
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


def event_media_batch(backlog: int, data_free: float, floor: float) -> int:
    """Clips prune_event_media may clear per camera per pass. 500 keeps a pass short; a backlog bigger than that
    (a camera making an event every 25 s adds ~140 an hour) or a data disk under 2x its floor needs the big batch
    to catch up instead of falling further behind every ten minutes."""
    if data_free < 2 * floor or backlog > 2 * EVENT_MEDIA_BATCH:
        return EVENT_MEDIA_BATCH_MAX
    return EVENT_MEDIA_BATCH


def prune_event_media(now: float, dry_run: bool, stats: Counter) -> None:
    """Past the continuous window, keep event clips only for events retention would keep.

    Kept events keep their clip, so they would fill the front of an oldest-first LIMIT for ever and the prune
    would stop advancing: a per-camera cursor walks past them (it wraps to the start once the camera is caught
    up, so an event that lost its reason to be kept is looked at again)."""
    floor, _ = data_floor()
    data_free = data_free_gb()
    for cam in db.cameras():
        cam_id = cam["id"]
        policy = keep.policy_for(cam)
        horizon = now - policy["continuous_days"] * 86400
        backlog = db.one("SELECT COUNT(*) n FROM events WHERE camera_id=? AND start_ts < ? AND clip IS NOT NULL",
                         [cam_id, horizon])["n"]
        if not backlog:
            _prune_cursor.pop(cam_id, None)
            continue
        batch = event_media_batch(backlog, data_free, floor)
        if batch > EVENT_MEDIA_BATCH:
            stats["event_media_catch_up"] += 1
        cursor = _prune_cursor.get(cam_id, (float("-inf"), -1))
        cleared = scanned = 0
        while cleared < batch and scanned < 4 * batch:   # bounded: keep_windows costs a few queries per event
            rows = db.all("SELECT id, start_ts, end_ts FROM events WHERE camera_id=? AND start_ts < ? AND clip IS NOT NULL "
                          "AND (start_ts > ? OR (start_ts = ? AND id > ?)) ORDER BY start_ts, id LIMIT ?",
                          [cam_id, horizon, cursor[0], cursor[0], cursor[1], min(batch, 1000)])
            if not rows:
                cursor = (float("-inf"), -1)  # caught up: start over next pass
                break
            for e in rows:
                cursor = (e["start_ts"], e["id"])
                scanned += 1
                windows, _ = keep.keep_windows(cam, e["start_ts"], e["end_ts"] or e["start_ts"], policy)
                if windows:
                    continue
                if dry_run:
                    stats["dry_event_clips"] += 1
                    continue
                stats["freed_bytes"] += diskguard.delete_event_media(settings.data_dir / "events" / str(e["id"]))
                db.update_event(e["id"], clip=None)
                stats["event_clips_deleted"] += 1
                cleared += 1
        _prune_cursor[cam_id] = cursor


# ---------------------------------------------------------------- event-rate guard

class EventRateGuard:
    """A camera opening more than `event_rate_max_per_hour` events keeps opening them (snapshot, YOLO, alerts) but
    its clips and crops are not kept: one PTZ camera made an event every ~25 s and its clips filled the disk."""

    CACHE_S = 30.0   # recount a camera's last hour at most this often (one indexed COUNT)

    def __init__(self) -> None:
        self.counts: dict[str, tuple[float, int]] = {}   # camera -> (counted at, events in the last hour)
        self.last_log: dict[str, float] = {}
        self.suppressed: dict[str, dict] = {}            # camera -> {"since", "count"}: what the alert shows

    @staticmethod
    def decide(count: int, limit: int, last_log: float | None, now: float) -> tuple[bool, bool]:
        """(suppress clips, log now). Logs at most once an hour per camera."""
        if limit <= 0 or count <= limit:
            return False, False
        return True, last_log is None or now - last_log >= 3600

    def count(self, camera_id: str, now: float) -> int:
        at, n = self.counts.get(camera_id, (float("-inf"), 0))
        if now - at >= self.CACHE_S:
            n = db.one("SELECT COUNT(*) n FROM events WHERE camera_id=? AND start_ts >= ?", [camera_id, now - 3600])["n"]
            self.counts[camera_id] = (now, n)
        return n

    def check(self, camera_id: str, now: float | None = None) -> bool:
        """True when this camera's new event should keep its snapshot only."""
        now = now or time.time()
        limit = settings.event_rate_max_per_hour
        if limit <= 0:
            self.suppressed.clear()
            return False
        n = self.count(camera_id, now)
        suppress, say = self.decide(n, limit, self.last_log.get(camera_id), now)
        if not suppress:
            self.suppressed.pop(camera_id, None)
            return False
        self.suppressed.setdefault(camera_id, {"since": now})["count"] = n
        if say:
            self.last_log[camera_id] = now
            log.warning("%s: %s events in the last hour; clips suppressed (limit %s/h)", camera_id, f"{n:,}", limit)
        return True


rate_guard = EventRateGuard()


def drop_clip_media(event_id: int) -> None:
    """Keep only the snapshot of an event whose camera is over the rate limit."""
    diskguard.delete_event_media(settings.data_dir / "events" / str(event_id))
    db.update_event(event_id, clip=None)


def _update_alert() -> None:
    """One alert for Settings -> System (RetentionPanel shows `message`): the continuous window could not be held,
    the data disk is under 2x its floor (event clips are going early), or a camera's clips are suppressed."""
    global alert
    problems = []
    if _continuous_breach:
        problems.append(_continuous_breach["message"])
    dfloor, _ = data_floor()
    dfree = data_free_gb()
    if dfree < 2 * dfloor:
        problems.append(f"Event media disk is low: {dfree:.1f} GB free (floor {dfloor:.0f} GB); "
                        "the oldest event clips are being deleted early.")
    for cam_id, s in sorted(rate_guard.suppressed.items()):
        problems.append(f"{cam_id}: {s['count']:,} events in the last hour; clips suppressed "
                        f"(limit {settings.event_rate_max_per_hour:,}/h). Check the camera's analytics or zones.")
    if not problems:
        alert = None
        return
    base = _continuous_breach or {"free_gb": round(dfree, 1), "floor_gb": round(dfloor, 1)}
    alert = {"at": time.time(), "free_gb": base["free_gb"], "floor_gb": base["floor_gb"],
             "kind": "continuous" if _continuous_breach else "event_media",
             "message": " ".join(problems), "problems": problems}


def enforce() -> dict:
    global last_pass
    now = time.time()
    dry_run = settings.retention_dry_run
    stats: Counter = Counter()
    floor, _ = data_floor()
    # Short of space on the data disk: clear expired clips before the (slower) curation rather than after it.
    early = data_free_gb() < 1.5 * floor
    if early:
        prune_event_media(now, dry_run, stats)
    for cam in db.cameras():
        curate_camera(cam, now, dry_run, stats)
    enforce_disk_floor(dry_run, stats, now)
    if not early:
        prune_event_media(now, dry_run, stats)
    purge_masked(now, dry_run, stats)
    _update_alert()
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
    dd = shutil.disk_usage(settings.data_dir)
    return {"cameras": out, "disk": {"total_gb": round(du.total / 1e9), "free_gb": round(du.free / 1e9)},
            "data_disk": {"total_gb": round(dd.total / 1e9), "free_gb": round(dd.free / 1e9),
                          "floor_gb": round(data_floor()[0]), "same_as_recordings": same_volume()},
            "clips_suppressed": dict(rate_guard.suppressed),
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


def emergency_free() -> None:
    """Filesystem-first rescue for a data disk that is nearly full while running: a full disk makes every
    SQLite write fail, including the ones the normal pass needs, so delete files before touching the database."""
    diskguard.emergency_free(settings.data_dir, int(settings.disk_emergency_free_gb * 1e9))
    ids, diskguard.cleared[:] = list(diskguard.cleared), []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        db.execute(f"UPDATE events SET clip=NULL WHERE id IN ({','.join('?' * len(chunk))})", chunk)


async def retention_loop(interval: float = 600, retry: float = 60) -> None:
    # The first pass runs at startup: a box that came up just above empty gets back to its floor straight away.
    while True:
        ok = False
        try:
            await asyncio.to_thread(emergency_free)
            await asyncio.to_thread(enforce)
            ok = True
        except Exception:
            log.exception("retention pass failed")
        await asyncio.sleep(interval if ok else retry)   # failed (e.g. disk full): try again soon, not in 10 min
