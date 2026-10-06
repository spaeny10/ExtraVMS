"""Detection zones: include / exclude / area polygons in normalized (0-1) image coordinates.

"area" zones never filter anything: they only name a place ("Bathroom 2", "Exit door") so each event can
record which places the object walked into (areas_visited). "ppe" zones don't filter either: people who stay
in one are checked for the hard hat / hi-vis vest it requires (ppe.py).

One rule everywhere (tracker, YOLO verification, past-event masking, the UI preview):
an object's point is the bottom-center of its box (where it touches the ground). It is *allowed* if
(there are no include zones OR it is inside one) AND it is not inside any exclude zone. Exclude wins.
"""
from __future__ import annotations

import re

import cv2
import numpy as np

MASK_GRAY = (114, 114, 114)  # YOLO's letterbox gray: reads as "nothing here"


def point_in_polygon(x: float, y: float, poly: list[list[float]]) -> bool:
    inside, j = False, len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def dist_to_polygon(x: float, y: float, poly: list[list[float]]) -> float:
    """0 inside, else the distance to the nearest edge (normalized units)."""
    if point_in_polygon(x, y, poly):
        return 0.0
    best = 9.0
    for i in range(len(poly)):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % len(poly)]
        dx, dy = x2 - x1, y2 - y1
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
        best = min(best, ((x - (x1 + t * dx)) ** 2 + (y - (y1 + t * dy)) ** 2) ** 0.5)
    return best


def place_of(x: float, y: float, zones: list[dict] | None, tol: float = 0.05) -> str | None:
    """The named area a point is in or right next to (within tol), nearest first."""
    areas = [z for z in normalize(zones) if z["type"] == "area"]
    best = min(((dist_to_polygon(x, y, z["points"]), z) for z in areas), key=lambda t: t[0], default=None)
    return (best[1].get("name") or "Area").strip() if best and best[0] <= tol else None


def edge_of(x: float, y: float, margin: float = 0.12) -> str | None:
    """Which edge of the picture a point is at, if any (where a track enters or leaves the view)."""
    if y >= 1 - margin:
        return "bottom edge (nearest the camera)"
    if y <= margin:
        return "top edge (far end of the view)"
    if x <= margin:
        return "left edge"
    if x >= 1 - margin:
        return "right edge"
    return None


PPE_ITEMS = ("hard_hat", "vest")


def normalize(zones: list[dict] | None) -> list[dict]:
    """Valid zones only, with a type (zones saved before exclude existed are include zones).
    A ppe zone's `required` is kept to the known items (hard_hat, vest), in that order."""
    out = []
    for z in zones or []:
        pts = z.get("points") or []
        if len(pts) >= 3:
            t = z.get("type")
            nz = {**z, "type": t if t in ("exclude", "area", "ppe") else "include", "points": pts}
            if nz["type"] == "ppe":
                req = z.get("required")
                nz["required"] = [i for i in PPE_ITEMS if i in (req if isinstance(req, list) else PPE_ITEMS)]
            out.append(nz)
    return out


def foot(box) -> tuple[float, float]:
    """Bottom-center of a (left, top, right, bottom) box."""
    return (box[0] + box[2]) / 2, box[3]


def allowed(point: tuple[float, float], zones: list[dict]) -> bool:
    x, y = point
    includes = [z for z in zones if z["type"] == "include"]
    if includes and not any(point_in_polygon(x, y, z["points"]) for z in includes):
        return False
    return not any(point_in_polygon(x, y, z["points"]) for z in zones if z["type"] == "exclude")


def path_allowed(path: list, zones: list[dict]) -> bool:
    """True if any sample of a track path ([ts, l, t, r, b, conf], ...) is in an allowed area."""
    if not zones:
        return True
    return any(allowed(foot(p[1:5]), zones) for p in path or [])


def mask_frame(img: np.ndarray, zones: list[dict]) -> np.ndarray:
    """Copy of img with disallowed areas painted gray, so a detector can't see them."""
    if not zones:
        return img
    h, w = img.shape[:2]
    to_px = lambda pts: np.array([[int(x * w), int(y * h)] for x, y in pts], dtype=np.int32)
    out = img.copy()
    includes = [z for z in zones if z["type"] == "include"]
    if includes:
        keep = np.zeros((h, w), dtype=np.uint8)
        cv2.fillPoly(keep, [to_px(z["points"]) for z in includes], 255)
        out[keep == 0] = MASK_GRAY
    excludes = [to_px(z["points"]) for z in zones if z["type"] == "exclude"]
    if excludes:
        cv2.fillPoly(out, excludes, MASK_GRAY)
    return out


def areas_visited(path: list, zones: list[dict], min_points: int = 2) -> list[dict]:
    """Named areas a track's feet entered, in order of first entry: [{name, from, to}] (epoch seconds).
    min_points: samples needed inside, so a box edge brushing a doorway doesn't count."""
    areas = [z for z in normalize(zones) if z["type"] == "area"]
    if not areas or not path:
        return []
    seen: dict[str, dict] = {}
    for p in path:
        x, y = foot(p[1:5])
        for z in areas:
            if point_in_polygon(x, y, z["points"]):
                name = (z.get("name") or "Area").strip()
                v = seen.setdefault(name, {"name": name, "from": p[0], "to": p[0], "n": 0})
                v["to"], v["n"] = p[0], v["n"] + 1
    out = [v for v in seen.values() if v["n"] >= min_points]
    out.sort(key=lambda v: v["from"])
    return [{"name": v["name"], "from": round(v["from"], 2), "to": round(v["to"], 2)} for v in out]


def draw_outlines(img: np.ndarray, zones: list[dict]) -> np.ndarray:
    """Thin zone outlines for snapshots (blue = include, red = exclude, yellow = PPE required; areas aren't drawn).
    Draws in place."""
    h, w = img.shape[:2]
    for z in zones:
        if z["type"] == "area":
            continue
        pts = np.array([[int(x * w), int(y * h)] for x, y in z["points"]], dtype=np.int32)
        color = {"exclude": (60, 60, 230), "ppe": (0, 210, 240)}.get(z["type"], (230, 150, 50))
        cv2.polylines(img, [pts], True, color, 2, cv2.LINE_AA)
    return img


# ---------------------------------------------------------------- doors: entries and exits the NVR knows for certain

DOOR_RE = re.compile(r"\b(door|entrance|entry|exit|gate)\b", re.I)
DOOR_WINDOW_S = 2.5   # at the door within this long of the track's start (came in) or end (went out)


def door_facts(event: dict) -> tuple[str | None, str | None]:
    """(entered_through, left_through): named door places the track started at / ended at, else None."""
    path = event.get("path") or []
    areas = event.get("areas") or []
    if not path or not areas:
        return None, None
    t0, t1 = path[0][0], path[-1][0]
    entered = next((a["name"] for a in areas if DOOR_RE.search(a["name"]) and a["from"] - t0 <= DOOR_WINDOW_S), None)
    left = next((a["name"] for a in reversed(areas) if DOOR_RE.search(a["name"]) and t1 - a["to"] <= DOOR_WINDOW_S), None)
    if entered and left == entered and len(areas) == 1 and t1 - t0 < 2 * DOOR_WINDOW_S:
        left = None  # a short track at the door: an entry, not in-and-straight-out
    return entered, left


_CONTRA_OUT = re.compile(r"\b(out of|walks? out|walked out|leav\w*|left|exit\w*|goes out|went out)\b", re.I)
_CONTRA_IN = re.compile(r"\b(came in|comes in|enter\w*|walks? in|walked in|into the building)\b", re.I)


def apply_door_facts(summary: str, event: dict) -> str:
    """Make the synopsis agree with the track: state a known entry/exit through a door and drop a sentence
    that says the opposite about that door (small models invert this)."""
    entered, left = door_facts(event)
    if not entered and not left:
        return summary
    sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+", summary.strip()) if x.strip()]
    keep = []
    for x in sentences:
        if entered and entered.lower() in x.lower() and _CONTRA_OUT.search(x) and not left:
            continue
        if left and left.lower() in x.lower() and _CONTRA_IN.search(x) and not entered:
            continue
        keep.append(x)
    out = " ".join(keep)
    if entered and not re.search(r"(came in|entered|comes in|enters)[^.]*" + re.escape(entered), out, re.I):
        out = f"Came in through the {entered}. " + out
    if left and not re.search(r"(left|leaves|went out|goes out|exited|exits)[^.]*" + re.escape(left), out, re.I):
        out = (out.rstrip(".") + ". " if out else "") + f"Left through the {left}."
    return out.strip()
