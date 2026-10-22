"""The site summary a heartbeat carries to the fleet hub: a trimmed version of what Home and System show.

Everything here is computed from the same sources as /api/home and /api/system (db, MediaMTX path status,
stream health, retention and backup state) so the hub's site card agrees with the site's own pages.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import shutil
import time

import httpx

from . import aischedule, backup, detector, direct, health, mediamtx, retention, talkdown, tts, vlmroute
from .config import settings
from .db import db

STARTED_AT = time.time()
_OK = "status='verified' AND (feedback IS NULL OR json_extract(feedback, '$.verdict') IS NOT 'false_alarm')"


def link_alert(state, cams: list[dict] | None = None) -> dict | None:
    """The site_link_down health alert (health.StreamHealth.link_down) or None."""
    h = getattr(state, "health", None)
    return h.link_down(cams) if h is not None and hasattr(h, "link_down") else None


async def site_summary(state, since: float | None = None, version: str = "") -> dict:
    """`since`: attention events after this time are listed (the hub raises alerts from them)."""
    now = time.time()
    since = since or now - 60
    day_start = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    status: dict = {}
    with contextlib.suppress(httpx.HTTPError):
        status = await mediamtx.path_status()
    cams = []
    enabled = db.cameras(enabled_only=True)
    link = link_alert(state, enabled)
    for c in enabled:
        ing = state.ingests.get(c["id"])
        st = ing.status() if ing else {}
        h = state.health.camera(c["id"]) or {}
        ptz = state.ptz.status(c["id"]) if getattr(state, "ptz", None) else None
        cam = {"id": c["id"], "name": c["name"], "stream_ready": bool(status.get(c["id"], {}).get("ready")),
               "record_stream": c.get("record_stream") or "main",   # the hub's live tiles pick <id> or <id>_hd for HD
               "metadata": bool(st.get("metadata")), "onvif_events": bool(st.get("onvif_events")),
               "bitrate_mbps": h.get("bitrate_mbps"), "problems": h.get("problems") or [],
               "ptz": {"at_home": ptz["at_home"], "preset_name": ptz["preset_name"]} if ptz else None}
        if link:
            # the site link is down: one site_link_down alert, not one camera_down per camera (the hub skips
            # camera_down for cameras marked link_down; their "no video" problems say nothing more)
            cam.update(link_down=True, problems=[])
        cams.append(cam)
    today: dict[str, int] = {}
    for r in db.all(f"SELECT camera_class, COUNT(*) n FROM events WHERE {_OK} AND start_ts >= ? GROUP BY 1", [day_start]):
        today[r["camera_class"]] = r["n"]
    attention = []
    for e in db.all(f"SELECT id, camera_id, camera_class, start_ts, priority, watched, policy, synopsis FROM events "
                    f"WHERE {_OK} AND start_ts >= ? AND (priority IN ('medium','high') OR policy IS NOT NULL OR watched IS NOT NULL) "
                    f"ORDER BY start_ts DESC LIMIT 20", [since]):
        attention.append({"id": e["id"], "camera_id": e["camera_id"], "label": e["camera_class"], "start_ts": e["start_ts"],
                          "priority": e["priority"], "watched": e["watched"],
                          "policy": json.loads(e["policy"])["text"] if e["policy"] else None,
                          "synopsis": (e["synopsis"] or "")[:160]})
    rec = shutil.disk_usage(settings.recordings_dir)
    p = state.pipeline
    return {
        "now": now, "version": version, "uptime_s": round(now - STARTED_AT),
        "disk": {"free_gb": round(rec.free / 1e9), "total_gb": round(rec.total / 1e9)},
        "retention_alert": retention.alert,
        "queues": {"verify": p.verify_q.qsize(), "synopsis": p.synopsis_q.qsize()},
        "yolo_ready": detector.yolo_status(p)["yolo_ready"], "vlm_ready": p.vlm_ready, "vlm_model": settings.vlm_model,
        # storage_failing / detector_fallback / detector_stalled / vlm_fallback_active / site_link_down: the hub opens an
        # alert per kind while it is listed (hub alerts.py)
        "health_alerts": _storage_alerts() + detector.health_alerts(p) + vlmroute.router.health_alerts() + ([link] if link else []),
        "site_link_down": bool(link),
        "storage": dict(db.storage),   # {ok, error, failing_since, last_ok, reopens, errors_total} (db.py): is the database taking writes
        # statements that failed on storage in the last 15 minutes (the hub's db_errors alert), and the data dir's own
        # filesystem (on a central instance its quota'd dataset: nvr.db and event media; recordings are `disk`)
        "db_errors": db.recent_errors(), "data_disk": _data_disk(),
        # what the cameras send this server: {mbps (5-min average), today_gb, month_gb, cameras: {id: mbps}}
        "bandwidth": state.health.bandwidth(enabled) if hasattr(state.health, "bandwidth") else None,
        "cameras": cams, "today": today, "attention": attention,
        # disabled cameras, separately: `cameras` stays "enabled only" for older hubs, newer ones mark these
        # off in their cameras registry instead of "missing"
        "disabled": [{"id": c["id"], "name": c["name"]} for c in db.cameras() if not c.get("enabled", 1)],
        "backup_last": (backup.status().get("last") or {}).get("at"),
        "bitrate_mbps": round(sum(c["bitrate_mbps"] or 0 for c in cams), 2),
        # Direct-on-LAN: where a browser on this LAN can reach the server and the HTTPS cert's fingerprint (None = off)
        "direct": direct.summary(),
        # talk-down speakers and the cameras they are bound to (no address or credentials): the hub's Live tiles
        "speakers": talkdown.summary_speakers(),
        # the talk-down voice (tts.summary: the setting, installed voice ids, engines): the hub pushes its Site's voice
        # when this differs and shows which servers have it
        "talkdown_voice": _talkdown_voice(),
        # the AI schedule this server follows ({key, mode, qwen: on | limited, reason, until, outside}): the hub pushes
        # its Site's again when the key differs, and shows each server's state
        "ai_schedule": aischedule.summary(),
    }


# ---------------------------------------------------------------- the hub's customer digest

DIGEST_ATTENTION_MAX = 10
DIGEST_UNUSUAL_MAX = 3
_WC_SQL = "json_extract(CASE WHEN json_valid(synopsis_json) THEN synopsis_json END, '$.weapon_check')"
_PPE_SQL = "json_extract(CASE WHEN json_valid(detections) THEN detections END, '$.ppe.{}')"


def _reviewed(e: dict) -> bool:
    """Someone looked: an operator's verdict on the event, or a corrected description."""
    fb = e.get("feedback")
    if isinstance(fb, str):
        with contextlib.suppress(ValueError):
            fb = json.loads(fb)
    return bool((isinstance(fb, dict) and fb.get("verdict")) or e.get("corrected_at"))


def digest_facts(since: float, until: float | None = None) -> dict:
    """What the hub's customer digest is written from (GET /api/digest/facts), as structured facts rather than prose,
    so the hub words weapons itself and can't upgrade a possible weapon to a confirmed one:
    - weapons: every event in the period whose description claimed a weapon and was not cleared, with the second
      look read under today's rules (weaponcheck.effective: confirmed takes a HIGH-confidence, consistent answer);
    - attention: high-priority events nobody has reviewed yet (and every unconfirmed weapon), PPE left out (PPE has
      its own one-line summary), newest first;
    - unusual: the most unusual other events, with their reasons ("the longest visit on record");
    - rules: broken site rules other than PPE, by kind; ppe: violations by zone; counts by label.
    No descriptions for weapon events: the model's own wording is what the second look corrects."""
    from . import baseline, weaponcheck
    until = until or time.time()
    names = {c["id"]: c["name"] for c in db.cameras()}
    cam = lambda cid: names.get(cid, cid)  # noqa: E731
    where = f"{_OK} AND start_ts >= ? AND start_ts <= ?"
    p = [since, until]
    counts: dict[str, int] = {}
    for r in db.all(f"SELECT camera_class, COUNT(*) n FROM events WHERE {where} GROUP BY 1", p):
        counts[r["camera_class"]] = r["n"]

    def policy(r: dict) -> dict:
        pol = r.get("policy")
        if isinstance(pol, str):
            with contextlib.suppress(ValueError):
                pol = json.loads(pol)
        return pol if isinstance(pol, dict) else {}

    weapons, weapon_ids = [], set()
    for r in db.all(f"SELECT id, camera_id, camera_class, start_ts, priority, feedback, corrected_at, {_WC_SQL} AS wc "
                    f"FROM events WHERE {where} AND {_WC_SQL} IS NOT NULL ORDER BY start_ts DESC LIMIT 50", p):
        with contextlib.suppress(ValueError, TypeError):
            eff = weaponcheck.effective(json.loads(r["wc"]))
            if not eff or eff["verdict"] == "not_confirmed":
                continue
            weapons.append({"id": r["id"], "camera_id": r["camera_id"], "camera": cam(r["camera_id"]), "label": r["camera_class"],
                            "start_ts": r["start_ts"], "priority": r["priority"], "reviewed": _reviewed(r), **eff})
            weapon_ids.add(r["id"])

    attention = []
    for r in db.all(f"SELECT id, camera_id, camera_class, start_ts, priority, watched, policy, synopsis, feedback, corrected_at, "
                    f"threat FROM events WHERE {where} AND (priority = 'high' OR id IN ({','.join('?' * len(weapon_ids)) or 'NULL'})) "
                    f"ORDER BY start_ts DESC LIMIT 200", [*p, *weapon_ids]):
        if _reviewed(r) or (policy(r).get("kind") == "ppe" and r["id"] not in weapon_ids):
            continue
        w = next((x for x in weapons if x["id"] == r["id"]), None)
        attention.append({"id": r["id"], "camera_id": r["camera_id"], "camera": cam(r["camera_id"]), "label": r["camera_class"],
                          "start_ts": r["start_ts"], "priority": r["priority"], "watched": r["watched"],
                          "policy": policy(r).get("text"),
                          # weapon events carry the check, not the model's description of the weapon
                          "weapon": w, "synopsis": None if w else (r["synopsis"] or "")[:200]})
        if len(attention) >= DIGEST_ATTENTION_MAX:
            break
    listed = {a["id"] for a in attention} | weapon_ids

    unusual = []
    for r in db.all(f"SELECT id, camera_id, camera_class, start_ts, anomaly, anomaly_json, synopsis, policy FROM events "
                    f"WHERE {where} AND COALESCE(anomaly, 0) >= ? ORDER BY anomaly DESC, start_ts DESC LIMIT 20",
                    [*p, baseline.PRIORITY_LOW]):
        if r["id"] in listed or policy(r).get("kind") == "ppe":
            continue
        an = json.loads(r["anomaly_json"]) if r["anomaly_json"] else {}
        unusual.append({"id": r["id"], "camera_id": r["camera_id"], "camera": cam(r["camera_id"]), "label": r["camera_class"],
                        "start_ts": r["start_ts"], "score": round(r["anomaly"] or 0, 3),
                        "reasons": [baseline.reason_text(x) for x in an.get("reasons") or []],
                        "synopsis": (r["synopsis"] or "")[:160]})
        if len(unusual) >= DIGEST_UNUSUAL_MAX:
            break

    rules: dict[str, dict] = {}
    for r in db.all(f"SELECT id, policy FROM events WHERE {where} AND policy IS NOT NULL ORDER BY start_ts DESC", p):
        pol = policy(r)
        if not pol or pol.get("kind") == "ppe":
            continue
        k = rules.setdefault(pol.get("kind") or "rule", {"kind": pol.get("kind") or "rule", "count": 0, "priority": pol.get("priority"),
                                                         "text": (pol.get("text") or "")[:160], "examples": []})
        k["count"] += 1
        if len(k["examples"]) < 2:
            k["examples"].append(r["id"])

    zones: dict[tuple, dict] = {}
    missing: dict[str, int] = {}
    total = 0
    for r in db.all(f"SELECT id, camera_id, {_PPE_SQL.format('zone')} AS zone, {_PPE_SQL.format('violation')} AS missing "
                    f"FROM events WHERE {where} AND {_PPE_SQL.format('verdict')} = 'violation'", p):
        total += 1
        z = zones.setdefault((r["camera_id"], r["zone"] or "PPE zone"), {"zone": r["zone"] or "PPE zone", "camera": cam(r["camera_id"]),
                                                                          "count": 0, "example": r["id"]})
        z["count"] += 1
        with contextlib.suppress(ValueError, TypeError):
            for item in json.loads(r["missing"] or "[]"):
                missing[item] = missing.get(item, 0) + 1
    from . import ppe
    return {"version": 1, "since": since, "until": until, "counts": counts, "weapons": weapons, "attention": attention,
            "unusual": unusual, "rules": sorted(rules.values(), key=lambda k: -k["count"]),
            "ppe": {"count": total, "zones": sorted(zones.values(), key=lambda z: -z["count"])[:5],
                    "missing": {ppe.ITEM_WORDS.get(k, k): v for k, v in sorted(missing.items(), key=lambda kv: -kv[1])}}}


def _talkdown_voice() -> dict | None:
    try:
        return tts.summary()
    except Exception:  # noqa: BLE001 - a voices folder problem must not cost the heartbeat
        return None


def _data_disk() -> dict | None:
    """Free / total GB of the filesystem holding data_dir (statvfs; no database needed)."""
    try:
        du = shutil.disk_usage(settings.data_dir)
    except OSError:
        return None
    return {"free_gb": round(du.free / 1e9, 1), "total_gb": round(du.total / 1e9, 1)}


def _storage_alerts() -> list[dict]:
    a = health.storage_alert()
    return [a] if a else []


def minimal_summary(state, version: str = "", error: str = "") -> dict:
    """The heartbeat when site_summary can't be built (2026-10-10: every query said "disk I/O error", the heartbeat was
    skipped, and the hub saw nothing wrong for 6 hours). Only what needs no database: the storage problem, the
    detector and AI alerts (the hub closes those kinds when a heartbeat doesn't list them), disk space. No `cameras`
    key: the hub leaves its camera registry and camera_down alerts as they were."""
    now = time.time()
    alerts = _storage_alerts()
    p = getattr(state, "pipeline", None)
    for part in (lambda: detector.health_alerts(p) if p is not None else [], vlmroute.router.health_alerts):
        with contextlib.suppress(Exception):
            alerts += part()
    out = {"now": now, "version": version, "uptime_s": round(now - STARTED_AT), "degraded": True, "error": error[:200],
           "storage": dict(db.storage), "db_errors": db.recent_errors(), "data_disk": _data_disk(),
           "retention_alert": retention.alert, "health_alerts": alerts}
    with contextlib.suppress(Exception):   # kept in memory: needs no database once loaded
        out["ai_schedule"] = aischedule.summary()
    with contextlib.suppress(OSError):
        rec = shutil.disk_usage(settings.recordings_dir)
        out["disk"] = {"free_gb": round(rec.free / 1e9), "total_gb": round(rec.total / 1e9)}
    return out
