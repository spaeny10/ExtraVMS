"""A leftover `ollama serve` from an earlier NVR process holding our port is stopped before ours starts
(synopsis.reclaim_port); our own server and other programs are left alone. No Ollama needed.
Run: ..\\.venv\\Scripts\\python.exe tests\\test_ollama_reclaim.py  (from backend/)"""
import asyncio
import os
import sys
import tempfile
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
    p = Path(os.environ["NVR_DATA_DIR"]) / "ollama.log"
    p.write_bytes(b"a" * 900 + b"b" * 100)
    assert not synopsis.trim_log(p, 2000, 100)            # under the limit: untouched
    with open(p, "ab") as writer:                         # Ollama's handle, open throughout
        assert synopsis.trim_log(p, 500, 100)
        assert Path(f"{p}.1").read_bytes() == b"b" * 100
        assert p.stat().st_size == 0
        writer.write(b"next line\n")
        writer.flush()
    assert p.read_bytes() == b"next line\n"               # no gap of zeros where the old content was


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
