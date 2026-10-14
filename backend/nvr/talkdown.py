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
Text-to-speech: Windows SAPI (System.Speech through PowerShell); on Linux piper (settings.tts_piper_model) or
espeak-ng / espeak. Without one, the routes say so and the UI hides the Say field.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import random
import re
import select
import shutil
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .config import settings
from .db import db
from .rtsp_client import Rtsp

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


_tts: dict = {}


def tts_engine(refresh: bool = False) -> str | None:
    """"sapi", "piper", "espeak-ng", "espeak" or None; found once (a PowerShell start on Windows: call from a thread)."""
    if refresh or "engine" not in _tts:
        _tts["engine"] = _find_tts()
    return _tts["engine"]


def _powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


def _find_tts() -> str | None:
    if os.name == "nt":
        ps = _powershell()
        if not ps:
            return None
        try:
            r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command",
                                "Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices().Count"],
                               capture_output=True, timeout=30, creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return "sapi" if r.returncode == 0 and r.stdout.strip() not in (b"", b"0") else None
    if settings.tts_piper_model and shutil.which("piper") and Path(settings.tts_piper_model).is_file():
        return "piper"
    return next((exe for exe in ("espeak-ng", "espeak") if shutil.which(exe)), None)


def clean_text(text: str) -> str:
    return " ".join((text or "").split())[:MAX_TEXT]


def synthesize(text: str, out: Path) -> Path:
    """Speak `text` into the WAV file `out`. The text goes through an environment variable or stdin, never a command line."""
    engine = tts_engine()
    if not engine:
        raise TalkdownError("this server has no text-to-speech engine")
    text = clean_text(text)
    if not text:
        raise TalkdownError("nothing to say")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + f".{uuid.uuid4().hex[:6]}.tmp.wav")
    if engine == "sapi":
        script = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  "$s.SetOutputToWaveFile($env:NVR_TTS_OUT); $s.Speak($env:NVR_TTS_TEXT); $s.Dispose()")
        cmd, stdin, env = [_powershell(), "-NoProfile", "-NonInteractive", "-Command", script], None, {**os.environ, "NVR_TTS_TEXT": text, "NVR_TTS_OUT": str(tmp)}
    elif engine == "piper":
        cmd, stdin, env = ["piper", "--model", settings.tts_piper_model, "--output_file", str(tmp)], text.encode(), None
    else:
        cmd, stdin, env = [engine, "--stdin", "-w", str(tmp)], text.encode(), None
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True, timeout=60, env=env, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        tmp.unlink(missing_ok=True)
        raise TalkdownError(f"text-to-speech failed ({e})") from e
    if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < 100:
        tmp.unlink(missing_ok=True)
        raise TalkdownError("text-to-speech failed: " + (r.stderr.decode(errors="replace").strip()[-160:] or "no audio"))
    tmp.replace(out)
    return out


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
            "created_at": m["created_at"], "available": kind == "file" or engine is not None}


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


def add_text_message(name: str, text: str) -> dict:
    text = clean_text(text)
    if not text:
        raise TalkdownError("type the text to speak")
    mid = db.execute_insert("INSERT INTO talkdown_messages (name, text, created_at) VALUES (?,?,?)", [name, text, time.time()])
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


class Talkdown:
    """The playbacks of this server's speakers: at most one per speaker (a new one replaces it), each in its own thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}

    def play(self, speaker: dict, msg: Message, by: str) -> dict:
        sid = speaker["id"]
        with self._lock:
            old = self._jobs.get(sid)
            if old and old.thread and old.thread.is_alive():
                old.replaced_by = msg.name
                old.stop.set()
            else:
                old = None
            job = Job(sid, speaker["name"], msg, by)
            job.log_id = db.execute_insert("INSERT INTO talkdown_log (ts, speaker_id, message, text, by, result) VALUES (?,?,?,?,?,?)",
                                           [job.requested_at, sid, msg.name, msg.text, by, "playing"])
            log.info("talk-down: %r on %s (%s) by %s%s", msg.name, sid, speaker["name"], by, f", replacing {old.message.name!r}" if old else "")
            self._jobs[sid] = job
            job.thread = threading.Thread(target=self._run, args=(job, dict(speaker), old), daemon=True, name=f"talkdown-{sid}")
            job.thread.start()
        return {"speaker_id": sid, "speaker": speaker["name"], "camera_ids": speaker.get("camera_ids") or [], "message": msg.name,
                "message_id": msg.message_id, "text": msg.text, "replaced": old.message.name if old else None, "status": self.status(sid)}

    def stop(self, speaker_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(speaker_id)
        if job and job.thread and job.thread.is_alive():
            job.stop.set()
            return True
        return False

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
        if job and job.thread and job.thread.is_alive():
            now = time.time()
            return {"state": job.state, "message": job.message.name, "by": job.by, "requested_at": job.requested_at,
                    "started_at": job.started_at, "elapsed_s": round(now - job.started_at, 1) if job.started_at else None,
                    "duration_s": job.duration_s, "last": None}
        return {"state": "idle", "message": None, "by": None, "requested_at": None, "started_at": None, "elapsed_s": None,
                "duration_s": None, "last": job.result if job else None}

    def _audio(self, msg: Message, codec: str) -> bytes:
        if msg.g711 is not None:
            return msg.g711
        if msg.text is not None:
            if msg.cache:
                key = hashlib.sha1(f"{tts_engine()}|{clean_text(msg.text)}".encode()).hexdigest()[:16]
                wav = talkdown_dir() / "tts" / f"{key}.wav"
                if not wav.is_file():
                    synthesize(msg.text, wav)
                return to_g711(wav, codec)
            wav = talkdown_dir() / "oneoff" / f"say-{uuid.uuid4().hex[:12]}.wav"
            try:
                return to_g711(synthesize(msg.text, wav), codec)
            finally:
                wav.unlink(missing_ok=True)
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
            audio = self._audio(job.message, name)[:MAX_S * RATE]
            job.duration_s = round(len(audio) / RATE, 1)
            if job.stop.is_set():
                raise _Stopped()
            sess.setup(track)
            sess.play()
            job.state, job.started_at = "playing", time.time()
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
            played = round(time.time() - job.started_at, 1) if job.started_at else 0.0
            job.result = {"message": job.message.name, "by": job.by, "result": result, "error": error, "at": time.time(),
                          "played_s": played}
            job.state = "done"
            with contextlib.suppress(Exception):
                db.execute("UPDATE talkdown_log SET result=?, error=?, duration_s=? WHERE id=?", [result, error, played, job.log_id])
            (log.warning if error else log.info)("talk-down: %r on %s %s after %.1f s%s", job.message.name, speaker["id"], result, played,
                                                 f": {error}" if error else "")


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
    """Every CHECK_EVERY_S: DESCRIBE each enabled speaker that isn't playing, so the hub can say whether it is online."""
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
