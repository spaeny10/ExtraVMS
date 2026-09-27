"""Fleet-wide Find and Ask: the same question to every online site the user may see, answers merged.

Find fans `GET /api/search` and `/api/footage/search` out through the tunnels and merges the results with a
site tag. Ask streams every site's own assistant answer (each site uses its own model and its own data) as
one NDJSON stream with a `site` field on every chunk, so the page can show them side by side.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from urllib.parse import urlencode

from . import auth
from .agents import AgentConn, registry

log = logging.getLogger("hub.fleet")
FANOUT_TIMEOUT_S = 12.0


def online_sites(u: dict, org_id: str) -> list[tuple[dict, AgentConn]]:
    out = []
    for s in auth.visible_sites(u, org_id):
        conn = registry.get(s["id"])
        if conn is not None:
            out.append((s, conn))
    return out


async def _get_json(conn: AgentConn, path: str, params: dict, headers: dict) -> tuple[int, object]:
    status, body = await conn.call("GET", path, urlencode({k: v for k, v in params.items() if v not in (None, "")}), headers, None, FANOUT_TIMEOUT_S)
    try:
        return status, json.loads(body.decode() or "null")
    except ValueError:
        return status, None


async def search(u: dict, org_id: str, q: str, since: float | None, until: float | None, limit: int = 30) -> dict:
    headers = {"x-hub-user": u["email"], "x-hub-role": "viewer"}
    sites = online_sites(u, org_id)

    async def one(site, conn):
        ev, fo = await asyncio.gather(
            _get_json(conn, "/api/search", {"q": q, "since": since, "until": until, "limit": limit}, headers),
            _get_json(conn, "/api/footage/search", {"q": q, "since": since, "until": until, "limit": 10}, headers),
            return_exceptions=True)
        events = ev[1] if isinstance(ev, tuple) and ev[0] == 200 and isinstance(ev[1], list) else []
        footage = fo[1] if isinstance(fo, tuple) and fo[0] == 200 and isinstance(fo[1], list) else []
        err = None
        if isinstance(ev, Exception) or (isinstance(ev, tuple) and ev[0] != 200):
            err = str(ev) if isinstance(ev, Exception) else f"HTTP {ev[0]}"
        return {"site_id": site["id"], "site_name": site["name"], "events": events, "footage": footage, "error": err}

    results = await asyncio.gather(*(one(s, c) for s, c in sites))
    events = sorted((dict(e, site_id=r["site_id"], site_name=r["site_name"]) for r in results for e in r["events"]),
                    key=lambda e: -(e.get("score") or 0) if "score" in e else -(e.get("start_ts") or 0))
    footage = sorted((dict(m, site_id=r["site_id"], site_name=r["site_name"]) for r in results for m in r["footage"]),
                     key=lambda m: -(m.get("score") or 0))
    return {"q": q, "sites": [{k: r[k] for k in ("site_id", "site_name", "error")} | {"events": len(r["events"]), "footage": len(r["footage"])} for r in results],
            "offline": [s["name"] for s in auth.visible_sites(u, org_id) if registry.get(s["id"]) is None],
            "events": events[:limit * 2], "footage": footage[:30]}


async def ask(u: dict, org_id: str, message: str):
    """Yield NDJSON lines: {"site": id, "site_name": name, ...chunk} from every site's assistant, interleaved."""
    headers = {"x-hub-user": u["email"], "x-hub-role": "viewer", "content-type": "application/json"}
    sites = online_sites(u, org_id)
    queue: asyncio.Queue = asyncio.Queue()

    async def one(site, conn):
        tag = {"site": site["id"], "site_name": site["name"]}
        try:
            s = await conn.request("POST", "/api/assistant/ask", "", headers, json.dumps({"message": message}).encode())
            try:
                await asyncio.wait_for(s.head.wait(), FANOUT_TIMEOUT_S)
                if s.status != 200:
                    body = await s.read_all()
                    await queue.put({**tag, "type": "error", "error": body.decode()[:300] or f"HTTP {s.status}"})
                    return
                buf = b""
                while True:
                    c = await asyncio.wait_for(s.read(), 240)
                    if c is None:
                        break
                    await conn.send({"t": "credit", "id": s.id, "bytes": len(c)})
                    buf += c
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if line.strip():
                            try:
                                await queue.put({**tag, **json.loads(line)})
                            except ValueError:
                                pass
            finally:
                conn.finish(s)
        except Exception as e:  # one slow or broken site must not spoil the others
            await queue.put({**tag, "type": "error", "error": str(e)[:200]})
        finally:
            await queue.put({**tag, "type": "site_done"})

    tasks = [asyncio.create_task(one(s, c)) for s, c in sites]
    yield json.dumps({"type": "sites", "sites": [{"site": s["id"], "site_name": s["name"]} for s, _ in sites],
                      "offline": [s["name"] for s in auth.visible_sites(u, org_id) if registry.get(s["id"]) is None]}) + "\n"
    done = 0
    while done < len(tasks):
        item = await queue.get()
        if item.get("type") == "site_done":
            done += 1
        yield json.dumps(item) + "\n"
    yield json.dumps({"type": "done", "at": time.time()}) + "\n"
