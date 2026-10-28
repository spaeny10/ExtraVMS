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


SEEK_GAP_S = 3.0   # a target further than this past the last packet read is reached by a seek, not by reading on

_hw_off_until = 0.0   # NVDEC failed on a segment the CPU then decoded: CPU only until then
HW_OFF_S = 600.0


def _hwaccel():
    """PyAV's HWAccel for settings.decode_hwaccel ("cuda": the GPU's NVDEC), or None to decode on the CPU. Frames stay
    on the card (is_hw_owned) and only the ones handed out are downloaded. FFmpeg makes its own CUDA context
    (primary_ctx 0): sharing the device's primary context fails ("Operation not supported") once torch has made it
    with other flags, as on a server running its own models."""
    if not settings.decode_hwaccel or time.time() < _hw_off_until:
        return None
    from av.codec.hwaccel import HWAccel
    kind, _, dev = settings.decode_hwaccel.partition(":")
    return HWAccel(device_type=kind, device=dev or "0", allow_software_fallback=False, is_hw_owned=True,
                   options={"primary_ctx": "0"} if kind == "cuda" else None)


def _bgr(fr) -> np.ndarray:
    """A decoded frame as full-resolution BGR. An NVDEC frame (cuda, or nv12 once downloaded) is first made planar,
    so swscale converts it exactly as the CPU decoder's yuv(j)420p frame: its nv12 input path rounds the chroma
    differently (mean 0.2, up to 44 levels apart), while the decoded planes themselves are identical."""
    if fr.format.name not in ("cuda", "nv12"):
        return fr.to_ndarray(format="bgr24")
    sw = fr.reformat(format="nv12") if fr.format.name == "cuda" else fr
    w, h = sw.width, sw.height
    y = np.frombuffer(sw.planes[0], np.uint8).reshape(-1, sw.planes[0].line_size)[:h, :w]
    uv = np.frombuffer(sw.planes[1], np.uint8).reshape(-1, sw.planes[1].line_size)[:h // 2, :w]
    yuv = np.concatenate([y.reshape(-1), uv[:, 0::2].reshape(-1), uv[:, 1::2].reshape(-1)]).reshape(h * 3 // 2, w)
    planar = av.VideoFrame.from_ndarray(yuv, format="yuvj420p" if int(fr.color_range) == 2 else "yuv420p")
    planar.color_range, planar.colorspace = fr.color_range, fr.colorspace
    return planar.to_ndarray(format="bgr24")


def iter_recording(camera_id: str, targets: list[float], tol: float = 0.6, seek_gap: float = SEEK_GAP_S):
    """Yield (target, BGR frame) for the recorded frame nearest each target epoch (within `tol` s), in time order,
    full resolution. Only the GOPs that hold a target are decoded (_decode_segment); a far target is reached with a
    seek to its keyframe. Targets with nothing recorded near them are left out. Blocking."""
    global _hw_off_until
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
        done: set[float] = set()
        try:
            hw = _hwaccel()
            if hw is not None:
                try:
                    for w, img in _decode_segment(path, seg_start, ws, tol, seek_gap, hw):
                        done.add(w)
                        yield w, img
                    continue
                except (av.FFmpegError, OSError, ValueError) as e:   # the CPU does the rest, and the next ones a while
                    _hw_off_until = time.time() + HW_OFF_S
                    log.warning("NVDEC failed on %s/%s (%s): decoding on the CPU for %.0f min", camera_id, path.name,
                                e, HW_OFF_S / 60)
                gc.collect()
                yield from _decode_segment(path, seg_start, [w for w in ws if w not in done], tol, seek_gap)
            else:
                yield from _decode_segment(path, seg_start, ws, tol, seek_gap)
        except (av.FFmpegError, OSError) as e:   # a damaged or vanished segment: the other segments still count
            log.warning("reading %s/%s failed: %s", camera_id, path.name, e)
        finally:
            gc.collect()   # PyAV frames are freed late (see verifier.grab_frames)


def _decode_segment(path: Path, seg_start: float, ws: list[float], tol: float, seek_gap: float, hw=None):
    """iter_recording's work in one segment. Packets are read (demuxed: cheap) from the first target's keyframe on,
    but decoded only in the GOPs that hold a target, from the keyframe to the first frame at or past the target: a
    4 MP HEVC frame costs ~20 ms to decode, so a 2 s GOP with no target in it is ~1.2 CPU-seconds saved. One decoder
    thread: frame threads (thread_type AUTO, ~16 on a big host) decoded the same frames for 2.5x the CPU-seconds
    and no gain in wall time (2026-10-28 on fred), since each seek or early stop throws their read-ahead away."""
    with av.open(str(path), **({"hwaccel": hw} if hw is not None else {})) as c:
        s = c.streams.video[0]
        cc = s.codec_context
        cc.thread_count = 1
        tb = float(s.time_base)
        base = s.start_time or 0
        best: dict[float, tuple[float, object]] = {}   # target -> (distance, av frame): converted once, when handed out
        i, pos, sought = 0, None, -1
        pending: list = []   # packets the decoder hasn't had: since the last one it had, or since the latest keyframe
        out: list = []       # (target, BGR) ready to yield

        def found(fr):
            """Fold a decoded frame in; hand out the targets it is at or past (no later frame is nearer)."""
            nonlocal i
            if fr.pts is None:
                return
            ft = seg_start + (fr.pts - base) * tb
            for w in ws[i:]:
                if w - tol >= ft:
                    break
                d = abs(w - ft)
                if d < tol and (w not in best or d < best[w][0]):
                    best[w] = (d, fr)
            while i < len(ws) and ft >= ws[i]:
                if ws[i] in best:
                    out.append((ws[i], _bgr(best.pop(ws[i])[1])))
                i += 1

        while i < len(ws):
            if pos is None or (i != sought and (ws[i] - tol) - pos > seek_gap):
                c.seek(base + int(max(0.0, ws[i] - tol - seg_start) / tb), stream=s, backward=True, any_frame=False)
                sought, pending = i, []
            reseek = False
            for pkt in c.demux(s):
                if pkt.pts is None:   # the demuxer's end-of-stream packet
                    continue
                pos = seg_start + (pkt.pts - base) * tb
                if pkt.is_keyframe:
                    pending = []
                pending.append(pkt)
                if pos >= ws[i] - tol:   # the next target's window is reached: decode up to here
                    for p in pending:
                        for fr in cc.decode(p):
                            found(fr)
                    pending = []
                    yield from out
                    out.clear()
                # far from the next target: seek, unless the read began with a seek for it (a long GOP: the seek
                # lands on a keyframe further back than seek_gap, and seeking again would land there again)
                if i < len(ws) and i != sought and (ws[i] - tol) - pos > seek_gap:
                    reseek = True
                    break
                if i >= len(ws):
                    break
            if not reseek:   # the segment ended (or everything was found): what is left has what it has
                if i < len(ws):
                    for fr in cc.decode(None):   # frames the decoder still holds (B-frames)
                        found(fr)
                    yield from out
                    out.clear()
                for w in ws[i:]:
                    if w in best:
                        yield w, _bgr(best.pop(w)[1])
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
