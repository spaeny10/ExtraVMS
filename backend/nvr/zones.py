"""Detection zones: include / exclude polygons in normalized (0-1) image coordinates.

One rule everywhere (tracker, YOLO verification, past-event masking, the UI preview):
an object's point is the bottom-centre of its box (where it touches the ground). It is *allowed* if
(there are no include zones OR it is inside one) AND it is not inside any exclude zone. Exclude wins.
"""
from __future__ import annotations

import cv2
import numpy as np

MASK_GREY = (114, 114, 114)  # YOLO's letterbox grey: reads as "nothing here"


def point_in_polygon(x: float, y: float, poly: list[list[float]]) -> bool:
    inside, j = False, len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def normalize(zones: list[dict] | None) -> list[dict]:
    """Valid zones only, with a type (zones saved before exclude existed are include zones)."""
    out = []
    for z in zones or []:
        pts = z.get("points") or []
        if len(pts) >= 3:
            out.append({**z, "type": "exclude" if z.get("type") == "exclude" else "include", "points": pts})
    return out


def foot(box) -> tuple[float, float]:
    """Bottom-centre of a (left, top, right, bottom) box."""
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
    """Copy of img with disallowed areas painted grey, so a detector can't see them."""
    if not zones:
        return img
    h, w = img.shape[:2]
    to_px = lambda pts: np.array([[int(x * w), int(y * h)] for x, y in pts], dtype=np.int32)
    out = img.copy()
    includes = [z for z in zones if z["type"] == "include"]
    if includes:
        keep = np.zeros((h, w), dtype=np.uint8)
        cv2.fillPoly(keep, [to_px(z["points"]) for z in includes], 255)
        out[keep == 0] = MASK_GREY
    excludes = [to_px(z["points"]) for z in zones if z["type"] == "exclude"]
    if excludes:
        cv2.fillPoly(out, excludes, MASK_GREY)
    return out


def draw_outlines(img: np.ndarray, zones: list[dict]) -> np.ndarray:
    """Thin zone outlines for snapshots (blue = include, red = exclude). Draws in place."""
    h, w = img.shape[:2]
    for z in zones:
        pts = np.array([[int(x * w), int(y * h)] for x, y in z["points"]], dtype=np.int32)
        color = (60, 60, 230) if z["type"] == "exclude" else (230, 150, 50)
        cv2.polylines(img, [pts], True, color, 2, cv2.LINE_AA)
    return img
