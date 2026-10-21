"""Merge back-to-back fragments of one visit on one camera into a single event before Qwen describes it.

A camera that loses and re-finds a person usually hands out a new ObjectId, and tracks longer than
`track_max_seconds` are split on purpose, so one visit often arrives as several short events. After a
fragment B is verified we look for the fragment A just before it on the same camera and merge B into A when
    - the gap is short (B starts within `track_merge_gap` of A ending),
    - same class, same PTZ view, A not too long already and untouched by an operator, and
    - the camera gave both the same track id, or B starts where A ended and (for people) the re-ID
      appearance embeddings agree.
A person who stands still is often dropped by the camera's analytics for a minute or two, so a longer gap
(up to `merge_long_gap`) also merges, for people only, when the recording proves they never left: YOLO is run
on a frame every `merge_gap_step_s` across the gap and must find a person where A ended / B started in every
one of them (`candidate_long` + `footage_check`).
The merged A goes back through verification as one span (clip, YOLO, keyframes, re-ID, cells) and is
described once. Every merge is logged with its reason so a wrong one is easy to spot.

Groups: people who work together (a crew in a warehouse aisle) each get their own camera track, so one activity used to
arrive as a dozen overlapping events, each verified and described on its own (T-304 cam1, 2026-10-20 15:37-15:45: 14
events in 8 minutes). Person events on one camera that overlap, or follow within `track_merge_gap`, are one activity:
`plan_groups` chains them (same class and PTZ view, the same include zone, nobody's touched them, no PPE violation of
their own) into the earliest one, up to `group_merge_max_s` from its start to its last end, and `apply` folds them in
the way fragments are folded, without re-ID agreement: different people, one activity. The event keeps its camera
tracks (`tracks`) and the most people in view at once (`people`), so the description can say "a group of 4 people".
A group keeps no re-ID vector: one fingerprint over different people would link or name the wrong person, so group
events take no part in cross-camera journeys or identity matching (links confirmed before it became a group stay).
Vehicles never group: each is described, fingerprinted and checked against the site rules on its own.
"""
from __future__ import annotations

import gc
import json
import logging
import math
import shutil
import time

import numpy as np

from . import cells, ruleevents, zones
from .config import settings
from .db import db

log = logging.getLogger("nvr.merge")
OVERLAP_S = 2.5   # the tracker closes a track after `track_end_gap` quiet seconds: B may start slightly "before" A ended


def _box_center(p: list) -> tuple[float, float]:
    _, l, t, r, b = p[:5]
    return ((l + r) / 2, (t + b) / 2)


def _overlap(p: list, q: list) -> bool:
    _, l1, t1, r1, b1 = p[:5]
    _, l2, t2, r2, b2 = q[:5]
    return not (r1 <= l2 or r2 <= l1 or b1 <= t2 or b2 <= t1)


def _last_box(path: list) -> list | None:
    """The last well-formed path point [ts, l, t, r, b, ...]; extended paths can carry shorter entries."""
    for p in reversed(path or []):
        if isinstance(p, (list, tuple)) and len(p) >= 5:
            return list(p)
    return None


def _first_box(path: list) -> list | None:
    for p in path or []:
        if isinstance(p, (list, tuple)) and len(p) >= 5:
            return list(p)
    return None


def continuous(a_path: list, b_path: list, max_dist: float | None = None) -> bool:
    """B's first box is where A's last box was (centers within max_dist, or overlapping)."""
    p, q = _last_box(a_path), _first_box(b_path)
    if p is None or q is None:
        return False
    if ruleevents.whole_frame(p[1:5]) or ruleevents.whole_frame(q[1:5]):
        return False   # no position (an ONVIF event not verified yet): nothing says it's the same object
    if _overlap(p, q):
        return True
    (x1, y1), (x2, y2) = _box_center(p), _box_center(q)
    return float(np.hypot(x2 - x1, y2 - y1)) <= (settings.merge_max_dist if max_dist is None else max_dist)


def reid_sim(a_id: int, b_id: int) -> float | None:
    va, vb = db.get_reid(a_id), db.get_reid(b_id)
    if va is None or vb is None:
        return None
    return float(np.dot(va, vb))


def untouched(e: dict) -> bool:
    """Nobody has corrected, rated or locked this event: it is still the pipeline's to reshape."""
    if e.get("feedback") or e.get("corrected_at"):
        return False
    return not db.one("SELECT 1 FROM locks WHERE event_id=?", [e["id"]])


def candidate(b: dict) -> tuple[dict, str] | None:
    """The event A that B continues, with the reason, or None."""
    if b["status"] != "verified" or b["end_ts"] is None:
        return None
    lo = b["start_ts"] - settings.track_merge_gap
    rows = db.all("SELECT id FROM events WHERE camera_id=? AND id!=? AND camera_class=? AND status IN ('verified','pending') "
                  "AND end_ts IS NOT NULL AND end_ts>=? AND start_ts<? ORDER BY end_ts DESC LIMIT 3",
                  [b["camera_id"], b["id"], b["camera_class"], lo, b["start_ts"]])
    for r in rows:
        a = db.event(r["id"])
        if not a:
            continue
        gap = b["start_ts"] - a["end_ts"]
        if gap < -OVERLAP_S or gap > settings.track_merge_gap:
            continue
        if (a.get("ptz_preset") or None) != (b.get("ptz_preset") or None):
            continue
        if b["end_ts"] - a["start_ts"] > settings.merge_max_seconds or not untouched(a):
            continue
        if a["track_id"] == b["track_id"]:
            return a, f"same track {a['track_id']}, gap {gap:.1f}s"
        if not continuous(a.get("path") or [], b.get("path") or []):
            continue
        if b["camera_class"] == "person":
            sim = reid_sim(a["id"], b["id"])
            if sim is None or sim < settings.merge_reid_min:
                continue
            return a, f"continuous, gap {gap:.1f}s, re-ID {sim:.2f}"
        return a, f"continuous, gap {gap:.1f}s"
    return None


def candidate_long(b: dict) -> dict | None:
    """People only: the event A that B may continue across a gap too long for `candidate` (track_merge_gap <
    gap <= merge_long_gap), passing every rule except the footage check, which the caller runs on the GPU."""
    if b["status"] != "verified" or b["end_ts"] is None or b["camera_class"] != "person":
        return None
    if settings.merge_long_gap <= settings.track_merge_gap:
        return None
    rows = db.all("SELECT id FROM events WHERE camera_id=? AND id!=? AND camera_class='person' AND status IN ('verified','pending') "
                  "AND end_ts IS NOT NULL AND end_ts>=? AND end_ts<? ORDER BY end_ts DESC LIMIT 5",
                  [b["camera_id"], b["id"], b["start_ts"] - settings.merge_long_gap, b["start_ts"] - settings.track_merge_gap])
    for r in rows:
        a = db.event(r["id"])
        if not a:
            continue
        gap = b["start_ts"] - a["end_ts"]
        if gap <= settings.track_merge_gap or gap > settings.merge_long_gap:
            continue
        if (a.get("ptz_preset") or None) != (b.get("ptz_preset") or None):
            continue
        if b["end_ts"] - a["start_ts"] > settings.merge_max_seconds or not untouched(a):
            continue
        if not continuous(a.get("path") or [], b.get("path") or []):
            continue
        sim = reid_sim(a["id"], b["id"])
        if sim is None or sim < settings.merge_reid_min:
            continue
        return a
    return None


GAP_IOU = 0.2           # a YOLO person box this close to where A ended / B started / in between counts as "still there"
FRAME_TOL_S = 10.0      # the decoded (keyframe) frame must be within this of the requested time


def gap_times(t0: float, t1: float) -> list[float]:
    """Sample times from t0 (A's last point) to t1 (B's first point), both included, every merge_gap_step_s
    (the step widens so there are never more than merge_gap_max_frames)."""
    if t1 <= t0:
        return [t0]
    cap = max(2, settings.merge_gap_max_frames)
    n = max(2, math.ceil((t1 - t0) / max(settings.merge_gap_step_s, 0.1)) + 1)
    n = min(n, cap)
    step = (t1 - t0) / (n - 1)
    return [t0 + i * step for i in range(n)]


def _near(box, ref) -> bool:
    """box and ref (both [l, t, r, b]) are the same standing person: overlap enough, or one's center in the other."""
    from .verifier import center_inside, iou
    return iou(box, ref) > GAP_IOU or center_inside(box, ref) or center_inside(ref, box)


def frame_person_boxes(camera_id: str, t: float, zone_list: list[dict] | None, model) -> list[list[float]] | None:
    """YOLO person boxes (normalized [l, t, r, b]) in the recorded frame at t, masked zones honored as in
    `Verifier.verify`; None when there is no recording at t. Blocking: call on the GPU executor."""
    import cv2
    from . import frames
    from .verifier import PERSON
    got = frames.preview_jpeg(camera_id, t, width=1280)
    if not got:
        return None
    jpeg, frame_t, _ = got
    if abs(frame_t - t) > FRAME_TOL_S:
        return None
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    zone_list = zones.normalize(zone_list)
    res = model.predict([zones.mask_frame(img, zone_list)], imgsz=settings.yolo_imgsz, conf=settings.yolo_conf,
                        device=settings.yolo_device, verbose=False, classes=sorted(PERSON))[0]
    boxes = [[float(v) for v in b] for b in res.boxes.xyxyn.tolist()]
    return [b for b in boxes if zones.allowed(zones.foot(b), zone_list)]


def footage_check(a: dict, b: dict, zone_list: list[dict] | None = None, model=None) -> str | None:
    """The merge reason if the recording shows a person where A ended / B started (or in between) in every
    sampled frame across the gap; None otherwise (including no recording). Blocking: run on the GPU executor."""
    p, q = _last_box(a.get("path") or []), _first_box(b.get("path") or [])
    if p is None or q is None:
        return None
    t0, t1 = p[0], q[0]
    times = gap_times(t0, t1)
    try:
        for t in times:
            boxes = frame_person_boxes(a["camera_id"], t, zone_list, model)
            if boxes is None:
                log.info("no long merge of %s into %s: no recording at %.0f", b["id"], a["id"], t)
                return None
            f = 0.0 if t1 <= t0 else min(1.0, max(0.0, (t - t0) / (t1 - t0)))
            mid = [p[i] + (q[i] - p[i]) * f for i in range(1, 5)]
            if not any(_near(bx, ref) for bx in boxes for ref in (p[1:5], q[1:5], mid)):
                log.info("no long merge of %s into %s: nobody there at %+.0fs of a %.0fs gap",
                         b["id"], a["id"], t - t0, t1 - t0)
                return None
    finally:
        gc.collect()  # PyAV frames are freed late (see verifier.grab_frames)
    return f"footage: person stayed in view for {b['start_ts'] - a['end_ts']:.0f} s ({len(times)} frames checked)"


def track_entry(e: dict, why: str | None = None) -> dict:
    """One camera track inside a merged event: where it came from and the span it covered."""
    end = e["end_ts"] if e.get("end_ts") is not None else e["start_ts"]
    return {"event": e["id"], "track": e.get("track_id"), "from": e["start_ts"], "to": end, **({"why": why} if why else {})}


def tracks_of(e: dict) -> list[dict]:
    """The camera tracks an event is made of (itself, until something was merged into it)."""
    return list(e.get("tracks") or []) or [track_entry(e)]


def people_at_once(tracks: list[dict]) -> int:
    """The most tracks in view at the same moment. The tracker closes a track `track_end_gap` after the camera lost
    the person and the camera often re-finds them under a new id at once, so an overlap of up to OVERLAP_S is a
    hand-off of one person, not a second one (each span is shortened by that much; a short track still counts)."""
    marks = []
    for m in tracks:
        s = float(m["from"])
        t = max(s + 1e-3, float(m["to"]) - OVERLAP_S)
        marks += [(s, 1), (t, -1)]
    best = n = 0
    for _, d in sorted(marks):   # at the same instant an end comes before a start: a hand-off is not two people
        n += d
        best = max(best, n)
    return max(1, best)


def _plate_of(a: dict, b: dict) -> dict:
    """The plate columns A keeps when B folds into it: B's when only B has one or B's is more confident."""
    rank = {"medium": 1, "high": 2}
    if b.get("plate") and (not a.get("plate") or rank.get(b.get("plate_conf") or "", 0) > rank.get(a.get("plate_conf") or "", 0)):
        return {k: b.get(k) for k in ("plate", "plate_raw", "plate_region", "plate_conf")}
    return {}


def apply(a: dict, b: dict, reason: str = "") -> dict:
    """Fold B into A: one span, one path; A goes back to `pending` for a fresh verification; B is gone.
    The path is kept in time order (a group's tracks overlap); A keeps B's tracks in `tracks` and, for people,
    the most in view at once in `people`. A group of people loses its re-ID vector (one fingerprint would mix them)."""
    path = sorted((a.get("path") or []) + (b.get("path") or []),
                  key=lambda p: p[0] if isinstance(p, (list, tuple)) and p else 0)
    rules = (a.get("rules") or []) + (b.get("rules") or [])
    tracks = sorted(tracks_of(a) + [{**m, "why": m.get("why") or reason} for m in tracks_of(b)],
                     key=lambda m: (m["from"], m["event"]))
    n = people_at_once(tracks) if a["camera_class"] == "person" else 1
    people = n if n >= 2 else None
    db.update_event(a["id"], end_ts=max(a["end_ts"] or a["start_ts"], b["end_ts"]), path=path, rules=rules,
                    cells=cells.for_event(path), status="pending", tracks=tracks, people=people,
                    camera_conf=max(a.get("camera_conf") or 0, b.get("camera_conf") or 0),
                    synopsis=None, synopsis_json=None, threat=None, error=None, detections=None, snapshot=None, clip=None,
                    areas=None, anomaly=None, anomaly_json=None, priority=None, policy=None, watched=None,
                    # one vehicle: a plate either fragment had read stays (the more confident); the merged event is
                    # described again, and that reads the plate only when it has none (plate.py)
                    **_plate_of(a, b))
    partners = [r["x"] for r in db.all("SELECT CASE WHEN a=? THEN b ELSE a END AS x FROM event_links WHERE (a=? OR b=?) "
                                       "AND status='confirmed'", [b["id"], b["id"], b["id"]])]
    with db.lock:
        db.conn.execute("UPDATE locks SET event_id=? WHERE event_id=?", (a["id"], b["id"]))
        db.conn.execute("UPDATE chat_messages SET event_id=? WHERE event_id=?", (a["id"], b["id"]))
        db.conn.execute("DELETE FROM event_links WHERE a=? OR b=?", (b["id"], b["id"]))
    if people:
        db.execute("DELETE FROM reid_vec WHERE rowid=?", [a["id"]])
    db.delete_event(b["id"])
    shutil.rmtree(settings.data_dir / "events" / str(b["id"]), ignore_errors=True)  # event_dir() would recreate it
    if partners:   # B was on a cross-camera journey: its other events' journey is rebuilt without it
        from . import journeys
        for x in set(partners) - {a["id"]}:
            journeys.rebuild(x)
    log.info("merged event %s into %s on %s (%s): %.0fs -> %.0fs%s", b["id"], a["id"], a["camera_id"], reason,
             (a["end_ts"] or a["start_ts"]) - a["start_ts"], max(a["end_ts"] or a["start_ts"], b["end_ts"]) - a["start_ts"],
             f", {people} people at once" if people else "")
    return db.event(a["id"])


# ---------------------------------------------------------------- groups: one activity, several people

GROUP_CLASSES = ("person",)   # vehicles stay one per vehicle: plates, fingerprints, parked spots and towing rules are per vehicle
REVERIFY_KEY = "merge_reverify"   # settings row: events the backfill tool merged, for the running NVR to verify again


def group_ok(e: dict | None) -> bool:
    """May this event be part of a group: a closed person event nobody has touched, without a PPE violation of its own
    (a person breaking a PPE rule keeps their own event: its marked frame, priority and alert are about them)."""
    if not e or e.get("camera_class") not in GROUP_CLASSES or e.get("end_ts") is None:
        return False
    if (((e.get("detections") or {}).get("ppe")) or {}).get("verdict") == "violation":
        return False
    return untouched(e)


def joinable(e: dict) -> bool:
    """May join another event's group now: verified, or a merged group waiting to be verified again (made of verified
    events; it is verified again as part of the bigger one)."""
    return e["status"] == "verified" or (e["status"] == "pending" and len(e.get("tracks") or []) > 1)


def zone_set(e: dict, zone_list: list[dict] | None) -> frozenset | None:
    """The camera's include zones the object stood in (YOLO's matched boxes and the camera's own boxes); None when
    the whole view counts (no include zones) or the PTZ camera was turned away (zones describe the home view)."""
    zl = zones.normalize(zone_list)
    inc = [i for i, z in enumerate(zl) if z["type"] == "include"]
    if not inc or e.get("ptz_preset"):
        return None
    feet = [zones.foot(s["match"]["box"]) for s in (e.get("detections") or {}).get("samples") or [] if s.get("match")]
    feet += [zones.foot(p[1:5]) for p in e.get("path") or []
             if isinstance(p, (list, tuple)) and len(p) >= 5 and not ruleevents.whole_frame(p[1:5])]
    return frozenset(i for i in inc if any(zones.point_in_polygon(x, y, zl[i]["points"]) for x, y in feet))


def same_activity(a: dict, b: dict, zone_list: list[dict] | None) -> bool:
    """Same camera, class and PTZ view, and (cameras with include zones) the same include zone."""
    if a["camera_id"] != b["camera_id"] or a["camera_class"] != b["camera_class"]:
        return False
    if (a.get("ptz_preset") or None) != (b.get("ptz_preset") or None):
        return False
    za, zb = zone_set(a, zone_list), zone_set(b, zone_list)
    return za is None or zb is None or bool(za & zb)


def plan_groups(events: list[dict], zone_list: list[dict] | None, cap: float | None = None) -> list[dict]:
    """Chain one camera's events into activities: [{target, joins: [(event, reason)], start, end, people}], every
    event in exactly one (a lone event is a group of one). In start order, an event joins the latest group of the same
    activity it overlaps (or starts within track_merge_gap after); if that would take the group past `cap` from its
    start, it starts the next group instead: a busy camera yields one event per `cap` of activity, never a day-long
    one. An event not verified yet never joins (it joins once it is; `joinable`) but may lead a group."""
    cap = settings.group_merge_max_s if cap is None else cap
    gap = settings.track_merge_gap
    groups: list[dict] = []
    for e in sorted(events, key=lambda x: (x["start_ts"], x["id"])):
        end = e["end_ts"] if e["end_ts"] is not None else e["start_ts"]
        home = next((g for g in reversed(groups)
                     if same_activity(g["target"], e, zone_list) and e["start_ts"] <= g["end"] + gap), None)
        if home is not None and max(home["end"], end) - home["start"] <= cap:
            if not joinable(e):
                continue   # not verified yet: it joins once it is
            # the member it overlaps most (or the one that ended last before it) names the reason
            over = max(home["spans"], key=lambda s: (min(s[2], end) - e["start_ts"], s[2]))
            o = min(over[2], end) - e["start_ts"]
            why = f"group: overlaps {over[0]} by {o:.0f} s" if o > 0 else f"group: starts {-o:.0f} s after {over[0]} ended"
            home["joins"].append((e, why))
            home["spans"].append((e["id"], e["start_ts"], end))
            home["end"] = max(home["end"], end)
            continue
        groups.append({"target": e, "joins": [], "start": e["start_ts"], "end": end, "spans": [(e["id"], e["start_ts"], end)]})
    for g in groups:
        g["people"] = people_at_once([m for x in [g["target"], *(j for j, _ in g["joins"])] for m in tracks_of(x)])
    return groups


def group_events(camera_id: str, lo: float, hi: float) -> list[dict]:
    """The camera's events between lo and hi that may group (verified, or pending again after a merge)."""
    rows = db.all(f"SELECT id FROM events WHERE camera_id=? AND camera_class IN ({','.join('?' * len(GROUP_CLASSES))}) "
                  "AND status IN ('verified','pending') AND end_ts IS NOT NULL AND end_ts>=? AND start_ts<=? ORDER BY start_ts",
                  [camera_id, *GROUP_CLASSES, lo, hi])
    return [e for r in rows if group_ok(e := db.event(r["id"]))]


def group_for(b: dict, zone_list: list[dict] | None) -> tuple[dict, list[tuple[dict, str]]] | None:
    """The just-verified event B's activity on its camera, when B shares it with other events: (target, joins), the
    target being the activity's earliest event and joins every other joinable event of it (B among them, unless B is
    the target). None when B is alone, may not group, or group merging is off."""
    if settings.group_merge_max_s <= 0 or b["status"] != "verified" or not group_ok(b):
        return None
    cap = settings.group_merge_max_s
    events = group_events(b["camera_id"], b["start_ts"] - cap, b["end_ts"] + cap)
    for g in plan_groups(events, zone_list, cap):
        ids = {g["target"]["id"], *(j["id"] for j, _ in g["joins"])}
        if b["id"] in ids:
            return (g["target"], g["joins"]) if g["joins"] else None
    return None


def group_waiting(e: dict, now: float | None = None, zone_list: list[dict] | None = None) -> bool:
    """Should this event's description (or its verification after a merge) wait: another person event on its camera
    and view that may still join its activity is open (being tracked) or pending (being verified) and overlaps it, or
    starts within track_merge_gap after it, and the activity could still fit in group_merge_max_s. Never for longer
    than that after the event ended, so a busy camera's description is never held for ever. A pending one must be
    of the same activity (include zone; group_ok); an open one's path isn't complete yet, so it always counts."""
    cap = settings.group_merge_max_s
    if cap <= 0 or not group_ok(e):
        return False
    now = time.time() if now is None else now
    end = e["end_ts"]
    if now - end > cap:
        return False
    gap = settings.track_merge_gap
    for r in db.all("SELECT id, status, start_ts, end_ts, ptz_preset FROM events WHERE camera_id=? AND id!=? AND camera_class=? "
                    "AND status IN ('open','pending') AND start_ts<=? AND (end_ts IS NULL OR end_ts>=?)",
                    [e["camera_id"], e["id"], e["camera_class"], end + gap, e["start_ts"] - gap]):
        if (r["ptz_preset"] or None) != (e.get("ptz_preset") or None):
            continue
        if max(end, r["end_ts"] if r["end_ts"] is not None else now) - min(e["start_ts"], r["start_ts"]) > cap:
            continue
        if r["status"] == "pending":
            o = db.event(r["id"])
            # two merged groups side by side in different zones must not wait for each other
            if not (o and group_ok(o) and same_activity(e, o, zone_list)):
                continue
        return True
    return False


def request_reverify(ids: list[int]) -> None:
    """Ask the running NVR to verify these events again (its hold loop takes them, take_reverify_requests; being
    pending, recover() picks them up at its next start anyway). One statement per id: a concurrent take loses none."""
    for i in ids:
        db.execute("INSERT INTO settings (key, value) VALUES (?, json_array(?)) "
                   "ON CONFLICT(key) DO UPDATE SET value=json_insert(value, '$[#]', ?)", [REVERIFY_KEY, int(i), int(i)])


def take_reverify_requests() -> list[int]:
    """The ids the backfill tool asked to verify again, removed from the request (only if nothing was added meanwhile:
    the rest is taken next time)."""
    row = db.one("SELECT value FROM settings WHERE key=?", [REVERIFY_KEY])
    if not row or row["value"] is None:
        return []
    cur = db.execute("DELETE FROM settings WHERE key=? AND value=?", [REVERIFY_KEY, row["value"]])
    if not cur.rowcount:
        return []
    try:
        return [int(x) for x in json.loads(row["value"])]
    except (TypeError, ValueError):
        return []
