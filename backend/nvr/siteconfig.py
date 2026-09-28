"""Export / import of a site's configuration (not its recordings or events): cameras, zones, named places,
site rules, PTZ settings, neighbour topology, named people/vehicles with their looks, timeline layouts,
retention and briefing settings. The fleet hub pulls this nightly; restoring a dead site is
"install, enrol, import". Camera passwords are not exported: they're re-entered on restore."""
from __future__ import annotations

import base64
import json
import time

from . import __version__
from .db import db

SETTINGS_KEYS = ("retention_policy", "briefing")


def export_config() -> dict:
    cams = []
    for c in db.cameras():
        cams.append({k: v for k, v in c.items() if k != "password"})
    links = db.all("SELECT cam_a, cam_b, min_s, max_s, one_way FROM camera_links")
    idents = []
    for i in db.all("SELECT id, name, kind, notes, sightings, watch, watch_note FROM identities"):
        looks = db.all("SELECT embedding, sightings FROM identity_looks WHERE identity_id=?", [i["id"]])
        idents.append({**{k: i[k] for k in ("name", "kind", "notes", "sightings", "watch", "watch_note")},
                       "looks": [{"embedding": base64.b64encode(lk["embedding"]).decode(), "sightings": lk["sightings"]} for lk in looks]})
    layouts = [{"name": r["name"], "config": json.loads(r["config"])} for r in db.all("SELECT name, config FROM layouts")]
    dashboards = [{"name": r["name"], "config": json.loads(r["config"])} for r in db.all("SELECT name, config FROM dashboards")]
    return {"format": 1, "exported_at": time.time(), "site_version": __version__, "cameras": cams, "camera_links": links,
            "identities": idents, "layouts": layouts, "dashboards": dashboards, "settings": {k: db.get_setting(k) for k in SETTINGS_KEYS}}


def import_config(data: dict, replace_identities: bool = False) -> dict:
    """Apply an export. Cameras are upserted (existing passwords kept), topology and layouts replaced,
    identities merged by name (or replaced), settings overwritten. Returns counts."""
    if data.get("format") != 1:
        raise ValueError("unknown backup format")
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
        for i in data.get("identities", []):
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
            counts["identities"] += 1
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
