"""Web push for alerts: VAPID keys made once and kept in the kv table, one subscription per browser, a
per-user choice of alert kinds. Sending goes through pywebpush; a dead subscription (404/410) is dropped."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

import sqlalchemy as sa

from . import alerts, auth, db
from .config import settings

log = logging.getLogger("hub.push")
DEFAULT_KINDS = ["offline", "event_policy", "event_watched", "event_high"]
_sender: Callable | None = None   # tests inject a fake


def set_sender(fn: Callable | None) -> None:
    global _sender
    _sender = fn


def vapid() -> dict:
    row = db.one(sa.select(db.kv).where(db.kv.c.key == "vapid"))
    if row:
        return row["value"]
    from py_vapid import Vapid
    import base64
    v = Vapid()
    v.generate_keys()
    from cryptography.hazmat.primitives import serialization
    priv = v.private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    pub = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    value = {"private_pem": priv, "public": pub}
    db.insert(db.kv, {"key": "vapid", "value": value})
    return value


def subscribe(user_id: str, sub: dict, kinds: list[str] | None, ua: str) -> None:
    endpoint = sub.get("endpoint")
    if not endpoint:
        raise ValueError("subscription needs an endpoint")
    db.run(sa.delete(db.push_subscriptions).where(db.push_subscriptions.c.endpoint == endpoint))
    db.insert(db.push_subscriptions, {"user_id": user_id, "endpoint": endpoint, "sub": sub, "kinds": kinds or DEFAULT_KINDS,
                                      "created_at": time.time(), "ua": ua[:200]})


def unsubscribe(user_id: str, endpoint: str) -> None:
    db.run(sa.delete(db.push_subscriptions).where(db.push_subscriptions.c.user_id == user_id, db.push_subscriptions.c.endpoint == endpoint))


def subscriptions_for(user_id: str) -> list[dict]:
    return db.rows(sa.select(db.push_subscriptions).where(db.push_subscriptions.c.user_id == user_id))


def _send_one(sub: dict, payload: dict) -> bool:
    """True if the subscription is still good."""
    if _sender is not None:
        return _sender(sub, payload)
    from pywebpush import WebPushException, webpush
    keys = vapid()
    try:
        webpush(subscription_info=sub["sub"], data=json.dumps(payload), vapid_private_key=keys["private_pem"],
                vapid_claims={"sub": f"mailto:{settings.push_contact or 'admin@' + settings.public_url.split('//')[-1]}"}, ttl=3600)
        return True
    except WebPushException as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (404, 410):
            return False
        log.warning("push failed: %s", e)
        return True


def title_name(site: dict) -> str:
    """How a notification names the server: "Site · Server" when the server sits in a Site with another name (a
    multi-server Site, or one renamed), else just the server's name, so one-server Sites read as they always did."""
    loc = db.one(sa.select(db.locations.c.name).where(db.locations.c.id == site["location_id"])) if site.get("location_id") else None
    lname = (loc or {}).get("name") or ""
    if lname and lname.strip().casefold() != (site.get("name") or "").strip().casefold():
        return f"{lname} · {site['name']}"
    return site["name"]


async def notify_alert(org_id: str, site: dict, kind: str, detail: dict) -> int:
    """Push an opened alert to every member of the org who may see this server and chose this kind. Returns pushes sent."""
    members = db.rows(sa.select(db.memberships.c.user_id).where(db.memberships.c.org_id == org_id))
    uids = {m["user_id"] for m in members if auth.can_see_server(m["user_id"], org_id, site)}
    uids |= {u["id"] for u in db.rows(sa.select(db.users.c.id).where(db.users.c.is_super == True))}  # noqa: E712
    if not uids:
        return 0
    subs = db.rows(sa.select(db.push_subscriptions).where(db.push_subscriptions.c.user_id.in_(list(uids))))
    name = title_name(site)
    title = {"offline": f"{name} is offline", "camera_down": f"{name}: camera down", "disk": f"{name}: disk low",
             "clock": f"{name}: clock skew", "event_high": f"{name}: high-priority event",
             "event_policy": f"{name}: site rule broken", "event_watched": f"{name}: watched person seen"}.get(kind, f"{name}: {kind}")
    body = detail.get("text") or detail.get("synopsis") or detail.get("name") or ", ".join(detail.get("problems") or []) or ""
    url = f"/s/{site['id']}/#timeline?cam={detail.get('camera_id')}&event={detail.get('id')}" if detail.get("id") and detail.get("camera_id") else f"/s/{site['id']}/"
    payload = {"title": title, "body": body[:180], "url": url, "kind": kind, "site_id": site["id"],
               "location_id": site.get("location_id")}
    sent = 0
    for s in subs:
        if kind not in (s["kinds"] or DEFAULT_KINDS):
            continue
        ok = await asyncio.to_thread(_send_one, s, payload)
        if ok:
            sent += 1
        else:
            db.run(sa.delete(db.push_subscriptions).where(db.push_subscriptions.c.id == s["id"]))
    return sent


# ---------------------------------------------------------------- SOC (soc.py)

SOC_KIND = "soc"                 # pages to SOC staff: sent to every subscription they have (duty, not a preference)
# Customers: "the SOC is handling an incident at your Site". Opt-in only, and not in DEFAULT_KINDS: the same event
# already pushes as event_high to whoever chose that, so sending this to event_high subscriptions too was a duplicate.
# The UI's PushCard lists it from GET /api/push/vapid `kinds` (label: "SOC incident at my site"); it only ever fires
# for Sites the customer has opted into SOC monitoring.
INCIDENT_KIND = "soc_incident"
CUSTOMER_KINDS = (*alerts.KINDS, INCIDENT_KIND)   # every kind a customer subscription may choose


def _incident_text(incident: dict) -> tuple[str, str]:
    where = " · ".join(x for x in (incident.get("org_name"), incident.get("location_name")) if x) or "Site"
    return f"{where}: {incident.get('priority', '')} priority incident".strip(), (incident.get("title") or "")[:180]


async def _send(subs: list[dict], payload: dict) -> int:
    sent = 0
    for s in subs:
        ok = await asyncio.to_thread(_send_one, s, payload)
        if ok:
            sent += 1
        else:
            db.run(sa.delete(db.push_subscriptions).where(db.push_subscriptions.c.id == s["id"]))
    return sent


async def notify_soc(incident: dict, audience: str = "operators") -> int:
    """Page the SOC about an incident. operators: SOC staff on shift (soc.on_shift_ids); supervisors: SOC supervisors
    and hub administrators, on shift or not (an escalation must reach someone). Returns pushes sent."""
    from . import soc
    if audience == "supervisors":
        uids = {r["id"] for r in db.rows(sa.select(db.users.c.id).where(sa.or_(db.users.c.soc_role == "supervisor",
                                                                                 db.users.c.is_super == True)))}  # noqa: E712
    else:
        uids = soc.on_shift_ids()
    if not uids:
        return 0
    subs = db.rows(sa.select(db.push_subscriptions).where(db.push_subscriptions.c.user_id.in_(list(uids))))
    title, body = _incident_text(incident)
    payload = {"title": title, "body": body, "url": f"/soc/incidents/{incident['id']}", "kind": SOC_KIND, "incident_id": incident["id"],
               "location_id": incident.get("location_id"), "priority": incident.get("priority"), "audience": audience}
    return await _send(subs, payload)


async def notify_incident_customers(incident: dict) -> int:
    """Tell the customer the SOC has a high-priority incident open at their Site: real members who can see the Site
    (not SOC staff widened in, not hub administrators: notify_soc covers them), only on subscriptions that chose
    soc_incident (event_high already pushes the same event). Only monitored Sites have incidents."""
    org_id, lid = incident["org_id"], incident["location_id"]
    members = db.rows(sa.select(db.memberships.c.user_id, db.memberships.c.all_sites).where(db.memberships.c.org_id == org_id))
    uids = {m["user_id"] for m in members
            if m["all_sites"] is not False or lid in auth.granted_location_ids(m["user_id"], org_id)}
    if not uids:
        return 0
    subs = [s for s in db.rows(sa.select(db.push_subscriptions).where(db.push_subscriptions.c.user_id.in_(list(uids))))
            if INCIDENT_KIND in (s["kinds"] or DEFAULT_KINDS)]
    _, body = _incident_text(incident)
    payload = {"title": f"{incident.get('location_name') or 'Your site'}: the SOC is reviewing a high-priority alarm", "body": body,
               "url": f"/sites/{lid}/alerts", "kind": INCIDENT_KIND, "incident_id": incident["id"], "location_id": lid}
    return await _send(subs, payload)
