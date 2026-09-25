"""Turns per-frame camera detections into object tracks, and tracks into events.

A camera ObjectId that persists for `track_min_seconds` opens an event (status=open, visible
live in the UI). When the object disappears for `track_end_gap` seconds the event closes and
goes to YOLO verification (status=pending). Rule events (intrusion, line crossing...) that fire
while a track is alive are attached to it.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from . import zones
from .config import settings
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


class Tracker:
    def __init__(self, on_closed):
        self.tracks: dict[tuple[str, str], Track] = {}
        self.zones: dict[str, list[dict]] = {}
        self.on_closed = on_closed  # async callback(event_id)

    def set_zones(self, camera_id: str, zone_list: list[dict]) -> None:
        self.zones[camera_id] = zones.normalize(zone_list)

    def _zone_hit(self, camera_id: str, box) -> bool:
        return zones.allowed(zones.foot(box), self.zones.get(camera_id, []))

    def on_frame(self, f: MetaFrame) -> None:
        now = time.time()
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
            t.in_zone = t.in_zone or self._zone_hit(f.camera_id, o.box)
            if t.event_id is None and t.in_zone and t.last_ts - t.first_ts >= settings.track_min_seconds:
                t.event_id = db.create_event(
                    camera_id=t.camera_id, track_id=t.object_id, camera_class=t.label,
                    camera_conf=t.max_conf, start_ts=t.first_ts, path=t.path, status="open",
                )
                log.info("[%s] event %s opened: %s track %s", t.camera_id, t.event_id, t.label, t.object_id)

    def on_rule_event(self, e: RuleEvent) -> None:
        db.execute("INSERT INTO rule_events (camera_id, ts, topic, rule, state, data) VALUES (?,?,?,?,?,?)",
                   [e.camera_id, e.ts, e.topic, e.rule, None if e.state is None else int(e.state), str(e.data)])
        if not e.state:
            return
        for t in self.tracks.values():
            if t.camera_id == e.camera_id:
                t.rules.append({"ts": e.ts, "topic": e.topic, "rule": e.rule})

    async def sweep(self) -> None:
        """Close tracks that went quiet or ran too long. Call about once a second."""
        now = time.time()
        for key, t in list(self.tracks.items()):
            quiet = now - t.last_wall > settings.track_end_gap
            too_long = t.last_ts - t.first_ts > settings.track_max_seconds
            if not (quiet or too_long):
                continue
            del self.tracks[key]
            if t.event_id is None:
                continue  # too short / never entered a zone: noise
            db.update_event(t.event_id, end_ts=t.last_ts, path=t.path, rules=t.rules,
                            camera_conf=t.max_conf, status="pending")
            log.info("[%s] event %s closed after %.1fs (%d samples)", t.camera_id, t.event_id,
                     t.last_ts - t.first_ts, len(t.path))
            await self.on_closed(t.event_id)
            if too_long and not quiet:
                # keep following the same object as a new event
                self.tracks[key] = Track(t.camera_id, t.object_id, t.label, t.last_ts, t.last_ts, t.last_wall,
                                         in_zone=t.in_zone)
