"""Wire protocol between a NewVMS site and the fleet hub, over one WebSocket the site opens to the hub.

Text frames are JSON control messages with a type in "t"; binary frames carry stream bodies as a 4-byte
big-endian stream id followed by bytes. The hub issues HTTP requests to the site as `req` streams (odd ids);
the site answers with `res` + body chunks + `end`. Either side may `abort` a stream. Flow control is a
credit window per stream: a sender may have at most WINDOW unacknowledged bytes in flight; the receiver
returns `credit` frames as it drains, so a slow phone never balloons memory at the hub or the site.

Frame types
  site -> hub : hello, heartbeat, event, res, end, abort, credit, rotated, pong, ping
  hub  -> site: welcome, enrolled, rotate, revoked, req, end, abort, credit, ping, pong, turn, vlm
"""
from __future__ import annotations

import asyncio
import json
import struct

PROTO = 1
WINDOW = 2 * 1024 * 1024  # bytes a sender may have unacknowledged per stream. 256 KB capped a stream at ~40 Mbit/s on a
                          # 50 ms round trip (window / RTT), which is what made far-back Timeline chunks crawl through
                          # the hub; 2 MB allows ~300 Mbit/s. Each side applies its own value, so versions may differ.
CHUNK = 64 * 1024        # largest single binary frame
HEARTBEAT_S = 30
OFFLINE_AFTER_S = 90     # three missed heartbeats
PING_S = 20

_HDR = struct.Struct(">I")


def encode(frame: dict) -> str:
    return json.dumps(frame, separators=(",", ":"), ensure_ascii=False)


def chunk(stream_id: int, data: bytes) -> bytes:
    return _HDR.pack(stream_id) + data


def decode(msg: str | bytes) -> dict | tuple[int, bytes]:
    """A control frame (dict) or a body chunk (stream id, bytes)."""
    if isinstance(msg, (bytes, bytearray, memoryview)):
        b = bytes(msg)
        return _HDR.unpack(b[:4])[0], b[4:]
    return json.loads(msg)


class Stream:
    """One request/response body in flight, with its credit window and inbox of received chunks."""

    def __init__(self, stream_id: int, window: int = WINDOW) -> None:
        self.id = stream_id
        self.inbox: asyncio.Queue[bytes | None] = asyncio.Queue()   # None = end of body
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.head = asyncio.Event()            # response head (or request) received
        self.done = asyncio.Event()
        self.aborted: str | None = None
        self._window = window                  # bytes we may still send
        self._credit = asyncio.Condition()

    # ---- sending side
    async def take_credit(self, n: int) -> None:
        """Block until n bytes of window are available, then consume them."""
        async with self._credit:
            while self._window < n and self.aborted is None:
                await self._credit.wait()
            self._window -= n

    async def grant(self, n: int) -> None:
        async with self._credit:
            self._window += n
            self._credit.notify_all()

    async def abort(self, reason: str = "aborted") -> None:
        self.aborted = reason
        self.inbox.put_nowait(None)
        self.done.set()
        self.head.set()
        async with self._credit:
            self._credit.notify_all()

    # ---- receiving side
    def push(self, data: bytes | None) -> None:
        self.inbox.put_nowait(data)
        if data is None:
            self.done.set()

    async def read(self) -> bytes | None:
        """Next body chunk, None at the end (or after an abort)."""
        return await self.inbox.get()

    async def read_all(self) -> bytes:
        out = bytearray()
        while (c := await self.read()) is not None:
            out += c
        return bytes(out)


def split(data: bytes, size: int = CHUNK):
    for i in range(0, len(data), size):
        yield data[i:i + size]
