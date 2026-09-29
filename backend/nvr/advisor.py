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
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass, field

import httpx

from . import keep, retention
from .config import settings
from .db import db

log = logging.getLogger("nvr.advisor")

IMPACT = ("high", "medium", "low")
BITRATE_HIGH_MBPS = 3.2      # above this an indoor/yard camera gains little
BITRATE_TARGET = "2–2.5 Mbps, VBR"
SHORT_EVENT_S = 1.5
FRAGMENT_GAP_S = 10.0


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
    ctx["events"] = _event_facts(ctx["now"])
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
    out = []
    for c in ctx["cameras"]:
        h = ctx["health"].get(c["id"]) or {}
        mbps = h.get("bitrate_mbps")
        if mbps is None or mbps < BITRATE_HIGH_MBPS:
            continue
        gbd = h.get("gb_per_day")
        out.append(Finding(
            key=f"bitrate:{c['id']}", area="cameras", impact="medium", camera_id=c["id"], camera=c["name"],
            title=f"{c['name']} records at {mbps:.1f} Mbps",
            why=f"Its main stream averages {mbps:.1f} Mbps" + (f", about {gbd:.0f} GB a day" if gbd else "") + ". Indoor and yard scenes at this resolution look the same at {BITRATE_TARGET}.".replace("{BITRATE_TARGET}", BITRATE_TARGET),
            effect=f"Roughly {max(0, (1 - 2.3 / mbps) * 100):.0f}% less disk per day and the same saving in remote playback bandwidth. Detection and Qwen are unaffected: they read frames, not bitrate.",
            steps=[f"Open the camera's web page ({c['host']}) → Video → main stream.", f"Set the bitrate mode to VBR and the target to {BITRATE_TARGET} (keep the frame rate and H.265).",
                   "Save; MediaMTX picks up the new stream within a few seconds."],
            fingerprint=f"{mbps:.0f}"))
    return out


def check_codecs(ctx: dict) -> list[Finding]:
    out = []
    for c in ctx["cameras"]:
        tracks = ctx["tracks"].get(c["id"]) or []
        if not tracks:
            continue
        if "H264" in tracks and "H265" not in tracks:
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
                               why=f"The median of the last {len(lat)} synopses is {med:.0f} s; a resident 7B model does them in 10–30 s.",
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


def check_events(ctx: dict) -> list[Finding]:
    out = []
    for cid, f in ctx["events"].items():
        name = _cam_name(ctx, cid)
        if f["total"] >= 20 and f["rejected"] / f["total"] > 0.5:
            out.append(Finding(key=f"events:rejected:{cid}", area="events", impact="medium", camera_id=cid, camera=name,
                               title=f"{name}: {f['rejected'] / f['total']:.0%} of detections are rejected by YOLO",
                               why=f"{f['rejected']} of {f['total']} camera detections in 24 h were not confirmed. The camera's own analytics are firing on something YOLO doesn't see (shadows, rain, reflections).",
                               effect="Fewer wasted clips and verifications; a cleaner timeline.",
                               steps=[f"Camera web page ({next((c['host'] for c in ctx['cameras'] if c['id'] == cid), '')}) → Event / Smart analytics: raise the sensitivity threshold or shrink the detection area.",
                                      "Or paint an exclude zone in Settings → Cameras → Zones over the trigger area."],
                               fingerprint=f"{int(f['rejected'] / f['total'] * 10)}"))
        if f["total"] >= 20 and f["fragments"] >= 8 and f["fragments"] / f["total"] > 0.15:
            out.append(Finding(key=f"events:fragments:{cid}", area="events", impact="low", camera_id=cid, camera=name,
                               title=f"{name} splits visits into many short events",
                               why=f"{f['fragments']} of {f['total']} events in 24 h are the same person (or the same camera track) re-appearing within {FRAGMENT_GAP_S:.0f} s, still recorded as separate events. The NVR merges fragments that continue where the last one ended; these re-appeared somewhere else in the frame.",
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
                                   why="A site rule can only recognise names from People & vehicles. Until this name exists every match trips the rule.",
                                   effect="The rule stops flagging your own people or trucks.",
                                   steps=[f"Find → Grouped by who → name the {kind} '{missing[0]}'.", "Reprocess a recent flagged event to confirm."],
                                   fingerprint=",".join(missing)))
    return out


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


CHECKS = [check_bitrate, check_codecs, check_storage, check_vlm, check_events, check_rules, check_ptz, check_clocks]


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
                  "markdown, no invented facts beyond the findings.")
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
                      "median_synopsis_s": round(statistics.median(ctx["vlm"]["latency_s"]), 1) if len(ctx["vlm"].get("latency_s") or []) >= 3 else None}}
