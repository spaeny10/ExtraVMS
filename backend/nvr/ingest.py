"""Per-camera ingest: ONVIF Profile M metadata (RTSP track) and Profile S/T rule events (PullPoint).

Both readers are blocking socket loops, so each runs in its own thread and hands parsed
results to the asyncio side via `loop.call_soon_threadsafe`.
"""
from __future__ import annotations

import asyncio
import collections
import datetime as dt
import logging
import socket
import statistics
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable

from . import mediamtx
from .config import settings
from .onvif_soap import ACTION_PULL, Onvif, OnvifError, escape, find, find_all, local, simple_items, text
from .rtsp_client import Rtsp, keepalive, play_track, rtp_payload

log = logging.getLogger("nvr.ingest")

ACTION_RENEW = "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewRequest"


@dataclass
class DetectedObject:
    object_id: str
    cls: str
    conf: float
    box: tuple[float, float, float, float]  # left, top, right, bottom, normalized 0..1, origin top-left


@dataclass
class MetaFrame:
    camera_id: str
    ts: float                      # epoch seconds (camera clock + configured offset)
    objects: list[DetectedObject] = field(default_factory=list)


@dataclass
class RuleEvent:
    camera_id: str
    ts: float
    topic: str
    rule: str | None
    state: bool | None
    data: dict
    initial: bool = False   # the subscription's state dump, not a transition (kept for relay / digital input)


def parse_utc(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_metadata(camera_id: str, xml: bytes, offset: float = 0.0) -> list[MetaFrame]:
    """Parse a metadata document. `offset` (seconds) is added to the camera's UtcTime."""
    root = ET.fromstring(xml)
    frames = []
    for frame in find_all(root, "Frame"):
        ts = parse_utc(frame.get("UtcTime"))
        if ts is None:
            continue
        # ONVIF: coordinates map to normalized [-1,1] (y up) via Transformation.
        tr, sc = find(frame, "Translate"), find(frame, "Scale")
        tx, ty = (float(tr.get("x")), float(tr.get("y"))) if tr is not None else (0.0, 0.0)
        sx, sy = (float(sc.get("x")), float(sc.get("y"))) if sc is not None else (1.0, 1.0)

        def to_img(x: float, y: float) -> tuple[float, float]:
            nx, ny = x * sx + tx, y * sy + ty
            return (nx + 1) / 2, (1 - ny) / 2

        objs = []
        for obj in (e for e in frame.iter() if local(e) == "Object"):
            box = find(obj, "BoundingBox")
            if box is None:
                continue
            x1, y1 = to_img(float(box.get("left")), float(box.get("top")))
            x2, y2 = to_img(float(box.get("right")), float(box.get("bottom")))
            types = [t for t in find_all(obj, "Type") if t.text]
            best = max(types, key=lambda t: float(t.get("Likelihood", 0)), default=None)
            cls = best.text.strip() if best is not None else (text(obj, "ObjectType") or "Unknown")
            conf = float(best.get("Likelihood", 0)) if best is not None else 0.0
            clamp = lambda v: min(max(v, 0.0), 1.0)
            objs.append(DetectedObject(
                object_id=obj.get("ObjectId"), cls=cls, conf=conf,
                box=(clamp(min(x1, x2)), clamp(min(y1, y2)), clamp(max(x1, x2)), clamp(max(y1, y2))),
            ))
        frames.append(MetaFrame(camera_id, ts + offset, objs))
    return frames


CLOCK_WINDOW = 4000        # metadata frames (~3-5 min) for the camera clock estimate
CLOCK_PERCENTILE = 0.05    # low percentile: the least-delayed arrivals show the true clock offset
SILENCE_LIMIT_S = 90  # no packets at all (RTP, RTCP or keepalive replies) for this long = dead session


class MetadataReader(threading.Thread):
    """Reads the vnd.onvif.metadata track from MediaMTX's proxy of the camera main stream.

    Camera clocks drift (seconds, even with NTP), but recordings are timestamped when MediaMTX receives
    them. So every detection is re-timed onto this PC's clock using (arrival time - camera UtcTime).
    Network and processing delays only ever make that difference larger (e.g. a burst of busy-highway
    metadata once backed this reader up by ~4 s), so the offset is a low percentile over a few minutes,
    not the median of the last few seconds.
    """

    def __init__(self, camera_id: str, on_frame: Callable[[MetaFrame], None]):
        super().__init__(name=f"meta-{camera_id}", daemon=True)
        self.camera_id = camera_id
        self.on_frame = on_frame
        self.stop_event = threading.Event()
        self.connected = False
        self.last_frame_at = 0.0
        self._deltas: collections.deque[float] = collections.deque(maxlen=CLOCK_WINDOW)
        self._since_update = 0
        self.clock_offset: float | None = None  # seconds to add to camera time to get PC time

    def _retime(self, raw_ts: float) -> float:
        self._deltas.append(time.time() - raw_ts)
        self._since_update += 1
        if self.clock_offset is None or self._since_update >= 20 or len(self._deltas) < 50:
            ordered = sorted(self._deltas)
            self.clock_offset = ordered[int(len(ordered) * CLOCK_PERCENTILE)]
            self._since_update = 0
        return raw_ts + self.clock_offset + settings.camera_clock_offset

    def run(self) -> None:
        backoff = 1
        while not self.stop_event.is_set():
            started = time.time()
            try:
                self._session()
            except (OSError, ConnectionError, LookupError, ET.ParseError) as e:
                if time.time() - started > 60:
                    backoff = 1  # the session was healthy for a while; reconnect quickly
                log.warning("[%s] metadata stream: %s; retry in %ss", self.camera_id, e, backoff)
            self.connected = False
            self.stop_event.wait(backoff)
            backoff = min(backoff * 2, 30)

    def _session(self) -> None:
        user, pw = mediamtx.reader_credentials() or ("", "")   # MediaMTX wants the NVR's reader password (rtsp_auth)
        cam = Rtsp(f"{settings.mediamtx_rtsp}/{self.camera_id}", user, pw)
        try:
            play_track(cam, "application")
            cam.sock.settimeout(5)
            self.connected = True
            log.info("[%s] metadata stream connected", self.camera_id)
            current, last_ka, last_data = b"", time.time(), time.time()
            while not self.stop_event.is_set():
                if time.time() - last_ka > 20:
                    keepalive(cam)
                    last_ka = time.time()
                try:
                    item = cam.read_interleaved()
                except socket.timeout:
                    # Cameras that only send metadata while objects are in view go quiet for long
                    # stretches; that's normal as long as the session itself is alive.
                    if time.time() - last_data > SILENCE_LIMIT_S:
                        raise ConnectionError(f"no packets for {SILENCE_LIMIT_S}s")
                    continue
                last_data = time.time()  # any RTP/RTCP packet or keepalive reply proves the session is alive
                if not item or item[0] != 0:
                    continue
                marker, payload = rtp_payload(item[1])
                current += payload
                if marker:
                    try:
                        for frame in parse_metadata(self.camera_id, current):
                            frame.ts = self._retime(frame.ts)
                            self.last_frame_at = time.time()
                            self.on_frame(frame)
                    except ET.ParseError as e:
                        log.debug("[%s] bad metadata doc: %s", self.camera_id, e)
                    current = b""
        finally:
            cam.sock.close()


class EventPuller(threading.Thread):
    """ONVIF PullPoint subscription on the camera for RuleEngine / VideoSource events."""

    def __init__(self, cam: dict, on_event: Callable[[RuleEvent], None]):
        super().__init__(name=f"events-{cam['id']}", daemon=True)
        self.cam = cam
        self.on_event = on_event
        self.stop_event = threading.Event()
        self.connected = False

    def run(self) -> None:
        backoff = 1
        while not self.stop_event.is_set():
            try:
                self._session()
                backoff = 1
            except (OnvifError, OSError) as e:
                log.warning("[%s] ONVIF events: %s; retry in %ss", self.cam["id"], e, backoff)
            self.connected = False
            self.stop_event.wait(backoff)
            backoff = min(backoff * 2, 60)

    def _session(self) -> None:
        c = self.cam
        onvif = Onvif(c["host"], c["onvif_port"], c["username"], c["password"])
        self._sync_clock(onvif)
        body = onvif.call(onvif.device_url, "<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability></tds:GetServices>")
        events_url = next((text(s, "XAddr") for s in find_all(body, "Service")
                           if text(s, "Namespace") == "http://www.onvif.org/ver10/events/wsdl"), None)
        if not events_url:
            raise OnvifError("camera has no events service")
        body = onvif.call(events_url, "<tev:CreatePullPointSubscription>"
                                      "<tev:InitialTerminationTime>PT300S</tev:InitialTerminationTime>"
                                      "</tev:CreatePullPointSubscription>")
        address = text(find(body, "SubscriptionReference"), "Address")
        if not address:
            raise OnvifError("no subscription address")
        self.connected = True
        log.info("[%s] ONVIF event subscription %s", c["id"], address)

        def wsa(action: str) -> str:
            return (f"<wsa:Action>{action}</wsa:Action><wsa:To>{escape(address)}</wsa:To>"
                    f"<wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID>")

        last_renew = time.time()
        while not self.stop_event.is_set():
            if time.time() - last_renew > 120:
                onvif.call(address, "<wsnt:Renew><wsnt:TerminationTime>PT300S</wsnt:TerminationTime></wsnt:Renew>",
                           header=wsa(ACTION_RENEW))
                last_renew = time.time()
            resp = onvif.call(address, "<tev:PullMessages><tev:Timeout>PT10S</tev:Timeout>"
                                       "<tev:MessageLimit>100</tev:MessageLimit></tev:PullMessages>",
                              header=wsa(ACTION_PULL), timeout=20)
            for n in find_all(resp, "NotificationMessage"):
                ev = self._parse(n)
                if ev:
                    self.on_event(ev)

    def _sync_clock(self, onvif: Onvif) -> None:
        from .onvif_soap import sync_clock
        offset = sync_clock(onvif)
        if abs(offset.total_seconds()) > 5:
            log.warning("[%s] camera clock is %.0fs off this PC (%s); set NTP on the camera",
                        self.cam["id"], offset.total_seconds(), self.cam.get("host"))

    def _parse(self, n: ET.Element) -> RuleEvent | None:
        topic = (text(n, "Topic") or "").split(":", 1)[-1]
        msg = find(n, "Message")
        inner = find(msg, "Message") if msg is not None else None
        if inner is None:
            return None
        initial = inner.get("PropertyOperation") == "Initialized"
        if initial and not ("DigitalInput" in topic or "Relay" in topic):
            return None  # initial state dump, not a transition (IO state is worth knowing at startup)
        source = simple_items(find(inner, "Source"))
        data = simple_items(find(inner, "Data"))
        state = next((v.lower() in ("true", "1", "active") for k, v in data.items()
                      if k.startswith("Is") or k in ("State", "LogicalState")), None)
        return RuleEvent(
            camera_id=self.cam["id"],
            ts=parse_utc(inner.get("UtcTime")) or time.time(),
            topic=topic, rule=source.get("Rule"), state=state, data={**source, **data}, initial=initial,
        )


class CameraIngest:
    """Owns the reader threads for one camera and bridges them onto asyncio queues."""

    def __init__(self, cam: dict, loop: asyncio.AbstractEventLoop,
                 frames: asyncio.Queue, events: asyncio.Queue):
        put_frame = lambda f: loop.call_soon_threadsafe(frames.put_nowait, f)
        put_event = lambda e: loop.call_soon_threadsafe(events.put_nowait, e)
        self.meta = MetadataReader(cam["id"], put_frame)
        self.events = EventPuller(cam, put_event)

    def start(self) -> None:
        self.meta.start()
        self.events.start()

    def stop(self) -> None:
        self.meta.stop_event.set()
        self.events.stop_event.set()

    def status(self) -> dict:
        return {"metadata": self.meta.connected, "metadata_last": self.meta.last_frame_at,
                "clock_offset": None if self.meta.clock_offset is None else round(self.meta.clock_offset, 2),
                "onvif_events": self.events.connected}
