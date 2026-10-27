"""The models this server uses: its own (torch on its GPU / CPU, a Hailo), or, on a central instance with
NVR_INFER_SOCKET set, the recording host's shared inference service (inferd.py; docs/shared-inference-plan.md).

The stand-ins answer exactly what the callers use today, so verifier, PPE, re-ID, journeys, the footage index and
vehicle fingerprints don't know the difference:

    RemoteYOLO("yolo" | "ppe")  .names, .predict(images, imgsz=, conf=, classes=, ...) -> [r.boxes.xyxyn / .cls / .conf]
    RemoteReID                  .embed(crops) -> mean 512-d vector or None
    RemoteClip                  .embed_images(bgr list) -> (n, 512), .embed_text(text) -> (512,)

Frames are letterboxed / resized here (inferwire) and the service only runs the models. This module never imports
torch, ultralytics or open_clip: an instance on the service saves the ~1.75 GB of RAM and the GPU memory they take.

A call that can't reach the service retries on the next address (NVR_INFER_SOCKET may list two, comma-separated) for
NVR_INFER_RETRY_S, then raises InferUnavailable: the pipeline puts the event back in the verify queue instead of
marking it an error, and after DOWN_ALERT_S the server raises `inference_unavailable`. The first call on each
connection checks the protocol version and that the service's YOLO / PPE / re-ID weights are this image's.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from . import inferwire as wire
from .config import ROOT, settings

log = logging.getLogger("nvr.inference")

DOWN_ALERT_S = 30.0
DOWN_TEXT = "The recording host's AI service is not answering; events wait to be verified"
CLIP_CHUNK = 160         # crops per clip_images request (a batch of the service's)
REQUEUE_S = 15.0         # an event that couldn't reach the service goes back in the verify queue after this long


class InferUnavailable(RuntimeError):
    """The service can't be reached (or answers with other weights / protocol): the work should wait, not fail."""


def active() -> bool:
    return bool(settings.infer_socket.strip())


# ---------------------------------------------------------------- the connection

def _weights_sha(path: Path, cache: dict = {}) -> str | None:  # noqa: B006 - module-lifetime cache
    if path not in cache:
        cache[path] = wire.sha256(path) if path.exists() else None
    return cache[path]


class Client:
    def __init__(self, addresses: list[str], token: str, timeout_s: float = 60.0, retry_s: float = 20.0,
                 clock=time.time) -> None:
        self.addresses = [a for a in addresses if a.strip()]
        self.token, self.timeout_s, self.retry_s, self.clock = token, timeout_s, retry_s, clock
        self._local = threading.local()
        self._lock = threading.Lock()
        self._next = 0                      # the address new connections try first (moves on after a failure)
        self.hello: dict | None = None
        self.down_since: float | None = None
        self.last_error: str | None = None
        self.calls = 0
        self.ms: deque = deque(maxlen=500)

    # ---- connections (one per thread: the pipeline's GPU thread, the API's search, ...)
    def _connect(self) -> socket.socket:
        errors = []
        for k in range(len(self.addresses)):
            i = (self._next + k) % len(self.addresses)
            addr = self.addresses[i]
            family, target = wire.parse_address(addr)
            s = socket.socket(family, socket.SOCK_STREAM)
            s.settimeout(self.timeout_s)
            try:
                s.connect(target)
                self._handshake(s, addr)
            except InferUnavailable:
                s.close()
                raise
            except OSError as e:
                s.close()
                errors.append(f"{addr}: {e}")
                continue
            self._next = i
            return s
        raise ConnectionError("; ".join(errors) or "no NVR_INFER_SOCKET")

    def _handshake(self, s: socket.socket, addr: str) -> None:
        wire.send(s, {"op": "hello", "token": self.token, "proto": wire.PROTO})
        h, _ = wire.recv(s)
        if not h.get("ok"):
            raise InferUnavailable(f"{addr}: {h.get('error')}")
        if h.get("proto") != wire.PROTO:
            raise InferUnavailable(f"{addr}: protocol {h.get('proto')}, this server speaks {wire.PROTO}")
        models = h.get("models") or {}
        mine = {"yolo": ROOT / "models" / settings.yolo_model, "ppe": ROOT / "models" / settings.ppe_model,
                "reid": ROOT / "models" / "osnet_x0_25_msmt17.pt"}
        for kind, path in mine.items():
            theirs, ours = (models.get(kind) or {}).get("sha256"), _weights_sha(path)
            if theirs and ours and theirs != ours:
                raise InferUnavailable(f"{addr}: its {kind} weights differ from this server's ({path.name})")
        self.hello = h

    def _sock(self) -> socket.socket:
        s = getattr(self._local, "sock", None)
        if s is None:
            s = self._local.sock = self._connect()
        return s

    def _drop(self) -> None:
        s = getattr(self._local, "sock", None)
        self._local.sock = None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    # ---- calls
    def call(self, header: dict, arrays: list[np.ndarray] = ()) -> tuple[dict, list[np.ndarray]]:
        header = {**header, "token": self.token}
        deadline = time.monotonic() + self.retry_s
        delay = 0.25
        while True:
            t0 = time.perf_counter()
            try:
                s = self._sock()
                wire.send(s, header, arrays)
                h, out = wire.recv(s)
            except InferUnavailable as e:
                self._drop()
                self._failed(str(e))
                raise
            except (OSError, wire.WireError) as e:
                self._drop()
                with self._lock:
                    self._next = (self._next + 1) % max(1, len(self.addresses))
                if time.monotonic() + delay > deadline:
                    self._failed(f"{type(e).__name__}: {e}")
                    raise InferUnavailable(self.last_error) from e
                time.sleep(delay)
                delay = min(delay * 2, 2.0)
                continue
            if not h.get("ok"):
                if h.get("kind") == "unauthorized":
                    self._failed("the service refused this server's token")
                    raise InferUnavailable(self.last_error)
                raise RuntimeError(f"inference service: {h.get('error')}")
            with self._lock:
                self.calls += 1
                self.ms.append((time.perf_counter() - t0) * 1000)
                if self.down_since is not None:
                    log.info("inference service answering again (down %.0f s)", self.clock() - self.down_since)
                self.down_since, self.last_error = None, None
            return h, out

    def _failed(self, error: str) -> None:
        with self._lock:
            if self.down_since is None:
                self.down_since = self.clock()
                log.warning("inference service unavailable: %s", error)
            self.last_error = error

    # ---- what /api/system and the health alerts read
    def status(self) -> dict:
        with self._lock:
            ms = sorted(self.ms)
        pick = (lambda q: round(ms[min(len(ms) - 1, int(q * len(ms)))], 1)) if ms else (lambda q: None)
        h = self.hello or {}
        return {"addresses": self.addresses, "device": h.get("device"), "models": sorted(h.get("models") or {}),
                "up": self.down_since is None and self.hello is not None, "down_since": self.down_since,
                "error": self.last_error, "calls": self.calls, "p50_ms": pick(0.5), "p95_ms": pick(0.95)}

    def health_alert(self, now: float | None = None) -> dict | None:
        since = self.down_since
        if since is None or (now or self.clock()) - since < DOWN_ALERT_S:
            return None
        return {"kind": "inference_unavailable", "text": DOWN_TEXT, "since": since, "error": self.last_error}

    def names(self, kind: str) -> dict[int, str] | None:
        if self.hello is None:
            try:
                self._sock()
            except (InferUnavailable, ConnectionError, OSError):
                return None
        info = ((self.hello or {}).get("models") or {}).get(kind) or {}
        return {int(k): v for k, v in (info.get("names") or {}).items()} or None


_client: Client | None = None
_client_lock = threading.Lock()


def client() -> Client:
    global _client
    with _client_lock:
        if _client is None:
            _client = Client(settings.infer_socket.split(","), settings.infer_token, settings.infer_timeout_s,
                             settings.infer_retry_s)
        return _client


def status() -> dict | None:
    return client().status() if active() else None


def health_alert() -> dict | None:
    return client().health_alert() if active() else None


# ---------------------------------------------------------------- the stand-ins

class RemoteYOLO:
    """ultralytics.YOLO's predict on the service: the same boxes YOLO.predict gives (hailo.Result's shape)."""

    def __init__(self, kind: str = "yolo", cl: Client | None = None) -> None:
        self.kind, self._cl = kind, cl
        self.device = "remote"
        self._names: dict[int, str] | None = None

    @property
    def cl(self) -> Client:
        return self._cl or client()

    @property
    def names(self) -> dict[int, str]:
        if self._names is None:
            self._names = self.cl.names(self.kind)
        if self._names is not None:
            return self._names
        if self.kind == "yolo":     # the service isn't up yet: COCO's ids are what callers look up
            from .hailo import NAMES
            return NAMES
        return {}

    @property
    def ready(self) -> bool:
        """detector.yolo_status: not ready while the service is down (the stall check then stays quiet; the
        inference_unavailable alert says why events wait)."""
        return self.cl.down_since is None

    def to(self, device) -> "RemoteYOLO":
        return self

    def predict(self, images, imgsz: int = 640, conf: float = 0.25, classes=None, prio: str = "verify", **_) -> list:
        from .hailo import Boxes, Result
        images = list(images)
        if not images:
            return []
        stride = int(((self.cl.hello or {}).get("models", {}).get(self.kind) or {}).get("stride") or 32)
        same = len({im.shape for im in images}) == 1     # ultralytics letterboxes to a rectangle only then
        boxed = [wire.letterbox(im, int(imgsz), stride, auto=same) for im in images]
        out: list = [None] * len(images)
        i = 0
        while i < len(images):           # one request per run of frames with the same input size
            j = i + 1
            while j < len(images) and boxed[j].shape == boxed[i].shape:
                j += 1
            h, arrs = self.cl.call({"op": "detect", "model": self.kind, "conf": float(conf), "prio": prio,
                                    "classes": sorted(int(c) for c in classes) if classes is not None else None,
                                    "orig_shapes": [list(im.shape[:2]) for im in images[i:j]]},
                                   [np.stack(boxed[i:j])])
            rows, k = arrs[0], 0
            for n, im in zip(h["counts"], images[i:j]):
                r = rows[k:k + n]
                out[i] = Result(Boxes(r[:, :4], r[:, 5], r[:, 4]), im.shape[:2])
                k += n
                i += 1
        return out

    __call__ = predict


class RemoteReID:
    """reid.ReID.embed on the service: per-crop features there, their mean here (as ReID.embed returns it)."""

    def __init__(self, cl: Client | None = None) -> None:
        self._cl = cl
        self.device = "remote"

    def embed(self, crops: list) -> np.ndarray | None:
        crops = wire.reid_crops(crops)
        if not crops:
            return None
        _, arrs = (self._cl or client()).call({"op": "reid", "prio": "verify"}, [np.stack([wire.reid_pixels(c) for c in crops])])
        mean = arrs[0].astype(np.float32).mean(0)
        return (mean / max(float(np.linalg.norm(mean)), 1e-12)).astype(np.float32)


class RemoteClip:
    """clip.Clip on the service. Images go at index priority (the footage index is most of them and can wait); text
    is someone searching, at verify priority."""

    def __init__(self, cl: Client | None = None) -> None:
        self._cl = cl
        self.device, self.precision = "remote", "fp16"

    def embed_images(self, images: list[np.ndarray], prio: str = "index") -> np.ndarray:
        if not len(images):
            return np.zeros((0, wire.CLIP_DIM), np.float32)
        cl = self._cl or client()
        out = []
        for k in range(0, len(images), CLIP_CHUNK):
            px = np.stack([wire.clip_pixels(im) for im in images[k:k + CLIP_CHUNK]])
            out.append(cl.call({"op": "clip_images", "prio": prio}, [px])[1][0])
        return np.concatenate(out).astype(np.float32)

    def embed_text(self, text: str) -> np.ndarray:
        return (self._cl or client()).call({"op": "clip_text", "prio": "verify", "text": text})[1][0].astype(np.float32)


# ---------------------------------------------------------------- what the pipeline loads

def reid_model():
    """Re-ID: the service's, or reid.ReID on this server's torch device."""
    if active():
        return RemoteReID()
    from .reid import ReID
    return ReID()


def clip_model():
    """CLIP: the service's, or clip.Clip on this server's torch device."""
    if active():
        return RemoteClip()
    from .clip import Clip
    return Clip()
