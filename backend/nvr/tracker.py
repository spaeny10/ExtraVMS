"""Turns per-frame camera detections into object tracks, and tracks into events.

A camera ObjectId that persists for `track_min_seconds` opens an event (status=open, visible
live in the UI). When the object disappears for `track_end_gap` seconds the event closes and
goes to YOLO verification (status=pending). Rule events (intrusion, line crossing...) that fire
while a track is alive are attached to it.

Cameras that send no object metadata (ruleevents.py): their ONVIF detection events (Reolink PeopleDetect true/false)
open and close events themselves, without boxes; the verifier finds the object with YOLO.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from . import ruleevents, zones
from .config import settings
from . import cells
from .db import db
from .ingest import MetaFrame, RuleEvent

log = logging.getLogger("nvr.tracker")

# Camera class names -> our canonical labels. Anything else is ignored.
CLASS_MAP = {"human": "person", "person": "person", "face": "person",
             "vehicle": "vehicle", "car": "vehicle", "truck": "vehicle", "bus": "vehicle",
             "motorcycle": "vehicle", "bicycle": "vehicle", "nonmotor": "vehicle"}


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
        self.watch = ruleevents.SourceWatch()

    def set_zones(self, camera_id: str, zone_list: list[dict]) -> None:
        self.zones[camera_id] = zones.normalize(zone_list)

    def _zone_hit(self, camera_id: str, box) -> bool:
        return zones.allowed(zones.foot(box), self.zones.get(camera_id, []))

    def on_frame(self, f: MetaFrame) -> None:
        now = time.time()
        if any(CLASS_MAP.get(o.cls.lower()) for o in f.objects):
            self.watch.objects(f.camera_id, now)
        else:
            self.watch.heard(f.camera_id, now)
        for o in f.objects:
            label = CLASS_MAP.get(o.cls.lower())
            if not label:
                continue
            key = (f.camera_id, o.object_id)
            t = self.tracks.get(key)
            if t is None or t.label != label:
                t = self.tracks[key] = Track(f.camera_id, o.object_id, label, f.ts, f.ts, now)
            t.last_ts, t.last_wall = f.ts, now
            t.max_conf = max(t.max_conf, o.conf)
            t.path.append([round(f.ts, 3), *(round(v, 4) for v in o.box), o.conf])
            away = self.away_preset(f.camera_id)
            if away and not t.away:
                t.away = away
            # zones are drawn for the home view: while the camera is turned away, record everything it sees
            t.in_zone = t.in_zone or away is not None or self._zone_hit(f.camera_id, o.box)
            if t.event_id is None and t.in_zone and t.last_ts - t.first_ts >= settings.track_min_seconds:
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
        return self.watch.decide(self.camera(camera_id), camera_id, now or time.time(), no_track)

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
        if e.state is False:   # the camera says it's gone: sweep closes it after track_end_gap (unless it comes back)
            if t is not None and t.on:
                t.on, t.last_ts, t.last_wall = False, max(t.last_ts, now), now
                t.path.append(ruleevents.point(now))
                t.rules.append({**rule, "state": False})
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
        if t is None:
            t = Track(cid, key[1], label, now, now, now, source=ruleevents.SOURCE, in_zone=True,
                      on=not pulse, last_on=now, away=self.away_preset(cid))
            t.path.append(ruleevents.point(now))
            t.rules.append(rule)
            self.tracks[key] = t
            self._open_rule_event(t)
            return
        # again while open: extend
        t.last_ts, t.last_wall = max(t.last_ts, now), now
        if not pulse:
            t.on, t.last_on = True, now
        t.path.append(ruleevents.point(now))
        t.rules.append(rule)

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

    async def sweep(self) -> None:
        """Close tracks that went quiet or ran too long. Call about once a second."""
        now = time.time()
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
            del self.tracks[key]
            if t.event_id is None:
                continue  # too short / never entered a zone: noise
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
            if too_long and not quiet:
                # keep following the same object as a new event
                nt = self.tracks[key] = Track(t.camera_id, t.object_id, t.label, t.last_ts, t.last_ts, t.last_wall,
                                              in_zone=t.in_zone, source=t.source, on=t.on, last_on=t.last_on)
                if rule:   # the camera still says it's there: the next event opens now, like the first one did
                    nt.away = self.away_preset(t.camera_id)
                    nt.path.append(ruleevents.point(nt.first_ts))
                    self._open_rule_event(nt)
