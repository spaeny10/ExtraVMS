"""A failing data disk: the database reopens itself after "disk I/O error", the storage state and the storage_failing
health alert follow it, the data-dir write probe feeds the same state, the hub heartbeat still goes out when the
summary can't be built, and a full metadata queue drops frames with one warning a minute instead of a traceback each.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_storage_health.py   (from backend/)
"""
import asyncio
import logging
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

for var, prefix in (("NVR_DATA_DIR", "nvr-storage-data-"), ("NVR_RECORDINGS_DIR", "nvr-storage-rec-"),
                    ("NVR_RUNTIME_DIR", "nvr-storage-rt-")):
    os.environ[var] = tempfile.mkdtemp(prefix=prefix)   # always new: never the real DB, recordings or MediaMTX config
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tunnelproto"))

from nvr import db as db_mod, health, hub_agent, ingest  # noqa: E402
from nvr.db import db  # noqa: E402

T = [1000.0]


class Flaky:
    """A connection that raises OperationalError(msg) `fails` times, then passes statements to a real one."""

    def __init__(self, real, fails=float("inf"), msg="disk I/O error", in_transaction=False):
        self.real, self.fails, self.msg, self.in_transaction = real, fails, msg, in_transaction
        self.closed = False

    def execute(self, *a):
        if self.fails > 0:
            self.fails -= 1
            raise sqlite3.OperationalError(self.msg)
        return self.real.execute(*a)

    def close(self):
        self.closed = True


class Logs(logging.Handler):
    def __init__(self, name):
        super().__init__(logging.WARNING)
        self.lines: list[str] = []
        self.logger = logging.getLogger(name)

    def emit(self, record):
        self.lines.append(record.getMessage())

    def __enter__(self):
        self.logger.addHandler(self)
        return self

    def __exit__(self, *exc):
        self.logger.removeHandler(self)


def _reset():
    """A healthy database with a fake clock: real connection, storage ok, no reopen yet."""
    db.__dict__.pop("_open", None)
    db.conn = db._open(db.path)
    T[0] = 1000.0
    db.clock = lambda: T[0]
    db.storage.update(ok=True, error=None, failing_since=None, last_ok=T[0], reopens=0)
    db._fail_source, db._last_reopen = None, 0.0


def test_reopens_after_disk_io_error_and_retries():
    _reset()
    flaky = Flaky(db._open(db.path), fails=2)
    db.conn = flaky
    db.set_setting("probe_key", {"n": 1})          # fails once, reopens, the retry on the new connection works
    assert db.conn is not flaky and flaky.closed and db.storage["reopens"] == 1
    assert db.get_setting("probe_key") == {"n": 1}
    s = db.storage
    assert s["ok"] and s["error"] is None and s["failing_since"] is None and s["last_ok"] == T[0], s


def test_rate_limit_failure_state_alert_and_recovery():
    _reset()
    opened = []

    def dead_open(path):                            # the file can't be used yet: every new connection fails too
        opened.append(path)
        return Flaky(None)
    db._open = dead_open
    db.conn = Flaky(None)
    try:
        db.set_setting("k", 1)
        raise AssertionError("should have raised")
    except sqlite3.OperationalError as e:
        assert "disk I/O error" in str(e)
    assert len(opened) == 1 and db.storage["reopens"] == 0
    assert not db.storage["ok"] and db.storage["failing_since"] == 1000.0 and db.storage["error"] == "disk I/O error"
    T[0] = 1002.0                                   # within REOPEN_EVERY_S: no second reopen, the dead disk doesn't spin
    for _ in range(20):
        try:
            db.one("SELECT 1")
        except sqlite3.OperationalError:
            pass
    assert len(opened) == 1 and db.storage["failing_since"] == 1000.0
    # the health problem only after STORAGE_ALERT_S
    assert health.storage_alert() is None
    T[0] = 1000.0 + health.STORAGE_ALERT_S
    assert health.storage_alert() is None
    T[0] = 1061.0
    a = health.storage_alert()
    assert a["kind"] == "storage_failing" and a["since"] == 1000.0 and a["error"] == "disk I/O error", a
    assert re.fullmatch(r"Database can't be written \(disk I/O error\) since \d\d:\d\d: events are not being stored\. "
                        r"Check the data disk \(.+\)\.", a["text"]), a["text"]
    assert f"({health.data_drive()})" in a["text"]
    # the disk comes back: the next statement reopens (rate limit passed) and works
    db.__dict__.pop("_open")
    T[0] = 1070.0
    with Logs("nvr.db") as logs:
        db.set_setting("k", 2)
    assert db.get_setting("k") == 2 and db.storage["ok"] and db.storage["reopens"] == 1
    assert health.storage_alert() is None
    assert any("database writable again after 70 s" in x for x in logs.lines), logs.lines


def test_malformed_and_locked_are_not_reopened():
    _reset()
    opened = []
    db._open = lambda path: opened.append(path) or Flaky(None)
    for msg in ("database disk image is malformed", "database is locked"):
        db.conn = Flaky(None, msg=msg)
        try:
            db.one("SELECT 1")
            raise AssertionError("should have raised")
        except sqlite3.OperationalError as e:
            assert msg in str(e)
    assert opened == [] and db.storage["ok"] and db.storage["reopens"] == 0
    _reset()


def test_no_reopen_inside_a_callers_transaction():
    """A caller's BEGIN ... COMMIT under db.lock: its connection is not swapped out mid-transaction."""
    _reset()
    opened = []
    db._open = lambda path: opened.append(path) or db_mod.Database._open(path)
    flaky = Flaky(None, in_transaction=True)
    db.conn = flaky
    with db.lock:
        try:
            db.execute("UPDATE settings SET value=value")
            raise AssertionError("should have raised")
        except sqlite3.OperationalError:
            pass
    assert db.conn is flaky and opened == [] and not db.storage["ok"]
    # outside any transaction of a caller the same failure recovers (a stuck in_transaction flag doesn't block it)
    db.execute("UPDATE settings SET value=value")
    assert db.conn is not flaky and len(opened) == 1 and db.storage["ok"]
    _reset()


def test_a_read_does_not_clear_but_a_write_does():
    _reset()
    db.storage_failed("disk I/O error")
    T[0] = 1100.0
    assert db.one("SELECT 1 AS x")["x"] == 1 and not db.storage["ok"]   # answered from cache on a dying disk, maybe
    db.set_setting("k", 3)
    assert db.storage["ok"] and db.storage["failing_since"] is None


def test_write_probe_feeds_the_storage_state():
    _reset()
    # a real probe: writes, fsyncs and removes its file; a missing directory can't be written
    health.probe_data_dir()
    assert not (Path(os.environ["NVR_DATA_DIR"]) / health.PROBE_FILE).exists()
    gone = Path(os.environ["NVR_DATA_DIR"]) / "unplugged" / "data"   # a directory that isn't there
    try:
        health.probe_data_dir(gone)
        raise AssertionError("should have raised")
    except OSError:
        pass

    async def run():
        bad = health.StorageWatch(probe=lambda: health.probe_data_dir(gone))
        assert await bad.check() is False
        assert not db.storage["ok"] and db.storage["error"].startswith("data disk not writable")
        T[0] = 1061.0
        a = health.storage_alert()
        assert a and a["kind"] == "storage_failing" and "data disk not writable" in a["text"], a
        assert await health.StorageWatch(probe=lambda: None).check() is True   # the disk is back
        assert db.storage["ok"] and health.storage_alert() is None
        # a probe success never clears a failure SQLite found (the file system can be back with the connection dead)
        db.storage_failed("disk I/O error")
        assert await health.StorageWatch(probe=lambda: None).check() is True and not db.storage["ok"]
        db.set_setting("k", 4)
        assert db.storage["ok"]
        # a write that hangs counts as a failure, and no second probe starts beside it
        release = threading.Event()
        old = health.PROBE_TIMEOUT_S
        health.PROBE_TIMEOUT_S = 0.1
        try:
            calls = []
            w = health.StorageWatch(probe=lambda: calls.append(1) or release.wait(5))
            assert await w.check() is False and "not responding" in db.storage["error"]
            assert await w.check() is False and "has hung" in db.storage["error"] and len(calls) == 1
        finally:
            health.PROBE_TIMEOUT_S = old
            release.set()
            await asyncio.sleep(0.05)
    asyncio.run(run())
    _reset()


def test_health_needs_no_database():
    """With every statement failing, stream health keeps sampling and a camera's health still computes."""
    _reset()
    db._open = lambda path: Flaky(None)
    db.conn = Flaky(None)
    h = health.StreamHealth()
    text = 'paths{name="cam1",state="ready"} 1\npaths_inbound_bytes{name="cam1",state="ready"} 1000\n'
    h.ingest(text, now=2000.0)
    h.ingest(text.replace("1000", "5000"), now=2010.0)
    assert h.paths["cam1"].total == 4000
    assert h.camera("cam1")["warnings"] == [] and h.camera("cam1")["bitrate_mbps"] is not None
    h.flush()                                           # shutting down: logged, not raised
    _reset()


def test_heartbeat_goes_out_when_the_summary_fails():
    _reset()
    db.storage_failed("disk I/O error")
    T[0] = 1000.0 + 120

    async def broken_summary(state, since):
        raise sqlite3.OperationalError("disk I/O error")

    async def run():
        agent = hub_agent.HubAgent(None, SimpleNamespace(pipeline=None), summary_fn=broken_summary)
        sent = []

        async def send(frame):
            sent.append(frame)
        agent._send = send
        before = agent._last_summary_at
        task = asyncio.create_task(agent._heartbeat_loop(0.05))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if len(sent) >= 2:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert len(sent) >= 2 and all(f["t"] == "heartbeat" for f in sent), sent
        s = sent[0]["summary"]
        assert s["degraded"] is True and "disk I/O error" in s["error"] and "cameras" not in s, s
        assert [a["kind"] for a in s["health_alerts"]] == ["storage_failing"], s["health_alerts"]
        assert s["storage"]["ok"] is False and "disk" in s
        assert agent._last_summary_at == before and agent.last_heartbeat is not None   # the missed attention events come later
    with Logs("nvr.hub") as logs:
        asyncio.run(run())
    assert sum("sending a minimal one" in x for x in logs.lines) == 1, logs.lines   # once, not every heartbeat
    _reset()


def test_agent_reconnects_with_the_token_it_last_read():
    _reset()
    db.set_setting("hub_token", "device-token-z")
    db.set_setting("hub_url", "wss://hub.example/agent")
    agent = hub_agent.HubAgent(None, SimpleNamespace())
    db._open = lambda path: Flaky(None)
    db.conn = Flaky(None)
    try:
        assert agent._auth_header() == "Bearer device-token-z" and agent.hub_url() == "wss://hub.example/agent"
    finally:
        _reset()
        db.set_setting("hub_token", None)
        db.set_setting("hub_url", None)


def test_full_queue_drops_with_one_warning_a_minute():
    clock = [0.0]

    async def run():
        loop = asyncio.get_running_loop()
        errors = []
        loop.set_exception_handler(lambda lp, ctx: errors.append(ctx))
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        feed = ingest.QueueFeed(loop, q, "cam7", "metadata frames", clock=lambda: clock[0])
        with Logs("nvr.ingest") as logs:
            # from a reader thread, as MetadataReader does
            t = threading.Thread(target=lambda: [feed(i) for i in range(50)])
            t.start()
            t.join()
            await asyncio.sleep(0.05)
            assert q.qsize() == 2 and feed.dropped_total == 48 and len(logs.lines) == 1, logs.lines
            assert "cam7" in logs.lines[0] and "queue full" in logs.lines[0]
            clock[0] = 30.0
            feed.put(99)
            assert len(logs.lines) == 1
            clock[0] = 61.0
            feed.put(100)
            assert len(logs.lines) == 2 and "dropped 49 metadata frames in the last 61 s" in logs.lines[1], logs.lines
        assert errors == [], errors   # no "Exception in callback Queue.put_nowait" from the loop
    asyncio.run(run())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
