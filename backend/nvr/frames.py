"""Single preview frames straight from the recorded fMP4 segments (timeline scrubbing).

A seek + keyframe decode of a 5 MP H.265 segment takes ~40 ms, so the UI can show ~10 frames/s
while the playhead is dragged. `exact=True` decodes forward to the frame nearest the requested time.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from pathlib import Path

import av
import cv2

from .config import settings
from .fmp4 import segment_start

_listing: dict[str, tuple[float, list[tuple[float, Path]]]] = {}
_listing_ttl = 5.0

_containers: "OrderedDict[Path, tuple[av.container.InputContainer, threading.Lock]]" = OrderedDict()
_containers_lock = threading.Lock()
_MAX_CONTAINERS = 8

_jpegs: "OrderedDict[tuple, tuple[bytes, float]]" = OrderedDict()
_jpegs_lock = threading.Lock()
_MAX_JPEGS = 300


def _segments(camera_id: str) -> list[tuple[float, Path]]:
    cached = _listing.get(camera_id)
    if cached and time.time() - cached[0] < _listing_ttl:
        return cached[1]
    segs = sorted((ts, f) for f in (settings.recordings_dir / camera_id).glob("*.mp4") if (ts := segment_start(f)))
    _listing[camera_id] = (time.time(), segs)
    return segs


def find_segment(camera_id: str, t: float) -> tuple[Path, float, bool] | None:
    """(segment path, segment start epoch, is_live) for time t, or None if t is before all recordings."""
    segs = _segments(camera_id)
    best = None
    for i, (start, f) in enumerate(segs):
        if start > t:
            break
        best = (f, start, i == len(segs) - 1)
    return best


def _container(path: Path, live: bool):
    """Cached open container for finished segments; the live (growing) segment is always reopened."""
    if live:
        return av.open(str(path)), threading.Lock(), True
    with _containers_lock:
        if path in _containers:
            _containers.move_to_end(path)
            c, lock = _containers[path]
            return c, lock, False
        c = av.open(str(path))
        _containers[path] = (c, threading.Lock())
        while len(_containers) > _MAX_CONTAINERS:
            _, (old, _) = _containers.popitem(last=False)
            old.close()
        return c, _containers[path][1], False


def release(path: Path) -> None:
    """Close a cached container (Windows can't delete a file that is open) and forget listings."""
    with _containers_lock:
        entry = _containers.pop(Path(path), None)
    if entry:
        c, lock = entry
        with lock:
            c.close()
    _listing.clear()


def preview_jpeg(camera_id: str, t: float, width: int = 960, exact: bool = False) -> tuple[bytes, float, bool] | None:
    """JPEG of the frame at (or the keyframe before) epoch t. Returns (jpeg, frame_epoch, is_live) or None."""
    seg = find_segment(camera_id, t)
    if not seg:
        return None
    path, seg_start, live = seg
    req_key = ("req", camera_id, path.name, round(t, 1), width, exact)
    with _jpegs_lock:
        hit = _jpegs.get(req_key)
        if hit:
            _jpegs.move_to_end(req_key)
            return hit[0], hit[1], live
    c, lock, owned = _container(path, live)
    try:
        with lock:
            s = c.streams.video[0]
            tb = float(s.time_base)
            base = s.start_time or 0
            if not live and c.duration and t - seg_start > c.duration / 1e6 + 1:
                return None  # in a gap after this segment
            target = base + int((t - seg_start) / tb)
            c.seek(max(base, target), stream=s, backward=True, any_frame=False)
            frame = None
            for fr in c.decode(s):
                if fr.pts is None:
                    continue
                if frame is None or not exact:
                    frame = fr
                if not exact:
                    break
                if fr.pts >= target:  # first frame at/after t; keep the closer of it and the previous one
                    if abs(fr.pts - target) < abs(frame.pts - target):
                        frame = fr
                    break
                frame = fr
            if frame is None:
                return None
            frame_epoch = seg_start + (frame.pts - base) * tb
            key = (camera_id, path.name, frame.pts, width)
            with _jpegs_lock:
                hit = _jpegs.get(key)
                if hit:
                    _jpegs.move_to_end(key)
                    _jpegs[req_key] = hit
                    return hit[0], hit[1], live
            img = frame.to_ndarray(format="bgr24")
    finally:
        if owned:
            c.close()
    scale = width / img.shape[1]
    if scale < 1:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
    data = buf.tobytes()
    with _jpegs_lock:
        _jpegs[key] = _jpegs[req_key] = (data, frame_epoch)
        while len(_jpegs) > _MAX_JPEGS:
            _jpegs.popitem(last=False)
    return data, frame_epoch, live
