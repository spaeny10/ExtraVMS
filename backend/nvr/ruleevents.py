"""Events from ONVIF rule events, for cameras that send no object metadata.

Most cameras here send ONVIF Profile M metadata (objects with boxes) and the tracker opens events from those tracks.
Some only announce detections as ONVIF events (Reolink RP-PCT8MD, firmware v3.2.0.5770: Analytics=false in its
metadata configuration, PullPoint topics RuleEngine/MyRuleDetector/PeopleDetect with State true/false). For those the
tracker opens an event from the event itself: label from the topic (or the event's object-type data), no box (the
path holds whole-frame points), track_id "onvif:<label>-<ms>". The verifier then looks for any YOLO object of that
label over the event's span and builds the path from YOLO's boxes, so zones, region cells, named places and journeys
work as for any other event.

Per camera `event_source`: "auto" (default) | "metadata" | "onvif_events". Auto uses the ONVIF events only while the
camera sends no object metadata: its metadata configuration says Analytics=false (streams.py probe), or no classified
object came from its connected metadata stream for AUTO_WINDOW_S while its ONVIF detection events kept arriving (a
metadata reader that can't connect is a health problem, never a reason to switch). A camera that sends objects keeps
today's behaviour (events from the metadata tracks only, never both: while an ONVIF-event event is open, the tracker
opens no metadata event on that camera). Motion opens nothing while a person / vehicle ONVIF event is open on the
camera, and an open motion event becomes the person / vehicle event when the camera names it (one visit, one event).

Motion topics (CellMotionDetector, VideoSource/MotionAlarm) open nothing unless the camera's `motion_events` is on;
then they open "motion" events that YOLO must confirm as a person or a vehicle.
"""
from __future__ import annotations

import re

SOURCES = ("auto", "metadata", "onvif_events")
SOURCE = "onvif_event"          # detections.source on events opened this way
TRACK_PREFIX = "onvif:"         # events.track_id prefix: how the verifier and merge know there were no camera boxes
WHOLE = (0.0, 0.0, 1.0, 1.0)    # the "box" of a path point when the camera gave none
AUTO_WINDOW_S = 600             # auto: no objects in the metadata for this long (while detection events arrive)
AUTO_SETTLE_S = 30              # auto: a detection event this old with no object since: the metadata has none for it
MIN_SPAN_S = 4.0                # the verifier samples at least this long a stretch (a pulse event has no duration)

# Topic (lowercase substring of the topic without its namespace prefix) -> label. None: the label comes from the
# event's object-type data (Hikvision / Dahua field and line detectors); without one it counts as motion.
OBJECT_TOPICS: list[tuple[str, str | None]] = [
    ("myruledetector/peopledetect", "person"),     # Reolink
    ("myruledetector/facedetect", "person"),
    ("myruledetector/visitor", "person"),          # Reolink doorbell press
    ("myruledetector/vehicledetect", "vehicle"),
    ("myruledetector/dogcatdetect", "animal"),     # opens only where the camera's synopsis_labels name it (see wanted)
    ("myruledetector/package", "package"),
    ("fielddetector/objectsinside", None),         # Hikvision / Dahua intrusion
    ("linedetector/crossed", None),                # line crossing (a pulse: no state)
    ("crosslinedetector", None),
    ("intrusiondetector", None),
]
MOTION_TOPICS = ("cellmotiondetector/motion", "videosource/motionalarm", "motionregiondetector/motion")
# event data keys that carry an object type, and the values we know
TYPE_KEYS = {"objecttype", "object_type", "objectclass", "class", "classtypes", "type", "object", "targettype"}
TYPE_MAP = {"human": "person", "person": "person", "people": "person", "pedestrian": "person", "face": "person",
            "vehicle": "vehicle", "car": "vehicle", "truck": "vehicle", "bus": "vehicle", "motorcycle": "vehicle",
            "motorvehicle": "vehicle", "nonmotor": "vehicle", "nonmotorvehicle": "vehicle", "bicycle": "vehicle",
            "animal": "animal", "dog": "animal", "cat": "animal"}
VERIFIABLE = ("person", "vehicle")   # what YOLO verifies (verifier.LABEL_CLASSES); "motion" is decided by YOLO


def object_type(data: dict | None) -> str | None:
    """person / vehicle / animal from an event's object-type data ("ObjectType": "Human"), or None."""
    for k, v in (data or {}).items():
        if str(k).lower() not in TYPE_KEYS or not isinstance(v, str):
            continue
        for word in re.split(r"[\s,;|/]+", v.lower()):
            if word in TYPE_MAP:
                return TYPE_MAP[word]
    return None


def classify(topic: str, data: dict | None) -> tuple[str, str] | None:
    """("object", label) for a detection topic, ("motion", "motion") for a motion topic, None for anything else."""
    t = (topic or "").lower()
    for pattern, label in OBJECT_TOPICS:
        if pattern in t:
            label = label or object_type(data)
            return ("object", label) if label else ("motion", "motion")
    typed = object_type(data)
    if typed and "ruleengine" in t:
        return "object", typed
    if any(p in t for p in MOTION_TOPICS):
        return "motion", "motion"
    return None


def wanted(label: str, cam: dict | None) -> bool:
    """Person, vehicle and motion always (YOLO verifies every label, like metadata events); anything else (animals,
    packages) only where the camera's synopsis_labels name it: YOLO here verifies people and vehicles only."""
    if label in VERIFIABLE or label == "motion":
        return True
    return label in ((cam or {}).get("synopsis_labels") or [])


def setting(cam: dict | None) -> str:
    s = (cam or {}).get("event_source") or "auto"
    return s if s in SOURCES else "auto"


def metadata_analytics(cam: dict | None) -> bool | None:
    """The camera's ONVIF metadata configuration Analytics flag from its last stream check (streams.py), or None."""
    st = (cam or {}).get("streams")
    if isinstance(st, dict):
        v = st.get("metadata_analytics")
        return v if isinstance(v, bool) else None
    return None


def is_rule_event(e: dict | None) -> bool:
    """An event opened from an ONVIF rule event: its path has no camera boxes."""
    return str((e or {}).get("track_id") or "").startswith(TRACK_PREFIX)


def track_id(label: str, ts: float) -> str:
    return f"{TRACK_PREFIX}{label}-{int(ts * 1000)}"


def point(ts: float) -> list:
    """A path point without a box: [ts, 0, 0, 1, 1, 0]."""
    return [round(ts, 3), *WHOLE, 0.0]


def whole_frame(box) -> bool:
    """A box that is the whole picture: the camera said "something", not where."""
    try:
        l, t, r, b = (float(v) for v in list(box)[:4])
    except (TypeError, ValueError):
        return False
    return l <= 0.001 and t <= 0.001 and r >= 0.999 and b >= 0.999


def sample_points(event: dict, n: int, pre_roll: float, post_roll: float) -> list:
    """n whole-frame path points evenly over the event's span (widened to MIN_SPAN_S inside the clip's pre/post-roll),
    for the verifier to look at when the camera gave no track."""
    start = float(event["start_ts"])
    end = float(event.get("end_ts") or start)
    t0, t1 = start, max(end, start)
    if t1 - t0 < MIN_SPAN_S:
        pad = (MIN_SPAN_S - (t1 - t0)) / 2
        t0, t1 = max(t0 - pad, start - pre_roll), min(t1 + pad, end + post_roll)
    n = max(1, n)
    if n == 1 or t1 <= t0:
        return [point((t0 + t1) / 2)]
    return [point(t0 + (t1 - t0) * i / (n - 1)) for i in range(n)]


class SourceWatch:
    """Per camera: does it send object metadata, or only ONVIF detection events? (auto mode)"""

    def __init__(self) -> None:
        self.first_heard: dict[str, float] = {}   # first frame or event from the camera since start
        self.objects_at: dict[str, float] = {}    # last metadata frame with a classified object
        self.detect_at: dict[str, float] = {}     # first ONVIF detection event since the last object

    def heard(self, camera_id: str, now: float) -> None:
        self.first_heard.setdefault(camera_id, now)

    def objects(self, camera_id: str, now: float) -> None:
        self.heard(camera_id, now)
        self.objects_at[camera_id] = now
        self.detect_at.pop(camera_id, None)

    def detection(self, camera_id: str, now: float) -> None:
        self.heard(camera_id, now)
        self.detect_at.setdefault(camera_id, now)

    def decide(self, cam: dict | None, camera_id: str, now: float, no_track: bool = False,
               reader: float | None = None) -> tuple[bool, str]:
        """(use ONVIF events for detections, why). no_track: the camera's RTSP stream has no metadata track at all
        (ingest.MetadataReader: "no application track in SDP"; the Reolink RP-PCT8MD). reader: when the metadata
        reader's session connected, 0 while it isn't connected, None when unknown (ingest.metadata_since). "No
        objects for AUTO_WINDOW_S" only counts while the reader is connected: a reader that can't connect (a 401 for
        hours) is a problem to show (health: "metadata reader not attached"), not a camera without objects."""
        src = setting(cam)
        if src == "metadata":
            return False, "set to the camera's object metadata"
        if src == "onvif_events":
            return True, "set to the camera's ONVIF events"
        seen = self.objects_at.get(camera_id)
        if seen is not None and now - seen < AUTO_WINDOW_S:
            return False, "the camera sends objects in its metadata"
        if no_track:
            return True, "the camera's stream has no metadata track"
        if metadata_analytics(cam) is False:
            return True, "the camera's metadata has no analytics (Analytics=false)"
        if reader is not None and not reader:
            return False, "the camera's metadata stream is not connected (its objects can't be seen until it is)"
        quiet_since = max(self.first_heard.get(camera_id, now), seen or 0.0, reader or 0.0)
        d = self.detect_at.get(camera_id)
        if now - quiet_since >= AUTO_WINDOW_S and d is not None and now - d >= AUTO_SETTLE_S:
            return True, f"no objects in the camera's metadata for {AUTO_WINDOW_S // 60} minutes while its ONVIF detection events arrive"
        if seen is not None:
            return False, "the camera sent objects in its metadata"
        return False, "watching which the camera sends (objects in its metadata, or only ONVIF events)"
