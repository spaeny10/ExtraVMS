"""record_stream "sub" (cellular sites): MediaMTX records the sub stream under the camera's own path, so playback,
clips, frames and verification (all keyed by the camera id) keep working; <id>_sub relays it locally for SD live
view and <id>_hd pulls the main stream on demand. "main" (the default) is unchanged.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_record_stream.py   (from backend/)
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-recstream-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import mediamtx  # noqa: E402
from nvr.config import settings  # noqa: E402

CAM = {"id": "yard", "name": "Yard", "host": "10.20.7.11", "username": "u", "password": "p", "main_path": "/main", "sub_path": "/sub",
       "onvif_port": 80, "rtsp_port": 554, "enabled": 1}


def test_main_is_unchanged():
    for cam in (CAM, {**CAM, "record_stream": "main"}, {**CAM, "record_stream": None}):
        p = mediamtx.build_config([cam])["paths"]
        assert set(p) == {"yard", "yard_sub"}
        assert p["yard"] == {"source": "rtsp://u:p@10.20.7.11:554/main", "rtspTransport": "tcp", "record": True}
        assert p["yard_sub"]["source"] == "rtsp://u:p@10.20.7.11:554/sub" and p["yard_sub"]["sourceOnDemand"] is True
        assert "record" not in p["yard_sub"]
    assert mediamtx.camera_paths(CAM) == ["yard", "yard_sub"]


def test_sub_records_the_sub_stream():
    cam = {**CAM, "record_stream": "sub"}
    p = mediamtx.build_config([cam])["paths"]
    assert set(p) == {"yard", "yard_sub", "yard_hd"}
    # the recorded path keeps the camera's id (playback / clips / frames / verifier) but pulls the sub stream
    assert p["yard"] == {"source": "rtsp://u:p@10.20.7.11:554/sub", "rtspTransport": "tcp", "record": True}
    # SD live relays the recorded stream from this MediaMTX (no second upload from the site), with the reader password
    relay = p["yard_sub"]
    assert relay["sourceOnDemand"] is True and "record" not in relay
    assert relay["source"].endswith("@127.0.0.1:8554/yard") or relay["source"] == f"{settings.mediamtx_rtsp}/yard", relay["source"]
    user, pw = mediamtx.reader_credentials() or ("", "")
    if settings.rtsp_auth:
        assert relay["source"] == f"rtsp://{user}:{pw}@127.0.0.1:8554/yard"
    # HD live pulls the main stream from the camera only while watched
    assert p["yard_hd"]["source"] == "rtsp://u:p@10.20.7.11:554/main" and p["yard_hd"]["sourceOnDemand"] is True
    assert "record" not in p["yard_hd"]
    assert mediamtx.camera_paths(cam) == ["yard", "yard_hd"]   # bandwidth from the site: not the local relay


def test_sub_with_port_forward():
    cam = {**CAM, "record_stream": "sub", "public_host": "203.0.113.50", "public_rtsp_port": 5541}
    p = mediamtx.build_config([cam])["paths"]
    assert p["yard"]["source"] == "rtsp://u:p@203.0.113.50:5541/sub" and p["yard_hd"]["source"] == "rtsp://u:p@203.0.113.50:5541/main"


def test_api_path_names_fit():
    """<id>_hd must pass the WHEP / playback path checks (a camera id is up to 32 characters)."""
    import re

    from nvr import api
    assert api.MTX_PATH_RE.fullmatch("a" * 32 + "_hd") and re.fullmatch(r"[a-z0-9_]{1,40}", "a" * 32 + "_sub")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
