"""Live-view timing from the hub UI's players (frontend whepTiming.ts): how long a live tile takes to show its first
frame, by route (through the hub's /s/<id> proxy, or Direct-on-LAN) and by stage of the WHEP connection.

In memory only: the last KEEP samples per server, gone on restart; no database, nothing per user is kept (the user
only counts toward a rate limit). A sample is one connection attempt that showed its first frame, or one that ended
without ever showing one. The "end" report of an attempt that did show a frame carries its viewing time, which isn't
stored yet (the audit's viewing sessions will use it).

The beacon is a same-origin POST with the session cookie (navigator.sendBeacon, or fetch keepalive). The hub's CSRF
check (auth.csrf_check: a foreign Origin is refused) applies to it as to any POST; on top of that the route takes only
application/json, which a cross-site form or a no-CORS fetch can't send, and the cookie is SameSite=Lax.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

KEEP = 300                 # samples per server
MAX_BODY = 4096            # bytes
RATE_LIMIT = 120           # reports per user per window
RATE_WINDOW_S = 60.0
MAX_STAGE_MS = 300_000     # a stage or TTFF beyond 5 min is not a measurement
MAX_DURATION_S = 30 * 86400   # a wall display may stay on for weeks: longer viewing is clamped, not refused
STAGES = ("offer", "ice", "post", "answer", "connected", "first_frame")
ROUTES = ("hub", "direct")

_ID = r"^[A-Za-z0-9_-]{1,64}$"
_Ms = Field(None, ge=0, le=MAX_STAGE_MS)


class Stages(BaseModel):
    model_config = ConfigDict(extra="ignore")
    offer: float | None = _Ms
    ice: float | None = _Ms
    post: float | None = _Ms
    answer: float | None = _Ms
    connected: float | None = _Ms
    first_frame: float | None = _Ms


class LiveIn(BaseModel):
    """One report (whepTiming.ts LiveReport, with the server id the hub UI read from the client's /s/<id> base)."""
    model_config = ConfigDict(extra="ignore")
    event: Literal["first_frame", "end"]
    server: str = Field(pattern=_ID)
    camera: str = Field(pattern=_ID)
    path: str = Field(pattern=_ID)
    quality: Literal["sd", "hd"]
    route: Literal["hub", "direct"]
    attempt: int = Field(1, ge=1, le=10_000)
    stages: Stages = Field(default_factory=Stages)
    ttff_ms: float | None = _Ms
    duration_s: float | None = Field(None, ge=0)
    ice_timed_out: bool = False


_samples: dict[str, deque] = defaultdict(lambda: deque(maxlen=KEEP))
_reports: dict[str, deque] = defaultdict(deque)


def rate_limited(uid: str, now: float | None = None) -> bool:
    """True when this user has sent too many reports lately; otherwise counts this one."""
    now = now if now is not None else time.time()
    q = _reports[uid]
    while q and q[0] < now - RATE_WINDOW_S:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(now)
    return False


def record(body: LiveIn, now: float | None = None) -> bool:
    """Keep a sample of this report; False when there is nothing to keep (the end of an attempt that had played)."""
    if body.event == "end" and body.ttff_ms is not None:
        return False
    if body.event == "first_frame" and body.ttff_ms is None:
        return False
    st = {k: round(v) for k, v in body.stages.model_dump().items() if v is not None}
    _samples[body.server].append({
        "at": now if now is not None else time.time(), "camera": body.camera, "path": body.path, "quality": body.quality,
        "route": body.route, "attempt": body.attempt, "stages": st,
        "ttff_ms": round(body.ttff_ms) if body.ttff_ms is not None else None,
        "duration_s": min(body.duration_s, MAX_DURATION_S) if body.duration_s is not None else None,
        "ice_timed_out": body.ice_timed_out,
    })
    return True


def percentile(values: list[float], p: float) -> float | None:
    """Linear interpolation between closest ranks (numpy's default); None for no values."""
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def _pp(values: list[float]) -> dict:
    p50, p95 = percentile(values, 50), percentile(values, 95)
    return {"p50": None if p50 is None else round(p50, 1), "p95": None if p95 is None else round(p95, 1)}


def summary(server: str) -> dict:
    """TTFF p50/p95 over the server's kept samples, by route and by stage (ms). `n` counts attempts that showed a
    frame; `no_frame` those that ended without one (failed, or closed before the picture came)."""
    rows = list(_samples.get(server, ()))
    played = [r for r in rows if r["ttff_ms"] is not None]
    by_route = {}
    for route in ROUTES:
        vals = [r["ttff_ms"] for r in played if r["route"] == route]
        by_route[route] = {"n": len(vals), **_pp(vals)}
    return {
        "n": len(played),
        "ttff": _pp([r["ttff_ms"] for r in played]),
        "by_route": by_route,
        "stages": {s: _pp([r["stages"][s] for r in played if s in r["stages"]]) for s in STAGES},
        "ice_timeouts": sum(1 for r in rows if r["ice_timed_out"]),
        "no_frame": len(rows) - len(played),
    }


def reset() -> None:
    """Tests: forget every sample and rate count."""
    _samples.clear()
    _reports.clear()
