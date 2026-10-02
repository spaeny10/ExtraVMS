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
from dataclasses import dataclass, field
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
    t: float            # seconds from segment start (video track's tfdt / its timescale)
    duration: float
    keyframe: bool      # first video sample is a sync sample
    tfdt_pos: int       # byte offset of the video track's tfdt value (for rebasing)
    tfdt_v1: bool
    # every track's tfdt in this moof: (byte offset of the value, 64-bit?, track id). A camera with audio writes
    # one traf per track; each must be rebased in its own track's timescale.
    tfdts: list = field(default_factory=list)


@dataclass
class Segment:
    path: Path
    header_end: int     # end of moov
    timescale: int
    mtxi_pos: int | None
    mvhd: tuple[int, int, int] | None  # (duration field offset, version, movie timescale); MediaMTX reads length here
    fragments: list[Fragment]
    data: bytes
    timescales: dict = field(default_factory=dict)   # track id -> timescale (audio and video differ)

    @property
    def duration(self) -> float:
        return self.fragments[-1].t + self.fragments[-1].duration if self.fragments else 0.0


def parse(path: Path) -> Segment:
    data = Path(path).read_bytes()
    header_end, mtxi_pos, mvhd = 0, None, None
    timescales: dict[int, int] = {}       # track id -> timescale
    video_track: int | None = None
    trak_id: int | None = None
    frags: list[Fragment] = []
    cur: dict | None = None
    traf: dict | None = None
    for p, start, body, end in _boxes(data, 0, len(data)):
        top = p.count("/") == 1
        if p == "/moov":
            header_end = end
        elif p == "/moov/mvhd":
            v = data[body]
            ts = struct.unpack(">I", data[body + (12 if v == 0 else 20):body + (16 if v == 0 else 24)])[0]
            mvhd = (body + (16 if v == 0 else 24), v, ts)
        elif p == "/moov/trak/tkhd":
            v = data[body]
            trak_id = struct.unpack(">I", data[body + (12 if v == 0 else 20):body + (16 if v == 0 else 24)])[0]
        elif p.endswith("/mdhd"):
            v = data[body]
            ts = struct.unpack(">I", data[body + 12:body + 16] if v == 0 else data[body + 20:body + 24])[0]
            timescales[trak_id if trak_id is not None else len(timescales) + 1] = ts
        elif p.endswith("/hdlr") and data[body + 8:body + 12] == b"vide" and video_track is None:
            video_track = trak_id
        elif p.endswith("/mtxi"):
            mtxi_pos = body
        elif p == "/moof":
            cur = {"start": start, "trafs": []}
        elif p == "/moof/traf" and cur is not None:
            traf = {"track": None, "tfdt": 0, "tfdt_pos": 0, "v1": False, "dur": 0, "key": True}
            cur["trafs"].append(traf)
        elif p.endswith("/tfhd") and traf is not None:
            traf["track"] = struct.unpack(">I", data[body + 4:body + 8])[0]
        elif p.endswith("/tfdt") and traf is not None:
            v1 = data[body] == 1
            traf["v1"], traf["tfdt_pos"] = v1, body + 4
            traf["tfdt"] = struct.unpack(">Q" if v1 else ">I", data[body + 4:body + (12 if v1 else 8)])[0]
        elif p.endswith("/trun") and traf is not None:
            traf["dur"], traf["key"] = _trun_info(data, body)
        elif top and p == "/mdat" and cur is not None and cur["trafs"]:
            # the fragment's time comes from the video track (the one MediaMTX indexes on); first traf if none
            lead = next((t for t in cur["trafs"] if t["track"] == video_track), cur["trafs"][0])
            ts = timescales.get(lead["track"]) or 90000
            frags.append(Fragment(cur["start"], end, lead["tfdt"] / ts, lead["dur"] / ts, lead["key"],
                                  lead["tfdt_pos"], lead["v1"],
                                  [(t["tfdt_pos"], t["v1"], t["track"]) for t in cur["trafs"] if t["tfdt_pos"]]))
            cur, traf = None, None
    main_ts = timescales.get(video_track) if video_track is not None else None
    if main_ts is None:
        main_ts = next(iter(timescales.values()), 90000)
    return Segment(Path(path), header_end, main_ts, mtxi_pos, mvhd, frags, data, timescales)


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
            # rebase every track's decode time in its own timescale (audio and video differ); never below zero
            for pos, v1, track in (f.tfdts or [(f.tfdt_pos, f.tfdt_v1, None)]):
                rel = pos - f.start
                units = round(base_t * (seg.timescales.get(track) or seg.timescale)) if track is not None else base_units
                fmt, width = (">Q", 8) if v1 else (">I", 4)
                old = struct.unpack(fmt, chunk[rel:rel + width])[0]
                struct.pack_into(fmt, chunk, rel, max(0, old - units))
            body += chunk
        dest = Path(dest_dir) / segment_name(new_start)
        tmp = dest.with_suffix(".mp4.part")
        with open(tmp, "wb") as fh:
            fh.write(header)
            fh.write(body)
        os.replace(tmp, dest)
        out.append((dest, new_start, new_start + kept_dur))
    return out
