"""Delete the per-event clips events no longer keep, where the recording still has the event.

    python -m nvr.clip_cleanup [--apply] [--camera CAM] [--min-age-days N]     (from backend/, or in an instance
                                                                                 container: docker exec ... python -m nvr.clip_cleanup)

Events stopped keeping a clip.mp4 on 2026-10-22: an event's video is its stretch of the continuous recording, which
retention keeps around events (keep.py), so a clip is a second copy. 2026-10-22 those copies filled a central
instance's 50 GB data dataset in a week and its database stopped taking writes.

For every event folder with a clip.mp4 (or other bulky media the snapshot and crops don't need: cached clip-chat
frames chat_*.jpg, leftover thumb_*.tmp) this checks that the camera's recording segments on disk cover the event's
span [start - pre-roll, end + post-roll] (continuous segments: start from the file name, end at the next segment or
the file's last write; kept footage: its kept_footage row). Only then are those files deleted; snapshot.jpg, wide.jpg,
crop_*.jpg, ppe.jpg and thumbnails are never touched. Events whose recording is gone (or has a gap in the span) keep
their clip: it is the only video they have. Files go first; then events.clip is set to NULL for the cleared events
(and for rows pointing at a clip that is already gone).

Dry run by default: prints what it would do. --apply deletes.
"""
from __future__ import annotations

import argparse
import bisect
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import settings
from .fmp4 import segment_start

BULKY = ("clip.mp4",)                 # always bulky
BULKY_PREFIX = (("chat_", ".jpg"), ("thumb_", ".tmp"))   # (prefix, suffix): regenerable caches and leftovers
GAP_TOLERANCE_S = 2.0                 # segment boundaries: MediaMTX's next segment starts a moment after the last ends


def bulky_files(d: Path) -> list[Path]:
    """The files in an event folder this tool may delete (never the snapshot, keyframes, crops, PPE picture)."""
    out = []
    try:
        for f in d.iterdir():
            n = f.name
            if n in BULKY or any(n.startswith(p) and n.endswith(s) for p, s in BULKY_PREFIX):
                out.append(f)
    except OSError:
        pass
    return out


def merge_intervals(spans: list[tuple[float, float]], gap: float = GAP_TOLERANCE_S) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for s, e in sorted(spans):
        if e <= s:
            continue
        if out and s <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def recorded_spans(camera_id: str, kept: dict[str, tuple[float, float]]) -> list[tuple[float, float]]:
    """What the camera's recording folder holds, as merged (start, end) epoch spans."""
    d = settings.recordings_dir / camera_id
    try:
        files = sorted((ts, f) for f in d.glob("*.mp4") if (ts := segment_start(f)))
    except OSError:
        return []
    spans = []
    for i, (start, f) in enumerate(files):
        k = kept.get(str(f))
        if k:
            spans.append(k)
            continue
        try:
            end = f.stat().st_mtime   # MediaMTX writes a segment until the next one opens: its last write is its end
        except OSError:
            continue
        if i + 1 < len(files):
            end = min(end, files[i + 1][0])
        spans.append((start, end))
    return merge_intervals(spans)


def covers(spans: list[tuple[float, float]], start: float, end: float) -> bool:
    """Do the merged spans hold all of [start, end] (within GAP_TOLERANCE_S at the edges)?"""
    if not spans:
        return False
    i = bisect.bisect_right([s for s, _ in spans], start + GAP_TOLERANCE_S) - 1
    return i >= 0 and spans[i][0] <= start + GAP_TOLERANCE_S and spans[i][1] >= end - GAP_TOLERANCE_S


@dataclass
class Report:
    folders: int = 0              # event folders with bulky media
    bytes_total: int = 0
    covered: int = 0              # recording covers the event: deletable
    covered_bytes: int = 0
    no_recording: int = 0         # kept: the recording (or part of the span) is gone
    no_recording_bytes: int = 0
    unfinished: int = 0           # kept: open / pending, or no end time yet
    orphans: int = 0              # a folder with no event row
    orphan_bytes: int = 0
    stale_rows: int = 0           # events.clip set, file already gone
    deleted: int = 0
    freed: int = 0
    nulled: int = 0
    null_failed: str | None = None
    ids: list[int] = field(default_factory=list)


def _size(files: list[Path]) -> int:
    total = 0
    for f in files:
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


def scan(db, camera: str | None = None, min_age_s: float = 0.0, apply: bool = False, now: float | None = None) -> Report:
    now = now or time.time()
    rep = Report()
    events_dir = settings.data_dir / "events"
    try:
        dirs = sorted((int(d.name), d) for d in events_dir.iterdir() if d.name.isdigit())
    except OSError:
        dirs = []
    found = [(i, d, files) for i, d in dirs if (files := bulky_files(d))]
    kept = {r["file"]: (r["start_ts"], r["end_ts"]) for r in db.all("SELECT file, start_ts, end_ts FROM kept_footage")}
    spans_by_cam: dict[str, list[tuple[float, float]]] = {}
    rows: dict[int, dict] = {}
    ids = [i for i, _, _ in found]
    for k in range(0, len(ids), 500):
        chunk = ids[k:k + 500]
        for r in db.all("SELECT id, camera_id, status, start_ts, end_ts, clip, clip_start FROM events "
                        f"WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            rows[r["id"]] = r
    cleared: list[int] = []
    for i, d, files in found:
        size = _size(files)
        r = rows.get(i)
        if r is None:
            rep.orphans += 1
            rep.orphan_bytes += size
            continue
        if camera and r["camera_id"] != camera:
            continue
        rep.folders += 1
        rep.bytes_total += size
        if r["status"] in ("open", "pending") or r["end_ts"] is None or r["start_ts"] > now - min_age_s:
            rep.unfinished += 1
            continue
        start = r["clip_start"] or r["start_ts"] - settings.clip_pre_roll
        end = r["end_ts"] + settings.clip_post_roll
        if r["camera_id"] not in spans_by_cam:
            spans_by_cam[r["camera_id"]] = recorded_spans(r["camera_id"], kept)
        if not covers(spans_by_cam[r["camera_id"]], start, end):
            rep.no_recording += 1
            rep.no_recording_bytes += size
            continue
        rep.covered += 1
        rep.covered_bytes += size
        if not apply:
            continue
        got = 0
        for f in files:   # files first: the space must not depend on the database taking a write
            try:
                n = f.stat().st_size
                f.unlink()
                got += n
            except OSError:
                pass
        if got:
            rep.deleted += 1
            rep.freed += got
        if r["clip"] is not None and not (d / "clip.mp4").exists():
            cleared.append(i)
    # rows that point at a clip already gone (the folder held nothing bulky, or was deleted)
    stale = [r["id"] for r in db.all("SELECT id, clip FROM events WHERE clip IS NOT NULL" +
                                     (" AND camera_id=?" if camera else ""), [camera] if camera else [])
             if not (settings.data_dir / r["clip"]).exists() and r["id"] not in cleared]
    rep.stale_rows = len(stale)
    if apply:
        todo = cleared + stale
        try:
            for k in range(0, len(todo), 500):
                chunk = todo[k:k + 500]
                db.execute(f"UPDATE events SET clip=NULL WHERE id IN ({','.join('?' * len(chunk))})", chunk)
            rep.nulled = len(todo)
        except sqlite3.Error as e:   # the files are gone; the rows can be fixed by running this again
            rep.null_failed = str(e)
        rep.ids = cleared
    return rep


class ReadOnlyDb:
    """The two reads scan() needs, on a read-only connection (nvr.db's Database writes on open: schema, migrations)."""

    def __init__(self, path: Path) -> None:
        self.conn = sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row

    def all(self, sql: str, params=()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def execute(self, sql: str, params=()):
        raise sqlite3.OperationalError("read-only (dry run)")


def _gb(b: int) -> str:
    return f"{b / 1e9:,.2f} GB"


def render(rep: Report, apply: bool) -> str:
    lines = [f"clip cleanup ({'APPLY' if apply else 'dry run'}): data {settings.data_dir}, recordings {settings.recordings_dir}",
             f"  event folders with a clip or other bulky media: {rep.folders:>7,}  {_gb(rep.bytes_total)}",
             f"  recording covers the event (deletable):         {rep.covered:>7,}  {_gb(rep.covered_bytes)}",
             f"  kept: recording gone or partial for the span:   {rep.no_recording:>7,}  {_gb(rep.no_recording_bytes)}",
             f"  kept: event open, pending or too new:           {rep.unfinished:>7,}",
             f"  folders with no event row (left alone):         {rep.orphans:>7,}  {_gb(rep.orphan_bytes)}",
             f"  rows pointing at a clip that is already gone:   {rep.stale_rows:>7,}"]
    if apply:
        lines.append(f"deleted the media of {rep.deleted:,} events, freed {_gb(rep.freed)}; "
                     f"events.clip set to NULL on {rep.nulled:,} rows")
        if rep.null_failed:
            lines.append(f"WARNING: the files are deleted but updating the rows failed ({rep.null_failed}); run again "
                         "once the database takes writes")
    else:
        lines.append("dry run: nothing deleted. Run again with --apply to delete.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m nvr.clip_cleanup", description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="delete (default: dry run, nothing changes)")
    ap.add_argument("--camera", help="only this camera's events")
    ap.add_argument("--min-age-days", type=float, default=0.0, help="only events older than this (default: all finished)")
    args = ap.parse_args(argv)
    if args.apply:
        from .db import db   # opens the database (after diskguard has made room if the disk is full)
    else:
        db = ReadOnlyDb(settings.data_dir / "nvr.db")   # a dry run changes nothing, not even on a full disk
    rep = scan(db, args.camera, args.min_age_days * 86400, args.apply)
    print(render(rep, args.apply))
    return 1 if rep.null_failed else 0


if __name__ == "__main__":
    sys.exit(main())
