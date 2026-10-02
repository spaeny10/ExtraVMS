"""A camera whose address is not an IP/hostname (a name typed in the wrong field) must not stop MediaMTX for the
whole site: the API rejects it and the config generator leaves it out. Run as a script (test.ps1) or under pytest."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-host-test-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pydantic  # noqa: E402

from nvr import mediamtx  # noqa: E402
from nvr.api import CameraIn  # noqa: E402

CAM = {"id": "cam1", "name": "Yard", "host": "192.168.1.10", "username": "u", "password": "p!", "main_path": "/main", "sub_path": "/sub",
       "onvif_port": 80, "rtsp_port": 554, "enabled": 1}


def test_valid_host():
    for h in ("192.168.105.6", "cam-7.local", "nvr.example.com", "10.0.0.1"):
        assert mediamtx.valid_host(h), h
    for h in ("SW Outside Doorway", "", None, "rtsp://x", "192.168.1.10:554", "a b", "-bad"):
        assert not mediamtx.valid_host(h), h


def test_bad_host_is_left_out_of_mediamtx_config():
    bad = {**CAM, "id": "cam2", "host": "SW Outside Doorway"}
    cfg = mediamtx.build_config([CAM, bad])
    assert "cam1" in cfg["paths"] and "cam1_sub" in cfg["paths"]
    assert "cam2" not in cfg["paths"] and "cam2_sub" not in cfg["paths"]
    assert cfg["paths"]["cam1"]["source"].startswith("rtsp://u:p%21@192.168.1.10:554/main")


def test_api_rejects_a_name_in_the_address_field():
    ok = CameraIn(id="cam2", name="SW Outside Doorway", host="192.168.105.6")
    assert ok.host == "192.168.105.6"
    try:
        CameraIn(id="cam2", name="192.168.105.6", host="SW Outside Doorway")
    except pydantic.ValidationError as e:
        assert "host" in str(e)
    else:
        raise AssertionError("a host with spaces was accepted")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
