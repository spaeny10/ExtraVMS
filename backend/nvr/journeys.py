"""Cross-camera journeys: link the same person across neighbouring cameras.

1. Topology (camera_links): which cameras neighbour each other and the walking-time window between them.
2. Each verified person event gets a re-ID appearance embedding (reid.py, stored in reid_vec).
3. Candidate search: person events on a neighbouring camera inside the time window, in either direction,
   whose re-ID similarity is at least REID_MIN_SIM; the best candidate per neighbour and direction is kept.
4. Qwen confirms "same person?" from one crop of each; the verdict is stored so a pair is never asked twice.
5. Confirmed links are joined (connected components) into a journey; after QUIET_S without changes Qwen
   writes one narrative for the whole journey, which is added to each member's search document.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import statistics
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

from . import synopsis as vlm
from .config import settings
from .db import db

log = logging.getLogger("nvr.journeys")

REID_MIN_SIM = 0.70       # measured on this site: same person ~0.8-0.9, different events median ~0.65
SUGGEST_MIN_SIM = 0.78
SUGGEST_WINDOW_S = (-30.0, 180.0)
QUIET_S = 90
MAX_NARRATIVE_IMAGES = 4


# ---------------------------------------------------------------- topology

def topology() -> list[dict]:
    return db.all("SELECT cam_a, cam_b, min_s, max_s, one_way FROM camera_links ORDER BY cam_a, cam_b")


def set_topology(links: list[dict]) -> None:
    with db.lock:
        db.conn.execute("BEGIN")
        try:
            db.conn.execute("DELETE FROM camera_links")
            for l in links:
                a, b = l["cam_a"], l["cam_b"]
                if a == b:
                    continue
                lo, hi = sorted((float(l["min_s"]), float(l["max_s"])))
                db.conn.execute("INSERT OR REPLACE INTO camera_links (cam_a, cam_b, min_s, max_s, one_way) VALUES (?,?,?,?,?)",
                                [a, b, lo, hi, int(bool(l.get("one_way")))])
            db.conn.execute("COMMIT")
        except Exception:
            db.conn.execute("ROLLBACK")
            raise


def edges() -> list[tuple[str, str, float, float]]:
    """Directed edges (from camera, to camera, min gap s, max gap s)."""
    out = []
    for l in topology():
        out.append((l["cam_a"], l["cam_b"], l["min_s"], l["max_s"]))
        if not l["one_way"]:
            out.append((l["cam_b"], l["cam_a"], l["min_s"], l["max_s"]))
    return out


# ---------------------------------------------------------------- candidates

def _person_events_between(cam: str, col: str, lo: float, hi: float) -> list[dict]:
    return db.all(f"SELECT id, camera_id, start_ts, COALESCE(end_ts, start_ts) AS end_ts FROM events "
                  f"WHERE camera_id=? AND camera_class='person' AND status='verified' AND {col} BETWEEN ? AND ?",
                  [cam, lo, hi])


def candidates(event: dict) -> list[dict]:
    """Best re-ID match per neighbouring camera and direction, inside the walking window."""
    vec = db.get_reid(event["id"])
    if vec is None:
        return []
    start, end = event["start_ts"], event["end_ts"] or event["start_ts"]
    found = []
    for src, dst, lo, hi in edges():
        if src == event["camera_id"]:      # this event first, then the neighbour
            rows = _person_events_between(dst, "start_ts", end + lo, end + hi)
            pairs = [(event["id"], r["id"], r["start_ts"] - end, r) for r in rows]
        elif dst == event["camera_id"]:    # the neighbour first, then this event
            rows = _person_events_between(src, "COALESCE(end_ts, start_ts)", start - hi, start - lo)
            pairs = [(r["id"], event["id"], start - r["end_ts"], r) for r in rows]
        else:
            continue
        best = None
        for a, b, gap, r in pairs:
            other = db.get_reid(r["id"])
            if other is None:
                continue
            sim = float(np.dot(vec, other))
            if sim >= REID_MIN_SIM and (best is None or sim > best["sim"]):
                best = {"a": a, "b": b, "gap": gap, "sim": sim}
        if best:
            found.append(best)
    return found


def _crop_bytes(event_id: int) -> bytes | None:
    d = settings.data_dir / "events" / str(event_id)
    for name in ("crop_0.jpg", "crop_1.jpg", "crop_2.jpg", "snapshot.jpg"):
        if (d / name).exists():
            return (d / name).read_bytes()
    return None


def _cam_name(cam_id: str) -> str:
    c = db.one("SELECT name FROM cameras WHERE id=?", [cam_id])
    return c["name"] if c else cam_id


def _hhmmss(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


async def link_event(gate, event_id: int) -> int:
    """Find and confirm links for one person event. Returns the number of new confirmed links."""
    e = db.event(event_id)
    if not e or e["camera_class"] != "person" or e["status"] != "verified":
        return 0
    confirmed = 0
    for c in candidates(e):
        if db.one("SELECT 1 FROM event_links WHERE a=? AND b=?", [c["a"], c["b"]]):
            continue  # already decided (confirmed, rejected or rejected by the operator)
        ea, eb = db.event(c["a"]), db.event(c["b"])
        ia, ib = _crop_bytes(c["a"]), _crop_bytes(c["b"])
        if not (ea and eb and ia and ib):
            continue
        facts = (f"First camera: '{_cam_name(ea['camera_id'])}', person seen {_hhmmss(ea['start_ts'])}-{_hhmmss(ea['end_ts'] or ea['start_ts'])}.\n"
                 f"Second camera: '{_cam_name(eb['camera_id'])}', person seen {c['gap']:.0f} s after leaving the first camera.\n"
                 f"First camera description: {ea.get('synopsis') or 'n/a'}\nSecond camera description: {eb.get('synopsis') or 'n/a'}")
        try:
            verdict = await vlm.same_person(ia, ib, facts)  # routed; waits its turn on the local gate if local
        except Exception as ex:
            log.warning("same-person check %s->%s failed: %s", c["a"], c["b"], ex)
            continue
        ok = bool(verdict.get("same_person")) and verdict.get("confidence") in ("medium", "high")
        db.execute("INSERT OR IGNORE INTO event_links (a, b, gap_s, sim, status, confidence, reason, created_at) VALUES (?,?,?,?,?,?,?,?)",
                   [c["a"], c["b"], c["gap"], c["sim"], "confirmed" if ok else "rejected",
                    verdict.get("confidence"), verdict.get("reason"), time.time()])
        log.info("link %s -> %s (%+.0fs, re-ID %.2f): %s, %s: %s", c["a"], c["b"], c["gap"], c["sim"],
                 "SAME" if ok else "different", verdict.get("confidence"), verdict.get("reason"))
        if ok:
            confirmed += 1
            rebuild(c["a"])
    return confirmed


# ---------------------------------------------------------------- journeys

def _component(event_id: int) -> set[int]:
    seen, todo = {event_id}, [event_id]
    while todo:
        x = todo.pop()
        for r in db.all("SELECT a, b FROM event_links WHERE status='confirmed' AND (a=? OR b=?)", [x, x]):
            for y in (r["a"], r["b"]):
                if y not in seen:
                    seen.add(y)
                    todo.append(y)
    return seen


def rebuild(event_id: int) -> int | None:
    """Recompute the journey containing event_id (merging or splitting as links changed)."""
    members = _component(event_id)
    old = {r["journey_id"] for r in db.all(
        f"SELECT journey_id FROM events WHERE id IN ({','.join('?' * len(members))}) AND journey_id IS NOT NULL", list(members))}
    if len(members) == 1:
        db.execute("UPDATE events SET journey_id=NULL WHERE id=?", [event_id])
        _drop_empty(old)
        return None
    rows = db.all(f"SELECT id, camera_id, start_ts, COALESCE(end_ts, start_ts) AS end_ts FROM events "
                  f"WHERE id IN ({','.join('?' * len(members))}) ORDER BY start_ts", list(members))
    cams = []
    for r in rows:
        if not cams or cams[-1] != r["camera_id"]:
            cams.append(r["camera_id"])
    fields = [rows[0]["start_ts"], max(r["end_ts"] for r in rows), json.dumps(cams), time.time()]
    if old:
        jid = min(old)
        db.execute("UPDATE journeys SET first_ts=?, last_ts=?, cameras=?, updated_at=?, dirty=1 WHERE id=?", fields + [jid])
    else:
        jid = db.execute("INSERT INTO journeys (first_ts, last_ts, cameras, updated_at, dirty) VALUES (?,?,?,?,1)", fields).lastrowid
    db.execute(f"UPDATE events SET journey_id=? WHERE id IN ({','.join('?' * len(members))})", [jid, *members])
    _drop_empty(old - {jid})
    return jid


def _drop_empty(journey_ids: set[int]) -> None:
    for jid in journey_ids:
        if not db.one("SELECT 1 FROM events WHERE journey_id=?", [jid]):
            db.execute("DELETE FROM journeys WHERE id=?", [jid])


def reject_link(link_id: int) -> list[int]:
    """Operator says 'not the same person': mark the link and split the journey. Returns the affected events."""
    link = db.one("SELECT * FROM event_links WHERE id=?", [link_id])
    if not link:
        return []
    db.execute("UPDATE event_links SET status='user_rejected' WHERE id=?", [link_id])
    rebuild(link["a"])
    rebuild(link["b"])
    return [link["a"], link["b"]]


def journey_for(event_id: int) -> dict | None:
    e = db.one("SELECT journey_id FROM events WHERE id=?", [event_id])
    if not e or not e["journey_id"]:
        return None
    j = db.one("SELECT * FROM journeys WHERE id=?", [e["journey_id"]])
    if not j:
        return None
    members = db.all("SELECT id, camera_id, camera_class, start_ts, end_ts, synopsis, snapshot, status FROM events "
                     "WHERE journey_id=? ORDER BY start_ts", [j["id"]])
    ids = [m["id"] for m in members]
    links = db.all(f"SELECT * FROM event_links WHERE status='confirmed' AND a IN ({','.join('?' * len(ids))}) "
                   f"AND b IN ({','.join('?' * len(ids))})", ids + ids)
    return {**j, "cameras": json.loads(j["cameras"]), "events": members, "links": links}


async def write_narrative(gate, journey_id: int) -> str | None:
    j = db.one("SELECT * FROM journeys WHERE id=?", [journey_id])
    if not j:
        return None
    members = db.all("SELECT id, camera_id, start_ts, end_ts, synopsis FROM events WHERE journey_id=? ORDER BY start_ts", [journey_id])
    if len(members) < 2:
        return None
    # Merge back-to-back sightings on the same camera into one visit: the story is the route between cameras.
    visits: list[dict] = []
    for m in members:
        end = m["end_ts"] or m["start_ts"]
        if visits and visits[-1]["cam"] == m["camera_id"]:
            v = visits[-1]
            v["end"] = max(v["end"], end)
            v["descriptions"].append(m["synopsis"])
            continue
        visits.append({"cam": m["camera_id"], "start": m["start_ts"], "end": end, "first_id": m["id"],
                       "descriptions": [m["synopsis"]], "gap_s": None if not visits else m["start_ts"] - visits[-1]["end"]})
    visits = visits[:MAX_NARRATIVE_IMAGES + 2]
    items = [{"camera": _cam_name(v["cam"]), "time": _hhmmss(v["start"]), "duration_s": v["end"] - v["start"],
              "gap_s": v["gap_s"], "descriptions": v["descriptions"]} for v in visits]
    images = [img for v in visits[:MAX_NARRATIVE_IMAGES] if (img := _crop_bytes(v["first_id"]))]
    result = await vlm.journey_narrative(items, images)
    model = result.pop("_model", None)
    # The route is built here so it is always correct and names every camera; Qwen adds actions and the gist.
    actions = [str(x).strip().rstrip(".") for x in (result.get("actions") or [])]
    settings_ = [str(x) for x in (result.get("settings") or [])]
    parts, prev_setting = [], None
    for i, it in enumerate(items):
        did = f" ({actions[i]})" if i < len(actions) and actions[i] else ""
        gap = f" +{it['gap_s']:.0f} s" if it["gap_s"] is not None else ""
        setting = settings_[i] if i < len(settings_) else "unclear"
        # plain words for the indoor/outdoor transition so searches like "went outside" find the journey
        moved = {("indoors", "outdoors"): ", went outside", ("outdoors", "indoors"): ", came back inside"}.get((prev_setting, setting), "")
        if setting != "unclear":
            prev_setting = setting
        parts.append(f"{it['camera']} {it['time']}{gap}{moved}{did}")
    summary = " → ".join(parts) + "."
    if overall := (result.get("overall") or "").strip():
        summary = overall.rstrip(".") + ". " + summary
    db.execute("UPDATE journeys SET synopsis=?, dirty=0 WHERE id=?", [summary, journey_id])
    db.execute("UPDATE journeys SET model=? WHERE id=?", [model, journey_id])
    log.info("journey %s narrative (%s): %s", journey_id, model, summary[:140])
    return summary


async def narrative_loop(pipeline) -> None:
    """Write (or rewrite) a journey's narrative once it has been quiet for QUIET_S."""
    while True:
        await asyncio.sleep(15)
        if not pipeline.vlm_ready:
            continue
        for j in db.all("SELECT id FROM journeys WHERE dirty=1 AND updated_at < ?", [time.time() - QUIET_S]):
            try:
                await write_narrative(pipeline.gate, j["id"])
                for m in db.all("SELECT id FROM events WHERE journey_id=?", [j["id"]]):
                    await pipeline.reindex(m["id"])
            except Exception:
                log.exception("journey %s narrative failed", j["id"])
                db.execute("UPDATE journeys SET dirty=0 WHERE id=?", [j["id"]])


# ---------------------------------------------------------------- backfill & suggestions

def _tight_crops(event_id: int, detections: dict) -> list[np.ndarray]:
    """Recover tight person crops from saved keyframe crops (their padding is known from save_keyframes)."""
    from .reid import person_crop
    samples = {s["ts"]: s for s in detections.get("samples", [])}
    out = []
    for k in detections.get("keyframes", []):
        if k.get("kind") != "crop":
            continue
        s = samples.get(k["ts"])
        box = (s.get("match") or {}).get("box") if s else None
        box = box or (s and s.get("cam_box"))
        img = cv2.imread(str(settings.data_dir / "events" / str(event_id) / k["file"]))
        if img is None or not box:
            continue
        l, t, r, b = box
        pw, ph = (r - l) * 0.6 + 0.03, (b - t) * 0.4 + 0.03
        x1, y1, x2, y2 = max(0, l - pw), max(0, t - ph), min(1, r + pw), min(1, b + ph)
        rel = ((l - x1) / (x2 - x1), (t - y1) / (y2 - y1), (r - x1) / (x2 - x1), (b - y1) / (y2 - y1))
        out.append(person_crop(img, rel))
    return out


def backfill(reid_model) -> int:
    """Embed verified person events that don't have a re-ID vector yet (from their saved crops)."""
    rows = db.all("SELECT id, detections FROM events WHERE camera_class='person' AND status='verified' "
                  "AND detections IS NOT NULL AND id NOT IN (SELECT rowid FROM reid_vec)")
    n = 0
    for r in rows:
        vec = reid_model.embed(_tight_crops(r["id"], json.loads(r["detections"])))
        if vec is not None:
            db.set_reid(r["id"], vec)
            n += 1
    return n


def suggestions(days: float = 14) -> list[dict]:
    """Camera pairs where re-ID-similar people appear close in time, with a typical walking window."""
    rows = db.all("SELECT e.id, e.camera_id, e.start_ts, COALESCE(e.end_ts, e.start_ts) AS end_ts FROM events e "
                  "WHERE e.camera_class='person' AND e.status='verified' AND e.start_ts > ? "
                  "AND e.id IN (SELECT rowid FROM reid_vec) ORDER BY e.start_ts", [time.time() - days * 86400])
    vecs = {r["id"]: db.get_reid(r["id"]) for r in rows}
    gaps: dict[tuple[str, str], list[float]] = defaultdict(list)
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            gap = b["start_ts"] - a["end_ts"]
            if gap > SUGGEST_WINDOW_S[1]:
                break
            if b["camera_id"] == a["camera_id"] or gap < SUGGEST_WINDOW_S[0]:
                continue
            if float(np.dot(vecs[a["id"]], vecs[b["id"]])) >= SUGGEST_MIN_SIM:
                gaps[(a["camera_id"], b["camera_id"])].append(gap)
    existing = {(l["cam_a"], l["cam_b"]) for l in topology()} | {(l["cam_b"], l["cam_a"]) for l in topology() if not l["one_way"]}
    out = []
    for (a, b), g in sorted(gaps.items(), key=lambda kv: -len(kv[1])):
        g.sort()
        p10, p90 = g[int(0.1 * (len(g) - 1))], g[int(0.9 * (len(g) - 1))]
        out.append({"cam_a": a, "cam_b": b, "count": len(g), "median_gap_s": round(statistics.median(g), 1),
                    "min_s": round(max(SUGGEST_WINDOW_S[0], p10 - 5)), "max_s": round(min(SUGGEST_WINDOW_S[1], p90 + 15)),
                    "configured": (a, b) in existing})
    return out
