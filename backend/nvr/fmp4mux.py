"""Write recording segments the way MediaMTX writes them (recordFormat fmp4), from depacketized frames.

Used for footage restored from a camera's SD card (sdbackfill.py): a segment written here sits in the camera's
recording folder next to MediaMTX's own and is listed, served, scrubbed, exported and curated exactly like them.

Layout, copied from MediaMTX 1.21's own segments (see fmp4.py for the reader):
  ftyp  mp42 / mp41 mp42 isom hlsf
  moov  mvhd (timescale 1000, duration in ms: MediaMTX's playback reads a segment's length here)
        trak 1: video, 90 kHz, hvc1+hvcC or avc1+avcC (+btrt), empty sample tables
        trak 2: audio when the camera records it: LPCM 16-bit big-endian ('ipcm' + pcmC; MediaMTX stores G.711 so)
        mvex  trex per track
        udta/mtxi  stream id (16 bytes), segment number, stream DTS (ns), wall-clock start (ns since 1970)
  moof+mdat per ~1 s: mfhd (sequence from 0), one traf per track (audio first), tfhd default-base-is-moof,
        tfdt v1 (64-bit, relative to the segment start), trun v1 (data offset, durations, sizes; video with
        sample flags: 0 sync, 0x10000 non-sync)
Video samples are AVCC/HVCC (4-byte lengths) and a keyframe carries its parameter sets in band, as MediaMTX's do.
The file name is the start in local time, %Y-%m-%d_%H-%M-%S-%f.mp4 (fmp4.segment_name), the same instant as mtxi's
wall-clock start and the first video sample (tfdt 0).

No B-frames: decode order is presentation order (true of the cameras this targets; trun has no composition offsets).
"""
from __future__ import annotations

import os
import struct
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .fmp4 import segment_name

VIDEO_TS = 90000
VIDEO_TRACK, AUDIO_TRACK = 1, 2
FRAGMENT_S = 1.0         # MediaMTX's recordPartDuration
SYNC, NON_SYNC = 0x00000000, 0x00010000


# --------------------------------------------------------------------------- bitstream

def unescape(nal: bytes) -> bytes:
    """RBSP: the NAL payload without emulation-prevention bytes (00 00 03 -> 00 00)."""
    out, zeros = bytearray(), 0
    for b in nal:
        if zeros >= 2 and b == 3:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


class Bits:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def u(self, n: int) -> int:
        v = 0
        for _ in range(n):
            byte = self.data[self.pos >> 3]
            v = (v << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v

    def ue(self) -> int:
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError("bad exp-Golomb code")
        return (1 << zeros) - 1 + self.u(zeros)

    def se(self) -> int:
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


@dataclass
class VideoInfo:
    codec: str            # H264 | H265
    width: int
    height: int
    params: list[bytes]   # H.265: VPS, SPS, PPS; H.264: SPS, PPS
    config: bytes         # hvcC / avcC box body


def h265_sps(sps: bytes) -> dict:
    b = Bits(unescape(sps[2:]))
    b.u(4)
    max_sub = b.u(3)
    nesting = b.u(1)
    ptl_start = b.pos // 8
    b.u(8)            # profile space / tier / idc
    b.u(32)           # compatibility flags
    b.u(48)           # constraint flags
    b.u(8)            # level
    ptl = b.data[ptl_start:ptl_start + 12]
    present = [(b.u(1), b.u(1)) for _ in range(max_sub)]
    if max_sub > 0:
        for _ in range(max_sub, 8):
            b.u(2)
    for prof, lev in present:
        if prof:
            b.u(32), b.u(32), b.u(24)    # 88 bits
        if lev:
            b.u(8)
    b.ue()            # sps id
    chroma = b.ue()
    if chroma == 3:
        b.u(1)
    w, h = b.ue(), b.ue()
    if b.u(1):        # conformance window
        l, r, t, bt = b.ue(), b.ue(), b.ue(), b.ue()
        sw = 2 if chroma in (1, 2) else 1
        sh = 2 if chroma == 1 else 1
        w -= sw * (l + r)
        h -= sh * (t + bt)
    return {"width": w, "height": h, "chroma": chroma, "luma_bits": b.ue() + 8, "chroma_bits": b.ue() + 8,
            "ptl": ptl, "max_sub_layers": max_sub + 1, "nesting": nesting}


def hvcc(vps: bytes, sps: bytes, pps: bytes) -> tuple[bytes, dict]:
    s = h265_sps(sps)
    p = s["ptl"]
    body = bytes([1]) + p[:1] + p[1:5] + p[5:11] + p[11:12]
    body += struct.pack(">HBBBBHB", 0xF000, 0xFC, 0xFC | s["chroma"], 0xF8 | (s["luma_bits"] - 8),
                        0xF8 | (s["chroma_bits"] - 8), 0,
                        ((s["max_sub_layers"] & 7) << 3) | (s["nesting"] << 2) | 3)
    body += bytes([3])
    for typ, nal in ((32, vps), (33, sps), (34, pps)):
        body += bytes([0x80 | typ]) + struct.pack(">HH", 1, len(nal)) + nal
    return body, s


def h264_sps(sps: bytes) -> dict:
    b = Bits(unescape(sps[1:]))
    profile, compat, level = b.u(8), b.u(8), b.u(8)
    b.ue()
    chroma, luma_bits, chroma_bits = 1, 8, 8
    if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma = b.ue()
        if chroma == 3:
            b.u(1)
        luma_bits, chroma_bits = b.ue() + 8, b.ue() + 8
        b.u(1)
        if b.u(1):
            for i in range(8 if chroma != 3 else 12):
                if b.u(1):
                    last, nxt = 8, 8
                    for _ in range(16 if i < 6 else 64):
                        if nxt:
                            nxt = (last + b.se() + 256) % 256
                        last = nxt or last
    b.ue()                       # log2_max_frame_num
    poc = b.ue()
    if poc == 0:
        b.ue()
    elif poc == 1:
        b.u(1), b.se(), b.se()
        for _ in range(b.ue()):
            b.se()
    b.ue(), b.u(1)
    w_mbs, h_units = b.ue() + 1, b.ue() + 1
    frame_mbs_only = b.u(1)
    if not frame_mbs_only:
        b.u(1)
    b.u(1)
    w, h = w_mbs * 16, (2 - frame_mbs_only) * h_units * 16
    if b.u(1):
        l, r, t, bt = b.ue(), b.ue(), b.ue(), b.ue()
        cx = 1 if chroma in (0, 3) else 2
        cy = (1 if chroma in (0, 2, 3) else 2) * (2 - frame_mbs_only)
        w -= cx * (l + r)
        h -= cy * (t + bt)
    return {"width": w, "height": h, "profile": profile, "compat": compat, "level": level, "chroma": chroma,
            "luma_bits": luma_bits, "chroma_bits": chroma_bits}


def avcc(sps: bytes, pps: bytes) -> tuple[bytes, dict]:
    s = h264_sps(sps)
    body = bytes([1, s["profile"], s["compat"], s["level"], 0xFF, 0xE1]) + struct.pack(">H", len(sps)) + sps
    body += bytes([1]) + struct.pack(">H", len(pps)) + pps
    if s["profile"] in (100, 110, 122, 144):
        body += bytes([0xFC | s["chroma"], 0xF8 | (s["luma_bits"] - 8), 0xF8 | (s["chroma_bits"] - 8), 0])
    return body, s


def nal_type(codec: str, nal: bytes) -> int:
    return (nal[0] >> 1) & 0x3F if codec == "H265" else nal[0] & 0x1F


PARAM_TYPES = {"H265": (32, 33, 34), "H264": (7, 8)}
AUD_TYPE = {"H265": 35, "H264": 9}


def video_info(codec: str, params: list[bytes]) -> VideoInfo | None:
    """From the latest parameter sets of each kind; None until all are known."""
    codec = codec.upper()
    by = {nal_type(codec, p): p for p in params if p}
    kinds = PARAM_TYPES[codec]
    if not all(k in by for k in kinds):
        return None
    if codec == "H265":
        config, s = hvcc(by[32], by[33], by[34])
    else:
        config, s = avcc(by[7], by[8])
    return VideoInfo(codec, s["width"], s["height"], [by[k] for k in kinds], config)


# --------------------------------------------------------------------------- boxes

def box(typ: str, *parts: bytes) -> bytes:
    body = b"".join(parts)
    return struct.pack(">I4s", 8 + len(body), typ.encode()) + body


def full(typ: str, version: int, flags: int, *parts: bytes) -> bytes:
    return box(typ, struct.pack(">I", (version << 24) | flags), *parts)


MATRIX = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
EMPTY_TABLES = (full("stts", 0, 0, struct.pack(">I", 0)) + full("stsc", 0, 0, struct.pack(">I", 0))
                + full("stsz", 0, 0, struct.pack(">II", 0, 0)) + full("stco", 0, 0, struct.pack(">I", 0)))
DINF = box("dinf", full("dref", 0, 0, struct.pack(">I", 1), full("url ", 0, 1)))


def _tkhd(track: int, width: int, height: int, audio: bool) -> bytes:
    return full("tkhd", 0, 3, struct.pack(">IIIII", 0, 0, track, 0, 0), b"\0" * 8,
                struct.pack(">hhhH", 0, 1 if audio else 0, 0x0100 if audio else 0, 0), MATRIX,
                struct.pack(">II", width << 16, height << 16))


def _mdhd(timescale: int) -> bytes:
    return full("mdhd", 0, 0, struct.pack(">IIIIHH", 0, 0, timescale, 0, 0x55C4, 0))


def _hdlr(kind: str, name: str) -> bytes:
    return full("hdlr", 0, 0, struct.pack(">I4sIII", 0, kind.encode(), 0, 0, 0), name.encode() + b"\0")


def video_trak(v: VideoInfo) -> bytes:
    entry = (b"\0" * 6 + struct.pack(">H", 1) + b"\0" * 16 + struct.pack(">HHIIIH", v.width, v.height, 0x480000, 0x480000, 0, 1)
             + b"\0" * 32 + struct.pack(">Hh", 0x18, -1))
    cfg = box("hvcC" if v.codec == "H265" else "avcC", v.config)
    stsd = full("stsd", 0, 0, struct.pack(">I", 1),
                box("hvc1" if v.codec == "H265" else "avc1", entry, cfg, box("btrt", struct.pack(">III", 0, 1000000, 1000000))))
    stbl = box("stbl", stsd, EMPTY_TABLES)
    minf = box("minf", full("vmhd", 0, 1, b"\0" * 8), DINF, stbl)
    return box("trak", _tkhd(VIDEO_TRACK, v.width, v.height, False),
               box("mdia", _mdhd(VIDEO_TS), _hdlr("vide", "VideoHandler"), minf))


def audio_trak(rate: int, channels: int) -> bytes:
    entry = b"\0" * 6 + struct.pack(">H", 1) + b"\0" * 8 + struct.pack(">HHHHI", channels, 16, 0, 0, rate << 16)
    bitrate = rate * 16 * channels
    stsd = full("stsd", 0, 0, struct.pack(">I", 1),
                box("ipcm", entry, full("pcmC", 0, 0, bytes([0, 16])), box("btrt", struct.pack(">III", 0, bitrate, bitrate))))
    stbl = box("stbl", stsd, EMPTY_TABLES)
    minf = box("minf", full("smhd", 0, 0, b"\0" * 4), DINF, stbl)
    return box("trak", _tkhd(AUDIO_TRACK, 0, 0, True), box("mdia", _mdhd(rate), _hdlr("soun", "SoundHandler"), minf))


def mtxi(stream_id: bytes, segment_no: int, dts_ns: int, ntp_ns: int) -> bytes:
    return box("udta", full("mtxi", 0, 0, stream_id, struct.pack(">QqQ", segment_no, dts_ns, ntp_ns)))


def init_segment(v: VideoInfo, audio: tuple[int, int] | None, stream_id: bytes, segment_no: int, dts_ns: int,
                 ntp_ns: int) -> tuple[bytes, int]:
    """ftyp + moov, and the byte offset of mvhd's duration field (patched when the segment is closed)."""
    ftyp = box("ftyp", b"mp42", struct.pack(">I", 1), b"mp41mp42isomhlsf")
    mvhd = full("mvhd", 0, 0, struct.pack(">IIII", 0, 0, 1000, 0), struct.pack(">IH", 0x10000, 0x100), b"\0" * 10,
                MATRIX, b"\0" * 24, struct.pack(">I", 0xFFFFFFFF))
    traks = video_trak(v) + (audio_trak(*audio) if audio else b"")
    trex = b"".join(full("trex", 0, 0, struct.pack(">IIIII", t, 1, 0, 0, 0))
                    for t in ([VIDEO_TRACK, AUDIO_TRACK] if audio else [VIDEO_TRACK]))
    moov = box("moov", mvhd, traks, box("mvex", trex), mtxi(stream_id, segment_no, dts_ns, ntp_ns))
    # ftyp, moov header (8), mvhd header (8 + 4 version/flags) then creation, modification, timescale
    return ftyp + moov, len(ftyp) + 8 + 12 + 12


def fragment(seq: int, video: list[tuple[int, int, bytes, bool]], video_dts: int,
             audio: list[tuple[int, bytes]] | None, audio_dts: int) -> bytes:
    """moof + mdat. video: [(duration, size, data, sync)], audio: [(duration, data)]; DTS in each track's timescale."""
    def traf(track: int, dts: int, trun_body: bytes, flags: int) -> bytes:
        return box("traf", full("tfhd", 0, 0x020000, struct.pack(">I", track)),
                   full("tfdt", 1, 0, struct.pack(">Q", max(0, dts))), full("trun", 1, flags, trun_body))

    def build(off_audio: int, off_video: int) -> bytes:
        trafs = b""
        if audio:
            trafs += traf(AUDIO_TRACK, audio_dts, struct.pack(">Ii", len(audio), off_audio)
                          + b"".join(struct.pack(">II", d, len(x)) for d, x in audio), 0x000301)
        trafs += traf(VIDEO_TRACK, video_dts, struct.pack(">Ii", len(video), off_video)
                      + b"".join(struct.pack(">III", d, s, SYNC if k else NON_SYNC) for d, s, _, k in video), 0x000701)
        return box("moof", full("mfhd", 0, 0, struct.pack(">I", seq)), trafs)

    audio_bytes = b"".join(x for _, x in audio or [])
    moof_len = len(build(0, 0))
    moof = build(moof_len + 8, moof_len + 8 + len(audio_bytes))
    payload = audio_bytes + b"".join(d for _, _, d, _ in video)
    return moof + struct.pack(">I4s", 8 + len(payload), b"mdat") + payload


def avcc_sample(nals: list[bytes]) -> bytes:
    return b"".join(struct.pack(">I", len(n)) + n for n in nals)


# --------------------------------------------------------------------------- segment files

class SegmentExists(Exception):
    """A file with the segment's name is already in the folder: it is never overwritten."""


@dataclass
class Written:
    path: Path
    start: float
    end: float
    bytes: int


@dataclass
class _Open:
    tmp: Path
    fh: object
    start: float
    mvhd_pos: int
    seq: int = 0
    video_units: int = 0       # 90 kHz units written so far
    audio_units: int = 0
    end: float = 0.0
    info: VideoInfo | None = None


@dataclass
class SegmentWriter:
    """Accepts frames on this server's clock (epoch seconds) and writes MediaMTX segments into `folder`.

    Frames are held per GOP: a GOP is written when the next keyframe arrives (or at `close`), so `rollback()`
    after a broken replay session can drop the GOP in progress and return its keyframe time to resume from.
    A new segment starts every `segment_s` (at a keyframe), after a hole of more than `max_gap_s`, and when the
    parameter sets change. A finished segment is published under its MediaMTX name only if no file has that
    name (never overwritten), with its modification time set to its end like MediaMTX's own.
    `lo` / `hi`: nothing before / at or after these times is written (the gap being filled)."""
    folder: Path
    codec: str
    params: list[bytes] = field(default_factory=list)
    audio: tuple[int, int] | None = None      # (sample rate, channels) to write an LPCM track
    segment_s: float = 600.0
    max_gap_s: float = 2.0
    lo: float = float("-inf")
    hi: float = float("inf")
    on_segment: object = None                  # callable(Written) after each segment is published

    def __post_init__(self):
        self.folder = Path(self.folder)
        self.codec = self.codec.upper()
        self.stream_id = uuid.uuid4().bytes
        self.run_start: float | None = None
        self.segment_no = 0
        self.gop: list[tuple[float, list[bytes], bool]] = []   # (t, nals, keyframe)
        self.gop_audio: list[tuple[float, bytes, int]] = []
        self.last_t: float | None = None                       # last frame time handed to the file (written GOPs)
        self.last_dur = 0.1
        self.cur: _Open | None = None
        self.written: list[Written] = []
        self.need_key = True
        self.dropped_frames = 0

    # ---- input
    def add_video(self, t: float, nals: list[bytes], keyframe: bool) -> None:
        if t < self.lo or t >= self.hi:
            return
        if self.last_t is not None and t <= self.last_t:
            return                                             # never twice, never backwards
        prev = self.gop[-1][0] if self.gop else self.last_t
        if prev is not None and t - prev > self.max_gap_s and not keyframe:
            self.need_key = True                               # a hole inside a GOP: wait for the next keyframe
        if self.need_key and not keyframe:
            self.dropped_frames += 1
            return
        nals = [n for n in nals if n and nal_type(self.codec, n) != AUD_TYPE[self.codec]]
        if keyframe:
            for n in nals:
                if nal_type(self.codec, n) in PARAM_TYPES[self.codec]:
                    self.params = [p for p in self.params if nal_type(self.codec, p) != nal_type(self.codec, n)] + [n]
            if self.gop:
                self._flush_gop(next_t=t)
            self.need_key = False
        self.gop.append((t, nals, keyframe))

    def add_audio(self, t: float, pcm: bytes, samples: int) -> None:
        if self.audio and self.lo <= t < self.hi and (self.last_t is None or t > self.last_t):
            self.gop_audio.append((t, pcm, samples))

    def rollback(self) -> float | None:
        """Drop the GOP in progress; the time to resume from (its keyframe, or the end of what is written)."""
        t = self.gop[0][0] if self.gop else (self.cur.end if self.cur else None)
        self.gop, self.gop_audio = [], []
        self.need_key = True
        return t

    def close(self) -> list[Written]:
        """Write the last GOP (its last frame lasts as long as the one before, within `hi`) and publish."""
        if self.gop:
            self._flush_gop(next_t=None)
        self._finish()
        return self.written

    def abort(self) -> None:
        """Drop everything not yet published (the open segment's temp file included)."""
        self.gop, self.gop_audio = [], []
        if self.cur:
            try:
                self.cur.fh.close()
            finally:
                self.cur.tmp.unlink(missing_ok=True)
                self.cur = None

    # ---- output
    def _flush_gop(self, next_t: float | None) -> None:
        gop, audio = self.gop, self.gop_audio
        self.gop, self.gop_audio = [], []
        t0 = gop[0][0]
        info = video_info(self.codec, self.params)
        if info is None:
            self.dropped_frames += len(gop)
            return                                             # no parameter sets yet: can't describe the track
        cur = self.cur
        if cur is not None and (t0 - cur.start >= self.segment_s - 0.05 or t0 - cur.end > self.max_gap_s
                                or info.params != cur.info.params):
            self._finish()
            cur = None
        if cur is None:
            cur = self._open(t0, info)
        diffs = sorted(b[0] - a[0] for a, b in zip(gop, gop[1:]))
        if diffs:
            self.last_dur = max(0.001, min(1.0, diffs[len(diffs) // 2]))   # the usual frame interval
        last = gop[-1][0]
        if next_t is None or next_t - last > self.max_gap_s:
            next_t = min(last + self.last_dur, self.hi)          # the end of the range, or the next GOP is after a hole
        times = [g[0] for g in gop] + [next_t]
        # ~1 s fragments; durations from the wall-clock times, rounded on the cumulative clock so they never drift
        i = 0
        while i < len(gop):
            j = i
            while j + 1 < len(gop) and times[j + 1] - times[i] < FRAGMENT_S:
                j += 1
            samples = []
            for k in range(i, j + 1):
                t, nals, key = gop[k]
                if key:   # parameter sets in band on every keyframe, as MediaMTX writes them
                    have = {nal_type(self.codec, n) for n in nals}
                    nals = [p for p in info.params if nal_type(self.codec, p) not in have] + nals
                data = avcc_sample(nals)
                end_units = round((times[k + 1] - cur.start) * VIDEO_TS)
                dur = max(1, end_units - cur.video_units - sum(s[0] for s in samples))
                samples.append((dur, len(data), data, key))
            # audio by time: the GOP's first fragment also takes late arrivals from before it, its last one the rest
            a_lo = times[i] if i else float("-inf")
            a_hi = times[j + 1] if j + 1 < len(gop) else float("inf")
            frag_audio = [x for x in audio if a_lo <= x[0] < a_hi and x[0] >= cur.start] if self.audio else []
            self._write_fragment(cur, samples, frag_audio)
            i = j + 1
        self.last_t = last
        cur.end = next_t

    def _write_fragment(self, cur: _Open, samples, audio) -> None:
        a_samples = []
        a_dts = cur.audio_units
        if audio and self.audio:
            rate = self.audio[0]
            first = round((audio[0][0] - cur.start) * rate)
            a_dts = max(cur.audio_units, first)
            for _, pcm, n in audio:
                a_samples.append((n, pcm))
        frag = fragment(cur.seq, samples, cur.video_units, a_samples or None, a_dts)
        cur.fh.write(frag)
        cur.seq += 1
        cur.video_units += sum(s[0] for s in samples)
        if a_samples:
            cur.audio_units = a_dts + sum(n for n, _ in a_samples)

    def _open(self, start: float, info: VideoInfo) -> _Open:
        if self.run_start is None:
            self.run_start = start
        self.folder.mkdir(parents=True, exist_ok=True)
        name = segment_name(start)
        tmp = self.folder / f".{name}.{os.getpid()}.sdpart"     # not *.mp4: invisible to MediaMTX and retention
        header, mvhd_pos = init_segment(info, self.audio, self.stream_id, self.segment_no,
                                        int(round((start - self.run_start) * 1e9)), int(round(start * 1e9)))
        fh = open(tmp, "xb")
        fh.write(header)
        self.segment_no += 1
        self.cur = _Open(tmp, fh, start, mvhd_pos, end=start, info=info)
        return self.cur

    def _finish(self) -> None:
        cur, self.cur = self.cur, None
        if cur is None:
            return
        try:
            if cur.seq == 0:
                cur.fh.close()
                cur.tmp.unlink(missing_ok=True)
                return
            dur_ms = int(round((cur.end - cur.start) * 1000))
            cur.fh.seek(cur.mvhd_pos)
            cur.fh.write(struct.pack(">I", max(0, dur_ms)))
            cur.fh.flush()
            os.fsync(cur.fh.fileno())
            cur.fh.close()
            dest = self.folder / segment_name(cur.start)
            publish(cur.tmp, dest)
            os.utime(dest, (cur.end, cur.end))   # like MediaMTX's: last written at its end (retention reads mtime)
            w = Written(dest, cur.start, cur.end, dest.stat().st_size)
            self.written.append(w)
            if callable(self.on_segment):
                self.on_segment(w)
        except BaseException:
            try:
                cur.fh.close()
            except OSError:
                pass
            cur.tmp.unlink(missing_ok=True)
            raise


def publish(tmp: Path, dest: Path) -> None:
    """Give the finished temp file its final name, never replacing an existing file: a hard link fails when the
    name exists (on every OS); where links are unsupported, Windows' rename refuses an existing name too."""
    try:
        os.link(tmp, dest)
    except FileExistsError:
        tmp.unlink(missing_ok=True)
        raise SegmentExists(str(dest)) from None
    except OSError:
        if dest.exists():
            tmp.unlink(missing_ok=True)
            raise SegmentExists(str(dest)) from None
        if os.name != "nt":
            raise
        os.rename(tmp, dest)     # Windows: fails if dest exists
        return
    tmp.unlink()
