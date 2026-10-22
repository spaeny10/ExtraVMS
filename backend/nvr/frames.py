"""Frames straight from the recorded fMP4 segments: timeline scrubbing, and every event's video.

A seek + keyframe decode of a 5 MP H.265 segment takes ~40 ms, so the UI can show ~10 frames/s
while the playhead is dragged. `exact=True` decodes forward to the frame nearest the requested time.

Events keep no clip.mp4 of their own (2026-10-22: per-event clips filled an instance's 50 GB data dataset in a
week): an event's video is its stretch of the continuous recording, which retention keeps around events (keep.py).
Verification, PPE, the weapon check, plates and clip chat read their frames here (iter_recording / recording_frames).
"""
from __future__ import annotations

import gc
import logging
import threading
import time
from collections import OrderedDict
from pathlib import Path

import av
import cv2
import numpy as np

from .config import settings
from .fmp4 import segment_start

log = logging.getLogger("nvr.frames")
_listing: dict[str, tuple[float, list[tuple[float, Path]]]] = {}
_listing_ttl = 5.0

_containers: "OrderedDict[Path, tuple[av.container.InputContainer, threading.Lock]]" = OrderedDict()
_containers_lock = threading.Lock()
_MAX_CONTAINERS = 8

_jpegs: "OrderedDict[tuple, tuple[bytes, float]]" = OrderedDict()
_jpegs_lock = threading.Lock()
_MAX_JPEGS = 300


def _segments(camera_id: str, fresh: bool = False) -> list[tuple[float, Path]]:
    """The camera's segments (start epoch, path), oldest first. Cached for a few seconds for scrubbing; `fresh` lists
    the folder again (an event that just closed may lie in a segment MediaMTX opened a moment ago)."""
    cached = _listing.get(camera_id)
    if cached and not fresh and time.time() - cached[0] < _listing_ttl:
        return cached[1]
    segs = sorted((ts, f) for f in (settings.recordings_dir / camera_id).glob("*.mp4") if (ts := segment_start(f)))
    _listing[camera_id] = (time.time(), segs)
    return segs


def find_segment(camera_id: str, t: float, fresh: bool = False) -> tuple[Path, float, bool] | None:
    """(segment path, segment start epoch, is_live) for time t, or None if t is before all recordings."""
    segs = _segments(camera_id, fresh)
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


def _seek_frame(c, s, target: int, exact: bool):
    """The keyframe at or before pts `target`, or (exact) the decoded frame nearest it."""
    c.seek(max(s.start_time or 0, target), stream=s, backward=True, any_frame=False)
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
    return frame


def video_height(path: Path) -> int | None:
    """The recorded picture's height in pixels (from the stream header; nothing is decoded)."""
    with av.open(str(path)) as c:
        s = c.streams.video[0]
        return int(s.codec_context.height or 0) or None


def frames_at(path: Path, offsets: list[float]) -> dict[float, np.ndarray]:
    """Full-resolution BGR frames nearest each offset (seconds from the file's first frame), one seek each: for a few
    frames of a long clip much less work than decoding it from the start (verifier.grab_frames). Offsets the file
    doesn't reach are left out."""
    out: dict[float, np.ndarray] = {}
    try:
        with av.open(str(path)) as c:
            s = c.streams.video[0]
            tb = float(s.time_base)
            base = s.start_time or 0
            for off in sorted(set(offsets)):
                fr = _seek_frame(c, s, base + int(max(0.0, off) / tb), exact=True)
                if fr is not None and abs((fr.pts - base) * tb - off) <= 1.0:
                    out[off] = fr.to_ndarray(format="bgr24")
    finally:
        gc.collect()   # PyAV frames are freed late (see verifier.grab_frames)
    return out


class Recording:
    """A frame source: the camera's continuous recording (verifier.grab_frames takes this or a legacy clip path)."""
    __slots__ = ("camera_id",)

    def __init__(self, camera_id: str) -> None:
        self.camera_id = camera_id

    def __repr__(self) -> str:
        return f"Recording({self.camera_id!r})"


SEEK_GAP_S = 3.0   # a target further than this past the last decoded frame is reached by a seek, not by decoding on


def iter_recording(camera_id: str, targets: list[float], tol: float = 0.6, seek_gap: float = SEEK_GAP_S):
    """Yield (target, BGR frame) for the recorded frame nearest each target epoch (within `tol` s), in time order,
    full resolution. Close targets share one forward decode (like verifier.grab_frames over a clip); a far one is
    reached with a seek to its keyframe. Targets with nothing recorded near them are left out. Blocking."""
    wanted = sorted({float(t) for t in targets})
    if not wanted:
        return
    segs = _segments(camera_id, fresh=True)
    groups: list[tuple[Path, float, list[float]]] = []
    for w in wanted:
        best = None
        for start, f in segs:
            if start > w + tol:   # a target just before a segment's first frame may still be within tol of it
                break
            best = (f, start)
        if best is None:
            continue
        if groups and groups[-1][0] == best[0]:
            groups[-1][2].append(w)
        else:
            groups.append((best[0], best[1], [w]))
    for path, seg_start, ws in groups:
        try:
            yield from _decode_segment(path, seg_start, ws, tol, seek_gap)
        except (av.FFmpegError, OSError) as e:   # a damaged or vanished segment: the other segments still count
            log.warning("reading %s/%s failed: %s", camera_id, path.name, e)
        finally:
            gc.collect()   # PyAV frames are freed late (see verifier.grab_frames)


def _decode_segment(path: Path, seg_start: float, ws: list[float], tol: float, seek_gap: float):
    with av.open(str(path)) as c:
        s = c.streams.video[0]
        s.thread_type = "AUTO"
        tb = float(s.time_base)
        base = s.start_time or 0
        best: dict[float, tuple[float, object]] = {}   # target -> (distance, av frame): converted once, when yielded
        i, it, pos, sought = 0, None, None, -1
        while i < len(ws):
            if it is None or pos is None or (ws[i] - tol) - pos > seek_gap:
                c.seek(base + int(max(0.0, ws[i] - tol - seg_start) / tb), stream=s, backward=True, any_frame=False)
                it, sought = c.decode(s), i
            reseek = False
            for fr in it:
                if fr.pts is None:
                    continue
                ft = seg_start + (fr.pts - base) * tb
                pos = ft
                j = i
                while j < len(ws) and ws[j] - tol < ft:   # targets whose window this frame has reached
                    d = abs(ws[j] - ft)
                    if d < tol and (ws[j] not in best or d < best[ws[j]][0]):
                        best[ws[j]] = (d, fr)
                    j += 1
                while i < len(ws) and ft > ws[i] + tol:   # windows this frame has passed: done
                    if ws[i] in best:
                        yield ws[i], best.pop(ws[i])[1].to_ndarray(format="bgr24")
                    i += 1
                # far from the next target: seek, unless this decode began with a seek for it (a long GOP: the seek
                # lands on a keyframe further back than seek_gap, and seeking again would land there again)
                if i < len(ws) and i != sought and (ws[i] - tol) - ft > seek_gap:
                    reseek = True
                    break
                if i >= len(ws):
                    break
            if not reseek:   # the segment ended (or everything was found): what is left has what it has
                for w in ws[i:]:
                    if w in best:
                        yield w, best.pop(w)[1].to_ndarray(format="bgr24")
                return


def recording_frames(camera_id: str, targets: list[float], tol: float = 0.6) -> dict[float, np.ndarray]:
    """{target epoch: full-resolution BGR frame} from the recording, for the targets anything was recorded near."""
    return dict(iter_recording(camera_id, targets, tol))


def recorded_frame(camera_id: str, t: float) -> np.ndarray | None:
    """The recorded frame nearest epoch t at full resolution (BGR), or None where nothing is recorded."""
    seg = find_segment(camera_id, t)
    if not seg:
        return None
    path, seg_start, _live = seg
    return frames_at(path, [t - seg_start]).get(t - seg_start)


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
            frame = _seek_frame(c, s, target, exact)
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
