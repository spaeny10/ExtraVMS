"""Dry run of the event verifier on a camera's past events: what would the CURRENT rules (boxless motion rule,
confidence / size floors, the Qwen confidence gate) have decided? Read-only: changes nothing anywhere.

Usage (from backend/, with the site's .env in place):
    ..\\.venv\\Scripts\\python.exe ..\\tools\\replay_verification.py --camera cam6 [--hours 48] [--data-dir E:\\NVR\\data]
        [--device cpu] [--limit 200] [--status verified] [--ids 15205,15064] [--quiet]

Per event it prints the old status (YOLO class and confidence) -> the new one and why, then totals. Each event is
re-verified from its stored clip (events/<id>/clip.mp4: the same frames, YOLO and the pre-roll frame the live
verifier uses, Verifier.verify(dry_run=True)); an event whose clip is gone is judged from the YOLO boxes stored in its
detections instead (no pre-roll frame then: an object that never moved does not count), marked "(stored boxes)".

Nothing is written: the site database is opened with ?mode=ro, the verifier writes no snapshot, keyframes, parked-spot
memory or re-ID in a dry run, and the NVR modules this script imports get a throwaway data / runtime / recordings
folder (their own scratch database), so not even the live server's folders are touched. YOLO runs on --device
(default cpu: the live server keeps its GPU to itself).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

BACKEND = Path(__file__).resolve().parents[1] / "backend"
JSON_COLS = ("path", "detections", "policy", "anomaly_json")


def open_ro(db_path: Path) -> sqlite3.Connection:
    """The site database, read-only (a write raises sqlite3.OperationalError)."""
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _row(r: sqlite3.Row) -> dict:
    e = dict(r)
    for k in JSON_COLS:
        if isinstance(e.get(k), str):
            try:
                e[k] = json.loads(e[k])
            except ValueError:
                e[k] = None
    return e


def ro_setting(conn: sqlite3.Connection, key: str, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", [key]).fetchone()
    try:
        return json.loads(r["value"]) if r and r["value"] is not None else default
    except ValueError:
        return default


def load_events(conn: sqlite3.Connection, camera: str, since: float, status: str | None, limit: int | None,
                ids: list[int] | None = None) -> list[dict]:
    if ids:   # just these events (any age)
        marks = ",".join("?" * len(ids))
        return [_row(r) for r in conn.execute(
            "SELECT id, camera_id, track_id, camera_class, start_ts, end_ts, path, status, yolo_class, yolo_conf, yolo_hits, "
            "detections, clip, clip_start, snapshot, ptz_preset, policy, anomaly FROM events WHERE camera_id=? AND id IN "
            f"({marks}) ORDER BY start_ts DESC", [camera, *ids])]
    q = ("SELECT id, camera_id, track_id, camera_class, start_ts, end_ts, path, status, yolo_class, yolo_conf, yolo_hits, "
         "detections, clip, clip_start, snapshot, ptz_preset, policy, anomaly FROM events WHERE camera_id=? AND start_ts>=? "
         "AND status IN ('verified', 'rejected')" + (" AND status=?" if status else "") + " ORDER BY start_ts DESC"
         + (f" LIMIT {int(limit)}" if limit else ""))
    return [_row(r) for r in conn.execute(q, [camera, since] + ([status] if status else []))]


def from_stored(e: dict, data_dir: Path) -> dict | None:
    """No clip: the boxless decision from the YOLO boxes stored per sample (no pre-roll frame, so only objects that
    moved count). None for an event that had camera boxes (nothing to re-decide without the frames)."""
    import cv2
    from nvr import ruleevents, verifier
    if not ruleevents.is_rule_event(e):
        return None
    samples = (e.get("detections") or {}).get("samples") or []
    snap = data_dir / (e.get("snapshot") or "")
    img = cv2.imread(str(snap)) if e.get("snapshot") and snap.is_file() else None
    if not samples or img is None:
        return None
    wh = img.shape[1::-1]
    frame_boxes = [(s["ts"], s.get("yolo") or []) for s in samples]
    label = e["camera_class"] if e["camera_class"] in verifier.LABEL_CLASSES else verifier.pick_label(frame_boxes)
    require_motion = verifier.settings.boxless_require_motion and label == "vehicle"
    matches, reason, _, _, single = verifier.boxless_matches(frame_boxes, None, wh, verifier.LABEL_CLASSES[label], require_motion,
                                                     check_size=label in verifier.settings.boxless_size_labels)
    need = (e.get("detections") or {}).get("needed") or 2
    hits = [m for m in matches if m]
    best = max(hits, key=lambda m: m["conf"], default=None)
    ok = len(hits) >= need or (single and len(hits) >= 1)
    return {"status": "verified" if ok else "rejected", "yolo_class": best["cls"] if best else None,
            "yolo_conf": best["conf"] if best else None, "yolo_hits": len(hits), "rejected": None if ok else reason,
            "stored": True}


def replay(conn: sqlite3.Connection, data_dir: Path, camera: str, since: float, verifier_obj, status: str | None = None,
           limit: int | None = None, out=print, per_event: bool = True, ids: list[int] | None = None) -> dict:
    """Re-verify the camera's events (dry run). Returns the totals; prints them (and one line per event) through `out`."""
    from nvr import parked
    from nvr.config import settings
    from nvr.pipeline import Pipeline

    cam = conn.execute("SELECT * FROM cameras WHERE id=?", [camera]).fetchone()
    cam = dict(cam) if cam else {"id": camera}
    for k in ("zones", "policies", "synopsis_labels"):
        if isinstance(cam.get(k), str):
            cam[k] = json.loads(cam[k])
    spots = ro_setting(conn, parked.key(camera), []) or []
    parked.load = lambda cid: list(spots) if cid == camera else []   # the site's parking spots, read-only
    floor = ro_setting(conn, "synopsis_min_conf")
    floor = float(floor) if isinstance(floor, (int, float)) else settings.synopsis_min_conf
    gate_self = SimpleNamespace(cameras={camera: cam})

    def gated(e: dict, new: dict) -> str | None:
        return Pipeline.synopsis_gate(gate_self, {**e, "camera_class": new.get("camera_class") or e["camera_class"],
                                                  "yolo_conf": new.get("yolo_conf")}, floor)

    events = load_events(conn, camera, since, status, limit, ids)
    totals = {"events": len(events), "skipped": 0, "old": {}, "moves": {}, "qwen": 0, "gated": 0, "stored": 0}
    out(f"{len(events)} {status or 'verified / rejected'} events on {camera} since {time.strftime('%Y-%m-%d %H:%M', time.localtime(since))}"
        f" | Qwen gate {floor:.0%}")
    for e in events:
        clip = data_dir / e["clip"] if e.get("clip") else None
        new = None
        try:
            if clip is not None and clip.is_file():
                clip_start = e.get("clip_start") or e["start_ts"] - settings.clip_pre_roll
                ev = {k: e[k] for k in ("id", "camera_id", "track_id", "camera_class", "path", "start_ts", "end_ts")}
                ev["path"] = ev["path"] or []
                new = verifier_obj.verify(ev, clip, clip_start, None if e.get("ptz_preset") else cam.get("zones"), dry_run=True)
            else:
                new = from_stored(e, data_dir)
        except Exception as ex:  # noqa: BLE001 - a broken clip: report it, keep going
            new = {"status": "error", "rejected": f"{type(ex).__name__}: {ex}"}
        if new is None:
            totals["skipped"] += 1
            if per_event:
                out(f"{e['id']:>7}  {_when(e)}  {e['camera_class']:<8} {_was(e):<22} ->  skipped (no clip, no stored boxes to judge)")
            continue
        totals["stored"] += bool(new.get("stored"))
        totals["old"][e["status"]] = totals["old"].get(e["status"], 0) + 1
        key = f"{e['status']} -> {new['status']}"
        totals["moves"][key] = totals["moves"].get(key, 0) + 1
        note = ""
        if new["status"] == "verified":
            g = gated(e, new)
            totals["gated" if g else "qwen"] += 1
            note = f"  [{g}]" if g else ""
        now = f"{new['status']}" + (f" {new.get('yolo_class')} {new['yolo_conf']:.0%}" if new.get("yolo_conf") is not None else "")
        why = new.get("rejected") or ""
        if per_event:
            out(f"{e['id']:>7}  {_when(e)}  {e['camera_class']:<8} {_was(e):<22} ->  {now:<22} {why}{note}"
                + ("  (stored boxes)" if new.get("stored") else ""))
    out("")
    out("Totals: " + ", ".join(f"{k}: {v}" for k, v in sorted(totals["moves"].items())))
    out(f"Still verified: {totals['qwen'] + totals['gated']} ({totals['qwen']} get a Qwen synopsis, {totals['gated']} gated "
        f"below {floor:.0%}) | skipped {totals['skipped']} | judged from stored boxes {totals['stored']}")
    return totals


def _when(e: dict) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(e["start_ts"]))


def _was(e: dict) -> str:
    return e["status"] + (f" {e['yolo_class']} {e['yolo_conf']:.0%}" if e.get("yolo_conf") is not None else "")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--camera", required=True)
    ap.add_argument("--hours", type=float, default=48.0)
    ap.add_argument("--data-dir", type=Path, default=None, help="the site's data folder (nvr.db, events/); default NVR_DATA_DIR")
    ap.add_argument("--device", default="cpu", help="YOLO device for the replay (cpu, cuda:0, ...)")
    ap.add_argument("--status", choices=("verified", "rejected"), default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--ids", type=lambda v: [int(x) for x in v.split(",") if x.strip()], default=None,
                    help="only these event ids (comma-separated), e.g. 15205,15064")
    ap.add_argument("--quiet", action="store_true", help="totals only")
    args = ap.parse_args(argv)

    # Resolve the site's data folder from its settings, then point the NVR modules at a throwaway folder BEFORE they
    # are imported (nvr.db opens <data_dir>/nvr.db read-write at import).
    sys.path.insert(0, str(BACKEND))
    if args.data_dir is None:
        from pydantic_settings import BaseSettings, SettingsConfigDict

        class _Site(BaseSettings):
            model_config = SettingsConfigDict(env_file=BACKEND.parent / ".env", env_prefix="NVR_", extra="ignore")
            data_dir: Path = BACKEND.parent / "data"
        args.data_dir = _Site().data_dir
    data_dir = args.data_dir.resolve()
    scratch = Path(tempfile.mkdtemp(prefix="nvr-replay-"))
    for k in ("NVR_DATA_DIR", "NVR_RUNTIME_DIR", "NVR_RECORDINGS_DIR"):
        os.environ[k] = str(scratch / k.split("_")[1].lower())
    os.environ["NVR_YOLO_DEVICE"] = args.device
    os.environ["NVR_HUB_ENABLED"] = "0"

    from nvr.verifier import Verifier
    conn = open_ro(data_dir / "nvr.db")
    v = Verifier()
    since = time.time() - args.hours * 3600
    replay(conn, data_dir, args.camera, since, v, args.status, args.limit, per_event=not args.quiet, ids=args.ids)


if __name__ == "__main__":
    main()
