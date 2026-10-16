"""Live talk (Hold to Talk: talkdown.LiveFeed / live(), the WebSocket /api/speakers/<id>/talk): the browser's 16-bit PCM
in 20 ms frames, encoded in the speaker's law and forwarded in order after the wake tone, silence bridging a gap, a
backlog trimmed at the pauses, the end on release (played out), after 1.5 s without audio and at the 60 s cap; against
the fake speaker: one session, the log row, replacing a message and being replaced; the socket's roles and guards.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_talkdown_live.py   (from backend/)
"""
import math
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_talkdown import FakeSession, FakeSpeaker, setup, tone  # noqa: E402 - sets the temp data dir before nvr loads

from starlette.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from nvr import hub_agent, talkdown  # noqa: E402
from nvr.api import app  # noqa: E402
from nvr.db import db  # noqa: E402

FRAME = talkdown.LIVE_PCM_FRAME


def pcm(samples) -> bytes:
    return b"".join(int(s).to_bytes(2, "little", signed=True) for s in samples)


def voiced(n: int, amp=8000) -> bytes:
    """Frame n: a tone whose level says which frame it was (each one encodes differently)."""
    return pcm(round((amp + 97 * n) * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(160))


SILENT = pcm([0] * 160)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def payloads(sess) -> list[bytes]:
    return [d[4 + 12:] for _, d in sess.sent]


def headers(sess) -> list[bytes]:
    return [d[4:4 + 12] for _, d in sess.sent]


def run_live(feed, clock, events=None, lead=b"", **kw):
    """live() on the fake clock; `events` maps a packet count to a function run before that packet's wait."""
    s = FakeSession(timeout_s=60)
    s.now = clock.now
    stop = threading.Event()

    def wait(d):
        clock.now += d
        s.now = clock.now
        fn = (events or {}).pop(len(s.sent), None)
        if fn:
            fn()
        return stop.is_set()
    why = talkdown.live(s, feed, "PCMU", 0, stop, lead=lead, clock=clock, wait=wait, ssrc=7, seq0=65530, ts0=2**32 - 320, **kw)
    return why, s


# ---------------------------------------------------------------- encoding and the feed

def test_pcm_to_g711_matches_the_sample_encoders():
    samples = [0, 1, -1, 100, -100, 330, 8159 * 4, 32767, -32768, 12345, -12345]
    raw = pcm(samples)
    assert talkdown.pcm_to_g711(raw, "PCMU") == bytes(talkdown.lin2ulaw(s) for s in samples)
    assert talkdown.pcm_to_g711(raw, "PCMA") == bytes(talkdown.lin2alaw(s) for s in samples)
    assert talkdown.pcm_to_g711(raw + b"\x01", "PCMU") == talkdown.pcm_to_g711(raw, "PCMU")   # a stray odd byte is dropped
    assert talkdown.quiet(SILENT) and talkdown.quiet(pcm([300, -329] * 80)) and not talkdown.quiet(pcm([0] * 159 + [-331]))


def test_feed_cuts_any_chunking_into_20_ms_frames():
    clock = Clock()
    f = talkdown.LiveFeed(clock)
    data = voiced(1) + voiced(2) + voiced(3)
    for i in range(0, len(data), 77):   # odd sizes, a sample split across messages
        f.push(data[i:i + 77])
    assert f.received == 3 and f.backlog() == 3
    assert [f.take() for _ in range(4)] == [voiced(1), voiced(2), voiced(3), None]
    f.push(voiced(4)[:100])
    assert f.backlog() == 0 and f.take() is None   # half a frame waits for the rest
    f.push(voiced(4)[100:])
    assert f.take() == voiced(4)
    # idle: nothing buffered and nothing arrived for 1.5 s
    assert not f.idle()
    clock.now += 1.6
    assert f.idle()
    f.push(voiced(5))
    assert not f.idle()
    # released: what is buffered still plays, then it is done; later audio is ignored
    f.end()
    assert not f.done() and f.take() == voiced(5) and f.done()
    f.push(voiced(6))
    assert f.backlog() == 0


def test_feed_trims_a_backlog_at_the_pauses_and_caps_it():
    f = talkdown.LiveFeed(Clock())
    for n in range(4):
        f.push(voiced(n))
    for _ in range(10):
        f.push(SILENT)   # the operator paused while the speaker connected
    f.push(voiced(9))
    got = [f.take() for _ in range(5)]
    assert got[:4] == [voiced(n) for n in range(4)]   # speech is never skipped
    # the pause is cut down to LIVE_TARGET frames in all before the next words
    rest = []
    while (x := f.take()) is not None:
        rest.append(x)
    assert rest[-1] == voiced(9) and len(got) + len(rest) < 15 and f.dropped >= 10 - talkdown.LIVE_TARGET
    g = talkdown.LiveFeed(Clock())
    for n in range(talkdown.LIVE_MAX_BACKLOG + 20):
        g.push(voiced(n))
    assert g.backlog() == talkdown.LIVE_MAX_BACKLOG and g.take() == voiced(20)   # the oldest went


# ---------------------------------------------------------------- live() on a fake clock

def test_live_forwards_frames_in_order_after_the_wake_tone():
    clock = Clock()
    f = talkdown.LiveFeed(clock)
    for n in range(3):
        f.push(voiced(n))
    f.end()   # a short tap: three frames, then let go
    lead = talkdown.wake_tone("PCMU", 100)   # 5 frames of tone
    why, s = run_live(f, clock, lead=lead)
    assert why == "released"
    p = payloads(s)
    assert len(p) == 8 and b"".join(p[:5]) == lead
    assert p[5:] == [talkdown.pcm_to_g711(voiced(n), "PCMU") for n in range(3)]
    hs = headers(s)
    seqs = [int.from_bytes(h[2:4], "big") for h in hs]
    tss = [int.from_bytes(h[4:8], "big") for h in hs]
    assert all((b - a) % 65536 == 1 for a, b in zip(seqs, seqs[1:])) and seqs[0] == 65530   # one run through the wrap
    assert all((b - a) % 2**32 == 160 for a, b in zip(tss, tss[1:]))
    assert hs[0][1] & 0x80 and not any(h[1] & 0x80 for h in hs[1:]) and {h[8:12] for h in hs} == {b"\0\0\0\x07"}
    assert all(abs(t - (100 + i * 0.02)) < 1e-9 for i, (t, _) in enumerate(s.sent))   # paced at 20 ms


def test_live_bridges_a_gap_with_silence_then_ends_idle():
    clock = Clock()
    f = talkdown.LiveFeed(clock)
    f.push(voiced(0) + voiced(1))
    # after packet 6 (a gap of 4 x 20 ms: the network stalled), two more frames arrive; then nothing at all
    why, s = run_live(f, clock, events={6: lambda: f.push(voiced(2) + voiced(3))})
    assert why == "idle"
    p = payloads(s)
    silence = b"\xff" * 160
    enc = [talkdown.pcm_to_g711(voiced(n), "PCMU") for n in range(4)]
    assert p[:2] == enc[:2] and p[2:6] == [silence] * 4 and p[6:8] == enc[2:]
    assert all(x == silence for x in p[8:])
    # idle counts from the last audio: 1.5 s of bridged silence after it, give or take a packet
    assert 74 <= len(p) - 8 <= 77, len(p)


def test_live_stops_at_the_cap_and_on_stop():
    clock = Clock()
    f = talkdown.LiveFeed(clock)
    s = FakeSession(timeout_s=60)
    stop = threading.Event()

    def wait(d):   # the operator never lets go: audio keeps coming
        clock.now += d
        s.now = clock.now
        f.push(voiced(1))
        return False
    assert talkdown.live(s, f, "PCMA", 8, stop, clock=clock, wait=wait, max_s=talkdown.LIVE_MAX_S) == "cap"
    assert len(s.sent) == talkdown.LIVE_MAX_S * 50 and s.keepalives   # 60 s of 20 ms packets, kept alive
    assert all(d[5] & 0x7F == 8 for _, d in s.sent)   # PCMA
    clock2 = Clock()
    f2 = talkdown.LiveFeed(clock2)
    f2.push(voiced(1) * 1)
    stop2 = threading.Event()
    stop2.set()
    assert talkdown.live(FakeSession(), f2, "PCMU", 0, stop2, clock=clock2, wait=lambda d: True) == "stopped"


# ---------------------------------------------------------------- against the fake speaker

def wait_state(sid, state, message=None, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = talkdown.manager.status(sid)
        if st["state"] == state and (message is None or st["message"] == message):
            return st
        time.sleep(0.01)
    raise AssertionError(f"{sid} never reached {state} {message}: {talkdown.manager.status(sid)}")


def test_live_talk_on_the_fake_speaker_is_logged():
    setup()
    fake = FakeSpeaker()
    db.upsert_speaker(fake.speaker("spl1"))
    job = talkdown.manager.talk(db.speaker("spl1"), "op@example")
    assert job.message.name == "Live talk" and job.replacing is None
    st = wait_state("spl1", "playing")
    assert st["message"] == "Live talk" and st["live"] is True and st["duration_s"] is None and st["by"] == "op@example"
    frames = [voiced(n) for n in range(10)]
    for fr in frames:   # in real time, as the browser sends them
        job.feed.push(fr)
        time.sleep(0.02)
    job.feed.end()
    talkdown.manager.wait("spl1")
    last = talkdown.manager.status("spl1")["last"]
    assert last["result"] == "played" and last["reason"] == "released" and last["message"] == "Live talk", last
    got = [x["pkt"][12:] for x in fake.rtp]
    enc = [talkdown.pcm_to_g711(fr, "PCMU") for fr in frames]
    voice = [p for p in got if p != b"\xff" * 160]
    assert voice == enc   # every frame, in order (silence only where the sender was late)
    assert [q["method"] for q in fake.requests][-3:] == ["SETUP", "PLAY", "TEARDOWN"]
    row = db.one("SELECT * FROM talkdown_log WHERE speaker_id='spl1' ORDER BY id DESC LIMIT 1")
    assert row["message"] == "Live talk" and row["by"] == "op@example" and row["result"] == "played" and row["text"] is None
    assert 0.1 <= row["duration_s"] <= 3


def test_live_talk_replaces_a_message_and_is_replaced():
    fake = FakeSpeaker()
    db.upsert_speaker(fake.speaker("spl2"))
    sp = db.speaker("spl2")
    talkdown.manager.play(sp, talkdown.Message("Long one", g711=tone(10)), "a@example")
    wait_state("spl2", "playing", "Long one")
    first = talkdown.manager._jobs["spl2"]
    job = talkdown.manager.talk(sp, "b@example")
    assert job.replacing == "Long one"
    first.thread.join(5)
    assert first.result["result"] == "replaced"
    wait_state("spl2", "playing", "Live talk")
    job.feed.push(voiced(1))
    # a message played meanwhile replaces the live talk (the operator's latest wins either way)
    r = talkdown.manager.play(sp, talkdown.Message("Police called", g711=tone(0.2)), "c@example")
    assert r["replaced"] == "Live talk"
    job.thread.join(5)
    assert job.result["result"] == "replaced" and job.feed.ended
    talkdown.manager.wait("spl2")
    # Stop ends a live talk too
    job = talkdown.manager.talk(sp, "d@example")
    wait_state("spl2", "playing", "Live talk")
    assert talkdown.manager.stop("spl2") is True
    talkdown.manager.wait("spl2")
    assert talkdown.manager.status("spl2")["last"]["result"] == "stopped"
    results = [(x["message"], x["result"]) for x in db.all("SELECT message, result FROM talkdown_log WHERE speaker_id='spl2' ORDER BY id")]
    assert results == [("Long one", "replaced"), ("Live talk", "replaced"), ("Police called", "played"), ("Live talk", "stopped")]
    teardowns = [q["conn"] for q in fake.requests if q["method"] == "TEARDOWN"]
    assert len(teardowns) == 4   # every session torn down, one at a time


# ---------------------------------------------------------------- the WebSocket

def ws_client(tunnel: bool) -> TestClient:
    return TestClient(hub_agent.as_tunnel(app) if tunnel else app, base_url="http://site")   # sockets: Host is passed per test


def closed_with(client, path, **kw) -> int:
    """A socket refused before it opened: its close code."""
    try:
        with client.websocket_connect(path, **kw) as sock:
            sock.receive_json()
    except WebSocketDisconnect as e:
        return e.code
    raise AssertionError("the socket was accepted")


def refused(client, path, **kw) -> tuple[str, int]:
    """A socket that opened and then refused the talk: (why, close code)."""
    with client.websocket_connect(path, **kw) as sock:
        m = sock.receive_json()
        assert m["type"] == "talk" and m["state"] == "refused", m
        try:
            sock.receive_json()
        except WebSocketDisconnect as e:
            return m["error"], e.code
    raise AssertionError("not closed")


def test_talk_socket_roles_and_guards():
    fake = FakeSpeaker()
    db.upsert_speaker(fake.speaker("spl3"))
    db.upsert_speaker({**fake.speaker("spl4"), "enabled": False})
    hub = ws_client(True)
    assert refused(hub, "/api/speakers/spl3/talk", headers={"x-hub-role": "viewer", "x-hub-user": "v@example"}) == ("needs the operator role", 4403)
    assert refused(hub, "/api/speakers/nope/talk", headers={"x-hub-role": "operator"}) == ("unknown speaker", 4404)
    assert refused(hub, "/api/speakers/spl4/talk", headers={"x-hub-role": "operator"}) == ("the speaker is turned off in its settings", 4409)
    lan = ws_client(False)
    # a page elsewhere (a Direct-on-LAN hub page included: direct is read-only) may not talk; x-hub-* headers count for nothing here
    assert closed_with(lan, "/api/speakers/spl3/talk", headers={"host": "site", "origin": "https://hub.example", "x-hub-role": "owner"}) == 4403
    assert closed_with(lan, "/api/speakers/spl3/talk", headers={"host": "evil.example", "origin": "http://evil.example"}) == 4403   # DNS rebinding
    assert fake.requests == []   # nothing reached the speaker
    # this server's own page: allowed, by "local"
    with lan.websocket_connect("/api/speakers/spl3/talk", headers={"host": "site", "origin": "http://site"}) as sock:
        assert sock.receive_json()["state"] == "connecting"
        sock.send_bytes(voiced(1))
        assert sock.receive_json() == {"type": "talk", "state": "live"}
        sock.send_text('{"type": "end"}')
        end = sock.receive_json()
        assert end["state"] == "ended" and end["result"] == "played" and end["reason"] == "released", end
    assert db.one("SELECT by FROM talkdown_log WHERE speaker_id='spl3' ORDER BY id DESC LIMIT 1")["by"] == "local"


def test_talk_socket_through_the_tunnel():
    fake = FakeSpeaker()
    db.upsert_speaker(fake.speaker("spl5", cams=("cam1", "cam2")))
    talkdown.manager.play(db.speaker("spl5"), talkdown.Message("Leave now", g711=tone(10)), "a@example")
    wait_state("spl5", "playing", "Leave now")
    hub = ws_client(True)
    with hub.websocket_connect("/api/speakers/spl5/talk", headers={"x-hub-role": "operator", "x-hub-user": "op@talk.example"}) as sock:
        hello = sock.receive_json()
        assert hello == {"type": "talk", "state": "connecting", "speaker_id": "spl5", "speaker": "Speaker spl5",
                         "camera_ids": ["cam1", "cam2"], "replaced": "Leave now"}
        for n in range(5):
            sock.send_bytes(voiced(n) + voiced(n + 100))   # 40 ms a message, as the panel sends
        assert sock.receive_json()["state"] == "live"
        assert talkdown.manager.status("spl5")["by"] == "op@talk.example"
    # the page went without saying "end": released all the same, what was sent plays out
    talkdown.manager.wait("spl5")
    last = talkdown.manager.status("spl5")["last"]
    assert last["message"] == "Live talk" and last["result"] == "played" and last["reason"] == "released", last
    voice = [x["pkt"][12:] for x in fake.rtp if x["conn"] == 2 and x["pkt"][12:] != b"\xff" * 160]
    want = [talkdown.pcm_to_g711(fr, "PCMU") for n in range(5) for fr in (voiced(n), voiced(n + 100))]
    assert voice == want
    row = db.one("SELECT * FROM talkdown_log WHERE speaker_id='spl5' ORDER BY id DESC LIMIT 1")
    assert row["message"] == "Live talk" and row["by"] == "op@talk.example"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    talkdown.manager.stop_all()
    print("all passed")
