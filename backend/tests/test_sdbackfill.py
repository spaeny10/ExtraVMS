"""SD card backfill (sdreplay.py, fmp4mux.py, sdbackfill.py): RTP depacketizing (H.264 / H.265 single, aggregated,
fragmented), ONVIF replay timestamps, the gap finder, segment writing (MediaMTX layout, only inside the camera's
folder, never over an existing file), reconnect + resume against a fake camera (no duplicates, no holes), the job
worker end to end, and the API's permissions.

Everything runs in temp folders: the data, recordings and runtime dirs below are set before nvr is imported, and the
MediaMTX URLs point at a closed port, so nothing here can reach the live server, its MediaMTX or a camera.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_sdbackfill.py   (from backend/)
"""
import asyncio
import base64
import datetime as dt
import hashlib
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="nvr-sdbackfill-test-"))
os.environ["NVR_DATA_DIR"] = str(TMP / "data")                # never the real DB
os.environ["NVR_RECORDINGS_DIR"] = str(TMP / "recordings")    # never the real recordings
os.environ["NVR_RUNTIME_DIR"] = str(TMP / "runtime")          # never the running server's MediaMTX config
os.environ["NVR_FOOTAGE_INDEX_DIR"] = str(TMP / "index")
os.environ["NVR_BACKUP_DIR"] = str(TMP / "backups")
os.environ["NVR_MEDIAMTX_API"] = "http://127.0.0.1:9"         # a closed port: never the live MediaMTX
os.environ["NVR_MEDIAMTX_PLAYBACK"] = "http://127.0.0.1:9"
os.environ.setdefault("NVR_ALLOWED_HOSTS", "site")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from nvr import fmp4, fmp4mux, hub_agent, sdbackfill, sdreplay  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402

for _p in (settings.data_dir, settings.recordings_dir, settings.runtime_dir):
    _s = str(_p).replace("\\", "/").lower()
    assert str(TMP).replace("\\", "/").lower() in _s and "e:/nvr" not in _s and "d:/nvr" not in _s, _p

# a 320x240 baseline H.264 stream's parameter sets (libx264)
SPS = bytes.fromhex("6742c00bd90141fb011000000300100000030140f142a480")
PPS = bytes.fromhex("68cb83cb20")
# and an H.265 Main one (libx265)
VPS5 = bytes.fromhex("40010c01ffff01600000030090000003000003003c959809")
SPS5 = bytes.fromhex("42010101600000030090000003000003003ca00a080f165959a4932bc05a020000030002000003001410")
PPS5 = bytes.fromhex("4401c172b46240")
CAM = {"id": "cam1", "name": "Front", "host": "127.0.0.1", "onvif_port": 80, "rtsp_port": 554, "username": "viewer",
       "password": "pw-" + "z" * 6, "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": [],
       "retention_days": None, "scene_notes": "", "retention_policy": None, "synopsis_labels": None, "policies": []}


# --------------------------------------------------------------------------- RTP helpers

def rtp(seq, ts, payload, marker=False, ntp=None, clean=False, cseq=0, pt=96):
    ext = b""
    if ntp is not None:
        sec = int(ntp) + sdreplay.NTP_EPOCH
        frac = int(round((ntp - int(ntp)) * 2 ** 32)) & 0xFFFFFFFF
        ext = struct.pack(">HHIIBBH", 0xABAC, 3, sec, frac, 0x80 if clean else 0, cseq & 0xFF, 0)
    b0 = 0x80 | (0x10 if ext else 0)
    return struct.pack(">BBHII", b0, (0x80 if marker else 0) | pt, seq & 0xFFFF, ts & 0xFFFFFFFF, 0x1234) + ext + payload


def packetize_h264(nals, mtu=1000):
    """Like a camera: parameter sets aggregated (STAP-A), big NAL units fragmented (FU-A), the rest single."""
    out = []
    small = [n for n in nals if n[0] & 0x1F in (7, 8)]
    if small:
        out.append(bytes([0x18]) + b"".join(struct.pack(">H", len(n)) + n for n in small))
    for n in (n for n in nals if n[0] & 0x1F not in (7, 8)):
        if len(n) <= mtu:
            out.append(n)
            continue
        hdr, body = n[0], n[1:]
        chunks = [body[i:i + mtu] for i in range(0, len(body), mtu)]
        for i, c in enumerate(chunks):
            fu_h = (hdr & 0x1F) | (0x80 if i == 0 else 0) | (0x40 if i == len(chunks) - 1 else 0)
            out.append(bytes([(hdr & 0xE0) | 28, fu_h]) + c)
    return out


def packetize_h265(nals, mtu=1000):
    out = []
    small = [n for n in nals if (n[0] >> 1) & 0x3F in (32, 33, 34)]
    if small:
        out.append(bytes([48 << 1, 1]) + b"".join(struct.pack(">H", len(n)) + n for n in small))
    for n in (n for n in nals if (n[0] >> 1) & 0x3F not in (32, 33, 34)):
        if len(n) <= mtu:
            out.append(n)
            continue
        t = (n[0] >> 1) & 0x3F
        body = n[2:]
        chunks = [body[i:i + mtu] for i in range(0, len(body), mtu)]
        for i, c in enumerate(chunks):
            fu_h = t | (0x80 if i == 0 else 0) | (0x40 if i == len(chunks) - 1 else 0)
            out.append(bytes([(n[0] & 0x81) | (49 << 1), n[1], fu_h]) + c)
    return out


def frame_nals(i, key, codec="H264"):
    """A frame whose payload names its index (so the output can be checked frame by frame)."""
    tag = struct.pack(">I", i) * (800 if key else 40)
    if codec == "H264":
        return ([SPS, PPS, b"\x65" + tag] if key else [b"\x41" + tag])
    return ([VPS5, SPS5, PPS5, b"\x26\x01" + tag] if key else [b"\x02\x01" + tag])


def frame_index(sample_nals, codec="H264"):
    last = sample_nals[-1]
    return struct.unpack(">I", last[(1 if codec == "H264" else 2):][:4])[0]


def read_samples(path):
    """(time, [nal], sync) of every video sample of a written segment (track 1), from its moof/trun boxes."""
    data = Path(path).read_bytes()
    start = fmp4.segment_start(Path(path))
    out = []
    off = 0
    while off < len(data):
        size, typ = struct.unpack(">I4s", data[off:off + 8])
        if typ == b"moof":
            moof, p = off, off + 8
            while p < off + size:
                s2, t2 = struct.unpack(">I4s", data[p:p + 8])
                if t2 == b"traf":
                    q, track, tfdt = p + 8, None, 0
                    while q < p + s2:
                        s3, t3 = struct.unpack(">I4s", data[q:q + 8])
                        if t3 == b"tfhd":
                            track = struct.unpack(">I", data[q + 12:q + 16])[0]
                        elif t3 == b"tfdt":
                            tfdt = struct.unpack(">Q", data[q + 12:q + 20])[0]
                        elif t3 == b"trun" and track == 1:
                            n, doff = struct.unpack(">Ii", data[q + 12:q + 20])
                            pos, r, units = moof + doff, q + 20, tfdt
                            for _ in range(n):
                                d, sz, fl = struct.unpack(">III", data[r:r + 12])
                                r += 12
                                nals, k = [], pos
                                while k < pos + sz:
                                    ln = struct.unpack(">I", data[k:k + 4])[0]
                                    nals.append(data[k + 4:k + 4 + ln])
                                    k += 4 + ln
                                out.append((start + units / 90000, nals, fl == 0))
                                pos += sz
                                units += d
                        q += s3
                p += s2
        off += size
    return out


# --------------------------------------------------------------------------- depacketizing and timestamps

def test_rtp_and_onvif_timestamp():
    t = 1791475144.909863
    p = sdreplay.parse_rtp(rtp(7, 90000, b"\x65abc", marker=True, ntp=t, clean=True, cseq=4))
    assert p.seq == 7 and p.marker and p.payload == b"\x65abc"
    assert abs(p.onvif.ntp - t) < 1e-6 and p.onvif.clean and p.onvif.cseq == 4
    assert sdreplay.parse_rtp(b"\x00" * 5) is None
    # padding is stripped
    pkt = bytearray(rtp(1, 0, b"\x41xy" + b"\0\0\x03"))
    pkt[0] |= 0x20
    assert sdreplay.parse_rtp(bytes(pkt)).payload == b"\x41xy"
    assert sdreplay.epoch_to_clock(dt.datetime(2026, 10, 8, 15, 59, 3, tzinfo=dt.timezone.utc).timestamp()) == "20261008T155903Z"
    assert sdreplay.epoch_to_clock(1791475143.25).endswith(".250Z")
    for s in ("20261008T155903Z", "20261008T155903.250Z"):
        assert sdreplay.epoch_to_clock(sdreplay.parse_clock(s)) == s
    assert sdreplay.ntp_to_epoch(sdreplay.NTP_EPOCH + 10, 2 ** 31) == 10.5


def _feed(dep, frames, codec, start_seq=0, drop=None, t0=1000.0, ext_every=1):
    out, seq = [], start_seq
    for i, (key) in enumerate(frames):
        pk = (packetize_h264 if codec == "H264" else packetize_h265)(frame_nals(i, key, codec))
        for j, pl in enumerate(pk):
            seq += 1
            if drop and (i, j) in drop:
                continue
            ntp = t0 + i * 0.1 if j == 0 and i % ext_every == 0 else None
            out += dep.push(sdreplay.parse_rtp(rtp(seq, int(i * 9000), pl, marker=j == len(pk) - 1, ntp=ntp)))
    return out + dep.flush()


def test_depacketize_h264_and_h265():
    for codec in ("H264", "H265"):
        keys = [i % 10 == 0 for i in range(25)]
        aus = _feed(sdreplay.VideoDepacketizer(codec), keys, codec)
        assert [frame_index(a.nals, codec) for a in aus] == list(range(25)), codec
        assert [a.keyframe for a in aus] == keys
        assert abs(aus[13].ntp - 1001.3) < 1e-6
        k = aus[0].nals
        assert (k[:2] == [SPS, PPS]) if codec == "H264" else (k[:3] == [VPS5, SPS5, PPS5]), "aggregated parameter sets come apart"
        assert len(k[-1]) > 3000, "a fragmented keyframe is put back together"


def test_time_between_extensions_follows_the_rtp_clock():
    aus = _feed(sdreplay.VideoDepacketizer("H264"), [i % 10 == 0 for i in range(12)], "H264", ext_every=5)
    assert [round(a.ntp - 1000, 3) for a in aus] == [round(i * 0.1, 3) for i in range(12)]


def test_lost_packet_drops_until_next_keyframe():
    keys = [i % 10 == 0 for i in range(25)]
    # frame 3 loses a packet: frames 3..9 can't be decoded, 10 (keyframe) is fine again
    aus = _feed(sdreplay.VideoDepacketizer("H264"), keys, "H264", drop={(3, 0)})
    assert [frame_index(a.nals) for a in aus] == [0, 1, 2] + list(range(10, 25))
    # a keyframe fragment lost: wait for the next keyframe
    aus = _feed(sdreplay.VideoDepacketizer("H265"), keys, "H265", drop={(10, 3)})
    assert [frame_index(a.nals, "H265") for a in aus] == list(range(10)) + list(range(20, 25))


def test_g711():
    dep = sdreplay.AudioDepacketizer("PCMU", 8000)
    ch = dep.push(sdreplay.parse_rtp(rtp(1, 800, bytes([0xFF, 0x00, 0x7F]), ntp=50.0, pt=0)))
    assert len(ch) == 1 and ch[0].samples == 3 and ch[0].ntp == 50.0
    assert struct.unpack(">3h", ch[0].pcm) == (0, -32124, 0)
    ch = dep.push(sdreplay.parse_rtp(rtp(2, 1600, b"\xff" * 160, pt=0)))
    assert abs(ch[0].ntp - 50.1) < 1e-9


def test_sdp_and_replay_url():
    sdp = ("v=0\r\nm=video 0 RTP/AVP 96\r\nb=AS:12000\r\na=rtpmap:96 H265/90000\r\n"
           "a=fmtp:96 profile-id=1;sprop-vps=" + base64.b64encode(VPS5).decode() + ";sprop-sps=" + base64.b64encode(SPS5).decode()
           + ";sprop-pps=" + base64.b64encode(PPS5).decode() + "\r\na=control:track1\r\n"
           "m=audio 0 RTP/AVP 0\r\na=control:track2\r\nm=application 0 RTP/AVP 107\r\na=rtpmap:107 vnd.onvif.metadata/90000\r\na=control:track3\r\n")
    t = sdreplay.parse_sdp(sdp)
    assert [(x.kind, x.codec, x.clock_rate, x.control) for x in t] == [
        ("video", "H265", 90000, "track1"), ("audio", "PCMU", 8000, "track2"), ("application", "vnd.onvif.metadata", 90000, "track3")]
    assert sdreplay.parameter_sets(t[0]) == [VPS5, SPS5, PPS5]
    cam = {"host": "192.168.105.43"}
    assert sdreplay.replay_url(cam) == "rtsp://192.168.105.43:555/onvifreplay"
    assert sdreplay.replay_url(cam, "rtsp://admin:pw@192.168.105.43:555/onvifreplay") == "rtsp://192.168.105.43:555/onvifreplay"
    fwd = {"host": "192.168.105.43", "public_host": "203.0.113.50", "public_replay_port": 5551}
    assert sdreplay.replay_url(fwd) == "rtsp://203.0.113.50:5551/onvifreplay"
    assert sdreplay.replay_url({**fwd, "public_replay_port": None}) == "rtsp://203.0.113.50:555/onvifreplay"


def test_sps_parsers():
    assert fmp4mux.video_info("H264", [SPS, PPS]).width == 320 and fmp4mux.video_info("H264", [SPS, PPS]).height == 240
    v = fmp4mux.video_info("H265", [VPS5, SPS5, PPS5])
    assert (v.width, v.height) == (320, 240) and v.config[0] == 1 and v.config[1] == 0x01   # Main profile
    assert fmp4mux.video_info("H265", [SPS5]) is None


# --------------------------------------------------------------------------- gaps

def test_gap_finder():
    iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()
    listing = [{"start": iso(1000), "duration": 600}, {"start": iso(1600.2), "duration": 300},   # joined (0.2 s apart)
               {"start": iso(1910), "duration": 90},                                            # 10 s hole: ignored
               {"start": iso(2240), "duration": 760}]
    spans = sdbackfill.spans_from_listing(listing)
    assert spans == [(1000, 1900.2), (1910, 2000), (2240, 3000)]
    assert sdbackfill.find_gaps(spans, 0, 3010) == [(2000, 2240)]
    assert sdbackfill.find_gaps(spans, 0, 3100) == [(2000, 2240), (3000, 3100)]   # still missing up to now
    assert sdbackfill.find_gaps(spans, 2100, 3010) == [(2100, 2240)]              # clipped to the window
    assert sdbackfill.find_gaps([], 0, 3000) == []
    assert sdbackfill.clip([(2000, 2240)], 2100, None) == [(2100, 2240)]
    assert sdbackfill.subtract([(0, 100)], [(10, 20), (50, 120)]) == [(0, 10), (20, 50)]


def test_card_status():
    now = time.time()
    # what a Milesight camera without a card reports: an "empty" recording from 2038 back to 1970
    assert not sdbackfill._plausible(2147483647.0, 0.0) and not sdbackfill._plausible(None, now)
    assert sdbackfill._plausible(now - 86400, now)
    st = {"has_recording": True, "earliest": now - 86400, "latest": now - 3000, "recording_now": True}
    assert sdbackfill.card_range(st, now) == (now - 86400, now)          # still recording: holds up to now
    assert sdbackfill.card_range({**st, "recording_now": False}, now) == (now - 86400, now - 3000)
    assert sdbackfill.card_range({"has_recording": False}) is None
    assert sdbackfill.status_text({"supported": True, "has_recording": False}) == "SD card: no recording on the camera"
    assert sdbackfill.status_text({"supported": True, "has_recording": True, "earliest": dt.datetime(2026, 9, 14, 19).timestamp(),
                                   "latest": now, "recording_now": True}) == "SD card: recording, holds Sep 14 → now"


def test_camera_gaps_uses_card_and_jobs():
    db.upsert_camera(CAM)
    now = time.time()
    iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()

    async def lister(cid, start, end):
        return [{"start": iso(now - 7200), "duration": 3000}, {"start": iso(now - 3600), "duration": 1000},
                {"start": iso(now - 2000), "duration": 1990}]
    sdbackfill.save_status({"camera_id": "cam1", "checked_at": now, "supported": True, "has_recording": True,
                            "earliest": now - 86400, "latest": now, "recording_now": True})
    g = asyncio.run(sdbackfill.camera_gaps(CAM, 24, now=now, list_spans=lister))
    assert [(round(x["from"] - now), round(x["to"] - now), x["on_card"]) for x in g["gaps"]] == [(-2600, -2000, "yes"), (-4200, -3600, "yes")]
    jid = db.execute_insert("INSERT INTO restored_spans (camera_id, from_ts, to_ts, state, created_at) VALUES (?,?,?,?,?)",
                            ["cam1", now - 2600, now - 2000, "recovering", now])
    g = asyncio.run(sdbackfill.camera_gaps(CAM, 24, now=now, list_spans=lister))
    assert len(g["gaps"]) == 1 and g["restored"][0]["id"] == jid
    db.execute("DELETE FROM restored_spans")


# --------------------------------------------------------------------------- writing segments

def _write(folder, codec="H264", n=45, t0=None, audio=False, **kw):
    t0 = t0 or time.time() - 3600
    w = fmp4mux.SegmentWriter(folder, codec, audio=(8000, 1) if audio else None, **kw)
    for i in range(n):
        t = t0 + i * 0.1
        if audio and i % 2 == 0:
            w.add_audio(t, b"\x01\x00" * 1600, 1600)
        w.add_video(t, frame_nals(i, i % 10 == 0, codec), i % 10 == 0)
    return w, w.close(), t0


def test_writer_layout_and_timing():
    for codec in ("H264", "H265"):
        folder = TMP / "w" / codec.lower()
        w, out, t0 = _write(folder, codec, lo=0)
        assert len(out) == 1 and out[0].path.parent == folder
        p = out[0].path
        assert p.name == fmp4.segment_name(t0) and abs(fmp4.segment_start(p) - t0) < 1e-5
        seg = fmp4.parse(p)
        assert seg.timescale == 90000 and abs(seg.duration - 4.5) < 0.01, seg.duration
        assert seg.mvhd and struct.unpack(">I", seg.data[seg.mvhd[0]:seg.mvhd[0] + 4])[0] == 4500   # MediaMTX reads the length here
        segno, dts, ntp = struct.unpack(">QqQ", seg.data[seg.mtxi_pos + 20:seg.mtxi_pos + 44])
        assert segno == 0 and dts == 0 and abs(ntp / 1e9 - t0) < 1e-6
        assert abs(p.stat().st_mtime - out[0].end) < 1, "mtime = the segment's end, like MediaMTX's (retention reads it)"
        samples = read_samples(p)
        assert [frame_index(s[1], codec) for s in samples] == list(range(45))
        assert all(abs(s[0] - (t0 + i * 0.1)) < 1e-4 for i, s in enumerate(samples))
        assert [s[2] for s in samples] == [i % 10 == 0 for i in range(45)]
        assert samples[10][1][0] in (SPS, VPS5), "every keyframe carries its parameter sets, as MediaMTX writes them"
        assert not list(folder.glob(".*")), "no temp files left"
        # retention's trimmer understands it
        (TMP / "w" / f"trim-{codec}").mkdir()
        trimmed = fmp4.trim(seg, fmp4.segment_start(p), [(t0 + 2.05, t0 + 3.0)], TMP / "w" / f"trim-{codec}")
        assert trimmed and abs(trimmed[0][1] - (t0 + 2.0)) < 1e-3


def test_writer_lo_hi_gaps_and_segments():
    folder = TMP / "w" / "lohi"
    t0 = time.time() - 7200
    _, out, _ = _write(folder, n=200, t0=t0, lo=t0 + 0.55, hi=t0 + 19.0, segment_s=6.0)
    # starts at the first keyframe at or after lo (1.0), ends at hi, new segment every 6 s at a keyframe
    assert [round(o.start - t0, 3) for o in out] == [1.0, 7.0, 13.0]
    assert abs(out[-1].end - (t0 + 19.0)) < 1e-6
    allf = [frame_index(s[1]) for o in out for s in read_samples(o.path)]
    assert allf == list(range(10, 190))
    # a hole of more than 2 s starts a new segment
    folder = TMP / "w" / "hole"
    w = fmp4mux.SegmentWriter(folder, "H264")
    for i in list(range(0, 30)) + list(range(60, 90)):
        w.add_video(t0 + i * 0.1, frame_nals(i, i % 10 == 0), i % 10 == 0)
    out = w.close()
    assert [round(o.start - t0, 2) for o in out] == [0.0, 6.0] and abs(out[0].end - (t0 + 3.0)) < 1e-6


def test_writer_audio_track():
    folder = TMP / "w" / "audio"
    _, out, t0 = _write(folder, audio=True, lo=0)
    data = out[0].path.read_bytes()
    assert b"ipcm" in data and b"pcmC" in data and data.count(b"traf") == 2 * data.count(b"moof")
    if shutil.which("ffprobe"):
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,sample_rate", "-of", "csv=p=0",
                            str(out[0].path)], capture_output=True, text=True)
        assert "audio" in r.stdout and "8000" in r.stdout, r.stdout + r.stderr


def test_writer_never_overwrites():
    folder = TMP / "w" / "exists"
    folder.mkdir(parents=True)
    t0 = time.time() - 600
    existing = folder / fmp4.segment_name(t0)
    existing.write_bytes(b"MediaMTX's own segment")
    try:
        _write(folder, t0=t0, lo=0)
        raise AssertionError("expected SegmentExists")
    except fmp4mux.SegmentExists:
        pass
    assert existing.read_bytes() == b"MediaMTX's own segment"
    assert [p.name for p in folder.iterdir()] == [existing.name], "the restored copy and its temp file are gone"


def test_real_stream_decodes():
    """When ffmpeg is installed: a real H.264 and H.265 stream through the writer is a file ffmpeg plays in full."""
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        print("  (ffmpeg not installed: skipped)")
        return
    for codec, enc, fmt in (("H264", "libx264", "h264"), ("H265", "libx265", "hevc")):
        raw = TMP / f"src.{fmt}"
        args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10", "-t", "6", "-pix_fmt", "yuv420p",
                "-c:v", enc, "-g", "20", "-bf", "0"] + (["-x265-params", "log-level=error"] if enc == "libx265" else []) + ["-f", fmt, str(raw)]
        if subprocess.run(args, capture_output=True).returncode:
            print(f"  ({enc} not available: skipped)")
            continue
        nals = [n for n in re.split(rb"\x00\x00\x00\x01|\x00\x00\x01", raw.read_bytes()) if n]
        aus, cur = [], []
        vcl = (lambda n: n[0] & 0x1F in (1, 5)) if codec == "H264" else (lambda n: (n[0] >> 1) & 0x3F < 32)
        for n in nals:
            cur.append(n)
            if vcl(n):
                aus.append(cur)
                cur = []
        w = fmp4mux.SegmentWriter(TMP / "w" / f"real-{fmt}", codec)
        t0 = time.time() - 900
        for i, au in enumerate(aus):
            key = any((n[0] & 0x1F) == 5 if codec == "H264" else 16 <= (n[0] >> 1) & 0x3F <= 23 for n in au)
            w.add_video(t0 + i * 0.1, au, key)
        out = w.close()
        r = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                            "stream=nb_read_frames,width,height", "-of", "csv=p=0", str(out[0].path)], capture_output=True, text=True)
        assert r.stdout.strip().split(",") == ["320", "240", str(len(aus))], (codec, r.stdout, r.stderr)
        d = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out[0].path), "-f", "null", "-"], capture_output=True, text=True)
        assert d.returncode == 0 and not d.stderr.strip(), d.stderr


def test_retention_sees_restored_segments_like_any_other():
    from nvr import retention
    folder = sdbackfill.camera_folder("cam2")
    t0 = time.time() - 5 * 86400
    _, out, _ = _write(folder, n=60, t0=t0, lo=0)
    (folder / ".2026-01-01_00-00-00-000000.mp4.1.sdpart").write_bytes(b"unfinished")   # a restore in progress
    segs = retention.camera_segments("cam2")
    assert [p for _, p in segs] == [out[0].path] and abs(segs[0][0] - t0) < 1e-5
    assert abs(retention._segment_end(t0, out[0].path, None) - (t0 + 6.0)) < 1, "its end comes from its mtime, as for MediaMTX's"
    for p in folder.iterdir():
        p.unlink()


# --------------------------------------------------------------------------- a fake camera: reconnect and resume

class FakeCamera(threading.Thread):
    """A minimal ONVIF replay RTSP server on 127.0.0.1: Digest auth, DESCRIBE / SETUP / PLAY (Range: clock=),
    GET_PARAMETER, TEARDOWN; streams RTP over the RTSP connection (H.264: STAP-A, FU-A) with the 0xABAC extension as
    fast as the socket takes it, from the keyframe at or before the requested start through the end (plus one frame).
    `break_after`: cut each of the first sessions after that many frames. After its last frame (n) the camera keeps
    the session open, answering keep-alives (RFC 2326 pauses at the end), or with `close_at_end` closes it.
    `rotate_nonce`: the first keep-alive is answered 401 with a new nonce (Digest stale=true); `frame_delay`: seconds
    between frames (a camera replaying at real time)."""

    def __init__(self, t0, n=600, gop=10, break_after=(), user="viewer", password="pw-" + "z" * 6, close_at_end=False,
                 rotate_nonce=False, frame_delay=0.0):
        super().__init__(daemon=True)
        self.t0, self.n, self.gop = t0, n, gop
        self.frames = [(t0 + 0.05 + i * 0.1, i % gop == 5) for i in range(n)]   # keyframes at x.55 s
        self.break_after = list(break_after)
        self.user, self.password = user, password
        self.close_at_end, self.rotate_nonce, self.frame_delay = close_at_end, rotate_nonce, frame_delay
        self.nonce = "n0nce"
        self.authed: dict[str, list[bool]] = {"GET_PARAMETER": [], "TEARDOWN": []}   # was each one authenticated
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.plays: list[tuple[float, float]] = []
        self.sessions = 0
        self.unauthorized = 0

    def run(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(c,), daemon=True).start()

    def close(self):
        self.srv.close()

    def _ok_auth(self, method, hdrs):
        a = hdrs.get("authorization", "")
        m = dict(re.findall(r'(\w+)="([^"]*)"', a))
        if not a.startswith("Digest") or m.get("username") != self.user:
            return False
        md5 = lambda s: hashlib.md5(s.encode()).hexdigest()
        want = md5(f"{md5(f'{self.user}:fake:{self.password}')}:{self.nonce}:{md5(method + ':' + m.get('uri', ''))}")
        return m.get("response") == want

    def serve(self, c):
        self.sessions += 1
        cut = self.break_after.pop(0) if self.break_after else None
        lock = threading.Lock()
        buf = b""
        streaming = None

        def send(b):
            with lock:
                c.sendall(b)
        try:
            while True:
                while b"\r\n\r\n" not in buf:
                    d = c.recv(65536)
                    if not d:
                        return
                    buf += d
                head, buf = buf.split(b"\r\n\r\n", 1)
                lines = head.decode().split("\r\n")
                method, uri = lines[0].split()[:2]
                hdrs = {k.strip().lower(): v.strip() for k, v in (x.split(":", 1) for x in lines[1:] if ":" in x)}
                cseq = hdrs.get("cseq", "0")
                if method in ("DESCRIBE", "SETUP", "PLAY") and not self._ok_auth(method, hdrs):
                    self.unauthorized += 1
                    send(f'RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\nWWW-Authenticate: Digest realm="fake", nonce="{self.nonce}"\r\n\r\n'.encode())
                    continue
                if method == "GET_PARAMETER" and self.rotate_nonce and self.nonce == "n0nce":
                    self.nonce = "n3wnonce"
                    send(f'RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\n'
                         f'WWW-Authenticate: Digest realm="fake", nonce="{self.nonce}", stale=true\r\n\r\n'.encode())
                    continue
                if method in self.authed:
                    self.authed[method].append(self._ok_auth(method, hdrs))
                assert hdrs.get("require") == "onvif-replay" or method in ("GET_PARAMETER", "TEARDOWN"), hdrs
                if method == "DESCRIBE":
                    sdp = ("v=0\r\ns=replay\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
                           f"a=fmtp:96 packetization-mode=1;sprop-parameter-sets={base64.b64encode(SPS).decode()},{base64.b64encode(PPS).decode()}\r\n"
                           "a=control:track1\r\n").encode()
                    send(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nContent-Base: rtsp://127.0.0.1:{self.port}/onvifreplay/\r\n"
                         f"Content-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n".encode() + sdp)
                elif method == "SETUP":
                    send(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nTransport: {hdrs['transport']}\r\nSession: 4242;timeout=60\r\n\r\n".encode())
                elif method == "PLAY":
                    a, b = hdrs["range"].split("=", 1)[1].split("-")
                    start, end = sdreplay.parse_clock(a), sdreplay.parse_clock(b)
                    self.plays.append((start, end))
                    send(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nSession: 4242\r\nRange: {hdrs['range']}\r\n\r\n".encode())
                    streaming = threading.Thread(target=self.stream, args=(c, send, start, end, int(cseq), cut), daemon=True)
                    streaming.start()
                elif method in ("GET_PARAMETER", "TEARDOWN"):
                    send(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nSession: 4242\r\n\r\n".encode())
                    if method == "TEARDOWN":
                        return
        except OSError:
            return
        finally:
            try:
                c.close()
            except OSError:
                pass

    def stream(self, c, send, start, end, cseq, cut):
        first = max(i for i, (t, k) in enumerate(self.frames) if k and t <= start + 1e-6) if start >= self.frames[5][0] else 5
        seq, sent = 0, 0
        try:
            for i in range(first, self.n):
                t, key = self.frames[i]
                pk = packetize_h264(frame_nals(i, key))
                for j, pl in enumerate(pk):
                    seq += 1
                    pkt = rtp(seq, int((t - self.t0) * 90000), pl, marker=j == len(pk) - 1, ntp=t if j == 0 else None, clean=key, cseq=cseq)
                    send(b"$\x00" + struct.pack(">H", len(pkt)) + pkt)
                sent += 1
                if cut is not None and sent >= cut:
                    c.shutdown(socket.SHUT_RDWR)     # the link drops mid-GOP
                    c.close()
                    return
                if t >= end:
                    return
                if self.frame_delay:
                    time.sleep(self.frame_delay)
            if self.close_at_end:
                c.shutdown(socket.SHUT_RDWR)         # no more footage: this camera closes the connection
                c.close()
        except OSError:
            return


def test_resume_after_reconnect_no_duplicates_no_holes():
    t0 = time.time() - 4 * 3600
    cam = FakeCamera(t0, n=600, break_after=(123, 77))
    cam.start()
    folder = TMP / "recordings" / "resume"
    sink = sdbackfill.WriterSink(folder, t0 + 10.0, t0 + 40.0, offset=0.0)
    try:
        res = sdreplay.fetch_range(f"rtsp://127.0.0.1:{cam.port}/onvifreplay", cam.user, cam.password, t0 + 10.0, t0 + 40.0, sink)
    finally:
        cam.close()
    out = sink.writer.close()
    assert res.complete and res.reconnects == 2, res
    assert cam.unauthorized >= 1, "Digest challenge answered"
    # frames 100.. are t0+10.05..; the first keyframe at or after 10.0 is frame 105 (10.55); the range ends before 40.0
    got = [frame_index(s[1]) for o in out for s in read_samples(o.path)]
    assert got == list(range(105, 400)), (got[:5], got[-5:], [x for x in range(105, 400) if got.count(x) != 1][:10])
    # each reconnect asked for footage from before the resume point (the camera starts at a keyframe at or before it)
    assert len(cam.plays) == 3 and cam.plays[1][0] < cam.plays[2][0] and all(abs(p[1] - (t0 + 40.0)) < 0.002 for p in cam.plays), cam.plays
    times = [s[0] for o in out for s in read_samples(o.path)]
    assert all(b > a for a, b in zip(times, times[1:]))


def test_gives_up_when_no_progress():
    t0 = time.time() - 4 * 3600
    cam = FakeCamera(t0, n=200, break_after=(3, 3, 3, 3, 3, 3, 3, 3, 3, 3))
    cam.start()
    sink = sdbackfill.WriterSink(TMP / "recordings" / "giveup", t0 + 1.0, t0 + 15.0, offset=0.0)
    old = time.sleep
    sdreplay.time.sleep = lambda s: None
    try:
        res = sdreplay.fetch_range(f"rtsp://127.0.0.1:{cam.port}/onvifreplay", cam.user, cam.password, t0 + 1.0, t0 + 15.0, sink,
                                   max_reconnects=3)
    finally:
        sdreplay.time.sleep = old
        cam.close()
    assert not res.complete and "gave up" in res.reason and res.reconnects == 4, res


class _QuickStall:
    """STALL_S / KEEPALIVE_S scaled down (30 s / 20 s) so a camera that goes quiet is noticed in about a second."""

    def __enter__(self):
        self.old = sdreplay.STALL_S, sdreplay.KEEPALIVE_S
        sdreplay.STALL_S, sdreplay.KEEPALIVE_S = 1.5, 0.4

    def __exit__(self, *exc):
        sdreplay.STALL_S, sdreplay.KEEPALIVE_S = self.old


def _bounded(fn, timeout=20.0):
    """fn() in a thread: a regression that never ends fails the test instead of hanging the suite."""
    box = {}
    th = threading.Thread(target=lambda: box.setdefault("res", fn()), daemon=True)
    th.start()
    th.join(timeout)
    assert not th.is_alive(), f"still running after {timeout} s"
    return box["res"]


def test_replay_ends_when_the_camera_has_nothing_more():
    """The card's footage ends before the range does and the camera keeps the session open, answering keep-alives
    (RFC 2326 pauses at the end of the range): keep-alive replies must not count as data. The replay ends there
    without a reconnect and the last GOP is written. The camera also rotates its Digest nonce on a keep-alive (401
    stale=true): answered with the new one; TEARDOWN is authenticated."""
    t0 = time.time() - 4 * 3600
    cam = FakeCamera(t0, n=160, rotate_nonce=True)        # footage up to t0+15.95; the range runs to t0+20
    cam.start()
    sink = sdbackfill.WriterSink(TMP / "recordings" / "tail", t0 + 10.0, t0 + 20.0, offset=0.0)
    began = time.monotonic()
    with _QuickStall():
        try:
            res = _bounded(lambda: sdreplay.fetch_range(f"rtsp://127.0.0.1:{cam.port}/onvifreplay", cam.user, cam.password,
                                                        t0 + 10.0, t0 + 20.0, sink, stop=threading.Event()))
        finally:
            cam.close()
    assert time.monotonic() - began < 10 and res.reconnects == 0 and cam.sessions == 1, res
    assert not res.complete and "recording ends at" in res.reason, res
    out = sink.writer.close()
    got = [frame_index(s[1]) for o in out for s in read_samples(o.path)]
    assert got == list(range(105, 160)), (got[:3], got[-3:])      # the final GOP (155..159) too
    for _ in range(50):                                           # TEARDOWN reaches the camera's thread
        if cam.authed["TEARDOWN"]:
            break
        time.sleep(0.05)
    assert cam.authed["GET_PARAMETER"] and all(cam.authed["GET_PARAMETER"]), cam.authed
    assert cam.authed["TEARDOWN"] == [True], cam.authed


def test_replay_ends_when_the_camera_closes_at_its_last_frame_twice():
    """A camera that closes the connection when its footage ends: the first close looks like a dropped link (one
    reconnect); the resumed session ending at the very same frame is the end of the card, not another failure. No
    reconnects burnt, and the GOP the first close rolled back is written."""
    t0 = time.time() - 4 * 3600
    cam = FakeCamera(t0, n=160, close_at_end=True)
    cam.start()
    sink = sdbackfill.WriterSink(TMP / "recordings" / "closes", t0 + 10.0, t0 + 20.0, offset=0.0)
    try:
        res = _bounded(lambda: sdreplay.fetch_range(f"rtsp://127.0.0.1:{cam.port}/onvifreplay", cam.user, cam.password,
                                                    t0 + 10.0, t0 + 20.0, sink, stop=threading.Event()))
    finally:
        cam.close()
    assert res.reconnects == 1 and cam.sessions == 2 and "recording ends at" in res.reason, res
    got = [frame_index(s[1]) for o in sink.writer.close() for s in read_samples(o.path)]
    assert got == list(range(105, 160)), (got[:3], got[-3:])


def test_replay_wall_clock_is_bounded():
    t0 = time.time() - 4 * 3600
    cam = FakeCamera(t0, n=600, frame_delay=0.02)               # 300 frames for the range: ~6 s
    cam.start()
    sink = sdbackfill.WriterSink(TMP / "recordings" / "slow", t0 + 10.0, t0 + 40.0, offset=0.0)
    began = time.monotonic()
    try:
        res = _bounded(lambda: sdreplay.fetch_range(f"rtsp://127.0.0.1:{cam.port}/onvifreplay", cam.user, cam.password,
                                                    t0 + 10.0, t0 + 40.0, sink, max_wall_s=1.0))
    finally:
        cam.close()
    assert time.monotonic() - began < 4 and "took longer than 1 s" in res.reason and not res.complete, res
    assert res.reconnects == 0 and res.last is not None and res.last < t0 + 40.0


class BFrameCamera(FakeCamera):
    """Sends I P B B P B B ... in decode order: the ONVIF / RTP times are presentation times, so they go backwards
    at every B-frame (from frame 0, whatever the range)."""

    def stream(self, c, send, start, end, cseq, cut):
        order = [0]
        for k in range(1, self.n - 2, 3):
            order += [k + 2, k, k + 1]
        seq = 0
        try:
            for i in order:
                t, key = self.t0 + 0.05 + i * 0.1, i % 30 == 0
                pk = packetize_h264(frame_nals(i, key))
                for j, pl in enumerate(pk):
                    seq += 1
                    pkt = rtp(seq, int((t - self.t0) * 90000), pl, marker=j == len(pk) - 1, ntp=t if j == 0 else None, clean=key, cseq=cseq)
                    send(b"$\x00" + struct.pack(">H", len(pkt)) + pkt)
        except OSError:
            return


def test_bframes_fail_the_job_instead_of_half_rate_video():
    t0 = time.time() - 4 * 3600
    cam = BFrameCamera(t0, n=300)
    cam.start()
    db.upsert_camera({**CAM, "id": "bfr", "host": "127.0.0.1"})
    sdbackfill.save_status({"camera_id": "bfr", "checked_at": time.time(), "supported": True, "has_recording": True,
                            "earliest": t0, "latest": time.time(), "recording_now": True, "clock_offset_s": 0.0,
                            "replay_uri": f"rtsp://127.0.0.1:{cam.port}/onvifreplay"})

    async def lister(cid, start, end):
        return []

    async def go():
        bf = sdbackfill.Backfill(list_spans=lister)
        row = bf.submit("bfr", t0 + 1.0, t0 + 25.0, by="test")
        await bf._job(row)
        return db.one("SELECT * FROM restored_spans WHERE id=?", [row["id"]])
    try:
        written, res = sdbackfill.restore_range({**CAM, "id": "bfr", "host": "127.0.0.1"}, t0 + 1.0, t0 + 25.0, 0.0,
                                                replay_uri=f"rtsp://127.0.0.1:{cam.port}/onvifreplay")
        r = asyncio.run(go())
    finally:
        cam.close()
    assert written == [] and res.unsupported and res.reason == sdreplay.BFRAMES and res.reconnects == 0, res
    assert r["state"] == "failed" and r["error"] == sdreplay.BFRAMES and not r["restored_s"], r
    folder = sdbackfill.camera_folder("bfr")
    assert not folder.exists() or not list(folder.iterdir()), list(folder.iterdir())
    db.execute("DELETE FROM restored_spans")


def test_writer_failure_leaves_no_temp_file():
    """A disk error mid-run: the job fails, the open segment's temp file and handle go."""
    t0 = time.time() - 4 * 3600
    cam = FakeCamera(t0, n=300)
    cam.start()
    real = fmp4mux.SegmentWriter._write_fragment
    calls = {"n": 0}

    def failing(self, *a):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(28, "No space left on device")
        return real(self, *a)
    fmp4mux.SegmentWriter._write_fragment = failing
    try:
        sdbackfill.restore_range({**CAM, "id": "fail1", "host": "127.0.0.1"}, t0 + 10.0, t0 + 25.0, 0.0,
                                 replay_uri=f"rtsp://127.0.0.1:{cam.port}/onvifreplay")
        raise AssertionError("expected OSError")
    except OSError as e:
        assert e.errno == 28, e
    finally:
        fmp4mux.SegmentWriter._write_fragment = real
        cam.close()
    folder = sdbackfill.camera_folder("fail1")
    assert calls["n"] == 3 and folder.exists() and not list(folder.glob(".*.sdpart")), list(folder.iterdir())


def test_writer_parameter_change_keeps_the_old_gop_with_its_own_parameter_sets():
    """New parameter sets at a keyframe start a new segment; the GOP before it is written under the old ones."""
    sps2 = SPS[:3] + bytes([0x0C]) + SPS[4:]                 # the same picture at another level
    assert fmp4mux.video_info("H264", [sps2, PPS]).params != fmp4mux.video_info("H264", [SPS, PPS]).params
    w = fmp4mux.SegmentWriter(TMP / "w" / "pchange", "H264")
    t0 = time.time() - 1800
    for i in range(40):
        key = i % 10 == 0
        nals = frame_nals(i, key)
        if key and i >= 20:
            nals = [sps2, PPS] + nals[2:]
        w.add_video(t0 + i * 0.1, nals, key)
    out = w.close()
    assert [round(o.start - t0, 2) for o in out] == [0.0, 2.0], [o.start - t0 for o in out]
    a, b = out[0].path.read_bytes(), out[1].path.read_bytes()
    assert SPS in a and sps2 not in a and sps2 in b and SPS not in b
    assert [frame_index(s[1]) for o in out for s in read_samples(o.path)] == list(range(40))


def test_clock_offset_trusts_the_live_estimate_only_when_it_agrees():
    f, base = sdbackfill.clock_offset_for, settings.camera_clock_offset
    st = {"clock_offset_s": 3.0}                               # the camera is 3 s ahead: -3 s onto this server's clock
    assert f("c", -2.6, st) == -2.6 + base                     # within 2 s of ONVIF's: the finer live estimate
    assert f("c", 4.0, st) == -3.0 + base                      # 7 s apart (a reader backed up, just restarted): ONVIF's
    assert f("c", -2.6, st, live_samples=5) == -3.0 + base     # too few samples behind it
    assert f("c", -2.6, st, live_samples=500) == -2.6 + base
    assert f("c", 4.0, None) == 4.0 + base and f("c", None, st) == -3.0 + base and f("c", None, None) == base


def test_at_most_three_recoveries_at_once():
    async def go():
        bf = sdbackfill.Backfill(list_spans=lister)
        gate, started = asyncio.Event(), []

        async def job(row):
            started.append(row["camera_id"])
            await gate.wait()
            db.execute("UPDATE restored_spans SET state='recovered' WHERE id=?", [row["id"]])
        bf._job = job
        now = time.time()
        for i in range(5):
            bf.submit(f"par{i}", now - 1000 + i, now - 900 + i)
        bf._start_waiting()
        await asyncio.sleep(0)
        assert started == ["par4", "par3", "par2"] and len(bf.running) == 3, started    # newest first, the rest wait
        assert db.one("SELECT COUNT(*) AS n FROM restored_spans WHERE state='waiting'")["n"] == 5
        gate.set()
        await asyncio.sleep(0.05)
        bf._start_waiting()
        await asyncio.sleep(0)
        assert sorted(started) == [f"par{i}" for i in range(5)], started
        return bf

    async def lister(cid, start, end):
        return []
    bf = asyncio.run(go())
    assert bf.pool._max_workers == sdbackfill.MAX_PARALLEL == 3
    db.execute("DELETE FROM restored_spans")


# --------------------------------------------------------------------------- the worker, end to end

def test_camera_folder_is_the_only_place_written():
    assert sdbackfill.camera_folder("cam1") == (settings.recordings_dir / "cam1").resolve()
    for bad in ("../cam1", "cam1/..", "CAM1", "", "a" * 40):
        try:
            sdbackfill.camera_folder(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    folder = sdbackfill.camera_folder("cam1")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".2026-01-01_00-00-00-000000.mp4.99.sdpart").write_bytes(b"x")
    (folder / "2026-01-01_00-00-00-000000.mp4").write_bytes(b"keep")
    (folder / ".other").write_bytes(b"keep")
    assert sdbackfill.clean_temp_files("cam1") == 1
    assert sorted(p.name for p in folder.iterdir()) == [".other", "2026-01-01_00-00-00-000000.mp4"]
    for p in folder.iterdir():
        p.unlink()


def test_worker_job_end_to_end():
    t0 = time.time() - 4 * 3600
    cam = FakeCamera(t0, n=900, break_after=(150,))
    cam.start()
    db.upsert_camera({**CAM, "host": "127.0.0.1"})
    sdbackfill.save_status({"camera_id": "cam1", "checked_at": time.time(), "supported": True, "has_recording": True,
                            "earliest": t0, "latest": time.time(), "recording_now": True, "clock_offset_s": 0.0,
                            "replay_uri": f"rtsp://127.0.0.1:{cam.port}/onvifreplay"})
    iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()

    async def lister(cid, start, end):    # the server's own recordings: up to t0+20, then from t0+60
        return [{"start": iso(t0), "duration": 20.0}, {"start": iso(t0 + 60.0), "duration": 600}]
    folder = sdbackfill.camera_folder("cam1")
    before = set(folder.glob("*")) if folder.exists() else set()

    async def go():
        bf = sdbackfill.Backfill(live_offset=lambda cid: -0.5, list_spans=lister)
        row = bf.submit("cam1", t0 + 20.0, t0 + 60.0, by="test")
        assert row["state"] == "waiting"
        await bf._job(row)
        return db.one("SELECT * FROM restored_spans WHERE id=?", [row["id"]])
    try:
        r = asyncio.run(go())
    finally:
        cam.close()
    assert r["state"] == "recovered" and r["error"] is None, r
    new = sorted(set(folder.glob("*")) - before)
    assert new and all(p.parent == folder and p.suffix == ".mp4" for p in new), new
    assert r["bytes"] == sum(p.stat().st_size for p in new) and 38 < r["restored_s"] <= 40, r
    # camera time + offset (-0.5): frame i at t0+0.05+0.1i-0.5; the first keyframe at or after t0+20 is frame 205
    first = read_samples(new[0])[0]
    assert frame_index(first[1]) == 205 and abs(first[0] - (t0 + 20.05)) < 1e-3
    assert r["done_from"] >= t0 + 20.0 and r["done_to"] <= t0 + 60.0 + 1e-6
    db.execute("DELETE FROM restored_spans")
    for p in new:
        p.unlink()


# --------------------------------------------------------------------------- API

def call(method, path, body=None, role=None):
    from nvr.api import app

    async def go():
        tunneled = role is not None
        transport = httpx.ASGITransport(app=hub_agent.as_tunnel(app) if tunneled else app,
                                        client=hub_agent.IN_PROCESS_CLIENT if tunneled else ("192.168.1.9", 5000))
        hdrs = {"x-hub-role": role, "x-hub-user": f"{role}@example"} if tunneled else {}
        async with httpx.AsyncClient(transport=transport, base_url="http://site") as c:
            return await c.request(method, path, json=body, headers=hdrs)
    return asyncio.run(go())


def test_api_permissions_and_checks():
    from nvr import api
    api.state.sd = sdbackfill.Backfill(list_spans=None)
    db.upsert_camera(CAM)
    now = time.time()
    sdbackfill.save_status({"camera_id": "cam1", "checked_at": now, "supported": True, "has_recording": True,
                            "earliest": now - 86400, "latest": now, "recording_now": True})
    body = {"camera_id": "cam1", "from": now - 3000, "to": now - 2700}
    for role in ("viewer", "operator"):
        r = call("POST", "/api/sd/recover", body, role=role)
        assert r.status_code == 403 and "admin" in r.text, (role, r.text)
    assert db.one("SELECT COUNT(*) AS n FROM restored_spans")["n"] == 0
    r = call("POST", "/api/sd/recover", body, role="admin")
    assert r.status_code == 200 and r.json()["state"] == "waiting" and r.json()["requested_by"] == "admin@example", r.text
    assert call("POST", "/api/sd/recover", body).status_code == 409                       # already being recovered
    assert call("POST", "/api/sd/recover", {**body, "to": body["from"]}).status_code == 422
    assert call("POST", "/api/sd/recover", {**body, "to": now + 60}).status_code == 422     # the future
    assert call("POST", "/api/sd/recover", {**body, "from": now - 3 * 86400, "to": now - 2.5 * 86400}).status_code == 409  # not on the card
    assert call("POST", "/api/sd/recover", {**body, "camera_id": "nope"}).status_code == 404
    r = call("POST", "/api/sd/recover", {**body, "from": now - 1000, "to": now - 900})    # the server's own UI (LAN): allowed
    assert r.status_code == 200 and r.json()["requested_by"] == "local", r.text
    sdbackfill.save_status({"camera_id": "cam1", "checked_at": now, "supported": True, "has_recording": False})
    assert call("POST", "/api/sd/recover", {**body, "from": now - 600, "to": now - 500}).status_code == 409
    # reading is open to viewers: status (cached) and the gaps listing (MediaMTX unreachable here: no gaps, an error)
    r = call("GET", "/api/cameras/cam1/sd", role="viewer")
    assert r.status_code == 200 and r.json()["text"] == "SD card: no recording on the camera", r.text
    r = call("GET", "/api/sd/gaps?camera=cam1", role="viewer")
    assert r.status_code == 200 and r.json()["cameras"][0]["error"] and len(r.json()["cameras"][0]["restored"]) == 2, r.text
    # the Timeline's listing carries the jobs to shade
    r = call("GET", f"/api/recordings/cam1?start={now - 4000}&end={now}", role="viewer")
    assert r.status_code == 200 and [x["state"] for x in r.json()["restored"]] == ["waiting", "waiting"], r.text
    db.execute("DELETE FROM restored_spans")


def test_public_replay_port_stored_and_validated():
    from nvr import mediamtx
    from nvr.api import CameraIn
    assert "public_replay_port" in mediamtx.camera_problem({**CAM, "public_host": "203.0.113.50", "public_replay_port": 70000})
    c = CameraIn(id="cam9", name="G", host="10.0.0.9", public_host="203.0.113.50", public_replay_port=0)
    assert c.public_replay_port is None
    db.upsert_camera({**CAM, "id": "cam9", "public_host": "203.0.113.50", "public_replay_port": 5551})
    assert next(c for c in db.cameras() if c["id"] == "cam9")["public_replay_port"] == 5551


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
