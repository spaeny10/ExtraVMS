"""Playback transcodes per camera: starting one more than the cap stops the oldest (api._ffmpeg_stream cap).

Run: ..\.venv\Scripts\python.exe tests\test_playback_cap.py   (from backend/)
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-playcap-test-")   # never the real DB
os.environ["NVR_RECORDINGS_DIR"] = tempfile.mkdtemp(prefix="nvr-playcap-rec-")
os.environ["NVR_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="nvr-playcap-run-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import api  # noqa: E402

# a stand-in for ffmpeg: writes a little forever, like a transcode whose reader stalls
SLOW = [sys.executable, "-c", "import sys,time\nwhile True:\n    sys.stdout.buffer.write(b'x'*1024); sys.stdout.flush(); time.sleep(0.05)"]


def test_oldest_transcode_is_stopped_past_the_cap():
    async def run():
        closed = []
        responses = []
        for i in range(4):
            responses.append(await api._ffmpeg_stream("camx", SLOW, on_close=lambda i=i: closed.append(i), cap=3))
        time.sleep(0.3)
        assert closed == [0], closed                      # the first one was stopped when the fourth started
        assert len(api._running["camx"]) == 3
        other = await api._ffmpeg_stream("camy", SLOW, cap=3)   # another camera has its own budget
        assert len(api._running["camy"]) == 1 and closed == [0]
        for r in responses + [other]:                    # the client reads, then goes away: all stop and are forgotten
            await r.body_iterator.__anext__()
            await r.body_iterator.aclose()
        assert sorted(closed) == [0, 1, 2, 3]
        assert not api._running["camx"] and not api._running["camy"]
    asyncio.run(run())


def test_without_a_cap_nothing_is_tracked():
    async def run():
        r = await api._ffmpeg_stream("camz", SLOW)
        assert "camz" not in api._running
        await r.body_iterator.__anext__()
        await r.body_iterator.aclose()
    asyncio.run(run())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
