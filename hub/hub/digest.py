"""Organisation digest: one morning note per org built from every site's own briefing and open alerts.
With the shared model configured it is summarised by vLLM; without it the site headlines are listed.
Servers are grouped by Site (location): a Site with one server reads exactly as before (one line, named after the
server); a Site with several gets a header line with its servers under it."""
from __future__ import annotations

import datetime as dt
import json
import logging
import time

import sqlalchemy as sa

from . import alerts, db, vlm_proxy
from .agents import registry
from .config import settings

log = logging.getLogger("hub.digest")


async def collect(org_id: str) -> dict:
    sites = db.rows(sa.select(db.sites).where(db.sites.c.org_id == org_id, db.sites.c.retired_at.is_(None)).order_by(db.sites.c.name))
    loc_names = {r["id"]: r["name"] for r in db.rows(sa.select(db.locations.c.id, db.locations.c.name).where(db.locations.c.org_id == org_id))}
    parts = []
    for s in sites:
        conn = registry.get(s["id"])
        briefing = None
        if conn is not None:
            try:
                status, body = await conn.call("GET", "/api/briefings", "limit=1", {"x-hub-user": "hub-digest", "x-hub-role": "viewer"}, None, 15)
                if status == 200:
                    bs = (json.loads(body.decode()).get("briefings") or [])
                    briefing = bs[0] if bs else None
            except Exception as e:
                log.warning("digest: %s briefing failed: %s", s["name"], e)
        summ = s.get("summary") or {}
        parts.append({"site_id": s["id"], "site_name": s["name"], "location_id": s.get("location_id"),
                      "location_name": loc_names.get(s.get("location_id")), "online": s["id"] in registry.by_site,
                      "headline": (briefing or {}).get("headline"), "text": (briefing or {}).get("text"),
                      "today": summ.get("today") or {}, "cameras_down": [c["name"] for c in summ.get("cameras") or [] if not c.get("stream_ready")],
                      "open_alerts": [{"kind": a["kind"], "detail": a["detail"]} for a in alerts.open_for_org(org_id, [s["id"]], 20)]})
    return {"generated_at": time.time(), "sites": parts, "locations": group(parts)}


def group(parts: list[dict]) -> list[dict]:
    """[{id, name, servers: [server ids]}] by Site name; a server without a Site is a group of its own (id None)."""
    groups: dict[str, dict] = {}
    for p in parts:
        key = p.get("location_id") or f"server:{p['site_id']}"
        g = groups.setdefault(key, {"id": p.get("location_id"), "name": p.get("location_name") or p["site_name"], "servers": []})
        g["servers"].append(p["site_id"])
    return sorted(groups.values(), key=lambda g: g["name"].casefold())


def _grouped(parts: list[dict]) -> list[tuple[dict, list[dict]]]:
    by_id = {p["site_id"]: p for p in parts}
    return [(g, [by_id[sid] for sid in g["servers"]]) for g in group(parts)]


def _plain_line(p: dict) -> str:
    status = "offline" if not p["online"] else ", ".join(f"{v} {k}" for k, v in p["today"].items()) or "quiet"
    line = f"{p['site_name']} — {status}"
    if p["cameras_down"]:
        line += f"; cameras down: {', '.join(p['cameras_down'])}"
    if p["open_alerts"]:
        line += f"; {len(p['open_alerts'])} open alert(s)"
    if p["headline"]:
        line += f". {p['headline']}"
    return line


def _plain(parts: list[dict]) -> str:
    lines = []
    for g, servers in _grouped(parts):
        if len(servers) == 1:   # one-server Site: the line it has always been
            lines.append(f"• {_plain_line(servers[0])}")
            continue
        lines.append(f"{g['name']} ({len(servers)} servers)")
        lines += [f"  • {_plain_line(p)}" for p in servers]
    return "\n".join(lines) or "No sites."


def _facts(parts: list[dict]) -> str:
    def block(p: dict, label: str) -> str:
        return (f"{label}: {p['site_name']} ({'online' if p['online'] else 'OFFLINE'})\nToday: {p['today']}\nCameras down: {p['cameras_down']}\n"
                f"Open alerts: {p['open_alerts']}\nSite briefing: {p['text'] or p['headline'] or 'none'}")
    blocks = []
    for g, servers in _grouped(parts):
        if len(servers) == 1:
            blocks.append(block(servers[0], "Site"))   # as before: a one-server Site goes by its server's name
        else:
            blocks.append(f"Site: {g['name']} ({len(servers)} servers)\n" + "\n".join(block(p, "Server") for p in servers))
    return "\n\n".join(blocks)


async def _summarise(org_name: str, parts: list[dict]) -> str | None:
    if not vlm_proxy.configured():
        return None
    facts = _facts(parts)
    messages = [
        {"role": "system", "content": "You write the morning digest for a security operator who runs several sites. Be factual and brief: "
                                      "5-8 bullets, the important things first (offline sites, cameras down, site-rule breaks, unusual events), "
                                      "then a one-line note per quiet site. Use the site names given; a site listed with several servers is one place, "
                                      "so name a server as 'Site · Server'. No preamble."},
        {"role": "user", "content": f"Organisation: {org_name}\n\n{facts}"}]
    try:
        return await vlm_proxy.complete(messages, max_tokens=500, temperature=0.2)   # direct vLLM or a site's tunnel
    except Exception as e:
        log.warning("digest: shared AI failed: %s", e)
    return None


async def generate(org_id: str) -> dict:
    org = db.one(sa.select(db.orgs).where(db.orgs.c.id == org_id))
    data = await collect(org_id)
    text = await _summarise(org["name"] if org else org_id, data["sites"]) or _plain(data["sites"])
    row = {"org_id": org_id, "day": dt.date.today().isoformat(), "created_at": time.time(), "text": text, "data": data,
           "model": settings.vllm_model if vlm_proxy.configured() else None}
    db.insert(db.digests, row)
    return db.one(sa.select(db.digests).where(db.digests.c.org_id == org_id).order_by(db.digests.c.id.desc()))


def latest(org_id: str, limit: int = 7) -> list[dict]:
    return db.rows(sa.select(db.digests).where(db.digests.c.org_id == org_id).order_by(db.digests.c.id.desc()).limit(limit))


async def daily_loop() -> None:
    """Every org gets a digest at settings.digest_hour (hub local time), once per day."""
    while True:
        now = dt.datetime.now()
        target = now.replace(hour=settings.digest_hour, minute=30, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        await _sleep((target - now).total_seconds())
        for org in db.rows(sa.select(db.orgs)):
            if not db.one(sa.select(db.digests).where(db.digests.c.org_id == org["id"], db.digests.c.day == dt.date.today().isoformat())):
                try:
                    await generate(org["id"])
                except Exception:
                    log.exception("digest for %s failed", org["name"])


async def _sleep(s: float) -> None:
    import asyncio
    await asyncio.sleep(max(1.0, s))


def scoped(row: dict, see) -> dict:
    """A digest cut down for a Site-restricted member (`see`: auth.scope_filter's predicate): only the servers they
    can see, and in place of the whole customer's text (or its AI summary) the plain lines for those servers."""
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    parts = [p for p in data.get("sites") or [] if see({"site_id": p.get("site_id")})]
    text = _plain(parts) if data.get("sites") is not None else "This digest covers Sites you don't have access to."
    return {**row, "text": text, "model": None, "scoped": True,
            "data": {**data, "sites": parts, "locations": group(parts)} if data else None}
