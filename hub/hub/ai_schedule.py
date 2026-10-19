"""A Site's AI schedule, and its servers' Qwen use (the server side is backend nvr/aischedule.py).

Qwen is the expensive shared GPU, so how much of it a Site gets is a pricing tier, per Site (locations.ai_mode):
  full    Qwen does all of its work around the clock. NULL means full: every Site that existed before this, and every
          new one, until a hub administrator switches it (full-time AI is the paid upgrade).
  soc     full Qwen only while the Site's SOC monitoring is armed (soc.compute_armed: override, holiday, weekly windows;
          an unmonitored Site is never armed). Outside those hours the servers still run PPE second looks and everything
          a person asks for; descriptions follow locations.ai_outside: "catchup" (NULL: queued at the lowest priority,
          written while Qwen is idle) or "skip" (never described, marked so).
Only hub administrators change them (PUT /api/locations/{id}/ai); the Site's customer admins read them. Every change is
audited in the customer's log.

Push: config(loc) is what each server gets: the mode and option with the Site's SOC windows, holidays, override and
timezone, so the server evaluates it itself while the hub is unreachable; `key` (a hash of it) is what the server
reports back in its heartbeat summary (`ai_schedule`). `welcome` carries it; a change of the AI schedule, the Site's
monitoring or arming, or its timezone goes to every connected server of the Site at once ({"t": "ai_schedule"}); a server
whose heartbeat reports another key gets it again, at most once per REPUSH_S (offline at the change, moved here from
another Site, restored from a backup). A server whose summary has no `ai_schedule` is too old for it: it runs full time.

Usage: each server sends {"t": "ai_usage", "rows": [{day (UTC), kind, sched (in | out of the schedule), requests,
prompt_tokens, completion_tokens, seconds}]} for its last 14 days every 10 minutes. The hub keeps one ai_usage_daily row
per server, day, kind and sched, replaced by every report (the server sends totals, so a repeat changes nothing), for
KEEP_DAYS (13 months and a bit). A Site's totals are the sums over its servers' rows (the Site recorded with each row, so
a server moved to another Site leaves its past use where it was).
"""
from __future__ import annotations

import asyncio
import csv
import datetime as dt
import hashlib
import io
import json
import logging
import re
import time

import sqlalchemy as sa

from . import db, soc

log = logging.getLogger("hub.ai_schedule")

MODES = ("full", "soc")
OUTSIDE = ("catchup", "skip")
REPUSH_S = 120.0
KEEP_DAYS = 400              # ai_usage_daily rows older than this many days are deleted (13 months is ~396)
PRUNE_EVERY_S = 3600.0
MAX_ROWS = 2000              # rows taken from one report (14 days x a dozen kinds x in/out is ~340)
KIND_RX = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
DAY_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RX = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
# the job kinds the servers report (nvr/aischedule.py KINDS), in the order the UI lists them; others come after
KIND_ORDER = ("synopsis", "catchup", "ppe", "ask", "talkdown", "briefing", "journey", "hub", "test")

_last: dict[str, tuple[str, float]] = {}   # server id -> (key pushed, when)
_tasks: set[asyncio.Task] = set()
_pruned_at = 0.0


# ---------------------------------------------------------------- the schedule

def mode_of(loc: dict | None) -> str:
    return (loc or {}).get("ai_mode") if (loc or {}).get("ai_mode") in MODES else "full"


def outside_of(loc: dict | None) -> str:
    return (loc or {}).get("ai_outside") if (loc or {}).get("ai_outside") in OUTSIDE else "catchup"


def key(cfg: dict) -> str:
    """A hash of what the server evaluates (backend aischedule.key computes the same)."""
    body = {k: cfg.get(k) for k in ("mode", "outside", "timezone", "monitored", "arm_schedule", "arm_holidays")}
    o = cfg.get("arm_override")
    body["arm_override"] = {"mode": o.get("mode"), "until": o.get("until")} if isinstance(o, dict) else None
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def config(loc: dict | None) -> dict:
    """What a server of this Site follows (welcome, "ai_schedule" frames). No Site: full time."""
    loc = loc or {}
    o = loc.get("arm_override")
    cfg = {"mode": mode_of(loc), "outside": outside_of(loc), "timezone": loc.get("timezone"),
           "monitored": bool(loc.get("monitored")),
           "arm_schedule": [w for w in loc.get("arm_schedule") or [] if isinstance(w, dict)],
           "arm_holidays": [h for h in loc.get("arm_holidays") or [] if isinstance(h, dict)],
           "arm_override": {"mode": o.get("mode"), "until": o.get("until")} if isinstance(o, dict) else None}
    return {**cfg, "key": key(cfg)}


def effective(loc: dict, now: float | None = None) -> dict:
    """{mode, outside, qwen: on | limited, reason (soc.compute_armed's, or full_time), until: the next change or None}."""
    now = time.time() if now is None else now
    mode, outside = mode_of(loc), outside_of(loc)
    if mode != "soc":
        return {"mode": "full", "outside": outside, "qwen": "on", "reason": "full_time", "until": None}
    armed, reason = soc.compute_armed(loc, now)
    nxt = soc.next_change(loc, now)
    return {"mode": "soc", "outside": outside, "qwen": "on" if armed else "limited", "reason": reason,
            "until": nxt["at"] if nxt else None}


def put(loc: dict, mode: str, outside: str | None) -> dict:
    """Store the Site's mode and option (outside None: unchanged); returns the Site row as stored."""
    vals = {"ai_mode": None if mode == "full" else mode}
    if outside is not None:
        vals["ai_outside"] = None if outside == "catchup" else outside
    db.run(sa.update(db.locations).where(db.locations.c.id == loc["id"]).values(**vals, updated_at=time.time()))
    return db.one(sa.select(db.locations).where(db.locations.c.id == loc["id"]))


def reported(summary: dict | None) -> dict | None:
    s = (summary or {}).get("ai_schedule")
    return s if isinstance(s, dict) else None


def servers_status(loc: dict, connected=None) -> list[dict]:
    """Per server of the Site (by name): online, `supported` (reports an AI schedule: version new enough), `in_sync`
    (follows this Site's), and its own state (qwen on | limited, until) as last reported."""
    want = config(loc)["key"]
    t = db.sites.c
    rows = db.rows(sa.select(t.id, t.name, t.summary, t.online, t.retired_at).where(t.location_id == loc["id"]))
    out = []
    for s in sorted((r for r in rows if not r.get("retired_at")), key=lambda r: (r["name"].casefold(), r["id"])):
        rep = reported(s.get("summary"))
        out.append({"server_id": s["id"], "server_name": s["name"],
                    "online": bool(connected(s["id"])) if connected else bool(s.get("online")),
                    "supported": rep is not None, "in_sync": bool(rep and rep.get("key") == want),
                    "qwen": rep.get("qwen") if rep else None, "until": rep.get("until") if rep else None})
    return out


def _conns(location_id: str, registry) -> list:
    return [c for c in list(registry.by_site.values())
            if c.site and c.site.get("location_id") == location_id and not c.site.get("retired_at")]


async def push_one(conn, loc: dict | None = None) -> bool:
    cfg = config(loc if loc is not None else conn.location)
    _last[conn.site_id] = (cfg["key"], time.time())
    try:
        await conn.send({"t": "ai_schedule", "ai_schedule": cfg})
        return True
    except Exception as e:  # noqa: BLE001 - offline, tunnel trouble: the next heartbeat tries again
        log.warning("AI schedule to %s failed: %s", conn.site_id, e)
        return False


async def push_location(location_id: str, registry) -> list[str]:
    """The Site's schedule to every connected server of it (also those too old to report one: they ignore the frame).
    Returns the server ids it went to."""
    loc = db.one(sa.select(db.locations).where(db.locations.c.id == location_id))
    if not loc:
        return []
    conns = _conns(location_id, registry)
    ok = await asyncio.gather(*(push_one(c, loc) for c in conns)) if conns else []
    return [c.site_id for c, good in zip(conns, ok) if good]


def push_soon(location_id: str, registry) -> None:
    """push_location in the background, from code that isn't async (soc_api._changed) or must not wait for it."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(push_location(location_id, registry))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def needs_push(conn, now: float | None = None) -> bool:
    """The server's heartbeat reports another schedule than its Site's, and the same one wasn't just pushed."""
    rep = reported(conn.summary)
    if rep is None or not conn.site_id:
        return False
    want = config(conn.location)["key"]
    if rep.get("key") == want:
        return False
    last = _last.get(conn.site_id)
    now = time.time() if now is None else now
    return not (last and last[0] == want and now - last[1] < REPUSH_S)


def on_heartbeat(conn) -> None:
    """agents.py, on each heartbeat: re-push the Site's schedule when the server follows another one."""
    if not needs_push(conn):
        return
    task = asyncio.get_running_loop().create_task(push_one(conn))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# ---------------------------------------------------------------- usage

def utc_today(now: float | None = None) -> dt.date:
    return dt.datetime.fromtimestamp(time.time() if now is None else now, dt.timezone.utc).date()


def _num(v, cap: float) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v < 0:
        return None
    return min(float(v), cap)


def clean_rows(rows, now: float | None = None) -> list[dict]:
    """A server's report as rows to store: known shapes only, days from KEEP_DAYS ago to tomorrow (clock skew)."""
    if not isinstance(rows, list):
        return []
    today = utc_today(now)
    lo, hi = (today - dt.timedelta(days=KEEP_DAYS)).isoformat(), (today + dt.timedelta(days=1)).isoformat()
    out: dict[tuple, dict] = {}
    for r in rows[:MAX_ROWS]:
        if not isinstance(r, dict):
            continue
        day, kind, sched = r.get("day"), r.get("kind"), r.get("sched")
        if not (isinstance(day, str) and DAY_RX.match(day) and lo <= day <= hi):
            continue
        if not (isinstance(kind, str) and KIND_RX.match(kind)) or sched not in ("in", "out"):
            continue
        req, pt, ct, sec = (_num(r.get("requests"), 1e9), _num(r.get("prompt_tokens"), 1e13),
                            _num(r.get("completion_tokens"), 1e13), _num(r.get("seconds"), 86400.0 * 64))
        if req is None or sec is None:
            continue
        out[(day, kind, sched)] = {"day": day, "kind": kind, "sched": sched, "requests": int(req),
                                   "prompt_tokens": int(pt or 0), "completion_tokens": int(ct or 0), "seconds": round(sec, 2)}
    return list(out.values())


def store(server: dict, rows: list[dict], now: float | None = None) -> int:
    """Replace this server's rows for the reported days, kinds and in/out (totals, so a repeat changes nothing)."""
    now = time.time() if now is None else now
    t = db.ai_usage_daily
    n = 0
    with db.engine().begin() as c:
        for r in rows:
            where = sa.and_(t.c.server_id == server["id"], t.c.day == r["day"], t.c.kind == r["kind"], t.c.sched == r["sched"])
            vals = {"requests": r["requests"], "prompt_tokens": r["prompt_tokens"], "completion_tokens": r["completion_tokens"],
                    "seconds": r["seconds"], "updated_at": now}
            if c.execute(sa.update(t).where(where).values(**vals)).rowcount == 0:
                c.execute(t.insert().values(server_id=server["id"], day=r["day"], kind=r["kind"], sched=r["sched"],
                                            location_id=server.get("location_id"), org_id=server["org_id"], **vals))
            n += 1
    prune(now)
    return n


def prune(now: float | None = None, force: bool = False) -> int:
    """Delete rows older than KEEP_DAYS (at most once an hour)."""
    global _pruned_at
    now = time.time() if now is None else now
    if not force and now - _pruned_at < PRUNE_EVERY_S:
        return 0
    _pruned_at = now
    cutoff = (utc_today(now) - dt.timedelta(days=KEEP_DAYS)).isoformat()
    with db.engine().begin() as c:
        return c.execute(sa.delete(db.ai_usage_daily).where(db.ai_usage_daily.c.day < cutoff)).rowcount or 0


def on_usage(conn, frame: dict) -> int:
    """agents.py: an "ai_usage" frame from an enrolled server."""
    if not conn.site:
        return 0
    rows = clean_rows(frame.get("rows"))
    return store(conn.site, rows) if rows else 0


def month_range(month: str) -> tuple[str, str]:
    """"2026-10" -> ("2026-10-01", "2026-11-01"): first day, and the first day after."""
    y, m = int(month[:4]), int(month[5:7])
    return f"{y:04d}-{m:02d}-01", (f"{y + 1:04d}-01-01" if m == 12 else f"{y:04d}-{m + 1:02d}-01")


def month_of(now: float | None = None, back: int = 0) -> str:
    d = utc_today(now).replace(day=1)
    for _ in range(back):
        d = (d - dt.timedelta(days=1)).replace(day=1)
    return d.strftime("%Y-%m")


def _kind_rank(k: str) -> tuple:
    return (KIND_ORDER.index(k), "") if k in KIND_ORDER else (len(KIND_ORDER), k)


ZERO = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0}


def _add(a: dict, r: dict) -> None:
    for k in ZERO:
        a[k] += r[k]


def _rounded(a: dict) -> dict:
    return {**a, "seconds": round(a["seconds"], 1)}


def site_month(location_id: str, month: str) -> dict:
    """One Site's month: totals, in/out of the schedule, and per kind (UI order)."""
    lo, hi = month_range(month)
    t = db.ai_usage_daily
    rows = db.rows(sa.select(t).where(t.c.location_id == location_id, t.c.day >= lo, t.c.day < hi))
    total, sched = dict(ZERO), {"in": dict(ZERO), "out": dict(ZERO)}
    kinds: dict[str, dict] = {}
    for r in rows:
        _add(total, r)
        _add(sched[r["sched"]], r)
        k = kinds.setdefault(r["kind"], {**ZERO, "out_seconds": 0.0})
        _add(k, r)
        if r["sched"] == "out":
            k["out_seconds"] += r["seconds"]
    return {"month": month, "total": _rounded(total), "in": _rounded(sched["in"]), "out": _rounded(sched["out"]),
            "kinds": [{"kind": k, **_rounded(v), "out_seconds": round(v["out_seconds"], 1)}
                      for k, v in sorted(kinds.items(), key=lambda kv: _kind_rank(kv[0]))]}


def billing(month: str) -> list[dict]:
    """GET /api/hub/ai-usage: per-Site totals for one month, every customer (Sites with no use left out), the Site's
    AI schedule now beside them. Ordered by customer, then Site."""
    lo, hi = month_range(month)
    t = db.ai_usage_daily
    rows = db.rows(sa.select(t.c.location_id, t.c.org_id, t.c.kind, t.c.sched, t.c.requests, t.c.prompt_tokens,
                             t.c.completion_tokens, t.c.seconds).where(t.c.day >= lo, t.c.day < hi))
    sites: dict[tuple, dict] = {}
    for r in rows:
        s = sites.setdefault((r["org_id"], r["location_id"]), {**ZERO, "seconds_in": 0.0, "seconds_out": 0.0, "kinds": {}})
        _add(s, r)
        s["seconds_in" if r["sched"] == "in" else "seconds_out"] += r["seconds"]
        s["kinds"][r["kind"]] = round(s["kinds"].get(r["kind"], 0.0) + r["seconds"], 2)
    if not sites:
        return []
    orgs = {o["id"]: o["name"] for o in db.rows(sa.select(db.orgs.c.id, db.orgs.c.name))}
    locs = {loc["id"]: loc for loc in db.rows(sa.select(db.locations).where(db.locations.c.id.in_([k[1] for k in sites if k[1]])))}
    out = []
    for (org_id, lid), s in sites.items():
        loc = locs.get(lid) or {}
        out.append({"month": month, "org_id": org_id, "org_name": orgs.get(org_id), "location_id": lid,
                    "location_name": loc.get("name"), "ai_mode": mode_of(loc) if loc else None,
                    "ai_outside": outside_of(loc) if loc else None,
                    "requests": s["requests"], "prompt_tokens": s["prompt_tokens"], "completion_tokens": s["completion_tokens"],
                    "seconds": round(s["seconds"], 1), "seconds_in": round(s["seconds_in"], 1), "seconds_out": round(s["seconds_out"], 1),
                    "kinds": dict(sorted(s["kinds"].items(), key=lambda kv: _kind_rank(kv[0])))})
    return sorted(out, key=lambda r: ((r["org_name"] or "").casefold(), (r["location_name"] or "").casefold(), r["location_id"] or ""))


CSV_COLUMNS = ("month", "org_id", "org_name", "location_id", "location_name", "ai_mode", "ai_outside", "requests",
               "prompt_tokens", "completion_tokens", "seconds", "seconds_in", "seconds_out")


def billing_csv(rows: list[dict]) -> str:
    """The billing rows as CSV: the fixed columns, then one `seconds_<kind>` column per kind any Site used."""
    kinds = sorted({k for r in rows for k in r["kinds"]}, key=_kind_rank)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([*CSV_COLUMNS, *(f"seconds_{k}" for k in kinds)])
    for r in rows:
        w.writerow([r[c] if r[c] is not None else "" for c in CSV_COLUMNS] + [r["kinds"].get(k, 0) for k in kinds])
    return buf.getvalue()


def server_days(server_ids: list[str], days: int = 7, now: float | None = None) -> dict[str, dict]:
    """server id -> {days: [{day, seconds, requests}] oldest first (every day, zeros included), today_s, total_s}: the
    Hosts page's Qwen seconds per day per instance."""
    ids = [s for s in server_ids if s]
    if not ids:
        return {}
    today = utc_today(now)
    span = [(today - dt.timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    t = db.ai_usage_daily
    rows = db.rows(sa.select(t.c.server_id, t.c.day, t.c.requests, t.c.seconds).where(t.c.server_id.in_(ids), t.c.day >= span[0]))
    acc: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        d = acc.setdefault(r["server_id"], {}).setdefault(r["day"], [0.0, 0])
        d[0] += r["seconds"]
        d[1] += r["requests"]
    out = {}
    for sid in ids:
        mine = acc.get(sid, {})
        series = [{"day": d, "seconds": round(mine.get(d, [0.0, 0])[0], 1), "requests": int(mine.get(d, [0.0, 0])[1])} for d in span]
        out[sid] = {"days": series, "today_s": series[-1]["seconds"], "total_s": round(sum(x["seconds"] for x in series), 1)}
    return out


def sum_days(parts: list[dict]) -> dict | None:
    """The per-day series of several servers added up (a host's instances), or None when there are none."""
    parts = [p for p in parts if p]
    if not parts:
        return None
    days = [{"day": d["day"], "seconds": 0.0, "requests": 0} for d in parts[0]["days"]]
    for p in parts:
        for acc, d in zip(days, p["days"]):
            acc["seconds"] += d["seconds"]
            acc["requests"] += d["requests"]
    for d in days:
        d["seconds"] = round(d["seconds"], 1)
    return {"days": days, "today_s": days[-1]["seconds"] if days else 0.0, "total_s": round(sum(d["seconds"] for d in days), 1)}
