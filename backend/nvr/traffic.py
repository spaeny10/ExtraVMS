"""Traffic cameras: the plate reads a Milesight traffic camera (TS series, LPR licensed) makes itself and pushes here.

Measured on TSS-DT cam8 (TS4441-X36RPE, docs/traffic-camera-plan.md): one JSON POST per vehicle (~1,450 an hour on a
highway), every field a string ("-" when empty), base64 JPEGs inline (plate crop ~2 KB, vehicle crop ~47 KB, full
1080p ~323 KB), the time in the camera's local time with no zone, and a status report `{"device_name": ...}` every
statusReportInterval (300 s). The camera resends a missed push only when it has an SD card.

- ingest(): one push -> a vehicle_reads row. The plate is normalized (plate.normalize; "No Plates" is none), the
  camera's state and direction are kept as hints (a Kansas plate read as Ohio, a rear view as "Approach"), boxes are
  normalized to the camera's frame. Time: the camera's zone offset is learned from fresh pushes (local time minus
  receive time, to the quarter hour) and kept on the camera; a resent read keeps its own time.
- Crops: the plate and vehicle crops of every read; the full snapshot only for a list hit (the recording has the frame)
  unless settings.traffic_keep_full. Under data_dir/traffic/<camera>/<YYYY-MM-DD>/. A read pushed without a vehicle
  picture (cam8 with "optimal push" on leaves it out of ~1 in 8 reads, 2026-10-28) gets one from the recording: the
  camera records continuously, so REC_DELAY_S after the read (the recording has that moment by then) the vehicle box is
  cut from the full-resolution recorded frame showing it (`...-vehicle-rec.jpg`, `vehicle_source` "recording").
  Which frame: a read's time and the recording's timeline differ by an offset that is steady within one recording
  segment and changes from one to the next (a segment's start time is the wall clock when MediaMTX opened it; the
  camera's keyframes are 2 s apart). On cam8 2026-10-28 it was -0.4 s, then +1.5 s, then -0.5 s; a fixed shift showed
  empty road or the vehicle two reads earlier. So the offset is learned per segment (Aligner) from reads whose plate crop
  matches the recording strongly (>= STRONG_MATCH; right frames scored 0.97-1.00, wrong ones up to 0.89), and a read
  with no strong match of its own uses its segment's offset, taken once AGREE strong matches agree (a single 0.89 match
  was wrong). Only a moving vehicle's match teaches it: one moment in time (PEAK_SPAN_S, PEAK_GAP) on a read with a
  vehicle type; the dealership sign the camera reads as a plate at night matches at every offset. Every recorded frame
  is matched, not a grid: a passing plate sits in its box for one frame (cam8 2026-10-29 07:3x: 0.98-1.00 at one frame,
  under 0.75 a frame either side), so a 0.25 s grid found almost none at dawn. The camera's clock drifts (+0.13 s an
  hour that day, the offset -0.5 s at 18:00 and -2.2 s by 07:30), so a segment not learned yet is searched first
  around the nearest learned one (PRIOR_S), then wide. Until then a read waits
  (REC_TRIES x REC_RETRY_S, the life of a segment) rather than show a guess. Cuts run one at a time on CUT_POOL, never
  in the shared pool: there a page of reads asking for their pictures filled it (TSS-DT 2026-10-28: the disk check queued behind them and read as a hung disk, a Read plate timed
  out). A picture asked for before it is cut answers 404 at once and the read is queued; older reads without one are
  queued at start-up (BACKFILL_DAYS). The recording disk is a shared HDD: a fresh read's moment is still in memory, an
  older one is read back from the disk (cam8's 4K segments; MediaMTX's listing timed out for every camera while
  older reads were cut), so a segment's learned offset is kept (recording_offsets) and an older read is cut only near
  a known offset (its segment's, NARROW, or the nearest one learned, PRIOR_S: ~2-3 s of video), paced (OLD_DUTY),
  never with a wide search.
- Passes: reads of one camera less than PASS_GAP_S apart whose plates share PASS_COMMON characters in order are one
  pass (671AED / 3671AED / 387AED); `best` marks the most confident. A parked vehicle is one pass too: the camera
  re-reads it every few seconds (cam8 aimed at a car lot, 2026-10-28: one blue car 56 times in a minute), so a read of
  the same type and colour in nearly the same box (IoU >= PARKED_IOU) within PARKED_GAP_S of the last one joins it.
  Counts and the browse list show a pass once.
- Events: a read at settings.traffic_attach_min_conf or more puts its plate on the vehicle event of that moment
  (attach_to_event, also called when such an event is verified later); Otso doesn't read plates on these cameras.
- Lists (plate_lists): allow marks the read; deny and watch open an alarm event (verified at once: the camera saw the
  vehicle) with the plate, the crops and a site rule (`policy`, kind "plate_list": high for deny, medium for watch), so
  it reaches the hub's alerts and the SOC like any broken rule. Exact or near (one character off, plates of 5+). The
  same plate on the same list and camera within LIST_COOLDOWN_S joins the open alarm.
- Read plate (on demand): the camera's reader misses plates its own crops show (cam8, 2026-10-28: plates ~60 px wide
  in its 1080p frame, 18 % of reads with a plate); a person can have Otso read the vehicle crop (plate.read, metered
  "plate", one run per read every settings.plate_rate_s). Otso's answer is kept beside the camera's (`otso`,
  `otso_plate`), never over it, and search finds it.
- Otso looks on its own (Looker): each vehicle's best read, newest first, while Qwen has nothing else to do and the
  Site's AI schedule allows catch-up work, at most settings.traffic_look_per_hour: the plate the camera missed (the
  strict instruction, kept only when Otso is sure) and what the vehicle is ("white Ford F-150 pickup truck, ladder
  rack": otso_look), so it can be found by description (search `look`, Ask). An Otso plate on a deny / watch list
  raises the alarm as a camera read does.
- Pattern alerts (check_patterns), per camera (traffic.alerts): a plate passing settings.traffic_circling_passes times
  within traffic_circling_s ("circling"), a plate never seen before ("new") or a regular far outside its usual times
  ("unusual_time"); new and unusual only while the Site's SOC monitoring is armed unless set to "always", and only
  once the camera has traffic_learn_days of history. An alarm event (policy kind "plate_pattern", with the plate's
  counted notes) reaches the hub and the SOC like a list hit.
- Setup (camera_state / configure): Milesight's own API (milesight.py): plate reading on, one push slot pointed at
  /api/traffic/push/<camera>/<secret> with every event type and a status report.
- Health: a camera set up but silent for settings.traffic_silent_s raises traffic_push_silent.
- No vehicle: a read without a vehicle type and with no plate or one under settings.traffic_noise_conf is something the
  camera mistook for a plate (cam8 at night: the car lot's sign, "556BE" / "56RE" up to ~6 a minute, conf 0.3-0.6, at
  one spot; the only real plates without a vehicle type that day scored 0.88 and 0.99). It is stored but hidden from the reads list (unless `noise`, or a plate search), counts, today's totals and
  Ask, and no picture is cut for it. The camera's own region didn't stop it: its polygon already left the sign's plate
  box out (2 of 554 inside), and raising its edge would have cost 96-386 of 528 real plates; its confidence level
  would have too, as real plates at night score 0.34-0.48 like the sign.

Blocking (sqlite, files, the camera's API): call from a worker thread.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import hmac
import json
import logging
import re
import secrets
import shutil
from concurrent.futures import ThreadPoolExecutor
import socket
import asyncio
import threading
import time
from collections import deque
from pathlib import Path

from . import aischedule, plate
from .config import settings
from .db import db

log = logging.getLogger("nvr.traffic")

PASS_GAP_S = 6.0
PASS_COMMON = 4
# A still vehicle re-read within PARKED_GAP_S of its last read, in a box overlapping it by PARKED_IOU, is the same pass.
# Measured on cam8 2026-10-28: a car lot's 444 reads -> ~210 vehicles (repeats come up to a minute apart, boxes wobble),
# while at most ~3 % of a road's 542 reads merged (some of them a car parked by the road). Wider limits merge more cars.
PARKED_GAP_S = 30.0
PARKED_IOU = 0.8
RECENT_READS = 200                 # per camera, for both (a busy lot reads several vehicles a second)
LIST_COOLDOWN_S = 600.0
REC_DELAY_S = 8.0                  # after a read without a vehicle picture: recording_lag (3 s) and a margin
REC_TRIES = 15                     # ...tried again REC_RETRY_S apart while the recording or its segment's offset isn't
REC_RETRY_S = 60.0                 # there: a 10-minute segment has a few strong matches by then (fewer at night)
BACKFILL_DAYS = 3                  # at start-up, reads this recent still without a vehicle picture are queued
BACKFILL_TRIES = REC_TRIES - 2     # ...with fewer tries (an old segment's offset may never be learned)
FRESH_S = 600.0                    # a read this recent may be searched wide (its recording is still in memory)
OLD_DUTY = 2.0                     # after an older read's cut, idle this many times as long as it took (shared disk)
OLD_GAP_S = 5.0                    # ...at most this long
CUT_POOL = ThreadPoolExecutor(2, thread_name_prefix="traffic-cut")   # decode + match: off the shared pool
STRONG_MATCH = 0.93                # plate crop vs recording: a match this strong places the read (right: 0.97-0.99)
AGREE = 2                          # ...and a segment's offset is taken once this many strong matches agree
AGREE_S = 0.5                      # ...within this
PEAK_SPAN_S = 0.5                  # a match teaches the offset only when it is one moment: its strong frames this close,
PEAK_GAP = 0.3                     # the best this far above the median (a still thing matches everywhere: the car lot's
                                   # sign read as "656BE" at night scored >= 0.93 at 53-70 of 81 offsets over 20 s)
FRAME_STEP = 0.02                  # every recorded frame is matched: targets this far apart, each answered by the frame
FRAME_TOL = 0.0101                 # within half a step (cam8 records 25 fps, a frame every 0.04 s)


def window(center: float, half: float) -> list[float]:
    n = round(half / FRAME_STEP)
    return [round(center + k * FRAME_STEP, 2) for k in range(-n, n + 1)]


WIDE = window(-5.0, 10.0)          # -15 .. +5 s: no offset learned near this read
NARROW_S = 1.0                     # +- around the segment's learned offset
PRIOR_S = 1.5                      # +- around the nearest learned offset (the camera's clock drifts between segments)
PRIOR_MAX_S = 6 * 3600.0           # ...learned at most this far from the read
SKEW_MAX_S = 120.0                 # a fresh push this far from the receive time (after the zone) has a wrong clock
RESEND_MAX_S = 7 * 86400           # a resent read (SD card) may be this old
BODY_MAX = 8 * 1024 * 1024
LISTS = ("allow", "deny", "watch")
ALARM_LISTS = {"deny": "high", "watch": "medium"}
IMAGE_KINDS = {"plate": "license_plate_snapshot", "vehicle": "vehicle_snapshot", "full": "full_snapshot",
               "violation": "violation_snapshot"}
PUSH_MASK = 1 | 4 | 8 | 16 | 32 | 64 | 128 | 256 | 512   # plate, attributes, violation, counting, lists, line-cross, congestion
STATUS_REPORT_S = 300
NO_PLATE = {"NOPLATES", "NOPLATE", "UNKNOWN", ""}
LOCAL_FMT = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")
PATH_RE = re.compile(r"/api/traffic/push/([^/]+)/")


class SetupError(Exception):
    """The camera can't be set up (no plate licence, every push slot taken, the camera refused)."""


# ---------------------------------------------------------------- parsing

def _s(v) -> str | None:
    """The camera's "-" (and empty) is none."""
    s = str(v).strip() if v is not None else ""
    return None if s in ("", "-") else s


def _f(v) -> float | None:
    try:
        return float(_s(v))   # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _box(p: dict, x1: str, y1: str, x2: str, y2: str, w: float | None, h: float | None) -> list[float] | None:
    vals = [_f(p.get(k)) for k in (x1, y1, x2, y2)]
    if None in vals or not w or not h:
        return None
    l, t, r, b = vals   # type: ignore[misc]
    box = [min(1.0, max(0.0, round(v, 4))) for v in (l / w, t / h, r / w, b / h)]
    return box if box[2] > box[0] and box[3] > box[1] else None


def local_time(v) -> dt.datetime | None:
    s = _s(v)
    for fmt in LOCAL_FMT:
        try:
            return dt.datetime.strptime(s or "", fmt)
        except ValueError:
            continue
    return None


def read_time(local: dt.datetime | None, received: float, zone_offset_s: int | None,
              resend: bool = False) -> tuple[float, int | None, bool]:
    """(UTC time of the read, the camera's zone offset (local minus UTC, s) as now known, clock_ok). A push within
    SKEW_MAX_S of the receive time at the known offset is fresh. Otherwise the offset is learned again (to the nearest
    quarter hour: the camera's zone changed), unless the camera resends missed reads (it has an SD card: `resend`) and
    the read is up to RESEND_MAX_S old at the known offset: a late read, kept at its own time. A two-hour-late resend and
    a zone moved two hours look the same; only a camera that can resend has late reads. Neither: the receive time."""
    if local is None:
        return received, zone_offset_s, False
    naive = local.replace(tzinfo=dt.timezone.utc).timestamp()   # the camera's wall clock read as if it were UTC
    if zone_offset_s is not None:
        late = received - (naive - zone_offset_s)
        if -SKEW_MAX_S <= late <= SKEW_MAX_S or (resend and 0 < late <= RESEND_MAX_S):
            return naive - zone_offset_s, zone_offset_s, True
    off = round((naive - received) / 900) * 900
    if abs(naive - off - received) <= SKEW_MAX_S:
        return naive - off, int(off), True
    return received, zone_offset_s, False


def parse(p: dict, received: float, zone_offset_s: int | None = None, resend: bool = False) -> dict:
    """One push -> the read's fields (no images). `zone_offset` the camera's offset after this read."""
    w, h = _f(p.get("resolution_width")), _f(p.get("resolution_height"))
    raw = _s(p.get("license_plate"))
    norm = plate.normalize(raw) if raw and re.sub(r"[^A-Z]", "", raw.upper()) not in NO_PLATE else None
    ts, off, clock_ok = read_time(local_time(p.get("time")), received, zone_offset_s, resend)
    conf = _f(p.get("plate_confidence"))
    direction = (_s(p.get("direction")) or "").lower()
    region = _s(p.get("country_region"))
    keep = {k: v for k, v in p.items() if k not in IMAGE_KINDS.values() and not str(k).startswith("evidence_snapshot")
            and k not in ("license_plate", "plate_confidence", "country_region", "plate_type", "plate_color", "vehicle_type",
                          "vehicle_color", "vehicle_brand", "direction", "speed", "alarm_type", "time")}
    return {
        "ts": ts, "received_at": received, "zone_offset": off, "clock_ok": clock_ok,
        "kind": (_s(p.get("event_type")) or "regular").lower()[:32],
        "plate": norm, "plate_raw": ((raw or "")[:20] or None) if norm else None,
        "confidence": round(conf, 3) if conf is not None and norm else None,
        "region": plate.region_of(region) if norm and region else None,
        "plate_type": _s(p.get("plate_type")) if norm else None, "plate_color": _s(p.get("plate_color")) if norm else None,
        "vehicle_type": _s(p.get("vehicle_type")), "vehicle_color": _s(p.get("vehicle_color")),
        "vehicle_brand": _s(p.get("vehicle_brand")),
        "direction": direction if direction in ("approach", "away") else None,
        "speed": _f(p.get("speed")), "alarm": _s(p.get("alarm_type")),
        "detection_region": _s(p.get("detection_region_name")) or _s(p.get("detection_region")),
        "box": _box(p, "vehicle_tracking_box_x1", "vehicle_tracking_box_y1", "vehicle_tracking_box_x2", "vehicle_tracking_box_y2", w, h),
        "plate_box": _box(p, "coordinate_x1", "coordinate_y1", "coordinate_x2", "coordinate_y2", w, h),
        "extra": keep,
    }


def noise_sql() -> tuple[str, list]:
    """SQL (and its parameter) for a read that is not a vehicle (see "No vehicle" above)."""
    return "(vehicle_type IS NULL AND (plate IS NULL OR COALESCE(confidence, 0) < ?))", [settings.traffic_noise_conf]


def is_noise(r: dict) -> bool:
    return not r.get("vehicle_type") and (not r.get("plate") or (r.get("confidence") or 0) < settings.traffic_noise_conf)


def is_status(p) -> bool:
    """The status report: no event_type (just {"device_name": ...})."""
    return isinstance(p, dict) and not _s(p.get("event_type"))


def jpeg(v) -> bytes | None:
    s = _s(v)
    if not s or len(s) < 100:
        return None
    if s.startswith("data:"):
        s = s.split(",", 1)[-1]
    try:
        b = base64.b64decode(s, validate=False)
    except (binascii.Error, ValueError):
        return None
    return b if b[:2] == b"\xff\xd8" else None


# ---------------------------------------------------------------- passes and lists

def common(a: str, b: str) -> int:
    """Characters the two plates share in order (longest common subsequence)."""
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for k, y in enumerate(b):
            cur.append(prev[k] + 1 if x == y else max(prev[k + 1], cur[k]))
        prev = cur
    return prev[-1]


def near(a: str, b: str) -> bool:
    """One character off (substituted, missing or extra), on plates of 5+ characters."""
    if a == b:
        return True
    if min(len(a), len(b)) < 5 or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    s, t = (a, b) if len(a) < len(b) else (b, a)
    return any(t[:i] + t[i + 1:] == s for i in range(len(t)))


class Passes:
    """The last reads per camera, to group one vehicle's reads (in memory: a restart starts new passes)."""

    def __init__(self) -> None:
        self.recent: dict[str, deque] = {}
        self.lock = threading.Lock()

    def place(self, camera_id: str, read_id: int, ts: float, plate_: str | None, conf: float | None,
              box: list[float] | None = None, look: tuple[str | None, str | None] = (None, None)) -> tuple[int, bool]:
        """(pass id, whether this read is now the pass's best). `look`: the camera's (vehicle type, colour)."""
        with self.lock:
            q = self.recent.setdefault(camera_id, deque(maxlen=RECENT_READS))
            mine = None
            if plate_ and len(plate_) >= PASS_COMMON:
                for r in reversed(q):
                    if abs(ts - r["ts"]) <= PASS_GAP_S and r["plate"] and common(r["plate"], plate_) >= PASS_COMMON:
                        mine = r
                        break
            if mine is None and box and all(look):
                for r in reversed(q):   # the same still vehicle read again
                    if (abs(ts - r["ts"]) <= PARKED_GAP_S and r["look"] == look and r["box"] and iou(r["box"], box) >= PARKED_IOU
                            and not (plate_ and r["plate"] and common(r["plate"], plate_) < PASS_COMMON)):
                        mine = r
                        break
            entry = {"ts": ts, "plate": plate_, "box": box, "look": look, "conf": conf or 0}
            if mine is None:
                q.append({**entry, "pass": read_id, "best": read_id})
                return read_id, True
            q.append({**entry, "pass": mine["pass"], "best": mine["best"]})
            if (conf or 0) > mine["conf"]:
                old = mine["best"]
                for r in q:
                    if r["pass"] == mine["pass"]:
                        r["best"], r["conf"] = read_id, max(r["conf"], conf or 0)
                db.execute("UPDATE vehicle_reads SET best=0 WHERE id=?", [old])
                return mine["pass"], True
            return mine["pass"], False


passes = Passes()


def iou(a: list[float], b: list[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


# ---- list entries and their options (see "Lists" in docs/traffic-camera-plan.md)

CATEGORIES = ("employee", "contractor", "visitor", "resident", "delivery", "trespass", "police", "other")
ACTIONS = ("alarm", "mark")
PRIORITIES = ("low", "medium", "high")
LIST_ORDER = {"deny": 0, "watch": 1, "allow": 2}
ENTRY_JSON = ("schedule", "cameras", "expect")
WILD = re.compile(r"[?*]")


def entry_row(r: dict | None) -> dict | None:
    """A plate_lists row with its JSON options decoded."""
    if r is None:
        return None
    out = dict(r)
    for k in ENTRY_JSON:
        out[k] = json.loads(r[k]) if r.get(k) else None
    return out


def list_entries(now: float | None = None) -> list[dict]:
    now = now or time.time()
    return [entry_row(r) for r in db.all("SELECT * FROM plate_lists WHERE expires_at IS NULL OR expires_at > ? ORDER BY list, plate", [now])]


def is_pattern(p: str | None) -> bool:
    return bool(p and WILD.search(p))


def normalize_entry_plate(raw: str) -> str | None:
    """A plate typed for a list: like plate.normalize, but ? (one character) and * (any run) are kept for a partial
    plate (a police lookout "ABC 12?", "starts with 34": 34*), which needs 2+ known characters."""
    s = str(raw or "").strip().upper()
    if "?" in s or "*" in s:
        p = re.sub(r"\*+", "*", re.sub(r"[^A-Z0-9?*]", "", s))
        known = len(WILD.sub("", p))
        return p if known >= 2 and len(p) <= 12 and p != "*" else None
    return plate.normalize(raw)


def plate_fits(entry_plate: str, plate_: str) -> bool:
    """An entry's plate (or partial plate) against a read plate: exactly, or ? = one character and * = any run."""
    if not is_pattern(entry_plate):
        return entry_plate == plate_
    rx = "".join("." if c == "?" else ".*" if c == "*" else re.escape(c) for c in entry_plate)
    return re.fullmatch(rx, plate_) is not None


def _hhmm(v) -> int | None:
    """"7:00" / "07:00" / "23:59" -> minutes of the day; None for anything else."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(v or ""))
    return int(m[1]) * 60 + int(m[2]) if m and int(m[1]) < 24 and int(m[2]) < 60 else None


def _site_now(now: float) -> dt.datetime:
    """`now` in the Site's time zone (the hub's AI schedule carries it), else the server's own."""
    cfg = aischedule.config()
    tz = aischedule.tz_of(cfg) if cfg and cfg.get("timezone") else None
    return dt.datetime.fromtimestamp(now, tz) if tz else dt.datetime.fromtimestamp(now)


def schedule_active(schedule: dict | None, now: float, armed: bool | None) -> bool:
    """always (None) | {when: soc}: while the Site's SOC monitoring is armed (unknown = not) | {when: hours, days:
    [0-6, Monday 0], from: "HH:MM", to: "HH:MM"} in the Site's time zone, `to` <= `from` running past midnight (the
    days are the start's)."""
    if not schedule:
        return True
    if schedule.get("when") == "soc":
        return bool(armed)
    f, t = _hhmm(schedule.get("from")), _hhmm(schedule.get("to"))
    if f is None or t is None:
        return True
    days = set(schedule.get("days") or range(7))
    local = _site_now(now)
    m = local.hour * 60 + local.minute
    if f < t:
        return local.weekday() in days and f <= m < t
    # overnight (or 24 h when from == to): today's evening part, or yesterday's after midnight
    return (local.weekday() in days and m >= f) or ((local.weekday() - 1) % 7 in days and m < t)


def entry_active(e: dict, now: float, camera_id: str | None = None, armed: bool | None = None) -> bool:
    """Is the entry in force for a read now: not expired, its schedule, its cameras."""
    if e.get("expires_at") and e["expires_at"] <= now:
        return False
    if camera_id and e.get("cameras") and camera_id not in e["cameras"]:
        return False
    return schedule_active(e.get("schedule"), now, armed)


def _armed_if_needed(entries: list[dict]) -> bool | None:
    if not any((e.get("schedule") or {}).get("when") == "soc" for e in entries):
        return None
    from .deterrence import armed_now
    return armed_now()


def match_list(plate_: str | None, now: float | None = None, camera_id: str | None = None, armed: bool | None = None,
               near_ok: bool = True) -> dict | None:
    """The list entry this plate hits, of those in force (entry_active): an exact entry first, then a partial plate
    ("pattern"), then a near one (one character off; near_ok), deny before watch before allow at each step. The entry
    with "match" and its "effect" (hit_effect)."""
    if not plate_:
        return None
    now = now or time.time()
    entries = list_entries(now)
    if armed is None:
        armed = _armed_if_needed(entries)
    entries = [e for e in entries if entry_active(e, now, camera_id, armed)]
    steps = (("exact", lambda e: not is_pattern(e["plate"]) and e["plate"] == plate_),
             ("pattern", lambda e: is_pattern(e["plate"]) and plate_fits(e["plate"], plate_)),
             ("near", lambda e: near_ok and not is_pattern(e["plate"]) and near(e["plate"], plate_)))
    for how, fits in steps:
        hits = [e for e in entries if fits(e)]
        if hits:
            e = min(hits, key=lambda e: LIST_ORDER.get(e["list"], 9))
            return {**e, "match": how, "effect": hit_effect(e)}
    return None


def hit_effect(e: dict) -> dict:
    """What a hit does: {alarm: bool, priority}. The list's own unless the entry says: deny a high alarm, watch a medium
    one, allow only marks the read (an allow entry set to alarm is a low one: "tell me when the owner arrives")."""
    action = e.get("action") or ("mark" if e["list"] == "allow" else "alarm")
    return {"alarm": action == "alarm", "priority": e.get("priority") or ALARM_LISTS.get(e["list"], "low")}


def conflicts(e: dict, entries: list[dict]) -> list[dict]:
    """Other entries for the same plate (or a partial plate that covers it): [{id, list, plate}]."""
    out = []
    for o in entries:
        if o["id"] == e["id"]:
            continue
        same = o["plate"] == e["plate"] or (is_pattern(o["plate"]) and not is_pattern(e["plate"]) and plate_fits(o["plate"], e["plate"])) \
            or (is_pattern(e["plate"]) and not is_pattern(o["plate"]) and plate_fits(e["plate"], o["plate"]))
        if same:
            out.append({"id": o["id"], "list": o["list"], "plate": o["plate"]})
    return out


def clean_schedule(v) -> dict | None:
    if not v:
        return None
    if not isinstance(v, dict) or v.get("when") not in ("always", "soc", "hours"):
        raise ValueError("schedule.when must be always, soc or hours")
    if v["when"] == "always":
        return None
    if v["when"] == "soc":
        return {"when": "soc"}
    f, t = _hhmm(v.get("from")), _hhmm(v.get("to"))
    if f is None or t is None:
        raise ValueError("set hours need from and to as HH:MM")
    days = sorted({int(d) for d in (v.get("days") or range(7)) if str(d).isdigit() and 0 <= int(d) <= 6})
    if not days:
        raise ValueError("pick at least one day")
    return {"when": "hours", "days": days, "from": f"{f // 60:02d}:{f % 60:02d}", "to": f"{t // 60:02d}:{t % 60:02d}"}


def clean_expect(v) -> dict | None:
    if not v:
        return None
    if not isinstance(v, dict):
        raise ValueError("expect must be {vehicle_type, color, make, model}")
    out = plate.parse_vehicle({k: v.get(k) for k in ("vehicle_type", "color", "make", "model")})
    if out and not (out.get("vehicle_type") or out.get("color") or out.get("make")):
        raise ValueError("the expected vehicle needs a type, colour or make")
    return out


def _entry_fields(category=None, schedule=None, action=None, priority=None, cameras=None, expect=None) -> dict:
    if category is not None and category not in CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(CATEGORIES)}")
    if action is not None and action not in ACTIONS:
        raise ValueError("action must be alarm or mark")
    if priority is not None and priority not in PRIORITIES:
        raise ValueError("priority must be low, medium or high")
    cams = [str(c)[:64] for c in cameras][:50] if cameras else None
    known = {c["id"] for c in db.cameras()}
    if cams and any(c not in known for c in cams):
        raise ValueError(f"unknown camera {next(c for c in cams if c not in known)!r}")
    sched, exp = clean_schedule(schedule), clean_expect(expect)
    return {"category": category, "schedule": json.dumps(sched) if sched else None, "action": action, "priority": priority,
            "cameras": json.dumps(cams) if cams else None, "expect": json.dumps(exp) if exp else None}


def add_entry(plate_raw: str, list_: str, note: str = "", expires_at: float | None = None, by: str | None = None,
              **options) -> dict:
    """Add (or replace: one entry per plate and list) an entry. options: category, schedule, action, priority, cameras,
    expect (see _entry_fields). ValueError for anything that isn't valid."""
    p = normalize_entry_plate(plate_raw)
    if not p:
        raise ValueError(f"{plate_raw[:20]!r} is not a plate (a partial plate needs 2 known characters)")
    if list_ not in LISTS:
        raise ValueError(f"list must be one of {', '.join(LISTS)}")
    f = _entry_fields(**options)
    now = time.time()
    db.execute("INSERT INTO plate_lists (plate, list, note, expires_at, created_at, by, category, schedule, action, priority, "
               "cameras, expect, updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
               "ON CONFLICT(plate, list) DO UPDATE SET note=excluded.note, expires_at=excluded.expires_at, "
               "category=excluded.category, schedule=excluded.schedule, action=excluded.action, priority=excluded.priority, "
               "cameras=excluded.cameras, expect=excluded.expect, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
               [p, list_, (note or "")[:200], expires_at, now, by, f["category"], f["schedule"], f["action"], f["priority"],
                f["cameras"], f["expect"], now, by])
    return entry_row(db.one("SELECT * FROM plate_lists WHERE plate=? AND list=?", [p, list_]))


def edit_entry(entry_id: int, by: str | None = None, **changes) -> dict | None:
    """Change an entry's note, expires_at or options; a key present with None clears it."""
    cur = entry_row(db.one("SELECT * FROM plate_lists WHERE id=?", [entry_id]))
    if cur is None:
        return None
    opts = {k: changes[k] if k in changes else cur.get(k) for k in ("category", "schedule", "action", "priority", "cameras", "expect")}
    f = _entry_fields(**opts)
    sets = {**f, "updated_at": time.time(), "updated_by": by}
    if "note" in changes:
        sets["note"] = (changes["note"] or "")[:200]
    if "expires_at" in changes:
        sets["expires_at"] = changes["expires_at"]
    db.execute(f"UPDATE plate_lists SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", [*sets.values(), entry_id])
    return entry_row(db.one("SELECT * FROM plate_lists WHERE id=?", [entry_id]))


# ---- what the Lists page shows

def _plate_where(p: str) -> tuple[str, list]:
    """SQL for reads of an entry's plate: the camera's or Otso's, a partial plate by GLOB (? and * mean the same there)."""
    if is_pattern(p):
        return "(plate GLOB ? OR otso_plate GLOB ?)", [p, p]
    return "(plate=? OR otso_plate=?)", [p, p]


def applies(e: dict, others: list[dict]) -> str | None:
    """Which list wins when the same plate is on several (deny, then watch, then allow, exact before partial): None
    when this entry does, else that list's name."""
    better = [o for o in others if LIST_ORDER[o["list"]] < LIST_ORDER[e["list"]]
              or (o["list"] == e["list"] and is_pattern(e["plate"]) and not is_pattern(o["plate"]))]
    return min(better, key=lambda o: LIST_ORDER[o["list"]])["list"] if better else None


def lists_overview(now: float | None = None) -> list[dict]:
    """Every entry (expired ones too, marked), each with: last seen (time, camera, read), passes in the last 7 and 30
    days, Otso's latest description of its vehicle, other entries for the same plate and which list applies, and
    whether it is in force now (its schedule; cameras aside)."""
    now = now or time.time()
    rows = [entry_row(r) for r in db.all("SELECT * FROM plate_lists ORDER BY list, plate")]
    armed = _armed_if_needed(rows)
    sql, p = noise_sql()
    out = []
    for e in rows:
        w, wp = _plate_where(e["plate"])
        last = db.one(f"SELECT id, ts, camera_id, otso_look FROM vehicle_reads WHERE {w} AND NOT {sql} ORDER BY ts DESC LIMIT 1", [*wp, *p])
        n = db.one(f"SELECT COUNT(DISTINCT CASE WHEN ts >= ? THEN COALESCE(pass_id, id) END) AS d7, "
                   f"COUNT(DISTINCT COALESCE(pass_id, id)) AS d30 FROM vehicle_reads WHERE {w} AND ts >= ? AND NOT {sql}",
                   [now - 7 * 86400, *wp, now - 30 * 86400, *p])
        look = db.one(f"SELECT otso_look FROM vehicle_reads WHERE {w} AND otso_look IS NOT NULL ORDER BY ts DESC LIMIT 1", wp)
        others = conflicts(e, rows)
        out.append({**e, "expired": bool(e["expires_at"] and e["expires_at"] <= now), "pattern": is_pattern(e["plate"]),
                    "last_seen": last["ts"] if last else None, "last_camera": last["camera_id"] if last else None,
                    "last_read_id": last["id"] if last else None, "seen_7d": n["d7"] or 0, "seen_30d": n["d30"] or 0,
                    "look": look["otso_look"] if look else None, "conflicts": others,
                    "overridden_by": applies(e, others), "active": entry_active(e, now, None, armed),
                    "effect": hit_effect(e)})
    return out


def entry_detail(entry_id: int, now: float | None = None) -> dict | None:
    """One entry opened on the Lists page: its row (lists_overview), its latest sightings (one read per pass, newest
    first), the plate's counted notes (an exact plate), the alarms it raised in the last 30 days, and the vehicle Otso
    saw last (to save as the vehicle the plate belongs on)."""
    now = now or time.time()
    row = next((x for x in lists_overview(now) if x["id"] == entry_id), None)
    if row is None:
        return None
    w, wp = _plate_where(row["plate"])
    sql, p = noise_sql()
    reads = db.all(f"SELECT * FROM vehicle_reads WHERE {w} AND NOT {sql} ORDER BY ts DESC LIMIT 60", [*wp, *p])
    seen, sightings = set(), []
    for r in reads:
        k = r["pass_id"] or r["id"]
        if k not in seen and len(sightings) < 8:
            seen.add(k)
            sightings.append(public(r))
    vehicle = None
    for r in reads:
        o = json.loads(r["otso"]) if r.get("otso") else {}
        if o.get("vehicle"):
            vehicle = {**o["vehicle"], "read_id": r["id"], "ts": r["ts"]}
            break
    pe, pp = ("plate GLOB ?", [row["plate"]]) if row["pattern"] else ("plate=?", [row["plate"]])
    alarms = [{"id": a["id"], "ts": a["start_ts"], "priority": a["priority"], "camera_id": a["camera_id"],
               "text": (json.loads(a["policy"]) if a["policy"] else {}).get("text")}
              for a in db.all(f"SELECT id, start_ts, priority, camera_id, policy FROM events WHERE {pe} AND start_ts >= ? "
                              f"AND (policy LIKE '%plate_list%' OR policy LIKE '%plate_pattern%') ORDER BY start_ts DESC LIMIT 10",
                              [*pp, now - 30 * 86400])]
    notes = plate_notes(row["plate"], now, now)["notes"] if not row["pattern"] and row["last_seen"] else []
    return {**row, "sightings": sightings, "notes": notes, "alarms": alarms, "vehicle": vehicle}


IMPORT_MAX = 2000


def import_entries(rows: list[dict], by: str | None = None) -> list[dict]:
    """CSV import (the page parses the file): each row added like add_entry; [{line, plate, list, ok, error?}]."""
    out = []
    for k, x in enumerate(rows[:IMPORT_MAX], 1):
        try:
            e = add_entry(str(x.get("plate") or ""), str(x.get("list") or "").lower(), str(x.get("note") or ""), x.get("expires_at"), by,
                          category=(str(x["category"]).lower() or None) if x.get("category") else None)
            out.append({"line": k, "plate": e["plate"], "list": e["list"], "ok": True})
        except (ValueError, TypeError) as ex:
            out.append({"line": k, "plate": str(x.get("plate") or "")[:20], "list": str(x.get("list") or "")[:10], "ok": False, "error": str(ex)})
    return out


# ---- the vehicle a listed plate belongs on (cloned-plate alarm)

TYPE_WORDS = (("motorcycle", ("motorcycle", "motorbike", "scooter")), ("bus", ("bus",)),
              ("truck", ("semi", "box truck", "dump", "tractor", "tanker", "flatbed", "tow truck", "wrecker")),
              ("van", ("van", "minivan")), ("pickup", ("pickup", "truck")), ("suv", ("suv", "crossover")),
              ("car", ("car", "sedan", "hatchback", "coupe", "wagon", "convertible")))
COLOR_WORDS = (("gray", ("gray", "grey", "silver", "charcoal", "gunmetal")), ("white", ("white", "pearl")),
               ("black", ("black",)), ("red", ("red", "maroon", "burgundy")), ("blue", ("blue", "navy")),
               ("green", ("green",)), ("brown", ("brown", "tan", "beige", "bronze", "gold")),
               ("yellow", ("yellow",)), ("orange", ("orange",)), ("purple", ("purple",)))
MAKE_SAME = {"chevy": "chevrolet", "ram": "dodge", "vw": "volkswagen", "mercedes-benz": "mercedes", "benz": "mercedes"}


def _class(text: str | None, table) -> str | None:
    s = f" {(text or '').lower()} "
    for name, words in table:
        if any(re.search(rf"\b{re.escape(w)}\b", s) for w in words):
            return name
    return None


def vehicle_mismatch(expect: dict | None, seen: dict | None) -> list[str]:
    """Why the vehicle seen isn't the one expected: a different kind of vehicle, or a different make and colour (one
    alone can be a misreading: Otso names makes from a back view, and colours shift at night). [] = it fits, or too
    little is known to say."""
    if not expect or not seen:
        return []
    out = []
    te, ts = _class(expect.get("vehicle_type"), TYPE_WORDS), _class(seen.get("vehicle_type"), TYPE_WORDS)
    if te and ts and te != ts:
        out.append(f"a {ts} instead of a {te}")
    me = MAKE_SAME.get((expect.get("make") or "").lower(), (expect.get("make") or "").lower())
    ms = MAKE_SAME.get((seen.get("make") or "").lower(), (seen.get("make") or "").lower())
    ce, cs = _class(expect.get("color"), COLOR_WORDS), _class(seen.get("color"), COLOR_WORDS)
    if me and ms and me != ms and ce and cs and ce != cs:
        out.append(f"a {cs} {seen.get('make')} instead of a {ce} {expect.get('make')}")
    return out


def expected_entries(plate_: str | None, now: float | None = None) -> list[dict]:
    """The entries (not expired) for exactly this plate that say which vehicle it belongs on."""
    if not plate_:
        return []
    return [e for e in list_entries(now) if e["plate"] == plate_ and e.get("expect")]


# ---------------------------------------------------------------- storing a read

def media_root() -> Path:
    return settings.data_dir / "traffic"


def _save_images(camera_id: str, p: dict, ts: float, kinds: list[str]) -> dict:
    day = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    d = media_root() / camera_id / day
    stem = f"{int(ts * 1000)}-{secrets.token_hex(3)}"
    out = {}
    for kind in kinds:
        b = jpeg(p.get(IMAGE_KINDS[kind]))
        if b is None:
            continue
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{stem}-{kind}.jpg"
        f.write_bytes(b)
        out[kind] = str(f.relative_to(settings.data_dir)).replace("\\", "/")
    return out


def conf_word(conf: float | None) -> str:
    return "high" if (conf or 0) >= 0.8 else "medium"


def camera(camera_id: str) -> dict | None:
    return next((c for c in db.cameras() if c["id"] == camera_id), None)


def check_secret(cam: dict | None, secret: str) -> bool:
    t = (cam or {}).get("traffic") or {}
    return bool(t.get("secret")) and hmac.compare_digest(str(t["secret"]), str(secret))


def set_traffic(camera_id: str, **changes) -> dict:
    cam = camera(camera_id) or {}
    t = {**(cam.get("traffic") or {}), **changes}
    db.execute("UPDATE cameras SET traffic=? WHERE id=?", [json.dumps(t), camera_id])
    return t


class Health:
    """When each camera last pushed (a read or a status report), in memory: a restart counts from the start."""

    def __init__(self) -> None:
        self.started = time.time()
        self.last: dict[str, dict] = {}

    def heard(self, camera_id: str, kind: str, now: float | None = None) -> None:
        h = self.last.setdefault(camera_id, {})
        h["push"] = h[kind] = now or time.time()

    def camera(self, camera_id: str) -> dict:
        return dict(self.last.get(camera_id) or {})

    def alerts(self, cams: list[dict] | None = None, now: float | None = None) -> list[dict]:
        now = now or time.time()
        silent = []
        for c in cams if cams is not None else db.cameras(enabled_only=True):
            t = c.get("traffic") or {}
            if not (c.get("enabled") and t.get("enabled")):
                continue
            since = self.last.get(c["id"], {}).get("push") or max(self.started, t.get("set_at") or 0)
            if now - since >= settings.traffic_silent_s:
                silent.append((c, since))
        if not silent:
            return []
        names = ", ".join(c["name"] for c, _ in silent[:3]) + (f" and {len(silent) - 3} more" if len(silent) > 3 else "")
        mins = round((now - min(s for _, s in silent)) / 60)
        return [{"kind": "traffic_push_silent", "since": min(s for _, s in silent), "count": len(silent),
                 "text": f"No plate reads or status from {names} for {mins} min: check the camera's network and its push "
                         "setting (Camera › Traffic)"}]


health = Health()


def ingest(cam: dict, p: dict, received: float | None = None) -> dict:
    """One push from `cam`: store the read, its crops, its pass, link it to an event, check the lists. Returns
    {read_id, event_id (an existing event it went on), alarm_id (an alarm event opened now), list}."""
    received = received or time.time()
    t = cam.get("traffic") or {}
    r = parse(p, received, t.get("zone_offset_s"), bool(t.get("resend")))
    if r["zone_offset"] is not None and r["zone_offset"] != t.get("zone_offset_s"):
        set_traffic(cam["id"], zone_offset_s=r["zone_offset"])
    if not r["clock_ok"]:
        log.warning("[%s] traffic read time %r doesn't match the receive time: using the receive time", cam["id"], p.get("time"))
    hit = match_list(r["plate"], received, cam["id"]) if (r["confidence"] or 0) >= settings.traffic_list_min_conf else None
    alarm = hit is not None and hit["effect"]["alarm"]
    kinds = ["plate", "vehicle", "violation"] + (["full"] if alarm or settings.traffic_keep_full else [])
    images = _save_images(cam["id"], p, r["ts"], kinds)
    read_id = db.execute_insert(
        "INSERT INTO vehicle_reads (camera_id, ts, received_at, kind, plate, plate_raw, confidence, region, plate_type, "
        "plate_color, vehicle_type, vehicle_color, vehicle_brand, direction, speed, alarm, detection_region, box, plate_box, "
        "images, list, list_match, list_entry, extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [cam["id"], r["ts"], received, r["kind"], r["plate"], r["plate_raw"], r["confidence"], r["region"], r["plate_type"],
         r["plate_color"], r["vehicle_type"], r["vehicle_color"], r["vehicle_brand"], r["direction"], r["speed"], r["alarm"],
         r["detection_region"], json.dumps(r["box"]) if r["box"] else None, json.dumps(r["plate_box"]) if r["plate_box"] else None,
         json.dumps(images), hit["list"] if hit else None, hit["match"] if hit else None, hit["id"] if hit else None,
         json.dumps(r["extra"])[:4000]])
    pass_id, best = passes.place(cam["id"], read_id, r["ts"], r["plate"], r["confidence"], r["box"],
                                 ((r["vehicle_type"] or "").lower() or None, (r["vehicle_color"] or "").lower() or None))
    db.execute("UPDATE vehicle_reads SET pass_id=?, best=? WHERE id=?", [pass_id, int(best), read_id])
    health.heard(cam["id"], "read", received)
    if "vehicle" not in images and not is_noise(r):
        from_recording.add(read_id, received + REC_DELAY_S)
    if r["plate"] and expected_entries(r["plate"], received):
        looker.urgent(read_id, received)   # Otso checks it is the vehicle the plate belongs on (cloned-plate alarm)
    out = {"read_id": read_id, "event_id": None, "alarm_id": None, "list": hit["list"] if hit else None}
    if alarm:
        out["alarm_id"] = open_alarm(cam, read_id, hit)
        return out
    if r["plate"] and (r["confidence"] or 0) >= settings.traffic_attach_min_conf:
        out["event_id"] = attach_read(read_id)
    if r["plate"] and not (hit and hit["list"] == "allow"):   # an allowed plate raises nothing
        try:
            out["alarm_id"] = check_patterns(cam, read_id)
        except Exception:  # noqa: BLE001 - the read is stored; a pattern check must not fail the camera's push
            log.exception("[%s] traffic read %s: pattern check failed", cam["id"], read_id)
    return out


def read(read_id: int) -> dict | None:
    r = db.one("SELECT * FROM vehicle_reads WHERE id=?", [read_id])
    return public(r) if r else None


def public(r: dict) -> dict:
    out = {k: r[k] for k in r if k not in ("box", "plate_box", "images", "extra", "otso")}
    out["otso"] = json.loads(r["otso"]) if r.get("otso") else None
    out["box"] = json.loads(r["box"]) if r.get("box") else None
    out["plate_box"] = json.loads(r["plate_box"]) if r.get("plate_box") else None
    imgs = json.loads(r["images"]) if r.get("images") else {}
    out["images"] = sorted(imgs.keys())
    out["vehicle_source"] = ("recording" if imgs["vehicle"].endswith("-rec.jpg") else "camera") if imgs.get("vehicle") else None
    out["best"] = bool(r.get("best"))
    return out


def image_path(read_id: int, kind: str) -> Path | None:
    r = db.one("SELECT images FROM vehicle_reads WHERE id=?", [read_id])
    rel = (json.loads(r["images"]) if r and r["images"] else {}).get(kind)
    f = settings.data_dir / rel if rel else None
    return f if f is not None and f.is_file() else None


read_limiter = plate.Limiter()   # Read plate on a read: its own limiter (plate.limiter is keyed by event id)


def recording_segment(camera_id: str, t: float) -> float | None:
    """Start of the recording segment holding t, or None when nothing is recorded there. Each segment's start comes from
    the wall clock when MediaMTX opened it, so where the camera's reads sit in it differs from segment to segment (cam8,
    2026-10-28: +1.5 s in the 18:03:56 segment, -0.5 s in every one from 18:23:56 on, -0.4 s earlier that day)."""
    from . import frames
    seg = frames.find_segment(camera_id, t)
    return seg[1] if seg else None


class Aligner:
    """Per camera and recording segment: where the camera's reads sit in it (seconds to add to a read's time), learned
    from reads whose plate crop matched the recording strongly. One strong match can still be wrong (cam8: a 0.89 at
    +4.75 s while the segment's true offset was +1.5 s), so the offset is the median of the largest group of matches
    agreeing within AGREE_S, once it has AGREE of them."""

    def __init__(self) -> None:
        self.found: dict[tuple[str, float], deque] = {}
        self.lock = threading.Lock()

    def offset(self, camera_id: str, seg: float) -> float | None:
        with self.lock:
            q = sorted(self.found.get((camera_id, seg)) or [])
        group = max(([d for d in q if abs(d - c) <= AGREE_S] for c in q), key=len, default=[])
        if len(group) >= AGREE:
            return float(group[len(group) // 2])
        row = db.one("SELECT offset_s FROM recording_offsets WHERE camera_id=? AND segment=?", [camera_id, seg])
        return float(row["offset_s"]) if row else None   # learned before a restart

    def near(self, camera_id: str, seg: float) -> float | None:
        """The offset learned for the segment nearest this one (starting within PRIOR_MAX_S of it): where to look first
        in a segment not learned yet."""
        row = db.one("SELECT offset_s FROM recording_offsets WHERE camera_id=? AND ABS(segment - ?) <= ? "
                     "ORDER BY ABS(segment - ?) LIMIT 1", [camera_id, seg, PRIOR_MAX_S, seg])
        return float(row["offset_s"]) if row else None

    def learn(self, camera_id: str, seg: float, d: float) -> None:
        with self.lock:
            self.found.setdefault((camera_id, seg), deque(maxlen=15)).append(d)
            if len(self.found) > 500:   # old segments
                for k in sorted(self.found, key=lambda k: k[1])[:250]:
                    del self.found[k]
        known = self.offset(camera_id, seg)
        if known is not None and len(self.found.get((camera_id, seg)) or []) >= AGREE:
            db.execute("INSERT INTO recording_offsets (camera_id, segment, offset_s, learned_at) VALUES (?,?,?,?) "
                       "ON CONFLICT(camera_id, segment) DO UPDATE SET offset_s=excluded.offset_s, learned_at=excluded.learned_at",
                       [camera_id, seg, known, time.time()])


aligner = Aligner()


def plate_match(frame, plate_img, plate_box: list[float]) -> float:
    """How well the camera's plate crop matches the recorded frame around the plate's box (normalized cross-
    correlation of the crop scaled to the box, searched inside the box padded by 60 %: a moving plate is near it)."""
    import cv2
    import numpy as np
    h, w = frame.shape[:2]
    l, t, r, b = plate_box
    pw, ph = (r - l) * 0.6, (b - t) * 0.6
    reg = frame[max(0, int((t - ph) * h)):int((b + ph) * h), max(0, int((l - pw) * w)):int((r + pw) * w)]
    tw, th = int((r - l) * w), int((b - t) * h)
    if reg.size == 0 or tw < 4 or th < 4 or reg.shape[0] < th or reg.shape[1] < tw:
        return -1.0
    gray = cv2.cvtColor(reg, cv2.COLOR_BGR2GRAY).astype(np.float32)
    tpl = cv2.resize(plate_img, (tw, th)).astype(np.float32)
    return float(cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED).max())


def find_plate(camera_id: str, ts: float, offsets: list[float], pc, plate_box: list[float]):
    """The recorded frame (of ts + offsets, every frame) whose plate region matches the plate crop `pc` best, if that
    is a strong match at one moment: (offset, frame), else None. Frames are matched as they are decoded and only the
    best is kept (a 4K frame is ~25 MB)."""
    from . import frames
    scores: list[tuple[float, float]] = []
    best = (-1.0, 0.0, None)
    for t, frame in frames.iter_recording(camera_id, [ts + x for x in offsets], tol=FRAME_TOL):
        s, d = plate_match(frame, pc, plate_box), round(t - ts, 2)
        scores.append((s, d))
        if s > best[0]:
            best = (s, d, frame)
    if best[0] < STRONG_MATCH:
        return None
    strong = [d for s, d in scores if s >= STRONG_MATCH]
    median = sorted(s for s, _ in scores)[len(scores) // 2]
    if max(strong) - min(strong) > PEAK_SPAN_S or best[0] - median < PEAK_GAP:
        return None   # matches over a stretch of time: something still (the sign at night, a parked car)
    return best[1], best[2]


def cut_from_recording(read_id: int) -> bool | None:
    """The vehicle picture of a read the camera sent without one, cut from the recorded frame showing it (the camera's
    box, padded as plate.crop does). The frame: the one whose plate region matches the read's plate crop strongly
    (find_plate: NARROW around its segment's learned offset; until that is known, PRIOR_S around the nearest learned
    one, then WIDE), else the frame at the segment's learned offset. True: the read has a picture now. None: not yet
    (the recording isn't written, or the segment's offset isn't known): try again later. False: nothing is recorded
    there."""
    from . import frames
    import cv2
    r = db.one("SELECT * FROM vehicle_reads WHERE id=?", [read_id])
    if not r:
        return True
    images = json.loads(r["images"]) if r["images"] else {}
    if images.get("vehicle"):
        return True
    cam, ts = r["camera_id"], r["ts"]
    seg = recording_segment(cam, ts)
    if seg is None:
        return False
    learned = aligner.offset(cam, seg)
    fresh = time.time() - r["received_at"] <= FRESH_S
    frame, d = None, learned
    pf = settings.data_dir / images["plate"] if images.get("plate") else None
    pc = cv2.imread(str(pf), cv2.IMREAD_GRAYSCALE) if pf is not None and pf.is_file() and r["plate_box"] else None
    if pc is not None:
        if learned is not None:
            searches = [window(learned, NARROW_S)]
        else:
            prior = aligner.near(cam, seg)
            # an older read: never wide (a wide search reads ~20 s of video back from the disk)
            searches = ([window(prior, PRIOR_S)] if prior is not None else []) + ([WIDE] if fresh else [])
        for offsets in searches:
            if (found := find_plate(cam, ts, offsets, pc, json.loads(r["plate_box"]))) is not None:
                d, frame = found
                if r["vehicle_type"]:
                    aligner.learn(cam, seg, d)
                break
    if frame is None:
        if d is None:
            return None
        frame = frames.recorded_frame(cam, ts + d)
        if frame is None:
            return None
    box = json.loads(r["box"]) if r["box"] else [0.0, 0.0, 1.0, 1.0]
    ok, buf = cv2.imencode(".jpg", plate.crop(frame, box), [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        return None
    day = media_root() / cam / dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    f = day / f"{int(ts * 1000)}-{secrets.token_hex(3)}-vehicle-rec.jpg"
    f.write_bytes(buf.tobytes())
    images["vehicle"] = str(f.relative_to(settings.data_dir)).replace("\\", "/")
    db.execute("UPDATE vehicle_reads SET images=? WHERE id=?", [json.dumps(images), read_id])
    return True


class FromRecording:
    """Reads waiting for their vehicle picture from the recording: (due time, read id, tries), each once; cut one at a
    time on CUT_POOL."""

    def __init__(self) -> None:
        self.due: deque = deque()
        self.queued: set[int] = set()
        self.lock = threading.Lock()

    def add(self, read_id: int, at: float, tries: int = 0) -> bool:
        """Queue a read (False: already queued, or the queue is full)."""
        with self.lock:
            if read_id in self.queued or len(self.due) >= 5000:
                return False
            self.queued.add(read_id)
            self.due.append((at, read_id, tries))
            return True

    def take(self, now: float) -> list[tuple[float, int, int]]:
        """The reads due now, newest first: a fresh read may still be searched wide, an older one waits less well
        (TSS-DT 2026-10-29: behind 253 reads queued at start-up the morning's new reads went stale)."""
        with self.lock:
            ready = sorted((x for x in self.due if x[0] <= now), key=lambda x: -x[1])
            self.due = deque(x for x in self.due if x[0] > now)
            self.queued -= {x[1] for x in ready}
            return ready

    def backfill(self, now: float | None = None) -> int:
        """Queue the recent reads still without a vehicle picture (not the ones that aren't a vehicle)."""
        now = now or time.time()
        sql, p = noise_sql()
        rows = db.all(f"SELECT id FROM vehicle_reads WHERE ts >= ? AND images NOT LIKE '%\"vehicle\"%' AND NOT {sql} "
                      "ORDER BY id DESC LIMIT 2000", [now - BACKFILL_DAYS * 86400, *p])
        n = sum(self.add(r["id"], now, BACKFILL_TRIES) for r in rows)
        if n:
            log.info("traffic: %d recent reads queued for a vehicle picture from the recording", n)
        return n

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(CUT_POOL, self.backfill)
        except Exception:  # noqa: BLE001 - new reads are still queued as they come
            log.exception("traffic: queueing older reads for pictures failed")
        while True:
            for _at, read_id, tries in self.take(time.time()):
                r = db.one("SELECT received_at FROM vehicle_reads WHERE id=?", [read_id])
                started = time.monotonic()
                try:
                    done = await loop.run_in_executor(CUT_POOL, cut_from_recording, read_id)
                except Exception:  # noqa: BLE001 - one read's picture must not stop the rest
                    log.exception("traffic: vehicle picture from the recording for read %s failed", read_id)
                    done = True
                if r and time.time() - r["received_at"] > FRESH_S:
                    # read back from the recording disk: gently, idle OLD_DUTY times as long as the cut read (a read
                    # with nothing to search near costs nothing and waits nothing)
                    await asyncio.sleep(min(OLD_GAP_S, OLD_DUTY * (time.monotonic() - started)))
                if done is None and tries + 1 < REC_TRIES:
                    self.add(read_id, time.time() + REC_RETRY_S, tries + 1)
            await asyncio.sleep(1)


from_recording = FromRecording()


PLATE_LOOK_PX = 640   # the camera's plate crop enlarged to this width for Otso (it arrives ~60-130 px wide)


def plate_look(jpeg_path: Path) -> bytes | None:
    """The camera's plate crop, enlarged (cubic) with a margin, for Otso: in the vehicle picture the plate is a few dozen
    pixels after Otso's own scaling (cam8 2026-10-28: "51121" plain in the camera's crop, "not legible" from the vehicle)."""
    import cv2
    img = cv2.imread(str(jpeg_path))
    if img is None or img.shape[1] < 8:
        return None
    s = PLATE_LOOK_PX / img.shape[1]
    big = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
    big = cv2.copyMakeBorder(big, 24, 24, 24, 24, cv2.BORDER_REPLICATE)
    ok, buf = cv2.imencode(".jpg", big, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return buf.tobytes() if ok else None


def otso_crops(read_id: int) -> list:
    """What Otso looks at, best first: the camera's own plate crop enlarged (asked as a plate close-up), then the
    vehicle (the camera's picture or the recording's), then the full frame when kept."""
    out: list = []
    f = image_path(read_id, "plate")
    if f is not None and (look := plate_look(f)) is not None:
        out.append((look, plate.PLATE_QUESTION))
    for kind in ("vehicle", "full"):
        f = image_path(read_id, kind)
        if f is not None:
            out.append(f.read_bytes())
    return out


def store_otso(read_id: int, result: dict, notes: dict | None = None, by: str = "person") -> dict | None:
    """Keep Otso's answer on the read (a read plate, or why not), beside the camera's own. Its state only when it is
    sure: on a soft crop it named a wrong one every time (plate.ON_REQUEST_SYSTEM). by: "person" (Ask Otso) or "otso"
    (Looker); a person's answer keeps the vehicle Otso described on its own."""
    old = db.one("SELECT otso, otso_look FROM vehicle_reads WHERE id=?", [read_id]) or {}
    prev = json.loads(old["otso"]) if old.get("otso") else {}
    info = {"at": round(time.time(), 1), "by": by,
            **{k: result[k] for k in ("result", "plate", "plate_raw", "region", "confidence", "reason", "model", "crops")
               if result.get(k) not in (None, "")}}
    if info.get("confidence") != "high":
        info.pop("region", None)
    vehicle = result.get("vehicle") or prev.get("vehicle")
    if vehicle:
        info["vehicle"] = vehicle
    if notes is not None:
        info["about"], info["notes"] = notes["plate"], notes["notes"]
    db.execute("UPDATE vehicle_reads SET otso=?, otso_plate=?, otso_look=? WHERE id=?",
               [json.dumps(info), result.get("plate") if result.get("result") == "read" else None,
                plate.vehicle_words(vehicle) or old.get("otso_look"), read_id])
    return read(read_id)


# ---------------------------------------------------------------- Otso looks on its own

LOOK_WAIT_S = 5.0        # between checks while there is nothing to look at, Qwen is busy or the hour's looks are used
LOOK_TIMEOUT_S = 60.0
URGENT_S = 900.0         # a listed plate's vehicle waits this long for its picture (REC_TRIES x REC_RETRY_S)


class Looker:
    """Otso looks at each vehicle's best read once (see "Otso looks on its own" above). In memory: when it looked, for
    the hourly limit (a restart starts the hour again)."""

    def __init__(self) -> None:
        self.looked: deque = deque()
        self.wanted: dict[int, float] = {}   # read id -> until when: a listed plate's vehicle to check now (urgent)
        self.lock = threading.Lock()

    def urgent(self, read_id: int, now: float | None = None) -> None:
        """Look at this read as soon as Otso is ready, outside the hourly limit and the AI schedule: its plate is on a
        list that says which vehicle it belongs on (a person set that up, so it is checked like a person's request).
        Waits up to URGENT_S for the vehicle picture (cut from the recording when the camera sent none)."""
        with self.lock:
            self.wanted[read_id] = (now or time.time()) + URGENT_S

    def next_urgent(self, now: float) -> int | None:
        with self.lock:
            for rid, until in sorted(self.wanted.items()):
                if until < now:
                    self.wanted.pop(rid, None)
                elif image_path(rid, "vehicle") is not None:
                    self.wanted.pop(rid, None)
                    return rid
        return None

    def room(self, now: float) -> bool:
        while self.looked and self.looked[0] < now - 3600:
            self.looked.popleft()
        return len(self.looked) < settings.traffic_look_per_hour

    def next_read(self, now: float) -> int | None:
        """The newest best read with a vehicle picture that no read of its pass has had Otso look at."""
        sql, p = noise_sql()
        r = db.one(f"SELECT id FROM vehicle_reads v WHERE best=1 AND otso IS NULL AND ts >= ? AND images LIKE '%\"vehicle\"%' "
                   f"AND NOT {sql} AND NOT EXISTS (SELECT 1 FROM vehicle_reads o WHERE o.pass_id = v.pass_id "
                   f"AND o.otso IS NOT NULL) ORDER BY id DESC LIMIT 1", [now - settings.traffic_look_max_age_s, *p])
        return r["id"] if r else None

    async def look(self, read_id: int) -> dict:
        """One look: the plate (kept only when Otso is sure) and the vehicle. Returns what was stored, with `alarm` (a list
        hit's event id) when Otso's plate is on the deny or watch list and the camera had read no plate."""
        r = db.one("SELECT * FROM vehicle_reads WHERE id=?", [read_id])
        vehicle_f = image_path(read_id, "vehicle")
        if not r or vehicle_f is None:
            return {"result": "skipped"}
        plate_f = image_path(read_id, "plate")
        plate_img = await asyncio.to_thread(plate_look, plate_f) if plate_f is not None else None
        try:
            with aischedule.job(plate.KIND):
                result = await asyncio.wait_for(plate.look(plate_img, vehicle_f.read_bytes()), LOOK_TIMEOUT_S)
        except Exception as ex:  # noqa: BLE001 - marked as tried: one bad read must not hold up the rest
            log.warning("traffic read %s: Otso's look failed: %s", read_id, ex)
            result = {"result": "skipped", "reason": str(ex)[:120] or type(ex).__name__}
        if result.get("result") == "read" and result.get("confidence") != "high":   # nobody checks a background guess
            result = {**{k: v for k, v in result.items() if k not in ("plate", "plate_raw", "region", "confidence")},
                      "result": "unreadable", "reason": "not sure of every character"}
        await asyncio.to_thread(store_otso, read_id, result, None, "otso")
        out = {**result, "alarm": None}
        cam = camera(r["camera_id"])
        if cam and result.get("vehicle"):
            out["mismatch"] = await asyncio.to_thread(check_expected, cam, read_id, result["vehicle"])
        if result.get("result") == "read" and not r["plate"]:
            hit = await asyncio.to_thread(match_list, result["plate"], None, r["camera_id"])
            if hit and hit["effect"]["alarm"] and cam:
                db.execute("UPDATE vehicle_reads SET list=?, list_match=?, list_entry=? WHERE id=?",
                           [hit["list"], hit["match"], hit["id"], read_id])
                out["alarm"] = await asyncio.to_thread(open_alarm, cam, read_id, hit, result["plate"])
        return out

    async def ready(self, pipeline, now: float) -> bool:
        """May Otso look now: switched on, looks left this hour, the Site's AI schedule allows catch-up work (inside
        its hours, or outside them unless it skips descriptions) and Qwen is free here (pipeline.catchup_ready)."""
        if not settings.traffic_look or not self.room(now):
            return False
        if not (aischedule.full() or aischedule.outside() == "catchup"):
            return False
        return await pipeline.catchup_ready()

    async def run(self, pipeline) -> None:
        """Forever: look at the next vehicle whenever ready(). A new alarm is published and described like a camera
        read's."""
        while True:
            await asyncio.sleep(LOOK_WAIT_S if not self.wanted else 1)
            try:
                now = time.time()
                read_id = await asyncio.to_thread(self.next_urgent, now) if self.wanted and pipeline.vlm_ready else None
                if read_id is None:
                    if not await self.ready(pipeline, now):
                        continue
                    read_id = await asyncio.to_thread(self.next_read, now)
                    if read_id is None:
                        continue
                    self.looked.append(now)
                out = await self.look(read_id)
                log.debug("traffic read %s: Otso looked: %s %s", read_id, out.get("plate") or out.get("result"),
                          plate.vehicle_words(out.get("vehicle")))
                for eid in (out.get("alarm"), out.get("mismatch")):
                    if eid:
                        pipeline.publish(eid)
                        if not pipeline.plan_synopsis(eid):
                            await pipeline.reindex(eid)
            except Exception:  # noqa: BLE001 - keep looking
                log.exception("traffic: Otso's look failed")


looker = Looker()


# ---------------------------------------------------------------- a plate's history (Ask Otso's notes)
# Counted from the reads, worded here, never by the model (a model asked for counts makes them up). One sighting = one
# pass. Reads are kept traffic_keep_days; when they go (prune) their sightings are kept per plate and day in plate_days
# (time of day, direction, vehicle type: no pictures) for traffic_history_days, so "this year" can be counted.

HABIT_GAP_MIN = 60       # sightings further apart in the day than this are different habits (morning, evening)
HABIT_SHARE = 0.25       # ...and a habit is at least this share of the sightings, on HABIT_DAYS different days at least
HABIT_DAYS = 2           # (a car circling one afternoon is no habit); "on weekdays" takes WEEKDAY_DAYS
WEEKDAY_DAYS = 5
RATE_DAYS = 56           # "about 5 times a week" over the last 8 weeks: how it is now, not since it was first seen
DIRECTION_WORDS = {"away": "heading away", "approach": "coming toward the camera"}


def _pass_sightings(rows: list[dict]) -> list[dict]:
    """Reads (oldest first) -> one sighting per pass: its first time, camera, most reported direction, vehicle type."""
    by: dict[int, dict] = {}
    for r in rows:
        if is_noise(r):
            continue
        s = by.setdefault(r["pass_id"] or r["id"], {"ts": r["ts"], "camera_id": r["camera_id"], "dirs": [], "vehicle": None})
        if r["direction"]:
            s["dirs"].append(r["direction"])
        s["vehicle"] = s["vehicle"] or r["vehicle_type"]
    out = []
    for s in by.values():
        d = s.pop("dirs")
        s["direction"] = max(set(d), key=d.count) if d else None
        out.append(s)
    return out


def sightings(plate_norm: str) -> list[dict]:
    """Every sighting of a plate (the camera's read or Otso's), oldest first: kept reads, then older days' summaries."""
    rows = db.all("SELECT id, pass_id, ts, camera_id, direction, vehicle_type, plate, confidence FROM vehicle_reads "
                  "WHERE plate=? OR otso_plate=? ORDER BY ts", [plate_norm, plate_norm])
    out = _pass_sightings(rows)
    for d in db.all("SELECT camera_id, day, sightings FROM plate_days WHERE plate=?", [plate_norm]):
        midnight = dt.datetime.strptime(d["day"], "%Y-%m-%d").timestamp()
        for minute, direction, vehicle in json.loads(d["sightings"]):
            out.append({"ts": midnight + minute * 60, "camera_id": d["camera_id"], "direction": direction, "vehicle": vehicle})
    return sorted(out, key=lambda s: s["ts"])


def roll_up(cutoff: float) -> int:
    """Before reads older than cutoff go: their sightings, per camera, plate and day, into plate_days. Returns how many."""
    rows = db.all("SELECT id, pass_id, ts, camera_id, direction, vehicle_type, plate, otso_plate, confidence FROM vehicle_reads "
                  "WHERE ts < ? AND (plate IS NOT NULL OR otso_plate IS NOT NULL) ORDER BY ts", [cutoff])
    passes: dict[int, list] = {}
    for r in rows:
        passes.setdefault(r["pass_id"] or r["id"], []).append(r)
    days: dict[tuple, list] = {}
    for reads in passes.values():
        s = _pass_sightings(reads)
        if not s:
            continue
        t = dt.datetime.fromtimestamp(s[0]["ts"])
        for p in {p for r in reads for p in (r["plate"], r["otso_plate"]) if p}:   # a pass read 671AED and 3671AED: both
            days.setdefault((s[0]["camera_id"], p, t.strftime("%Y-%m-%d")), []).append(
                [t.hour * 60 + t.minute, s[0]["direction"], s[0]["vehicle"]])
    for (cam, p, day), new in days.items():
        old = db.one("SELECT sightings FROM plate_days WHERE camera_id=? AND plate=? AND day=?", [cam, p, day])
        db.execute("INSERT INTO plate_days (camera_id, plate, day, sightings) VALUES (?,?,?,?) "
                   "ON CONFLICT(camera_id, plate, day) DO UPDATE SET sightings=excluded.sightings",
                   [cam, p, day, json.dumps((json.loads(old["sightings"]) if old else []) + new)])
    return sum(len(v) for v in days.values())


def clock(minute: float) -> str:
    h, m = divmod(int(round(minute)), 60)
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h % 24 < 12 else 'PM'}"


def times(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def clock_span(lo: float, hi: float) -> str:
    """"8:04-8:30 AM", "11:50 AM-12:10 PM"."""
    a, b = clock(lo), clock(hi)
    return f"{a[:-3]}-{b}" if a[-2:] == b[-2:] else f"{a}-{b}"


def day_text(d: dt.date, today: dt.date) -> str:
    if d == today:
        return "today"
    if d == today - dt.timedelta(days=1):
        return "yesterday"
    return f"{d:%b} {d.day}" + (f", {d.year}" if d.year != today.year else "")


def habits(seen: list[dict]) -> list[dict]:
    """Times of day a plate keeps coming back at: groups of sightings within HABIT_GAP_MIN of each other holding
    HABIT_SHARE of them; each with its middle time, range, weekday share and the direction most of them went."""
    when = [(dt.datetime.fromtimestamp(s["ts"]), s) for s in seen]
    mins = sorted(((t.hour * 60 + t.minute, t.weekday(), s) for t, s in when), key=lambda x: x[0])
    groups: list[list] = []
    for m in mins:
        if groups and m[0] - groups[-1][-1][0] <= HABIT_GAP_MIN:
            groups[-1].append(m)
        else:
            groups.append([m])
    out = []
    for g in groups:
        days = {dt.date.fromtimestamp(x[2]["ts"]) for x in g}
        if len(g) < max(2, HABIT_SHARE * len(seen)) or len(days) < HABIT_DAYS:
            continue
        d = [x[2]["direction"] for x in g if x[2]["direction"]]
        top = max(set(d), key=d.count) if d else None
        out.append({"n": len(g), "days": len(days), "mid": g[len(g) // 2][0], "lo": g[0][0], "hi": g[-1][0],
                    "weekdays_only": all(x.weekday() < 5 for x in days), "weekends_only": all(x.weekday() >= 5 for x in days),
                    "direction": top if top and d.count(top) >= 0.75 * len(g) else None})
    return out


def plate_notes(plate_norm: str, this_ts: float, now: float | None = None) -> dict:
    """Ask Otso's notes on a plate: how often, when and which way it is usually seen, when last, on which vehicles."""
    now = now or time.time()
    today = dt.date.fromtimestamp(now)
    seen = sightings(plate_norm)
    notes: list[str] = []
    if len(seen) <= 1:
        return {"plate": plate_norm, "notes": ["The first time this plate has been seen here."], "sightings": len(seen)}
    first = dt.date.fromtimestamp(seen[0]["ts"])
    this_year = sum(dt.date.fromtimestamp(s["ts"]).year == today.year for s in seen)
    notes.append(f"Seen {len(seen)} times today." if first == today else
                 f"Seen {len(seen)} times since {day_text(first, today)}"
                 + (f", {this_year} of them this year." if first.year < today.year else "."))
    names = {c["id"]: c["name"] for c in db.all("SELECT id, name FROM cameras")}
    before = [s for s in seen if s["ts"] < this_ts - PASS_GAP_S]
    cams = {s["camera_id"] for s in seen}
    if before:
        s = before[-1]
        t = dt.datetime.fromtimestamp(s["ts"])
        notes.append(f"Before this, last seen {day_text(t.date(), today)} at {clock(t.hour * 60 + t.minute)}"
                     + (f" on {names.get(s['camera_id'], s['camera_id'])}" if len(cams) > 1 else "")
                     + (f", {DIRECTION_WORDS[s['direction']]}" if s["direction"] in DIRECTION_WORDS else "") + ".")
    if len(seen) >= 3:
        hs = habits(seen)
        if not hs and len({dt.date.fromtimestamp(s["ts"]) for s in seen}) >= 3:
            notes.append("Seen at different times of day, no usual time.")
        for h in hs:
            when = ("on weekdays " if h["weekdays_only"] and h["days"] >= WEEKDAY_DAYS
                    else "at weekends " if h["weekends_only"] and h["days"] >= 3 else "")
            span = f" ({clock_span(h['lo'], h['hi'])})" if h["hi"] - h["lo"] >= 5 else ""
            way = f", {DIRECTION_WORDS[h['direction']]}" if h["direction"] in DIRECTION_WORDS else ""
            notes.append(f"Usually {when}around {clock(round(h['mid'] / 5) * 5)}{span}{way}: {h['n']} of {len(seen)} times, "
                         f"on {h['days']} days.")
    # how often lately: over the last RATE_DAYS (or since first seen, when that is sooner)
    span_days = min((today - first).days + 1, RATE_DAYS)   # calendar days, today counted
    recent = [s for s in seen if (today - dt.date.fromtimestamp(s["ts"])).days < span_days]
    days_seen = len({dt.date.fromtimestamp(s["ts"]) for s in recent})
    over = f"over the last {span_days // 7} weeks" if span_days == RATE_DAYS else f"since {day_text(first, today)}"
    if span_days >= 14:
        per_week = round(len(recent) / (span_days / 7))
        per_month = round(len(recent) / (span_days / 30))
        notes.append(f"About {times(per_week)} a week {over}, on {days_seen} of {span_days} days." if per_week >= 1
                     else f"About {times(per_month)} a month {over}." if per_month >= 1 else f"Seen {times(len(recent))} {over}.")
    elif span_days >= 2:
        notes.append(f"On {days_seen} of the last {span_days} days.")
    types: dict[str, int] = {}
    for s in seen:
        if s["vehicle"]:
            types[s["vehicle"]] = types.get(s["vehicle"], 0) + 1
    shown = {k: v for k, v in types.items() if v >= 2 and v >= 0.2 * len(seen)}
    if len(shown) > 1:
        notes.append("Seen on different kinds of vehicle (" + ", ".join(f"{k} {v} times" for k, v in sorted(shown.items(), key=lambda x: -x[1]))
                     + "): the plate may be misread, or on more than one vehicle.")
    if len(cams) > 1:
        per = {c: sum(s["camera_id"] == c for s in seen) for c in cams}
        notes.append("By camera: " + ", ".join(f"{names.get(c, c)} {n}" for c, n in sorted(per.items(), key=lambda x: -x[1])) + ".")
    return {"plate": plate_norm, "notes": notes, "sightings": len(seen)}


# ---------------------------------------------------------------- events

def _event_plate_ok(e: dict, conf: float) -> bool:
    """A camera read may set the event's plate unless a person read it, or a better camera read already did."""
    info = (e.get("detections") or {}).get("plate_read") or {}
    if info.get("source") == "user" and e.get("plate"):
        return False
    if info.get("source") == "camera" and e.get("plate") and (info.get("confidence") or 0) >= conf:
        return False
    return True


def _put_plate(e: dict, r: dict) -> None:
    det = {**(e.get("detections") or {}), "plate_read": {"at": round(time.time(), 1), "source": "camera", "result": "read",
                                                          "read_id": r["id"], "confidence": r["confidence"]}}
    db.update_event(e["id"], plate=r["plate"], plate_raw=r["plate_raw"], plate_region=r["region"],
                    plate_conf=conf_word(r["confidence"]), detections=det)


def attach_read(read_id: int) -> int | None:
    """Put a confident read's plate on the vehicle event of its camera at that moment. Returns the event id."""
    r = db.one("SELECT * FROM vehicle_reads WHERE id=?", [read_id])
    if not r or not r["plate"] or (r["confidence"] or 0) < settings.traffic_attach_min_conf:
        return None
    e = db.one("SELECT id FROM events WHERE camera_id=? AND camera_class='vehicle' AND status IN ('open','pending','verified') "
               "AND start_ts <= ? AND COALESCE(end_ts, ?) >= ? ORDER BY ABS(start_ts - ?) LIMIT 1",
               [r["camera_id"], r["ts"] + 3, r["ts"], r["ts"] - 3, r["ts"]])
    if not e:
        return None
    ev = db.event(e["id"])
    if ev and _event_plate_ok(ev, r["confidence"]):
        _put_plate(ev, r)
    db.execute("UPDATE vehicle_reads SET event_id=? WHERE pass_id=?", [e["id"], r["pass_id"] or read_id])
    return e["id"]


def attach_to_event(event_id: int) -> dict | None:
    """A vehicle event of a traffic camera (verified now): the best confident read inside its span gives its plate."""
    e = db.event(event_id)
    if not e or e["camera_class"] != "vehicle":
        return None
    r = db.one("SELECT * FROM vehicle_reads WHERE camera_id=? AND ts BETWEEN ? AND ? AND plate IS NOT NULL AND confidence >= ? "
               "ORDER BY confidence DESC LIMIT 1",
               [e["camera_id"], e["start_ts"] - 3, (e["end_ts"] or e["start_ts"]) + 3, settings.traffic_attach_min_conf])
    if not r:
        return None
    if _event_plate_ok(e, r["confidence"]):
        _put_plate(e, r)
    db.execute("UPDATE vehicle_reads SET event_id=? WHERE pass_id=? AND event_id IS NULL", [event_id, r["pass_id"] or r["id"]])
    return db.event(event_id)


def reads_plates(camera_id: str) -> bool:
    """Plates on this camera come from the camera itself (set up and pushing): Otso doesn't read them."""
    c = camera(camera_id)
    return bool(c and (c.get("traffic") or {}).get("enabled"))


def _plate_event(cam: dict, r: dict, policy: dict, topic: str, rule: str, plate_: str, by_otso: bool = False) -> int:
    """An alarm event for a read (verified at once: the camera saw the vehicle) carrying the plate, the read's picture and
    the site rule `policy`, so it reaches the hub's alerts and the SOC like any broken rule."""
    from . import baseline, verifier
    box = json.loads(r["box"]) if r["box"] else [0.0, 0.0, 1.0, 1.0]
    conf = (r["confidence"] or 0.0) if not by_otso else 0.9
    otso = json.loads(r["otso"]) if by_otso and r.get("otso") else {}
    eid = db.create_event(
        camera_id=cam["id"], track_id=f"lpr:{plate_}-{int(r['ts'] * 1000)}", camera_class="vehicle", camera_conf=conf,
        start_ts=r["ts"] - 2, end_ts=r["ts"] + 2, path=[[round(r["ts"] - 2, 3), *box, conf], [round(r["ts"] + 2, 3), *box, conf]],
        rules=[{"ts": round(r["ts"], 3), "topic": topic, "rule": rule}], status="verified",
        yolo_class="vehicle", yolo_hits=0, policy=policy, plate=plate_,
        plate_raw=otso.get("plate_raw") if by_otso else r["plate_raw"], plate_region=otso.get("region") if by_otso else r["region"],
        plate_conf="high" if by_otso else conf_word(conf),
        detections={"source": "lpr", "read_id": r["id"], "plate_read": {
            "at": round(time.time(), 1), "source": "otso" if by_otso else "camera", "result": "read", "read_id": r["id"],
            "confidence": "high" if by_otso else conf}})
    images = json.loads(r["images"]) if r["images"] else {}
    src = images.get("full") or images.get("vehicle")
    if src and (settings.data_dir / src).is_file():
        snap = verifier.event_dir(eid) / "snapshot.jpg"
        shutil.copyfile(settings.data_dir / src, snap)
        db.update_event(eid, snapshot=str(snap.relative_to(settings.data_dir)).replace("\\", "/"))
    db.update_event(eid, priority=baseline.priority(db.event(eid), 0.0))
    db.execute("UPDATE vehicle_reads SET event_id=? WHERE id=?", [eid, r["id"]])
    return eid


def open_alarm(cam: dict, read_id: int, hit: dict, otso_plate: str | None = None) -> int:
    """A deny / watch hit: an alarm event (or the open one of the same plate, list and camera within LIST_COOLDOWN_S).
    otso_plate: the plate Otso read on a read the camera had none for (Looker)."""
    r = db.one("SELECT * FROM vehicle_reads WHERE id=?", [read_id])
    prev = db.one("SELECT event_id FROM vehicle_reads WHERE camera_id=? AND list=? AND list_entry=? AND event_id IS NOT NULL "
                  "AND ts >= ? AND id != ? ORDER BY ts DESC LIMIT 1",
                  [cam["id"], hit["list"], hit["id"], r["ts"] - LIST_COOLDOWN_S, read_id])
    if prev and db.event(prev["event_id"]):
        db.execute("UPDATE vehicle_reads SET event_id=? WHERE id=?", [prev["event_id"], read_id])
        return prev["event_id"]
    plate_ = otso_plate or r["plate"]
    text = hit_text(plate_, hit) + (" (read by Otso: the camera read no plate)" if otso_plate else "")
    policy = {"kind": "plate_list", "list": hit["list"], "match": hit["match"], "plate": plate_, "entry": hit["plate"],
              "priority": hit_effect(hit)["priority"], "text": text, **({"category": hit["category"]} if hit.get("category") else {})}
    eid = _plate_event(cam, r, policy, "traffic/plate_list", hit["list"], plate_, by_otso=bool(otso_plate))
    log.info("[%s] plate %s on the %s list (%s match, read %s%s): alarm event %s", cam["id"], plate_, hit["list"], hit["match"],
             read_id, ", Otso's read" if otso_plate else "", eid)
    return eid


LIST_WORDS = {"deny": "on the deny list", "watch": "on the watch list", "allow": "on the allow list"}


def hit_text(plate_: str, hit: dict) -> str:
    """"Plate ABC 1234 is on the deny list (trespass: banned 10/12)", "... matches ABC12? on the watch list", "... is one
    character off ..."."""
    shown, words = plate.display(plate_), LIST_WORDS[hit["list"]]
    text = (f"Plate {shown} is {words}" if hit["match"] == "exact"
            else f"Plate {shown} matches {hit['plate']} {words}" if hit["match"] == "pattern"
            else f"Plate {shown} is one character off {plate.display(hit['plate'])}, {words}")
    why = ": ".join(x for x in (hit.get("category"), hit.get("note")) if x)
    return text + (f" ({why})" if why else "")


def footage_list_rule(e: dict) -> dict | None:
    """A plate Otso read from an event's footage (plate.py) on a list entry that raises an alarm: the event's broken
    rule (policy.check), as a traffic read's list hit would be. Exact and partial plates only (a near match on a plate
    read from footage is too loose), never on a traffic camera (its plate is the camera's read, checked at the read)."""
    if not e.get("plate") or reads_plates(e["camera_id"]):
        return None
    if ((e.get("detections") or {}).get("plate_read") or {}).get("source") == "camera":
        return None
    hit = match_list(e["plate"], e["start_ts"], e["camera_id"], near_ok=False)
    if not hit or not hit["effect"]["alarm"]:
        return None
    return {"kind": "plate_list", "list": hit["list"], "match": hit["match"], "plate": e["plate"], "entry": hit["plate"],
            "priority": hit["effect"]["priority"], "text": hit_text(e["plate"], hit) + " (read by Otso from the footage)",
            **({"category": hit["category"]} if hit.get("category") else {})}


# ---------------------------------------------------------------- plate pattern alerts

PATTERNS = ("circling", "new", "unusual_time")
PATTERN_PRIORITY = {"circling": "medium", "new": "medium", "unusual_time": "low"}
DEFAULT_ALERTS = {"circling": True, "new": False, "unusual_time": False, "when": "armed"}
CIRCLE_APART_S = 120.0   # sightings closer than this are one (a car parked in view, re-read now and then)
PATTERN_MIN_CONF = 0.8   # "new" and "unusual time" need a sure read: a misread plate is always new
UNUSUAL_MIN = 120        # minutes of the day from every earlier sighting
UNUSUAL_SEEN = 8         # ...of a plate seen at least this often, on UNUSUAL_DAYS days, mostly at its usual times
UNUSUAL_DAYS = 5
UNUSUAL_HABIT_SHARE = 0.6


def alerts_config(cam: dict | None) -> dict:
    """The camera's pattern alerts: {circling, new, unusual_time: bool, when: armed | always}."""
    a = ((cam or {}).get("traffic") or {}).get("alerts") or {}
    out = {**DEFAULT_ALERTS, **{k: bool(a[k]) for k in PATTERNS if k in a}}
    out["when"] = a.get("when") if a.get("when") in ("armed", "always") else DEFAULT_ALERTS["when"]
    return out


def set_alerts(camera_id: str, **changes) -> dict:
    cur = alerts_config(camera(camera_id))
    cur.update({k: v for k, v in changes.items() if v is not None and (k in PATTERNS or k == "when")})
    set_traffic(camera_id, alerts=cur)
    return cur


def history_since(camera_id: str) -> float | None:
    """When the camera's plate history starts: its oldest kept read or older day summary."""
    r = db.one("SELECT MIN(ts) AS t FROM vehicle_reads WHERE camera_id=?", [camera_id])
    d = db.one("SELECT MIN(day) AS d FROM plate_days WHERE camera_id=?", [camera_id])
    ts = [x for x in ((r or {}).get("t"), dt.datetime.strptime(d["d"], "%Y-%m-%d").timestamp() if d and d["d"] else None) if x]
    return min(ts) if ts else None


def _apart(seen: list[dict], gap: float = CIRCLE_APART_S) -> list[dict]:
    out: list[dict] = []
    for s in seen:
        if not out or s["ts"] - out[-1]["ts"] >= gap:
            out.append(s)
    return out


def _minutes_apart(a: int, b: int) -> int:
    d = abs(a - b) % 1440
    return min(d, 1440 - d)


def _sighting_text(s: dict, today: dt.date) -> str:
    t = dt.datetime.fromtimestamp(s["ts"])
    return ((f"{day_text(t.date(), today)} " if t.date() != today else "") + clock(t.hour * 60 + t.minute)
            + (f" {DIRECTION_WORDS[s['direction']]}" if s.get("direction") in DIRECTION_WORDS else ""))


def find_pattern(cam: dict, r: dict, cfg: dict, armed: bool | None) -> tuple[str, str] | None:
    """(pattern, text) for a read's plate, or None. Pure over the database: no event made."""
    p_ = r["plate"]
    now_d = dt.date.fromtimestamp(r["ts"])
    shown = plate.display(p_)
    if cfg["circling"]:
        rows = db.all("SELECT id, pass_id, ts, camera_id, direction, vehicle_type, plate, confidence FROM vehicle_reads "
                      "WHERE plate=? AND ts >= ? AND ts <= ? ORDER BY ts", [p_, r["ts"] - settings.traffic_circling_s, r["ts"] + 1])
        seen = _apart(sorted(_pass_sightings(rows), key=lambda s: s["ts"]))
        if len(seen) >= settings.traffic_circling_passes:
            mins = max(1, round((seen[-1]["ts"] - seen[0]["ts"]) / 60))
            return "circling", (f"Plate {shown} passed {len(seen)} times in {mins} minutes ("
                                + ", ".join(_sighting_text(s, now_d) for s in seen[-6:]) + ")")
    if (r["confidence"] or 0) < PATTERN_MIN_CONF or not (cfg["when"] == "always" or armed):
        return None
    if not (cfg["new"] or cfg["unusual_time"]):
        return None
    start = history_since(cam["id"])
    if start is None or r["ts"] - start < settings.traffic_learn_days * 86400:
        return None
    seen = sightings(p_)
    before = [s for s in seen if s["ts"] < r["ts"] - CIRCLE_APART_S]
    when = " during SOC hours" if armed else ""
    if cfg["new"] and not before:
        known = {x["plate"] for x in db.all("SELECT DISTINCT plate FROM vehicle_reads WHERE plate IS NOT NULL AND plate != ?", [p_])}
        known |= {x["plate"] for x in db.all("SELECT DISTINCT plate FROM plate_days WHERE plate != ?", [p_])}
        if not any(near(k, p_) for k in known):   # one character off a plate seen before: a misread, not a new car
            days = round((r["ts"] - start) / 86400)
            return "new", f"Plate {shown} has never been seen here before{when} (history of {days} days)"
    if cfg["unusual_time"] and len(before) >= UNUSUAL_SEEN and len({dt.date.fromtimestamp(s["ts"]) for s in before}) >= UNUSUAL_DAYS:
        t = dt.datetime.fromtimestamp(r["ts"])
        minute = t.hour * 60 + t.minute
        mins = [dt.datetime.fromtimestamp(s["ts"]) for s in before]
        if all(_minutes_apart(minute, m.hour * 60 + m.minute) >= UNUSUAL_MIN for m in mins):
            hs = habits(before)
            if hs and sum(h["n"] for h in hs) >= UNUSUAL_HABIT_SHARE * len(before):
                usual = " and ".join(f"around {clock(round(h['mid'] / 5) * 5)}" for h in hs[:2])
                return "unusual_time", (f"Plate {shown}, seen {len(before)} times before, usually {usual}, "
                                        f"is here at {clock(minute)}{when}")
    return None


def check_patterns(cam: dict, read_id: int, armed: bool | None = None) -> int | None:
    """A read's plate against the camera's pattern alerts: a new alarm event's id, or None (no pattern, or the plate's
    alarm for that pattern is still open: the read joins it)."""
    cfg = alerts_config(cam)
    if not any(cfg[k] for k in PATTERNS):
        return None
    r = db.one("SELECT * FROM vehicle_reads WHERE id=?", [read_id])
    if not r or not r["plate"] or (r["confidence"] or 0) < settings.traffic_list_min_conf or is_noise(r):
        return None
    if armed is None and cfg["when"] == "armed" and (cfg["new"] or cfg["unusual_time"]):
        from .deterrence import armed_now
        armed = armed_now()
    found = find_pattern(cam, r, cfg, armed)
    if not found:
        return None
    kind, text = found
    prev = db.one("SELECT id FROM events WHERE plate=? AND track_id LIKE 'lpr:%' AND rules LIKE ? AND start_ts >= ? "
                  "ORDER BY start_ts DESC LIMIT 1",
                  [r["plate"], f'%"rule": "{kind}"%', r["ts"] - settings.traffic_pattern_cooldown_s])
    if prev:
        db.execute("UPDATE vehicle_reads SET event_id=? WHERE id=?", [prev["id"], read_id])
        return None
    notes = plate_notes(r["plate"], r["ts"], r["ts"])["notes"]
    policy = {"kind": "plate_pattern", "pattern": kind, "plate": r["plate"], "priority": PATTERN_PRIORITY[kind],
              "text": text, "notes": notes}
    eid = _plate_event(cam, r, policy, "traffic/plate_pattern", kind, r["plate"])
    log.info("[%s] plate %s: %s (read %s): alarm event %s", cam["id"], r["plate"], kind, read_id, eid)
    return eid


def check_expected(cam: dict, read_id: int, seen: dict) -> int | None:
    """After Otso described a read's vehicle: is it the vehicle the plate's list entry says the plate belongs on? A
    different one raises the cloned-plate alarm (high: a plate on the wrong vehicle is often stolen), once per plate
    per traffic_pattern_cooldown_s. Returns a new alarm's id."""
    r = db.one("SELECT * FROM vehicle_reads WHERE id=?", [read_id])
    p_ = (r or {}).get("plate") or (r or {}).get("otso_plate")
    if not r or not p_:
        return None
    for e in expected_entries(p_, r["ts"]):
        why = vehicle_mismatch(e["expect"], seen)
        if not why:
            continue
        prev = db.one("SELECT id FROM events WHERE plate=? AND track_id LIKE 'lpr:%' AND rules LIKE ? AND start_ts >= ? LIMIT 1",
                      [p_, '%"rule": "mismatch"%', r["ts"] - settings.traffic_pattern_cooldown_s])
        if prev:
            db.execute("UPDATE vehicle_reads SET event_id=? WHERE id=?", [prev["id"], read_id])
            return None
        expected = plate.vehicle_words(e["expect"])
        text = (f"Plate {plate.display(p_)} belongs on a {expected}" + (f" ({e['note']})" if e.get("note") else "")
                + f", but Otso saw {why[0]} ({plate.vehicle_words(seen)}): the plate may be stolen or on the wrong vehicle")
        policy = {"kind": "plate_pattern", "pattern": "mismatch", "plate": p_, "priority": "high", "text": text,
                  "expected": e["expect"], "seen": seen, "entry": e["id"]}
        eid = _plate_event(cam, r, policy, "traffic/plate_pattern", "mismatch", p_, by_otso=not r["plate"])
        log.info("[%s] plate %s on the wrong vehicle (read %s): %s; alarm event %s", cam["id"], p_, read_id, why[0], eid)
        return eid
    return None


# ---------------------------------------------------------------- search and counts

LOOK_STOP = {"a", "an", "the", "with", "and", "on", "in", "of", "its", "it", "that", "has", "having", "vehicle", "vehicles",
             "any", "some", "color", "colour", "colored", "coloured"}
LOOK_SAME = {"grey": "gray", "pick-up": "pickup", "lettered": "lettering", "logos": "logo", "racks": "rack"}
LOOK_TEXT = ("(' ' || LOWER(COALESCE(otso_look, '') || ' ' || COALESCE(vehicle_color, '') || ' ' || COALESCE(vehicle_type, '') "
             "|| ' ' || COALESCE(vehicle_brand, '')) || ' ')")


def look_words(text: str) -> list[str]:
    """The words of a description search ("white pickups with ladder racks" -> white, pickup, ladder, rack): each must
    start a word of Otso's description or the camera's colour, type or make."""
    out = []
    for w in re.findall(r"[a-z0-9][a-z0-9-]*", (text or "").lower()):
        if w in LOOK_STOP:
            continue
        w = LOOK_SAME.get(w, w)
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return list(dict.fromkeys(out))[:8]


def search(camera: str | None = None, since: float | None = None, until: float | None = None, q: str | None = None,
           vehicle_type: str | None = None, color: str | None = None, direction: str | None = None, list_: str | None = None,
           min_conf: float = 0.0, plates_only: bool = False, all_reads: bool = False, before_id: int | None = None,
           limit: int = 100, noise: bool = False, look: str | None = None) -> dict:
    """Reads newest first. q: a plate or part of one (letters and digits; 3+ finds it inside plates), with near plates
    (one character off) as `near`. look: a description ("white pickup ladder rack"), every word in Otso's description
    or the camera's colour, type or make (look_words); one read per pass, the described one. Without all_reads only each
    pass's best read. Reads that aren't a vehicle are left out unless `noise` or a plate search (q)."""
    words = look_words(look) if look else []
    if words:
        where, params = [f"{LOOK_TEXT} LIKE ?" for _ in words], [f"% {w}%" for w in words]
        sql, p = noise_sql()
        where.append(f"NOT {sql}"); params += p
        for col, v in (("camera_id", camera), ("direction", direction)):
            if v:
                where.append(f"{col}=?"); params.append(v)
        for op, v in ((">=", since), ("<=", until)):
            if v:
                where.append(f"ts{op}?"); params.append(v)
        if before_id:
            where.append("id<?"); params.append(before_id)
        n = max(1, min(int(limit), 500))
        rows = db.all(f"SELECT * FROM vehicle_reads WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", [*params, n * 3])
        out, passes_ = [], set()
        for r in sorted(rows, key=lambda r: (r["otso_look"] is None, -r["id"])):   # a pass's described read first
            k = r["pass_id"] or r["id"]
            if k not in passes_:
                passes_.add(k)
                out.append(r)
        out = sorted(out, key=lambda r: -r["id"])[:n]
        return {"reads": [public(r) for r in out], "near": [], "look": words}
    where, params = [], []
    if not noise and not q:
        sql, p = noise_sql()
        where.append(f"NOT {sql}"); params += p
    if camera:
        where.append("camera_id=?"); params.append(camera)
    if since:
        where.append("ts>=?"); params.append(since)
    if until:
        where.append("ts<=?"); params.append(until)
    if vehicle_type:
        where.append("LOWER(vehicle_type)=?"); params.append(vehicle_type.lower())
    if color:
        where.append("LOWER(vehicle_color)=?"); params.append(color.lower())
    if direction:
        where.append("direction=?"); params.append(direction)
    if list_:
        where.append("list=?" if list_ != "any" else "list IS NOT NULL")
        params += [list_] if list_ != "any" else []
    if min_conf:
        where.append("confidence>=?"); params.append(min_conf)
    if before_id:
        where.append("id<?"); params.append(before_id)
    qn = re.sub(r"[^A-Z0-9]", "", (q or "").upper())
    near_plates: list[str] = []
    if qn:
        base = list(where), list(params)
        if len(qn) >= 5:   # near plates (one character off) seen in the same window
            seen = db.all(f"SELECT DISTINCT COALESCE(plate, otso_plate) AS plate FROM vehicle_reads WHERE COALESCE(plate, otso_plate) "
                          f"IS NOT NULL{' AND ' if base[0] else ''}{' AND '.join(base[0])} LIMIT 50000", base[1])
            near_plates = sorted({s["plate"] for s in seen if s["plate"] != qn and near(s["plate"], qn)})[:20]
        where.append("(plate LIKE ? OR otso_plate LIKE ? OR pass_id IN (SELECT pass_id FROM vehicle_reads WHERE plate LIKE ?)"
                     + "".join(" OR plate=? OR otso_plate=?" for _ in near_plates) + ")")
        params += [f"%{qn}%", f"%{qn}%", f"%{qn}%", *(p for x in near_plates for p in (x, x))]
    elif plates_only:
        where.append("(plate IS NOT NULL OR otso_plate IS NOT NULL)")
    if not all_reads and not qn:
        where.append("best=1")
    rows = db.all("SELECT * FROM vehicle_reads" + (f" WHERE {' AND '.join(where)}" if where else "")
                  + " ORDER BY id DESC LIMIT ?", [*params, max(1, min(int(limit), 500))])
    return {"reads": [public(r) for r in rows], "near": near_plates}


def counts(camera: str | None = None, since: float | None = None, until: float | None = None, bucket: str = "hour") -> dict:
    """Vehicles per hour (or day, local time) from the reads themselves: total, with a plate, by direction and type.
    One vehicle is one pass."""
    now = time.time()
    since = since or now - 86400
    until = until or now
    size = 86400 if bucket == "day" else 3600
    sql, p = noise_sql()
    where, params = ["ts>=?", "ts<?", "best=1", f"NOT {sql}"], [since, until, *p]
    if camera:
        where.append("camera_id=?"); params.append(camera)
    rows = db.all(f"SELECT ts, plate, direction, vehicle_type FROM vehicle_reads WHERE {' AND '.join(where)}", params)
    out: dict[float, dict] = {}
    for r in rows:
        local = dt.datetime.fromtimestamp(r["ts"])
        start = local.replace(minute=0, second=0, microsecond=0)
        if size == 86400:
            start = start.replace(hour=0)
        k = start.timestamp()
        b = out.setdefault(k, {"t": k, "total": 0, "plates": 0, "direction": {}, "type": {}})
        b["total"] += 1
        b["plates"] += r["plate"] is not None
        d = r["direction"] or "unknown"
        b["direction"][d] = b["direction"].get(d, 0) + 1
        t = (r["vehicle_type"] or "unknown").lower()
        b["type"][t] = b["type"].get(t, 0) + 1
    return {"bucket": bucket, "since": since, "until": until, "buckets": [out[k] for k in sorted(out)]}


# ---------------------------------------------------------------- Ask

PLATE_TOKEN = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9-]{2,9}\b")
NOT_PLATE = re.compile(r"^(\d{1,2}(:\d{2})?(am|pm)|\d+(st|nd|rd|th|s|min|mins|h|hr|hrs|am|pm|days?|weeks?|ft|mph|k)|"
                       r"(19|20)\d{2}|f-?\d{3}|rav4|4runner|e-?\d{3})$", re.I)
COLORS = {"white", "black", "gray", "grey", "silver", "red", "blue", "green", "yellow", "brown", "orange", "gold", "beige", "purple"}
TYPES = {"car": "car", "cars": "car", "sedan": "car", "suv": "suv", "suvs": "suv", "truck": "truck", "trucks": "truck",
         "pickup": "truck", "pickups": "truck", "van": "van", "vans": "van", "bus": "bus", "motorcycle": "motorbike"}
ASK_LIMIT = 15


def plate_tokens(question: str, plate_words: bool) -> list[str]:
    """Plates (or parts of one) named in a question: letters and digits together ("ABC-123", "1691AJN"), or 5+ digits
    when the question is about a plate ("tag 547344"); never a time, a year or a model name."""
    out = []
    for m in PLATE_TOKEN.finditer(question):
        t = m.group(0)
        n = re.sub(r"[^A-Z0-9]", "", t.upper())
        if NOT_PLATE.match(t) or len(n) < 3:
            continue
        if re.search(r"\d", n) and (re.search(r"[A-Z]", n) or (plate_words and len(n) >= 5)):
            out.append(n)
    return list(dict.fromkeys(out))


def read_line(r: dict, names: dict[str, str] | None = None) -> str:
    """One read for Ask: what the camera's plate reader saw, its doubts said ("state reported", "going away (reported)")."""
    if r.get("plate"):
        bits = [f"license plate {plate.display(r['plate'])} read by the camera's plate reader"
                f" ({round((r['confidence'] or 0) * 100)} % sure{'; state reported ' + r['region'] if r.get('region') else ''})"]
    elif r.get("otso_plate"):
        bits = [f"license plate {plate.display(r['otso_plate'])} read by Otso from the camera's picture (the camera read none)"]
    else:
        bits = ["no license plate read"]
    what = " ".join(x for x in ((r.get("vehicle_color") or "").lower(), (r.get("vehicle_type") or "vehicle").lower()) if x)
    bits.append(f"a {what}" + (f" (looks like a {r['vehicle_brand']})" if r.get("vehicle_brand") else "")
                + (" (the camera's guess)" if r.get("otso_look") else "")
                + (f", going {r['direction'] if r['direction'] == 'away' else 'toward the camera'} (reported)" if r.get("direction") else ""))
    if r.get("otso_look"):
        bits.append(f"Otso saw a {r['otso_look']}")
    if r.get("list"):
        bits.append(f"{'possibly ' if r.get('list_match') == 'near' else ''}on the {r['list']} list")
    return ", ".join(bits)


def period_text(since: float, until: float | None, now: float) -> str:
    """"since today at 12:00 AM", "from Oct 27 at 6:00 AM to yesterday at 9:00 PM"."""
    today = dt.date.fromtimestamp(now)

    def at(t: float) -> str:
        d = dt.datetime.fromtimestamp(t)
        return f"{day_text(d.date(), today)} at {clock(d.hour * 60 + d.minute)}"
    return f"since {at(since)}" if until is None or until >= now - 60 else f"from {at(since)} to {at(until)}"


REPEATS_LIMIT = 10      # Ask: the plates seen most often in the period, each with up to REPEAT_TIMES of its times
REPEAT_TIMES = 6
# words that make a question about the traffic camera's vehicles without saying "plate" ("top 5 visitors?", "regulars")
# (not "most", "top" or "cars": "the most recent person", "top of the stairs" and "cars in the lot" are about events)
VISIT_WORDS = {"visitor", "visitors", "regular", "regulars", "repeat", "repeats", "returning", "frequent", "frequently",
               "often", "twice", "traffic"}


def ask_totals(since: float, until: float | None, now: float, names: dict[str, str],
               tokens: list[str] | None = None) -> list[dict]:
    """Counted totals for Ask, one note per camera. The reads listed with them are only the latest few (ASK_LIMIT),
    and a model counting those answered "16 plates today" when there were 1,079 (TSS-DT 2026-10-29); the hub's and
    the server's answer rules say to count from these."""
    sql, p = noise_sql()
    end = until if until is not None else now + 1
    period = period_text(since, until, now)
    out = []
    if tokens:
        for t in tokens[:3]:
            for r in db.all(f"SELECT camera_id, COUNT(DISTINCT COALESCE(pass_id, id)) AS n FROM vehicle_reads WHERE ts >= ? "
                            f"AND ts < ? AND (plate LIKE ? OR otso_plate LIKE ?) AND NOT {sql} GROUP BY camera_id",
                            [since, end, f"%{t}%", f"%{t}%", *p]):
                out.append({"kind": "note", "camera_id": r["camera_id"], "camera_name": names.get(r["camera_id"], r["camera_id"]),
                            "text": f"COUNTED: plate {t} {period}: seen {times(r['n'])}"})
        return out
    for r in db.all(f"SELECT camera_id, COUNT(DISTINCT COALESCE(pass_id, id)) AS passes, "
                    f"COUNT(DISTINCT CASE WHEN plate IS NOT NULL THEN COALESCE(pass_id, id) END) AS with_plate, "
                    f"COUNT(DISTINCT plate) AS plates FROM vehicle_reads WHERE ts >= ? AND ts < ? AND NOT {sql} "
                    f"GROUP BY camera_id", [since, end, *p]):
        out.append({"kind": "note", "camera_id": r["camera_id"], "camera_name": names.get(r["camera_id"], r["camera_id"]),
                    "text": f"COUNTED: {r['with_plate']:,} vehicles with a license plate read {period} "
                            f"({r['plates']:,} different plates; {r['passes']:,} vehicles passed in all)"})
    return out


def ask_repeats(since: float, until: float | None, now: float, names: dict[str, str]) -> list[dict]:
    """The plates seen more than once in the period, most first, counted, each with its times, the way it went and the
    vehicle: "top 5 visitors?", "which plates came back?" (a list of the latest reads can't answer those)."""
    sql, p = noise_sql()
    end = until if until is not None else now + 1
    period = period_text(since, until, now)
    top = db.all(f"SELECT plate, COUNT(DISTINCT COALESCE(pass_id, id)) AS n FROM vehicle_reads WHERE ts >= ? AND ts < ? "
                 f"AND plate IS NOT NULL AND NOT {sql} GROUP BY plate HAVING n >= 2 ORDER BY n DESC, MAX(ts) DESC LIMIT ?",
                 [since, end, *p, REPEATS_LIMIT])
    if not top:
        return [{"kind": "note", "camera_id": None, "text": f"COUNTED: no license plate was seen more than once {period}"}]
    today = dt.date.fromtimestamp(now)
    lines = []
    for t in top:
        rows = db.all("SELECT id, pass_id, ts, camera_id, direction, vehicle_type, vehicle_color, vehicle_brand, plate, confidence "
                      "FROM vehicle_reads WHERE plate=? AND ts >= ? AND ts < ? ORDER BY ts", [t["plate"], since, end])
        seen = _pass_sightings(rows)
        stamps = []
        for x in seen[-REPEAT_TIMES:]:
            d = dt.datetime.fromtimestamp(x["ts"])
            stamps.append((f"{day_text(d.date(), today)} " if d.date() != today else "") + clock(d.hour * 60 + d.minute)
                          + (f" {DIRECTION_WORDS[x['direction']]}" if x["direction"] in DIRECTION_WORDS else ""))
        r = next((x for x in reversed(rows) if x["vehicle_type"]), None)
        what = " ".join(v for v in ((r["vehicle_color"] or "").lower(), (r["vehicle_brand"] or ""), (r["vehicle_type"] or "").lower()) if v) if r else ""
        cams = {x["camera_id"] for x in seen}
        lines.append(f"{plate.display(t['plate'])}: {times(t['n'])}"
                     + (f" (latest {len(stamps)}: " if t["n"] > len(stamps) else " (") + ", ".join(stamps) + ")"
                     + (f", a {what} (reported)" if what else "")
                     + (f", on {', '.join(names.get(c, c) for c in sorted(cams))}" if len(cams) > 1 else ""))
    return [{"kind": "note", "camera_id": None,
             "text": f"COUNTED: license plates seen more than once {period}, most often first: " + "; ".join(lines)}]


# words that pick a vehicle out: with a make or model, they make a question a search of the vehicles' descriptions
# ("did a white box truck with a ladder rack come by?"), not of plates
DETAIL_WORDS = {"ladder", "ladders", "rack", "racks", "trailer", "toolbox", "logo", "logos", "lettering", "lettered",
                "lifted", "roof", "box", "dent", "dented", "damage", "damaged", "bumper", "sticker", "stickers", "decal",
                "flatbed", "dump", "utility", "tonneau", "topper", "camper", "kayak", "bike", "wrap", "wrapped", "tinted",
                "chrome", "rims", "cargo", "semi", "tanker", "police", "taxi", "ambulance", "school", "company", "fleet",
                "work", "delivery", "tow", "wrecker", "crane", "bucket", "mixer", "plow"}
LOOK_DAYS = 7            # a description question with no period looks this far back
LOOK_LIMIT = 10


def ask_looks(used: list[str], since: float, until: float | None, now: float, names: dict[str, str]) -> list[dict]:
    """A description question: the matching vehicles counted (with how many Otso has described, since the camera's own
    colour, type and make are often wrong), and the latest few as items."""
    sql, p = noise_sql()
    end = until if until is not None else now + 1
    words = look_words(" ".join(used))
    conds = " AND ".join(f"{LOOK_TEXT} LIKE ?" for _ in words)
    likes = [f"% {w}%" for w in words]
    period = period_text(since, until, now)
    n = db.one(f"SELECT COUNT(DISTINCT COALESCE(pass_id, id)) AS n FROM vehicle_reads WHERE ts >= ? AND ts < ? AND NOT {sql} "
               f"AND {conds}", [since, end, *p, *likes])["n"]
    tot = db.one(f"SELECT COUNT(DISTINCT COALESCE(pass_id, id)) AS n, COUNT(DISTINCT CASE WHEN otso_look IS NOT NULL "
                 f"THEN COALESCE(pass_id, id) END) AS d FROM vehicle_reads WHERE ts >= ? AND ts < ? AND NOT {sql}",
                 [since, end, *p])
    out = [{"kind": "note", "camera_id": None,
            "text": f"COUNTED: {n:,} vehicle{'' if n == 1 else 's'} {period} match \"{' '.join(words)}\" in Otso's or the camera's description "
                    f"(Otso has described {tot['d']:,} of the {tot['n']:,} vehicles that passed; for the rest only the "
                    f"camera's colour, type and make, which are often wrong)"}]
    for r in search(since=since, until=until, look=" ".join(words), limit=LOOK_LIMIT)["reads"]:
        out.append({"kind": "plate", "read_id": r["id"], "ts": r["ts"], "camera_id": r["camera_id"],
                    "camera_name": names.get(r["camera_id"], r["camera_id"]), "plate": r["plate"] or r.get("otso_plate"),
                    "confidence": r["confidence"], "event_id": r["event_id"], "text": read_line(r)})
    return out


def ask_evidence(question: str, since: float | None, until: float | None, now: float | None = None,
                 previous: str | None = None) -> list[dict]:
    """Plate reads for an Ask question: the plates it names (and near ones), or, when it asks about plates (or the
    traffic camera's vehicles, VISIT_WORDS) without naming one, the latest confident reads matching its colour and type
    words. Items: {kind: "plate", read_id, ts, camera_id, camera_name, plate, confidence, event_id, text}, after counted
    notes ({kind: "note", text}: ask_totals per camera, and for a general question ask_repeats). `previous`: the last
    question of the conversation; a follow-up to a plate question ("who were the top 5?") stays on plates. Nothing
    without reads."""
    if not db.one("SELECT 1 AS x FROM vehicle_reads LIMIT 1"):
        return []
    from .assistant import MAKE_WORDS, MODEL_WORDS, PLATE_WORDS
    now = now or time.time()
    words = set(re.findall(r"[a-z0-9]+", question.lower()))
    before = set(re.findall(r"[a-z0-9]+", (previous or "").lower()))
    plate_words = bool(words & PLATE_WORDS) or bool(words & VISIT_WORDS) or bool(before & PLATE_WORDS)
    tokens = plate_tokens(question, plate_words)
    one = words | {w[:-1] for w in words if len(w) > 3 and w.endswith("s")}   # "Tundras", "ladders"
    if not tokens and (one & (MAKE_WORDS | MODEL_WORDS)
                       or one & DETAIL_WORDS and one & (set(TYPES) | {"vehicle", "vehicles"})):
        # a vehicle described ("white box truck with a ladder rack", "a Tundra"): its description, plate or not
        vocab = COLORS | set(TYPES) | MAKE_WORDS | MODEL_WORDS | DETAIL_WORDS
        used = [w for w in dict.fromkeys(re.findall(r"[a-z0-9][a-z0-9-]*", question.lower()))
                if w in vocab or (w.endswith("s") and w[:-1] in vocab)]
        names = {c["id"]: c["name"] for c in db.cameras() if c.get("traffic")} or {c["id"]: c["name"] for c in db.cameras()}
        return ask_looks(used, since if since is not None else now - LOOK_DAYS * 86400, until, now, names)
    if not tokens and not plate_words:
        return []
    since = since if since is not None else now - (30 if tokens else 1) * 86400
    rows: list[dict] = []
    if tokens:
        for t in tokens[:3]:
            rows += [r for r in search(since=since, until=until, q=t, limit=ASK_LIMIT)["reads"] if not is_noise(r)]
    else:
        color = next((c for c in words & COLORS), None)
        kind = next((TYPES[w] for w in words if w in TYPES), None)
        rows = search(since=since, until=until, color="gray" if color == "grey" else color, vehicle_type=kind,
                      min_conf=settings.traffic_attach_min_conf, plates_only=True, limit=ASK_LIMIT)["reads"]
    names = {c["id"]: c["name"] for c in db.cameras() if c.get("traffic")} or {c["id"]: c["name"] for c in db.cameras()}
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: -r["ts"]):
        if r["id"] in seen or not r["plate"]:
            continue
        seen.add(r["id"])
        out.append({"kind": "plate", "read_id": r["id"], "ts": r["ts"], "camera_id": r["camera_id"],
                    "camera_name": names.get(r["camera_id"], r["camera_id"]), "plate": r["plate"],
                    "confidence": r["confidence"], "event_id": r["event_id"], "text": read_line(r)})
    notes = ask_totals(since, until, now, names, tokens)
    if not tokens:
        notes += ask_repeats(since, until, now, names)
    return notes + out[:ASK_LIMIT]


# ---------------------------------------------------------------- vehicles around an SOC incident

AROUND_MAX_S = 6 * 3600      # the longest window asked for
AROUND_LIMIT = 20            # vehicles, those with a plate first
AROUND_NOTES = 3             # of a plate's notes (plate_notes), the first few
FOOTAGE_DAYS = 30            # a footage plate's note counts its other events this far back


def _first_sentence(text: str | None, n: int = 140) -> str | None:
    s = re.split(r"(?<=[.!?])\s", (text or "").strip(), maxsplit=1)[0]
    return (s[:n].rsplit(" ", 1)[0] + "…") if len(s) > n else s or None


def around(since: float, until: float, at: float | None = None, now: float | None = None) -> dict:
    """The vehicles seen between since and until, for the hub's SOC incident card (docs/soc-plan.md §1): the traffic
    cameras' passes (one per pass: its plate, the camera's or Otso's, Otso's description or else the camera's guess,
    direction, the plate's counted notes) and the plates Qwen read from event footage on other cameras (source
    "footage"). Counted and worded in code, never by the model. {vehicles (oldest first), total}: at most AROUND_LIMIT,
    those with a plate first, then the nearest to `at` (the incident's first event; a highway passes hundreds in the
    window, cam8 ~360 in 15 minutes); total = all of them."""
    now = now or time.time()
    at = at if at is not None else since + (until - since) / 2
    sql, p = noise_sql()
    names = {c["id"]: c["name"] for c in db.cameras()}
    rows = db.all(f"SELECT * FROM vehicle_reads WHERE ts >= ? AND ts <= ? AND NOT {sql} ORDER BY ts", [since, until, *p])
    by: dict[int, list[dict]] = {}
    for r in rows:
        by.setdefault(r["pass_id"] or r["id"], []).append(r)
    out = []
    for reads in by.values():
        best = next((r for r in reads if r["best"]), reads[-1])
        looked = next((r for r in reads if r["otso_look"]), None)
        otso_plate = next((r["otso_plate"] for r in reads if r["otso_plate"]), None)
        plate_ = best["plate"] or otso_plate
        dirs = [r["direction"] for r in reads if r["direction"]]
        imgs = json.loads(best["images"]) if best["images"] else {}
        out.append({
            "source": "traffic", "ts": reads[0]["ts"], "camera_id": best["camera_id"],
            "camera_name": names.get(best["camera_id"], best["camera_id"]), "read_id": best["id"], "event_id": best["event_id"],
            "plate": plate_, "plate_by": ("camera" if best["plate"] else "otso") if plate_ else None,
            "confidence": best["confidence"] if best["plate"] else None,
            "vehicle": looked["otso_look"] if looked else " ".join(x for x in ((best["vehicle_color"] or "").lower(),
                                                                         best["vehicle_brand"] or "", (best["vehicle_type"] or "").lower()) if x) or None,
            "vehicle_by": "otso" if looked else "camera",
            "direction": max(set(dirs), key=dirs.count) if dirs else None,
            "list": best["list"], "images": sorted(k for k in ("plate", "vehicle") if k in imgs), "notes": []})
    events = db.all("SELECT id, camera_id, start_ts, plate, plate_region, plate_conf, synopsis FROM events WHERE status='verified' "
                    "AND plate IS NOT NULL AND start_ts >= ? AND start_ts <= ? AND id NOT IN "
                    "(SELECT event_id FROM vehicle_reads WHERE event_id IS NOT NULL AND ts >= ? AND ts <= ?) ORDER BY start_ts",
                    [since, until, since - 60, until + 60])
    traffic_cams = {c["id"] for c in db.cameras() if (c.get("traffic") or {}).get("enabled")}
    for e in events:
        if e["camera_id"] in traffic_cams:   # its plate is a traffic read's (attach_read), listed as the read
            continue
        n = db.one("SELECT COUNT(*) AS n FROM events WHERE plate=? AND id != ? AND status='verified' AND start_ts >= ?",
                   [e["plate"], e["id"], now - FOOTAGE_DAYS * 86400])["n"]
        out.append({"source": "footage", "ts": e["start_ts"], "camera_id": e["camera_id"],
                    "camera_name": names.get(e["camera_id"], e["camera_id"]), "read_id": None, "event_id": e["id"],
                    "plate": e["plate"], "plate_by": "footage", "confidence": None, "region": e["plate_region"],
                    "vehicle": _first_sentence(e["synopsis"]), "vehicle_by": "otso", "direction": None, "list": None,
                    "images": [], "notes": [f"Read from footage on {times(n)} other events in the last {FOOTAGE_DAYS} days."
                                            if n else f"No other event with this plate in the last {FOOTAGE_DAYS} days."]})
    keep = sorted(out, key=lambda v: (v["plate"] is None, abs(v["ts"] - at)))[:AROUND_LIMIT]
    for v in keep:
        if v["source"] == "traffic" and v["plate"]:
            v["notes"] = plate_notes(v["plate"], v["ts"], now)["notes"][:AROUND_NOTES]
    return {"vehicles": sorted(keep, key=lambda v: v["ts"]), "total": len(out)}


# ---------------------------------------------------------------- retention

def prune(keep_days: int | None = None, now: float | None = None) -> int:
    """Reads and their crops older than keep_days (an alarm event keeps its own copy of the picture), their plates'
    sightings kept first (roll_up) for traffic_history_days. Returns rows deleted."""
    keep_days = keep_days if keep_days is not None else settings.traffic_keep_days
    cutoff = (now or time.time()) - keep_days * 86400
    roll_up(cutoff)
    n = db.execute("DELETE FROM vehicle_reads WHERE ts < ?", [cutoff]).rowcount or 0
    oldest = dt.datetime.fromtimestamp((now or time.time()) - settings.traffic_history_days * 86400).strftime("%Y-%m-%d")
    db.execute("DELETE FROM plate_days WHERE day < ?", [oldest])
    day = dt.datetime.fromtimestamp(cutoff).strftime("%Y-%m-%d")
    root = media_root()
    if root.is_dir():
        for cam_dir in root.iterdir():
            if not cam_dir.is_dir():
                continue
            for d in cam_dir.iterdir():
                if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name) and d.name < day:
                    shutil.rmtree(d, ignore_errors=True)
    if n:
        log.info("traffic: %d reads older than %d days removed", n, keep_days)
    return n


# ---------------------------------------------------------------- camera setup (Milesight)

async def prune_loop() -> None:
    """Hourly: reads and crops past settings.traffic_keep_days."""
    while True:
        try:
            await asyncio.to_thread(prune)
        except Exception:  # noqa: BLE001 - try again next hour
            log.exception("traffic: pruning old reads failed")
        await asyncio.sleep(3600)


def local_address(host: str) -> str:
    """This server's address on the camera's network (the route the OS would take to it; nothing is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, 9))
        return s.getsockname()[0]


def push_url(cam: dict, secret: str) -> str:
    base = settings.traffic_push_base.rstrip("/") or f"http://{local_address(cam['host'])}:{settings.port}"
    return f"{base}/api/traffic/push/{cam['id']}/{secret}"


def hide_secret(url: str) -> str:
    return re.sub(r"(/api/traffic/push/[^/]+/)[^/?#]+", r"\1...", url)


def _ours(slot: dict, camera_id: str) -> bool:
    m = PATH_RE.search(str(slot.get("url") or ""))
    return bool(m and m.group(1) == camera_id)


def camera_state(cam: dict) -> dict:
    """What the camera says (read-only): model, plate licence, plate reading on, its push slots (ours marked, URLs
    without their secret), the SD card. Raises milesight errors when it doesn't answer."""
    from . import milesight
    with milesight._client(cam) as c:
        info = milesight.get(c, cam, "get.system.information")
        lpr = milesight.get(c, cam, "get.lpr.settings")
        post = milesight.get(c, cam, "get.lpr.post")
        try:
            sd = milesight.get(c, cam, "get.storage.settings")
        except Exception:  # noqa: BLE001 - older firmware: unknown, not a failure
            sd = {}
    slots = []
    for i, s in enumerate((post.get("http") or {}).get("httpList") or []):
        slots.append({"index": i, "enabled": bool(s.get("enabled") or s.get("enable")), "ours": _ours(s, cam["id"]),
                      "url": hide_secret(str(s.get("url") or "")), "event_mask": s.get("eventMask"),
                      "status_report": bool(s.get("statusReportEnable"))})
    return {"model": info.get("model"), "firmware": info.get("firmwareVersion"),
            "licensed": lpr.get("licenseStatus") == 1, "lpr_enabled": lpr.get("lprEnable") == 1,
            "country": lpr.get("curArea"), "slots": slots,
            "sd_card": None if not sd else sd.get("sdcardStatus") not in (0, None),
            "resend": bool(post.get("automaticRetransmission"))}


def configure(cam: dict, enable: bool, plate_reading: bool | None = None) -> dict:
    """Set the camera up (enable: plate reading on unless plate_reading is False, a push slot -> this server with a new
    secret when it has none) or stop its push (enable False: our slot emptied; plate reading left as it is unless
    plate_reading is given). The camera's own pages save the whole object back; so does this."""
    from . import milesight
    t = (cam.get("traffic") or {})
    secret = t.get("secret") or secrets.token_urlsafe(18)
    with milesight._client(cam) as c:
        lpr = milesight.get(c, cam, "get.lpr.settings")
        if enable and lpr.get("licenseStatus") not in (1, None):
            raise SetupError("the camera has no plate-reading licence")
        want = 1 if (plate_reading if plate_reading is not None else enable or lpr.get("lprEnable") == 1) else 0
        if want != lpr.get("lprEnable"):
            milesight.post(c, cam, "set.lpr.settings", {**lpr, "lprEnable": want})
        post = milesight.get(c, cam, "get.lpr.post")
        slots = (post.get("http") or {}).get("httpList") or []
        mine = next((s for s in slots if _ours(s, cam["id"])), None)
        if enable:
            if mine is None:
                mine = next((s for s in slots if not (s.get("enable") or s.get("enabled")) and not s.get("url")), None)
            if mine is None:
                raise SetupError("all of the camera's HTTP push slots are in use: free one on the camera's page (Event › Post)")
            mine.update({"enable": 1, "url": push_url(cam, secret), "user": "", "password": "", "eventMask": PUSH_MASK,
                         "statusReportEnable": 1, "statusReportInterval": STATUS_REPORT_S})
            milesight.post(c, cam, "set.lpr.post", {"postType": 1, **post})
        elif mine is not None:
            mine.update({"enable": 0, "url": "", "statusReportEnable": 0})
            milesight.post(c, cam, "set.lpr.post", {"postType": 1, **post})
    state = camera_state({**cam, "traffic": {**t, "secret": secret}})
    set_traffic(cam["id"], enabled=enable, secret=secret, set_at=time.time(), resend=bool(state["resend"] and state["sd_card"]))
    log.info("[%s] traffic push %s", cam["id"], "set up" if enable else "stopped")
    return state
