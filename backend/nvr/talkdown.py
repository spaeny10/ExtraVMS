"""Talk-down: play an audio message on an IP speaker through its ONVIF RTSP backchannel.

The speaker (the first one: Tonmind IP Speaker CS20, rtsp://host:554/main, Digest auth) answers a DESCRIBE that
carries `Require: www.onvif.org/ver20/backchannel` with an extra audio section marked a=sendonly (from its side: it
receives what we send), e.g. `m=audio 0 RTP/AVP 0 8 97` + `a=control:audioback`. We SETUP only that track over TCP
interleaved channels, PLAY, and send RTP: 8 kHz mono G.711 (PCMU or PCMA, payload type from the SDP), 160 bytes =
20 ms per packet, paced in real time. Every request carries the Require header (devices drop the backchannel without
it). GET_PARAMETER keeps the session alive; TEARDOWN ends it.

One playback per speaker: a new one REPLACES the one playing (the operator's latest message wins; the old session is
torn down before the new one connects, since a speaker takes one backchannel at a time). Messages are cut at MAX_S.
Playback runs in a worker thread per speaker; nothing here blocks the event loop. Every play is logged (talkdown_log
and the server log): who, which message (and the words of a spoken text), which speaker, how it ended.

Messages: a library under data_dir/talkdown/ (uploaded mp3/wav/m4a/ogg of at most 2 MB, or a text the server's
text-to-speech engine speaks), or a one-off upload or typed text. ffmpeg converts everything to 8 kHz mono G.711.
Text-to-speech is tts.py: a Piper neural voice (the Site's voice setting, or a library message's own voice / speed),
falling back to another Piper voice, Windows SAPI, then espeak-ng / espeak; the play answer says who will speak, the
finished play's result and the log who did. Without any engine, the routes say so and the UI hides the Say field.

Live talk (Hold to Talk, speakers_api.py's WebSocket): the browser's microphone as 8 kHz 16-bit PCM (LiveFeed), on the
same kind of session: the wake tone, then the frames in the speaker's law as they arrive, one packet per 20 ms, silence
while none is buffered (a short gap in the network), a backlog trimmed at the operator's pauses. It ends when the
operator lets go (what is buffered still plays), after LIVE_IDLE_S without audio, or at LIVE_MAX_S. It is a playback
like any other: it replaces what plays on the speaker, a message replaces it, Stop ends it, and it is logged as "Live talk".

The siren (siren.py; deterrence's "siren" on a camera whose own siren can't sound, through a speaker linked to it:
siren()): exactly the asked seconds of the built-in wail/yelp in the speaker's law, no wake tone (the siren wakes the
amplifier itself), logged as "Siren". Who wins on one speaker:
  a person holding to talk     wins over the siren: a siren asked for while a live talk is on is refused (SpeakerBusy:
                               409, a failed deterrence_log row) and Hold to Talk started during a siren replaces it
  a siren                      replaces a recorded or spoken message that is playing (or connecting) and drops one that
                               waits; a newer siren replaces the older one; Stop (or the siren switched off) ends it
  an operator's message        replaces a siren like anything else (the operator's latest choice wins, as before)
  an automatic rule's message  (play(..., after_siren=True)) never cuts a siren: it waits and plays when the siren has
                               played to its end, so "siren, then the talk-down message" is one rule. One message waits
                               per speaker (a newer one takes its place); it is dropped, with a talkdown_log row, when
                               the siren is stopped, replaced or fails.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import random
import re
import select
import shutil
import socket
import subprocess
import threading
import sys
import time
import uuid
from array import array
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from . import siren as siren_sound
from . import tts
from .config import settings
from .db import db
from .rtsp_client import Rtsp
from .tts import _tts, tts_engine  # noqa: F401 - talkdown.tts_engine / talkdown._tts: the routes' and tests' names

log = logging.getLogger("nvr.talkdown")

BACKCHANNEL = "www.onvif.org/ver20/backchannel"
REQUIRE = {"Require": BACKCHANNEL}
LAWS = {"PCMU": ("mulaw", 0, 0xFF), "PCMA": ("alaw", 8, 0xD5)}   # codec -> (ffmpeg format, static payload type, silence byte)
STATIC_PT = {0: ("PCMU", 8000), 8: ("PCMA", 8000)}               # payload types an SDP may list without an rtpmap
RATE = 8000
FRAME_BYTES = 160                  # 20 ms of 8 kHz G.711
FRAME_S = FRAME_BYTES / RATE
MAX_S = 120                        # longest message played
MAX_UPLOAD = 2 * 1024 * 1024
MAX_TEXT = 500
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".ogg")
KEEPALIVE_S = 20                   # GET_PARAMETER at least this often (sooner when the session timeout is shorter)
TAIL_S = 0.4                       # the speaker's jitter buffer still holds the last packets when the sending ends
CHECK_EVERY_S = 600                # background DESCRIBE of each speaker (online / offline on the hub)
# the wake-up lead-in (settings.talkdown_wake_ms before every message): a speaker whose amplifier sleeps after a quiet
# spell (the Tonmind CS20) wakes on signal, so the words would start late or lose their first syllable. A faint low
# tone, about -40 dBFS, faded in and out, is enough for its signal detection and hardly audible.
WAKE_HZ = 200
WAKE_PEAK = 330                    # of 32767
WAKE_FADE_S = 0.05
# live talk (LiveFeed, live()): the browser sends 8 kHz mono 16-bit little-endian PCM, any length per message
LIVE_NAME = "Live talk"            # its name in the status, the log and the hub's audit
LIVE_MAX_S = 60                    # longest live talk
LIVE_IDLE_S = 1.5                  # no audio from the browser this long: it is over (the page went, the network failed)
LIVE_PCM_FRAME = FRAME_BYTES * 2   # 20 ms of 16-bit PCM
LIVE_TARGET = 5                    # frames (100 ms): the jitter buffer kept when trimming a backlog
LIVE_MAX_BACKLOG = 100             # frames (2 s): beyond this the oldest go
LIVE_QUIET = 330                   # peak of a frame that may be skipped to trim a backlog (about -40 dBFS, as WAKE_PEAK)
LIVE_MAX_MESSAGE = 64 * 1024       # bytes of one WebSocket message
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DEFAULT_MESSAGES = (
    ("Leave now", "You are on private property and being recorded. Leave now."),
    ("Police called", "The police have been called. Leave the property immediately."),
    ("Monitored site", "This site is monitored by security. You are being recorded. Please leave the area."),
)


class TalkdownError(RuntimeError):
    """A playback or check that can't go ahead; the text is shown to the operator."""


class Unreachable(TalkdownError):
    """No RTSP connection to the speaker at all (offline, wrong address or port)."""


class SpeakerBusy(TalkdownError):
    """Someone is talking on the speaker (Hold to Talk): a siren doesn't cut a person off."""


SIREN_NAME = "Siren"               # the siren's name in the status, the log and the hub's audit


# ---------------------------------------------------------------- SDP and RTP

def backchannel(sdp: str) -> dict | None:
    """The backchannel track of a DESCRIBE answer: the audio section marked a=sendonly. {control, codecs: [{pt, name,
    rate}]} or None when the device offers none."""
    for sec in ("\n" + sdp.replace("\r\n", "\n")).split("\nm=")[1:]:
        lines = [x.strip() for x in sec.split("\n")]
        mline = lines[0].split()
        if not mline or mline[0] != "audio" or "a=sendonly" not in lines:
            continue
        names = {}
        for x in lines:
            m = re.match(r"a=rtpmap:(\d+)\s+([\w.\-]+)/(\d+)", x)
            if m:
                names[int(m[1])] = (m[2].upper(), int(m[3]))
        codecs = []
        for f in mline[3:]:
            if f.isdigit():
                name, rate = names.get(int(f)) or STATIC_PT.get(int(f), ("?", 0))
                codecs.append({"pt": int(f), "name": name, "rate": rate})
        control = next((x[len("a=control:"):] for x in lines if x.startswith("a=control:")), None)
        return {"control": control, "codecs": codecs}
    return None


def pick_codec(track: dict, prefer: str | None = "PCMU") -> tuple[str, int] | None:
    """(codec, payload type) to send: the preferred G.711 law when offered, else the other one; None = no G.711."""
    usable = [c for c in track["codecs"] if c["name"] in LAWS and c["rate"] == RATE]
    usable.sort(key=lambda c: c["name"] != (prefer or "PCMU"))
    return (usable[0]["name"], usable[0]["pt"]) if usable else None


def track_url(base: str, control: str | None) -> str:
    if not control or control == "*":
        return base
    return control if control.startswith("rtsp://") else base.rstrip("/") + "/" + control


def rtp_packet(payload: bytes, pt: int, seq: int, ts: int, ssrc: int, marker: bool = False) -> bytes:
    """RTP version 2, no padding / extension / CSRC (RFC 3550)."""
    return (bytes([0x80, (0x80 if marker else 0) | pt]) + (seq & 0xFFFF).to_bytes(2, "big")
            + (ts & 0xFFFFFFFF).to_bytes(4, "big") + (ssrc & 0xFFFFFFFF).to_bytes(4, "big") + payload)


def packets(audio: bytes, pt: int, ssrc: int, seq0: int, ts0: int, silence: int = 0xFF) -> list[bytes]:
    """The audio as RTP packets of FRAME_BYTES (the last padded with silence): sequence +1 and timestamp +160 per
    packet (both wrap), the marker on the first (start of a talkspurt)."""
    out = []
    for i in range(0, len(audio), FRAME_BYTES):
        frame = audio[i:i + FRAME_BYTES]
        frame += bytes([silence]) * (FRAME_BYTES - len(frame))
        n = i // FRAME_BYTES
        out.append(rtp_packet(frame, pt, seq0 + n, ts0 + n * FRAME_BYTES, ssrc, marker=n == 0))
    return out


def lin2ulaw(s: int) -> int:
    """One 16-bit sample as G.711 mu-law, exactly as the Sun reference (and audioop, gone in Python 3.13): the 14-bit
    value, clip 8159, bias 0x21."""
    s >>= 2
    if s < 0:
        mask, s = 0x7F, -s
    else:
        mask = 0xFF
    s = min(s, 8159) + 0x21
    seg = max(0, s.bit_length() - 6)
    if seg >= 8:
        return 0x7F ^ mask
    return ((seg << 4) | ((s >> (seg + 1)) & 0x0F)) ^ mask


def lin2alaw(s: int) -> int:
    """One 16-bit sample as G.711 A-law (the ITU-T reference on the 13-bit value, even bits inverted)."""
    s >>= 3
    if s >= 0:
        mask = 0xD5
    else:
        mask, s = 0x55, -s - 1
    seg = max(0, s.bit_length() - 5)
    if seg >= 8:
        return 0x7F ^ mask
    return ((seg << 4) | ((s >> (1 if seg < 2 else seg)) & 0x0F)) ^ mask


def wake_tone(codec: str, ms: int) -> bytes:
    """`ms` of the faint wake-up tone in the speaker's G.711 law (b"" for 0)."""
    n = max(0, int(RATE * ms / 1000))
    enc = lin2ulaw if codec == "PCMU" else lin2alaw
    fade = max(1, int(RATE * WAKE_FADE_S))
    return bytes(enc(round(WAKE_PEAK * min(1.0, i / fade, (n - 1 - i) / fade) * math.sin(2 * math.pi * WAKE_HZ * i / RATE)))
                 for i in range(n))


_LAW_TABLES: dict[str, bytes] = {}


def _pcm(pcm: bytes, code: str) -> array:
    a = array(code)
    a.frombytes(pcm[:len(pcm) & ~1])
    if sys.byteorder == "big":
        a.byteswap()   # the wire is little-endian
    return a


def pcm_to_g711(pcm: bytes, codec: str) -> bytes:
    """16-bit little-endian PCM as G.711 in the codec's law, through a table of every sample (built once per law)."""
    table = _LAW_TABLES.get(codec)
    if table is None:
        enc = lin2ulaw if codec == "PCMU" else lin2alaw
        table = _LAW_TABLES[codec] = bytes(enc(u - 65536 if u >= 32768 else u) for u in range(65536))
    return bytes(map(table.__getitem__, _pcm(pcm, "H")))


def quiet(pcm: bytes) -> bool:
    """A 16-bit PCM frame with no sample louder than LIVE_QUIET (the operator's pause, the mic's noise floor)."""
    a = _pcm(pcm, "h")
    return not a or max(-min(a), max(a)) < LIVE_QUIET


def interleaved(channel: int, pkt: bytes) -> bytes:
    """RTSP interleaved framing (RFC 2326 10.12): $, channel, 16-bit length."""
    return b"$" + bytes([channel]) + len(pkt).to_bytes(2, "big") + pkt


# ---------------------------------------------------------------- the RTSP session

def speaker_url(s: dict) -> str:
    path = s.get("path") or "/main"
    return f"rtsp://{s['host']}:{int(s.get('rtsp_port') or 554)}{path if path.startswith('/') else '/' + path}"


class Session:
    """One backchannel session: DESCRIBE, SETUP the sendonly track, PLAY, send, TEARDOWN (every request with Require)."""

    def __init__(self, speaker: dict, timeout: float = 10):
        self.url = speaker_url(speaker)
        try:
            self.rtsp = Rtsp(self.url, speaker.get("username") or "", speaker.get("password") or "", timeout)
        except OSError as e:
            raise Unreachable(f"can't reach the speaker at {speaker['host']}:{speaker.get('rtsp_port') or 554} ({e})") from e
        self.rtsp.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # TCP_NODELAY: 20 ms packets go out as they are written
        self.base = self.url
        self.channel = 0
        self.timeout_s = 60

    def request(self, method: str, uri: str, headers: dict | None = None) -> tuple[int, dict, bytes]:
        return self.rtsp.request(method, uri, {**(headers or {}), **REQUIRE})

    def describe(self) -> dict:
        status, h, body = self.request("DESCRIBE", self.url, {"Accept": "application/sdp"})
        if status == 401:
            raise TalkdownError("the speaker refused the username or password")
        if status != 200:
            raise TalkdownError(f"the speaker answered DESCRIBE with {status}")
        self.base = h.get("content-base", self.url)
        track = backchannel(body.decode(errors="replace"))
        if not track:
            raise TalkdownError("the speaker offers no audio backchannel (no sendonly audio track)")
        return track

    def setup(self, track: dict) -> int:
        status, h, _ = self.request("SETUP", track_url(self.base, track["control"]), {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"})
        if status != 200 or "session" not in h:
            raise TalkdownError(f"the speaker answered SETUP with {status}")
        self.rtsp.session = h["session"].split(";")[0].strip()
        m = re.search(r"timeout=(\d+)", h["session"])
        if m and int(m[1]) > 0:
            self.timeout_s = int(m[1])
        m = re.search(r"interleaved=(\d+)", h.get("transport", ""))
        self.channel = int(m[1]) if m else 0
        return self.channel

    def play(self) -> None:
        status, _, _ = self.request("PLAY", self.base, {"Range": "npt=0.000-"})
        if status != 200:
            raise TalkdownError(f"the speaker answered PLAY with {status}")

    def keepalive(self) -> None:
        self.request("GET_PARAMETER", self.base)   # any answer will do (some devices say 501 and still count it)

    def send(self, data: bytes) -> None:
        self.rtsp.sock.sendall(data)

    def drain(self) -> None:
        """Read what the speaker sends meanwhile (RTCP receiver reports) without blocking, and drop the complete
        interleaved packets; anything else stays for the next request's reader."""
        r = self.rtsp
        while select.select([r.sock], [], [], 0)[0]:
            chunk = r.sock.recv(65536)
            if not chunk:
                raise TalkdownError("the speaker closed the connection")
            r.buf += chunk
        while len(r.buf) >= 4 and r.buf[:1] == b"$":
            n = int.from_bytes(r.buf[2:4], "big")
            if len(r.buf) < 4 + n:
                break
            r.buf = r.buf[4 + n:]

    def teardown(self) -> None:
        with contextlib.suppress(OSError, ConnectionError, ValueError, IndexError):
            self.rtsp.sock.settimeout(3)
            self.request("TEARDOWN", self.base)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.rtsp.sock.close()


def stream(sess: Session, audio: bytes, pt: int, stop: threading.Event, silence: int = 0xFF, clock=time.monotonic, wait=None,
           ssrc: int | None = None, seq0: int | None = None, ts0: int | None = None) -> int:
    """Send `audio` (G.711 bytes) as RTP over the session's interleaved channel, one packet per 20 ms by `clock`
    (packets are scheduled from the start, so a slow send catches up instead of stretching the message). Stops
    early when `stop` is set. Returns how many packets went out. `clock`/`wait` are the tests' fake time."""
    wait = wait or stop.wait
    ssrc = random.getrandbits(32) if ssrc is None else ssrc
    pkts = packets(audio, pt, ssrc, random.getrandbits(16) if seq0 is None else seq0, random.getrandbits(32) if ts0 is None else ts0, silence)
    keep_every = max(5.0, min(KEEPALIVE_S, sess.timeout_s / 2))
    t0 = last_keep = clock()
    sent = 0
    for i, p in enumerate(pkts):
        if stop.is_set():
            break
        delay = t0 + i * FRAME_S - clock()
        if delay > 0 and wait(delay):
            break   # stopped while waiting
        sess.send(interleaved(sess.channel, p))
        sent += 1
        if clock() - last_keep >= keep_every:
            sess.keepalive()
            last_keep = clock()
        elif i % 25 == 0:
            sess.drain()
    return sent


class LiveFeed:
    """The browser's audio of one live talk, between the WebSocket (push / end, on the event loop) and the playback
    thread (take): 16-bit PCM in, 20 ms frames out. A backlog (the time the speaker took to connect, a burst after a
    network stall) is trimmed by skipping quiet frames down to LIVE_TARGET, and never grows past LIVE_MAX_BACKLOG."""

    def __init__(self, clock=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._frames: deque[bytes] = deque()
        self._rest = b""
        self.clock = clock
        self.last_rx = clock()
        self.ended = False
        self.received = 0              # frames
        self.dropped = 0               # frames skipped or dropped

    def push(self, pcm: bytes) -> None:
        with self._lock:
            if self.ended:
                return
            data = self._rest + pcm
            whole = len(data) - len(data) % LIVE_PCM_FRAME
            for i in range(0, whole, LIVE_PCM_FRAME):
                self._frames.append(data[i:i + LIVE_PCM_FRAME])
                self.received += 1
            self._rest = data[whole:]
            while len(self._frames) > LIVE_MAX_BACKLOG:
                self._frames.popleft()
                self.dropped += 1
            self.last_rx = self.clock()

    def end(self) -> None:
        """The operator let go: what is buffered still plays."""
        with self._lock:
            self.ended = True

    def touch(self) -> None:
        """Count idleness from now (the speaker is ready)."""
        with self._lock:
            self.last_rx = max(self.last_rx, self.clock())

    def take(self) -> bytes | None:
        """The next 20 ms frame to play, None when nothing is buffered (bridge with silence)."""
        with self._lock:
            while len(self._frames) > LIVE_TARGET and quiet(self._frames[0]):
                self._frames.popleft()
                self.dropped += 1
            return self._frames.popleft() if self._frames else None

    def backlog(self) -> int:
        return len(self._frames)

    def done(self) -> bool:
        """Released and played out."""
        with self._lock:
            return self.ended and not self._frames

    def idle(self, idle_s: float = LIVE_IDLE_S) -> bool:
        """Nothing buffered and nothing arrived for idle_s."""
        with self._lock:
            return not self._frames and self.clock() - self.last_rx >= idle_s


def live(sess: Session, feed: LiveFeed, codec: str, pt: int, stop: threading.Event, lead: bytes = b"", clock=time.monotonic, wait=None,
         ssrc: int | None = None, seq0: int | None = None, ts0: int | None = None, max_s: float = LIVE_MAX_S, idle_s: float = LIVE_IDLE_S) -> str:
    """Send a live talk as RTP over the session, one packet per 20 ms by `clock` (scheduled from the start, as stream()):
    `lead` first (the wake tone, G.711), then the feed's frames in the codec's law, silence while none is buffered, with
    one sequence and timestamp run throughout. Returns why it ended: "released" (let go and played out), "idle" (no audio
    for idle_s), "cap" (max_s) or "stopped" (`stop` set: Stop, or replaced). `clock`/`wait` are the tests' fake time."""
    wait = wait or stop.wait
    ssrc = random.getrandbits(32) if ssrc is None else ssrc
    seq = random.getrandbits(16) if seq0 is None else seq0
    ts = random.getrandbits(32) if ts0 is None else ts0
    silence = bytes([LAWS[codec][2]]) * FRAME_BYTES
    leads = [(lead[i:i + FRAME_BYTES] + silence)[:FRAME_BYTES] for i in range(0, len(lead), FRAME_BYTES)]
    keep_every = max(5.0, min(KEEPALIVE_S, sess.timeout_s / 2))
    t0 = last_keep = clock()
    feed.touch()
    i = 0
    while True:
        if stop.is_set():
            return "stopped"
        if i * FRAME_S >= max_s - 1e-9:   # the next packet would start past the cap
            return "cap"
        if i >= len(leads):
            if feed.done():
                return "released"
            if feed.idle(idle_s):
                return "idle"
        delay = t0 + i * FRAME_S - clock()
        if delay > 0 and wait(delay):
            return "stopped"
        if i < len(leads):
            payload = leads[i]
        else:
            pcm = feed.take()
            payload = pcm_to_g711(pcm, codec) if pcm else silence
        sess.send(interleaved(sess.channel, rtp_packet(payload, pt, seq + i, ts + i * FRAME_BYTES, ssrc, marker=i == 0)))
        i += 1
        if clock() - last_keep >= keep_every:
            sess.keepalive()
            last_keep = clock()
        elif i % 25 == 0:
            sess.drain()


def check(speaker: dict, timeout: float = 5) -> dict:
    """DESCRIBE with the backchannel Require header (blocking: call from a thread). {online: it answered RTSP,
    backchannel: it offers a sendonly audio track, codecs: what that track takes, codec: the G.711 law we'd send,
    ok: all of these, error}."""
    sess = None
    out = {"ok": False, "online": False, "backchannel": False, "codecs": [], "codec": None, "error": None}
    try:
        sess = Session(speaker, timeout)
        out["online"] = True
        track = sess.describe()
        codec = pick_codec(track, speaker.get("codec"))
        out.update(backchannel=True, codecs=[c["name"] for c in track["codecs"]], codec=codec[0] if codec else None, ok=codec is not None,
                   error=None if codec else "the backchannel offers no G.711 (PCMU / PCMA)")
        sess.teardown()
    except TalkdownError as e:
        out["error"] = str(e)
    except (OSError, ConnectionError, ValueError, IndexError) as e:
        out.update(online=False, error=f"no answer from the speaker ({e})")
    finally:
        if sess:
            sess.close()
    return out


# ---------------------------------------------------------------- audio: ffmpeg and text-to-speech

def ffmpeg_exe() -> str:
    return shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"


def to_g711(src: Path, codec: str = "PCMU") -> bytes:
    """Any audio file as 8 kHz mono G.711 raw bytes, at most MAX_S long."""
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(src), "-t", str(MAX_S), "-vn",
           "-ac", "1", "-ar", str(RATE), "-f", LAWS[codec][0], "pipe:1"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TalkdownError(f"ffmpeg could not run ({e})") from e
    if r.returncode != 0 or not r.stdout:
        err = (r.stderr.decode(errors="replace").strip().splitlines() or ["no audio in the file"])[-1]
        raise TalkdownError(f"could not read the audio ({err[:160]})")
    return r.stdout


def clean_text(text: str) -> str:
    return " ".join((text or "").split())[:MAX_TEXT]


def synthesize(text: str, out: Path, voice: str | None = None, speed=1.0, chime: bool = False) -> tts.Spoken:
    """Speak `text` into the WAV file `out` (tts.synthesize, its errors as TalkdownError): who spoke it."""
    try:
        return tts.synthesize(clean_text(text), out, voice, speed, chime)
    except tts.TTSError as e:
        raise TalkdownError(str(e)) from e


def voice_for(m: dict | None = None) -> dict:
    """{voice, speed, chime} for a text: the Site's voice setting, a library message's own voice / speed over it."""
    s = tts.site_setting()
    if m and m.get("voice"):
        s["voice"] = m["voice"]
    if m and m.get("speed"):
        s["speed"] = tts.clamp_speed(m["speed"])
    return s


# ---------------------------------------------------------------- the message library

def talkdown_dir() -> Path:
    d = settings.data_dir / "talkdown"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _seed_defaults() -> None:
    """The default texts, once per server (a deleted default stays deleted)."""
    if db.get_setting("talkdown_defaults_seeded"):
        return
    now = time.time()
    for name, text in DEFAULT_MESSAGES:
        db.execute("INSERT INTO talkdown_messages (name, text, created_at) VALUES (?,?,?)", [name, text, now])
    db.set_setting("talkdown_defaults_seeded", True)


def public_message(m: dict, engine: str | None) -> dict:
    kind = "file" if m.get("file") else "tts"
    return {"id": m["id"], "name": m["name"], "kind": kind, "text": m.get("text"), "duration_s": m.get("duration_s"),
            "created_at": m["created_at"], "available": kind == "file" or engine is not None,
            "voice": m.get("voice"), "speed": m.get("speed")}   # a text's own voice / speed; None = the Site's


def messages() -> list[dict]:
    _seed_defaults()
    return db.all("SELECT * FROM talkdown_messages ORDER BY id")


def message(message_id: int) -> dict | None:
    return db.one("SELECT * FROM talkdown_messages WHERE id=?", [message_id])


def check_upload(filename: str, data: bytes) -> str:
    """The file's extension when it is an audio type we take and not too big; TalkdownError otherwise."""
    ext = Path(filename or "").suffix.lower()
    if ext not in AUDIO_EXT:
        raise TalkdownError(f"send an {', '.join(e[1:] for e in AUDIO_EXT)} file")
    if not data:
        raise TalkdownError("the file is empty")
    if len(data) > MAX_UPLOAD:
        raise TalkdownError(f"the file is larger than {MAX_UPLOAD // (1024 * 1024)} MB")
    return ext


def add_file_message(name: str, filename: str, data: bytes) -> dict:
    """Store an uploaded audio file in the library after ffmpeg has read it (blocking: call from a thread)."""
    ext = check_upload(filename, data)
    path = talkdown_dir() / f"msg-{uuid.uuid4().hex[:12]}{ext}"
    path.write_bytes(data)
    try:
        duration = len(to_g711(path)) / RATE
    except TalkdownError:
        path.unlink(missing_ok=True)
        raise
    mid = db.execute_insert("INSERT INTO talkdown_messages (name, file, duration_s, created_at) VALUES (?,?,?,?)",
                            [name, path.name, round(duration, 1), time.time()])
    return message(mid)


def add_text_message(name: str, text: str, voice: str | None = None, speed: float | None = None) -> dict:
    text = clean_text(text)
    if not text:
        raise TalkdownError("type the text to speak")
    mid = db.execute_insert("INSERT INTO talkdown_messages (name, text, voice, speed, created_at) VALUES (?,?,?,?,?)",
                            [name, text, voice or None, tts.clamp_speed(speed) if speed else None, time.time()])
    return message(mid)


def delete_message(message_id: int) -> bool:
    m = message(message_id)
    if not m:
        return False
    db.execute("DELETE FROM talkdown_messages WHERE id=?", [message_id])
    if m.get("file"):
        (talkdown_dir() / m["file"]).unlink(missing_ok=True)
    return True


# ---------------------------------------------------------------- playback

@dataclass
class Message:
    """What to play: an audio file (`path`), a text for TTS (`text`) or ready G.711 in the speaker's law (`g711`)."""
    name: str
    path: Path | None = None
    text: str | None = None
    g711: bytes | None = None
    message_id: int | None = None
    cleanup: bool = False          # a one-off upload: deleted after playing
    cache: bool = False            # a library text: its speech is kept under talkdown/tts/
    voice: dict | None = None      # a text: {voice, speed, chime} (voice_for); None = the Site's at play time
    spoken: dict | None = None     # a text, once synthesized: who spoke it (tts.Spoken.public)
    siren_s: float | None = None   # the built-in siren (siren.py) for this many seconds, in the speaker's law


@dataclass
class Job:
    speaker_id: str
    speaker_name: str
    message: Message
    by: str
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    state: str = "connecting"      # connecting | playing | done
    started_at: float | None = None
    requested_at: float = field(default_factory=time.time)
    duration_s: float | None = None
    replaced_by: str | None = None
    result: dict | None = None
    log_id: int | None = None
    feed: LiveFeed | None = None    # a live talk: the browser's audio
    replacing: str | None = None   # the message it replaced
    reason: str | None = None      # how a live talk ended (live())


class Talkdown:
    """The playbacks of this server's speakers: at most one per speaker (a new one replaces it), each in its own thread."""

    def __init__(self) -> None:
        self._lock = threading.RLock()   # re-entered: a siren's thread starts the message that waited for it
        self._jobs: dict[str, Job] = {}
        self._queued: dict[str, tuple[dict, Message, str]] = {}   # speaker -> (speaker, message, by) waiting for its siren

    @staticmethod
    def _live(job: Job | None) -> bool:
        """Still playing (or connecting): not yet past its finally."""
        return bool(job and job.thread and job.thread.is_alive() and job.state != "done")

    def play(self, speaker: dict, msg: Message, by: str, after_siren: bool = False) -> dict:
        """Start the playback; the answer says who will speak a text (`tts`: tts.effective, without synthesizing: the
        finished play's `last.tts` and the log say who did). `after_siren` (an automatic rule): while the speaker sounds
        a siren the message waits for it instead (`queued`)."""
        sid = speaker["id"]
        will = None
        if msg.text is not None:
            msg.voice = msg.voice or voice_for()
            eff = tts.effective(msg.voice["voice"], probe=False)
            will = eff.public() if eff else None
        out = {"speaker_id": sid, "speaker": speaker["name"], "camera_ids": speaker.get("camera_ids") or [], "message": msg.name,
               "message_id": msg.message_id, "text": msg.text, "tts": will, "queued": False}
        with self._lock:
            cur = self._jobs.get(sid)
            if after_siren and self._live(cur) and cur.message.siren_s is not None:
                if sid in self._queued:
                    self._drop(sid, "a newer message took its place")
                self._queued[sid] = (dict(speaker), msg, by)
                log.info("talk-down: %r on %s by %s waits for the siren", msg.name, sid, by)
                return {**out, "replaced": None, "queued": True, "status": self.status(sid)}
            job = self._start(speaker, msg, by)
        return {**out, "replaced": job.replacing, "status": self.status(sid)}

    def siren(self, speaker: dict, seconds: float, by: str) -> Job:
        """Sound the built-in siren on the speaker for `seconds` (1 to MAX_S): it replaces a message or an older siren
        and drops a waiting message; SpeakerBusy while someone holds to talk there."""
        secs = float(min(MAX_S, max(1.0, float(seconds))))
        return self._start(speaker, Message(SIREN_NAME, siren_s=secs), by, refuse_live=True)

    def talk(self, speaker: dict, by: str, feed: LiveFeed | None = None) -> Job:
        """Start a live talk on the speaker (it replaces what plays there, as play() does): the caller pushes the
        browser's audio into job.feed and ends it when the operator lets go; the job's thread ends on its own after that."""
        return self._start(speaker, Message(LIVE_NAME), by, feed or LiveFeed())

    def _start(self, speaker: dict, msg: Message, by: str, feed: LiveFeed | None = None, refuse_live: bool = False) -> Job:
        sid = speaker["id"]
        with self._lock:
            old = self._jobs.get(sid)
            if not self._live(old):
                old = None
            if refuse_live and old is not None and old.feed is not None:
                raise SpeakerBusy(f"{old.by} is talking on {speaker['name']} (Hold to Talk)")
            if sid in self._queued:
                self._drop(sid, f"replaced by {msg.name!r}")   # it waited for a siren that is now cut short
            if old:
                old.replaced_by = msg.name
                old.stop.set()
            job = Job(sid, speaker["name"], msg, by, feed=feed, replacing=old.message.name if old else None)
            job.log_id = db.execute_insert("INSERT INTO talkdown_log (ts, speaker_id, message, text, by, result) VALUES (?,?,?,?,?,?)",
                                           [job.requested_at, sid, msg.name, msg.text, by, "playing"])
            log.info("talk-down: %r on %s (%s) by %s%s", msg.name, sid, speaker["name"], by, f", replacing {old.message.name!r}" if old else "")
            self._jobs[sid] = job
            job.thread = threading.Thread(target=self._run, args=(job, dict(speaker), old), daemon=True, name=f"talkdown-{sid}")
            job.thread.start()
        return job

    def stop(self, speaker_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(speaker_id)
            if speaker_id in self._queued:
                self._drop(speaker_id, "stopped")
        if job and job.thread and job.thread.is_alive():
            job.stop.set()
            return True
        return False

    def stop_job(self, speaker_id: str, job: Job) -> bool:
        """Stop this playback only if it is still the one on the speaker (deterrence switching its siren off must not
        end a message or a live talk that took over since)."""
        with self._lock:
            if self._jobs.get(speaker_id) is not job or not self._live(job):
                return False
            return self.stop(speaker_id)

    def current(self, speaker_id: str) -> Job | None:
        """The playback on the speaker now (None when idle)."""
        job = self._jobs.get(speaker_id)
        return job if self._live(job) else None

    def _drop(self, speaker_id: str, why: str) -> None:
        """A message that waited for a siren won't play: logged (talkdown_log 'dropped'). Called with the lock held."""
        sp, msg, by = self._queued.pop(speaker_id)
        log.info("talk-down: %r on %s by %s dropped (%s)", msg.name, speaker_id, by, why)
        with contextlib.suppress(Exception):
            db.execute("INSERT INTO talkdown_log (ts, speaker_id, message, text, by, result, error, duration_s) VALUES (?,?,?,?,?,?,?,?)",
                       [time.time(), speaker_id, msg.name, msg.text, by, "dropped", f"waited for the siren: {why}", 0.0])
        if msg.cleanup and msg.path:
            msg.path.unlink(missing_ok=True)

    def _after_siren(self, job: Job, result: str) -> None:
        """A siren's thread is done: start the message that waited for it when the siren played to its end."""
        with self._lock:
            if self._jobs.get(job.speaker_id) is not job or job.speaker_id not in self._queued:
                return   # something newer is on the speaker (it dropped the waiting message), or nothing waits
            if result != "played":
                self._drop(job.speaker_id, f"the siren {result}")
                return
            sp, msg, by = self._queued.pop(job.speaker_id)
            self._start(sp, msg, by)

    def stop_all(self) -> None:
        for sid in list(self._jobs):
            self.stop(sid)

    def busy(self, speaker_id: str) -> bool:
        job = self._jobs.get(speaker_id)
        return bool(job and job.thread and job.thread.is_alive())

    def wait(self, speaker_id: str, timeout: float = 10) -> None:
        """Tests: until the speaker's playback has finished."""
        job = self._jobs.get(speaker_id)
        if job and job.thread:
            job.thread.join(timeout)

    def status(self, speaker_id: str) -> dict:
        job = self._jobs.get(speaker_id)
        queued = self._queued.get(speaker_id)
        if job and job.thread and job.thread.is_alive():
            now = time.time()
            return {"state": job.state, "message": job.message.name, "by": job.by, "requested_at": job.requested_at,
                    "started_at": job.started_at, "elapsed_s": round(now - job.started_at, 1) if job.started_at else None,
                    "duration_s": job.duration_s, "live": job.feed is not None, "siren": job.message.siren_s is not None,
                    "queued": queued[1].name if queued else None, "last": None}
        return {"state": "idle", "message": None, "by": None, "requested_at": None, "started_at": None, "elapsed_s": None,
                "duration_s": None, "live": False, "siren": False, "queued": None, "last": job.result if job else None}

    def _audio(self, msg: Message, codec: str) -> bytes:
        if msg.g711 is not None:
            return msg.g711
        if msg.siren_s is not None:
            return siren_sound.g711(msg.siren_s, codec)
        if msg.text is not None:
            v = msg.voice or voice_for()
            wav = talkdown_dir() / "oneoff" / f"say-{uuid.uuid4().hex[:12]}.wav"
            meta = None
            if msg.cache:
                # a library text keeps its speech per voice / speed / chime, only when the voice asked for spoke it (a
                # fallback is spoken anew each time, so a voice installed later takes over at once)
                key = hashlib.sha1(json.dumps([clean_text(msg.text), v["voice"], v["speed"], v["chime"]]).encode()).hexdigest()[:16]
                wav, meta = talkdown_dir() / "tts" / f"{key}.wav", talkdown_dir() / "tts" / f"{key}.json"
                with contextlib.suppress(OSError, ValueError):
                    if wav.is_file():
                        msg.spoken = json.loads(meta.read_text())
                        return to_g711(wav, codec)
            spoken = synthesize(msg.text, wav, v["voice"], v["speed"], v["chime"])
            msg.spoken = spoken.public()
            try:
                audio = to_g711(wav, codec)
            except TalkdownError:
                wav.unlink(missing_ok=True)
                raise
            if meta is not None and not spoken.fallback:
                meta.write_text(json.dumps(msg.spoken))
            else:
                wav.unlink(missing_ok=True)
            return audio
        if msg.path is None:
            raise TalkdownError("nothing to play")
        if not msg.path.is_file():
            raise TalkdownError("the message's audio file is missing")
        return to_g711(msg.path, codec)

    def _run(self, job: Job, speaker: dict, old: Job | None) -> None:
        if old and old.thread:
            old.thread.join(10)   # its TEARDOWN first: the speaker takes one backchannel session at a time
        result, error, sess = "played", None, None
        try:
            if job.stop.is_set():
                raise _Stopped()
            sess = Session(speaker)
            track = sess.describe()
            codec = pick_codec(track, speaker.get("codec"))
            if not codec:
                raise TalkdownError("the speaker's backchannel offers no G.711 (PCMU / PCMA)")
            name, pt = codec
            # the wake-up tone first, so a speaker whose amplifier slept is awake when the words come (not before a
            # siren: it wakes the amplifier itself, and it lasts exactly the seconds asked)
            lead = b"" if job.message.siren_s is not None else wake_tone(name, settings.talkdown_wake_ms)
            if job.feed is None:
                audio = (lead + self._audio(job.message, name))[:MAX_S * RATE]
                job.duration_s = round(len(audio) / RATE, 1)
            if job.stop.is_set():
                raise _Stopped()
            sess.setup(track)
            sess.play()
            job.state, job.started_at = "playing", time.time()
            if job.feed is not None:
                job.reason = live(sess, job.feed, name, pt, job.stop, lead=lead)
            else:
                stream(sess, audio, pt, job.stop, silence=LAWS[name][2])
            if job.stop.is_set():
                result = "replaced" if job.replaced_by else "stopped"
            else:
                job.stop.wait(TAIL_S)
            sess.teardown()
            db.set_speaker_check(speaker["id"], {"ok": True, "online": True, "backchannel": True, "codecs": [c["name"] for c in track["codecs"]],
                                                 "codec": name, "error": None})
        except _Stopped:
            result = "replaced" if job.replaced_by else "stopped"
        except TalkdownError as e:
            result, error = "failed", str(e)
        except (OSError, ConnectionError, ValueError, IndexError) as e:
            result, error = "failed", f"the speaker connection failed ({e})"
        except Exception as e:  # noqa: BLE001 - the operator must see that it failed, whatever it was
            log.exception("talk-down on %s failed", speaker["id"])
            result, error = "failed", str(e)[:200]
        finally:
            if sess:
                sess.close()
            if job.message.cleanup and job.message.path:
                job.message.path.unlink(missing_ok=True)
            if job.feed is not None:
                job.feed.end()   # nothing takes its audio any more
            played = round(time.time() - job.started_at, 1) if job.started_at else 0.0
            spoken = job.message.spoken
            job.result = {"message": job.message.name, "by": job.by, "result": result, "error": error, "at": time.time(),
                          "played_s": played, "tts": spoken}
            if job.feed is not None:
                job.result["reason"] = job.reason   # a live talk: released, idle or cap (None when it never started)
            job.state = "done"
            said = f"{spoken['engine']}:{spoken['voice']}" if spoken and spoken.get("voice") else (spoken or {}).get("engine")
            with contextlib.suppress(Exception):
                db.execute("UPDATE talkdown_log SET result=?, error=?, duration_s=?, tts=? WHERE id=?", [result, error, played, said, job.log_id])
            (log.warning if error else log.info)("talk-down: %r on %s %s after %.1f s%s%s%s", job.message.name, speaker["id"], result, played,
                                                 f" ({job.reason})" if job.reason else "",
                                                 f", spoken by {said}" + (f" (fallback: {spoken['reason']})" if spoken.get("fallback") else "") if spoken else "",
                                                 f": {error}" if error else "")
            if job.message.siren_s is not None:
                self._after_siren(job, result)


class _Stopped(Exception):
    pass


manager = Talkdown()


def summary_speakers() -> list[dict]:
    """What the hub heartbeat carries (summary.py): no address or credentials."""
    out = []
    for s in db.speakers(enabled_only=True):
        st = manager.status(s["id"])
        chk = s.get("check") or {}
        out.append({"id": s["id"], "name": s["name"], "camera_ids": s["camera_ids"],
                    "online": chk.get("online") if s.get("checked_at") else None, "ok": chk.get("ok"),
                    "checked_at": s.get("checked_at"), "playing": st["message"] if st["state"] != "idle" else None})
    return out


async def check_loop() -> None:
    """Every CHECK_EVERY_S: DESCRIBE each enabled speaker that isn't playing, so the hub can say whether it is online.
    First the text-to-speech probes (Piper import, SAPI's PowerShell), so the heartbeat can report the engines."""
    with contextlib.suppress(Exception):
        await asyncio.to_thread(tts.engines)
    await asyncio.sleep(30)
    while True:
        for s in db.speakers(enabled_only=True):
            if manager.busy(s["id"]):
                continue
            try:
                db.set_speaker_check(s["id"], await asyncio.to_thread(check, s))
            except Exception:  # noqa: BLE001 - one speaker must not end the loop
                log.exception("speaker check of %s failed", s["id"])
        await asyncio.sleep(CHECK_EVERY_S)
