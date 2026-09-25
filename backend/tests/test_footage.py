"""Footage index tests: moment grouping and pruning with a throwaway recordings folder and index.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_footage.py   (from backend/)
"""
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="nvr-footage-test-"))
os.environ["NVR_DATA_DIR"] = str(TMP / "data")                  # never the real DB
os.environ["NVR_RECORDINGS_DIR"] = str(TMP / "recordings")      # never the real recordings
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from nvr import footage  # noqa: E402
from nvr.clip import DIM  # noqa: E402


def test_moments():
    hits = [("cam1", 100, 0, 0.30), ("cam1", 110, 2, 0.35), ("cam1", 125, 0, 0.20),   # one moment (gaps <= 20 s)
            ("cam1", 200, 1, 0.25),                                                     # separate
            ("cam2", 105, 0, 0.40)]
    ms = footage.moments(hits)
    assert [(m["camera_id"], m["start"], m["end"]) for m in ms] == [("cam2", 105, 105), ("cam1", 100, 125), ("cam1", 200, 200)], ms
    assert ms[1]["ts"] == 110 and ms[1]["tile"] == 2 and ms[1]["hits"] == 3 and ms[1]["box"] == footage.TILES[2]


def _seg(cam: str, start: float, end: float) -> Path:
    d = TMP / "recordings" / cam
    d.mkdir(parents=True, exist_ok=True)
    f = d / (time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(start)) + "-000000.mp4")
    f.write_bytes(b"")
    os.utime(f, (end, end))
    return f


def test_prune_keeps_existing_footage():
    now = time.time()
    old = now - 5 * 86400
    # two old files survive (e.g. AI-kept windows), the footage between them was deleted by retention
    _seg("cam1", old, old + 600)
    _seg("cam1", old + 3600, old + 3900)
    _seg("cam1", now - 300, now)
    idx = footage.Index(TMP / "index" / "footage.db")
    vec = np.ones(DIM, np.float32) / np.sqrt(DIM)
    idx.add("cam1", [(old + 60, 0, vec), (old + 1800, 0, vec), (old + 3700, 0, vec), (now - 100, 0, vec),
                     (old - 3600, 0, vec)], now)
    ix = footage.Indexer(pipeline=None, index=idx)
    removed = ix.prune()
    kept = sorted(ts for (ts,) in idx.q("SELECT ts FROM frames"))
    assert removed == 2, removed                                  # the deleted gap and the pre-history row
    assert kept == sorted([old + 60, old + 3700, now - 100]), kept
    assert idx.q("SELECT COUNT(*) FROM frame_vec")[0][0] == 3


def test_step_crosses_short_segment_tail():
    """Regression: a cursor <1 s before the next segment must move on, not report 'caught up' forever."""
    import asyncio
    now = float(int(time.time()))
    a, b = now - 4000, now - 3400            # file names have whole-second starts
    _seg("cam9", a, b)
    _seg("cam9", b, now)
    idx = footage.Index(TMP / "index" / "step.db")
    idx.set_cursor("cam9", b - 0.5)
    ix = footage.Indexer(pipeline=None, index=idx)
    footage.framecache._listing.clear()
    assert asyncio.run(ix.step("cam9")) == 0
    assert abs(idx.cursor("cam9") - footage.framecache._segments("cam9")[1][0]) < 1e-3


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
