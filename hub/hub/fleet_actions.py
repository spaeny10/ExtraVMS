"""Fleet actions: an operator types "Migrate Ironsight to Hailo T1", "Move the front door camera from Ironsight to
Qwenbot", "Add 192.168.105.19 to Hailo T1 as Front Door", "Lock Side Yard footage 3-4 pm today" or "Quiet alerts
tonight" into the Ask box, and the hub carries it out across sites (which cannot talk to each other).

  VERBS                    the single registry of what the hub can be asked to do: the planner's prompt and JSON
                           schema, the confirmation card's options, the reference page (GET .../actions/reference)
                           and the README all come from it, so they cannot drift.
  plan_for(u, org, text)   no side effects: is this an instruction at all (questions never are), which action,
                           which sites and cameras (names resolved server-side against the org's real sites and
                           cameras), and a confirmation card: what moves, what stays, capacity after, warnings,
                           open questions.
  execute(plan, u, extras) only after Confirm (admin; migrate/retire also need the site's name typed): does it,
                           writes one audit_log row with a reverse plan that `undo(audit_id)` can run for 24 h.

Parsing: the shared AI (vlm_proxy.complete with a strict JSON schema, prompted with the org's site and camera
names) when configured, else a small rule parser for every verb. A plan with unresolved names, an unsure reading,
or a blocker (a site offline) carries `needs` / `blockers` and cannot be executed.

Moving cameras: the source hands the chosen cameras over WITH their passwords and what it learned about them
(`GET /api/config/handoff`, served only down its tunnel with `x-hub-internal: handoff`); the hub passes that
straight to the destination's `POST /api/config/merge` (adds the cameras, their zones/places/rules/labels, links
among them, named people/vehicles with their fingerprints, and seeds the baseline, parked spots and corrections;
the destination's own data stays). The hub then proves the destination can pull each stream (MediaMTX ready,
up to STREAM_CHECK_S) before the source lets go; if not, the merged cameras are removed again and the source was
never touched. Then the source disables them (never deletes: events reference the camera), the event history is
copied when asked (metadata, snapshots, crops, fingerprints; never clips), and the hub's own references follow the
camera: dashboard widgets, camera groups, open alerts, and the source's saved Find views. Passwords are never
stored, logged or written to the audit row. Migrating a site does that for all its enabled cameras, then retires
it: `sites.retired_at` hides it from Fleet, Home, Find, Ask and alerts; its tunnel stays connected.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import datetime as dt
import json
import logging
import re
import time
from urllib.parse import quote, urlencode

import sqlalchemy as sa

from . import auth, db, vlm_proxy
from .agents import AgentConn, registry

log = logging.getLogger("hub.fleet_actions")

PLAN_TTL_S = 600
UNDO_TTL_S = 24 * 3600
CAMERAS_CACHE_S = 30
CALL_TIMEOUT_S = 20
HANDOFF_TIMEOUT_S = 60
STREAM_CHECK_S = 60          # how long the destination gets to pull a moved camera's stream before the move is rolled back
STREAM_POLL_S = 2.0
BITRATE_WARN_MBPS = 60
CPU_CAMERA_WARN = 4
NEW_CAMERA_MBPS = 4.0        # assumed bitrate of a camera that is not streaming yet (add_camera)
MAX_LOCK_S = 7 * 86400
MAX_QUIET_S = 7 * 86400
FILE_BATCH_BYTES = 4 * 1024 * 1024
HANDOFF_HDR = {"x-hub-internal": "handoff"}
CAMERA_FIELDS = ("id", "name", "host", "onvif_port", "rtsp_port", "username", "main_path", "sub_path", "enabled", "zones",
                 "retention_days", "scene_notes", "retention_policy", "synopsis_labels", "policies")   # the site's CameraIn
HOST_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,252}$")

# ---------------------------------------------------------------- the registry

_COPY_HISTORY = {"key": "copy_history", "label": "Copy event history",
                 "title": "Copy the moved cameras' events (synopses, snapshots, crops, tags, fingerprints; not the clips) so the "
                          "destination's Find shows their past too"}
_SKIP_STREAM = {"key": "skip_stream_check", "label": "Skip the stream check", "default": False,
                "title": "Move even if the destination cannot pull the stream yet (e.g. the camera is offline at the source too)"}
_MOVE_MOVES = ["Each camera with its password, zones, named places and site rules",
               "Neighbor links between the moved cameras",
               "Named people and vehicles, with their re-ID / vehicle fingerprints (merged by name; names the destination knows are kept)",
               "What the source learned about each camera: the \"what's normal\" baseline, parked-spot memory and operator synopsis corrections",
               "Hub dashboard widgets, camera groups and open alerts that point at the camera, and the source's saved Find views for it",
               "Event history (synopses, snapshots, crops, tags, fingerprints; never clips) when \"Copy event history\" is ticked"]
_MOVE_STAYS = ["Recordings and event clips stay on the source; the destination records the cameras from now on",
               "The destination's own settings, retention, layouts, dashboards and other cameras",
               "The cameras on the source are disabled, not deleted, so their past events stay searchable there"]

VERBS: dict[str, dict] = {
    "move_cameras": {
        "title": "Move cameras to another server", "role": "admin", "confirm_name": False,
        "prompt": "move the named cameras from server source_site to server target_site.",
        "examples": ["Move the front door camera from Ironsight to Qwenbot", "Move gate and dock cameras to Qwenbot",
                     "Transfer Back Lot from Ironsight to Hailo T1 with history"],
        "moves": _MOVE_MOVES, "stays": _MOVE_STAYS,
        "options": [{**_COPY_HISTORY, "default": False}, _SKIP_STREAM],
        "undo": "Moves the cameras back to where they were (copied history stays on the destination)",
    },
    "migrate_site": {
        "title": "Migrate a whole server", "role": "admin", "confirm_name": True,
        "prompt": "move every camera of server source_site to server target_site, then retire source_site.",
        "examples": ["Migrate Ironsight to Hailo T1", "Migrate the server Qwenbot onto Hailo T1", "Migrate Ironsight to Hailo T1 without history"],
        "moves": ["Every enabled camera, as for Move cameras", *_MOVE_MOVES[1:]],
        "stays": [*_MOVE_STAYS, "Disabled cameras are not moved",
                  "The source is then retired: hidden from Fleet, Home, Find, Ask and alerts; its tunnel stays connected and its page still opens"],
        "options": [{**_COPY_HISTORY, "default": True}, _SKIP_STREAM],
        "undo": "Restores the source server and moves its cameras back",
    },
    "retire_site": {
        "title": "Retire a server", "role": "admin", "confirm_name": True,
        "prompt": "retire server source_site (hide it; no cameras move).",
        "examples": ["Retire Ironsight", "Decommission the server Old Barn"],
        "moves": ["The server is hidden from Fleet, Home, Find, Ask, the digest and alerts (Fleet → Show retired lists it)"],
        "stays": ["Its recordings, events and settings stay on the server", "Its tunnel stays connected and /s/<server>/ still opens",
                  "Its cameras keep recording on the server"],
        "options": [], "undo": "Restores the server",
    },
    "set_retention": {
        "title": "Set continuous recording retention", "role": "admin", "confirm_name": False,
        "prompt": "keep `days` days of continuous recording at server source_site.",
        "examples": ["Set Qwenbot to 7 days of recording", "Keep 10 days at Hailo T1", "Set the retention on Ironsight to 21 days"],
        "moves": ["The server's continuous_days (1-365)"],
        "stays": ["Event clips, locked footage and per-camera overrides follow their own rules", "The free-space floor still wins"],
        "options": [], "undo": "Sets the previous number of days again",
    },
    "rename_camera": {
        "title": "Rename a camera", "role": "admin", "confirm_name": False,
        "prompt": "rename cameras[0] at source_site to new_name.",
        "examples": ["Rename cam3 on Hailo T1 to Loading Dock", "Rename the gate camera to North Gate"],
        "moves": ["The camera's display name"], "stays": ["Its id, recordings, events, zones and rules"],
        "options": [], "undo": "Gives it its old name back",
    },
    "add_camera": {
        "title": "Add a camera to a server", "role": "admin", "confirm_name": False,
        "prompt": "add a new camera at address `host` to server target_site, named new_name.",
        "examples": ["Add 192.168.105.19 to Hailo T1 as Front Door", "Add camera 10.2.0.9 to Qwenbot called Loading Dock"],
        "moves": ["A new camera on the server (RTSP main/sub paths, ONVIF port); the server starts recording it"],
        "stays": ["The password you type goes to the server in the Confirm call only: the hub never stores, logs or audits it",
                  "Other cameras and settings"],
        "options": [], "undo": "Removes the camera again (disabled instead if it already has events)",
        "inputs": [{"key": "password", "label": "Camera password", "type": "password"},
                   {"key": "username", "label": "User", "type": "text", "placeholder": "admin", "optional": True},
                   {"key": "main_path", "label": "Main stream path", "type": "text", "placeholder": "/main", "optional": True},
                   {"key": "sub_path", "label": "Sub stream path", "type": "text", "placeholder": "/sub", "optional": True},
                   {"key": "onvif_port", "label": "ONVIF port", "type": "number", "placeholder": "80", "optional": True},
                   {"key": "rtsp_port", "label": "RTSP port", "type": "number", "placeholder": "554", "optional": True}],
    },
    "set_synopsis_labels": {
        "title": "Choose what Qwen describes on a camera", "role": "admin", "confirm_name": False,
        "prompt": "change which object kinds Qwen describes on cameras[0] at source_site: labels (person / vehicle) with "
                  "label_mode remove (stop describing), add (also describe) or only.",
        "examples": ["Stop describing vehicles on cam2", "Describe only people on Gate at Hailo T1", "Start describing vehicles on Back Lot"],
        "moves": ["The camera's synopsis labels (YOLO still verifies every label; only Qwen's descriptions change)"],
        "stays": ["Past synopses", "Other cameras"],
        "options": [], "undo": "Restores the previous labels (or the site default)",
    },
    "lock_footage": {
        "title": "Lock footage", "role": "operator", "confirm_name": False,
        "prompt": "lock (keep regardless of retention) cameras[0]'s footage at source_site between time_from and time_to "
                  "(24 h HH:MM) on day (today / yesterday / YYYY-MM-DD).",
        "examples": ["Lock Side Yard footage 3-4 pm today", "Lock Gate footage from 9am to 10:30am yesterday"],
        "moves": ["A lock on that camera and time span (at most 7 days): retention keeps it until the lock is removed"],
        "stays": ["Footage outside the span follows the retention policy"],
        "options": [], "undo": "Removes the lock",
    },
    "quiet_alerts": {
        "title": "Quiet alerts", "role": "admin", "confirm_name": False,
        "prompt": "stop opening event alerts (and their push notifications) at source_site, or every site when empty, "
                  "until `until` (tonight / for N hours / until HH:MM).",
        "examples": ["Quiet alerts tonight", "Mute alerts at Hailo T1 for 2 hours", "Silence alerts until 7am"],
        "moves": ["Event alerts (high priority, broken site rules, watched people) are not opened until then (at most 7 days)"],
        "stays": ["Health alerts: site offline, camera down, disk, clock", "Events are still recorded and described on the sites"],
        "options": [], "undo": "Turns alerts back on (or restores the previous quiet period)",
    },
    # internal: only the reverse plans of Undo use these; never parsed from text
    "restore_site": {"title": "Restore a retired server", "role": "admin", "internal": True},
    "remove_camera": {"title": "Remove a camera that was just added", "role": "admin", "internal": True},
    "unlock_footage": {"title": "Remove a lock", "role": "operator", "internal": True},
    "unquiet_alerts": {"title": "Turn alerts back on", "role": "admin", "internal": True},
}
ACTIONS = tuple(k for k, v in VERBS.items() if not v.get("internal"))
ALL_ACTIONS = tuple(VERBS)
MOVES = ("move_cameras", "migrate_site")

SAFETY = [
    "Nothing happens until Confirm on the card. Plans expire after 10 minutes and run once.",
    "Only an admin of the organization can confirm (lock_footage: operator). Anyone in the org may see a card.",
    "Migrate and Retire also need the source server's name typed into the card.",
    "Questions (\"how many people today?\") are never actions: they go to the sites' assistants as before.",
    "Names are matched against the org's real Sites, servers and cameras; a Site with several servers is asked about "
    "for server-level actions; anything unclear becomes a question on the card.",
    "Camera passwords go server to server through the hub in one call; they are never stored, logged or audited. "
    "A new camera's password is typed into the card and sent only with Confirm.",
    "A move first proves the destination can pull each stream (up to 60 s); if not, it is rolled back and the source is untouched.",
    "Every executed action writes one Audit row with its outcome. Undo is offered for 24 hours on the result and on the Audit row.",
]
CAPACITY = [
    "The card shows the destination after the action: total Mbps, about how many days of continuous footage fit "
    "(free disk plus today's continuous footage, minus the free-space floor, at that rate) against its retention policy, "
    "and detection load (device, current YOLO ms per frame, number of cameras).",
    f"Warnings: more than {BITRATE_WARN_MBPS} Mbps, detection on the CPU with more than {CPU_CAMERA_WARN} cameras, "
    "fewer days fit than the retention policy asks for, a camera already at that address, a camera offline at the source.",
]

LABELS = ("person", "vehicle")
SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": [*ACTIONS, "none"]},
        "source_site": {"type": "string"},
        "target_site": {"type": "string"},
        "cameras": {"type": "array", "items": {"type": "string"}},
        "days": {"type": "integer"},
        "new_name": {"type": "string"},
        "host": {"type": "string"},
        "labels": {"type": "array", "items": {"type": "string", "enum": list(LABELS)}},
        "label_mode": {"type": "string", "enum": ["", "remove", "add", "only"]},
        "time_from": {"type": "string"},
        "time_to": {"type": "string"},
        "day": {"type": "string"},
        "until": {"type": "string"},
        "copy_history": {"type": "string", "enum": ["", "yes", "no"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["action", "source_site", "target_site", "cameras", "days", "new_name", "host", "labels", "label_mode",
                 "time_from", "time_to", "day", "until", "copy_history", "confidence"],
    "additionalProperties": False,
}
EMPTY = {"action": "none", "source_site": "", "target_site": "", "cameras": [], "days": 0, "new_name": "", "host": "", "labels": [],
         "label_mode": "", "time_from": "", "time_to": "", "day": "", "until": "", "copy_history": "", "confidence": "high"}

SYSTEM = ("You turn one operator instruction for a fleet of video-surveillance servers into a single action, as JSON.\nActions:\n"
          + "".join(f"- {k}: {v['prompt']}\n" for k, v in VERBS.items() if not v.get("internal"))
          + """- none: anything else. Questions, searches and requests about footage, events, people or counts are ALWAYS none,
  even when they mention cameras or sites. Never guess an action from a question.
A Site is a physical place with one or more servers; source_site / target_site name a server (or the Site the
operator said, when it is unclear which server). Use Site, server and camera names exactly as listed below when the
operator means one of them; otherwise copy the operator's words. Leave fields that don't apply as "" / [] / 0.
copy_history: "yes" / "no" only when the operator says so. confidence is high only when the instruction is explicit.
Sites, their servers and cameras ([id]):
""")

_plans: dict[str, dict] = {}
_cameras_cache: dict[str, tuple[float, list[dict]]] = {}
_exec_lock = asyncio.Lock()


class ActionError(Exception):
    pass


def reference(u: dict, org_id: str, is_admin: bool) -> dict:
    """The reference page's content (hub UI → Organization → Fleet actions): every verb from VERBS, the safety
    rules, the capacity notes and (for admins) the last 50 fleet actions with whether Undo is still possible."""
    verbs = [{"action": k, **{f: v.get(f) for f in ("title", "role", "confirm_name", "examples", "moves", "stays", "undo")},
              "options": [o["label"] for o in v.get("options") or []], "inputs": [i["label"] for i in v.get("inputs") or []]}
             for k, v in VERBS.items() if not v.get("internal")]
    recent = []
    if is_admin:
        rows = db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id == org_id, db.audit_log.c.method == "ACTION")
                       .order_by(db.audit_log.c.ts.desc()).limit(50))
        recent = [{"id": r["id"], "ts": r["ts"], "user_email": r["user_email"], "action": r["action"], "status": r["status"],
                   "lines": (r["detail"] or {}).get("result") or [], "undo_until": undo_until(r)} for r in rows]
    return {"verbs": verbs, "safety": SAFETY, "capacity": CAPACITY, "recent": recent, "undo_hours": UNDO_TTL_S // 3600}


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


async def _get(conn: AgentConn | None, u: dict, path: str, timeout: float = 10) -> object:
    """A GET that answers None instead of raising (cards must render with a site half-answering)."""
    if conn is None:
        return None
    try:
        st, data = await _call(conn, u, "GET", path, timeout=timeout)
    except Exception:
        return None
    return data if st == 200 else None


def _detail(data: object, status: int) -> str:
    d = data.get("detail") if isinstance(data, dict) else None
    return f"HTTP {status}{f': {str(d)[:160]}' if d else ''}"


async def site_cameras(site: dict, u: dict, fresh: bool = False) -> list[dict] | None:
    """The site's cameras (public fields, no passwords) plus live bitrate and stream state; None when offline."""
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
        st = (c.get("status") or {}) if isinstance(c, dict) else {}
        health = st.get("health") or {}
        cams.append({**{k: c.get(k) for k in CAMERA_FIELDS}, "bitrate_mbps": health.get("bitrate_mbps"),
                     "stream_ready": st.get("stream_ready")})
    _cameras_cache[site["id"]] = (time.time(), cams)
    return cams


def _forget(*site_ids: str) -> None:
    for s in site_ids:
        _cameras_cache.pop(s, None)


async def _index(u: dict, org_id: str) -> list[dict]:
    """Every visible, non-retired server with its cameras (live list, or the last heartbeat's names when offline) and
    its Site ({id, name}; None for a server the backfill has not given one yet). _locations() groups it by Site."""
    sites = auth.visible_sites(u, org_id)
    locs = {loc["id"]: loc for loc in auth.visible_locations(u, org_id)}
    lists = await asyncio.gather(*(site_cameras(s, u) for s in sites))
    out = []
    for s, cams in zip(sites, lists):
        live = cams is not None
        if cams is None:
            cams = [{"id": c.get("id"), "name": c.get("name"), "enabled": 1, "bitrate_mbps": c.get("bitrate_mbps")}
                    for c in ((s.get("summary") or {}).get("cameras") or []) if c.get("id")]
        loc = locs.get(s.get("location_id"))
        out.append({"site": s, "online": registry.get(s["id"]) is not None, "live": live, "cameras": cams,
                    "location": {"id": loc["id"], "name": loc["name"]} if loc else None})
    return out


def _locations(index: list[dict]) -> list[dict]:
    """The index by Site: [{id, name, servers: [index entries]}] in index order. Only visible, non-retired servers
    count, so a Site whose second server was retired resolves like a one-server Site. A server without a Site
    (id None) stands alone under its own name, as the fleet looked before Sites."""
    out: dict[str, dict] = {}
    for e in index:
        loc = e.get("location")
        key = loc["id"] if loc else "server:" + e["site"]["id"]
        g = out.setdefault(key, {"id": loc["id"] if loc else None, "name": loc["name"] if loc else e["site"]["name"], "servers": []})
        g["servers"].append(e)
    return list(out.values())


# ---------------------------------------------------------------- times: "3-4 pm today", "tonight", "for 2 hours"

def _hm(h: int, m: int, ap: str | None) -> tuple[int, int] | None:
    if ap:
        if not 1 <= h <= 12:
            return None
        h = (h % 12) + (12 if ap.startswith("p") else 0)
    return (h, m) if 0 <= h <= 23 and 0 <= m <= 59 else None


TIME_RANGE = re.compile(r"(?:from\s+|between\s+)?(?P<a>\d{1,2})(?::(?P<am>\d{2}))?\s*(?P<ap>[ap]\.?m\.?)?\s*(?:-|–|to|and|until|till)\s*"
                        r"(?P<b>\d{1,2})(?::(?P<bm>\d{2}))?\s*(?P<bp>[ap]\.?m\.?)?", re.I)
ONE_TIME = re.compile(r"^(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]\.?m\.?)?$", re.I)


def parse_range_text(when: str) -> dict | None:
    """'3-4 pm today' / 'from 9am to 10:30am yesterday' -> {time_from, time_to, day} (24 h strings), or None."""
    w = when.strip().lower()
    day = "today"
    m = re.search(r"\b(today|yesterday|\d{4}-\d{2}-\d{2})\b", w)
    if m:
        day, w = m.group(1), (w[:m.start()] + w[m.end():]).strip()
    r = TIME_RANGE.search(w)
    if not r:
        return None
    ap, bp = (r.group("ap") or "").replace(".", ""), (r.group("bp") or "").replace(".", "")
    ha, hb = int(r.group("a")), int(r.group("b"))
    ma, mb = int(r.group("am") or 0), int(r.group("bm") or 0)
    inferred = False
    if bp and not ap:
        ap, inferred = bp, True
    if ap and not bp:
        bp = ap
    a, b = _hm(ha, ma, ap or None), _hm(hb, mb, bp or None)
    if a and b and inferred and a >= b and ap == "pm":
        a = _hm(ha, ma, "am")      # "11-1 pm" is 11 am to 1 pm
    if not a or not b:
        return None
    return {"time_from": f"{a[0]:02d}:{a[1]:02d}", "time_to": f"{b[0]:02d}:{b[1]:02d}", "day": day}


def _local_midnight(now: float, off: float, day: str) -> float | None:
    """Unix time of the site's local midnight starting `day` (today / yesterday / YYYY-MM-DD), given its UTC offset."""
    if day in ("", "today"):
        loc = now + off
        return loc - (loc % 86400) - off
    if day == "yesterday":
        loc = now + off
        return loc - (loc % 86400) - 86400 - off
    try:
        d = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    return d.timestamp() - off


def _clock(s: str) -> tuple[int, int] | None:
    m = ONE_TIME.match(s.strip())
    if not m:
        return None
    return _hm(int(m.group("h")), int(m.group("m") or 0), (m.group("ap") or "").replace(".", "") or None)


def range_ts(time_from: str, time_to: str, day: str, now: float, off: float) -> tuple[float, float] | None:
    a, b, mid = _clock(time_from or ""), _clock(time_to or ""), _local_midnight(now, off, (day or "today").strip().lower())
    if not a or not b or mid is None:
        return None
    start, end = mid + a[0] * 3600 + a[1] * 60, mid + b[0] * 3600 + b[1] * 60
    if end <= start:
        end += 86400    # "10 pm - 2 am"
    return start, end


def until_ts(until: str, now: float, off: float) -> float | None:
    """'tonight' / 'overnight' / 'until morning' -> next 07:00 site time; 'for 2 hours'; 'until 7am' / 'until 18:30'."""
    u = until.strip().lower()
    u = re.sub(r"^(?:until|till|til)\s+", "", u)

    def next_at(h: int, m: int) -> float:
        t = _local_midnight(now, off, "today") + h * 3600 + m * 60
        return t if t > now else t + 86400

    if u in ("tonight", "overnight", "morning", "the morning", "tomorrow", "tomorrow morning", "the night"):
        return next_at(7, 0)
    m = re.fullmatch(r"for\s+(?:an?\s+|(\d+(?:\.\d+)?)\s*)(h|hr|hrs|hours?|m|min|mins|minutes?)", u)
    if m:
        n = float(m.group(1) or 1)
        return now + n * (60 if m.group(2).startswith("m") else 3600)
    c = _clock(u)
    return next_at(*c) if c else None


def _hub_off() -> float:
    return float(time.localtime().tm_gmtoff or 0)


def _fmt_local(ts: float, off: float) -> str:
    return time.strftime("%a %d %b %H:%M", time.gmtime(ts + off))


# ---------------------------------------------------------------- is it an instruction, and which one

POLITE = re.compile(r"^\s*(please|pls|kindly|ok|okay|now|go ahead and|can you|could you|would you|will you|i want to|i'd like to|"
                    r"i would like to|we need to|let's|lets)\b[\s,]*", re.I)
QUESTION = re.compile(r"^\s*(how|what|what's|whats|when|where|who|whom|whose|why|which|did|does|do(?!\s+not\b)|is|are|was|were|has|have|had|"
                      r"show|list|find|search|any|anyone|anybody|count|tell|give|should|shall|may|might)\b", re.I)
VERB_WORDS = re.compile(r"\b(migrate|move|transfer|relocate|retire|decommission|rename|retention|retain|keep|set|add|lock|protect|"
                        r"quiet|mute|silence|snooze|hush|describe|describing)\b", re.I)


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
    return t.rstrip("?.! "), bool(VERB_WORDS.search(t))


CAM_SITE = r"(?P<cam>.+?)(?:\s+(?:at|on|in)\s+(?P<site>.+))?"
RULES: list[tuple[str, re.Pattern]] = [(a, re.compile(rx, re.I)) for a, rx in [
    ("rename_camera", r"^rename\s+(?:the\s+)?(?P<cam>.+?)\s+(?:camera\s+)?(?:on|at|in)\s+(?P<site>.+?)\s+(?:to|as)\s+(?P<name>.+)$"),
    ("rename_camera", r"^rename\s+(?:the\s+)?(?P<cam>.+?)\s+(?:to|as)\s+(?P<name>.+)$"),
    ("migrate_site", r"^migrate\s+(?:the\s+)?(?:site\s+|server\s+)?(?P<src>.+?)\s+(?:to|into|onto|over\s+to)\s+(?P<dst>.+)$"),
    ("move_cameras", r"^(?:move|transfer|relocate)\s+(?:the\s+)?(?P<cams>.+?)\s+from\s+(?P<src>.+?)\s+(?:to|into|onto|over\s+to)\s+(?P<dst>.+)$"),
    ("move_cameras", r"^(?:move|transfer|relocate)\s+(?:the\s+)?(?P<cams>.+?)\s+(?:to|into|onto|over\s+to)\s+(?P<dst>.+)$"),
    ("retire_site", r"^(?:retire|decommission)\s+(?:the\s+)?(?:site\s+|server\s+)?(?P<src>.+)$"),
    ("set_retention", r"^set\s+(?:the\s+)?(?:retention\s+(?:on|at|for|of)\s+)?(?P<site>.+?)\s+(?:retention\s+)?to\s+(?P<days>\d+)\s*(?:days?|d)\b"),
    ("set_retention", r"^(?:keep|retain)\s+(?P<days>\d+)\s*(?:days?|d)\b.*?\b(?:at|on|for)\s+(?P<site>.+)$"),
    ("add_camera", r"^add\s+(?:a\s+|the\s+)?(?:new\s+)?(?:camera\s+)?(?:at\s+)?(?P<host>\d{1,3}(?:\.\d{1,3}){3}|[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)+)"
                   r"\s+(?:to|on|at)\s+(?P<site>.+?)(?:\s+(?:as|called|named)\s+(?P<name>.+))?$"),
    ("set_synopsis_labels", r"^(?P<neg>stop|don't|dont|do\s+not|no\s+longer)\s+(?:describing|describe)\s+(?P<what>[a-z]+)\s+(?:on|at|for|in|from)\s+"
                            + CAM_SITE + "$"),
    ("set_synopsis_labels", r"^(?:start\s+describing|also\s+describe|describe)\s+(?P<only>only\s+)?(?P<what>[a-z]+)(?P<only2>\s+only)?\s+(?:on|at|for|in)\s+"
                            + CAM_SITE + "$"),
    ("lock_footage", r"^(?:lock|protect|preserve)\s+(?:the\s+)?(?:footage|recordings?|video)\s+(?:on|of|from|at)\s+(?P<cam>.+?)\s+(?P<when>(?:from\s+|between\s+)?\d.*)$"),
    ("lock_footage", r"^(?:lock|protect|preserve)\s+(?:the\s+)?(?P<cam>.+?)\s+(?:camera\s+)?(?:footage|recordings?|video)\s+(?P<when>.+)$"),
    ("quiet_alerts", r"^(?:quiet|mute|silence|snooze|pause|hush)\s+(?:all\s+|the\s+)?(?:hub\s+)?(?:event\s+)?alerts?(?P<rest>.*)$"),
]]
WHAT = {"people": ["person"], "person": ["person"], "persons": ["person"], "humans": ["person"],
        "vehicles": ["vehicle"], "vehicle": ["vehicle"], "cars": ["vehicle"], "car": ["vehicle"], "trucks": ["vehicle"],
        "everything": list(LABELS), "both": list(LABELS), "all": list(LABELS), "anything": list(LABELS)}
HISTORY_FLAG = re.compile(r"\s+(?P<w>with|without|no)\s+(?:the\s+|its\s+|their\s+)?(?:event\s+)?history$", re.I)


def _split_cameras(s: str) -> list[str]:
    parts = re.split(r"\s*(?:,|&|\band\b)\s*", s)
    return [p for p in (re.sub(r"^(?:the)\s+|\s+cam(?:era)?s?$", "", x.strip(), flags=re.I).strip() for x in parts) if p]


def _quiet_rest(rest: str) -> tuple[str, str]:
    """' at Hailo T1 for 2 hours' -> ('Hailo T1', 'for 2 hours')."""
    rest = rest.strip()
    site = ""
    m = re.match(r"^(?:at|on|for)\s+(?!\d|an?\s+hour)(?P<site>.+?)(?=\s+(?:tonight|overnight|for\s+(?:\d|an?\b)|until|till|til)\b|$)", rest, re.I)
    if m:
        site, rest = m.group("site"), rest[m.end():].strip()
    return site.strip(), rest.strip()


def parse_rules(text: str) -> dict | None:
    copy_history = ""
    h = HISTORY_FLAG.search(text)
    if h:
        copy_history, text = ("yes" if h.group("w").lower() == "with" else "no"), text[:h.start()]
    for action, rx in RULES:
        m = rx.match(text)
        if not m:
            continue
        g = m.groupdict()
        out = {**EMPTY, "action": action, "copy_history": copy_history}
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
        elif action == "add_camera":
            out.update(target_site=g["site"], host=g["host"], new_name=(g.get("name") or "").strip().strip("\"'"))
        elif action == "set_synopsis_labels":
            mode = "remove" if g.get("neg") else "only" if (g.get("only") or g.get("only2")) else "add"
            out.update(cameras=_split_cameras(g["cam"])[:1], source_site=g.get("site") or "", labels=WHAT.get(g["what"].lower(), []), label_mode=mode)
        elif action == "lock_footage":
            when, site = g["when"], ""
            w = re.match(r"^(?:(?:at|on|in)\s+(?P<site>.+?)\s+)(?P<range>(?:from\s+|between\s+)?\d.*|today.*|yesterday.*)$", when, re.I)
            if w:
                site, when = w.group("site"), w.group("range")
            cam_words = g["cam"]
            cs = re.match(r"^(?P<cam>.+?)\s+(?:at|on|in)\s+(?P<site>.+)$", cam_words, re.I)
            if cs and not site:
                cam_words, site = cs.group("cam"), cs.group("site")
            out.update(cameras=_split_cameras(cam_words)[:1], source_site=site, **(parse_range_text(when) or {}))
        elif action == "quiet_alerts":
            site, until = _quiet_rest(g.get("rest") or "")
            out.update(source_site=site, until=until)
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
    for g in _locations(index):
        servers = []
        for e in g["servers"]:
            cams = ", ".join(f"{c['name']} [{c['id']}]" for c in e["cameras"]) or "no cameras"
            servers.append(f"server {e['site']['name']} ({'online' if e['online'] else 'offline'}): {cams}")
        lines.append(f"- Site {g['name']}: {'; '.join(servers)}")
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
            "new_name": str(d.get("new_name") or "").strip(), "host": str(d.get("host") or "").strip(),
            "labels": [x for x in (d.get("labels") or []) if x in LABELS], "label_mode": str(d.get("label_mode") or ""),
            "time_from": str(d.get("time_from") or ""), "time_to": str(d.get("time_to") or ""), "day": str(d.get("day") or ""),
            "until": str(d.get("until") or ""), "copy_history": str(d.get("copy_history") or ""), "confidence": d.get("confidence") or "low"}


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


def _resolve(parsed: dict, index: list[dict], org_id: str) -> dict:
    """Turn the parsed names into sites and cameras of this org; questions for whatever doesn't resolve."""
    a = parsed["action"]
    needs: list[str] = []
    out: dict = {"action": a, "source": None, "target": None, "site": None, "location": None, "cameras": [],
                 "days": parsed.get("days") or None, "new_name": parsed.get("new_name") or None}
    cam_keys = lambda c: [c["name"], c["id"]]                   # noqa: E731
    # Names the operator may use for a server: its name, its id, and its Site's name when that Site has only this
    # server (backfilled one-server Sites share the server's name, which must still give one hit). A Site with
    # several servers is a separate candidate ({"group": ...}) so the caller can ask which server, or find the
    # camera's owner among them.
    groups = [g for g in _locations(index) if g["id"]]
    only = {g["servers"][0]["site"]["id"]: g["name"] for g in groups if len(g["servers"]) == 1}
    places = [*index, *({"group": g} for g in groups if len(g["servers"]) > 1)]

    def place_keys(x: dict) -> list:
        if "group" in x:
            return [x["group"]["name"], x["group"]["id"]]
        return [x["site"]["name"], x["site"]["id"], only.get(x["site"]["id"])]

    def place(words: str) -> dict | None:
        """An index entry, {"group": Site} for a several-server Site, or None (question appended)."""
        exact = [e for e in index if _norm(words) in (_norm(e["site"]["name"]), _norm(e["site"]["id"]))]
        if len(exact) == 1:   # a server's own name wins over a Site named like it
            return exact[0]
        x, q = _pick(words, places, place_keys, "site", "site|server")
        if q:
            needs.append(q)
        return x

    def which_server(g: dict) -> None:
        names = ", ".join(e["site"]["name"] for e in g["servers"])
        needs.append(f"{g['name']} has {len(g['servers'])} servers ({names}): which one?")

    def site(words: str, role: str):
        """A server for a server-level verb: a several-server Site becomes a question."""
        if not words.strip():
            needs.append(f"Which site should be the {role}?")
            return None
        x = place(words)
        if x is not None and "group" in x:
            which_server(x["group"])
            return None
        return x

    def owner_in(g: dict, words: list[str]) -> dict | None:
        """The one server of Site g that has (any of) these cameras; otherwise a question."""
        owners = [e for e in g["servers"] if any(_pick(w, e["cameras"], cam_keys, "camera", "cameras?|cams?")[0] for w in words)]
        if len(owners) == 1:
            return owners[0]
        names = ", ".join(words)
        needs.append(f'Which server at {g["name"]} is "{names}" on: {", ".join(e["site"]["name"] for e in owners)}?' if owners
                     else f'No server at {g["name"]} has a camera called "{names}".')
        return None

    def cameras_on(e: dict, words: list[str]) -> list[dict]:
        found = []
        for w in words:
            c, q = _pick(w, e["cameras"], cam_keys, f"camera on {e['site']['name']}", "cameras?|cams?")
            if q:
                needs.append(q)
            elif c not in found:
                found.append(c)
        return found

    def one_camera(what: str) -> None:
        """rename / labels / lock / remove: one camera, its server named (or its Site: the owner among the Site's
        servers) or found from the camera's name."""
        named = parsed["source_site"] or parsed["target_site"]
        words = parsed.get("cameras") or []
        e = place(named) if named else None
        if e is not None and "group" in e:
            e = owner_in(e["group"], words[:1]) if words else None
        if not words:
            needs.append(f"Which camera should {what}?")
        elif e is None and not (parsed["source_site"] or parsed["target_site"]):
            owners = [(x, c) for x in index for c in [_pick(words[0], x["cameras"], cam_keys, "camera", "cameras?|cams?")[0]] if c]
            if len(owners) == 1:
                e = owners[0][0]
            else:
                needs.append(f'Which site is "{words[0]}" on?' if owners else f'No site has a camera called "{words[0]}".')
        if e is not None and words:
            out["cameras"] = cameras_on(e, words[:1])
        out["site"] = _site_ref(e) if e else None

    if a in MOVES:
        src_words = parsed["source_site"]
        if a == "move_cameras" and src_words.strip() and parsed.get("cameras"):
            # "Move Lobby from Austin HQ to …": the cameras pick the server inside a several-server Site
            src = place(src_words)
            if src is not None and "group" in src:
                src = owner_in(src["group"], parsed["cameras"])
        else:
            src = site(src_words, "source") if (src_words or a == "migrate_site") else None
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
    elif a in ("retire_site", "set_retention"):
        e = site(parsed["source_site"] or parsed["target_site"], "site")
        if a == "set_retention" and not out["days"]:
            needs.append("How many days of recording should it keep?")
        out["site"] = _site_ref(e) if e else None
    elif a == "restore_site":
        words = parsed["source_site"] or parsed["target_site"]
        rows = db.rows(sa.select(db.sites).where(db.sites.c.org_id == org_id))
        row = next((r for r in rows if r["id"] == words), None) or next((r for r in rows if _norm(r["name"]) == _norm(words)), None)
        if row is None:   # a Site's name: fine when it has one server, a question when it has several
            loc = next((r for r in db.rows(sa.select(db.locations).where(db.locations.c.org_id == org_id))
                        if _norm(r["name"]) == _norm(words)), None)
            members = [r for r in rows if loc and r.get("location_id") == loc["id"]]
            if len(members) == 1:
                row = members[0]
            elif members:
                needs.append(f"{loc['name']} has {len(members)} servers ({', '.join(r['name'] for r in members)}): which one?")
        if row is None:
            if not needs:
                needs.append(f'There is no site called "{words}".')
        else:
            out["site"] = {"id": row["id"], "name": row["name"], "online": registry.get(row["id"]) is not None}
    elif a == "rename_camera":
        one_camera("be renamed")
        if not out["new_name"]:
            needs.append("What should the new name be?")
    elif a == "set_synopsis_labels":
        one_camera("change")
        out["labels"] = [x for x in parsed.get("labels") or [] if x in LABELS]
        out["label_mode"] = parsed.get("label_mode") or "add"
        out["labels_inherit"] = bool(parsed.get("labels_inherit"))
        if not out["labels"] and out["label_mode"] != "exact":
            needs.append("Describe what: people or vehicles?")
    elif a == "lock_footage":
        one_camera("have its footage locked")
        spec = {k: parsed.get(k) or "" for k in ("time_from", "time_to", "day")}
        if not (spec["time_from"] and spec["time_to"]) or range_ts(spec["time_from"], spec["time_to"], spec["day"], time.time(), 0) is None:
            needs.append('Which time span? e.g. "3-4 pm today" or "from 9:00 to 10:30 yesterday".')
        out["span"] = spec
    elif a == "remove_camera":
        one_camera("be removed")
    elif a == "unlock_footage":
        e = site(parsed["source_site"], "site")
        out["site"] = _site_ref(e) if e else None
        out["lock_id"] = int(parsed.get("lock_id") or 0)
    elif a == "add_camera":
        e = site(parsed["target_site"] or parsed["source_site"], "site to add it to")
        host = (parsed.get("host") or "").strip()
        if not host:
            needs.append("What is the camera's address (IP or hostname)?")
        elif not HOST_RX.match(host):
            needs.append(f'"{host[:60]}" is not an IP address or hostname.')
        out.update(site=_site_ref(e) if e else None, host=host or None, new_name=out["new_name"] or host or None)
    elif a in ("quiet_alerts", "unquiet_alerts"):
        words = parsed["source_site"] or parsed["target_site"]
        e = place(words) if words else None
        if e is not None and "group" in e:   # a several-server Site: quiet every one of its servers
            g = e["group"]
            out["location"] = {"id": g["id"], "name": g["name"], "servers": [_site_ref(x) for x in g["servers"]]}
            e = None
        out["site"] = _site_ref(e) if e else None
        out["until"] = parsed.get("until") or ""
        out["previous"] = parsed.get("previous")
        if a == "quiet_alerts" and (not out["until"] or until_ts(out["until"], time.time(), 0) is None):
            needs.append('Until when? e.g. "quiet alerts tonight", "for 2 hours" or "until 7am".')
    ch = parsed.get("copy_history") or ""
    out["options"] = {"copy_history": ch == "yes" if ch else next((o["default"] for o in VERBS[a].get("options") or [] if o["key"] == "copy_history"), False),
                      "skip_stream_check": bool(parsed.get("skip_stream_check"))}
    out["needs"] = needs
    return out


def _quiet_where(p: dict) -> str:
    loc = p.get("location")
    if loc:
        n = len(loc.get("servers") or [])
        return f"at {loc['name']} ({n} server{'' if n == 1 else 's'})"
    return f"at {p['site']['name']}" if p.get("site") else "at every site"


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
    if a == "restore_site":
        return f"Restore {name(p['site'])}"
    if a == "set_retention":
        return f"Keep {p.get('days') or '?'} days of recording at {name(p['site'])}"
    if a == "rename_camera":
        return f'Rename "{cams}" on {name(p["site"])} to "{p.get("new_name") or "?"}"'
    if a == "add_camera":
        return f'Add {p.get("host") or "?"} to {name(p["site"])} as "{p.get("new_name") or "?"}"'
    if a == "remove_camera":
        return f"Remove {cams} from {name(p['site'])}"
    if a == "set_synopsis_labels":
        mode, labels = p.get("label_mode"), " and ".join("people" if x == "person" else "vehicles" for x in p.get("labels") or []) or "nothing"
        verb = {"remove": "Stop describing", "only": "Describe only", "exact": "Describe"}.get(mode, "Also describe")
        return f"{verb} {labels if not p.get('labels_inherit') else 'the site default'} on {cams} at {name(p['site'])}"
    if a == "lock_footage":
        sp = p.get("span") or {}
        return f"Lock {cams} footage at {name(p['site'])}, {sp.get('time_from') or '?'}-{sp.get('time_to') or '?'} {sp.get('day') or 'today'}"
    if a == "unlock_footage":
        return f"Remove lock {p.get('lock_id')} at {name(p['site'])}"
    if a == "quiet_alerts":
        return f"Quiet alerts {_quiet_where(p)} {p.get('until') or ''}".strip()
    if a == "unquiet_alerts":
        return "Turn alerts back on"
    return "Nothing to do"


# ---------------------------------------------------------------- preview: the confirmation card

def _zone_counts(c: dict) -> tuple[int, int, int]:
    zones = c.get("zones") or []
    places = sum(1 for z in zones if isinstance(z, dict) and z.get("type") == "area")
    return len(zones) - places, places, len(c.get("policies") or [])


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _row(site_id: str) -> dict:
    return db.one(sa.select(db.sites).where(db.sites.c.id == site_id)) or {}


async def capacity(ref: dict, u: dict, cams: list[dict], add_mbps: float, add_cams: int) -> tuple[dict | None, list[str], list[str]]:
    """The destination after the action: total Mbps, about how many days of continuous footage fit (free disk plus
    the continuous footage already there, minus the free-space floor) against its retention policy, and detection
    load. (data, card lines, warnings)."""
    conn = registry.get(ref["id"])
    if conn is None:
        return None, [], []
    sysinfo, stats, pol = await asyncio.gather(_get(conn, u, "/api/system"), _get(conn, u, "/api/retention/stats"), _get(conn, u, "/api/retention/policy"))
    sysinfo = sysinfo if isinstance(sysinfo, dict) else {}
    stats = stats if isinstance(stats, dict) else {}
    policy = (pol or {}).get("policy") if isinstance(pol, dict) else None
    policy = policy if isinstance(policy, dict) else {}
    enabled = [c for c in cams if c.get("enabled", 1)]
    summ = _row(ref["id"]).get("summary") or {}
    rate_now = sum(c.get("bitrate_mbps") or 0 for c in enabled) or (summ.get("bitrate_mbps") or 0)
    mbps = rate_now + add_mbps
    n = len(enabled) + add_cams
    disk = stats.get("disk") or sysinfo.get("recordings_disk") or summ.get("disk") or {}
    free = disk.get("free_gb")
    cont = sum(float(c.get("continuous_gb") or 0) for c in stats.get("cameras") or [])
    floor = policy.get("min_free_gb")
    want = policy.get("continuous_days") or sysinfo.get("retention_days")
    gb_day = mbps * 86400 / 8 / 1000
    days = max(0.0, (free + cont - (floor or 0))) / gb_day if (free is not None and gb_day) else None
    device, ms = sysinfo.get("yolo_device"), sysinfo.get("yolo_frame_ms")
    data = {"site": ref["name"], "mbps": round(mbps, 1), "cameras": n, "free_gb": free, "continuous_gb": round(cont, 1), "floor_gb": floor,
            "days": round(days, 1) if days is not None else None, "retention_days": want, "device": device, "yolo_frame_ms": ms}
    lines = [f"{ref['name']} would pull about {mbps:.0f} Mbps from {_plural(n, 'camera')}"]
    if days is not None:
        lines.append(f"Disk: {free:,} GB free + {cont:,.0f} GB of continuous footage, {floor if floor is not None else '?'} GB kept free → "
                     f"about {days:,.1f} days of continuous footage at that rate (retention policy: {want if want is not None else '?'} days)")
    if device:
        lines.append(f"Detection: {str(device).upper()}{f', {ms} ms per frame now' if ms else ''}, {_plural(n, 'camera')}")
    warnings = []
    if days is not None and want and days < float(want):
        warnings.append(f"Only about {days:,.1f} days of continuous footage fit on {ref['name']} at {mbps:.0f} Mbps, under its {want}-day "
                        "retention policy: the oldest footage will be trimmed sooner.")
    if mbps > BITRATE_WARN_MBPS and add_mbps:
        warnings.append(f"{ref['name']} would pull about {mbps:.0f} Mbps after the move (over {BITRATE_WARN_MBPS} Mbps): "
                        "check its network and disk can keep up.")
    if device == "cpu" and n > CPU_CAMERA_WARN and add_cams:
        warnings.append(f"{ref['name']} runs detection on its CPU and would have {n} cameras (over {CPU_CAMERA_WARN}): "
                        "verification may fall behind.")
    return data, lines, warnings


async def _site_info(ref: dict, u: dict) -> dict:
    data = await _get(registry.get(ref["id"]), u, "/api/system")
    return data if isinstance(data, dict) else {}


def _effective_labels(cam: dict, info: dict) -> list[str]:
    own = cam.get("synopsis_labels")
    return list(own) if own is not None else list(info.get("synopsis_labels_default") or ["person"])


def new_labels(p: dict, current: list[str]) -> list[str] | None:
    """The camera's labels after the action (None = back to the site default)."""
    mode, labels = p.get("label_mode"), p.get("labels") or []
    if p.get("labels_inherit"):
        return None
    if mode == "remove":
        out = [x for x in current if x not in labels]
    elif mode in ("only", "exact"):
        out = list(labels)
    else:
        out = [*current, *[x for x in labels if x not in current]]
    return [x for x in LABELS if x in out]


def _mute_key(org_id: str) -> str:
    return f"alerts_mute:{org_id}"


def current_mute(org_id: str) -> dict | None:
    row = db.one(sa.select(db.kv.c.value).where(db.kv.c.key == _mute_key(org_id)))
    v = row["value"] if row else None
    return v if isinstance(v, dict) and float(v.get("until") or 0) > time.time() else None


def _set_mute(org_id: str, value: dict | None) -> None:
    db.run(sa.delete(db.kv).where(db.kv.c.key == _mute_key(org_id)))
    if value:
        db.insert(db.kv, {"key": _mute_key(org_id), "value": value})


async def preview(p: dict, u: dict) -> dict:
    a = p["action"]
    moves: list[str] = []
    stays: list[str] = []
    warnings: list[str] = []
    blockers: list[str] = []
    cap_lines: list[str] = []
    cap = None
    verb = VERBS[a]
    if a in MOVES and p["source"] and p["target"]:
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
            if c.get("stream_ready") is False and not p["options"].get("skip_stream_check"):
                warnings.append(f"\"{c['name']}\" is not streaming at {src['name']} either: the stream check at {dst['name']} will likely fail "
                                "and roll the move back. Tick \"Skip the stream check\" to move it anyway.")
        ids = {c["id"] for c in cams}
        if src_conn is not None and len(ids) > 1:
            links = await _get(src_conn, u, "/api/topology")
            n = sum(1 for l in links if l.get("cam_a") in ids and l.get("cam_b") in ids) if isinstance(links, list) else 0
            if n:
                moves.append(f"{_plural(n, 'neighbor link')} between the moved cameras")
        if cams:
            moves.append(f"Named people and vehicles with their fingerprints, merged into {dst['name']} by name (names it already knows are kept)")
            moves.append(f"What {src['name']} learned about each camera: its \"what's normal\" baseline, parked-spot memory and operator corrections")
            moves.append("Hub dashboard widgets, camera groups and open alerts for these cameras, and their saved Find views, follow them")
            moves.append(f"Camera passwords go straight from {src['name']} to {dst['name']} through the hub; they are not stored or logged")
            moves.append(f"{dst['name']} must pull each stream within {STREAM_CHECK_S} s before {src['name']} lets go; otherwise nothing changes"
                         if not p["options"].get("skip_stream_check") else "Stream check skipped: the cameras move even if they are not streaming yet")
        for c in p.get("skipped") or []:
            stays.append(f"Camera \"{c['name']}\" is disabled on {src['name']} and is not moved")
        stays.append(f"Recordings and event clips stay on {src['name']}; {dst['name']} records these cameras from now on")
        stays.append(f"Event history: copied to {dst['name']} without clips (\"Copy event history\" is ticked); it also stays at {src['name']}"
                     if p["options"].get("copy_history") else f"Event history stays searchable at {src['name']} (tick \"Copy event history\" to copy it)")
        stays.append(f"{dst['name']}'s own settings, retention, layouts, dashboards and other cameras are not changed")
        if cams:
            stays.append(f"{src['name']} stops pulling the moved cameras (disabled there, not deleted, so their past events remain)")
        if a == "migrate_site":
            stays.append(f"{src['name']} is then retired: hidden from Fleet, Home, Find, Ask and alerts (Fleet → Show retired). "
                         "Its tunnel stays connected, so its history is still reachable")
            if not cams:
                warnings.append(f"{src['name']} has no enabled cameras; it will only be retired.")
        if dst_conn is not None and cams:
            dst_cams = await site_cameras(_row(dst["id"]), u) or []
            by_host = {str(c.get("host") or "").lower(): c for c in dst_cams}
            for c in cams:
                same = by_host.get(str(c.get("host") or "").lower())
                if same:
                    warnings.append(f"{dst['name']} already has \"{same['name']}\" at {same.get('host')}: if it is the same stream it is updated in place, not duplicated.")
            cap, cap_lines, w = await capacity(dst, u, dst_cams, sum(c.get("bitrate_mbps") or 0 for c in cams), len(cams))
            warnings += w
    elif a in ("retire_site", "restore_site") and p["site"]:
        s = p["site"]
        if a == "retire_site":
            moves.append(f"{s['name']} is hidden from Fleet, Home, Find, Ask and alerts (Fleet → Show retired brings it back)")
            stays.append(f"Its recordings, events and settings stay on {s['name']}; its tunnel stays connected and its page still opens")
            e_cams = [c for c in (await site_cameras(_row(s["id"]), u) or []) if c.get("enabled", 1)]
            if e_cams:
                warnings.append(f"{s['name']} still has {_plural(len(e_cams), 'enabled camera')}: they keep recording on the server but nobody sees "
                                f"them at the hub. To keep watching them, migrate instead (\"Migrate {s['name']} to …\").")
        else:
            moves.append(f"{s['name']} shows in Fleet, Home, Find, Ask and alerts again")
    elif a == "set_retention" and p["site"]:
        s = p["site"]
        conn = registry.get(s["id"])
        days = p.get("days") or 0
        if conn is None:
            blockers.append(f"{s['name']} is offline.")
        if not 1 <= days <= 365:
            blockers.append("Retention must be between 1 and 365 days.")
        pol = await _get(conn, u, "/api/retention/policy")
        current = ((pol or {}).get("policy") or {}).get("continuous_days") if isinstance(pol, dict) else None
        p["previous_days"] = current
        moves.append(f"{s['name']} keeps {_plural(days, 'day')} of continuous recording{f' (now {current})' if current is not None else ''}")
        stays.append("Event clips, locked footage and per-camera overrides follow their own rules, unchanged")
        summ = _row(s["id"]).get("summary") or {}
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
    elif a == "add_camera" and p["site"]:
        s = p["site"]
        if registry.get(s["id"]) is None:
            blockers.append(f"{s['name']} is offline.")
        moves.append(f"A new camera \"{p.get('new_name')}\" at {p.get('host')} on {s['name']}; it starts recording once its stream comes up")
        stays.append("The password goes to the site with Confirm only; the hub does not store, log or audit it")
        cams = await site_cameras(_row(s["id"]), u) or []
        same = next((c for c in cams if str(c.get("host") or "").lower() == str(p.get("host") or "").lower()), None)
        if same:
            warnings.append(f"{s['name']} already has \"{same['name']}\" at {same.get('host')}.")
        cap, cap_lines, w = await capacity(s, u, cams, NEW_CAMERA_MBPS, 1)
        if cap_lines:
            cap_lines.append(f"(counting {NEW_CAMERA_MBPS:.0f} Mbps for the new camera until it streams)")
        warnings += w
    elif a == "remove_camera" and p["site"]:
        for c in p["cameras"]:
            moves.append(f"\"{c['name']}\" is removed from {p['site']['name']} (disabled instead if it already has events)")
    elif a == "set_synopsis_labels" and p["site"]:
        s = p["site"]
        if registry.get(s["id"]) is None:
            blockers.append(f"{s['name']} is offline.")
        info = await _site_info(s, u)
        for c in p["cameras"]:
            cur = _effective_labels(c, info)
            new = new_labels(p, cur)
            after = new if new is not None else list(info.get("synopsis_labels_default") or ["person"])
            say = lambda xs: " and ".join("people" if x == "person" else "vehicles" for x in xs) or "nothing"   # noqa: E731
            p["previous_labels"] = c.get("synopsis_labels")
            if after == cur and new is not None and c.get("synopsis_labels") is not None:
                blockers.append(f"Qwen already describes {say(cur)} on \"{c['name']}\".")
            moves.append(f"Qwen describes {say(after)} on \"{c['name']}\" (now {say(cur)}){' (the site default)' if new is None else ''}")
            if not after:
                warnings.append("Nothing would be described on this camera: events are still verified by YOLO but get no synopsis.")
        stays.append("YOLO still verifies every person and vehicle; past synopses are kept")
    elif a == "lock_footage" and p["site"]:
        s = p["site"]
        if registry.get(s["id"]) is None:
            blockers.append(f"{s['name']} is offline.")
        info = await _site_info(s, u)
        off = float(info.get("tz_offset_s") if info.get("tz_offset_s") is not None else _hub_off())
        sp = p.get("span") or {}
        rng = range_ts(sp.get("time_from", ""), sp.get("time_to", ""), sp.get("day", ""), time.time(), off)
        if rng:
            p["lock"] = {"start_ts": rng[0], "end_ts": rng[1]}
            if rng[1] - rng[0] > MAX_LOCK_S:
                blockers.append("A lock can cover at most 7 days.")
            if rng[0] > time.time():
                blockers.append("That time hasn't happened yet.")
            for c in p["cameras"]:
                moves.append(f"\"{c['name']}\" footage from {_fmt_local(rng[0], off)} to {_fmt_local(rng[1], off)} ({s['name']} time) is kept "
                             "regardless of retention")
        stays.append("Footage outside that span follows the retention policy; the lock can be removed on the site's Retention page")
    elif a == "unlock_footage" and p["site"]:
        moves.append(f"Lock {p.get('lock_id')} at {p['site']['name']} is removed; that footage follows the retention policy again")
    elif a == "quiet_alerts":
        loc = p.get("location")
        # a Site's servers share one place, so the first one's clock reads "tonight" for all of them
        ref = p.get("site") or ((loc.get("servers") or [None])[0] if loc else None)
        label = loc["name"] if loc else ref["name"] if ref else None
        info = await _site_info(ref, u) if ref else {}
        off = float(info.get("tz_offset_s") if info.get("tz_offset_s") is not None else _hub_off())
        until = until_ts(p.get("until") or "", time.time(), off)
        if until:
            if until - time.time() > MAX_QUIET_S:
                blockers.append("Alerts can be quieted for at most 7 days.")
            p["mute_until"] = until
            servers = f" ({', '.join(s['name'] for s in loc['servers'])})" if loc else ""
            moves.append(f"No new event alerts (high priority, broken site rules, watched people) {'at ' + label + servers if label else 'at any site'} "
                         f"until {_fmt_local(until, off)}{' (' + label + ' time)' if label else ''}, and no push notifications for them")
        stays.append("Health alerts (site offline, camera down, disk, clock) still open; events are still recorded and described")
        prev = current_mute(p.get("org_id") or "")
        if prev:
            warnings.append(f"Alerts are already quiet until {_fmt_local(prev['until'], off)}: this replaces that.")
    elif a == "unquiet_alerts":
        moves.append("Event alerts open again" + (" (the previous quiet period is restored)" if p.get("previous") else ""))
    card = {"title": summary(p), "moves": moves, "stays": stays, "warnings": warnings, "blockers": blockers,
            "needs": p["needs"], "can_execute": not p["needs"] and not blockers, "capacity": cap_lines, "capacity_data": cap,
            "confirm_name": (p.get("source") or p.get("site") or {}).get("name") if verb.get("confirm_name") else None,
            "options": [{**o, "default": bool(p["options"].get(o["key"], o.get("default", False)))} for o in verb.get("options") or []],
            "inputs": verb.get("inputs") or [], "undo": verb.get("undo"), "role": verb.get("role", "admin")}
    return card


# ---------------------------------------------------------------- plans

def _sweep() -> None:
    now = time.time()
    for k in [k for k, v in _plans.items() if v["expires_at"] < now]:
        del _plans[k]


def public(p: dict) -> dict:
    cam = lambda c: {"id": c["id"], "name": c["name"], "host": c.get("host")}   # noqa: E731
    return {"id": p["id"], "action": p["action"], "summary": summary(p), "parser": p["parser"], "confidence": p["confidence"],
            "source": p["source"], "target": p["target"], "site": p["site"], "location": p.get("location"),
            "cameras": [cam(c) for c in p["cameras"]],
            "days": p["days"], "new_name": p["new_name"], "needs": p["needs"], "card": p["card"], "expires_at": p["expires_at"],
            "options": p["options"]}


async def build(u: dict, org_id: str, parsed: dict, text: str, parser: str, index: list[dict] | None = None) -> dict:
    index = index if index is not None else await _index(u, org_id)
    p = _resolve({**EMPTY, **parsed}, index, org_id)
    p.update(id=db.new_id("p_"), org_id=org_id, user_id=u["id"], text=text[:500], parser=parser,
             confidence=parsed.get("confidence") or "low", created_at=time.time(), expires_at=time.time() + PLAN_TTL_S)
    if p["confidence"] != "high" and not p["needs"]:
        p["needs"].append(f'I read this as "{summary(p)}" but I am not sure. Say it plainly to confirm, '
                          'e.g. "Move <camera> from <server> to <server>".')
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


def from_fields(fields: dict, internal: bool = False) -> dict:
    """An explicit plan from the API ({action, source_site, target_site, cameras, days, new_name, ...}): no AI
    involved. `internal` admits the undo-only verbs (restore_site, remove_camera, unlock_footage, unquiet_alerts)."""
    allowed = ALL_ACTIONS if internal else ACTIONS
    if fields.get("action") not in allowed:
        raise ActionError(f"action must be one of {', '.join(ACTIONS)}")
    out = {**EMPTY, "action": fields["action"], "confidence": "high"}
    for k in ("source_site", "target_site", "new_name", "host", "label_mode", "time_from", "time_to", "day", "until", "copy_history"):
        out[k] = str(fields.get(k) or "")
    out["cameras"] = [str(c) for c in fields.get("cameras") or []]
    out["labels"] = [x for x in fields.get("labels") or [] if x in LABELS]
    out["days"] = int(fields.get("days") or 0)
    if isinstance(fields.get("copy_history"), bool):
        out["copy_history"] = "yes" if fields["copy_history"] else "no"
    for k in ("skip_stream_check", "labels_inherit", "lock_id", "previous"):
        if k in fields:
            out[k] = fields[k]
    return out


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


async def _stream_check(dst: dict, u: dict, new_ids: dict[str, str], hosts: dict[str, str]) -> list[str]:
    """Wait until the destination's MediaMTX has every moved camera's stream ready. Returns the source ids that never came up."""
    deadline = time.time() + STREAM_CHECK_S
    waiting = set(new_ids)
    while waiting:
        listed = await site_cameras(_site_row(dst), u, fresh=True) or []
        ready = {c["id"] for c in listed if c.get("stream_ready")}
        waiting = {s for s in waiting if new_ids[s] not in ready}
        if not waiting or time.time() >= deadline:
            break
        await asyncio.sleep(STREAM_POLL_S)
    return sorted(waiting, key=lambda s: hosts.get(s, s))


async def _rollback(dst: dict, conn: AgentConn, u: dict, moved: dict[str, str], before: dict[str, bool]) -> list[str]:
    """Undo a merge on the destination: cameras it did not have are removed, ones it had disabled are disabled again."""
    failed = []
    for sid, new_id in moved.items():
        if new_id in before and before[new_id]:
            continue   # it had this camera, enabled, before the move: leave it
        q = {"purge": "true"} if new_id not in before else {}
        try:
            st, _ = await _call(conn, u, "DELETE", f"/api/cameras/{quote(new_id)}", "admin", urlencode(q))
            if st != 200:
                failed.append(new_id)
        except Exception:
            failed.append(new_id)
    _forget(dst["id"])
    return failed


async def _copy_history(p: dict, u: dict, src_conn: AgentConn, dst_conn: AgentConn, cam_map: dict[str, str]) -> tuple[int, int, dict[str, int]]:
    """Copy the moved cameras' events, a page of 200 at a time: metadata + fingerprints as JSON, then each event's
    images fetched from the source and posted to the destination in batches of up to FILE_BATCH_BYTES."""
    src, dst = p["source"], p["target"]
    after, events, images = 0, 0, 0
    event_map: dict[str, int] = {}
    headers = {"x-hub-user": u["email"], "x-hub-role": "viewer"}

    async def flush(batch: list[dict]) -> int:
        if not batch:
            return 0
        st, res = await _call(dst_conn, u, "POST", "/api/config/history/files", "admin", body={"files": batch}, extra=HANDOFF_HDR, timeout=HANDOFF_TIMEOUT_S)
        if st != 200:
            raise ActionError(f"{dst['name']} refused the event images ({_detail(res, st)})")
        return int((res or {}).get("files") or 0)

    while True:
        st, page = await _call(src_conn, u, "GET", "/api/config/history", "admin", urlencode({"cameras": ",".join(cam_map), "after_id": after}),
                               extra=HANDOFF_HDR, timeout=HANDOFF_TIMEOUT_S)
        if st != 200 or not isinstance(page, dict):
            raise ActionError(f"{src['name']} did not hand over the event history ({_detail(page, st)})")
        evs = page.get("events") or []
        if evs:
            wanted = {str(e["src_id"]): e.pop("files", []) or [] for e in evs}
            st, res = await _call(dst_conn, u, "POST", "/api/config/history", "admin",
                                  body={"source": {"site": src["name"], "site_id": src["id"]}, "cameras": cam_map, "events": evs},
                                  extra=HANDOFF_HDR, timeout=HANDOFF_TIMEOUT_S)
            if st != 200 or not isinstance(res, dict):
                raise ActionError(f"{dst['name']} refused the event history ({_detail(res, st)})")
            ids = {str(k): int(v) for k, v in (res.get("ids") or {}).items()}
            event_map.update(ids)
            events += int(res.get("added") or 0)
            batch, size = [], 0
            for sid, names in wanted.items():
                if sid not in ids:
                    continue
                for name in names:
                    try:
                        fst, raw = await src_conn.call("GET", f"/api/events/{sid}/media/{quote(name)}", "", headers, None, CALL_TIMEOUT_S)
                    except Exception:
                        continue
                    if fst != 200 or not raw:
                        continue
                    batch.append({"event_id": ids[sid], "name": name, "data": base64.b64encode(raw).decode()})
                    size += len(raw)
                    if size >= FILE_BATCH_BYTES:
                        images += await flush(batch)
                        batch, size = [], 0
            images += await flush(batch)
        after = page.get("next_after_id")
        if not after:
            break
    return events, images, event_map


def _remap(site_id: str, cam: str, src: str, dst: str, cam_map: dict[str, str]) -> tuple[str, str] | None:
    return (dst, cam_map[cam]) if site_id == src and cam in cam_map else None


def rewrite_references(org_id: str, src: str, dst: str, cam_map: dict[str, str], event_map: dict[str, int] | None = None) -> dict:
    """Hub-side references to a moved camera follow it: dashboard widgets (camera tiles, event-feed camera
    lists), camera groups and open alerts. Camera-down alerts are closed (the destination just proved the stream);
    event alerts move when their event was copied. Returns counts."""
    counts = {"dashboards": 0, "groups": 0, "alerts": 0, "alerts_closed": 0}
    for row in db.rows(sa.select(db.dashboards).where(db.dashboards.c.org_id == org_id)):
        cfg = copy.deepcopy(row["config"] or {})
        changed = False
        for w in cfg.get("widgets") or []:
            pr = w.get("props") or {}
            if w.get("type") == "camera":
                r = _remap(pr.get("site"), pr.get("camera"), src, dst, cam_map)
                if r:
                    pr["site"], pr["camera"] = r
                    changed = True
            for ref in pr.get("cameras") or []:
                r = _remap(ref.get("site"), ref.get("camera"), src, dst, cam_map)
                if r:
                    ref["site"], ref["camera"] = r
                    changed = True
            if changed and pr.get("sites") is not None and src in pr["sites"] and dst not in pr["sites"] and pr.get("cameras"):
                pr["sites"] = [*pr["sites"], dst]
        if changed:
            db.run(sa.update(db.dashboards).where(db.dashboards.c.id == row["id"]).values(config=cfg, updated_at=time.time()))
            counts["dashboards"] += 1
    for row in db.rows(sa.select(db.camera_groups).where(db.camera_groups.c.org_id == org_id)):
        members, changed, seen = [], False, set()
        for m in row["members"] or []:
            r = _remap(m.get("site_id"), m.get("camera_id"), src, dst, cam_map)
            if r:
                m, changed = {"site_id": r[0], "camera_id": r[1]}, True
            k = (m.get("site_id"), m.get("camera_id"))
            if k not in seen:
                seen.add(k)
                members.append(m)
        if changed:
            db.run(sa.update(db.camera_groups).where(db.camera_groups.c.id == row["id"]).values(members=members, updated_at=time.time()))
            counts["groups"] += 1
    for a in db.rows(sa.select(db.alerts).where(db.alerts.c.org_id == org_id, db.alerts.c.site_id == src, db.alerts.c.closed_at.is_(None))):
        det = dict(a["detail"] or {})
        if a["kind"] == "camera_down" and a["key"] in cam_map:
            db.run(sa.update(db.alerts).where(db.alerts.c.id == a["id"]).values(closed_at=time.time()))
            counts["alerts_closed"] += 1
        elif det.get("camera_id") in cam_map and event_map and str(det.get("id") or a["key"]) in event_map:
            new_eid = event_map[str(det.get("id") or a["key"])]
            det.update(camera_id=cam_map[det["camera_id"]], id=new_eid, moved_from=src)
            db.run(sa.update(db.alerts).where(db.alerts.c.id == a["id"]).values(site_id=dst, key=str(new_eid), detail=det))
            counts["alerts"] += 1
    return counts


async def _copy_find_views(p: dict, u: dict, src_conn: AgentConn, dst_conn: AgentConn, cam_map: dict[str, str]) -> int:
    """The source's saved Find views filtered to a moved camera are added to the destination with the new id
    (the source keeps its own: the history they show is still there)."""
    st, have = await _call(src_conn, u, "GET", "/api/find/views")
    views = (have or {}).get("views") if st == 200 and isinstance(have, dict) else None
    mine = [v for v in views or [] if isinstance(v, dict) and (v.get("filters") or {}).get("camera") in cam_map]
    if not mine:
        return 0
    st, there = await _call(dst_conn, u, "GET", "/api/find/views")
    dst_views = list((there or {}).get("views") or []) if st == 200 and isinstance(there, dict) else []
    added = 0
    for v in mine:
        nv = copy.deepcopy(v)
        nv["filters"]["camera"] = cam_map[v["filters"]["camera"]]
        if any((x.get("filters") or {}) == nv["filters"] and x.get("mode") == nv.get("mode") for x in dst_views):
            continue   # already there (a retried move, or moving back)
        same = next((x for x in dst_views if x.get("name") == nv["name"]), None)
        if same:
            nv["name"] = f"{nv['name']} ({p['source']['name']})"[:60]
        if "id" in nv:
            nv["id"] = f"{nv['id']}-{cam_map[v['filters']['camera']]}"[:60]
        dst_views.append(nv)
        added += 1
    if added:
        st, res = await _call(dst_conn, u, "PUT", "/api/find/views", "admin", body={"views": dst_views[:50]})
        if st != 200:
            raise ActionError(f"{p['target']['name']} refused the saved Find views ({_detail(res, st)})")
    return added


async def _move(p: dict, u: dict, lines: list[str], detail: dict) -> None:
    src, dst = p["source"], p["target"]
    src_conn, dst_conn = _conn(src), _conn(dst)
    _site_row(src)
    _site_row(dst)
    ids = [c["id"] for c in p["cameras"]]
    names = {c["id"]: c["name"] for c in p["cameras"]}
    hosts = {c["id"]: c.get("host") or c["id"] for c in p["cameras"]}
    moved: dict[str, str] = {}
    opts = p["options"]
    if ids:
        before = {c["id"]: bool(c.get("enabled", 1)) for c in await site_cameras(_site_row(dst), u, fresh=True) or []}
        st, handoff = await _call(src_conn, u, "GET", "/api/config/handoff", "admin", urlencode({"cameras": ",".join(ids)}),
                                  extra=HANDOFF_HDR, timeout=HANDOFF_TIMEOUT_S)
        if st != 200 or not isinstance(handoff, dict):
            raise ActionError(f"{src['name']} did not hand the cameras over ({_detail(handoff, st)}); nothing changed")
        got = {c.get("id") for c in handoff.get("cameras") or []}
        if set(ids) - got:
            raise ActionError(f"{src['name']} no longer has {', '.join(names[i] for i in set(ids) - got)}; nothing changed")
        handoff["source"] = src["name"]
        try:
            st, res = await _call(dst_conn, u, "POST", "/api/config/merge", "admin", body={"data": handoff}, timeout=HANDOFF_TIMEOUT_S)
        finally:
            handoff = None   # the only copy of the passwords at the hub: drop it now  # noqa: F841
        if st != 200 or not isinstance(res, dict):
            raise ActionError(f"{dst['name']} could not add the cameras ({_detail(res, st)}); {src['name']} is unchanged")
        moved = {k: v for k, v in (res.get("ids") or {}).items() if k in names}
        detail["merge"] = {k: res.get(k) for k in ("cameras", "camera_links", "identities", "learned")}
        listed = await site_cameras(_site_row(dst), u, fresh=True)
        missing = [names[k] for k, v in moved.items() if not listed or v not in {c["id"] for c in listed}]
        if len(moved) != len(ids) or missing:
            await _rollback(dst, dst_conn, u, moved, before)
            raise ActionError(f"{dst['name']} does not list {', '.join(missing) or 'every camera'} after the import; "
                              f"{src['name']} keeps pulling them. Check {dst['name']}'s Cameras page")
        if not opts.get("skip_stream_check"):
            down = await _stream_check(dst, u, moved, hosts)
            if down:
                failed = await _rollback(dst, dst_conn, u, moved, before)
                detail["stream_check"] = {"failed": down, "rollback_failed": failed}
                raise ActionError(f"{dst['name']} could not reach {', '.join(hosts[s] for s in down)} within {STREAM_CHECK_S:g} s: check VLAN/firewall. "
                                  f"The move was rolled back; {src['name']} is unchanged"
                                  + (f" (remove {', '.join(failed)} on {dst['name']} by hand)" if failed else ""))
            lines.append(f"{dst['name']} is pulling {'the stream' if len(moved) == 1 else 'every stream'}")
        for sid, new_id in moved.items():
            lines.append(f"\"{names[sid]}\" now records on {dst['name']}" + (f" (as {new_id})" if new_id != sid else ""))
        if res.get("identities"):
            n = int(res["identities"])
            lines.append(f"{n} named {'person/vehicle' if n == 1 else 'people/vehicles'} added to {dst['name']}")
        learned = res.get("learned") or {}
        if any(learned.values()):
            parts = [x for x in (f"{_plural(learned.get('baseline') or 0, 'baseline')}" if learned.get("baseline") else "",
                                 f"{_plural(learned.get('parked') or 0, 'parked spot')}" if learned.get("parked") else "",
                                 f"{_plural(learned.get('corrections') or 0, 'operator correction')}" if learned.get("corrections") else "") if x]
            lines.append(f"Learned state carried over: {', '.join(parts)}")
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
        event_map: dict[str, int] = {}
        if opts.get("copy_history") and moved:
            try:
                n_ev, n_img, event_map = await _copy_history(p, u, src_conn, dst_conn, moved)
                lines.append(f"Copied {_plural(n_ev, 'event')} ({_plural(n_img, 'image')}, no clips) to {dst['name']}; they also stay at {src['name']}"
                             if n_ev else "No event history to copy")
                detail["history"] = {"events": n_ev, "images": n_img}
            except (ActionError, RuntimeError) as e:
                lines.append(f"Warning: the event history was not fully copied ({e}); it is still at {src['name']}")
                detail["history_error"] = str(e)[:200]
        refs = rewrite_references(p["org_id"], src["id"], dst["id"], moved, event_map)
        try:
            refs["find_views"] = await _copy_find_views(p, u, src_conn, dst_conn, moved)
        except (ActionError, RuntimeError) as e:
            refs["find_views"] = 0
            lines.append(f"Warning: saved Find views were not copied ({e})")
        detail["references"] = refs
        said = [x for x in (_plural(refs["dashboards"], "dashboard") if refs["dashboards"] else "",
                            _plural(refs["groups"], "camera group") if refs["groups"] else "",
                            _plural(refs["alerts"], "open alert") if refs["alerts"] else "",
                            _plural(refs["find_views"], "saved Find view") + f" (added to {dst['name']})" if refs["find_views"] else "") if x]
        if said:
            lines.append(f"References updated to {dst['name']}: {', '.join(said)}")
        if refs["alerts_closed"]:
            lines.append(f"{_plural(refs['alerts_closed'], 'camera-down alert')} closed (the stream is up at {dst['name']})")
    detail["cameras"] = [{"id": k, "name": names[k], "new_id": v} for k, v in moved.items()]
    _forget(src["id"], dst["id"])
    if p["action"] == "migrate_site":
        retire(src["id"])
        lines.append(f"{src['name']} is retired (Fleet → Show retired)")
    if moved:
        detail["reverse"] = [*([{"action": "restore_site", "source_site": src["id"]}] if p["action"] == "migrate_site" else []),
                             {"action": "move_cameras", "source_site": dst["id"], "target_site": src["id"], "cameras": list(moved.values()),
                              "copy_history": False, **({"skip_stream_check": True} if opts.get("skip_stream_check") else {})}]
    elif p["action"] == "migrate_site":
        detail["reverse"] = [{"action": "restore_site", "source_site": src["id"]}]


CAMERA_SECRETS = ("password", "username", "main_path", "sub_path", "onvif_port", "rtsp_port")


def _camera_inputs(raw: dict | None) -> dict:
    """The add_camera card's fields, checked without ever echoing what was typed."""
    raw = raw if isinstance(raw, dict) else {}
    out: dict = {"password": "", "username": "admin", "main_path": "/main", "sub_path": "/sub", "onvif_port": 80, "rtsp_port": 554}
    for k in ("password", "username", "main_path", "sub_path"):
        v = raw.get(k)
        if v is None or v == "":
            continue
        if not isinstance(v, str) or len(v) > 128:
            raise ActionError(f"{k.replace('_', ' ')} is not valid")
        if k.endswith("_path") and not re.fullmatch(r"/[A-Za-z0-9_\-./?=&%]{0,120}", v):
            raise ActionError(f"{k.replace('_', ' ')} must start with / (e.g. /main)")
        out[k] = v
    for k in ("onvif_port", "rtsp_port"):
        v = raw.get(k)
        if v in (None, ""):
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ActionError(f"{k.replace('_', ' ')} must be a number") from None
        if not 1 <= n <= 65535:
            raise ActionError(f"{k.replace('_', ' ')} must be 1-65535")
        out[k] = n
    if not out["password"]:
        raise ActionError("Type the camera's password into the card")
    return out


async def _put_camera(p: dict, u: dict, cur: dict, **changes) -> object:
    body = {k: cur[k] for k in CAMERA_FIELDS if cur.get(k) is not None}   # no password: the site keeps it
    body.update(enabled=bool(cur.get("enabled", 1)), **changes)
    st, res = await _call(_conn(p["site"]), u, "PUT", f"/api/cameras/{quote(cur['id'])}", "admin", body=body)
    if st != 200:
        raise ActionError(f"{p['site']['name']} refused ({_detail(res, st)})")
    _forget(p["site"]["id"])
    return res


async def _current_camera(p: dict, u: dict) -> dict:
    cam = p["cameras"][0]
    cams = await site_cameras(_site_row(p["site"]), u, fresh=True)
    cur = next((c for c in cams or [] if c["id"] == cam["id"]), None)
    if cur is None:
        raise ActionError(f"{p['site']['name']} no longer has {cam['name']}")
    return cur


async def _run(p: dict, u: dict, lines: list[str], detail: dict, extras: dict) -> None:
    a = p["action"]
    if a in MOVES:
        await _move(p, u, lines, detail)
    elif a == "retire_site":
        _site_row(p["site"])
        retire(p["site"]["id"])
        lines.append(f"{p['site']['name']} is retired (Fleet → Show retired)")
        detail["reverse"] = [{"action": "restore_site", "source_site": p["site"]["id"]}]
    elif a == "restore_site":
        _site_row(p["site"])
        retire(p["site"]["id"], False)
        _forget(p["site"]["id"])
        lines.append(f"{p['site']['name']} is back in Fleet, Home, Find, Ask and alerts")
    elif a == "set_retention":
        st, res = await _call(_conn(p["site"]), u, "PUT", "/api/retention/policy", "admin", body={"continuous_days": int(p["days"])})
        if st != 200:
            raise ActionError(f"{p['site']['name']} refused ({_detail(res, st)})")
        days = ((res or {}).get("policy") or {}).get("continuous_days", p["days"]) if isinstance(res, dict) else p["days"]
        lines.append(f"{p['site']['name']} now keeps {_plural(int(days), 'day')} of continuous recording")
        detail.update(days=p["days"], previous_days=p.get("previous_days"))
        if p.get("previous_days"):
            detail["reverse"] = [{"action": "set_retention", "source_site": p["site"]["id"], "days": int(p["previous_days"])}]
    elif a == "rename_camera":
        cam = p["cameras"][0]
        cur = await _current_camera(p, u)
        await _put_camera(p, u, cur, name=p["new_name"])
        lines.append(f"\"{cam['name']}\" on {p['site']['name']} is now \"{p['new_name']}\"")
        detail.update(camera=cam["id"], old_name=cur["name"], new_name=p["new_name"])
        detail["reverse"] = [{"action": "rename_camera", "source_site": p["site"]["id"], "cameras": [cam["id"]], "new_name": cur["name"]}]
    elif a == "set_synopsis_labels":
        cur = await _current_camera(p, u)
        info = await _site_info(p["site"], u)
        new = new_labels(p, _effective_labels(cur, info))
        await _put_camera(p, u, cur, synopsis_labels=new)
        say = " and ".join("people" if x == "person" else "vehicles" for x in (new if new is not None else info.get("synopsis_labels_default") or ["person"])) or "nothing"
        lines.append(f"Qwen now describes {say} on \"{cur['name']}\" at {p['site']['name']}")
        prev = cur.get("synopsis_labels")
        detail.update(camera=cur["id"], labels=new, previous_labels=prev)
        detail["reverse"] = [{"action": "set_synopsis_labels", "source_site": p["site"]["id"], "cameras": [cur["id"]], "label_mode": "exact",
                              "labels": prev or [], "labels_inherit": prev is None}]
    elif a == "add_camera":
        conn = _conn(p["site"])
        fields = _camera_inputs(extras.get("camera"))
        cams = await site_cameras(_site_row(p["site"]), u, fresh=True) or []
        taken = {c["id"] for c in cams}
        n = 1
        while f"cam{n}" in taken:
            n += 1
        cid = f"cam{n}"
        body = {"id": cid, "name": p["new_name"] or p["host"], "host": p["host"], "enabled": True, "zones": [], "scene_notes": "", "policies": [], **fields}
        try:
            st, res = await _call(conn, u, "PUT", f"/api/cameras/{cid}", "admin", body=body)
        finally:
            body = fields = None   # the password: gone from the hub  # noqa: F841
        if st != 200:
            raise ActionError(f"{p['site']['name']} refused the camera ({_detail(res, st)})")
        _forget(p["site"]["id"])
        lines.append(f"\"{p['new_name']}\" ({p['host']}) added to {p['site']['name']} as {cid}")
        down = await _stream_check(p["site"], u, {cid: cid}, {cid: p["host"]})
        lines.append(f"{p['site']['name']} is pulling its stream" if not down else
                     f"Warning: {p['site']['name']} could not reach {p['host']} within {STREAM_CHECK_S:g} s: check the address, password, VLAN and firewall")
        detail.update(camera=cid, host=p["host"], name=p["new_name"], streaming=not down)
        detail["reverse"] = [{"action": "remove_camera", "source_site": p["site"]["id"], "cameras": [cid]}]
    elif a == "remove_camera":
        cam = p["cameras"][0]
        st, res = await _call(_conn(p["site"]), u, "DELETE", f"/api/cameras/{quote(cam['id'])}", "admin", urlencode({"purge": "true"}))
        if st != 200:
            raise ActionError(f"{p['site']['name']} refused ({_detail(res, st)})")
        _forget(p["site"]["id"])
        how = (res or {}).get("removed") if isinstance(res, dict) else None
        lines.append(f"\"{cam['name']}\" {'removed from' if how == 'deleted' else 'disabled on'} {p['site']['name']}")
    elif a == "lock_footage":
        cam, lk = p["cameras"][0], p.get("lock")
        if not lk:
            raise ActionError("no time span")
        st, res = await _call(_conn(p["site"]), u, "POST", "/api/locks", "operator",
                              body={"camera_id": cam["id"], "start_ts": lk["start_ts"], "end_ts": lk["end_ts"], "note": f"locked from the hub by {u['email']}"[:200]})
        if st != 200 or not isinstance(res, dict):
            raise ActionError(f"{p['site']['name']} refused the lock ({_detail(res, st)})")
        lines.append(f"\"{cam['name']}\" footage is locked at {p['site']['name']} ({card_span(p)})")
        detail.update(camera=cam["id"], lock_id=res.get("id"), start_ts=lk["start_ts"], end_ts=lk["end_ts"])
        if res.get("id"):
            detail["reverse"] = [{"action": "unlock_footage", "source_site": p["site"]["id"], "lock_id": res["id"]}]
    elif a == "unlock_footage":
        st, res = await _call(_conn(p["site"]), u, "DELETE", f"/api/locks/{int(p.get('lock_id') or 0)}", "operator")
        if st != 200:
            raise ActionError(f"{p['site']['name']} refused ({_detail(res, st)})")
        lines.append(f"Lock {p.get('lock_id')} removed at {p['site']['name']}")
    elif a == "quiet_alerts":
        prev = current_mute(p["org_id"])
        until = p.get("mute_until")
        if not until:
            raise ActionError("no end time")
        loc = p.get("location")
        # alerts.muted() reads only `sites` (server ids); `location` is for people reading the kv row / audit
        mute = {"until": until, "sites": [s["id"] for s in loc["servers"]] if loc else [p["site"]["id"]] if p.get("site") else None,
                "by": u["email"], "at": time.time()}
        if loc:
            mute["location"] = {"id": loc["id"], "name": loc["name"]}
        _set_mute(p["org_id"], mute)
        lines.append(f"Event alerts are quiet {_quiet_where(p)} until {time.strftime('%a %H:%M', time.localtime(until))} (hub time)")
        detail.update(until=until, site=loc["name"] if loc else (p.get("site") or {}).get("name"))
        detail["reverse"] = [{"action": "unquiet_alerts", "previous": prev}]
    elif a == "unquiet_alerts":
        prev = p.get("previous")
        _set_mute(p["org_id"], prev if isinstance(prev, dict) and float(prev.get("until") or 0) > time.time() else None)
        lines.append("Event alerts are on again" if not prev else "The previous quiet period is back")


def card_span(p: dict) -> str:
    sp = p.get("span") or {}
    return f"{sp.get('time_from')}-{sp.get('time_to')} {sp.get('day') or 'today'}"


def _insert_audit(values: dict) -> int:
    with db.engine().begin() as c:
        r = c.execute(db.audit_log.insert().values(**values))
        return int(r.inserted_primary_key[0])


def undo_until(row: dict) -> float | None:
    """When Undo stops being offered for this audit row (None: not undoable / already undone)."""
    d = row.get("detail") or {}
    if row.get("method") != "ACTION" or row.get("status") != 200 or not d.get("reverse") or d.get("undone_at"):
        return None
    until = float(row["ts"]) + UNDO_TTL_S
    return until if until > time.time() else None


async def execute(p: dict, u: dict, extras: dict | None = None, undo_of: int | None = None) -> dict:
    """Carry out a confirmed plan. Returns {"ok", "lines", "summary", "audit_id", "undo_until"}; one audit_log row
    either way (never any secret). `extras`: the card's options and add_camera fields (password: sent on, never kept)."""
    extras = extras or {}
    for k, v in (extras.get("options") or {}).items():
        if k in ("copy_history", "skip_stream_check") and isinstance(v, bool):
            p["options"][k] = v
    async with _exec_lock:
        card = await preview(p, u)          # sites may have gone offline since the card was shown
        if not card["can_execute"]:
            raise ActionError("; ".join(card["needs"] + card["blockers"]) or "this plan cannot be carried out")
        lines: list[str] = []
        detail: dict = {"plan": p["id"], "action": p["action"], "text": p["text"], "options": p["options"],
                        **{k: (p[k] or {}).get("name") for k in ("source", "target", "site") if p.get(k)}}
        if undo_of:
            detail["undo_of"] = undo_of
        ok, error = True, None
        try:
            await _run(p, u, lines, detail, extras)
        except ActionError as e:
            ok, error = False, str(e)
        except Exception as e:   # a tunnel failure mid-way
            ok, error = False, f"{type(e).__name__}: {str(e)[:160]}"
        finally:
            extras.pop("camera", None)
        if error:
            lines.append(f"Failed: {error}")
            detail["error"] = error
            detail.pop("reverse", None)
        if undo_of:
            detail.pop("reverse", None)   # an undo is not itself undone from the card; do the action again instead
        detail["result"] = lines
        row = {"ts": time.time(), "user_id": u["id"], "user_email": u["email"], "org_id": p["org_id"],
               "site_id": (p.get("source") or p.get("site") or {}).get("id"), "action": f"fleet action: {'Undo: ' if undo_of else ''}{summary(p)}"[:200],
               "method": "ACTION", "path": None, "status": 200 if ok else 500, "ip": None, "detail": detail}
        audit_id = _insert_audit(row)
        log.info("fleet action by %s: %s -> %s", u["email"], summary(p), "done" if ok else error)
        _plans.pop(p["id"], None)
        return {"ok": ok, "lines": lines, "summary": summary(p), "audit_id": audit_id, "undo_until": undo_until({**row, "id": audit_id})}


async def undo(u: dict, org_id: str, audit_id: int) -> dict:
    """Run the reverse plan stored with an audit row (within 24 h, once): move back, restore, old name, ..."""
    row = db.one(sa.select(db.audit_log).where(db.audit_log.c.id == audit_id, db.audit_log.c.org_id == org_id))
    if not row:
        raise LookupError("no such action")
    if not undo_until(row):
        raise ActionError("this action can no longer be undone (undone already, failed, or older than 24 hours)")
    lines: list[str] = []
    ok = True
    steps = (row["detail"] or {}).get("reverse") or []
    ids = []
    for step in steps:
        try:
            p = await build(u, org_id, from_fields(step, internal=True), f"(undo of #{audit_id}) {step.get('action')}", "undo")
        except ActionError as e:
            lines.append(f"Failed: {e}")
            ok = False
            break
        if not p["card"]["can_execute"]:
            lines.append("Failed: " + ("; ".join(p["card"]["needs"] + p["card"]["blockers"]) or "cannot be carried out"))
            ok = False
            break
        res = await execute(p, u, {}, undo_of=audit_id)
        ids.append(res["audit_id"])
        lines += res["lines"]
        if not res["ok"]:
            ok = False
            break
    if ok:
        d = dict(row["detail"] or {})
        d.update(undone_at=time.time(), undone_by=u["email"], undo_audit=ids)
        db.run(sa.update(db.audit_log).where(db.audit_log.c.id == audit_id).values(detail=d))
    return {"ok": ok, "lines": lines, "summary": f"Undo: {row['action'].removeprefix('fleet action: ')}", "audit_id": ids[-1] if ids else None,
            "undo_until": None}
