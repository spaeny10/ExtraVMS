"""Users, passwords (argon2), optional TOTP, cookie sessions, roles and the org a request acts in."""
from __future__ import annotations

import time
from collections import defaultdict, deque

import pyotp
import sqlalchemy as sa
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import HTTPException, Request, Response

from . import db
from .config import settings
from .roles import allows

COOKIE = "hub_session"
_ph = PasswordHasher()
_failures: dict[str, deque] = defaultdict(deque)   # login rate limit: key -> timestamps
FAIL_LIMIT, FAIL_WINDOW_S = 10, 15 * 60


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, pw)
    except VerifyMismatchError:
        return False


def too_many_failures(key: str) -> bool:
    q = _failures[key]
    cutoff = time.time() - FAIL_WINDOW_S
    while q and q[0] < cutoff:
        q.popleft()
    return len(q) >= FAIL_LIMIT


def record_failure(key: str) -> None:
    _failures[key].append(time.time())


# ---------------------------------------------------------------- users

def create_user(email: str, password: str, is_super: bool = False) -> dict:
    email = email.strip().lower()
    if db.one(sa.select(db.users).where(db.users.c.email == email)):
        raise ValueError("email already registered")
    u = {"id": db.new_id("u_"), "email": email, "password_hash": hash_password(password), "totp_secret": None,
         "totp_enabled": False, "is_super": is_super, "created_at": time.time(), "last_login_at": None}
    db.insert(db.users, u)
    return u


def set_password(email: str, password: str) -> dict:
    """Replace a user's password and end all of their sessions (used by `python -m hub setpassword`)."""
    u = user_by_email(email)
    if not u:
        raise ValueError("no such user")
    if len(password) < 10:
        raise ValueError("password must be at least 10 characters")
    with db.engine().begin() as c:
        c.execute(sa.update(db.users).where(db.users.c.id == u["id"]).values(password_hash=hash_password(password)))
        c.execute(sa.delete(db.sessions).where(db.sessions.c.user_id == u["id"]))
    return u


def user_by_email(email: str) -> dict | None:
    return db.one(sa.select(db.users).where(db.users.c.email == email.strip().lower()))


def user_by_id(uid: str) -> dict | None:
    return db.one(sa.select(db.users).where(db.users.c.id == uid))


def public_user(u: dict) -> dict:
    return {"id": u["id"], "email": u["email"], "totp_enabled": bool(u["totp_enabled"]), "is_super": bool(u["is_super"])}


def totp_ok(u: dict, code: str | None) -> bool:
    if not u["totp_enabled"]:
        return True
    return bool(code) and pyotp.TOTP(u["totp_secret"]).verify(code.strip().replace(" ", ""), valid_window=1)


# ---------------------------------------------------------------- sessions

def new_session(u: dict, request: Request) -> str:
    sid = db.new_token()
    orgs = user_orgs(u["id"])
    db.insert(db.sessions, {"id": sid, "user_id": u["id"], "org_id": orgs[0]["id"] if orgs else None, "created_at": time.time(),
                            "expires_at": time.time() + settings.session_days * 86400,
                            "ip": request.client.host if request.client else None, "ua": (request.headers.get("user-agent") or "")[:300]})
    db.run(sa.update(db.users).where(db.users.c.id == u["id"]).values(last_login_at=time.time()))
    return sid


def set_cookie(resp: Response, sid: str) -> None:
    resp.set_cookie(COOKIE, sid, max_age=settings.session_days * 86400, httponly=True, secure=settings.cookie_secure, samesite="lax", path="/")


def clear_cookie(resp: Response) -> None:
    resp.delete_cookie(COOKIE, path="/")


def session_of(request: Request) -> dict | None:
    sid = request.cookies.get(COOKIE)
    if not sid:
        return None
    s = db.one(sa.select(db.sessions).where(db.sessions.c.id == sid))
    if not s or s["expires_at"] < time.time():
        return None
    return s


def current_user(request: Request) -> dict | None:
    s = session_of(request)
    if not s:
        return None
    u = user_by_id(s["user_id"])
    if u:
        u["session"] = s
    return u


def require_user(request: Request) -> dict:
    u = current_user(request)
    if not u:
        raise HTTPException(401, "sign in")
    return u


def csrf_check(request: Request) -> None:
    """Browsers send Origin on cross-site POSTs; a mismatch means a foreign page is driving the session."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin")
    if not origin:
        return
    allowed = {settings.public_url.rstrip("/"), f"{request.url.scheme}://{request.headers.get('host', '')}"}
    if origin.rstrip("/") not in allowed:
        raise HTTPException(403, "cross-site request refused")


# ---------------------------------------------------------------- orgs and roles

def user_orgs(uid: str) -> list[dict]:
    q = sa.select(db.orgs, db.memberships.c.role).join(db.memberships, db.memberships.c.org_id == db.orgs.c.id).where(db.memberships.c.user_id == uid)
    return db.rows(q)


def role_in(u: dict, org_id: str) -> str | None:
    if u.get("is_super"):
        return "owner"
    m = db.one(sa.select(db.memberships).where(db.memberships.c.user_id == u["id"], db.memberships.c.org_id == org_id))
    return m["role"] if m else None


def require_role(u: dict, org_id: str, needed: str) -> str:
    role = role_in(u, org_id)
    if not role or not allows(role, needed):
        raise HTTPException(403, f"needs {needed} in this organisation")
    return role


def visible_sites(u: dict, org_id: str, include_retired: bool = False) -> list[dict]:
    """Sites of the org this user may see: all, or only the granted ones when grants exist. Retired sites
    (fleet actions) are left out unless asked for; they stay reachable at /s/<site>/ (site_access)."""
    q = sa.select(db.sites).where(db.sites.c.org_id == org_id).order_by(db.sites.c.name)
    if not include_retired:
        q = q.where(db.sites.c.retired_at.is_(None))
    all_sites = db.rows(q)
    if u.get("is_super"):
        return all_sites
    granted = {g["site_id"] for g in db.rows(sa.select(db.site_grants).where(db.site_grants.c.user_id == u["id"]))}
    granted_here = {s["id"] for s in all_sites} & granted
    return [s for s in all_sites if s["id"] in granted_here] if granted_here else all_sites


def site_access(u: dict, site_id: str) -> tuple[dict, str]:
    """(site, role) or 403/404."""
    s = db.one(sa.select(db.sites).where(db.sites.c.id == site_id))
    if not s:
        raise HTTPException(404, "unknown site")
    role = role_in(u, s["org_id"])
    if not role:
        raise HTTPException(403, "not a member of this site's organisation")
    if not u.get("is_super"):
        granted = db.rows(sa.select(db.site_grants).where(db.site_grants.c.user_id == u["id"]))
        org_site_ids = {x["id"] for x in db.rows(sa.select(db.sites.c.id).where(db.sites.c.org_id == s["org_id"]))}
        granted_here = {g["site_id"] for g in granted} & org_site_ids
        if granted_here and site_id not in granted_here:
            raise HTTPException(403, "not granted this site")
    return s, role
