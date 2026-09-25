"""Stream health from MediaMTX metrics: parsing, counter resets, stalls, corrupt frames (no network).

Run: ..\\.venv\\Scripts\\python.exe tests\\test_health.py   (from backend/)
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-health-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.health import PathStats, StreamHealth, parse_metrics  # noqa: E402

TEXT = """# HELP paths
paths{name="cam1",state="ready"} 1
paths_readers{name="cam1",readerType="rtspSession",state="ready"} 1
paths_inbound_bytes{name="cam1",state="ready"} 1000000
paths_inbound_frames_in_error{name="cam1",state="ready"} 2
paths{name="cam1_sub",state="ready"} 1
paths_inbound_bytes{name="cam1_sub",state="ready"} 500
webrtc_sessions{id="x",state="ready"} 1
"""


def test_parse():
    m = parse_metrics(TEXT)
    assert set(m) == {"cam1", "cam1_sub"}
    assert m["cam1"]["inbound_bytes"] == 1e6 and m["cam1"]["frames_in_error"] == 2 and m["cam1"]["readers"] == {"rtspSession": 1}
    assert m["cam1"]["state"] == "ready" and m["cam1_sub"]["readers"] == {}


def sample(bytes_, err=0, readers=None):
    return {"inbound_bytes": float(bytes_), "frames_in_error": float(err), "state": "ready", "readers": readers if readers is not None else {"rtspSession": 1}}


def test_bitrate_stall_and_counter_reset():
    p = PathStats()
    t = 1000.0
    for i in range(7):  # 1 Mbps for a minute: 1.25 MB per 10 s
        p.update(t + i * 10, sample(1_250_000 * (i + 1)))
    assert abs(p.bitrate_mbps() - 1.0) < 0.01 and abs(p.gb_per_day() - 10.8) < 0.1
    # the path is recreated (counter restarts) but keeps flowing: no phantom drop
    p.update(t + 70, sample(1_250_000))
    p.update(t + 80, sample(2_500_000))
    assert abs(p.bitrate_mbps() - 1.0) < 0.1
    # then it freezes
    for i in range(9, 14):
        p.update(t + i * 10, sample(2_500_000))
    assert p.bitrate_mbps() == 0 and t + 130 - p.last_increase == 50


def test_camera_problems():
    h = StreamHealth()
    h.last_sample = 1.0
    p = h.paths.setdefault("cam9", PathStats())
    import time
    now = time.time()
    p.update(now - 40, sample(100, err=0))
    p.update(now - 30, sample(200, err=0))
    p.update(now - 20, sample(200, err=3, readers={}))   # frozen, 3 bad frames, reader gone
    p.update(now - 10, sample(200, err=3, readers={}))
    c = h.camera("cam9")
    assert c["stalled_s"] >= 30 and c["frames_in_error_1h"] == 3 and c["metadata_reader"] is False
    assert len(c["problems"]) == 3 and "no video" in c["problems"][0] and "detections" in c["problems"][1], c["problems"]
    assert h.camera("nope")["problems"] == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
