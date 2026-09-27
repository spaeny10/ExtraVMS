"""The hub's FastAPI app: sign-in, organisations and members, site enrolment, the fleet, alerts, audit,
the /agent tunnel endpoint, the per-site proxy, and the hub's own pages."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from pathlib import Path
from typing import Literal

import pyotp
import sqlalchemy as sa
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, EmailStr, Field

from . import __version__, alerts, auth, backups, db, digest, proxy, push, turn, vlm_proxy
from . import fleet as fleet_mod
from fastapi.responses import StreamingResponse
from .agents import registry
from .config import settings
from .roles import ROLES

log = logging.getLogger("hub")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.engine()
    alerts.on_open = _notify
    tasks = [asyncio.create_task(_sweeper(), name="sweeper"), asyncio.create_task(digest.daily_loop(), name="digests"),
             asyncio.create_task(backups.nightly_loop(), name="backups")]
    task = tasks[0]
    log.info("hub %s up on http://%s:%s (%s)", __version__, settings.host, settings.port, settings.public_url)
    yield
    for t in tasks:
        t.cancel()


def _notify(org_id: str, site: dict, kind: str, detail: dict) -> None:
    try:
        asyncio.get_running_loop().create_task(push.notify_alert(org_id, site, kind, detail))
    except RuntimeError:
        pass  # no loop (tests calling alerts directly)


async def _sweeper() -> None:
    while True:
        try:
            await registry.sweep()
            alerts.expire_event_alerts()
            db.run(sa.delete(db.sessions).where(db.sessions.c.expires_at < time.time()))
            db.run(sa.delete(db.claims).where(db.claims.c.expires_at < time.time() - 3600))
        except Exception:
            log.exception("sweeper")
        await asyncio.sleep(30)


app = FastAPI(title="NewVMS Hub", lifespan=lifespan)


@app.middleware("http")
async def csrf(request: Request, call_next):
    if request.url.path.startswith(("/auth/", "/api/")):
        try:
            auth.csrf_check(request)
        except HTTPException as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status_code)
    return await call_next(request)


def user(request: Request) -> dict:
    return auth.require_user(request)


# ---------------------------------------------------------------- auth

class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)
    totp: str | None = Field(None, max_length=12)


@app.post("/auth/login")
async def login(body: LoginIn, request: Request, response: Response):
    key = f"{body.email.lower()}|{request.client.host if request.client else ''}"
    if auth.too_many_failures(key):
        raise HTTPException(429, "too many attempts; try again in 15 minutes")
    u = auth.user_by_email(body.email)
    if not u or not auth.verify_password(body.password, u["password_hash"]):
        auth.record_failure(key)
        raise HTTPException(401, "wrong email or password")
    if u["totp_enabled"] and not body.totp:
        return {"totp_required": True}
    if not auth.totp_ok(u, body.totp):
        auth.record_failure(key)
        raise HTTPException(401, "wrong code")
    sid = auth.new_session(u, request)
    auth.set_cookie(response, sid)
    return {"user": auth.public_user(u), "orgs": auth.user_orgs(u["id"])}


@app.post("/auth/logout")
async def logout(request: Request, response: Response):
    s = auth.session_of(request)
    if s:
        db.run(sa.delete(db.sessions).where(db.sessions.c.id == s["id"]))
    auth.clear_cookie(response)
    return {"ok": True}


@app.get("/auth/me")
async def me(request: Request):
    u = auth.current_user(request)
    if not u:
        raise HTTPException(401, "sign in")
    return {"user": auth.public_user(u), "orgs": auth.user_orgs(u["id"]), "active_org": u["session"].get("org_id")}


class PasswordIn(BaseModel):
    current: str
    new: str = Field(min_length=10, max_length=200)


@app.post("/auth/password")
async def change_password(body: PasswordIn, u: dict = Depends(user)):
    if not auth.verify_password(body.current, u["password_hash"]):
        raise HTTPException(401, "current password is wrong")
    db.run(sa.update(db.users).where(db.users.c.id == u["id"]).values(password_hash=auth.hash_password(body.new)))
    return {"ok": True}


@app.post("/auth/totp/setup")
async def totp_setup(u: dict = Depends(user)):
    secret = pyotp.random_base32()
    db.run(sa.update(db.users).where(db.users.c.id == u["id"]).values(totp_secret=secret, totp_enabled=False))
    return {"secret": secret, "uri": pyotp.TOTP(secret).provisioning_uri(name=u["email"], issuer_name="NewVMS Hub")}


class TotpIn(BaseModel):
    code: str = Field(min_length=6, max_length=8)


@app.post("/auth/totp/enable")
async def totp_enable(body: TotpIn, u: dict = Depends(user)):
    if not u["totp_secret"] or not pyotp.TOTP(u["totp_secret"]).verify(body.code.strip(), valid_window=1):
        raise HTTPException(400, "code doesn't match")
    db.run(sa.update(db.users).where(db.users.c.id == u["id"]).values(totp_enabled=True))
    return {"ok": True}


@app.post("/auth/totp/disable")
async def totp_disable(u: dict = Depends(user)):
    db.run(sa.update(db.users).where(db.users.c.id == u["id"]).values(totp_enabled=False, totp_secret=None))
    return {"ok": True}


# ---------------------------------------------------------------- organisations, members, invites

class OrgIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9-]+$")


@app.get("/api/orgs")
async def list_orgs(u: dict = Depends(user)):
    if u["is_super"]:
        return [{**o, "role": "owner"} for o in db.rows(sa.select(db.orgs).order_by(db.orgs.c.name))]
    return auth.user_orgs(u["id"])


@app.post("/api/orgs")
async def create_org(body: OrgIn, u: dict = Depends(user)):
    if not u["is_super"]:
        raise HTTPException(403, "only a hub administrator creates organisations")
    if db.one(sa.select(db.orgs).where(db.orgs.c.slug == body.slug)):
        raise HTTPException(409, "slug taken")
    o = {"id": db.new_id("o_"), "name": body.name, "slug": body.slug, "created_at": time.time(), "branding": None, "ai_shared": False}
    db.insert(db.orgs, o)
    db.insert(db.memberships, {"user_id": u["id"], "org_id": o["id"], "role": "owner"})
    return o


class OrgPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    ai_shared: bool | None = None


@app.patch("/api/orgs/{org_id}")
async def patch_org(org_id: str, body: OrgPatch, u: dict = Depends(user)):
    auth.require_role(u, org_id, "owner")
    vals = {k: v for k, v in body.model_dump().items() if v is not None}
    if vals:
        db.run(sa.update(db.orgs).where(db.orgs.c.id == org_id).values(**vals))
    org = db.one(sa.select(db.orgs).where(db.orgs.c.id == org_id))
    if "ai_shared" in vals:
        await registry.push_vlm(org)
        _audit(u, org_id, None, f"shared AI {'enabled' if vals['ai_shared'] else 'disabled'}")
    return org


@app.get("/api/orgs/{org_id}/usage")
async def org_usage(org_id: str, days: int = Query(30, ge=1, le=365), u: dict = Depends(user)):
    auth.require_role(u, org_id, "viewer")
    org = db.one(sa.select(db.orgs).where(db.orgs.c.id == org_id))
    names = {s["id"]: s["name"] for s in db.rows(sa.select(db.sites.c.id, db.sites.c.name).where(db.sites.c.org_id == org_id))}
    rows = vlm_proxy.usage_report(org_id, days)
    for r in rows:
        r["site_name"] = names.get(r["site_id"], r["site_id"])
    return {"ai_shared": bool(org and org.get("ai_shared")), "configured": vlm_proxy.configured(), "model": settings.vllm_model,
            "turn": turn.configured(), "days": days, "sites": rows}


class MemberIn(BaseModel):
    email: EmailStr
    role: Literal["viewer", "operator", "admin", "owner"] = "viewer"
    password: str | None = Field(None, min_length=10, max_length=200)   # create the user if they don't exist yet


@app.get("/api/orgs/{org_id}/members")
async def list_members(org_id: str, u: dict = Depends(user)):
    auth.require_role(u, org_id, "admin")
    q = sa.select(db.users.c.id, db.users.c.email, db.users.c.totp_enabled, db.users.c.last_login_at, db.memberships.c.role) \
        .join(db.memberships, db.memberships.c.user_id == db.users.c.id).where(db.memberships.c.org_id == org_id).order_by(db.users.c.email)
    members = db.rows(q)
    grants = db.rows(sa.select(db.site_grants).join(db.sites, db.sites.c.id == db.site_grants.c.site_id).where(db.sites.c.org_id == org_id))
    for m in members:
        m["sites"] = [g["site_id"] for g in grants if g["user_id"] == m["id"]]
    return members


@app.post("/api/orgs/{org_id}/members")
async def add_member(org_id: str, body: MemberIn, u: dict = Depends(user)):
    role = auth.require_role(u, org_id, "admin")
    if body.role == "owner" and role != "owner":
        raise HTTPException(403, "only an owner can add an owner")
    target = auth.user_by_email(body.email)
    if not target:
        if not body.password:
            raise HTTPException(400, "new user: supply an initial password (they can change it after signing in)")
        target = auth.create_user(body.email, body.password)
    if db.one(sa.select(db.memberships).where(db.memberships.c.user_id == target["id"], db.memberships.c.org_id == org_id)):
        db.run(sa.update(db.memberships).where(db.memberships.c.user_id == target["id"], db.memberships.c.org_id == org_id).values(role=body.role))
    else:
        db.insert(db.memberships, {"user_id": target["id"], "org_id": org_id, "role": body.role})
    _audit(u, org_id, None, f"member {body.email} -> {body.role}")
    return {"id": target["id"], "email": target["email"], "role": body.role}


class GrantsIn(BaseModel):
    site_ids: list[str] = []   # empty = every site in the org


@app.put("/api/orgs/{org_id}/members/{uid}/grants")
async def set_grants(org_id: str, uid: str, body: GrantsIn, u: dict = Depends(user)):
    auth.require_role(u, org_id, "admin")
    org_sites = {s["id"] for s in db.rows(sa.select(db.sites.c.id).where(db.sites.c.org_id == org_id))}
    with db.engine().begin() as c:
        c.execute(sa.delete(db.site_grants).where(db.site_grants.c.user_id == uid, db.site_grants.c.site_id.in_(list(org_sites))))
        for sid in body.site_ids:
            if sid in org_sites:
                c.execute(db.site_grants.insert().values(user_id=uid, site_id=sid))
    return {"ok": True}


@app.delete("/api/orgs/{org_id}/members/{uid}")
async def remove_member(org_id: str, uid: str, u: dict = Depends(user)):
    auth.require_role(u, org_id, "admin")
    if uid == u["id"]:
        raise HTTPException(400, "remove yourself from another owner's account")
    db.run(sa.delete(db.memberships).where(db.memberships.c.user_id == uid, db.memberships.c.org_id == org_id))
    _audit(u, org_id, None, f"member {uid} removed")
    return {"ok": True}


# ---------------------------------------------------------------- sites and enrolment

class ClaimIn(BaseModel):
    code: str = Field(min_length=8, max_length=9)
    name: str = Field(min_length=1, max_length=120)
    location: str = Field("", max_length=200)


@app.get("/api/orgs/{org_id}/sites")
async def list_sites(org_id: str, u: dict = Depends(user)):
    auth.require_role(u, org_id, "viewer")
    return [_site_card(s) for s in auth.visible_sites(u, org_id)]


@app.get("/api/claims/{code}")
async def claim_preview(code: str, u: dict = Depends(user)):
    """What the site waiting under this code says about itself (before enrolling it)."""
    c = db.one(sa.select(db.claims).where(db.claims.c.code == code.upper().strip()))
    if not c or c["expires_at"] < time.time() or c["consumed_site_id"]:
        raise HTTPException(404, "no site is waiting with that code (codes last 15 minutes; check Settings → System at the site)")
    return {"code": c["code"], "hint": c["hint"], "agent_ip": c["agent_ip"], "waiting": c["code"] in registry.pending}


@app.post("/api/orgs/{org_id}/sites/claim")
async def claim_site(org_id: str, body: ClaimIn, u: dict = Depends(user)):
    auth.require_role(u, org_id, "admin")
    code = body.code.upper().strip()
    c = db.one(sa.select(db.claims).where(db.claims.c.code == code))
    if not c or c["expires_at"] < time.time() or c["consumed_site_id"]:
        raise HTTPException(404, "no site is waiting with that code")
    if code not in registry.pending:
        raise HTTPException(409, "the site is not connected right now; it reconnects within a minute")
    token = db.new_token()
    site = {"id": db.new_id("s_"), "org_id": org_id, "name": body.name, "location": body.location, "token_hash": db.token_hash(token),
            "token_prev_hash": None, "token_rotated_at": None, "created_at": time.time(), "last_seen_at": None, "online": False,
            "version": (c["hint"] or {}).get("version"), "summary": None, "clock_skew_s": None, "agent_ip": c["agent_ip"],
            "hostname": (c["hint"] or {}).get("hostname")}
    db.insert(db.sites, site)
    db.run(sa.update(db.claims).where(db.claims.c.code == code).values(consumed_site_id=site["id"]))
    if not await registry.enrol(code, site, token):
        raise HTTPException(409, "the site disconnected while enrolling; try again")
    _audit(u, org_id, site["id"], f"site enrolled: {body.name}")
    return _site_card(site)


class SiteIn(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    location: str | None = Field(None, max_length=200)


@app.patch("/api/sites/{site_id}")
async def update_site(site_id: str, body: SiteIn, u: dict = Depends(user)):
    site, _ = auth.site_access(u, site_id)
    auth.require_role(u, site["org_id"], "admin")
    vals = {k: v for k, v in body.model_dump().items() if v is not None}
    if vals:
        db.run(sa.update(db.sites).where(db.sites.c.id == site_id).values(**vals))
    return _site_card(db.one(sa.select(db.sites).where(db.sites.c.id == site_id)))


@app.post("/api/sites/{site_id}/rotate-token")
async def rotate_token(site_id: str, u: dict = Depends(user)):
    site, _ = auth.site_access(u, site_id)
    auth.require_role(u, site["org_id"], "admin")
    token = db.new_token()
    db.run(sa.update(db.sites).where(db.sites.c.id == site_id).values(token_prev_hash=site["token_hash"], token_hash=db.token_hash(token), token_rotated_at=time.time()))
    pushed = await registry.push(site_id, {"t": "rotate", "token": token})
    _audit(u, site["org_id"], site_id, "site token rotated")
    return {"ok": True, "pushed": pushed}


@app.delete("/api/sites/{site_id}")
async def revoke_site(site_id: str, u: dict = Depends(user)):
    site, _ = auth.site_access(u, site_id)
    auth.require_role(u, site["org_id"], "admin")
    await registry.push(site_id, {"t": "revoked", "reason": "removed at the hub"})
    with db.engine().begin() as c:
        c.execute(sa.delete(db.site_grants).where(db.site_grants.c.site_id == site_id))
        c.execute(sa.delete(db.alerts).where(db.alerts.c.site_id == site_id))
        c.execute(sa.delete(db.sites).where(db.sites.c.id == site_id))
    _audit(u, site["org_id"], site_id, f"site removed: {site['name']}")
    return {"ok": True}


def _site_card(s: dict) -> dict:
    summ = s.get("summary") or {}
    open_alerts = db.one(sa.select(sa.func.count()).select_from(db.alerts).where(db.alerts.c.site_id == s["id"], db.alerts.c.closed_at.is_(None)))
    return {"id": s["id"], "org_id": s["org_id"], "name": s["name"], "location": s["location"], "online": bool(s["online"]) and s["id"] in registry.by_site,
            "last_seen_at": s["last_seen_at"], "version": s["version"], "hostname": s["hostname"], "clock_skew_s": s["clock_skew_s"],
            "summary": summ, "open_alerts": list(open_alerts.values())[0] if open_alerts else 0}


# ---------------------------------------------------------------- fleet, alerts, audit

@app.get("/api/fleet")
async def fleet(org: str | None = None, u: dict = Depends(user)):
    orgs = auth.user_orgs(u["id"]) if not u["is_super"] else [{**o, "role": "owner"} for o in db.rows(sa.select(db.orgs).order_by(db.orgs.c.name))]
    if org:
        orgs = [o for o in orgs if o["id"] == org]
    out = []
    for o in orgs:
        sites = [_site_card(s) for s in auth.visible_sites(u, o["id"])]
        out.append({"org": {"id": o["id"], "name": o["name"], "slug": o["slug"], "role": o["role"]}, "sites": sites,
                    "open_alerts": sum(s["open_alerts"] for s in sites)})
    return {"orgs": out, "now": time.time(), "offline_after_s": settings.offline_after_s}


@app.get("/api/alerts")
async def list_alerts(org: str, open: bool = True, limit: int = Query(200, le=1000), u: dict = Depends(user)):
    auth.require_role(u, org, "viewer")
    site_ids = [s["id"] for s in auth.visible_sites(u, org)]
    if open:
        rows = alerts.open_for_org(org, site_ids, limit)
    else:
        rows = db.rows(sa.select(db.alerts).where(db.alerts.c.org_id == org, db.alerts.c.site_id.in_(site_ids)).order_by(db.alerts.c.opened_at.desc()).limit(limit))
    names = {s["id"]: s["name"] for s in db.rows(sa.select(db.sites.c.id, db.sites.c.name).where(db.sites.c.org_id == org))}
    for r in rows:
        r["site_name"] = names.get(r["site_id"], r["site_id"])
    return rows


@app.post("/api/alerts/{alert_id}/ack")
async def ack_alert(alert_id: int, u: dict = Depends(user)):
    a = db.one(sa.select(db.alerts).where(db.alerts.c.id == alert_id))
    if not a:
        raise HTTPException(404)
    auth.require_role(u, a["org_id"], "viewer")
    alerts.ack(alert_id, u["id"])
    return {"ok": True}


@app.get("/api/audit")
async def audit(org: str, site: str | None = None, since: float | None = None, limit: int = Query(200, le=2000), u: dict = Depends(user)):
    auth.require_role(u, org, "admin")
    q = sa.select(db.audit_log).where(db.audit_log.c.org_id == org).order_by(db.audit_log.c.ts.desc()).limit(limit)
    if site:
        q = q.where(db.audit_log.c.site_id == site)
    if since:
        q = q.where(db.audit_log.c.ts >= since)
    return db.rows(q)


def _audit(u: dict, org_id: str | None, site_id: str | None, action: str) -> None:
    db.insert(db.audit_log, {"ts": time.time(), "user_id": u["id"], "user_email": u["email"], "org_id": org_id, "site_id": site_id,
                             "action": action, "method": None, "path": None, "status": None, "ip": None, "detail": {}})


# ---------------------------------------------------------------- fleet find / ask, digests, backups, push

@app.get("/api/fleet/search")
async def fleet_search(org: str, q: str = Query(min_length=1, max_length=200), since: float | None = None, until: float | None = None,
                       u: dict = Depends(user)):
    auth.require_role(u, org, "viewer")
    return await fleet_mod.search(u, org, q, since, until)


class FleetAskIn(BaseModel):
    org: str
    message: str = Field(min_length=1, max_length=2000)


@app.post("/api/fleet/ask")
async def fleet_ask(body: FleetAskIn, u: dict = Depends(user)):
    auth.require_role(u, body.org, "viewer")
    return StreamingResponse(fleet_mod.ask(u, body.org, body.message), media_type="application/x-ndjson")


@app.get("/api/orgs/{org_id}/digests")
async def org_digests(org_id: str, limit: int = Query(7, le=60), u: dict = Depends(user)):
    auth.require_role(u, org_id, "viewer")
    return digest.latest(org_id, limit)


@app.post("/api/orgs/{org_id}/digests/generate")
async def org_digest_now(org_id: str, u: dict = Depends(user)):
    auth.require_role(u, org_id, "viewer")
    return await digest.generate(org_id)


@app.get("/api/sites/{site_id}/backups")
async def site_backups(site_id: str, u: dict = Depends(user)):
    site, _ = auth.site_access(u, site_id)
    auth.require_role(u, site["org_id"], "admin")
    return backups.list_for(site_id)


@app.post("/api/sites/{site_id}/backups")
async def site_backup_now(site_id: str, u: dict = Depends(user)):
    site, _ = auth.site_access(u, site_id)
    auth.require_role(u, site["org_id"], "admin")
    try:
        row = await backups.backup_site(site, u["email"])
    except Exception as e:
        raise HTTPException(503, f"backup failed: {e}")
    if not row:
        raise HTTPException(503, "site offline")
    _audit(u, site["org_id"], site_id, "config backup taken")
    return {k: v for k, v in row.items() if k != "data"}


@app.get("/api/sites/{site_id}/backups/{backup_id}")
async def site_backup_download(site_id: str, backup_id: int, u: dict = Depends(user)):
    site, _ = auth.site_access(u, site_id)
    auth.require_role(u, site["org_id"], "admin")
    b = db.one(sa.select(db.config_backups).where(db.config_backups.c.id == backup_id, db.config_backups.c.site_id == site_id))
    if not b:
        raise HTTPException(404)
    return JSONResponse(b["data"], headers={"Content-Disposition": f'attachment; filename="{site["name"]}-{time.strftime("%Y%m%d", time.localtime(b["created_at"]))}.json"'})


class RestoreIn(BaseModel):
    replace_identities: bool = False


@app.post("/api/sites/{site_id}/backups/{backup_id}/restore")
async def site_backup_restore(site_id: str, backup_id: int, body: RestoreIn, u: dict = Depends(user)):
    site, _ = auth.site_access(u, site_id)
    auth.require_role(u, site["org_id"], "admin")
    try:
        result = await backups.restore(site, backup_id, u["email"], body.replace_identities)
    except LookupError:
        raise HTTPException(404, "no such backup")
    except Exception as e:
        raise HTTPException(503, f"restore failed: {e}")
    _audit(u, site["org_id"], site_id, f"config restored from backup {backup_id}")
    return result


@app.get("/api/push/vapid")
async def push_vapid(u: dict = Depends(user)):
    return {"public_key": push.vapid()["public"], "subscriptions": [{"endpoint": s["endpoint"], "kinds": s["kinds"], "ua": s["ua"]} for s in push.subscriptions_for(u["id"])],
            "kinds": list(alerts.KINDS)}


class PushIn(BaseModel):
    subscription: dict
    kinds: list[str] | None = None


@app.post("/api/push/subscribe")
async def push_subscribe(body: PushIn, request: Request, u: dict = Depends(user)):
    try:
        push.subscribe(u["id"], body.subscription, body.kinds, request.headers.get("user-agent") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


class UnpushIn(BaseModel):
    endpoint: str


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(body: UnpushIn, u: dict = Depends(user)):
    push.unsubscribe(u["id"], body.endpoint)
    return {"ok": True}


# ---------------------------------------------------------------- tunnel + proxy

@app.websocket("/agent")
async def agent(ws: WebSocket):
    await registry.serve(ws)


@app.get("/s/{site_id}/api/turn")
async def site_turn(site_id: str, request: Request):
    """ICE servers for a browser watching this site through the hub (answered here, not by the site)."""
    u = auth.require_user(request)
    auth.site_access(u, site_id)
    return {"iceServers": turn.ice_servers(f"user:{u['id']}", settings.turn_user_ttl_s)}


app.include_router(vlm_proxy.router)
app.include_router(proxy.router)


# ---------------------------------------------------------------- hub pages

@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": __version__, "sites_online": len(registry.by_site), "turn": turn.configured(), "shared_ai": vlm_proxy.configured()}


@app.get("/{full_path:path}")
async def ui(full_path: str):
    dist: Path = settings.ui_dir
    target = (dist / full_path).resolve() if full_path else None
    if target and full_path and target.is_file() and str(target).startswith(str(dist.resolve())):
        return FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable" if full_path.startswith("hub-assets/") else "no-cache"})
    index = dist / "index.html"
    if not index.exists():
        return JSONResponse({"detail": "hub UI not built (run npm run build in hub/ui)"}, status_code=503)
    return FileResponse(index, headers={"Cache-Control": "no-cache"})
