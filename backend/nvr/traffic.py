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
  was wrong). Only a moving vehicle's match teaches it: one peak in time (PEAK_MAX, PEAK_GAP) on a read with a vehicle
  type; the dealership sign the camera reads as a plate at night matches at every offset. Until then a read waits (REC_TRIES x REC_RETRY_S, the life of a segment) rather than show a guess. An
  older read without a picture is cut the first time it is asked for.
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
import socket
import asyncio
import threading
import time
from collections import deque
from pathlib import Path

from . import plate
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
STRONG_MATCH = 0.93                # plate crop vs recording: a match this strong places the read (right: 0.97-0.99)
AGREE = 2                          # ...and a segment's offset is taken once this many strong matches agree
AGREE_S = 0.5                      # ...within this
PEAK_MAX = 2                       # a match teaches the offset only when it is one moment: at most this many strong
PEAK_GAP = 0.3                     # frames, the best this far above the median (a still thing matches everywhere: the
                                   # car lot's sign read as "656BE" at night scored >= 0.93 at 53-70 of 81 offsets)
WIDE = [round(x * 0.25, 2) for x in range(-60, 21)]   # -15 .. +5 s: a segment's offset not known yet
NARROW = [round(x * 0.25, 2) for x in range(-4, 5)]   # +-1 s around the segment's learned offset
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


def list_entries(now: float | None = None) -> list[dict]:
    now = now or time.time()
    return db.all("SELECT * FROM plate_lists WHERE expires_at IS NULL OR expires_at > ? ORDER BY list, plate", [now])


def match_list(plate_: str | None, now: float | None = None) -> dict | None:
    """The list entry this plate hits: an exact entry first, deny before watch before allow; then a near one."""
    if not plate_:
        return None
    entries = list_entries(now)
    order = {"deny": 0, "watch": 1, "allow": 2}
    for how in ("exact", "near"):
        hits = [e for e in entries if (e["plate"] == plate_ if how == "exact" else near(e["plate"], plate_))]
        if hits:
            e = min(hits, key=lambda e: order.get(e["list"], 9))
            return {**e, "match": how}
    return None


def add_entry(plate_raw: str, list_: str, note: str = "", expires_at: float | None = None, by: str | None = None) -> dict:
    p = plate.normalize(plate_raw)
    if not p:
        raise ValueError(f"{plate_raw[:20]!r} is not a plate")
    if list_ not in LISTS:
        raise ValueError(f"list must be one of {', '.join(LISTS)}")
    db.execute("INSERT INTO plate_lists (plate, list, note, expires_at, created_at, by) VALUES (?,?,?,?,?,?) "
               "ON CONFLICT(plate, list) DO UPDATE SET note=excluded.note, expires_at=excluded.expires_at, by=excluded.by",
               [p, list_, (note or "")[:200], expires_at, time.time(), by])
    return db.one("SELECT * FROM plate_lists WHERE plate=? AND list=?", [p, list_])


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
    hit = match_list(r["plate"], received) if (r["confidence"] or 0) >= settings.traffic_list_min_conf else None
    alarm = hit is not None and hit["list"] in ALARM_LISTS
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
    out = {"read_id": read_id, "event_id": None, "alarm_id": None, "list": hit["list"] if hit else None}
    if alarm:
        out["alarm_id"] = open_alarm(cam, read_id, hit)
    elif r["plate"] and (r["confidence"] or 0) >= settings.traffic_attach_min_conf:
        out["event_id"] = attach_read(read_id)
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
        return float(group[len(group) // 2]) if len(group) >= AGREE else None

    def learn(self, camera_id: str, seg: float, d: float) -> None:
        with self.lock:
            self.found.setdefault((camera_id, seg), deque(maxlen=15)).append(d)
            if len(self.found) > 500:   # old segments
                for k in sorted(self.found, key=lambda k: k[1])[:250]:
                    del self.found[k]


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


def cut_from_recording(read_id: int) -> bool | None:
    """The vehicle picture of a read the camera sent without one, cut from the recorded frame showing it (the camera's
    box, padded as plate.crop does). The frame: the one whose plate region matches the read's plate crop strongly
    (STRONG_MATCH; searched over WIDE until its segment's offset is known, then NARROW around it), else the frame at
    the segment's learned offset. True: the read has a picture now. None: not yet (the recording isn't written, or the
    segment's offset isn't known): try again later. False: nothing is recorded there."""
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
    frame, d = None, learned
    pf = settings.data_dir / images["plate"] if images.get("plate") else None
    pc = cv2.imread(str(pf), cv2.IMREAD_GRAYSCALE) if pf is not None and pf.is_file() and r["plate_box"] else None
    if pc is not None:
        offsets = WIDE if learned is None else [round(learned + x, 2) for x in NARROW]
        got = frames.recording_frames(cam, [ts + x for x in offsets], tol=0.12)
        scores = [(plate_match(got[ts + x], pc, json.loads(r["plate_box"])), x) for x in offsets if ts + x in got]
        best = max(scores, default=(-1.0, None))
        median = sorted(s for s, _ in scores)[len(scores) // 2] if scores else 0.0
        one_moment = sum(s >= STRONG_MATCH for s, _ in scores) <= PEAK_MAX and best[0] - median >= PEAK_GAP
        if best[0] >= STRONG_MATCH and one_moment:
            if r["vehicle_type"]:
                aligner.learn(cam, seg, best[1])
            frame, d = got[ts + best[1]], best[1]
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
    """Reads waiting for their vehicle picture from the recording: (due time, read id, tries)."""

    def __init__(self) -> None:
        self.due: deque = deque()
        self.lock = threading.Lock()

    def add(self, read_id: int, at: float, tries: int = 0) -> None:
        with self.lock:
            if len(self.due) < 5000:   # a camera flooding plate-only reads: the rest are cut when looked at
                self.due.append((at, read_id, tries))

    def take(self, now: float) -> list[tuple[float, int, int]]:
        with self.lock:
            ready = [x for x in self.due if x[0] <= now]
            self.due = deque(x for x in self.due if x[0] > now)
            return ready

    async def run(self) -> None:
        while True:
            for _at, read_id, tries in self.take(time.time()):
                try:
                    done = await asyncio.to_thread(cut_from_recording, read_id)
                except Exception:  # noqa: BLE001 - one read's picture must not stop the rest
                    log.exception("traffic: vehicle picture from the recording for read %s failed", read_id)
                    done = True
                if done is None and tries + 1 < REC_TRIES:
                    self.add(read_id, time.time() + REC_RETRY_S, tries + 1)
            await asyncio.sleep(1)


from_recording = FromRecording()


def otso_crops(read_id: int) -> list[bytes]:
    """What Otso looks at: the camera's vehicle crop (full resolution of the vehicle), then the full frame when kept."""
    out = []
    for kind in ("vehicle", "full"):
        f = image_path(read_id, kind)
        if f is not None:
            out.append(f.read_bytes())
    return out


def store_otso(read_id: int, result: dict) -> dict | None:
    """Keep Otso's answer on the read (a read plate, or why not), beside the camera's own."""
    info = {"at": round(time.time(), 1), **{k: result[k] for k in ("result", "plate", "plate_raw", "region", "confidence",
                                                                   "reason", "model", "crops") if result.get(k) not in (None, "")}}
    db.execute("UPDATE vehicle_reads SET otso=?, otso_plate=? WHERE id=?",
               [json.dumps(info), result.get("plate") if result.get("result") == "read" else None, read_id])
    return read(read_id)


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


def open_alarm(cam: dict, read_id: int, hit: dict) -> int:
    """A deny / watch hit: an alarm event (or the open one of the same plate, list and camera within LIST_COOLDOWN_S)."""
    from . import baseline, verifier
    r = db.one("SELECT * FROM vehicle_reads WHERE id=?", [read_id])
    prev = db.one("SELECT event_id FROM vehicle_reads WHERE camera_id=? AND list=? AND list_entry=? AND event_id IS NOT NULL "
                  "AND ts >= ? AND id != ? ORDER BY ts DESC LIMIT 1",
                  [cam["id"], hit["list"], hit["id"], r["ts"] - LIST_COOLDOWN_S, read_id])
    if prev and db.event(prev["event_id"]):
        db.execute("UPDATE vehicle_reads SET event_id=? WHERE id=?", [prev["event_id"], read_id])
        return prev["event_id"]
    box = json.loads(r["box"]) if r["box"] else [0.0, 0.0, 1.0, 1.0]
    conf = r["confidence"] or 0.0
    shown = plate.display(r["plate"])
    words = {"deny": "on the deny list", "watch": "on the watch list"}[hit["list"]]
    text = (f"Plate {shown} is {words}" if hit["match"] == "exact"
            else f"Plate {shown} is one character off {plate.display(hit['plate'])}, {words}")
    text += f" ({hit['note']})" if hit.get("note") else ""
    policy = {"kind": "plate_list", "list": hit["list"], "match": hit["match"], "plate": r["plate"], "entry": hit["plate"],
              "priority": ALARM_LISTS[hit["list"]], "text": text}
    eid = db.create_event(
        camera_id=cam["id"], track_id=f"lpr:{r['plate']}-{int(r['ts'] * 1000)}", camera_class="vehicle", camera_conf=conf,
        start_ts=r["ts"] - 2, end_ts=r["ts"] + 2, path=[[round(r["ts"] - 2, 3), *box, conf], [round(r["ts"] + 2, 3), *box, conf]],
        rules=[{"ts": round(r["ts"], 3), "topic": "traffic/plate_list", "rule": hit["list"]}], status="verified",
        yolo_class="vehicle", yolo_hits=0, policy=policy, plate=r["plate"], plate_raw=r["plate_raw"], plate_region=r["region"],
        plate_conf=conf_word(conf),
        detections={"source": "lpr", "read_id": read_id, "plate_read": {"at": round(time.time(), 1), "source": "camera",
                                                                           "result": "read", "read_id": read_id, "confidence": conf}})
    images = json.loads(r["images"]) if r["images"] else {}
    src = images.get("full") or images.get("vehicle")
    if src and (settings.data_dir / src).is_file():
        snap = verifier.event_dir(eid) / "snapshot.jpg"
        shutil.copyfile(settings.data_dir / src, snap)
        db.update_event(eid, snapshot=str(snap.relative_to(settings.data_dir)).replace("\\", "/"))
    db.update_event(eid, priority=baseline.priority(db.event(eid), 0.0))
    db.execute("UPDATE vehicle_reads SET event_id=? WHERE id=?", [eid, read_id])
    log.info("[%s] plate %s %s (%s match, read %s): alarm event %s", cam["id"], r["plate"], words, hit["match"], read_id, eid)
    return eid


# ---------------------------------------------------------------- search and counts

def search(camera: str | None = None, since: float | None = None, until: float | None = None, q: str | None = None,
           vehicle_type: str | None = None, color: str | None = None, direction: str | None = None, list_: str | None = None,
           min_conf: float = 0.0, plates_only: bool = False, all_reads: bool = False, before_id: int | None = None,
           limit: int = 100, noise: bool = False) -> dict:
    """Reads newest first. q: a plate or part of one (letters and digits; 3+ finds it inside plates), with near plates
    (one character off) as `near`. Without all_reads only each pass's best read. Reads that aren't a vehicle are left
    out unless `noise` or a plate search (q)."""
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
    bits = [f"license plate {plate.display(r['plate'])} read by the camera's plate reader"
            f" ({round((r['confidence'] or 0) * 100)} % sure{'; state reported ' + r['region'] if r.get('region') else ''})"]
    what = " ".join(x for x in ((r.get("vehicle_color") or "").lower(), (r.get("vehicle_type") or "vehicle").lower()) if x)
    bits.append(f"a {what}" + (f" (looks like a {r['vehicle_brand']})" if r.get("vehicle_brand") else "")
                + (f", going {r['direction'] if r['direction'] == 'away' else 'toward the camera'} (reported)" if r.get("direction") else ""))
    if r.get("list"):
        bits.append(f"{'possibly ' if r.get('list_match') == 'near' else ''}on the {r['list']} list")
    return ", ".join(bits)


def ask_evidence(question: str, since: float | None, until: float | None, now: float | None = None) -> list[dict]:
    """Plate reads for an Ask question: the plates it names (and near ones), or, when it asks about plates without
    naming one ("tag of the white truck?"), the latest confident reads matching its colour and type words. Items:
    {kind: "plate", read_id, ts, camera_id, camera_name, plate, confidence, event_id, text}. Nothing without reads."""
    if not db.one("SELECT 1 AS x FROM vehicle_reads LIMIT 1"):
        return []
    from .assistant import PLATE_WORDS
    now = now or time.time()
    words = set(re.findall(r"[a-z0-9]+", question.lower()))
    plate_words = bool(words & PLATE_WORDS)
    tokens = plate_tokens(question, plate_words)
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
    names = {c["id"]: c["name"] for c in db.cameras()}
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: -r["ts"]):
        if r["id"] in seen or not r["plate"]:
            continue
        seen.add(r["id"])
        out.append({"kind": "plate", "read_id": r["id"], "ts": r["ts"], "camera_id": r["camera_id"],
                    "camera_name": names.get(r["camera_id"], r["camera_id"]), "plate": r["plate"],
                    "confidence": r["confidence"], "event_id": r["event_id"], "text": read_line(r)})
    return out[:ASK_LIMIT]


# ---------------------------------------------------------------- retention

def prune(keep_days: int | None = None, now: float | None = None) -> int:
    """Reads and their crops older than keep_days (an alarm event keeps its own copy of the picture). Returns rows deleted."""
    keep_days = keep_days if keep_days is not None else settings.traffic_keep_days
    cutoff = (now or time.time()) - keep_days * 86400
    n = db.execute("DELETE FROM vehicle_reads WHERE ts < ?", [cutoff]).rowcount or 0
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
