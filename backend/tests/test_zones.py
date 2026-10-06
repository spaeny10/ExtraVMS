"""Zone rules: named areas label places but never filter; include / exclude behave as before.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_zones.py   (from backend/)
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-zones-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from nvr import zones  # noqa: E402

DOOR = {"name": "Bathroom 2", "type": "area", "points": [[0.6, 0.2], [0.8, 0.2], [0.8, 0.6], [0.6, 0.6]]}
EXIT = {"name": "Exit door", "type": "area", "points": [[0.1, 0.1], [0.3, 0.1], [0.3, 0.4], [0.1, 0.4]]}
MASK = {"name": "Road", "type": "exclude", "points": [[0, 0.8], [1, 0.8], [1, 1], [0, 1]]}


def box(cx, bottom, w=0.06, h=0.2):
    return [cx - w / 2, bottom - h, cx + w / 2, bottom]


def test_areas_never_filter():
    zl = zones.normalize([DOOR, EXIT])
    assert all(z["type"] == "area" for z in zl)
    hallway = (0.45, 0.7)  # in no zone at all
    assert zones.allowed(hallway, zl) and zones.allowed((0.7, 0.4), zl)
    assert zones.path_allowed([[0, *box(0.45, 0.7), 0.9]], zl)
    img = np.full((90, 160, 3), 200, np.uint8)
    assert (zones.mask_frame(img, zl) == img).all()          # nothing grayed out for YOLO
    # mixed with a real mask: the mask still applies, areas still don't
    zl = zones.normalize([DOOR, MASK])
    assert not zones.allowed((0.5, 0.9), zl) and zones.allowed((0.45, 0.7), zl)


def test_areas_visited_in_order():
    # walks from the exit door, down the hallway, into Bathroom 2
    path = [[t, *box(x, y), 0.9] for t, x, y in [
        (100, 0.2, 0.35), (101, 0.2, 0.3), (102, 0.4, 0.5), (103, 0.55, 0.65), (104, 0.7, 0.55), (105, 0.72, 0.5), (106, 0.72, 0.45)]]
    v = zones.areas_visited(path, [DOOR, EXIT, MASK])
    assert [a["name"] for a in v] == ["Exit door", "Bathroom 2"], v
    assert v[0]["from"] == 100 and v[1]["from"] == 104 and v[1]["to"] == 106
    # a single sample brushing a doorway doesn't count
    assert zones.areas_visited([[1, *box(0.7, 0.5), 0.9], [2, *box(0.45, 0.7), 0.9]], [DOOR]) == []
    assert zones.areas_visited(path, [MASK]) == [] and zones.areas_visited([], [DOOR]) == []


def test_legacy_zones_default_to_include():
    zl = zones.normalize([{"name": "old", "points": [[0, 0], [0.5, 0], [0.5, 0.5]]}])
    assert zl[0]["type"] == "include" and not zones.allowed((0.9, 0.9), zl)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
