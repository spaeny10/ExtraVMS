"""GET /api/live/codecs: the video codec of every live-view path (<id>, <id>_sub, <id>_hd), from MediaMTX's tracks when
the path is pulled, else from what it relays or the camera's ONVIF profiles (streams.plan). A fake path_status and
camera list: no MediaMTX, camera or network needed.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_live_codecs.py   (from backend/)
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="nvr-livecodecs-test-"))
for k, sub in (("NVR_DATA_DIR", "data"), ("NVR_RECORDINGS_DIR", "recordings"), ("NVR_RUNTIME_DIR", "runtime")):
    os.environ[k] = str(TMP / sub)   # never the real server's database, recordings or mediamtx.yml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tunnelproto"))

import httpx  # noqa: E402

from nvr import api, mediamtx  # noqa: E402
from nvr.config import settings  # noqa: E402

for d in (settings.data_dir, settings.recordings_dir, settings.runtime_dir):
    assert "nvr-livecodecs-test-" in str(d).replace("\\", "/"), d


def prof(token, enc, w, h, path):
    return {"token": token, "encoding": enc, "width": w, "height": h, "fps": 25, "path": path}


H265_MAIN_H264_SUB = [prof("1", "H.265", 2592, 1520, "/main"), prof("2", "H.264", 640, 360, "/sub")]
H265_ONLY = [prof("1", "H.265", 2592, 1520, "/main")]


def cam(cid, profiles=None, **kw):
    return {"id": cid, "name": cid, "host": "10.0.0.9", "username": "u", "password": "p", "main_path": "/main", "sub_path": "/sub",
            "rtsp_port": 554, "onvif_port": 80, "enabled": 1, "streams": {"profiles": profiles} if profiles is not None else None, **kw}


def test_paths_follow_build_config():
    # every path live_codecs reports is one build_config configures for that camera, and the other way round
    for c in (cam("a", H265_MAIN_H264_SUB), cam("b", H265_ONLY), cam("c", H265_MAIN_H264_SUB, record_stream="sub"),
              cam("d", H265_ONLY, record_stream="sub"), cam("e")):
        assert set(mediamtx.live_paths(c)) == set(mediamtx.build_config([c])["paths"]), c["id"]
    assert mediamtx.live_paths(cam("b", H265_ONLY))["b_sub"] == ("main", "b")              # no sub stream: relays the main
    assert mediamtx.live_paths(cam("c", H265_MAIN_H264_SUB, record_stream="sub")) == {
        "c": ("sub", None), "c_sub": ("sub", "c"), "c_hd": ("main", None)}


def test_codec_names():
    assert [mediamtx.codec_name(x) for x in ("H264", "H.264", "h265", "H.265", "HEVC", {"codec": "H265"}, "MPEG-4 Audio", None, "MJPEG")] == [
        "H264", "H264", "H265", "H265", "H265", "H265", None, None, None]


def test_ready_paths_from_tracks_others_from_profiles_or_relays():
    cams = [cam("a", H265_MAIN_H264_SUB), cam("b", H265_ONLY), cam("c", H265_MAIN_H264_SUB, record_stream="sub"), cam("e")]
    status = {
        "a": {"ready": True, "tracks": ["H265", "G711"]},          # recorded main, pulled 24/7
        "b": {"ready": True, "tracks": [{"codec": "H265"}, {"codec": "Opus"}]},   # newer MediaMTX: track objects
        "c": {"ready": True, "tracks": ["H264"]},                   # sub-mode: the recorded path IS the sub stream
        "c_sub": {"ready": True, "tracks": ["H264"]},               # watched now
        "e": {"ready": False, "tracks": []},
    }
    out = mediamtx.live_codecs(cams, status)
    assert out["a"] == {"video": "H265", "ready": True}
    assert out["a_sub"] == {"video": "H264", "ready": False}       # on demand, not pulled: the profile's encoding
    assert out["b_sub"] == {"video": "H265", "ready": False}       # relay of the ready H.265 main
    assert out["c_sub"] == {"video": "H264", "ready": True}
    assert out["c_hd"] == {"video": "H265", "ready": False}        # the main stream, from its profile
    # never probed and not pulled: unknown (the browser just tries)
    assert out["e"] == {"video": None, "ready": False} and out["e_sub"] == {"video": None, "ready": False}
    assert set(out) == {"a", "a_sub", "b", "b_sub", "c", "c_sub", "c_hd", "e", "e_sub"}


def test_unsafe_camera_left_out():
    assert mediamtx.live_codecs([cam("bad", main_path="@evil:554/x")], {}) == {}


def test_endpoint_cached_30_s():
    calls = []

    async def fake_status():
        calls.append(1)
        return {"a": {"ready": True, "tracks": ["H264"]}}
    real_status, real_cams = mediamtx.path_status, api.db.cameras
    mediamtx.path_status = fake_status
    api.db.cameras = lambda enabled_only=False: [cam("a", H265_MAIN_H264_SUB)]
    api._live_codecs = None

    async def get():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://localhost:8080") as c:
            return await c.get("/api/live/codecs")
    try:
        r = asyncio.run(get())
        assert r.status_code == 200 and r.json() == {"a": {"video": "H264", "ready": True}, "a_sub": {"video": "H264", "ready": False}}
        asyncio.run(get())
        assert len(calls) == 1                                      # the second answer came from the cache
        api._live_codecs = (api._live_codecs[0] - api.LIVE_CODECS_TTL_S - 1, api._live_codecs[1])
        asyncio.run(get())
        assert len(calls) == 2

        async def down():
            raise httpx.ConnectError("MediaMTX not running")
        mediamtx.path_status = down
        api._live_codecs = None
        r = asyncio.run(get())   # MediaMTX down: still answers, from the profiles
        assert r.status_code == 200 and r.json()["a"] == {"video": "H265", "ready": False}
    finally:
        mediamtx.path_status, api.db.cameras = real_status, real_cams
        api._live_codecs = None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
