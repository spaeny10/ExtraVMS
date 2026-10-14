"""Talk-down voices (tts.py, the voice routes in speakers_api.py) with a fake Piper (no model, no onnxruntime): the
catalog and its repository paths (and that frontend/src/talkdownVoice.ts lists the same voices), installed voices from
fake .onnx/.onnx.json files, the speed -> length_scale mapping, the chime in front of the words, the fallback chain
(another Piper voice -> SAPI -> espeak) and who spoke, the Site setting (hub-managed: the own page can't change it),
the preview route (audio/wav, roles, length cap, rate limit), a play reporting its engine and a library message's own
voice, the heartbeat summary.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_tts.py   (from backend/)
"""
import asyncio
import io
import json
import os
import re
import struct
import sys
import tempfile
import wave
from pathlib import Path

os.environ.setdefault("NVR_ALLOWED_HOSTS", "site")  # the test client's Host (lan_guard Host allow-list)
for _k in ("NVR_DATA_DIR", "NVR_RECORDINGS_DIR", "NVR_RUNTIME_DIR", "NVR_TTS_VOICES_DIR"):
    os.environ[_k] = tempfile.mkdtemp(prefix="nvr-tts-test-")  # never the real folders
os.environ["NVR_TTS_PIPER_MODEL"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from nvr import hub_agent, talkdown, tts  # noqa: E402
from nvr.api import app  # noqa: E402
from nvr.db import db  # noqa: E402

VOICES = Path(os.environ["NVR_TTS_VOICES_DIR"])
LAN = ("192.168.1.9", 5000)


def call(method, path, body=None, role=None, user="op@example"):
    async def go():
        tunnel = role is not None
        transport = httpx.ASGITransport(app=hub_agent.as_tunnel(app) if tunnel else app, client=hub_agent.IN_PROCESS_CLIENT if tunnel else LAN)
        headers = {"x-hub-role": role, "x-hub-user": user} if tunnel else {}
        async with httpx.AsyncClient(transport=transport, base_url="http://site") as c:
            return await c.request(method, path, json=body, headers=headers)
    return asyncio.run(go())


def write_wav(path: Path, rate: int, seconds: float, value: int = 1000) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<h", value) * int(rate * seconds))


def wav_of(data_or_path) -> tuple[int, bytes]:
    src = io.BytesIO(data_or_path) if isinstance(data_or_path, bytes) else str(data_or_path)
    with wave.open(src, "rb") as r:
        return r.getframerate(), r.readframes(r.getnframes())


class FakePiper:
    """Speaks like the piper-tts runtime: a WAV at the voice's rate, longer for a longer length_scale. Voices in `fail` raise."""
    name = "piper-tts"

    def __init__(self):
        self.calls: list[tuple[str, str, float, float]] = []
        self.fail: set[str] = set()

    def speak(self, v, text, length_scale, pause_s, out):
        self.calls.append((v["id"], text, length_scale, pause_s))
        if v["id"] in self.fail:
            raise RuntimeError(f"model {v['id']} is broken")
        write_wav(out, v["sample_rate"], 0.02 * len(text.split()) * length_scale, 2000)


def voice_files(vid: str, rate: int = 22050, lang: str | None = None, config=True) -> None:
    (VOICES / f"{vid}.onnx").write_bytes(b"onnx")
    if config is True:
        cfg = {"audio": {"sample_rate": rate, "quality": "medium"}, "language": {"code": lang or vid.split("-")[0]}, "num_speakers": 1}
        (VOICES / f"{vid}.onnx.json").write_text(json.dumps(cfg))
    elif config:
        (VOICES / f"{vid}.onnx.json").write_text(config)


def engines(piper=None, sapi=False, espeak=None) -> None:
    """Pin the engine probes (no PowerShell, no PATH lookups)."""
    tts._tts.clear()
    tts._tts.update(runtime=piper, sapi=sapi, espeak=espeak)


def setup_voices() -> FakePiper:
    for p in VOICES.iterdir():
        p.unlink()
    voice_files("en_US-amy-medium")
    voice_files("en_GB-alan-medium")
    voice_files("my_voice", 16000, "de_DE")
    voice_files("no_config", config=False)            # a model without its .json: skipped
    voice_files("en_US-joe-medium", config="{broken")  # unreadable .json: skipped
    fake = FakePiper()
    engines(fake)
    return fake


# ---------------------------------------------------------------- catalog, voices, speed

def test_catalog_paths_and_urls():
    assert tts.repo_path("en_US-hfc_female-medium") == "en/en_US/hfc_female/medium/en_US-hfc_female-medium.onnx"
    assert tts.voice_url("en_GB-jenny_dioco-medium") == ("https://huggingface.co/rhasspy/piper-voices/resolve/main/"
                                                          "en/en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium.onnx")
    cat = tts.catalog()
    ids = [c["id"] for c in cat]
    assert len(ids) == len(set(ids)) >= 8
    for c in cat:
        region, name, quality = c["id"].split("-")
        assert re.fullmatch(r"[a-z]{2}_[A-Z]{2}", region) and quality in ("low", "medium", "high") and re.fullmatch(r"[a-z_]+", name)
        assert c["url"].endswith(f"/{region.split('_')[0]}/{region}/{name}/{quality}/{c['id']}.onnx") and c["config_url"] == c["url"] + ".json"
        assert c["gender"] in ("female", "male") and " · " in c["label"] and c["language"] == region


def test_catalog_matches_the_frontend():
    ts = Path(__file__).resolve().parents[2] / "frontend" / "src" / "talkdownVoice.ts"
    if not ts.is_file():
        print("  (skipped: no frontend)")
        return
    rows = re.findall(r'\{ id: "([^"]+)", name: "[^"]+", label: "([^"]+)", gender: "(\w+)" \}', ts.read_text(encoding="utf-8"))
    assert rows == [(c["id"], c["label"], c["gender"]) for c in tts.CATALOG], rows


def test_installed_voices_from_the_folder():
    setup_voices()
    vs = tts.installed_voices()
    assert [v["id"] for v in vs] == ["en_US-amy-medium", "en_GB-alan-medium", "my_voice"]   # catalog order, then the others
    amy, alan, mine = vs
    assert (amy["language"], amy["quality"], amy["gender"], amy["sample_rate"], amy["catalog"]) == ("en_US", "medium", "female", 22050, True)
    assert alan["gender"] == "male" and alan["label"] == "British English · male · measured"
    assert (mine["language"], mine["gender"], mine["sample_rate"], mine["catalog"], mine["label"]) == ("de_DE", None, 16000, False, None)
    assert set(tts.public_voice(amy)) == {"id", "language", "quality", "gender", "sample_rate", "speakers", "label", "catalog"}
    # a voice installed while the server runs is picked up (the folder is rescanned when it changes)
    voice_files("en_US-ryan-high")
    assert [v["id"] for v in tts.installed_voices()][:3] == ["en_US-amy-medium", "en_US-ryan-high", "en_GB-alan-medium"]
    (VOICES / "en_US-ryan-high.onnx").unlink()
    assert "en_US-ryan-high" not in [v["id"] for v in tts.installed_voices()]


def test_speed_mapping():
    assert tts.length_scale(0.7) == 1.4 and tts.length_scale(1.0) == 1.0 and tts.length_scale(1.3) == 0.8
    assert tts.length_scale(0.85) == 1.2 and tts.length_scale(1.15) == 0.9
    assert tts.length_scale(0.1) == 1.4 and tts.length_scale(5) == 0.8 and tts.length_scale("x") == 1.0 and tts.length_scale(float("nan")) == 1.0
    assert tts.length_scale(0.8) > tts.length_scale(0.9) > tts.length_scale(1.1) > tts.length_scale(1.2)   # monotonic
    assert (tts.sapi_rate(0.7), tts.sapi_rate(1.0), tts.sapi_rate(1.3)) == (-3, 0, 3)
    assert (tts.espeak_wpm(0.7), tts.espeak_wpm(1.0), tts.espeak_wpm(1.3)) == (122, 175, 228)


# ---------------------------------------------------------------- synthesis and fallback

def test_piper_speaks_with_the_chime_first():
    fake = setup_voices()
    out = Path(tempfile.mkdtemp()) / "x.wav"
    spoken = tts.synthesize("  Attention.   Leave   now. ", out, "en_GB-alan-medium", 1.3, chime=True)
    assert (spoken.engine, spoken.voice, spoken.requested, spoken.fallback, spoken.reason) == ("piper", "en_GB-alan-medium", "en_GB-alan-medium", False, None)
    assert spoken.public()["label"] == "en_GB-alan-medium (British English · male · measured)"
    assert fake.calls == [("en_GB-alan-medium", "Attention. Leave now.", 0.8, tts.SENTENCE_PAUSE_S)]
    rate, frames = wav_of(out)
    chime = tts.chime_frames(22050)
    assert rate == 22050 and frames.startswith(chime) and len(frames) > len(chime)
    assert frames[len(chime):] == struct.pack("<h", 2000) * (len(frames[len(chime):]) // 2)   # then the words
    peak = max(abs(x) for x in struct.unpack(f"<{len(chime) // 2}h", chime))
    assert 8000 < peak < 12000 and 0.4 < len(chime) / 2 / 22050 < 0.8   # audible, not clipping, under a second
    # no chime: the words alone, at the voice's own rate
    tts.synthesize("Leave now.", out, "my_voice", 0.7)
    rate, frames = wav_of(out)
    assert rate == 16000 and frames[:2] == struct.pack("<h", 2000) and fake.calls[-1][2] == 1.4
    assert not list(out.parent.glob("*.tmp.wav"))


def test_fallback_chain_and_who_spoke():
    fake = setup_voices()
    out = Path(tempfile.mkdtemp()) / "x.wav"
    # not installed: another Piper voice, same language and gender first (Ryan is a man: Alan, not Amy)
    s = tts.synthesize("Hello there.", out, "en_US-ryan-high")
    assert (s.engine, s.voice, s.fallback, s.reason) == ("piper", "en_GB-alan-medium", True, "not installed on this server")
    s = tts.synthesize("Hello there.", out, "en_US-kristin-medium")
    assert s.voice == "en_US-amy-medium" and s.fallback
    # automatic: the first installed voice; never a fallback
    s = tts.synthesize("Hello there.", out, None)
    assert (s.voice, s.fallback, s.requested) == ("en_US-amy-medium", False, None)
    # the voice fails: the next Piper voice, and the reason names the failure
    fake.fail = {"en_US-amy-medium"}
    s = tts.synthesize("Hello there.", out, "en_US-amy-medium")
    assert s.voice == "en_GB-alan-medium" and s.fallback and "broken" in s.reason
    # every Piper voice fails: Windows SAPI, then espeak
    fake.fail = {"en_US-amy-medium", "en_GB-alan-medium", "my_voice"}
    said: list[tuple[str, object]] = []
    orig = tts._sapi, tts._espeak
    tts._sapi = lambda text, out, speed: (said.append(("sapi", speed)), write_wav(out, 22050, 0.3))
    tts._espeak = lambda exe, text, out, speed: (said.append((exe, speed)), write_wav(out, 22050, 0.3))
    try:
        engines(fake, sapi=True, espeak="espeak-ng")
        s = tts.synthesize("Hello there.", out, "en_US-amy-medium", 1.2)
        assert (s.engine, s.voice, s.fallback, s.public()["label"]) == ("sapi", None, True, "Windows voice") and said == [("sapi", 1.2)]
        tts._sapi = lambda text, out, speed: (_ for _ in ()).throw(tts.TTSError("SAPI failed: no voice"))
        s = tts.synthesize("Hello there.", out, "en_US-amy-medium")
        assert s.engine == "espeak-ng" and said[-1] == ("espeak-ng", 1.0)
        # no Piper at all: the reason says so
        engines(None, sapi=False, espeak="espeak-ng")
        s = tts.synthesize("Hello there.", out, "en_US-amy-medium")
        assert s.engine == "espeak-ng" and s.reason == "Piper (piper-tts) is not installed on this server"
        e = tts.effective("en_US-amy-medium")
        assert e.engine == "espeak-ng" and e.fallback and "Piper" in e.reason
        # nothing works / nothing there
        tts._espeak = lambda exe, text, out, speed: (_ for _ in ()).throw(tts.TTSError("espeak-ng failed: exit 1"))
        try:
            tts.synthesize("Hello there.", out)
            raise AssertionError("expected TTSError")
        except tts.TTSError as err:
            assert "espeak-ng failed" in str(err)
        engines(None)
        assert tts.effective(None) is None and tts.tts_engine() is None
        try:
            tts.synthesize("Hello", out)
            raise AssertionError("expected TTSError")
        except tts.TTSError as err:
            assert "no text-to-speech" in str(err)
    finally:
        tts._sapi, tts._espeak = orig
    assert not list(out.parent.glob("*.tmp.wav"))


def test_effective_engine_and_summary():
    setup_voices()
    assert tts.tts_engine() == "piper"
    e = tts.effective("en_GB-alan-medium")
    assert (e.engine, e.voice, e.fallback) == ("piper", "en_GB-alan-medium", False)
    db.set_setting("hub_token", None)
    tts.set_site_setting({"voice": "en_US-ryan-high", "speed": 1.25, "chime": True}, "local")
    s = tts.summary()
    assert s["setting"] == {"voice": "en_US-ryan-high", "speed": 1.25, "chime": True}
    assert s["voices"] == ["en_US-amy-medium", "en_GB-alan-medium", "my_voice"]
    assert s["engines"] == {"piper": "piper-tts", "sapi": False, "espeak": None}
    assert s["effective"]["voice"] == "en_GB-alan-medium" and s["effective"]["fallback"] is True
    assert str(VOICES) not in json.dumps(s) and ".onnx" not in json.dumps(s)   # no paths to the hub
    # bad stored values read as the defaults
    db.set_setting(tts.SETTING, {"voice": "../evil", "speed": 9, "chime": "yes"})
    assert tts.site_setting() == {"voice": None, "speed": 1.3, "chime": True}
    db.set_setting(tts.SETTING, "junk")
    assert tts.site_setting() == tts.DEFAULT


# ---------------------------------------------------------------- routes

def test_voice_routes_and_hub_management():
    setup_voices()
    db.set_setting("hub_token", None)
    db.set_setting(tts.SETTING, None)
    r = call("GET", "/api/talkdown/voice")
    assert r.status_code == 200 and r.json()["setting"] == tts.DEFAULT and r.json()["managed"] is False
    assert r.json()["effective"]["voice"] == "en_US-amy-medium"
    # not enrolled: the own page sets it
    r = call("PUT", "/api/talkdown/voice", {"voice": "en_GB-alan-medium", "speed": 0.9, "chime": True})
    assert r.status_code == 200, r.text
    assert r.json()["setting"] == {"voice": "en_GB-alan-medium", "speed": 0.9, "chime": True} and r.json()["set_by"] == "local"
    assert call("PUT", "/api/talkdown/voice", {"voice": "../x", "speed": 1}).status_code == 422
    assert call("PUT", "/api/talkdown/voice", {"voice": None, "speed": 2}).status_code == 422
    # roles through the tunnel: read operator, write admin
    assert call("GET", "/api/talkdown/voice", role="viewer").status_code == 403
    assert call("GET", "/api/talkdown/voices", role="viewer").status_code == 403
    assert call("GET", "/api/talkdown/voice", role="operator").status_code == 200
    assert call("PUT", "/api/talkdown/voice", {"voice": None}, role="operator").status_code == 403
    # enrolled with a hub: the hub sets it (through the tunnel), the own page can't
    db.set_setting("hub_token", "t-" + "x" * 8)
    try:
        assert call("GET", "/api/talkdown/voice").json()["managed"] is True
        r = call("PUT", "/api/talkdown/voice", {"voice": "en_US-amy-medium"})
        assert r.status_code == 409 and "hub" in r.text
        r = call("PUT", "/api/talkdown/voice", {"voice": "en_US-ryan-high", "speed": 1.1, "chime": False}, role="admin", user="hub")
        assert r.status_code == 200 and r.json()["setting"]["voice"] == "en_US-ryan-high" and r.json()["set_by"] == "hub"
        assert r.json()["effective"]["fallback"] is True and r.json()["effective"]["voice"] == "en_GB-alan-medium"
    finally:
        db.set_setting("hub_token", None)
    # the installed voices and the catalog
    j = call("GET", "/api/talkdown/voices", role="operator").json()
    assert [v["id"] for v in j["voices"]] == ["en_US-amy-medium", "en_GB-alan-medium", "my_voice"]
    inst = {c["id"]: c["installed"] for c in j["catalog"]}
    assert inst["en_US-amy-medium"] and inst["en_GB-alan-medium"] and not inst["en_US-ryan-high"]
    assert j["engines"]["piper"] == "piper-tts" and j["speed"] == {"min": 0.7, "max": 1.3}


def test_preview_route():
    fake = setup_voices()
    tts._previews.clear()
    db.set_setting(tts.SETTING, {"voice": "en_GB-alan-medium", "speed": 1.0, "chime": False})
    r = call("POST", "/api/talkdown/preview", {}, role="operator")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert (r.headers["x-tts-engine"], r.headers["x-tts-voice"], r.headers["x-tts-fallback"]) == ("piper", "en_GB-alan-medium", "0")
    assert wav_of(r.content)[0] == 22050 and fake.calls[-1][1] == tts.PREVIEW_TEXT and r.headers["cache-control"] == "no-store"
    # the draft's values over the Site's; voice null = automatic
    r = call("POST", "/api/talkdown/preview", {"text": "Gate is closed.", "voice": "my_voice", "speed": 0.7, "chime": True}, role="operator")
    rate, frames = wav_of(r.content)
    assert rate == 16000 and frames.startswith(tts.chime_frames(16000)) and fake.calls[-1] == ("my_voice", "Gate is closed.", 1.4, tts.SENTENCE_PAUSE_S)
    r = call("POST", "/api/talkdown/preview", {"voice": None}, role="operator")
    assert r.headers["x-tts-voice"] == "en_US-amy-medium"
    r = call("POST", "/api/talkdown/preview", {"voice": "en_US-ryan-high"}, role="operator")
    assert r.headers["x-tts-voice"] == "en_GB-alan-medium" and r.headers["x-tts-fallback"] == "1"
    # viewers can't; the text is capped; nothing went to a speaker
    assert call("POST", "/api/talkdown/preview", {}, role="viewer").status_code == 403
    assert call("POST", "/api/talkdown/preview", {"text": "x" * 301}, role="operator").status_code == 422
    assert call("POST", "/api/talkdown/preview", {"voice": "a b"}, role="operator").status_code == 422
    assert not talkdown.manager._jobs
    # at most PREVIEW_PER_MIN a minute per user
    tts._previews.clear()
    codes = [call("POST", "/api/talkdown/preview", {}, role="operator", user="busy@example").status_code for _ in range(tts.PREVIEW_PER_MIN + 1)]
    assert codes[:-1] == [200] * tts.PREVIEW_PER_MIN and codes[-1] == 429
    assert call("POST", "/api/talkdown/preview", {}, role="operator", user="other@example").status_code == 200
    assert tts.preview_limited("x", now=0) is False and all(not tts.preview_limited("y", now=i) for i in range(tts.PREVIEW_PER_MIN))
    assert tts.preview_limited("y", now=30) is True and tts.preview_limited("y", now=61) is False
    # no engine: 409
    engines(None)
    assert call("POST", "/api/talkdown/preview", {}, role="operator", user="third@example").status_code == 409
    assert not list((talkdown.talkdown_dir() / "preview").iterdir())


class FakeSession:
    """The speaker's RTSP session, answering at once (no network): the play path is exercised end to end."""

    def __init__(self, speaker, timeout=10):
        self.channel, self.timeout_s, self.sent = 0, 60, 0

    def describe(self):
        return {"control": "audioback", "codecs": [{"pt": 0, "name": "PCMU", "rate": 8000}]}

    def setup(self, track):
        return 0

    def play(self):
        pass

    def keepalive(self):
        pass

    def send(self, data):
        self.sent += 1

    def drain(self):
        pass

    def teardown(self):
        pass

    def close(self):
        pass


def test_play_reports_who_spoke_and_message_voices():
    fake = setup_voices()
    db.set_setting(tts.SETTING, {"voice": "en_US-ryan-high", "speed": 1.0, "chime": False})
    db.upsert_speaker({"id": "spk1", "name": "Back wall", "host": "10.0.0.50", "rtsp_port": 554, "onvif_port": 80, "path": "/main",
                       "username": "admin", "password": "", "codec": "PCMU", "volume_note": "", "camera_ids": [], "enabled": True})
    orig = talkdown.Session, talkdown.to_g711
    talkdown.Session = FakeSession
    talkdown.to_g711 = lambda src, codec="PCMU": b"\xff" * 800   # 0.1 s: no ffmpeg needed
    try:
        r = call("POST", "/api/speakers/spk1/play", {"text": "Leave the yard now."}, role="operator")
        assert r.status_code == 200, r.text
        will = r.json()["tts"]   # the answer: who will speak (the Site's voice isn't here: its fallback)
        assert (will["engine"], will["voice"], will["requested"], will["fallback"]) == ("piper", "en_GB-alan-medium", "en_US-ryan-high", True)
        talkdown.manager.wait("spk1")
        last = talkdown.manager.status("spk1")["last"]
        assert last["result"] == "played" and last["tts"]["voice"] == "en_GB-alan-medium" and last["tts"]["fallback"] is True
        row = db.one("SELECT * FROM talkdown_log WHERE speaker_id='spk1' ORDER BY id DESC LIMIT 1")
        assert row["tts"] == "piper:en_GB-alan-medium" and row["text"] == "Leave the yard now."
        assert not list((talkdown.talkdown_dir() / "oneoff").glob("*.wav"))
        # a library text with its own voice and speed: spoken with them, and its speech kept for the next play
        r = call("POST", "/api/talkdown/messages", {"name": "Closed", "text": "The site is closed.", "voice": "en_US-amy-medium", "speed": 0.85})
        assert r.status_code == 200 and (r.json()["voice"], r.json()["speed"]) == ("en_US-amy-medium", 0.85)
        mid = r.json()["id"]
        n = len(fake.calls)
        for _ in range(2):
            assert call("POST", "/api/speakers/spk1/play", {"message_id": mid}, role="operator").json()["tts"]["voice"] == "en_US-amy-medium"
            talkdown.manager.wait("spk1")
            assert talkdown.manager.status("spk1")["last"]["tts"]["voice"] == "en_US-amy-medium"
        assert [c[:3] for c in fake.calls[n:]] == [("en_US-amy-medium", "The site is closed.", 1.2)]   # synthesized once
        # back to the Site's voice (kept name); a fallback isn't cached: spoken again each time
        r = call("PUT", f"/api/talkdown/messages/{mid}", {"name": "Site closed", "voice": None})
        assert r.json()["name"] == "Site closed" and r.json()["voice"] is None and r.json()["speed"] == 0.85
        assert call("PUT", f"/api/talkdown/messages/{mid}", {"name": "Site closed"}).json()["speed"] == 0.85   # left out = kept
        n = len(fake.calls)
        for _ in range(2):
            call("POST", "/api/speakers/spk1/play", {"message_id": mid}, role="operator")
            talkdown.manager.wait("spk1")
        assert [c[0] for c in fake.calls[n:]] == ["en_GB-alan-medium", "en_GB-alan-medium"]
        assert call("POST", "/api/talkdown/messages", {"name": "x", "text": "hi", "voice": "bad voice"}).status_code == 422
    finally:
        talkdown.Session, talkdown.to_g711 = orig


def test_install_tool_imports_without_the_database():
    """tools/install_piper_voices.py imports nvr.tts for the catalog: that must not open the server's database."""
    import subprocess
    code = ("import sys; sys.path.insert(0, '.'); import nvr.tts as t; assert 'nvr.db' not in sys.modules, 'db imported'; "
            "print(len(t.catalog()))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1], timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == str(len(tts.CATALOG)), r.stderr


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    talkdown.manager.stop_all()
    print("all passed")
