"""Re-ID's input constants and person crops, without torch: reid.py (the model) and the shared inference service's
instances (inference.py, which never load torch) both use them."""
from __future__ import annotations

import numpy as np

INPUT_HW = (256, 128)
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
EMBED_DIM = 512


def person_crop(img: np.ndarray, box, pad: float = 0.05) -> np.ndarray:
    """Tight person crop from a normalized (l, t, r, b) box, with a little padding."""
    h, w = img.shape[:2]
    l, t, r, b = box
    pw, ph = (r - l) * pad, (b - t) * pad
    x1, y1 = int(max(0, l - pw) * w), int(max(0, t - ph) * h)
    x2, y2 = int(min(1, r + pw) * w), int(min(1, b + ph) * h)
    return img[y1:y2, x1:x2]
