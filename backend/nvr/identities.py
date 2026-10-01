"""Who was on site: group repeated sightings of the same person or vehicle, and let the operator name them.

People are matched by their re-ID fingerprint (reid.py, stored per event in reid_vec); vehicles by a CLIP
image embedding of their tight crops joined with a colour histogram (stored in vehicle_vec). CLIP alone groups
vehicles by shape and setting ("pickup in this yard"): a white and a black pickup scored 0.90. The colour part
pulls them apart while the same truck in different light still matches. Sightings in a time window are joined by
average-link clustering; confirmed cross-camera journeys always count as the same person.

Named identities ("Shawn", "UPS truck") hold one or more *looks*: a centroid per outfit / appearance
(identity_looks). A sighting matches the identity if it is close to any look. Naming a cluster averages it into
the nearest look when it resembles one, otherwise it becomes a new look: the same person in a different shirt
on another day is recognised once you have named them in that shirt. Person re-ID sees clothing and build,
not faces, so each outfit has to be taught once.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import numpy as np

from .config import settings
from .db import COLOR_DIM, VEHICLE_DIM, db

log = logging.getLogger("nvr.identities")

# same-identity thresholds (cosine). Person: measured on this site, same person 0.8-0.9, different median ~0.65.
# Vehicle: 0.7*CLIP(tight crop) + 0.3*colour, measured on this site: same truck twice 0.885, two white trucks of the
# same fleet 0.89, white vs black pickup 0.81, white pickup vs white van 0.77.
GROUP_SIM = {"person": 0.76, "vehicle": 0.86}
NAME_SIM = {"person": 0.85, "vehicle": 0.87}   # conservative: a wrong name is worse than a missing one (indoor re-ID: different people reach ~0.82)
VEC_TABLE = {"person": "reid_vec", "vehicle": "vehicle_vec"}
EVENT_COLS = ("id, camera_id, camera_class, start_ts, end_ts, synopsis, snapshot, yolo_class, yolo_conf, priority, anomaly, journey_id, watched, "
              "camera_conf, threat, policy, areas, anomaly_json, corrected_at, ptz_preset")
# what an event card in the grouped ("by who") view shows, so it matches the browse cards
CARD_KEYS = ("id", "camera_id", "start_ts", "end_ts", "snapshot", "synopsis", "priority", "anomaly", "yolo_class", "yolo_conf",
             "camera_conf", "threat", "watched", "policy", "areas", "anomaly_json", "corrected_at", "ptz_preset", "journey_id")


# ---------------------------------------------------------------- vehicle fingerprints (CLIP)

CLIP_W, COLOR_W = 0.7, 0.3   # the fingerprint's dot product = CLIP_W*cos(CLIP) + COLOR_W*cos(colour)


def _padded_window(box):
    """The crop window verifier.py saves around a box (same padding formula), in frame coordinates."""
    l, t, r, b = box
    pw, ph = (r - l) * 0.6 + 0.03, (b - t) * 0.4 + 0.03
    return max(0.0, l - pw), max(0.0, t - ph), min(1.0, r + pw), min(1.0, b + ph)


def tight_boxes(e: dict) -> dict[str, list[float]]:
    """crop file -> the vehicle's box inside that crop (0-1). New events store it; for older ones it is derived
    from the detection sample at the same time, using the verifier's padding formula."""
    d = e.get("detections") or {}
    out = {}
    by_ts = {s["ts"]: s for s in d.get("samples", []) if "ts" in s}
    for k in d.get("keyframes", []):
        if k.get("kind") != "crop":
            continue
        if k.get("box"):
            out[k["file"]] = k["box"]
            continue
        s = by_ts.get(k.get("ts"))
        box = (s.get("match") or {}).get("box") if s else None
        box = box or (s.get("cam_box") if s else None)
        if not box:
            continue
        x1, y1, x2, y2 = _padded_window(box)
        cw, ch = max(1e-6, x2 - x1), max(1e-6, y2 - y1)
        out[k["file"]] = [(box[0] - x1) / cw, (box[1] - y1) / ch, (box[2] - x1) / cw, (box[3] - y1) / ch]
    return out


def vehicle_crops(event_id: int, tight: bool = True) -> list[np.ndarray]:
    """The saved vehicle crops as BGR images, cut to the vehicle itself (a little margin) when tight."""
    import cv2
    d = settings.data_dir / "events" / str(event_id)
    boxes = tight_boxes(db.event(event_id) or {}) if tight else {}
    imgs = []
    for i in range(4):
        p = d / f"crop_{i}.jpg"
        if not p.exists():
            continue
        img = cv2.imdecode(np.frombuffer(p.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue
        box = boxes.get(p.name)
        if box:
            h, w = img.shape[:2]
            m = 0.04  # keep a sliver of margin so wheels/mirrors on the box edge stay in
            x1, y1 = int(max(0.0, box[0] - m) * w), int(max(0.0, box[1] - m) * h)
            x2, y2 = int(min(1.0, box[2] + m) * w), int(min(1.0, box[3] + m) * h)
            cut = img[y1:y2, x1:x2]
            if cut.shape[0] >= 16 and cut.shape[1] >= 16:
                img = cut
        imgs.append(img)
    return imgs


def color_hist(bgr: np.ndarray) -> np.ndarray:
    """48-d colour signature of a vehicle crop: hue x 2 saturation levels for coloured pixels (32) and a
    brightness histogram for grey/white/black pixels (16). Taken from the central 80% so background matters less."""
    import cv2
    h, w = bgr.shape[:2]
    core = bgr[int(h * 0.1):max(int(h * 0.9), int(h * 0.1) + 1), int(w * 0.1):max(int(w * 0.9), int(w * 0.1) + 1)]
    hsv = cv2.cvtColor(core, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.float32)
    hue, sat, val = hsv[:, 0], hsv[:, 1], hsv[:, 2]
    chroma = (sat > 60) & (val > 40)
    out = np.zeros(COLOR_DIM, np.float32)
    if chroma.any():
        hb = np.minimum((hue[chroma] / 180 * 16).astype(int), 15)
        sb = (sat[chroma] > 140).astype(int)
        np.add.at(out, hb * 2 + sb, 1.0)
    if (~chroma).any():
        vb = np.minimum((val[~chroma] / 256 * 16).astype(int), 15)
        np.add.at(out, 32 + vb, 1.0)
    out /= max(1.0, float(hsv.shape[0]))
    out = np.sqrt(out)  # Hellinger-style: softens the dominant bin so cosine reflects the whole palette
    return out / (np.linalg.norm(out) or 1.0)


def vehicle_fingerprint(clip, imgs: list[np.ndarray]) -> np.ndarray:
    v = clip.embed_images(imgs).mean(axis=0)
    v /= np.linalg.norm(v) or 1.0
    c = np.mean([color_hist(i) for i in imgs], axis=0)
    c /= np.linalg.norm(c) or 1.0
    return np.concatenate([v * np.sqrt(CLIP_W), c * np.sqrt(COLOR_W)]).astype(np.float32)


def embed_vehicle(clip, event_id: int) -> np.ndarray | None:
    """Fingerprint of a verified vehicle from its saved crops (runs on the GPU executor)."""
    imgs = vehicle_crops(event_id)
    if not imgs:
        return None
    v = vehicle_fingerprint(clip, imgs)
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
        return [], np.zeros((0, VEHICLE_DIM if kind == "vehicle" else 512), np.float32)
    V = np.stack(vecs).astype(np.float64)
    V /= np.linalg.norm(V, axis=1, keepdims=True) + 1e-9
    return have, V


def named(kind: str) -> list[dict]:
    """Named identities with their looks: r["vecs"] (unit vectors) and r["vec"] (the main look) for prompts/UI."""
    rows = db.all("SELECT id, name, kind, notes, embedding, sightings, updated_at, watch, watch_note FROM identities WHERE kind=? ORDER BY name", [kind])
    looks = db.all("SELECT identity_id, embedding FROM identity_looks ORDER BY sightings DESC, id")
    by_id: dict[int, list] = {}
    for lk in looks:
        v = np.frombuffer(lk["embedding"], dtype=np.float32).astype(np.float64)
        by_id.setdefault(lk["identity_id"], []).append(v / (np.linalg.norm(v) + 1e-9))
    for r in rows:
        main = np.frombuffer(r.pop("embedding"), dtype=np.float32).astype(np.float64)
        r["vecs"] = by_id.get(r["id"]) or [main / (np.linalg.norm(main) + 1e-9)]
        r["vec"] = r["vecs"][0]
        r["looks"] = len(r["vecs"])
    return rows


def best_look(ident: dict, v: np.ndarray) -> float:
    """Cosine similarity of a unit vector to the identity's closest look."""
    return max(float(lk @ v) for lk in ident["vecs"])


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
            sim = best_look(nm, centroid)
            if sim >= NAME_SIM[kind] and sim > best_sim:
                best, best_sim = nm, sim
        out.append(_summary(kind, members, cams, best, best_sim))
    for e in unfingerprinted:  # no crops saved (old events): shown on their own
        out.append(_summary(kind, [e], cams, None, 0.0, fingerprinted=False))
    out.sort(key=lambda c: -c["last_ts"])
    db.enrich([ev for c in out for ev in c["events"]])  # locked / journey_cameras, as on the browse cards
    for i, c in enumerate(out):
        c["key"] = f"{kind[0]}{i + 1}"
    return {"kind": kind, "since": since, "until": until, "clusters": out, "sightings": len(events),
            "named": [{k: v for k, v in n.items() if k not in ("vec", "vecs")} for n in names]}


def _card(e: dict) -> dict:
    c = {k: e.get(k) for k in CARD_KEYS}
    for k in ("policy", "areas", "anomaly_json"):
        if isinstance(c[k], str):
            c[k] = json.loads(c[k])
    return c


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
        "events": [_card(e) for e in members],
    }


# ---------------------------------------------------------------- naming

def name_cluster(kind: str, name: str, event_ids: list[int], notes: str = "") -> dict:
    """Give these sightings a name. Their fingerprint joins the identity's nearest look when it resembles one
    (>= GROUP_SIM), otherwise it becomes a new look (a different outfit). Creates the identity if needed."""
    name = name.strip()
    have, V = _vectors(kind, [{"id": i} for i in event_ids])
    if not len(have):
        raise ValueError("none of these sightings has a fingerprint")
    vec, n = V.mean(axis=0), len(have)
    vec /= np.linalg.norm(vec) + 1e-9
    now = time.time()
    existing = db.one("SELECT id, sightings FROM identities WHERE kind=? AND name=? COLLATE NOCASE", [kind, name])
    if not existing:
        iid = db.execute_insert("INSERT INTO identities (name, kind, embedding, notes, sightings, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                                [name, kind, vec.astype(np.float32).tobytes(), notes, n, now, now])
        db.execute("INSERT INTO identity_looks (identity_id, embedding, sightings, created_at, updated_at) VALUES (?,?,?,?,?)",
                   [iid, vec.astype(np.float32).tobytes(), n, now, now])
        return get_identity(iid)
    iid = existing["id"]
    looks = db.all("SELECT id, embedding, sightings FROM identity_looks WHERE identity_id=?", [iid])
    best, best_sim = None, -1.0
    for lk in looks:
        u = np.frombuffer(lk["embedding"], dtype=np.float32).astype(np.float64)
        sim = float(u / (np.linalg.norm(u) + 1e-9) @ vec)
        if sim > best_sim:
            best, best_sim = lk, sim
    if best and best_sim >= GROUP_SIM[kind]:  # same look: refine its centroid
        old = np.frombuffer(best["embedding"], dtype=np.float32).astype(np.float64)
        total = best["sightings"] + n
        merged = (old * best["sightings"] + vec * n) / total
        merged /= np.linalg.norm(merged) + 1e-9
        db.execute("UPDATE identity_looks SET embedding=?, sightings=?, updated_at=? WHERE id=?",
                   [merged.astype(np.float32).tobytes(), total, now, best["id"]])
    else:  # a new outfit / appearance of the same identity
        db.execute("INSERT INTO identity_looks (identity_id, embedding, sightings, created_at, updated_at) VALUES (?,?,?,?,?)",
                   [iid, vec.astype(np.float32).tobytes(), n, now, now])
    main = db.one("SELECT embedding, SUM(sightings) OVER () AS total FROM identity_looks WHERE identity_id=? "
                  "ORDER BY sightings DESC, id LIMIT 1", [iid])
    db.execute("UPDATE identities SET embedding=?, sightings=?, notes=COALESCE(NULLIF(?, ''), notes), updated_at=? WHERE id=?",
               [main["embedding"], main["total"], notes, now, iid])
    return get_identity(iid)


IDENT_COLS = ("id, name, kind, notes, sightings, updated_at, watch, watch_note, "
              "(SELECT COUNT(*) FROM identity_looks WHERE identity_id = identities.id) AS looks")


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
        sim = best_look(n, v)
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
    db.execute("DELETE FROM identity_looks WHERE identity_id=?", [iid])
    db.execute("DELETE FROM identities WHERE id=?", [iid])


def identity_facts(kind: str, event_id: int) -> str | None:
    """For prompts: the name of a known identity this sighting matches, if any."""
    m = match_identity(kind, event_id)
    if not m:
        return None
    n = m[0]
    return (f"Known {kind}: '{n['name']}'" + (f" ({n['notes']})" if n["notes"] else "") + " (matched by appearance)."
            + (f" This {kind} is on the operator's watch list" + (f": {n['watch_note']}" if n["watch_note"] else "") + "." if n["watch"] else ""))
