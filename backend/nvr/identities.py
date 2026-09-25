"""Who was on site: group repeated sightings of the same person or vehicle, and let the operator name them.

People are matched by their re-ID fingerprint (reid.py, stored per event in reid_vec); vehicles by a CLIP
image embedding of their crops (clip.py, stored in vehicle_vec). Sightings in a time window are joined by
average-link clustering; confirmed cross-camera journeys always count as the same person.

Named identities ("Shawn", "UPS truck") are a stored centroid; any cluster whose centroid is close enough
gets the name. Naming a cluster adds its sightings to that centroid, so recognition improves over time.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import numpy as np

from .config import settings
from .db import db

log = logging.getLogger("nvr.identities")

# same-identity thresholds (cosine). Person: measured on this site, same person 0.8-0.9, different median ~0.65.
# Vehicle: CLIP image-image; same vehicle ~0.9+, different vehicles of the same type ~0.8.
GROUP_SIM = {"person": 0.76, "vehicle": 0.88}
NAME_SIM = {"person": 0.85, "vehicle": 0.88}   # conservative: a wrong name is worse than a missing one (indoor re-ID: different people reach ~0.82)
VEC_TABLE = {"person": "reid_vec", "vehicle": "vehicle_vec"}
EVENT_COLS = "id, camera_id, camera_class, start_ts, end_ts, synopsis, snapshot, yolo_class, yolo_conf, priority, anomaly, journey_id, watched"


# ---------------------------------------------------------------- vehicle fingerprints (CLIP)

def vehicle_crops(event_id: int) -> list[bytes]:
    d = settings.data_dir / "events" / str(event_id)
    return [(d / f"crop_{i}.jpg").read_bytes() for i in range(4) if (d / f"crop_{i}.jpg").exists()]


def embed_vehicle(clip, event_id: int) -> np.ndarray | None:
    """CLIP fingerprint of a verified vehicle from its saved crops (runs on the GPU executor)."""
    import cv2
    imgs = [cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR) for b in vehicle_crops(event_id)]
    imgs = [i for i in imgs if i is not None]
    if not imgs:
        return None
    v = clip.embed_images(imgs).mean(axis=0)
    v /= np.linalg.norm(v) or 1.0
    db.set_vec("vehicle_vec", event_id, v)
    return v


def backfill_vehicles(clip) -> int:
    rows = db.all("SELECT id FROM events WHERE status='verified' AND camera_class='vehicle' "
                  "AND id NOT IN (SELECT rowid FROM vehicle_vec)")
    n = 0
    for r in rows:
        if embed_vehicle(clip, r["id"]) is not None:
            n += 1
    return n


# ---------------------------------------------------------------- clustering

def average_link(S: np.ndarray, threshold: float) -> list[list[int]]:
    """Merge the two most similar clusters (mean pairwise similarity) until none is above threshold."""
    n = len(S)
    clusters = [[i] for i in range(n)]
    if n < 2:
        return clusters
    M = S.astype(np.float64).copy()
    np.fill_diagonal(M, -np.inf)
    size = np.ones(n)
    alive = list(range(n))
    while len(alive) > 1:
        sub = M[np.ix_(alive, alive)]
        k = int(np.argmax(sub))
        a, b = alive[k // len(alive)], alive[k % len(alive)]
        if M[a, b] < threshold:
            break
        merged = (size[a] * M[a] + size[b] * M[b]) / (size[a] + size[b])
        M[a], M[:, a] = merged, merged
        M[a, a] = -np.inf
        M[b], M[:, b] = -np.inf, -np.inf
        size[a] += size[b]
        clusters[a] += clusters[b]
        clusters[b] = []
        alive.remove(b)
    return [c for c in clusters if c]


def _vectors(kind: str, events: list[dict]) -> tuple[list[dict], np.ndarray]:
    have, vecs = [], []
    for e in events:
        v = db.get_vec(VEC_TABLE[kind], e["id"])
        if v is not None:
            have.append(e)
            vecs.append(v)
    if not vecs:
        return [], np.zeros((0, 512), np.float32)
    V = np.stack(vecs).astype(np.float64)
    V /= np.linalg.norm(V, axis=1, keepdims=True) + 1e-9
    return have, V


def named(kind: str) -> list[dict]:
    rows = db.all("SELECT id, name, kind, notes, embedding, sightings, updated_at, watch, watch_note FROM identities WHERE kind=? ORDER BY name", [kind])
    for r in rows:
        r["vec"] = np.frombuffer(r.pop("embedding"), dtype=np.float32).astype(np.float64)
    return rows


def clusters(kind: str, since: float, until: float | None = None, camera_id: str | None = None) -> dict:
    """Sightings of `kind` between since and until, grouped into identities."""
    until = until or time.time()
    where = "status='verified' AND camera_class=? AND start_ts BETWEEN ? AND ? AND " \
            "(feedback IS NULL OR json_extract(feedback, '$.verdict') IS NOT 'false_alarm')"
    params: list = [kind, since, until]
    if camera_id:
        where += " AND camera_id=?"
        params.append(camera_id)
    events = db.all(f"SELECT {EVENT_COLS} FROM events WHERE {where} ORDER BY start_ts", params)
    have, V = _vectors(kind, events)
    unfingerprinted = [e for e in events if e["id"] not in {h["id"] for h in have}]
    groups: list[list[int]] = []
    if len(have):
        S = V @ V.T
        if kind == "person":  # confirmed journeys: same person, whatever the fingerprints say
            pos = {e["id"]: i for i, e in enumerate(have)}
            by_j: dict[int, list[int]] = {}
            for e in have:
                if e.get("journey_id"):
                    by_j.setdefault(e["journey_id"], []).append(pos[e["id"]])
            for idx in by_j.values():
                for a in idx:
                    for b in idx:
                        if a != b:
                            S[a, b] = 1.0
        groups = average_link(S, GROUP_SIM[kind])
    names = named(kind)
    cams = {c["id"]: c["name"] for c in db.cameras()}
    out = []
    for g in groups:
        members = sorted((have[i] for i in g), key=lambda e: e["start_ts"])
        centroid = V[g].mean(axis=0)
        centroid /= np.linalg.norm(centroid) + 1e-9
        best, best_sim = None, 0.0
        for nm in names:
            sim = float(nm["vec"] @ centroid / (np.linalg.norm(nm["vec"]) + 1e-9))
            if sim >= NAME_SIM[kind] and sim > best_sim:
                best, best_sim = nm, sim
        out.append(_summary(kind, members, cams, best, best_sim))
    for e in unfingerprinted:  # no crops saved (old events): shown on their own
        out.append(_summary(kind, [e], cams, None, 0.0, fingerprinted=False))
    out.sort(key=lambda c: -c["last_ts"])
    for i, c in enumerate(out):
        c["key"] = f"{kind[0]}{i + 1}"
    return {"kind": kind, "since": since, "until": until, "clusters": out, "sightings": len(events),
            "named": [{k: v for k, v in n.items() if k != "vec"} for n in names]}


def _summary(kind: str, members: list[dict], cams: dict, identity: dict | None, sim: float, fingerprinted: bool = True) -> dict:
    cover = max(members, key=lambda e: (e.get("yolo_conf") or 0, e["start_ts"]))
    desc = next((e["synopsis"] for e in reversed(members) if e.get("synopsis")), None)
    return {
        "kind": kind, "identity_id": identity["id"] if identity else None, "name": identity["name"] if identity else None,
        "watch": bool(identity["watch"]) if identity else False,
        "name_sim": round(sim, 3) if identity else None, "fingerprinted": fingerprinted,
        "sightings": len(members), "first_ts": members[0]["start_ts"], "last_ts": members[-1]["end_ts"] or members[-1]["start_ts"],
        "on_site_s": round(sum(max(0.0, (e["end_ts"] or e["start_ts"]) - e["start_ts"]) for e in members)),
        "cameras": [cams.get(c, c) for c in dict.fromkeys(e["camera_id"] for e in members)],
        "cover": cover["id"], "description": desc,
        "priority": max((e.get("priority") or "none" for e in members), key=lambda p: ["none", "low", "medium", "high"].index(p)),
        "unusual": any((e.get("anomaly") or 0) >= 0.75 for e in members),
        "events": [{k: e[k] for k in ("id", "camera_id", "start_ts", "end_ts", "snapshot", "synopsis", "priority", "anomaly", "yolo_class")} for e in members],
    }


# ---------------------------------------------------------------- naming

def name_cluster(kind: str, name: str, event_ids: list[int], notes: str = "") -> dict:
    """Give these sightings a name. Their fingerprints are averaged into the identity's centroid (created or
    merged into an existing identity of that name)."""
    name = name.strip()
    have, V = _vectors(kind, [{"id": i} for i in event_ids])
    if not len(have):
        raise ValueError("none of these sightings has a fingerprint")
    vec, n = V.mean(axis=0), len(have)
    existing = db.one("SELECT id, embedding, sightings FROM identities WHERE kind=? AND name=? COLLATE NOCASE", [kind, name])
    if existing:
        old = np.frombuffer(existing["embedding"], dtype=np.float32).astype(np.float64)
        total = existing["sightings"] + n
        vec = (old * existing["sightings"] + vec * n) / total
        n = total
    vec = (vec / (np.linalg.norm(vec) + 1e-9)).astype(np.float32)
    if existing:
        db.execute("UPDATE identities SET embedding=?, sightings=?, notes=COALESCE(NULLIF(?, ''), notes), updated_at=? WHERE id=?",
                   [vec.tobytes(), n, notes, time.time(), existing["id"]])
        iid = existing["id"]
    else:
        iid = db.execute_insert("INSERT INTO identities (name, kind, embedding, notes, sightings, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                                [name, kind, vec.tobytes(), notes, n, time.time(), time.time()])
    return get_identity(iid)


IDENT_COLS = "id, name, kind, notes, sightings, updated_at, watch, watch_note"


def get_identity(iid: int) -> dict | None:
    return db.one(f"SELECT {IDENT_COLS} FROM identities WHERE id=?", [iid])


def update_identity(iid: int, name: str | None = None, notes: str | None = None) -> dict | None:
    if name is not None:
        db.execute("UPDATE identities SET name=?, updated_at=? WHERE id=?", [name.strip(), time.time(), iid])
    if notes is not None:
        db.execute("UPDATE identities SET notes=?, updated_at=? WHERE id=?", [notes, time.time(), iid])
    return get_identity(iid)


# ---------------------------------------------------------------- watch list

def match_identity(kind: str, event_id: int) -> tuple[dict, float] | None:
    """The named identity this sighting's fingerprint matches (above NAME_SIM), if any."""
    v = db.get_vec(VEC_TABLE[kind], event_id)
    if v is None:
        return None
    v = v.astype(np.float64)
    v /= np.linalg.norm(v) + 1e-9
    best, best_sim = None, 0.0
    for n in named(kind):
        sim = float(n["vec"] @ v / (np.linalg.norm(n["vec"]) + 1e-9))
        if sim >= NAME_SIM[kind] and sim > best_sim:
            best, best_sim = n, sim
    return (best, best_sim) if best else None


def check_watch(event_id: int) -> str | None:
    """Mark a verified sighting that matches a watched identity (events.watched = name). Returns the name."""
    e = db.event(event_id)
    if not e or e["status"] != "verified" or e["camera_class"] not in VEC_TABLE:
        return None
    m = match_identity(e["camera_class"], event_id)
    name = m[0]["name"] if m and m[0]["watch"] else None
    if (e.get("watched") or None) != name:
        db.update_event(event_id, watched=name)
    return name


def set_watch(iid: int, watch: bool, note: str | None = None, recheck_hours: float = 24) -> tuple[dict | None, list[int]]:
    """Turn watching on or off for a name. Recent sightings are re-marked so Home/priority update at once.
    Returns (identity, ids of events whose mark changed)."""
    ident = get_identity(iid)
    if not ident:
        return None, []
    db.execute("UPDATE identities SET watch=?, watch_note=COALESCE(?, watch_note), updated_at=? WHERE id=?",
               [int(watch), note, time.time(), iid])
    changed = []
    rows = db.all("SELECT id, watched FROM events WHERE status='verified' AND camera_class=? AND start_ts >= ?",
                  [ident["kind"], time.time() - recheck_hours * 3600])
    for r in rows:
        before = r["watched"]
        if check_watch(r["id"]) != before:
            changed.append(r["id"])
    return get_identity(iid), changed


def delete_identity(iid: int) -> None:
    db.execute("DELETE FROM identities WHERE id=?", [iid])


def identity_facts(kind: str, event_id: int) -> str | None:
    """For prompts: the name of a known identity this sighting matches, if any."""
    m = match_identity(kind, event_id)
    if not m:
        return None
    n = m[0]
    return (f"Known {kind}: '{n['name']}'" + (f" ({n['notes']})" if n["notes"] else "") + " (matched by appearance)."
            + (f" This {kind} is on the operator's watch list" + (f": {n['watch_note']}" if n["watch_note"] else "") + "." if n["watch"] else ""))
