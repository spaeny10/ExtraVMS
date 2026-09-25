"""Verifier tolerance for camera-clock error: one consistent time shift, never a per-frame free-for-all.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_verify_shift.py   (from backend/)
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-shift-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.verifier import best_shift, box_at  # noqa: E402

CAR, TRUCK = 2, 7
ALLOWED = {CAR, TRUCK}


def moving_path(t0=0.0, dur=6.8, v=-0.11, x0=0.74, w=0.24):
    """A vehicle driving right to left, sampled every 0.2 s (like the Side Yard event 3608)."""
    pts, t = [], t0
    while t <= t0 + dur:
        x = x0 + v * (t - t0)
        pts.append([t, x, 0.35, x + w, 0.52, 0.9])
        t += 0.2
    return pts


def yolo(box, cls=TRUCK, conf=0.8):
    return {"cls": "truck", "cls_id": cls, "conf": conf, "box": list(box)}


def test_clock_offset_is_recovered():
    path = moving_path()
    lag = 3.9  # the frames show where the camera track says the vehicle is 3.9 s later
    frames = [(ts, [yolo(box_at(path, ts + lag))]) for ts in (0.6, 1.6, 2.4)]
    assert box_at(path, 0.6)[0] - box_at(path, 0.6 + lag)[0] > 0.4    # far apart: plain matching fails
    shift, hits = best_shift(path, frames, ALLOWED)
    assert abs(shift - lag) <= 0.5 and hits == 3, (shift, hits)


def test_parked_car_does_not_fake_a_match():
    path = moving_path()
    parked = yolo((0.30, 0.36, 0.52, 0.50))  # sits on the track's route the whole time
    frames = [(ts, [parked]) for ts in (0.6, 1.6, 2.4, 3.6, 5.2)]
    _, hits = best_shift(path, frames, ALLOWED)
    assert hits <= 2, hits  # only frames where the shifted track happens to pass it; not all of them


def test_wrong_class_ignored():
    path = moving_path()
    frames = [(ts, [yolo(box_at(path, ts + 2.0), cls=0)]) for ts in (0.6, 1.6, 2.4)]  # a "person" box
    assert best_shift(path, frames, ALLOWED)[1] == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
