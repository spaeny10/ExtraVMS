"""Web push for alerts: VAPID keys made once and kept in the kv table, one subscription per browser, a
per-user choice of alert kinds. Sending goes through pywebpush; a dead subscription (404/410) is dropped."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

import sqlalchemy as sa

from . import db
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


async def notify_alert(org_id: str, site: dict, kind: str, detail: dict) -> int:
    """Push an opened alert to every member of the org who chose this kind. Returns pushes sent."""
    members = db.rows(sa.select(db.memberships.c.user_id).where(db.memberships.c.org_id == org_id))
    uids = {m["user_id"] for m in members} | {u["id"] for u in db.rows(sa.select(db.users.c.id).where(db.users.c.is_super == True))}  # noqa: E712
    if not uids:
        return 0
    subs = db.rows(sa.select(db.push_subscriptions).where(db.push_subscriptions.c.user_id.in_(list(uids))))
    title = {"offline": f"{site['name']} is offline", "camera_down": f"{site['name']}: camera down", "disk": f"{site['name']}: disk low",
             "clock": f"{site['name']}: clock skew", "event_high": f"{site['name']}: high-priority event",
             "event_policy": f"{site['name']}: site rule broken", "event_watched": f"{site['name']}: watched person seen"}.get(kind, f"{site['name']}: {kind}")
    body = detail.get("text") or detail.get("synopsis") or detail.get("name") or ", ".join(detail.get("problems") or []) or ""
    url = f"/s/{site['id']}/#timeline?cam={detail.get('camera_id')}&event={detail.get('id')}" if detail.get("id") and detail.get("camera_id") else f"/s/{site['id']}/"
    payload = {"title": title, "body": body[:180], "url": url, "kind": kind, "site_id": site["id"]}
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
