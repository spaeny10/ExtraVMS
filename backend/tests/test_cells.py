"""Region cells (cells.py): grid mapping, bitmap encoding, which cells a path visits, backfill and API rows.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_cells.py   (from backend/)
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-cells-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import cells  # noqa: E402
from nvr.db import db  # noqa: E402

W, H = cells.GRID_W, cells.GRID_H


def box(cx, bottom, w=0.02, h=0.1):
    return [cx - w / 2, bottom - h, cx + w / 2, bottom]


def test_cell_mapping_and_clamping():
    assert cells.cell(0, 0) == 0
    assert cells.cell(1.0, 1.0) == W * H - 1
    assert cells.cell(-0.1, 2.0) == (H - 1) * W
    assert cells.cell(0.5, 0.5) == 9 * W + 16


def test_encode_decode_round_trip():
    s = cells.encode({0, 7, 8, W * H - 1})
    assert len(s) == 96 and "=" not in s
    assert cells.decode(s) == {0, 7, 8, W * H - 1}
    assert cells.decode(cells.encode(set())) == set()
    assert cells.for_event([]) is None
    assert cells.overlaps(cells.encode({5, 6}), cells.encode({6})) and not cells.overlaps(cells.encode({5}), cells.encode({6}))
    assert not cells.overlaps(None, cells.encode({1}))


def test_narrow_box_marks_only_the_foot_cell():
    assert cells.path_cells([[0.0, *box(0.5, 0.5)]]) == {cells.cell(0.5, 0.5)}


def test_wide_box_marks_columns_under_its_middle_half():
    got = cells.path_cells([[0.0, *box(0.5, 0.5, w=0.25)]])
    row = cells.cell(0.5, 0.5) // W
    cols = {c % W for c in got}
    assert all(c // W == row for c in got)
    assert cols >= {cells.cell(0.5 - 0.0625, 0.5) % W, cells.cell(0.5 + 0.0625, 0.5) % W}
    assert cells.cell(0.5 - 0.12, 0.5) not in got  # outer quarter of the box is not counted


def test_gap_fill_between_close_samples_only():
    fast = cells.path_cells([[0.0, *box(0.1, 0.5)], [0.3, *box(0.9, 0.5)]])
    assert cells.cell(0.5, 0.5) in fast and len(fast) > 20
    slow = cells.path_cells([[0.0, *box(0.1, 0.5)], [5.0, *box(0.9, 0.5)]])
    assert slow == {cells.cell(0.1, 0.5), cells.cell(0.9, 0.5)}


def test_backfill_and_api_rows():
    db.upsert_camera({"id": "cam1", "name": "Cam", "host": "127.0.0.1", "onvif_port": 80, "rtsp_port": 554, "username": "u",
                      "password": "", "main_path": "/m", "sub_path": "/s", "enabled": 1, "zones": [], "retention_days": None,
                      "scene_notes": "", "retention_policy": None})
    now = time.time()
    path = [[now, *box(0.3, 0.6)], [now + 0.2, *box(0.32, 0.61)]]
    a = db.create_event(camera_id="cam1", track_id="a", camera_class="person", camera_conf=0.9, start_ts=now,
                        end_ts=now + 3, path=path, status="verified")
    b = db.create_event(camera_id="cam1", track_id="b", camera_class="person", camera_conf=0.9, start_ts=now + 1,
                        path=path, status="open")
    c = db.create_event(camera_id="cam1", track_id="c", camera_class="person", camera_conf=0.9, start_ts=now + 2,
                        end_ts=now + 4, path=[], status="rejected")
    assert cells.backfill() == 1
    assert db.event(a)["cells"] == cells.for_event(path) and db.event(b)["cells"] is None and db.event(c)["cells"] is None
    assert cells.backfill() == 0

    from nvr import api, mediamtx
    async def no_spans(*_a, **_k):
        return []
    mediamtx.recording_spans = no_spans
    from nvr.pipeline import Pipeline
    api.state.pipeline = Pipeline()  # list_events annotates rows with live pipeline state
    rows = asyncio.run(api.recordings("cam1", now - 10, now + 10))["events"]
    assert {r["id"]: r["cells"] for r in rows}[a] == cells.for_event(path)
    listed = asyncio.run(api.list_events(camera="cam1", status="verified", min_yolo=0, limit=50))
    assert listed[0]["cells"] == cells.for_event(path)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
