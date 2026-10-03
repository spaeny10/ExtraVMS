"""Fleet actions: an operator types "Migrate Ironsight to Hailo T1", "Move the front door camera from Ironsight to
Qwenbot", "Retire Ironsight", "Set Qwenbot to 7 days of recording" or "Rename cam3 on Hailo T1 to Loading Dock"
into the Ask box, and the hub carries it out across sites (which cannot talk to each other).

  plan_for(u, org, text)   no side effects: is this an instruction at all (questions never are), which action,
                           which sites and cameras (names resolved server-side against the org's real sites and
                           cameras), and a confirmation card: what moves, what stays, warnings, open questions.
  execute(plan, u)         only after Confirm (admin): does it, writes one audit_log row.

Parsing: the shared AI (vlm_proxy.complete with a strict JSON schema, prompted with the org's site and camera
names) when configured, else a small rule parser for the five verbs. A plan with unresolved names, an unsure
reading, or a blocker (a site offline) carries `needs` / `blockers` and cannot be executed.

Moving cameras: the source hands the chosen cameras over WITH their passwords (`GET /api/config/handoff`, which
the site serves only down its tunnel with `x-hub-internal: handoff`); the hub passes that straight to the
destination's `POST /api/config/merge` (adds the cameras, their zones/places/rules/labels, links among them and
named people/vehicles; the destination's own settings, layouts and cameras stay), checks the destination now
lists them, then disables them on the source (`DELETE /api/cameras/{id}` disables, never deletes: events
reference the camera). Recordings and event clips stay where they were recorded. Passwords are never stored,
logged or written to the audit row. Migrating a site does that for all its enabled cameras, then retires it:
`sites.retired_at` hides it from Fleet, Home, Find, Ask and alerts; its tunnel is left connected so its history
stays reachable at /s/<site>/.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from urllib.parse import quote, urlencode

import sqlalchemy as sa

from . import auth, db, vlm_proxy
from .agents import AgentConn, registry

log = logging.getLogger("hub.fleet_actions")

ACTIONS = ("move_cameras", "migrate_site", "retire_site", "set_retention", "rename_camera")
PLAN_TTL_S = 600
CAMERAS_CACHE_S = 30
CALL_TIMEOUT_S = 20
HANDOFF_TIMEOUT_S = 60
BITRATE_WARN_MBPS = 60
CPU_CAMERA_WARN = 4
CAMERA_FIELDS = ("id", "name", "host", "onvif_port", "rtsp_port", "username", "main_path", "sub_path", "enabled", "zones",
                 "retention_days", "scene_notes", "retention_policy", "synopsis_labels", "policies")   # the site's CameraIn

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": [*ACTIONS, "none"]},
        "source_site": {"type": "string"},
        "target_site": {"type": "string"},
        "cameras": {"type": "array", "items": {"type": "string"}},
        "days": {"type": "integer"},
        "new_name": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["action", "source_site", "target_site", "cameras", "days", "new_name", "confidence"],
    "additionalProperties": False,
}

SYSTEM = """You turn one operator instruction for a fleet of video-surveillance sites into a single action, as JSON.
Actions:
- move_cameras: move the named cameras from source_site to target_site.
- migrate_site: move every camera of source_site to target_site, then retire source_site.
- retire_site: retire source_site (hide it; no cameras move).
- set_retention: keep `days` days of continuous recording at source_site.
- rename_camera: rename cameras[0] at source_site to new_name.
- none: anything else. Questions, searches and requests about footage, events, people or counts are ALWAYS none,
  even when they mention cameras or sites. Never guess an action from a question.
Use site and camera names exactly as listed below when the operator means one of them; otherwise copy the operator's
words. Leave fields that don't apply as "" / [] / 0. confidence is high only when the instruction is explicit.
Sites and their cameras ([id]):
"""

_plans: dict[str, dict] = {}
_cameras_cache: dict[str, tuple[float, list[dict]]] = {}
_exec_lock = asyncio.Lock()


class ActionError(Exception):
    pass


# ---------------------------------------------------------------- talking to sites

async def _call(conn: AgentConn, u: dict, method: str, path: str, role: str = "viewer", query: str = "", body: dict | None = None,
                extra: dict | None = None, timeout: float = CALL_TIMEOUT_S) -> tuple[int, object]:
    headers = {"x-hub-user": u["email"], "x-hub-role": role, **(extra or {})}
    if body is not None:
        headers["content-type"] = "application/json"
    status, raw = await conn.call(method, path, query, headers, json.dumps(body).encode() if body is not None else None, timeout)
    try:
        return status, json.loads(raw.decode() or "null")
    except ValueError:
        return status, None


def _detail(data: object, status: int) -> str:
    d = data.get("detail") if isinstance(data, dict) else None
    return f"HTTP {status}{f': {str(d)[:160]}' if d else ''}"


async def site_cameras(site: dict, u: dict, fresh: bool = False) -> list[dict] | None:
    """The site's cameras (public fields, no passwords) plus live bitrate; None when offline or not answering."""
    conn = registry.get(site["id"])
    if conn is None:
        return None
    hit = _cameras_cache.get(site["id"])
    if hit and not fresh and time.time() - hit[0] < CAMERAS_CACHE_S:
        return hit[1]
    try:
        status, data = await _call(conn, u, "GET", "/api/cameras")
    except Exception as e:
        log.info("cameras of %s: %s", site["name"], e)
        return None
    if status != 200 or not isinstance(data, list):
        return None
    cams = []
    for c in data:
        health = ((c.get("status") or {}).get("health") or {}) if isinstance(c, dict) else {}
        cams.append({**{k: c.get(k) for k in CAMERA_FIELDS}, "bitrate_mbps": health.get("bitrate_mbps")})
    _cameras_cache[site["id"]] = (time.time(), cams)
    return cams


def _forget(*site_ids: str) -> None:
    for s in site_ids:
        _cameras_cache.pop(s, None)


async def _index(u: dict, org_id: str) -> list[dict]:
    """Every visible, non-retired site with its cameras (live list, or the last heartbeat's names when offline)."""
    sites = auth.visible_sites(u, org_id)
    lists = await asyncio.gather(*(site_cameras(s, u) for s in sites))
    out = []
    for s, cams in zip(sites, lists):
        live = cams is not None
        if cams is None:
            cams = [{"id": c.get("id"), "name": c.get("name"), "enabled": 1, "bitrate_mbps": c.get("bitrate_mbps")}
                    for c in ((s.get("summary") or {}).get("cameras") or []) if c.get("id")]
        out.append({"site": s, "online": registry.get(s["id"]) is not None, "live": live, "cameras": cams})
    return out


# ---------------------------------------------------------------- is it an instruction, and which one

POLITE = re.compile(r"^\s*(please|pls|kindly|ok|okay|now|go ahead and|can you|could you|would you|will you|i want to|i'd like to|"
                    r"i would like to|we need to|let's|lets)\b[\s,]*", re.I)
QUESTION = re.compile(r"^\s*(how|what|what's|whats|when|where|who|whom|whose|why|which|did|does|do|is|are|was|were|has|have|had|"
                      r"show|list|find|search|any|anyone|anybody|count|tell|give|should|shall|may|might)\b", re.I)
VERBS = re.compile(r"\b(migrate|move|transfer|relocate|retire|decommission|rename|retention|retain|keep|set)\b", re.I)


def _clean(text: str) -> tuple[str, bool]:
    """(the instruction without polite padding, whether it may be one at all)."""
    t, polite = text.strip(), False
    while True:
        m = POLITE.match(t)
        if not m or not t[m.end():]:
            break
        t, polite = t[m.end():], True
    t = t.strip()
    if QUESTION.match(t) or (t.endswith("?") and not polite):
        return t, False
    return t.rstrip("?.! "), bool(VERBS.search(t))


RULES: list[tuple[str, re.Pattern]] = [(a, re.compile(rx, re.I)) for a, rx in [
    ("rename_camera", r"^rename\s+(?:the\s+)?(?P<cam>.+?)\s+(?:camera\s+)?(?:on|at|in)\s+(?P<site>.+?)\s+(?:to|as)\s+(?P<name>.+)$"),
    ("rename_camera", r"^rename\s+(?:the\s+)?(?P<cam>.+?)\s+(?:to|as)\s+(?P<name>.+)$"),
    ("migrate_site", r"^migrate\s+(?:the\s+)?(?:site\s+)?(?P<src>.+?)\s+(?:to|into|onto|over\s+to)\s+(?P<dst>.+)$"),
    ("move_cameras", r"^(?:move|transfer|relocate)\s+(?:the\s+)?(?P<cams>.+?)\s+from\s+(?P<src>.+?)\s+(?:to|into|onto|over\s+to)\s+(?P<dst>.+)$"),
    ("move_cameras", r"^(?:move|transfer|relocate)\s+(?:the\s+)?(?P<cams>.+?)\s+(?:to|into|onto|over\s+to)\s+(?P<dst>.+)$"),
    ("retire_site", r"^(?:retire|decommission)\s+(?:the\s+)?(?:site\s+)?(?P<src>.+)$"),
    ("set_retention", r"^set\s+(?:the\s+)?(?:retention\s+(?:on|at|for|of)\s+)?(?P<site>.+?)\s+(?:retention\s+)?to\s+(?P<days>\d+)\s*(?:days?|d)\b"),
    ("set_retention", r"^(?:keep|retain)\s+(?P<days>\d+)\s*(?:days?|d)\b.*?\b(?:at|on|for)\s+(?P<site>.+)$"),
]]


def _split_cameras(s: str) -> list[str]:
    parts = re.split(r"\s*(?:,|&|\band\b)\s*", s)
    return [p for p in (re.sub(r"^(?:the)\s+|\s+cam(?:era)?s?$", "", x.strip(), flags=re.I).strip() for x in parts) if p]


def parse_rules(text: str) -> dict | None:
    for action, rx in RULES:
        m = rx.match(text)
        if not m:
            continue
        g = m.groupdict()
        out = {"action": action, "source_site": "", "target_site": "", "cameras": [], "days": 0, "new_name": "", "confidence": "high"}
        if action == "rename_camera":
            out.update(source_site=g.get("site") or "", cameras=_split_cameras(g["cam"])[:1], new_name=g["name"].strip().strip("\"'"))
        elif action == "migrate_site":
            out.update(source_site=g["src"], target_site=g["dst"])
        elif action == "move_cameras":
            out.update(source_site=g.get("src") or "", target_site=g["dst"], cameras=_split_cameras(g["cams"]))
        elif action == "retire_site":
            out.update(source_site=g["src"])
        elif action == "set_retention":
            out.update(source_site=g["site"], days=int(g["days"]))
        return out
    return None


def _first_json(raw: str) -> dict | None:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\s*|\s*```$", "", raw)
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        d = json.loads(m.group(0) if m else raw)
    except (ValueError, AttributeError):
        return None
    return d if isinstance(d, dict) else None


async def parse_ai(text: str, index: list[dict]) -> dict | None:
    """The shared AI's reading, or None when it is not configured / not answering (the rules take over)."""
    if not vlm_proxy.configured():
        return None
    lines = []
    for e in index:
        cams = ", ".join(f"{c['name']} [{c['id']}]" for c in e["cameras"]) or "no cameras"
        lines.append(f"- {e['site']['name']}{'' if e['online'] else ' (offline)'}: {cams}")
    try:
        raw = await vlm_proxy.complete([{"role": "system", "content": SYSTEM + "\n".join(lines)}, {"role": "user", "content": text}],
                                       max_tokens=300, temperature=0, schema=SCHEMA)
    except Exception as e:
        log.warning("fleet action parse: shared AI failed: %s", e)
        return None
    d = _first_json(raw) if raw else None
    if not d or d.get("action") not in (*ACTIONS, "none"):
        return None
    return {"action": d["action"], "source_site": str(d.get("source_site") or ""), "target_site": str(d.get("target_site") or ""),
            "cameras": [str(c) for c in (d.get("cameras") or []) if str(c).strip()], "days": int(d.get("days") or 0),
            "new_name": str(d.get("new_name") or "").strip(), "confidence": d.get("confidence") or "low"}


# ---------------------------------------------------------------- names -> ids

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def _pick(query: str, items: list, keys, kind: str, strip: str) -> tuple[object | None, str | None]:
    """One item for the operator's words, or (None, a question to ask)."""
    q = re.sub(rf"^(?:the)\s+|\s+(?:{strip})$", "", _norm(query)).strip()
    if not q:
        return None, f"Which {kind}?"
    names = [(it, [_norm(k) for k in keys(it) if k]) for it in items]
    tests = [lambda ks: q in ks,
             lambda ks: q.replace(" ", "") in [k.replace(" ", "") for k in ks],
             lambda ks: any(q in k for k in ks),
             lambda ks: any(set(q.split()) <= set(k.split()) for k in ks)]
    for t in tests:
        hits = [it for it, ks in names if t(ks)]
        if len(hits) == 1:
            return hits[0], None
        if len(hits) > 1:
            return None, f'Which {kind} do you mean by "{query}": {", ".join(keys(h)[0] for h in hits[:8])}?'
    known = ", ".join(keys(it)[0] for it, _ in names[:12]) or "none"
    return None, f'There is no {kind} called "{query}". Known: {known}.'


def _site_ref(e: dict) -> dict:
    s = e["site"]
    return {"id": s["id"], "name": s["name"], "online": e["online"]}


def _resolve(parsed: dict, index: list[dict]) -> dict:
    """Turn the parsed names into sites and cameras of this org; questions for whatever doesn't resolve."""
    a = parsed["action"]
    needs: list[str] = []
    out: dict = {"action": a, "source": None, "target": None, "site": None, "cameras": [], "days": parsed.get("days") or None,
                 "new_name": parsed.get("new_name") or None}
    site_keys = lambda e: [e["site"]["name"], e["site"]["id"]]   # noqa: E731
    cam_keys = lambda c: [c["name"], c["id"]]                   # noqa: E731

    def site(words: str, role: str):
        if not words.strip():
            needs.append(f"Which site should be the {role}?")
            return None
        e, q = _pick(words, index, site_keys, "site", "site")
        if q:
            needs.append(q)
        return e

    def cameras_on(e: dict, words: list[str]) -> list[dict]:
        found = []
        for w in words:
            c, q = _pick(w, e["cameras"], cam_keys, f"camera on {e['site']['name']}", "cameras?|cams?")
            if q:
                needs.append(q)
            elif c not in found:
                found.append(c)
        return found

    if a in ("move_cameras", "migrate_site"):
        src = site(parsed["source_site"], "source") if (parsed["source_site"] or a == "migrate_site") else None
        dst = site(parsed["target_site"], "destination")
        if a == "move_cameras":
            words = parsed.get("cameras") or []
            if not words:
                needs.append("Which cameras should move?")
            elif src is None and not parsed["source_site"]:
                # no source named: the camera names decide it, when they all live on one site
                owners = {}
                for e in index:
                    if dst is not None and e is dst:
                        continue
                    for w in words:
                        c, _ = _pick(w, e["cameras"], cam_keys, "camera", "cameras?|cams?")
                        if c is not None:
                            owners.setdefault(e["site"]["id"], e)
                if len(owners) == 1:
                    src = next(iter(owners.values()))
                else:
                    needs.append(f'Which site are {", ".join(words)} on now?' if owners else f'No site has a camera called {", ".join(words)}.')
            if src is not None and words:
                out["cameras"] = cameras_on(src, words)
        elif src is not None:
            out["cameras"] = [c for c in src["cameras"] if c.get("enabled", 1)]
            out["skipped"] = [c for c in src["cameras"] if not c.get("enabled", 1)]
        if src is not None and dst is not None and src is dst:
            needs.append("The source and destination are the same site.")
        out["source"] = _site_ref(src) if src else None
        out["target"] = _site_ref(dst) if dst else None
    elif a in ("retire_site", "set_retention", "rename_camera"):
        e = site(parsed["source_site"] or parsed["target_site"], "site") if (parsed["source_site"] or parsed["target_site"] or a != "rename_camera") else None
        if a == "rename_camera":
            words = parsed.get("cameras") or []
            if not words:
                needs.append("Which camera should be renamed?")
            elif e is None and not (parsed["source_site"] or parsed["target_site"]):
                owners = [(x, c) for x in index for c in [_pick(words[0], x["cameras"], cam_keys, "camera", "cameras?|cams?")[0]] if c]
                if len(owners) == 1:
                    e = owners[0][0]
                else:
                    needs.append(f'Which site is "{words[0]}" on?' if owners else f'No site has a camera called "{words[0]}".')
            if e is not None and words:
                out["cameras"] = cameras_on(e, words[:1])
            if not out["new_name"]:
                needs.append("What should the new name be?")
        if a == "set_retention" and not out["days"]:
            needs.append("How many days of recording should it keep?")
        out["site"] = _site_ref(e) if e else None
    out["needs"] = needs
    return out


def summary(p: dict) -> str:
    a = p["action"]
    name = lambda r: r["name"] if r else "?"   # noqa: E731
    cams = ", ".join(c["name"] for c in p.get("cameras") or []) or "?"
    if a == "move_cameras":
        return f"Move {cams} from {name(p['source'])} to {name(p['target'])}"
    if a == "migrate_site":
        return f"Migrate {name(p['source'])} to {name(p['target'])}"
    if a == "retire_site":
        return f"Retire {name(p['site'])}"
    if a == "set_retention":
        return f"Keep {p.get('days') or '?'} days of recording at {name(p['site'])}"
    if a == "rename_camera":
        return f'Rename "{cams}" on {name(p["site"])} to "{p.get("new_name") or "?"}"'
    return "Nothing to do"


# ---------------------------------------------------------------- preview: the confirmation card

def _zone_counts(c: dict) -> tuple[int, int, int]:
    zones = c.get("zones") or []
    places = sum(1 for z in zones if isinstance(z, dict) and z.get("type") == "area")
    return len(zones) - places, places, len(c.get("policies") or [])


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


async def preview(p: dict, u: dict) -> dict:
    a = p["action"]
    moves: list[str] = []
    stays: list[str] = []
    warnings: list[str] = []
    blockers: list[str] = []
    if a in ("move_cameras", "migrate_site") and p["source"] and p["target"]:
        src, dst = p["source"], p["target"]
        cams = p["cameras"]
        src_conn, dst_conn = registry.get(src["id"]), registry.get(dst["id"])
        if src_conn is None:
            blockers.append(f"{src['name']} is offline: camera passwords can only be handed over while it is connected.")
        if dst_conn is None:
            blockers.append(f"{dst['name']} is offline.")
        if a == "move_cameras" and not cams and not p["needs"]:
            blockers.append("No cameras to move.")
        for c in cams:
            z, pl, rules = _zone_counts(c)
            br = c.get("bitrate_mbps")
            extras = [x for x in (_plural(z, "zone") if z else "", _plural(pl, "place") if pl else "", _plural(rules, "site rule") if rules else "") if x]
            moves.append(f"Camera \"{c['name']}\" ({c.get('host') or c['id']}{f', {br:.1f} Mbps' if br else ''})"
                         f"{' with its ' + ', '.join(extras) if extras else ''}{'' if c.get('enabled', 1) else ' (disabled, stays disabled)'}")
        ids = {c["id"] for c in cams}
        if src_conn is not None and len(ids) > 1:
            try:
                st, links = await _call(src_conn, u, "GET", "/api/topology")
                n = sum(1 for l in links if l.get("cam_a") in ids and l.get("cam_b") in ids) if st == 200 and isinstance(links, list) else 0
                if n:
                    moves.append(f"{_plural(n, 'neighbour link')} between the moved cameras")
            except Exception:
                pass
        if cams:
            moves.append(f"Named people and vehicles, merged into {dst['name']} by name (names it already knows are kept)")
            moves.append(f"Camera passwords go straight from {src['name']} to {dst['name']} through the hub; they are not stored or logged")
        for c in p.get("skipped") or []:
            stays.append(f"Camera \"{c['name']}\" is disabled on {src['name']} and is not moved")
        stays.append(f"Recordings and event clips stay on {src['name']}; nothing recorded is copied. {dst['name']} records these cameras from now on")
        stays.append(f"Event history for these cameras stays searchable at {src['name']}")
        stays.append(f"{dst['name']}'s own settings, retention, layouts, dashboards and other cameras are not changed")
        if cams:
            stays.append(f"{src['name']} stops pulling the moved cameras (disabled there, not deleted, so their past events remain)")
        if a == "migrate_site":
            stays.append(f"{src['name']} is then retired: hidden from Fleet, Home, Find, Ask and alerts (Fleet → Show retired). "
                         "Its tunnel stays connected, so its history is still reachable")
            if not cams:
                warnings.append(f"{src['name']} has no enabled cameras; it will only be retired.")
        if dst_conn is not None and cams:
            dst_cams = await site_cameras(db.one(sa.select(db.sites).where(db.sites.c.id == dst["id"])), u) or []
            by_host = {str(c.get("host") or "").lower(): c for c in dst_cams}
            for c in cams:
                same = by_host.get(str(c.get("host") or "").lower())
                if same:
                    warnings.append(f"{dst['name']} already has \"{same['name']}\" at {same.get('host')}: if it is the same stream it is updated in place, not duplicated.")
            row = db.one(sa.select(db.sites).where(db.sites.c.id == dst["id"])) or {}
            dst_rate = sum(c.get("bitrate_mbps") or 0 for c in dst_cams if c.get("enabled", 1)) or ((row.get("summary") or {}).get("bitrate_mbps") or 0)
            moved_rate = sum(c.get("bitrate_mbps") or 0 for c in cams)
            if dst_rate + moved_rate > BITRATE_WARN_MBPS:
                warnings.append(f"{dst['name']} would pull about {dst_rate + moved_rate:.0f} Mbps after the move (over {BITRATE_WARN_MBPS} Mbps): "
                                "check its network and disk can keep up.")
            after = sum(1 for c in dst_cams if c.get("enabled", 1)) + len(cams)
            try:
                st, sysinfo = await _call(dst_conn, u, "GET", "/api/system", timeout=10)
                device = (sysinfo or {}).get("yolo_device") if st == 200 and isinstance(sysinfo, dict) else None
            except Exception:
                device = None
            if device == "cpu" and after > CPU_CAMERA_WARN:
                warnings.append(f"{dst['name']} runs detection on its CPU and would have {after} cameras (over {CPU_CAMERA_WARN}): "
                                "verification may fall behind.")
    elif a == "retire_site" and p["site"]:
        s = p["site"]
        moves.append(f"{s['name']} is hidden from Fleet, Home, Find, Ask and alerts (Fleet → Show retired brings it back)")
        stays.append(f"Its recordings, events and settings stay on {s['name']}; its tunnel stays connected and its page still opens")
        e_cams = [c for c in (await site_cameras(db.one(sa.select(db.sites).where(db.sites.c.id == s["id"])), u) or []) if c.get("enabled", 1)]
        if e_cams:
            warnings.append(f"{s['name']} still has {_plural(len(e_cams), 'enabled camera')}: they keep recording at the site but nobody sees "
                            f"them at the hub. To keep watching them, migrate instead (\"Migrate {s['name']} to …\").")
    elif a == "set_retention" and p["site"]:
        s = p["site"]
        conn = registry.get(s["id"])
        days = p.get("days") or 0
        if conn is None:
            blockers.append(f"{s['name']} is offline.")
        if not 1 <= days <= 365:
            blockers.append("Retention must be between 1 and 365 days.")
        current = None
        if conn is not None:
            try:
                st, pol = await _call(conn, u, "GET", "/api/retention/policy", timeout=10)
                current = ((pol or {}).get("policy") or {}).get("continuous_days") if st == 200 and isinstance(pol, dict) else None
            except Exception:
                pass
        moves.append(f"{s['name']} keeps {_plural(days, 'day')} of continuous recording{f' (now {current})' if current is not None else ''}")
        stays.append("Event clips, locked footage and per-camera overrides follow their own rules, unchanged")
        row = db.one(sa.select(db.sites).where(db.sites.c.id == s["id"])) or {}
        summ = row.get("summary") or {}
        rate, total = summ.get("bitrate_mbps"), (summ.get("disk") or {}).get("total_gb")
        if rate and total and days:
            need = rate * 86400 / 8 / 1000 * days
            if need > total:
                warnings.append(f"At {rate:.1f} Mbps, {days} days needs about {need:,.0f} GB but the recordings disk holds {total:,} GB: "
                                "the oldest footage will be trimmed sooner.")
    elif a == "rename_camera" and p["site"]:
        s = p["site"]
        if registry.get(s["id"]) is None:
            blockers.append(f"{s['name']} is offline.")
        for c in p["cameras"]:
            moves.append(f"\"{c['name']}\" ({c['id']}) on {s['name']} becomes \"{p.get('new_name') or '?'}\"")
        stays.append("Its id, recordings, events, zones and rules are unchanged")
    return {"title": summary(p), "moves": moves, "stays": stays, "warnings": warnings, "blockers": blockers,
            "needs": p["needs"], "can_execute": not p["needs"] and not blockers}


# ---------------------------------------------------------------- plans

def _sweep() -> None:
    now = time.time()
    for k in [k for k, v in _plans.items() if v["expires_at"] < now]:
        del _plans[k]


def public(p: dict) -> dict:
    cam = lambda c: {"id": c["id"], "name": c["name"], "host": c.get("host")}   # noqa: E731
    return {"id": p["id"], "action": p["action"], "summary": summary(p), "parser": p["parser"], "confidence": p["confidence"],
            "source": p["source"], "target": p["target"], "site": p["site"], "cameras": [cam(c) for c in p["cameras"]],
            "days": p["days"], "new_name": p["new_name"], "needs": p["needs"], "card": p["card"], "expires_at": p["expires_at"]}


async def build(u: dict, org_id: str, parsed: dict, text: str, parser: str, index: list[dict] | None = None) -> dict:
    index = index if index is not None else await _index(u, org_id)
    p = _resolve(parsed, index)
    p.update(id=db.new_id("p_"), org_id=org_id, user_id=u["id"], text=text[:500], parser=parser,
             confidence=parsed.get("confidence") or "low", created_at=time.time(), expires_at=time.time() + PLAN_TTL_S)
    if p["confidence"] != "high" and not p["needs"]:
        p["needs"].append(f'I read this as "{summary(p)}" but I am not sure. Say it plainly to confirm, '
                          'e.g. "Move <camera> from <site> to <site>".')
    p["card"] = await preview(p, u)
    _sweep()
    _plans[p["id"]] = p
    return p


async def plan_for(u: dict, org_id: str, text: str) -> dict:
    """{"action": "none"} for anything that isn't an instruction (the Ask box then asks the sites as before)."""
    cleaned, maybe = _clean(text)
    if not maybe:
        return {"action": "none"}
    index = await _index(u, org_id)
    parsed, parser = await parse_ai(cleaned, index), "ai"
    if parsed is None:
        parsed, parser = parse_rules(cleaned), "rules"
    if not parsed or parsed["action"] == "none":
        return {"action": "none"}
    return public(await build(u, org_id, parsed, text, parser, index))


def get(plan_id: str, org_id: str) -> dict | None:
    _sweep()
    p = _plans.get(plan_id)
    return p if p and p["org_id"] == org_id else None


def from_fields(fields: dict) -> dict:
    """An explicit plan from the API ({action, source_site, target_site, cameras, days, new_name}): no AI involved."""
    if fields.get("action") not in ACTIONS:
        raise ActionError(f"action must be one of {', '.join(ACTIONS)}")
    return {"action": fields["action"], "source_site": str(fields.get("source_site") or ""), "target_site": str(fields.get("target_site") or ""),
            "cameras": [str(c) for c in fields.get("cameras") or []], "days": int(fields.get("days") or 0),
            "new_name": str(fields.get("new_name") or ""), "confidence": "high"}


# ---------------------------------------------------------------- execute

def _site_row(ref: dict) -> dict:
    row = db.one(sa.select(db.sites).where(db.sites.c.id == ref["id"]))
    if not row:
        raise ActionError(f"{ref['name']} is no longer enrolled")
    return row


def _conn(ref: dict) -> AgentConn:
    conn = registry.get(ref["id"])
    if conn is None:
        raise ActionError(f"{ref['name']} is offline")
    return conn


def retire(site_id: str, retired: bool = True) -> None:
    db.run(sa.update(db.sites).where(db.sites.c.id == site_id).values(retired_at=time.time() if retired else None))
    if retired:
        db.run(sa.update(db.alerts).where(db.alerts.c.site_id == site_id, db.alerts.c.closed_at.is_(None)).values(closed_at=time.time()))


async def _move(p: dict, u: dict, lines: list[str], detail: dict) -> None:
    src, dst = p["source"], p["target"]
    src_conn, dst_conn = _conn(src), _conn(dst)
    _site_row(src)
    _site_row(dst)
    ids = [c["id"] for c in p["cameras"]]
    names = {c["id"]: c["name"] for c in p["cameras"]}
    moved: dict[str, str] = {}
    if ids:
        st, handoff = await _call(src_conn, u, "GET", "/api/config/handoff", "admin", urlencode({"cameras": ",".join(ids)}),
                                  extra={"x-hub-internal": "handoff"}, timeout=HANDOFF_TIMEOUT_S)
        if st != 200 or not isinstance(handoff, dict):
            raise ActionError(f"{src['name']} did not hand the cameras over ({_detail(handoff, st)}); nothing changed")
        got = {c.get("id") for c in handoff.get("cameras") or []}
        if set(ids) - got:
            raise ActionError(f"{src['name']} no longer has {', '.join(names[i] for i in set(ids) - got)}; nothing changed")
        try:
            st, res = await _call(dst_conn, u, "POST", "/api/config/merge", "admin", body={"data": handoff}, timeout=HANDOFF_TIMEOUT_S)
        finally:
            handoff = None   # the only copy of the passwords at the hub: drop it now  # noqa: F841
        if st != 200 or not isinstance(res, dict):
            raise ActionError(f"{dst['name']} could not add the cameras ({_detail(res, st)}); {src['name']} is unchanged")
        moved = {k: v for k, v in (res.get("ids") or {}).items() if k in names}
        detail["merge"] = {k: res.get(k) for k in ("cameras", "camera_links", "identities")}
        listed = await site_cameras(_site_row(dst), u, fresh=True)
        missing = [names[k] for k, v in moved.items() if not listed or v not in {c["id"] for c in listed}]
        if len(moved) != len(ids) or missing:
            raise ActionError(f"{dst['name']} does not list {', '.join(missing) or 'every camera'} after the import; "
                              f"{src['name']} keeps pulling them. Check {dst['name']}'s Cameras page")
        for sid, new_id in moved.items():
            lines.append(f"\"{names[sid]}\" now records on {dst['name']}" + (f" (as {new_id})" if new_id != sid else ""))
        if res.get("identities"):
            n = int(res["identities"])
            lines.append(f"{n} named {'person/vehicle' if n == 1 else 'people/vehicles'} added to {dst['name']}")
        failed = []
        for sid in moved:
            try:
                st, _ = await _call(src_conn, u, "DELETE", f"/api/cameras/{quote(sid)}", "admin", urlencode({"moved_to": dst["name"]}))
                if st != 200:
                    failed.append(names[sid])
            except Exception:
                failed.append(names[sid])
        if failed:
            lines.append(f"Warning: {src['name']} could not be told to stop pulling {', '.join(failed)}; disable them on its Cameras page")
            detail["source_disable_failed"] = failed
        else:
            lines.append(f"{src['name']} stopped pulling {_plural(len(moved), 'camera')} (their past events stay there)")
    detail["cameras"] = [{"id": k, "name": names[k], "new_id": v} for k, v in moved.items()]
    _forget(src["id"], dst["id"])
    if p["action"] == "migrate_site":
        retire(src["id"])
        lines.append(f"{src['name']} is retired (Fleet → Show retired)")


async def execute(p: dict, u: dict) -> dict:
    """Carry out a confirmed plan. Returns {"ok", "lines"}; one audit_log row either way (never any secret)."""
    async with _exec_lock:
        card = await preview(p, u)          # sites may have gone offline since the card was shown
        if not card["can_execute"]:
            raise ActionError("; ".join(card["needs"] + card["blockers"]) or "this plan cannot be carried out")
        lines: list[str] = []
        detail: dict = {"plan": p["id"], "action": p["action"], "text": p["text"],
                        **{k: (p[k] or {}).get("name") for k in ("source", "target", "site") if p.get(k)}}
        ok, error = True, None
        try:
            if p["action"] in ("move_cameras", "migrate_site"):
                await _move(p, u, lines, detail)
            elif p["action"] == "retire_site":
                _site_row(p["site"])
                retire(p["site"]["id"])
                lines.append(f"{p['site']['name']} is retired (Fleet → Show retired)")
            elif p["action"] == "set_retention":
                st, res = await _call(_conn(p["site"]), u, "PUT", "/api/retention/policy", "admin", body={"continuous_days": int(p["days"])})
                if st != 200:
                    raise ActionError(f"{p['site']['name']} refused ({_detail(res, st)})")
                days = ((res or {}).get("policy") or {}).get("continuous_days", p["days"]) if isinstance(res, dict) else p["days"]
                lines.append(f"{p['site']['name']} now keeps {_plural(int(days), 'day')} of continuous recording")
                detail["days"] = p["days"]
            elif p["action"] == "rename_camera":
                conn = _conn(p["site"])
                cam = p["cameras"][0]
                cams = await site_cameras(_site_row(p["site"]), u, fresh=True)
                cur = next((c for c in cams or [] if c["id"] == cam["id"]), None)
                if cur is None:
                    raise ActionError(f"{p['site']['name']} no longer has {cam['name']}")
                body = {k: cur[k] for k in CAMERA_FIELDS if cur.get(k) is not None}   # no password: the site keeps it
                body.update(name=p["new_name"], enabled=bool(cur.get("enabled", 1)))
                st, res = await _call(conn, u, "PUT", f"/api/cameras/{quote(cam['id'])}", "admin", body=body)
                if st != 200:
                    raise ActionError(f"{p['site']['name']} refused the rename ({_detail(res, st)})")
                _forget(p["site"]["id"])
                lines.append(f"\"{cam['name']}\" on {p['site']['name']} is now \"{p['new_name']}\"")
                detail.update(camera=cam["id"], old_name=cam["name"], new_name=p["new_name"])
        except ActionError as e:
            ok, error = False, str(e)
        except Exception as e:   # a tunnel failure mid-way
            ok, error = False, f"{type(e).__name__}: {str(e)[:160]}"
        if error:
            lines.append(f"Failed: {error}")
            detail["error"] = error
        detail["result"] = lines
        db.insert(db.audit_log, {"ts": time.time(), "user_id": u["id"], "user_email": u["email"], "org_id": p["org_id"],
                                 "site_id": (p.get("source") or p.get("site") or {}).get("id"), "action": f"fleet action: {summary(p)}",
                                 "method": "ACTION", "path": None, "status": 200 if ok else 500, "ip": None, "detail": detail})
        log.info("fleet action by %s: %s -> %s", u["email"], summary(p), "done" if ok else error)
        _plans.pop(p["id"], None)
        return {"ok": ok, "lines": lines, "summary": summary(p)}
