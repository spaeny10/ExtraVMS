"""Users, passwords (argon2), optional TOTP, cookie sessions, roles and the org a request acts in."""
from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque

import pyotp
import sqlalchemy as sa
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import HTTPException, Request, Response

from . import db, soc
from .config import settings
from .roles import RANK, allows

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


_dummy_hash: str | None = None


def burn_password_check(pw: str) -> None:
    """For an unknown email: the same argon2 work a real check costs, so sign-in time doesn't reveal which emails
    have accounts."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = _ph.hash("not a real account: timing only")
    verify_password(pw, _dummy_hash)


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
    # soc_role is the stored role; the UI treats is_super as supervisor too (soc_level below)
    return {"id": u["id"], "email": u["email"], "totp_enabled": bool(u["totp_enabled"]), "is_super": bool(u["is_super"]),
            "soc_role": u.get("soc_role") if u.get("soc_role") in SOC_ROLES else None}


# ---------------------------------------------------------------- hub administrators
# users.is_super = hub administrator: owner of every customer (membership() below), never listed as a member.
# The flag is read from the users row on every request, so granting or revoking takes effect at once without
# touching anyone's sessions. Changes are audited with org_id NULL ("hub-level"): no customer's audit shows them,
# GET /api/hub/audit does.

LAST_ADMIN_MSG = "that would leave the hub without an administrator: make someone else one first"


def hub_admins() -> list[dict]:
    q = sa.select(db.users.c.id, db.users.c.email, db.users.c.totp_enabled, db.users.c.last_login_at) \
        .where(db.users.c.is_super.is_(True)).order_by(db.users.c.email)
    return [{**r, "totp_enabled": bool(r["totp_enabled"])} for r in db.rows(q)]


def set_super(email: str, on: bool, actor: dict | None = None) -> tuple[dict, bool]:
    """Grant (on) or revoke hub administrator for an existing user. Returns (user, changed); an unchanged flag writes
    no audit row. LookupError: no such user. ValueError(LAST_ADMIN_MSG): revoking the last one, which would leave
    nobody able to manage the hub except through the CLI. `actor` None = the command line."""
    u = user_by_email(email)
    if not u:
        raise LookupError("no such user")
    if bool(u["is_super"]) == on:
        return u, False
    with db.engine().begin() as c:
        if not on:
            # counted inside the transaction that writes, so two admins revoking each other can't both pass the check
            n = c.execute(sa.select(sa.func.count()).select_from(db.users).where(db.users.c.is_super.is_(True))).scalar_one()
            if n <= 1:
                raise ValueError(LAST_ADMIN_MSG)
        c.execute(sa.update(db.users).where(db.users.c.id == u["id"]).values(is_super=on))
        c.execute(db.audit_log.insert().values(
            ts=time.time(), user_id=actor["id"] if actor else None, user_email=actor["email"] if actor else "(command line)",
            org_id=None, site_id=None, action=f"hub admin {'granted' if on else 'revoked'}: {u['email']}", method=None, path=None,
            status=None, ip=None, detail={"target_user_id": u["id"]}))
    u["is_super"] = on
    return u, True


def require_super(u: dict) -> None:
    if not u.get("is_super"):
        raise HTTPException(403, "hub administrators only")


# ---------------------------------------------------------------- SOC staff
# users.soc_role = operator | supervisor: hub-level like is_super (read from the users row on every request, audited
# with org_id NULL), but scoped: membership() widens a SOC user's access only in customers that have at least one
# monitored Site (soc.org_monitored), to operator (SOC operators) or admin (supervisors), every Site. A hub
# administrator counts as a supervisor. Customers without a monitored Site never see SOC staff.

SOC_ROLES = soc.SOC_ROLES


def soc_level(u: dict | None) -> str | None:
    """None | "operator" | "supervisor" (hub administrators are supervisors)."""
    if not u:
        return None
    if u.get("is_super"):
        return "supervisor"
    r = u.get("soc_role")
    return r if r in SOC_ROLES else None


def require_soc(u: dict, level: str = "operator") -> str:
    lvl = soc_level(u)
    if not lvl or soc.SOC_RANK[lvl] < soc.SOC_RANK[level]:
        raise HTTPException(403, "SOC supervisors only" if level == "supervisor" else "SOC staff only")
    return lvl


def soc_members() -> list[dict]:
    """Users holding a SOC role, by email (hub administrators are supervisors without being listed)."""
    q = sa.select(db.users.c.id, db.users.c.email, db.users.c.soc_role, db.users.c.totp_enabled, db.users.c.last_login_at) \
        .where(db.users.c.soc_role.in_(list(SOC_ROLES))).order_by(db.users.c.email)
    return [{**r, "totp_enabled": bool(r["totp_enabled"])} for r in db.rows(q)]


def set_soc_role(email: str, role: str | None, actor: dict | None = None) -> tuple[dict, bool]:
    """Grant operator / supervisor, or revoke (None), for an existing user. Returns (user, changed); an unchanged role
    writes no audit row. LookupError: no such user; ValueError: unknown role. `actor` None = the command line."""
    if role is not None and role not in SOC_ROLES:
        raise ValueError(f"role must be one of {', '.join(SOC_ROLES)} (or none)")
    u = user_by_email(email)
    if not u:
        raise LookupError("no such user")
    if (u.get("soc_role") or None) == role:
        return u, False
    action = f"soc {role} granted: {u['email']}" if role else f"soc role revoked: {u['email']}"
    with db.engine().begin() as c:
        c.execute(sa.update(db.users).where(db.users.c.id == u["id"]).values(soc_role=role))
        c.execute(db.audit_log.insert().values(
            ts=time.time(), user_id=actor["id"] if actor else None, user_email=actor["email"] if actor else "(command line)",
            org_id=None, site_id=None, action=action, method=None, path=None, status=None, ip=None,
            detail={"target_user_id": u["id"], "from": u.get("soc_role"), "to": role}))
    u["soc_role"] = role
    return u, True


SOC_STAFF_MSG = "SOC staff cannot manage customer members"


def real_membership(u: dict, org_id: str) -> dict | None:
    """The memberships row alone, without the SOC widening (hub administrators: None unless they hold one)."""
    return db.one(sa.select(db.memberships).where(db.memberships.c.user_id == u["id"], db.memberships.c.org_id == org_id))


def require_customer_role(u: dict, org_id: str, needed: str) -> str:
    """require_role for managing the customer itself: its members, invites, audit log, deleting its Sites. SOC
    supervisors are widened to admin in monitored customers so they can configure monitoring, contacts, procedures
    and servers, but that widening must not let them add people to (or read the audit of) a customer they serve:
    only a real membership with the role counts there. Hub administrators are unaffected."""
    role = require_role(u, org_id, needed)
    m = membership(u, org_id)
    if m and m.get("soc"):
        row = real_membership(u, org_id)
        if not row or not allows(row["role"], needed):
            raise HTTPException(403, SOC_STAFF_MSG)
        return row["role"]
    return role


def me_user(u: dict) -> dict:
    """public_user plus soc_only: SOC staff with no real membership anywhere (the UI lands them on the SOC and
    hides customer management)."""
    out = public_user(u)
    out["soc_only"] = bool(soc_level(u)) and not u.get("is_super") and not user_orgs(u["id"])
    return out


def _soc_membership(u: dict, org_id: str) -> dict | None:
    lvl = soc_level(u)
    if not lvl or not soc.org_monitored(org_id):
        return None
    return {"role": "admin" if lvl == "supervisor" else "operator", "all_sites": True, "soc": True}


TOTP_STEP_S = 30


def totp_step_for(secret: str | None, code: str | None, now: float | None = None) -> int | None:
    """The 30 s time step (now, or one either side for clock drift) whose code this is, or None."""
    code = (code or "").strip().replace(" ", "")
    if not secret or not code.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    cur = int((now if now is not None else time.time()) // TOTP_STEP_S)
    for step in (cur, cur - 1, cur + 1):
        if hmac.compare_digest(totp.generate_otp(step), code):
            return step
    return None


def consume_totp_step(uid: str, step: int, conn=None) -> bool:
    """Record `step` as this user's last used code, only if it is newer than the last one: a code works once, so one
    seen over a shoulder or replayed from a log within its 90 s window is refused. Atomic (conditional UPDATE)."""
    stmt = sa.update(db.users).where(db.users.c.id == uid, sa.or_(db.users.c.totp_last_step.is_(None),
                                                                   db.users.c.totp_last_step < step)).values(totp_last_step=step)
    if conn is not None:
        return conn.execute(stmt).rowcount == 1
    with db.engine().begin() as c:
        return c.execute(stmt).rowcount == 1


def totp_ok(u: dict, code: str | None) -> bool:
    """True when the user has no TOTP, or `code` is a current code not used before (it is then used up)."""
    if not u["totp_enabled"]:
        return True
    step = totp_step_for(u["totp_secret"], code)
    return step is not None and consume_totp_step(u["id"], step)


# ---------------------------------------------------------------- sessions

def session_key(sid: str) -> str:
    """What sessions.id stores for a cookie value: its sha256, so a leaked database (or backup) holds no usable
    session. Sessions made before this was introduced are stored plain; session_of upgrades them on first use."""
    return db.token_hash(sid)


def new_session(u: dict, request: Request) -> str:
    """Start a session; returns the cookie value (the row is keyed by session_key of it)."""
    sid = db.new_token()
    orgs = user_orgs(u["id"])
    db.insert(db.sessions, {"id": session_key(sid), "user_id": u["id"], "org_id": orgs[0]["id"] if orgs else None, "created_at": time.time(),
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
    if not sid or len(sid) > 128:
        return None
    key = session_key(sid)
    s = db.one(sa.select(db.sessions).where(db.sessions.c.id == key))
    if s is None:
        # a session from before ids were hashed: rehash it in place, so nobody is signed out by the upgrade
        s = db.one(sa.select(db.sessions).where(db.sessions.c.id == sid))
        if s is not None:
            db.run(sa.update(db.sessions).where(db.sessions.c.id == sid).values(id=key))
            s["id"] = key
    if not s or s["expires_at"] < time.time():
        return None
    return s


def end_other_sessions(uid: str, keep_id: str | None) -> None:
    """Sign the user out everywhere except the session `keep_id` (a sessions.id, i.e. already hashed)."""
    q = sa.delete(db.sessions).where(db.sessions.c.user_id == uid)
    if keep_id:
        q = q.where(db.sessions.c.id != keep_id)
    db.run(q)


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


def orgs_for(u: dict) -> list[dict]:
    """The customers this user can act in, with their role: a hub administrator owns every customer (membership()),
    so they get all of them, by name, whether or not they hold a membership row."""
    # `member`: a real membership row (false where only hub administration or the SOC brings the user in)
    if u.get("is_super"):
        real = {o["id"] for o in user_orgs(u["id"])}
        return [{**o, "role": "owner", "member": o["id"] in real} for o in db.rows(sa.select(db.orgs).order_by(db.orgs.c.name))]
    out = [{**o, "member": True} for o in user_orgs(u["id"])]
    if soc_level(u):
        # SOC staff also act in every customer with a monitored Site, at the wider of the two roles
        mine = {o["id"]: o for o in out}
        for oid in soc.monitored_org_ids():
            sm = _soc_membership(u, oid)
            if sm is None:
                continue
            if oid in mine:
                if RANK.get(sm["role"], -1) > RANK.get(mine[oid]["role"], -1):
                    mine[oid]["role"] = sm["role"]
                mine[oid]["soc"] = True
        extra = [oid for oid in soc.monitored_org_ids() if oid not in mine]
        if extra:
            for o in db.rows(sa.select(db.orgs).where(db.orgs.c.id.in_(extra)).order_by(db.orgs.c.name)):
                out.append({**o, "role": _soc_membership(u, o["id"])["role"], "soc": True, "member": False})
    return out


def membership(u: dict, org_id: str) -> dict | None:
    """{"role", "all_sites"} for this user in this customer, or None. Hub administrators are owners of everything.
    SOC staff in a customer with a monitored Site get the wider of their real membership and the SOC one
    ({"role": operator | admin, "all_sites": True, "soc": True})."""
    if u.get("is_super"):
        return {"role": "owner", "all_sites": True}
    row = db.one(sa.select(db.memberships).where(db.memberships.c.user_id == u["id"], db.memberships.c.org_id == org_id))
    m = {"role": row["role"], "all_sites": row.get("all_sites") is not False} if row else None
    sm = _soc_membership(u, org_id)
    if sm is None:
        return m
    if m is None:
        return sm
    return {"role": m["role"] if RANK.get(m["role"], -1) >= RANK[sm["role"]] else sm["role"], "all_sites": True, "soc": True}


def role_in(u: dict, org_id: str) -> str | None:
    m = membership(u, org_id)
    return m["role"] if m else None


def require_role(u: dict, org_id: str, needed: str) -> str:
    role = role_in(u, org_id)
    if not role or not allows(role, needed):
        raise HTTPException(403, f"needs {needed} in this organization")
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


def scope_filter(u: dict, org_id: str):
    """None when this user sees the whole customer (all-sites members, hub administrators, SOC staff); otherwise a
    predicate for rows tagged with a server (`site_id`) and/or a Site (`location_id`, or detail.location_id as audit
    rows carry it): true only for the servers and Sites they can see. Rows tagged with neither are customer-wide
    and are hidden from Site-restricted members."""
    m = membership(u, org_id)
    if m and m["all_sites"]:
        return None
    servers = {s["id"] for s in visible_sites(u, org_id, include_retired=True)} if m else set()
    locs = granted_location_ids(u["id"], org_id) if m else set()

    def ok(row: dict) -> bool:
        sid = row.get("site_id") or row.get("server_id")
        if sid:
            return sid in servers
        detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
        lid = row.get("location_id") or detail.get("location_id")
        return bool(lid) and lid in locs
    return ok


def site_access(u: dict, site_id: str) -> tuple[dict, str]:
    """(server, role) or 403/404."""
    s = db.one(sa.select(db.sites).where(db.sites.c.id == site_id))
    if not s:
        raise HTTPException(404, "unknown site")
    m = membership(u, s["org_id"])
    if not m:
        raise HTTPException(403, "not a member of this site's organization")
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
        raise HTTPException(403, "not a member of this site's organization")
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
    """Replace a member's Site access in one transaction.
    LookupError: not a member. ValueError: a location id that isn't this customer's."""
    org_locs = {r["id"] for r in db.rows(sa.select(db.locations.c.id).where(db.locations.c.org_id == org_id))}
    wanted = list(dict.fromkeys(location_ids or []))
    bad = [lid for lid in wanted if lid not in org_locs]
    if bad:
        raise ValueError(f"unknown site {bad[0]}")
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
    return access_of(org_id, uid)


GRANT_SCOPE_MSG = "you can only grant Sites you can see"


def check_grant_scope(u: dict, org_id: str, all_sites: bool, location_ids: list[str] | None) -> None:
    """403 unless `u` may hand out this Site access in this customer. An admin can grant at most what they can
    see themselves: otherwise a Site-restricted admin could invite (or add) a member with every Site, sign in as
    them and escalate. Hub administrators and all-sites members (any role: an owner can be restricted too, since
    all_sites lives on the membership, not the role) are unrestricted. Refused outright rather than silently
    narrowed, so the admin knows the grant didn't happen as asked."""
    m = membership(u, org_id)
    if not m:
        raise HTTPException(403, "not a member of this organization")
    if m["all_sites"]:
        return
    if all_sites:
        raise HTTPException(403, GRANT_SCOPE_MSG)
    granted = granted_location_ids(u["id"], org_id)
    if any(lid not in granted for lid in (location_ids or [])):
        raise HTTPException(403, GRANT_SCOPE_MSG)


def access_of(org_id: str, uid: str) -> dict:
    """{all_sites, location_ids}: every Site of the customer, or only these."""
    m = db.one(sa.select(db.memberships).where(db.memberships.c.user_id == uid, db.memberships.c.org_id == org_id))
    all_sites = bool(m and m.get("all_sites") is not False)
    return {"all_sites": all_sites, "location_ids": sorted(granted_location_ids(uid, org_id))}


def drop_access(org_id: str, uid: str, conn) -> None:
    """A member leaves the customer: their Site grants there go too."""
    org_locs = [r["id"] for r in db.rows(sa.select(db.locations.c.id).where(db.locations.c.org_id == org_id))]
    if org_locs:
        conn.execute(sa.delete(db.location_grants).where(db.location_grants.c.user_id == uid, db.location_grants.c.location_id.in_(org_locs)))
