"""Turns per-frame camera detections into object tracks, and tracks into events.

A camera ObjectId that persists for `track_min_seconds` opens an event (status=open, visible
live in the UI). When the object disappears for `track_end_gap` seconds the event closes and
goes to YOLO verification (status=pending). Rule events (intrusion, line crossing...) that fire
while a track is alive are attached to it.

Cameras that send no object metadata (ruleevents.py): their ONVIF detection events (Reolink PeopleDetect true/false)
open and close events themselves, without boxes; the verifier finds the object with YOLO.

Parked vehicles: a vehicle track whose event the verifier rejected, or whose event the track_max_seconds split closed
while the track went on, is held: it opens no new event while it stays where it was (parked_track_move / _iou), until
parked_track_hold_s has passed. Before this a parked car opened, and lost, an event every minute all day.

An open event that no live track owns any more is closed by the sweep after orphan_event_after_s (and at start-up),
so it is verified instead of staying "open" for ever.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from . import ingest, parked, ruleevents, zones
from .config import settings
from . import cells
from .db import db
from .ingest import MetaFrame, RuleEvent

log = logging.getLogger("nvr.tracker")

# Camera class names -> our canonical labels. Anything else is ignored.
CLASS_MAP = {"human": "person", "person": "person", "face": "person",
             "vehicle": "vehicle", "car": "vehicle", "truck": "vehicle", "bus": "vehicle",
             "motorcycle": "vehicle", "bicycle": "vehicle", "nonmotor": "vehicle"}
HELD_LABELS = {"vehicle"}   # a person standing still is never "parked": their tracks are never held
ORPHAN_CHECK_S = 30.0       # how often the sweep looks for open events no track owns


def moved(held_box, box) -> bool:
    """The vehicle left the position it was held at: its box centre moved parked_track_move of the frame, or the
    boxes overlap less than parked_track_iou (camera boxes jitter a little around a parked car; this is more)."""
    (x1, y1), (x2, y2) = parked._center(held_box), parked._center(box)
    far = max(abs(x2 - x1), abs(y2 - y1)) > settings.parked_track_move
    return far or parked.iou(held_box, box) < settings.parked_track_iou


def last_sample(path, default: float) -> float:
    """The time of the last point of an event's path, or `default` when it has none."""
    for p in reversed(path or []):
        if isinstance(p, (list, tuple)) and p and isinstance(p[0], (int, float)):
            return float(p[0])
    return default


@dataclass
class Hold:
    """A vehicle track that opens no new event while it stays at `box` (normalized l, t, r, b)."""
    box: tuple
    since: float      # wall time it was held (renewed by a later rejection)
    why: str          # "rejected" | "split"


@dataclass
class Track:
    camera_id: str
    object_id: str
    label: str
    first_ts: float
    last_ts: float
    last_wall: float
    max_conf: float = 0.0
    path: list = field(default_factory=list)     # [ts, l, t, r, b, conf]
    rules: list = field(default_factory=list)
    in_zone: bool = False
    event_id: int | None = None
    away: str | None = None     # PTZ camera turned away from home when the track opened (preset name / "away")
    source: str = "metadata"    # ruleevents.SOURCE: opened by the camera's ONVIF detection event, no boxes
    on: bool = True             # rule-event track: the camera has not said state=false yet
    last_on: float = 0.0        # rule-event track: wall time of the last state=true
    # rule-event track: the camera's detectors (topic|rule) that said true and not false yet. Two topics can name one
    # label (Reolink PeopleDetect and FaceDetect): the track is on while any of them is, off when the last one says false
    states: set = field(default_factory=set)
    # rule-event track: wall time the camera's detector turned on (kept across the track_max_seconds split), and when
    # an event of this streak was rejected while the detector stayed on (0: not held). There is no box to tell a
    # parked car from a new one, so this hold lasts only while the camera keeps saying "on".
    streak: float = 0.0
    hold_since: float = 0.0


class Tracker:
    def __init__(self, on_closed):
        self.tracks: dict[tuple[str, str], Track] = {}
        self.zones: dict[str, list[dict]] = {}
        self.on_closed = on_closed  # async callback(event_id)
        # PTZ cameras (ptz.py): where the camera points now / during a window. Fixed cameras answer None.
        self.away_preset = lambda camera_id: None
        self.away_between = lambda camera_id, t0, t1: None
        # the camera's settings row (event_source, motion_events, enabled, synopsis_labels, streams); None = unknown
        self.camera = lambda camera_id: None
        self.on_opened = lambda event_id: None   # a rule-event track opened an event (the pipeline publishes it)
        self.metadata_missing = lambda camera_id: False   # its RTSP stream has no metadata track (ingest.MetadataReader)
        # when its metadata reader connected (0: not connected, None: no reader known): auto mode reads "no objects in
        # the metadata" as "the camera sends none" only while the reader is connected (a 401 must not look like that)
        self.metadata_reader = ingest.metadata_since
        self.watch = ruleevents.SourceWatch()
        self.holds: dict[tuple[str, str], Hold] = {}   # (camera, camera track id): parked vehicles (see moved)
        self._retired: list[Track] = []   # tracks whose id the camera gave to another kind of object: the sweep closes them
        self._orphans_at = time.time()    # the first orphan check waits a round: start-up closes the old ones (recover)

    def set_zones(self, camera_id: str, zone_list: list[dict]) -> None:
        self.zones[camera_id] = zones.normalize(zone_list)

    def _zone_hit(self, camera_id: str, box) -> bool:
        return zones.allowed(zones.foot(box), self.zones.get(camera_id, []))

    # ---- parked vehicles
    def hold(self, camera_id: str, object_id: str, box, why: str, now: float | None = None) -> None:
        """This vehicle track opens no new event while it stays at `box` (see moved), for parked_track_hold_s."""
        key = (camera_id, object_id)
        if key not in self.holds:
            log.info("[%s] vehicle track %s held where it is (%s): no new event until it moves", camera_id, object_id, why)
        self.holds[key] = Hold(tuple(box), now or time.time(), why)

    def _held(self, key: tuple[str, str], box, now: float) -> bool:
        h = self.holds.get(key)
        if h is None:
            return False
        if now - h.since >= settings.parked_track_hold_s:
            del self.holds[key]
            log.info("[%s] vehicle track %s held for %.0f min: it may open an event again", key[0], key[1], (now - h.since) / 60)
            return False
        if moved(h.box, box):
            del self.holds[key]
            log.info("[%s] vehicle track %s moved off where it was held", key[0], key[1])
            return False
        return True

    def rejected(self, e: dict, now: float | None = None) -> None:
        """The verifier rejected a vehicle event (for any reason: parked, 0 hits...). Its camera track is held where
        the event left it; a rule-event vehicle (no box) is held while the camera's detector stays on."""
        if not e or e.get("camera_class") not in HELD_LABELS:
            return
        now = now or time.time()
        if ruleevents.is_rule_event(e):
            t = self.tracks.get((e["camera_id"], ruleevents.TRACK_PREFIX + e["camera_class"]))
            # only the detector streak that event belongs to: after a false and a fresh true it is a new detection
            if t is not None and t.source == ruleevents.SOURCE and t.on and e["start_ts"] >= t.streak - 1:
                if not t.hold_since:
                    log.info("[%s] %s ONVIF event %s rejected while the camera still says it's there: no new event "
                             "until it says it's gone", e["camera_id"], e["camera_class"], e["id"])
                t.hold_since = now
            return
        box = next((p[1:5] for p in reversed(e.get("path") or []) if isinstance(p, (list, tuple)) and len(p) >= 5), None)
        if box is not None and e.get("track_id") is not None:
            self.hold(e["camera_id"], str(e["track_id"]), box, "rejected", now)

    def on_frame(self, f: MetaFrame) -> None:
        now = time.time()
        if any(CLASS_MAP.get(o.cls.lower()) for o in f.objects):
            self.watch.objects(f.camera_id, now)
        else:
            self.watch.heard(f.camera_id, now)
        cam = self.camera(f.camera_id) if f.objects else None
        for o in f.objects:
            label = CLASS_MAP.get(o.cls.lower())
            if not label or not ruleevents.opens(label, cam):
                continue   # not a label we know, or one this camera opens no events for ("Open events for")
            key = (f.camera_id, o.object_id)
            t = self.tracks.get(key)
            if t is None or t.label != label:
                if t is not None and t.event_id is not None:
                    # The camera gave this object id to another kind of object (T-304's NW PTZ reuses its ids for
                    # cars and people alike). Replacing the track used to drop its event, which then stayed "open"
                    # for ever: it is closed and verified on the next sweep like any track that ended.
                    self._retired.append(t)
                t = self.tracks[key] = Track(f.camera_id, o.object_id, label, f.ts, f.ts, now)
            t.last_ts, t.last_wall = f.ts, now
            t.max_conf = max(t.max_conf, o.conf)
            t.path.append([round(f.ts, 3), *(round(v, 4) for v in o.box), o.conf])
            away = self.away_preset(f.camera_id)
            if away and not t.away:
                t.away = away
            # zones are drawn for the home view: while the camera is turned away, record everything it sees
            t.in_zone = t.in_zone or away is not None or self._zone_hit(f.camera_id, o.box)
            if t.event_id is None and t.label in HELD_LABELS and self._held(key, o.box, now):
                # a parked vehicle that was rejected (or already had its event): it counts again from the frame it
                # moves on, so the event that follows starts when it drove off, not when it parked
                t.first_ts, t.path = f.ts, t.path[-1:]
                continue
            if t.event_id is None and t.in_zone and t.last_ts - t.first_ts >= settings.track_min_seconds:
                if self._rule_event_open(f.camera_id):
                    # never both: the camera's ONVIF event already opened this visit's event (auto mode had switched
                    # to its events while no objects came). This track counts again from now, so it opens an event
                    # of its own only if the object is still there after that one ends.
                    t.first_ts, t.path = f.ts, t.path[-1:]
                    continue
                t.event_id = db.create_event(
                    camera_id=t.camera_id, track_id=t.object_id, camera_class=t.label,
                    camera_conf=t.max_conf, start_ts=t.first_ts, path=t.path, status="open",
                    cells=cells.for_event(t.path), ptz_preset=t.away,
                )
                log.info("[%s] event %s opened: %s track %s", t.camera_id, t.event_id, t.label, t.object_id)

    def on_rule_event(self, e: RuleEvent) -> None:
        if e.initial:
            return  # a state dump at subscription time, not something that happened
        db.execute("INSERT INTO rule_events (camera_id, ts, topic, rule, state, data) VALUES (?,?,?,?,?,?)",
                   [e.camera_id, e.ts, e.topic, e.rule, None if e.state is None else int(e.state), str(e.data)])
        if e.state:
            for t in self.tracks.values():
                if t.camera_id == e.camera_id and t.source == "metadata":
                    t.rules.append({"ts": e.ts, "topic": e.topic, "rule": e.rule})
        kind = ruleevents.classify(e.topic, e.data)
        if kind is None:
            self.watch.heard(e.camera_id, e.received)
            return
        self._rule_track(e, *kind)

    # ---- cameras without object metadata: events from their ONVIF detection events
    def uses_rule_events(self, camera_id: str, now: float | None = None) -> tuple[bool, str]:
        try:
            no_track = bool(self.metadata_missing(camera_id))
        except Exception:  # noqa: BLE001 - the ingest may be restarting
            no_track = False
        try:
            reader = self.metadata_reader(camera_id)
        except Exception:  # noqa: BLE001
            reader = None
        return self.watch.decide(self.camera(camera_id), camera_id, now or time.time(), no_track, reader)

    def _rule_event_open(self, camera_id: str) -> bool:
        return any(t.camera_id == camera_id and t.source == ruleevents.SOURCE and t.event_id is not None
                   for t in self.tracks.values())

    def _object_rule_track(self, camera_id: str) -> Track | None:
        """The newest open rule-event track of an object (person, vehicle...) on the camera: not motion."""
        return max((t for t in self.tracks.values() if t.camera_id == camera_id and t.source == ruleevents.SOURCE
                    and t.label != "motion" and t.event_id is not None), key=lambda t: t.first_ts, default=None)

    def detection_status(self, camera_id: str) -> dict:
        """Settings → Cameras: where this camera's detections come from now, and why."""
        cam = self.camera(camera_id)
        on, why = self.uses_rule_events(camera_id)
        return {"setting": ruleevents.setting(cam), "source": "onvif_events" if on else "metadata", "reason": why,
                "motion_events": bool((cam or {}).get("motion_events"))}

    def _rule_track(self, e: RuleEvent, kind: str, label: str) -> None:
        cid, now = e.camera_id, e.received
        if kind == "object" and e.state is not False:
            self.watch.detection(cid, now)
        else:
            self.watch.heard(cid, now)
        key = (cid, ruleevents.TRACK_PREFIX + label)
        t = self.tracks.get(key)
        rule = {"ts": round(now, 3), "topic": e.topic, "rule": e.rule}
        src = f"{e.topic}|{e.rule or ''}"   # one detector of the camera
        if e.state is False:
            if t is not None and t.on:
                t.rules.append({**rule, "state": False})
                t.states.discard(src)
                if not t.states:   # the last detector says it's gone: sweep closes it after track_end_gap (unless it comes back)
                    t.on, t.last_ts, t.last_wall = False, max(t.last_ts, now), now
                    t.path.append(ruleevents.point(now))
            return
        cam = self.camera(cid)
        if cam is not None and not cam.get("enabled", True):
            return
        if not self.uses_rule_events(cid, now)[0]:
            return   # the camera sends object metadata (or we can't tell yet): events come from its tracks
        if kind == "motion" and not (cam or {}).get("motion_events"):
            return   # motion alone is too noisy unless the camera is set to open events on it
        if not ruleevents.wanted(label, cam):
            return
        pulse = e.state is None   # e.g. a line crossing: no state, nothing will turn it off
        if kind == "motion":
            host = self._object_rule_track(cid)
            if host is not None:
                # one visit, one event: the person (vehicle...) event open on this camera takes the motion as one
                # more rule; it opens nothing of its own and doesn't keep that event on
                host.rules.append(rule)
                return
        elif t is None:
            m = self.tracks.get((cid, ruleevents.TRACK_PREFIX + "motion"))
            if m is not None and m.event_id is not None:
                t = self._upgrade_motion(m, key, label)
        if t is None:
            t = Track(cid, key[1], label, now, now, now, source=ruleevents.SOURCE, in_zone=True,
                      on=not pulse, last_on=now, away=self.away_preset(cid), streak=now)
            if not pulse:
                t.states.add(src)
            t.path.append(ruleevents.point(now))
            t.rules.append(rule)
            self.tracks[key] = t
            self._open_rule_event(t)
            return
        # again while open: extend
        t.last_ts, t.last_wall = max(t.last_ts, now), now
        if not pulse:
            t.on, t.last_on = True, now
            t.states.add(src)
        t.path.append(ruleevents.point(now))
        t.rules.append(rule)

    def _upgrade_motion(self, m: Track, key: tuple[str, str], label: str) -> Track:
        """The camera now says what its motion was (PeopleDetect after CellMotionDetector): the open motion event
        becomes the person (vehicle...) event rather than a second event for the same visit. Its motion detectors
        no longer keep it on; the object detector that upgraded it does (the caller turns it on, unless a pulse)."""
        del self.tracks[(m.camera_id, m.object_id)]
        m.object_id, m.label, m.states, m.on = key[1], label, set(), False
        self.tracks[key] = m
        db.update_event(m.event_id, camera_class=label, track_id=ruleevents.track_id(label, m.first_ts))
        log.info("[%s] event %s: the motion is a %s (the camera's ONVIF event)", m.camera_id, m.event_id, label)
        try:
            self.on_opened(m.event_id)
        except Exception:  # noqa: BLE001 - a UI push must not stop the tracker
            log.exception("publishing event %s failed", m.event_id)
        return m

    def _open_rule_event(self, t: Track) -> None:
        """Rule-event tracks open their event at once: the camera already decided there is a person. No zone check
        here (no box): the verifier masks the zones and keeps only YOLO's objects inside them."""
        t.event_id = db.create_event(
            camera_id=t.camera_id, track_id=ruleevents.track_id(t.label, t.first_ts), camera_class=t.label,
            camera_conf=0.0, start_ts=t.first_ts, path=t.path, rules=t.rules, status="open", cells=None,
            ptz_preset=t.away, detections={"source": ruleevents.SOURCE},
        )
        log.info("[%s] event %s opened: %s from the camera's ONVIF event %s", t.camera_id, t.event_id, t.label,
                 (t.rules[-1] if t.rules else {}).get("topic"))
        try:
            self.on_opened(t.event_id)
        except Exception:  # noqa: BLE001 - a UI push must not stop the tracker
            log.exception("publishing event %s failed", t.event_id)

    async def _close(self, t: Track) -> None:
        """The track ended (or was split): its event goes to verification."""
        rule = t.source == ruleevents.SOURCE
        if rule and (not t.path or t.path[-1][0] < round(t.last_ts, 3)):
            t.path.append(ruleevents.point(t.last_ts))   # the path spans the whole event
        # a camera that moved during the track can't be trusted against zones drawn for home
        away = self.away_between(t.camera_id, t.first_ts, t.last_ts) or t.away
        db.update_event(t.event_id, end_ts=t.last_ts, path=t.path, rules=t.rules,
                        camera_conf=t.max_conf, status="pending", cells=cells.for_event(t.path),
                        **({"ptz_preset": away} if away else {}))
        log.info("[%s] event %s closed after %.1fs (%d samples)", t.camera_id, t.event_id,
                 t.last_ts - t.first_ts, len(t.path))
        await self.on_closed(t.event_id)

    def close_orphans(self, older_than: float, now: float | None = None) -> list[int]:
        """Close open events that no live track owns any more, once their last sample is `older_than` seconds old:
        end_ts = that sample, status pending. Returns their ids; the caller sends them to verification. At start-up
        (older_than 0) that is every open event left from before the restart."""
        now = now or time.time()
        owned = {t.event_id for t in [*self.tracks.values(), *self._retired] if t.event_id is not None}
        out = []
        for r in db.all("SELECT id, camera_id, start_ts, created_at, path FROM events WHERE status='open'"):
            if r["id"] in owned:
                continue
            try:
                path = json.loads(r["path"]) if isinstance(r["path"], str) else r["path"]
            except ValueError:
                path = None
            last = max(last_sample(path, r["start_ts"]), r["start_ts"])
            # a camera clock ahead of this PC's would keep its event "recent" for ever: count from when it was opened
            seen = last if last <= now else (r["created_at"] or now)
            if now - seen < older_than:
                continue
            db.update_event(r["id"], end_ts=last, status="pending")
            log.warning("[%s] event %s was still open with no track (last seen %.0fs ago): closed for verification",
                        r["camera_id"], r["id"], now - seen)
            out.append(r["id"])
        return out

    async def sweep(self) -> None:
        """Close tracks that went quiet or ran too long, and events no track owns. Call about once a second."""
        now = time.time()
        retired, self._retired = self._retired, []
        for t in retired:   # their id now names another kind of object: closed as if they had ended
            await self._close(t)
        if now - self._orphans_at >= ORPHAN_CHECK_S:
            self._orphans_at = now
            for eid in self.close_orphans(settings.orphan_event_after_s, now):
                await self.on_closed(eid)
            for key, h in list(self.holds.items()):
                if now - h.since >= settings.parked_track_hold_s:
                    del self.holds[key]   # its track is long gone (or will open an event again when seen)
        for key, t in list(self.tracks.items()):
            rule = t.source == ruleevents.SOURCE
            if rule and t.on:
                # still there as far as the camera says; a state=false that never comes ends it after a timeout
                quiet = now - t.last_on > settings.rule_event_on_timeout
                too_long = now - t.first_ts > settings.track_max_seconds
                if quiet or too_long:
                    t.last_ts = max(t.last_ts, now)   # it was "on" until now
            else:
                quiet = now - t.last_wall > settings.track_end_gap
                too_long = t.last_ts - t.first_ts > settings.track_max_seconds
            if not (quiet or too_long):
                continue
            if rule and t.event_id is None and not quiet:
                # held after a rejection (see rejected) and the camera still says it's there: keep following it without
                # an event, or open one again once the hold has run out
                t.first_ts, t.path, t.rules = now, [ruleevents.point(now)], t.rules[-1:]
                if now - t.hold_since >= settings.parked_track_hold_s:
                    t.hold_since = 0.0
                    t.away = self.away_preset(t.camera_id)
                    self._open_rule_event(t)
                continue
            del self.tracks[key]
            if t.event_id is None:
                continue  # too short / never entered a zone: noise
            await self._close(t)
            if too_long and not quiet and (t.on or not rule):
                # keep following the same object as a new event (a rule-event track only while the camera still says
                # it's there: after its state=false the next true opens a new event the usual way)
                # an object track earns its zone pass again from its own positions: a camera that reuses one object
                # id for every car (T-304's NW PTZ, 2026-10-17) otherwise carried one old pass outside a mask into
                # every 60 s event after it, highway traffic included. A rule-event track has no positions: it keeps it.
                nt = self.tracks[key] = Track(t.camera_id, t.object_id, t.label, t.last_ts, t.last_ts, t.last_wall,
                                              in_zone=t.in_zone if rule else False, source=t.source, on=t.on,
                                              last_on=t.last_on, states=set(t.states), streak=t.streak,
                                              hold_since=t.hold_since)
                if rule:
                    nt.away = self.away_preset(t.camera_id)
                    nt.path.append(ruleevents.point(nt.first_ts))
                    if t.hold_since and now - t.hold_since < settings.parked_track_hold_s:
                        log.info("[%s] %s ONVIF event still on after a rejected event: followed without a new event",
                                 t.camera_id, t.label)
                    else:   # the camera still says it's there: the next event opens now, like the first one did
                        nt.hold_since = 0.0
                        self._open_rule_event(nt)
                elif t.label in HELD_LABELS and (box := next((p[1:5] for p in reversed(t.path) if len(p) >= 5), None)):
                    # a vehicle still there after its event: one event for it, not one a minute while it stays parked
                    # (merge folded verified ones only up to merge_max_seconds; rejected ones every minute, all day)
                    self.hold(t.camera_id, t.object_id, box, "split", now)
