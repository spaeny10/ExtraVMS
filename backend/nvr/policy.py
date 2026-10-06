"""Site rules the operator writes per camera, checked by code once Qwen has described a vehicle.

Rule shapes (cameras.policies, JSON list):
    {"kind": "towing", "asset": "solar light tower", "allowed": ["BIGView truck"], "priority": "high"}
  -> a vehicle Qwen saw towing / hitched to something, whose fingerprint does not match one of the *named*
     vehicles in `allowed` (People & vehicles), breaks the rule.
    {"kind": "entry", "area": "South Exterior Door", "allowed": ["Shawn"], "priority": "high"}
  -> a person whose track starts at that named place (an exterior door) and who is not recognized as one of
     the named people in `allowed` breaks the rule. Checked right after verification (no Qwen needed).

  -> PPE zones (zones of type "ppe", ppe.py) are site rules too: a person who stayed in one without the
     required hard hat / hi-vis vest breaks it (kind "ppe", medium priority unless the zone says otherwise).

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

KINDS = ("towing", "entry")
ENTRY_WINDOW_S = 2.5   # the track must be at the door within this long of its start to count as coming in
TOWING_RE = re.compile(r"\b(tow(s|ing|ed)?|hitch(ed|ing)?|hauling|pulling (a|the) (trailer|tower))\b", re.I)


def rules(camera: dict | None) -> list[dict]:
    raw = (camera or {}).get("policies") or []
    if isinstance(raw, str):
        raw = json.loads(raw)
    return [r for r in raw if r.get("kind") in KINDS]


def labels_needed(camera: dict | None) -> set[str]:
    """Object labels Qwen must describe on this camera for its rules to be checkable (entry rules need no Qwen)."""
    return {"vehicle"} if any(r["kind"] == "towing" for r in rules(camera)) else set()


def prompt_lines(camera: dict | None) -> str | None:
    """The rules in plain words for Qwen's threat judgment."""
    out = []
    for r in rules(camera):
        allowed = ", ".join(f"'{n}'" for n in r.get("allowed") or []) or "no vehicle"
        if r["kind"] == "entry":
            out.append(f"Site rule from the operator: '{r.get('area', 'the door')}' is an exterior door. Only these known "
                       f"people may come in through it: {allowed or 'nobody'}. Anyone else entering there is a "
                       f"{r.get('priority', 'high')} threat; say so in threat_reason.")
            continue
        out.append(f"Site rule from the operator: the {r.get('asset', 'equipment')}s here belong to the site. "
                   f"Only these known vehicles may tow or hitch up one: {allowed}. A vehicle towing one that is not "
                   f"listed as a known vehicle above is a {r.get('priority', 'high')} threat; say so in threat_reason. "
                   f"Set towing=true only if the vehicle is actually pulling or hitched to something, not merely parked near it.")
    return "\n".join(out) or None


def entered_at(e: dict, area: str) -> bool:
    """The track was at `area` within ENTRY_WINDOW_S of its start (it came in through it)."""
    path = e.get("path") or []
    t0 = path[0][0] if path else e["start_ts"]
    for a in e.get("areas") or []:
        if a["name"].strip().lower() == area.strip().lower() and a["from"] - t0 <= ENTRY_WINDOW_S:
            return True
    return False


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
    if not e or e["status"] != "verified" or e["camera_class"] not in ("vehicle", "person") or e.get("ptz_preset"):
        return None  # (a PTZ camera turned away from home: its rules describe the home view)
    camera = camera or db.one("SELECT * FROM cameras WHERE id=?", [e["camera_id"]])
    broken = None
    for r in rules(camera):
        if r["kind"] == "entry" and e["camera_class"] == "person" and entered_at(e, r.get("area", "")):
            m = identities.match_identity("person", event_id)
            name = m[0]["name"] if m else None
            allowed = [a.lower() for a in r.get("allowed") or []]
            if name and name.lower() in allowed:
                continue
            who = f"recognized as '{name}', who is not on the list" if name else "not a recognized person"
            broken = {"kind": "entry", "priority": r.get("priority", "high"),
                      "text": f"Entered through {r.get('area')}: {who} (allowed: {', '.join(r.get('allowed') or []) or 'nobody'})"}
            break
        if r["kind"] == "towing" and e["camera_class"] == "vehicle" and is_towing(e):
            m = identities.match_identity("vehicle", event_id)
            name = m[0]["name"] if m else None
            allowed = [a.lower() for a in r.get("allowed") or []]
            if name and name.lower() in allowed:
                continue
            who = f"recognized as '{name}', which is not allowed" if name else "not a recognized vehicle"
            towed = (e.get("synopsis_json") or {}).get("towed") or r.get("asset", "equipment")
            broken = {"kind": "towing", "priority": r.get("priority", "high"),
                      "text": f"Unknown vehicle towing a {towed}: {who} to tow a {r.get('asset', 'equipment')} "
                              f"(allowed: {', '.join(r.get('allowed') or []) or 'none'})"}
            break
    if broken is None and e["camera_class"] == "person":
        broken = ppe_rule(e, camera)
    if (e.get("policy") or None) != broken:
        db.update_event(event_id, policy=broken)
        from . import baseline
        baseline.apply(event_id, rescore=False)  # the broken rule lifts (or no longer lifts) the priority
        if broken:
            log.info("event %s breaks a site rule: %s", event_id, broken["text"])
    return broken


def ppe_rule(e: dict, camera: dict | None) -> dict | None:
    """The PPE check's violation as a broken rule, while the camera still has that PPE zone."""
    from . import ppe
    res = (e.get("detections") or {}).get("ppe")
    if not res or res.get("verdict") != "violation":
        return None
    names = {(z.get("name") or "PPE zone").strip() for z in ppe.ppe_zones((camera or {}).get("zones"))}
    if res.get("zone") not in names:
        return None
    return {"kind": "ppe", "priority": res.get("priority") or "medium", "text": ppe.describe(res),
            "tags": ppe.tags(res)}


def recheck(camera_id: str, days: float = 7) -> int:
    """After a camera's rules change: re-evaluate its recent described vehicles. Returns how many break a rule."""
    import time
    camera = db.one("SELECT * FROM cameras WHERE id=?", [camera_id])
    n = 0
    for r in db.all("SELECT id FROM events WHERE camera_id=? AND camera_class IN ('vehicle','person') AND status='verified' "
                    "AND ptz_preset IS NULL AND start_ts >= ?", [camera_id, time.time() - days * 86400]):
        if check(r["id"], camera):
            n += 1
    return n
