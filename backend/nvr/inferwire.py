"""The shared inference service's wire format and the frame preparation both sides agree on (no torch here).

A recording host runs one service per GPU (inferd.py) holding YOLO, the PPE detector, re-ID and CLIP once; the
central instances send it frames (inference.py) instead of loading the models themselves
(docs/shared-inference-plan.md). Frames are prepared in the instance, where the CPU work belongs (phase 0: the
letterbox, not the GPU, limited one process to 85 frames/s), so the service only stacks, runs and filters.

Message (both directions, over a Unix socket or, for tests and Windows, 127.0.0.1 TCP):

    !II  header length, payload length
    header  UTF-8 JSON; "arrays": [{"dtype", "shape"}, ...] describes the payload
    payload the arrays' bytes, C order, one after another

Requests: {"op": "hello" | "stats" | "detect" | "reid" | "clip_images" | "clip_text", "token", "prio", ...};
answers: {"ok": true, ...} or {"ok": false, "error", "kind"}.

letterbox() is ultralytics' LetterBox (auto, scaleup, centered, value 114, INTER_LINEAR) so the service's boxes are
the ones YOLO.predict gives today (tests/test_inference.py compares them pixel for pixel).
"""
from __future__ import annotations

import hashlib
import json
import socket
import struct

import cv2
import numpy as np

PROTO = 1
PRIOS = ("verify", "background", "index")   # served strictly in this order (inferd.Scheduler)
HEAD = struct.Struct("!II")
MAX_HEADER = 1 << 20
MAX_PAYLOAD = 1 << 30
CLIP_SIZE = 224
CLIP_DIM = 512
REID_HW = (256, 128)
REID_MIN_SIDE = 16


class WireError(ConnectionError):
    """The other side closed or sent something that is not a message."""


def pack(header: dict, arrays: list[np.ndarray] = ()) -> list:
    arrays = [np.ascontiguousarray(a) for a in arrays]
    header = {**header, "arrays": [{"dtype": a.dtype.str, "shape": list(a.shape)} for a in arrays]}
    hb = json.dumps(header, separators=(",", ":")).encode()
    return [HEAD.pack(len(hb), sum(a.nbytes for a in arrays)), hb,
            *(memoryview(a.reshape(-1).view(np.uint8)) for a in arrays if a.nbytes)]


def send(sock: socket.socket, header: dict, arrays: list[np.ndarray] = ()) -> None:
    for part in pack(header, arrays):
        sock.sendall(part)


def _exact(sock: socket.socket, n: int) -> bytearray:
    buf = bytearray(n)
    view, got = memoryview(buf), 0
    while got < n:
        k = sock.recv_into(view[got:], n - got)
        if not k:
            raise WireError("connection closed")
        got += k
    return buf


def recv(sock: socket.socket) -> tuple[dict, list[np.ndarray]]:
    hlen, plen = HEAD.unpack(_exact(sock, HEAD.size))
    if hlen > MAX_HEADER or plen > MAX_PAYLOAD:
        raise WireError(f"message too large ({hlen} + {plen} bytes)")
    header = json.loads(bytes(_exact(sock, hlen)))
    payload = _exact(sock, plen) if plen else bytearray()
    arrays, off = [], 0
    for d in header.pop("arrays", []):
        dt, shape = np.dtype(d["dtype"]), tuple(d["shape"])
        n = int(np.prod(shape, dtype=np.int64)) * dt.itemsize
        if off + n > plen:
            raise WireError("payload shorter than its arrays")
        arrays.append(np.frombuffer(payload, dt, int(np.prod(shape, dtype=np.int64)), off).reshape(shape))
        off += n
    return header, arrays


def sha256(path) -> str:
    """A weights file's SHA-256: the service reports its models', the instances check them against their own."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_address(addr: str) -> tuple[int, object]:
    """"/run/axiom-infer/gpu1.sock" -> (AF_UNIX, path); "127.0.0.1:8770" -> (AF_INET, (host, port))."""
    addr = addr.strip()
    if addr.startswith("/") or addr.startswith("unix:"):
        return getattr(socket, "AF_UNIX", -1), addr.removeprefix("unix:")   # -1: no Unix sockets here (Windows)
    host, _, port = addr.rpartition(":")
    return socket.AF_INET, (host or "127.0.0.1", int(port))


# ---------------------------------------------------------------- frame preparation (the instance's side)

def letterbox(img: np.ndarray, imgsz: int, stride: int = 32, auto: bool = True) -> np.ndarray:
    """ultralytics LetterBox(new_shape=(imgsz, imgsz), auto=auto, stride=stride): resize keeping the aspect, pad to
    a stride multiple (auto) or the full square, centered, grey 114. BGR uint8 in and out."""
    h, w = img.shape[:2]
    r = min(imgsz / h, imgsz / w)
    new_unpad = round(w * r), round(h * r)
    dw, dh = imgsz - new_unpad[0], imgsz - new_unpad[1]
    if auto:
        dw, dh = np.mod(dw, stride), np.mod(dh, stride)
    dw, dh = dw / 2, dh / 2
    if (w, h) != new_unpad:
        img = cv2.resize(img, new_unpad, interpolation=cv2.INTER_LINEAR)
    top, bottom = round(dh - 0.1), round(dh + 0.1)
    left, right = round(dw - 0.1), round(dw + 0.1)
    return cv2.copyMakeBorder(img, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(114, 114, 114))


def clip_pixels(bgr: np.ndarray) -> np.ndarray:
    """clip.Clip._prep before its normalization: squashed to 224x224 (no crop, the view's edges stay), RGB uint8."""
    return cv2.cvtColor(cv2.resize(bgr, (CLIP_SIZE, CLIP_SIZE), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)


def reid_crops(crops: list) -> list[np.ndarray]:
    """reid.ReID.embed's crops: those at least 16 px on each side."""
    return [c for c in crops if c is not None and c.size and min(c.shape[:2]) >= REID_MIN_SIDE]


def reid_pixels(bgr: np.ndarray) -> np.ndarray:
    """reid.ReID.embed before its normalization: resized to 256x128 (h x w), RGB uint8."""
    return cv2.cvtColor(cv2.resize(bgr, (REID_HW[1], REID_HW[0]), interpolation=cv2.INTER_LINEAR), cv2.COLOR_BGR2RGB)
