"""Event snapshots and their thumbnails.

An event's snapshot.jpg is the picture the event list, alerts, the hub and the viewer's poster show. Until 2026-10-22
it was the full camera frame (2592 px wide, JPEG quality 88, ~1 MB); on a central instance 48,929 of them took 47.6 GB
of its data dataset. A new snapshot is at most `snapshot_max_px` (1280) on its long side at quality
`snapshot_quality` (80): ~150-250 KB. Nothing that needs full resolution reads it any more: verification, re-ID, the
vehicle fingerprint's crops, plates, PPE and the weapon check decode the recording (frames.recording_frames /
verifier.event_frames) or keep their own full-resolution crops; the weapon check falls back to the snapshot only when
the recording is gone (it can then confirm a weapon, never clear one). `python -m nvr.snapshot_shrink` shrinks the
older ones in place.

Thumbnails (thumb_<w>.jpg beside the snapshot) are made on first request by GET /api/events/{id}/media/snapshot.jpg?w=
and, for a rejected event whose snapshot retention removed (retention.purge_rejected_media), thumb_320.jpg is all that
is left: the media endpoint serves the stored thumbnail when snapshot.jpg is gone.

Imports nothing from the database: snapshot_shrink's dry run must not open it.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .config import settings
from .diskguard import THUMB_KEPT

THUMB_WIDTHS = (320, 480, 640, 960)   # the sizes a thumbnail may be asked for (w is rounded up to one of these)
THUMB_KEPT_W = 320
THUMB_QUALITY = 78


def scale_for(width: int, height: int, max_px: int | None = None) -> float:
    """The factor that fits (width, height) inside max_px on the long side (1.0 when it already does)."""
    max_px = max_px or settings.snapshot_max_px
    longest = max(width, height)
    return max_px / longest if longest > max_px else 1.0


def fit(img: np.ndarray, max_px: int | None = None) -> np.ndarray:
    """The frame scaled down to the snapshot size (a copy only when it is bigger). Draw boxes after this, so the
    lines stay as thick as they were drawn."""
    s = scale_for(img.shape[1], img.shape[0], max_px)
    if s >= 1.0:
        return img
    return cv2.resize(img, (max(1, round(img.shape[1] * s)), max(1, round(img.shape[0] * s))), interpolation=cv2.INTER_AREA)


def encode(img: np.ndarray, max_px: int | None = None, quality: int | None = None) -> bytes:
    ok, buf = cv2.imencode(".jpg", fit(img, max_px), [cv2.IMWRITE_JPEG_QUALITY, int(quality or settings.snapshot_quality)])
    if not ok:
        raise ValueError("JPEG encoding failed")
    return buf.tobytes()


def write(path: Path, img: np.ndarray) -> Path:
    """Store an event snapshot: at most snapshot_max_px on the long side, JPEG snapshot_quality."""
    Path(path).write_bytes(encode(img))
    return Path(path)


def thumb_width(w: int) -> int:
    return next((t for t in THUMB_WIDTHS if t >= w), THUMB_WIDTHS[-1])


def make_thumb(src: Path, dest: Path, width: int) -> None:
    """Shrink a snapshot to `width` px wide (~20-30 KB) once; later requests read the file. Written beside it as .tmp
    and renamed, so a reader never sees half a thumbnail."""
    from PIL import Image
    with Image.open(src) as im:
        if im.width > width:
            im.draft("RGB", (width, max(1, round(im.height * width / im.width))))   # JPEG: decode at 1/2, 1/4, 1/8
        im = im.convert("RGB")
        if im.width > width:
            im = im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
        tmp = dest.with_suffix(".tmp")
        im.save(tmp, "JPEG", quality=THUMB_QUALITY, optimize=True)
        tmp.replace(dest)


def ensure_kept_thumb(d: Path) -> Path | None:
    """The event folder's thumb_320.jpg, made from its snapshot when missing. None when there is neither (or the
    snapshot can't be read)."""
    thumb = d / THUMB_KEPT
    if thumb.is_file():
        return thumb
    snap = d / "snapshot.jpg"
    if not snap.is_file():
        return None
    try:
        make_thumb(snap, thumb, THUMB_KEPT_W)
    except Exception:  # noqa: BLE001 - a damaged snapshot: no thumbnail (the caller keeps the snapshot)
        return None
    return thumb


def stored_thumb(d: Path, width: int | None = None) -> Path | None:
    """For an event whose snapshot.jpg is gone: the stored thumbnail closest to `width` (the smallest one at least that
    wide, else the biggest there is), or None."""
    have = [(w, d / f"thumb_{w}.jpg") for w in THUMB_WIDTHS if (d / f"thumb_{w}.jpg").is_file()]
    if not have:
        return None
    want = thumb_width(width) if width else THUMB_WIDTHS[-1]
    return next((p for w, p in have if w >= want), have[-1][1])
