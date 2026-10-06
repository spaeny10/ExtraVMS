"""SOC reports: operator performance, false-alarm rates, shift handover reports and monthly customer summaries.

Everything is computed from the incident tables (incidents, incident_events, incident_log) at request time, except
shift and monthly reports, which are also stored in soc_reports (kind "shift" / "monthly") so a supervisor can read
last night's handover and a customer can read past months without them changing as incidents are re-worked.

Report windows select incidents by opened_at in [since, until): an incident belongs to the period it started in,
so the same incident never counts in two reports. Percentiles are computed in Python (linear interpolation between
closest ranks, numpy's default), so SQLite in tests and Postgres in production agree to the decimal.

Shift reports are written by vLLM when the shared model is configured, with a plain-text fallback (the digest.py
pattern); monthly summaries are plain text (customer-facing: a fixed format is easier to trust than a summary).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import time
from collections import Counter, defaultdict

import sqlalchemy as sa

from . import db, soc, vlm_proxy
from .config import settings

log = logging.getLogger("hub.soc_reports")

FA_CODES = soc.FEEDBACK_DISPOSITIONS              # false_alarm + nuisance: "the detection was wrong"
SYSTEM_DISPOSITIONS = frozenset({"swept", "expired"})   # closed without anyone judging them: not in rate denominators
NOTABLE_MAX = 25
COVERAGE_STEP_S = 900                             # armed-hours estimate: sample the schedule every 15 min


# ---------------------------------------------------------------- helpers

def percentile(values, q: float) -> float | None:
    """Linear interpolation between closest ranks (numpy's default): p50 of [1, 2, 3, 4] is 2.5."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    f = math.floor(k)
    c = min(f + 1, len(xs) - 1)
    return round(xs[f] + (xs[c] - xs[f]) * (k - f), 1)


def spread(values) -> dict:
    xs = [v for v in values if v is not None]
    return {"n": len(xs), "p50": percentile(xs, 0.5), "p95": percentile(xs, 0.95)}


def _incidents(since: float, until: float, org_id: str | None = None, location_id: str | None = None) -> list[dict]:
    t = db.incidents
    q = sa.select(t).where(t.c.opened_at >= since, t.c.opened_at < until).order_by(t.c.opened_at, t.c.id)
    if org_id:
        q = q.where(t.c.org_id == org_id)
    if location_id:
        q = q.where(t.c.location_id == location_id)
    return db.rows(q)


def _names(table: sa.Table, ids) -> dict[str, str]:
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    return {r["id"]: r["name"] for r in db.rows(sa.select(table.c.id, table.c.name).where(table.c.id.in_(ids)))}


def _calls(log_rows: list[dict]) -> Counter:
    return Counter((r.get("detail") or {}).get("outcome") or "unknown" for r in log_rows if r["action"] == "call")


def _fa_share(dispositions: Counter, n: int) -> float | None:
    return round(sum(dispositions[c] for c in FA_CODES) / n, 3) if n else None


# ---------------------------------------------------------------- operators

def _holders(inc: dict, rows: list[dict]) -> dict:
    """Walk an incident's log: who held it when. Returns {claimers: [uid in order], escalations: Counter(uid ->
    escalated/overdue rows while they held it), escalated_claims: {uids who claimed it after it had escalated}}."""
    holder, claimers, esc, after_esc, escalated = None, [], Counter(), set(), False
    for r in rows:
        a, d = r["action"], (r.get("detail") or {})
        took = None
        if a in ("claim", "takeover"):
            took = r["user_id"]
        elif a == "handoff":
            took = d.get("to_user_id")
        elif a == "rejected" and d.get("to") == "claimed":
            took = d.get("resolver_id")
        elif a in ("release", "resolve", "swept", "expired") or a == "rejected":
            holder = None
        elif a in ("escalated", "overdue"):
            escalated = True
            if holder:
                esc[holder] += 1
        if took:
            holder = took
            if took not in claimers:
                claimers.append(took)
            if escalated and a != "rejected":
                after_esc.add(took)
    return {"claimers": claimers, "escalations": esc, "escalated_claims": after_esc}


def operator_stats(since: float, until: float, org_id: str | None = None) -> dict:
    """Per operator, over incidents opened in [since, until) (optionally one customer's):
    claimed (incidents they held at some point), resolved (incidents whose resolution is theirs), time to claim
    (first_claimed_at - opened_at, credited to whoever claimed first), time to resolve (resolved_at - opened_at),
    dispositions of what they resolved, escalations received (escalated / overdue rows while they held the
    incident: in practice overdue, since an escalating incident is unclaimed), escalated_claimed (incidents they
    picked up after they had escalated) and false-alarm share (false_alarm + nuisance over what they resolved)."""
    incs = _incidents(since, until, org_id)
    logs = soc.log_of([i["id"] for i in incs]) if incs else {}
    per: dict[str, dict] = defaultdict(lambda: {"claimed": set(), "resolved": 0, "ttc": [], "ttr": [], "dispositions": Counter(),
                                                "escalations_received": 0, "escalated_claimed": 0})
    for inc in incs:
        h = _holders(inc, logs.get(inc["id"], []))
        for uid in h["claimers"]:
            per[uid]["claimed"].add(inc["id"])
        if h["claimers"] and inc.get("first_claimed_at") is not None:
            per[h["claimers"][0]]["ttc"].append(inc["first_claimed_at"] - inc["opened_at"])
        for uid, n in h["escalations"].items():
            per[uid]["escalations_received"] += n
        for uid in h["escalated_claims"]:
            per[uid]["escalated_claimed"] += 1
        if inc.get("resolved_by") and inc.get("resolved_at") is not None:
            p = per[inc["resolved_by"]]
            p["resolved"] += 1
            p["ttr"].append(inc["resolved_at"] - inc["opened_at"])
            p["dispositions"][inc["disposition"] or "unknown"] += 1
    users = {r["id"]: r for r in db.rows(sa.select(db.users.c.id, db.users.c.email, db.users.c.soc_role, db.users.c.is_super)
                                         .where(db.users.c.id.in_(list(per) or [""])))}
    ops = []
    for uid, p in per.items():
        u = users.get(uid) or {}
        ops.append({"user_id": uid, "email": u.get("email"), "soc_role": soc.level_of(u) if u else None,
                    "claimed": len(p["claimed"]), "resolved": p["resolved"],
                    "time_to_claim": spread(p["ttc"]), "time_to_resolve": spread(p["ttr"]),
                    "dispositions": dict(p["dispositions"]), "escalations_received": p["escalations_received"],
                    "escalated_claimed": p["escalated_claimed"], "false_alarm_share": _fa_share(p["dispositions"], p["resolved"])})
    ops.sort(key=lambda o: ((o["email"] or "~").casefold(), o["user_id"]))
    resolved = [i for i in incs if i.get("resolved_at") is not None]
    disp = Counter(i["disposition"] or "unknown" for i in resolved)
    return {"since": since, "until": until, "org_id": org_id, "operators": ops,
            "totals": {"incidents": len(incs), "claimed": sum(1 for i in incs if i.get("first_claimed_at") is not None),
                       "resolved": len(resolved),
                       "time_to_claim": spread(i["first_claimed_at"] - i["opened_at"] for i in incs if i.get("first_claimed_at") is not None),
                       "time_to_resolve": spread(i["resolved_at"] - i["opened_at"] for i in resolved),
                       "dispositions": dict(disp), "false_alarm_share": _fa_share(disp, len(resolved))}}


# ---------------------------------------------------------------- false alarms

def _rate_row(closed: list[dict]) -> dict:
    disp = Counter(i["disposition"] or "unknown" for i in closed)
    judged = sum(n for c, n in disp.items() if c not in SYSTEM_DISPOSITIONS)
    fa = sum(disp[c] for c in FA_CODES)
    top = sorted(disp.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if disp else None   # ties: alphabetical, so stable
    return {"closed": len(closed), "judged": judged, "false_alarms": fa, "rate": round(fa / judged, 3) if judged else None,
            "top_disposition": top, "dispositions": dict(disp),
            "last_incident_at": max((i["opened_at"] for i in closed), default=None)}


def false_alarm_rate(since: float, until: float, org_id: str | None = None, location_ids: set[str] | None = None) -> dict:
    """Closed incidents opened in [since, until), per Site and per camera (server_id, camera_id): how many, how
    many were false_alarm or nuisance, the rate over judged incidents (swept and expired ones were never looked at,
    so they are counted in `closed` but not in the rate), the most common disposition and the newest incident."""
    closed = [i for i in _incidents(since, until, org_id) if i["state"] == "closed"
              and (location_ids is None or i["location_id"] in location_ids)]   # a Site-restricted customer admin
    by_loc: dict[str, list[dict]] = defaultdict(list)
    for i in closed:
        by_loc[i["location_id"]].append(i)
    locs = {r["id"]: r for r in db.rows(sa.select(db.locations.c.id, db.locations.c.name, db.locations.c.org_id)
                                        .where(db.locations.c.id.in_(list(by_loc) or [""])))}
    orgs = _names(db.orgs, {i["org_id"] for i in closed})
    sites = []
    for lid, items in by_loc.items():
        loc = locs.get(lid) or {}
        sites.append({"location_id": lid, "location_name": loc.get("name"), "org_id": items[0]["org_id"],
                      "org_name": orgs.get(items[0]["org_id"]), **_rate_row(items)})
    ie = db.incident_events
    by_inc = {i["id"]: i for i in closed}
    pairs: dict[tuple[str, str], dict[int, dict]] = defaultdict(dict)
    if by_inc:
        for e in db.rows(sa.select(ie.c.incident_id, ie.c.server_id, ie.c.camera_id).where(
                ie.c.incident_id.in_(list(by_inc)), ie.c.camera_id.is_not(None))):
            pairs[(e["server_id"], e["camera_id"])][e["incident_id"]] = by_inc[e["incident_id"]]   # once per incident
    srv_names = _names(db.sites, {k[0] for k in pairs})
    cam_names = {(r["server_id"], r["camera_id"]): r["name"] for r in db.rows(
        sa.select(db.cameras.c.server_id, db.cameras.c.camera_id, db.cameras.c.name).where(
            db.cameras.c.server_id.in_(list({k[0] for k in pairs}) or [""])))}
    cams = []
    for (sid, cid), items in pairs.items():
        first = next(iter(items.values()))
        cams.append({"server_id": sid, "server_name": srv_names.get(sid, sid), "camera_id": cid,
                     "camera_name": cam_names.get((sid, cid)) or cid, "location_id": first["location_id"],
                     "location_name": (locs.get(first["location_id"]) or {}).get("name"), **_rate_row(list(items.values()))})

    def order(r):   # worst first: most false alarms, then the highest rate
        return (-r["false_alarms"], -(r["rate"] or 0), (r.get("location_name") or r.get("camera_name") or "").casefold())
    return {"since": since, "until": until, "org_id": org_id, "totals": _rate_row(closed),
            "sites": sorted(sites, key=order), "cameras": sorted(cams, key=order)}


# ---------------------------------------------------------------- shift reports

def shift_ends() -> list[int]:
    """settings.soc_shift_ends as minutes after midnight, sorted; malformed entries are skipped, none valid = one
    report a day at 06:00 (a typo must not silence handover reports)."""
    out = sorted({m for m in (soc.parse_hhmm(x.strip()) for x in (settings.soc_shift_ends or "").split(",")) if m is not None})
    return out or [360]


def _boundaries(around: dt.datetime) -> list[dt.datetime]:
    days = [around.date() + dt.timedelta(days=d) for d in (-2, -1, 0, 1)]
    return sorted(dt.datetime(d.year, d.month, d.day, m // 60, m % 60) for d in days for m in shift_ends())


def last_shift(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """The latest completed shift at hub-local `now` (naive): (start, end), end <= now."""
    bs = _boundaries(now)
    i = max(k for k, b in enumerate(bs) if b <= now)
    return bs[i - 1], bs[i]


def next_shift_end(now: dt.datetime) -> dt.datetime:
    return min(b for b in _boundaries(now) if b > now)


def _fmt(ts: float | None, f: str = "%H:%M") -> str:
    return dt.datetime.fromtimestamp(ts).strftime(f) if ts else "-"


def _dur(s: float | None) -> str:
    if s is None:
        return "-"
    return f"{s:.0f} s" if s < 120 else f"{s / 60:.1f} min"


def shift_data(start: float, end: float) -> dict:
    incs = _incidents(start, end)
    logs = soc.log_of([i["id"] for i in incs]) if incs else {}
    rows = [r for i in incs for r in logs.get(i["id"], [])]
    calls = _calls(rows)
    tagged = {i["id"]: i for i in soc.tag(incs)} if incs else {}
    notable = [i for i in incs if i["priority"] == "high" or (i["escalation_level"] or 0) > 0]
    notable.sort(key=lambda i: (-soc.rank(i["priority"]), -(i["escalation_level"] or 0), i["opened_at"]))
    max_level = Counter()
    for i in incs:   # the highest level each incident reached (escalation_level is reset when a reject restarts it)
        lv = max([i["escalation_level"] or 0] + [int((r.get("detail") or {}).get("level") or 0)
                                                 for r in logs.get(i["id"], []) if r["action"] == "escalated"])
        max_level[str(lv)] += 1
    return {"start": start, "end": end,
            "counts": {"incidents": len(incs), "by_priority": dict(Counter(i["priority"] for i in incs)),
                       "by_lane": dict(Counter(i["lane"] for i in incs)), "by_escalation": dict(max_level),
                       "by_state": dict(Counter(i["state"] for i in incs)),
                       "overdue": sum(1 for r in rows if r["action"] == "overdue"),
                       "open_now": sum(1 for i in incs if i["state"] in soc.OPEN_STATES)},
            "dispositions": dict(Counter(i["disposition"] for i in incs if i["disposition"])),
            "calls": {"total": sum(calls.values()), "by_outcome": dict(calls)},
            "notable": [{"id": i["id"], "title": i["title"], "org_name": tagged[i["id"]]["org_name"],
                         "location_name": tagged[i["id"]]["location_name"], "opened_at": i["opened_at"], "priority": i["priority"],
                         "escalation_level": i["escalation_level"], "state": i["state"], "disposition": i["disposition"]}
                        for i in notable[:NOTABLE_MAX]],
            "notable_total": len(notable),
            "operators": operator_stats(start, end)["operators"]}


def shift_plain(d: dict) -> str:
    c = d["counts"]
    pr = ", ".join(f"{c['by_priority'][p]} {p}" for p in ("high", "medium", "low") if c["by_priority"].get(p)) or "none"
    lines = [f"Shift {_fmt(d['start'], '%Y-%m-%d %H:%M')} to {_fmt(d['end'], '%Y-%m-%d %H:%M')}: {c['incidents']} incident(s) ({pr}); "
             f"{c['by_lane'].get('ring', 0)} ringing, {c['by_lane'].get('quiet', 0)} quiet."]
    esc = {k: v for k, v in c["by_escalation"].items() if k != "0"}
    if esc or c["overdue"]:
        lines.append("Escalations: " + ", ".join(f"level {k}: {v}" for k, v in sorted(esc.items())) +
                     (f"{'; ' if esc else ''}{c['overdue']} overdue" if c["overdue"] else ""))
    if d["dispositions"]:
        lines.append("Dispositions: " + ", ".join(f"{soc.DISPOSITIONS.get(k, ('', k))[1]} {v}" for k, v in
                                                  sorted(d["dispositions"].items(), key=lambda kv: -kv[1])))
    if d["calls"]["total"]:
        lines.append(f"Calls: {d['calls']['total']} (" + ", ".join(f"{k} {v}" for k, v in d["calls"]["by_outcome"].items()) + ")")
    if c["open_now"]:
        lines.append(f"Still open: {c['open_now']}")
    if d["notable"]:
        lines.append("Notable:")
        for n in d["notable"]:
            where = " · ".join(x for x in (n["org_name"], n["location_name"]) if x)
            esc_s = f", level {n['escalation_level']}" if n["escalation_level"] else ""
            lines.append(f"  • {_fmt(n['opened_at'])} {where}: {n['title'] or 'incident'} ({n['priority']}{esc_s}) -> "
                         f"{n['disposition'] or n['state']}")
    if d["operators"]:
        lines.append("Operators:")
        for o in d["operators"]:
            lines.append(f"  • {o['email'] or o['user_id']}: claimed {o['claimed']}, resolved {o['resolved']}, "
                         f"claim p50 {_dur(o['time_to_claim']['p50'])}")
    return "\n".join(lines)


async def _shift_llm(d: dict) -> str | None:
    if not vlm_proxy.configured():
        return None
    messages = [
        {"role": "system", "content": "You write the shift handover report for a video-alarm monitoring center (SOC). Be factual and "
                                      "brief: one summary sentence, then bullets for escalations, true alarms and anything still open, "
                                      "then one line per operator. Use only the facts given; times are local. No preamble. "
                                      "Use American English spelling."},
        {"role": "user", "content": shift_plain(d)}]
    try:
        return await vlm_proxy.complete(messages, max_tokens=500, temperature=0.2)
    except Exception as e:
        log.warning("shift report: shared AI failed: %s", e)
    return None


def _store(kind: str, org_id: str | None, start: float, end: float, text: str, data: dict, model: str | None,
           created_by: str | None) -> dict:
    t = db.soc_reports
    with db.engine().begin() as c:
        rid = c.execute(t.insert().values(kind=kind, org_id=org_id, period_start=start, period_end=end, created_at=time.time(),
                                          created_by=created_by, text=text, data=data, model=model)).inserted_primary_key[0]
    return db.one(sa.select(t).where(t.c.id == rid))


async def shift_report(start: float, end: float, created_by: str | None = None) -> dict:
    """Build and store a shift report for incidents opened in [start, end). Returns the stored row."""
    d = shift_data(start, end)
    llm = await _shift_llm(d)
    return _store("shift", None, start, end, llm or shift_plain(d), d, settings.vllm_model if llm else None, created_by)


def stored(kind: str, org_id: str | None = None, limit: int = 20) -> list[dict]:
    t = db.soc_reports
    q = sa.select(t).where(t.c.kind == kind).order_by(t.c.period_end.desc(), t.c.id.desc()).limit(limit)
    if org_id:
        q = q.where(t.c.org_id == org_id)
    return db.rows(q)


def stored_one(rid: int, kind: str | None = None) -> dict | None:
    t = db.soc_reports
    q = sa.select(t).where(t.c.id == rid)
    if kind:
        q = q.where(t.c.kind == kind)
    return db.one(q)


# ---------------------------------------------------------------- monthly customer summary

def month_bounds(year: int, month: int) -> tuple[float, float]:
    """Hub-local calendar month (like the shift boundaries and the digest hour)."""
    start = dt.datetime(year, month, 1)
    end = dt.datetime(year + (month == 12), month % 12 + 1, 1)
    return start.timestamp(), end.timestamp()


def previous_month(today: dt.date) -> tuple[int, int]:
    return (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)


def armed_hours(loc: dict, start: float, end: float, step: float = COVERAGE_STEP_S) -> float:
    """An estimate from the Site's current schedule and holidays (overrides are not kept historically, so the
    current one is ignored): armed samples every 15 minutes, in hours."""
    if not loc.get("monitored"):
        return 0.0
    base = {**loc, "arm_override": None}
    n, ts = 0, start
    while ts < end:
        n += soc.compute_armed(base, ts)[0]
        ts += step
    return round(n * step / 3600, 1)


def monthly_data(org_id: str, year: int, month: int) -> dict:
    start, end = month_bounds(year, month)
    incs = _incidents(start, end, org_id)
    logs = soc.log_of([i["id"] for i in incs]) if incs else {}
    locs = db.rows(sa.select(db.locations).where(db.locations.c.org_id == org_id).order_by(db.locations.c.name))
    by_loc: dict[str, list[dict]] = defaultdict(list)
    for i in incs:
        by_loc[i["location_id"]].append(i)
    period_h = round((end - start) / 3600, 1)
    sites = []
    for loc in locs:
        items = by_loc.get(loc["id"], [])
        if not loc.get("monitored") and not items:
            continue
        calls = _calls([r for i in items for r in logs.get(i["id"], [])])
        hours = armed_hours(loc, start, end)
        sites.append({"location_id": loc["id"], "name": loc["name"], "timezone": loc.get("timezone"), "monitored": bool(loc.get("monitored")),
                      "incidents": len(items), "by_priority": dict(Counter(i["priority"] for i in items)),
                      "median_response_s": percentile([i["first_claimed_at"] - i["opened_at"] for i in items if i.get("first_claimed_at") is not None], 0.5),
                      "dispositions": dict(Counter(i["disposition"] for i in items if i["disposition"])),
                      "calls": sum(calls.values()), "armed_hours": hours, "period_hours": period_h,
                      "coverage": round(hours / period_h, 3) if period_h else None})
    all_calls = _calls([r for i in incs for r in logs.get(i["id"], [])])
    return {"org_id": org_id, "year": year, "month": month, "start": start, "end": end, "sites": sites,
            "totals": {"incidents": len(incs),
                       "median_response_s": percentile([i["first_claimed_at"] - i["opened_at"] for i in incs if i.get("first_claimed_at") is not None], 0.5),
                       "dispositions": dict(Counter(i["disposition"] for i in incs if i["disposition"])),
                       "calls": sum(all_calls.values()), "armed_hours": round(sum(s["armed_hours"] for s in sites), 1)}}


def monthly_plain(org_name: str, d: dict) -> str:
    label = dt.date(d["year"], d["month"], 1).strftime("%B %Y")
    t = d["totals"]
    lines = [f"{org_name}: SOC monitoring summary for {label}",
             f"{t['incidents']} incident(s) handled; median response {_dur(t['median_response_s'])}; {t['calls']} call(s) made."]
    for s in d["sites"]:
        disp = ", ".join(f"{soc.DISPOSITIONS.get(k, ('', k))[1]} {v}" for k, v in sorted(s["dispositions"].items(), key=lambda kv: -kv[1]))
        cov = f"{s['coverage'] * 100:.0f}%" if s["coverage"] is not None else "-"
        lines.append(f"• {s['name']}: {s['incidents']} incident(s), median response {_dur(s['median_response_s'])}, {s['calls']} call(s), "
                     f"armed about {s['armed_hours']:.0f} h ({cov} of the month){'; ' + disp if disp else ''}")
    return "\n".join(lines)


def monthly_scoped(row: dict, location_ids: set[str], org_name: str) -> dict:
    """A stored monthly summary cut down to these Sites (a Site-restricted customer admin): their Sites' rows, totals
    summed from them (the customer-wide median can't be split, so it is left out) and the text rebuilt."""
    d = row.get("data") if isinstance(row.get("data"), dict) else None
    if not d or "sites" not in d:
        return {**row, "text": "This summary covers Sites you don't have access to.", "data": None}
    sites = [x for x in d["sites"] if x.get("location_id") in location_ids]
    disp: Counter = Counter()
    for x in sites:
        disp.update(x.get("dispositions") or {})
    totals = {"incidents": sum(x.get("incidents") or 0 for x in sites), "median_response_s": None, "dispositions": dict(disp),
              "calls": sum(x.get("calls") or 0 for x in sites), "armed_hours": round(sum(x.get("armed_hours") or 0 for x in sites), 1)}
    nd = {**d, "sites": sites, "totals": totals}
    return {**row, "text": monthly_plain(org_name, nd), "data": nd}


def monthly_customer_summary(org_id: str, year: int, month: int, created_by: str | None = None) -> dict:
    """Build and store (kind "monthly") a customer's month. Returns the stored row. No SOC staff names: customers
    read this."""
    org = db.one(sa.select(db.orgs.c.name).where(db.orgs.c.id == org_id))
    d = monthly_data(org_id, year, month)
    return _store("monthly", org_id, d["start"], d["end"], monthly_plain(org["name"] if org else org_id, d), d, None, created_by)


def monthly_stored(org_id: str, year: int, month: int) -> dict | None:
    start, end = month_bounds(year, month)
    t = db.soc_reports
    return db.one(sa.select(t).where(t.c.kind == "monthly", t.c.org_id == org_id, t.c.period_start == start, t.c.period_end == end)
                  .order_by(t.c.id.desc()).limit(1))


# ---------------------------------------------------------------- the loop

async def run_due(end: dt.datetime) -> list[dict]:
    """What shift_loop does at a boundary `end` (hub-local, naive): the shift that just ended, and on the 1st the
    previous month for every customer with a monitored Site. Idempotent (an already stored report is skipped), and
    nothing at all while no Site is monitored."""
    if not soc.monitored_org_ids():
        return []
    made = []
    start, end_ = last_shift(end)
    t = db.soc_reports
    if not db.one(sa.select(t.c.id).where(t.c.kind == "shift", t.c.period_end == end_.timestamp())):
        made.append(await shift_report(start.timestamp(), end_.timestamp()))
    if end.day == 1:
        y, m = previous_month(end.date())
        for org_id in sorted(soc.monitored_org_ids()):
            if monthly_stored(org_id, y, m) is None:
                made.append(await asyncio.to_thread(monthly_customer_summary, org_id, y, m))
    return made


async def shift_loop() -> None:
    """api.lifespan runs this: sleep to the next shift boundary (settings.soc_shift_ends, hub-local), then run_due."""
    while True:
        now = dt.datetime.now()
        nxt = next_shift_end(now)
        await asyncio.sleep(max(1.0, (nxt - now).total_seconds()))
        try:
            await run_due(nxt)
        except Exception:
            log.exception("soc reports at %s", nxt)
