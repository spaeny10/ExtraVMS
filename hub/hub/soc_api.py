"""SOC routes: SOC staff management, per-Site monitoring and arming, contacts and procedures, the dispositions
catalog (stage 1), and the incident queue, its socket and operator presence (stage 2). The incident engine itself
is soc.py; these routes check who may act and translate soc.SocError into HTTP errors.

Who may do what:
  /api/hub/soc/members                       hub administrators (like /api/hub/admins)
  GET  /api/locations/{id}/monitoring        anyone who can see the Site
  PUT  /api/locations/{id}/monitoring        admin of the Site's customer, which includes SOC supervisors (auth.membership)
  POST/DELETE /api/locations/{id}/arm        operator or above there, which includes SOC operators
  GET  /api/locations/{id}/contacts|procedures   anyone who can see the Site
  PUT  /api/locations/{id}/contacts|procedures   as monitoring PUT
  GET  /api/soc/dispositions                 SOC staff
  /api/soc/incidents..., presence, overview, sites, sla (GET), ws     SOC staff (operator or supervisor)
  takeover, verify, reject, PUT /api/soc/sla   SOC supervisors (hub administrators count as supervisors)
  GET  /api/locations/{id}/incidents         anyone who can see the Site (what the SOC did there)
  GET  /api/soc/reports/operators|false-alarms|shifts[/{id}]   SOC supervisors (colleagues' numbers)
  POST /api/soc/reports/shifts/generate, GET /api/soc/reports/customers/{org}   SOC supervisors
  GET  /api/orgs/{org}/soc/reports|false-alarms   a real admin of that customer (require_customer_role)
Every write is audited in the customer's audit log (SOC staff changes hub-level, org_id NULL).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, EmailStr, Field, field_validator

from . import auth, db, geocode, soc, soc_reports
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
    loc = db.one(sa.select(db.locations).where(db.locations.c.id == location_id))
    if loc:
        # the SOC's Sites board and arming banners follow without polling
        site = soc_site(loc)
        soc.broadcast(soc.frame("arming", site=site, location_id=loc["id"], armed=site["armed"], reason=site["reason"]))


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


# ================================================================ incidents (stage 2)
# Every action route answers {"incident": <tagged row>, ...}; soc.SocError subclasses carry their HTTP status
# (409 for a lost race, e.g. "already claimed by op@example.com 42 s ago").

def _soc(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except soc.SocError as e:
        raise HTTPException(e.status, str(e))


async def _soc_async(fn, *args, **kw):
    try:
        return await fn(*args, **kw)
    except soc.SocError as e:
        raise HTTPException(e.status, str(e))


def _staff(u: dict, level: str = "operator") -> str:
    return auth.require_soc(u, level)


def _csv(v: str | None, allowed: tuple[str, ...], name: str) -> list[str] | None:
    if not v:
        return None
    items = [x for x in v.split(",") if x]
    bad = [x for x in items if x not in allowed]
    if bad:
        raise HTTPException(422, f"{name} is one of {', '.join(allowed)}")
    return items


@router.get("/api/soc/incidents")
async def list_incidents(state: str | None = None, lane: str | None = None, org: str | None = None, location: str | None = None,
                         since: float | None = None, limit: int = Query(200, ge=1, le=1000), u: dict = Depends(user)):
    """The queue (default: every open incident), highest priority first, then oldest first. state: comma list."""
    _staff(u)
    lanes = _csv(lane, soc.LANES, "lane")
    return soc.query(_csv(state, soc.STATES, "state") or soc.OPEN_STATES, lanes[0] if lanes and len(lanes) == 1 else None,
                     org, location, since, limit)


def _applies(proc: dict, priority: str) -> bool:
    return not proc.get("priority") or soc.rank(priority) >= soc.rank(proc["priority"])


def _procedures_with_progress(inc: dict, log_rows: list[dict]) -> list[dict]:
    """The Site's procedures that apply at this priority, each step with its state derived from the log."""
    prog = soc.sop_progress(log_rows)
    out = []
    for p in procedures_for(inc["location_id"]):
        if not _applies(p, inc["priority"]):
            continue
        steps = [{**s, **(prog.get((p["id"], s["id"])) or {"done": False, "by": None, "at": None, "note": None})} for s in p["steps"] or []]
        need = [s for s in steps if s.get("required")] or steps
        out.append({**p, "steps": steps, "done_count": sum(1 for s in steps if s["done"]), "complete": all(s["done"] for s in need)})
    return out


@router.get("/api/soc/incidents/{iid}")
async def incident_detail(iid: int, u: dict = Depends(user)):
    _staff(u)
    inc = _soc(soc.get, iid)
    logs = soc.log_of(iid)[iid]
    loc = db.one(sa.select(db.locations).where(db.locations.c.id == inc["location_id"])) or {}
    # `site`: where to send help (the Dispatch block): the address an operator reads to police, and the point
    site = {"id": inc["location_id"], "name": loc.get("name"), "address": loc.get("address") or "", "timezone": loc.get("timezone"), **geocode.place_of(loc)}
    return {"incident": inc, "site": site, "events": soc.events_of(iid), "log": logs, "contacts": contacts_for(inc["location_id"]),
            "procedures": _procedures_with_progress(inc, logs), "calls": [r for r in logs if r["action"] == "call"]}


class HandoffIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=24)


class ResolveIn(BaseModel):
    disposition: str = Field(min_length=1, max_length=32)
    notes: str | None = Field(None, max_length=4000)


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class CallIn(BaseModel):
    contact_id: int
    outcome: Literal["spoke", "voicemail", "no_answer", "busy", "dispatched", "refused"]
    notes: str | None = Field(None, max_length=2000)


class SopIn(BaseModel):
    procedure_id: int
    step_id: str = Field(min_length=1, max_length=24)
    done: bool = True
    note: str | None = Field(None, max_length=2000)


class RelayIn(BaseModel):
    server_id: str = Field(min_length=1, max_length=24)
    camera_id: str = Field(min_length=1, max_length=64)
    on: bool


class SweepIn(BaseModel):
    location_id: str | None = None


@router.post("/api/soc/incidents/sweep")
async def sweep_incidents(body: SweepIn | None = None, u: dict = Depends(user)):
    """Close the unclaimed quiet lane (optionally one Site) as swept."""
    _staff(u)
    ids = soc.sweep(u, body.location_id if body else None)
    return {"swept": ids, "count": len(ids)}


@router.post("/api/soc/incidents/{iid}/claim")
async def claim_incident(iid: int, u: dict = Depends(user)):
    _staff(u)
    return {"incident": _soc(soc.claim, iid, u)}


@router.post("/api/soc/incidents/{iid}/release")
async def release_incident(iid: int, u: dict = Depends(user)):
    return {"incident": _soc(soc.release, iid, u, _staff(u))}


@router.post("/api/soc/incidents/{iid}/handoff")
async def handoff_incident(iid: int, body: HandoffIn, u: dict = Depends(user)):
    return {"incident": _soc(soc.handoff, iid, u, _staff(u), body.user_id)}


@router.post("/api/soc/incidents/{iid}/takeover")
async def takeover_incident(iid: int, u: dict = Depends(user)):
    _staff(u, "supervisor")
    return {"incident": _soc(soc.takeover, iid, u)}


@router.post("/api/soc/incidents/{iid}/resolve")
async def resolve_incident(iid: int, body: ResolveIn, u: dict = Depends(user)):
    """Close (or send to four-eyes verification). False alarms are reported back to each event's server."""
    inc = _soc(soc.resolve, iid, u, _staff(u), body.disposition, body.notes)
    feedback = None
    if body.disposition in soc.FEEDBACK_DISPOSITIONS:
        feedback = await soc.send_feedback(iid, u, body.notes)
        inc = soc.get(iid)
    return {"incident": inc, "feedback": feedback}


@router.post("/api/soc/incidents/{iid}/verify")
async def verify_incident(iid: int, u: dict = Depends(user)):
    _staff(u, "supervisor")
    return {"incident": _soc(soc.verify, iid, u)}


class RejectIn(BaseModel):
    note: str = Field(min_length=1, max_length=4000)


@router.post("/api/soc/incidents/{iid}/reject")
async def reject_incident(iid: int, body: RejectIn, u: dict = Depends(user)):
    """Four eyes: send a pending_verify incident back to its resolver (or the queue when they are off shift)."""
    _staff(u, "supervisor")
    return {"incident": _soc(soc.reject, iid, u, body.note)}


@router.post("/api/soc/incidents/{iid}/note")
async def note_incident(iid: int, body: NoteIn, u: dict = Depends(user)):
    _staff(u)
    return {"incident": _soc(soc.note, iid, u, body.text)}


@router.post("/api/soc/incidents/{iid}/promote")
async def promote_incident(iid: int, u: dict = Depends(user)):
    _staff(u)
    return {"incident": _soc(soc.promote, iid, u)}


@router.post("/api/soc/incidents/{iid}/calls")
async def call_incident(iid: int, body: CallIn, u: dict = Depends(user)):
    return {"incident": _soc(soc.log_call, iid, u, _staff(u), body.contact_id, body.outcome, body.notes)}


@router.post("/api/soc/incidents/{iid}/sop")
async def sop_incident(iid: int, body: SopIn, u: dict = Depends(user)):
    lvl = _staff(u)
    inc = _soc(soc.sop_tick, iid, u, lvl, body.procedure_id, body.step_id, body.done, body.note)
    return {"incident": inc, "procedures": _procedures_with_progress(inc, soc.log_of(iid)[iid])}


@router.post("/api/soc/incidents/{iid}/relay")
async def relay_incident(iid: int, body: RelayIn, u: dict = Depends(user)):
    """Deterrence: a camera relay at the incident's Site (the camera must be on one of its servers)."""
    return await _soc_async(soc.relay, iid, u, _staff(u), body.server_id, body.camera_id, body.on)


# ---------------------------------------------------------------- SLA

class SlaRuleIn(BaseModel):
    claim_s: int | None = Field(None, ge=1, le=86400)
    resolve_s: int | None = Field(None, ge=1, le=86400)
    lane: Literal["ring", "quiet"] | None = None


class SlaIn(BaseModel):
    high: SlaRuleIn | None = None
    medium: SlaRuleIn | None = None
    low: SlaRuleIn | None = None


def _sla_out() -> dict:
    return {"sla": soc.sla(), "defaults": soc.SLA}


@router.get("/api/soc/sla")
async def get_sla(u: dict = Depends(user)):
    _staff(u)
    return _sla_out()


@router.put("/api/soc/sla")
async def put_sla(body: SlaIn, u: dict = Depends(user)):
    """Partial: per priority, only the fields sent change (claim_s / resolve_s null = no clock). New incidents use
    it; open ones keep the clocks they started with."""
    _staff(u, "supervisor")
    row = db.one(sa.select(db.kv.c.value).where(db.kv.c.key == soc.SLA_KV))
    over = dict(row["value"]) if row and isinstance(row["value"], dict) else {}
    sent = body.model_dump(exclude_unset=True)
    for p, rule in sent.items():
        if rule is None:
            over.pop(p, None)   # null priority: back to the defaults
        else:
            over[p] = {**(over.get(p) or {}), **rule}
    with db.engine().begin() as c:
        c.execute(sa.delete(db.kv).where(db.kv.c.key == soc.SLA_KV))
        c.execute(db.kv.insert().values(key=soc.SLA_KV, value=over))
    _audit(u, None, "soc sla updated", {"sent": sent, "override": over})
    return _sla_out()


# ---------------------------------------------------------------- presence, overview, Sites

class PresenceIn(BaseModel):
    status: Literal["available", "engaged", "break", "offline"]


@router.get("/api/soc/presence")
async def get_presence(u: dict = Depends(user)):
    _staff(u)
    return soc.roster()


@router.put("/api/soc/presence")
async def put_presence(body: PresenceIn, u: dict = Depends(user)):
    """The workstation's status selector, and its 30 s heartbeat (resending the current status)."""
    _staff(u)
    cur = soc.presence_of(u["id"])
    if cur and cur["status"] == body.status and cur["on_shift"]:
        soc.touch_presence(u["id"])   # a heartbeat: no broadcast, `since` unchanged
        return soc.presence_of(u["id"])
    return _soc(soc.set_presence, u["id"], body.status)


@router.get("/api/soc/overview")
async def overview(u: dict = Depends(user)):
    """Supervisor tiles: open incidents by state / lane / escalation level, SLA breaches, and each operator's load."""
    _staff(u)
    now = time.time()
    t = db.incidents
    rows = db.rows(sa.select(t.c.state, t.c.lane, t.c.escalation_level, t.c.claimed_by, t.c.resolved_by, t.c.sla_due_at,
                             t.c.resolve_due_at, t.c.opened_at).where(t.c.state.in_(list(soc.OPEN_STATES))))
    active = [r for r in rows if r["state"] in soc.ACTIVE_STATES]   # escalation is about incidents still being worked
    by_state = {s: 0 for s in soc.STATES}
    by_lane = {lane: 0 for lane in soc.LANES}
    by_esc: dict[str, int] = {}
    for r in rows:
        by_state[r["state"]] += 1
        by_lane[r["lane"]] = by_lane.get(r["lane"], 0) + 1
        by_esc[str(r["escalation_level"] or 0)] = by_esc.get(str(r["escalation_level"] or 0), 0) + 1
    closed_24h = db.rows(sa.select(t.c.resolved_by, t.c.disposition).where(t.c.state == "closed", t.c.closed_at >= now - 86400))
    by_state["closed"] = len(closed_24h)   # closed: the last 24 h, not all time
    unclaimed = [r for r in rows if r["state"] == "new" and r["lane"] == "ring"]
    ops = []
    for e in soc.roster(now):
        ops.append({**e, "claimed": sum(1 for r in rows if r["state"] == "claimed" and r["claimed_by"] == e["user_id"]),
                    "pending_verify": sum(1 for r in rows if r["state"] == "pending_verify" and r["resolved_by"] == e["user_id"]),
                    "resolved_24h": sum(1 for r in closed_24h if r["resolved_by"] == e["user_id"])})
    snd, ring = soc.sound()
    return {"now": now, "by_state": by_state, "by_lane": by_lane, "by_escalation": by_esc,
            "breaches": {"claim": sum(1 for r in rows if r["state"] == "new" and r["sla_due_at"] and r["sla_due_at"] < now),
                         "resolve": sum(1 for r in rows if r["state"] == "claimed" and r["resolve_due_at"] and r["resolve_due_at"] < now)},
            "oldest_unclaimed_at": min((r["opened_at"] for r in unclaimed), default=None),
            # the escalation engine's state: open incidents at each level, and claimed ones past their resolve clock
            "escalations": {f"level{n}": sum(1 for r in active if (r["escalation_level"] or 0) == n) for n in (1, 2, 3)},
            "overdue": sum(1 for r in rows if r["state"] == "claimed" and r["resolve_due_at"] and r["resolve_due_at"] < now),
            "ring_count": ring, "sound": snd, "operators": ops}


def soc_site(loc: dict, counts: dict | None = None, orgs: dict | None = None, servers: list[dict] | None = None) -> dict:
    """A monitored Site as the SOC's Sites board shows it (also the `site` of an `arming` frame)."""
    now = time.time()
    armed, reason = soc.armed_now(loc, now)
    if orgs is None:
        orgs = {r["id"]: r["name"] for r in db.rows(sa.select(db.orgs.c.id, db.orgs.c.name).where(db.orgs.c.id == loc["org_id"]))}
    if counts is None:
        t = db.incidents
        counts = {(r["location_id"], r["state"], r["lane"]): r["n"] for r in db.rows(
            sa.select(t.c.location_id, t.c.state, t.c.lane, sa.func.count().label("n")).where(
                t.c.location_id == loc["id"], t.c.state.in_(list(soc.OPEN_STATES))).group_by(t.c.location_id, t.c.state, t.c.lane))}
    if servers is None:
        servers = db.rows(sa.select(db.sites.c.id, db.sites.c.online, db.sites.c.location_id).where(
            db.sites.c.location_id == loc["id"], db.sites.c.retired_at.is_(None)))
    mine = [s for s in servers if s["location_id"] == loc["id"]]
    return {"id": loc["id"], "org_id": loc["org_id"], "org_name": orgs.get(loc["org_id"]), "name": loc["name"],
            "timezone": loc.get("timezone"), "monitored": bool(loc.get("monitored")), "armed": armed, "reason": reason,
            "next_change": soc.next_change(loc, now), "override": soc.active_override(loc, now),
            "open_incidents": sum(n for (lid, _, _), n in counts.items() if lid == loc["id"]),
            "ringing": counts.get((loc["id"], "new", "ring"), 0),
            "servers_total": len(mine), "servers_online": sum(1 for s in mine if s["online"] and registry.get(s["id"]) is not None),
            "address": loc.get("address") or "", **geocode.place_of(loc)}


@router.get("/api/soc/sites")
async def soc_sites(u: dict = Depends(user)):
    """Every monitored Site across customers, with its armed state and open incidents."""
    _staff(u)
    locs = db.rows(sa.select(db.locations).where(db.locations.c.monitored.is_(True)))
    if not locs:
        return []
    orgs = {r["id"]: r["name"] for r in db.rows(sa.select(db.orgs.c.id, db.orgs.c.name).where(db.orgs.c.id.in_(list({l["org_id"] for l in locs}))))}
    t = db.incidents
    counts = {(r["location_id"], r["state"], r["lane"]): r["n"] for r in db.rows(
        sa.select(t.c.location_id, t.c.state, t.c.lane, sa.func.count().label("n")).where(t.c.state.in_(list(soc.OPEN_STATES)))
        .group_by(t.c.location_id, t.c.state, t.c.lane))}
    servers = db.rows(sa.select(db.sites.c.id, db.sites.c.online, db.sites.c.location_id).where(
        db.sites.c.location_id.in_([l["id"] for l in locs]), db.sites.c.retired_at.is_(None)))
    out = [soc_site(loc, counts, orgs, servers) for loc in locs]
    return sorted(out, key=lambda s: ((s["org_name"] or "").casefold(), s["name"].casefold()))


# ---------------------------------------------------------------- the SOC socket

@router.websocket("/api/soc/ws")
async def soc_ws(ws: WebSocket):
    """Live queue for SOC staff: a `snapshot` frame (open incidents, roster), then incident_opened | incident_updated |
    incident_event_added | incident_resolved | presence | arming. Every frame carries `sound` and `ring_count`; an
    incident_updated sent by the escalation engine also carries `escalation` {level, overdue?}.
    Connecting sets the user available, the last tab closing sets them offline; the open socket keeps them on shift.
    Client messages (any text) count as a heartbeat."""
    u = auth.current_user(ws)  # type: ignore[arg-type]
    if not u or not auth.soc_level(u):
        await ws.close(code=4401 if not u else 4403)
        return
    await ws.accept()
    q = soc.subscribe()
    soc.socket_opened(u)

    async def pump():
        checked = time.time()
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), 30)
            except asyncio.TimeoutError:
                soc.touch_presence(u["id"])
                msg = None
            if time.time() - checked > 60:   # a revoked SOC role ends the feed within a minute
                checked = time.time()
                fresh = auth.user_by_id(u["id"])
                if not auth.soc_level(fresh):
                    await ws.close(code=4403)
                    return
            if msg is not None:
                await ws.send_json(msg)

    task = None
    try:
        await ws.send_json(soc.snapshot())
        task = asyncio.create_task(pump())
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect":
                break
            soc.touch_presence(u["id"])
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if task:
            task.cancel()
        soc.unsubscribe(q)
        soc.socket_closed(u)


# ---------------------------------------------------------------- customers: what the SOC did at their Site

@router.get("/api/locations/{location_id}/incidents")
async def location_incidents(location_id: str, since: float | None = None, limit: int = Query(50, ge=1, le=200),
                             u: dict = Depends(user)):
    """The Site's incidents, newest first, each with its log (the Site's Alerts tab). SOC staff appear as "SOC"
    unless they are also members of this customer: a customer sees what was done, not who on the SOC rota did it."""
    loc, _ = auth.location_access(u, location_id)
    t = db.incidents
    q = sa.select(t).where(t.c.location_id == location_id).order_by(t.c.opened_at.desc(), t.c.id.desc()).limit(limit)
    if since is not None:
        q = q.where(t.c.opened_at >= since)
    rows = soc.tag(db.rows(q))
    members = {r["user_id"] for r in db.rows(sa.select(db.memberships.c.user_id).where(db.memberships.c.org_id == loc["org_id"]))}
    logs = soc.log_of([r["id"] for r in rows])

    def who(uid, email):
        return email if uid in members else ("SOC" if uid else None)
    out = []
    for r in rows:
        r = {**r, **{f"{k}_email": who(r.get(k), r.get(f"{k}_email")) for k in soc.USER_FIELDS}}
        r["log"] = [{"id": lr["id"], "ts": lr["ts"], "action": lr["action"], "by": who(lr["user_id"], lr["user_email"]),
                     "detail": lr["detail"]} for lr in logs.get(r["id"], [])]
        out.append(r)
    return out


# ================================================================ reports (stage 5, soc_reports.py)
# Windows are epoch seconds; incidents count in the period they opened in. Defaults: the last 7 days for SOC
# reports, the last 30 for a customer's false-alarm view.

WEEK_S, MONTH_S = 7 * 86400, 30 * 86400
MAX_SPAN_S = 400 * 86400   # a year and a bit: enough for any report, bounded so one request can't scan everything


def _window(since: float | None, until: float | None, default_s: float) -> tuple[float, float]:
    until = time.time() if until is None else until
    since = until - default_s if since is None else since
    if since >= until:
        raise HTTPException(422, "since must be before until")
    if until - since > MAX_SPAN_S:
        raise HTTPException(422, "a report covers at most 400 days")
    return since, until


def _report_out(r: dict, customer: bool = False) -> dict:
    out = {k: r[k] for k in ("id", "kind", "org_id", "period_start", "period_end", "created_at", "text", "data", "model")}
    if not customer:
        out["created_by"] = r.get("created_by")
    return out


@router.get("/api/soc/reports/operators")
async def report_operators(since: float | None = None, until: float | None = None, org: str | None = None, u: dict = Depends(user)):
    _staff(u, "supervisor")
    return soc_reports.operator_stats(*_window(since, until, WEEK_S), org)


@router.get("/api/soc/reports/false-alarms")
async def report_false_alarms(since: float | None = None, until: float | None = None, org: str | None = None, u: dict = Depends(user)):
    _staff(u, "supervisor")
    return soc_reports.false_alarm_rate(*_window(since, until, WEEK_S), org)


@router.get("/api/soc/reports/shifts")
async def report_shifts(limit: int = Query(20, ge=1, le=200), u: dict = Depends(user)):
    """Stored shift reports, newest shift first."""
    _staff(u, "supervisor")
    return [_report_out(r) for r in soc_reports.stored("shift", limit=limit)]


@router.get("/api/soc/reports/shifts/{rid}")
async def report_shift(rid: int, u: dict = Depends(user)):
    _staff(u, "supervisor")
    r = soc_reports.stored_one(rid, "shift")
    if r is None:
        raise HTTPException(404, "unknown shift report")
    return _report_out(r)


class ShiftGenIn(BaseModel):
    start: float | None = None
    end: float | None = None


@router.post("/api/soc/reports/shifts/generate")
async def report_shift_generate(body: ShiftGenIn | None = None, u: dict = Depends(user)):
    """Build and store a shift report now: [start, end), default the last completed shift (settings.soc_shift_ends)."""
    _staff(u, "supervisor")
    body = body or ShiftGenIn()
    if body.start is None and body.end is None:
        st, en = soc_reports.last_shift(dt.datetime.now())
        start, end = st.timestamp(), en.timestamp()
    else:
        end = body.end if body.end is not None else time.time()
        start = body.start if body.start is not None else end - 8 * 3600
    start, end = _window(start, end, 0)
    r = await soc_reports.shift_report(start, end, created_by=u["id"])
    _audit(u, None, "soc shift report generated", {"report_id": r["id"], "start": start, "end": end})
    return _report_out(r)


def _month(year: int | None, month: int | None) -> tuple[int, int]:
    if year is None and month is None:
        return soc_reports.previous_month(dt.date.today())
    if year is None or month is None:
        raise HTTPException(422, "give both year and month, or neither (last month)")
    if not (1 <= month <= 12 and 2000 <= year <= 2100):
        raise HTTPException(422, "month is 1..12 and year 2000..2100")
    return year, month


@router.get("/api/soc/reports/customers/{org_id}")
async def report_customer(org_id: str, year: int | None = None, month: int | None = None, u: dict = Depends(user)):
    """A customer's monthly summary (default last month): the stored one, or built and stored now if missing."""
    _staff(u, "supervisor")
    if not db.one(sa.select(db.orgs.c.id).where(db.orgs.c.id == org_id)):
        raise HTTPException(404, "unknown customer")
    y, m = _month(year, month)
    r = soc_reports.monthly_stored(org_id, y, m)
    if r is None:
        r = await asyncio.to_thread(soc_reports.monthly_customer_summary, org_id, y, m, u["id"])
    return _report_out(r)


# ---------------------------------------------------------------- customers: their SOC reports

@router.get("/api/orgs/{org_id}/soc/reports")
async def customer_soc_reports(org_id: str, limit: int = Query(12, ge=1, le=60), u: dict = Depends(user)):
    """The customer's stored monthly summaries, newest first (a real admin of the customer: SOC supervisors read
    them through /api/soc/reports/customers)."""
    auth.require_customer_role(u, org_id, "admin")
    rows = soc_reports.stored("monthly", org_id=org_id, limit=limit)
    m = auth.membership(u, org_id)
    if m and not m["all_sites"]:   # a Site-restricted admin: their Sites' part of each month
        locs = auth.granted_location_ids(u["id"], org_id)
        org = db.one(sa.select(db.orgs.c.name).where(db.orgs.c.id == org_id))
        rows = [soc_reports.monthly_scoped(r, locs, org["name"] if org else org_id) for r in rows]
    return [_report_out(r, customer=True) for r in rows]


@router.get("/api/orgs/{org_id}/soc/false-alarms")
async def customer_false_alarms(org_id: str, since: float | None = None, until: float | None = None, u: dict = Depends(user)):
    """False-alarm rates at the customer's Sites and cameras since `since` (default 30 days): which cameras the
    SOC keeps dismissing, so the customer can re-aim or re-zone them. `until` defaults to now."""
    auth.require_customer_role(u, org_id, "admin")
    m = auth.membership(u, org_id)
    locs = None if not m or m["all_sites"] else auth.granted_location_ids(u["id"], org_id)   # Site-restricted: their Sites
    return soc_reports.false_alarm_rate(*_window(since, until, MONTH_S), org_id, locs)
