"""Site actions: the same confirm-card pattern as the hub's fleet actions (hub/hub/fleet_actions.py), for the verbs
that make sense on one site, typed into this site's Find → Ask box:

  "Rename cam3 to Loading Dock"            rename_camera        admin
  "Set retention to 7 days"                set_retention        admin
  "Stop describing vehicles on cam2"       set_synopsis_labels  admin
  "Lock Side Yard footage 3-4 pm today"    lock_footage         operator

plan_for(text, role) has no side effects: {"action": "none"} for anything that is not one of these (questions never
are; the Ask box then asks Qwen as before), else a plan with a card (what changes, what stays, warnings, open
questions). execute(plan_id, role) carries it out. Roles come from the hub (x-hub-role) when the request arrived
through it; on the site's own LAN page there is no sign-in, as for every other endpoint. The parser is rules only
(no model call): the verbs are few and the wording is short. Small helpers are copied from the hub module on
purpose, the site and the hub are deployed separately.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
import uuid

from . import keep
from .config import settings
from .db import db

PLAN_TTL_S = 600
MAX_LOCK_S = 7 * 86400
ROLES = ["viewer", "operator", "admin", "owner"]
LABELS = ("person", "vehicle")

VERBS: dict[str, dict] = {
    "rename_camera": {"title": "Rename a camera", "role": "admin", "examples": ["Rename cam3 to Loading Dock", "Rename the gate camera to North Gate"]},
    "set_retention": {"title": "Set continuous recording retention", "role": "admin", "examples": ["Set retention to 7 days", "Keep 14 days of recording"]},
    "set_synopsis_labels": {"title": "Choose what Qwen describes", "role": "admin",
                            "examples": ["Stop describing vehicles on cam2", "Describe only people on Gate", "Start describing vehicles on Back Lot"]},
    "lock_footage": {"title": "Lock footage", "role": "operator", "examples": ["Lock Side Yard footage 3-4 pm today", "Lock Gate footage from 9am to 10:30am yesterday"]},
}

_plans: dict[str, dict] = {}

POLITE = re.compile(r"^\s*(please|pls|kindly|ok|okay|now|go ahead and|can you|could you|would you|will you|i want to|i'd like to|"
                    r"i would like to|we need to|let's|lets)\b[\s,]*", re.I)
QUESTION = re.compile(r"^\s*(how|what|what's|whats|when|where|who|whom|whose|why|which|did|does|do(?!\s+not\b)|is|are|was|were|has|have|had|"
                      r"show|list|find|search|any|anyone|anybody|count|tell|give|should|shall|may|might)\b", re.I)
VERB_WORDS = re.compile(r"\b(rename|retention|retain|keep|set|lock|protect|describe|describing)\b", re.I)
RULES: list[tuple[str, re.Pattern]] = [(a, re.compile(rx, re.I)) for a, rx in [
    ("rename_camera", r"^rename\s+(?:the\s+)?(?P<cam>.+?)\s+(?:camera\s+)?(?:to|as)\s+(?P<name>.+)$"),
    ("set_retention", r"^(?:set\s+)?(?:the\s+)?(?:continuous\s+)?(?:retention|recording)\s+(?:to\s+)?(?P<days>\d+)\s*(?:days?|d)\b"),
    ("set_retention", r"^(?:keep|retain)\s+(?P<days>\d+)\s*(?:days?|d)\b(?:\s+of\s+(?:continuous\s+)?(?:recordings?|footage|video))?$"),
    ("set_synopsis_labels", r"^(?P<neg>stop|don't|dont|do\s+not|no\s+longer)\s+(?:describing|describe)\s+(?P<what>[a-z]+)\s+(?:on|at|for|in|from)\s+(?P<cam>.+)$"),
    ("set_synopsis_labels", r"^(?:start\s+describing|also\s+describe|describe)\s+(?P<only>only\s+)?(?P<what>[a-z]+)(?P<only2>\s+only)?\s+(?:on|at|for|in)\s+(?P<cam>.+)$"),
    ("lock_footage", r"^(?:lock|protect|preserve)\s+(?:the\s+)?(?:footage|recordings?|video)\s+(?:on|of|from|at)\s+(?P<cam>.+?)\s+(?P<when>(?:from\s+|between\s+)?\d.*)$"),
    ("lock_footage", r"^(?:lock|protect|preserve)\s+(?:the\s+)?(?P<cam>.+?)\s+(?:camera\s+)?(?:footage|recordings?|video)\s+(?P<when>.+)$"),
]]
WHAT = {"people": ["person"], "person": ["person"], "persons": ["person"], "humans": ["person"],
        "vehicles": ["vehicle"], "vehicle": ["vehicle"], "cars": ["vehicle"], "car": ["vehicle"], "trucks": ["vehicle"],
        "everything": list(LABELS), "both": list(LABELS), "all": list(LABELS), "anything": list(LABELS)}


class ActionError(Exception):
    pass


def allows(role: str | None, needed: str) -> bool:
    """No hub role (the site's own page on its LAN) allows everything, as for every other site endpoint."""
    if role is None:
        return True
    return role in ROLES and ROLES.index(role) >= ROLES.index(needed)


def _clean(text: str) -> tuple[str, bool]:
    t, polite = text.strip(), False
    while True:
        m = POLITE.match(t)
        if not m or not t[m.end():]:
            break
        t, polite = t[m.end():], True
    t = t.strip()
    if QUESTION.match(t) or (t.endswith("?") and not polite):
        return t, False
    return t.rstrip("?.! "), bool(VERB_WORDS.search(t))


# ---- times (as in hub/hub/fleet_actions.py)

def _hm(h: int, m: int, ap: str | None) -> tuple[int, int] | None:
    if ap:
        if not 1 <= h <= 12:
            return None
        h = (h % 12) + (12 if ap.startswith("p") else 0)
    return (h, m) if 0 <= h <= 23 and 0 <= m <= 59 else None


TIME_RANGE = re.compile(r"(?:from\s+|between\s+)?(?P<a>\d{1,2})(?::(?P<am>\d{2}))?\s*(?P<ap>[ap]\.?m\.?)?\s*(?:-|–|to|and|until|till)\s*"
                        r"(?P<b>\d{1,2})(?::(?P<bm>\d{2}))?\s*(?P<bp>[ap]\.?m\.?)?", re.I)


def parse_range(when: str, now: float | None = None) -> tuple[float, float, str] | None:
    """'3-4 pm today' / 'from 9am to 10:30am yesterday' -> (start_ts, end_ts, label) in this site's local time."""
    now = now or time.time()
    w = when.strip().lower()
    day = "today"
    m = re.search(r"\b(today|yesterday|\d{4}-\d{2}-\d{2})\b", w)
    if m:
        day, w = m.group(1), (w[:m.start()] + w[m.end():]).strip()
    r = TIME_RANGE.search(w)
    if not r:
        return None
    ap, bp = (r.group("ap") or "").replace(".", ""), (r.group("bp") or "").replace(".", "")
    ha, hb, ma, mb = int(r.group("a")), int(r.group("b")), int(r.group("am") or 0), int(r.group("bm") or 0)
    inferred = False
    if bp and not ap:
        ap, inferred = bp, True
    if ap and not bp:
        bp = ap
    a, b = _hm(ha, ma, ap or None), _hm(hb, mb, bp or None)
    if a and b and inferred and a >= b and ap == "pm":
        a = _hm(ha, ma, "am")
    if not a or not b:
        return None
    today = dt.datetime.fromtimestamp(now).date()
    if day == "yesterday":
        d = today - dt.timedelta(days=1)
    elif day == "today":
        d = today
    else:
        try:
            d = dt.date.fromisoformat(day)
        except ValueError:
            return None
    start = dt.datetime.combine(d, dt.time(*a)).timestamp()
    end = dt.datetime.combine(d, dt.time(*b)).timestamp()
    if end <= start:
        end += 86400
    return start, end, f"{a[0]:02d}:{a[1]:02d}-{b[0]:02d}:{b[1]:02d} {day}"


# ---- names

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def pick_camera(words: str, cams: list[dict]) -> tuple[dict | None, str | None]:
    q = re.sub(r"^(?:the)\s+|\s+(?:cameras?|cams?)$", "", _norm(words)).strip()
    if not q:
        return None, "Which camera?"
    keys = [(c, [_norm(c["name"]), _norm(c["id"])]) for c in cams]
    for t in (lambda ks: q in ks, lambda ks: q.replace(" ", "") in [k.replace(" ", "") for k in ks],
              lambda ks: any(q in k for k in ks), lambda ks: any(set(q.split()) <= set(k.split()) for k in ks)):
        hits = [c for c, ks in keys if t(ks)]
        if len(hits) == 1:
            return hits[0], None
        if len(hits) > 1:
            return None, f'Which camera do you mean by "{words}": {", ".join(h["name"] for h in hits[:8])}?'
    return None, f'There is no camera called "{words}". Known: {", ".join(c["name"] for c in cams[:12]) or "none"}.'


def parse(text: str) -> dict | None:
    for action, rx in RULES:
        m = rx.match(text)
        if not m:
            continue
        g = m.groupdict()
        out: dict = {"action": action}
        if action == "rename_camera":
            out.update(camera=g["cam"], new_name=g["name"].strip().strip("\"'"))
        elif action == "set_retention":
            out.update(days=int(g["days"]))
        elif action == "set_synopsis_labels":
            out.update(camera=g["cam"], labels=WHAT.get(g["what"].lower(), []),
                       mode="remove" if g.get("neg") else "only" if (g.get("only") or g.get("only2")) else "add")
        elif action == "lock_footage":
            out.update(camera=g["cam"], when=g["when"])
        return out
    return None


def _say(labels: list[str]) -> str:
    return " and ".join("people" if x == "person" else "vehicles" for x in labels) or "nothing"


def _labels_after(current: list[str], mode: str, labels: list[str]) -> list[str]:
    out = [x for x in current if x not in labels] if mode == "remove" else list(labels) if mode == "only" else [*current, *labels]
    return [x for x in LABELS if x in out]


def build(parsed: dict, text: str) -> dict:
    a = parsed["action"]
    needs: list[str] = []
    moves: list[str] = []
    stays: list[str] = []
    warnings: list[str] = []
    cams = db.cameras()
    p: dict = {"id": "sp_" + uuid.uuid4().hex[:12], "action": a, "text": text[:500], "camera": None, "created_at": time.time(),
               "expires_at": time.time() + PLAN_TTL_S}
    cam = None
    if "camera" in parsed:
        cam, q = pick_camera(parsed["camera"], cams)
        if q:
            needs.append(q)
        p["camera"] = {"id": cam["id"], "name": cam["name"]} if cam else None
    title = VERBS[a]["title"]
    if a == "rename_camera":
        p["new_name"] = (parsed.get("new_name") or "")[:80]
        if not p["new_name"]:
            needs.append("What should the new name be?")
        if cam:
            title = f'Rename "{cam["name"]}" to "{p["new_name"]}"'
            moves.append(f'"{cam["name"]}" ({cam["id"]}) becomes "{p["new_name"]}"')
        stays.append("Its id, recordings, events, zones and rules are unchanged")
    elif a == "set_retention":
        days = int(parsed.get("days") or 0)
        p["days"] = days
        if not 1 <= days <= 365:
            needs.append("Retention must be between 1 and 365 days.")
        now = keep.site_policy()["continuous_days"]
        title = f"Keep {days} days of continuous recording"
        moves.append(f"This site keeps {days} day{'s' if days != 1 else ''} of continuous recording (now {now})")
        stays.append("Event clips, locked footage and per-camera overrides follow their own rules")
    elif a == "set_synopsis_labels":
        labels = [x for x in parsed.get("labels") or [] if x in LABELS]
        if not labels:
            needs.append("Describe what: people or vehicles?")
        if cam:
            cur = list(cam["synopsis_labels"]) if cam.get("synopsis_labels") is not None else list(settings.synopsis_labels)
            new = _labels_after(cur, parsed.get("mode") or "add", labels)
            p["labels"] = new
            title = f'Qwen describes {_say(new)} on "{cam["name"]}"'
            if new == cur and labels:
                needs.append(f'Qwen already describes {_say(cur)} on "{cam["name"]}".')
            moves.append(f'Qwen describes {_say(new)} on "{cam["name"]}" (now {_say(cur)})')
            if not new:
                warnings.append("Nothing would be described on this camera: events are still verified by YOLO but get no synopsis.")
        stays.append("YOLO still verifies every person and vehicle; past synopses are kept")
    elif a == "lock_footage":
        rng = parse_range(parsed.get("when") or "")
        if not rng:
            needs.append('Which time span? e.g. "3-4 pm today" or "from 9:00 to 10:30 yesterday".')
        else:
            p["start_ts"], p["end_ts"] = rng[0], rng[1]
            if rng[1] - rng[0] > MAX_LOCK_S:
                needs.append("A lock can cover at most 7 days.")
            if rng[0] > time.time():
                needs.append("That time hasn't happened yet.")
            if cam:
                title = f'Lock "{cam["name"]}" footage, {rng[2]}'
                fmt = lambda t: time.strftime("%a %d %b %H:%M", time.localtime(t))   # noqa: E731
                moves.append(f'"{cam["name"]}" footage from {fmt(rng[0])} to {fmt(rng[1])} is kept regardless of retention')
        stays.append("Footage outside that span follows the retention policy; the lock can be removed on the Retention page")
    p["card"] = {"title": title, "moves": moves, "stays": stays, "warnings": warnings, "blockers": [], "needs": needs,
                 "can_execute": not needs, "capacity": [], "confirm_name": None, "options": [], "inputs": [], "role": VERBS[a]["role"]}
    return p


def _sweep() -> None:
    now = time.time()
    for k in [k for k, v in _plans.items() if v["expires_at"] < now]:
        del _plans[k]


def plan_for(text: str, role: str | None) -> dict:
    cleaned, maybe = _clean(text)
    if not maybe:
        return {"action": "none"}
    parsed = parse(cleaned)
    if not parsed:
        return {"action": "none"}
    p = build(parsed, text)
    _sweep()
    _plans[p["id"]] = p
    return {**p, "summary": p["card"]["title"], "parser": "rules", "allowed": allows(role, VERBS[p["action"]]["role"])}


def execute(plan_id: str, role: str | None, user: str = "") -> dict:
    """Carry out a plan made by plan_for. Raises LookupError (expired), PermissionError (role), ActionError."""
    _sweep()
    p = _plans.get(plan_id)
    if p is None:
        raise LookupError("that plan expired (plans last 10 minutes); ask again")
    if not allows(role, VERBS[p["action"]]["role"]):
        raise PermissionError(f"needs the {VERBS[p['action']]['role']} role")
    if not p["card"]["can_execute"]:
        raise ActionError("; ".join(p["card"]["needs"]) or "this plan cannot be carried out")
    _plans.pop(plan_id, None)
    a, cam = p["action"], p.get("camera")
    if cam and not db.one("SELECT 1 FROM cameras WHERE id=?", [cam["id"]]):
        raise ActionError(f"{cam['name']} no longer exists")
    lines: list[str] = []
    if a == "rename_camera":
        db.execute("UPDATE cameras SET name=? WHERE id=?", [p["new_name"], cam["id"]])
        lines.append(f'"{cam["name"]}" is now "{p["new_name"]}"')
    elif a == "set_retention":
        stored = keep._merge(db.get_setting("retention_policy") or {}, {"continuous_days": p["days"]})
        db.set_setting("retention_policy", stored)
        lines.append(f"This site now keeps {p['days']} days of continuous recording")
    elif a == "set_synopsis_labels":
        db.execute("UPDATE cameras SET synopsis_labels=? WHERE id=?", [json.dumps(p["labels"]), cam["id"]])
        lines.append(f'Qwen now describes {_say(p["labels"])} on "{cam["name"]}"')
    elif a == "lock_footage":
        note = f"locked from Ask{f' by {user}' if user else ''}"[:200]
        lock_id = db.execute_insert("INSERT INTO locks (camera_id, start_ts, end_ts, note, created_at) VALUES (?,?,?,?,?)",
                                    [cam["id"], p["start_ts"], p["end_ts"], note, time.time()])
        lines.append(f'"{cam["name"]}" footage is locked (lock {lock_id}); remove it on the Retention page')
        p["lock_id"] = lock_id
    return {"ok": True, "lines": lines, "summary": p["card"]["title"], "action": a, "camera_id": (cam or {}).get("id"),
            "audit_id": None, "undo_until": None}
