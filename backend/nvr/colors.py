"""Color names for an event's object: a vehicle's body color, a person's top and bottoms, for Find's color filter.

Three sources, best first, per part:
1. Otso's answer: each object it describes carries color / top_color / bottom_color (synopsis.SCHEMA) from the
   full-resolution frames; older answers have only text, read for "<color> <garment>" ("olive green hoodie").
2. The camera's own read (camattrs.py: a Milesight 8477 sends clothing and vehicle colors now and then).
3. The event's saved crops (crop_0..3.jpg, cut tight to YOLO's box): each pixel of a region is named (black, white,
   gray or a hue); a region's answer is its dominant hue when there is enough of it (a red car is red although its
   windows and tires are black), else its most common neutral; the crops vote. Infrared night pictures name nothing.

Measured on TSS-DT against Otso's words (2,500 events, 2026-11-18), from the crops alone: tops red 94%, green 96%,
black 88%; bottoms black 70%, blue 79%, brown 95%; vehicles white 74%. But tops white 4%, gray 32%, blue 41% (a gray
hoodie overexposed reads white, under a cool indoor light blue), orange 0-7% (the wooden floor behind the legs), and
vehicles black 62% (a white car in haze on the pano, between the dark cars of a dealership lot). CLIP on the same
regions did no better (70%). So the crops name a part only in the colors they get right (PICTURE_OK); Otso and the
camera name the rest.

Stored in events.colors: {"top": "green", "bottom": "black", "by": {"top": "otso", ...}, "picture": {...}}; the
picture part is kept so a later answer from Otso can be combined again without the crops (refresh).
"""
from __future__ import annotations

import re
from collections import Counter

import numpy as np

COLORS = ("black", "white", "gray", "red", "orange", "yellow", "green", "blue", "purple", "pink", "brown")
PARTS = {"vehicle": ("vehicle",), "person": ("top", "bottom")}
# region of the object's box (x1, y1, x2, y2 as fractions of it) each part is read from
REGIONS = {
    "vehicle": (0.12, 0.2, 0.88, 0.75),   # the body: above the wheels and road shadow
    "top": (0.28, 0.2, 0.72, 0.48),       # torso, below the head, inside the arms
    "bottom": (0.32, 0.56, 0.68, 0.86),   # legs, above the feet
}
# the colors the crops name reliably for each part (see the measurements above)
PICTURE_OK = {"vehicle": set(COLORS) - {"black", "gray"},
              "top": {"black", "red", "green", "yellow", "purple", "pink"},
              "bottom": {"black", "blue", "brown"}}
MIN_CHROMA = {"vehicle": 0.22, "top": 0.3, "bottom": 0.3}   # share of a region a hue needs to name it
BLACK_V = 75              # value below this: black (dark clothing indoors sits at 60-90)
WHITE_V = 160             # value at or above this (and unsaturated): white
CHROMA_S, CHROMA_V = 50, 55   # a pixel has a hue at or above this saturation and value
WHITE_SHARE = 0.25        # vehicles: white wins among the neutrals once it holds this share (windows and tires are black)
CENTER_SIGMA = 0.3        # pixels count less away from the box's middle (the next car in a lot sits at its edges)
NEUTRAL_LEAD = 1.3        # a neutral names the region only when it has this many times the next neutral's share
NIGHT_SAT = 14.0          # a crop with mean saturation below this and almost no colored pixels is infrared
MIN_PIXELS = 60


def label_of(e: dict) -> str | None:
    cls = e.get("camera_class")
    if cls not in PARTS:
        cls = "vehicle" if e.get("yolo_class") in ("car", "truck", "bus", "motorcycle") else e.get("yolo_class")
    return cls if cls in PARTS else None


# ---------------------------------------------------------------- the crops

def _names(hsv: np.ndarray) -> np.ndarray:
    """One color index (into COLORS) per pixel of an (N, 3) OpenCV HSV array (H 0-180, S and V 0-255)."""
    h, s, v = hsv[:, 0].astype(np.int32), hsv[:, 1].astype(np.int32), hsv[:, 2].astype(np.int32)
    out = np.full(len(hsv), COLORS.index("gray"), np.int8)
    out[v >= WHITE_V] = COLORS.index("white")
    chroma = (s >= CHROMA_S) & (v >= CHROMA_V)
    hue = np.select(
        [(h < 8) | (h >= 168), h < 21, h < 34, h < 86, h < 128, h < 146, h < 168],
        [COLORS.index(c) for c in ("red", "orange", "yellow", "green", "blue", "purple", "pink")], 0)
    hue = np.where(chroma & (h < 25) & (v < 150) & (s < 200), COLORS.index("brown"), hue)
    hue = np.where(chroma & (h >= 21) & (h < 34) & (v < 140), COLORS.index("green"), hue)   # dark yellow is olive
    out = np.where(chroma, hue, out)
    out[v < BLACK_V] = COLORS.index("black")
    return out


def region_color(bgr: np.ndarray, part: str) -> tuple[str, float] | None:
    """(color, share of the region) for one crop's region, or None (too small, or night)."""
    import cv2
    hh, ww = bgr.shape[:2]
    x1, y1, x2, y2 = REGIONS[part]
    cut = bgr[int(hh * y1):max(int(hh * y2), int(hh * y1) + 1), int(ww * x1):max(int(ww * x2), int(ww * x1) + 1)]
    if cut.size == 0 or cut.shape[0] * cut.shape[1] < MIN_PIXELS:
        return None
    hsv = cv2.cvtColor(cut, cv2.COLOR_BGR2HSV).reshape(-1, 3)
    if CENTER_SIGMA:
        yy, xx = np.mgrid[0:cut.shape[0], 0:cut.shape[1]]
        wgt = np.exp(-(((xx / max(1, cut.shape[1] - 1)) - 0.5) ** 2 + ((yy / max(1, cut.shape[0] - 1)) - 0.5) ** 2)
                     / (2 * CENTER_SIGMA ** 2)).reshape(-1)
    else:
        wgt = np.ones(len(hsv))
    counts = np.bincount(_names(hsv), weights=wgt, minlength=len(COLORS)) / wgt.sum()
    neutral = [COLORS.index(c) for c in ("black", "white", "gray")]
    chromatic = [i for i in range(len(COLORS)) if i not in neutral]
    if float(hsv[:, 1].mean()) < NIGHT_SAT and counts[chromatic].sum() < 0.03:
        return None
    best = max(chromatic, key=lambda i: counts[i])
    if counts[best] >= MIN_CHROMA[part]:
        return COLORS[best], round(float(counts[best]), 2)
    if part == "vehicle" and counts[COLORS.index("white")] >= WHITE_SHARE:
        return "white", round(float(counts[COLORS.index("white")]), 2)
    first, second = sorted(neutral, key=lambda i: counts[i], reverse=True)[:2]
    if counts[first] < NEUTRAL_LEAD * counts[second]:
        return None   # black, gray and white about even: can't tell
    return COLORS[first], round(float(counts[first]), 2)


def crop_colors(crops: list[np.ndarray], label: str | None) -> dict:
    """{part: color} from the event's crops (each already cut to the object's box): the crops vote per part; a part
    is named when at least half of the crops that could tell agree, and only in a color the crops get right."""
    out = {}
    for part in PARTS.get(label or "", ()):
        votes = [c for c in (region_color(img, part) for img in crops) if c]
        if not votes:
            continue
        tally, share = Counter(c for c, _ in votes), Counter()
        for c, s in votes:
            share[c] += s
        color, n = max(tally.items(), key=lambda kv: (kv[1], share[kv[0]]))
        if n * 2 >= len(votes) and color in PICTURE_OK[part]:
            out[part] = color
    return out


# ---------------------------------------------------------------- Otso and the camera

WORDS = {"grey": "gray", "silver": "gray", "charcoal": "gray", "navy": "blue", "denim": "blue", "maroon": "red",
         "burgundy": "red", "olive": "green", "teal": "green", "tan": "brown", "beige": "brown", "khaki": "brown",
         "gold": "yellow", "golden": "yellow", "violet": "purple", "cyan": "blue"}
_COLOR_RX = "|".join(sorted([*COLORS, *WORDS], key=len, reverse=True))
_NOUNS = {
    "top": r"(?:t-shirt|shirt|tee|jacket|hoodie|sweatshirt|top|vest|coat|sweater|polo|blouse|pullover|uniform|fleece|jersey|parka)",
    "bottom": r"(?:pants|jeans|shorts|trousers|skirt|sweatpants|joggers|slacks|leggings|overalls)",
    "vehicle": r"(?:sedan|suv|truck|pickup|van|car|vehicle|minivan|hatchback|coupe|jeep|wagon|crossover|bus|motorcycle)",
}


def _word(w: str | None) -> str | None:
    w = (w or "").strip().lower()
    w = WORDS.get(w, w)
    return w if w in COLORS else None


def from_text(text: str | None, part: str) -> str | None:
    """The first "<color> [word] <garment / vehicle>" in Otso's text: "olive green hooded jacket" -> green."""
    if not text:
        return None
    # up to three words between ("white Ford F-150 pickup truck"), never across "and", another color or a garment
    # ("blue jeans and a green jacket" has a green top)
    stop = rf"(?:and|with|or|but|{_COLOR_RX}|{_NOUNS['top']}|{_NOUNS['bottom']})\b"
    m = re.search(rf"\b({_COLOR_RX})(?:[\s-]+(?:and|light|dark|bright|pale|deep|{_COLOR_RX}))*[\s-]+"
                  rf"(?:(?!{stop})[a-z0-9-]+\s){{0,3}}{_NOUNS[part]}\b", text.lower())
    if m:
        return _word(m.group(1))
    if part == "bottom" and re.search(r"\bjeans\b", text.lower()):
        return "blue"
    return None


_PERSON = re.compile(r"person|man|woman|male|female|worker|pedestrian|people|child|boy|girl|individual|subject", re.I)
_VEHICLE = re.compile(_NOUNS["vehicle"] + "|car|automobile", re.I)


def from_synopsis(synopsis_json: dict | None, synopsis: str | None, label: str) -> dict:
    """{part: color} from Otso's answer: the first object of the event's kind with color fields, else its text."""
    out = {}
    s = synopsis_json or {}
    kind = _PERSON if label == "person" else _VEHICLE
    objs = [o for o in s.get("objects") or [] if isinstance(o, dict) and kind.search(str(o.get("type") or ""))]
    for part in PARTS[label]:
        key = "color" if part == "vehicle" else f"{part}_color"
        val = next((w for o in objs if (w := _word(o.get(key)))), None)
        if val is None:   # an answer from before the color fields: its words
            texts = [synopsis, s.get("summary"), *(o.get("description") for o in objs)]
            val = next((c for t in texts if (c := from_text(t, part))), None)
        if val:
            out[part] = val
    return out


CAMERA_KEYS = {"vehicle": "vehicle_color", "top": "top_color", "bottom": "bottom_color"}


def from_camera(camera_attrs: dict | None, label: str) -> dict:
    from . import camattrs
    out = {}
    for part in PARTS[label]:
        b = camattrs.best(camera_attrs, CAMERA_KEYS[part])
        if b and (w := _word(b["value"])):
            out[part] = w
    return out


def combine(e: dict, picture: dict | None) -> dict | None:
    """The event's colors: Otso's, else the camera's, else the crops', per part, with where each came from."""
    label = label_of(e)
    if label is None:
        return None
    sources = (("otso", from_synopsis(e.get("synopsis_json"), e.get("synopsis"), label)),
               ("camera", from_camera(e.get("camera_attrs"), label)), ("picture", picture or {}))
    out: dict = {"by": {}}
    for part in PARTS[label]:
        for name, found in sources:
            if found.get(part):
                out[part], out["by"][part] = found[part], name
                break
    if picture:
        out["picture"] = picture
    return out if len(out) > 1 else None


def describe(colors: dict | None, label: str | None) -> str | None:
    """"white vehicle" / "green top, black bottoms" (search text)."""
    if not colors:
        return None
    if label == "vehicle":
        return f"{colors['vehicle']} vehicle" if colors.get("vehicle") else None
    parts = [f"{colors[p]} {w}" for p, w in (("top", "top"), ("bottom", "bottoms")) if colors.get(p)]
    return ", ".join(parts) or None


# ---------------------------------------------------------------- per event

def event_colors(event_id: int) -> dict | None:
    """A verified event's colors from its saved crops (and Otso / the camera when they have spoken): stored, returned.
    Runs off the event loop (decodes up to four JPEGs)."""
    from . import identities
    from .db import db
    e = db.event(event_id)
    if not e or e.get("status") != "verified" or label_of(e) is None:
        return None
    picture = crop_colors(identities.vehicle_crops(event_id), label_of(e))   # the crops cut to the object (people too)
    colors = combine(e, picture)
    db.update_event(event_id, colors=colors)
    return colors


def refresh(e: dict) -> dict | None:
    """Combine again after Otso answered or the answer was corrected (no crops read: the picture part is stored).
    Returns the new colors when they changed, else None."""
    from .db import db
    if label_of(e) is None or e.get("status") != "verified":
        return None
    old = e.get("colors") or None
    new = combine(e, (old or {}).get("picture"))
    if new != old:
        db.update_event(e["id"], colors=new)
        return new
    return None


BACKFILL_DAYS = 30
BACKFILL_SETTING = "colors_backfilled"


def backfill_batch(after_id: int, since: float, n: int = 200) -> tuple[int, int, int]:
    """Name the colors of up to n verified people and vehicles from before colors existed (id > after_id, newer than
    since, colors still NULL). Returns (last id looked at or 0 when done, events looked at, events named)."""
    from .db import db
    rows = db.all("SELECT id FROM events WHERE id > ? AND start_ts >= ? AND status='verified' AND colors IS NULL "
                  "AND (camera_class IN ('person','vehicle') OR yolo_class IN ('person','car','truck','bus','motorcycle')) "
                  "ORDER BY id LIMIT ?", [after_id, since, n])
    named = sum(1 for r in rows if event_colors(r["id"]))
    return (rows[-1]["id"] if rows else 0), len(rows), named
