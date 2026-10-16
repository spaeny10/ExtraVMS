"""The agent's latest-picture sender (hub_agent.send_still / _still_loop / _still_on_event): off at interval 0 and for a
hub that didn't ask, the size cap, an unchanged frame not resent, a failing camera skipped, one still per event burst;
still_jpeg's stale check and re-encode. Fakes only: no hub, no recordings, no PyAV decode.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_hub_stills.py   (from backend/)
"""
import asyncio
import base64
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-stills-test-")  # never the real DB
os.environ["NVR_RECORDINGS_DIR"] = tempfile.mkdtemp(prefix="nvr-stills-rec-")
os.environ["NVR_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="nvr-stills-runtime-")
os.environ["NVR_HUB_URL"] = "ws://127.0.0.1:0/agent"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tunnelproto"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from nvr import frames, hub_agent  # noqa: E402
from nvr.config import settings  # noqa: E402
from tunnelproto import decode  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 2000


class FakeWs:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(decode(raw))


def _agent(still_fn, stills=True):
    agent = hub_agent.HubAgent(None, SimpleNamespace(pipeline=SimpleNamespace(subscribers=set())), still_fn=still_fn)
    agent._ws = FakeWs()
    agent._hub_stills = stills
    return agent


def _stills(agent):
    return [f for f in agent._ws.sent if f.get("t") == "still"]


def test_off_at_interval_zero_and_for_old_hubs():
    calls = []
    fn = lambda cam, now: calls.append(cam) or (JPEG, now)  # noqa: E731

    async def run():
        settings.hub_still_interval = 0
        a = _agent(fn)
        assert await a.send_still("front") is False
        await asyncio.wait_for(a._still_loop(), 1)   # returns at once: nothing scheduled
        a._still_on_event({"type": "event", "event": {"id": 1, "camera_id": "front", "status": "open"}})
        settings.hub_still_interval = 60
        b = _agent(fn, stills=False)                  # the hub's welcome had no "stills"
        assert await b.send_still("front") is False
        await asyncio.wait_for(b._still_loop(), 1)
        assert not _stills(a) and not _stills(b) and not calls and not a._still_tasks
    try:
        asyncio.run(run())
    finally:
        settings.hub_still_interval = 60


def test_sends_caps_and_skips():
    ts = time.time() - 2

    def fn(cam, now):
        if cam == "broken":
            raise RuntimeError("decode failed")
        if cam == "huge":
            return b"\xff\xd8" + b"x" * (hub_agent.STILL_MAX + 1), ts
        if cam == "dark":
            return None
        return JPEG, ts

    async def run():
        a = _agent(fn)
        assert await a.send_still("broken") is False     # a failing camera is skipped...
        assert await a.send_still("front") is True       # ...and the next one still goes
        assert await a.send_still("huge") is False       # over the cap: never sent
        assert await a.send_still("dark") is False       # no recent frame
        assert await a.send_still("front") is False      # the same frame again: not resent
        got = _stills(a)
        assert len(got) == 1 and got[0]["cam"] == "front" and abs(got[0]["ts"] - ts) < 0.01
        assert base64.b64decode(got[0]["jpeg"]) == JPEG
    asyncio.run(run())


def test_event_opening_sends_one_still_per_burst():
    n = [0]

    def fn(cam, now):
        n[0] += 1
        return JPEG, now - n[0]     # a new frame every call

    async def run():
        a = _agent(fn)
        ev = lambda i, status="open", cam="door": {"type": "event", "event": {"id": i, "camera_id": cam, "status": status}}  # noqa: E731
        a._still_on_event(ev(1))
        a._still_on_event(ev(2))                 # same camera within STILL_EVENT_GAP_S: no second still
        a._still_on_event(ev(3, "pending"))      # an update, not an opening
        a._still_on_event({"type": "briefing"})
        a._still_on_event(ev(4, cam="yard"))
        await asyncio.sleep(0.2)
        assert sorted(f["cam"] for f in _stills(a)) == ["door", "yard"], _stills(a)
        a._still_sent["door"] = time.time() - hub_agent.STILL_EVENT_GAP_S - 1
        a._still_on_event(ev(1))                 # an id already seen open: nothing
        a._still_on_event(ev(5))                 # the gap has passed: a new one
        await asyncio.sleep(0.2)
        assert [f["cam"] for f in _stills(a)].count("door") == 2
    asyncio.run(run())


def test_still_jpeg_stale_and_reencode():
    real = frames.preview_jpeg
    noise = np.random.default_rng(7).integers(0, 256, (480, 640, 3), dtype=np.uint8)
    big = cv2.imencode(".jpg", noise, [cv2.IMWRITE_JPEG_QUALITY, 98])[1].tobytes()
    assert len(big) > hub_agent.STILL_MAX
    now = time.time()
    try:
        frames.preview_jpeg = lambda cam, t, w, exact: (JPEG, now - 3, True)
        assert hub_agent.still_jpeg("front", now) == (JPEG, now - 3)
        frames.preview_jpeg = lambda cam, t, w, exact: (JPEG, now - hub_agent.STILL_STALE_S - 5, True)
        assert hub_agent.still_jpeg("front", now) is None          # the camera stopped recording a while ago
        frames.preview_jpeg = lambda cam, t, w, exact: None
        assert hub_agent.still_jpeg("front", now) is None
        frames.preview_jpeg = lambda cam, t, w, exact: (big, now - 1, True)
        got = hub_agent.still_jpeg("front", now)        # 477 KB at q98: re-encoded at q50 (~126 KB)
        assert got and len(got[0]) <= hub_agent.STILL_MAX and got[0][:2] == b"\xff\xd8" and got[1] == now - 1
    finally:
        frames.preview_jpeg = real


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
