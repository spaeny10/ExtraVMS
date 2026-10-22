"""Hand events that failed verification back to the running NVR, to verify again in the background.

    python -m nvr.reverify --status error                         # dry run: counts, changes nothing
    python -m nvr.reverify --status error --since 2026-10-15 --camera cam3 --limit 500 --apply
    python -m nvr.reverify --status error --any-error --apply     # every error, not only storage failures

(from backend/ with the site's .env, or in an instance container: docker exec ... python -m nvr.reverify ...)

2026-10-22 a central instance's data dataset filled up; every event verified while it was full ended in status
`error` ("[Errno 28] No space left on device", "database or disk is full", "disk I/O error"). Their recording is
still there, so they can simply be verified again. This lists the events in status `error` whose error text is a
storage / database failure (STORAGE_RX; --any-error: any error) and whose recording on disk covers their span
[start - pre-roll, end + post-roll] (clip_cleanup's coverage check; a legacy clip.mp4 counts too).

With --apply their ids go into the `reverify_queue` settings row. The running NVR (Pipeline.background_reverify_tick)
takes them one at a time, only while nothing else waits for verification and Qwen's queue is short, so live events
are never held up: each one goes back to `pending` (as the event viewer's Reprocess does) and through the normal
verification; its description then follows the Site's AI schedule as usual. Not running? They wait in the row, and
since they stay in `error` until taken, a restart doesn't flood the verifier with them.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field

from .config import settings

QUEUE_KEY = "reverify_queue"   # settings row: JSON array of event ids, oldest first
STORAGE_RX = re.compile(
    r"no space left|errno 28|enospc|disk i/o error|database or disk is full|disk is full|database is locked|"
    r"readonly database|read-only file system|errno 30|unable to open database|input/output error|errno 5\b|"
    r"disk full|quota exceeded|errno 122", re.I)


def is_storage_error(text: str | None) -> bool:
    return bool(text and STORAGE_RX.search(text))


# ---------------------------------------------------------------- the queue (settings row), shared with the pipeline

def queued(db) -> list[int]:
    rows = db.all("SELECT value FROM settings WHERE key=?", [QUEUE_KEY])
    if not rows or rows[0]["value"] is None:
        return []
    try:
        return [int(x) for x in json.loads(rows[0]["value"])]
    except (TypeError, ValueError):
        return []


def request(db, ids: list[int]) -> int:
    """Add these ids to the queue (once each, kept in id order) in one statement, so the NVR taking one meanwhile
    loses nothing. Returns how many ids the queue now holds."""
    if ids:
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ("
                   "SELECT json_group_array(v) FROM (SELECT value AS v FROM json_each(CASE WHEN json_valid(settings.value) "
                   "THEN settings.value ELSE '[]' END) UNION SELECT value FROM json_each(excluded.value) ORDER BY v))",
                   [QUEUE_KEY, json.dumps(sorted({int(i) for i in ids}))])
    return len(queued(db))


def take_next(db) -> int | None:
    """Remove and return the first queued id (None when the queue is empty). Compare-and-swap on the whole value, so
    an id request() adds meanwhile is never lost (the take simply waits for the next tick)."""
    row = db.one("SELECT value FROM settings WHERE key=?", [QUEUE_KEY])
    if not row:
        return None
    try:
        ids = json.loads(row["value"])
    except (TypeError, ValueError):
        ids = None
    if not isinstance(ids, list) or not ids:
        db.execute("DELETE FROM settings WHERE key=? AND value=?", [QUEUE_KEY, row["value"]])
        return None
    cur = db.execute("UPDATE settings SET value=json_remove(value, '$[0]') WHERE key=? AND value=?",
                     [QUEUE_KEY, row["value"]])
    if not cur.rowcount:
        return None
    try:
        return int(ids[0])
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- the scan

@dataclass
class Report:
    status: str = "error"
    matched: int = 0              # events in that status (and window / camera)
    eligible: int = 0             # recording covers them: handed back (with --apply)
    recording_missing: int = 0    # the recording (or part of the span) is gone: can't be verified again
    other_error: int = 0          # an error that isn't a storage failure (counted with --any-error)
    already_queued: int = 0       # waiting in the NVR's queue from an earlier run
    unfinished: int = 0           # no end time
    over_limit: int = 0           # eligible, past --limit
    already_fine: int = 0         # in the window / camera and already verified or rejected
    errors: dict[str, int] = field(default_factory=dict)   # the eligible ones' error texts (first 60 chars)
    ids: list[int] = field(default_factory=list)
    queue_len: int = 0


def scan(db, status: str = "error", since: float | None = None, camera: str | None = None, limit: int | None = None,
         any_error: bool = False, apply: bool = False) -> Report:
    from .clip_cleanup import covers, recorded_spans
    rep = Report(status=status)
    where, params = ["status=?"], [status]
    if since is not None:
        where.append("start_ts >= ?")
        params.append(since)
    if camera:
        where.append("camera_id = ?")
        params.append(camera)
    rows = db.all(f"SELECT id, camera_id, start_ts, end_ts, clip, clip_start, error FROM events WHERE {' AND '.join(where)} "
                  "ORDER BY start_ts, id", params)
    fine_where = ["status IN ('verified','rejected')"] + where[1:]
    rep.already_fine = db.all(f"SELECT COUNT(*) AS n FROM events WHERE {' AND '.join(fine_where)}", params[1:])[0]["n"]
    waiting = set(queued(db))
    kept = {r["file"]: (r["start_ts"], r["end_ts"]) for r in db.all("SELECT file, start_ts, end_ts FROM kept_footage")}
    spans: dict[str, list] = {}
    rep.matched = len(rows)
    for r in rows:
        if not any_error and not is_storage_error(r["error"]):
            rep.other_error += 1
            continue
        if r["id"] in waiting:
            rep.already_queued += 1
            continue
        if r["end_ts"] is None:
            rep.unfinished += 1
            continue
        start = r["clip_start"] or r["start_ts"] - settings.clip_pre_roll
        end = r["end_ts"] + settings.clip_post_roll
        if r["camera_id"] not in spans:
            spans[r["camera_id"]] = recorded_spans(r["camera_id"], kept)
        clip = settings.data_dir / (r["clip"] or "")
        if not (covers(spans[r["camera_id"]], start, end) or (r["clip"] and clip.is_file())):
            rep.recording_missing += 1
            continue
        if limit is not None and rep.eligible >= limit:
            rep.over_limit += 1
            continue
        rep.eligible += 1
        rep.ids.append(r["id"])
        key = (r["error"] or "")[:60]
        rep.errors[key] = rep.errors.get(key, 0) + 1
    if apply and rep.ids:
        rep.queue_len = request(db, rep.ids)
    else:
        rep.queue_len = len(waiting)
    return rep


def render(rep: Report, apply: bool, any_error: bool) -> str:
    what = "any error" if any_error else "a storage / database failure"
    row = lambda label, n: f"  {label:<46}{n:>7,}"
    lines = [f"reverify ({'APPLY' if apply else 'dry run'}): status '{rep.status}' with {what}; "
             f"data {settings.data_dir}, recordings {settings.recordings_dir}",
             row(f"events in status '{rep.status}':", rep.matched),
             row("eligible (recording covers the event):", rep.eligible),
             row("recording missing (can't verify again):", rep.recording_missing),
             row("already fine (verified / rejected):", rep.already_fine),
             row("already waiting in the NVR's queue:", rep.already_queued)]
    if not any_error:
        lines.append(row("other errors (not storage; --any-error):", rep.other_error))
    if rep.unfinished:
        lines.append(row("no end time (left alone):", rep.unfinished))
    if rep.over_limit:
        lines.append(row("eligible past --limit (next run):", rep.over_limit))
    if rep.errors:
        lines.append("  eligible by error:")
    for text, n in sorted(rep.errors.items(), key=lambda kv: -kv[1])[:8]:
        lines.append(f"    {n:>6,}  {text}")
    if apply:
        lines.append(f"handed {rep.eligible:,} events to the NVR ({rep.queue_len:,} waiting in its queue); it verifies them "
                     "one at a time while live events don't need the verifier")
    else:
        lines.append("dry run: nothing changed. Run again with --apply to hand them to the NVR.")
    return "\n".join(lines)


def parse_when(s: str) -> float:
    """'2026-10-20', '2026-10-20 15:30' or '2026-10-20T15:30:00' in local time, or epoch seconds (as merge_groups)."""
    try:
        return float(s)
    except ValueError:
        pass
    return dt.datetime.fromisoformat(s.strip().replace(" ", "T")).timestamp()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m nvr.reverify", description=__doc__.split("\n\n")[0])
    ap.add_argument("--status", default="error", choices=["error"], help="events in this status (default: error)")
    ap.add_argument("--since", help="only events that started at or after this (ISO date/time, local, or epoch)")
    ap.add_argument("--camera", help="only this camera's events")
    ap.add_argument("--limit", type=int, help="hand back at most this many (oldest first)")
    ap.add_argument("--any-error", action="store_true", help="any error, not only storage / database failures")
    ap.add_argument("--apply", action="store_true", help="hand them to the running NVR (default: dry run)")
    args = ap.parse_args(argv)
    if args.apply:
        from .db import db   # opens the database
    else:
        from .clip_cleanup import ReadOnlyDb
        db = ReadOnlyDb(settings.data_dir / "nvr.db")   # a dry run changes nothing
    since = parse_when(args.since) if args.since else None
    rep = scan(db, args.status, since, args.camera, args.limit, args.any_error, args.apply)
    print(render(rep, args.apply, args.any_error))
    return 0


if __name__ == "__main__":
    sys.exit(main())
