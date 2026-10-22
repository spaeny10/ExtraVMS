"""Shrink the full-resolution event snapshots stored before 2026-10-22 in place.

    python -m nvr.snapshot_shrink [--apply] [--camera CAM]     (from backend/, or in an instance container:
                                                                docker exec ... python -m nvr.snapshot_shrink)

New snapshots are at most `snapshot_max_px` (1280) on the long side at JPEG `snapshot_quality` (80), ~150-250 KB
(snapshots.py). The older ones are the full camera frame (2592 px, ~1 MB): on a central instance 48,929 of them took
47.6 GB of its data dataset. This rewrites every snapshot.jpg bigger than that at the new size, except:

- an event that is kept as evidence (locked, operator feedback / a correction / a saved note, or a specific keep-rule
  reason: a weapon check, high priority or threat, a broken site rule, a watch-list match, a PPE violation, a read
  plate: keep.event_reasons) AND whose video is gone (the recording no longer covers its span and it has no legacy
  clip.mp4): its snapshot may be the only full-resolution picture of it, so it stays as it is;
- events still open or pending (the pipeline may be writing the snapshot).

Thumbnails (thumb_*.jpg) are left alone: they are already small. A dry run (the default) reads the database
read-only, counts, and estimates the saving by re-encoding a sample in memory; --apply writes each file beside the
original and renames it over it.
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from . import keep, snapshots
from .clip_cleanup import ReadOnlyDb, covers, recorded_spans
from .config import settings

SAMPLE = 40   # dry run: snapshots re-encoded in memory to estimate the saving


@dataclass
class Report:
    snapshots: int = 0            # event folders with a snapshot.jpg
    bytes_total: int = 0
    small: int = 0                # already at most snapshot_max_px
    to_shrink: int = 0
    to_shrink_bytes: int = 0
    kept_full: int = 0            # kept event whose recording is gone: left at full size
    kept_full_bytes: int = 0
    unfinished: int = 0           # open / pending
    orphans: int = 0              # a folder with no event row (left alone)
    unreadable: int = 0
    shrunk: int = 0
    before: int = 0               # bytes of the files rewritten (apply), or of the sample (dry run)
    after: int = 0
    sampled: int = 0
    ids: list[int] = field(default_factory=list)

    def saved(self) -> int:
        """Bytes saved (apply), or estimated from the sample (dry run)."""
        if self.shrunk:
            return self.before - self.after
        if not self.sampled or not self.before:
            return 0
        return int(self.to_shrink_bytes * (1 - self.after / self.before))


def image_size(f: Path) -> tuple[int, int] | None:
    """(width, height) from the JPEG header, without decoding."""
    from PIL import Image
    try:
        with Image.open(f) as im:
            return im.size
    except Exception:  # noqa: BLE001 - damaged: counted as unreadable
        return None


def shrink_bytes(f: Path) -> bytes | None:
    img = cv2.imdecode(np.fromfile(str(f), np.uint8), cv2.IMREAD_COLOR)
    return None if img is None else snapshots.encode(img)


def shrink_file(f: Path) -> tuple[int, int] | None:
    """Rewrite one snapshot at the new size; (bytes before, after), or None when it can't be read."""
    before = f.stat().st_size
    data = shrink_bytes(f)
    if data is None:
        return None
    if len(data) >= before:   # a tiny or very plain picture: the original is already smaller
        return before, before
    tmp = f.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(f)
    return before, len(data)


LOCKED_SQL = "EXISTS(SELECT 1 FROM locks WHERE locks.event_id = events.id)"   # db.LOCKED_SQL (nvr.db opens the database)


def _rows(db, ids: list[int]) -> dict[int, dict]:
    out = {}
    for k in range(0, len(ids), 500):
        chunk = ids[k:k + 500]
        for r in db.all(f"SELECT {keep.EVENT_COLS}, camera_id, clip, clip_start, {LOCKED_SQL} AS locked FROM events "
                        f"WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            out[r["id"]] = r
    return out


def _policies(db) -> tuple[dict, dict[str, dict]]:
    import json
    stored = None
    row = next(iter(db.all("SELECT value FROM settings WHERE key='retention_policy'")), None)
    if row and row["value"]:
        try:
            stored = json.loads(row["value"])
        except ValueError:
            stored = None
    site = keep._merge(keep.DEFAULT_POLICY, stored or {})
    cams = {}
    for c in db.all("SELECT id, retention_policy FROM cameras"):
        try:
            override = json.loads(c["retention_policy"]) if c.get("retention_policy") else None
        except ValueError:
            override = None
        cams[c["id"]] = keep._merge(site, override)
    return site, cams


def kept(db, e: dict, policy: dict, noted: bool) -> bool:
    """Evidence worth a full-resolution picture: locked (the event, or a lock over its time), operator feedback / a
    correction / a saved note, or one of the keep rules' specific reasons (keep.event_reasons: a weapon check, high
    priority or threat, a broken site rule, a watch-list match, a PPE violation, a read plate). Not the generic ones
    (any verified person, any synopsis): those are most events, and the recording kept around them has them."""
    if e.get("locked") or e.get("feedback") or e.get("corrected_at") or noted:
        return True
    if db.all("SELECT 1 FROM locks WHERE camera_id=? AND start_ts < ? AND end_ts > ? LIMIT 1",
              [e["camera_id"], e["end_ts"] or e["start_ts"], e["start_ts"]]):
        return True
    return bool(keep.event_reasons(e, policy["keep"]))


def has_video(e: dict, spans: list[tuple[float, float]]) -> bool:
    clip = settings.data_dir / (e.get("clip") or "")
    if e.get("clip") and clip.is_file():
        return True
    start = e.get("clip_start") or e["start_ts"] - settings.clip_pre_roll
    end = (e["end_ts"] or e["start_ts"]) + settings.clip_post_roll
    return covers(spans, start, end)


def scan(db, camera: str | None = None, apply: bool = False, sample: int = SAMPLE, out=None) -> Report:
    rep = Report()
    events_dir = settings.data_dir / "events"
    try:
        dirs = sorted((int(d.name), d) for d in events_dir.iterdir() if d.name.isdigit())
    except OSError:
        dirs = []
    found = [(i, d / "snapshot.jpg") for i, d in dirs if (d / "snapshot.jpg").is_file()]
    rows = _rows(db, [i for i, _ in found])
    site, cam_policies = _policies(db)
    noted = keep.saved_notes(db, list(rows))
    kept_rows = {r["file"]: (r["start_ts"], r["end_ts"]) for r in db.all("SELECT file, start_ts, end_ts FROM kept_footage")}
    spans: dict[str, list] = {}
    todo: list[tuple[int, Path, int]] = []
    max_px = settings.snapshot_max_px
    for i, f in found:
        r = rows.get(i)
        if r is None:
            rep.orphans += 1
            continue
        if camera and r["camera_id"] != camera:
            continue
        try:
            size = f.stat().st_size
        except OSError:
            continue
        rep.snapshots += 1
        rep.bytes_total += size
        if r["status"] in ("open", "pending"):
            rep.unfinished += 1
            continue
        wh = image_size(f)
        if wh is None:
            rep.unreadable += 1
            continue
        if max(wh) <= max_px:
            rep.small += 1
            continue
        if kept(db, r, cam_policies.get(r["camera_id"], site), i in noted):
            if r["camera_id"] not in spans:
                spans[r["camera_id"]] = recorded_spans(r["camera_id"], kept_rows)
            if not has_video(r, spans[r["camera_id"]]):
                rep.kept_full += 1
                rep.kept_full_bytes += size
                continue
        rep.to_shrink += 1
        rep.to_shrink_bytes += size
        todo.append((i, f, size))
    if not apply:
        for i, f, size in random.Random(0).sample(todo, min(sample, len(todo))):
            data = shrink_bytes(f)
            if data is not None:
                rep.sampled += 1
                rep.before += size
                rep.after += min(size, len(data))
        return rep
    t0 = time.time()
    for n, (i, f, _) in enumerate(todo, 1):
        try:
            got = shrink_file(f)
        except OSError:
            got = None
        if got is None:
            rep.unreadable += 1
            continue
        rep.shrunk += 1
        rep.before += got[0]
        rep.after += got[1]
        rep.ids.append(i)
        if out and n % 1000 == 0:
            out(f"  {n:,} of {len(todo):,} ({(rep.before - rep.after) / 1e9:,.2f} GB saved, {time.time() - t0:,.0f} s)")
    return rep


def _gb(b: int) -> str:
    return f"{b / 1e9:,.2f} GB"


def render(rep: Report, apply: bool) -> str:
    lines = [f"snapshot shrink ({'APPLY' if apply else 'dry run'}): data {settings.data_dir}, to at most "
             f"{settings.snapshot_max_px} px, JPEG quality {settings.snapshot_quality}",
             f"  event snapshots:                                  {rep.snapshots:>7,}  {_gb(rep.bytes_total)}",
             f"  already at most {settings.snapshot_max_px} px:                         {rep.small:>7,}",
             f"  to shrink:                                        {rep.to_shrink:>7,}  {_gb(rep.to_shrink_bytes)}",
             f"  kept at full size (kept event, video gone):       {rep.kept_full:>7,}  {_gb(rep.kept_full_bytes)}",
             f"  skipped: event open or pending:                   {rep.unfinished:>7,}",
             f"  skipped: unreadable picture:                      {rep.unreadable:>7,}",
             f"  folders with no event row (left alone):           {rep.orphans:>7,}"]
    if apply:
        lines.append(f"shrank {rep.shrunk:,} snapshots: {_gb(rep.before)} -> {_gb(rep.after)}, saved {_gb(rep.saved())}")
    else:
        avg = rep.after / rep.sampled / 1e3 if rep.sampled else 0
        lines.append(f"estimated saving: {_gb(rep.saved())} (from {rep.sampled} sampled; ~{avg:,.0f} KB each after)")
        lines.append("dry run: nothing changed. Run again with --apply to shrink.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m nvr.snapshot_shrink", description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="rewrite the snapshots (default: dry run, nothing changes)")
    ap.add_argument("--camera", help="only this camera's events")
    args = ap.parse_args(argv)
    if args.apply:
        from .db import db   # opens the database (after diskguard has made room if the disk is full)
    else:
        db = ReadOnlyDb(settings.data_dir / "nvr.db")   # a dry run changes nothing
    rep = scan(db, args.camera, args.apply, out=print)
    print(render(rep, args.apply))
    return 0


if __name__ == "__main__":
    sys.exit(main())
