"""tools/replay_verification.py: the dry run of the verifier over a camera's past events must never write. The site
database is opened read-only, the verifier is called with dry_run=True, and no file in the site's data folder changes.
Throwaway folders, fake verifier for clips (no YOLO), stored-box judgement for events without a clip.
Run: ..\\.venv\\Scripts\\python.exe tests\\test_replay_verification.py   (from backend/)
"""
import hashlib
import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="nvr-replay-test-"))
for k, sub in (("NVR_DATA_DIR", "data"), ("NVR_RECORDINGS_DIR", "recordings"), ("NVR_RUNTIME_DIR", "runtime")):
    os.environ[k] = str(TMP / sub)   # never the real server's database
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from nvr import parked, ruleevents  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402

spec = importlib.util.spec_from_file_location("replay_verification", Path(__file__).resolve().parents[2] / "tools" / "replay_verification.py")
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)

assert "nvr-replay-test-" in str(settings.data_dir)
CAR = 2
NOW = time.time()
db.upsert_camera({"id": "cam6", "name": "REOPANO Back Driveway", "host": "10.0.0.6", "onvif_port": 8000, "rtsp_port": 554,
                  "username": "u", "password": "p", "main_path": "/m", "sub_path": "/s", "enabled": 1, "zones": [],
                  "synopsis_labels": ["person", "vehicle"]})


def sample(ts, boxes):
    return {"ts": ts, "cam_box": [0, 0, 1, 1], "yolo": [{"cls": "car", "cls_id": CAR, "conf": c, "box": list(b)} for c, b in boxes],
            "match": None, "iou": 0.0}


def event(start, samples, clip=False, status="verified", conf=0.4):
    eid = db.create_event(camera_id="cam6", track_id=ruleevents.track_id("vehicle", start), camera_class="vehicle",
                          camera_conf=0.0, start_ts=start, end_ts=start + 5, path=[ruleevents.point(start)], status=status)
    db.update_event(eid, yolo_class="car", yolo_conf=conf, yolo_hits=3, snapshot=f"events/{eid}/snapshot.jpg",
                    clip=f"events/{eid}/clip.mp4" if clip else None, clip_start=start - 5,
                    detections={"samples": samples, "keyframes": [], "needed": 2, "source": ruleevents.SOURCE})
    return eid


def site_folder() -> Path:
    """A copy of the test database plus event folders, as a site's data folder looks (journal_mode=DELETE: no -wal)."""
    site = TMP / "site"
    site.mkdir()
    dst = sqlite3.connect(site / "nvr.db")
    src = sqlite3.connect(settings.data_dir / "nvr.db")
    src.backup(dst)
    src.close()
    dst.execute("PRAGMA journal_mode=DELETE")
    dst.close()
    return site


def fingerprint(folder: Path) -> dict:
    return {str(p.relative_to(folder)): (p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(folder.rglob("*")) if p.is_file()}


class DryVerifier:
    def __init__(self):
        self.calls = []

    def verify(self, ev, clip, clip_start, zone_list, dry_run=False):
        assert dry_run is True, "the replay must call the verifier in dry-run mode"
        assert clip.is_file()
        self.calls.append(ev["id"])
        return {"status": "rejected", "yolo_class": None, "yolo_conf": None, "yolo_hits": 0, "needed": 2,
                "rejected": "nothing moving: YOLO found only stationary car(s)"}


def test_replay_never_writes():
    parked_box = (0.40, 0.45, 0.52, 0.62)
    with_clip = event(NOW - 3600, [sample(NOW - 3600 + i, [(0.8, parked_box)]) for i in range(6)], clip=True)
    still = event(NOW - 3000, [sample(NOW - 3000 + i, [(0.85, parked_box)]) for i in range(6)])
    moving = event(NOW - 2400, [sample(NOW - 2400 + i, [(0.55, (0.1 + 0.03 * i, 0.45, 0.22 + 0.03 * i, 0.62))]) for i in range(6)],
                   status="rejected")
    unsure = event(NOW - 1800, [sample(NOW - 1800 + i, [(0.42, parked_box)]) for i in range(6)])
    db.set_setting(parked.key("cam6"), [])
    site = site_folder()
    for eid in (with_clip, still, moving, unsure):
        d = site / "events" / str(eid)
        d.mkdir(parents=True)
        cv2.imwrite(str(d / "snapshot.jpg"), np.zeros((125, 410, 3), np.uint8))   # 4096x1248 aspect
    (site / "events" / str(with_clip) / "clip.mp4").write_bytes(b"\x00" * 64)
    before = fingerprint(site)

    conn = replay.open_ro(site / "nvr.db")
    try:
        conn.execute("UPDATE events SET status='rejected'")
        raise AssertionError("the site database must be read-only")
    except sqlite3.OperationalError as e:
        assert "readonly" in str(e) or "read-only" in str(e) or "query_only" in str(e) or "attempt to write" in str(e), e
    lines = []
    load = parked.load
    v = DryVerifier()
    try:
        totals = replay.replay(conn, site, "cam6", NOW - 86400, v, out=lines.append)
    finally:
        parked.load = load
        conn.close()
    assert v.calls == [with_clip]
    text = "\n".join(lines)
    assert totals["events"] == 4 and totals["stored"] == 3, totals
    assert totals["moves"] == {"verified -> rejected": 3, "rejected -> verified": 1}, totals["moves"]
    assert totals["gated"] == 1 and totals["qwen"] == 0   # the moving car at 55% stays verified, no Qwen at 60%
    row = lambda eid: next(ln for ln in lines if ln.startswith(f"{eid:>7}  "))
    assert "nothing moving: YOLO found only stationary car(s)" in row(still) and "(stored boxes)" in row(still)
    assert "too unsure: YOLO car 42% < 50%" in row(unsure), text
    assert "No synopsis: YOLO 55% < 60%" in row(moving)
    assert "Totals:" in text
    assert fingerprint(site) == before, "the replay changed the site's data folder"
    assert not (site / "nvr.db-wal").exists() and not (site / "nvr.db-journal").exists()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
