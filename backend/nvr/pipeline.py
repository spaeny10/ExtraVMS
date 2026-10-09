"""Event pipeline: tracker -> recording clip -> YOLO verify -> Qwen synopsis -> search index."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import cv2
import httpx

from . import baseline, cells, detector, identities, journeys, policy, ppe, retention, vlmroute, weaponcheck, zones, merge
from . import synopsis as vlm
from .config import settings
from .db import db
from .mediamtx import fetch_clip
from .tracker import Tracker
from .verifier import Verifier, event_dir, grab_frames

log = logging.getLogger("nvr.pipeline")
_ATTEMPT = re.compile(r"^synopsis \(attempt (\d+)\)")


def synopsis_attempts(e: dict | None) -> int:
    """How many times Qwen has failed on this event, read back from its error text (no schema change)."""
    err = (e or {}).get("error") or ""
    if not err.startswith("synopsis"):
        return 0
    m = _ATTEMPT.match(err)
    return int(m.group(1)) if m else 1


def synopsis_error(e: dict | None, ex: Exception) -> str:
    """Error text for a failed synopsis; the second failure onwards carries the attempt count for retry_loop."""
    n = synopsis_attempts(e) + 1
    return f"synopsis: {ex}" if n == 1 else f"synopsis (attempt {n}): {ex}"


class Pipeline:
    def __init__(self) -> None:
        self.frames: asyncio.Queue = asyncio.Queue(maxsize=10000)
        self.rule_events: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.verify_q: asyncio.Queue[int] = asyncio.Queue()
        # (priority, seq, event_id): people before vehicles, oldest first within a class
        self.synopsis_q: asyncio.PriorityQueue[tuple[int, int, int]] = asyncio.PriorityQueue()
        self._synopsis_seq = 0
        self.synopsis_pending: set[int] = set()  # queued or being written now; the UI shows "Qwen is writing…"
        self.synopsis_hold: dict[int, float] = {}  # verified events waiting for a possible follow-on fragment (merge.py)
        self.synopsis_times: deque[float] = deque(maxlen=50)  # seconds per synopsis, for Optimize my system
        self.synopsis_failed_at = 0.0    # last synopsis that ended in an error (retry_loop backs off from it)
        self.journey_q: asyncio.Queue[int] = asyncio.Queue()
        self.tracker = Tracker(self.verify_q.put)
        self.tracker.camera = self.camera_row        # event_source / motion_events / metadata Analytics flag
        self.tracker.on_opened = self.publish        # events opened by a camera's ONVIF detection events
        self._camera_rows: dict[str, dict] = {}
        self._camera_rows_at = 0.0
        self.gpu = ThreadPoolExecutor(max_workers=1, thread_name_prefix="yolo")
        self.verifier: Verifier | None = None
        self.verify_done = 0              # events the verifier finished (verified / rejected / error): the stall check's progress
        self._stall_state: dict | None = None
        self.verify_stall: dict = {"stalled": False, "since": None, "queue": 0}   # detector.health_alerts reads this
        self.ppe = ppe.Detector()  # PPE YOLO, loaded on the GPU thread the first time a camera has a PPE zone
        self.subscribers: set[asyncio.Queue] = set()
        self.vlm_ready = False
        self.vlm_state = "starting"       # starting | ready | unresponsive (shown on Settings → System and by the advisor)
        self.vlm_timeouts = 0             # consecutive Qwen calls that timed out
        self.vlm_down_since: float | None = None
        self.ollama = None                # synopsis.OllamaServer, set by the API at startup (for restarts)
        self.cameras: dict[str, dict] = {}
        self.gate = vlm.VlmGate()
        vlmroute.router.gate = self.gate  # local Qwen calls take turns here; remote ones don't need to
        # the fallback model (NVR_FALLBACK_VLM_MODEL) is on another GPU: its own turns, beside the primary's
        vlmroute.router.fallback_gate = vlm.VlmGate("fallback")
        vlmroute.router.on_unresponsive = lambda role: asyncio.create_task(self.restart_vlm(role), name=f"vlm-restart-{role}")
        self.decode = ThreadPoolExecutor(max_workers=2, thread_name_prefix="decode")
        self.clip = None  # OpenCLIP, shared by the footage index and vehicle fingerprints (loaded on the GPU thread)
        self.ptz = None   # ptz.PtzManager, set by the API at startup (PTZ cameras: home/away, relay, digital input)

    async def get_clip(self):
        if self.clip is None:
            from .clip import Clip
            self.clip = await asyncio.get_running_loop().run_in_executor(self.gpu, Clip)
            log.info("CLIP model loaded (footage search, vehicle fingerprints)")
        return self.clip

    CAMERA_ROWS_S = 30.0   # the camera rows the tracker reads are re-read this often (a stream check updates them)

    def camera_row(self, camera_id: str) -> dict | None:
        """The camera's settings row for the tracker: settings and its stream check (streams.py stores the metadata
        Analytics flag there) change without sync_cameras, so the rows are re-read every CAMERA_ROWS_S."""
        now = time.time()
        if now - self._camera_rows_at >= self.CAMERA_ROWS_S:
            try:
                self._camera_rows = {c["id"]: c for c in db.cameras()}
            except Exception:  # noqa: BLE001 - keep the last rows
                log.exception("reading cameras for the tracker failed")
            self._camera_rows_at = now
        return self._camera_rows.get(camera_id) or self.cameras.get(camera_id)

    def forget_camera_rows(self) -> None:
        """A camera was saved: the tracker reads its new settings on the next event."""
        self._camera_rows_at = 0.0

    # ---- live updates for the UI
    def annotate(self, e: dict) -> dict:
        """Add live pipeline state the DB doesn't hold."""
        e["synopsis_pending"] = e["id"] in self.synopsis_pending
        return e

    def queue_synopsis(self, event_id: int) -> bool:
        """Ask Qwen for a synopsis once; a second request while it's queued or running is ignored."""
        if event_id in self.synopsis_pending:
            return False
        self.synopsis_pending.add(event_id)
        row = db.one("SELECT camera_class FROM events WHERE id=?", [event_id])
        self._synopsis_seq += 1
        self.synopsis_q.put_nowait((0 if row and row["camera_class"] == "person" else 1, self._synopsis_seq, event_id))
        return True

    def publish(self, event_id: int) -> None:
        e = db.event(event_id)
        if not e:
            return
        self.annotate(e)
        for q in list(self.subscribers):
            if q.qsize() < 100:
                q.put_nowait({"type": "event", "event": e})

    def publish_removed(self, event_id: int) -> None:
        """A fragment was merged into an earlier event: browsers drop its card."""
        self.publish_msg({"type": "event_removed", "id": event_id})

    def publish_msg(self, msg: dict) -> None:
        """Push any other live update (e.g. a new briefing) to connected browsers."""
        for q in list(self.subscribers):
            if q.qsize() < 100:
                q.put_nowait(msg)

    # ---- stages
    async def ingest_loop(self) -> None:
        last_sweep = 0.0
        while True:
            try:
                frame = await asyncio.wait_for(self.frames.get(), 0.5)
                opened_before = {t.event_id for t in self.tracker.tracks.values()}
                self.tracker.on_frame(frame)
                for t in self.tracker.tracks.values():
                    if t.event_id and t.event_id not in opened_before:
                        self.publish(t.event_id)
            except asyncio.TimeoutError:
                pass
            while not self.rule_events.empty():
                ev = self.rule_events.get_nowait()
                if self.ptz and ("DigitalInput" in ev.topic or "Relay" in ev.topic):
                    self.ptz.io_event(ev.camera_id, ev.topic, ev.state, ev.ts)
                self.tracker.on_rule_event(ev)
            if time.time() - last_sweep >= 1:
                last_sweep = time.time()
                await self.tracker.sweep()

    async def verify_loop(self) -> None:
        loop = asyncio.get_running_loop()
        self.verifier = await loop.run_in_executor(self.gpu, Verifier)
        asyncio.create_task(self._reid_backfill(), name="reid-backfill")
        asyncio.create_task(self._vehicle_backfill(), name="vehicle-backfill")
        asyncio.create_task(self._cells_backfill(), name="cells-backfill")
        asyncio.create_task(self.hold_loop(), name="synopsis-hold")
        asyncio.create_task(self.retry_loop(), name="synopsis-retry")
        while True:
            event_id = await self.verify_q.get()
            try:
                await self._verify(event_id)
            except Exception as e:  # keep the worker alive
                log.exception("verify %s failed", event_id)
                db.update_event(event_id, status="error", error=f"verify: {e}")
            row = db.one("SELECT status FROM events WHERE id=?", [event_id])
            if row and row["status"] in ("verified", "rejected", "error"):
                self.verify_done += 1
            self.publish(event_id)

    def check_verify_stall(self, now: float | None = None) -> bool:
        """Verify queue with work and no event finished for detector.STALL_AFTER_S while YOLO claims to be ready:
        log an ERROR once and raise `detector_stalled` (detector.health_alerts) until events move again."""
        q = self.verify_q.qsize()
        ready = detector.yolo_status(self)["yolo_ready"]
        self._stall_state, stalled = detector.stall_check(self._stall_state, q if ready else 0, self.verify_done, now or time.time())
        was = self.verify_stall.get("stalled")
        if stalled and not was:
            log.error("event verification has stalled: %d events waiting, none finished since %s (YOLO on %s)", q,
                      time.strftime("%H:%M:%S", time.localtime(self._stall_state["since"])), getattr(self.verifier, "device", "?"))
        elif was and not stalled:
            log.info("event verification is moving again (%d waiting)", q)
        self.verify_stall = {"stalled": stalled, "since": self._stall_state["since"], "queue": q}
        return stalled

    async def health_loop(self) -> None:
        """Detector health every 30 s (the Hailo fallback reports itself; this catches a queue that stopped moving)."""
        while True:
            await asyncio.sleep(30)
            try:
                self.check_verify_stall()
            except Exception:
                log.exception("verify stall check failed")

    async def _verify(self, event_id: int) -> None:
        e = db.event(event_id)
        if not e or e["status"] != "pending":
            return
        if e["end_ts"] is None:  # queued while still being tracked: the tracker re-queues it when it closes
            return
        # MediaMTX flushes fMP4 parts every second; wait until the end of the clip is on disk.
        clip_end = e["end_ts"] + settings.clip_post_roll
        wait = clip_end + settings.recording_lag - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        clip_start = e["start_ts"] - settings.clip_pre_roll
        clip = event_dir(event_id) / "clip.mp4"
        await fetch_clip(e["camera_id"], clip_start, clip_end - clip_start, clip)
        away = bool(e.get("ptz_preset"))  # PTZ camera turned away from home: zones/masks don't apply
        result = await asyncio.get_running_loop().run_in_executor(
            self.gpu, self.verifier.verify, e, clip, clip_start, None if away else self.cameras.get(e["camera_id"], {}).get("zones"))
        reid = result.pop("reid", None)
        if "path" in result:  # extended into the pre/post-roll: the region cells follow
            result["cells"] = cells.for_event(result["path"])
        db.update_event(event_id, clip=str(clip.relative_to(settings.data_dir)), error=None, **result)
        if reid:
            db.set_reid(event_id, reid)
        label = result.get("camera_class") or e["camera_class"]   # a "motion" event takes the class YOLO found
        if result["status"] == "verified" and label == "vehicle":
            clip = await self.get_clip()
            await asyncio.get_running_loop().run_in_executor(self.gpu, identities.embed_vehicle, clip, event_id)
        # Verified from the clip (snapshot, re-ID and fingerprint are made); a camera over the hourly event limit
        # keeps the snapshot only, or its clips fill the disk (retention.EventRateGuard).
        if retention.rate_guard.check(e["camera_id"]):
            retention.drop_clip_media(event_id)
        if result["status"] == "verified" and (watched := identities.check_watch(event_id)):
            log.info("event %s matches watched %s '%s'", event_id, e["camera_class"], watched)
        log.info("event %s %s (yolo %s, %s hits)", event_id, result["status"],
                 result.get("yolo_class"), result.get("yolo_hits"))
        if result["status"] != "verified":
            return
        # A continuation of the fragment just before it? Fold it in and verify the whole visit as one event.
        found = merge.candidate(db.event(event_id))
        if not found and (long_a := merge.candidate_long(db.event(event_id))):
            # A person who stood still between two fragments: merge only if the recording shows them there throughout.
            reason = await asyncio.get_running_loop().run_in_executor(
                self.gpu, merge.footage_check, long_a, db.event(event_id),
                None if away else self.cameras.get(e["camera_id"], {}).get("zones"), self.verifier.model)
            a_now, b_now = db.event(long_a["id"]), db.event(event_id)
            ok = bool(reason and a_now and b_now and b_now["status"] == "verified" and a_now["status"] in ("verified", "pending"))
            if ok and merge.untouched(a_now) and merge.untouched(b_now):
                found = (a_now, reason)
        if found:
            target, reason = found
            self.synopsis_hold.pop(event_id, None)
            self.synopsis_hold.pop(target["id"], None)
            merge.apply(target, db.event(event_id), reason)
            self.publish_removed(event_id)
            self.publish(target["id"])
            self.verify_q.put_nowait(target["id"])
            return
        a: dict = {}
        if away:  # named places, site rules and "what's normal" all describe the home view
            db.update_event(event_id, anomaly=None, anomaly_json=None, priority=None)
        else:
            self.record_areas(event_id)
            await self.check_ppe(event_id)          # hard hat / hi-vis vest in a PPE zone (before the rules: it is one)
            policy.check(event_id, self.cameras.get(e["camera_id"]))  # e.g. an unrecognized person entering by an exterior door
            a = baseline.apply(event_id) or {}
        if a.get("reasons"):
            log.info("event %s unusual %.2f: %s", event_id, a["score"], "; ".join(a["reasons"]))
        if self.wants_synopsis(db.event(event_id)) or a.get("score", 0) >= settings.anomaly_synopsis_min:
            # hold long enough for a follow-on fragment to close, be verified and merge in; then describe once
            self.synopsis_hold[event_id] = e["end_ts"] + settings.track_merge_gap + settings.clip_post_roll + settings.recording_lag + 2
        else:
            await self.reindex(event_id)  # no VLM for this label: index the YOLO tags for search

    async def hold_loop(self) -> None:
        """Queue held synopses once their merge window has passed (events merged meanwhile were removed from the hold)."""
        while True:
            await asyncio.sleep(1)
            now = time.time()
            for event_id, due in list(self.synopsis_hold.items()):
                if due > now:
                    continue
                self.synopsis_hold.pop(event_id, None)
                e = db.event(event_id)
                if e and e["status"] == "verified" and not e["synopsis"]:
                    self.queue_synopsis(event_id)

    def synopsis_workers(self) -> int:
        """One synopsis at a time with one local model (as before). With a fallback model, enough workers that the
        primary's queue can pass fallback_when_queue_over, so a burst spills over to the fallback (vlmroute.plan)
        instead of waiting behind the primary."""
        return max(1, settings.fallback_when_queue_over + 2) if settings.fallback_vlm_enabled else 1

    async def synopsis_loop(self) -> None:
        n = self.synopsis_workers()
        if n == 1:
            return await self._synopsis_worker()
        log.info("synopses: %d workers (primary %s, fallback %s when its queue is over %d)", n, settings.vlm_model,
                 settings.fallback_vlm_model, settings.fallback_when_queue_over)
        await asyncio.gather(*(self._synopsis_worker() for _ in range(n)))

    async def _synopsis_worker(self) -> None:
        while True:
            item = await self.synopsis_q.get()
            event_id = item[2]
            if not self.vlm_ready:
                await asyncio.sleep(5)
                await self.synopsis_q.put(item)
                continue
            try:
                await self._synopsis(event_id)
                self.vlm_timeouts = 0
            except (httpx.TimeoutException, asyncio.TimeoutError) as ex:
                self.vlm_timeouts += 1
                log.warning("synopsis %s timed out (%s in a row): %s", event_id, self.vlm_timeouts, type(ex).__name__)
                self.synopsis_pending.discard(event_id)
                # with a fallback model the router already retried there and restarts whichever instance failed
                if self.vlm_timeouts >= 2 and self.vlm_ready and not settings.fallback_vlm_enabled:
                    asyncio.create_task(self.restart_vlm(), name="vlm-restart")
                self.queue_synopsis(event_id)   # not the event's fault: it goes back to the queue (behind the restart)
                continue
            except Exception as ex:
                log.exception("synopsis %s failed", event_id)
                self.synopsis_failed_at = time.time()
                db.update_event(event_id, error=synopsis_error(db.event(event_id), ex))
            finally:
                self.synopsis_pending.discard(event_id)
            self.publish(event_id)
            # Link across cameras after the synopsis, so Qwen can also compare both descriptions.
            await self.journey_q.put(event_id)

    RETRY_MAX = 3            # attempts per event before it stays undescribed (the error is shown on the card)
    RETRY_BACKOFF_S = 300    # after a failure, wait this long before trying the backlog again
    RETRY_BATCH = 10         # per minute at most: the backlog must not crowd out live events on a shared model
    RETRY_WINDOW_S = 7 * 86400

    def retry_failed_synopses(self, limit: int = RETRY_BATCH) -> int:
        """Requeue verified events whose synopsis failed (Qwen or the remote AI was down): newest first, at most
        RETRY_MAX attempts each, only events from the last week that this camera still wants described."""
        n = 0
        for r in db.all("SELECT id FROM events WHERE status='verified' AND synopsis IS NULL AND error LIKE 'synopsis%' "
                        "AND start_ts > ? ORDER BY start_ts DESC", [time.time() - self.RETRY_WINDOW_S]):
            e = db.event(r["id"])
            if not e or synopsis_attempts(e) >= self.RETRY_MAX or not self.wants_synopsis(e):
                continue
            if self.queue_synopsis(e["id"]):
                n += 1
                if n >= limit:
                    break
        if n:
            log.info("Qwen is back: retrying %d events whose synopsis failed", n)
        return n

    def retry_due(self, now: float | None = None) -> bool:
        """Retry only when the model is ready, the live queue is idle, nothing failed recently, and (remote AI) the
        router's circuit breaker is not open. Keeps a dead remote from being hammered and live events first."""
        now = now or time.time()
        if not self.vlm_ready or self.synopsis_pending or now - self.synopsis_failed_at < self.RETRY_BACKOFF_S:
            return False
        return now >= vlmroute.router.down_until

    async def retry_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                if self.retry_due():
                    self.retry_failed_synopses()
            except Exception:
                log.exception("synopsis retry pass failed")

    async def journey_loop(self) -> None:
        while True:
            event_id = await self.journey_q.get()
            while not self.vlm_ready:
                await asyncio.sleep(5)
            try:
                if await journeys.link_event(self.gate, event_id):
                    for m in db.all("SELECT id FROM events WHERE journey_id=(SELECT journey_id FROM events WHERE id=?)", [event_id]):
                        self.publish(m["id"])
            except Exception:
                log.exception("journey linking for %s failed", event_id)

    async def relink(self, days: float = 7) -> int:
        """Queue every recent person event for cross-camera linking (e.g. after the topology changed)."""
        rows = db.all("SELECT id FROM events WHERE camera_class='person' AND status='verified' AND start_ts > ? "
                      "AND id IN (SELECT rowid FROM reid_vec) ORDER BY start_ts", [time.time() - days * 86400])
        for r in rows:
            await self.journey_q.put(r["id"])
        return len(rows)

    async def _cells_backfill(self) -> None:
        """One-time: region cells for events recorded before the paint-a-region filter existed."""
        try:
            n = await asyncio.to_thread(cells.backfill)
            if n:
                log.info("region cells: backfilled %d events", n)
        except Exception:
            log.exception("region cells backfill failed")

    async def _vehicle_backfill(self) -> None:
        """One-time: CLIP fingerprints for vehicles verified before they existed (from their saved crops)."""
        try:
            clip = await self.get_clip()
            n = await asyncio.get_running_loop().run_in_executor(self.gpu, identities.backfill_vehicles, clip)
            if n:
                log.info("vehicle fingerprints: embedded %d events", n)
        except Exception:
            log.exception("vehicle fingerprint backfill failed")

    async def _reid_backfill(self) -> None:
        """One-time: re-ID vectors for person events verified before re-ID existed (from their saved crops)."""
        def work():
            from .reid import ReID
            if self.verifier._reid is None:
                self.verifier._reid = ReID()
            return journeys.backfill(self.verifier._reid)
        try:
            n = await asyncio.get_running_loop().run_in_executor(self.gpu, work)
            if n:
                log.info("re-ID backfill: embedded %d person events", n)
        except Exception:
            log.exception("re-ID backfill failed")

    async def _synopsis(self, event_id: int) -> None:
        e = db.event(event_id)
        if not e or e["synopsis"]:
            return
        d = event_dir(event_id)
        images = [(d / k["file"]).read_bytes() for k in (e["detections"] or {}).get("keyframes", [])
                  if (d / k["file"]).exists()][: settings.synopsis_images]
        if not images:
            return
        camera = self.cameras.get(e["camera_id"], {"name": e["camera_id"]})
        t0 = time.time()
        result = await vlm.synopsis(e, camera, images, self.correction_examples(e["camera_id"], label=e["camera_class"]))
        result["model"] = result.pop("_model", None)  # shown as a small tag in the event viewer
        summary = result.get("summary", "").strip()
        summary = zones.apply_door_facts(summary, e)  # entries/exits through named doors come from the track, not the model
        names = [a["name"] for a in (e.get("areas") or []) if a["name"].lower() not in summary.lower()]
        if names:  # the model sometimes ignores the operator's place names: state them anyway, as "went to" because
            # a door place visited and left again (event 8377) is not an entry; entries/exits come from apply_door_facts
            summary = (summary.rstrip(".") + ". " if summary else "") + "Went to " + ", then ".join(dict.fromkeys(names)) + "."
        result["summary"] = summary
        # A weapon in the description is checked at full resolution BEFORE anything is stored: the threat, priority
        # and the hub's high-priority alert only ever see the checked wording (event 9118: a towel read as a handgun).
        result, original = await weaponcheck.review(e, result, lambda: self.weapon_evidence(e))
        summary = result.get("summary", summary)
        extra = {"synopsis_original": original} if original is not None and not e.get("synopsis_original") else {}
        db.update_event(event_id, synopsis=summary, synopsis_json=result, threat=result.get("threat_level"), error=None, **extra)
        if not e.get("ptz_preset"):
            await policy.confirm_towing(event_id)   # a towing claim needs a second, focused look before a rule can break
            policy.check(event_id, camera)          # site rules (who may tow what) now that Qwen has looked
            baseline.apply(event_id, rescore=False)  # threat changed: update priority
        await self.reindex(event_id)
        self.synopsis_times.append(round(time.time() - t0, 1))
        log.info("event %s synopsis in %.1fs: %s", event_id, time.time() - t0, summary[:120])

    async def weapon_evidence(self, e: dict) -> dict:
        """Full-resolution crops of every person in the event for weaponcheck: the clip is decoded on a decode thread,
        YOLO (the verifier's model) runs on the GPU thread like every other YOLO call."""
        model = self.verifier.model if self.verifier else None
        predict = None
        if model is not None:
            predict = lambda imgs: self.gpu.submit(weaponcheck.yolo_predict, model, imgs).result(  # noqa: E731
                timeout=weaponcheck.COLLECT_TIMEOUT_S)
        return await asyncio.get_running_loop().run_in_executor(self.decode, weaponcheck.gather_evidence, e, predict)

    @staticmethod
    def correction_examples(camera_id: str, n: int = 3, label: str | None = None) -> list[dict]:
        """Most recent operator corrections on this camera, used as few-shot guidance. Events now outside the
        zones (masked) are skipped: e.g. a corrected "routine highway traffic" example would otherwise teach Qwen
        to call every vehicle in the yard highway traffic. Corrections of the same label come first."""
        rows = db.all("SELECT synopsis_original, synopsis_json FROM events WHERE camera_id=? AND corrected_at "
                      "IS NOT NULL AND synopsis_original IS NOT NULL AND status != 'masked' "
                      "ORDER BY (camera_class = ?) DESC, corrected_at DESC LIMIT ?", [camera_id, label or "", n])
        out = []
        for r in rows:
            orig, corr = json.loads(r["synopsis_original"]), json.loads(r["synopsis_json"])
            fmt = lambda j: f"{j.get('summary', '')} [threat: {j.get('threat_level', '?')}]"
            if fmt(orig) != fmt(corr):
                out.append({"original": fmt(orig), "corrected": fmt(corr)})
        if len(out) < n:  # a camera moved here from another site brings that site's corrections (siteconfig.merge_cameras)
            seeds = db.get_setting(f"correction_seed:{camera_id}") or []
            seeds = sorted(seeds, key=lambda s: (s.get("label") != (label or ""), -(s.get("at") or 0)))
            seen = {(o["original"], o["corrected"]) for o in out}
            for s in seeds:
                if len(out) >= n:
                    break
                if (s.get("original"), s.get("corrected")) not in seen and s.get("original") and s.get("corrected"):
                    out.append({"original": s["original"], "corrected": s["corrected"]})
                    seen.add((s["original"], s["corrected"]))
        return out

    async def reindex(self, event_id: int) -> None:
        """Rebuild the search document: synopsis (corrected if edited), saved chat notes, feedback, labels."""
        e = db.event(event_id)
        if not e or e["status"] != "verified" and not e["synopsis"]:
            return
        s = e.get("synopsis_json") or {}
        fb = e.get("feedback") or {}
        notes = [m["content"] for m in db.chat(event_id) if m["saved"]]
        if e.get("areas"):  # named places, so "did anyone use the bathroom" finds "entered Bathroom 2"
            notes.append("Went to: " + ", ".join(f"entered {a['name']}" for a in e["areas"]))
        an = e.get("anomaly_json") or {}
        if an.get("reasons"):  # so "unusual activity" / "at night" style searches find it
            notes.append("Unusual for this camera: " + "; ".join(an["reasons"]))
        if e.get("policy"):  # a broken site rule, so "unknown truck towing" finds it
            notes.append("Site rule broken: " + e["policy"]["text"])
        ppe_res = (e.get("detections") or {}).get("ppe")
        if ppe_res and ppe_res.get("verdict") == "compliant":  # so "who wore a hard hat in the yard" finds it too
            notes.append(f"Wore the required PPE in '{ppe_res.get('zone')}': "
                         + ", ".join(ppe.ITEM_WORDS[i] for i in ppe_res.get("required") or []))
        if e.get("ptz_preset"):  # so "when the camera was on the parking lot" finds it
            notes.append("Camera turned away from its usual view" + (f" (at preset '{e['ptz_preset']}')" if e["ptz_preset"] != "away" else ""))
        if e.get("journey_id"):  # cross-camera narrative, so e.g. "went outside" finds every leg
            j = db.one("SELECT synopsis FROM journeys WHERE id=?", [e["journey_id"]])
            if j and j["synopsis"]:
                notes.append(j["synopsis"])
        labels = " ".join(filter(None, [
            *(a["name"] for a in (e.get("areas") or [])),
            e["camera_class"], e["yolo_class"], *s.get("tags", []), *(o.get("type", "") for o in s.get("objects", [])),
            *ppe.tags(ppe_res),
            (fb.get("verdict") or "").replace("_", " "), fb.get("correct_class"),
        ]))
        doc = " ".join(filter(None, [
            e["synopsis"], s.get("activity"), s.get("threat_reason"),
            *(o.get("description", "") for o in s.get("objects", [])), *notes, fb.get("note"), labels,
        ]))
        db.index_event_text(event_id, doc, labels, await vlm.embed(f"search_document: {doc}"))

    async def chat_frames(self, e: dict, at: float | None) -> list[tuple[float, bytes, str]]:
        """Frames for a chat turn: an overview across the track, or a close look around clip time `at`.

        Returns [(clip_time, jpeg_bytes, filename)]; files are cached in the event folder.
        """
        clip = settings.data_dir / (e["clip"] or "")
        if not e["clip"] or not clip.exists():
            return []
        clip_start = e.get("clip_start") or e["start_ts"] - settings.clip_pre_roll
        end = (e["end_ts"] or e["start_ts"]) - clip_start
        # Ollama sizes every image to ~1,050 Qwen tokens, so 4 frames + prompt + history fit in vlm_num_ctx.
        n = settings.chat_frames
        if at is None:
            first, last, width = max(0.0, e["start_ts"] - clip_start), max(end, 0.5), 640
            times = [round(first + (last - first) * i / (n - 1), 2) for i in range(n)]
        else:
            offsets = (-1.0, 0.0, 0.6, 1.2, -2.0, 2.0)[:n]
            times, width = [round(max(0.0, at + d), 2) for d in offsets], 896
        times = sorted(set(times))
        d = event_dir(e["id"])

        def work():
            cached = {t: d / f"chat_{int(t * 1000)}_{width}.jpg" for t in times}
            missing = [t for t, f in cached.items() if not f.exists()]
            if missing:
                for t, img in grab_frames(clip, 0.0, missing).items():
                    scale = width / img.shape[1]
                    img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                    cv2.putText(img, f"t={t:.1f}s", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 4)
                    cv2.putText(img, f"t={t:.1f}s", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
                    cv2.imwrite(str(cached[t]), img, [cv2.IMWRITE_JPEG_QUALITY, 85])
            return [(t, f.read_bytes(), f.name) for t, f in cached.items() if f.exists()]

        return await asyncio.get_running_loop().run_in_executor(self.decode, work)

    async def start_vlm(self) -> None:
        if not settings.local_vlm_enabled:
            # No Ollama here: Qwen work is remote (hub shared AI or NVR_REMOTE_VLM_*). Ready whenever a remote is
            # configured; the hub may push that config after start-up, so keep looking.
            while True:
                ready = vlmroute.router.configured
                if ready != self.vlm_ready:
                    log.info("remote VLM %s", f"ready: {settings.remote_vlm_model}" if ready else "not configured; Qwen tasks wait")
                self.vlm_ready, self.vlm_state = ready, "ready" if ready else "starting"
                await asyncio.sleep(30)
        roles = ["primary", "fallback"] if settings.fallback_vlm_enabled else ["primary"]
        ok = await asyncio.gather(*(self._start_local(r) for r in roles))
        if settings.fallback_vlm_enabled:
            # with two models the other one serves meanwhile: keep retrying the one that didn't come up
            for role, up in zip(roles, ok):
                if not up:
                    asyncio.create_task(self.restart_vlm(role), name=f"vlm-restart-{role}")

    def _set_local(self, role: str, state: str) -> None:
        """A local model changed state. vlm_state / vlm_down_since mirror the primary (Settings → System, the
        advisor); vlm_ready means some local model can answer (the primary, or the fallback while it is down)."""
        models = vlmroute.router.models
        models[role].set_state(state)
        if role == "primary":
            self.vlm_state, self.vlm_down_since = state, models["primary"].down_since
        self.vlm_ready = any(m.configured and m.state == "ready" for m in models.values())

    async def _start_local(self, role: str = "primary") -> bool:
        """Wait for that instance's `ollama serve`, pull and warm its model, check it is all in VRAM."""
        m = vlmroute.router.models[role]
        if await vlm.wait_ready(role=role):
            try:
                await vlm.ensure_models(role)
                log.info("loading %s (%s) into VRAM...", m.model, role)
                log.info("VLM ready: %s (%s, loaded and warmed in %.0fs)", m.model, role, await vlm.warm_up(role))
                await vlm.check_vram(role)
                if role == "primary":
                    self.vlm_timeouts = 0
                self._set_local(role, "ready")
                return True
            except Exception:
                log.exception("could not prepare Ollama models (%s)", role)
        else:
            log.error("Ollama did not start at %s", m.url)
        m.failed_start()
        return False

    async def restart_vlm(self, role: str = "primary") -> None:
        """Qwen stopped answering: restart that instance's Ollama and warm its model; keep trying every 5 min while
        it stays down (a GPU that fell off the bus needs a reboot, which Settings → System and the advisor say).
        With a fallback model configured the other instance answers meanwhile (vlmroute)."""
        m = vlmroute.router.models[role]
        if m.state == "unresponsive" or not settings.local_vlm_enabled or not m.configured:
            return   # nothing local to restart: the router's circuit breaker handles a remote outage
        self._set_local(role, "unresponsive")
        log.error("Qwen (%s, %s) is not answering: restarting its Ollama", role, m.model)
        while m.state != "ready":
            try:
                if self.ollama is not None:
                    await self.ollama.stop(role)   # its supervisor loop starts a fresh `ollama serve` in 5 s
                    await asyncio.sleep(8)
                await asyncio.wait_for(self._start_local(role), 240)
            except Exception as e:
                log.warning("Qwen (%s) restart did not succeed: %s", role, e)
            if m.state != "ready":
                log.error("Qwen (%s) still down (%.0f min); next try in 5 min. If nvidia-smi reports the GPU as lost, "
                          "reboot.", role, (time.time() - (m.down_since or time.time())) / 60)
                await asyncio.sleep(300)

    def synopsis_labels(self, camera_id: str) -> list[str]:
        """What Qwen describes on this camera: its own choice (Cameras -> Edit), else the site default."""
        cam = self.cameras.get(camera_id) or {}
        own = cam.get("synopsis_labels")
        labels = own if own is not None else settings.synopsis_labels
        return sorted(set(labels) | policy.labels_needed(cam))  # a site rule about vehicles needs them described

    def wants_synopsis(self, e: dict | None) -> bool:
        """Qwen describes a verified event if its label is chosen for the camera and, when the camera has include
        zones, YOLO confirmed it inside one (never for something only seen outside the zones)."""
        if not e or e["status"] != "verified" or e["camera_class"] not in self.synopsis_labels(e["camera_id"]):
            return False
        if e.get("ptz_preset"):
            # The zones describe the home view. Away from it, people are still worth describing; vehicles are
            # almost always public-road traffic the camera happens to be pointed at (and no site rule can apply).
            return e["camera_class"] == "person"
        zl = zones.normalize((self.cameras.get(e["camera_id"]) or {}).get("zones"))
        if not any(z["type"] == "include" for z in zl):
            return True
        samples = (e.get("detections") or {}).get("samples", [])
        return any(s.get("match") and zones.allowed(zones.foot(s["match"]["box"]), zl) for s in samples)

    async def check_ppe(self, event_id: int) -> dict | None:
        """People who stayed in a PPE zone: detector on the recorded frames, Qwen on doubt; stored in detections["ppe"]
        (policy.check turns a violation into the event's broken rule). Nothing runs on cameras without a PPE zone."""
        e = db.event(event_id)
        if not e or e["camera_class"] != "person":
            return None
        cam = self.cameras.get(e["camera_id"]) or {}
        det = dict(e.get("detections") or {})
        loop = asyncio.get_running_loop()
        try:
            res = await ppe.check_event(e, cam.get("zones"), lambda fn, *a: loop.run_in_executor(self.gpu, fn, *a),
                                        self.ppe, self.verifier.model if self.verifier else None, self.vlm_ready)
        except Exception as ex:  # a PPE problem must not lose the event
            log.exception("PPE check for event %s failed", event_id)
            res = {"verdict": "error", "error": str(ex)[:200], "violation": [], "checked_at": round(time.time(), 1)}
        if res is None:
            if "ppe" in det:  # the zone was removed or the person no longer stays long enough: drop the old answer
                det.pop("ppe")
                db.update_event(event_id, detections=det)
            return None
        images = res.pop("_images", None) or {}
        if images:
            res["marked"] = await loop.run_in_executor(self.decode, ppe.save_marked, event_id, res, images)
        det["ppe"] = res
        db.update_event(event_id, detections=det)
        log.info("event %s PPE in '%s': %s%s (%s)", event_id, res.get("zone"), res["verdict"],
                 f" {res['violation']}" if res.get("violation") else "", "Qwen asked" if res.get("vlm") else "detector only")
        return res

    def record_areas(self, event_id: int) -> list[dict]:
        """Store which named areas (zones of type "area") the object walked into."""
        e = db.event(event_id)
        if not e:
            return []
        visited = zones.areas_visited(e["path"], (self.cameras.get(e["camera_id"]) or {}).get("zones"))
        db.update_event(event_id, areas=visited or None)
        return visited

    def queue_missing_synopses(self, camera_id: str) -> int:
        """After a camera's Qwen choice changes: describe its verified, in-zone events that have no synopsis yet."""
        n = 0
        for r in db.all("SELECT id FROM events WHERE camera_id=? AND status='verified' AND synopsis IS NULL "
                        "AND error IS NULL ORDER BY start_ts DESC", [camera_id]):
            if self.wants_synopsis(db.event(r["id"])):
                self.queue_synopsis(r["id"])
                n += 1
        if n:
            log.info("[%s] queued %d events for Qwen", camera_id, n)
        return n

    def recover(self) -> None:
        """Requeue work interrupted by a restart, and describe verified events whose camera now wants Qwen."""
        for r in db.all("SELECT id, status FROM events WHERE status IN ('open','pending') "
                        "OR (status='verified' AND synopsis IS NULL AND error IS NULL)"):
            if r["status"] == "verified" and not self.wants_synopsis(db.event(r["id"])):
                continue
            if r["status"] == "open":
                db.update_event(r["id"], status="pending",
                                end_ts=db.one("SELECT start_ts FROM events WHERE id=?", [r["id"]])["start_ts"])
            if r["status"] in ("open", "pending"):
                self.verify_q.put_nowait(r["id"])
            else:
                self.queue_synopsis(r["id"])
