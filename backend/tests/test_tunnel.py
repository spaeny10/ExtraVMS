"""tunnelproto: frame codec, chunk framing, credit window and abort.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_tunnel.py   (from backend/)
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tunnelproto"))

from tunnelproto import CHUNK, WINDOW, Stream, chunk, decode, encode, split  # noqa: E402


def test_codec_round_trip():
    f = {"t": "req", "id": 7, "method": "GET", "path": "/api/system", "query": "a=1", "headers": {"x-hub-user": "shawn"}}
    assert decode(encode(f)) == f
    sid, data = decode(chunk(4_000_000_001, b"\x00\x01body"))
    assert sid == 4_000_000_001 and data == b"\x00\x01body"
    assert [len(p) for p in split(b"x" * (CHUNK * 2 + 5))] == [CHUNK, CHUNK, 5]


def test_credit_window_blocks_and_grants():
    async def run():
        s = Stream(1, window=10)
        await s.take_credit(6)
        blocked = asyncio.create_task(s.take_credit(6))   # only 4 left: must wait for a grant
        await asyncio.sleep(0.05)
        assert not blocked.done()
        await s.grant(10)
        await asyncio.wait_for(blocked, 1)
        assert s._window == 8
    asyncio.run(run())


def test_abort_releases_waiters_and_ends_inbox():
    async def run():
        s = Stream(2, window=0)
        waiter = asyncio.create_task(s.take_credit(1))
        reader = asyncio.create_task(s.read())
        await asyncio.sleep(0.02)
        await s.abort("gone")
        await asyncio.wait_for(waiter, 1)
        assert await asyncio.wait_for(reader, 1) is None
        assert s.aborted == "gone" and s.done.is_set()
    asyncio.run(run())


def test_inbox_read_all():
    async def run():
        s = Stream(3)
        s.push(b"ab"); s.push(b"cd"); s.push(None)
        assert await s.read_all() == b"abcd" and s.done.is_set()
    asyncio.run(run())
    assert WINDOW >= CHUNK


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
