"""ONVIF Profile G replay: fetch a time range from a camera's own recording (its SD card) over RTSP.

The camera serves its recording at a separate RTSP URL (GetReplayUri; Milesight: rtsp://<camera>:555/onvifreplay).
A replay session is an ordinary RTSP session with two ONVIF additions (ONVIF Streaming Specification, "Replay"):

- every request carries `Require: onvif-replay`, and PLAY names the range in camera wall-clock time
  (`Range: clock=20261008T155903Z-20261008T160304Z`);
- every access unit's first RTP packet carries a header extension (profile 0xABAC) with the NTP wall-clock time of
  the frame on the camera, a clean-point flag (keyframe), a discontinuity flag (a hole in the camera's own
  recording) and the low byte of the PLAY request's CSeq.

ffmpeg and MediaMTX can't send either, hence this client. What the cameras tested do (docs/sd-card-backfill.md):
replay runs at real time only (Rate-Control / Scale are ignored), a session goes silent and is closed after ~65 s
without a keep-alive (GET_PARAMETER every 20 s), and footage is the main stream.

Blocking sockets, like ingest.py: run `fetch_range` in a thread. Credentials are never logged.
"""
from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import logging
import re
import socket
import struct
import threading
import time
from typing import Callable, Iterator

from .onvif_soap import rewrite
from .rtsp_client import Rtsp

log = logging.getLogger("nvr.sdreplay")

DEFAULT_REPLAY_PORT = 555          # Milesight; GetReplayUri gives the camera's own when it has the replay service
DEFAULT_REPLAY_PATH = "/onvifreplay"
KEEPALIVE_S = 20.0                 # GET_PARAMETER this often (the camera ends a silent session after ~65 s)
STALL_S = 30.0                     # no packet at all for this long = the session is dead
NTP_EPOCH = 2208988800             # seconds from 1900-01-01 to 1970-01-01
USER_AGENT = "AxiomVision-sdreplay"


class ReplayError(Exception):
    """The session failed (connection, protocol); worth a reconnect."""


class NotOnCard(ReplayError):
    """The camera has no footage for the range (PLAY refused with 457 Invalid Range, 404...)."""


class AuthFailed(ReplayError):
    """The camera refused the credentials: no point retrying."""


# --------------------------------------------------------------------------- RTP

@dataclasses.dataclass
class OnvifExt:
    ntp: float          # epoch seconds (camera clock)
    clean: bool         # C: the access unit starts a keyframe (clean point)
    end: bool           # E: last access unit of a contiguous section
    discontinuity: bool  # D: there is a hole in the recording before this access unit
    cseq: int           # low byte of the PLAY request's CSeq


@dataclasses.dataclass
class RtpPacket:
    seq: int
    ts: int
    marker: bool
    pt: int
    ssrc: int
    payload: bytes
    onvif: OnvifExt | None


def ntp_to_epoch(sec: int, frac: int) -> float:
    return sec - NTP_EPOCH + frac / 2 ** 32


def epoch_to_clock(t: float) -> str:
    """Epoch seconds -> the RTSP clock range form, UTC: 20261008T155903Z (fractions as .123 when present)."""
    d = dt.datetime.fromtimestamp(t, dt.timezone.utc)
    s = d.strftime("%Y%m%dT%H%M%S")
    if d.microsecond:
        s += f".{d.microsecond // 1000:03d}"
    return s + "Z"


def parse_clock(s: str) -> float:
    """20261008T155903Z / 20261008T155903.250Z -> epoch seconds."""
    m = re.fullmatch(r"(\d{8}T\d{6})(\.\d+)?Z", s.strip())
    if not m:
        raise ValueError(f"not an RTSP clock time: {s!r}")
    base = dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=dt.timezone.utc).timestamp()
    return base + (float(m.group(2)) if m.group(2) else 0.0)


def parse_rtp(pkt: bytes) -> RtpPacket | None:
    """One RTP packet (RFC 3550) with the ONVIF replay extension decoded when present. None if malformed."""
    if len(pkt) < 12 or pkt[0] >> 6 != 2:
        return None
    b0, b1 = pkt[0], pkt[1]
    seq, ts, ssrc = struct.unpack(">HII", pkt[2:12])
    off = 12 + 4 * (b0 & 0x0F)
    onvif = None
    if b0 & 0x10:
        if len(pkt) < off + 4:
            return None
        prof, words = struct.unpack(">HH", pkt[off:off + 4])
        ext = pkt[off + 4:off + 4 + 4 * words]
        if len(ext) < 4 * words:
            return None
        if prof == 0xABAC and len(ext) >= 8:
            sec, frac = struct.unpack(">II", ext[:8])
            flags = ext[8] if len(ext) > 8 else 0
            onvif = OnvifExt(ntp_to_epoch(sec, frac), bool(flags & 0x80), bool(flags & 0x40), bool(flags & 0x20),
                             ext[9] if len(ext) > 9 else 0)
        off += 4 + 4 * words
    end = len(pkt)
    if b0 & 0x20:   # padding
        end -= pkt[-1]
    if end < off:
        return None
    return RtpPacket(seq, ts, bool(b1 & 0x80), b1 & 0x7F, ssrc, pkt[off:end], onvif)


# --------------------------------------------------------------------------- depacketizing

@dataclasses.dataclass
class AccessUnit:
    """One video frame: its NAL units (no start codes), camera wall-clock time and whether it is a keyframe."""
    ntp: float
    nals: list[bytes]
    keyframe: bool
    discontinuity: bool = False


@dataclasses.dataclass
class AudioChunk:
    ntp: float
    pcm: bytes          # 16-bit big-endian signed samples (MediaMTX records G.711 as LPCM)
    samples: int


def h264_type(nal: bytes) -> int:
    return nal[0] & 0x1F


def h265_type(nal: bytes) -> int:
    return (nal[0] >> 1) & 0x3F


class VideoDepacketizer:
    """RTP -> access units for H.264 (RFC 6184: single NAL, STAP-A, FU-A) and H.265 (RFC 7798: single NAL, AP, FU).

    An access unit ends on the marker bit or when the RTP timestamp changes. A lost packet (sequence gap) or a
    broken fragment drops the frame and everything up to the next keyframe, so the output always decodes. The
    frame's time is the ONVIF extension's NTP time (first packet that carries it), or, between extensions, the
    last NTP time advanced by the RTP clock (90 kHz)."""

    def __init__(self, codec: str, clock_rate: int = 90000):
        self.codec = codec.upper()
        if self.codec not in ("H264", "H265"):
            raise ValueError(f"unsupported video codec {codec}")
        self.rate = clock_rate or 90000
        self.nals: list[bytes] = []
        self.fu: bytearray | None = None
        self.cur_ts: int | None = None
        self.cur_ntp: float | None = None
        self.cur_disc = False
        self.cur_broken = False
        self.last_seq: int | None = None
        self.anchor: tuple[int, float] | None = None   # (rtp ts, ntp) of the last extension seen
        self.need_key = True
        self.lost = 0                                  # packets lost (sequence gaps)

    def reset(self) -> None:
        """A new session: forget the partial frame, the sequence and the time anchor; wait for a keyframe."""
        self.__init__(self.codec, self.rate)

    def _keyframe(self, nals: list[bytes]) -> bool:
        if self.codec == "H264":
            return any(h264_type(n) == 5 for n in nals)
        return any(16 <= h265_type(n) <= 23 for n in nals)

    def _finish(self) -> AccessUnit | None:
        nals, ntp, disc, broken = self.nals, self.cur_ntp, self.cur_disc, self.cur_broken or self.fu is not None
        self.nals, self.fu, self.cur_ts, self.cur_ntp, self.cur_disc, self.cur_broken = [], None, None, None, False, False
        if not nals or ntp is None:
            return None
        if broken:
            self.need_key = True
            return None
        key = self._keyframe(nals)
        if self.need_key and not key:
            return None
        self.need_key = False
        return AccessUnit(ntp, nals, key, disc)

    def push(self, p: RtpPacket) -> list[AccessUnit]:
        out: list[AccessUnit] = []
        if self.last_seq is not None and p.seq != (self.last_seq + 1) & 0xFFFF:
            self.lost += (p.seq - self.last_seq - 1) & 0xFFFF
            self.cur_broken = True            # the frame being assembled lost a packet
        self.last_seq = p.seq
        if self.cur_ts is not None and p.ts != self.cur_ts:
            au = self._finish()
            if au:
                out.append(au)
        if self.cur_ts is None:
            self.cur_ts = p.ts
            if p.onvif:
                self.anchor = (p.ts, p.onvif.ntp)
            if self.anchor:
                d = ((p.ts - self.anchor[0] + 2 ** 31) % 2 ** 32) - 2 ** 31   # signed 32-bit difference
                self.cur_ntp = self.anchor[1] + d / self.rate
        elif p.onvif and self.cur_ntp is None:
            self.anchor = (p.ts, p.onvif.ntp)
            self.cur_ntp = p.onvif.ntp
        if p.onvif and p.onvif.discontinuity:
            self.cur_disc = True
        try:
            self._payload(p.payload)
        except (IndexError, struct.error):
            self.cur_broken = True
        if p.marker:
            au = self._finish()
            if au:
                out.append(au)
        return out

    def _payload(self, pl: bytes) -> None:
        if not pl:
            return
        if self.codec == "H264":
            t = pl[0] & 0x1F
            if 1 <= t <= 23:
                self.nals.append(bytes(pl))
            elif t == 24:   # STAP-A
                i = 1
                while i + 2 <= len(pl):
                    n = struct.unpack(">H", pl[i:i + 2])[0]
                    if n == 0 or i + 2 + n > len(pl):
                        raise IndexError("bad STAP-A")
                    self.nals.append(bytes(pl[i + 2:i + 2 + n]))
                    i += 2 + n
            elif t == 28:   # FU-A
                fh = pl[1]
                if fh & 0x80:
                    if self.fu is not None:
                        self.cur_broken = True
                    self.fu = bytearray([(pl[0] & 0xE0) | (fh & 0x1F)])
                elif self.fu is None:
                    self.cur_broken = True     # continuation without its start
                    return
                self.fu += pl[2:]
                if fh & 0x40:
                    self.nals.append(bytes(self.fu))
                    self.fu = None
            # 25-27, 29 (STAP-B, MTAP, FU-B): not used by cameras in non-interleaved mode; ignored
            return
        t = (pl[0] >> 1) & 0x3F
        if t < 48:
            self.nals.append(bytes(pl))
        elif t == 48:       # aggregation packet (no DONL: sprop-max-don-diff is 0 for cameras)
            i = 2
            while i + 2 <= len(pl):
                n = struct.unpack(">H", pl[i:i + 2])[0]
                if n == 0 or i + 2 + n > len(pl):
                    raise IndexError("bad AP")
                self.nals.append(bytes(pl[i + 2:i + 2 + n]))
                i += 2 + n
        elif t == 49:       # fragmentation unit
            fh = pl[2]
            if fh & 0x80:
                if self.fu is not None:
                    self.cur_broken = True
                self.fu = bytearray([(pl[0] & 0x81) | ((fh & 0x3F) << 1), pl[1]])
            elif self.fu is None:
                self.cur_broken = True
                return
            self.fu += pl[3:]
            if fh & 0x40:
                self.nals.append(bytes(self.fu))
                self.fu = None
        # 50 (PACI): ignored

    def flush(self) -> list[AccessUnit]:
        au = self._finish()
        return [au] if au else []


def _ulaw_table() -> list[int]:
    out = []
    for i in range(256):
        u = ~i & 0xFF
        sign, exp, mant = u & 0x80, (u >> 4) & 0x07, u & 0x0F
        v = ((mant << 3) + 0x84) << exp
        v -= 0x84
        out.append(-v if sign else v)
    return out


def _alaw_table() -> list[int]:
    out = []
    for i in range(256):
        a = i ^ 0x55
        sign, exp, mant = a & 0x80, (a >> 4) & 0x07, a & 0x0F
        v = (mant << 4) + 8 if exp == 0 else ((mant << 4) + 0x108) << (exp - 1)
        out.append(v if sign else -v)
    return out


ULAW = [struct.pack(">h", v) for v in _ulaw_table()]
ALAW = [struct.pack(">h", v) for v in _alaw_table()]


class AudioDepacketizer:
    """G.711 (PCMU / PCMA) or L16 RTP -> 16-bit big-endian PCM chunks with their camera wall-clock time."""

    def __init__(self, codec: str, clock_rate: int = 8000, channels: int = 1):
        self.codec = codec.upper()
        if self.codec not in ("PCMU", "PCMA", "L16"):
            raise ValueError(f"unsupported audio codec {codec}")
        self.rate = clock_rate or 8000
        self.channels = channels or 1
        self.anchor: tuple[int, float] | None = None

    def reset(self) -> None:
        self.anchor = None

    def push(self, p: RtpPacket) -> list[AudioChunk]:
        if p.onvif:
            self.anchor = (p.ts, p.onvif.ntp)
        if not self.anchor or not p.payload:
            return []
        d = ((p.ts - self.anchor[0] + 2 ** 31) % 2 ** 32) - 2 ** 31
        ntp = self.anchor[1] + d / self.rate
        if self.codec == "L16":
            pcm = p.payload[:len(p.payload) // 2 * 2]
        else:
            table = ULAW if self.codec == "PCMU" else ALAW
            pcm = b"".join(table[b] for b in p.payload)
        return [AudioChunk(ntp, pcm, len(pcm) // (2 * self.channels))]


# --------------------------------------------------------------------------- SDP

@dataclasses.dataclass
class Track:
    kind: str           # video | audio | application
    codec: str          # H264, H265, PCMU, PCMA, L16, vnd.onvif.metadata...
    clock_rate: int
    channels: int
    control: str
    fmtp: dict[str, str]


def parse_sdp(sdp: str) -> list[Track]:
    tracks: list[Track] = []
    cur: dict | None = None

    def done():
        if cur:
            tracks.append(Track(cur["kind"], cur.get("codec", ""), cur.get("rate", 0), cur.get("ch", 1),
                                cur.get("control", ""), cur.get("fmtp", {})))
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("m="):
            done()
            parts = line[2:].split()
            cur = {"kind": parts[0], "pt": parts[3] if len(parts) > 3 else ""}
            if cur["pt"] == "0":
                cur.update(codec="PCMU", rate=8000)
            elif cur["pt"] == "8":
                cur.update(codec="PCMA", rate=8000)
        elif cur is not None and line.startswith("a=rtpmap:"):
            enc = line.split(None, 1)[1] if " " in line else ""
            bits = enc.split("/")
            cur["codec"] = bits[0].upper() if cur["kind"] != "application" else bits[0]
            cur["rate"] = int(bits[1]) if len(bits) > 1 and bits[1].isdigit() else 0
            cur["ch"] = int(bits[2]) if len(bits) > 2 and bits[2].isdigit() else 1
        elif cur is not None and line.startswith("a=fmtp:"):
            body = line.split(None, 1)[1] if " " in line else ""
            cur["fmtp"] = dict(kv.strip().split("=", 1) for kv in body.split(";") if "=" in kv)
        elif cur is not None and line.startswith("a=control:"):
            cur["control"] = line[len("a=control:"):]
    done()
    return tracks


def parameter_sets(track: Track) -> list[bytes]:
    """VPS/SPS/PPS (H.265) or SPS/PPS (H.264) from the SDP's fmtp, in that order."""
    out = []
    for key in ("sprop-vps", "sprop-sps", "sprop-pps", "sprop-parameter-sets"):
        for part in (track.fmtp.get(key) or "").split(","):
            if part.strip():
                try:
                    out.append(base64.b64decode(part.strip()))
                except ValueError:
                    pass
    return out


# --------------------------------------------------------------------------- the session

def replay_url(cam: dict, reported: str | None = None) -> str:
    """Where to replay from: the URI GetReplayUri reported (credentials stripped) or rtsp://<host>:555/onvifreplay,
    pointed at the camera's outside address and replay port in port-forward mode (public_host /
    public_replay_port; an empty public_replay_port keeps the port the camera reported)."""
    url = reported or f"rtsp://{cam['host']}:{DEFAULT_REPLAY_PORT}{DEFAULT_REPLAY_PATH}"
    url = re.sub(r"^(rtsps?://)[^@/]*@", r"\1", url.strip())   # never carry user info from the camera
    return rewrite(url, {**cam, "public_rtsp_port": cam.get("public_replay_port")}) if cam.get("public_host") else url


class ReplaySession:
    """One RTSP replay session (TCP interleaved). `open()` -> DESCRIBE, SETUP the wanted tracks, PLAY the range;
    `packets()` yields (track index, RtpPacket) with keep-alives; `close()` tears down."""

    def __init__(self, url: str, user: str, password: str, timeout: float = 15):
        self.url = url
        try:
            self.rtsp = Rtsp(url, user, password, timeout=timeout)
        except OSError as e:
            raise ReplayError(f"cannot connect to the camera's replay port: {e}") from None
        self.tracks: list[Track] = []
        self.channels: dict[int, int] = {}     # interleaved RTP channel -> index into self.tracks
        self.play_cseq = 0
        self._last_ka = 0.0

    def _req(self, method: str, uri: str, extra: dict | None = None) -> tuple[int, dict, bytes]:
        h = {"User-Agent": USER_AGENT, "Require": "onvif-replay", **(extra or {})}
        try:
            status, hdrs, body = self.rtsp.request(method, uri, h)
        except (OSError, ConnectionError, ValueError, IndexError) as e:
            raise ReplayError(f"{method}: {e}") from None
        if status == 401:
            raise AuthFailed(f"{method}: the camera refused the credentials")
        return status, hdrs, body

    def open(self, start: float, end: float, kinds: tuple[str, ...] = ("video", "audio")) -> list[Track]:
        status, hdrs, sdp = self._req("DESCRIBE", self.url, {"Accept": "application/sdp"})
        if status != 200:
            raise ReplayError(f"DESCRIBE {status}")
        base = hdrs.get("content-base") or self.url
        all_tracks = parse_sdp(sdp.decode("utf-8", "replace"))
        wanted = []
        for kind in kinds:
            t = next((x for x in all_tracks if x.kind == kind), None)
            if t is not None:
                wanted.append(t)
        if not any(t.kind == "video" for t in wanted):
            raise ReplayError("the replay has no video track")
        for i, t in enumerate(wanted):
            turl = t.control if t.control.startswith("rtsp://") else base.rstrip("/") + "/" + t.control.lstrip("/")
            ch = 2 * i
            status, hdrs, _ = self._req("SETUP", turl, {"Transport": f"RTP/AVP/TCP;unicast;interleaved={ch}-{ch + 1}"})
            if status != 200:
                raise ReplayError(f"SETUP {t.kind} {status}")
            if not self.rtsp.session:
                self.rtsp.session = (hdrs.get("session") or "").split(";")[0] or None
            m = re.search(r"interleaved=(\d+)", hdrs.get("transport", ""))
            self.channels[int(m.group(1)) if m else ch] = i
        self.tracks = wanted
        self.base = base
        status, hdrs, _ = self._req("PLAY", base, {"Range": f"clock={epoch_to_clock(start)}-{epoch_to_clock(end)}",
                                                   "Rate-Control": "no", "Immediate": "yes"})
        self.play_cseq = self.rtsp.cseq & 0xFF
        if status in (404, 457):
            raise NotOnCard(f"PLAY {status}: no recording for that range on the camera")
        if status != 200:
            raise ReplayError(f"PLAY {status}")
        self._last_ka = time.time()
        return wanted

    def keepalive(self) -> None:
        r = self.rtsp
        r.cseq += 1
        h = {"CSeq": str(r.cseq), "User-Agent": USER_AGENT, "Require": "onvif-replay", "Session": r.session or ""}
        auth = r._auth_header("GET_PARAMETER", self.base)
        if auth:
            h["Authorization"] = auth
        msg = f"GET_PARAMETER {self.base} RTSP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
        r.sock.sendall(msg.encode())
        self._last_ka = time.time()

    def packets(self, stop: threading.Event | None = None) -> Iterator[tuple[int, RtpPacket]]:
        r = self.rtsp
        r.sock.settimeout(5)
        last_data = time.time()
        while not (stop and stop.is_set()):
            if time.time() - self._last_ka > KEEPALIVE_S:
                try:
                    self.keepalive()
                except OSError as e:
                    raise ReplayError(f"keep-alive: {e}") from None
            try:
                item = r.read_interleaved()
            except socket.timeout:
                if time.time() - last_data > STALL_S:
                    raise ReplayError(f"no data for {STALL_S:.0f} s")
                continue
            except (OSError, ConnectionError) as e:
                raise ReplayError(f"connection lost: {e}") from None
            except (ValueError, IndexError):
                raise ReplayError("unexpected data from the camera") from None
            last_data = time.time()
            if not item:
                continue          # an RTSP reply (keep-alive)
            ch, data = item
            idx = self.channels.get(ch)
            if idx is None:
                continue          # RTCP (odd channel) or unknown
            p = parse_rtp(data)
            if p is None:
                continue
            if p.onvif and p.onvif.cseq not in (0, self.play_cseq):
                continue          # data from an earlier PLAY on this session
            yield idx, p

    def close(self) -> None:
        try:
            if self.rtsp.session:
                self.rtsp.cseq += 1
                self.rtsp.sock.sendall((f"TEARDOWN {getattr(self, 'base', self.url)} RTSP/1.0\r\nCSeq: {self.rtsp.cseq}\r\n"
                                        f"Session: {self.rtsp.session}\r\nUser-Agent: {USER_AGENT}\r\n\r\n").encode())
        except OSError:
            pass
        try:
            self.rtsp.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- fetch with reconnect + resume

class _Done(Exception):
    """The range's end was reached."""


class Sink:
    """What `fetch_range` feeds. Times are camera wall-clock epoch seconds.
    `rollback()` is called after a broken session: drop anything not yet safely written (an incomplete GOP) and
    return the camera time to resume from (the dropped keyframe's time, or the last written frame's end)."""

    def start(self, tracks: list[Track]) -> None: ...
    def video(self, au: AccessUnit) -> None: ...
    def audio(self, chunk: AudioChunk) -> None: ...
    def rollback(self) -> float | None: ...


@dataclasses.dataclass
class FetchResult:
    first: float | None = None      # first frame delivered (camera time)
    last: float | None = None       # last frame delivered
    reconnects: int = 0
    reason: str = ""                # why it stopped: "end", "not on card", "stopped", error text
    complete: bool = False


def fetch_range(url: str, user: str, password: str, start: float, end: float, sink: Sink, *,
                stop: threading.Event | None = None, kinds: tuple[str, ...] = ("video", "audio"),
                max_reconnects: int = 5, resume_lead_s: float = 4.0, session_factory: Callable[..., ReplaySession] | None = None,
                on_progress: Callable[[float], None] | None = None) -> FetchResult:
    """Replay [start, end) (camera clock) into `sink`, reconnecting up to `max_reconnects` times in a row after a
    broken session and resuming where the sink says, without duplicates: the new session starts `resume_lead_s`
    early (so the camera begins at a keyframe at or before the resume point) and frames before the resume point
    are skipped. A frame is never delivered twice (strictly increasing times)."""
    factory = session_factory or ReplaySession
    res = FetchResult()
    pos = start
    failures = 0
    started = False
    while pos < end - 0.05:
        if stop and stop.is_set():
            res.reason = "stopped"
            return res
        session = None
        skip_before = pos if pos > start else None
        try:
            session = factory(url, user, password)
            tracks = session.open(max(start, pos - resume_lead_s) if skip_before else pos, end, kinds)
            if not started:
                sink.start(tracks)
                started = True
            deps: dict[int, VideoDepacketizer | AudioDepacketizer] = {}
            for i, t in enumerate(tracks):
                if t.kind == "video":
                    deps[i] = VideoDepacketizer(t.codec, t.clock_rate)
                elif t.kind == "audio" and t.codec in ("PCMU", "PCMA", "L16"):
                    deps[i] = AudioDepacketizer(t.codec, t.clock_rate, t.channels)
            last_progress = 0.0
            for idx, p in session.packets(stop):
                d = deps.get(idx)
                if d is None:
                    continue
                if isinstance(d, VideoDepacketizer):
                    for au in d.push(p):
                        if au.ntp >= end:
                            res.reason, res.complete = "end", True
                            raise _Done
                        if skip_before is not None and au.ntp < skip_before - 0.001:
                            continue
                        if res.last is not None and au.ntp <= res.last:
                            continue          # never twice
                        skip_before = None
                        sink.video(au)
                        res.first = au.ntp if res.first is None else res.first
                        res.last = au.ntp
                        if on_progress and au.ntp - last_progress >= 5:
                            last_progress = au.ntp
                            on_progress(au.ntp)
                else:
                    for ch in d.push(p):
                        if start <= ch.ntp < end and (skip_before is None or ch.ntp >= skip_before):
                            sink.audio(ch)
            res.reason = "stopped" if stop and stop.is_set() else "camera ended the replay"
            if res.reason == "stopped":
                return res
            # the camera closed the stream before the end of the range: the card has nothing more, or it gave up
            if res.last is not None and res.last >= end - 2:
                res.complete = True
                return res
            raise ReplayError("the camera ended the replay early")
        except _Done:
            return res
        except NotOnCard as e:
            res.reason = str(e)
            return res
        except AuthFailed as e:
            res.reason = str(e)
            return res
        except ReplayError as e:
            if res.last is not None and res.last >= end - 1.0:
                # it ended (or broke) at the very end of the range: everything asked for has arrived
                res.reason, res.complete = "end", True
                return res
            res.reconnects += 1
            resume = sink.rollback()
            progressed = resume is not None and resume > pos + 0.5
            failures = 1 if progressed else failures + 1     # "in a row": a session that moved the resume point resets it
            if resume is not None:
                pos = max(pos, resume)
                if res.last is not None and res.last >= pos:
                    res.last = pos - 1e-6     # frames from the resume point on were dropped by the sink: deliver them again
            log.warning("replay %s: %s; reconnect %d/%d from %s", _safe(url), e, failures, max_reconnects, epoch_to_clock(pos))
            if failures > max_reconnects:
                res.reason = f"gave up after {failures} failed sessions: {e}"
                return res
            delay = min(2 ** (failures - 1), 15)
            if stop is not None:
                if stop.wait(delay):
                    res.reason = "stopped"
                    return res
            else:
                time.sleep(delay)
        finally:
            if session is not None:
                session.close()
    res.complete, res.reason = True, res.reason or "end"
    return res


def _safe(url: str) -> str:
    return re.sub(r"//[^@/]*@", "//", url)
