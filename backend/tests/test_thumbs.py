"""Snapshot thumbnails: GET /api/events/{id}/media/snapshot.jpg?w= shrinks the 2592x1520 snapshot once and caches it.

Run: ..\.venv\Scripts\python.exe tests\test_thumbs.py   (from backend/)
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-thumbs-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from nvr import api  # noqa: E402


def test_width_rounding():
    assert api._thumb_width(160) == 320 and api._thumb_width(480) == 480 and api._thumb_width(500) == 640 and api._thumb_width(1280) == 960


def test_make_thumb_shrinks_and_keeps_aspect():
    d = Path(os.environ["NVR_DATA_DIR"]) / "events" / "1"
    d.mkdir(parents=True)
    src = d / "snapshot.jpg"
    Image.new("RGB", (2592, 1520), (20, 40, 60)).save(src, "JPEG", quality=92)
    dest = d / "thumb_480.jpg"
    api._make_thumb(src, dest, 480)
    with Image.open(dest) as im:
        assert im.size == (480, 281)
    assert dest.stat().st_size < src.stat().st_size / 10
    assert not dest.with_suffix(".tmp").exists()


def test_small_source_is_not_upscaled():
    d = Path(os.environ["NVR_DATA_DIR"]) / "events" / "2"
    d.mkdir(parents=True)
    src = d / "snapshot.jpg"
    Image.new("RGB", (300, 200)).save(src, "JPEG")
    api._make_thumb(src, d / "thumb_480.jpg", 480)
    with Image.open(d / "thumb_480.jpg") as im:
        assert im.size == (300, 200)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
