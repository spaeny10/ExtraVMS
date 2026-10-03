"""Export / import of a site's configuration (not its recordings or events): cameras, zones, named places,
site rules, PTZ settings, neighbour topology, named people/vehicles with their looks, timeline layouts,
retention and briefing settings. The fleet hub pulls this nightly; restoring a dead site is
"install, enrol, import". Camera passwords are not exported: they're re-entered on restore.

Fleet actions (hub/hub/fleet_actions.py) move cameras between sites with `handoff` (a partial export of some
cameras WITH their passwords, served only down the hub tunnel; see api.config_handoff) and `merge_cameras`
(adds those cameras to this site without touching its own settings, layouts, dashboards or other cameras)."""
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
            "camera_links": links, "identities": _export_identities()}


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
    return counts


def mark_moved(camera_id: str, to: str) -> None:
    """Remember where a camera went (the camera itself is only disabled, so its events stay here)."""
    moved = db.get_setting("moved_cameras") or {}
    moved[camera_id] = {"to": to[:120], "at": time.time()}
    db.set_setting("moved_cameras", moved)
