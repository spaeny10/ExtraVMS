"""Qwen-VL scene synopsis via a dedicated Ollama server, plus text embeddings for search."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import datetime as dt
import json
import logging
import os
import re
import signal
import subprocess
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
    "Weapons especially: describe a small dark object in someone's hand neutrally unless a gun or knife is clearly "
    "visible, and never state a weapon as fact from a distant, small or low-resolution view (a towel, phone or tool "
    "held at a distance looks much like a handgun). If it might be a weapon, say so as a possibility, e.g. 'a dark "
    "object, possibly a handgun': the system then takes a closer look at full resolution. "
    "threat_level: none = routine (passer-by, resident, delivery), low = unusual but benign, "
    "medium = suspicious (loitering, checking doors/cars, face concealed at night), high = clear criminal or dangerous act. "
    "A 'Location (confirmed by the NVR)' line says where the object really is and overrides general scene notes "
    "about background traffic. For vehicles set towing=true only when the vehicle is pulling or hitched to a trailer "
    "or equipment (and name it in towed); parked trailers nearby do not count. Use American English spelling."
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
    """Where the NVR first and last saw the object (a named place it was in or beside, else a picture edge), and
    what the track says about doors. Only what the track shows (zones.door_facts): "entered through" a door only
    when the track begins in it, "left through" only when it ends in it; a door it walked to from inside and away
    from again is "went to <door> and came back", one it was only beside is "was near <door>". (A hint that a
    track starting *beside* a door meant an entry made models write "entered through the South Exterior Door" for
    a man who walked up to a tablet by the door from inside and back: event 8377.)"""
    from . import zones
    path = event.get("path") or []
    if len(path) < 2:
        return None
    zl = zones.normalize(camera.get("zones"))
    x0, y0 = zones.foot(path[0][1:5])
    x1, y1 = zones.foot(path[-1][1:5])

    def describe(x, y):
        place = zones.place_of(x, y, zl)
        if place:
            inside = any((z.get("name") or "Area").strip() == place and zones.point_in_polygon(x, y, z["points"])
                         for z in zl if z["type"] == "area")
            return place, f"in '{place}'" if inside else f"beside '{place}'"
        edge = zones.edge_of(x, y)
        return None, f"at the {edge}" if edge else "in the middle of the view"

    (p0, first), (p1, last) = describe(x0, y0), describe(x1, y1)
    line = f"Track (from the NVR): first seen {first}; last seen {last}."
    entered, left = zones.door_facts(event)
    # first / last sample in (or on the mat of) a door and not the same door at the other end: that is an entry / exit
    # too (door_facts needs the event's areas, which a short track may not have)
    entered = entered or (p0 if p0 and p0 != p1 and zones.DOOR_RE.search(p0) else None)
    left = left or (p1 if p1 and p1 != p0 and zones.DOOR_RE.search(p1) else None)
    visited =[a["name"] for a in event.get("areas") or []]
    doors = list(dict.fromkeys(n for n in [*visited, p0, p1] if n and zones.DOOR_RE.search(n)))
    for name in doors:
        if name == entered and name == left:
            line += (f" It appeared in '{name}' when the track began and was in it again when the track ended: it came "
                     f"in through {name} and went back out through it.")
        elif name == entered:
            line += (f" It appeared in '{name}' when the track began: it came in through that door. Say it entered "
                     f"through {name}.")
        elif name == left:
            line += f" It was in '{name}' when the track ended: it left through that door. Say it left through {name}."
        elif name in visited:
            line += (f" It was already in view away from '{name}' when the track began, walked to it and moved away "
                     f"from it again: "
                     f"write 'went to {name} and came back' (or 'was near {name}'). It did NOT come in or go out "
                     f"through {name}: never write 'entered through' or 'left through' it.")
        else:
            line += (f" It was only near '{name}' and was not seen going through it: write 'was near {name}', never "
                     f"'entered through' or 'left through' it.")
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


def ppe_fact(event: dict) -> str | None:
    """The stored PPE check in plain words for the synopsis / chat prompt (None if the person wasn't checked)."""
    from . import ppe
    r = (event.get("detections") or {}).get("ppe")
    if not r or r.get("verdict") not in ("violation", "compliant"):
        return None
    zone = r.get("zone", "the PPE zone")
    if r["verdict"] == "compliant":
        return (f"PPE check (confirmed by the NVR): in the PPE-required zone '{zone}' this person wore the required "
                + " and ".join(ppe.ITEM_WORDS[i] for i in r.get("required") or []) + ".")
    missing = " or ".join(ppe.ITEM_WORDS[i] for i in r["violation"])
    return (f"PPE violation (confirmed by the NVR): this person spent {r.get('dwell_s', 0):.0f} s in the PPE-required "
            f"zone '{zone}' without a {missing}. Say so in the summary (e.g. 'not wearing a {missing} in {zone}'), "
            f"and rate the threat at least medium for the safety breach.")


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
        (f"Camera analytics: {event['camera_class']} (the camera's own detection event, no position or confidence)"
         if str(event.get("track_id") or "").startswith("onvif:")   # ruleevents.TRACK_PREFIX
         else f"Camera analytics: {event['camera_class']} (confidence {event['camera_conf'] or 0:.2f})")
        + (f"; rules triggered: {', '.join(rules)}" if rules else ""),
        f"YOLO verification: {event['yolo_class'] or 'not confirmed'} (confidence {event['yolo_conf'] or 0:.2f}, "
        f"matched in {event['yolo_hits'] or 0} frames)",
    ]
    from . import identities
    known = identities.identity_facts(event["camera_class"], event["id"]) if event.get("id") else None
    if known:  # operator-named person/vehicle, e.g. "Known person: 'Shawn' (owner)"
        lines.append(known)
    if event.get("areas"):  # named by the operator: say where they went in the site's own words
        from . import zones
        t0 = event["start_ts"]
        a0 = event["areas"][0]["name"]
        doors = any(zones.DOOR_RE.search(a["name"]) for a in event["areas"])
        # no "came in through <door>" example here: whether it did is the Track line's call (_track_fact)
        lines.append("Places (named by the operator) the " + event["camera_class"] + " was at, in order: "
                     + ", ".join(f"'{a['name']}' at {a['from'] - t0:+.0f} s" for a in event["areas"])
                     + f". In the summary name these places (e.g. '{'went to' if zones.DOOR_RE.search(a0) else 'went into'} "
                     + f"{a0}'), never just 'a bathroom' or 'a door'."
                     + (" Being at a door is not going through it: the Track line says whether it came in or went "
                        "out through one." if doors else ""))
    where = _zone_fact(event, camera)
    if where:  # otherwise Qwen tends to call anything with the road behind it "background highway traffic"
        lines.append(where)
    track = _track_fact(event, camera)
    if track:  # first/last position, so "came in through the South door" isn't left to guesswork
        lines.append(track)
    reasons = (event.get("anomaly_json") or {}).get("reasons")
    if reasons:  # learned baseline for this camera; lets the threat judgment account for what's normal here
        lines.append("Unusual for this camera: " + "; ".join(reasons))
    ppe_line = ppe_fact(event)
    if ppe_line:  # the PPE check (detector + Qwen on doubt) already decided: the synopsis must agree
        lines.append(ppe_line)
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
                 "detail level and threat judgment:\n")
        for ex in examples:
            shots += f"- Model wrote: {ex['original']}\n  Operator corrected to: {ex['corrected']}\n"
    return event_facts(event, camera) + shots + "\nWrite the synopsis of this event as JSON."


def ollama_env(inst: vlmroute.LocalModel, base: dict | None = None) -> dict:
    """Environment for one managed `ollama serve` (primary or fallback): its own port and GPU, the same model store."""
    return {**(os.environ if base is None else base), "OLLAMA_HOST": inst.url.split("://", 1)[-1],
            "CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": inst.gpu,
            # Vulkan ignores CUDA_VISIBLE_DEVICES and would expose the other GPU too.
            "OLLAMA_VULKAN": "0", "GGML_VK_VISIBLE_DEVICES": "",
            # never unload the model (a reload costs 1-3 min on Windows)
            "OLLAMA_KEEP_ALIVE": "-1", "OLLAMA_NUM_PARALLEL": str(max(1, inst.parallel)),
            # a q8 KV cache (needs flash attention) keeps the whole model and its context in VRAM instead of
            # spilling layers to the CPU, which made calls take minutes on a card shared with the desktop.
            "OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0",
            # requests through the OpenAI-compatible /v1 (the hub's shared AI for other sites) can't set num_ctx
            # per call, so the server default must be the same context the native calls ask for
            "OLLAMA_CONTEXT_LENGTH": str(inst.num_ctx)}


class OllamaServer:
    """Runs `ollama serve` on its own port with only the VLM GPU visible, and a second one for the fallback model
    (NVR_FALLBACK_VLM_MODEL) on its own port and GPU when that is configured. Each is restarted if it exits."""

    LOGS = {"primary": "ollama.log", "fallback": "ollama-fallback.log"}

    def __init__(self) -> None:
        self.procs: dict[str, asyncio.subprocess.Process | None] = {"primary": None, "fallback": None}

    @property
    def proc(self) -> asyncio.subprocess.Process | None:
        return self.procs["primary"]

    def roles(self) -> list[str]:
        return ["primary", "fallback"] if settings.fallback_vlm_enabled else ["primary"]

    async def run(self) -> None:
        if not settings.ollama_exe.exists():
            log.error("Ollama not found at %s; synopses disabled", settings.ollama_exe)
            return
        await asyncio.gather(*(self._serve(role) for role in self.roles()))

    async def _serve(self, role: str) -> None:
        env = ollama_env(vlmroute.router.models[role])
        log_file = open(settings.runtime_dir / self.LOGS[role], "ab")
        while True:
            # own process group / session so stop() can take the model runners (llama-server) down with the server
            kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            self.procs[role] = await asyncio.create_subprocess_exec(str(settings.ollama_exe), "serve", env=env,
                                                                    stdout=log_file, stderr=log_file, **kw)
            code = await self.procs[role].wait()
            log.warning("ollama serve (%s) exited (%s); restarting in 5s", role, code)
            await asyncio.sleep(5)

    async def stop(self, role: str | None = None) -> None:
        """Stop `ollama serve` and its runner children (one instance, or both). Terminating only the server orphaned
        one llama-server per watchdog restart while a GPU was lost; each kept ~7 GB committed and twelve of them
        exhausted the machine."""
        for r in ([role] if role else list(self.procs)):
            p = self.procs.get(r)
            if p and p.returncode is None:
                await kill_tree(p.pid)


async def kill_tree(pid: int) -> None:
    """Kill a process and every descendant (Windows: taskkill /T; POSIX: the process group it leads)."""
    try:
        if os.name == "nt":
            p = await asyncio.create_subprocess_exec("taskkill", "/PID", str(pid), "/T", "/F",
                                                     stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await p.wait()
        else:
            os.killpg(pid, signal.SIGTERM)
            await asyncio.sleep(3)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError) as e:
        log.debug("kill_tree %s: %s", pid, e)


def _inst(role: str) -> vlmroute.LocalModel:
    return vlmroute.router.models[role]


async def wait_ready(timeout: float = 60, role: str = "primary") -> bool:
    url = _inst(role).url
    async with httpx.AsyncClient(timeout=3) as c:
        for _ in range(int(timeout)):
            try:
                if (await c.get(f"{url}/api/version")).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
    return False


def warm_body(inst: vlmroute.LocalModel, image: bytes) -> dict:
    """The warm-up request: the same model and num_ctx every later request uses, so nothing reloads afterwards."""
    return {"model": inst.model, "stream": False, "keep_alive": -1,
            "messages": [{"role": "user", "content": "Reply OK.", "images": [base64.b64encode(image).decode()]}],
            "options": {"num_ctx": inst.num_ctx, "num_predict": 2}}


async def warm_up(role: str = "primary") -> float:
    """Load Qwen (and its vision projector) into VRAM now, so the first real question doesn't wait for it.
    On Windows+CUDA Ollama loads without mmap: ~1 min, plus a slow first inference."""
    import cv2
    import numpy as np
    inst = _inst(role)
    img = cv2.imencode(".jpg", np.full((64, 64, 3), 128, np.uint8))[1].tobytes()
    t0 = time.time()
    async with httpx.AsyncClient(timeout=900) as c:
        (await c.post(f"{inst.url}/api/chat", json=warm_body(inst, img))).raise_for_status()
    return time.time() - t0


async def ensure_models(role: str = "primary") -> None:
    """Pull what this instance serves (the model store is shared: the primary also pulls the embedding model)."""
    inst = _inst(role)
    wanted = (inst.model, settings.embed_model) if role == "primary" else (inst.model,)
    async with httpx.AsyncClient(timeout=None) as c:
        have = {m["name"] for m in (await c.get(f"{inst.url}/api/tags")).json().get("models", [])}
        for model in wanted:
            if model in have or f"{model}:latest" in have:
                continue
            log.info("pulling %s (first run only)...", model)
            async with c.stream("POST", f"{inst.url}/api/pull", json={"model": model}) as r:
                async for line in r.aiter_lines():
                    if '"error"' in line:
                        raise RuntimeError(line)
            log.info("pulled %s", model)


def vram_verdict(ps: dict, model: str) -> dict | None:
    """From Ollama's /api/ps: {size, size_vram, on_gpu} for `model`, or None if it is not loaded."""
    for m in ps.get("models") or []:
        if m.get("name") == model or m.get("model") == model or m.get("name") == f"{model}:latest":
            size, vram = int(m.get("size") or 0), int(m.get("size_vram") or 0)
            return {"size": size, "size_vram": vram, "on_gpu": size > 0 and vram >= size}
    return None


async def check_vram(role: str = "primary") -> dict | None:
    """After warm-up: is the whole model (weights + KV cache) in VRAM? A model partly on the CPU answers many
    times slower; log a WARNING with what to change."""
    inst = _inst(role)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{inst.url}/api/ps")
            r.raise_for_status()
            v = vram_verdict(r.json(), inst.model)
    except (httpx.HTTPError, ValueError) as e:
        log.warning("could not read %s placement from Ollama (%s): %s", inst.model, role, e)
        return None
    inst.vram = v
    if v is None:
        log.warning("%s (%s) is not listed as loaded by Ollama after warm-up", inst.model, role)
    elif not v["on_gpu"]:
        log.warning("%s (%s, GPU %s) is partly on the CPU: %.1f of %.1f GB in VRAM (num_ctx %d, parallel %d). "
                    "Lower %s or %s, or free that GPU.", inst.model, role, inst.gpu or "auto", v["size_vram"] / 1e9,
                    v["size"] / 1e9, inst.num_ctx, inst.parallel,
                    "NVR_VLM_NUM_CTX" if role == "primary" else "NVR_FALLBACK_NUM_CTX",
                    "NVR_OLLAMA_PARALLEL" if role == "primary" else "NVR_FALLBACK_OLLAMA_PARALLEL")
    else:
        log.info("%s (%s) fully in VRAM on GPU %s: %.1f GB", inst.model, role, inst.gpu or "auto", v["size_vram"] / 1e9)
    return v


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
            r = await c.post(f"{vlmroute.router.embed_url()}/api/embed",
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
    "If asked to, say so plainly and point to the 'Watch this person' button in this event's details, which does it for real. "
    "Use American English spelling."
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
    """One VLM request at a time; interactive chat always goes ahead of background synopses.

    Re-entrant within a task: a request that already holds the gate (an Ask whose handler and the router both
    take a chat turn) passes straight through instead of waiting on itself, which once froze every synopsis.
    A background turn that waits unusually long is logged, so a stuck holder shows up in the log.
    Each local model (primary, fallback) has its own gate; holding one says nothing about the other."""

    WAIT_WARN_S = 120

    def __init__(self, name: str = "primary") -> None:
        self.name = name
        self._held: contextvars.ContextVar[bool] = contextvars.ContextVar(f"vlm_gate_held_{name}_{id(self)}", default=False)
        self.lock = asyncio.Lock()
        self.chat_waiting = 0
        self.no_chat = asyncio.Event()
        self.no_chat.set()
        self.held_since: float | None = None
        self.holder = ""

    @contextlib.asynccontextmanager
    async def chat(self):
        if self._held.get():
            yield
            return
        self.chat_waiting += 1
        self.no_chat.clear()
        try:
            async with self.lock:
                token = self._held.set(True)
                self.held_since, self.holder = time.time(), "chat"
                try:
                    yield
                finally:
                    self._held.reset(token)
                    self.held_since = None
        finally:
            self.chat_waiting -= 1
            if not self.chat_waiting:
                self.no_chat.set()

    @contextlib.asynccontextmanager
    async def background(self):
        if self._held.get():
            yield
            return
        t0, warned = time.time(), False
        while True:
            try:
                await asyncio.wait_for(self.no_chat.wait(), self.WAIT_WARN_S)
                await asyncio.wait_for(self.lock.acquire(), self.WAIT_WARN_S)
            except asyncio.TimeoutError:
                if not warned:
                    warned = True
                    log.warning("VLM gate (%s): a background call has waited %.0fs (held by %s for %.0fs, %d chat waiting)",
                                time.time() - t0, self.holder or "nobody",
                                time.time() - self.held_since if self.held_since else 0, self.chat_waiting)
                continue
            if not self.chat_waiting:
                break
            self.lock.release()
        token = self._held.set(True)
        self.held_since, self.holder = time.time(), "background"
        try:
            yield
        finally:
            self._held.reset(token)
            self.held_since = None
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
              "Decide whether they show the SAME individual. Judge only visible, stable evidence: clothing colors "
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
              "write short action phrases. Be factual; don't guess identity or intent. Use American English spelling.")
    text = ("\n".join(lines) + f"\nOne image per visit is attached, in order. Give exactly {len(visits)} actions "
            f"(one per visit, at most 8 words each), exactly {len(visits)} settings (indoors/outdoors per visit, from the image) "
            "and one 'overall' sentence about the movement between places. Answer as JSON.")
    return await _chat_json(system, text, images, JOURNEY_SCHEMA, 300)
