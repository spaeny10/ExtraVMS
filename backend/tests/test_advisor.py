"""Optimize-my-system checks over a hand-built context (no cameras, GPU or Ollama needed).

Run: ..\\.venv\\Scripts\\python.exe tests\\test_advisor.py   (from backend/)
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-advisor-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import advisor  # noqa: E402
from nvr.db import db  # noqa: E402

CAMS = [
    {"id": "cam1", "name": "Yard", "host": "10.0.0.1", "zones": [], "policies": [{"kind": "towing", "asset": "tower", "allowed": ["BIGView truck", "Ghost truck"], "priority": "high"}]},
    {"id": "cam2", "name": "Door", "host": "10.0.0.2", "zones": [{"name": "Area 1", "type": "include", "points": []}], "policies": []},
]


def ctx(**over):
    base = {
        "now": time.time(), "cameras": CAMS,
        "health": {"cam1": {"bitrate_mbps": 4.3, "gb_per_day": 46}, "cam2": {"bitrate_mbps": 1.9, "gb_per_day": 20}},
        "tracks": {"cam1": ["H265", "MPEG-4 Audio"], "cam2": ["H264", "G711"]},
        "retention": {"cameras": [{"gb_per_day": 46, "continuous_gb": 300, "kept_gb": 5}, {"gb_per_day": 20, "continuous_gb": 150, "kept_gb": 2}],
                      "disk": {"total_gb": 8000, "free_gb": 7400}, "alert": None},
        "vlm": {"ready": True, "size_gb": 6.3, "vram_gb": 5.5, "queue": 3, "latency_s": [90, 120, 70, 100, 130], "calls": {"synopsis": 40, "same_person": 60}},
        "events": {"cam1": {"total": 100, "rejected": 60, "short": 10, "fragments": 5, "away": 0, "vehicles": 80, "verified": 40},
                   "cam2": {"total": 50, "rejected": 2, "short": 3, "fragments": 12, "away": 0, "vehicles": 0, "verified": 48}},
        "ptz": {"cam1": {"home_token": "8", "return_home_min": 0, "away_s_24h": 5400}},
        "clocks": {"cam2": 41.0},
        "named": {"person": set(), "vehicle": {"BIGView truck"}},
        "baseline": [],
    }
    base.update(over)
    return base


def keys(findings):
    return sorted(f.key for f in findings)


def test_checks_fire_on_the_right_measurements():
    fs = advisor.run_checks(ctx())
    ks = keys(fs)
    assert "bitrate:cam1" in ks and "bitrate:cam2" not in ks          # 4.3 Mbps yes, 1.9 no
    assert "codec:cam2" in ks and "audio:cam1" in ks                    # H.264 main; AAC audio
    assert "ai:vram" in ks and "ai:slow" in ks and "ai:mix:same_person" in ks
    assert "events:rejected:cam1" in ks and "events:fragments:cam2" in ks and "zones:cam1" in ks
    assert "rule:cam1:towing" in ks and "Ghost truck" in next(f.title for f in fs if f.key == "rule:cam1:towing")
    assert "ptz:return:cam1" in ks and next(f for f in fs if f.key == "ptz:return:cam1").apply == {"action": "ptz_return_home", "camera_id": "cam1", "minutes": 5}
    assert "clock:cam2" in ks and "storage:slack" in ks
    # ordered high -> low
    impacts = [f.impact for f in fs]
    assert impacts == sorted(impacts, key=advisor.IMPACT.index)
    # every finding explains itself
    assert all(f.why and f.effect and (f.steps or f.apply) for f in fs)


def test_quiet_system_has_nothing_to_say():
    quiet = ctx(health={"cam1": {"bitrate_mbps": 2.0, "gb_per_day": 20}, "cam2": {"bitrate_mbps": 1.9, "gb_per_day": 20}},
                tracks={"cam1": ["H265", "G711"], "cam2": ["H265"]},
                retention={"cameras": [{"gb_per_day": 20, "continuous_gb": 200, "kept_gb": 1}, {"gb_per_day": 20, "continuous_gb": 200, "kept_gb": 1}],
                           "disk": {"total_gb": 1000, "free_gb": 400}, "alert": None},
                vlm={"ready": True, "size_gb": 5.4, "vram_gb": 5.4, "queue": 0, "latency_s": [12, 15, 20, 11, 14], "calls": {"synopsis": 90, "same_person": 5}},
                events={"cam1": {"total": 30, "rejected": 3, "short": 1, "fragments": 2, "away": 0, "vehicles": 10, "verified": 27}},
                ptz={"cam1": {"home_token": "8", "return_home_min": 5, "away_s_24h": 300}}, clocks={"cam2": 2.0},
                named={"person": set(), "vehicle": {"BIGView truck", "Ghost truck"}})
    fs = advisor.run_checks(quiet)
    assert fs == [], keys(fs)
    assert "within their comfortable ranges" in advisor.plain_summary([], 2)


def test_storage_short_and_alert():
    c = ctx(retention={"cameras": [{"gb_per_day": 200, "continuous_gb": 900, "kept_gb": 0}], "disk": {"total_gb": 1000, "free_gb": 50}, "alert": "disk full"})
    ks = keys(advisor.check_storage(c))
    assert "storage:short" in ks and "storage:alert" in ks


def test_dismiss_hides_until_the_measurement_changes():
    fs = advisor.run_checks(ctx())
    f = next(x for x in fs if x.key == "bitrate:cam1")
    advisor.dismiss(f.key, f.fingerprint)
    shown, hidden = advisor.visible(fs)
    assert f.key in keys(hidden) and f.key not in keys(shown)
    # the camera changed: bitrate now 6 Mbps -> different fingerprint -> back in view
    fs2 = advisor.run_checks(ctx(health={"cam1": {"bitrate_mbps": 6.0, "gb_per_day": 60}, "cam2": {"bitrate_mbps": 1.9}}))
    shown2, _ = advisor.visible(fs2)
    assert "bitrate:cam1" in keys(shown2)
    advisor.undismiss(f.key)
    assert "bitrate:cam1" in keys(advisor.visible(fs)[0])
    assert db.get_setting("advisor_dismissed") == {}


def test_plain_summary_leads_with_high_impact():
    fs = advisor.run_checks(ctx())
    text = advisor.plain_summary(fs, 2)
    assert text.startswith(f"{sum(1 for f in fs if f.impact == 'high')} things need attention")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
