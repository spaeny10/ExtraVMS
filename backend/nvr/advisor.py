"""Optimize my system: measured facts -> plain suggestions.

Each check is a small function over a context dict of things the NVR already measures (stream bitrates,
codecs, disk growth, Qwen's VRAM and latency, event quality, zones, rules, PTZ time away, camera clocks).
A check returns Findings: what to change, why, the expected effect and either steps the operator follows
on the camera or an `apply` action the NVR performs itself. `summarize()` then has Qwen write the one-
paragraph briefing an operator reads first; without Qwen a deterministic paragraph is used.

Adding a lesson learned = adding a check. The context is gathered once per run (gather()), so checks stay
pure and unit-testable.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import statistics
import subprocess
import time
from collections import Counter
from dataclasses import asdict, dataclass, field

import httpx

from . import keep, retention
from .config import settings
from .db import db

log = logging.getLogger("nvr.advisor")

IMPACT = ("high", "medium", "low")
BITRATE_HIGH_MBPS = 3.2      # fallback when the stream could not be probed: above this a 1440p-class camera gains little
SHORT_EVENT_S = 1.5
FRAGMENT_GAP_S = 10.0
PARKED_MIN = 20          # parked-vehicle rejections in 24 h before the camera's VCA settings are worth changing
PROBE_TTL_S = 6 * 3600       # re-read a camera's resolution / frame rate from its newest recording this often
FPS_MAX = 10.0               # security recording above this buys little: the camera's analytics and YOLO both work at 10
# Bits per pixel per frame that keep a clean H.265 picture for YOLO and Qwen. Indexed by (scene, activity).
BPP_H265 = {("indoor", "quiet"): 0.025, ("indoor", "normal"): 0.035, ("indoor", "busy"): 0.045,
            ("outdoor", "quiet"): 0.040, ("outdoor", "normal"): 0.055, ("outdoor", "busy"): 0.075}
CODEC_FACTOR = {"hevc": 1.0, "h265": 1.0, "h264": 1.6, "avc": 1.6}
OUTDOOR_WORDS = ("yard", "lot", "parking", "road", "highway", "street", "drive", "gate", "outdoor", "outside", "exterior", "dock", "field")


@dataclass
class Finding:
    key: str                      # stable id, e.g. "bitrate:cam3"
    area: str                     # cameras | storage | ai | events | rules | ptz | time
    impact: str                   # high | medium | low
    title: str
    why: str                      # the measurement, in words
    effect: str                   # what improves
    steps: list[str] = field(default_factory=list)      # operator does this (camera web page etc.)
    apply: dict | None = None                            # {"action": ..., ...} the NVR can do itself
    fingerprint: str = ""         # when this changes, a dismissed finding comes back
    camera_id: str | None = None
    camera: str | None = None


# ---------------------------------------------------------------- gathering
async def gather(state) -> dict:
    """Everything the checks look at, fetched once. Missing pieces become None, never exceptions."""
    ctx: dict = {"now": time.time(), "cameras": db.cameras(enabled_only=True), "settings": settings}
    try:
        ctx["health"] = state.health.all() if getattr(state, "health", None) else {}
    except Exception:
        ctx["health"] = {}
    try:
        from . import mediamtx
        paths = await mediamtx.path_status()
        ctx["tracks"] = {name: [t for t in p.get("tracks", [])] for name, p in paths.items()}
    except Exception:
        ctx["tracks"] = {}
    try:
        ctx["retention"] = retention.stats()
    except Exception:
        ctx["retention"] = None
    ctx["vlm"] = await _vlm_facts(state)
    ctx["yolo"] = _yolo_facts(state)
    ctx["events"] = _event_facts(ctx["now"])
    ctx["probe"] = await asyncio.to_thread(_probe_streams, ctx["cameras"], ctx["now"])
    from .config import ROOT
    ctx["ppe_model_present"] = (ROOT / "models" / settings.ppe_model).exists()
    ctx["activity"] = _activity_facts(ctx["cameras"], ctx["events"], ctx["now"])
    ctx["ptz"] = _ptz_facts(state, ctx["now"])
    ctx["clocks"] = _clock_facts(state)
    try:
        from . import identities
        ctx["named"] = {k: {n["name"] for n in identities.named(k)} for k in ("person", "vehicle")}
    except Exception:
        ctx["named"] = {"person": set(), "vehicle": set()}
    try:
        ctx["baseline"] = db.all("SELECT camera_id, built_at FROM baselines") if db.one("SELECT name FROM sqlite_master WHERE name='baselines'") else []
    except Exception:
        ctx["baseline"] = []
    return ctx


def _yolo_facts(state) -> dict:
    """Which device YOLO verification runs on and how long it takes per frame (Verifier.frame_ms)."""
    v = getattr(getattr(state, "pipeline", None), "verifier", None)
    return {"device": getattr(v, "device", None) or settings.yolo_device, "model": settings.yolo_model,
            "frame_ms": list(getattr(v, "frame_ms", None) or [])}


async def _vlm_facts(state) -> dict:
    out: dict = {"ready": bool(getattr(getattr(state, "pipeline", None), "vlm_ready", False)), "size_gb": None, "vram_gb": None,
                 "queue": None, "latency_s": [], "calls": {}, "state": getattr(getattr(state, "pipeline", None), "vlm_state", "ready"),
                 "down_since": getattr(getattr(state, "pipeline", None), "vlm_down_since", None)}
    p = getattr(state, "pipeline", None)
    if p is not None:
        out["queue"] = p.synopsis_q.qsize()
        out["latency_s"] = list(getattr(p, "synopsis_times", []))
    try:
        from . import vlmroute
        out["calls"] = dict(getattr(vlmroute.router, "task_calls", {}))
        out["remote"] = vlmroute.router.configured
    except Exception:
        pass
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{settings.ollama_url}/api/ps")
            for m in r.json().get("models", []):
                if m["name"] == settings.vlm_model:
                    out["size_gb"] = round(m["size"] / 1e9, 2)
                    out["vram_gb"] = round(m["size_vram"] / 1e9, 2)
    except Exception:
        pass
    return out


def probe_segment(path) -> dict | None:
    """Resolution, average frame rate and codec of one recorded segment (ffprobe), or None."""
    exe = shutil.which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe"
    try:
        out = subprocess.run([exe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate,codec_name",
                              "-of", "json", str(path)], capture_output=True, text=True, timeout=20).stdout
        st = (json.loads(out).get("streams") or [None])[0]
        if not st or not st.get("width"):
            return None
        fps = None
        for key in ("avg_frame_rate", "r_frame_rate"):
            n, _, d = (st.get(key) or "").partition("/")
            if n and d and float(d) > 0 and 1 <= float(n) / float(d) <= 120:
                fps = round(float(n) / float(d), 1)
                break
        return {"width": int(st["width"]), "height": int(st["height"]), "fps": fps or 15.0, "codec": (st.get("codec_name") or "").lower()}
    except Exception as e:
        log.debug("ffprobe failed for %s: %s", path, e)
        return None


def _probe_streams(cameras: list[dict], now: float) -> dict[str, dict]:
    """Per camera: {width, height, fps, codec}, from the newest recording, cached in settings for PROBE_TTL_S."""
    cache = db.get_setting("advisor_probe", {}) or {}
    out: dict[str, dict] = {}
    changed = False
    for c in cameras:
        hit = cache.get(c["id"])
        if hit and now - hit.get("at", 0) < PROBE_TTL_S:
            out[c["id"]] = hit
            continue
        try:
            segs = retention.camera_segments(c["id"])
        except Exception:
            segs = []
        info = probe_segment(segs[-1][1]) if segs else None
        if info:
            info["at"] = now
            cache[c["id"]] = out[c["id"]] = info
            changed = True
        elif hit:
            out[c["id"]] = hit
    if changed:
        db.set_setting("advisor_probe", cache)
    return out


def _activity_facts(cameras: list[dict], events: dict, now: float) -> dict[str, dict]:
    """Per camera: how busy the scene is and whether it looks outdoors, from 24 h of events and the scene notes."""
    since = now - 86400
    out: dict[str, dict] = {}
    for c in cameras:
        rows = db.all("SELECT start_ts, end_ts, camera_class FROM events WHERE camera_id=? AND start_ts>? AND status IN ('verified','open','pending')", [c["id"], since])
        tracked_s = sum(min(600.0, max(0.0, (r["end_ts"] or r["start_ts"]) - r["start_ts"])) for r in rows)
        per_day = len(rows)
        share = tracked_s / 86400
        # "quiet" needs evidence: a camera with no events at all may be new, or its analytics may not be feeding us
        activity = "busy" if share > 0.05 or per_day > 150 else "quiet" if 1 <= per_day < 15 and share < 0.005 else "normal"
        notes = (c.get("scene_notes") or "").lower() + " " + (c.get("name") or "").lower()
        vehicles = sum(1 for r in rows if r["camera_class"] == "vehicle")
        indoor_words = ("kitchen", "office", "hall", "room", "bathroom", "lobby", "indoor", "inside", "warehouse", "shop floor")
        if vehicles >= 3 or any(w in notes for w in OUTDOOR_WORDS):
            scene = "outdoor"
        elif per_day >= 15 and any(w in notes for w in indoor_words):
            scene = "indoor"
        else:
            scene = "outdoor"   # unknown: the outdoor table asks for more bits, the safe direction
        out[c["id"]] = {"activity": activity, "scene": scene, "tracked_share": round(share, 4), "events_per_day": per_day}
    return out


def bitrate_target_mbps(probe: dict, activity: str, scene: str) -> float:
    """Bits per second that keep the picture clean for this camera's pixels, frame rate, codec and scene."""
    bpp = BPP_H265[(scene, activity)] * CODEC_FACTOR.get(probe.get("codec", "hevc"), 1.0)
    return round(bpp * probe["width"] * probe["height"] * probe["fps"] / 1e6, 2)


def _event_facts(now: float) -> dict:
    """Last 24 h per camera: totals, rejected share, very short events, unmerged fragments, away share.

    `fragments` counts back-to-back pairs the NVR *would* merge but could not: the camera gave the same track
    id, or re-ID says the same person, yet they are still two events. Different people passing the same spot
    in sequence are not fragments and don't count."""
    since = now - 86400
    rows = db.all("SELECT id, camera_id, camera_class, track_id, start_ts, end_ts, status, ptz_preset FROM events WHERE start_ts>? ORDER BY camera_id, start_ts", [since])
    out: dict[str, dict] = {}
    prev: dict[str, dict] = {}
    try:
        from . import merge
        reid_sim = merge.reid_sim
    except Exception:  # pragma: no cover
        reid_sim = lambda a, b: None  # noqa: E731
    for r in rows:
        f = out.setdefault(r["camera_id"], {"total": 0, "rejected": 0, "short": 0, "fragments": 0, "away": 0, "vehicles": 0, "verified": 0})
        f["total"] += 1
        if r["status"] == "rejected":
            f["rejected"] += 1
        if r["status"] == "verified":
            f["verified"] += 1
        if r["camera_class"] == "vehicle":
            f["vehicles"] += 1
        if r["ptz_preset"]:
            f["away"] += 1
        if r["end_ts"] and r["end_ts"] - r["start_ts"] < SHORT_EVENT_S:
            f["short"] += 1
        p = prev.get(r["camera_id"])
        if (p and p["camera_class"] == r["camera_class"] and p["status"] == r["status"] == "verified"
                and r["start_ts"] - (p["end_ts"] or p["start_ts"]) <= FRAGMENT_GAP_S):
            if p["track_id"] == r["track_id"]:
                f["fragments"] += 1
            elif r["camera_class"] == "person":
                sim = reid_sim(p["id"], r["id"])
                if sim is not None and sim >= settings.merge_reid_min:
                    f["fragments"] += 1
        prev[r["camera_id"]] = r
    # Vehicle events rejected because a parked vehicle "confirmed" motion next to it (verifier / parked.py)
    try:
        from .parked import REASON
        for r in db.all("SELECT camera_id, COUNT(*) AS n, json_extract(detections, '$.parked.box') AS box, MAX(start_ts) "
                        "FROM events WHERE start_ts>? AND status='rejected' AND json_extract(detections, '$.rejected')=? "
                        "GROUP BY camera_id", [since, REASON]):
            if r["camera_id"] in out:
                out[r["camera_id"]]["parked"] = r["n"]
                out[r["camera_id"]]["parked_box"] = json.loads(r["box"]) if r["box"] else None
    except Exception:
        log.exception("parked-vehicle facts failed")
    return out


def _ptz_facts(state, now: float) -> dict:
    out: dict = {}
    mgr = getattr(state, "ptz", None)
    cams = getattr(mgr, "cams", None) or getattr(mgr, "cameras", None) or {}
    for cid, p in dict(cams).items():
        try:
            if not p.available or not getattr(p, "pan_tilt", True):
                continue
            cfg = p.cfg
            away_s = 0.0
            moves = db.all("SELECT ts, at_home FROM ptz_moves WHERE camera_id=? AND ts>? ORDER BY ts", [cid, now - 86400])
            last_ts, last_home = now - 86400, True
            for m in moves:
                if not last_home:
                    away_s += m["ts"] - last_ts
                last_ts, last_home = m["ts"], bool(m["at_home"])
            if not last_home:
                away_s += now - last_ts
            out[cid] = {"home_token": cfg.get("home_token"), "return_home_min": cfg.get("return_home_min"), "away_s_24h": round(away_s)}
        except Exception:
            continue
    return out


def _clock_facts(state) -> dict:
    out = {}
    for cid, ing in dict(getattr(state, "ingests", {}) or {}).items():
        try:
            st = ing.status()
            if st.get("clock_offset") is not None:
                out[cid] = st["clock_offset"]
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- checks
def _cam_name(ctx: dict, cid: str) -> str:
    return next((c["name"] for c in ctx["cameras"] if c["id"] == cid), cid)


def check_bitrate(ctx: dict) -> list[Finding]:
    """Measured bitrate against a target from the camera's pixels, frame rate, codec, scene and activity."""
    out = []
    for c in ctx["cameras"]:
        h = ctx["health"].get(c["id"]) or {}
        mbps = h.get("bitrate_mbps")
        if mbps is None:
            continue
        gbd = h.get("gb_per_day")
        probe = (ctx.get("probe") or {}).get(c["id"])
        act = (ctx.get("activity") or {}).get(c["id"]) or {"activity": "normal", "scene": "indoor", "events_per_day": 0}
        if not probe:
            if mbps >= BITRATE_HIGH_MBPS:
                out.append(Finding(key=f"bitrate:{c['id']}", area="cameras", impact="low", camera_id=c["id"], camera=c["name"],
                                   title=f"{c['name']} records at {mbps:.1f} Mbps (resolution unknown)",
                                   why=f"Its main stream averages {mbps:.1f} Mbps; the recording could not be probed yet, so this is a rough flag.",
                                   effect="Probably less disk and remote bandwidth; check again once a recording exists.",
                                   steps=[f"Camera web page ({c['host']}) → Video → main stream: VBR, and compare with cameras of the same resolution."], fingerprint=f"{mbps:.0f}"))
            continue
        target = bitrate_target_mbps(probe, act["activity"], act["scene"])
        desc = f"{probe['width']}×{probe['height']} at {probe['fps']:.0f} fps, {'H.265' if probe['codec'] in ('hevc', 'h265') else 'H.264' if probe['codec'] in ('h264', 'avc') else probe['codec']}, {act['scene']}, {act['activity']} scene ({act['events_per_day']} events a day)"
        if mbps >= target * 1.4 and mbps >= 1.0:
            lo, hi = round(target * 0.9, 1), round(target * 1.15, 1)
            out.append(Finding(
                key=f"bitrate:{c['id']}", area="cameras", impact="medium" if mbps >= target * 1.8 else "low", camera_id=c["id"], camera=c["name"],
                title=f"{c['name']} records at {mbps:.1f} Mbps; about {target:.1f} would do",
                why=f"{desc}. That picture stays clean for YOLO and Qwen at about {target:.1f} Mbps; the camera sends {mbps:.1f}" + (f", about {gbd:.0f} GB a day" if gbd else "") + ".",
                effect=f"Roughly {(1 - target / mbps) * 100:.0f}% less disk per day and the same saving in remote playback bandwidth, with no change to detection: the camera's analytics work on the sensor image, and YOLO verifies from a picture this rate keeps sharp.",
                steps=[f"Open the camera's web page ({c['host']}) → Video → main stream.", f"Set the bitrate mode to VBR with a target of {lo}–{hi} Mbps (keep the resolution, frame rate and codec).",
                       "Save; MediaMTX picks up the new stream within a few seconds. Check the live picture for blockiness on a busy moment."],
                fingerprint=f"{mbps:.0f}:{target:.0f}"))
        elif mbps < target * 0.5 and act["activity"] == "busy":
            out.append(Finding(
                key=f"bitrate-low:{c['id']}", area="cameras", impact="medium", camera_id=c["id"], camera=c["name"],
                title=f"{c['name']} is starved at {mbps:.1f} Mbps",
                why=f"{desc}. A busy scene this size wants about {target:.1f} Mbps; at {mbps:.1f} fast movement smears, which costs YOLO confirmations and Qwen detail.",
                effect="Sharper clips and fewer missed verifications on busy moments.",
                steps=[f"Camera web page ({c['host']}) → Video → main stream: raise the VBR target toward {target:.1f} Mbps."],
                fingerprint=f"{mbps:.0f}:{target:.0f}"))
    return out


def check_framerate(ctx: dict) -> list[Finding]:
    """Recording above FPS_MAX costs bits and disk for smoother motion nobody reviews at full speed."""
    out = []
    for c in ctx["cameras"]:
        probe = (ctx.get("probe") or {}).get(c["id"])
        if not probe or probe.get("fps") is None or probe["fps"] <= FPS_MAX + 0.5:
            continue
        fps = probe["fps"]
        h = ctx["health"].get(c["id"]) or {}
        mbps = h.get("bitrate_mbps")
        saving = 1 - (FPS_MAX / fps) ** 0.7   # bitrate does not fall linearly with frame rate; ~40% for 16 -> 8
        out.append(Finding(
            key=f"fps:{c['id']}", area="cameras", impact="medium" if fps >= 20 else "low", camera_id=c["id"], camera=c["name"],
            title=f"{c['name']} records at {fps:.0f} fps; {FPS_MAX:.0f} is enough",
            why=f"The main stream runs at {fps:.0f} frames a second" + (f" and {mbps:.1f} Mbps" if mbps else "") + f". The camera's own analytics, YOLO verification and Qwen all work from a few frames per event; {FPS_MAX:.0f} fps keeps every walking step and every passing vehicle.",
            effect=f"About {saving * 100:.0f}% less bitrate at the same quality setting, or a sharper picture at the same bitrate, plus lighter live decoding in browsers.",
            steps=[f"Open the camera's web page ({c['host']}) → Video → main stream.", f"Set the frame rate to {FPS_MAX:.0f} fps and set the I-frame (GOP) interval to about 2 s ({FPS_MAX * 2:.0f} frames).",
                   "Save; the stream reconnects on its own. The bitrate card, if any, is recalculated for the new frame rate."],
            fingerprint=f"{fps:.0f}"))
    return out


def check_codecs(ctx: dict) -> list[Finding]:
    out = []
    for c in ctx["cameras"]:
        tracks = ctx["tracks"].get(c["id"]) or []
        if not tracks:
            continue
        probe = (ctx.get("probe") or {}).get(c["id"]) or {}
        if ("H264" in tracks and "H265" not in tracks) or probe.get("codec") in ("h264", "avc"):
            out.append(Finding(key=f"codec:{c['id']}", area="cameras", impact="low", camera_id=c["id"], camera=c["name"],
                               title=f"{c['name']} records H.264", why="The main stream is H.264; H.265 halves the bytes for the same picture and this NVR records and plays it.",
                               effect="About half the disk and remote playback bandwidth for this camera.",
                               steps=[f"Camera web page ({c['host']}) → Video → main stream → codec H.265 (if offered).", "Leave the sub stream on H.264: browsers play it live."],
                               fingerprint="h264"))
        audio = [t for t in tracks if t not in ("H264", "H265", "Generic", "MJPEG", "VP8", "VP9", "AV1")]
        if any(a.startswith("MPEG-4 Audio") or a == "AAC" for a in audio):
            out.append(Finding(key=f"audio:{c['id']}", area="cameras", impact="low", camera_id=c["id"], camera=c["name"],
                               title=f"{c['name']} sends AAC audio the browser can't play live",
                               why="The stream's audio is AAC. WebRTC live view can only play G.711, G.722 or Opus, so the speaker button never appears for this camera.",
                               effect="Listen-only audio on its live tile.",
                               steps=[f"Camera web page ({c['host']}) → Audio → encoding G.711 (µ-law or A-law).", "The stream reconnects on its own."],
                               fingerprint="aac"))
        if f"{c['id']}_sub" in ctx["tracks"] and not ctx["tracks"][f"{c['id']}_sub"] and ctx["health"].get(c["id"], {}).get("sub_bitrate_mbps") is None:
            pass  # sub stream only pulled while watched: silence is normal
    return out


def check_storage(ctx: dict) -> list[Finding]:
    r = ctx.get("retention")
    if not r:
        return []
    out = []
    gbd = sum(c["gb_per_day"] for c in r["cameras"])
    days = keep.site_policy().get("continuous_days", 0)
    free = r["disk"]["free_gb"]
    if gbd > 0:
        needed = gbd * days
        used = sum(c["continuous_gb"] + c["kept_gb"] for c in r["cameras"])
        headroom_days = (free + used) / gbd if gbd else 0
        if needed > (free + used) * 0.9:
            out.append(Finding(key="storage:short", area="storage", impact="high",
                               title=f"The disk holds about {headroom_days:.0f} days but the policy asks for {days}",
                               why=f"Cameras write about {gbd:.0f} GB a day; {days} days needs {needed:.0f} GB and the recording disk offers {free + used:.0f} GB.",
                               effect="Retention keeps its promise instead of the oldest continuous footage vanishing early.",
                               steps=["Lower the camera bitrates (see the camera suggestions) or the continuous days in Settings → System → Retention, or add disk."],
                               apply=None, fingerprint=f"{days}:{gbd:.0f}"))
        elif headroom_days > days * 3 and days < 60:
            out.append(Finding(key="storage:slack", area="storage", impact="low",
                               title=f"Room for about {headroom_days:.0f} days of continuous recording; the policy keeps {days}",
                               why=f"At {gbd:.0f} GB a day the disk could hold {headroom_days:.0f} days.",
                               effect="More footage to scrub back through, at no cost.",
                               steps=[f"Settings → System → Retention: raise continuous days toward {min(int(headroom_days * 0.7), 90)}."],
                               apply={"action": "retention_days", "days": min(int(headroom_days * 0.7), 90)}, fingerprint=f"{days}"))
    if r.get("alert"):
        out.append(Finding(key="storage:alert", area="storage", impact="high", title="Retention can't hold the continuous window",
                           why=str(r["alert"]), effect="Footage stops disappearing before its time.",
                           steps=["Lower bitrates, shorten the continuous window, or add disk."], fingerprint="alert"))
    return out


def check_vlm(ctx: dict) -> list[Finding]:
    v = ctx["vlm"]
    out = []
    if v.get("state") == "unresponsive":
        since = v.get("down_since")
        mins = (ctx["now"] - since) / 60 if since else 0
        out.append(Finding(key="ai:down", area="ai", impact="high", title="Qwen has stopped answering",
                           why=f"Synopses have been timing out for {mins:.0f} min; the NVR is restarting Ollama every 5 minutes without success.",
                           effect="Synopses, Ask, briefings and journeys come back.",
                           steps=["Run nvidia-smi: if it says a GPU is lost, reboot the machine.", "Otherwise check that nothing else is using Qwen's GPU memory."],
                           fingerprint=f"{int(mins // 15)}"))
    if v.get("size_gb") and v.get("vram_gb") is not None and v["vram_gb"] < v["size_gb"] * 0.98:
        out.append(Finding(key="ai:vram", area="ai", impact="high", title="Qwen is partly running on the CPU",
                           why=f"Ollama holds {v['vram_gb']} of {v['size_gb']} GB of the model in VRAM; the rest runs on the CPU, which makes every synopsis several times slower.",
                           effect="Synopses in seconds instead of minutes; the queue keeps up.",
                           steps=["Free VRAM on Qwen's GPU: close GPU-heavy desktop apps, or move the monitor to the other card.", "Lower vlm_num_ctx in the site .env if it stays short."],
                           fingerprint=f"{v['vram_gb']}"))
    lat = v.get("latency_s") or []
    if len(lat) >= 5:
        med = statistics.median(lat)
        if med > 60:
            out.append(Finding(key="ai:slow", area="ai", impact="high", title=f"Synopses take {med:.0f} s each",
                               why=f"The median of the last {len(lat)} synopses is {med:.0f} s; a resident 7-9B model does them in 10–30 s.",
                               effect="The event feed catches up and Ask answers promptly.",
                               steps=["Check the VRAM finding above; if the model is resident, look at what else uses Qwen (below)."],
                               fingerprint=f"{int(med // 30)}"))
    calls = v.get("calls") or {}
    total = sum(calls.values())
    if total >= 50:
        top = Counter(calls).most_common(1)[0]
        if top[0] != "synopsis" and top[1] / total > 0.4:
            out.append(Finding(key=f"ai:mix:{top[0]}", area="ai", impact="medium", title=f"{top[1] / total:.0%} of Qwen's work is '{top[0]}'",
                               why=f"Of {total} Qwen calls since start-up, {top[1]} were {top[0]} and only {calls.get('synopsis', 0)} synopses.",
                               effect="More of Qwen's time goes to describing events.",
                               steps=["Journey checks: raise REID_AUTO_SIM so more links are confirmed by re-ID alone." if top[0] in ("same_person", "journey") else "Review what triggers this task."],
                               fingerprint=top[0]))
    q = v.get("queue") or 0
    if q > 40:
        out.append(Finding(key="ai:queue", area="ai", impact="medium", title=f"{q} events are waiting for Qwen",
                           why="The synopsis queue is deep; new events wait behind old ones.",
                           effect="Fresh events get described within a minute.",
                           steps=["Usually a burst from a PTZ camera turned toward a road, or Qwen running slow (see above)."], fingerprint=f"{q // 40}"))
    return out


YOLO_SLOW_MS = {"cpu": 400.0, "hailo": 150.0, "cuda": 150.0}  # median per verified frame before it is worth a finding


def check_yolo(ctx: dict) -> list[Finding]:
    y = ctx.get("yolo") or {}
    ms = y.get("frame_ms") or []
    if len(ms) < 5:
        return []
    med = statistics.median(ms)
    dev = str(y.get("device") or "cpu")
    kind = "hailo" if dev.startswith("hailo") else "cuda" if dev.startswith("cuda") else "cpu"
    if med <= YOLO_SLOW_MS[kind]:
        return []
    frames = settings.verify_frames
    if kind == "cpu":
        return [Finding(key="ai:yolo_slow", area="ai", impact="medium", title=f"YOLO takes {med:.0f} ms a frame on the CPU",
                        why=f"Verification runs {y.get('model')} on the processor: the median of the last {len(ms)} checks is {med:.0f} ms a frame, "
                            f"about {med * frames / 1000:.1f} s of YOLO per event ({frames} frames), and the CPU is busy recording at the same time.",
                        effect="Verification in a few milliseconds a frame (Hailo-8: ~10 ms with yolov11s), leaving the CPU to recording and decoding.",
                        steps=["Fit a Hailo-8 M.2/PCIe accelerator and run tools/hailo_setup.sh (or deploy_site.sh --hailo); it switches YOLO to the Hailo.",
                               "Or an NVIDIA GPU: NVR_YOLO_DEVICE=cuda:0.", "Meanwhile NVR_YOLO_IMGSZ=640 and NVR_VERIFY_FRAMES=4 keep it down."],
                        fingerprint=f"{int(med // 200)}")]
    return [Finding(key="ai:yolo_slow", area="ai", impact="low", title=f"YOLO takes {med:.0f} ms a frame on {dev}",
                    why=f"The median of the last {len(ms)} checks is {med:.0f} ms a frame on {dev} ({y.get('model')}); this accelerator should need "
                        f"well under {YOLO_SLOW_MS[kind]:.0f} ms, so the time goes elsewhere (the host resizing 4K frames, or the device shared with another process).",
                    effect="Events verified sooner after they end.",
                    steps=["hailortcli monitor shows whether another process holds the Hailo." if kind == "hailo" else "nvidia-smi shows what else uses the GPU.",
                           "Check the box's CPU load: frame decoding and resizing run there."],
                    fingerprint=f"{kind}:{int(med // 100)}")]


def check_events(ctx: dict) -> list[Finding]:
    out = []
    for cid, f in ctx["events"].items():
        name = _cam_name(ctx, cid)
        parked = f.get("parked", 0)
        if parked >= PARKED_MIN:
            box = f.get("parked_box")
            spot = (f" The vehicle sits at about {box[0]:.0%}–{box[2]:.0%} across and {box[1]:.0%}–{box[3]:.0%} down the picture."
                    if box else "")
            out.append(Finding(key=f"events:parked:{cid}", area="events", impact="medium", camera_id=cid, camera=name,
                               title=f"{name}: {parked} events were a parked vehicle",
                               why=f"In 24 h the camera reported {parked} moving vehicles where YOLO saw a vehicle that never moved; the camera's motion boxes were small movements next to it (shimmer, shadows, a flapping strap). The NVR rejects these, but each one still costs a clip and a YOLO check.{spot}",
                               effect="Fewer wasted clips and verifications; real arrivals and departures stand out.",
                               steps=[f"Camera web page ({next((c['host'] for c in ctx['cameras'] if c['id'] == cid), '')}) → Event / Smart analytics (VCA): raise the minimum object size for vehicles so small boxes are ignored.",
                                      "Or exclude the parking area from the camera's detection region (the vehicle is still recorded; driving in or out of the area still starts an event)."],
                               fingerprint=f"{parked // 20}"))
        rej = f["rejected"] - parked  # parked-vehicle rejections have their own finding
        if f["total"] >= 20 and rej / f["total"] > 0.5:
            out.append(Finding(key=f"events:rejected:{cid}", area="events", impact="medium", camera_id=cid, camera=name,
                               title=f"{name}: {rej / f['total']:.0%} of detections are rejected by YOLO",
                               why=f"{rej} of {f['total']} camera detections in 24 h were not confirmed. The camera's own analytics are firing on something YOLO doesn't see (shadows, rain, reflections).",
                               effect="Fewer wasted clips and verifications; a cleaner timeline.",
                               steps=[f"Camera web page ({next((c['host'] for c in ctx['cameras'] if c['id'] == cid), '')}) → Event / Smart analytics: raise the sensitivity threshold or shrink the detection area.",
                                      "Or paint an exclude zone in Settings → Cameras → Zones over the trigger area."],
                               fingerprint=f"{int(rej / f['total'] * 10)}"))
        if f["total"] >= 20 and f["fragments"] >= 8 and f["fragments"] / f["total"] > 0.15:
            out.append(Finding(key=f"events:fragments:{cid}", area="events", impact="low", camera_id=cid, camera=name,
                               title=f"{name} splits visits into many short events",
                               why=f"{f['fragments']} of {f['total']} events in 24 h are the same person (or the same camera track) re-appearing within {FRAGMENT_GAP_S:.0f} s, still recorded as separate events. The NVR merges fragments that continue where the last one ended (and, for people, longer gaps when the recording shows them still standing there); these re-appeared somewhere else in the frame.",
                               effect="One event per visit: fewer cards, one synopsis, cleaner journeys.",
                               steps=["Camera web page → analytics / object tracking: raise the 'object lost' or 'disappear' tolerance to 5–10 s if offered.",
                                      "Or lower the minimum object size so the person isn't dropped when partly hidden."],
                               fingerprint=f"{int(f['fragments'] / f['total'] * 10)}"))
        if f["vehicles"] >= 40 and not any(z.get("type") == "include" for z in (next((c for c in ctx["cameras"] if c["id"] == cid), {}).get("zones") or [])):
            out.append(Finding(key=f"zones:{cid}", area="rules", impact="medium", camera_id=cid, camera=name,
                               title=f"{name} has no include zone and saw {f['vehicles']} vehicles in 24 h",
                               why="Without an include zone every passing vehicle on a public road behind the property becomes an event and a Qwen call.",
                               effect="Only vehicles on the property are recorded as events; Qwen's load drops.",
                               steps=["Settings → Cameras → Edit → Zones: draw an include zone around the property, leaving the road out."],
                               fingerprint=f"{f['vehicles'] // 40}"))
    return out


def check_rules(ctx: dict) -> list[Finding]:
    out = []
    for c in ctx["cameras"]:
        for r in c.get("policies") or []:
            kind = "vehicle" if r.get("kind") == "towing" else "person"
            missing = [n for n in r.get("allowed") or [] if n not in ctx["named"].get(kind, set())]
            if missing:
                out.append(Finding(key=f"rule:{c['id']}:{r.get('kind')}", area="rules", impact="high", camera_id=c["id"], camera=c["name"],
                                   title=f"{c['name']}: rule allows {', '.join(missing)}, who isn't named yet",
                                   why="A site rule can only recognize names from People & vehicles. Until this name exists every match trips the rule.",
                                   effect="The rule stops flagging your own people or trucks.",
                                   steps=[f"Find → Grouped by who → name the {kind} '{missing[0]}'.", "Reprocess a recent flagged event to confirm."],
                                   fingerprint=",".join(missing)))
    return out


def check_ppe(ctx: dict) -> list[Finding]:
    """A PPE zone is painted but the PPE detector's weights aren't on disk: nobody in it is being checked."""
    from . import ppe
    if ctx.get("ppe_model_present", True):
        return []
    cams = [c for c in ctx["cameras"] if ppe.ppe_zones(c.get("zones"))]
    if not cams:
        return []
    names = ", ".join(c["name"] for c in cams)
    return [Finding(key="ppe:model", area="rules", impact="high",
                    title=f"PPE zone{'s' if len(cams) > 1 else ''} on {names} but the PPE detector is missing",
                    why=f"models/{settings.ppe_model} is not on disk, so people in the PPE-required zones are not checked "
                        "for hard hats or hi-vis vests.",
                    effect="Hard hat / hi-vis vest violations show up as medium-priority events again.",
                    steps=[f"Copy the PPE model to models/{settings.ppe_model} (or set NVR_PPE_MODEL to the file you have) "
                           "and restart the NVR."],
                    fingerprint=settings.ppe_model)]


def check_ptz(ctx: dict) -> list[Finding]:
    out = []
    for cid, p in ctx["ptz"].items():
        name = _cam_name(ctx, cid)
        if not p.get("home_token"):
            out.append(Finding(key=f"ptz:home:{cid}", area="ptz", impact="medium", camera_id=cid, camera=name,
                               title=f"{name} has no home view",
                               why="Zones, named places, site rules and what's-normal all describe one view. Without a home preset they apply wherever the camera happens to point.",
                               effect="Analytics stay honest when someone turns the camera.",
                               steps=["Settings → Cameras → Edit → PTZ: point the camera at the usual view and press 'Set current position as home'."],
                               fingerprint="nohome"))
        elif p.get("away_s_24h", 0) > 3600 and not p.get("return_home_min"):
            out.append(Finding(key=f"ptz:return:{cid}", area="ptz", impact="medium", camera_id=cid, camera=name,
                               title=f"{name} spent {p['away_s_24h'] / 3600:.1f} h away from home with no return timer",
                               why="While turned away, events are recorded but zones and rules don't apply, and a road view floods the timeline with traffic.",
                               effect="The camera comes back on its own after a look around.",
                               steps=["Settings → Cameras → Edit → PTZ → 'Return home after' 5 minutes."],
                               apply={"action": "ptz_return_home", "camera_id": cid, "minutes": 5}, fingerprint=f"{p['away_s_24h'] // 3600}"))
    return out


def check_clocks(ctx: dict) -> list[Finding]:
    out = []
    for cid, off in ctx["clocks"].items():
        if abs(off) > 20:
            name = _cam_name(ctx, cid)
            out.append(Finding(key=f"clock:{cid}", area="time", impact="low", camera_id=cid, camera=name,
                               title=f"{name}'s clock is {abs(off):.0f} s {'behind' if off > 0 else 'ahead'}",
                               why="The NVR corrects detection times against its own clock, but a big drift makes the camera's on-screen time and exported clips disagree with the timeline.",
                               effect="Camera OSD, events and recordings agree to the second.",
                               steps=[f"Camera web page ({next((c['host'] for c in ctx['cameras'] if c['id'] == cid), '')}) → System → Time: enable NTP pointing at this NVR or your router."],
                               fingerprint=f"{int(abs(off) // 20)}"))
    return out


CHECKS = [check_bitrate, check_framerate, check_codecs, check_storage, check_vlm, check_yolo, check_events, check_rules, check_ppe, check_ptz,
          check_clocks]


def run_checks(ctx: dict) -> list[Finding]:
    out: list[Finding] = []
    for fn in CHECKS:
        try:
            out.extend(fn(ctx))
        except Exception:  # one broken check must not hide the others
            log.exception("advisor check %s failed", fn.__name__)
    out.sort(key=lambda f: (IMPACT.index(f.impact), f.area, f.title))
    return out


# ---------------------------------------------------------------- dismissals
def dismissed() -> dict:
    return db.get_setting("advisor_dismissed", {}) or {}


def dismiss(key: str, fingerprint: str) -> None:
    d = dismissed()
    d[key] = fingerprint
    db.set_setting("advisor_dismissed", d)


def undismiss(key: str) -> None:
    d = dismissed()
    d.pop(key, None)
    db.set_setting("advisor_dismissed", d)


def visible(findings: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    """(shown, hidden): a dismissed finding stays hidden while its measurement fingerprint is unchanged."""
    d = dismissed()
    shown, hidden = [], []
    for f in findings:
        (hidden if d.get(f.key) == f.fingerprint else shown).append(f)
    return shown, hidden


# ---------------------------------------------------------------- the briefing
def plain_summary(findings: list[Finding], n_cameras: int) -> str:
    if not findings:
        return f"All {n_cameras} cameras, storage and the AI are within their comfortable ranges. Nothing to change right now."
    high = [f for f in findings if f.impact == "high"]
    parts = []
    if high:
        parts.append(f"{len(high)} thing{'s' if len(high) > 1 else ''} need attention: " + "; ".join(f.title.rstrip(".") for f in high) + ".")
    rest = [f for f in findings if f.impact != "high"]
    if rest:
        parts.append(f"{len(rest)} smaller improvement{'s' if len(rest) > 1 else ''} would save disk, bandwidth or Qwen time.")
    return " ".join(parts)


async def summarize(findings: list[Finding], n_cameras: int, use_ai: bool = True) -> dict:
    """{text, model}: Qwen's one-paragraph read of the findings, or the plain summary."""
    fallback = {"text": plain_summary(findings, n_cameras), "model": None}
    if not use_ai or not findings:
        return fallback
    try:
        from . import vlmroute
        system = ("You are the resident engineer for a small camera security system. You get a JSON list of measured findings "
                  "about it. Write ONE short paragraph (3-5 sentences) for the owner: lead with what matters most and why, "
                  "mention the expected gain in plain terms, group small items together. No bullet points, no headings, no "
                  "markdown, no invented facts beyond the findings. Use American English spelling.")
        text = json.dumps([{k: v for k, v in asdict(f).items() if k in ("impact", "title", "why", "effect")} for f in findings])
        r = await asyncio.wait_for(vlmroute.router.chat_json("assistant", system, text, [], {"type": "object", "properties": {"paragraph": {"type": "string"}}, "required": ["paragraph"]}, 220, 0.2, "chat"), 40)
        p = (r or {}).get("paragraph", "").strip()
        return {"text": p, "model": r.get("_model")} if p else fallback
    except Exception as e:
        log.info("advisor summary fell back to plain text: %s", e)
        return fallback


async def report(state, use_ai: bool = True) -> dict:
    ctx = await gather(state)
    findings = run_checks(ctx)
    shown, hidden = visible(findings)
    summary = await summarize(shown, len(ctx["cameras"]), use_ai)
    return {"generated_at": ctx["now"], "cameras": len(ctx["cameras"]), "summary": summary,
            "findings": [asdict(f) for f in shown], "hidden": [asdict(f) for f in hidden],
            "facts": {"gb_per_day": round(sum(c["gb_per_day"] for c in (ctx["retention"] or {}).get("cameras", [])), 1) if ctx.get("retention") else None,
                      "vlm": {k: v for k, v in ctx["vlm"].items() if k in ("size_gb", "vram_gb", "queue")},
                      "median_synopsis_s": round(statistics.median(ctx["vlm"]["latency_s"]), 1) if len(ctx["vlm"].get("latency_s") or []) >= 3 else None,
                      "yolo": {"device": ctx["yolo"]["device"], "model": ctx["yolo"]["model"],
                               "median_frame_ms": round(statistics.median(ctx["yolo"]["frame_ms"]), 1) if ctx["yolo"]["frame_ms"] else None}}}
