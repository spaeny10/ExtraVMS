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

from . import backup, detector, direct, mediamtx, retention, vlmroute
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
        # detector_fallback / detector_stalled / vlm_fallback_active / site_link_down: the hub opens an alert per kind while it is listed
        # (hub alerts.py)
        "health_alerts": detector.health_alerts(p) + vlmroute.router.health_alerts() + ([link] if link else []),
        "site_link_down": bool(link),
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
    }
