"""A Site's talk-down voice: one Piper voice, speed and attention chime for every server of the Site (backend tts.py).

Stored per Site (site_talkdown_voice; no row = never set, the servers keep their own). Site admins change it (api.py
PUT /api/locations/{id}/talkdown-voice); the hub then pushes it to every connected server of the Site with
PUT /api/talkdown/voice through the tunnel (role admin, as the user who changed it, or "hub" for a re-push).
Each server reports its setting, its installed voice ids, its engines and who speaks now in the heartbeat summary
(`talkdown_voice`, no paths or secrets). A server that reports another setting (offline at the change, restored, moved
here from another Site, edited directly) gets the Site's again on that heartbeat, at most once per REPUSH_S for the same
value. A server whose summary has no `talkdown_voice` is too old to take it and is left alone.
The voice needn't be installed on every server: each one falls back on its own (another Piper voice, Windows SAPI,
espeak) and says so; Settings › Cameras shows that per server.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time

import sqlalchemy as sa

from . import db

log = logging.getLogger("hub.talkdown_voice")

VOICE_RX = r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,79}$"   # backend tts.VOICE_RX
SPEED_MIN, SPEED_MAX = 0.7, 1.3
REPUSH_S = 120.0          # a server still reporting another value gets the same value again at most this often
PUSH_TIMEOUT_S = 10.0
PATH = "/api/talkdown/voice"

_last: dict[str, tuple[tuple, float]] = {}   # server id -> (value pushed, when)
_tasks: set[asyncio.Task] = set()


def clean(value) -> dict:
    """{voice: id | None, speed: 0.7-1.3, chime: bool} from a stored or reported value (bad parts: the defaults)."""
    v = value if isinstance(value, dict) else {}
    voice = v.get("voice")
    voice = voice if isinstance(voice, str) and re.match(VOICE_RX, voice) else None
    try:
        speed = float(v.get("speed", 1.0))
    except (TypeError, ValueError):
        speed = 1.0
    speed = 1.0 if math.isnan(speed) else round(min(SPEED_MAX, max(SPEED_MIN, speed)), 2)
    return {"voice": voice, "speed": speed, "chime": bool(v.get("chime", False))}


def key(s: dict) -> tuple:
    c = clean(s)
    return c["voice"], c["speed"], c["chime"]


def get(location_id: str) -> dict | None:
    """The Site's setting with who changed it when; None = never set."""
    r = db.one(sa.select(db.site_talkdown_voice).where(db.site_talkdown_voice.c.location_id == location_id))
    if not r:
        return None
    return {**clean(r), "updated_at": r["updated_at"], "updated_by": r["updated_by"]}


def put(loc: dict, value: dict, by: str) -> dict:
    s = clean(value)
    t = db.site_talkdown_voice
    row = {**s, "org_id": loc["org_id"], "updated_at": time.time(), "updated_by": by}
    with db.engine().begin() as c:
        if c.execute(sa.select(t.c.location_id).where(t.c.location_id == loc["id"])).first():
            c.execute(sa.update(t).where(t.c.location_id == loc["id"]).values(**row))
        else:
            c.execute(t.insert().values(location_id=loc["id"], **row))
    return get(loc["id"])


def drop_location(conn, location_id: str) -> None:
    """The Site is deleted: its voice goes with it (its servers moving elsewhere take the new Site's)."""
    conn.execute(sa.delete(db.site_talkdown_voice).where(db.site_talkdown_voice.c.location_id == location_id))


def _servers(location_id: str) -> list[dict]:
    t = db.sites.c
    rows = db.rows(sa.select(t.id, t.name, t.summary, t.online, t.retired_at).where(t.location_id == location_id))
    return sorted((r for r in rows if not r.get("retired_at")), key=lambda r: (r["name"].casefold(), r["id"]))


def servers_status(location_id: str, setting: dict | None, connected=None) -> list[dict]:
    """Per server (by name): what it reports. `supported` false = too old to take the voice (nothing is pushed);
    `in_sync` = its setting is the Site's; `voices` = the Piper voice ids installed on it; `effective` = who speaks
    there now (the voice, or its fallback, with the reason)."""
    out = []
    for s in _servers(location_id):
        tv = (s.get("summary") or {}).get("talkdown_voice")
        ok = isinstance(tv, dict)
        mine = clean(tv.get("setting")) if ok else None
        out.append({"server_id": s["id"], "server_name": s["name"],
                    "online": bool(connected(s["id"])) if connected else bool(s.get("online")),
                    "supported": ok, "setting": mine,
                    "in_sync": ok and setting is not None and key(mine) == key(setting),
                    "voices": [str(v) for v in (tv.get("voices") or []) if isinstance(v, str)] if ok else [],
                    "engines": tv.get("engines") if ok and isinstance(tv.get("engines"), dict) else {},
                    "effective": tv.get("effective") if ok and isinstance(tv.get("effective"), dict) else None})
    return out


def _reflect(server_id: str, answer: dict) -> None:
    """A server took the voice: its stored summary says so at once (the next heartbeat would anyway)."""
    row = db.one(sa.select(db.sites.c.summary).where(db.sites.c.id == server_id))
    summary = dict((row or {}).get("summary") or {})
    tv = summary.get("talkdown_voice")
    if not isinstance(tv, dict):
        return
    eff = answer.get("effective")
    tv = {**tv, "setting": clean(answer.get("setting")), "effective": eff if isinstance(eff, dict) else None}
    db.run(sa.update(db.sites).where(db.sites.c.id == server_id).values(summary={**summary, "talkdown_voice": tv}))


async def push_one(conn, setting: dict, by: str = "hub") -> dict:
    """PUT the Site's voice to one connected server. {server_id, server_name, ok, error, effective}."""
    name = (conn.site or {}).get("name") or conn.site_id
    s = clean(setting)
    _last[conn.site_id] = (key(s), time.time())
    out = {"server_id": conn.site_id, "server_name": name, "ok": False, "error": None, "effective": None}
    try:
        status, raw = await conn.call("PUT", PATH, "", {"x-hub-user": by, "x-hub-role": "admin", "content-type": "application/json"},
                                      json.dumps(s).encode(), PUSH_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 - offline, timeout, tunnel trouble: reported, retried on a heartbeat
        out["error"] = str(e)[:160] or type(e).__name__
        log.warning("talk-down voice to %s failed: %s", conn.site_id, out["error"])
        return out
    try:
        answer = json.loads(raw.decode() or "null")
    except ValueError:
        answer = None
    if not 200 <= status < 300:
        d = answer.get("detail") if isinstance(answer, dict) else None
        out["error"] = f"HTTP {status}" + (f": {str(d)[:160]}" if d else "")
        log.warning("talk-down voice to %s refused: %s", conn.site_id, out["error"])
        return out
    out["ok"] = True
    if isinstance(answer, dict):
        out["effective"] = answer.get("effective")
        try:
            _reflect(conn.site_id, answer)
        except Exception:  # noqa: BLE001 - the push itself worked
            log.exception("talk-down voice: reflecting %s", conn.site_id)
    log.info("talk-down voice %s pushed to %s by %s", s["voice"] or "automatic", conn.site_id, by)
    return out


def _conns(location_id: str, registry) -> list:
    return [c for c in list(registry.by_site.values())
            if c.site and c.site.get("location_id") == location_id and not c.site.get("retired_at")]


def supports(conn) -> bool:
    """The server reports `talkdown_voice` (its last heartbeat, else the stored summary): it has the route."""
    summary = conn.summary or (conn.site or {}).get("summary") or {}
    return isinstance(summary.get("talkdown_voice"), dict)


async def push_location(location_id: str, setting: dict, registry, by: str) -> list[dict]:
    """Push to every connected server of the Site that takes it, together; the offline ones get it on their first
    heartbeat. A server too old for it is listed with `skipped`."""
    conns = _conns(location_id, registry)
    old = [{"server_id": c.site_id, "server_name": c.site.get("name") or c.site_id, "ok": False, "skipped": True,
            "error": "this server's version has no talk-down voices", "effective": None} for c in conns if not supports(c)]
    new = [c for c in conns if supports(c)]
    return (list(await asyncio.gather(*(push_one(c, setting, by) for c in new))) if new else []) + old


def needs_push(conn, setting: dict | None, now: float | None = None) -> bool:
    """The server's heartbeat reports a voice that isn't the Site's, and the same value wasn't just pushed to it."""
    if setting is None or not conn.site_id:
        return False
    tv = (conn.summary or {}).get("talkdown_voice")
    if not isinstance(tv, dict) or key(tv.get("setting")) == key(setting):
        return False
    last = _last.get(conn.site_id)
    now = time.time() if now is None else now
    return not (last and last[0] == key(setting) and now - last[1] < REPUSH_S)


def on_heartbeat(conn) -> None:
    """agents.py, on each heartbeat: re-push the Site's voice when the server reports another one (in the background)."""
    loc = (conn.location or {}).get("id") or (conn.site or {}).get("location_id")
    if not loc or not isinstance((conn.summary or {}).get("talkdown_voice"), dict):
        return
    setting = get(loc)
    if not needs_push(conn, setting):
        return
    task = asyncio.get_running_loop().create_task(push_one(conn, setting))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
