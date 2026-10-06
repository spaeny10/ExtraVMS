"""The hub's FastAPI app: sign-in, organizations and members, site enrollment, the fleet, alerts, audit,
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
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, EmailStr, Field

from . import __version__, alerts, auth, backups, cameras, dashboards, db, digest, direct, fleet_actions, proxy, push, soc_api, turn, vlm_proxy
from . import fleet as fleet_mod
from . import find as find_mod
from . import geocode, hosts, security, soc, soc_reports
from fastapi.responses import StreamingResponse
from .agents import registry
from .config import settings
from .roles import RANK, ROLES, allows

log = logging.getLogger("hub")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.engine()
    alerts.on_open = _notify
    tasks = [asyncio.create_task(_sweeper(), name="sweeper"), asyncio.create_task(digest.daily_loop(), name="digests"),
             asyncio.create_task(backups.nightly_loop(), name="backups")]
    if settings.soc_loops:
        # SOC: escalation every 10 s and the shift / monthly reports; both idle unless a Site is monitored
        tasks += [asyncio.create_task(soc.escalation_loop(), name="soc-escalation"),
                  asyncio.create_task(soc_reports.shift_loop(), name="soc-reports")]
    if settings.geocode_backfill:
        tasks.append(asyncio.create_task(geocode.backfill_once_at_start(), name="geocode-backfill"))   # once per start, 1 request/s
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
            await hosts.registry.sweep()
            alerts.expire_event_alerts()
            db.run(sa.delete(db.sessions).where(db.sessions.c.expires_at < time.time()))
            db.run(sa.delete(db.claims).where(db.claims.c.expires_at < time.time() - 3600))
        except Exception:
            log.exception("sweeper")
        await asyncio.sleep(30)


app = FastAPI(title="Axiom Vision Hub", lifespan=lifespan)


@app.middleware("http")
async def csrf(request: Request, call_next):
    if request.url.path.startswith(("/auth/", "/api/")):
        try:
            auth.csrf_check(request)
        except HTTPException as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status_code)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """HSTS, nosniff, framing, referrer/permissions policies and a CSP on every response (security.py)."""
    response = await call_next(request)
    security.apply(request, response)
    return response


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
    if not u:
        auth.burn_password_check(body.password)   # as slow as a wrong password, so unknown emails don't stand out
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
    return {"user": auth.me_user(u), "orgs": auth.orgs_for(u)}


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
    # a hub administrator gets every customer (they own them all), so the Customer picker can switch into any of them
    return {"user": auth.me_user(u), "orgs": auth.orgs_for(u), "active_org": u["session"].get("org_id"),
            "map": geocode.map_config()}   # the hub UI's Site maps


class PasswordIn(BaseModel):
    current: str
    new: str = Field(min_length=10, max_length=200)


@app.post("/auth/password")
async def change_password(body: PasswordIn, u: dict = Depends(user)):
    """Change the password and sign out every other session of this user (this one stays signed in)."""
    _reauth_limit(u)
    if not auth.verify_password(body.current, u["password_hash"]):
        auth.record_failure(f"reauth|{u['id']}")
        raise HTTPException(401, "current password is wrong")
    db.run(sa.update(db.users).where(db.users.c.id == u["id"]).values(password_hash=auth.hash_password(body.new)))
    auth.end_other_sessions(u["id"], u["session"]["id"])
    return {"ok": True}


# Two-factor changes need more than a session (a stolen cookie must not be able to turn 2FA off or move it to the
# thief's phone): the current password always, and a current authenticator code while 2FA is on. A new secret
# waits in kv ("totp_pending:<user>") until a code from it is confirmed, so re-setup never leaves 2FA off.

class ReauthIn(BaseModel):
    password: str = Field(min_length=1, max_length=200)
    code: str | None = Field(None, max_length=12)


def _pending_key(uid: str) -> str:
    return f"totp_pending:{uid}"


def _reauth_limit(u: dict) -> None:
    if auth.too_many_failures(f"reauth|{u['id']}"):
        raise HTTPException(429, "too many attempts; try again in 15 minutes")


def _reauth(u: dict, body: ReauthIn) -> None:
    """401 unless the password is right and, with 2FA on, the code is a current unused one (it is used up)."""
    _reauth_limit(u)
    if not auth.verify_password(body.password, u["password_hash"]):
        auth.record_failure(f"reauth|{u['id']}")
        raise HTTPException(401, "current password is wrong")
    if u["totp_enabled"]:
        if not body.code:
            raise HTTPException(401, "enter a code from your authenticator app")
        if not auth.totp_ok(u, body.code):
            auth.record_failure(f"reauth|{u['id']}")
            raise HTTPException(401, "wrong or already used authenticator code")


@app.post("/auth/totp/setup")
async def totp_setup(body: ReauthIn, u: dict = Depends(user)):
    """A new secret to add to an authenticator app; nothing changes until /auth/totp/enable confirms a code from it."""
    _reauth(u, body)
    secret = pyotp.random_base32()
    with db.engine().begin() as c:
        c.execute(sa.delete(db.kv).where(db.kv.c.key == _pending_key(u["id"])))
        c.execute(db.kv.insert().values(key=_pending_key(u["id"]), value={"secret": secret, "at": time.time()}))
    return {"secret": secret, "uri": pyotp.TOTP(secret).provisioning_uri(name=u["email"], issuer_name="Axiom Vision")}


class TotpIn(BaseModel):
    code: str = Field(min_length=6, max_length=8)


@app.post("/auth/totp/enable")
async def totp_enable(body: TotpIn, u: dict = Depends(user)):
    """Confirm the pending secret with a code from it: it becomes the user's (2FA on, or moved to the new device)."""
    _reauth_limit(u)
    row = db.one(sa.select(db.kv).where(db.kv.c.key == _pending_key(u["id"])))
    secret = (row or {}).get("value", {}).get("secret") if row else None
    if not secret or time.time() - float(row["value"].get("at") or 0) > 3600:
        raise HTTPException(400, "start the set-up again (it lasts an hour)")
    step = auth.totp_step_for(secret, body.code)
    if step is None:
        auth.record_failure(f"reauth|{u['id']}")
        raise HTTPException(400, "code doesn't match")
    with db.engine().begin() as c:
        c.execute(sa.update(db.users).where(db.users.c.id == u["id"]).values(totp_secret=secret, totp_enabled=True))
        auth.consume_totp_step(u["id"], step, c)
        c.execute(sa.delete(db.kv).where(db.kv.c.key == _pending_key(u["id"])))
    return {"ok": True}


@app.post("/auth/totp/disable")
async def totp_disable(body: ReauthIn, u: dict = Depends(user)):
    _reauth(u, body)
    db.run(sa.update(db.users).where(db.users.c.id == u["id"]).values(totp_enabled=False, totp_secret=None))
    db.run(sa.delete(db.kv).where(db.kv.c.key == _pending_key(u["id"])))
    return {"ok": True}


# ---------------------------------------------------------------- organizations, members, invites

class OrgIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9-]+$")


@app.get("/api/orgs")
async def list_orgs(u: dict = Depends(user)):
    return auth.orgs_for(u)


@app.post("/api/orgs")
async def create_org(body: OrgIn, u: dict = Depends(user)):
    if not u["is_super"]:
        raise HTTPException(403, "only a hub administrator creates organizations")
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
    tags = _location_tags(org_id)
    rows = vlm_proxy.usage_report(org_id, days)
    m = auth.membership(u, org_id)
    if not m["all_sites"]:
        # a Site-restricted member sees the usage of the servers they can see; everyone else keeps the whole report
        # (including servers since removed, whose rows have no Site)
        seen = {s["id"] for s in auth.visible_sites(u, org_id, include_retired=True)}
        rows = [r for r in rows if r["site_id"] in seen]
    per_loc: dict[str, dict] = {}
    for r in rows:
        r["site_name"] = names.get(r["site_id"], r["site_id"])
        r |= tags.get(r["site_id"], {"location_id": None, "location_name": None})
        if r["location_id"]:
            t = per_loc.setdefault(r["location_id"], {"location_id": r["location_id"], "name": r["location_name"], "requests": 0,
                                                      "prompt_tokens": 0, "completion_tokens": 0, "errors": 0})
            for k in ("requests", "prompt_tokens", "completion_tokens", "errors"):
                t[k] += int(r.get(k) or 0)
    return {"ai_shared": bool(org and org.get("ai_shared")), "configured": vlm_proxy.configured(), "model": settings.vllm_model,
            "provider": vlm_proxy.status(), "turn": turn.configured(), "days": days, "sites": rows,
            "locations": sorted(per_loc.values(), key=lambda t: (t["name"] or "").casefold())}


class MemberIn(BaseModel):
    email: EmailStr
    role: Literal["viewer", "operator", "admin", "owner"] = "viewer"
    password: str | None = Field(None, min_length=10, max_length=200)   # create the user if they don't exist yet
    all_sites: bool | None = None          # None: unchanged (new members see every Site)
    location_ids: list[str] | None = None  # given without all_sites: only these Sites


@app.get("/api/orgs/{org_id}/members")
async def list_members(org_id: str, u: dict = Depends(user)):
    auth.require_customer_role(u, org_id, "admin")
    q = sa.select(db.users.c.id, db.users.c.email, db.users.c.totp_enabled, db.users.c.last_login_at, db.memberships.c.role) \
        .join(db.memberships, db.memberships.c.user_id == db.users.c.id).where(db.memberships.c.org_id == org_id).order_by(db.users.c.email)
    members = db.rows(q)
    for m in members:
        m.update(auth.access_of(org_id, m["id"]))   # all_sites, location_ids
    return members


@app.post("/api/orgs/{org_id}/members")
async def add_member(org_id: str, body: MemberIn, u: dict = Depends(user)):
    role = auth.require_customer_role(u, org_id, "admin")
    if body.role == "owner" and role != "owner":
        raise HTTPException(403, "only an owner can add an owner")
    target = auth.user_by_email(body.email)
    existing = target and db.one(sa.select(db.memberships).where(db.memberships.c.user_id == target["id"], db.memberships.c.org_id == org_id))
    if body.all_sites is not None or body.location_ids is not None:
        auth.check_grant_scope(u, org_id, body.all_sites if body.all_sites is not None else not body.location_ids, body.location_ids)
    elif not existing:
        # a new member with no access fields defaults to every Site, which a Site-restricted admin can't grant;
        # checked before the user is created so a refused request leaves nothing behind
        auth.check_grant_scope(u, org_id, True, [])
    if not target:
        if not body.password:
            raise HTTPException(400, "new user: supply an initial password (they can change it after signing in)")
        target = auth.create_user(body.email, body.password)
    if db.one(sa.select(db.memberships).where(db.memberships.c.user_id == target["id"], db.memberships.c.org_id == org_id)):
        db.run(sa.update(db.memberships).where(db.memberships.c.user_id == target["id"], db.memberships.c.org_id == org_id).values(role=body.role))
    else:
        db.insert(db.memberships, {"user_id": target["id"], "org_id": org_id, "role": body.role, "all_sites": True})
    out = {"id": target["id"], "email": target["email"], "role": body.role}
    if body.all_sites is not None or body.location_ids is not None:
        all_sites = body.all_sites if body.all_sites is not None else not body.location_ids
        out |= _set_access(org_id, target["id"], all_sites, body.location_ids or [])
    _audit(u, org_id, None, f"member {body.email} -> {body.role}")
    return out


def _set_access(org_id: str, uid: str, all_sites: bool, location_ids: list[str]) -> dict:
    try:
        return auth.set_access(org_id, uid, all_sites, location_ids)
    except LookupError:
        raise HTTPException(404, "not a member of this organization")
    except ValueError as e:
        raise HTTPException(422, str(e))


class AccessIn(BaseModel):
    all_sites: bool
    location_ids: list[str] = []   # ignored for access while all_sites is true (kept as the member's list)


@app.put("/api/orgs/{org_id}/members/{uid}/access")
async def set_member_access(org_id: str, uid: str, body: AccessIn, u: dict = Depends(user)):
    """A member's Sites: every Site of the customer, or exactly these (none = nothing at all)."""
    auth.require_customer_role(u, org_id, "admin")
    auth.check_grant_scope(u, org_id, body.all_sites, body.location_ids)
    out = _set_access(org_id, uid, body.all_sites, body.location_ids)
    target = auth.user_by_id(uid)
    _audit(u, org_id, None, f"member {target['email'] if target else uid} access: "
                            + ("all sites" if out["all_sites"] else f"{len(out['location_ids'])} site(s)"))
    return out


@app.delete("/api/orgs/{org_id}/members/{uid}")
async def remove_member(org_id: str, uid: str, u: dict = Depends(user)):
    auth.require_customer_role(u, org_id, "admin")
    if uid == u["id"]:
        raise HTTPException(400, "remove yourself from another owner's account")
    with db.engine().begin() as c:
        c.execute(sa.delete(db.memberships).where(db.memberships.c.user_id == uid, db.memberships.c.org_id == org_id))
        auth.drop_access(org_id, uid, c)
    _audit(u, org_id, None, f"member {uid} removed")
    return {"ok": True}


# Invites: an admin makes a link (nothing is emailed; they send it however they like). The code is the secret, so
# the public preview/accept routes are rate-limited per IP like sign-in, and an invite made for an email address
# can only be accepted by that address. Accepting never narrows an existing member: the higher role and the
# wider Site access win.

class InviteIn(BaseModel):
    email: str = Field("", max_length=200)   # optional: "" = anyone holding the link
    role: Literal["viewer", "operator", "admin", "owner"] = "viewer"
    all_sites: bool = True
    location_ids: list[str] = []
    label: str | None = Field(None, max_length=120)
    expires_days: int = Field(7, ge=1, le=90)


class AcceptIn(BaseModel):
    email: EmailStr | None = None   # ignored when the browser is already signed in
    password: str | None = Field(None, max_length=200)
    totp: str | None = Field(None, max_length=12)


def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return ""
    local, domain = email.split("@", 1)
    return f"{local[:1]}***@{domain}"


def _invite_ip(request: Request) -> str:
    return f"invite|{request.client.host if request.client else ''}"


def _live_invite(code: str) -> dict | None:
    inv = db.one(sa.select(db.invites).where(db.invites.c.code == code))
    if not inv or inv["accepted_at"] or inv["expires_at"] < time.time():
        return None
    return inv


def _invite_locations(org_id: str, inv: dict) -> list[dict]:
    """The invite's Sites that still exist, by name (a Site deleted since the invite was made just drops out)."""
    ids = list(inv.get("location_ids") or [])
    if not ids:
        return []
    return db.rows(sa.select(db.locations.c.id, db.locations.c.name)
                   .where(db.locations.c.org_id == org_id, db.locations.c.id.in_(ids)).order_by(db.locations.c.name))


def _invite_out(inv: dict) -> dict:
    return {"code": inv["code"], "url": f"{settings.public_url.rstrip('/')}/invite/{inv['code']}", "email": inv["email"],
            "role": inv["role"], "all_sites": bool(inv["all_sites"]), "location_ids": list(inv.get("location_ids") or []),
            "label": inv.get("label"), "expires_at": inv["expires_at"], "created_at": inv.get("created_at"), "created_by": inv.get("created_by")}


@app.post("/api/orgs/{org_id}/invites")
async def create_invite(org_id: str, body: InviteIn, u: dict = Depends(user)):
    role = auth.require_customer_role(u, org_id, "admin")
    if body.role == "owner" and role != "owner":
        raise HTTPException(403, "only an owner can invite an owner")
    email = body.email.strip().lower()
    if email and ("@" not in email or " " in email):
        raise HTTPException(422, "that doesn't look like an email address")
    org_locs = {r["id"] for r in db.rows(sa.select(db.locations.c.id).where(db.locations.c.org_id == org_id))}
    wanted = list(dict.fromkeys(body.location_ids))
    bad = [lid for lid in wanted if lid not in org_locs]
    if bad:
        raise HTTPException(422, f"unknown site {bad[0]}")
    # an invite is a deferred grant: capped like a direct one, or it would be the easy way round the cap
    auth.check_grant_scope(u, org_id, body.all_sites, [] if body.all_sites else wanted)
    t = time.time()
    inv = {"code": db.new_token(), "org_id": org_id, "email": email, "role": body.role, "expires_at": t + body.expires_days * 86400,
           "accepted_at": None, "all_sites": body.all_sites, "location_ids": [] if body.all_sites else wanted, "created_by": u["id"],
           "created_at": t, "accepted_user_id": None, "label": (body.label or "").strip() or None}
    db.insert(db.invites, inv)
    _audit(u, org_id, None, f"invite created: {email or inv['label'] or 'link'} -> {body.role}",
           {"all_sites": body.all_sites, "location_ids": inv["location_ids"], "label": inv["label"]})   # never the code
    return _invite_out(inv)


@app.get("/api/orgs/{org_id}/invites")
async def list_invites(org_id: str, u: dict = Depends(user)):
    """Pending invites (not accepted, not expired), newest first."""
    auth.require_customer_role(u, org_id, "admin")
    rows = db.rows(sa.select(db.invites).where(db.invites.c.org_id == org_id, db.invites.c.accepted_at.is_(None),
                                               db.invites.c.expires_at >= time.time()).order_by(db.invites.c.expires_at.desc()))
    emails = {r["id"]: r["email"] for r in db.rows(sa.select(db.users.c.id, db.users.c.email).where(
        db.users.c.id.in_([r["created_by"] for r in rows if r.get("created_by")] or [""])))}
    return [_invite_out(r) | {"locations": _invite_locations(org_id, r), "created_by_email": emails.get(r.get("created_by"))} for r in rows]


@app.delete("/api/orgs/{org_id}/invites/{code}")
async def revoke_invite(org_id: str, code: str, u: dict = Depends(user)):
    auth.require_customer_role(u, org_id, "admin")
    inv = db.one(sa.select(db.invites).where(db.invites.c.code == code, db.invites.c.org_id == org_id))
    if not inv:
        raise HTTPException(404, "no such invite")
    db.run(sa.delete(db.invites).where(db.invites.c.code == code))
    _audit(u, org_id, None, f"invite revoked: {inv['email'] or inv.get('label') or 'link'}")
    return {"ok": True}


@app.get("/api/invites/{code}")
async def invite_preview(code: str, request: Request):
    """Public: what accepting this invite gives (shown on the /invite/<code> page before signing in)."""
    key = _invite_ip(request)
    if auth.too_many_failures(key):
        raise HTTPException(429, "too many attempts; try again in 15 minutes")
    inv = _live_invite(code)
    if not inv:
        auth.record_failure(key)
        raise HTTPException(404, "this invite is unknown, used or expired")
    org = db.one(sa.select(db.orgs.c.name).where(db.orgs.c.id == inv["org_id"]))
    return {"org_name": org["name"] if org else "", "role": inv["role"], "all_sites": bool(inv["all_sites"]),
            "locations": [] if inv["all_sites"] else _invite_locations(inv["org_id"], inv),
            "email_hint": _mask_email(inv["email"]), "expires_at": inv["expires_at"], "label": inv.get("label")}


@app.post("/api/invites/{code}/accept")
async def invite_accept(code: str, body: AcceptIn, request: Request, response: Response):
    """Public. Signed in: the invite joins that account (body ignored). Otherwise an existing account proves itself
    with its password (and TOTP code), or a new account is made with the email and password given."""
    key = _invite_ip(request)
    if auth.too_many_failures(key):
        raise HTTPException(429, "too many attempts; try again in 15 minutes")
    inv = _live_invite(code)
    if not inv:
        auth.record_failure(key)
        raise HTTPException(404, "this invite is unknown, used or expired")
    target = auth.current_user(request)
    attached, created = target is not None, False
    if not attached:
        if not body.email or not body.password:
            raise HTTPException(422, "email and password required")
        login_key = f"{body.email.lower()}|{request.client.host if request.client else ''}"   # shares sign-in's lockout
        if auth.too_many_failures(login_key):
            raise HTTPException(429, "too many attempts; try again in 15 minutes")
        target = auth.user_by_email(body.email)
        if target:
            if not auth.verify_password(body.password, target["password_hash"]):
                auth.record_failure(key)
                auth.record_failure(login_key)
                raise HTTPException(401, "wrong email or password")
            if target["totp_enabled"] and not body.totp:
                return {"totp_required": True}
            if not auth.totp_ok(target, body.totp):
                auth.record_failure(key)
                auth.record_failure(login_key)
                raise HTTPException(401, "wrong code")
        elif inv["email"] and body.email.lower() != inv["email"]:
            raise HTTPException(403, f"this invite is for {_mask_email(inv['email'])}")   # before creating anyone
        else:
            if len(body.password) < 10:
                raise HTTPException(422, "password must be at least 10 characters")
            target, created = auth.create_user(body.email, body.password), True
    if inv["email"] and target["email"] != inv["email"]:
        raise HTTPException(403, f"this invite is for {_mask_email(inv['email'])}; sign in as that account")
    org_id = inv["org_id"]
    now = time.time()
    with db.engine().begin() as c:
        # consume first, conditionally, so two concurrent accepts can't both use one invite
        res = c.execute(sa.update(db.invites).where(db.invites.c.code == code, db.invites.c.accepted_at.is_(None),
                                                    db.invites.c.expires_at >= now)
                        .values(accepted_at=now, accepted_user_id=target["id"]))
        if not res.rowcount:
            raise HTTPException(404, "this invite is unknown, used or expired")
        m = c.execute(sa.select(db.memberships).where(db.memberships.c.user_id == target["id"],
                                                      db.memberships.c.org_id == org_id)).mappings().first()
        if m:
            role = m["role"] if RANK.get(m["role"], -1) >= RANK[inv["role"]] else inv["role"]
            c.execute(sa.update(db.memberships).where(db.memberships.c.user_id == target["id"], db.memberships.c.org_id == org_id)
                      .values(role=role))
        else:
            role = inv["role"]
            c.execute(db.memberships.insert().values(user_id=target["id"], org_id=org_id, role=role, all_sites=bool(inv["all_sites"])))
    org_locs = {r["id"] for r in db.rows(sa.select(db.locations.c.id).where(db.locations.c.org_id == org_id))}
    locs = [lid for lid in (inv.get("location_ids") or []) if lid in org_locs]
    all_sites = bool(inv["all_sites"])
    if m:   # an existing member keeps whatever wider access they already had
        all_sites = all_sites or m.get("all_sites") is not False
        locs = list(dict.fromkeys(sorted(auth.granted_location_ids(target["id"], org_id)) + locs))
    access = auth.set_access(org_id, target["id"], all_sites, locs)
    if attached:
        key = target["session"]["id"]
    else:
        sid = auth.new_session(target, request)
        auth.set_cookie(response, sid)
        key = auth.session_key(sid)
    db.run(sa.update(db.sessions).where(db.sessions.c.id == key).values(org_id=org_id))   # land in the customer just joined
    _audit(target, org_id, None, f"invite accepted: {target['email']} -> {role}",
           {"label": inv.get("label"), "invited_by": inv.get("created_by"), "new_user": created,
            "all_sites": access["all_sites"], "location_ids": access["location_ids"]})
    return {"user": auth.public_user(target), "orgs": auth.user_orgs(target["id"]), "org_id": org_id, "role": role, **access}


# ---------------------------------------------------------------- sites and enrollment

class ClaimIn(BaseModel):
    code: str = Field(min_length=8, max_length=9)
    name: str = Field(min_length=1, max_length=120)
    location: str = Field("", max_length=200)
    location_id: str | None = Field(None, max_length=24)   # the Site to enroll into; None = a new one-server Site


@app.get("/api/orgs/{org_id}/sites")
@app.get("/api/orgs/{org_id}/servers")
async def list_sites(org_id: str, u: dict = Depends(user)):
    auth.require_role(u, org_id, "viewer")
    return _cards(auth.visible_sites(u, org_id, include_retired=True))


@app.get("/api/claims/{code}")
async def claim_preview(code: str, u: dict = Depends(user)):
    """What the site waiting under this code says about itself (before enrolling it)."""
    c = db.one(sa.select(db.claims).where(db.claims.c.code == code.upper().strip()))
    if not c or c["expires_at"] < time.time() or c["consumed_site_id"]:
        raise HTTPException(404, "no site is waiting with that code (codes last 15 minutes; check Settings → System at the site)")
    return {"code": c["code"], "hint": c["hint"], "agent_ip": c["agent_ip"], "waiting": c["code"] in registry.pending}


@app.post("/api/orgs/{org_id}/sites/claim")
@app.post("/api/orgs/{org_id}/servers/claim")
async def claim_site(org_id: str, body: ClaimIn, u: dict = Depends(user)):
    auth.require_role(u, org_id, "admin")
    code = body.code.upper().strip()
    c = db.one(sa.select(db.claims).where(db.claims.c.code == code))
    if not c or c["expires_at"] < time.time() or c["consumed_site_id"]:
        raise HTTPException(404, "no site is waiting with that code")
    if code not in registry.pending:
        raise HTTPException(409, "the site is not connected right now; it reconnects within a minute")
    if body.location_id and not _org_location(org_id, body.location_id):
        raise HTTPException(404, "unknown site")
    token = db.new_token()
    site = {"id": db.new_id("s_"), "org_id": org_id, "name": body.name, "location": body.location, "token_hash": db.token_hash(token),
            "token_prev_hash": None, "token_rotated_at": None, "created_at": time.time(), "last_seen_at": None, "online": False,
            "version": (c["hint"] or {}).get("version"), "summary": None, "clock_skew_s": None, "agent_ip": c["agent_ip"],
            "hostname": (c["hint"] or {}).get("hostname"), "location_id": body.location_id or None}
    with db.engine().begin() as conn:
        conn.execute(db.sites.insert().values(**site))
        db.location_for_server(conn, site)   # no Site chosen: its own one-server Site, as the fleet looked before Sites
    db.run(sa.update(db.claims).where(db.claims.c.code == code).values(consumed_site_id=site["id"]))
    if not await registry.enrol(code, site, token):
        raise HTTPException(409, "the site disconnected while enrolling; try again")
    _audit(u, org_id, site["id"], f"site enrolled: {body.name}")
    return _site_card(site)


class SiteIn(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    location: str | None = Field(None, max_length=200)
    location_id: str | None = Field(None, max_length=24)   # move the server to this Site (same customer)


@app.patch("/api/sites/{site_id}")
@app.patch("/api/servers/{site_id}")
async def update_site(site_id: str, body: SiteIn, u: dict = Depends(user)):
    site, _ = auth.site_access(u, site_id)
    auth.require_role(u, site["org_id"], "admin")
    vals = {k: v for k, v in body.model_dump().items() if v is not None}
    target = None
    if vals.get("location_id") == site.get("location_id"):
        vals.pop("location_id", None)
    elif "location_id" in vals:
        target = _org_location(site["org_id"], vals["location_id"])
        if not target:
            raise HTTPException(404, "unknown site")
    if vals:
        with db.engine().begin() as c:
            c.execute(sa.update(db.sites).where(db.sites.c.id == site_id).values(**vals))
            if target:
                cameras.relocate(site_id, target["id"], c)
                # a central instance's server moved: the instance (and its Peplink sheet) follows it
                c.execute(sa.update(db.central_instances).where(db.central_instances.c.server_id == site_id).values(location_id=target["id"]))
        registry.refresh(site_id)   # broadcasts and proxy headers use the live connection's copy
    if target:
        _audit(u, site["org_id"], site_id, f"server moved: {site['name']} -> {target['name']}", {"location_id": target["id"]})
    return _site_card(db.one(sa.select(db.sites).where(db.sites.c.id == site_id)))


def _org_location(org_id: str, location_id: str) -> dict | None:
    return db.one(sa.select(db.locations).where(db.locations.c.id == location_id, db.locations.c.org_id == org_id))


@app.post("/api/sites/{site_id}/rotate-token")
@app.post("/api/servers/{site_id}/rotate-token")
async def rotate_token(site_id: str, u: dict = Depends(user)):
    site, _ = auth.site_access(u, site_id)
    auth.require_role(u, site["org_id"], "admin")
    token = db.new_token()
    db.run(sa.update(db.sites).where(db.sites.c.id == site_id).values(token_prev_hash=site["token_hash"], token_hash=db.token_hash(token), token_rotated_at=time.time()))
    pushed = await registry.push(site_id, {"t": "rotate", "token": token})
    _audit(u, site["org_id"], site_id, "site token rotated")
    return {"ok": True, "pushed": pushed}


@app.post("/api/sites/{site_id}/direct-token")
@app.post("/api/servers/{site_id}/direct-token")
async def direct_token(site_id: str, u: dict = Depends(user)):
    """Direct-on-LAN: a short-lived token the browser presents straight to this server (on its LAN) for media,
    plus the URLs and certificate fingerprint the server last reported. Anyone who may view the server through
    the hub's proxy may have one, with the same role the proxy would send as x-hub-role. Not audited: it is
    read-only media access, exactly what the proxy already allows. `available: false` (no URLs reported yet)
    still returns a token; the UI simply keeps using the proxy. The token itself is never logged."""
    site, role = auth.site_access(u, site_id)
    if direct.rate_limited(u["id"], site_id):
        raise HTTPException(429, "too many direct-access tokens; try again shortly")
    token = direct.mint(site, u, role)
    return {"token": token, "exp": direct.payload_of(token)["exp"], "role": role, **direct.info(site.get("summary"))}


class RetireIn(BaseModel):
    retired: bool = True


@app.post("/api/sites/{site_id}/retire")
@app.post("/api/servers/{site_id}/retire")
async def retire_site(site_id: str, body: RetireIn, u: dict = Depends(user)):
    """Hide a site from Fleet, Home, Find, Ask and alerts (or bring it back). Its tunnel and data are untouched."""
    site, _ = auth.site_access(u, site_id)
    auth.require_role(u, site["org_id"], "admin")
    fleet_actions.retire(site_id, body.retired)
    _audit(u, site["org_id"], site_id, f"site {'retired' if body.retired else 'restored'}: {site['name']}")
    return _site_card(db.one(sa.select(db.sites).where(db.sites.c.id == site_id)))


@app.delete("/api/sites/{site_id}")
@app.delete("/api/servers/{site_id}")
async def revoke_site(site_id: str, u: dict = Depends(user)):
    """Remove a server and everything the hub keeps about it (its Site stays, even if now empty)."""
    site, _ = auth.site_access(u, site_id)
    auth.require_role(u, site["org_id"], "admin")
    await registry.push(site_id, {"t": "revoked", "reason": "removed at the hub"})
    with db.engine().begin() as c:
        c.execute(sa.delete(db.alerts).where(db.alerts.c.site_id == site_id))
        c.execute(sa.delete(db.config_backups).where(db.config_backups.c.site_id == site_id))
        cameras.delete_server(site_id, c)
        for g in c.execute(sa.select(db.camera_groups).where(db.camera_groups.c.org_id == site["org_id"])).mappings().all():
            members = [m for m in (g["members"] or []) if m.get("site_id") != site_id]
            if len(members) != len(g["members"] or []):
                c.execute(sa.update(db.camera_groups).where(db.camera_groups.c.id == g["id"]).values(members=members, updated_at=time.time()))
        c.execute(sa.delete(db.sites).where(db.sites.c.id == site_id))
    _audit(u, site["org_id"], site_id, f"site removed: {site['name']}")
    return {"ok": True}


def _site_card(s: dict, ctx: dict | None = None) -> dict:
    """A server as the fleet pages show it. `ctx` (from _card_ctx) batches the lookups for a list of servers."""
    ctx = ctx or _card_ctx([s])
    summ = s.get("summary") or {}
    cams_total, cams_online = ctx["cameras"].get(s["id"], (0, 0))
    return {"id": s["id"], "org_id": s["org_id"], "name": s["name"], "location": s["location"], "online": bool(s["online"]) and s["id"] in registry.by_site,
            "last_seen_at": s["last_seen_at"], "version": s["version"], "hostname": s["hostname"], "clock_skew_s": s["clock_skew_s"],
            "summary": summ, "open_alerts": ctx["alerts"].get(s["id"], 0), "retired_at": s.get("retired_at"),
            "location_id": s.get("location_id"), "location_name": ctx["locations"].get(s.get("location_id")),
            "cameras_total": cams_total, "cameras_online": cams_online,
            "central": ctx.get("central", {}).get(s["id"]),   # a central recording instance: {mode, quota_gb, used_gb, ...}
            "direct": direct.info(summ)}   # Direct-on-LAN: lets the UI know whether to ask for a token at all


def _card_ctx(servers: list[dict]) -> dict:
    ids = [s["id"] for s in servers]
    if not ids:
        return {"alerts": {}, "cameras": {}, "locations": {}}
    alerts_by = {r["site_id"]: r["n"] for r in db.rows(sa.select(db.alerts.c.site_id, sa.func.count().label("n")).where(
        db.alerts.c.site_id.in_(ids), db.alerts.c.closed_at.is_(None)).group_by(db.alerts.c.site_id))}
    online = {s["id"] for s in servers if s["online"] and s["id"] in registry.by_site}
    loc_ids = list({s.get("location_id") for s in servers if s.get("location_id")})
    names = {r["id"]: r["name"] for r in db.rows(sa.select(db.locations.c.id, db.locations.c.name).where(db.locations.c.id.in_(loc_ids)))} if loc_ids else {}
    return {"alerts": alerts_by, "cameras": cameras.counts(ids, online), "locations": names, "central": hosts.by_server(ids)}


def _cards(servers: list[dict]) -> list[dict]:
    ctx = _card_ctx(servers)
    return [_site_card(s, ctx) for s in servers]


# ---------------------------------------------------------------- fleet, alerts, audit

@app.get("/api/fleet")
async def fleet(org: str | None = None, include_retired: bool = False, u: dict = Depends(user)):
    orgs = auth.orgs_for(u)
    if org:
        orgs = [o for o in orgs if o["id"] == org]
    out = []
    for o in orgs:
        r = _org_rollup(u, o["id"], include_retired)
        sites = r["sites"]
        out.append({"org": {"id": o["id"], "name": o["name"], "slug": o["slug"], "role": o["role"]}, "sites": sites,
                    "open_alerts": sum(s["open_alerts"] for s in sites if not s["retired_at"]),
                    "retired": r["retired"], "locations": r["locations"], "unassigned": r["unassigned"]})
    return {"orgs": out, "now": time.time(), "offline_after_s": settings.offline_after_s}


def _org_rollup(u: dict, org_id: str, include_retired: bool = False) -> dict:
    """One customer as `u` sees it: server cards (`sites`), Site rollups (`locations`), servers in no visible Site
    (`unassigned`) and the retired count. Shared by /api/fleet and /api/hub/sites so the two never disagree."""
    every = auth.visible_sites(u, org_id, include_retired=True)
    cards = _cards(every)
    sites = [c for c in cards if include_retired or not c["retired_at"]]
    locs = _location_rollups(auth.visible_locations(u, org_id), cards, include_retired)
    known = {loc["id"] for loc in locs}
    return {"sites": sites, "locations": locs, "retired": sum(1 for s in every if s.get("retired_at")),
            "unassigned": [c for c in sites if c["location_id"] not in known]}


def _location_rollups(locs: list[dict], cards: list[dict], include_retired: bool = False) -> list[dict]:
    """Each Site with its servers' cards and the numbers a Sites list shows. Retired servers are counted
    (retired_servers) but left out of `servers` and the totals unless include_retired."""
    out = []
    for loc in locs:
        mine = [c for c in cards if c["location_id"] == loc["id"]]
        live = [c for c in mine if not c["retired_at"]]
        out.append({**{k: loc[k] for k in ("id", "org_id", "name", "address", "timezone", "notes", "created_at", "updated_at")},
                    **geocode.place_of(loc),
                    "monitored": bool(loc.get("monitored")),   # watched by the SOC (soc.py)
                    "servers_total": len(live), "servers_online": sum(1 for c in live if c["online"]),
                    "cameras_total": sum(c["cameras_total"] for c in live), "cameras_online": sum(c["cameras_online"] for c in live),
                    "open_alerts": sum(c["open_alerts"] for c in live), "retired_servers": len(mine) - len(live),
                    "servers": mine if include_retired else live})
    return out


def _locations_for(u: dict, org_id: str, include_retired: bool = False) -> list[dict]:
    return _location_rollups(auth.visible_locations(u, org_id), _cards(auth.visible_sites(u, org_id, include_retired=True)), include_retired)


def _location_tags(org_id: str) -> dict[str, dict]:
    """server id -> {location_id, location_name}, for rows that only carry a server id (alerts, audit)."""
    names = {r["id"]: r["name"] for r in db.rows(sa.select(db.locations.c.id, db.locations.c.name).where(db.locations.c.org_id == org_id))}
    return {r["id"]: {"location_id": r["location_id"], "location_name": names.get(r["location_id"])}
            for r in db.rows(sa.select(db.sites.c.id, db.sites.c.location_id).where(db.sites.c.org_id == org_id))} | \
        {f"l:{lid}": {"location_id": lid, "location_name": name} for lid, name in names.items()}


# ---------------------------------------------------------------- Sites (locations)

GeocodeSource = Literal["nominatim", "census", "geocoder", "marker", "manual"]


class LocationIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    address: str = Field("", max_length=200)
    timezone: str | None = Field(None, max_length=64)
    notes: str | None = Field(None, max_length=2000)
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    address_parts: dict | None = None
    geocode_source: GeocodeSource | None = None


class LocationPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    address: str | None = Field(None, max_length=200)
    timezone: str | None = Field(None, max_length=64)
    notes: str | None = Field(None, max_length=2000)
    lat: float | None = Field(None, ge=-90, le=90)     # lat and lon go together; both null clears the point
    lon: float | None = Field(None, ge=-180, le=180)
    address_parts: dict | None = None
    geocode_source: GeocodeSource | None = None


def _place_vals(vals: dict, current: dict) -> dict:
    """Turn the place fields of a create/patch into column values. Coordinates come in pairs. A new point stamps
    geocoded_at/source; a Site that has a point and would be left without a time zone gets the one at the point.
    A new address text without a new point drops the old point (it described the old address) so the backfill locates
    the new one; address_parts without a point are ignored."""
    has_lat, has_lon = "lat" in vals, "lon" in vals
    if has_lat != has_lon or (has_lat and (vals["lat"] is None) != (vals["lon"] is None)):
        raise HTTPException(422, "lat and lon go together")
    src = vals.pop("geocode_source", None)
    parts = vals.pop("address_parts", None) if "address_parts" in vals else ...
    if has_lat:
        if vals["lat"] is None:
            vals.update(address_parts=None, geocoded_at=None, geocode_source=None)
        else:
            vals["lat"], vals["lon"] = round(float(vals["lat"]), 7), round(float(vals["lon"]), 7)
            vals["geocoded_at"] = time.time()
            vals["geocode_source"] = src or "manual"
            if parts is not ...:
                vals["address_parts"] = geocode.clean_parts(parts)
    elif "address" in vals and (vals["address"] or "").strip() != (current.get("address") or "").strip() and current.get("lat") is not None:
        vals.update(lat=None, lon=None, address_parts=None, geocoded_at=None, geocode_source=None)
    # a located Site never ends up without a time zone: an empty one (kept or sent) comes from the point
    lat, lon = vals.get("lat", current.get("lat")), vals.get("lon", current.get("lon"))
    if lat is not None and lon is not None and not (vals.get("timezone", current.get("timezone")) or "").strip():
        tz = geocode.tz_for(lat, lon)
        if tz:
            vals["timezone"] = tz
    return vals


def _name_taken(org_id: str, name: str, exclude: str | None = None) -> bool:
    q = sa.select(db.locations.c.id, db.locations.c.name).where(db.locations.c.org_id == org_id)
    return any(r["name"].strip().casefold() == name.strip().casefold() and r["id"] != exclude for r in db.rows(q))


@app.get("/api/orgs/{org_id}/locations")
async def list_locations(org_id: str, include_retired: bool = False, u: dict = Depends(user)):
    auth.require_role(u, org_id, "viewer")
    return _locations_for(u, org_id, include_retired)


@app.post("/api/orgs/{org_id}/locations")
async def create_location(org_id: str, body: LocationIn, u: dict = Depends(user)):
    auth.require_role(u, org_id, "admin")
    if _name_taken(org_id, body.name):
        raise HTTPException(409, "a site with that name already exists")
    t = time.time()
    place = _place_vals(body.model_dump(include={"lat", "lon", "address_parts", "geocode_source"}, exclude_unset=True)
                        | ({"timezone": body.timezone} if body.timezone else {}), {})
    loc = {"id": db.new_id("l_"), "org_id": org_id, "name": body.name.strip(), "address": body.address, "timezone": body.timezone,
           "notes": body.notes, "created_at": t, "updated_at": t, **place}
    db.insert(db.locations, loc)
    _audit(u, org_id, None, f"site created: {loc['name']}", {"location_id": loc["id"]})
    if loc["address"].strip() and loc.get("lat") is None:
        geocode.locate_soon(loc["id"])
    return _location_rollups([loc], [])[0]


@app.get("/api/locations/{location_id}")
async def get_location(location_id: str, include_retired: bool = False, u: dict = Depends(user)):
    loc, _ = auth.location_access(u, location_id)
    servers = [s for s in auth.visible_sites(u, loc["org_id"], include_retired=True) if s.get("location_id") == location_id]
    return _location_rollups([loc], _cards(servers), include_retired)[0]


@app.patch("/api/locations/{location_id}")
async def update_location(location_id: str, body: LocationPatch, u: dict = Depends(user)):
    loc, _ = auth.location_access(u, location_id)
    auth.require_role(u, loc["org_id"], "admin")
    vals = _place_vals({k: v for k, v in body.model_dump(exclude_unset=True).items() if k != "name" or v}, loc)
    if "name" in vals:
        vals["name"] = vals["name"].strip()
        if _name_taken(loc["org_id"], vals["name"], exclude=location_id):
            raise HTTPException(409, "a site with that name already exists")
    if vals:
        db.run(sa.update(db.locations).where(db.locations.c.id == location_id).values(**vals, updated_at=time.time()))
        registry.refresh_location(location_id)   # welcome/proxy/broadcasts carry the Site name
        _audit(u, loc["org_id"], None, f"site updated: {vals.get('name', loc['name'])}", {"location_id": location_id})
        if "address" in vals and (vals["address"] or "").strip() and vals.get("lat", loc.get("lat")) is None:
            geocode.locate_soon(location_id)
    return await get_location(location_id, False, u)


# ---------------------------------------------------------------- address lookup (geocode.py)

def _geocode_user(u: dict) -> None:
    if geocode.rate_limited(u["id"]):
        raise HTTPException(429, "too many address lookups; wait a minute")


@app.get("/api/geocode")
async def geocode_search(q: str = Query(min_length=1, max_length=200), u: dict = Depends(user)):
    """Address suggestions: [{display, address_parts, lat, lon, timezone}] (best first; [] when the geocoder is down)."""
    if len(geocode.normalise(q)) < 3:
        return []
    _geocode_user(u)
    return await geocode.search(q)


@app.get("/api/geocode/reverse")
async def geocode_reverse(lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180), u: dict = Depends(user)):
    """The address at a point, or null."""
    _geocode_user(u)
    return await geocode.reverse(lat, lon)


@app.get("/api/geocode/timezone")
async def geocode_timezone(lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180), u: dict = Depends(user)):
    """The IANA time zone at a point (offline; a dragged map marker asks this), or {"timezone": null}."""
    return {"timezone": geocode.tz_for(lat, lon)}


@app.delete("/api/locations/{location_id}")
async def delete_location(location_id: str, move_to: str | None = None, u: dict = Depends(user)):
    """Delete a Site. One that still has servers needs ?move_to=<another Site of the customer>; grants on the deleted
    Site are dropped, not carried over (moving servers must never widen who sees them)."""
    loc, _ = auth.location_access(u, location_id)
    auth.require_customer_role(u, loc["org_id"], "admin")   # deleting a Site is customer management, not SOC config
    servers = db.rows(sa.select(db.sites.c.id).where(db.sites.c.location_id == location_id))
    if db.one(sa.select(db.central_instances.c.id).where(db.central_instances.c.location_id == location_id,
                                                         db.central_instances.c.state.in_(hosts.LIVE_STATES))):
        raise HTTPException(409, "this site has central recording: a hub administrator must remove it first (Servers tab)")
    target = None
    if servers:
        if not move_to:
            raise HTTPException(409, f"this site still has {len(servers)} server(s): move them first (or pass move_to)")
        target = _org_location(loc["org_id"], move_to)
        if not target or target["id"] == location_id:
            raise HTTPException(404, "unknown site to move the servers to")
    with db.engine().begin() as c:
        if target:
            c.execute(sa.update(db.sites).where(db.sites.c.location_id == location_id).values(location_id=target["id"]))
            c.execute(sa.update(db.cameras).where(db.cameras.c.location_id == location_id).values(location_id=target["id"]))
        c.execute(sa.delete(db.location_grants).where(db.location_grants.c.location_id == location_id))
        # the Site's SOC call list and procedures describe this place only: they go with it, not to move_to
        c.execute(sa.delete(db.location_contacts).where(db.location_contacts.c.location_id == location_id))
        c.execute(sa.delete(db.location_procedures).where(db.location_procedures.c.location_id == location_id))
        find_mod.drop_views(c, location_id)
        c.execute(sa.delete(db.locations).where(db.locations.c.id == location_id))
    for srv in servers:
        registry.refresh(srv["id"])
    if loc.get("monitored"):
        soc.invalidate()
    _audit(u, loc["org_id"], None, f"site deleted: {loc['name']}" + (f" (servers moved to {target['name']})" if target else ""),
           {"location_id": location_id, "moved_to": target["id"] if target else None})
    return {"ok": True, "moved": len(servers)}


@app.get("/api/locations/{location_id}/cameras")
async def location_cameras(location_id: str, u: dict = Depends(user)):
    """The registry's cameras for this Site's servers: `online` = stream ready on an online server; missing and
    disabled cameras are included (missing_since / enabled say so)."""
    loc, _ = auth.location_access(u, location_id)
    servers = {s["id"]: s for s in auth.visible_sites(u, loc["org_id"]) if s.get("location_id") == location_id}
    out = []
    for cam in cameras.for_location(location_id):
        srv = servers.get(cam["server_id"])
        if srv is None:
            continue   # retired server (or a stale denormalized row)
        up = bool(srv["online"]) and srv["id"] in registry.by_site
        out.append({**cam, "server_name": srv["name"], "server_online": up,
                    "online": up and bool(cam["stream_ready"]) and bool(cam["enabled"]) and not cam["missing_since"]})
    return out


def _location_scope(u: dict, org_id: str, location_id: str | None) -> set[str] | None:
    """?location= on an org-wide route: that Site's servers the user may see (None = no filter). A Site of another
    customer is a 404, an ungranted one a 403 (auth.location_access)."""
    if not location_id:
        return None
    loc, _ = auth.location_access(u, location_id)
    if loc["org_id"] != org_id:
        raise HTTPException(404, "unknown site")
    return _servers_of(u, loc)


def _servers_of(u: dict, loc: dict) -> set[str]:
    return {s["id"] for s in auth.visible_sites(u, loc["org_id"]) if s.get("location_id") == loc["id"]}


def _alert_rows(u: dict, org: str, open: bool, limit: int, within: set[str] | None = None) -> list[dict]:
    site_ids = [s["id"] for s in auth.visible_sites(u, org) if within is None or s["id"] in within]
    if open:
        rows = alerts.open_for_org(org, site_ids, limit)
    else:
        rows = db.rows(sa.select(db.alerts).where(db.alerts.c.org_id == org, db.alerts.c.site_id.in_(site_ids)).order_by(db.alerts.c.opened_at.desc()).limit(limit))
    names = {s["id"]: s["name"] for s in db.rows(sa.select(db.sites.c.id, db.sites.c.name).where(db.sites.c.org_id == org))}
    tags = _location_tags(org)
    for r in rows:
        r["site_name"] = names.get(r["site_id"], r["site_id"])
        r |= tags.get(r["site_id"], {"location_id": None, "location_name": None})
    return rows


@app.get("/api/alerts")
async def list_alerts(org: str, open: bool = True, limit: int = Query(200, le=1000), location: str | None = None, u: dict = Depends(user)):
    auth.require_role(u, org, "viewer")
    return _alert_rows(u, org, open, limit, _location_scope(u, org, location))


@app.get("/api/locations/{location_id}/alerts")
async def location_alerts(location_id: str, open: bool = True, limit: int = Query(200, le=1000), u: dict = Depends(user)):
    loc, _ = auth.location_access(u, location_id)
    return _alert_rows(u, loc["org_id"], open, limit, _servers_of(u, loc))


@app.get("/api/locations/{location_id}/events")
async def location_events(location_id: str, cameras: str | None = None, classes: str | None = None,
                          limit: int = Query(20, ge=1, le=100), since: float | None = None, u: dict = Depends(user)):
    """The latest events across this Site's servers (same shape and params as /api/fleet/events)."""
    loc, _ = auth.location_access(u, location_id)
    return await dashboards.fleet_events(u, loc["org_id"], None, _cam_refs(cameras), None,
                                         [c for c in (classes or "").split(",") if c] or None, limit, since, within=_servers_of(u, loc))


@app.get("/api/locations/{location_id}/search")
async def location_search(location_id: str, q: str = Query(min_length=1, max_length=200), since: float | None = None,
                          until: float | None = None, u: dict = Depends(user)):
    loc, _ = auth.location_access(u, location_id)
    return await fleet_mod.search(u, loc["org_id"], q, since, until, server_ids=_servers_of(u, loc))


# ---- a Site's Find tab (find.py): the server UI's browse / search filters across the Site's servers, paged per server

def _find_servers(u: dict, loc: dict) -> list[dict]:
    """The Site's servers this user sees, in a stable order (name, then id): the merge's tie-break."""
    return sorted((s for s in auth.visible_sites(u, loc["org_id"]) if s.get("location_id") == loc["id"]),
                  key=lambda s: (s["name"].casefold(), s["id"]))


async def _site_find(location_id: str, u: dict, path: str, params: dict, camera: str | None, cursor: str | None,
                     limit: int, sort: str) -> dict:
    loc, _ = auth.location_access(u, location_id)
    servers = _find_servers(u, loc)
    ids = {s["id"] for s in servers}
    try:
        cur = find_mod.parse_cursor(cursor, ids)
        cam = find_mod.camera_param(camera, ids)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except LookupError:
        return {"events": [], "next": None, "offline": [], "errors": []}   # a camera of another Site: nothing
    flags_ok = {"rule", "ppe", "unusual", "watched", "multicam", "locked", "corrected", "false_alarm"}
    if params.get("flags") and not set(params["flags"].split(",")) <= flags_ok:
        raise HTTPException(400, "unknown flag")
    return await find_mod.site_find(u, loc["org_id"], servers, path, params, cur, limit, sort, cam)


@app.get("/api/locations/{location_id}/find/events")
async def location_find_events(location_id: str, camera: str | None = Query(None, max_length=120), status: str | None = Query(None, max_length=60),
                               label: str | None = Query(None, max_length=20), since: float | None = None, until: float | None = None,
                               min_yolo: float = Query(0, ge=0, le=1), priority: Literal["low", "medium", "high"] | None = None,
                               flags: str | None = Query(None, max_length=200), place: str | None = Query(None, max_length=120),
                               ppe_zone: str | None = Query(None, max_length=120), attention: bool = False,
                               sort: Literal["newest", "priority"] = "newest", limit: int = Query(60, ge=1, le=200),
                               cursor: str | None = Query(None, max_length=4000), u: dict = Depends(user)):
    """Browse a Site's events with a server's /api/events filters (camera = <server>:<camera>). Newest first (by
    start time across servers) or by priority; `next` is the cursor for the following page (null = that's all)."""
    params = {"status": status, "label": label, "since": since, "until": until, "min_yolo": min_yolo or None, "priority": priority,
              "flags": flags, "place": place, "ppe_zone": ppe_zone, "attention": "true" if attention else None, "sort": sort}
    return await _site_find(location_id, u, "/api/events", params, camera, cursor, limit, sort)


@app.get("/api/locations/{location_id}/find/search")
async def location_find_search(location_id: str, q: str = Query(min_length=1, max_length=200), camera: str | None = Query(None, max_length=120),
                               status: str | None = Query(None, max_length=60), label: str | None = Query(None, max_length=20),
                               since: float | None = None, until: float | None = None, min_yolo: float = Query(0, ge=0, le=1),
                               priority: Literal["low", "medium", "high"] | None = None, flags: str | None = Query(None, max_length=200),
                               place: str | None = Query(None, max_length=120), ppe_zone: str | None = Query(None, max_length=120),
                               attention: bool = False, limit: int = Query(100, ge=1, le=200),
                               cursor: str | None = Query(None, max_length=4000), u: dict = Depends(user)):
    """Search a Site's events by meaning (each server's /api/search with the same filters), merged by relevance."""
    params = {"q": q, "status": status, "label": label, "since": since, "until": until, "min_yolo": min_yolo or None, "priority": priority,
              "flags": flags, "place": place, "ppe_zone": ppe_zone, "attention": "true" if attention else None}
    return await _site_find(location_id, u, "/api/search", params, camera, cursor, limit, "score")


def _can_edit_views(u: dict, org_id: str) -> bool:
    role = auth.role_in(u, org_id)
    return bool(role) and allows(role, "operator")


class FindViewsIn(BaseModel):
    views: list[dict] = Field(max_length=find_mod.MAX_VIEWS)


@app.get("/api/locations/{location_id}/find-views")
async def get_location_find_views(location_id: str, u: dict = Depends(user)):
    """The Site's saved Find views (shared by everyone who sees the Site), shaped like a server's /api/find/views."""
    loc, _ = auth.location_access(u, location_id)
    return {"views": find_mod.get_views(loc["id"]), "can_edit": _can_edit_views(u, loc["org_id"])}


@app.put("/api/locations/{location_id}/find-views")
async def put_location_find_views(location_id: str, body: FindViewsIn, u: dict = Depends(user)):
    loc, _ = auth.location_access(u, location_id)
    auth.require_role(u, loc["org_id"], "operator")
    try:
        views = find_mod.clean_views(body.views)
    except ValueError as e:
        raise HTTPException(400, str(e))
    before = {v["id"]: v["name"] for v in find_mod.get_views(loc["id"])}
    find_mod.set_views(loc["id"], views)
    after = {v["id"]: v["name"] for v in views}
    _audit(u, loc["org_id"], None, f"site find views saved: {loc['name']}",
           {"location_id": loc["id"], "views": len(views), "added": sorted(after.keys() - before.keys()), "removed": sorted(before.keys() - after.keys())})
    return {"views": views, "can_edit": True}


@app.get("/api/locations/{location_id}/backups")
async def location_backups(location_id: str, u: dict = Depends(user)):
    """The newest config backup of each of this Site's servers (admins, like the per-server list)."""
    loc, _ = auth.location_access(u, location_id)
    auth.require_role(u, loc["org_id"], "admin")
    servers = [s for s in auth.visible_sites(u, loc["org_id"]) if s.get("location_id") == location_id]
    latest = backups.latest_for([s["id"] for s in servers])
    return [{"server_id": s["id"], "server_name": s["name"], "online": bool(s["online"]) and s["id"] in registry.by_site,
             "latest": latest.get(s["id"], {}).get("latest"), "count": latest.get(s["id"], {}).get("count", 0)} for s in servers]


@app.post("/api/alerts/{alert_id}/ack")
async def ack_alert(alert_id: int, u: dict = Depends(user)):
    a = db.one(sa.select(db.alerts).where(db.alerts.c.id == alert_id))
    if not a:
        raise HTTPException(404)
    auth.require_role(u, a["org_id"], "viewer")
    see = auth.scope_filter(u, a["org_id"])
    if see is not None and not see({"site_id": a["site_id"]}):
        raise HTTPException(404)   # a server on a Site this member isn't granted: as if it didn't exist
    alerts.ack(alert_id, u["id"])
    return {"ok": True}


@app.get("/api/audit")
async def audit(org: str, site: str | None = None, since: float | None = None, limit: int = Query(200, le=2000), u: dict = Depends(user)):
    auth.require_customer_role(u, org, "admin")
    see = auth.scope_filter(u, org)   # a Site-restricted admin reads the rows of their own servers and Sites only
    if site and see is not None and not see({"site_id": site}):
        raise HTTPException(403, "not granted this site")
    q = sa.select(db.audit_log).where(db.audit_log.c.org_id == org).order_by(db.audit_log.c.ts.desc(), db.audit_log.c.id.desc())
    if site:
        q = q.where(db.audit_log.c.site_id == site)
    if since:
        q = q.where(db.audit_log.c.ts >= since)
    if see is None:
        rows = db.rows(q.limit(limit))
    else:
        rows, offset, page = [], 0, max(limit, 200)
        while len(rows) < limit:
            batch = db.rows(q.limit(page).offset(offset))
            rows += [r for r in batch if see(r)]
            if len(batch) < page:
                break
            offset += page
        rows = rows[:limit]
    tags = _location_tags(org)
    for r in rows:
        r["undo_until"] = fleet_actions.undo_until(r)   # fleet actions: Undo is offered on the row for 24 h
        lid = (r.get("detail") or {}).get("location_id") if isinstance(r.get("detail"), dict) else None
        r |= tags.get(r["site_id"]) or tags.get(f"l:{lid}") or {"location_id": lid, "location_name": None}
    return rows


def _audit(u: dict, org_id: str | None, site_id: str | None, action: str, detail: dict | None = None) -> None:
    db.insert(db.audit_log, {"ts": time.time(), "user_id": u["id"], "user_email": u["email"], "org_id": org_id, "site_id": site_id,
                             "action": action, "method": None, "path": None, "status": None, "ip": None, "detail": detail or {}})


# ---------------------------------------------------------------- hub administrators (users.is_super)
# Hub-wide, not per customer: every route here is for hub administrators only (403 otherwise). SOC staff
# (users.soc_role) are managed the same way at /api/hub/soc/members (soc_api.py). Granting needs an
# existing account (invite the person to a customer first); the hub never creates a login from this screen.

class HubAdminIn(BaseModel):
    email: EmailStr


@app.get("/api/hub/admins")
async def list_hub_admins(u: dict = Depends(user)):
    auth.require_super(u)
    return auth.hub_admins()


@app.post("/api/hub/admins")
async def add_hub_admin(body: HubAdminIn, u: dict = Depends(user)):
    auth.require_super(u)
    try:
        target, _ = auth.set_super(body.email, True, actor=u)
    except LookupError:
        raise HTTPException(404, "no account with that email: invite them to a customer (or create them) first")
    return {"id": target["id"], "email": target["email"], "totp_enabled": bool(target["totp_enabled"]), "last_login_at": target["last_login_at"]}


@app.delete("/api/hub/admins/{uid}")
async def remove_hub_admin(uid: str, u: dict = Depends(user)):
    auth.require_super(u)
    target = auth.user_by_id(uid)
    if not target or not target["is_super"]:
        raise HTTPException(404, "not a hub administrator")
    try:
        auth.set_super(target["email"], False, actor=u)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.get("/api/hub/audit")
async def hub_audit(since: float | None = None, limit: int = Query(200, le=2000), u: dict = Depends(user)):
    """Hub-level audit rows (org_id NULL: hub administrators granted/revoked). Per-customer rows stay on /api/audit."""
    auth.require_super(u)
    q = sa.select(db.audit_log).where(db.audit_log.c.org_id.is_(None)).order_by(db.audit_log.c.ts.desc()).limit(limit)
    if since:
        q = q.where(db.audit_log.c.ts >= since)
    return db.rows(q)


@app.get("/api/hub/sites")
async def hub_sites(include_retired: bool = False, u: dict = Depends(user)):
    """Every customer's Sites at once (the "All customers" view): [{org, locations, unassigned}], customers by name,
    each with the same rollups as /api/orgs/{org}/locations."""
    auth.require_super(u)
    out = []
    for o in auth.orgs_for(u):
        r = _org_rollup(u, o["id"], include_retired)
        out.append({"org": {"id": o["id"], "name": o["name"]}, "locations": r["locations"], "unassigned": r["unassigned"]})
    return out


# ---------------------------------------------------------------- central recording (hosts.py)
# Hosts and placement are for hub administrators only. A Site's admins may read its central instance (mode, subnet or
# public IP, and the Peplink settings sheet) to set up the BR1; everyone else gets what location_access gives.

class HostIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    notes: str | None = Field(None, max_length=2000)
    fusionhub: str | None = Field(None, max_length=200)


class HostPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    notes: str | None = Field(None, max_length=2000)
    fusionhub: str | None = Field(None, max_length=200)


def _host_or_404(host_id: str) -> dict:
    h = db.one(sa.select(db.hosts).where(db.hosts.c.id == host_id))
    if not h:
        raise HTTPException(404, "unknown host")
    return h


@app.get("/api/hub/hosts")
async def list_hosts(u: dict = Depends(user)):
    auth.require_super(u)
    return {"hosts": hosts.list_hosts(), "install": hosts.install_command(), "host_agent_url": hosts.host_agent_url()}


@app.post("/api/hub/hosts")
async def create_host(body: HostIn, u: dict = Depends(user)):
    """The token is in this answer only (stored hashed): it goes in the host's token file."""
    auth.require_super(u)
    row, token = hosts.create_host(body.name, (body.notes or "").strip() or None, (body.fusionhub or "").strip() or None)
    _audit(u, None, None, f"host added: {row['name']}", {"host_id": row["id"]})
    return {"host": hosts.host_out(row), "token": token, "install": hosts.install_command()}


@app.patch("/api/hub/hosts/{host_id}")
async def update_host(host_id: str, body: HostPatch, u: dict = Depends(user)):
    auth.require_super(u)
    h = _host_or_404(host_id)
    vals = {k: (v.strip() or None) if isinstance(v, str) else v for k, v in body.model_dump(exclude_unset=True).items()}
    if "name" in vals and not vals["name"]:
        raise HTTPException(400, "a host needs a name")
    if vals:
        db.run(sa.update(db.hosts).where(db.hosts.c.id == host_id).values(**vals))
        _audit(u, None, None, f"host updated: {h['name']}", {"host_id": host_id, "fields": sorted(vals)})
    return hosts.host_out(_host_or_404(host_id), db.rows(sa.select(db.central_instances).where(db.central_instances.c.host_id == host_id)))


@app.delete("/api/hub/hosts/{host_id}")
async def delete_host(host_id: str, u: dict = Depends(user)):
    auth.require_super(u)
    h = _host_or_404(host_id)
    try:
        await hosts.delete_host(host_id)
    except hosts.Conflict as e:
        raise HTTPException(409, str(e))
    _audit(u, None, None, f"host removed: {h['name']}", {"host_id": host_id})
    return {"ok": True}


@app.post("/api/hub/hosts/{host_id}/rotate-token")
async def rotate_host_token(host_id: str, u: dict = Depends(user)):
    """A new token (shown once); the host is disconnected until its agent runs with the new token file."""
    auth.require_super(u)
    h = _host_or_404(host_id)
    token = await hosts.rotate_host_token(host_id)
    _audit(u, None, None, f"host token rotated: {h['name']}", {"host_id": host_id})
    return {"token": token, "install": hosts.install_command()}


@app.get("/api/hub/central")
async def all_central(include_deleted: bool = False, u: dict = Depends(user)):
    auth.require_super(u)
    return hosts.instances(include_deleted=include_deleted)


class CentralIn(BaseModel):
    host_id: str | None = Field(None, max_length=24)     # None = the hub picks the host with the most room
    mode: Literal["vpn", "forward"] = "vpn"
    subnet: str | None = Field(None, max_length=43)      # VPN mode: default 10.20.<site number>.0/24
    public_ip: str | None = Field(None, max_length=253)  # port-forward mode: the BR1's public address
    quota_gb: int = Field(ge=10, le=1_000_000)   # the host agent refuses less than 10 GB
    gpu: int | None = Field(None, ge=0, le=64)           # None = the hub picks (an A10 first, the A40 only if alone)
    name: str | None = Field(None, max_length=120)       # the server's name in the Site; default "Central"


class CentralPatch(BaseModel):
    quota_gb: int = Field(ge=10, le=1_000_000)   # the host agent refuses less than 10 GB


def _central_of(location_id: str, ci_id: str) -> dict:
    ci = hosts.get_instance(ci_id)
    if not ci or ci["location_id"] != location_id:
        raise HTTPException(404, "unknown central instance")
    return ci


@app.get("/api/locations/{location_id}/central")
async def location_central(location_id: str, u: dict = Depends(user)):
    """The Site's central instance and the Peplink settings sheet: Site admins (and hub administrators)."""
    loc, _ = auth.location_access(u, location_id)
    auth.require_role(u, loc["org_id"], "admin")
    rows = hosts.instances(db.central_instances.c.location_id == location_id, hub_admin=bool(u.get("is_super")))
    servers = [r["server_id"] for r in rows if r["server_id"]]
    cams: dict[str, list] = {}
    if servers:
        for c in db.rows(sa.select(db.cameras.c.server_id, db.cameras.c.camera_id, db.cameras.c.name).where(
                db.cameras.c.server_id.in_(servers), db.cameras.c.missing_since.is_(None)).order_by(db.cameras.c.first_seen_at)):
            cams.setdefault(c["server_id"], []).append({"id": c["camera_id"], "name": c["name"]})
    return {"instances": [{**r, "cameras": cams.get(r["server_id"] or "", [])} for r in rows], "can_provision": bool(u.get("is_super")),
            "hosts": [{"id": h["id"], "name": h["name"], "online": h["online"], "capacity": h["capacity"], "instances": h["instances"]}
                      for h in hosts.list_hosts()] if u.get("is_super") else []}


@app.post("/api/locations/{location_id}/central")
async def location_central_create(location_id: str, body: CentralIn, u: dict = Depends(user)):
    """Provision: answers at once with the instance (phase provisioning); the page polls GET until it is running."""
    auth.require_super(u)
    auth.location_access(u, location_id)
    try:
        ci = await hosts.provision(location_id, body.host_id or None, body.mode, body.subnet, body.public_ip, body.quota_gb, u,
                                   name=body.name, gpu=body.gpu)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except hosts.Conflict as e:
        raise HTTPException(409, str(e))
    return hosts.instances(db.central_instances.c.id == ci["id"])[0]


@app.patch("/api/locations/{location_id}/central/{ci_id}")
async def location_central_update(location_id: str, ci_id: str, body: CentralPatch, u: dict = Depends(user)):
    auth.require_super(u)
    _central_of(location_id, ci_id)
    try:
        await hosts.set_quota(ci_id, body.quota_gb, u)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except hosts.HostError as e:
        raise HTTPException(502, str(e))
    return hosts.instances(db.central_instances.c.id == ci_id, include_deleted=True)[0]


@app.delete("/api/locations/{location_id}/central/{ci_id}")
async def location_central_delete(location_id: str, ci_id: str, purge: bool = False, force: bool = False, u: dict = Depends(user)):
    """Delete the instance on its host and retire its server. Recordings stay on the host unless purge; force forgets
    it at the hub even when the host is offline or refuses."""
    auth.require_super(u)
    _central_of(location_id, ci_id)
    try:
        await hosts.deprovision(ci_id, u, purge=purge, force=force)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except hosts.HostError as e:
        raise HTTPException(502, f"{e} (remove anyway to forget it at the hub)")
    return hosts.instances(db.central_instances.c.id == ci_id, include_deleted=True)[0]


# ---------------------------------------------------------------- fleet find / ask, digests, backups, push

@app.get("/api/fleet/search")
async def fleet_search(org: str, q: str = Query(min_length=1, max_length=200), since: float | None = None, until: float | None = None,
                       location: str | None = None, u: dict = Depends(user)):
    auth.require_role(u, org, "viewer")
    return await fleet_mod.search(u, org, q, since, until, server_ids=_location_scope(u, org, location))


class FleetAskIn(BaseModel):
    org: str
    message: str = Field(min_length=1, max_length=2000)
    location: str | None = Field(None, max_length=24)   # ask only this Site's servers


@app.post("/api/fleet/ask")
async def fleet_ask(body: FleetAskIn, u: dict = Depends(user)):
    auth.require_role(u, body.org, "viewer")
    scope = _location_scope(u, body.org, body.location)
    return StreamingResponse(fleet_mod.ask(u, body.org, body.message, server_ids=scope), media_type="application/x-ndjson")


class ActionPlanIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    # where the plan was made: only "actions_page" (Customer › Actions) plans can be executed (fleet_actions.gate)
    origin: str | None = Field(None, max_length=40)


class ActionExecIn(BaseModel):
    plan_id: str | None = Field(None, max_length=40)
    plan: dict | None = None    # no longer accepted (refused and logged): fleet actions run only from Actions-page plans
    confirm_name: str | None = Field(None, max_length=200)   # migrate / retire: the source site's name, typed on the card
    options: dict | None = None  # the card's ticks: {copy_history, skip_stream_check}
    camera: dict | None = None   # add_camera: password, username, paths, ports. Forwarded to the site, never stored or logged


def _verb_role(action: str) -> str:
    return fleet_actions.VERBS.get(action, {}).get("role", "admin")


@app.post("/api/orgs/{org_id}/actions/plan")
async def action_plan(org_id: str, body: ActionPlanIn, u: dict = Depends(user)):
    """Is this text a fleet instruction? {"action": "none"} if not; else the plan and its confirmation card, with
    `parser` ("ai" / "rules"). No side effects, not logged. Anyone in the org may see a card; only admins (operators
    for lock_footage) can confirm it, and only a plan made with origin "actions_page" by the same user."""
    role = auth.require_role(u, org_id, "viewer")
    origin = body.origin if body.origin == fleet_actions.ORIGIN else None
    out = await fleet_actions.plan_for(u, org_id, body.text, origin)
    return out | {"allowed": auth.allows(role, _verb_role(out["action"]))} if out["action"] != "none" else out


@app.post("/api/orgs/{org_id}/actions/execute")
async def action_execute(org_id: str, body: ActionExecIn, u: dict = Depends(user)):
    """Run a confirmed plan. Every attempt is written to the audit log: done, failed (why) or refused (why: not made
    on the Actions page, someone else's plan, expired or already run, role, Site scope, typed name, busy server,
    rate limit). See fleet_actions.gate."""
    role = auth.require_role(u, org_id, "viewer")   # members only; the verb's own role is checked (and logged) in gate
    try:
        try:
            p = fleet_actions.gate(u, org_id, role, body.plan_id, explicit=body.plan is not None, confirm_name=body.confirm_name)
        except fleet_actions.Refused as e:
            raise HTTPException(e.status, e.message)
        try:
            return await fleet_actions.execute(p, u, {"options": body.options or {}, "camera": body.camera})
        except fleet_actions.ActionError as e:
            fleet_actions.refuse(u, org_id, 409, str(e), p)
            raise HTTPException(409, str(e))
    finally:
        body.camera = None


@app.post("/api/orgs/{org_id}/actions/undo/{audit_id}")
async def action_undo(org_id: str, audit_id: int, u: dict = Depends(user)):
    """Run the reverse plan stored with a fleet action's audit row (within 24 h, once). Admins may undo any action
    they can see; operators their own (a footage lock). Every attempt is logged."""
    role = auth.require_role(u, org_id, "operator")
    see = auth.scope_filter(u, org_id)
    if see is not None:
        row = db.one(sa.select(db.audit_log.c.site_id, db.audit_log.c.detail).where(db.audit_log.c.id == audit_id, db.audit_log.c.org_id == org_id))
        if not row or not see(row):
            raise HTTPException(404, "no such fleet action")
    try:
        return await fleet_actions.undo(u, org_id, audit_id, role)
    except LookupError:
        raise HTTPException(404, "no such fleet action")
    except fleet_actions.Refused as e:
        raise HTTPException(e.status, e.message)
    except fleet_actions.ActionError as e:
        raise HTTPException(409, str(e))


@app.get("/api/orgs/{org_id}/actions/reference")
async def action_reference(org_id: str, u: dict = Depends(user)):
    """Every instruction the hub understands (from fleet_actions.VERBS), the safety rules, and the Action log: the
    customer's last 50 fleet actions for admins, the user's own for everyone else, narrowed to their Site scope."""
    role = auth.require_role(u, org_id, "viewer")
    out = fleet_actions.reference(u, org_id, role)
    see = auth.scope_filter(u, org_id)
    if see is not None and out["recent"]:   # a Site-restricted member: only actions on servers they can see
        tags = {r["id"]: r for r in db.rows(sa.select(db.audit_log.c.id, db.audit_log.c.site_id, db.audit_log.c.detail)
                                             .where(db.audit_log.c.id.in_([x["id"] for x in out["recent"]])))}
        out["recent"] = [r for r in out["recent"] if see(tags.get(r["id"]) or {})]
    return out


@app.get("/api/orgs/{org_id}/digests")
async def org_digests(org_id: str, limit: int = Query(7, le=60), u: dict = Depends(user)):
    """The customer's digests. A Site-restricted member gets each one cut down to the servers they can see (the
    per-server data, and a plain-text note rebuilt from it in place of the whole customer's summary)."""
    auth.require_role(u, org_id, "viewer")
    rows = digest.latest(org_id, limit)
    see = auth.scope_filter(u, org_id)
    return rows if see is None else [digest.scoped(r, see) for r in rows]


@app.post("/api/orgs/{org_id}/digests/generate")
async def org_digest_now(org_id: str, u: dict = Depends(user)):
    """Write a digest now (it asks every server of the customer and may use the shared AI): admins who see every Site."""
    auth.require_role(u, org_id, "admin")
    if auth.scope_filter(u, org_id) is not None:
        raise HTTPException(403, "the digest covers every Site of the customer: an admin with access to all Sites generates it")
    return await digest.generate(org_id)


@app.get("/api/sites/{site_id}/backups")
@app.get("/api/servers/{site_id}/backups")
async def site_backups(site_id: str, u: dict = Depends(user)):
    site, _ = auth.site_access(u, site_id)
    auth.require_role(u, site["org_id"], "admin")
    return backups.list_for(site_id)


@app.post("/api/sites/{site_id}/backups")
@app.post("/api/servers/{site_id}/backups")
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
@app.get("/api/servers/{site_id}/backups/{backup_id}")
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
@app.post("/api/servers/{site_id}/backups/{backup_id}/restore")
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
            # soc_incident is opt-in (push.INCIDENT_KIND); host_offline is offered to hub administrators only
            "kinds": [*push.CUSTOMER_KINDS, *(alerts.HUB_KINDS if u.get("is_super") else ())]}


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


# ---------------------------------------------------------------- home dashboards, camera groups, fleet events

class DashboardIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    config: dict
    shared: bool = False


class DashboardPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    config: dict | None = None
    shared: bool | None = None


class DefaultIn(BaseModel):
    id: str | None = None


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    members: list[dict] = Field(default_factory=list)


class GroupPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    members: list[dict] | None = None


@app.get("/api/orgs/{org_id}/dashboards")
async def list_dashboards(org_id: str, u: dict = Depends(user)):
    auth.require_role(u, org_id, "viewer")
    return {"dashboards": dashboards.list_for(u, org_id), "default_id": dashboards.default_id(u, org_id),
            "generated": dashboards.default_config(u, org_id)}


@app.put("/api/orgs/{org_id}/dashboards/default")
async def set_default_dashboard(org_id: str, body: DefaultIn, u: dict = Depends(user)):
    auth.require_role(u, org_id, "viewer")
    if body.id and not dashboards.get(u, org_id, body.id):
        raise HTTPException(404, "no such dashboard")
    dashboards.set_default(u, org_id, body.id)
    return {"default_id": body.id}


@app.get("/api/orgs/{org_id}/dashboards/{dash_id}")
async def get_dashboard(org_id: str, dash_id: str, u: dict = Depends(user)):
    auth.require_role(u, org_id, "viewer")
    row = dashboards.get(u, org_id, dash_id)
    if not row:
        raise HTTPException(404, "no such dashboard")
    return dashboards._public(row) | {"can_edit": dashboards.can_edit(u, org_id, row)}


@app.post("/api/orgs/{org_id}/dashboards")
async def create_dashboard(org_id: str, body: DashboardIn, u: dict = Depends(user)):
    auth.require_role(u, org_id, "admin" if body.shared else "viewer")
    try:
        row = dashboards.create(u, org_id, body.name, body.config, body.shared)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if body.shared:
        _audit(u, org_id, None, f"dashboard.publish {row['name']}")
    return row | {"can_edit": True}


@app.put("/api/orgs/{org_id}/dashboards/{dash_id}")
async def update_dashboard(org_id: str, dash_id: str, body: DashboardPatch, u: dict = Depends(user)):
    auth.require_role(u, org_id, "viewer")
    row = dashboards.get(u, org_id, dash_id)
    if not row:
        raise HTTPException(404, "no such dashboard")
    if not dashboards.can_edit(u, org_id, row):
        raise HTTPException(403, "shared dashboards are edited by admins; save your own copy instead")
    if body.shared is not None and body.shared != row["shared"]:
        auth.require_role(u, org_id, "admin")
    try:
        out = dashboards.update(u, org_id, row, body.name, body.config, body.shared)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if out["shared"] or row["shared"]:
        _audit(u, org_id, None, f"dashboard.{'publish' if body.shared and not row['shared'] else 'unpublish' if body.shared is False and row['shared'] else 'update'} {out['name']}")
    return out | {"can_edit": dashboards.can_edit(u, org_id, {**row, **out})}


@app.delete("/api/orgs/{org_id}/dashboards/{dash_id}")
async def delete_dashboard(org_id: str, dash_id: str, u: dict = Depends(user)):
    auth.require_role(u, org_id, "viewer")
    row = dashboards.get(u, org_id, dash_id)
    if not row:
        raise HTTPException(404, "no such dashboard")
    if not dashboards.can_edit(u, org_id, row):
        raise HTTPException(403, "only admins delete shared dashboards")
    dashboards.delete(row)
    if row["shared"]:
        _audit(u, org_id, None, f"dashboard.delete {row['name']}")
    return {"ok": True}


@app.get("/api/orgs/{org_id}/groups")
async def list_groups(org_id: str, u: dict = Depends(user)):
    auth.require_role(u, org_id, "viewer")
    groups = dashboards.groups_for(org_id)
    see = auth.scope_filter(u, org_id)
    if see is None:
        return groups
    out = []
    for g in groups:   # a Site-restricted member: only cameras on servers they can see; a group with none is hidden
        mine = [m for m in g["members"] or [] if see({"site_id": m.get("site_id")})]
        if mine or not g["members"]:
            out.append({**g, "members": mine})
    return out


def _group_members_in_scope(u: dict, org_id: str, members: list, existing: list[dict] | None = None) -> list:
    """A Site-restricted admin may only name cameras they can see; the group's cameras on other Sites (which they
    were never shown) are kept as they were."""
    see = auth.scope_filter(u, org_id)
    if see is None:
        return members
    for m in members or []:
        site = (m.get("site") or m.get("site_id")) if isinstance(m, dict) else None
        if not see({"site_id": site or "?"}):
            raise HTTPException(403, "not granted that server")
    hidden = [{"site": m["site_id"], "camera": m["camera_id"]} for m in existing or [] if not see({"site_id": m.get("site_id")})]
    return [*(members or []), *hidden]


@app.post("/api/orgs/{org_id}/groups")
async def create_group(org_id: str, body: GroupIn, u: dict = Depends(user)):
    auth.require_role(u, org_id, "admin")
    try:
        row = dashboards.create_group(u, org_id, body.name, _group_members_in_scope(u, org_id, body.members))
    except ValueError as e:
        raise HTTPException(422, str(e))
    _audit(u, org_id, None, f"group.create {row['name']}")
    return row


@app.put("/api/orgs/{org_id}/groups/{group_id}")
async def update_group(org_id: str, group_id: str, body: GroupPatch, u: dict = Depends(user)):
    auth.require_role(u, org_id, "admin")
    see = auth.scope_filter(u, org_id)
    members = body.members
    if see is not None:
        old = db.one(sa.select(db.camera_groups).where(db.camera_groups.c.id == group_id, db.camera_groups.c.org_id == org_id))
        if not old or (old["members"] and not any(see({"site_id": m.get("site_id")}) for m in old["members"])):
            raise HTTPException(404, "no such group")   # hidden from them in the list, so not theirs to edit
        if members is not None:
            members = _group_members_in_scope(u, org_id, members, old["members"])
    try:
        row = dashboards.update_group(org_id, group_id, body.name, members)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if not row:
        raise HTTPException(404, "no such group")
    _audit(u, org_id, None, f"group.update {row['name']}")
    return row


@app.delete("/api/orgs/{org_id}/groups/{group_id}")
async def delete_group(org_id: str, group_id: str, u: dict = Depends(user)):
    auth.require_role(u, org_id, "admin")
    see = auth.scope_filter(u, org_id)
    if see is not None:
        old = db.one(sa.select(db.camera_groups).where(db.camera_groups.c.id == group_id, db.camera_groups.c.org_id == org_id))
        if old and old["members"] and not all(see({"site_id": m.get("site_id")}) for m in old["members"]):
            raise HTTPException(403, "this group includes cameras on Sites you can't see")
    if not dashboards.delete_group(org_id, group_id):
        raise HTTPException(404, "no such group")
    _audit(u, org_id, None, f"group.delete {group_id}")
    return {"ok": True}


def _cam_refs(cameras: str | None) -> list[tuple[str, str]] | None:
    """?cameras=site:cam,site:cam"""
    if not cameras:
        return None
    out = []
    for part in cameras.split(","):
        if ":" in part:
            site, cam = part.split(":", 1)
            out.append((site.strip(), cam.strip()))
    return out or None


@app.get("/api/fleet/events")
async def fleet_events(org: str, sites: str | None = None, cameras: str | None = None, group: str | None = None,
                       classes: str | None = None, limit: int = Query(20, ge=1, le=500), since: float | None = None,
                       location: str | None = None, u: dict = Depends(user)):
    auth.require_role(u, org, "viewer")
    return await dashboards.fleet_events(u, org, [s for s in (sites or "").split(",") if s] or None, _cam_refs(cameras), group,
                                         [c for c in (classes or "").split(",") if c] or None, limit, since,
                                         within=_location_scope(u, org, location))


@app.websocket("/api/fleet/ws")
async def fleet_ws(ws: WebSocket, org: str):
    """Every event from every site of the org the viewer may see: {type:"event", event, site_id, site_name},
    plus site_online / site_offline."""
    u = auth.current_user(ws)  # type: ignore[arg-type]
    if not u or not auth.role_in(u, org):
        await ws.close(code=4401 if not u else 4403)
        return
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue()
    registry.org_subscribers.setdefault(org, set()).add(q)
    allowed = {s["id"] for s in auth.visible_sites(u, org)}
    refreshed = time.time()
    try:
        while True:
            msg = await q.get()
            if time.time() - refreshed > 60:   # grants can change while a tab stays open
                allowed = {s["id"] for s in auth.visible_sites(u, org)}
                refreshed = time.time()
            if msg.get("site_id") in allowed:
                await ws.send_json(msg)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        registry.org_subscribers.get(org, set()).discard(q)


# ---------------------------------------------------------------- tunnel + proxy

@app.websocket("/agent")
async def agent(ws: WebSocket):
    await registry.serve(ws)


@app.websocket("/host-agent")
async def host_agent(ws: WebSocket):
    """Central recording hosts (hosts.py): their own token and protocol, never the server tunnel."""
    await hosts.registry.serve(ws)


@app.get("/s/{site_id}/api/turn")
async def site_turn(site_id: str, request: Request):
    """ICE servers for a browser watching this site through the hub (answered here, not by the site)."""
    u = auth.require_user(request)
    auth.site_access(u, site_id)
    return {"iceServers": turn.ice_servers(f"user:{u['id']}", settings.turn_user_ttl_s)}


app.include_router(vlm_proxy.router)
app.include_router(soc_api.router)   # SOC: staff, monitoring/arming, contacts, procedures
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
        # .webmanifest isn't in every platform's mimetypes table (Windows reads the registry), so don't guess
        media = "application/manifest+json" if target.suffix == ".webmanifest" else None
        return FileResponse(target, media_type=media, headers={"Cache-Control": "public, max-age=31536000, immutable" if full_path.startswith("hub-assets/") else "no-cache"})
    index = dist / "index.html"
    if not index.exists():
        return JSONResponse({"detail": "hub UI not built (run npm run build in hub/ui)"}, status_code=503)
    return FileResponse(index, headers={"Cache-Control": "no-cache"})
