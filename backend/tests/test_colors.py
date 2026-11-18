"""Event colors for Find (colors.py): from the crops, the camera's read and Otso's answer; the filter.

Synthetic crops (no camera, GPU or network): a red car with dark windows and tires, a white car, a person in a green
top and black pants, an infrared night crop, a gray hoodie (the crops get gray tops wrong: Otso names those).

Run: ..\\.venv\\Scripts\\python.exe tests\\test_colors.py   (from backend/)
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="nvr-colors-test-"))
for k, sub in (("NVR_DATA_DIR", "data"), ("NVR_RECORDINGS_DIR", "recordings"), ("NVR_RUNTIME_DIR", "runtime")):
    os.environ[k] = str(TMP / sub)   # never the real server's database or recordings
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from nvr import colors, synopsis  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db, event_filters  # noqa: E402

for d in (settings.data_dir, settings.recordings_dir):
    v = str(d).replace("\\", "/").lower()
    assert "nvr-colors-test-" in v and "e:/nvr" not in v and "d:/nvr" not in v, d

db.upsert_camera({"id": "cam1", "name": "Yard", "host": "10.0.0.9", "zones": []})

BGR = {"red": (40, 40, 200), "white": (235, 235, 235), "black": (20, 20, 20), "green": (60, 150, 50),
       "gray": (128, 128, 128), "road": (110, 115, 120), "blue": (190, 90, 40)}


def car(body: str) -> np.ndarray:
    img = np.full((120, 200, 3), BGR[body], np.uint8)
    img[15:45, 40:160] = BGR["black"]    # windows
    img[95:120, 20:60] = BGR["black"]    # wheels
    img[95:120, 140:180] = BGR["black"]
    return img


def person(top: str, bottom: str) -> np.ndarray:
    img = np.full((200, 80, 3), BGR["road"], np.uint8)
    img[0:35, 25:55] = (120, 150, 200)      # head
    img[35:105, 10:70] = BGR[top]
    img[105:185, 18:62] = BGR[bottom]
    return img


def noisy(img: np.ndarray, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.clip(img.astype(np.int16) + rng.integers(-12, 12, img.shape), 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- the crops

def test_cars_and_clothing_from_the_crops():
    assert colors.crop_colors([noisy(car("red"), i) for i in range(3)], "vehicle") == {"vehicle": "red"}
    assert colors.crop_colors([noisy(car("white"), i) for i in range(3)], "vehicle") == {"vehicle": "white"}, \
        "white beats the black windows and tires"
    assert colors.crop_colors([noisy(person("green", "black"), i) for i in range(4)], "person") == {"top": "green", "bottom": "black"}
    assert colors.crop_colors([noisy(person("red", "blue"), i) for i in range(2)], "person") == {"top": "red", "bottom": "blue"}


def test_the_crops_leave_out_what_they_get_wrong():
    """Gray and white tops measured 32% and 4% right from the crops, black vehicles 62%: Otso names those."""
    assert colors.crop_colors([noisy(person("gray", "black"), i) for i in range(3)], "person") == {"bottom": "black"}
    assert colors.crop_colors([noisy(car("gray"), 1)], "vehicle") == {}
    assert colors.crop_colors([noisy(car("black"), 1)], "vehicle") == {}


def test_infrared_and_split_votes_name_nothing():
    ir = cv2.cvtColor(cv2.cvtColor(person("green", "blue"), cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    assert colors.crop_colors([ir, ir], "person") == {}
    assert colors.crop_colors([car("red"), car("blue"), car("green")], "vehicle") == {}, "no color has half the votes"
    assert colors.crop_colors([], "vehicle") == {} and colors.crop_colors([car("red")], "animal") == {}


# ---------------------------------------------------------------- Otso and the camera

def test_otso_text_and_fields():
    assert colors.from_text("A man wearing an olive green hooded jacket and dark blue jeans walked in", "top") == "green"
    assert colors.from_text("A man wearing an olive green hooded jacket and dark blue jeans walked in", "bottom") == "blue"
    assert colors.from_text("A white Ford F-150 pickup truck backs into the lot", "vehicle") == "white"
    assert colors.from_text("A grey sedan", "vehicle") == "gray" and colors.from_text("A man in jeans", "bottom") == "blue"
    assert colors.from_text("A man with dark hair walks", "top") is None
    s = {"objects": [{"type": "person", "description": "man", "color": "none", "top_color": "red", "bottom_color": "none"},
                     {"type": "vehicle", "description": "a car", "color": "white", "top_color": "none", "bottom_color": "none"}]}
    assert colors.from_synopsis(s, "A man in a red shirt and khaki pants", "person") == {"top": "red", "bottom": "brown"}, \
        "the field where there is one, the text for the rest"
    assert colors.from_synopsis(s, "", "vehicle") == {"vehicle": "white"}
    for key in ("color", "top_color", "bottom_color"):   # Otso's answer carries them, one word from the same list
        prop = synopsis.SCHEMA["properties"]["objects"]["items"]["properties"][key]
        assert set(prop["enum"]) == set(colors.COLORS) | {"none"}
    assert {"color", "top_color", "bottom_color"} <= set(synopsis.SCHEMA["properties"]["objects"]["items"]["required"])


def test_otso_first_then_the_camera_then_the_crops():
    e = {"camera_class": "person", "synopsis": "A person in a gray hoodie walks by.",
         "camera_attrs": {"bottom_color": {"Black": [3, 1.5]}, "top_color": {"green": [2, 1.0]}}}
    out = colors.combine(e, {"top": "blue", "bottom": "blue"})
    assert (out["top"], out["bottom"]) == ("gray", "black"), out
    assert out["by"] == {"top": "otso", "bottom": "camera"} and out["picture"] == {"top": "blue", "bottom": "blue"}
    assert colors.combine({"camera_class": "vehicle"}, {"vehicle": "red"})["by"] == {"vehicle": "picture"}
    assert colors.combine({"camera_class": "vehicle"}, {}) is None
    assert colors.combine({"camera_class": "motion", "yolo_class": "truck"}, {"vehicle": "red"})["vehicle"] == "red"
    assert colors.describe({"top": "green", "bottom": "black"}, "person") == "green top, black bottoms"


# ---------------------------------------------------------------- per event, the filter, search

def make_event(label: str, crops: list[np.ndarray], **kw) -> int:
    t = time.time() - 3600
    box = [0.2, 0.2, 0.6, 0.9]
    det = {"samples": [{"ts": t + i, "match": {"cls": label, "conf": 0.9, "box": box}, "cam_box": box} for i in range(len(crops))],
           "keyframes": [{"file": f"crop_{i}.jpg", "ts": t + i, "kind": "crop", "box": [0, 0, 1, 1]} for i in range(len(crops))]}
    eid = db.create_event(camera_id="cam1", track_id="t", camera_class=label, start_ts=t, end_ts=t + 5, status="verified",
                          path=[[t, *box, 0.9]], detections=det, yolo_class="car" if label == "vehicle" else label, **kw)
    d = settings.data_dir / "events" / str(eid)
    d.mkdir(parents=True, exist_ok=True)
    for i, img in enumerate(crops):
        cv2.imwrite(str(d / f"crop_{i}.jpg"), img)
    return eid


def test_event_colors_are_stored_filtered_and_searched():
    red = make_event("vehicle", [noisy(car("red"), i) for i in range(3)])
    green = make_event("person", [noisy(person("green", "black"), i) for i in range(3)])
    plain = make_event("person", [noisy(person("gray", "gray"), i) for i in range(3)])   # the crops can't name gray
    assert colors.event_colors(red)["vehicle"] == "red"
    assert colors.event_colors(green) == {"top": "green", "bottom": "black", "by": {"top": "picture", "bottom": "picture"},
                                          "picture": {"top": "green", "bottom": "black"}}
    assert colors.event_colors(plain) is None and db.event(plain)["colors"] is None

    def ids(**kw):
        where, params = event_filters(**kw)
        return {r["id"] for r in db.all(f"SELECT id FROM events WHERE {' AND '.join(where) or '1'}", params)}
    assert ids(color="red") == {red}
    assert ids(color="green", color_part="top") == {green} and ids(color="green", color_part="bottom") == set()
    assert ids(color="black") == {green} and ids(color="black", label="vehicle") == set()
    for bad in ({"color": "chartreuse"}, {"color": "red", "color_part": "hat"}):
        try:
            event_filters(**bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} should be refused")

    # Otso answers later: its colors come first, the crops' are kept for the next combine
    db.update_event(plain, synopsis="A man in a gray hoodie and khaki pants.")
    new = colors.refresh(db.event(plain))
    assert (new["top"], new["bottom"], new["by"]["top"]) == ("gray", "brown", "otso")
    assert colors.refresh(db.event(plain)) is None, "nothing changed"

    from nvr import pipeline as pl

    async def embed(text):
        return None
    pl.vlm.embed = embed
    asyncio.run(pl.Pipeline().reindex(green))
    assert any(h["id"] == green for h in db.search("green top", None))


def test_backfill_names_earlier_events_once():
    a = make_event("vehicle", [noisy(car("blue"), i) for i in range(2)])
    b = make_event("person", [], synopsis="A woman in a pink jacket and white shorts")   # no crops: Otso's words
    last, seen, named = colors.backfill_batch(a - 1, 0)
    assert seen == 2 and named == 2 and last == b
    assert db.event(a)["colors"]["vehicle"] == "blue" and db.event(b)["colors"]["top"] == "pink"
    assert colors.backfill_batch(last, 0) == (0, 0, 0)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
