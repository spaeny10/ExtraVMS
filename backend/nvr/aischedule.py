"""The Site's AI schedule: when Qwen does all of its work and when only the small and the interactive jobs, plus the
meter of what each kind of Qwen work used (reported to the hub, which bills full-time AI per Site).

The hub pushes the Site's choice to every server of the Site over the agent channel ("ai_schedule" in `welcome`, and
its own frame when it changes; hub/hub/ai_schedule.py) together with the Site's SOC monitoring schedule and timezone.
The server keeps the last one it was given (settings `hub_ai_schedule`) and evaluates it itself, so it keeps working
while the hub is unreachable. No schedule pushed, a stand-alone server (no hub, or unenrolled) or mode "full": Qwen
does everything around the clock, as before.

Mode "soc": full Qwen while the Site's SOC monitoring is armed, by the hub's own rules (soc.compute_armed, copied below:
an unexpired override, then a holiday on the local date, then the weekly windows; a monitored Site with no windows is
armed around the clock, an unmonitored one never). Outside those hours ("limited"):
  always     PPE second looks (ppe_vlm_all counts as off), every request a person starts (Ask, clip chat, footage search,
             Describe & warn, Generate with Qwen, Read plate, briefings, Optimize my system, the remote AI test), and the
             second looks a description takes when one runs (weapon check, towing, the vehicle's plate: plate.py)
  outside    event descriptions, by the Site's option: "catchup" (default) waits in a queue of its own and runs one at a
             time when Qwen has nothing else to do here (and, on a central instance, the shared vLLM is not busy);
             "skip" is never described: the event says "No description (outside AI hours)" and is never retried
  journeys   same-person checks and journey narratives: at idle like catch-up, or (skip) when the schedule starts again
  schedule   retrying failed descriptions (pipeline.retry_due) waits for the schedule
YOLO verification never asks Qwen and is not affected.

Metering: every Qwen request through vlmroute is counted per UTC day, job kind (KINDS) and in/out of schedule:
requests, prompt / completion tokens when the answer has them (Ollama eval counts, OpenAI `usage`) and seconds at the
model. Kept here for KEEP_DAYS, reported to the hub every few minutes (hub_agent "ai_usage").
"""
from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import hashlib
import json
import logging
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .config import settings
from .db import db

log = logging.getLogger("nvr.aischedule")

SETTING = "hub_ai_schedule"
MODES = ("full", "soc")
OUTSIDE = ("catchup", "skip")
SKIP_NOTE = "No description (outside AI hours)"
STATE_TTL_S = 15.0     # the evaluated state is reused this long (never past its next change)

# vlmroute task -> metering kind; a job (job()) names its own kind instead, e.g. a catch-up description
KINDS = {"synopsis": "synopsis", "unusual_review": "synopsis", "journey": "journey", "ppe": "ppe",
         "assistant": "ask", "chat": "ask", "footage_verify": "ask", "briefing": "briefing", "talkdown": "talkdown",
         "hub": "hub", "test": "test", "plate": "plate"}
JOB: contextvars.ContextVar[str | None] = contextvars.ContextVar("ai_job", default=None)


# ---------------------------------------------------------------- the schedule (hub soc.py's rules, copied)
# Times are wall-clock in the Site's timezone (UTC when it has none or an unknown one). Weekly windows: [{dow, from,
# to}], dow 0..6 with Monday = 0; `to` <= `from` is an overnight window ending the next day (from == to: 24 hours). A
# window covers [from, to). A holiday governs its whole local date: armed all day, disarmed all day, or (with from/to)
# armed only inside that window on that date.

def tz_of(cfg: dict) -> dt.tzinfo:
    try:
        return ZoneInfo(cfg.get("timezone") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return dt.timezone.utc


def parse_hhmm(v) -> int | None:
    """"HH:MM" -> minutes after midnight (00:00..23:59), else None."""
    if not isinstance(v, str) or len(v) != 5 or v[2] != ":" or not (v[:2].isdigit() and v[3:].isdigit()):
        return None
    h, m = int(v[:2]), int(v[3:])
    return h * 60 + m if h < 24 and m < 60 else None


def _in_window(minute: int, f: int, t: int) -> tuple[bool, bool]:
    """(inside by today's part, inside by yesterday's spill-over) for a window f..t at this minute of the day."""
    if t > f:
        return f <= minute < t, False
    return minute >= f, minute < t


def _schedule_armed(schedule: list, local: dt.datetime) -> bool:
    minute = local.hour * 60 + local.minute
    wd = local.weekday()
    for w in schedule or []:
        if not isinstance(w, dict):
            continue
        f, t = parse_hhmm(w.get("from")), parse_hhmm(w.get("to"))
        days = {d for d in (w.get("dow") or []) if isinstance(d, int)}
        if f is None or t is None:
            continue
        today, spill = _in_window(minute, f, t)
        if (today and wd in days) or (spill and (wd - 1) % 7 in days):
            return True
    return False


def _holiday(cfg: dict, local: dt.datetime) -> dict | None:
    day = local.date().isoformat()
    for h in cfg.get("arm_holidays") or []:
        if isinstance(h, dict) and h.get("date") == day:
            return h
    return None


def _holiday_armed(h: dict, local: dt.datetime) -> bool:
    if not h.get("armed"):
        return False
    f, t = parse_hhmm(h.get("from")), parse_hhmm(h.get("to"))
    if f is None or t is None:
        return True
    today, before = _in_window(local.hour * 60 + local.minute, f, t)
    return today or before


def active_override(cfg: dict, now: float) -> dict | None:
    o = cfg.get("arm_override")
    if isinstance(o, dict) and o.get("mode") in ("arm", "disarm") and float(o.get("until") or 0) > now:
        return o
    return None


def compute_armed(cfg: dict, now: float) -> tuple[bool, str]:
    """(armed, reason) at `now`: unmonitored | override | holiday | schedule | disarmed_schedule | always."""
    if not cfg.get("monitored"):
        return False, "unmonitored"
    o = active_override(cfg, now)
    if o:
        return o["mode"] == "arm", "override"
    local = dt.datetime.fromtimestamp(now, tz_of(cfg))
    h = _holiday(cfg, local)
    if h is not None:
        return _holiday_armed(h, local), "holiday"
    schedule = [w for w in (cfg.get("arm_schedule") or []) if isinstance(w, dict)]
    if not schedule:
        return True, "always"
    return (True, "schedule") if _schedule_armed(schedule, local) else (False, "disarmed_schedule")


NEXT_CHANGE_DAYS = 8
HOLIDAY_HORIZON_DAYS = 400


def _local_ts(tz: dt.tzinfo, day: dt.date, minute: int) -> float:
    return dt.datetime(day.year, day.month, day.day, minute // 60, minute % 60, tzinfo=tz).timestamp()


def next_change(cfg: dict, now: float) -> dict | None:
    """The next moment the armed state flips ({"at", "armed"}), or None within the horizon (as hub soc.next_change)."""
    if not cfg.get("monitored"):
        return None
    tz = tz_of(cfg)
    cur = compute_armed(cfg, now)[0]
    today = dt.datetime.fromtimestamp(now, tz).date()
    cands: set[float] = set()
    o = active_override(cfg, now)
    if o:
        cands.add(float(o["until"]))
    edges = set()
    for w in cfg.get("arm_schedule") or []:
        if isinstance(w, dict):
            edges |= {m for m in (parse_hhmm(w.get("from")), parse_hhmm(w.get("to"))) if m is not None}
    for i in range(NEXT_CHANGE_DAYS + 1):
        day = today + dt.timedelta(days=i)
        cands.add(_local_ts(tz, day, 0))
        for m in edges:
            cands.add(_local_ts(tz, day, m))
    for h in cfg.get("arm_holidays") or []:
        try:
            day = dt.date.fromisoformat(str(h.get("date")))
        except (ValueError, AttributeError):
            continue
        if not (today <= day <= today + dt.timedelta(days=HOLIDAY_HORIZON_DAYS)):
            continue
        cands |= {_local_ts(tz, day, 0), _local_ts(tz, day + dt.timedelta(days=1), 0)}
        for m in (parse_hhmm(h.get("from")), parse_hhmm(h.get("to"))):
            if m is not None:
                cands.add(_local_ts(tz, day, m))
        for m in edges:
            cands.add(_local_ts(tz, day + dt.timedelta(days=1), m))
    for c in sorted(x for x in cands if x > now):
        if compute_armed(cfg, c)[0] != cur:
            return {"at": c, "armed": not cur}
    return None


# ---------------------------------------------------------------- the pushed config

def clean(raw) -> dict | None:
    """The hub's push as this server keeps it, or None (no schedule: full time). Unknown values fall back safely:
    an unknown mode is full time, an unknown outside option is catch-up."""
    if not isinstance(raw, dict):
        return None
    mode = raw.get("mode") if raw.get("mode") in MODES else "full"
    o = raw.get("arm_override")
    return {"mode": mode, "outside": raw.get("outside") if raw.get("outside") in OUTSIDE else "catchup",
            "timezone": raw.get("timezone") if isinstance(raw.get("timezone"), str) else None,
            "monitored": bool(raw.get("monitored")),
            "arm_schedule": [w for w in raw.get("arm_schedule") or [] if isinstance(w, dict)],
            "arm_holidays": [h for h in raw.get("arm_holidays") or [] if isinstance(h, dict)],
            "arm_override": {"mode": o.get("mode"), "until": o.get("until")} if isinstance(o, dict) else None,
            "key": str(raw.get("key") or "")[:64] or key(raw)}


def key(cfg: dict) -> str:
    """What the hub compares with its own (hub ai_schedule.key): a hash of the schedule's content."""
    body = {k: cfg.get(k) for k in ("mode", "outside", "timezone", "monitored", "arm_schedule", "arm_holidays")}
    o = cfg.get("arm_override")
    body["arm_override"] = {"mode": o.get("mode"), "until": o.get("until")} if isinstance(o, dict) else None
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


_cfg: tuple[bool, dict | None] = (False, None)   # (loaded, config)
_state: tuple[float, dict] | None = None          # (valid until, state)
_lock = threading.Lock()


def config() -> dict | None:
    """The schedule last pushed by the hub (None: full time). Read from the database once, then kept in memory (the
    PPE check reads it on the GPU thread)."""
    global _cfg
    if not settings.hub_enabled:
        return None   # a stand-alone server: no hub decides anything
    loaded, cfg = _cfg
    if not loaded:
        try:
            cfg = clean(db.get_setting(SETTING))
        except Exception:  # noqa: BLE001 - a failing database means full time, never no Qwen at all
            cfg = None
        _cfg = (True, cfg)
    return cfg


def apply(raw) -> dict | None:
    """hub_agent: the hub's push (welcome or its own frame), or None on unenrolling. Persisted, so the server keeps
    following it while the hub is unreachable and after a restart."""
    global _cfg, _state
    cfg = clean(raw)
    before = _cfg[1] if _cfg[0] else None
    with contextlib.suppress(Exception):
        db.set_setting(SETTING, cfg)
    with _lock:
        _cfg, _state = (True, cfg), None
    if (before or {}).get("key") != (cfg or {}).get("key"):
        log.info("AI schedule from the hub: %s", describe(state()))
    return cfg


def reload() -> None:
    """Forget the cached config (the settings row was changed directly, e.g. by unenrolling)."""
    global _cfg, _state
    with _lock:
        _cfg, _state = (False, None), None


def evaluate(cfg: dict | None, now: float) -> dict:
    """Pure: {mode, qwen: on | limited, reason, until (next change or None), outside, key} for a config at `now`."""
    if not cfg or cfg.get("mode") != "soc":
        return {"mode": "full", "qwen": "on", "reason": "full_time" if cfg else "default", "until": None,
                "outside": (cfg or {}).get("outside") or "catchup", "key": (cfg or {}).get("key")}
    armed, reason = compute_armed(cfg, now)
    nxt = next_change(cfg, now)
    return {"mode": "soc", "qwen": "on" if armed else "limited", "reason": reason, "until": nxt["at"] if nxt else None,
            "outside": cfg.get("outside") or "catchup", "key": cfg.get("key")}


def state(now: float | None = None) -> dict:
    """The schedule's state now (cached up to STATE_TTL_S, never past its next change); with `now` it is pure."""
    global _state
    if now is not None:
        return evaluate(config(), now)
    t = time.time()
    hit = _state
    if hit and hit[0] > t:
        return hit[1]
    s = evaluate(config(), t)
    with _lock:
        _state = (min(t + STATE_TTL_S, s["until"]) if s["until"] else t + STATE_TTL_S, s)
    return s


def full(now: float | None = None) -> bool:
    """Qwen does all of its work now (full time, or inside the Site's SOC hours)."""
    return state(now)["qwen"] == "on"


def outside() -> str:
    """What happens to descriptions outside the schedule: catchup | skip."""
    return state()["outside"]


def describe(s: dict) -> str:
    """One line for the log: "full time", "SOC hours: on until <time>", "SOC hours: limited (catch up when idle)"."""
    if s["mode"] != "soc":
        return "full time"
    until = f" until {dt.datetime.fromtimestamp(s['until']).strftime('%Y-%m-%d %H:%M')}" if s.get("until") else ""
    return f"SOC hours: {s['qwen']}{until}" + ("" if s["qwen"] == "on" else f" ({'catch up when idle' if s['outside'] == 'catchup' else 'skip descriptions'})")


def summary() -> dict:
    """For the heartbeat: the hub re-pushes when `key` is not its own, and shows each server's state."""
    s = state()
    return {k: s[k] for k in ("key", "mode", "qwen", "reason", "until", "outside")}


@contextlib.contextmanager
def job(kind: str):
    """Qwen requests made inside count as this kind (a catch-up description, say) whatever their vlmroute task."""
    token = JOB.set(kind)
    try:
        yield
    finally:
        JOB.reset(token)


def kind_of(task: str) -> str:
    return JOB.get() or KINDS.get(task, task)


# ---------------------------------------------------------------- the shared vLLM (central instances)

class SharedModel:
    """Is the host's shared vLLM busy? Read from its /metrics (no key needed; the instance's network reaches it):
    busy when requests wait, or catchup_vllm_busy_running or more run. Only on a server without a local Qwen whose
    remote is a vLLM (a central instance). A /metrics that can't be read is asked again after FAIL_S; meanwhile catch-up
    goes by this server's own queue only."""

    TTL_S = 5.0
    FAIL_S = 600.0

    def __init__(self) -> None:
        self._at = 0.0
        self._busy: bool | None = None
        self._failed_at = 0.0

    def url(self) -> str | None:
        own = (settings.catchup_vllm_metrics_url or "").strip()
        if own.lower() == "off":
            return None
        if own:
            return own
        if settings.local_vlm_enabled or not settings.remote_vlm_url:
            return None
        try:
            if db.get_setting("hub_vlm"):
                return None   # the hub's shared AI: no /metrics there (the hub caps it itself)
        except Exception:  # noqa: BLE001
            return None
        base = settings.remote_vlm_url.rstrip("/")
        return (base[:-3] if base.endswith("/v1") else base) + "/metrics"

    async def busy(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        url = self.url()
        if url is None or now - self._failed_at < self.FAIL_S:
            return False
        if now - self._at < self.TTL_S and self._busy is not None:
            return self._busy
        try:
            async with httpx.AsyncClient(timeout=3) as c:
                r = await c.get(url)
                r.raise_for_status()
                running, waiting = metric(r.text, "vllm:num_requests_running"), metric(r.text, "vllm:num_requests_waiting")
        except (httpx.HTTPError, ValueError) as e:
            if not self._failed_at:
                log.info("catch-up: the shared model's /metrics (%s) can't be read (%s); going by this server's queue", url, e)
            self._failed_at, self._busy = now, None
            return False
        if running is None and waiting is None:
            self._failed_at, self._busy = now, None
            return False
        self._at, self._busy = now, (waiting or 0) > 0 or (running or 0) >= settings.catchup_vllm_busy_running
        return self._busy


def metric(text: str, name: str) -> float | None:
    """The sum of one Prometheus metric's samples (every label set), or None when it isn't there."""
    total, seen = 0.0, False
    for line in text.splitlines():
        if not line.startswith(name) or line[len(name):len(name) + 1] not in ("{", " "):
            continue
        try:
            total += float(line.rsplit(None, 1)[1])
            seen = True
        except (IndexError, ValueError):
            continue
    return total if seen else None


shared = SharedModel()


# ---------------------------------------------------------------- metering

KEEP_DAYS = 40         # kept on the server (the hub keeps 13 months)
REPORT_DAYS = 14       # each report carries this many days, so a hub that was away a while still gets them
FLUSH_S = 30.0


def utc_day(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()


class Meter:
    """Qwen use per UTC day, kind and in/out of schedule: [requests, prompt tokens, completion tokens, seconds].
    Kept in memory and written to the settings table (`ai_usage`) at most every FLUSH_S."""

    SETTING = "ai_usage"

    def __init__(self) -> None:
        self._days: dict[str, dict[str, list[float]]] | None = None
        self._dirty_at = 0.0
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if self._days is None:
            try:
                raw = db.get_setting(self.SETTING) or {}
            except Exception:  # noqa: BLE001
                raw = {}
            self._days = {d: {k: [float(x) for x in v][:4] for k, v in rows.items() if isinstance(v, list) and len(v) >= 4}
                          for d, rows in (raw.get("days") or {}).items() if isinstance(rows, dict)}
        return self._days

    def record(self, kind: str, in_schedule: bool, seconds: float = 0.0, prompt: int | None = None,
               completion: int | None = None, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            days = self._load()
            row = days.setdefault(utc_day(now), {}).setdefault(f"{kind}|{'in' if in_schedule else 'out'}", [0.0, 0.0, 0.0, 0.0])
            row[0] += 1
            row[1] += int(prompt or 0)
            row[2] += int(completion or 0)
            row[3] += max(0.0, float(seconds or 0.0))
            if not self._dirty_at:
                self._dirty_at = now
        if now - self._dirty_at >= FLUSH_S:
            self.flush(now)

    def flush(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            if self._days is None:
                return
            cutoff = utc_day(now - KEEP_DAYS * 86400)
            for d in [d for d in self._days if d < cutoff]:
                del self._days[d]
            data = {"days": {d: {k: [round(x, 2) for x in v] for k, v in rows.items()} for d, rows in self._days.items()}}
            self._dirty_at = 0.0
        try:
            db.set_setting(self.SETTING, data)
        except Exception as e:  # noqa: BLE001 - counting must never break a Qwen request
            log.warning("could not save the Qwen meter: %s", e)

    def rows(self, days: int = REPORT_DAYS, now: float | None = None) -> list[dict]:
        """The report for the hub: one row per day, kind and in/out of schedule, the last `days` UTC days."""
        now = time.time() if now is None else now
        since = utc_day(now - (days - 1) * 86400)
        with self._lock:
            src = {d: {k: list(v) for k, v in r.items()} for d, r in self._load().items() if d >= since}
        out = []
        for d in sorted(src):
            for k, v in sorted(src[d].items()):
                kind, _, sched = k.partition("|")
                out.append({"day": d, "kind": kind, "sched": sched or "in", "requests": int(v[0]), "prompt_tokens": int(v[1]),
                            "completion_tokens": int(v[2]), "seconds": round(v[3], 2)})
        return out


meter = Meter()
