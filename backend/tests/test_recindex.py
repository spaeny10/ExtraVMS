"""Recordings index: segment spans from moov/udta/mtxi + moov/mvhd, kept in step with the folder; the listing from the
index, the file being written up to now, MediaMTX only before a camera's first scan.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_recindex.py   (from backend/)
"""
import asyncio
import os
import struct
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-recindex-test-")
os.environ["NVR_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="nvr-recindex-runtime-")
os.environ["NVR_RECORDINGS_DIR"] = REC = tempfile.mkdtemp(prefix="nvr-recindex-rec-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import mediamtx, recindex  # noqa: E402
from nvr.db import db  # noqa: E402
from nvr.fmp4 import segment_name  # noqa: E402

T0 = float(int(time.time()) // 60 * 60 - 3600)   # an hour ago: a newest file without a length counts as being written


def box(typ: bytes, body: bytes) -> bytes:
    return struct.pack(">I", 8 + len(body)) + typ + body


def segment(path: str, start: float, seconds: float | None, version: int = 0, tail: bytes = b"\0" * 5000,
            wall: float | None = None, name: str | None = None) -> Path:
    """A file shaped like a MediaMTX segment's head: ftyp + moov(mvhd with the length; 0 = still being written; and
    udta/mtxi with the wall-clock start `wall`, when given)."""
    units = int((seconds or 0) * 1000)
    if version == 0:
        mvhd = box(b"mvhd", bytes([0, 0, 0, 0]) + struct.pack(">IIII", 0, 0, 1000, units) + b"\0" * 80)
    else:
        mvhd = box(b"mvhd", bytes([1, 0, 0, 0]) + struct.pack(">QQIQ", 0, 0, 1000, units) + b"\0" * 80)
    udta = box(b"udta", box(b"mtxi", b"\0" * 4 + b"\x11" * 16 + struct.pack(">QqQ", 7, 0, int(wall * 1e9)))) if wall else b""
    data = box(b"ftyp", b"iso5\0\0\0\x01iso5") + box(b"moov", mvhd + box(b"trak", b"\0" * 40) + udta) + box(b"mdat", tail)
    d = Path(REC) / path
    d.mkdir(parents=True, exist_ok=True)
    p = d / (name or segment_name(start))
    p.write_bytes(data)
    return p


def test_header_duration():
    assert recindex.header_duration(segment("hd", T0, 60.006)) == 60.006
    assert recindex.header_duration(segment("hd", T0 + 100, 600.5, version=1)) == 600.5
    assert recindex.header_duration(segment("hd", T0 + 800, None)) is None          # still being written
    bad = Path(REC) / "hd" / "junk.mp4"
    bad.write_bytes(b"\0\0\0\x01")
    assert recindex.header_duration(bad) is None


def test_scan_keeps_in_step_with_the_folder():
    a = segment("cam1", T0, 60)
    segment("cam1", T0 + 60, 60)
    segment("cam1", T0 + 120, None)                    # the file being written: not indexed yet
    assert recindex.scan_path("cam1") == 2
    rows = db.all("SELECT name, start_ts, end_ts FROM rec_segments WHERE camera_id='cam1' ORDER BY start_ts")
    assert [(r["start_ts"], r["end_ts"]) for r in rows] == [(T0, T0 + 60), (T0 + 60, T0 + 120)]
    assert recindex.scan_path("cam1") == 0             # nothing changed: no header read, no write
    # the written file closes (gets its length) and a new one opens; retention deletes the first
    segment("cam1", T0 + 120, 59.5)
    segment("cam1", T0 + 180, None)
    a.unlink()
    assert recindex.scan_path("cam1") == 2             # one added, one gone
    rows = db.all("SELECT start_ts, end_ts FROM rec_segments WHERE camera_id='cam1' ORDER BY start_ts")
    assert [(r["start_ts"], r["end_ts"]) for r in rows] == [(T0 + 60, T0 + 120), (T0 + 120, T0 + 179.5)]


def test_the_start_comes_from_mtxi_not_the_name():
    """Daylight saving ends: the hour repeats, so two files an hour apart have (almost) the same local name."""
    first = segment("cam4", T0, 600, wall=T0, name="2026-11-01_01-04-28-804425.mp4")
    segment("cam4", T0, 600, wall=T0 + 3600, name="2026-11-01_01-04-29-134425.mp4")
    segment("cam4", T0 + 7200, None, wall=T0 + 7200, name="2026-11-01_02-04-29-000000.mp4")
    assert recindex.header_info(first) == (T0, 600.0)
    recindex.scan_path("cam4")
    rows = db.all("SELECT start_ts, end_ts FROM rec_segments WHERE camera_id='cam4' ORDER BY start_ts")
    assert [(r["start_ts"], r["end_ts"]) for r in rows] == [(T0, T0 + 600), (T0 + 3600, T0 + 4200)]


def test_a_finished_file_without_its_length_ends_at_the_next():
    segment("cam2", T0, None)                          # MediaMTX stopped mid-segment: mvhd never patched
    segment("cam2", T0 + 45, 60)
    segment("cam2", T0 + 105, None)
    recindex.scan_path("cam2")
    rows = db.all("SELECT start_ts, end_ts FROM rec_segments WHERE camera_id='cam2' ORDER BY start_ts")
    assert rows[0]["start_ts"] == T0 and T0 < rows[0]["end_ts"] <= T0 + 45


def test_listing_from_the_index_the_file_being_written_up_to_now():
    n0 = float(int(time.time()) - 600)   # the last ten minutes: five finished 1-minute files, a hole, the one being written
    for i in range(5):
        segment("cam3", n0 + 60 * i, 60, wall=n0 + 60 * i)
    segment("cam3", n0 + 400, None, wall=n0 + 400)
    asked = []

    async def mtx(path, start=None, end=None):
        asked.append((path, start, end))
        return [{"start": mediamtx.rfc3339(n0), "duration": 12.5}]

    real = mediamtx.recording_spans
    mediamtx.recording_spans = mtx
    try:
        assert asyncio.run(recindex.recording_spans("cam3", n0 + 30, n0 + 900)) == [{"start": mediamtx.rfc3339(n0), "duration": 12.5}]
        assert asked == [("cam3", n0 + 30, n0 + 900)]   # not scanned yet: all of it from MediaMTX
        asked.clear()
        recindex.scan_all()
        out = asyncio.run(recindex.recording_spans("cam3", n0 + 30, n0 + 900))
        assert asked == []                               # scanned: MediaMTX isn't asked at all
        assert out[0] == {"start": mediamtx.rfc3339(n0 + 30), "duration": 270.0}      # five files, one span, clipped
        assert len(out) == 2 and out[1]["start"] == mediamtx.rfc3339(n0 + 400)
        assert 600 - recindex.LIVE_LAG_S - 400 - 2 < out[1]["duration"] <= 600 - recindex.LIVE_LAG_S - 400 + 2   # up to now
        out = asyncio.run(recindex.recording_spans("cam3", n0 + 70, n0 + 130))
        assert out == [{"start": mediamtx.rfc3339(n0 + 70), "duration": 60.0}]
    finally:
        mediamtx.recording_spans = real


def test_a_camera_that_stopped_and_an_abandoned_file():
    n0 = float(int(time.time()) - 900)
    segment("cam5", n0, 60, wall=n0)
    last = segment("cam5", n0 + 60, None, wall=n0 + 60)    # being written
    recindex.scan_path("cam5")
    assert "cam5" in recindex._live and db.one("SELECT COUNT(*) n FROM rec_segments WHERE camera_id='cam5'")["n"] == 1
    last.unlink()
    segment("cam5", n0 + 60, 42.0, wall=n0 + 60)            # the camera stopped: MediaMTX closed it, no new file
    recindex.scan_path("cam5")
    assert "cam5" not in recindex._live
    rows = db.all("SELECT start_ts, end_ts FROM rec_segments WHERE camera_id='cam5' ORDER BY start_ts")
    assert rows[-1]["end_ts"] == n0 + 102
    # an unfinished file from long ago (MediaMTX died mid-segment, nothing since) isn't "being written" forever
    old = float(int(time.time()) - recindex.LIVE_MAX_S - 600)
    segment("cam6", old, None, wall=old)
    recindex.scan_path("cam6")
    assert "cam6" not in recindex._live


def test_lane_queries_from_many_threads_get_their_own_rows():
    """The Timeline asks for every lane at once and each lane's query runs on a worker thread. sqlite3 shares one
    prepared statement per SQL text, so reading rows outside the lock mixed lanes' rows (None columns, IndexError)."""
    from concurrent.futures import ThreadPoolExecutor
    rows = [(f"lane{i}", f"f{j}.mp4", T0 + j * 60, T0 + j * 60 + 60, 1, 1.0) for i in range(8) for j in range(300)]
    for i in range(8):
        db.replace_rec_segments(f"lane{i}", [r for r in rows if r[0] == f"lane{i}"], [])

    def one_lane(i: int) -> bool:
        for _ in range(40):
            try:
                got = recindex._rows(f"lane{i}", T0, T0 + 300 * 60)
            except Exception:   # noqa: BLE001 - IndexError / TypeError from mixed rows
                return False
            if len(got) != 300 or any(s is None or e is None or e - s != 60 for s, e in got):
                return False
        return True

    def scanner() -> bool:   # the index's scan thread: transactions on the same connection meanwhile
        for k in range(60):
            db.replace_rec_segments("busy", [("busy", f"b{k}.mp4", T0 + k, T0 + k + 1, 1, 1.0)], [])
        return True

    with ThreadPoolExecutor(9) as ex:
        results = list(ex.map(lambda f: f(), [scanner] + [lambda i=i: one_lane(i) for i in range(8)]))
    assert all(results), results


def test_the_lane_answer_is_gzipped_when_the_browser_takes_it():
    import gzip
    import json

    from starlette.requests import Request

    from nvr import api

    def req(accept: str | None) -> Request:
        return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                        "headers": [(b"accept-encoding", accept.encode())] if accept else []})

    data = {"events": [{"id": i, "cells": "A" * 96, "start_ts": T0 + i} for i in range(200)]}
    r = api.json_maybe_gzip(req("gzip, deflate, br"), data)
    assert r.headers["content-encoding"] == "gzip" and r.headers["vary"] == "Accept-Encoding"
    assert json.loads(gzip.decompress(r.body)) == data and len(r.body) < len(json.dumps(data)) / 4
    plain = api.json_maybe_gzip(req(None), data)
    assert "content-encoding" not in plain.headers and json.loads(plain.body) == data
    small = api.json_maybe_gzip(req("gzip"), {"spans": []})   # not worth it
    assert "content-encoding" not in small.headers


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
