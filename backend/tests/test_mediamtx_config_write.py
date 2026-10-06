"""The MediaMTX config is replaced atomically, never truncated in place (a reload that caught the empty file ran
MediaMTX on its defaults: no API, no recording, no camera paths).

Run: ..\.venv\Scripts\python.exe tests\test_mediamtx_config_write.py   (from backend/)
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-mtxwrite-test-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import mediamtx  # noqa: E402


def test_atomic_write_replaces_and_leaves_no_temp():
    d = Path(tempfile.mkdtemp())
    p = d / "mediamtx.yml"
    p.write_text("old: 1\n")
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(Path(dst).read_text())          # at the moment of the swap the live file is still complete
        real_replace(src, dst)
    mediamtx.os.replace = spy
    try:
        mediamtx.atomic_write(p, "new: 2\n" * 1000)
    finally:
        mediamtx.os.replace = real_replace
    assert seen == ["old: 1\n"]
    assert p.read_text() == "new: 2\n" * 1000
    assert [x.name for x in d.iterdir()] == ["mediamtx.yml"]


def test_write_config_uses_atomic_write():
    calls = []
    real = mediamtx.atomic_write
    mediamtx.atomic_write = lambda path, text: calls.append(path) or real(path, text)
    try:
        m = mediamtx.MediaMTX()
        m.write_config([])
    finally:
        mediamtx.atomic_write = real
    assert calls and calls[0] == m.config_path and m.config_path.read_text().strip()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
