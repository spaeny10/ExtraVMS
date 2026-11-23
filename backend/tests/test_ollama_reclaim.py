"""A leftover `ollama serve` from an earlier NVR process holding our port is stopped before ours starts
(synopsis.reclaim_port); our own server and other programs are left alone. No Ollama needed.
Run: ..\\.venv\\Scripts\\python.exe tests\\test_ollama_reclaim.py  (from backend/)"""
import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-reclaim-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import synopsis  # noqa: E402


def _run(listeners, mine=frozenset()):
    killed: list[int] = []
    state = {"l": list(listeners)}

    async def kill(pid):
        killed.append(pid)
        state["l"] = [x for x in state["l"] if x[0] != pid]

    real = synopsis.port_listeners, synopsis.kill_tree
    synopsis.port_listeners = lambda port: list(state["l"])
    synopsis.kill_tree = kill
    try:
        asyncio.run(synopsis.reclaim_port(11435, "primary", set(mine), wait_s=0.2))
    finally:
        synopsis.port_listeners, synopsis.kill_tree = real
    return killed


def test_leftover_ollama_is_stopped():
    assert _run([(5176, "ollama.exe")]) == [5176]
    assert _run([(5176, "ollama")]) == [5176]   # Linux


def test_our_own_server_is_kept():
    assert _run([(5176, "ollama.exe")], mine={5176}) == []


def test_other_programs_are_left_alone():
    assert _run([(900, "nginx.exe")]) == []


def test_free_port():
    assert _run([]) == []


def test_port_from_url():
    assert synopsis._port("http://127.0.0.1:11437") == 11437
    assert synopsis._port("http://127.0.0.1:11437/") == 11437
    assert synopsis._port("http://localhost") is None


def test_listeners_returns_pairs():
    assert all(isinstance(pid, int) and isinstance(name, str) for pid, name in synopsis.port_listeners(1))


def test_trim_log_keeps_the_tail_and_appends_after():
    """Another process (Ollama) writes through the inherited handle. Python's own "ab" appends only for Python's
    writes on Windows: the child kept its offset and refilled the emptied log with 1.1 GB of zeros (TSS-DT 2026-11-04)."""
    p = Path(os.environ["NVR_DATA_DIR"]) / "ollama.log"
    p.write_bytes(b"a" * 900 + b"b" * 100)
    assert not synopsis.trim_log(p, 2000, 100)            # under the limit: untouched
    child = ("import sys, time\n"
             "sys.stdout.buffer.write(b'first\\n'); sys.stdout.flush()\n"
             "while not __import__('os').path.exists(sys.argv[1]): time.sleep(0.05)\n"
             "sys.stdout.buffer.write(b'next\\n'); sys.stdout.flush()\n")
    go = Path(os.environ["NVR_DATA_DIR"]) / "go"
    with synopsis.open_log(p) as log_file:                # the handle Ollama inherits, open throughout
        proc = subprocess.Popen([sys.executable, "-c", child, str(go)], stdout=log_file)
        deadline = time.time() + 20
        while p.stat().st_size < 1006 and time.time() < deadline:
            time.sleep(0.05)
        assert synopsis.trim_log(p, 500, 100)
        assert Path(f"{p}.1").read_bytes() == b"b" * 94 + b"first\n"
        assert p.stat().st_size == 0
        go.write_text("x")
        assert proc.wait(20) == 0
    assert p.read_bytes() == b"next\n"                    # no gap of zeros where the old content was


def test_restart_backs_off_while_it_keeps_exiting():
    w = synopsis.OllamaServer.restart_wait
    assert [w(n) for n in range(9)] == [5, 5, 10, 20, 40, 80, 160, 300, 300]


def _serve_exits(runs: list[float]):
    """Run OllamaServer._serve against a fake `ollama serve` that exits with 1 after each of `runs` seconds (a fake
    clock); returns the waits it chose and the warnings it logged."""
    import logging
    waits, warnings, clock = [], [], {"t": 0.0}
    runs = list(runs)

    class Proc:
        pid, returncode = 4242, None

        async def wait(self):
            clock["t"] += runs.pop(0)
            self.returncode = 1
            return 1

    async def spawn(*a, **kw):
        return Proc()

    class Stop(Exception):
        pass

    async def sleep(s):
        waits.append(s)
        if not runs:
            raise Stop

    class Catch(logging.Handler):
        def emit(self, r):
            if r.levelno >= logging.WARNING:
                warnings.append(r.getMessage())

    real = (synopsis.asyncio.create_subprocess_exec, synopsis.asyncio.sleep, synopsis.time.monotonic,
            synopsis.port_listeners, synopsis.open_log)
    synopsis.asyncio.create_subprocess_exec, synopsis.asyncio.sleep = spawn, sleep
    synopsis.time.monotonic = lambda: clock["t"]
    synopsis.port_listeners = lambda port: []
    synopsis.open_log = lambda path: None
    h = Catch()
    synopsis.log.addHandler(h)
    try:
        asyncio.run(synopsis.OllamaServer()._serve("primary"))
    except Stop:
        pass
    finally:
        (synopsis.asyncio.create_subprocess_exec, synopsis.asyncio.sleep, synopsis.time.monotonic,
         synopsis.port_listeners, synopsis.open_log) = real
        synopsis.log.removeHandler(h)
    return waits, warnings


def test_an_ollama_that_cannot_start_is_not_warned_about_every_5_s():
    waits, warnings = _serve_exits([0.1] * 12)
    assert waits == [5, 10, 20, 40, 80, 160, 300, 300, 300, 300, 300, 300], waits
    assert len(warnings) == 2, warnings                # the first exit, and when it reaches the slowest retry
    assert "keeps exiting" in warnings[1] and "ollama.log" in warnings[1]


def test_an_ollama_that_ran_a_while_restarts_quickly_again():
    waits, warnings = _serve_exits([0.1, 0.1, 0.1, 3600, 0.1])
    assert waits == [5, 10, 20, 5, 5], waits          # a long run resets the back-off
    assert len(warnings) == 3, warnings               # each new streak's first exit is a warning


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
