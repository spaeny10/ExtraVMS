"""Lossless trimming of MediaMTX fMP4 recording segments.

A MediaMTX segment is `ftyp` + `moov` (one video track; `moov/udta/mtxi` holds the stream id, segment
number, stream DTS and wall-clock start in ns) followed by ~1 s `moof`+`mdat` fragments whose `tfdt`
starts at 0. Keeping a time window means copying the header with `mtxi` shifted to the window start,
plus the fragments in the window with `tfdt` rebased to 0. No re-encoding; the result is still a
MediaMTX segment, so MediaMTX playback, the timeline and frame previews keep working.
"""
from __future__ import annotations

import datetime as dt
import os
import struct
from dataclasses import dataclass
from pathlib import Path

CONTAINERS = {"moov", "trak", "mdia", "minf", "stbl", "moof", "traf", "udta", "mvex", "edts"}


def _boxes(buf: bytes, off: int, end: int, path: str = ""):
    """Yield (path, start, body_start, end) for every box, descending into containers."""
    while off + 8 <= end:
        size, typ = struct.unpack(">I4s", buf[off:off + 8])
        hdr = 8
        if size == 1:
            size, hdr = struct.unpack(">Q", buf[off + 8:off + 16])[0], 16
        elif size == 0:
            size = end - off
        if size < hdr:
            raise ValueError(f"corrupt box at {off}")
        name = typ.decode("latin1")
        yield f"{path}/{name}", off, off + hdr, off + size
        if name in CONTAINERS:
            yield from _boxes(buf, off + hdr, off + size, f"{path}/{name}")
        off += size


@dataclass
class Fragment:
    start: int          # byte offset of moof
    end: int            # byte offset after the following mdat
    t: float            # seconds from segment start (tfdt / timescale)
    duration: float
    keyframe: bool      # first sample is a sync sample
    tfdt_pos: int       # byte offset of the tfdt value (for rebasing)
    tfdt_v1: bool


@dataclass
class Segment:
    path: Path
    header_end: int     # end of moov
    timescale: int
    mtxi_pos: int | None
    mvhd: tuple[int, int, int] | None  # (duration field offset, version, movie timescale); MediaMTX reads length here
    fragments: list[Fragment]
    data: bytes

    @property
    def duration(self) -> float:
        return self.fragments[-1].t + self.fragments[-1].duration if self.fragments else 0.0


def parse(path: Path) -> Segment:
    data = Path(path).read_bytes()
    timescale, header_end, mtxi_pos, mvhd = 90000, 0, None, None
    frags: list[Fragment] = []
    cur: dict | None = None
    for p, start, body, end in _boxes(data, 0, len(data)):
        top = p.count("/") == 1
        if p == "/moov":
            header_end = end
        elif p == "/moov/mvhd":
            v = data[body]
            ts = struct.unpack(">I", data[body + (12 if v == 0 else 20):body + (16 if v == 0 else 24)])[0]
            mvhd = (body + (16 if v == 0 else 24), v, ts)
        elif p.endswith("/mdhd"):
            v = data[body]
            timescale = struct.unpack(">I", data[body + 12:body + 16] if v == 0 else data[body + 20:body + 24])[0]
        elif p.endswith("/mtxi"):
            mtxi_pos = body
        elif p == "/moof":
            cur = {"start": start, "tfdt": 0, "tfdt_pos": 0, "v1": False, "dur": 0, "key": True}
        elif p.endswith("/tfdt") and cur is not None:
            v1 = data[body] == 1
            cur["v1"], cur["tfdt_pos"] = v1, body + 4
            cur["tfdt"] = struct.unpack(">Q" if v1 else ">I", data[body + 4:body + (12 if v1 else 8)])[0]
        elif p.endswith("/trun") and cur is not None:
            cur["dur"], cur["key"] = _trun_info(data, body)
        elif top and p == "/mdat" and cur is not None:
            frags.append(Fragment(cur["start"], end, cur["tfdt"] / timescale, cur["dur"] / timescale,
                                  cur["key"], cur["tfdt_pos"], cur["v1"]))
            cur = None
    return Segment(Path(path), header_end, timescale, mtxi_pos, mvhd, frags, data)


def _trun_info(data: bytes, body: int) -> tuple[int, bool]:
    """(total duration in timescale units, first sample is sync) from a trun box."""
    flags = int.from_bytes(data[body + 1:body + 4], "big")
    count = struct.unpack(">I", data[body + 4:body + 8])[0]
    pos = body + 8
    if flags & 0x1:
        pos += 4  # data offset
    first_flags = None
    if flags & 0x4:
        first_flags = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4
    total, sample_flags0 = 0, None
    for i in range(count):
        dur = 0
        if flags & 0x100:
            dur = struct.unpack(">I", data[pos:pos + 4])[0]
            pos += 4
        if flags & 0x200:
            pos += 4
        if flags & 0x400:
            if i == 0:
                sample_flags0 = struct.unpack(">I", data[pos:pos + 4])[0]
            pos += 4
        if flags & 0x800:
            pos += 4
        total += dur
    f0 = first_flags if first_flags is not None else sample_flags0
    keyframe = True if f0 is None else not (f0 & 0x10000)  # sample_is_non_sync_sample bit
    return total, keyframe


def segment_start(p: Path) -> float | None:
    """Start epoch from a MediaMTX segment filename (local time), or None if it isn't one."""
    try:
        return dt.datetime.strptime(Path(p).stem, "%Y-%m-%d_%H-%M-%S-%f").timestamp()
    except ValueError:
        return None


def segment_name(start_epoch: float) -> str:
    """MediaMTX recordPath naming: %Y-%m-%d_%H-%M-%S-%f (local time)."""
    return dt.datetime.fromtimestamp(start_epoch).strftime("%Y-%m-%d_%H-%M-%S-%f") + ".mp4"


def trim(seg: Segment, seg_start: float, windows: list[tuple[float, float]], dest_dir: Path) -> list[tuple[Path, float, float]]:
    """Write one MediaMTX-compatible file per window (epoch start, end). Returns [(path, start, end)].

    Each window is widened back to the nearest keyframe fragment and forward to fragment boundaries.
    """
    out = []
    frags = seg.fragments
    for w_start, w_end in windows:
        rel_s, rel_e = w_start - seg_start, w_end - seg_start
        idx = [i for i, f in enumerate(frags) if f.t + f.duration > rel_s and f.t < rel_e]
        if not idx:
            continue
        first = idx[0]
        while first > 0 and not frags[first].keyframe:
            first -= 1
        kept = frags[first:idx[-1] + 1]
        base_t = kept[0].t
        base_units = round(base_t * seg.timescale)
        new_start = seg_start + base_t

        header = bytearray(seg.data[:seg.header_end])
        if seg.mtxi_pos is not None:
            p = seg.mtxi_pos + 20  # version(4) + stream id(16)
            segno, dts, ntp = struct.unpack(">QqQ", header[p:p + 24])
            shift_ns = int(round(base_t * 1e9))
            struct.pack_into(">QqQ", header, p, segno, dts + shift_ns, ntp + shift_ns)
        kept_dur = kept[-1].t + kept[-1].duration - base_t
        if seg.mvhd:
            pos, v, movie_ts = seg.mvhd
            struct.pack_into(">I" if v == 0 else ">Q", header, pos, int(round(kept_dur * movie_ts)))
        body = bytearray()
        for f in kept:
            chunk = bytearray(seg.data[f.start:f.end])
            rel = f.tfdt_pos - f.start
            if f.tfdt_v1:
                old = struct.unpack(">Q", chunk[rel:rel + 8])[0]
                struct.pack_into(">Q", chunk, rel, old - base_units)
            else:
                old = struct.unpack(">I", chunk[rel:rel + 4])[0]
                struct.pack_into(">I", chunk, rel, old - base_units)
            body += chunk
        dest = Path(dest_dir) / segment_name(new_start)
        tmp = dest.with_suffix(".mp4.part")
        with open(tmp, "wb") as fh:
            fh.write(header)
            fh.write(body)
        os.replace(tmp, dest)
        out.append((dest, new_start, new_start + kept_dur))
    return out
