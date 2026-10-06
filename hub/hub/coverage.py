"""Cellular coverage per Site from the CoverageMap API (https://enterprise.coveragemap.com/api/v1, docs/guides-documentation.md).

The question a Site answers: which carrier, and will the upload carry the cameras? A typical Site is 5 cameras behind a
Peplink BR1 Pro 5G. One `POST /coverage` looks up a batch of up to 100 locations (the Site's coordinates, else its
address) in the configured datasets for LTE and 5G and the default carriers (AT&T, Verizon, T-Mobile):
  summary       1 unit per location: overall / performance / coverage / reliability scores 0-10
  fcc-coverage  1 unit per location: FCC modeled signal (dBm) and the covered share at 0.5 / 1 / 2 km
  speed-tests   2 units per location, only when some carrier/technology has tests nearby: download / upload / latency
Units are counted from each answer exactly as the vendor bills them (count_units) and kept per month in coverage_usage;
HUB_COVERAGEMAP_MONTHLY_UNITS caps them (every call checks the worst case first; the cap reached stops the daily refresh
and raises the hub-level `coverage_budget` alert).

Licensing. The free trial is for evaluation only and may not be shown in any product: with HUB_COVERAGEMAP_PLAN=trial
only hub administrators see or fetch anything ("Evaluation only"). With `paid` everyone who sees a Site sees its
coverage; fetching stays with hub administrators and the Site's admins. A row fetched during the trial
(plan_at_fetch = trial) is never shown to a customer, even after switching to paid: it is due for a refetch at once.
Paid plans may store answers only while subscribed: `python -m hub coverage purge` deletes everything (rows, usage,
the address-check cache). Emptying HUB_COVERAGEMAP_KEY switches the feature off but deletes nothing.

Refresh: a daily loop refetches rows older than HUB_COVERAGEMAP_REFRESH_DAYS, rows whose Site moved more than 100 m
(also kicked right away by a Site update that moves the pin), and (paid) trial rows; on `paid` it also fetches every
located Site that has none yet, on `trial` only Sites someone fetched by hand. A failed lookup (network, 5xx, 400) keeps
the previous data. The API key never reaches a log or an error message (redact).
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import logging
import math
import time

import httpx
import sqlalchemy as sa

from . import alerts, db
from .config import settings

log = logging.getLogger("hub.coverage")

DEFAULT_URL = "https://enterprise.coveragemap.com/api/v1"
DATASETS = ("summary", "fcc-coverage", "speed-tests")
UNIT_COST = {"summary": 1, "fcc-coverage": 1, "speed-tests": 2}   # per location (speed tests: only when found)
TECHNOLOGIES = ["lte", "5g"]
TIMEOUT_S = 30.0
BATCH = 100                     # the API's limit per request
MOVED_M = 100.0                 # a Site whose pin moved further than this is looked up again
RETRY_FAILED_S = 24 * 3600      # the daily loop leaves a failed Site alone this long
MANUAL_EVERY_S = 600            # manual refresh: once per Site per 10 minutes
CHECK_LIMIT, CHECK_WINDOW_S = 20, 3600   # address checks that reach the API, per user per hour
RADII = (("halfKilometer", "0.5km"), ("oneKilometer", "1km"), ("twoKilometers", "2km"))
SOURCE_LINE = "Data: CoverageMap (FCC Broadband Data Collection and crowdsourced speed tests)"
ALERT_SUBJECT_ID = "coverage"   # alerts.site_id of the hub-level coverage_budget alert
CHECK_PREFIX = "coverage:check:"   # kv keys of the address-check cache

# upload fit: per-camera assumptions when a server doesn't report what each camera sends
ASSUMED_MAIN_MBPS, ASSUMED_SUB_MBPS = 6.0, 1.0
TYPICAL_CAMERAS = 5             # a Site with no cameras known yet (the address check): the typical BR1 site
FIT_HEADROOM = 1.5              # fits: median upload >= 1.5x the need; tight: >= the need
FAILED_SHARE_TIGHT = 0.25       # a "fits" with this share of failed tests or more reads as tight

# tests swap these (a fake transport, a fake clock)
transport: httpx.AsyncBaseTransport | None = None
_clock = time.time
_locks: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}
_manual: dict[str, float] = {}                       # location_id -> last manual refresh attempt
_checks: dict[str, collections.deque] = collections.defaultdict(collections.deque)


class CoverageError(Exception):
    """A lookup failed. `status` the HTTP status (None: network), `errors` the API's messages.errors, `network` true when
    nothing was answered (old data stays), `location` true when the API answered with a per-location error."""

    def __init__(self, message: str, status: int | None = None, errors: list[str] | None = None, network: bool = False,
                 location: bool = False):
        super().__init__(redact(message))
        self.status, self.errors, self.network, self.location = status, [redact(e) for e in errors or []], network, location


class BudgetError(Exception):
    """The monthly unit budget would be exceeded."""


# ---------------------------------------------------------------- settings

def enabled() -> bool:
    return bool((settings.coveragemap_key or "").strip())


def plan() -> str:
    """trial unless the setting says paid (anything else fails safe to trial)."""
    return "paid" if (settings.coveragemap_plan or "").strip().lower() == "paid" else "trial"


def base_url() -> str:
    return ((settings.coveragemap_url or "").strip() or DEFAULT_URL).rstrip("/")


def datasets() -> list[str]:
    want = [d.strip().lower() for d in (settings.coveragemap_datasets or "").split(",")]
    out = [d for d in DATASETS if d in want]
    return out or list(DATASETS)


def refresh_s() -> float:
    return max(1, int(settings.coveragemap_refresh_days or 30)) * 86400.0


def cap() -> int:
    return max(0, int(settings.coveragemap_monthly_units or 0))


def max_units_per_location(ds: list[str] | None = None) -> int:
    return sum(UNIT_COST[d] for d in (ds or datasets()))


def redact(text) -> str:
    s = str(text or "")
    key = (settings.coveragemap_key or "").strip()
    if key:
        s = s.replace(key, "***")
    return s


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    cur = _locks.get("api")
    if cur is None or cur[0] is not loop:
        cur = _locks["api"] = (loop, asyncio.Lock())
    return cur[1]


# ---------------------------------------------------------------- who sees what

def visible_to(u: dict) -> bool:
    """May this user see coverage data at all (beyond seeing the Site)? Trial: hub administrators only."""
    return enabled() and (bool(u.get("is_super")) or plan() == "paid")


def shown_to(u: dict, row: dict | None) -> bool:
    """May this stored row be shown to this user? Trial-era data never reaches a customer."""
    if not row or not row.get("fetched_at"):
        return False
    return bool(u.get("is_super")) or (plan() == "paid" and row.get("plan_at_fetch") == "paid")


def me_info(u: dict) -> dict:
    """What /auth/me tells the UI: off, or the plan and whether this user sees coverage anywhere."""
    on = enabled()
    return {"enabled": on, "plan": plan() if on else None, "visible": visible_to(u), "evaluation": on and plan() == "trial",
            "cost_per_lookup": max_units_per_location() if on else 0}


# ---------------------------------------------------------------- the API call

def target_of(loc: dict) -> dict | None:
    """What the API is asked for a Site: its coordinates when located, else its address (<= 256 chars), else None."""
    if loc.get("lat") is not None and loc.get("lon") is not None:
        return {"latitude": round(float(loc["lat"]), 6), "longitude": round(float(loc["lon"]), 6)}
    addr = (loc.get("address") or "").strip()
    return {"address": addr[:256]} if addr else None


async def post_coverage(locations: list[dict], ds: list[str] | None = None) -> dict:
    """One POST /coverage for up to 100 locations ({id, latitude, longitude} or {id, address}). Returns the response's
    `data`. Raises CoverageError (redacted) on a network failure, a non-200 answer or `data: null`."""
    if not enabled():
        raise CoverageError("CoverageMap is not configured (HUB_COVERAGEMAP_KEY)")
    if not 1 <= len(locations) <= BATCH:
        raise ValueError(f"1 to {BATCH} locations per request")
    body = {"datasets": ds or datasets(), "technologies": TECHNOLOGIES, "locations": locations}
    headers = {"Authorization": f"Bearer {settings.coveragemap_key.strip()}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_S) as c:
            r = await c.post(f"{base_url()}/coverage", json=body, headers=headers)
    except httpx.HTTPError as e:
        raise CoverageError(f"CoverageMap unreachable: {type(e).__name__} {e}", network=True) from None
    try:
        payload = r.json()
    except ValueError:
        raise CoverageError(f"CoverageMap answered HTTP {r.status_code} without JSON", status=r.status_code, network=r.status_code >= 500 or r.status_code == 429) from None
    msgs = payload.get("messages") if isinstance(payload, dict) and isinstance(payload.get("messages"), dict) else {}
    errors = [str(e) for e in msgs.get("errors") or [] if e]
    data = payload.get("data") if isinstance(payload, dict) else None
    if r.status_code != 200 or not isinstance(data, dict):
        raise CoverageError("CoverageMap refused the lookup: " + ("; ".join(errors) or f"HTTP {r.status_code}"), status=r.status_code,
                            errors=errors, network=r.status_code >= 500 or r.status_code == 429)
    data["_information"] = [str(m) for m in msgs.get("information") or [] if m]
    return data


# ---------------------------------------------------------------- units (the vendor's billing rules)

def count_units(data: dict, requested: list[str]) -> tuple[int, list[int]]:
    """(total, per location) units a response costs, per the billing rules: a location with an `error` costs nothing;
    every other location costs 1 for fcc-coverage and 1 for the summary (even with no coverage there), and 2 for speed
    tests only when at least one of its coverage entries has speedTest data. A dataset that is null on every coverage
    entry of the whole response was temporarily unavailable and is not billed."""
    locs = [l for l in data.get("locations") or [] if isinstance(l, dict)]
    entries = [e for l in locs if not l.get("error") for e in l.get("coverage") or [] if isinstance(e, dict)]

    def delivered(field: str) -> bool:
        return not entries or any(e.get(field) is not None for e in entries)
    fcc = "fcc-coverage" in requested and delivered("fccCoverage")
    summ = "summary" in requested and delivered("summary")
    per: list[int] = []
    for l in locs:
        if l.get("error"):
            per.append(0)
            continue
        n = int(fcc) + int(summ)
        if "speed-tests" in requested and any(isinstance(e, dict) and e.get("speedTest") for e in l.get("coverage") or []):
            n += UNIT_COST["speed-tests"]
        per.append(n)
    return sum(per), per


# ---------------------------------------------------------------- normalization

def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int(v) -> int:
    n = _num(v)
    return int(n) if n is not None else 0


def _stats(m: dict, radius: str, distance=None) -> dict:
    out = {"radius": radius, "med": _num(m.get("med")), "min": _num(m.get("min")), "avg": _num(m.get("avg")), "max": _num(m.get("max")),
           "count": _int(m.get("count")), "failed": _int(m.get("failedCount")), "accuracy": m.get("accuracy")}
    if distance is not None:
        out["distance_km"] = _num(distance)
    return out


def speed_metric(m) -> dict | None:
    """One speed-test metric at the nearest radius with successful tests (0.5, 1, then 2 km), else the closest tested
    area (up to 10 km away, with its distance). When nothing nearby succeeded but tests failed: the failures (count 0)."""
    if not isinstance(m, dict):
        return None
    for key, name in RADII:
        r = m.get(key)
        if isinstance(r, dict) and _int(r.get("count")) > 0 and _num(r.get("med")) is not None:
            return _stats(r, name)
    if _int(m.get("count")) > 0 and _num(m.get("med")) is not None:
        return _stats(m, "closest", m.get("distance"))
    for key, name in RADII:
        r = m.get(key)
        if isinstance(r, dict) and _int(r.get("failedCount")) > 0:
            return _stats(r, name)
    if _int(m.get("failedCount")) > 0:
        return _stats(m, "closest", m.get("distance"))
    return None


def _entry(e: dict) -> dict:
    tech = e.get("technology") if isinstance(e.get("technology"), dict) else {}
    s = e.get("summary") if isinstance(e.get("summary"), dict) else None
    f = e.get("fccCoverage") if isinstance(e.get("fccCoverage"), dict) else None
    st = e.get("speedTest") if isinstance(e.get("speedTest"), dict) else None
    out: dict = {"technology": (tech.get("code") or "all"), "technology_name": tech.get("name") or ("All" if not tech else None),
                 "summary": None, "fcc": None, "speed": None}
    if s:
        out["summary"] = {"overall": _num(s.get("overall")), "performance": _num(s.get("performance")), "coverage": _num(s.get("coverage")),
                          "reliability": _num(s.get("reliability")), "is_fully_covered": bool(s.get("isFullyCovered")),
                          "source": s.get("source"), "accuracy": s.get("accuracy")}
    if f:
        sig = f.get("signal") if isinstance(f.get("signal"), dict) else {}
        cov = f.get("coverage") if isinstance(f.get("coverage"), dict) else {}
        out["fcc"] = {"signal": {"point": _num(sig.get("signal")), "r05": _num(sig.get("halfKilometer")), "r1": _num(sig.get("oneKilometer")),
                                 "r2": _num(sig.get("twoKilometers"))},
                      "coverage": {"r05": _num(cov.get("halfKilometer")), "r1": _num(cov.get("oneKilometer")), "r2": _num(cov.get("twoKilometers"))}}
    if st:
        out["speed"] = {"download": speed_metric(st.get("downloadSpeed")), "upload": speed_metric(st.get("uploadSpeed")),
                        "latency": speed_metric(st.get("latency"))}
    return out


def _score(entry: dict, field: str = "overall") -> float:
    v = (entry.get("summary") or {}).get(field)
    return v if isinstance(v, (int, float)) else -1.0


def normalize_location(loc: dict) -> dict:
    """One response location -> {latitude, longitude, address, confidence, error, carriers}. carriers: one per provider,
    best first (highest overall score of its technologies, then coverage score, then code), each with `tech` keyed by
    technology code (lte, 5g; "all" when technologies were combined) and `best` (its top overall score, None if none)."""
    by: dict[str, dict] = {}
    for e in loc.get("coverage") or []:
        if not isinstance(e, dict):
            continue
        p = e.get("provider") if isinstance(e.get("provider"), dict) else {}
        code = str(p.get("code") or "?")
        c = by.setdefault(code, {"code": code, "name": p.get("name") or code, "tech": {}})
        ent = _entry(e)
        c["tech"][ent["technology"]] = ent
    carriers = []
    for c in by.values():
        ents = list(c["tech"].values())
        best = max((_score(x) for x in ents), default=-1.0)
        c["best"] = best if best >= 0 else None
        c["best_technology"] = max(ents, key=_score)["technology"] if ents and best >= 0 else None
        c["_cov"] = max((_score(x, "coverage") for x in ents), default=-1.0)
        carriers.append(c)
    carriers.sort(key=lambda c: (-(c["best"] if c["best"] is not None else -1), -c["_cov"], c["code"]))
    for c in carriers:
        c.pop("_cov", None)
    return {"latitude": _num(loc.get("latitude")), "longitude": _num(loc.get("longitude")), "address": loc.get("address"),
            "confidence": loc.get("confidence"), "error": loc.get("error"), "carriers": carriers}


# ---------------------------------------------------------------- upload fit (pure)

def camera_need(servers: list[dict], registry: list[dict] | None = None) -> dict:
    """What the Site's cameras would push up the cellular link, in Mbit/s. Per camera: what its server measures
    (summary.bandwidth.cameras), else 6 Mbit/s for the main stream or 1 Mbit/s when it records its sub stream
    (record_stream "sub"). Cameras come from the servers' heartbeat summaries (retired servers left out), else from the
    cameras registry; with none known, the typical Site: 5 cameras on main streams (`typical`)."""
    cams: list[tuple[str, float, bool]] = []
    for s in servers:
        if s.get("retired_at"):
            continue
        summ = s.get("summary") if isinstance(s.get("summary"), dict) else {}
        bw = summ.get("bandwidth") if isinstance(summ.get("bandwidth"), dict) else {}
        per = bw.get("cameras") if isinstance(bw.get("cameras"), dict) else {}
        for c in summ.get("cameras") or []:
            if not isinstance(c, dict) or not c.get("id"):
                continue
            m = _num(per.get(c["id"]))
            if m is not None and m > 0:
                cams.append((str(c["id"]), m, True))
            else:
                cams.append((str(c["id"]), ASSUMED_SUB_MBPS if c.get("record_stream") == "sub" else ASSUMED_MAIN_MBPS, False))
    if not cams:
        for c in registry or []:
            if c.get("enabled") is False or c.get("missing_since"):
                continue
            cams.append((str(c.get("camera_id") or c.get("id")), ASSUMED_SUB_MBPS if c.get("record_stream") == "sub" else ASSUMED_MAIN_MBPS, False))
    if not cams:
        return {"mbps": TYPICAL_CAMERAS * ASSUMED_MAIN_MBPS, "cameras": TYPICAL_CAMERAS, "measured": 0, "assumed": TYPICAL_CAMERAS, "typical": True}
    measured = sum(1 for c in cams if c[2])
    return {"mbps": round(sum(c[1] for c in cams), 1), "cameras": len(cams), "measured": measured, "assumed": len(cams) - measured, "typical": False}


def upload_fit(need_mbps: float | None, upload: dict | None) -> dict:
    """Will the measured upload carry the cameras? fits: median >= 1.5x the need; tight: >= the need; wont_fit: below
    it, or every nearby test failed; unknown: no tests (or no need). A "fits" with 25% or more failed tests is tight."""
    need = _num(need_mbps) or 0.0
    if need <= 0 or not isinstance(upload, dict):
        return {"fit": "unknown", "ratio": None, "reason": "no speed tests nearby" if need > 0 else "no cameras"}
    count, failed, med = int(upload.get("count") or 0), int(upload.get("failed") or 0), _num(upload.get("med"))
    if count == 0 or med is None:
        if failed > 0:
            return {"fit": "wont_fit", "ratio": None, "reason": f"all {failed} nearby upload test{'s' if failed != 1 else ''} failed"}
        return {"fit": "unknown", "ratio": None, "reason": "no speed tests nearby"}
    ratio = round(med / need, 2)
    fit = "fits" if med >= FIT_HEADROOM * need else "tight" if med >= need else "wont_fit"
    reason = f"median upload {med:g} Mbit/s for {need:g} Mbit/s needed"
    share = failed / (count + failed)
    if fit == "fits" and share >= FAILED_SHARE_TIGHT:
        fit, reason = "tight", reason + f"; {failed} of {count + failed} tests failed"
    mn = _num(upload.get("min"))
    if fit == "fits" and mn is not None and mn < need:
        reason += f"; slowest test {mn:g} Mbit/s"
    return {"fit": fit, "ratio": ratio, "reason": reason}


def fits_for(norm: dict | None, need_mbps: float) -> dict:
    """{carrier: {technology: upload_fit}} for a normalized location."""
    out: dict[str, dict] = {}
    for c in (norm or {}).get("carriers") or []:
        out[c["code"]] = {t: upload_fit(need_mbps, (e.get("speed") or {}).get("upload")) for t, e in c["tech"].items()}
    return out


# ---------------------------------------------------------------- usage and budget

def month_of(ts: float | None = None) -> str:
    return time.strftime("%Y-%m", time.gmtime(ts if ts is not None else _clock()))


def usage(month: str | None = None) -> dict:
    m = month or month_of()
    row = db.one(sa.select(db.coverage_usage).where(db.coverage_usage.c.month == m))
    return {"month": m, "units": int(row["units"]) if row else 0, "calls": int(row["calls"]) if row else 0}


def _record_usage(units: int, calls: int = 1) -> None:
    m = month_of()
    with db.engine().begin() as c:
        t = db.coverage_usage
        if c.execute(sa.select(t.c.month).where(t.c.month == m)).first() is None:
            c.execute(t.insert().values(month=m, units=units, calls=calls))
        else:
            c.execute(sa.update(t).where(t.c.month == m).values(units=t.c.units + units, calls=t.c.calls + calls))


def room(n_locations: int = 1) -> int:
    """How many of `n_locations` the budget still allows this month, counting each at its worst case."""
    if cap() == 0:
        return n_locations
    per = max(1, max_units_per_location())
    return max(0, min(n_locations, (cap() - usage()["units"]) // per))


def budget_reached() -> bool:
    return room(1) == 0


def _alert_subject() -> dict:
    from .hosts import HUB_ORG
    return {"id": ALERT_SUBJECT_ID, "org_id": HUB_ORG, "name": "CoverageMap", "location_id": None, "hub": True}


def _budget_alert() -> None:
    u = usage()
    text = f"{u['units']} of {cap()} CoverageMap units used in {u['month']}: automatic cellular coverage refreshes stopped until next month"
    if alerts.open(_alert_subject(), "coverage_budget", u["month"], {"text": text, "units": u["units"], "cap": cap(), "month": u["month"]}):
        log.warning("coverage: %s", text)


def _budget_settle() -> None:
    """Open the alert once the month's budget is used up; close older months' alerts."""
    from .hosts import HUB_ORG
    m = month_of()
    db.run(sa.update(db.alerts).where(db.alerts.c.org_id == HUB_ORG, db.alerts.c.kind == "coverage_budget", db.alerts.c.key != m,
                                      db.alerts.c.closed_at.is_(None)).values(closed_at=time.time()))
    if cap() and budget_reached():
        _budget_alert()


def budget_alert_open() -> bool:
    from .hosts import HUB_ORG
    return db.one(sa.select(db.alerts.c.id).where(db.alerts.c.org_id == HUB_ORG, db.alerts.c.kind == "coverage_budget",
                                                  db.alerts.c.closed_at.is_(None))) is not None


# ---------------------------------------------------------------- stored rows

def row_of(location_id: str) -> dict | None:
    return db.one(sa.select(db.site_coverage).where(db.site_coverage.c.location_id == location_id))


def distance_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def moved(loc: dict, row: dict) -> bool:
    """Does the stored lookup describe another place than the Site now? A located Site: more than 100 m from where it
    was looked up (or it was looked up by address). An unlocated one: its address text changed."""
    if loc.get("lat") is not None and loc.get("lon") is not None:
        if (row.get("data") or {}).get("basis") != "point" or row.get("lat") is None or row.get("lon") is None:
            return True
        return distance_m(float(loc["lat"]), float(loc["lon"]), float(row["lat"]), float(row["lon"])) > MOVED_M
    return ((row.get("data") or {}).get("address") or "").strip() != (loc.get("address") or "").strip()


def due_reason(loc: dict, row: dict | None, now: float | None = None) -> str | None:
    """Why this Site should be looked up again (None = not due): never, age, plan (trial data on a paid plan), moved."""
    now = now if now is not None else _clock()
    if target_of(loc) is None:
        return None
    if row is None or not row.get("fetched_at"):
        return "never"
    if now - float(row["fetched_at"]) > refresh_s():
        return "age"
    if plan() == "paid" and row.get("plan_at_fetch") != "paid":
        return "plan"
    if moved(loc, row):
        return "moved"
    return None


def _store(loc: dict, target: dict, raw: dict, units: int, information: list[str], now: float) -> None:
    norm = normalize_location(raw)
    if "latitude" in target:
        lat, lon, basis = target["latitude"], target["longitude"], "point"
    else:
        lat, lon, basis = norm["latitude"], norm["longitude"], "address"
    data = {"normalized": norm, "raw": raw, "basis": basis, "address": (loc.get("address") or "").strip(), "datasets": datasets(),
            "information": information}
    vals = {"fetched_at": now, "lat": lat, "lon": lon, "data": data, "units": units, "plan_at_fetch": plan(), "attempted_at": now, "error": None}
    with db.engine().begin() as c:
        t = db.site_coverage
        if c.execute(sa.select(t.c.location_id).where(t.c.location_id == loc["id"])).first() is None:
            c.execute(t.insert().values(location_id=loc["id"], **vals))
        else:
            c.execute(sa.update(t).where(t.c.location_id == loc["id"]).values(**vals))


def _note_failure(location_id: str, error: str, now: float) -> None:
    """A failed attempt: remembered (the admins see it) but the previous data stays as it was."""
    with db.engine().begin() as c:
        t = db.site_coverage
        if c.execute(sa.select(t.c.location_id).where(t.c.location_id == location_id)).first() is None:
            c.execute(t.insert().values(location_id=location_id, fetched_at=None, lat=None, lon=None, data=None, units=0, plan_at_fetch=None,
                                        attempted_at=now, error=redact(error)[:500]))
        else:
            c.execute(sa.update(t).where(t.c.location_id == location_id).values(attempted_at=now, error=redact(error)[:500]))


# ---------------------------------------------------------------- fetching

async def fetch_batch(locs: list[dict]) -> dict[str, dict]:
    """Look up up to 100 Sites in one request (inside the budget, which the caller checked). Returns
    {location_id: {"ok": bool, "units": int, "error": str | None}}. A request-level failure marks each Site failed and
    raises nothing; the previous data stays."""
    now = _clock()
    items = [(loc, target_of(loc)) for loc in locs]
    items = [(loc, t) for loc, t in items if t is not None]
    if not items:
        return {}
    ds = datasets()
    try:
        data = await post_coverage([{"id": loc["id"], **t} for loc, t in items], ds)
    except CoverageError as e:
        for loc, _ in items:
            _note_failure(loc["id"], str(e), now)
        log.warning("coverage lookup of %d site(s) failed: %s", len(items), e)
        raise
    total, per = count_units(data, ds)
    _record_usage(total)
    answers = [l for l in data.get("locations") or [] if isinstance(l, dict)]
    by_id = {str(l.get("id")): (l, per[i]) for i, l in enumerate(answers) if l.get("id") is not None}
    out: dict[str, dict] = {}
    for i, (loc, t) in enumerate(items):
        got = by_id.get(loc["id"]) or ((answers[i], per[i]) if i < len(answers) else None)
        if got is None:
            _note_failure(loc["id"], "no answer for this location", now)
            out[loc["id"]] = {"ok": False, "units": 0, "error": "no answer for this location"}
            continue
        raw, units = got
        if raw.get("error"):
            _note_failure(loc["id"], str(raw["error"]), now)
            out[loc["id"]] = {"ok": False, "units": 0, "error": redact(raw["error"])}
            continue
        _store(loc, t, raw, units, data.get("_information") or [], now)
        out[loc["id"]] = {"ok": True, "units": units, "error": None}
    log.info("coverage: looked up %d site(s), %d unit(s)", len(items), total)
    return out


def _location(location_id: str) -> dict | None:
    return db.one(sa.select(db.locations).where(db.locations.c.id == location_id))


def manual_wait(location_id: str) -> int:
    """Seconds until this Site may be refreshed by hand again (0 = now)."""
    last = _manual.get(location_id, 0.0)
    row = row_of(location_id)
    if row and row.get("attempted_at"):
        last = max(last, float(row["attempted_at"]))
    return max(0, int(last + MANUAL_EVERY_S - _clock() + 0.999))


async def refresh_location(location_id: str) -> dict:
    """A manual refresh: rate-limited per Site, inside the budget. Returns fetch_batch's result for the Site; raises
    LookupError (no such Site / nothing to look up), BudgetError, CoverageError, or RuntimeError("rate", seconds)."""
    loc = _location(location_id)
    if not loc:
        raise LookupError("unknown site")
    if target_of(loc) is None:
        raise LookupError("this site has no address or map pin yet")
    async with _lock():
        if room(1) < 1:
            _budget_alert()
            raise BudgetError(f"the monthly CoverageMap budget ({cap()} units) is used up")
        wait = manual_wait(location_id)
        if wait:
            raise RuntimeError("rate", wait)
        _manual[location_id] = _clock()
        try:
            res = await fetch_batch([loc])
        finally:
            _budget_settle()
    r = res.get(location_id) or {"ok": False, "units": 0, "error": "no answer"}
    if not r["ok"]:
        raise CoverageError(r["error"] or "lookup failed", status=200, location=True)
    return r


async def refresh_due(ids: list[str] | None = None, now: float | None = None) -> dict:
    """The daily pass (and the "pin moved" kick): look up every due Site, 100 per request, while the budget lasts.
    Paid: every Site with a pin or an address; trial: only Sites already looked up. Returns stats."""
    stats = {"due": 0, "fetched": 0, "failed": 0, "units": 0, "budget_stop": False}
    if not enabled():
        return stats
    now = now if now is not None else _clock()
    q = sa.select(db.locations)
    if ids is not None:
        q = q.where(db.locations.c.id.in_(ids))
    locs = db.rows(q)
    rows = {r["location_id"]: r for r in db.rows(sa.select(db.site_coverage))}
    due = []
    for loc in locs:
        row = rows.get(loc["id"])
        if plan() == "trial" and (row is None or not row.get("fetched_at")):
            continue   # the trial is for evaluation: only Sites a hub administrator looked up by hand
        reason = due_reason(loc, row, now)
        if not reason:
            continue
        if row and row.get("error") and row.get("attempted_at") and now - float(row["attempted_at"]) < RETRY_FAILED_S and reason in ("never", "age"):
            continue   # failed lately: tomorrow
        due.append(loc)
    stats["due"] = len(due)
    async with _lock():
        try:
            while due:
                n = room(min(BATCH, len(due)))
                if n < 1:
                    stats["budget_stop"] = True
                    log.warning("coverage: monthly budget of %d units reached with %d site(s) still due", cap(), len(due))
                    _budget_alert()
                    break
                batch, due = due[:n], due[n:]
                try:
                    res = await fetch_batch(batch)
                except CoverageError:
                    stats["failed"] += len(batch)
                    if not ids:
                        break   # the API is down or refusing: try again tomorrow
                    continue
                for r in res.values():
                    stats["fetched" if r["ok"] else "failed"] += 1
                    stats["units"] += r["units"]
        finally:
            _budget_settle()
    if stats["due"]:
        log.info("coverage refresh: %s", stats)
    return stats


def location_moved(location_id: str) -> bool:
    """A Site update changed its place: if its stored lookup no longer describes it, look it up again soon (in the
    background, inside the budget). Returns whether a refresh was scheduled."""
    if not enabled():
        return False
    loc, row = _location(location_id), row_of(location_id)
    if not loc or not row or not row.get("fetched_at") or not moved(loc, row):
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False

    async def run():
        try:
            await refresh_due(ids=[location_id])
        except Exception:
            log.exception("coverage refresh %s", location_id)
    loop.create_task(run())
    return True


async def daily_loop(start_delay_s: float = 120.0) -> None:
    await asyncio.sleep(start_delay_s)   # let the hub settle first
    while True:
        if enabled():
            try:
                await refresh_due()
            except Exception:
                log.exception("coverage refresh")
        await asyncio.sleep(24 * 3600)


# ---------------------------------------------------------------- the address check (before a Site exists)

def _check_key(lat: float | None, lon: float | None, address: str | None) -> str:
    if lat is not None and lon is not None:
        q = f"pt:{round(float(lat), 4):.4f},{round(float(lon), 4):.4f}"
    else:
        q = "addr:" + " ".join((address or "").casefold().replace(",", " ").split())
    return CHECK_PREFIX + hashlib.sha1(f"{q}|{','.join(datasets())}".encode()).hexdigest()


def check_limited(uid: str) -> bool:
    now = _clock()
    q = _checks[uid]
    while q and q[0] < now - CHECK_WINDOW_S:
        q.popleft()
    if len(q) >= CHECK_LIMIT:
        return True
    q.append(now)
    return False


def check_cached(lat: float | None, lon: float | None, address: str | None) -> dict | None:
    row = db.one(sa.select(db.kv.c.value).where(db.kv.c.key == _check_key(lat, lon, address)))
    v = row["value"] if row else None
    if not isinstance(v, dict) or _clock() - float(v.get("at") or 0) > refresh_s():
        return None
    if v.get("plan") != "paid" and plan() == "paid":
        return None   # trial-era answers are not shown once paid: look it up again
    return v


async def check(lat: float | None, lon: float | None, address: str | None, uid: str) -> dict:
    """Look up a place that is not (yet) a Site: cached by the point rounded to 4 decimals (~11 m), or the address, for
    REFRESH_DAYS so the same spot is never billed twice. Returns {cached, fetched_at, units, data}."""
    hit = check_cached(lat, lon, address)
    if hit:
        return {"cached": True, "fetched_at": hit["at"], "units": 0, "data": hit["data"], "plan_at_fetch": hit.get("plan")}
    if check_limited(uid):
        raise RuntimeError("rate", CHECK_WINDOW_S)
    target = ({"latitude": round(float(lat), 6), "longitude": round(float(lon), 6)} if lat is not None and lon is not None
              else {"address": (address or "").strip()[:256]})
    async with _lock():
        if room(1) < 1:
            _budget_alert()
            raise BudgetError(f"the monthly CoverageMap budget ({cap()} units) is used up")
        ds = datasets()
        data = await post_coverage([{"id": "check", **target}], ds)   # a failure costs nothing and caches nothing
        total, _ = count_units(data, ds)
        _record_usage(total)
        _budget_settle()
    raw = next((l for l in data.get("locations") or [] if isinstance(l, dict)), None)
    if raw is None:
        raise CoverageError("no answer for this location", status=200, location=True)
    if raw.get("error"):
        raise CoverageError(str(raw["error"]), status=200, location=True)
    norm = normalize_location(raw)
    now = _clock()
    key = _check_key(lat, lon, address)
    with db.engine().begin() as c:
        c.execute(sa.delete(db.kv).where(db.kv.c.key == key))
        c.execute(db.kv.insert().values(key=key, value={"at": now, "plan": plan(), "units": total, "data": norm}))
    return {"cached": False, "fetched_at": now, "units": total, "data": norm, "plan_at_fetch": plan()}


# ---------------------------------------------------------------- purge (unsubscribing)

def purge() -> dict:
    """Delete every stored CoverageMap answer and the usage rows (the license: stored data goes when the subscription
    ends), the address-check cache and the budget alerts."""
    from .hosts import HUB_ORG
    with db.engine().begin() as c:
        n_sites = c.execute(sa.delete(db.site_coverage)).rowcount or 0
        n_usage = c.execute(sa.delete(db.coverage_usage)).rowcount or 0
        n_checks = c.execute(sa.delete(db.kv).where(db.kv.c.key.like(CHECK_PREFIX + "%"))).rowcount or 0
        c.execute(sa.delete(db.alerts).where(db.alerts.c.org_id == HUB_ORG, db.alerts.c.kind == "coverage_budget"))
    _manual.clear()
    _checks.clear()
    return {"sites": n_sites, "usage_months": n_usage, "checks": n_checks}


def stored_count() -> int:
    return int(db.one(sa.select(sa.func.count().label("n")).select_from(db.site_coverage).where(db.site_coverage.c.fetched_at.is_not(None)))["n"])


def usage_report(months: int = 6) -> dict:
    rows = db.rows(sa.select(db.coverage_usage).order_by(db.coverage_usage.c.month.desc()).limit(months))
    cur = usage()
    return {"enabled": enabled(), "plan": plan() if enabled() else None, "budget": cap(), "month": cur["month"], "units": cur["units"],
            "calls": cur["calls"], "remaining": (max(0, cap() - cur["units"]) if cap() else None), "budget_reached": bool(cap()) and budget_reached(),
            "alert_open": budget_alert_open(), "history": [{"month": r["month"], "units": r["units"], "calls": r["calls"]} for r in rows],
            "stored_sites": stored_count(), "datasets": datasets(), "refresh_days": int(settings.coveragemap_refresh_days or 30),
            "cost_per_lookup": max_units_per_location()}


# ---------------------------------------------------------------- what the Site page gets

def site_payload(u: dict, loc: dict, servers: list[dict], registry: list[dict], can_refresh: bool) -> dict:
    """GET /api/locations/{id}/coverage for a user who may see coverage (visible_to): the stored lookup when this user
    may see it (shown_to), the cameras' upload need and each carrier/technology's fit. Errors from the last attempt are
    for those who can refresh."""
    row = row_of(loc["id"])
    show = shown_to(u, row)
    need = camera_need(servers, registry)
    norm = (row.get("data") or {}).get("normalized") if show and row else None
    hidden = bool(row and row.get("fetched_at") and not show)
    reason = due_reason(loc, row) if row else None
    return {
        "enabled": True, "plan": plan(), "evaluation": plan() == "trial", "location_id": loc["id"],
        "data": norm, "fetched_at": row.get("fetched_at") if show and row else None,
        "units": row.get("units") if show and row else None, "plan_at_fetch": row.get("plan_at_fetch") if show and row else None,
        "basis": (row.get("data") or {}).get("basis") if show and row else None,
        "information": (row.get("data") or {}).get("information") if show and row and can_refresh else [],
        "hidden": hidden,   # evaluation-era data exists but may not be shown: it is refetched before customers see it
        "stale": reason in ("age", "moved", "plan") if show else False, "stale_reason": reason if show else None,
        "error": (row.get("error") if row and can_refresh else None), "attempted_at": (row.get("attempted_at") if row and can_refresh else None),
        "can_refresh": can_refresh, "refresh_cost": max_units_per_location(), "refresh_wait_s": manual_wait(loc["id"]) if can_refresh else None,
        "locatable": target_of(loc) is not None,
        "need": need, "fits": fits_for(norm, need["mbps"]) if norm else {},
        "source": SOURCE_LINE,
    }
