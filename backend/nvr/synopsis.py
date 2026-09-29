"""Qwen-VL scene synopsis via a dedicated Ollama server, plus text embeddings for search."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import json
import logging
import os
import re
import time

import httpx

from . import vlmroute
from .config import settings

log = logging.getLogger("nvr.synopsis")

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "1-3 sentence factual description of the event"},
        "objects": {"type": "array", "items": {"type": "object", "properties": {
            "type": {"type": "string"}, "description": {"type": "string"}}, "required": ["type", "description"]}},
        "activity": {"type": "string"},
        "threat_level": {"type": "string", "enum": ["none", "low", "medium", "high"]},
        "threat_reason": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "towing": {"type": "boolean", "description": "a vehicle is pulling or hitched to a trailer/equipment"},
        "towed": {"type": "string", "description": "what is being towed, if anything"},
    },
    "required": ["summary", "objects", "activity", "threat_level", "tags", "towing"],
}

SYSTEM = (
    "You are the analyst for an AI security camera system. You receive frames from a verified detection: "
    "a wide shot (amber box = camera detection, green box = verified object) and close-up crops in time order. "
    "Describe only what is visible. Be specific about people (clothing colors, what they are doing) "
    "and vehicles (type, color, make if clearly visible, direction). Do not guess identities. "
    "Small objects (phones, cups, bags, tools) are usually a few pixels in these crops: name one only when it is "
    "unmistakable; otherwise say nothing about the hands rather than guess. "
    "threat_level: none = routine (passer-by, resident, delivery), low = unusual but benign, "
    "medium = suspicious (loitering, checking doors/cars, face concealed at night), high = clear criminal or dangerous act. "
    "A 'Location (confirmed by the NVR)' line says where the object really is and overrides general scene notes "
    "about background traffic. For vehicles set towing=true only when the vehicle is pulling or hitched to a trailer "
    "or equipment (and name it in towed); parked trailers nearby do not count."
)


def describe_motion(path: list) -> str:
    if len(path) < 2:
        return "stationary"
    (x0, y0), (x1, y1) = [((p[1] + p[3]) / 2, p[4]) for p in (path[0], path[-1])]
    dx, dy = x1 - x0, y1 - y0
    parts = []
    if abs(dx) > 0.05:
        parts.append("left to right" if dx > 0 else "right to left")
    if abs(dy) > 0.05:
        parts.append("toward the camera" if dy > 0 else "away from the camera")
    return ", ".join(parts) or "mostly stationary"


def _track_fact(event: dict, camera: dict) -> str | None:
    """Where the NVR first and last saw the object: a named place it was at or beside, else a picture edge."""
    from . import zones
    path = event.get("path") or []
    if len(path) < 2:
        return None
    zl = camera.get("zones")
    x0, y0 = zones.foot(path[0][1:5])
    x1, y1 = zones.foot(path[-1][1:5])
    def describe(x, y):
        place = zones.place_of(x, y, zl)
        if place:
            return f"at '{place}'"
        edge = zones.edge_of(x, y)
        return f"at the {edge}" if edge else "in the middle of the view"
    first, last = describe(x0, y0), describe(x1, y1)
    line = f"Track (from the NVR): first seen {first}; last seen {last}."
    if first.startswith("at '") and "door" in first.lower():
        line += (" It appeared at that door, so it came into the building through it: state that plainly "
                 "('entered through the South door'), don't say it came from the kitchen.")
    if last.startswith("at '") and "door" in last.lower():
        line += " It was last seen at that door: it most likely left through it."
    return line


def _zone_fact(event: dict, camera: dict) -> str | None:
    """Which monitored zone YOLO confirmed the object in (cameras with include zones only)."""
    from . import zones
    zl = [z for z in zones.normalize(camera.get("zones")) if z["type"] == "include"]
    if not zl:
        return None
    feet = [zones.foot(s["match"]["box"]) for s in (event.get("detections") or {}).get("samples", []) if s.get("match")]
    names = [next((z.get("name") or "monitored area" for z in zl if zones.point_in_polygon(x, y, z["points"])), None) for x, y in feet]
    names = [n for n in names if n]
    if not names:
        return None
    zone = max(set(names), key=names.count)
    return (f"Location (confirmed by the NVR): this {event['camera_class']} is inside the monitored zone '{zone}', "
            f"which is the property itself (the yard / drive), not the road or lots in the background. Describe it as "
            f"on the property; do not call it background or highway traffic.")


def event_facts(event: dict, camera: dict) -> str:
    """Shared factual context for synopsis and chat prompts."""
    start = dt.datetime.fromtimestamp(event["start_ts"]).astimezone()
    dur = (event["end_ts"] or event["start_ts"]) - event["start_ts"]
    rules = sorted({r["topic"].split("/")[-2] if "/" in r["topic"] else r["topic"] for r in event.get("rules") or []})
    notes = (camera.get("scene_notes") or "").strip()
    lines = [f"Camera: {camera.get('name', event['camera_id'])}"]
    if notes:
        lines.append(f"Scene notes from the operator (treat as ground truth about this view):\n{notes}")
    if event.get("ptz_preset"):  # PTZ camera turned away from its home view
        where = "not at any saved preset" if event["ptz_preset"] == "away" else f"pointed at preset '{event['ptz_preset']}'"
        lines.append(f"The camera was turned away from its usual view for this event ({where}). The scene notes and "
                     "place names describe the usual view and may not apply here.")
    lines += [
        f"Local time: {start:%A %Y-%m-%d %H:%M:%S} ({'night' if start.hour < 6 or start.hour >= 20 else 'day'})",
        f"Duration on scene: {dur:.0f} s; movement across the frame: {describe_motion(event['path'])}",
        f"Camera analytics: {event['camera_class']} (confidence {event['camera_conf'] or 0:.2f})"
        + (f"; rules triggered: {', '.join(rules)}" if rules else ""),
        f"YOLO verification: {event['yolo_class'] or 'not confirmed'} (confidence {event['yolo_conf'] or 0:.2f}, "
        f"matched in {event['yolo_hits'] or 0} frames)",
    ]
    from . import identities
    known = identities.identity_facts(event["camera_class"], event["id"]) if event.get("id") else None
    if known:  # operator-named person/vehicle, e.g. "Known person: 'Shawn' (owner)"
        lines.append(known)
    if event.get("areas"):  # named by the operator: say where they went in the site's own words
        t0 = event["start_ts"]
        lines.append("Places (named by the operator) the " + event["camera_class"] + " was at, in order: "
                     + ", ".join(f"'{a['name']}' at {a['from'] - t0:+.0f} s" for a in event["areas"])
                     + ". In the summary name these places (e.g. 'went into "
                     + f"{event['areas'][0]['name']}', or for a door 'came in through {event['areas'][0]['name']}'), "
                     + "never just 'a bathroom' or 'a door'.")
    where = _zone_fact(event, camera)
    if where:  # otherwise Qwen tends to call anything with the road behind it "background highway traffic"
        lines.append(where)
    track = _track_fact(event, camera)
    if track:  # first/last position, so "came in through the South door" isn't left to guesswork
        lines.append(track)
    reasons = (event.get("anomaly_json") or {}).get("reasons")
    if reasons:  # learned baseline for this camera; lets the threat judgement account for what's normal here
        lines.append("Unusual for this camera: " + "; ".join(reasons))
    from . import policy
    rule = policy.prompt_lines(camera) if event["camera_class"] == "vehicle" else None
    if rule:  # operator's site rules, e.g. who may tow the solar towers
        lines.append(rule)
    return "\n".join(lines) + "\n"


def build_prompt(event: dict, camera: dict, examples: list[dict]) -> str:
    """examples: [{original, corrected}] recent operator corrections for this camera (few-shot)."""
    shots = ""
    if examples:
        shots = ("\nThe operator corrected these earlier synopses from this camera. Follow the corrected style, "
                 "detail level and threat judgement:\n")
        for ex in examples:
            shots += f"- Model wrote: {ex['original']}\n  Operator corrected to: {ex['corrected']}\n"
    return event_facts(event, camera) + shots + "\nWrite the synopsis of this event as JSON."


class OllamaServer:
    """Runs `ollama serve` on its own port with only the VLM GPU visible."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None

    async def run(self) -> None:
        env = {**os.environ, "OLLAMA_HOST": settings.ollama_url.split("://", 1)[-1],
               "CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": settings.ollama_gpu,
               # Vulkan ignores CUDA_VISIBLE_DEVICES and would expose the YOLO GPU too.
               "OLLAMA_VULKAN": "0", "GGML_VK_VISIBLE_DEVICES": "",
               # GPU 1 is Qwen's: never unload it (a reload costs 1-3 min on Windows)
               "OLLAMA_KEEP_ALIVE": "-1", "OLLAMA_NUM_PARALLEL": "1",
               # 8 GB shared with the Windows desktop: a q8 KV cache (needs flash attention) keeps the whole
               # model and its context in VRAM instead of spilling layers to the CPU, which made calls take minutes.
               "OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0",
               # requests through the OpenAI-compatible /v1 (the hub's shared AI for other sites) can't set num_ctx
               # per call, so the server default must be the same context the native calls ask for
               "OLLAMA_CONTEXT_LENGTH": str(settings.vlm_num_ctx)}
        if not settings.ollama_exe.exists():
            log.error("Ollama not found at %s; synopses disabled", settings.ollama_exe)
            return
        log_file = open(settings.runtime_dir / "ollama.log", "ab")
        while True:
            self.proc = await asyncio.create_subprocess_exec(str(settings.ollama_exe), "serve", env=env,
                                                             stdout=log_file, stderr=log_file)
            code = await self.proc.wait()
            log.warning("ollama serve exited (%s); restarting in 5s", code)
            await asyncio.sleep(5)

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()


async def wait_ready(timeout: float = 60) -> bool:
    async with httpx.AsyncClient(timeout=3) as c:
        for _ in range(int(timeout)):
            try:
                if (await c.get(f"{settings.ollama_url}/api/version")).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
    return False


async def warm_up() -> float:
    """Load Qwen (and its vision projector) into VRAM now, so the first real question doesn't wait for it.
    On Windows+CUDA Ollama loads without mmap: ~1 min, plus a slow first inference."""
    import cv2
    import numpy as np
    img = cv2.imencode(".jpg", np.full((64, 64, 3), 128, np.uint8))[1].tobytes()
    body = {"model": settings.vlm_model, "stream": False, "keep_alive": -1,
            "messages": [{"role": "user", "content": "Reply OK.", "images": [base64.b64encode(img).decode()]}],
            "options": {"num_ctx": settings.vlm_num_ctx, "num_predict": 2}}
    t0 = time.time()
    async with httpx.AsyncClient(timeout=900) as c:
        (await c.post(f"{settings.ollama_url}/api/chat", json=body)).raise_for_status()
    return time.time() - t0


async def ensure_models() -> None:
    async with httpx.AsyncClient(timeout=None) as c:
        have = {m["name"] for m in (await c.get(f"{settings.ollama_url}/api/tags")).json().get("models", [])}
        for model in (settings.vlm_model, settings.embed_model):
            if model in have or f"{model}:latest" in have:
                continue
            log.info("pulling %s (first run only)...", model)
            async with c.stream("POST", f"{settings.ollama_url}/api/pull", json={"model": model}) as r:
                async for line in r.aiter_lines():
                    if '"error"' in line:
                        raise RuntimeError(line)
            log.info("pulled %s", model)


async def synopsis(event: dict, camera: dict, images: list[bytes], examples: list[dict]) -> dict:
    """Routine synopses run locally; events that are unusual for their camera count as "unusual_review", which
    can go to the larger remote model when one is configured. Result has "_model"."""
    task = "unusual_review" if (event.get("anomaly") or 0) >= settings.anomaly_synopsis_min else "synopsis"
    return await vlmroute.router.chat_json(task, SYSTEM, build_prompt(event, camera, examples), images, SCHEMA, 600, 0.2)


async def embed(text: str) -> list[float] | None:
    if not settings.local_vlm_enabled:
        return None   # no local Ollama: the search index stays keyword-only
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{settings.ollama_url}/api/embed",
                             json={"model": settings.embed_model, "input": text})
            r.raise_for_status()
            return r.json()["embeddings"][0]
    except (httpx.HTTPError, KeyError, IndexError) as e:
        log.warning("embedding failed: %s", e)
        return None


CHAT_SYSTEM = (
    "You are reviewing a recorded security camera event together with the operator. "
    "You are given frames from the clip; each frame is stamped with its clip time (t=seconds). "
    "Answer from what is visible in the frames. Be concrete and brief. Say clearly when something is not visible "
    "or uncertain, and if another moment of the clip would answer the question, say which time to look at. "
    "Do not guess identities. You cannot take actions: you can't watch for anyone, set reminders or send alerts. "
    "If asked to, say so plainly and point to the 'Watch this person' button in this event's details, which does it for real."
)


async def chat_stream(event: dict, camera: dict, frames: list[tuple[float, bytes]], history: list[dict],
                      question: str):
    """Yield answer text chunks. history: prior [{role, content}] turns (text only)."""
    stamps = ", ".join(f"t={t:.1f}s" for t, _ in frames)
    synopsis_text = event.get("synopsis") or "(none yet)"
    context = (
        event_facts(event, camera)
        + f"Current synopsis: {synopsis_text}\n"
        + f"Attached frames in order: {stamps}. The amber box, where drawn, is the camera detection."
    )
    messages = [
        {"role": "system", "content": CHAT_SYSTEM},
        {"role": "user", "content": context, "images": [b for _, b in frames]},   # raw bytes: each backend encodes
        {"role": "assistant", "content": "Understood. I have the frames and event details. What would you like to know?"},
        *history[-8:],
        {"role": "user", "content": question},
    ]
    # routed like every other task (vlmroute): the local Qwen, or the remote when "chat" is a remote task or
    # the site has no local model
    async for kind, val in vlmroute.router.stream("chat", messages, 500, 0.3, "chat"):
        if kind == "delta" and val:
            yield val


class VlmGate:
    """One VLM request at a time; interactive chat always goes ahead of background synopses."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.chat_waiting = 0
        self.no_chat = asyncio.Event()
        self.no_chat.set()

    @contextlib.asynccontextmanager
    async def chat(self):
        self.chat_waiting += 1
        self.no_chat.clear()
        try:
            async with self.lock:
                yield
        finally:
            self.chat_waiting -= 1
            if not self.chat_waiting:
                self.no_chat.set()

    @contextlib.asynccontextmanager
    async def background(self):
        while True:
            await self.no_chat.wait()
            await self.lock.acquire()
            if not self.chat_waiting:
                break
            self.lock.release()
        try:
            yield
        finally:
            self.lock.release()


# ---------------------------------------------------------------- cross-camera journeys

SAME_PERSON_SCHEMA = {
    "type": "object",
    "properties": {
        "same_person": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "reason": {"type": "string", "description": "one sentence: the visible evidence (clothing, build, carried items)"},
    },
    "required": ["same_person", "confidence", "reason"],
}

JOURNEY_SCHEMA = {
    "type": "object",
    "properties": {
        # the route itself is written by code; Qwen supplies a short action per visit and the gist
        "actions": {"type": "array", "items": {"type": "string"},
                    "description": "one short phrase per visit, at most 8 words, e.g. 'came out of the exit door'"},
        "settings": {"type": "array", "items": {"type": "string", "enum": ["indoors", "outdoors", "unclear"]},
                     "description": "one per visit: is that camera's place inside a building or outside"},
        "overall": {"type": "string", "description": "one sentence, at most 25 words, e.g. 'Stepped outside to the side yard to check the trailers, then came back in.'"},
    },
    "required": ["actions", "settings", "overall"],
}


async def _chat_json(system: str, text: str, images: list[bytes], schema: dict, num_predict: int = 300,
                     task: str = "journey", priority: str = "background") -> dict:
    """Structured Qwen call, routed to the local or remote model by task (see vlmroute). Result has "_model"."""
    return await vlmroute.router.chat_json(task, system, text, images, schema, num_predict, 0.1, priority)


FOOTAGE_MATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "seen": {"type": "string", "description": "a few words: what is actually visible that matches (or not)"},
    },
    "required": ["matches", "confidence", "seen"],
}


async def footage_match(image: bytes, query: str) -> dict:
    """Does this recorded frame (or part of it) show what the operator searched for?"""
    system = ("You check search results from security camera footage. Say whether the image clearly shows what was "
              "searched for. Only count things that are actually visible; if unsure, say it doesn't match.")
    return await _chat_json(system, f"Search: {query}\nDoes this image show it? Answer as JSON.", [image],
                            FOOTAGE_MATCH_SCHEMA, 120, task="footage_verify", priority="chat")


async def same_person(image_a: bytes, image_b: bytes, facts: str) -> dict:
    """Ask Qwen whether two person crops (different cameras) show the same individual."""
    system = ("You compare two images of people captured by different security cameras a short time apart. "
              "Decide whether they show the SAME individual. Judge only visible, stable evidence: clothing colours "
              "and types, footwear, build, hair/headwear, carried items. Lighting and angle differ between cameras; "
              "don't be misled by that. If the evidence is weak or ambiguous, say not the same or use low confidence.")
    return await _chat_json(system, facts + "\nImage 1 is from the first camera, image 2 from the second. Answer as JSON.",
                            [image_a, image_b], SAME_PERSON_SCHEMA, 200)


async def journey_narrative(visits: list[dict], images: list[bytes]) -> dict:
    """visits: [{camera, time, duration_s, gap_s, descriptions}] in order (consecutive sightings on one camera
    already merged); images: one crop per visit, same order. Returns {actions: [..per visit], overall}."""
    lines = []
    for i, v in enumerate(visits, 1):
        gap = f", {v['gap_s']:.0f} s after leaving the previous camera" if v.get("gap_s") is not None else ", first sighting"
        desc = " ".join(d for d in v["descriptions"] if d)[:240] or "no description"
        lines.append(f"Visit {i}: '{v['camera']}' at {v['time']}, about {v['duration_s']:.0f} s{gap}. Notes: {desc}")
    system = ("One person was matched by appearance across several security cameras. Using the camera names as places, "
              "tell what they did in each place and where they went. Do NOT repeat the notes or describe clothing or scenery; "
              "write short action phrases. Be factual; don't guess identity or intent.")
    text = ("\n".join(lines) + f"\nOne image per visit is attached, in order. Give exactly {len(visits)} actions "
            f"(one per visit, at most 8 words each), exactly {len(visits)} settings (indoors/outdoors per visit, from the image) "
            "and one 'overall' sentence about the movement between places. Answer as JSON.")
    return await _chat_json(system, text, images, JOURNEY_SCHEMA, 300)
