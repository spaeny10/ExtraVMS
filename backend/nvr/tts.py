"""Text-to-speech for talk-down (talkdown.py): Piper neural voices first, then Windows SAPI, then espeak-ng / espeak.

Piper voices are <id>.onnx + <id>.onnx.json in the voices folder (settings.tts_voices_dir, default data_dir/voices;
tools/install_piper_voices.py fetches the CATALOG ones from rhasspy/piper-voices). They run in-process through the
`piper-tts` package (it brings onnxruntime; Windows and Linux) when it is installed, else through an external `piper`
binary (settings.tts_piper_bin or `piper` on PATH). NVR_TTS_PIPER_MODEL, an .onnx anywhere, counts as one more voice.
Without the package the server starts as before: the import is tried once, on first use.

A synthesis takes a voice id, a speed (0.7-1.3: Piper's length_scale 1.4-0.8, SAPI's Rate, espeak's words per minute),
an optional attention chime before the words (two tones generated here, no file) and a pause between sentences (Piper).
When the voice asked for is missing or fails: another installed Piper voice (same language and gender first), then
Windows SAPI, then espeak. The answer (Spoken) says which engine and voice actually spoke, for the play log and the hub.

The Site's voice is the settings key `talkdown_voice` = {voice, speed, chime}; voice None = automatic (the first
installed voice in CATALOG order, else SAPI / espeak). A hub-enrolled server takes it from the hub (PUT
/api/talkdown/voice through the tunnel, per Site); a server without a hub sets it on its own Settings › Speakers.
The text goes through stdin, an environment variable or a Python call, never a command line.
"""
from __future__ import annotations

import array
import collections
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import settings

log = logging.getLogger("nvr.talkdown")

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SPEED_MIN, SPEED_MAX = 0.7, 1.3
LENGTH_SLOW, LENGTH_FAST = 1.4, 0.8      # Piper length_scale at SPEED_MIN / SPEED_MAX (1.0 at speed 1.0)
SENTENCE_PAUSE_S = 0.3                    # silence between sentences (Piper)
VOICE_RX = r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,79}$"
SETTING = "talkdown_voice"
DEFAULT = {"voice": None, "speed": 1.0, "chime": False}
PREVIEW_TEXT = "Attention. This site is monitored by security and you are being recorded."
PREVIEW_MAX = 300
PREVIEW_PER_MIN = 12                      # previews a minute per user
LOADED_MAX = 2                            # Piper voices kept loaded in memory (~60-120 MB each)

# Good English voices of the official Piper repository (https://huggingface.co/rhasspy/piper-voices, voices.json):
# each is <lang>/<lang_REGION>/<name>/<quality>/<id>.onnx (+ .onnx.json, MODEL_CARD). Their datasets carry different
# licenses: read each voice's MODEL_CARD before a commercial deployment. Also in frontend/src/talkdownVoice.ts (the
# hub's dropdown); test_tts checks that the two agree. The order is the automatic choice's preference.
HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
CATALOG = [
    {"id": "en_US-lessac-medium", "label": "US English · female · clear", "gender": "female"},
    {"id": "en_US-amy-medium", "label": "US English · female · warm", "gender": "female"},
    {"id": "en_US-kristin-medium", "label": "US English · female · calm", "gender": "female"},
    {"id": "en_US-hfc_female-medium", "label": "US English · female · bright", "gender": "female"},
    {"id": "en_US-ryan-high", "label": "US English · male · deep", "gender": "male"},
    {"id": "en_US-joe-medium", "label": "US English · male · firm", "gender": "male"},
    {"id": "en_US-hfc_male-medium", "label": "US English · male · steady", "gender": "male"},
    {"id": "en_GB-alan-medium", "label": "British English · male · measured", "gender": "male"},
    {"id": "en_GB-jenny_dioco-medium", "label": "British English · female · crisp", "gender": "female"},
]
_CATALOG = {v["id"]: v for v in CATALOG}


class TTSError(RuntimeError):
    """Nothing could speak the text; the message is shown to the operator."""


def parts(voice_id: str) -> tuple[str, str, str]:
    """en_US-hfc_female-medium -> ("en_US", "hfc_female", "medium")."""
    region, name, quality = voice_id.split("-", 2)
    return region, name, quality


def repo_path(voice_id: str) -> str:
    """The voice's .onnx in the repository: en/en_US/amy/medium/en_US-amy-medium.onnx."""
    region, name, quality = parts(voice_id)
    return f"{region.split('_')[0]}/{region}/{name}/{quality}/{voice_id}.onnx"


def voice_url(voice_id: str) -> str:
    return HF_BASE + repo_path(voice_id)


def catalog() -> list[dict]:
    """The catalog with each voice's language, quality and download URLs."""
    out = []
    for v in CATALOG:
        region, _, quality = parts(v["id"])
        out.append({**v, "language": region, "quality": quality, "url": voice_url(v["id"]), "config_url": voice_url(v["id"]) + ".json"})
    return out


# ---------------------------------------------------------------- installed voices

def voices_dir() -> Path:
    return Path(settings.tts_voices_dir) if settings.tts_voices_dir else settings.data_dir / "voices"


_scan: dict = {"key": None, "voices": []}


def _candidates() -> list[Path]:
    d = voices_dir()
    out = sorted(d.glob("*.onnx")) if d.is_dir() else []
    if settings.tts_piper_model:
        p = Path(settings.tts_piper_model)
        if p.is_file() and p not in out:
            out.append(p)
    return out


def _read_voice(model: Path) -> dict | None:
    config = model.with_name(model.name + ".json")
    try:
        cfg = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None   # a model without its config can't be spoken (sample rate, phonemes)
    if not isinstance(cfg, dict):
        return None
    vid = model.name[:-len(".onnx")]
    if not re.match(VOICE_RX, vid):
        return None
    audio = cfg.get("audio") if isinstance(cfg.get("audio"), dict) else {}
    lang = cfg.get("language") if isinstance(cfg.get("language"), dict) else {}
    known = _CATALOG.get(vid) or {}
    quality = audio.get("quality") or (parts(vid)[2] if vid.count("-") >= 2 else None)
    return {"id": vid, "language": lang.get("code") or (cfg.get("espeak") or {}).get("voice") or (parts(vid)[0] if "-" in vid else None),
            "quality": quality, "gender": known.get("gender"), "sample_rate": int(audio.get("sample_rate") or 22050),
            "speakers": int(cfg.get("num_speakers") or 1), "label": known.get("label"), "catalog": vid in _CATALOG,
            "model": model, "config": config}


def installed_voices() -> list[dict]:
    """The Piper voices on disk, catalog ones first (in catalog order), then the others by id. Rescanned when the folder
    changes (an operator installing voices needs no restart)."""
    files = _candidates()
    key = []
    for p in files:
        try:
            st, cst = p.stat(), p.with_name(p.name + ".json").stat()
            key.append((str(p), st.st_size, st.st_mtime_ns, cst.st_mtime_ns))
        except OSError:
            key.append((str(p), None))
    key = tuple(key)
    if _scan["key"] != key:
        voices = [v for v in (_read_voice(p) for p in files) if v]
        order = {vid: i for i, vid in enumerate(_CATALOG)}
        voices.sort(key=lambda v: (order.get(v["id"], len(order)), v["id"]))
        _scan.update(key=key, voices=voices)
    return list(_scan["voices"])


def public_voice(v: dict) -> dict:
    return {k: v[k] for k in ("id", "language", "quality", "gender", "sample_rate", "speakers", "label", "catalog")}


# ---------------------------------------------------------------- engines

_tts: dict = {}   # cached probes: "runtime", "sapi", "espeak"; "engine" set = an override (tests)


class _Package:
    """Piper in-process through the piper-tts package (1.2: synthesize(text, wav_file, ...); 1.3+: synthesize(text,
    syn_config) yielding AudioChunks). The last LOADED_MAX voices stay loaded; one synthesis at a time."""
    name = "piper-tts"

    def __init__(self, voice_cls, config_cls) -> None:
        self.voice_cls, self.config_cls = voice_cls, config_cls
        self.loaded: collections.OrderedDict = collections.OrderedDict()
        self.lock = threading.Lock()

    def _load(self, v: dict):
        key = str(v["model"])
        if key in self.loaded:
            self.loaded.move_to_end(key)
            return self.loaded[key]
        voice = self.voice_cls.load(str(v["model"]), config_path=str(v["config"]))
        self.loaded[key] = voice
        while len(self.loaded) > LOADED_MAX:
            self.loaded.popitem(last=False)
        return voice

    def speak(self, v: dict, text: str, length_scale: float, pause_s: float, out: Path) -> None:
        with self.lock:
            voice = self._load(v)
            if self.config_cls is None:   # piper-tts 1.2
                with wave.open(str(out), "wb") as w:
                    voice.synthesize(text, w, length_scale=length_scale, sentence_silence=pause_s)
                return
            chunks = list(voice.synthesize(text, syn_config=self.config_cls(length_scale=length_scale)))
        if not chunks:
            raise TTSError("Piper made no audio")
        first = chunks[0]
        gap = b"\0" * (int(first.sample_rate * pause_s) * first.sample_width * first.sample_channels)
        with wave.open(str(out), "wb") as w:
            w.setnchannels(first.sample_channels)
            w.setsampwidth(first.sample_width)
            w.setframerate(first.sample_rate)
            w.writeframes(gap.join(c.audio_int16_bytes for c in chunks))


class _Binary:
    """An external piper binary (the C++ release): the text on stdin, the WAV to --output_file."""
    name = "piper-binary"

    def __init__(self, exe: str) -> None:
        self.exe = exe

    def speak(self, v: dict, text: str, length_scale: float, pause_s: float, out: Path) -> None:
        cmd = [self.exe, "--model", str(v["model"]), "--config", str(v["config"]), "--output_file", str(out),
               "--length_scale", f"{length_scale:.3f}", "--sentence_silence", f"{pause_s:.2f}"]
        r = subprocess.run(cmd, input=text.encode(), capture_output=True, timeout=60, creationflags=NO_WINDOW)
        if r.returncode != 0:
            raise TTSError("piper failed: " + (r.stderr.decode(errors="replace").strip()[-160:] or f"exit {r.returncode}"))


def piper_runtime():
    """The package (preferred) or the binary, found once; None = no Piper on this server."""
    if "runtime" not in _tts:
        rt = None
        try:
            from piper import PiperVoice   # noqa: PLC0415 - optional dependency
            try:
                from piper import SynthesisConfig   # noqa: PLC0415 - 1.3+
            except ImportError:
                SynthesisConfig = None   # noqa: N806
            rt = _Package(PiperVoice, SynthesisConfig)
        except Exception:  # noqa: BLE001 - not installed, or onnxruntime failing to load: the server still starts
            exe = settings.tts_piper_bin or shutil.which("piper")
            if exe and (Path(exe).is_file() or shutil.which(exe)):
                rt = _Binary(exe)
        _tts["runtime"] = rt
    return _tts["runtime"]


def _powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


def sapi_ok() -> bool:
    """Windows SAPI has at least one voice (a PowerShell start, ~1-2 s, once: call from a thread)."""
    if "sapi" not in _tts:
        ok = False
        ps = _powershell() if os.name == "nt" else None
        if ps:
            try:
                r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command",
                                    "Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices().Count"],
                                   capture_output=True, timeout=30, creationflags=NO_WINDOW)
                ok = r.returncode == 0 and r.stdout.strip() not in (b"", b"0")
            except (OSError, subprocess.TimeoutExpired):
                ok = False
        _tts["sapi"] = ok
    return _tts["sapi"]


def espeak_exe() -> str | None:
    if "espeak" not in _tts:
        _tts["espeak"] = next((exe for exe in ("espeak-ng", "espeak") if shutil.which(exe)), None)
    return _tts["espeak"]


def engines(probe: bool = True) -> dict:
    """{piper: "piper-tts" | "piper-binary" | None, sapi: bool, espeak: exe | None}. probe=False: only what is known
    already (the heartbeat must not start PowerShell); unknown = None."""
    if probe:
        piper_runtime(), sapi_ok(), espeak_exe()
    rt = _tts.get("runtime")
    return {"piper": rt.name if rt else None, "sapi": _tts.get("sapi"), "espeak": _tts.get("espeak")}


# ---------------------------------------------------------------- choosing who speaks

def clamp_speed(speed) -> float:
    try:
        s = float(speed)
    except (TypeError, ValueError):
        return 1.0
    return 1.0 if math.isnan(s) else round(min(SPEED_MAX, max(SPEED_MIN, s)), 2)


def length_scale(speed) -> float:
    """Piper's length_scale (longer = slower) for a speed: 0.7 -> 1.4, 1.0 -> 1.0, 1.3 -> 0.8, linear on each side."""
    s = clamp_speed(speed)
    if s <= 1.0:
        return round(1.0 + (1.0 - s) / (1.0 - SPEED_MIN) * (LENGTH_SLOW - 1.0), 3)
    return round(1.0 - (s - 1.0) / (SPEED_MAX - 1.0) * (1.0 - LENGTH_FAST), 3)


def sapi_rate(speed) -> int:
    """SAPI Rate (-10..10, ~10% a step): 0.7 -> -3, 1.3 -> 3."""
    return int(round((clamp_speed(speed) - 1.0) * 10))


def espeak_wpm(speed) -> int:
    return int(round(175 * clamp_speed(speed)))


def _piper_order(voice: str | None, installed: list[dict]) -> list[dict]:
    """The asked-for voice, then the others: same language, then same gender, then catalog order."""
    by_id = {v["id"]: v for v in installed}
    want = by_id.get(voice) if voice else None
    ref = want or _CATALOG.get(voice or "") or {}
    lang = (ref.get("language") or (parts(voice)[0] if voice and voice.count("-") >= 2 else "")).split("_")[0]
    rest = [v for v in installed if v is not want]
    rest.sort(key=lambda v: (not lang or (v.get("language") or "").split("_")[0] != lang,
                             not ref.get("gender") or v.get("gender") != ref.get("gender")))   # stable: catalog order within
    return ([want] if want else []) + rest


def plan(voice: str | None, probe: bool = True) -> list[tuple[str, dict | None]]:
    """The engines to try, in order: ("piper", voice) for each installed voice, ("sapi", None), (espeak exe, None)."""
    out: list[tuple[str, dict | None]] = []
    rt = piper_runtime() if probe else _tts.get("runtime")
    if rt:
        out += [("piper", v) for v in _piper_order(voice, installed_voices())]
    if (sapi_ok() if probe else _tts.get("sapi")):
        out.append(("sapi", None))
    ex = espeak_exe() if probe else _tts.get("espeak")
    if ex:
        out.append((ex, None))
    return out


@dataclass
class Spoken:
    """Who spoke (or will speak) a text. `voice` is the Piper voice id (None for SAPI / espeak, their default voice)."""
    engine: str
    voice: str | None
    requested: str | None
    fallback: bool
    reason: str | None = None

    def public(self) -> dict:
        return {**asdict(self), "label": self.label()}

    def label(self) -> str:
        if self.engine == "piper":
            lab = (_CATALOG.get(self.voice or "") or {}).get("label")
            return f"{self.voice} ({lab})" if lab else str(self.voice)
        return "Windows voice" if self.engine == "sapi" else self.engine


def _spoken(engine: str, v: dict | None, requested: str | None, reason: str | None = None) -> Spoken:
    vid = v["id"] if v else None
    fallback = bool(requested) and vid != requested
    return Spoken(engine, vid, requested, fallback, reason if fallback else None)


def _missing(probe: bool = True) -> str:
    """Why a voice asked for isn't speaking when it didn't fail: no Piper at all, or not this voice."""
    if not (piper_runtime() if probe else _tts.get("runtime")):
        return "Piper (piper-tts) is not installed on this server"
    return "not installed on this server"


def effective(voice: str | None, probe: bool = True) -> Spoken | None:
    """Who would speak with this voice now, without synthesizing (None = no engine)."""
    steps = plan(voice, probe)
    if not steps:
        return None
    engine, v = steps[0]
    return _spoken(engine, v, voice, _missing(probe) if voice and not (v and v["id"] == voice) else None)


def tts_engine(refresh: bool = False) -> str | None:
    """"piper", "sapi", "espeak-ng", "espeak" or None: what speaks by default (probes once; call from a thread)."""
    if "engine" in _tts and not refresh:
        return _tts["engine"]   # set by tests
    if refresh:
        for k in ("runtime", "sapi", "espeak", "engine"):
            _tts.pop(k, None)
    steps = plan(None)
    return steps[0][0] if steps else None


# ---------------------------------------------------------------- the chime

def chime_frames(rate: int, sampwidth: int = 2, channels: int = 1) -> bytes:
    """Two soft tones (880 Hz then 660 Hz, ~0.45 s) and a short silence, as 16-bit PCM at `rate`: the speaker's
    "attention" before the words. Generated, so there is no file to ship or lose."""
    def tone(freq: float, secs: float) -> list[int]:
        n, fade = int(rate * secs), max(1, int(rate * 0.012))
        return [int(0.32 * 32767 * math.sin(2 * math.pi * freq * i / rate) * min(1.0, i / fade, (n - 1 - i) / fade)) for i in range(n)]
    samples = tone(880, 0.16) + [0] * int(rate * 0.05) + tone(660, 0.24) + [0] * int(rate * 0.25)
    a = array.array("h", (s for s in samples for _ in range(channels)))
    if a.itemsize != 2 or sampwidth != 2:
        raise TTSError("the chime needs 16-bit audio")
    if sys.byteorder == "big":
        a.byteswap()   # WAV is little-endian
    return a.tobytes()


def prepend_chime(path: Path) -> bool:
    """Put the chime in front of a 16-bit WAV in place. False (file unchanged) for another sample format."""
    with wave.open(str(path), "rb") as r:
        params, frames = r.getparams(), r.readframes(r.getnframes())
    if params.sampwidth != 2:
        log.warning("talk-down chime skipped: %d-bit audio", params.sampwidth * 8)
        return False
    with wave.open(str(path), "wb") as w:
        w.setnchannels(params.nchannels)
        w.setsampwidth(2)
        w.setframerate(params.framerate)
        w.writeframes(chime_frames(params.framerate, 2, params.nchannels) + frames)
    return True


# ---------------------------------------------------------------- synthesis

def _sapi(text: str, out: Path, speed) -> None:
    script = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              "$s.Rate = [int]$env:NVR_TTS_RATE; $s.SetOutputToWaveFile($env:NVR_TTS_OUT); $s.Speak($env:NVR_TTS_TEXT); $s.Dispose()")
    env = {**os.environ, "NVR_TTS_TEXT": text, "NVR_TTS_OUT": str(out), "NVR_TTS_RATE": str(sapi_rate(speed))}
    r = subprocess.run([_powershell() or "powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, timeout=60, env=env, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise TTSError("SAPI failed: " + (r.stderr.decode(errors="replace").strip()[-160:] or f"exit {r.returncode}"))


def _espeak(exe: str, text: str, out: Path, speed) -> None:
    r = subprocess.run([exe, "--stdin", "-s", str(espeak_wpm(speed)), "-w", str(out)], input=text.encode(),
                       capture_output=True, timeout=60, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise TTSError(f"{exe} failed: " + (r.stderr.decode(errors="replace").strip()[-160:] or f"exit {r.returncode}"))


def synthesize(text: str, out: Path, voice: str | None = None, speed=1.0, chime: bool = False,
               pause_s: float = SENTENCE_PAUSE_S) -> Spoken:
    """Speak `text` into the WAV `out` (the engine's own rate; ffmpeg makes it G.711 for the speaker). Tries plan(voice)
    in order; TTSError when nothing could. Blocking: call from a thread."""
    text = " ".join((text or "").split())
    if not text:
        raise TTSError("nothing to say")
    steps = plan(voice)
    if not steps:
        raise TTSError("this server has no text-to-speech engine")
    out.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for engine, v in steps:
        tmp = out.with_name(out.stem + f".{uuid.uuid4().hex[:6]}.tmp.wav")
        t0 = time.monotonic()
        try:
            if engine == "piper":
                piper_runtime().speak(v, text, length_scale(speed), max(0.0, float(pause_s)), tmp)
            elif engine == "sapi":
                _sapi(text, tmp, speed)
            else:
                _espeak(engine, text, tmp, speed)
            if not tmp.is_file() or tmp.stat().st_size < 100:
                raise TTSError(f"{engine} made no audio")
            if chime:
                prepend_chime(tmp)
        except Exception as e:  # noqa: BLE001 - any engine failure moves on to the next one
            tmp.unlink(missing_ok=True)
            what = f"{v['id']}" if v else engine
            errors.append(f"{what}: {str(e)[:160] or type(e).__name__}")
            log.warning("text-to-speech with %s failed: %s", what, e)
            continue
        tmp.replace(out)
        reason = None
        if voice and not (v and v["id"] == voice):
            reason = next((x for x in errors if x.startswith(voice + ":")), None) or _missing()
        spoken = _spoken(engine, v, voice, reason)
        if spoken.fallback:
            log.info("talk-down voice %s unavailable (%s): spoke with %s", voice, reason, spoken.label())
        log.debug("text-to-speech: %s in %.2f s", spoken.label(), time.monotonic() - t0)
        return spoken
    raise TTSError("text-to-speech failed: " + "; ".join(errors)[-300:])


# ---------------------------------------------------------------- the Site's voice setting

def clean_setting(value) -> dict:
    """{voice: id | None, speed: 0.7-1.3, chime: bool} from whatever is stored (bad parts fall back to the default)."""
    v = value if isinstance(value, dict) else {}
    voice = v.get("voice")
    voice = voice if isinstance(voice, str) and re.match(VOICE_RX, voice) else None
    return {"voice": voice, "speed": clamp_speed(v.get("speed", 1.0)), "chime": bool(v.get("chime", False))}


# the database is imported where it is used: tools/install_piper_voices.py imports this module for the catalog and the
# voices folder, and must not open the server's database

def site_setting() -> dict:
    from .db import db   # noqa: PLC0415
    return clean_setting(db.get_setting(SETTING))


def set_site_setting(value: dict, by: str) -> dict:
    from .db import db   # noqa: PLC0415
    s = clean_setting(value)
    db.set_setting(SETTING, s)
    db.set_setting(SETTING + "_meta", {"by": by, "at": time.time()})
    log.info("talk-down voice set by %s: %s, speed %.2f%s", by, s["voice"] or "automatic", s["speed"], ", chime" if s["chime"] else "")
    return s


def setting_meta() -> dict:
    from .db import db   # noqa: PLC0415
    m = db.get_setting(SETTING + "_meta")
    return m if isinstance(m, dict) else {}


def summary() -> dict:
    """What the hub heartbeat carries (no paths, nothing secret): the setting, the installed voice ids, the engines
    known so far and who would speak now. Never starts a probe."""
    s = site_setting()
    eff = effective(s["voice"], probe=False)
    return {"setting": s, "voices": [v["id"] for v in installed_voices()], "engines": engines(probe=False),
            "effective": eff.public() if eff else None}


# ---------------------------------------------------------------- preview rate limit

_previews: dict[str, collections.deque] = {}


def preview_limited(who: str, now: float | None = None) -> bool:
    """True when `who` previewed PREVIEW_PER_MIN times in the last minute; else counts this one."""
    now = time.monotonic() if now is None else now
    q = _previews.setdefault(who, collections.deque())
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= PREVIEW_PER_MIN:
        return True
    q.append(now)
    return False
