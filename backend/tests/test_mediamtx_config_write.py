"""The MediaMTX config is replaced atomically, never truncated in place (a reload that caught the empty file ran
MediaMTX on its defaults: no API, no recording, no camera paths).

Run: ..\.venv\Scripts\python.exe tests\test_mediamtx_config_write.py   (from backend/)
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-mtxwrite-test-")
# never the real runtime folder: write_config there rewrote the RUNNING server's mediamtx.yml with no cameras, and
# MediaMTX stopped recording every camera until the server rewrote it (~4 min per test run, 2026-10-06..08)
os.environ["NVR_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="nvr-mtxwrite-runtime-")
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
        assert Path(tempfile.gettempdir()) in m.config_path.parents, m.config_path   # a scratch folder, not the live server
        m.write_config([])
    finally:
        mediamtx.atomic_write = real
    assert calls and calls[0] == m.config_path and m.config_path.read_text().strip()


def test_reader_password_is_created_once_under_concurrency():
    """Qwenbot 2026-10-08: the config writer, the metadata readers and the WHEP proxy asked at once, each made its own
    password, and mediamtx.yml and the database disagreed (every metadata session 401 for 4.5 h)."""
    import threading
    from nvr.db import db
    db.execute("DELETE FROM settings WHERE key='mediamtx_reader'")
    real_get, gate = db.get_setting, threading.Barrier(8)

    def slow_get(key, default=None):   # every thread reads "missing" before any of them writes, if unprotected
        v = real_get(key, default)
        if key == "mediamtx_reader" and v is None:
            try:
                gate.wait(timeout=0.5)
            except threading.BrokenBarrierError:
                pass
        return v
    db.get_setting = slow_get
    got = []
    try:
        threads = [threading.Thread(target=lambda: got.append(mediamtx.reader_credentials())) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    finally:
        db.get_setting = real_get
    assert len(got) == 8 and len(set(got)) == 1, got
    assert real_get("mediamtx_reader")["pass"] == got[0][1]


def test_write_config_reports_drift_and_repairs_it():
    m = mediamtx.MediaMTX()
    assert Path(tempfile.gettempdir()) in m.config_path.parents, m.config_path
    m.write_config([])
    assert m.write_config([]) is False                          # nothing changed: nothing written
    user, pw = mediamtx.reader_credentials()
    m.config_path.write_text(m.config_path.read_text().replace(pw, "stale-password"))   # what Qwenbot ended up with
    assert m.write_config([]) is True and pw in m.config_path.read_text()


def test_ensure_loaded_makes_mediamtx_reload_a_missed_change():
    """MediaMTX's watcher missed a config write (DC1 Server 2026-10-12): its loaded paths lack the new camera, so the
    same file is swapped in again; when they match nothing is written."""
    import asyncio
    m = mediamtx.MediaMTX()
    m.config_path.write_text("paths:\n  cam1: {}\n  cam1_sub: {}\n  cam3: {}\n")
    loaded = {"names": ["cam1", "cam1_sub"]}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"items": [{"name": n} for n in loaded["names"]] + [{"name": "~^any$"}]}

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            assert url.endswith("/v3/config/paths/list")
            return Resp()

    writes = []
    real_client, real_write = mediamtx.httpx.AsyncClient, mediamtx.atomic_write
    mediamtx.httpx.AsyncClient = Client
    mediamtx.atomic_write = lambda path, text: writes.append(text)
    try:
        assert asyncio.run(m.ensure_loaded()) == {"cam3"}
        assert writes == [m.config_path.read_text()]          # the same text again: the watcher sees a change
        loaded["names"] = ["cam1", "cam1_sub", "cam3"]
        assert asyncio.run(m.ensure_loaded()) == set()
        assert len(writes) == 1
    finally:
        mediamtx.httpx.AsyncClient, mediamtx.atomic_write = real_client, real_write


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
