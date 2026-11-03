"""MediaMTX calls share one pooled client per event loop: a new httpx client per call built an SSL context on the
event loop each time (0.2-1 s under load), and a Timeline asking for every lane at once stalled the server for
seconds (TSS-DT 2026-11-03: listings 7-24 s, the Timeline empty and nothing to scrub).

Run: ..\.venv\Scripts\python.exe tests\test_mediamtx_client.py   (from backend/)
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-mtxclient-test-")
os.environ["NVR_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="nvr-mtxclient-runtime-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from nvr import mediamtx  # noqa: E402


def test_one_client_per_loop():
    async def two():
        return mediamtx.local_client(), mediamtx.local_client()
    a, b = asyncio.run(two())
    assert a is b
    c, _ = asyncio.run(two())
    assert c is not a          # a client is bound to its loop: a new loop gets its own


def test_calls_reuse_it_without_building_clients():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.url.path, dict(req.url.params)))
        if req.url.path == "/list":
            return httpx.Response(200, json=[{"start": "2026-11-03T13:15:19Z", "duration": 60}])
        if req.url.path == "/v3/paths/list":
            return httpx.Response(200, json={"items": [{"name": "cam1", "ready": True}]})
        return httpx.Response(404)

    built = []
    real = mediamtx.httpx.AsyncClient

    class Counting(real):
        def __init__(self, *a, **kw):
            built.append(1)
            super().__init__(*a, **{**kw, "transport": httpx.MockTransport(handler)})

    async def go():
        mediamtx.local_client()                 # the one client this loop gets
        spans = [await mediamtx.recording_spans("cam1", 1793711719.8, 1793719519.8) for _ in range(5)]
        status = await mediamtx.path_status()
        return spans, status

    mediamtx.httpx.AsyncClient = Counting
    try:
        spans, status = asyncio.run(go())
    finally:
        mediamtx.httpx.AsyncClient = real
        mediamtx._local = None
    assert len(built) == 1
    assert spans[0] == [{"start": "2026-11-03T13:15:19Z", "duration": 60}] and status["cam1"]["ready"]
    assert calls[0] == ("/list", {"path": "cam1", "start": mediamtx.rfc3339(1793711719.8), "end": mediamtx.rfc3339(1793719519.8)})


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
