"""Bandwidth from the cameras (Mbit/s over 5 min, GB today / this month kept per day) and "site link down" (every
enabled camera silent for 90 s: one alert instead of one per camera), and how both reach the heartbeat summary.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_site_link.py   (from backend/)
"""
import asyncio
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-link-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import health, summary  # noqa: E402
from nvr.db import db  # noqa: E402

BASE = {"username": "u", "password": "p", "main_path": "/main", "sub_path": "/sub", "onvif_port": 80, "rtsp_port": 554, "enabled": 1}
CAMS = [{**BASE, "id": "c1", "name": "One", "host": "10.20.7.11"},
        {**BASE, "id": "c2", "name": "Two", "host": "10.20.7.12", "record_stream": "sub"}]


def metrics(counts: dict[str, float]) -> str:
    lines = []
    for path, n in counts.items():
        lines += [f'paths{{name="{path}",state="ready"}} 1', f'paths_inbound_bytes{{name="{path}",state="ready"}} {n}',
                  f'paths_readers{{name="{path}",readerType="rtspSession",state="ready"}} 1']
    return "\n".join(lines) + "\n"


def setup():
    db.execute("DELETE FROM cameras")
    db.set_setting(health.BANDWIDTH_KEY, None)
    for c in CAMS:
        db.upsert_camera(c)


def test_bandwidth():
    setup()
    h = health.StreamHealth()
    t0 = dt.datetime(2026, 10, 6, 12, 0).timestamp()
    # c1 records main (c1 + c1_sub come from the camera), c2 records sub (c2 + c2_hd; c2_sub is a local relay)
    rate = {"c1": 1_250_000, "c1_sub": 125_000, "c2": 250_000, "c2_hd": 0, "c2_sub": 999_999}   # bytes per 10 s
    for i in range(31):   # 5 minutes
        h.ingest(metrics({p: r * i for p, r in rate.items()}), now=t0 + i * 10)
    b = h.bandwidth(now=t0 + 300)
    assert abs(b["cameras"]["c1"] - 1.1) < 0.01 and abs(b["cameras"]["c2"] - 0.2) < 0.01 and abs(b["mbps"] - 1.3) < 0.01, b
    received = (1_250_000 + 125_000 + 250_000) * 30
    assert abs(b["today_gb"] - received / 1e9) < 0.01
    # persisted per day (site time) and summed for the month; a new day starts at 0
    h.flush()
    assert db.get_setting(health.BANDWIDTH_KEY) == {"2026-10-06": received}
    db.set_setting(health.BANDWIDTH_KEY, {"2026-10-01": 40e9, "2026-09-30": 99e9, "2026-10-06": received})
    h2 = health.StreamHealth()   # a restart reloads the totals
    b = h2.bandwidth(cams=db.cameras(enabled_only=True), now=t0)
    assert b["month_gb"] == round((40e9 + received) / 1e9, 1) and b["cameras"] == {"c1": None, "c2": None}
    h2.add_bytes(5e9, now=dt.datetime(2026, 10, 7, 0, 5).timestamp())
    b = h2.bandwidth(now=dt.datetime(2026, 10, 7, 0, 6).timestamp())
    assert b["today_gb"] == 5.0 and b["month_gb"] == round((45e9 + received) / 1e9, 1)
    old = {f"2026-07-{d:02d}": 1 for d in range(1, 32)} | {f"2026-08-{d:02d}": 1 for d in range(1, 32)}
    db.set_setting(health.BANDWIDTH_KEY, old)
    h3 = health.StreamHealth()
    h3.add_bytes(1, now=t0)
    assert len(db.get_setting(health.BANDWIDTH_KEY)) == health.BANDWIDTH_KEEP_DAYS


def test_link_down():
    setup()
    h = health.StreamHealth()
    t0 = 1_000_000.0
    n = {"c1": 0, "c2": 0}
    for i in range(10):          # both cameras deliver for 90 s
        n = {k: v + 100_000 for k, v in n.items()}
        h.ingest(metrics(n), now=t0 + i * 10)
    assert h.link_down(now=t0 + 90) is None
    last_good = t0 + 90
    for i in range(10, 21):      # the tunnel drops: counters freeze
        h.ingest(metrics(n), now=t0 + i * 10)
    assert h.link_down(now=t0 + 170) is None                 # 80 s: not yet
    a = h.link_down(now=t0 + 200)
    assert a and a["kind"] == "site_link_down" and a["since"] == last_good and a["text"] == health.LINK_DOWN_TEXT, a
    # one camera comes back: per-camera alerts again, no site alert
    n["c2"] += 5000
    h.ingest(metrics(n), now=t0 + 210)
    assert h.link_down(now=t0 + 210) is None
    # one enabled camera: its own camera_down says the same thing
    assert h.link_down(cams=[CAMS[0]], now=t0 + 400) is None
    # MediaMTX metrics unreadable: not the site's link
    h.error = "connection refused"
    assert h.link_down(now=t0 + 400) is None


def test_link_down_cameras_never_heard_from():
    setup()
    h = health.StreamHealth()
    h.ingest(metrics({}), now=5000.0)                        # MediaMTX up, no camera path ever connected
    for t in (5010.0, 5020.0, 5100.0):
        h.ingest(metrics({}), now=t)
    a = h.link_down(now=5100.0)
    assert a and a["since"] == 5000.0


def test_summary_flags():
    setup()
    h = health.StreamHealth()
    h.ingest(metrics({"c1": 1, "c2": 1}), now=1000.0)
    h.ingest(metrics({"c1": 1, "c2": 1}), now=1100.0)
    h.link_down = (lambda orig: (lambda cams=None, now=None: orig(cams, now=1100.0)))(h.link_down)
    queue = SimpleNamespace(qsize=lambda: 0)
    pipeline = SimpleNamespace(verify_q=queue, synopsis_q=queue, vlm_ready=True, verifier=None, yolo_fallback=None,
                               verify_stall={"stalled": False}, detector=None, hailo=None)
    state = SimpleNamespace(ingests={}, health=h, ptz=None, pipeline=pipeline)
    import nvr.detector as detector
    orig = detector.yolo_status, detector.health_alerts
    detector.yolo_status = lambda p: {"yolo_ready": True, "yolo_fallback": None}
    detector.health_alerts = lambda p: []
    try:
        s = asyncio.run(summary.site_summary(state, version="t"))
    finally:
        detector.yolo_status, detector.health_alerts = orig
    assert s["site_link_down"] is True
    assert [a["kind"] for a in s["health_alerts"]] == ["site_link_down"]
    assert all(c["link_down"] is True and c["problems"] == [] for c in s["cameras"]), s["cameras"]
    assert set(s["bandwidth"]) == {"mbps", "today_gb", "month_gb", "cameras"} and set(s["bandwidth"]["cameras"]) == {"c1", "c2"}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
