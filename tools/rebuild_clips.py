"""Rebuild missing event clips from the recordings still on disk.

Usage (from backend/, with the site's .env in place):  ..\\.venv\\Scripts\\python.exe ..\\tools\\rebuild_clips.py [--days 5] [--dry-run]

For every verified event inside the recordings window whose clip.mp4 is gone (events.clip NULL), cut the clip
again from MediaMTX exactly as the pipeline does (start - clip_pre_roll .. end + clip_post_roll) and set the
clip column back. Snapshots and crops are untouched. Written after the Oct 5 2026 floor bug removed clips on the
desktop; safe to re-run, it only touches events without a clip.
"""
import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402
from nvr import mediamtx  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=5.0, help="only events newer than this many days (the recordings window)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--parallel", type=int, default=2)
    args = ap.parse_args()
    since = time.time() - args.days * 86400
    rows = db.all("SELECT id, camera_id, start_ts, end_ts FROM events WHERE clip IS NULL AND status='verified' "
                  "AND end_ts IS NOT NULL AND start_ts > ? ORDER BY start_ts DESC", [since])
    print(f"{len(rows)} verified events without a clip since {time.strftime('%Y-%m-%d %H:%M', time.localtime(since))}")
    sem = asyncio.Semaphore(args.parallel)
    done = failed = skipped = 0

    async def one(e: dict) -> None:
        nonlocal done, failed, skipped
        d = settings.data_dir / "events" / str(e["id"])
        clip = d / "clip.mp4"
        if clip.exists():
            db.update_event(e["id"], clip=str(clip.relative_to(settings.data_dir)))
            skipped += 1
            return
        start = e["start_ts"] - settings.clip_pre_roll
        dur = (e["end_ts"] + settings.clip_post_roll) - start
        if args.dry_run:
            done += 1
            return
        async with sem:
            try:
                d.mkdir(parents=True, exist_ok=True)
                await mediamtx.fetch_clip(e["camera_id"], start, dur, clip)
                if clip.exists() and clip.stat().st_size > 0:
                    db.update_event(e["id"], clip=str(clip.relative_to(settings.data_dir)))
                    done += 1
                else:
                    clip.unlink(missing_ok=True)
                    failed += 1
            except Exception as ex:  # a gap in the recordings or a camera that no longer exists: skip, keep going
                clip.unlink(missing_ok=True)
                failed += 1
                if failed <= 5:
                    print(f"  event {e['id']} {e['camera_id']}: {type(ex).__name__}: {str(ex)[:80]}")

    await asyncio.gather(*(one(dict(r)) for r in rows))
    print(f"rebuilt {done}, re-linked {skipped}, failed {failed}{' (dry run)' if args.dry_run else ''}")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    asyncio.run(main())
