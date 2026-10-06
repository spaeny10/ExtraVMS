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

Towing needs corroboration. The synopsis model's `towing` flag alone has been wrong (a zero-turn mower's own
cutting deck read as a trailer; a pickup "towing" a light tower it merely drove past), and a towing rule raises a
HIGH alert. So when the synopsis says towing, confirm_towing() asks a strict, focused yes/no question about the
vehicle's crops and wide frame, and code checks the answer's geometry: the towed object must be on the side the
vehicle drove away from (behind it), not ahead of or beside it. The outcome is stored in
synopsis_json["towing_check"] = {"confirmed", "reason", ...}; the model's own `towing` stays as it was for display.
A towing rule breaks only when the check confirmed it; no answer (model down, timeout) means not confirmed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

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


def model_says_towing(e: dict) -> bool:
    """The synopsis model's claim: its towing flag, or for older synopses without one, the wording."""
    s = e.get("synopsis_json") or {}
    if isinstance(s.get("towing"), bool):
        return s["towing"]
    text = " ".join(filter(None, [e.get("synopsis"), s.get("activity"), *(s.get("tags") or [])]))
    return bool(TOWING_RE.search(text))


def is_towing(e: dict) -> bool:
    """Towing as far as the site rules are concerned: the model said so AND the second look confirmed it."""
    chk = (e.get("synopsis_json") or {}).get("towing_check") or {}
    return chk.get("confirmed") is True and model_says_towing(e)


# ---------------------------------------------------------------- towing confirmation (second, focused look)

TOWING_TASK = "unusual_review"   # the escalation task: the larger remote model when one is configured, else local
TOWING_TIMEOUT_S = 240
MOVE_MIN = 0.05                  # the track must move this far (fraction of the frame) for a direction to count
TOWING_SIDES = ("left", "right", "farther", "nearer", "none")
TOWING_SCHEMA = {
    "type": "object",
    "properties": {
        "vehicle": {"type": "string", "description": "what the vehicle in the green box is, a few words"},
        "behind": {"type": "string", "description": "what is directly attached to or behind the vehicle, or 'nothing'"},
        "connection": {"type": "string", "description": "the hitch, tongue, tow bar or chains you can see, or 'none visible'"},
        "towing": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "towed": {"type": "string", "description": "what is being towed, or empty"},
        "towed_side": {"type": "string", "enum": list(TOWING_SIDES),
                       "description": "where the towed object is relative to the vehicle in the FIRST (wide) image"},
    },
    "required": ["vehicle", "behind", "connection", "towing", "towed_side"],
}
TOWING_SYSTEM = (
    "You double-check one claim from a security camera system: that a vehicle is towing something. Judge only what is "
    "visible. Towing means the vehicle is physically connected to and pulling a SEPARATE trailer or piece of equipment "
    "through a hitch, tongue or tow bar (a trailer, a light tower or generator on its own wheels, a boat trailer, a car on a "
    "dolly). Answer no for: a mower's own cutting deck, attached implements (buckets, forks, plows, spreaders, decks), the "
    "vehicle's own bed, cargo or anything loaded in it, and objects merely parked nearby or passing in front of or behind "
    "the vehicle in the picture. If you cannot see a connection between them, answer unclear. towed_side: in the first "
    "(wide) image, where the towed object is relative to the vehicle: left, right, farther (higher in the picture, further "
    "from the camera), nearer (lower in the picture, closer to the camera), or none."
)


def travel(path: list | None) -> tuple[float, float] | None:
    """(dx, dy) of the vehicle's foot point from the first to the last track point (normalized frame units)."""
    if not path or len(path) < 2:
        return None
    (x0, y0), (x1, y1) = [((p[1] + p[3]) / 2, p[4]) for p in (path[0], path[-1])]
    return x1 - x0, y1 - y0


def trailing_sides(path: list | None) -> set[str] | None:
    """Sides of the vehicle (in the picture) that are behind it as it drives; None if it barely moved."""
    d = travel(path)
    if d is None or (abs(d[0]) < MOVE_MIN and abs(d[1]) < MOVE_MIN):
        return None
    dx, dy = d
    sides = set()
    if abs(dx) >= MOVE_MIN:
        sides.add("left" if dx > 0 else "right")      # driving to the right: what it pulls is on its left
    if abs(dy) >= MOVE_MIN:
        sides.add("farther" if dy > 0 else "nearer")  # coming toward the camera: what it pulls is farther away
    return sides


def _direction_words(path: list | None) -> str:
    d = travel(path)
    if d is None:
        return "without moving"
    parts = []
    if abs(d[0]) >= MOVE_MIN:
        parts.append("left to right" if d[0] > 0 else "right to left")
    if abs(d[1]) >= MOVE_MIN:
        parts.append("toward the camera" if d[1] > 0 else "away from the camera")
    return ", ".join(parts) or "without moving"


def judge_towing(answer: dict, path: list | None) -> dict:
    """The focused answer plus the geometry check -> {confirmed, reason, answer, direction}."""
    said = str(answer.get("towing", "")).strip().lower()
    side = str(answer.get("towed_side", "")).strip().lower()
    towed = str(answer.get("towed") or answer.get("behind") or "").strip()[:80] or "something"
    keep = {k: str(answer.get(k, ""))[:160] for k in ("vehicle", "behind", "connection", "towing", "towed", "towed_side")}
    out = {"answer": keep, "direction": _direction_words(path), "model": answer.get("_model")}
    if said != "yes":
        what = keep["vehicle"] or "the vehicle"
        why = "no hitch or tow bar to a separate trailer was seen" if said == "no" else "a connection could not be seen"
        return {**out, "confirmed": False, "reason": f"Second look: {what} is not towing ({why}; {keep['behind'] or 'nothing'} behind it)."}
    if side not in TOWING_SIDES or side == "none":
        return {**out, "confirmed": False, "reason": f"Second look said towing a {towed}, but could not place it next to the vehicle."}
    sides = trailing_sides(path)
    if sides is None:
        return {**out, "confirmed": True,
                "reason": f"Second look confirmed a hitched {towed} ({keep['connection'] or 'connection seen'}); "
                          "the vehicle barely moved, so the direction was not checked."}
    if side in sides:
        return {**out, "confirmed": True,
                "reason": f"Second look confirmed a hitched {towed} behind the vehicle (on its {side} side while it drove "
                          f"{out['direction']})."}
    return {**out, "confirmed": False,
            "reason": f"Second look said towing a {towed}, but it is on the vehicle's {side} side while the vehicle drove "
                      f"{out['direction']}: ahead of or beside it, not behind it (e.g. a mower deck or something passed by)."}


def _towing_images(e: dict) -> list[bytes]:
    """The wide frame first, then up to three crops of the vehicle (the synopsis keyframes)."""
    from .config import settings
    d = settings.data_dir / "events" / str(e["id"])
    kf = (e.get("detections") or {}).get("keyframes") or []
    files = [k["file"] for k in kf if k.get("kind") == "wide"][:1] + [k["file"] for k in kf if k.get("kind") == "crop"][:3]
    return [(d / f).read_bytes() for f in files if (d / f).exists()]


async def confirm_towing(event_id: int) -> dict | None:
    """After a synopsis that says towing: the second, focused look. Stores and returns synopsis_json["towing_check"]
    (None when the synopsis doesn't claim towing). Never raises: any failure is stored as not confirmed."""
    try:
        e = db.event(event_id)
        if not e or e["camera_class"] != "vehicle" or not e.get("synopsis_json") or not model_says_towing(e):
            return None
        images = _towing_images(e)
        if not images:
            result = {"confirmed": False, "reason": "Towing not confirmed: no frames of the vehicle to check."}
        else:
            from .vlmroute import router
            text = ("The first image is the wide shot (green box = the vehicle; amber box = the camera's detection); the "
                    "others are close-ups of it in time order. Is this vehicle physically connected to and pulling a "
                    "separate trailer or piece of equipment via a "
                    "hitch, tongue or tow bar? Answer no for mower decks, attached implements, beds or cargo, or objects merely "
                    "nearby. Answer as JSON.")
            try:
                answer = await asyncio.wait_for(router.chat_json(TOWING_TASK, TOWING_SYSTEM, text, images, TOWING_SCHEMA,
                                                                 num_predict=260, temperature=0.1), TOWING_TIMEOUT_S)
                result = judge_towing(answer, e.get("path"))
            except Exception as ex:  # noqa: BLE001 - fail safe: no confirmation, no HIGH alert
                log.warning("event %s: towing check failed (%s: %s); not treated as towing", event_id, type(ex).__name__, ex)
                result = {"confirmed": False, "error": f"{type(ex).__name__}: {str(ex)[:160]}",
                          "reason": "Towing not confirmed: the second look could not run (model unavailable)."}
        result["checked_at"] = round(time.time(), 1)
        cur = db.event(event_id) or e   # re-read: keep anything written meanwhile
        db.update_event(event_id, synopsis_json={**(cur.get("synopsis_json") or {}), "towing_check": result})
        log.info("event %s: towing %s - %s", event_id, "confirmed" if result["confirmed"] else "not confirmed", result["reason"])
        return result
    except Exception:  # noqa: BLE001 - never break the synopsis worker
        log.exception("event %s: towing check failed", event_id)
        return None


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
