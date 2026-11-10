"""Shared export links: a public, expiring link to a copy of an export, for the police, an insurer or a lawyer.

POST /api/shares (operator) makes a link to a finished export: the hub's own (a Site export across servers, exports.py)
or one server's (fetched through the tunnel). The files are copied under <exports_dir>/shares/<id>/, so the link keeps
working when the server is offline and after the export itself is cleaned up. The link is
<public_url>/x/<token>; only the token's sha256 is stored. It expires after 1, 7 (default) or 30 days, or when revoked,
and its files are deleted then (the row stays, for the record). An optional passcode (argon2) guards it; wrong passcodes
are rate-limited. Every view and download is logged (share_access) and shown with the link under Site › Settings.

The public pages are plain HTML from here: no script, a strict CSP, noindex, no referrer.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import shutil
import time
import urllib.parse
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import auth, db, exports
from .agents import registry
from .config import settings
from .roles import allows

log = logging.getLogger("hub.shares")
router = APIRouter()

DAYS = (1, 7, 30)
PAGE_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; media-src 'self'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'")
FILE_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; media-src 'self'; sandbox"
TRIES, TRY_WINDOW_S = 5, 600          # wrong passcodes per link and address before a pause
EVENTS_FILE = "events.json"           # in the link's folder: the export's events (exports' `events`), shown on the page
EVENTS_SHOWN = 50
_tries: dict[tuple[str, str], list[float]] = {}
_tasks: set[asyncio.Task] = set()


def root() -> Path:
    return Path(settings.exports_dir) / "shares"


def link_dir(link_id: str) -> Path:
    if not re.fullmatch(r"sh_[a-z0-9]{12}", link_id):
        raise HTTPException(404)
    return root() / link_id


def _public(row: dict) -> dict:
    now = time.time()
    status = ("revoked" if row.get("revoked_at") else "expired" if row["expires_at"] <= now else row["state"])
    return {k: row.get(k) for k in ("id", "created_email", "created_at", "expires_at", "revoked_at", "title", "note", "range_start",
                                    "range_end", "state", "error", "zip_name", "views", "downloads")} | {
        "status": status, "passcode": bool(row.get("passcode_hash")), "files": len(row.get("files") or [])}


def _audit(u: dict, row: dict, action: str, detail: dict | None = None) -> None:
    db.insert(db.audit_log, {"ts": time.time(), "user_id": u["id"], "user_email": u.get("email"), "org_id": row["org_id"],
                             "site_id": None, "action": action, "method": None, "path": None, "status": None, "ip": None,
                             "detail": {"link": row["id"], "title": row["title"], **(detail or {})}})


# ---------------------------------------------------------------- making a link

class SourceIn(BaseModel):
    kind: str = Field(pattern="^(hub|server)$")
    id: str = Field(pattern="^[a-z0-9]{12}$")
    server: str | None = Field(None, max_length=32)


class ShareIn(BaseModel):
    source: SourceIn
    days: int = 7
    passcode: str | None = Field(None, min_length=4, max_length=64)
    note: str = Field("", max_length=300)


def user(request: Request) -> dict:
    return auth.require_user(request)


@router.post("/api/shares")
async def create(body: ShareIn, u: dict = Depends(user)):
    if body.days not in DAYS:
        raise HTTPException(422, f"a link lasts {', '.join(map(str, DAYS))} days")
    src = body.source
    if src.kind == "hub":
        job = exports._mine(src.id, u)
        if job.state != "done":
            raise HTTPException(409, "the export isn't finished")
        loc, role = auth.location_access(u, job.location_id)
        title, start, end = f"{job.site} · {len(job.files)} files", job.start, job.end
    else:
        if not src.server:
            raise HTTPException(422, "which server")
        server, role = auth.site_access(u, src.server)
        loc = db.one(sa.select(db.locations).where(db.locations.c.id == server.get("location_id")))
        if not loc:
            raise HTTPException(422, "the server isn't in a Site")
        title, start, end = f"{loc.get('name') or loc['id']} · {server.get('name') or src.server}", None, None
    if not allows(role, "operator"):
        raise HTTPException(403, "needs the operator role")
    token = secrets.token_urlsafe(24)
    row = {"id": "sh_" + secrets.token_hex(6), "token_hash": db.token_hash(token), "org_id": loc["org_id"], "location_id": loc["id"],
           "created_by": u["id"], "created_email": u.get("email"), "created_at": time.time(),
           "expires_at": time.time() + body.days * 86400, "revoked_at": None, "purged_at": None,
           "passcode_hash": auth.hash_password(body.passcode) if body.passcode else None, "title": title[:200],
           "note": body.note or None, "range_start": start, "range_end": end, "source": src.model_dump(), "state": "copying",
           "error": None, "files": [], "zip_name": None, "views": 0, "downloads": 0}
    db.insert(db.share_links, row)
    t = asyncio.get_running_loop().create_task(_copy(row, u, role), name=f"share-{row['id']}")
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    _audit(u, row, "share link created", {"days": body.days, "passcode": bool(body.passcode)})
    log.info("share %s by %s: %s, %d days", row["id"], u.get("email"), title, body.days)
    return {**_public(row), "url": f"{settings.public_url.rstrip('/')}/x/{token}"}


def _set(link_id: str, **values) -> None:
    db.run(sa.update(db.share_links).where(db.share_links.c.id == link_id).values(**values))


async def _copy(row: dict, u: dict, role: str) -> None:
    """The export's files into the link's own folder."""
    d = link_dir(row["id"])
    d.mkdir(parents=True, exist_ok=True)
    src = row["source"]
    try:
        if src["kind"] == "hub":
            job = exports._get(src["id"])
            names = [f["name"] for f in job.files] + ["checksums.txt", job.zip_name]
            for n in names:
                p = exports.job_dir(job.id) / n
                try:
                    os.link(p, d / n)       # same disk: no second copy
                except OSError:
                    await asyncio.to_thread(shutil.copyfile, p, d / n)
            files, zip_name = job.files, job.zip_name
            rng = (job.start, job.end)
            events = job.events
        else:
            conn = registry.get(src["server"])
            if conn is None:
                raise RuntimeError("the server is offline")
            part = {"server_name": (conn.site or {}).get("name") or src["server"], "role": role}
            by = u.get("email") or u["id"]
            st = await exports._call_json(conn, "GET", f"/api/exports/{src['id']}", part, by)
            if st.get("state") != "done":
                raise RuntimeError("the export isn't finished")
            files = [f for f in st["files"] if f["kind"] != "tile"]
            zip_name = st["zip_name"]
            for n in [f["name"] for f in files] + ["checksums.txt"]:
                await exports._fetch(conn, f"/api/exports/{src['id']}/files/{n}", d / n, part, by)
            await exports._fetch(conn, f"/api/exports/{src['id']}/download", d / zip_name, part, by)
            rng = (st.get("start"), st.get("end"))
            events = st.get("events") or []
        # the events with Otso's descriptions, for the page (not a file of the link: not listed, not downloadable)
        (d / EVENTS_FILE).write_text(json.dumps(events), encoding="utf-8")
        _set(row["id"], state="ready", files=[{k: f.get(k) for k in ("name", "bytes", "sha256", "kind")} for f in files],
             zip_name=zip_name, range_start=rng[0], range_end=rng[1])
    except Exception as e:  # noqa: BLE001 - shown with the link
        log.exception("share %s: copying failed", row["id"])
        _set(row["id"], state="failed", error=str(e)[:300])
        shutil.rmtree(d, ignore_errors=True)


def _row_for(link_id: str, u: dict, need: str = "operator") -> dict:
    row = db.one(sa.select(db.share_links).where(db.share_links.c.id == link_id))
    if not row:
        raise HTTPException(404, "no such link")
    _, role = auth.location_access(u, row["location_id"])
    if not allows(role, need):
        raise HTTPException(403, f"needs the {need} role")
    return row


@router.get("/api/locations/{location_id}/shares")
async def list_links(location_id: str, u: dict = Depends(user)):
    _, role = auth.location_access(u, location_id)
    if not allows(role, "operator"):
        raise HTTPException(403, "needs the operator role")
    rows = db.rows(sa.select(db.share_links).where(db.share_links.c.location_id == location_id)
                   .order_by(db.share_links.c.created_at.desc()).limit(200))
    return [_public(r) for r in rows]


@router.get("/api/shares/{link_id}")
async def get_link(link_id: str, u: dict = Depends(user)):
    return _public(_row_for(link_id, u))


@router.delete("/api/shares/{link_id}")
async def revoke(link_id: str, u: dict = Depends(user)):
    row = _row_for(link_id, u)
    if not row.get("revoked_at"):
        _set(link_id, revoked_at=time.time(), purged_at=time.time())
        shutil.rmtree(link_dir(link_id), ignore_errors=True)
        _audit(u, row, "share link revoked")
    return _public(db.one(sa.select(db.share_links).where(db.share_links.c.id == link_id)))


@router.get("/api/shares/{link_id}/access")
async def access_log(link_id: str, u: dict = Depends(user)):
    _row_for(link_id, u)
    return db.rows(sa.select(db.share_access).where(db.share_access.c.link_id == link_id)
                   .order_by(db.share_access.c.ts.desc()).limit(500))


def sweep() -> None:
    """Expired links lose their files (the hub's sweeper, every 30 s; one query when nothing is due)."""
    due = db.rows(sa.select(db.share_links.c.id).where(db.share_links.c.expires_at <= time.time(), db.share_links.c.purged_at.is_(None)))
    for r in due:
        shutil.rmtree(link_dir(r["id"]), ignore_errors=True)
        _set(r["id"], purged_at=time.time())


# ---------------------------------------------------------------- the public side: /x/<token>

def _live(token: str) -> dict | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", token):
        return None
    row = db.one(sa.select(db.share_links).where(db.share_links.c.token_hash == db.token_hash(token)))
    if not row or row.get("revoked_at") or row["expires_at"] <= time.time():
        return None
    return row


def _cookie_name(row: dict) -> str:
    return f"xs_{row['id'][3:]}"


def _cookie_value(row: dict) -> str:
    return hmac.new(settings.secret.encode(), f"{row['id']}|{row['expires_at']}".encode(), hashlib.sha256).hexdigest()


def _unlocked(request: Request, row: dict) -> bool:
    return not row.get("passcode_hash") or hmac.compare_digest(request.cookies.get(_cookie_name(row), ""), _cookie_value(row))


def _log(request: Request, row: dict, action: str, name: str | None = None) -> None:
    db.insert(db.share_access, {"link_id": row["id"], "ts": time.time(), "ip": request.client.host if request.client else None,
                                "user_agent": (request.headers.get("user-agent") or "")[:300], "action": action, "name": name})
    if action in ("view", "download"):
        col = db.share_links.c.views if action == "view" else db.share_links.c.downloads
        db.run(sa.update(db.share_links).where(db.share_links.c.id == row["id"]).values({col: col + 1}))


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{html.escape(title)}</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;margin:0;background:#0f1115;color:#e7e9ee}}main{{max-width:980px;margin:0 auto;padding:24px 16px}}
h1{{font-size:20px;margin:0 0 4px}}.muted{{color:#9aa3b2}}.card{{background:#171a21;border:1px solid #2a2f3a;border-radius:12px;padding:14px;margin:14px 0}}
video{{width:100%;max-height:70vh;background:#000;border-radius:8px}}a.btn,button{{display:inline-block;background:#3b82f6;color:#fff;border:0;
border-radius:8px;padding:8px 14px;text-decoration:none;font-weight:600;font-size:14px;cursor:pointer}}a.sub{{color:#93b4f8}}
input{{font:inherit;padding:8px;border-radius:8px;border:1px solid #2a2f3a;background:#0f1115;color:inherit}}
.row{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}h2{{font-size:16px;margin:0 0 4px}}.small{{font-size:13px}}
.ev{{border-top:1px solid #2a2f3a;padding:10px 0 2px}}.ev p{{margin:4px 0}}.ev.focus{{border-left:3px solid #3b82f6;padding-left:10px}}
.pri{{font-size:12px;border:1px solid #2a2f3a;border-radius:99px;padding:0 8px}}.pri.high,.pri.critical{{border-color:#ef4444;color:#fca5a5}}
.pri.medium{{border-color:#f59e0b;color:#fcd34d}}.warn{{color:#fca5a5}}.mono{{font-family:ui-monospace,monospace;font-size:11px;word-break:break-all}}</style>
</head><body><main>{body}</main></body></html>"""
    return HTMLResponse(doc, status_code=status, headers={"content-security-policy": PAGE_CSP, "x-robots-tag": "noindex, nofollow",
                                                          "referrer-policy": "no-referrer", "cache-control": "no-store"})


def _gone() -> HTMLResponse:
    return _page("Link not available", "<h1>This link isn't available</h1><p class=muted>It has expired, or it was withdrawn. "
                 "Ask whoever sent it for a new one.</p>", 404)


def _t(t: float | None) -> str:
    return dt.datetime.fromtimestamp(t).strftime("%a %b %d %Y, %H:%M:%S") if t else ""


def _mb(b: int | None) -> str:
    return f"{(b or 0) / 1e6:.1f} MB"


@router.get("/x/{token}")
async def public_page(token: str, request: Request):
    row = _live(token)
    if row is None:
        return _gone()
    e = html.escape
    if not _unlocked(request, row):
        return _page("Passcode", f"""<h1>{e(row['title'])}</h1><p class=muted>This video export is protected. Enter the passcode
you were given.</p><form method="post" class="row card"><input type="password" name="passcode" autocomplete="off" autofocus
aria-label="Passcode"><button type="submit">Open</button></form>""")
    if row["state"] == "copying":
        return _page("Being prepared", f"<h1>{e(row['title'])}</h1><p class=muted>The files are being prepared. Reload this page in a minute.</p>")
    if row["state"] == "failed":
        return _page("Not available", f"<h1>{e(row['title'])}</h1><p class=muted>The files couldn't be prepared. Ask whoever sent this for a new link.</p>")
    _log(request, row, "view")
    base = f"/x/{token}/f/"
    videos = "".join(
        f"""<div class=card><div class=row><strong>{e(f['name'])}</strong><span class=muted>{_mb(f.get('bytes'))}</span>
<a class=sub href="{base}{e(f['name'])}?dl=1">Download</a></div>
<video controls preload="metadata" src="{base}{e(f['name'])}"></video></div>"""
        for f in sorted(row.get("files") or [], key=lambda f: (f.get("kind") != "grid", f["name"])) if f["name"].endswith(".mp4"))
    report = next((f["name"] for f in row.get("files") or [] if f["name"] == "report.html"), None)
    sums = "".join(f"<div class=mono>{e(f.get('sha256') or '')}  {e(f['name'])}</div>" for f in row.get("files") or [])
    happened = events_html(_events(row), bool(report))
    body = f"""<h1>{e(row['title'])}</h1>
<p class=muted>{_t(row.get('range_start'))}{' to ' + _t(row.get('range_end')) if row.get('range_end') else ''} · shared by
{e(row.get('created_email') or '')} · this link works until {_t(row['expires_at'])}</p>
{f'<p>{e(row["note"])}</p>' if row.get('note') else ''}
<div class="row card"><a class=btn href="{base}{e(row['zip_name'] or '')}?dl=1">Download all ({e(row['zip_name'] or 'zip')})</a>
{f'<a class=sub href="{base}{e(report)}">Report</a>' if report else ''}</div>
{happened}
<p class=muted>If a video doesn't play here (some cameras record H.265), download it: VLC plays every file.</p>
{videos}
<details class=card><summary>Checksums (SHA-256)</summary>{sums}</details>"""
    return _page(row["title"], body)


def _events(row: dict) -> list[dict]:
    """The link's events (events.json; none for a link made before 2026-11-10 or from a server that doesn't send them)."""
    try:
        evs = json.loads((link_dir(row["id"]) / EVENTS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [ev for ev in evs if isinstance(ev, dict)] if isinstance(evs, list) else []


def events_html(events: list[dict], has_report: bool) -> str:
    """"What happened": each event's camera, what, when and priority, Otso's description and a broken rule; the event
    the export was made from first. Nothing when there are none."""
    if not events:
        return ""
    e = html.escape

    def one(ev: dict) -> str:
        pri = str(ev.get("priority") or "none")
        head = " · ".join(x for x in (str(ev.get("server") or ""), str(ev.get("camera") or ""), str(ev.get("what") or ""),
                                      _t(ev.get("start_ts"))[-8:]) if x)
        return (f"<div class='ev{' focus' if ev.get('focus') else ''}'><div class=row><strong>{e(head)}</strong>"
                + (f"<span class='pri {e(pri)}'>{e(pri)} priority</span>" if pri not in ("none", "") else "")
                + ("<span class=muted>the event this export was made from</span>" if ev.get("focus") else "") + "</div>"
                + (f"<p>{e(str(ev['synopsis']))}</p>" if ev.get("synopsis") else "<p class=muted>No description.</p>")
                + (f"<p class=warn>Rule: {e(str(ev['rule']))}</p>" if ev.get("rule") else "") + "</div>")

    more = len(events) - EVENTS_SHOWN
    tail = f"<p class=muted>{more} more {'in the Report' if has_report else 'not shown'}.</p>" if more > 0 else ""
    n = len(events)
    return (f"<section class=card><h2>What happened <span class=muted>{n} event{'s' if n != 1 else ''}</span></h2>"
            "<p class='muted small'>Descriptions written by Otso, Axiom Vision's AI, from the video. Check them against the footage.</p>"
            + "".join(one(ev) for ev in events[:EVENTS_SHOWN]) + tail + "</section>")


@router.post("/x/{token}")
async def public_passcode(token: str, request: Request):
    # the form body read by hand: FastAPI's Form needs python-multipart, which the hub image doesn't have (declaring a
    # Form parameter made the hub fail at start, 2026-11-03)
    raw = (await request.body())[:2048].decode("utf-8", "replace")
    passcode = (urllib.parse.parse_qs(raw).get("passcode") or [""])[0][:64]
    row = _live(token)
    if row is None or not row.get("passcode_hash"):
        return _gone()
    ip = request.client.host if request.client else "-"
    key = (row["id"], ip)
    now = time.time()
    recent = [t for t in _tries.get(key, []) if now - t < TRY_WINDOW_S]
    if len(recent) >= TRIES:
        return _page("Too many tries", "<h1>Too many tries</h1><p class=muted>Wait ten minutes, then try again.</p>", 429)
    if not await asyncio.to_thread(auth.verify_password, passcode, row["passcode_hash"]):
        _tries[key] = recent + [now]
        _log(request, row, "passcode-failed")
        return _page("Passcode", f"<h1>{html.escape(row['title'])}</h1><p class=muted>That passcode isn't right.</p>"
                     "<form method=post class='row card'><input type=password name=passcode autocomplete=off autofocus aria-label=Passcode>"
                     "<button type=submit>Open</button></form>", 403)
    _tries.pop(key, None)
    r = RedirectResponse(f"/x/{token}", status_code=303)
    r.set_cookie(_cookie_name(row), _cookie_value(row), max_age=max(1, int(row["expires_at"] - now)), path=f"/x/{token}",
                 httponly=True, secure=settings.cookie_secure, samesite="lax")
    return r


@router.get("/x/{token}/f/{name}")
async def public_file(token: str, name: str, request: Request, dl: int = 0):
    row = _live(token)
    if row is None or row["state"] != "ready" or not _unlocked(request, row):
        raise HTTPException(404)
    allowed = {f["name"] for f in row.get("files") or []} | {"checksums.txt", row.get("zip_name")}
    if name not in allowed:
        raise HTTPException(404)
    p = link_dir(row["id"]) / name
    if not p.is_file():
        raise HTTPException(404)
    rng = request.headers.get("range", "")
    if dl or name == row.get("zip_name"):
        if not rng or rng.startswith("bytes=0-"):
            _log(request, row, "download", name)
    headers = {"x-robots-tag": "noindex, nofollow", "referrer-policy": "no-referrer", "cache-control": "private, no-store"}
    if name.endswith(".html"):
        headers["content-security-policy"] = FILE_CSP
    return FileResponse(p, filename=name if dl else None, headers=headers,
                        content_disposition_type="attachment" if dl else "inline")
