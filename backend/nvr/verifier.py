"""YOLO verification of camera (edge) detections.

For a closed event: fetch the clip from MediaMTX recordings, decode frames at sampled track
timestamps, run YOLO, and check that YOLO sees the same class where the camera said it was.

Events opened from a camera's ONVIF detection events (ruleevents.py) have no camera boxes: frames are sampled evenly
over the event, a YOLO object of the event's label (inside the zones) counts if it is confident and big enough
(boxless_min_conf, boxless_min_area / boxless_min_side_px) and, for vehicles, moved during the event or was not in the
pre-roll frame (boxless_matches), and the path is rebuilt from YOLO's boxes. A "motion" event takes the class YOLO
sees most (person or vehicle), or is rejected.
"""
from __future__ import annotations

import gc
import logging
import os
import time
from collections import deque
from pathlib import Path

os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")  # GPU indices match nvidia-smi / Ollama

import av
import cv2
import numpy as np

from . import parked, ruleevents, zones
from .config import ROOT, settings

log = logging.getLogger("nvr.verifier")

PERSON = {0}
VEHICLE = {1, 2, 3, 5, 7}  # bicycle, car, motorcycle, bus, truck
LABEL_CLASSES = {"person": PERSON, "vehicle": VEHICLE}


def event_dir(event_id: int) -> Path:
    d = settings.data_dir / "events" / str(event_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def center_inside(a, b) -> bool:
    cx, cy = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2
    return b[0] <= cx <= b[2] and b[1] <= cy <= b[3]


MAX_SHIFT_S = 5.0     # clock error the verifier tolerates between camera metadata and the recording
SHIFT_STEP_S = 0.25


def box_at(path: list, t: float, tol: float = 0.4):
    """Camera box at time t, from the nearest path point (None if the track has no point near t)."""
    best = min(path, key=lambda p: abs(p[0] - t), default=None)
    return tuple(best[1:5]) if best is not None and abs(best[0] - t) <= tol else None


def _matches(cam_box, boxes: list, allowed: set) -> tuple[dict | None, float]:
    match, match_iou = None, 0.0
    for b in boxes:
        if b["cls_id"] not in allowed:
            continue
        score = iou(cam_box, b["box"])
        if score >= settings.verify_iou or center_inside(cam_box, b["box"]) or center_inside(b["box"], cam_box):
            if match is None or b["conf"] > match["conf"]:
                match, match_iou = b, score
    return match, match_iou


def _center(b) -> tuple[float, float]:
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def _moves_together(pairs: list[tuple[tuple[float, float], tuple[float, float]]]) -> bool:
    """If the camera track moved between the first and last matched frames, the YOLO boxes must have moved
    the same way by at least half as far; a parked object can't follow a moving track."""
    (t0, y0), (t1, y1) = pairs[0], pairs[-1]
    tdx, tdy = t1[0] - t0[0], t1[1] - t0[1]
    track_move = (tdx * tdx + tdy * tdy) ** 0.5
    if track_move < 0.08:  # the track barely moved: nothing to check
        return True
    ydx, ydy = y1[0] - y0[0], y1[1] - y0[1]
    along = (ydx * tdx + ydy * tdy) / track_move  # YOLO movement in the track's direction
    return along >= 0.5 * track_move


def best_shift(path: list, frames_boxes: list[tuple[float, list]], allowed: set) -> tuple[float, int]:
    """The single time shift (s) that lines the camera track up with YOLO in the most frames.
    One shift for the whole event means the matches must follow the track's motion, so a parked car
    that happens to sit on the path can't produce matches in several frames."""
    best, best_key = (0.0, 0), (0, 0.0)
    steps = int(MAX_SHIFT_S / SHIFT_STEP_S)
    for k in sorted(range(-steps, steps + 1), key=abs):  # smallest shift wins exact ties
        shift = k * SHIFT_STEP_S
        hits, overlap, pairs = 0, 0.0, []
        for ts, boxes in frames_boxes:
            cb = box_at(path, ts + shift)
            m, m_iou = _matches(cb, boxes, allowed) if cb is not None else (None, 0.0)
            if m is not None:
                hits, overlap = hits + 1, overlap + m_iou
                pairs.append((_center(cb), _center(m["box"])))
        if hits >= 2 and not _moves_together(pairs):
            continue
        # most matching frames first; among those, the tightest overlap is the real clock offset
        if (hits, round(overlap, 3)) > best_key:
            best, best_key = (shift, hits), (hits, round(overlap, 3))
    return best


EXTEND_STEP_S = 0.5    # how often to look for the object before the camera first reported it


def chain_boxes(start_box, frames_boxes: list[tuple[float, list]], allowed: set) -> list:
    """Follow the object through frames_boxes (in the order given, i.e. backwards in time for the pre-roll):
    each step takes the allowed-class box that overlaps or sits nearest the previous one; stops at the first
    frame without a plausible continuation. Returns [[ts, l, t, r, b, conf], ...] in the order walked."""
    out, prev = [], tuple(start_box)
    for ts, boxes in frames_boxes:
        best, best_score = None, 0.0
        for b in boxes:
            if b["cls_id"] not in allowed:
                continue
            (px, py), (bx, by) = _center(prev), _center(b["box"])
            near = ((px - bx) ** 2 + (py - by) ** 2) ** 0.5 < 0.12 * (1 + abs(prev[3] - prev[1]))
            score = iou(prev, b["box"]) + (0.5 if near else 0.0)
            if (score >= 0.2 or near) and score > best_score:
                best, best_score = b, score
        if best is None:
            break
        out.append([round(ts, 3), *[round(v, 4) for v in best["box"]], best["conf"]])
        prev = tuple(best["box"])
    return out


def sample_path(path: list, n: int) -> list:
    """Pick n path entries spread over the track, preferring confident ones."""
    if len(path) <= n:
        return list(path)
    step = len(path) / n
    out = []
    for i in range(n):
        window = path[int(i * step):int((i + 1) * step)] or [path[int(i * step)]]
        out.append(max(window, key=lambda p: p[5]))
    return out


LINK_IOU = 0.3          # boxless: a box in a later frame is the same object as one this much overlapping before it
MOVE_DIAG = 0.02        # boxless: an object moved if its foot point travelled this share of the frame diagonal...
MOVE_WIDTHS = 0.5       # ...and at least this many of its own widths (between any two of its frames)
MOVE_SCALE = 1.25       # ...or its box grew / shrank by this factor in width AND height (driving toward / away from us)


def big_enough(b: dict, wh: tuple[int, int]) -> bool:
    """Boxless events: more than a blob. The box covers boxless_min_area of the frame, or boxless_min_side_px on its
    shorter side at the size YOLO analysed the frame (its long side scaled to yolo_imgsz)."""
    w, h = wh
    bw, bh = (b["box"][2] - b["box"][0]), (b["box"][3] - b["box"][1])
    side = min(bw * w, bh * h) * settings.yolo_imgsz / max(w, h, 1)
    return bw * bh >= settings.boxless_min_area or side >= settings.boxless_min_side_px


def link_objects(frame_boxes: list[tuple[float, list]]) -> list[list[tuple[int, dict]]]:
    """Group boxes into objects across the frames: each box (most confident first) joins the object whose latest box
    overlaps it most (IoU >= LINK_IOU), one box per object per frame. Vehicle classes are one family here: YOLO
    calls the same pickup a car in one frame and a truck in the next. Returns [[(frame index, box), ...], ...]."""
    objects: list[list[tuple[int, dict]]] = []
    for fi, (_, boxes) in enumerate(frame_boxes):
        taken: set[int] = set()
        for b in sorted(boxes, key=lambda b: -b["conf"]):
            best, best_iou = None, LINK_IOU
            for k, obj in enumerate(objects):
                if k not in taken and obj[-1][0] < fi and (s := iou(obj[-1][1]["box"], b["box"])) >= best_iou:
                    best, best_iou = k, s
            if best is None:
                objects.append([(fi, b)])
                best = len(objects) - 1
            else:
                objects[best].append((fi, b))
            taken.add(best)
    return objects


def object_moved(obj: list[tuple[int, dict]], wh: tuple[int, int]) -> bool:
    """Its foot point (bottom center) moved, between any two frames it was in, by more than max(MOVE_DIAG of the frame
    diagonal, MOVE_WIDTHS of its width) in pixels; or its box changed size by MOVE_SCALE in both directions (a vehicle
    driving straight at the camera barely moves its foot point: event 15064, a telehandler)."""
    if len(obj) < 2:
        return False
    w, h = wh
    feet = [zones.foot(b["box"]) for _, b in obj]
    dist = max((((x1 - x0) * w) ** 2 + ((y1 - y0) * h) ** 2) ** 0.5 for x0, y0 in feet for x1, y1 in feet)
    ws = [max(1e-6, b["box"][2] - b["box"][0]) for _, b in obj]
    hs = [max(1e-6, b["box"][3] - b["box"][1]) for _, b in obj]
    if max(ws) / min(ws) >= MOVE_SCALE and max(hs) / min(hs) >= MOVE_SCALE:
        return True
    return dist > max(MOVE_DIAG * (w * w + h * h) ** 0.5, MOVE_WIDTHS * sum(ws) / len(ws) * w)


def _in_pre_roll(obj: list[tuple[int, dict]], pre_boxes: list | None, allowed: set) -> bool | None:
    """Was the object already there in the pre-roll frame (before the camera fired)? None: no pre-roll frame."""
    if pre_boxes is None:
        return None
    first = obj[0][1]["box"]
    return any(p["cls_id"] in allowed and iou(p["box"], first) >= LINK_IOU for p in pre_boxes)


def _plural(classes) -> str:
    return " and ".join(f"{c}(s)" for c in sorted(set(classes)))


def boxless_matches(frame_boxes: list[tuple[float, list]], pre_boxes: list | None, wh: tuple[int, int], allowed: set,
                    require_motion: bool, check_size: bool = True
                    ) -> tuple[list[dict | None], str | None, dict[int, bool], list[list], bool]:
    """Events without camera boxes (ruleevents.py): which YOLO box in each frame is the event's object.

    YOLO's boxes of the label are grouped into objects across the frames (link_objects). An object counts if YOLO was
    at least boxless_min_conf sure of it in one frame (its weaker frames then count too) and, with check_size, one of
    its boxes is big enough to judge (big_enough). With require_motion (vehicles) it must also have moved during the
    event (object_moved) or not be in the pre-roll frame (it arrived): a parked one that was already there and stayed
    put is not what the camera saw (event 15205: a dark blob behind a tree "confirmed" a vehicle event, and Qwen then
    described the parked pickups as driving). Without a pre-roll frame an object that did not move never counts.
    Per frame the most confident box of a counting object.

    One very confident frame is enough (event 14031: a black SUV driving through, 76% in one frame, 37% in the next):
    an object YOLO saw in only one frame, at boxless_single_frame_conf or more, that was new (not in the pre-roll frame)
    or whose spot is empty in every other frame (it went by) counts, and alone meets the verifier's frames-needed.

    Returns (match per frame or None, reason when nothing counted, {frame index: the match moved}, the boxes per frame
    that count: the parked check may pick another of them, whether one confident single-frame object is enough)."""
    objects = link_objects([(ts, [b for b in boxes if b["cls_id"] in allowed]) for ts, boxes in frame_boxes])
    def gone_elsewhere(obj) -> bool:   # its spot is empty in every other frame: it was passing through
        fi0, b0 = obj[0]
        return not any(o["cls_id"] in allowed and iou(o["box"], b0["box"]) >= LINK_IOU
                       for fi, (_, boxes) in enumerate(frame_boxes) if fi != fi0 for o in boxes)

    live, still, weak, single = [], [], [], False
    for obj in objects:
        if max(b["conf"] for _, b in obj) < settings.boxless_min_conf or (check_size and not any(big_enough(b, wh) for _, b in obj)):
            weak.append(obj)
            continue
        went = require_motion and object_moved(obj, wh)
        pre = _in_pre_roll(obj, pre_boxes, allowed)
        alone = (len(obj) == 1 and obj[0][1]["conf"] >= settings.boxless_single_frame_conf
                 and (pre is False or gone_elsewhere(obj)))
        if require_motion and not went and pre is not False:
            if not alone:
                still.append(obj)
                continue
            went = True   # in one frame only, its spot empty in all the others: it moved through
        single = single or alone
        live.append((went, obj))
    matches: list[dict | None] = [None] * len(frame_boxes)
    moved: dict[int, bool] = {}
    pool: list[list] = [[] for _ in frame_boxes]
    for went, obj in live:
        for fi, b in obj:
            pool[fi].append(b)
            if matches[fi] is None or b["conf"] > matches[fi]["conf"]:   # the most confident counting box
                matches[fi], moved[fi] = b, went
    reason = None
    if not live and still:
        reason = f"nothing moving: YOLO found only stationary {_plural(b['cls'] for obj in still for _, b in obj)}"
    elif not live and weak:
        top = max((b for obj in weak for _, b in obj), key=lambda b: b["conf"])
        reason = (f"too small to judge: YOLO found only tiny {_plural(b['cls'] for obj in weak for _, b in obj)}"
                  if top["conf"] >= settings.boxless_min_conf else
                  f"too unsure: YOLO {top['cls']} {top['conf']:.0%} < {settings.boxless_min_conf:.0%}")
    return matches, reason, moved, pool, single


def grab_frames(clip: Path, clip_start: float, targets: list[float]) -> dict[float, np.ndarray]:
    """Decode the clip once and return the frame nearest each target epoch timestamp."""
    wanted = sorted(targets)
    best: dict[float, tuple[float, np.ndarray]] = {}
    with av.open(str(clip)) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        last_target = wanted[-1] - clip_start
        for frame in container.decode(stream):
            if frame.time is None:
                continue
            t = frame.time
            near = [w for w in wanted if abs((w - clip_start) - t) < 0.6]
            if near:
                img = None
                for w in near:
                    d = abs((w - clip_start) - t)
                    if w not in best or d < best[w][0]:
                        img = img if img is not None else frame.to_ndarray(format="bgr24")
                        best[w] = (d, img)
            if t > last_target + 0.6:
                break
    # Frame-threaded decoding leaves the decoder's frames in reference cycles that only the cyclic collector
    # frees. On a CPU-only site (Python 3.14, 4K HEVC) that leaked ~14 MB per decoded frame, ~600 MB per event,
    # and the kernel killed the service every five minutes. Collect now, while the frames are still small in number.
    gc.collect()
    return {w: v[1] for w, v in best.items()}


def load_yolo():
    """(model, device label) for settings.yolo_device: a Hailo-8 HEF (hailo.py), or ultralytics on CUDA / the CPU.
    Both answer .names and .predict(...) the same way. On the Hailo the label is None: the supervisor's .device says
    where it runs now (it falls back to the CPU when the Hailo is missing)."""
    weights = ROOT / "models" / settings.yolo_model
    if settings.yolo_device == "hailo":
        if weights.suffix != ".hef":
            raise ValueError(f"NVR_YOLO_DEVICE=hailo needs a Hailo .hef in NVR_YOLO_MODEL, not {settings.yolo_model}")
        return load_hailo(weights), None
    from ultralytics import YOLO  # heavy import; keep module import cheap

    weights.parent.mkdir(exist_ok=True)
    if not weights.exists():
        from ultralytics.utils.downloads import attempt_download_asset
        attempt_download_asset(str(weights))
    model = YOLO(str(weights))
    model.to(settings.yolo_device)
    return model, settings.yolo_device


def load_hailo(weights: Path):
    """The HEF on the Hailo, supervised: if the Hailo cannot be opened (or stops answering) YOLO runs on the CPU with
    the matching .pt and the Hailo is retried every NVR_HAILO_RETRY_S (detector.py)."""
    from . import detector

    def hailo_factory():
        from .hailo import HailoYOLO  # imports hailo_platform; an ImportError here is a fallback like any other
        return HailoYOLO(weights)

    def cpu_factory():
        pt = detector.cpu_weights_for(weights.name, weights.parent)
        if pt is None:
            return None
        log.warning("YOLO fallback: %s on the CPU", pt.name)
        return detector.load_cpu_model(pt)

    return detector.HailoSupervisor(hailo_factory, cpu_factory, retry_s=settings.hailo_retry_s)


class Verifier:
    def __init__(self) -> None:
        self.model, self._device = load_yolo()
        self.frame_ms: deque[float] = deque(maxlen=200)  # YOLO time per frame (ms), for Optimize my system
        self._reid = None  # person re-ID (loaded on first person event)
        log.info("YOLO %s loaded on %s", settings.yolo_model, self.device)

    @property
    def device(self) -> str:
        """Where YOLO runs now: on a Hailo site this follows the fallback (e.g. "cpu (hailo unavailable)")."""
        return getattr(self, "_device", None) or getattr(getattr(self, "model", None), "device", None) or settings.yolo_device

    def _predict(self, images: list) -> list:
        """YOLO on the verifier's frames (masked), timed per frame."""
        if not images:
            return []
        t0 = time.perf_counter()
        res = self.model.predict(images, imgsz=settings.yolo_imgsz, conf=settings.yolo_conf, device=settings.yolo_device,
                                 verbose=False, classes=sorted(PERSON | VEHICLE))
        ms = self.__dict__.setdefault("frame_ms", deque(maxlen=200))
        if self.__dict__.get("_frame_dev") != (dev := self.device):   # a Hailo/CPU switch: time the new device afresh
            ms.clear()
            self._frame_dev = dev
        ms.append(round((time.perf_counter() - t0) * 1000 / len(images), 1))
        return res

    def _reid_embedding(self, frames: dict, detections: list) -> list[float] | None:
        """Appearance fingerprint of the verified person (tight crops of YOLO's matched boxes)."""
        from .reid import ReID, person_crop
        try:
            if self._reid is None:
                self._reid = ReID()
            crops = [person_crop(frames[d["ts"]], d["match"]["box"]) for d in _largest_matches(detections) if d["ts"] in frames]
            vec = self._reid.embed(crops)
            return vec.tolist() if vec is not None else None
        except Exception:
            log.exception("re-ID embedding failed")
            return None

    def _parked(self, event: dict, detections: list, hits: int, best, need: int, allowed: set, pre_boxes: list | None = None,
                pool: list[list] | None = None, moved: dict[int, bool] | None = None, save: bool = True):
        """Parked-vehicle check and the camera's parked-spot memory (parked.py). Returns
        (parked_info or None, detections, hits, best); the matches change only when another vehicle passing the
        parked one turns out to be what the camera saw. Boxless events (boxless_matches) pass `pool`, the boxes per
        frame that may still match (those of objects that moved or arrived), and `moved`, which matches moved: the
        motion rule already judged them, so only the parking-spot memory can still drop one that never moved.
        save=False (the replay tool): the remembered spots are left as they are."""
        label, cam, now = event["camera_class"], event.get("camera_id"), event.get("start_ts") or 0.0
        try:
            entries = parked.load(cam) if cam else []
        except Exception:
            entries = []
        info = None
        pool = pool if pool is not None else [d["yolo"] for d in detections]
        if settings.parked_suppress and label in parked.VEHICLE_LABELS:
            # Per-frame: a match on a vehicle that sits unmoved through the whole clip is not a hit. Only the
            # matches on something that moved (or appeared) count.
            spots = [e["box"] for e in parked.active(entries, now)]
            idx = parked.static_matches(detections, pre_boxes, spots)
            if moved is not None:   # boxless: arrived (not in the pre-roll) counts, unless it sits on a known spot
                idx = [i for i in idx if not moved.get(i)
                       and any(parked.iou(detections[i]["match"]["box"], sp) >= parked.MEMORY_IOU for sp in spots)]
            if idx:
                dropped = [detections[i]["match"] for i in idx]
                for i in idx:
                    detections[i] = {**detections[i], "match": None, "iou": 0.0, "static": True}
                hits = sum(d["match"] is not None for d in detections)
                live = [d for d in detections if d["match"]]
                best = max(((d["ts"], d["match"], tuple(d["cam_box"])) for d in live), key=lambda t: t[1]["conf"], default=None)
                if hits < need:
                    top = max(dropped, key=lambda m: m["conf"])
                    info = {"box": top["box"], "cls": top["cls"], "via": "static"}
                    best = best or (detections[idx[0]]["ts"], top, tuple(detections[idx[0]]["cam_box"]))  # class still shown
        if info is None:
            info = parked.judge(label, detections, event.get("path") or [], entries, now)
        if info:
            alt = [_matches(tuple(d["cam_box"]), parked.without(p, info["box"]), allowed) for d, p in zip(detections, pool)]
            trial = [{**d, "match": m, "iou": round(s, 3), "static": False} for d, (m, s) in zip(detections, alt)]
            stills = [] if moved is not None else parked.static_matches(trial, pre_boxes, [e["box"] for e in parked.active(entries, now)])
            for i in stills:  # another parked vehicle is no better (boxless: the pool holds only moving / arriving ones)
                trial[i] = {**trial[i], "match": None, "iou": 0.0, "static": True}
            if sum(d["match"] is not None for d in trial) >= need:
                if parked.judge(label, trial, event.get("path") or [], entries, now) is None:
                    detections, info = trial, None
                    hits = sum(d["match"] is not None for d in trial)
                    top = max((d for d in trial if d["match"]), key=lambda d: d["match"]["conf"])
                    best = (top["ts"], top["match"], tuple(top["cam_box"]))
        if info:
            log.info("event %s: %s %s sat still (%s), camera motion elsewhere: rejected as parked",
                     event.get("id"), info["cls"], info["box"], info["via"])
        # Remember (or forget) parking spots: verified events (and the parked ones) only; others never matched anything.
        if save and cam and (entries or label in parked.VEHICLE_LABELS):
            ref = info["box"] if info else (parked.static_box(detections) if label in parked.VEHICLE_LABELS else None)
            cls = info["cls"] if info else (best[1]["cls"] if best and ref else None)
            new = parked.update([dict(e) for e in entries], detections, ref, cls, now)
            if new != entries:
                try:
                    parked.save(cam, new)
                except Exception:
                    log.exception("saving parked spots for %s failed", cam)
        return info, detections, hits, best

    def verify(self, event: dict, clip: Path, clip_start: float, zone_list: list[dict] | None = None,
               dry_run: bool = False) -> dict:
        """Verify a closed event from its clip. dry_run (tools/replay_verification.py): the decision only; nothing is
        written (no snapshot, keyframes, parked-spot memory or re-ID) and the path is not extended."""
        zone_list = zones.normalize(zone_list)
        label = event["camera_class"]
        no_box = ruleevents.is_rule_event(event)   # opened by the camera's ONVIF event: no camera boxes to match
        motion = no_box and label not in LABEL_CLASSES   # "motion": YOLO decides person or vehicle
        allowed = LABEL_CLASSES.get(label, set())
        if no_box:
            samples = ruleevents.sample_points(event, settings.verify_frames, settings.clip_pre_roll, settings.clip_post_roll)
        else:
            # a merged event (merge.py) spans several camera tracks, a group several people at once: a frame more per
            # extra track, up to twice the usual, so each of them is likely to be looked at (and cropped for Qwen)
            n = settings.verify_frames
            samples = sample_path(event["path"], min(2 * n, n + len(event.get("tracks") or [1]) - 1))
        # vehicles also get one pre-roll frame from before the camera saw motion: what was already parked there
        # (boxless ones always: an object that was not there before arrived, see boxless_matches)
        pre_t = clip_start + 0.5 if ((label in parked.VEHICLE_LABELS or motion)
                                     and (settings.parked_suppress or (no_box and settings.boxless_require_motion))) else None
        frames = grab_frames(clip, clip_start, [s[0] for s in samples] + ([pre_t] if pre_t else []))
        if not frames:
            return {"status": "error", "error": "no frames decoded from recording"}

        ts_list = [s[0] for s in samples if s[0] in frames]
        has_pre = pre_t is not None and pre_t in frames and pre_t not in ts_list
        # YOLO only sees allowed areas: masked regions are painted gray before inference.
        results = self._predict([zones.mask_frame(frames[t], zone_list) for t in ts_list + ([pre_t] if has_pre else [])])
        names = self.model.names
        pre_boxes = None
        if has_pre:
            pre_res = results[len(ts_list)]
            results = results[:len(ts_list)]
            pre_boxes = [{"cls": names[int(c)], "cls_id": int(c), "conf": round(float(p), 3), "box": [round(float(v), 4) for v in b]}
                         for b, c, p in zip(pre_res.boxes.xyxyn.tolist(), pre_res.boxes.cls.tolist(), pre_res.boxes.conf.tolist())]
            pre_boxes = [b for b in pre_boxes if zones.allowed(zones.foot(b["box"]), zone_list)]
        detections, hits, best = [], 0, None
        sample_by_ts = {s[0]: s for s in samples}
        frame_boxes = []
        for ts, res in zip(ts_list, results):
            boxes = [
                {"cls": names[int(c)], "cls_id": int(c), "conf": round(float(p), 3),
                 "box": [round(float(v), 4) for v in b]}
                for b, c, p in zip(res.boxes.xyxyn.tolist(), res.boxes.cls.tolist(), res.boxes.conf.tolist())
            ]
            # Drop anything standing in a masked area (e.g. a box that straddles the mask edge).
            frame_boxes.append((ts, [b for b in boxes if zones.allowed(zones.foot(b["box"]), zone_list)]))
        wh = frames[ts_list[0]].shape[1::-1] if ts_list else (1, 1)   # (width, height) of the decoded frames
        if motion:   # by what YOLO is sure of (boxless_matches counts nothing else)
            label = pick_label([(ts, [b for b in boxes if b["conf"] >= settings.boxless_min_conf]) for ts, boxes in frame_boxes])
            allowed = LABEL_CLASSES[label]
        boxless_reason, pool, moved, single = None, None, None, False
        if no_box:
            # no camera box (the whole frame): a confident (vehicles: big-enough) YOLO object of the label; vehicles
            # must also move or arrive during the event
            require_motion = settings.boxless_require_motion and label in parked.VEHICLE_LABELS
            box_matches, boxless_reason, moved, pool, single = boxless_matches(
                frame_boxes, pre_boxes, wh, allowed, require_motion, check_size=label in settings.boxless_size_labels)
            if not require_motion:   # people (or the rule off): the parked check works as before, on the strong boxes
                moved = None
        for i, (ts, boxes) in enumerate(frame_boxes):
            cam_box = tuple(sample_by_ts[ts][1:5])
            if no_box:
                match, match_iou = box_matches[i], 0.0
            else:
                match, match_iou = _matches(cam_box, boxes, allowed)
            if match:
                hits += 1
                if best is None or match["conf"] > best[1]["conf"]:
                    best = (ts, match, cam_box)
            detections.append({"ts": ts, "cam_box": list(cam_box), "yolo": boxes,
                               "match": match, "iou": round(match_iou, 3)})

        need = min(settings.verify_min_hits, max(1, len(ts_list) // 2))
        shift = 0.0
        if hits < need and not no_box:
            # The camera's metadata clock may be off by a few seconds (fast vehicles then never overlap):
            # try one consistent time shift of the camera track against the frames YOLO looked at.
            shift, shifted_hits = best_shift(event["path"], [(d["ts"], d["yolo"]) for d in detections], allowed)
            if shift and shifted_hits >= need:
                hits, best = 0, None
                for d in detections:
                    cb = box_at(event["path"], d["ts"] + shift)
                    m, m_iou = _matches(cb, d["yolo"], allowed) if cb else (None, 0.0)
                    if cb:
                        d["cam_box"] = list(cb)
                    d["match"], d["iou"] = m, round(m_iou, 3)
                    if m:
                        hits += 1
                        if best is None or m["conf"] > best[1]["conf"]:
                            best = (d["ts"], m, tuple(cb))
            else:
                shift = 0.0
        verified = hits >= need or (single and hits >= 1)   # boxless: one very confident frame of a passing object
        # A parked vehicle "confirming" motion next to it (shimmer, shadows): rejected, with the reason recorded.
        parked_info = None
        if verified:
            parked_info, detections, hits, best = self._parked({**event, "camera_class": label}, detections, hits, best,
                                                               need, allowed, pre_boxes, pool, moved, save=not dry_run)
            if parked_info:
                verified = False
        # boxless: why nothing counted (only parked / tiny / unsure objects), shown on the card like the parked reason
        rejected = parked.REASON if parked_info else (boxless_reason if not verified else None)
        # Where did it come from / go to? Cameras often report a person a step or two late, so the doorway
        # is on the pre-roll but not in the camera's path. Follow the verified object into the pre- and
        # post-roll with YOLO and extend the path (stored in camera-clock time, i.e. frame time + shift).
        path_ext = {"before": 0, "after": 0}
        path = list(event["path"])
        if no_box:   # the camera gave no positions: the object's path is where YOLO saw it
            path = [[d["ts"], *d["match"]["box"], d["match"]["conf"]] for d in detections if d["match"]] if verified else []
        if dry_run:
            return {"status": "verified" if verified else "rejected", "yolo_class": best[1]["cls"] if best else None,
                    "yolo_conf": best[1]["conf"] if best else None, "yolo_hits": hits, "needed": need, "rejected": rejected,
                    **({"camera_class": label} if motion and verified else {})}
        if verified and path:
            clip_end = clip_start + (event["end_ts"] or event["start_ts"]) + settings.clip_post_roll - event["start_ts"] + settings.clip_pre_roll
            first_ts, last_ts = path[0][0] - shift, path[-1][0] - shift
            before = [t for t in np.arange(first_ts - EXTEND_STEP_S, clip_start + 0.05, -EXTEND_STEP_S)]
            after = [t for t in np.arange(last_ts + EXTEND_STEP_S, clip_end - 0.05, EXTEND_STEP_S)]
            targets = [float(t) for t in before + after]
            extra = grab_frames(clip, clip_start, targets) if targets else {}
            if extra:
                ts_extra = [t for t in targets if t in extra]
                res_extra = self._predict([zones.mask_frame(extra[t], zone_list) for t in ts_extra])
                by_ts = {}
                for t, res in zip(ts_extra, res_extra):
                    bx = [{"cls_id": int(c), "conf": round(float(p), 3), "box": [round(float(v), 4) for v in b]}
                          for b, c, p in zip(res.boxes.xyxyn.tolist(), res.boxes.cls.tolist(), res.boxes.conf.tolist())]
                    by_ts[t] = [b for b in bx if zones.allowed(zones.foot(b["box"]), zone_list)]
                first_box = next((d["match"]["box"] for d in detections if d.get("match")), path[0][1:5])
                last_box = next((d["match"]["box"] for d in reversed(detections) if d.get("match")), path[-1][1:5])
                pre = chain_boxes(first_box, [(t, by_ts[t]) for t in before if t in by_ts], allowed)
                post = chain_boxes(last_box, [(t, by_ts[t]) for t in after if t in by_ts], allowed)
                for p in pre + post:
                    p[0] = round(p[0] + shift, 3)  # back to the camera's clock, like the rest of the path
                path = list(reversed(pre)) + path + post
                path_ext = {"before": len(pre), "after": len(post)}
        out_dir = event_dir(event["id"])
        # Snapshot: best matching frame, or the middle sample if nothing matched.
        snap_ts, snap_match, snap_cam = best if best else (ts_list[len(ts_list) // 2], None,
                                                           tuple(sample_by_ts[ts_list[len(ts_list) // 2]][1:5]))
        snapshot = out_dir / "snapshot.jpg"
        snap_img = zones.draw_outlines(annotate(frames[snap_ts], snap_cam, snap_match), zone_list)
        cv2.imwrite(str(snapshot), snap_img, [cv2.IMWRITE_JPEG_QUALITY, 88])
        keyframes = save_keyframes(out_dir, frames, detections)
        reid_vec = self._reid_embedding(frames, detections) if verified and label == "person" else None
        return {
            "reid": reid_vec,
            "status": "verified" if verified else "rejected",
            "yolo_class": best[1]["cls"] if best else None,
            "yolo_conf": best[1]["conf"] if best else None,
            "yolo_hits": hits,
            "detections": {"samples": detections, "keyframes": keyframes, "needed": need, "time_shift_s": shift,
                           "path_extended": path_ext, **({"source": ruleevents.SOURCE} if no_box else {}),
                           **({"rejected": rejected} if rejected else {}), **({"parked": parked_info} if parked_info else {})},
            "snapshot": str(snapshot.relative_to(settings.data_dir)),
            "clip_start": clip_start,
            **({"path": path} if path_ext["before"] or path_ext["after"] or (no_box and path) else {}),
            **({"camera_class": label} if motion and verified else {}),
        }


def pick_label(frame_boxes: list[tuple[float, list]]) -> str:
    """A "motion" event's label: the class (person / vehicle) YOLO saw in more of the frames; people win ties."""
    def frames_with(classes: set) -> int:
        return sum(any(b["cls_id"] in classes for b in boxes) for _, boxes in frame_boxes)
    return "vehicle" if frames_with(VEHICLE) > frames_with(PERSON) else "person"


def _largest_matches(detections: list, n: int = 4) -> list:
    matched = [d for d in detections if d["match"]]
    area = lambda b: (b[2] - b[0]) * (b[3] - b[1])
    return sorted(matched, key=lambda d: area(d["match"]["box"]), reverse=True)[:n]


def annotate(img: np.ndarray, cam_box, match: dict | None) -> np.ndarray:
    out = img.copy()
    h, w = out.shape[:2]
    px = lambda b: (int(b[0] * w), int(b[1] * h), int(b[2] * w), int(b[3] * h))
    if cam_box is not None and not ruleevents.whole_frame(cam_box):   # no camera box (ONVIF event): nothing to draw
        x1, y1, x2, y2 = px(cam_box)
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 200, 255), 2)  # camera: amber
    if match:
        x1, y1, x2, y2 = px(match["box"])
        cv2.rectangle(out, (x1, y1), (x2, y2), (80, 220, 80), 3)  # YOLO: green
        cv2.putText(out, f"{match['cls']} {match['conf']:.2f}", (x1, max(20, y1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (80, 220, 80), 2)
    return out


def save_keyframes(out_dir: Path, frames: dict, detections: list) -> list[dict]:
    """Images for the VLM: one annotated wide shot plus object crops across the track."""
    # Prefer frames where YOLO confirmed the object; otherwise fall back to the camera's boxes
    # so rejected / unverified events can still be reviewed or discussed with the VLM.
    matched = [d for d in detections if d["match"]] or detections
    if not matched:
        return []
    picks = [matched[int(i * (len(matched) - 1) / max(1, settings.synopsis_images - 2))]
             for i in range(min(len(matched), settings.synopsis_images - 1))]
    keyframes = []
    mid = matched[len(matched) // 2]
    wide = annotate(frames[mid["ts"]], mid["cam_box"], mid["match"])
    scale = 1280 / wide.shape[1]
    wide = cv2.resize(wide, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(out_dir / "wide.jpg"), wide, [cv2.IMWRITE_JPEG_QUALITY, 85])
    keyframes.append({"file": "wide.jpg", "ts": mid["ts"], "kind": "wide"})
    seen = set()
    for i, d in enumerate(picks):
        if d["ts"] in seen:
            continue
        seen.add(d["ts"])
        img = frames[d["ts"]]
        h, w = img.shape[:2]
        l, t, r, b = d["match"]["box"] if d["match"] else d["cam_box"]
        # pad the crop so the VLM sees context around the object
        pw, ph = (r - l) * 0.6 + 0.03, (b - t) * 0.4 + 0.03
        x1, y1 = int(max(0, l - pw) * w), int(max(0, t - ph) * h)
        x2, y2 = int(min(1, r + pw) * w), int(min(1, b + ph) * h)
        crop = img[y1:y2, x1:x2]
        if crop.size == 0:
            continue
        s = 512 / max(crop.shape[:2])
        if s < 1:
            crop = cv2.resize(crop, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        name = f"crop_{i}.jpg"
        cv2.imwrite(str(out_dir / name), crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
        # the object's box inside this padded crop (0-1), so re-ID can cut the tight crop later
        cw, ch = max(1, x2 - x1), max(1, y2 - y1)
        box = [round(max(0.0, (l * w - x1) / cw), 4), round(max(0.0, (t * h - y1) / ch), 4),
               round(min(1.0, (r * w - x1) / cw), 4), round(min(1.0, (b * h - y1) / ch), 4)]
        keyframes.append({"file": name, "ts": d["ts"], "kind": "crop", "box": box})
    return keyframes
