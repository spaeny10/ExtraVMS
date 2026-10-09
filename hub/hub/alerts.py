"""Fleet alerts derived from heartbeats and live events; one open row per (site, kind, key)."""
from __future__ import annotations

import json
import time

import sqlalchemy as sa

from . import cameras, db

KINDS = ("offline", "camera_down", "disk", "clock", "detector_fallback", "detector_stalled", "vlm_fallback_active", "event_high", "event_policy", "event_watched",
         "site_link_down", "host_offline", "coverage_budget", "host_queue_growing")
# site_link_down  the server's heartbeat says every camera is unreachable at once (summary.site_link_down): the link to
#                 the Site (SpeedFusion tunnel, port forwards, the BR1's cellular uplink) is probably down. One alert for
#                 the server instead of one camera_down per camera; cameras flagged link_down raise no camera_down.
# host_offline    a central recording host (hosts.py) stopped heartbeating. Hub-level: org_id hosts.HUB_ORG, site_id =
#                 the host id; only hub administrators are notified (HUB_KINDS).
# coverage_budget the CoverageMap monthly unit budget (HUB_COVERAGEMAP_MONTHLY_UNITS) is used up: automatic coverage
#                 refreshes stopped until next month. Hub-level (org hosts.HUB_ORG, site_id "coverage", key = the month).
# host_queue_growing  a central host's work queues (capacity.work, hosts.check_work_alerts): an instance's YOLO verify
#                 queue above 20 and higher than 15 minutes earlier (key verify:<instance id>), or the shared vLLM with
#                 requests waiting for 10 minutes without a break (key vllm). Hub-level like host_offline; closes when it
#                 recovers.
HUB_KINDS = ("host_offline", "coverage_budget", "host_queue_growing")
# Server detector health, from the heartbeat's `health_alerts` list (backend nvr/detector.py): open while listed.
#   detector_fallback  the Hailo accelerator is missing or failing; YOLO runs on the CPU (or nothing can detect)
#   vlm_fallback_active  the main local Qwen (e.g. the 27B) is down and the fallback model (the 9B) is answering
#   detector_stalled   the verify queue has had events for 15 min and none finished
HEALTH_KINDS = ("detector_fallback", "detector_stalled", "vlm_fallback_active")
# Alerts about one site event. Unlike the condition kinds above (offline, disk...), which close when the condition
# clears and must re-open when it returns, an event happens once: its key is the event id, and the same event is
# re-published many times (heartbeat attention lists, synopsis, feedback, lock), so it dedupes on any row.
EVENT_KINDS = ("event_high", "event_policy", "event_watched")
on_open = None   # set by api: called with (org_id, site, kind, detail) when a new alert opens (push notifications)
EVENT_TTL_S = 24 * 3600
_camera_strikes: dict[tuple[str, str], int] = {}


def open(site: dict, kind: str, key: str = "", detail: dict | None = None) -> bool:
    q = sa.select(db.alerts.c.id).where(db.alerts.c.site_id == site["id"], db.alerts.c.kind == kind, db.alerts.c.key == key)
    if kind in EVENT_KINDS:
        # any row, open or acknowledged, within the TTL: acking an event alert closes it, and the next re-publish of
        # the same event must not open (and push) it again
        q = q.where(db.alerts.c.opened_at >= time.time() - EVENT_TTL_S)
    else:
        q = q.where(db.alerts.c.closed_at.is_(None))
    if db.one(q):
        return False
    row = db.one(sa.select(db.sites.c.retired_at).where(db.sites.c.id == site["id"]))
    if row and row["retired_at"]:
        return False   # a retired site (fleet actions) raises no new alerts
    db.insert(db.alerts, {"org_id": site["org_id"], "site_id": site["id"], "kind": kind, "key": key,
                          "opened_at": time.time(), "closed_at": None, "acked_by": None, "acked_at": None, "detail": detail or {}})
    if on_open is not None:
        try:
            on_open(site["org_id"], site, kind, detail or {})
        except Exception:  # notifications must never break alerting
            pass
    return True


def close(site: dict, kind: str, key: str = "") -> None:
    db.run(sa.update(db.alerts).where(db.alerts.c.site_id == site["id"], db.alerts.c.kind == kind, db.alerts.c.key == key,
                                      db.alerts.c.closed_at.is_(None)).values(closed_at=time.time()))


def link_down_text(site: dict) -> str:
    loc = db.one(sa.select(db.locations.c.name).where(db.locations.c.id == site["location_id"])) if site.get("location_id") else None
    return f"All cameras at {(loc or {}).get('name') or site.get('name') or 'the site'} are unreachable: the link to the site may be down"


def on_heartbeat(site: dict, summary: dict, skew_s: float) -> None:
    off = cameras.disabled_ids(site["id"])   # a camera switched off at the server is not "down"
    for cid in off:
        _camera_strikes.pop((site["id"], cid), None)
        close(site, "camera_down", cid)
    link_down = summary.get("site_link_down") is True
    if link_down:
        open(site, "site_link_down", "", {"text": link_down_text(site)})
    else:
        close(site, "site_link_down")
    live = [c for c in summary.get("cameras") or [] if isinstance(c, dict) and c.get("id") and c["id"] not in off]
    # Every camera of a server dark at once is most likely the site's link: the server says so after 90 s
    # (site_link_down). Wait 4 heartbeats (~2 min) instead of 2 before camera_down, so one link drop doesn't
    # push five camera alerts first.
    all_dark = len(live) >= 2 and all(not c.get("stream_ready") for c in live)
    strikes_needed = 4 if all_dark else 2
    for cam in summary.get("cameras") or []:
        if not isinstance(cam, dict) or not cam.get("id") or cam["id"] in off:
            continue
        if link_down and (cam.get("link_down") is True or (cam.get("link_down") is None and not cam.get("stream_ready"))):
            # covered by the one site_link_down alert: no camera_down, and any opened just before the server noticed closes
            _camera_strikes.pop((site["id"], cam["id"]), None)
            close(site, "camera_down", cam["id"])
            continue
        k = (site["id"], cam["id"])
        bad = not cam.get("stream_ready") or bool(cam.get("problems"))
        _camera_strikes[k] = _camera_strikes.get(k, 0) + 1 if bad else 0
        if _camera_strikes[k] >= strikes_needed:
            open(site, "camera_down", cam["id"], {"name": cam.get("name"), "problems": cam.get("problems") or [], "stream_ready": cam.get("stream_ready")})
        elif not bad:
            close(site, "camera_down", cam["id"])
    if summary.get("retention_alert"):
        open(site, "disk", "", summary["retention_alert"] if isinstance(summary["retention_alert"], dict) else {"message": str(summary["retention_alert"])})
    else:
        close(site, "disk")
    health = {a["kind"]: a for a in summary.get("health_alerts") or [] if isinstance(a, dict) and a.get("kind") in HEALTH_KINDS}
    for kind in HEALTH_KINDS:
        if kind in health:
            a = health[kind]
            open(site, kind, "", {k: a[k] for k in ("text", "since", "error", "queue") if a.get(k) is not None})
        else:
            close(site, kind)
    if abs(skew_s) > 30:
        open(site, "clock", "", {"skew_s": round(skew_s)})
    else:
        close(site, "clock")
    for e in summary.get("attention") or []:
        _from_event(site, e)


def on_event(site: dict, msg: dict) -> None:
    if msg.get("type") == "event" and isinstance(msg.get("event"), dict):
        e = msg["event"]
        if e.get("status") != "verified":
            return
        fb = e.get("feedback")
        if isinstance(fb, str):   # the site may send the stored JSON text rather than the parsed object
            try:
                fb = json.loads(fb)
            except ValueError:
                fb = None
        _from_event(site, {"id": e["id"], "camera_id": e.get("camera_id"), "priority": e.get("priority"),
                           "verdict": fb.get("verdict") if isinstance(fb, dict) else None,
                           "policy": (e.get("policy") or {}).get("text") if isinstance(e.get("policy"), dict) else e.get("policy"),
                           "watched": e.get("watched"), "label": e.get("camera_class"), "start_ts": e.get("start_ts"),
                           "synopsis": (e.get("synopsis") or "")[:160]})


def muted(site: dict) -> bool:
    """Fleet action "quiet alerts": event alerts for this site are muted until kv alerts_mute:<org>.until."""
    row = db.one(sa.select(db.kv.c.value).where(db.kv.c.key == f"alerts_mute:{site.get('org_id')}"))
    v = row["value"] if row else None
    if not isinstance(v, dict) or float(v.get("until") or 0) <= time.time():
        return False
    return not v.get("sites") or site.get("id") in v["sites"]


def _from_event(site: dict, e: dict) -> None:
    if muted(site):
        return
    if e.get("verdict") == "false_alarm" or e.get("priority") == "none":
        return   # someone marked it a false alarm, or the site ranked it as nothing: no alert, however it's flagged
    if not (e.get("policy") or e.get("watched") or e.get("priority") == "high"):
        return
    if e.get("camera_id") and str(e["camera_id"]) in cameras.disabled_ids(site["id"]):
        return
    key = str(e.get("id"))
    detail = {k: e.get(k) for k in ("id", "camera_id", "label", "start_ts", "priority", "synopsis")}
    if e.get("policy"):
        open(site, "event_policy", key, {**detail, "text": e["policy"]})
    elif e.get("watched"):
        open(site, "event_watched", key, {**detail, "name": e["watched"]})
    elif e.get("priority") == "high":
        open(site, "event_high", key, detail)


def expire_event_alerts() -> None:
    cutoff = time.time() - EVENT_TTL_S
    db.run(sa.update(db.alerts).where(db.alerts.c.kind.in_(EVENT_KINDS),
                                      db.alerts.c.closed_at.is_(None), db.alerts.c.opened_at < cutoff).values(closed_at=time.time()))


def open_for_org(org_id: str, site_ids: list[str] | None = None, limit: int = 200) -> list[dict]:
    q = sa.select(db.alerts).where(db.alerts.c.org_id == org_id, db.alerts.c.closed_at.is_(None)).order_by(db.alerts.c.opened_at.desc()).limit(limit)
    if site_ids is not None:
        q = q.where(db.alerts.c.site_id.in_(site_ids))
    return db.rows(q)


def ack(alert_id: int, user_id: str) -> None:
    db.run(sa.update(db.alerts).where(db.alerts.c.id == alert_id).values(acked_by=user_id, acked_at=time.time(), closed_at=time.time()))
