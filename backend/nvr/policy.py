"""Site rules the operator writes per camera, checked by code once Qwen has described a vehicle.

Rule shape (cameras.policies, JSON list):
    {"kind": "towing", "asset": "solar light tower", "allowed": ["BIGView truck"], "priority": "high"}
  -> a vehicle Qwen saw towing / hitched to something, whose fingerprint does not match one of the *named*
     vehicles in `allowed` (People & vehicles), breaks the rule.

A broken rule is stored on the event (events.policy = {"kind", "text", "priority"}), lifts its priority,
puts it under Needs attention on Home and into the search index. The rule text is also given to Qwen so its
own threat rating agrees. A vehicle the system has never seen breaks the rule once, until it is named.
"""
from __future__ import annotations

import json
import logging
import re

from .db import db

log = logging.getLogger("nvr.policy")

KINDS = ("towing",)
TOWING_RE = re.compile(r"\b(tow(s|ing|ed)?|hitch(ed|ing)?|hauling|pulling (a|the) (trailer|tower))\b", re.I)


def rules(camera: dict | None) -> list[dict]:
    raw = (camera or {}).get("policies") or []
    if isinstance(raw, str):
        raw = json.loads(raw)
    return [r for r in raw if r.get("kind") in KINDS]


def labels_needed(camera: dict | None) -> set[str]:
    """Object labels Qwen must describe on this camera for its rules to be checkable."""
    return {"vehicle"} if rules(camera) else set()


def prompt_lines(camera: dict | None) -> str | None:
    """The rules in plain words for Qwen's threat judgement."""
    out = []
    for r in rules(camera):
        allowed = ", ".join(f"'{n}'" for n in r.get("allowed") or []) or "no vehicle"
        out.append(f"Site rule from the operator: the {r.get('asset', 'equipment')}s here belong to the site. "
                   f"Only these known vehicles may tow or hitch up one: {allowed}. A vehicle towing one that is not "
                   f"listed as a known vehicle above is a {r.get('priority', 'high')} threat; say so in threat_reason. "
                   f"Set towing=true only if the vehicle is actually pulling or hitched to something, not merely parked near it.")
    return "\n".join(out) or None


def is_towing(e: dict) -> bool:
    s = e.get("synopsis_json") or {}
    if isinstance(s.get("towing"), bool):
        return s["towing"]
    text = " ".join(filter(None, [e.get("synopsis"), s.get("activity"), *(s.get("tags") or [])]))
    return bool(TOWING_RE.search(text))


def check(event_id: int, camera: dict | None = None) -> dict | None:
    """Evaluate the camera's rules for a described vehicle event; store and return the broken rule (or None)."""
    from . import identities
    e = db.event(event_id)
    if not e or e["status"] != "verified" or e["camera_class"] != "vehicle":
        return None
    camera = camera or db.one("SELECT * FROM cameras WHERE id=?", [e["camera_id"]])
    broken = None
    for r in rules(camera):
        if r["kind"] == "towing" and is_towing(e):
            m = identities.match_identity("vehicle", event_id)
            name = m[0]["name"] if m else None
            allowed = [a.lower() for a in r.get("allowed") or []]
            if name and name.lower() in allowed:
                continue
            who = f"recognised as '{name}', which is not allowed" if name else "not a recognised vehicle"
            towed = (e.get("synopsis_json") or {}).get("towed") or r.get("asset", "equipment")
            broken = {"kind": "towing", "priority": r.get("priority", "high"),
                      "text": f"Unknown vehicle towing a {towed}: {who} to tow a {r.get('asset', 'equipment')} "
                              f"(allowed: {', '.join(r.get('allowed') or []) or 'none'})"}
            break
    if (e.get("policy") or None) != broken:
        db.update_event(event_id, policy=broken)
        from . import baseline
        baseline.apply(event_id, rescore=False)  # the broken rule lifts (or no longer lifts) the priority
        if broken:
            log.info("event %s breaks a site rule: %s", event_id, broken["text"])
    return broken


def recheck(camera_id: str, days: float = 7) -> int:
    """After a camera's rules change: re-evaluate its recent described vehicles. Returns how many break a rule."""
    import time
    camera = db.one("SELECT * FROM cameras WHERE id=?", [camera_id])
    n = 0
    for r in db.all("SELECT id FROM events WHERE camera_id=? AND camera_class='vehicle' AND status='verified' "
                    "AND synopsis IS NOT NULL AND start_ts >= ?", [camera_id, time.time() - days * 86400]):
        if check(r["id"], camera):
            n += 1
    return n
