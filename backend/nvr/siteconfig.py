"""Export / import of a site's configuration (not its recordings or events): cameras, zones, named places,
site rules, PTZ settings, neighbour topology, named people/vehicles with their looks, timeline layouts,
retention and briefing settings. The fleet hub pulls this nightly; restoring a dead site is
"install, enrol, import". Camera passwords are not exported: they're re-entered on restore.

Fleet actions (hub/hub/fleet_actions.py) move cameras between sites with `handoff` (a partial export of some
cameras WITH their passwords, served only down the hub tunnel; see api.config_handoff) and `merge_cameras`
(adds those cameras to this site without touching its own settings, layouts, dashboards or other cameras). The
handoff also carries what the site learned about each camera (`learned_state`: baseline, parked spots, operator
corrections) and the merge seeds it (`adopt_learned`); `export_history` / `import_history` copy the cameras'
event history (metadata, images, fingerprints; never clips) when the operator asks for it."""
from __future__ import annotations

import base64
import json
import re
import time

from . import __version__
from .db import db

SETTINGS_KEYS = ("retention_policy", "briefing")
CAMERA_COLS = ("name", "host", "onvif_port", "rtsp_port", "username", "main_path", "sub_path", "enabled", "zones",
               "retention_days", "scene_notes", "retention_policy", "synopsis_labels", "policies")


def _export_identities() -> list[dict]:
    idents = []
    for i in db.all("SELECT id, name, kind, notes, sightings, watch, watch_note FROM identities"):
        looks = db.all("SELECT embedding, sightings FROM identity_looks WHERE identity_id=?", [i["id"]])
        idents.append({**{k: i[k] for k in ("name", "kind", "notes", "sightings", "watch", "watch_note")},
                       "looks": [{"embedding": base64.b64encode(lk["embedding"]).decode(), "sightings": lk["sightings"]} for lk in looks]})
    return idents


def _merge_identities(idents: list[dict], now: float) -> int:
    """Add the named people/vehicles this site doesn't know yet (by kind + name). Caller holds db.lock and commits."""
    n = 0
    for i in idents:
        row = db.conn.execute("SELECT id FROM identities WHERE kind=? AND name=? COLLATE NOCASE", [i["kind"], i["name"]]).fetchone()
        if row:
            continue  # merge by name: keep what the site already knows
        looks = i.get("looks") or []
        if not looks:
            continue
        main = base64.b64decode(looks[0]["embedding"])
        cur = db.conn.execute("INSERT INTO identities (name, kind, embedding, notes, sightings, created_at, updated_at, watch, watch_note) "
                              "VALUES (?,?,?,?,?,?,?,?,?)", [i["name"], i["kind"], main, i.get("notes") or "", i.get("sightings") or 0, now, now,
                                                             int(bool(i.get("watch"))), i.get("watch_note") or ""])
        for lk in looks:
            db.conn.execute("INSERT INTO identity_looks (identity_id, embedding, sightings, created_at, updated_at) VALUES (?,?,?,?,?)",
                            [cur.lastrowid, base64.b64decode(lk["embedding"]), lk.get("sightings") or 0, now, now])
        n += 1
    return n


def export_config() -> dict:
    cams = []
    for c in db.cameras():
        cams.append({k: v for k, v in c.items() if k != "password"})
    links = db.all("SELECT cam_a, cam_b, min_s, max_s, one_way FROM camera_links")
    layouts = [{"name": r["name"], "config": json.loads(r["config"])} for r in db.all("SELECT name, config FROM layouts")]
    dashboards = [{"name": r["name"], "config": json.loads(r["config"])} for r in db.all("SELECT name, config FROM dashboards")]
    return {"format": 1, "exported_at": time.time(), "site_version": __version__, "cameras": cams, "camera_links": links,
            "identities": _export_identities(), "layouts": layouts, "dashboards": dashboards,
            "settings": {k: db.get_setting(k) for k in SETTINGS_KEYS}}


def import_config(data: dict, replace_identities: bool = False) -> dict:
    """Apply an export. Cameras are upserted (existing passwords kept), topology and layouts replaced,
    identities merged by name (or replaced), settings overwritten. Returns counts."""
    if data.get("format") != 1:
        raise ValueError("unknown backup format")
    if data.get("partial"):
        raise ValueError("a partial export (camera handoff) is merged with merge_cameras, not restored")
    counts = {"cameras": 0, "camera_links": 0, "identities": 0, "layouts": 0, "dashboards": 0}
    existing_pw = {c["id"]: c["password"] for c in db.cameras()}
    for c in data.get("cameras", []):
        cam = {**c, "password": existing_pw.get(c["id"], "")}
        db.upsert_camera(cam)
        if c.get("ptz_config") is not None:
            db.set_ptz_config(c["id"], c["ptz_config"])
        counts["cameras"] += 1
    with db.lock:
        db.conn.execute("DELETE FROM camera_links")
        for l in data.get("camera_links", []):
            db.conn.execute("INSERT OR REPLACE INTO camera_links (cam_a, cam_b, min_s, max_s, one_way) VALUES (?,?,?,?,?)",
                            [l["cam_a"], l["cam_b"], l["min_s"], l["max_s"], int(bool(l.get("one_way")))])
            counts["camera_links"] += 1
        if replace_identities:
            db.conn.execute("DELETE FROM identity_looks")
            db.conn.execute("DELETE FROM identities")
        now = time.time()
        counts["identities"] = _merge_identities(data.get("identities", []), now)
        db.conn.execute("DELETE FROM layouts")
        for l in data.get("layouts", []):
            db.conn.execute("INSERT INTO layouts (name, config, created_at, updated_at) VALUES (?,?,?,?)", [l["name"], json.dumps(l["config"]), now, now])
            counts["layouts"] += 1
        if "dashboards" in data:
            db.conn.execute("DELETE FROM dashboards")
            for d in data["dashboards"]:
                db.conn.execute("INSERT INTO dashboards (name, config, created_at, updated_at) VALUES (?,?,?,?)", [d["name"], json.dumps(d["config"]), now, now])
                counts["dashboards"] += 1
        db.conn.commit()
    for k, v in (data.get("settings") or {}).items():
        if k in SETTINGS_KEYS and v is not None:
            db.set_setting(k, v)
    return counts


# ---------------------------------------------------------------- fleet actions: moving cameras between sites

def handoff(camera_ids: list[str] | None = None) -> dict:
    """A partial export for moving cameras to another site: the chosen cameras (all when None) WITH their
    passwords, the neighbour links among them, and the named people/vehicles. Only ever served down the hub
    tunnel (api.config_handoff checks); the hub passes it straight to the destination and never stores it."""
    cams = [c for c in db.cameras() if camera_ids is None or c["id"] in camera_ids]
    ids = {c["id"] for c in cams}
    links = [l for l in db.all("SELECT cam_a, cam_b, min_s, max_s, one_way FROM camera_links") if l["cam_a"] in ids and l["cam_b"] in ids]
    return {"format": 1, "partial": True, "exported_at": time.time(), "site_version": __version__, "cameras": cams,
            "camera_links": links, "identities": _export_identities(), "learned": {c["id"]: learned_state(c["id"]) for c in cams}}


def _address(c: dict) -> tuple:
    return (str(c.get("host") or "").lower(), int(c.get("rtsp_port") or 554), c.get("main_path") or "/main")


def _free_id(want: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9_]", "_", want.lower())[:28] or "cam"
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def merge_cameras(data: dict) -> dict:
    """Add the cameras of a (partial) export to this site WITHOUT replacing anything the site already has:
    no settings, layouts, dashboards or other cameras' links are touched. A camera whose address (host, RTSP
    port, main path) is already here updates that camera (so a retried move doesn't duplicate it); a camera
    whose id is taken by a different camera gets a new id. Passwords in the export are used; when absent the
    existing one is kept. Returns counts and the id mapping {source id: id here}."""
    if data.get("format") != 1:
        raise ValueError("unknown export format")
    existing = db.cameras()
    by_id = {c["id"]: c for c in existing}
    by_addr = {_address(c): c["id"] for c in existing}
    taken = set(by_id)
    ids: dict[str, str] = {}
    updated: list[str] = []
    for c in data.get("cameras", []):
        src_id = str(c["id"])
        if _address(c) in by_addr:
            new_id = by_addr[_address(c)]
            updated.append(new_id)
        elif src_id not in taken and re.fullmatch(r"[a-z0-9_]{1,32}", src_id):
            new_id = src_id
        else:
            new_id = _free_id(src_id, taken)
        taken.add(new_id)
        ids[src_id] = new_id
        pw = c.get("password")
        if pw is None:
            pw = (by_id.get(new_id) or {}).get("password") or ""
        cam = {k: c[k] for k in CAMERA_COLS if k in c}
        enabled = bool(c.get("enabled", 1))
        if new_id in by_id and by_id[new_id]["enabled"]:
            enabled = True   # updating a camera in place never switches it off (e.g. a copy already disabled at the source)
        cam.update(id=new_id, password=pw, enabled=int(enabled))
        db.upsert_camera(cam)
        if c.get("ptz_config") is not None:
            db.set_ptz_config(new_id, c["ptz_config"])
    counts = {"cameras": len(ids), "updated": updated, "camera_links": 0, "identities": 0, "ids": ids}
    with db.lock:
        for l in data.get("camera_links", []):
            a, b = ids.get(l["cam_a"]), ids.get(l["cam_b"])
            if not (a and b):
                continue
            db.conn.execute("INSERT OR REPLACE INTO camera_links (cam_a, cam_b, min_s, max_s, one_way) VALUES (?,?,?,?,?)",
                            [a, b, l["min_s"], l["max_s"], int(bool(l.get("one_way")))])
            counts["camera_links"] += 1
        counts["identities"] = _merge_identities(data.get("identities", []), time.time())
        db.conn.commit()
    learned = {"baseline": 0, "parked": 0, "corrections": 0}
    for src_id, state in (data.get("learned") or {}).items():
        if str(src_id) in ids:
            got = adopt_learned(ids[str(src_id)], state, str(data.get("source") or ""))
            learned["baseline"] += int(bool(got["baseline"]))
            learned["parked"] += got["parked"]
            learned["corrections"] += got["corrections"]
    counts["learned"] = learned
    return counts


def mark_moved(camera_id: str, to: str) -> None:
    """Remember where a camera went (the camera itself is only disabled, so its events stay here)."""
    moved = db.get_setting("moved_cameras") or {}
    moved[camera_id] = {"to": to[:120], "at": time.time()}
    db.set_setting("moved_cameras", moved)


# ---------------------------------------------------------------- fleet actions: what a moved camera has learned

CORRECTIONS_CARRIED = 30


def _fmt_synopsis(j: dict) -> str:
    return f"{j.get('summary', '')} [threat: {j.get('threat_level', '?')}]"   # as Pipeline.correction_examples shows them


def learned_state(camera_id: str) -> dict:
    """What this site has learned about one camera, so it moves with the camera: the "what's normal" baseline
    (baseline.py, per label and hour), parked-spot memory (parked.py) and the operator's synopsis corrections
    (the few-shot pairs Pipeline.correction_examples feeds Qwen)."""
    from . import baseline, parked
    out: dict = {"baseline": None, "parked": parked.load(camera_id), "corrections": []}
    try:
        out["baseline"] = baseline.entry(camera_id)
    except Exception:
        out["baseline"] = None
    rows = db.all("SELECT camera_class, corrected_at, synopsis_original, synopsis_json FROM events WHERE camera_id=? AND corrected_at IS NOT NULL "
                  "AND synopsis_original IS NOT NULL AND status != 'masked' ORDER BY corrected_at DESC LIMIT ?", [camera_id, CORRECTIONS_CARRIED])
    seen = set()
    for r in rows:
        try:
            orig, corr = _fmt_synopsis(json.loads(r["synopsis_original"])), _fmt_synopsis(json.loads(r["synopsis_json"]))
        except (ValueError, TypeError, AttributeError):
            continue
        if orig != corr and (orig, corr) not in seen:
            seen.add((orig, corr))
            out["corrections"].append({"original": orig, "corrected": corr, "label": r["camera_class"], "at": r["corrected_at"]})
    for s in db.get_setting(f"correction_seed:{camera_id}") or []:   # it may have moved here from elsewhere before
        if (s.get("original"), s.get("corrected")) not in seen and len(out["corrections"]) < CORRECTIONS_CARRIED:
            seen.add((s.get("original"), s.get("corrected")))
            out["corrections"].append(s)
    return out


def adopt_learned(camera_id: str, learned: dict, source: str = "") -> dict:
    """Seed a camera that just arrived with what its old site learned. Non-destructive: this site's own baseline
    wins when it has more history; parked spots and corrections are added to (never replace) what is here."""
    from . import baseline, parked
    out = {"baseline": False, "parked": 0, "corrections": 0}
    if not isinstance(learned, dict):
        return out
    if learned.get("baseline"):
        out["baseline"] = baseline.seed(camera_id, learned["baseline"], source)
    spots = [p for p in learned.get("parked") or [] if isinstance(p, dict) and isinstance(p.get("box"), list)]
    if spots:
        mine = parked.load(camera_id)
        have = {json.dumps(p.get("box")) for p in mine}
        new = [p for p in spots if json.dumps(p["box"]) not in have]
        if new:
            parked.save(camera_id, mine + new)
            out["parked"] = len(new)
    corr = [c for c in learned.get("corrections") or [] if isinstance(c, dict) and c.get("original") and c.get("corrected")]
    if corr:
        key = f"correction_seed:{camera_id}"
        mine = db.get_setting(key) or []
        have = {(c.get("original"), c.get("corrected")) for c in mine}
        new = [{k: c.get(k) for k in ("original", "corrected", "label", "at")} for c in corr if (c["original"], c["corrected"]) not in have]
        if new:
            db.set_setting(key, (mine + new)[-CORRECTIONS_CARRIED * 2:])
            out["corrections"] = len(new)
    return out


def remove_camera(camera_id: str) -> str:
    """Undo of a camera that was just added here (a fleet move rolled back, an add undone): delete it when no event
    references it, otherwise only disable it. Returns "deleted", "disabled" or "missing"."""
    if not db.one("SELECT 1 FROM cameras WHERE id=?", [camera_id]):
        return "missing"
    if db.one("SELECT 1 FROM events WHERE camera_id=? LIMIT 1", [camera_id]):
        db.execute("UPDATE cameras SET enabled=0 WHERE id=?", [camera_id])
        return "disabled"
    with db.lock:
        db.conn.execute("DELETE FROM camera_links WHERE cam_a=? OR cam_b=?", [camera_id, camera_id])
        db.conn.execute("DELETE FROM rule_events WHERE camera_id=?", [camera_id])
        db.conn.execute("DELETE FROM settings WHERE key IN (?, ?)", [f"parked:{camera_id}", f"correction_seed:{camera_id}"])
        db.conn.execute("DELETE FROM cameras WHERE id=?", [camera_id])
        db.conn.commit()
    seeds = db.get_setting("baseline_seeds") or {}
    if seeds.pop(camera_id, None) is not None:
        db.set_setting("baseline_seeds", seeds)
    return "deleted"


# ---------------------------------------------------------------- fleet actions: copying event history

HISTORY_PAGE = 200
HISTORY_SKIP = {"id", "clip", "clip_start", "journey_id", "migrated_from"}   # clips stay; journeys are per site
HISTORY_STATUSES = ("verified", "rejected", "masked", "error")                  # not still being processed
FILE_RX = re.compile(r"[a-z_0-9]+\.jpg")
MAX_FILE_BYTES = 8 * 1024 * 1024


def _event_cols() -> list[str]:
    return [r["name"] for r in db.all("PRAGMA table_info(events)")]


def _vec_b64(table: str, event_id: int) -> str | None:
    row = db.one(f"SELECT embedding FROM {table} WHERE rowid=?", [event_id])
    return base64.b64encode(row["embedding"]).decode() if row else None


def _basename(p: str) -> str:
    return str(p).replace("\\", "/").rsplit("/", 1)[-1]


def export_history(camera_ids: list[str], after_id: int = 0, limit: int = HISTORY_PAGE) -> dict:
    """One page of these cameras' event metadata for a fleet move: every column except the clip (and its
    journey), the names of the event's images (snapshot and crops; fetched one by one through
    /api/events/{id}/media/{name}) and its re-ID / vehicle fingerprints. Tunnel-only (api.config_history)."""
    from .config import settings
    if not camera_ids:
        return {"events": [], "next_after_id": None, "total": 0}
    marks = ",".join("?" * len(camera_ids))
    status = ",".join("?" * len(HISTORY_STATUSES))
    limit = max(1, min(HISTORY_PAGE, int(limit)))
    rows = db.all(f"SELECT * FROM events WHERE camera_id IN ({marks}) AND status IN ({status}) AND id > ? ORDER BY id LIMIT ?",
                  [*camera_ids, *HISTORY_STATUSES, int(after_id), limit])
    out = []
    for r in rows:
        d = settings.data_dir / "events" / str(r["id"])
        files = sorted(f.name for f in d.iterdir() if f.is_file() and FILE_RX.fullmatch(f.name)) if d.is_dir() else []
        ev = {k: v for k, v in r.items() if k not in HISTORY_SKIP}
        ev.update(src_id=r["id"], files=files, reid=_vec_b64("reid_vec", r["id"]), vehicle=_vec_b64("vehicle_vec", r["id"]))
        out.append(ev)
    total = None
    if not after_id:
        total = db.one(f"SELECT COUNT(*) n FROM events WHERE camera_id IN ({marks}) AND status IN ({status})", [*camera_ids, *HISTORY_STATUSES])["n"]
    return {"events": out, "next_after_id": rows[-1]["id"] if len(rows) == limit else None, "total": total}


def import_history(data: dict) -> dict:
    """Add another site's events for cameras that moved here: new event ids, camera ids remapped, a
    `migrated_from` marker, images arriving separately (import_history_files). Idempotent: an event already
    copied (same source site and event id) is not copied twice. Returns {"ids": {source event id: id here}}."""
    src = data.get("source") or {}
    site, site_id = str(src.get("site") or "")[:120], str(src.get("site_id") or "")[:40]
    cam_map = {str(k): str(v) for k, v in (data.get("cameras") or {}).items()}
    here = {c["id"] for c in db.cameras()}
    cols = set(_event_cols()) - HISTORY_SKIP
    ids: dict[str, int] = {}
    added = skipped = 0
    for e in data.get("events") or []:
        cam = cam_map.get(str(e.get("camera_id")))
        src_id = int(e["src_id"])
        if not cam or cam not in here:
            skipped += 1
            continue
        old = db.one("SELECT id FROM events WHERE json_extract(migrated_from, '$.site_id')=? AND json_extract(migrated_from, '$.event_id')=?",
                     [site_id, src_id])
        if old:
            ids[str(src_id)] = old["id"]
            continue
        fields = {k: v for k, v in e.items() if k in cols}
        fields.update(camera_id=cam, migrated_from=json.dumps({"site": site, "site_id": site_id, "event_id": src_id, "camera_id": e.get("camera_id")}))
        snap = fields.pop("snapshot", None)
        fields.setdefault("track_id", f"moved-{src_id}")
        fields.setdefault("camera_class", "person")
        fields.setdefault("start_ts", time.time())
        fields.setdefault("created_at", time.time())
        names = list(fields)
        new_id = db.execute_insert(f"INSERT INTO events ({','.join(names)}) VALUES ({','.join('?' * len(names))})", [fields[k] for k in names])
        if snap:
            db.execute("UPDATE events SET snapshot=? WHERE id=?", [f"events/{new_id}/{_basename(snap)}", new_id])
        for table, key in (("reid_vec", "reid"), ("vehicle_vec", "vehicle")):
            if e.get(key):
                try:
                    with db.lock:
                        db.conn.execute(f"INSERT INTO {table}(rowid, embedding) VALUES (?,?)", (new_id, base64.b64decode(e[key])))
                except Exception:   # a fingerprint of another width (older site): the backfill makes a new one
                    pass
        ids[str(src_id)] = new_id
        added += 1
    return {"ids": ids, "added": added, "skipped": skipped}


def import_history_files(files: list[dict]) -> int:
    """Images of copied events: [{event_id (here), name, data (base64)}]. Only jpg, only events copied here."""
    from .config import settings
    n = 0
    for f in files or []:
        name, eid = str(f.get("name") or ""), int(f.get("event_id") or 0)
        if not FILE_RX.fullmatch(name) or not db.one("SELECT 1 FROM events WHERE id=? AND migrated_from IS NOT NULL", [eid]):
            continue
        raw = base64.b64decode(f.get("data") or "")
        if not raw or len(raw) > MAX_FILE_BYTES:
            continue
        d = settings.data_dir / "events" / str(eid)
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(raw)
        n += 1
    return n
