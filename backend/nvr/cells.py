"""Which cells of a coarse grid an event's object walked through, for the "paint a region" filter.

The frame is a 32 x 18 grid in normalised coordinates (cell = row*32 + col). For every path sample the cell
under the object's feet (zones.foot) counts, plus the cells under the middle half of the box's bottom edge
(a wide vehicle stands on several columns), plus a straight line between consecutive foot cells when they are
not neighbours and the samples are close in time (a fast object can't skip a painted cell).

Stored per event as a 72-byte bitmap in base64url without padding (96 chars, bit i = byte i//8, LSB first),
identical to frontend/src/region.ts, so the UI filters by a bytewise AND with the painted cells.
"""
from __future__ import annotations

import base64
import json

from .db import db

GRID_W, GRID_H = 32, 18
NCELLS = GRID_W * GRID_H
NBYTES = NCELLS // 8   # 72
GAP_FILL_S = 2.0       # fill the line between samples this close in time


def cell(x: float, y: float) -> int:
    col = min(GRID_W - 1, max(0, int(x * GRID_W)))
    row = min(GRID_H - 1, max(0, int(y * GRID_H)))
    return row * GRID_W + col


def _line(a: int, b: int) -> set[int]:
    """Cells on the straight line between two cells (Bresenham), endpoints excluded."""
    r0, c0, r1, c1 = a // GRID_W, a % GRID_W, b // GRID_W, b % GRID_W
    n = max(abs(r1 - r0), abs(c1 - c0))
    out = set()
    for i in range(1, n):
        out.add(round(r0 + (r1 - r0) * i / n) * GRID_W + round(c0 + (c1 - c0) * i / n))
    return out


def path_cells(path: list) -> set[int]:
    out: set[int] = set()
    prev_cell, prev_ts = None, None
    for p in path or []:
        if len(p) < 5:
            continue
        ts, l, t, r, b = p[0], p[1], p[2], p[3], p[4]
        foot = cell((l + r) / 2, b)
        out.add(foot)
        w = r - l
        if w > 1.0 / GRID_W:  # wider than a cell: the columns under the middle half of the bottom edge
            x = l + w * 0.25
            while x <= r - w * 0.25:
                out.add(cell(x, b))
                x += 1.0 / GRID_W
        if prev_cell is not None and ts - prev_ts <= GAP_FILL_S:
            dr, dc = abs(foot // GRID_W - prev_cell // GRID_W), abs(foot % GRID_W - prev_cell % GRID_W)
            if max(dr, dc) > 1:
                out |= _line(prev_cell, foot)
        prev_cell, prev_ts = foot, ts
    return out


def encode(cells: set[int]) -> str:
    buf = bytearray(NBYTES)
    for c in cells:
        if 0 <= c < NCELLS:
            buf[c >> 3] |= 1 << (c & 7)
    return base64.urlsafe_b64encode(bytes(buf)).decode().rstrip("=")


def decode(s: str) -> set[int]:
    raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    return {i for i in range(min(NCELLS, len(raw) * 8)) if raw[i >> 3] & (1 << (i & 7))}


def overlaps(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    ra = base64.urlsafe_b64decode(a + "=" * (-len(a) % 4))
    rb = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4))
    return any(x & y for x, y in zip(ra, rb))


def for_event(path: list | None) -> str | None:
    cells = path_cells(path or [])
    return encode(cells) if cells else None


def backfill() -> int:
    """Events from before this existed: compute their cells from the stored path (open events wait for close)."""
    rows = db.all("SELECT id, path FROM events WHERE cells IS NULL AND status != 'open' AND path != '[]'")
    n = 0
    for r in rows:
        try:
            c = for_event(json.loads(r["path"]))
        except (ValueError, TypeError):
            c = None
        if c:
            db.update_event(r["id"], cells=c)
            n += 1
    return n
