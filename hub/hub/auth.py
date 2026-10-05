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


def membership(u: dict, org_id: str) -> dict | None:
    """{"role", "all_sites"} for this user in this customer, or None. Hub administrators are owners of everything."""
    if u.get("is_super"):
        return {"role": "owner", "all_sites": True}
    m = db.one(sa.select(db.memberships).where(db.memberships.c.user_id == u["id"], db.memberships.c.org_id == org_id))
    if not m:
        return None
    return {"role": m["role"], "all_sites": m.get("all_sites") is not False}


def role_in(u: dict, org_id: str) -> str | None:
    m = membership(u, org_id)
    return m["role"] if m else None


def require_role(u: dict, org_id: str, needed: str) -> str:
    role = role_in(u, org_id)
    if not role or not allows(role, needed):
        raise HTTPException(403, f"needs {needed} in this organisation")
    return role


# ---------------------------------------------------------------- what a member may see
# A member sees every Site (location) of the customer when memberships.all_sites is true, otherwise exactly the
# Sites in location_grants: no grants means nothing (the old implicit "no grants = everything" is gone, so
# revoking someone's last Site can never widen their view). Servers are visible through their Site; a server
# with no Site yet (only possible for rows written outside the API) is visible to all-sites members only.

def granted_location_ids(uid: str, org_id: str) -> set[str]:
    q = sa.select(db.location_grants.c.location_id).join(db.locations, db.locations.c.id == db.location_grants.c.location_id) \
        .where(db.location_grants.c.user_id == uid, db.locations.c.org_id == org_id)
    return {r["location_id"] for r in db.rows(q)}


def _sees(u: dict, m: dict | None, org_id: str, location_id: str | None, cache: dict | None = None) -> bool:
    if not m:
        return False
    if m["all_sites"]:
        return True
    if cache is None:
        cache = {}
    if "granted" not in cache:
        cache["granted"] = granted_location_ids(u["id"], org_id)
    return location_id is not None and location_id in cache["granted"]


def visible_locations(u: dict, org_id: str) -> list[dict]:
    """The customer's Sites this user may see, by name."""
    m = membership(u, org_id)
    if not m:
        return []
    locs = db.rows(sa.select(db.locations).where(db.locations.c.org_id == org_id).order_by(db.locations.c.name))
    if m["all_sites"]:
        return locs
    granted = granted_location_ids(u["id"], org_id)
    return [loc for loc in locs if loc["id"] in granted]


def visible_sites(u: dict, org_id: str, include_retired: bool = False) -> list[dict]:
    """Servers of the customer this user may see (see above). Retired servers (fleet actions) are left out
    unless asked for; they stay reachable at /s/<server>/ (site_access)."""
    m = membership(u, org_id)
    if not m:
        return []
    q = sa.select(db.sites).where(db.sites.c.org_id == org_id).order_by(db.sites.c.name)
    if not include_retired:
        q = q.where(db.sites.c.retired_at.is_(None))
    every = db.rows(q)
    if m["all_sites"]:
        return every
    granted = granted_location_ids(u["id"], org_id)
    return [s for s in every if s.get("location_id") in granted]


visible_servers = visible_sites


def site_access(u: dict, site_id: str) -> tuple[dict, str]:
    """(server, role) or 403/404."""
    s = db.one(sa.select(db.sites).where(db.sites.c.id == site_id))
    if not s:
        raise HTTPException(404, "unknown site")
    m = membership(u, s["org_id"])
    if not m:
        raise HTTPException(403, "not a member of this site's organisation")
    if not _sees(u, m, s["org_id"], s.get("location_id")):
        raise HTTPException(403, "not granted this site")
    return s, m["role"]


server_access = site_access


def location_access(u: dict, location_id: str) -> tuple[dict, str]:
    """(location, role) or 403/404."""
    loc = db.one(sa.select(db.locations).where(db.locations.c.id == location_id))
    if not loc:
        raise HTTPException(404, "unknown site")
    m = membership(u, loc["org_id"])
    if not m:
        raise HTTPException(403, "not a member of this site's organisation")
    if not _sees(u, m, loc["org_id"], loc["id"]):
        raise HTTPException(403, "not granted this site")
    return loc, m["role"]


def camera_access(u: dict, server_id: str, camera_id: str) -> tuple[dict, dict, str]:
    """(server, registry camera row, role) or 403/404."""
    s, role = site_access(u, server_id)
    cam = db.one(sa.select(db.cameras).where(db.cameras.c.server_id == server_id, db.cameras.c.camera_id == camera_id))
    if not cam:
        raise HTTPException(404, "unknown camera")
    return s, cam, role


def can_see_server(uid: str, org_id: str, server: dict) -> bool:
    """For hub-side fan-out (push): may this user id see this server?"""
    u = user_by_id(uid)
    if not u:
        return False
    return _sees(u, membership(u, org_id), org_id, server.get("location_id"))


def set_access(org_id: str, uid: str, all_sites: bool, location_ids: list[str]) -> dict:
    """Replace a member's Site access in one transaction. The customer's legacy site_grants for the user are
    rewritten to the servers of the granted Sites, so older hub code (if ever rolled back) stays restricted.
    LookupError: not a member. ValueError: a location id that isn't this customer's."""
    org_locs = {r["id"] for r in db.rows(sa.select(db.locations.c.id).where(db.locations.c.org_id == org_id))}
    wanted = list(dict.fromkeys(location_ids or []))
    bad = [lid for lid in wanted if lid not in org_locs]
    if bad:
        raise ValueError(f"unknown site {bad[0]}")
    org_servers = db.rows(sa.select(db.sites.c.id, db.sites.c.location_id).where(db.sites.c.org_id == org_id))
    with db.engine().begin() as c:
        res = c.execute(sa.update(db.memberships).where(db.memberships.c.user_id == uid, db.memberships.c.org_id == org_id)
                        .values(all_sites=bool(all_sites)))
        if not res.rowcount:
            raise LookupError("not a member")
        if org_locs:
            c.execute(sa.delete(db.location_grants).where(db.location_grants.c.user_id == uid,
                                                          db.location_grants.c.location_id.in_(list(org_locs))))
        for lid in wanted:
            c.execute(db.location_grants.insert().values(user_id=uid, location_id=lid))
        if org_servers:
            c.execute(sa.delete(db.site_grants).where(db.site_grants.c.user_id == uid,
                                                      db.site_grants.c.site_id.in_([s["id"] for s in org_servers])))
        if not all_sites:
            for s in org_servers:
                if s["location_id"] in wanted:
                    c.execute(db.site_grants.insert().values(user_id=uid, site_id=s["id"]))
    return access_of(org_id, uid)


def access_of(org_id: str, uid: str) -> dict:
    """{all_sites, location_ids, sites}; `sites` is the legacy view (server ids, [] = all) for the current UI."""
    m = db.one(sa.select(db.memberships).where(db.memberships.c.user_id == uid, db.memberships.c.org_id == org_id))
    all_sites = bool(m and m.get("all_sites") is not False)
    locs = sorted(granted_location_ids(uid, org_id))
    servers = [] if all_sites else [r["id"] for r in db.rows(sa.select(db.sites.c.id).where(
        db.sites.c.org_id == org_id, db.sites.c.location_id.in_(locs)).order_by(db.sites.c.name))] if locs else []
    return {"all_sites": all_sites, "location_ids": locs, "sites": servers}


def drop_access(org_id: str, uid: str, conn) -> None:
    """A member leaves the customer: their Site grants (and legacy server grants) there go too."""
    org_locs = [r["id"] for r in db.rows(sa.select(db.locations.c.id).where(db.locations.c.org_id == org_id))]
    org_servers = [r["id"] for r in db.rows(sa.select(db.sites.c.id).where(db.sites.c.org_id == org_id))]
    if org_locs:
        conn.execute(sa.delete(db.location_grants).where(db.location_grants.c.user_id == uid, db.location_grants.c.location_id.in_(org_locs)))
    if org_servers:
        conn.execute(sa.delete(db.site_grants).where(db.site_grants.c.user_id == uid, db.site_grants.c.site_id.in_(org_servers)))
