"""Low-bitrate (SD) playback: the ffmpeg argv for NVENC vs x264, encoder detection, capabilities, the transcode
limit, and (when ffmpeg is installed) a real transcode of a generated 1080p30 clip.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_playback_sd.py   (from backend/)
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("NVR_ALLOWED_HOSTS", "lan")  # the test client's Host (lan_guard Host allow-list)
os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-sd-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tunnelproto"))

import httpx  # noqa: E402

from nvr import api  # noqa: E402
from nvr.config import settings  # noqa: E402

SRC = "http://127.0.0.1:9996/get?path=cam1&start=x&duration=60&format=fmp4"


def _after(argv, flag):
    return argv[argv.index(flag) + 1]


def test_args_x264():
    a = api.sd_ffmpeg_args("ffmpeg", SRC, "libx264")
    assert a[0] == "ffmpeg" and _after(a, "-i") == SRC and "-hwaccel" not in a
    assert _after(a, "-c:v") == "libx264" and _after(a, "-preset") == "veryfast" and _after(a, "-tune") == "zerolatency"
    assert _after(a, "-b:v") == "700k" and _after(a, "-fpsmax") == "15" and _after(a, "-pix_fmt") == "yuv420p"
    assert _after(a, "-vf") == "scale=w=-2:h='min(720,ih)'"
    assert _after(a, "-c:a") == "aac" and _after(a, "-b:a") == "48k" and a.count("-map") == 2 and "0:a:0?" in a
    assert _after(a, "-movflags") == "frag_keyframe+empty_moov+default_base_moof" and a[-1] == "pipe:1"
    assert _after(a, "-f") == "mp4"


def test_args_nvenc():
    a = api.sd_ffmpeg_args("ffmpeg", SRC, "h264_nvenc")
    assert _after(a, "-c:v") == "h264_nvenc" and _after(a, "-preset") == "p4"
    assert a.index("-hwaccel") < a.index("-i") and _after(a, "-hwaccel") == "cuda"   # an input option
    assert "-tune" in a and _after(a, "-tune") == "ll" and "libx264" not in a
    assert _after(a, "-b:v") == "700k" and _after(a, "-fpsmax") == "15"


def test_limits():
    old = settings.playback_transcode_max
    try:
        settings.playback_transcode_max = 0
        assert api.sd_max("h264_nvenc") == 4 and api.sd_max("libx264") == 2
        settings.playback_transcode_max = 7
        assert api.sd_max("libx264") == 7
    finally:
        settings.playback_transcode_max = old


async def _get(url):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app, raise_app_exceptions=False), base_url="http://lan") as c:
        return await c.get(url)


def test_capabilities_and_busy():
    api._encoder[:] = ["libx264"]
    try:
        r = asyncio.run(_get("/api/playback/capabilities"))
        assert r.status_code == 200 and r.json() == {"sd": True, "encoder": "libx264"}
        api._sd_active = api.sd_max("libx264")
        r = asyncio.run(_get("/api/playback/cam1?start=1700000000&duration=30&q=sd"))
        assert r.status_code == 503 and r.json()["detail"] == "too many transcodes"
        api._sd_active = 0
        assert asyncio.run(_get("/api/playback/cam1?start=1700000000&q=xx")).status_code == 422   # only sd|hd
        api._encoder[:] = [None]
        r = asyncio.run(_get("/api/playback/capabilities"))
        assert r.json() == {"sd": False, "encoder": None}
        assert asyncio.run(_get("/api/playback/cam1?start=1700000000&q=sd")).status_code == 501
    finally:
        api._encoder.clear()
        api._sd_active = 0


def test_detect_encoder_without_ffmpeg():
    assert api.detect_encoder(str(Path(tempfile.gettempdir()) / "no-such-ffmpeg.exe")) is None


def test_real_transcode():
    exe = api._ffmpeg_exe()
    if not (shutil.which(exe) or Path(exe).exists()):
        print("  (no ffmpeg here - skipped)")
        return
    probe = shutil.which("ffprobe") or str(Path(exe).with_name("ffprobe.exe"))
    d = Path(tempfile.mkdtemp(prefix="nvr-sd-clip-"))
    src = d / "src.mp4"
    subprocess.run([exe, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30:d=4", "-f", "lavfi", "-i",
                    "sine=f=440:r=8000:d=4", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_mulaw", "-f", "mov",
                    str(src)], check=True, timeout=120)
    encoders = ["libx264"]
    found = api.detect_encoder(exe)
    print("  detected encoder:", found)
    if found == "h264_nvenc":
        encoders.append("h264_nvenc")
    for enc in encoders:
        out = d / f"{enc}.mp4"
        with open(out, "wb") as f:
            subprocess.run(api.sd_ffmpeg_args(exe, str(src), enc), stdout=f, check=True, timeout=120)
        info = json.loads(subprocess.run([probe, "-v", "error", "-show_streams", "-of", "json", str(out)],
                                         capture_output=True, text=True, check=True).stdout)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        a = next(s for s in info["streams"] if s["codec_type"] == "audio")
        num, den = (int(x) for x in v["avg_frame_rate"].split("/"))
        assert v["codec_name"] == "h264" and v["height"] == 720 and v["width"] == 1280 and v["pix_fmt"] == "yuv420p", v
        assert num / den <= 15.01, v["avg_frame_rate"]
        assert a["codec_name"] == "aac"
        print(f"  {enc}: {out.stat().st_size // 1024} KiB for 4 s, {v['avg_frame_rate']} fps")
    # through the route: streams fMP4 and frees its transcode slot afterwards
    real = api._mediamtx_fmp4
    api._mediamtx_fmp4 = lambda params: str(src)
    api._encoder[:] = ["libx264"]
    try:
        r = asyncio.run(_get("/api/playback/cam1?start=1700000000&duration=4&q=sd"))
        assert r.status_code == 200 and r.headers["content-type"] == "video/mp4" and r.content[4:8] == b"ftyp", r.status_code
        assert api._sd_active == 0
    finally:
        api._mediamtx_fmp4 = real
        api._encoder.clear()
    # a source smaller than 720p is not upscaled
    small = d / "small.mp4"
    subprocess.run([exe, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=10:d=2", "-c:v", "libx264", "-preset",
                    "ultrafast", str(small)], check=True, timeout=60)
    out = d / "small_sd.mp4"
    with open(out, "wb") as f:
        subprocess.run(api.sd_ffmpeg_args(exe, str(small), "libx264"), stdout=f, check=True, timeout=60)
    info = json.loads(subprocess.run([probe, "-v", "error", "-show_streams", "-of", "json", str(out)],
                                     capture_output=True, text=True, check=True).stdout)
    assert info["streams"][0]["height"] == 360
    shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
