"""SOC routes (stage 1): SOC staff management, per-Site monitoring and arming, contacts and procedures, and the
dispositions catalogue. The incident queue, its socket and the escalation engine build on these in stage 2.

Who may do what:
  /api/hub/soc/members                       hub administrators (like /api/hub/admins)
  GET  /api/locations/{id}/monitoring        anyone who can see the Site
  PUT  /api/locations/{id}/monitoring        admin of the Site's customer, which includes SOC supervisors (auth.membership)
  POST/DELETE /api/locations/{id}/arm        operator or above there, which includes SOC operators
  GET  /api/locations/{id}/contacts|procedures   anyone who can see the Site
  PUT  /api/locations/{id}/contacts|procedures   as monitoring PUT
  GET  /api/soc/dispositions                 SOC staff
Every write is audited in the customer's audit log (SOC staff changes hub-level, org_id NULL).
"""
from __future__ import annotations

import datetime as dt
import time
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field, field_validator

from . import auth, db, soc
from .agents import registry
from .roles import allows

router = APIRouter()

MAX_WINDOWS, MAX_HOLIDAYS, MAX_CONTACTS, MAX_PROCEDURES, MAX_STEPS = 50, 200, 50, 50, 50
MAX_OVERRIDE_S = 24 * 3600


def user(request: Request) -> dict:
    return auth.require_user(request)


def _audit(u: dict, org_id: str | None, action: str, detail: dict | None = None) -> None:
    db.insert(db.audit_log, {"ts": time.time(), "user_id": u["id"], "user_email": u["email"], "org_id": org_id, "site_id": None,
                             "action": action[:200], "method": None, "path": None, "status": None, "ip": None, "detail": detail or {}})


# ---------------------------------------------------------------- SOC staff (hub administrators manage them)

class SocMemberIn(BaseModel):
    email: EmailStr
    role: Literal["operator", "supervisor"]


def _member_out(u: dict) -> dict:
    return {"id": u["id"], "email": u["email"], "soc_role": u.get("soc_role"), "totp_enabled": bool(u["totp_enabled"]),
            "last_login_at": u.get("last_login_at")}


@router.get("/api/hub/soc/members")
async def list_soc_members(u: dict = Depends(user)):
    auth.require_super(u)
    return auth.soc_members()


@router.post("/api/hub/soc/members")
async def add_soc_member(body: SocMemberIn, u: dict = Depends(user)):
    """Grant (or change) a SOC role. Needs an existing account, like hub administrators."""
    auth.require_super(u)
    try:
        target, changed = auth.set_soc_role(body.email, body.role, actor=u)
    except LookupError:
        raise HTTPException(404, "no account with that email: invite them to a customer (or create them) first")
    if changed:
        soc.invalidate()
    return _member_out(target)


@router.delete("/api/hub/soc/members/{uid}")
async def remove_soc_member(uid: str, u: dict = Depends(user)):
    auth.require_super(u)
    target = auth.user_by_id(uid)
    if not target or target.get("soc_role") not in auth.SOC_ROLES:
        raise HTTPException(404, "not SOC staff")
    auth.set_soc_role(target["email"], None, actor=u)
    return {"ok": True}


# ---------------------------------------------------------------- monitoring and arming

def _can_configure_site(u: dict, loc: dict) -> bool:
    """Monitoring, contacts and procedures: an admin of the Site's customer. SOC supervisors are admins of every
    customer with a monitored Site (auth.membership), so they qualify there; SOC operators (operator) do not."""
    m = auth.membership(u, loc["org_id"])
    return bool(m) and allows(m["role"], "admin")


def _can_arm(u: dict, loc: dict) -> bool:
    m = auth.membership(u, loc["org_id"])
    return bool(m) and allows(m["role"], "operator")


def _configurable(u: dict, location_id: str) -> dict:
    loc, _ = auth.location_access(u, location_id)
    if not _can_configure_site(u, loc):
        raise HTTPException(403, "needs admin for this site's customer (or SOC supervisor)")
    return loc


def _monitoring_out(u: dict, loc: dict) -> dict:
    now = time.time()
    armed, reason = soc.armed_now(loc, now)
    o = loc.get("arm_override")
    return {"location_id": loc["id"], "name": loc["name"], "timezone": loc.get("timezone"), "monitored": bool(loc.get("monitored")),
            "arm_schedule": loc.get("arm_schedule") or [], "arm_holidays": loc.get("arm_holidays") or [],
            "arm_override": o if isinstance(o, dict) else None,
            "override_active": soc.active_override(loc, now) is not None,
            "soc_group_minutes": loc.get("soc_group_minutes"),
            "armed": armed, "reason": reason, "next_change": soc.next_change(loc, now), "now": now,
            "can_configure": _can_configure_site(u, loc), "can_arm": _can_arm(u, loc)}


class WindowIn(BaseModel):
    dow: list[int] = Field(min_length=1, max_length=7)
    from_: str = Field(alias="from")
    to: str

    @field_validator("dow")
    @classmethod
    def _dow(cls, v: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("dow is 0..6 (Monday = 0)")
        return sorted(set(v))

    @field_validator("from_", "to")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if soc.parse_hhmm(v) is None:
            raise ValueError("times are HH:MM, 00:00 to 23:59")
        return v


class HolidayIn(BaseModel):
    date: str
    name: str = Field("", max_length=80)
    armed: bool = True
    from_: str | None = Field(None, alias="from")
    to: str | None = None

    @field_validator("date")
    @classmethod
    def _date(cls, v: str) -> str:
        try:
            return dt.date.fromisoformat(v).isoformat()
        except ValueError:
            raise ValueError("date is YYYY-MM-DD")

    @field_validator("from_", "to")
    @classmethod
    def _hhmm(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        if soc.parse_hhmm(v) is None:
            raise ValueError("times are HH:MM, 00:00 to 23:59")
        return v


class MonitoringIn(BaseModel):
    """Partial: only the fields sent change."""
    monitored: bool | None = None
    arm_schedule: list[WindowIn] | None = Field(None, max_length=MAX_WINDOWS)
    arm_holidays: list[HolidayIn] | None = Field(None, max_length=MAX_HOLIDAYS)
    soc_group_minutes: int | None = Field(None, ge=1, le=240)


def _window_json(w: WindowIn) -> dict:
    return {"dow": w.dow, "from": w.from_, "to": w.to}


def _holiday_json(h: HolidayIn) -> dict:
    if (h.from_ is None) != (h.to is None):
        raise HTTPException(422, f"holiday {h.date}: give both from and to, or neither")
    out = {"date": h.date, "name": h.name.strip(), "armed": h.armed}
    if h.from_ is not None:
        out |= {"from": h.from_, "to": h.to}
    return out


def _changed(location_id: str) -> None:
    registry.refresh_location(location_id)   # connections cache their Site row
    soc.invalidate()


@router.get("/api/locations/{location_id}/monitoring")
async def get_monitoring(location_id: str, u: dict = Depends(user)):
    loc, _ = auth.location_access(u, location_id)
    return _monitoring_out(u, loc)


@router.put("/api/locations/{location_id}/monitoring")
async def put_monitoring(location_id: str, body: MonitoringIn, u: dict = Depends(user)):
    loc = _configurable(u, location_id)
    sent = body.model_dump(exclude_unset=True, by_alias=True)
    vals: dict = {}
    if "monitored" in sent and body.monitored is not None:
        if body.monitored and not soc.valid_timezone(loc.get("timezone")):
            # arming windows are wall-clock times at the Site: without its timezone they would silently mean UTC
            raise HTTPException(409, "set the site's timezone before turning on monitoring")
        vals["monitored"] = body.monitored
    if "arm_schedule" in sent:
        vals["arm_schedule"] = [_window_json(w) for w in body.arm_schedule or []]
    if "arm_holidays" in sent:
        hol = sorted((_holiday_json(h) for h in body.arm_holidays or []), key=lambda h: h["date"])
        dupes = {h["date"] for h in hol if sum(1 for x in hol if x["date"] == h["date"]) > 1}
        if dupes:
            raise HTTPException(422, f"one entry per holiday date ({sorted(dupes)[0]} is listed twice)")
        vals["arm_holidays"] = hol
    if "soc_group_minutes" in sent:
        vals["soc_group_minutes"] = body.soc_group_minutes
    if vals:
        db.run(sa.update(db.locations).where(db.locations.c.id == location_id).values(**vals, updated_at=time.time()))
        _changed(location_id)
        if "monitored" in vals and bool(vals["monitored"]) != bool(loc.get("monitored")):
            action = f"site monitoring {'enabled' if vals['monitored'] else 'disabled'}: {loc['name']}"
        else:
            action = f"site monitoring updated: {loc['name']}"
        _audit(u, loc["org_id"], action, {"location_id": location_id, "changed": sorted(vals),
                                          **{k: v for k, v in vals.items() if k != "arm_holidays"}})
    loc = db.one(sa.select(db.locations).where(db.locations.c.id == location_id))
    return _monitoring_out(u, loc)


class ArmIn(BaseModel):
    mode: Literal["arm", "disarm"]
    until: float
    reason: str = Field(min_length=1, max_length=200)


@router.post("/api/locations/{location_id}/arm")
async def arm_override(location_id: str, body: ArmIn, u: dict = Depends(user)):
    """Arm or disarm a monitored Site now, until `until` (epoch seconds, at most 24 h ahead), with a reason."""
    loc, _ = auth.location_access(u, location_id)
    if not _can_arm(u, loc):
        raise HTTPException(403, "needs operator for this site's customer (or SOC operator)")
    if not loc.get("monitored"):
        raise HTTPException(409, "this site is not monitored")
    now = time.time()
    if body.until <= now:
        raise HTTPException(422, "until must be in the future")
    if body.until > now + MAX_OVERRIDE_S + 60:   # a minute of slack for the browser's clock and the round trip
        raise HTTPException(422, "an override lasts at most 24 hours")
    o = {"mode": body.mode, "until": min(body.until, now + MAX_OVERRIDE_S), "by": u["email"], "by_id": u["id"],
         "reason": body.reason.strip(), "at": now}
    db.run(sa.update(db.locations).where(db.locations.c.id == location_id).values(arm_override=o, updated_at=now))
    _changed(location_id)
    until_s = dt.datetime.fromtimestamp(o["until"], soc.tz_of(loc)).strftime("%Y-%m-%d %H:%M")
    _audit(u, loc["org_id"], f"site {body.mode}ed until {until_s}: {loc['name']} ({o['reason']})"[:200],
           {"location_id": location_id, "override": o})
    loc = db.one(sa.select(db.locations).where(db.locations.c.id == location_id))
    return _monitoring_out(u, loc)


@router.delete("/api/locations/{location_id}/arm")
async def clear_arm_override(location_id: str, u: dict = Depends(user)):
    """End the override: the schedule (or holiday) decides again."""
    loc, _ = auth.location_access(u, location_id)
    if not _can_arm(u, loc):
        raise HTTPException(403, "needs operator for this site's customer (or SOC operator)")
    if loc.get("arm_override") is not None:
        db.run(sa.update(db.locations).where(db.locations.c.id == location_id).values(arm_override=None, updated_at=time.time()))
        _changed(location_id)
        _audit(u, loc["org_id"], f"site arm override cleared: {loc['name']}", {"location_id": location_id, "was": loc["arm_override"]})
        loc = db.one(sa.select(db.locations).where(db.locations.c.id == location_id))
    return _monitoring_out(u, loc)


# ---------------------------------------------------------------- contacts and procedures
# PUT replaces the whole ordered list. Rows that come back with their `id` keep it (incident logs refer to contacts
# and procedure steps by id), rows without one are new, rows left out are deleted.

class ContactIn(BaseModel):
    id: int | None = None
    name: str = Field(min_length=1, max_length=120)
    role: str | None = Field(None, max_length=80)
    phone: str | None = Field(None, max_length=40)
    email: EmailStr | None = None
    notify_on_open: bool = False
    notes: str | None = Field(None, max_length=2000)

    @field_validator("email", "role", "phone", "notes", mode="before")
    @classmethod
    def _blank(cls, v):
        return None if isinstance(v, str) and not v.strip() else v


class ContactsIn(BaseModel):
    contacts: list[ContactIn] = Field(max_length=MAX_CONTACTS)


class StepIn(BaseModel):
    id: str | None = Field(None, max_length=24)
    text: str = Field(min_length=1, max_length=500)
    required: bool = False


class ProcedureIn(BaseModel):
    id: int | None = None
    title: str = Field(min_length=1, max_length=200)
    category: str | None = Field(None, max_length=60)
    steps: list[StepIn] = Field(default_factory=list, max_length=MAX_STEPS)
    priority: Literal["low", "medium", "high"] | None = None   # applies to incidents of this priority and above; None = all


class ProceduresIn(BaseModel):
    procedures: list[ProcedureIn] = Field(max_length=MAX_PROCEDURES)


CONTACT_KEYS = ("id", "order", "name", "role", "phone", "email", "notify_on_open", "notes", "created_at", "updated_at")
PROCEDURE_KEYS = ("id", "order", "title", "category", "steps", "priority", "created_at", "updated_at")


def contacts_for(location_id: str) -> list[dict]:
    t = db.location_contacts
    return [{k: r[k] for k in CONTACT_KEYS} | {"notify_on_open": bool(r["notify_on_open"])}
            for r in db.rows(sa.select(t).where(t.c.location_id == location_id).order_by(t.c["order"], t.c.id))]


def procedures_for(location_id: str) -> list[dict]:
    t = db.location_procedures
    return [{k: r[k] for k in PROCEDURE_KEYS} for r in db.rows(sa.select(t).where(t.c.location_id == location_id).order_by(t.c["order"], t.c.id))]


def _replace(table: sa.Table, loc: dict, items: list[dict]) -> None:
    """Upsert `items` (ordered) for this Site and delete the rest, in one transaction. An id that isn't one of this
    Site's rows is treated as new, so a list copied from another Site can't touch that Site's rows."""
    now = time.time()
    with db.engine().begin() as c:
        have = {r.id for r in c.execute(sa.select(table.c.id).where(table.c.location_id == loc["id"]))}
        keep = set()
        for i, it in enumerate(items):
            vals = {k: v for k, v in it.items() if k != "id"} | {"order": i, "updated_at": now}
            if it.get("id") in have and it["id"] not in keep:
                c.execute(sa.update(table).where(table.c.id == it["id"]).values(**vals))
                keep.add(it["id"])
            else:
                res = c.execute(table.insert().values(**vals, location_id=loc["id"], org_id=loc["org_id"], created_at=now))
                keep.add(res.inserted_primary_key[0])
        gone = have - keep
        if gone:
            c.execute(sa.delete(table).where(table.c.id.in_(list(gone))))


@router.get("/api/locations/{location_id}/contacts")
async def get_contacts(location_id: str, u: dict = Depends(user)):
    auth.location_access(u, location_id)
    return contacts_for(location_id)


@router.put("/api/locations/{location_id}/contacts")
async def put_contacts(location_id: str, body: ContactsIn, u: dict = Depends(user)):
    loc = _configurable(u, location_id)
    items = [{"id": c.id, "name": c.name.strip(), "role": c.role, "phone": c.phone, "email": str(c.email) if c.email else None,
              "notify_on_open": c.notify_on_open, "notes": c.notes} for c in body.contacts]
    _replace(db.location_contacts, loc, items)
    _audit(u, loc["org_id"], f"site contacts updated: {loc['name']} ({len(items)})",
           {"location_id": location_id, "names": [i["name"] for i in items]})
    return contacts_for(location_id)


@router.get("/api/locations/{location_id}/procedures")
async def get_procedures(location_id: str, u: dict = Depends(user)):
    auth.location_access(u, location_id)
    return procedures_for(location_id)


def _steps(steps: list[StepIn]) -> list[dict]:
    """Stable step ids (SOP ticks in the incident log refer to them): keep the ones given, number the rest."""
    used = {s.id for s in steps if s.id}
    out, n = [], 1
    for s in steps:
        sid = s.id
        if not sid or sid in {o["id"] for o in out}:
            while f"s{n}" in used:
                n += 1
            sid = f"s{n}"
            used.add(sid)
        out.append({"id": sid, "text": s.text.strip(), "required": s.required})
    return out


@router.put("/api/locations/{location_id}/procedures")
async def put_procedures(location_id: str, body: ProceduresIn, u: dict = Depends(user)):
    loc = _configurable(u, location_id)
    items = [{"id": p.id, "title": p.title.strip(), "category": (p.category or "").strip() or None, "steps": _steps(p.steps),
              "priority": p.priority} for p in body.procedures]
    _replace(db.location_procedures, loc, items)
    _audit(u, loc["org_id"], f"site procedures updated: {loc['name']} ({len(items)})",
           {"location_id": location_id, "titles": [i["title"] for i in items]})
    return procedures_for(location_id)


# ---------------------------------------------------------------- SOC vocabulary

@router.get("/api/soc/dispositions")
async def dispositions(u: dict = Depends(user)):
    auth.require_soc(u)
    return soc.dispositions_catalogue()
