"""A proxied body cut short (the viewer left) is aborted at the site, not just forgotten (proxy.end_stream).

TSS-DT 2026-11-08: the hub dropped such streams silently, the site waited for credit forever, and four SD playback
transcodes held every slot from the evening before: every SD video on the hub failed ("too many transcodes")."""
import asyncio

from tunnelproto import Stream

from hub import proxy


class Conn:
    def __init__(self, s):
        self.streams = {s.id: s}
        self.sent: list[dict] = []

    async def send(self, msg):
        self.sent.append(msg)

    async def abort(self, s, reason):   # agents.SiteConn.abort
        if self.streams.pop(s.id, None) is not None and not s.done.is_set():
            await s.abort(reason)
            await self.send({"t": "abort", "id": s.id, "reason": reason})

    def finish(self, s):
        self.streams.pop(s.id, None)


def test_a_viewer_leaving_mid_body_aborts_the_site_stream():
    async def run():
        s = Stream(3)
        s.push(b"some video")              # the site is still sending: no end yet
        conn = Conn(s)
        proxy.end_stream(conn, s)
        await asyncio.sleep(0)             # the abort is sent from its own task (the response is being canceled)
        await asyncio.sleep(0)
        assert conn.sent == [{"t": "abort", "id": 3, "reason": "client left"}]
        assert s.aborted == "client left" and not conn.streams
    asyncio.run(run())


def test_a_body_the_site_ended_is_only_forgotten():
    async def run():
        s = Stream(4)
        s.push(None)                       # the site's `end`
        conn = Conn(s)
        proxy.end_stream(conn, s)
        await asyncio.sleep(0)
        assert conn.sent == [] and not conn.streams and s.aborted is None
    asyncio.run(run())
