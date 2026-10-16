"""Talk-down (talkdown.py, speakers_api.py): the ONVIF backchannel RTSP exchange against a fake speaker that answers
like the Tonmind IP Speaker (Digest auth, the exact SDP it sends), RTP packetization and real-time pacing, one playback
per speaker (a new one replaces it) and Stop, speakers bound to several cameras and a camera with two speakers,
speaker CRUD without ever returning the password, roles through the tunnel, the hub summary, the message library and
the camera -> speaker conversion.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_talkdown.py   (from backend/)
"""
import asyncio
import base64
import hashlib
import io
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

os.environ.setdefault("NVR_ALLOWED_HOSTS", "site")  # the test client's Host (lan_guard Host allow-list)
os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-talkdown-test-")  # never the real DB
os.environ["NVR_TALKDOWN_WAKE_MS"] = "0"   # exact packet counts below; test_wake_tone_* turns it on
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from nvr import api, hub_agent, siteconfig, talkdown  # noqa: E402
from nvr.api import app  # noqa: E402
from nvr.db import db  # noqa: E402

LAN = ("192.168.1.9", 5000)
PW = "spk-" + "z" * 8   # a stand-in speaker password: must never come back from the API
USER = "admin"
# what the Tonmind IP Speaker (CS20-V3.3.45N) answers to DESCRIBE with the backchannel Require header
SDP = ("v=0\r\no=- 1 1 IN IP4 192.168.105.101\r\ns=IP Speaker\r\nc=IN IP4 0.0.0.0\r\nt=0 0\r\na=control:*\r\n"
       "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=control:trackID=1\r\na=recvonly\r\n"
       "m=audio 0 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\na=control:trackID=2\r\na=recvonly\r\n"
       "m=audio 0 RTP/AVP 0 8 97\r\na=rtpmap:0 PCMU/8000\r\na=rtpmap:8 PCMA/8000\r\na=rtpmap:97 G726-16/8000\r\n"
       "a=control:audioback\r\na=sendonly\r\n")
CAM = {"id": "cam1", "name": "Back Wall", "host": "10.0.0.5", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": "cam-pw-" + "q" * 6,
       "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": [], "retention_days": None, "scene_notes": "",
       "retention_policy": None, "synopsis_labels": ["person"], "policies": []}
md5 = lambda s: hashlib.md5(s.encode()).hexdigest()  # noqa: E731


class FakeSpeaker:
    """An RTSP server on 127.0.0.1 answering like the speaker: 401 + Digest challenge, DESCRIBE (the SDP above),
    SETUP, PLAY, GET_PARAMETER, TEARDOWN; it records every request and every interleaved packet with its arrival time."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.requests: list[dict] = []
        self.rtp: list[dict] = []
        self.bad_auth = 0
        self.conns = 0
        self.lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def speaker(self, sid="spk1", cams=("cam1",), **kw) -> dict:
        return {"id": sid, "name": f"Speaker {sid}", "host": "127.0.0.1", "rtsp_port": self.port, "onvif_port": 80, "path": "/main",
                "username": USER, "password": PW, "codec": "PCMU", "volume_note": "", "camera_ids": list(cams), "enabled": True, **kw}

    def _accept(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with self.lock:
                self.conns += 1
                n = self.conns
            threading.Thread(target=self._serve, args=(conn, n), daemon=True).start()

    def _reply(self, conn, cseq, code, headers=None, body=b""):
        reason = {200: "OK", 401: "Unauthorized"}.get(code, "Error")
        h = {"CSeq": cseq, **(headers or {}), "Content-Length": str(len(body))}
        conn.sendall((f"RTSP/1.0 {code} {reason}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n").encode() + body)

    def _serve(self, conn, n):
        buf = b""
        with conn:
            while True:
                if buf[:1] == b"$" and len(buf) >= 4 and len(buf) >= 4 + int.from_bytes(buf[2:4], "big"):
                    ln = int.from_bytes(buf[2:4], "big")
                    self.rtp.append({"conn": n, "channel": buf[1], "pkt": buf[4:4 + ln], "t": time.monotonic()})
                    buf = buf[4 + ln:]
                    continue
                if buf[:1] not in (b"", b"$") and b"\r\n\r\n" in buf:
                    head, buf = buf.split(b"\r\n\r\n", 1)
                    lines = head.decode().split("\r\n")
                    method, uri, _ = lines[0].split(" ")
                    hdrs = {k.strip().lower(): v.strip() for k, v in (x.split(":", 1) for x in lines[1:] if ":" in x)}
                    self.requests.append({"conn": n, "method": method, "uri": uri, "headers": hdrs, "t": time.monotonic()})
                    cseq = hdrs.get("cseq", "0")
                    auth = hdrs.get("authorization", "")
                    if not auth.startswith("Digest "):
                        self._reply(conn, cseq, 401, {"WWW-Authenticate": 'Digest realm="IPSpeaker", nonce="0a4f113b"'})
                        continue
                    p = dict(re.findall(r'(\w+)="([^"]*)"', auth))
                    ha1, ha2 = md5(f"{USER}:IPSpeaker:{PW}"), md5(f"{method}:{p.get('uri')}")
                    want = md5(f"{ha1}:0a4f113b:{ha2}")
                    if p.get("response") != want or p.get("username") != USER or p.get("uri") != uri:
                        self.bad_auth += 1
                        self._reply(conn, cseq, 401, {"WWW-Authenticate": 'Digest realm="IPSpeaker", nonce="0a4f113b"'})
                        continue
                    if method == "DESCRIBE":
                        self._reply(conn, cseq, 200, {"Content-Type": "application/sdp", "Content-Base": f"rtsp://127.0.0.1:{self.port}/main/"}, SDP.encode())
                    elif method == "SETUP":
                        self._reply(conn, cseq, 200, {"Session": f"5A1B{n};timeout=60", "Transport": "RTP/AVP/TCP;unicast;interleaved=0-1;ssrc=0"})
                    elif method == "TEARDOWN":
                        self._reply(conn, cseq, 200, {"Session": f"5A1B{n}"})
                        return
                    else:   # PLAY, GET_PARAMETER
                        self._reply(conn, cseq, 200, {"Session": f"5A1B{n}"})
                    continue
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk


def call(method, path, body=None, role=None, user="op@example"):
    async def go():
        tunnel = role is not None
        transport = httpx.ASGITransport(app=hub_agent.as_tunnel(app) if tunnel else app, client=hub_agent.IN_PROCESS_CLIENT if tunnel else LAN)
        headers = {"x-hub-role": role, "x-hub-user": user} if tunnel else {}
        async with httpx.AsyncClient(transport=transport, base_url="http://site") as c:
            return await c.request(method, path, json=body, headers=headers)
    return asyncio.run(go())


def tone(seconds: float, fill=0x7F) -> bytes:
    return bytes([fill]) * int(seconds * talkdown.RATE)


def wav_bytes(seconds=0.5) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x10" * int(16000 * seconds))
    return out.getvalue()


def setup():
    db.upsert_camera(CAM)
    db.upsert_camera({**CAM, "id": "cam2", "name": "Gate", "host": "10.0.0.6"})
    db.upsert_camera({**CAM, "id": "cam3", "name": "Yard", "host": "10.0.0.7"})


# ---------------------------------------------------------------- SDP, RTP, pacing

def test_backchannel_from_the_speakers_sdp():
    t = talkdown.backchannel(SDP)
    assert t["control"] == "audioback"   # the sendonly section, not the recvonly PCMU one before it
    assert [(c["pt"], c["name"], c["rate"]) for c in t["codecs"]] == [(0, "PCMU", 8000), (8, "PCMA", 8000), (97, "G726-16", 8000)]
    assert talkdown.pick_codec(t, "PCMU") == ("PCMU", 0) and talkdown.pick_codec(t, "PCMA") == ("PCMA", 8)
    assert talkdown.pick_codec({"codecs": [{"pt": 97, "name": "G726-16", "rate": 8000}]}) is None
    assert talkdown.backchannel(SDP.replace("a=sendonly", "a=recvonly")) is None
    # static payload types without an rtpmap still count
    assert talkdown.backchannel("v=0\nm=audio 0 RTP/AVP 8\na=sendonly\n")["codecs"] == [{"pt": 8, "name": "PCMA", "rate": 8000}]
    assert talkdown.track_url("rtsp://h:554/main/", "audioback") == "rtsp://h:554/main/audioback"


def test_rtp_packets():
    audio = tone(0.05)   # 400 bytes: 2 full frames + 80 bytes padded with silence
    pkts = talkdown.packets(audio, 0, 0xDEADBEEF, 65534, 2**32 - 160)
    assert len(pkts) == 3 and all(len(p) == 12 + 160 for p in pkts)
    seqs = [int.from_bytes(p[2:4], "big") for p in pkts]
    tss = [int.from_bytes(p[4:8], "big") for p in pkts]
    assert seqs == [65534, 65535, 0] and tss == [2**32 - 160, 0, 160]   # both wrap
    assert pkts[0][0] == 0x80 and pkts[0][1] == 0x80 and pkts[1][1] == 0   # version 2; marker on the first, payload type 0
    assert all(p[8:12] == bytes.fromhex("deadbeef") for p in pkts)
    assert pkts[2][12 + 80:] == b"\xff" * 80   # mu-law silence
    assert talkdown.interleaved(0, b"abc") == b"$\x00\x00\x03abc"


class FakeSession:
    def __init__(self, timeout_s=10):
        self.sent, self.keepalives, self.drains, self.channel, self.timeout_s = [], [], 0, 0, timeout_s
        self.now = 0.0

    def send(self, data):
        self.sent.append((self.now, data))

    def keepalive(self):
        self.keepalives.append(self.now)

    def drain(self):
        self.drains += 1


def test_pacing_and_keepalive_on_a_fake_clock():
    s = FakeSession(timeout_s=10)   # keep-alive every max(5, min(20, 10 / 2)) = 5 s
    stop = threading.Event()

    def wait(d):
        s.now += d
        return False
    n = talkdown.stream(s, tone(8.0), 0, stop, clock=lambda: s.now, wait=wait, ssrc=1, seq0=10, ts0=1000)
    assert n == 400 and len(s.sent) == 400
    assert all(abs(t - i * 0.02) < 1e-9 for i, (t, _) in enumerate(s.sent))   # one packet per 20 ms from the start
    assert len(s.keepalives) == 1 and 5 <= s.keepalives[0] < 5.1
    assert s.sent[0][1][:4] == b"$\x00\x00\xac"   # channel 0, 172 bytes
    # stop part way: the next wait sees it
    s2 = FakeSession()
    stop2 = threading.Event()

    def wait2(d):
        s2.now += d
        if len(s2.sent) >= 10:
            stop2.set()
        return stop2.is_set()
    assert talkdown.stream(s2, tone(2.0), 0, stop2, clock=lambda: s2.now, wait=wait2) == 10


# ---------------------------------------------------------------- against the fake speaker

def test_play_sequence_against_the_fake_speaker():
    setup()
    fake = FakeSpeaker()
    sp = fake.speaker()
    db.upsert_speaker(sp)
    t0 = time.monotonic()
    r = talkdown.manager.play(db.speaker("spk1"), talkdown.Message("Leave now", g711=tone(0.5)), "op@example")
    assert r["message"] == "Leave now" and r["replaced"] is None and r["camera_ids"] == ["cam1"]
    talkdown.manager.wait("spk1")
    elapsed = time.monotonic() - t0
    st = talkdown.manager.status("spk1")
    assert st["state"] == "idle" and st["last"]["result"] == "played", st
    methods = [(q["method"], q["uri"]) for q in fake.requests]
    base = f"rtsp://127.0.0.1:{fake.port}"
    assert methods == [("DESCRIBE", f"{base}/main"), ("DESCRIBE", f"{base}/main"), ("SETUP", f"{base}/main/audioback"),
                       ("PLAY", f"{base}/main/"), ("TEARDOWN", f"{base}/main/")], methods
    assert fake.bad_auth == 0
    for q in fake.requests:
        assert q["headers"]["require"] == talkdown.BACKCHANNEL, q   # on every request, the 401'd one too
    assert all(q["headers"]["authorization"].startswith("Digest ") for q in fake.requests[1:])
    setup_req = fake.requests[2]
    assert setup_req["headers"]["transport"] == "RTP/AVP/TCP;unicast;interleaved=0-1"
    assert fake.requests[3]["headers"]["session"] == "5A1B1" and fake.requests[4]["headers"]["session"] == "5A1B1"
    pkts = [x for x in fake.rtp if x["conn"] == 1]
    assert len(pkts) == 25 and all(x["channel"] == 0 for x in pkts)   # 0.5 s = 25 x 20 ms
    heads = [x["pkt"][:12] for x in pkts]
    assert all(len(x["pkt"]) == 12 + 160 and x["pkt"][1] & 0x7F == 0 for x in pkts)   # payload type 0 (PCMU), 160 bytes
    seqs = [int.from_bytes(h[2:4], "big") for h in heads]
    tss = [int.from_bytes(h[4:8], "big") for h in heads]
    assert all((b - a) % 65536 == 1 for a, b in zip(seqs, seqs[1:]))
    assert all((b - a) % 2**32 == 160 for a, b in zip(tss, tss[1:]))
    assert len({h[8:12] for h in heads}) == 1 and heads[0][1] & 0x80   # one SSRC, marker on the first
    assert all(x["pkt"][12:] == b"\x7f" * 160 for x in pkts)
    spread = pkts[-1]["t"] - pkts[0]["t"]
    assert 0.40 <= spread < 1.5 and elapsed >= 0.45, (spread, elapsed)   # paced in real time, not sent in a burst
    assert fake.requests[-1]["t"] >= pkts[-1]["t"]   # TEARDOWN after the audio
    row = db.one("SELECT * FROM talkdown_log ORDER BY id DESC LIMIT 1")
    assert row["speaker_id"] == "spk1" and row["message"] == "Leave now" and row["by"] == "op@example" and row["result"] == "played"
    assert db.speaker("spk1")["check"]["ok"] is True   # it answered: online


def test_g711_encoders_match_the_reference():
    # silence is each law's silence byte; full scale and its sign; values from the ITU-T G.711 tables
    assert talkdown.lin2ulaw(0) == 0xFF and talkdown.lin2alaw(0) == 0xD5
    assert talkdown.lin2ulaw(32767) == 0x80 and talkdown.lin2ulaw(-32768) == 0x00
    assert talkdown.lin2alaw(32767) == 0xAA and talkdown.lin2alaw(-32768) == 0x2A
    try:
        import audioop   # noqa: PLC0415 - gone in Python 3.13; compare where it still exists
    except ImportError:
        return
    for s in range(-32768, 32768, 7):
        b = s.to_bytes(2, "little", signed=True)
        assert talkdown.lin2ulaw(s) == audioop.lin2ulaw(b, 2)[0], s
        assert talkdown.lin2alaw(s) == audioop.lin2alaw(b, 2)[0], s


def test_wake_tone_is_faint_and_goes_first():
    assert talkdown.wake_tone("PCMU", 0) == b""
    tone_u = talkdown.wake_tone("PCMU", 500)
    assert len(tone_u) == 4000 and tone_u[0] == 0xFF and tone_u[-1] == 0xFF   # faded in and out from silence
    level = lambda b: ~b & 0x7F   # noqa: E731 - a mu-law byte's magnitude code (segment and step): larger = louder
    assert len(set(tone_u)) > 4 and max(map(level, tone_u)) <= level(talkdown.lin2ulaw(talkdown.WAKE_PEAK + 8))   # a tone, never loud
    assert talkdown.wake_tone("PCMA", 100)[0] == 0xD5
    fake = FakeSpeaker()
    db.upsert_speaker(fake.speaker("spkw"))
    talkdown.settings.talkdown_wake_ms = 200
    try:
        talkdown.manager.play(db.speaker("spkw"), talkdown.Message("Leave now", g711=tone(0.1)), "t")
        talkdown.manager.wait("spkw")
    finally:
        talkdown.settings.talkdown_wake_ms = 0
    pkts = [x["pkt"][12:] for x in fake.rtp]
    assert len(pkts) == 15   # 0.2 s of tone, then the 0.1 s message
    assert b"".join(pkts[:10]) == talkdown.wake_tone("PCMU", 200) and all(p == b"\x7f" * 160 for p in pkts[10:])


def test_pcma_preference_sends_payload_type_8():
    fake = FakeSpeaker()
    db.upsert_speaker(fake.speaker("spka", codec="PCMA"))
    talkdown.manager.play(db.speaker("spka"), talkdown.Message("A-law", g711=tone(0.1)), "t")
    talkdown.manager.wait("spka")
    assert fake.rtp and all(x["pkt"][1] & 0x7F == 8 for x in fake.rtp)


def test_one_at_a_time_replace_then_stop():
    fake = FakeSpeaker()
    db.upsert_speaker(fake.speaker("spk2"))
    sp = db.speaker("spk2")
    talkdown.manager.play(sp, talkdown.Message("Long one", g711=tone(10)), "a@example")
    for _ in range(100):
        if talkdown.manager.status("spk2")["state"] == "playing":
            break
        time.sleep(0.02)
    first = talkdown.manager._jobs["spk2"]
    r = talkdown.manager.play(sp, talkdown.Message("Police called", g711=tone(10)), "b@example")
    assert r["replaced"] == "Long one"   # a new message replaces the one playing
    first.thread.join(5)
    assert first.result["result"] == "replaced"
    for _ in range(200):
        st = talkdown.manager.status("spk2")
        if st["state"] == "playing" and st["message"] == "Police called":
            break
        time.sleep(0.02)
    assert st["message"] == "Police called" and st["by"] == "b@example" and st["duration_s"] == 10.0
    time.sleep(0.3)
    t0 = time.monotonic()
    assert talkdown.manager.stop("spk2") is True
    talkdown.manager.wait("spk2")
    assert time.monotonic() - t0 < 2
    assert talkdown.manager.status("spk2")["last"]["result"] == "stopped"
    assert talkdown.manager.stop("spk2") is False   # nothing playing
    # two sessions, one after the other: the first torn down before the second connected
    teardowns = [q for q in fake.requests if q["method"] == "TEARDOWN"]
    assert [q["conn"] for q in teardowns] == [1, 2]
    second_describe = next(q for q in fake.requests if q["conn"] == 2)
    assert second_describe["t"] >= teardowns[0]["t"]
    results = [r["result"] for r in db.all("SELECT result FROM talkdown_log WHERE speaker_id='spk2' ORDER BY id")]
    assert results == ["replaced", "stopped"]


def test_unreachable_speaker_fails_cleanly():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()   # nothing listens there
    db.upsert_speaker({**FakeSpeaker().speaker("spkx"), "rtsp_port": port})
    talkdown.manager.play(db.speaker("spkx"), talkdown.Message("x", g711=tone(0.1)), "t")
    talkdown.manager.wait("spkx")
    last = talkdown.manager.status("spkx")["last"]
    assert last["result"] == "failed" and "can't reach" in last["error"]
    chk = talkdown.check(db.speaker("spkx"), timeout=1)
    assert chk["online"] is False and chk["ok"] is False


# ---------------------------------------------------------------- routes

def test_speaker_crud_never_returns_the_password():
    setup()
    body = {"id": "spk3", "name": "Back wall", "host": "192.168.105.101", "username": USER, "password": PW, "camera_ids": ["cam1", "cam2"]}
    r = call("PUT", "/api/speakers/spk3", body)
    assert r.status_code == 200, r.text
    assert PW not in r.text and "password" not in r.json() and r.json()["camera_ids"] == ["cam1", "cam2"]
    assert r.json()["status"]["state"] == "idle" and r.json()["path"] == "/main" and r.json()["rtsp_port"] == 554
    listed = call("GET", "/api/speakers")
    assert listed.status_code == 200 and PW not in listed.text
    # omitted password = kept; bindings replaced
    r = call("PUT", "/api/speakers/spk3", {**{k: v for k, v in body.items() if k != "password"}, "camera_ids": ["cam2", "cam3", "cam2"]})
    assert r.status_code == 200 and r.json()["camera_ids"] == ["cam2", "cam3"]
    assert db.speaker("spk3")["password"] == PW
    # cameras must be this server's
    assert call("PUT", "/api/speakers/spk3", {**body, "camera_ids": ["cam9"]}).status_code == 422
    # the RTSP path can't smuggle another host
    assert call("PUT", "/api/speakers/spk3", {**body, "path": "@evil:554/x"}).status_code == 422
    assert call("PUT", "/api/speakers/spk3", {**body, "id": "other"}).status_code == 400
    assert call("DELETE", "/api/speakers/spk3").status_code == 200
    assert db.speaker("spk3") is None and call("DELETE", "/api/speakers/spk3").status_code == 404


def test_check_route_reports_the_backchannel():
    fake = FakeSpeaker()
    db.upsert_speaker(fake.speaker("spk4"))
    r = call("POST", "/api/speakers/spk4/check")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] and j["online"] and j["backchannel"] and j["codecs"] == ["PCMU", "PCMA", "G726-16"] and j["codec"] == "PCMU"
    assert [q["method"] for q in fake.requests] == ["DESCRIBE", "DESCRIBE", "TEARDOWN"]
    assert all(q["headers"]["require"] == talkdown.BACKCHANNEL for q in fake.requests)
    assert db.speaker("spk4")["check"]["codecs"] == ["PCMU", "PCMA", "G726-16"]


def test_roles_through_the_tunnel():
    fake = FakeSpeaker()
    db.upsert_speaker(fake.speaker("spk5"))
    talkdown._tts["engine"] = None
    m = talkdown.messages()[0]
    body = {"message_id": m["id"]}
    assert call("POST", "/api/speakers/spk5/play", body, role="viewer").status_code == 403
    assert call("POST", "/api/speakers/spk5/stop", role="viewer").status_code == 403
    assert call("PUT", "/api/speakers/spk5", {"id": "spk5", "name": "x", "host": "10.0.0.9"}, role="operator").status_code == 403
    assert call("POST", "/api/speakers/spk5/check", role="operator").status_code == 403
    assert call("POST", "/api/talkdown/messages", {"name": "x", "text": "hi"}, role="operator").status_code == 403
    assert call("POST", "/api/cameras/cam1/to-speaker", {}, role="operator").status_code == 403
    # an operator may play: a TTS message without an engine says so
    r = call("POST", "/api/speakers/spk5/play", body, role="operator")
    assert r.status_code == 409 and "text-to-speech" in r.text
    up = {"upload": {"filename": "x.wav", "data_b64": base64.b64encode(b"RIFF....").decode()}}
    r = call("POST", "/api/speakers/spk5/play", {"text": "hello"}, role="operator")
    assert r.status_code == 409
    assert call("POST", "/api/speakers/spk5/play", {**up, "text": "hello"}, role="operator").status_code == 422   # exactly one
    assert call("POST", "/api/speakers/spk5/play", {"upload": {"filename": "x.exe", "data_b64": "AAAA"}}, role="operator").status_code == 422
    # who played it is the hub user
    talkdown._tts["engine"] = None
    r = call("POST", "/api/speakers/spk5/stop", role="operator")
    assert r.status_code == 200 and r.json()["stopped"] is False


def test_summary_lists_speakers_without_credentials():
    setup()
    fake = FakeSpeaker()
    db.upsert_speaker(fake.speaker("spk6", cams=("cam1", "cam3")))
    db.upsert_speaker({**fake.speaker("spk7"), "enabled": False})
    s = talkdown.summary_speakers()
    one = next(x for x in s if x["id"] == "spk6")
    assert set(one) == {"id", "name", "camera_ids", "online", "ok", "checked_at", "playing"}
    assert one["camera_ids"] == ["cam1", "cam3"] and one["playing"] is None
    assert not any(x["id"] == "spk7" for x in s)   # turned off: not offered on the hub
    text = repr(s)
    assert PW not in text and "127.0.0.1" not in text and USER not in text


def test_camera_with_two_speakers_all_speakers_play_and_stop():
    """The panel's "All speakers": the same message on every speaker bound to the camera, each one at a time, then Stop on each."""
    setup()
    a, b = FakeSpeaker(), FakeSpeaker()
    db.upsert_speaker(a.speaker("spk8", cams=("cam1", "cam2")))
    db.upsert_speaker(b.speaker("spk9", cams=("cam1",)))
    bound = [s["id"] for s in call("GET", "/api/speakers").json() if "cam1" in s["camera_ids"]]
    assert {"spk8", "spk9"} <= set(bound)
    assert [s["id"] for s in call("GET", "/api/speakers").json() if "cam2" in s["camera_ids"]] == ["spk8"]
    path = talkdown.talkdown_dir() / "long.raw"
    orig = talkdown.to_g711
    talkdown.to_g711 = lambda src, codec="PCMU": tone(10)   # no ffmpeg needed: a 10 s message
    try:
        mid = db.execute_insert("INSERT INTO talkdown_messages (name, file, duration_s, created_at) VALUES (?,?,?,?)", ["Long", path.name, 10, time.time()])
        path.write_bytes(b"x")
        for sid in ("spk8", "spk9"):
            r = call("POST", f"/api/speakers/{sid}/play", {"message_id": mid}, role="operator", user="soc@example")
            assert r.status_code == 200, r.text
            assert r.json()["message"] == "Long" and r.json()["speaker_id"] == sid
        for _ in range(200):
            states = {s["id"]: s["status"] for s in call("GET", "/api/speakers").json()}
            if all(states[x]["state"] == "playing" for x in ("spk8", "spk9")):
                break
            time.sleep(0.02)
        assert all(states[x]["message"] == "Long" and states[x]["by"] == "soc@example" for x in ("spk8", "spk9")), states
        assert {x["playing"] for x in talkdown.summary_speakers() if x["id"] in ("spk8", "spk9")} == {"Long"}
        for sid in ("spk8", "spk9"):
            assert call("POST", f"/api/speakers/{sid}/stop", role="operator").json()["stopped"] is True
        for sid in ("spk8", "spk9"):
            talkdown.manager.wait(sid)
            assert call("GET", f"/api/speakers/{sid}/status").json()["last"]["result"] == "stopped"
        assert a.rtp and b.rtp and {q["method"] for q in a.requests} >= {"SETUP", "PLAY", "TEARDOWN"}
        assert path.is_file()   # a library message's file stays
    finally:
        talkdown.to_g711 = orig


def test_message_library_and_tts_flag():
    talkdown._tts["engine"] = None
    r = call("GET", "/api/talkdown/messages")
    j = r.json()
    names = [m["name"] for m in j["messages"]]
    assert {"Leave now", "Police called", "Monitored site"} <= set(names)
    assert j["tts"] == {"available": False, "engine": None} and j["max_s"] == 120
    leave = next(m for m in j["messages"] if m["name"] == "Leave now")
    assert leave["kind"] == "tts" and leave["available"] is False and "private property" in leave["text"]
    # defaults are seeded once: a deleted one stays deleted
    assert call("DELETE", f"/api/talkdown/messages/{leave['id']}").status_code == 200
    assert "Leave now" not in [m["name"] for m in call("GET", "/api/talkdown/messages").json()["messages"]]
    talkdown._tts["engine"] = "sapi"   # as if the engine were there: TTS messages become playable
    assert all(m["available"] for m in call("GET", "/api/talkdown/messages").json()["messages"])
    r = call("POST", "/api/talkdown/messages", {"name": "Closed", "text": "  The site is   closed. "})
    assert r.status_code == 200 and r.json()["text"] == "The site is closed." and r.json()["kind"] == "tts"
    assert call("PUT", f"/api/talkdown/messages/{r.json()['id']}", {"name": "Site closed"}).json()["name"] == "Site closed"
    big = base64.b64encode(b"\0" * (talkdown.MAX_UPLOAD + 1)).decode()
    assert call("POST", "/api/talkdown/messages", {"name": "big", "upload": {"filename": "a.mp3", "data_b64": big}}).status_code == 422
    assert call("POST", "/api/talkdown/messages", {"name": "bad", "upload": {"filename": "a.txt", "data_b64": "AAAA"}}).status_code == 422
    talkdown._tts["engine"] = None


def test_upload_converts_with_ffmpeg():
    if not Path(talkdown.ffmpeg_exe()).exists() and not shutil.which("ffmpeg"):
        print("  (skipped: no ffmpeg)")
        return
    data = wav_bytes(0.5)
    r = call("POST", "/api/talkdown/messages", {"name": "Chime", "upload": {"filename": "chime.wav", "data_b64": base64.b64encode(data).decode()}})
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["kind"] == "file" and 0.4 <= m["duration_s"] <= 0.6
    stored = db.one("SELECT file FROM talkdown_messages WHERE id=?", [m["id"]])["file"]
    assert (talkdown.talkdown_dir() / stored).is_file()
    g = talkdown.to_g711(talkdown.talkdown_dir() / stored, "PCMA")
    assert abs(len(g) - 4000) <= 160   # 0.5 s at 8 kHz, one byte a sample
    # not audio: refused, nothing kept
    before = set(talkdown.talkdown_dir().iterdir())
    r = call("POST", "/api/talkdown/messages", {"name": "junk", "upload": {"filename": "junk.mp3", "data_b64": base64.b64encode(b"not audio" * 10).decode()}})
    assert r.status_code == 422 and set(talkdown.talkdown_dir().iterdir()) == before
    # a one-off upload plays and its file is removed afterwards
    fake = FakeSpeaker()
    db.upsert_speaker(fake.speaker("spk10"))
    r = call("POST", "/api/speakers/spk10/play", {"upload": {"filename": "oneoff.wav", "data_b64": base64.b64encode(data).decode()}})
    assert r.status_code == 200 and r.json()["message"] == "oneoff.wav", r.text
    talkdown.manager.wait("spk10")
    assert talkdown.manager.status("spk10")["last"]["result"] == "played"
    assert 20 <= len(fake.rtp) <= 30
    assert not list((talkdown.talkdown_dir() / "oneoff").iterdir())
    m = talkdown.delete_message(m["id"])
    assert m and not (talkdown.talkdown_dir() / stored).exists()


def test_camera_to_speaker_copies_the_login_server_side():
    setup()
    db.upsert_camera({**CAM, "id": "cam7", "name": "Speaker Back Wall", "host": "192.168.105.75", "username": "admin", "password": PW})
    orig = api.sync_cameras
    api.sync_cameras = lambda: None   # no MediaMTX here
    try:
        r = call("POST", "/api/cameras/cam7/to-speaker", {"speaker_id": "spk11", "host": "192.168.105.101", "camera_ids": ["cam1", "cam2"]})
        assert r.status_code == 200, r.text
        j = r.json()
        assert PW not in r.text and j["id"] == "spk11" and j["name"] == "Speaker Back Wall" and j["host"] == "192.168.105.101"
        assert j["camera_ids"] == ["cam1", "cam2"] and j["path"] == "/main" and j["username"] == "admin"
        sp = db.speaker("spk11")
        assert sp["password"] == PW and sp["rtsp_port"] == 554 and sp["onvif_port"] == 80
        assert db.one("SELECT enabled FROM cameras WHERE id='cam7'")["enabled"] == 0   # disabled, kept
        assert call("POST", "/api/cameras/cam7/to-speaker", {"speaker_id": "spk11"}).status_code == 409
        # no bindings, the next free id
        r = call("POST", "/api/cameras/cam7/to-speaker", {"camera_ids": []})
        assert r.status_code == 200 and re.fullmatch(r"spk\d+", r.json()["id"]) and r.json()["camera_ids"] == []
        assert call("POST", "/api/cameras/nope/to-speaker", {}).status_code == 404
        assert call("POST", "/api/cameras/cam7/to-speaker", {"camera_ids": ["cam99"]}).status_code == 422
    finally:
        api.sync_cameras = orig


def test_config_export_import_keeps_passwords_out():
    setup()
    fake = FakeSpeaker()
    db.upsert_speaker(fake.speaker("spk12", cams=("cam1", "cam2")))
    exp = siteconfig.export_config()
    mine = next(s for s in exp["speakers"] if s["id"] == "spk12")
    assert "password" not in mine and mine["camera_ids"] == ["cam1", "cam2"] and PW not in repr(exp["speakers"])
    mine["name"] = "Renamed"
    counts = siteconfig.import_config(exp)
    assert counts["speakers"] == len(exp["speakers"])
    assert db.speaker("spk12")["name"] == "Renamed" and db.speaker("spk12")["password"] == PW   # stored password kept
    bad = {**exp, "speakers": [{**mine, "path": "@evil/x"}]}
    try:
        siteconfig.import_config(bad)
        raise AssertionError("an unsafe speaker path must be refused")
    except ValueError:
        pass
    assert db.speaker("spk12")["path"] == "/main"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    talkdown.manager.stop_all()
    print("all passed")
