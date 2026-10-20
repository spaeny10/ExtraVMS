"""One-off: fold the overlapping person events recorded before group merging existed into one event per activity
(merge.py groups), with the live pipeline's own rules and code.

    python -m nvr.merge_groups --since 2026-10-20 --camera cam1            # dry run: prints the groups, changes nothing
    python -m nvr.merge_groups --since 2026-10-20 --camera cam1 --apply    # merges them

Run from backend/ with the site's .env in place (the same database the NVR uses). Only verified person events nobody
has touched are considered (locked, rated or corrected events, masked, rejected and PPE violations never are); each
group's earliest event takes the others in (merge.apply) and goes back to `pending`. The running NVR picks it up within
half a minute (merge.request_reverify; at its next start otherwise), verifies the whole span again and describes it
once, inside the Site's AI hours or as they say (catch up when idle / skip). Events that ended in the last
`group_merge_max_s` + 5 minutes are left to the live pipeline.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time

from . import merge
from .config import settings
from .db import db

LIVE_MARGIN_S = 300.0   # on top of group_merge_max_s: an activity this recent may still be growing in the live pipeline


def parse_when(s: str) -> float:
    """'2026-10-20', '2026-10-20 15:30' or '2026-10-20T15:30:00' in local time, or epoch seconds."""
    try:
        return float(s)
    except ValueError:
        pass
    return dt.datetime.fromisoformat(s.strip().replace(" ", "T")).timestamp()


def _hms(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def plan(camera: dict, since: float, until: float) -> tuple[list[dict], int]:
    """The camera's activities with more than one event in [since, until] (merge.plan_groups on its verified events),
    and how many events it looked at."""
    events = [e for e in merge.group_events(camera["id"], since, until)
              if e["status"] == "verified" and e["start_ts"] >= since and e["end_ts"] <= until]
    return [g for g in merge.plan_groups(events, camera.get("zones")) if g["joins"]], len(events)


def run(since: float, until: float, camera_ids: list[str] | None, apply: bool, out=print) -> dict:
    """Print (and with apply, merge) every camera's groups. Returns {groups, merged, targets}."""
    cams = [c for c in db.cameras() if not camera_ids or c["id"] in camera_ids]
    missing = set(camera_ids or []) - {c["id"] for c in cams}
    if missing:
        out(f"unknown camera(s): {', '.join(sorted(missing))}")
    totals = {"groups": 0, "merged": 0, "targets": []}
    for cam in cams:
        groups, n = plan(cam, since, until)
        if not groups and not camera_ids:
            continue
        out(f"{cam['id']} {cam.get('name') or ''}: {n} person events, {len(groups)} group{'s' if len(groups) != 1 else ''} "
            f"of more than one")
        for g in groups:
            t = g["target"]
            out(f"  {t['id']}  {_hms(g['start'])}-{_hms(g['end'])} ({g['end'] - g['start']:.0f} s), {g['people']} "
                f"{'people' if g['people'] != 1 else 'person'} at once, {1 + len(g['joins'])} events: "
                + " ".join(str(j["id"]) for j, _ in g["joins"]))
            for j, why in g["joins"]:
                out(f"      {j['id']} {_hms(j['start_ts'])}-{_hms(j['end_ts'])}: {why}")
            totals["groups"] += 1
            if not apply:
                continue
            merged = 0
            for j, why in g["joins"]:
                a_now, b_now = db.event(t["id"]), db.event(j["id"])
                # the live NVR or an operator may have changed either since the plan: the same checks as the pipeline
                if not (a_now and b_now and a_now["status"] in ("verified", "pending") and b_now["status"] == "verified"
                        and merge.group_ok(a_now) and merge.group_ok(b_now)):
                    out(f"      {j['id']} skipped: changed since the plan")
                    continue
                merge.apply(a_now, b_now, why)
                merged += 1
            if merged:
                merge.request_reverify([t["id"]])
                totals["merged"] += merged
                totals["targets"].append(t["id"])
    if apply:
        out(f"merged {totals['merged']} events into {len(totals['targets'])}; the NVR verifies and describes them again "
            "(within half a minute while it runs, else at its next start)")
    else:
        out("dry run: nothing changed (add --apply to merge)")
    return totals


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m nvr.merge_groups", description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", required=True, help="local date/time, e.g. 2026-10-20 or '2026-10-20 15:30'")
    ap.add_argument("--until", help="local date/time (default: now, less the live pipeline's margin)")
    ap.add_argument("--camera", action="append", help="camera id (repeatable; default: every camera)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the groups only (the default)")
    mode.add_argument("--apply", action="store_true", help="merge them")
    args = ap.parse_args(argv)
    if settings.group_merge_max_s <= 0:
        print("group merging is off (NVR_GROUP_MERGE_MAX_S=0)")
        return 1
    live = time.time() - settings.group_merge_max_s - LIVE_MARGIN_S
    until = min(parse_when(args.until), live) if args.until else live
    since = parse_when(args.since)
    print(f"person events {dt.datetime.fromtimestamp(since):%Y-%m-%d %H:%M} .. {dt.datetime.fromtimestamp(until):%Y-%m-%d %H:%M}, "
          f"groups up to {settings.group_merge_max_s:.0f} s{'' if args.apply else ' (dry run)'}")
    run(since, until, args.camera, apply=bool(args.apply))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    sys.exit(main())
