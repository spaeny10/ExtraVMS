"""The shared inference service: one per GPU on a recording host, holding YOLO, the PPE detector, re-ID and CLIP once
for every central instance on it (docs/shared-inference-plan.md).

    python -m nvr.inferd --listen /run/axiom-infer/gpu1.sock --device cuda:0 [--tokens tokens.json]
    python -m nvr.inferd --listen 127.0.0.1:8770 --device cpu             (tests, Windows)

It runs from the instance image, so the weights, the ultralytics version and the preprocessing are the instances'
own; the instances (inference.py) letterbox / resize their frames and send them here (inferwire.py).

Work is served strictly by priority (verify, then background, then index) and, within a priority, round-robin by Site,
so one Site's burst never holds back another Site's alarm. Requests for the same model and input size are batched
(up to --max-batch frames); YOLO and the PPE detector run in fp16 on a GPU (--half; phase 0 replayed 400 events with
no verdict changed), re-ID in fp32, CLIP in fp16 as the instances always ran it. A batch that runs out of GPU memory is
retried one request at a time. Per Site: requests, frames and GPU-seconds (the "stats" op; the host agent reports
them to the hub).

--tokens: JSON {token: instance id}, re-read when it changes (axiom_host writes it). Without it any token is accepted
and names its own Site (development only).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import socketserver
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import inferwire as wire
from .config import ROOT, settings

log = logging.getLogger("nvr.inferd")

DETECT_MODELS = ("yolo", "ppe")
ALL_MODELS = ("yolo", "ppe", "reid", "clip")
MAX_TEXT = 1000
LATENCY_KEEP = 500
BUSY_WINDOW_S = 60.0


class BadRequest(ValueError):
    pass


@dataclass
class Request:
    op: str
    site: str
    prio: str
    key: tuple | None            # requests with the same key can share a batch; None = runs alone
    n: int                       # frames / crops (the batch budget and the Site's accounting)
    header: dict
    arrays: list
    t_in: float = field(default_factory=time.perf_counter)
    done: threading.Event = field(default_factory=threading.Event)
    result: tuple | None = None  # (header, arrays)
    error: str | None = None


# ---------------------------------------------------------------- scheduling (pure)

class Scheduler:
    """Priority queues, round-robin by Site inside each. take() returns one batch: the oldest-waiting Site's next
    request in the highest non-empty priority, then the other Sites' next requests with the same key, in turn, while
    they fit in max_n frames."""

    def __init__(self) -> None:
        self.queues: dict[str, OrderedDict[str, deque]] = {p: OrderedDict() for p in wire.PRIOS}
        self.cond = threading.Condition()

    def put(self, req: Request) -> None:
        with self.cond:
            self.queues[req.prio].setdefault(req.site, deque()).append(req)
            self.cond.notify()

    def depth(self) -> dict[str, int]:
        with self.cond:
            return {p: sum(len(d) for d in q.values()) for p, q in self.queues.items()}

    def take(self, max_n: int, timeout: float | None = None) -> list[Request]:
        with self.cond:
            if not any(self.queues.values()):
                self.cond.wait(timeout)
            return self._take(max_n)

    def _take(self, max_n: int) -> list[Request]:
        for prio in wire.PRIOS:
            q = self.queues[prio]
            if not q:
                continue
            key = next(iter(q.values()))[0].key
            batch, total = [], 0
            while True:
                progressed = False
                for site in list(q):
                    dq = q[site]
                    head = dq[0]
                    if head.key != key or (batch and (key is None or total + head.n > max_n)):
                        continue
                    batch.append(dq.popleft())
                    total += head.n
                    progressed = True
                    if dq:
                        q.move_to_end(site)
                    else:
                        del q[site]
                    if key is None:
                        return batch
                if not progressed or not q:
                    return batch
        return []


# ---------------------------------------------------------------- tokens

class Tokens:
    """token -> instance id, from a JSON file re-read when it changes; no file: development mode (any token)."""

    def __init__(self, path: Path | None) -> None:
        self.path, self._mtime, self._map = path, None, {}

    def site(self, token: str | None) -> str | None:
        if self.path is None:
            return str(token or "anon")
        try:
            mtime = self.path.stat().st_mtime
            if mtime != self._mtime:
                self._map = {str(k): str(v) for k, v in json.loads(self.path.read_text()).items()}
                self._mtime = mtime
        except (OSError, ValueError) as e:
            log.warning("tokens file %s: %s", self.path, e)
        return self._map.get(str(token)) if token else None


# ---------------------------------------------------------------- models

class Models:
    """The four models on one device. detect / reid / clip_images take what the instances prepared (inferwire)."""

    def __init__(self, device: str, half: set[str], load: set[str]) -> None:
        import torch
        self.torch = torch
        self.device = device
        gpu = device.startswith("cuda")
        self.det: dict[str, dict] = {}
        self.reid = self.clip = None
        self.info: dict[str, dict] = {}
        for kind, name, imgsz in (("yolo", settings.yolo_model, settings.yolo_imgsz), ("ppe", settings.ppe_model, settings.ppe_imgsz)):
            weights = ROOT / "models" / name
            if kind not in load or not weights.exists():
                continue
            self.det[kind] = d = self._load_yolo(weights, imgsz, gpu and kind in half)
            self.info[kind] = {"names": {str(k): v for k, v in d["names"].items()}, "stride": d["stride"],
                               "sha256": wire.sha256(weights), "half": d["half"], "file": name}
            log.info("%s %s loaded on %s (%s)", kind, name, device, "fp16" if d["half"] else "fp32")
        if "reid" in load:
            from .reid import ReID, WEIGHTS_PATH
            from .reidprep import MEAN, STD
            self.reid = ReID(device)
            self.reid_mean = torch.tensor(MEAN, device=device).view(1, 3, 1, 1)
            self.reid_std = torch.tensor(STD, device=device).view(1, 3, 1, 1)
            self.info["reid"] = {"sha256": wire.sha256(WEIGHTS_PATH)}
        if "clip" in load:
            from .clip import MODEL, PRETRAINED, Clip
            self.clip = Clip(device)
            self.clip_mean = torch.tensor(self.clip.mean, device=device).view(1, 3, 1, 1)
            self.clip_std = torch.tensor(self.clip.std, device=device).view(1, 3, 1, 1)
            self.info["clip"] = {"model": MODEL, "pretrained": PRETRAINED, "precision": self.clip.precision}
            log.info("CLIP %s loaded on %s (%s)", MODEL, device, self.clip.precision)

    def _load_yolo(self, weights: Path, imgsz: int, half: bool) -> dict:
        from ultralytics import YOLO
        from ultralytics.cfg import DEFAULT_CFG_DICT
        m = YOLO(str(weights))
        kw = ({"quantize": 16} if "quantize" in DEFAULT_CFG_DICT else {"half": True}) if half else {}
        m.predict([np.zeros((imgsz // 2, imgsz, 3), np.uint8)], imgsz=imgsz, device=self.device, verbose=False, **kw)
        p = m.predictor
        stride = int(p.model.stride.max()) if hasattr(p.model.stride, "max") else int(p.model.stride)
        return {"model": m, "pred": p, "names": dict(m.names), "stride": stride, "half": bool(p.model.fp16)}

    def detect(self, kind: str, frames: np.ndarray, items: list[dict]) -> list[list[np.ndarray]]:
        """frames: (B, H, W, 3) letterboxed BGR uint8, as DetectionPredictor.preprocess takes them; items: per request
        {n, conf, classes, orig_shapes}. Per request, per frame: (k, 6) float32 rows [x1 y1 x2 y2 normalized to the
        original frame, conf, cls] - DetectionPredictor.postprocess / construct_result's boxes."""
        torch = self.torch
        from ultralytics.utils import nms, ops
        d = self.det[kind]
        pred = d["pred"]
        a = pred.args
        with torch.inference_mode():
            im = torch.from_numpy(frames).to(pred.device).permute(0, 3, 1, 2).flip(1).contiguous()
            im = (im.half() if pred.model.fp16 else im.float()).div_(255)
            preds = pred.model(im)
            p = preds[0] if isinstance(preds, (list, tuple)) else preds
            out, i = [], 0
            for it in items:
                rows = []
                for (h, w) in it["orig_shapes"]:
                    det = nms.non_max_suppression(p[i:i + 1], it["conf"], a.iou, it["classes"], a.agnostic_nms,
                                                  max_det=a.max_det, nc=0, end2end=getattr(pred.model, "end2end", False),
                                                  rotated=False)[0]
                    det[:, :4] = ops.scale_boxes(im.shape[2:], det[:, :4], (h, w))
                    scale = torch.tensor([w, h, w, h], device=det.device, dtype=det.dtype)
                    rows.append(torch.cat([det[:, :4] / scale, det[:, 4:6]], 1).float().cpu().numpy())
                    i += 1
                out.append(rows)
        return out

    def reid_features(self, pixels: np.ndarray) -> np.ndarray:
        """(B, 256, 128, 3) RGB uint8 -> (B, 512) L2-normalized, as reid.ReID.embed computes per crop."""
        torch = self.torch
        with torch.inference_mode():
            x = torch.from_numpy(pixels).to(self.device).permute(0, 3, 1, 2).float().div_(255)
            x = ((x - self.reid_mean) / self.reid_std).contiguous()
            f = torch.nn.functional.normalize(self.reid.model(x), dim=1)
        return f.float().cpu().numpy()

    def clip_images(self, pixels: np.ndarray) -> np.ndarray:
        """(B, 224, 224, 3) RGB uint8 -> (B, 512) L2-normalized, as clip.Clip.embed_images."""
        torch = self.torch
        with torch.inference_mode():
            x = torch.from_numpy(pixels).to(self.device).permute(0, 3, 1, 2).float().div_(255)
            x = ((x - self.clip_mean) / self.clip_std).contiguous()
            if self.clip.precision == "fp16":
                x = x.half()
            f = self.clip.model.encode_image(x).float()
            f = torch.nn.functional.normalize(f, dim=-1)
        return f.cpu().numpy()

    def clip_text(self, text: str) -> np.ndarray:
        return self.clip.embed_text(text)

    def is_oom(self, e: BaseException) -> bool:
        from .gpumem import is_oom
        return is_oom(e)

    def free(self) -> None:
        if self.device.startswith("cuda"):
            self.torch.cuda.empty_cache()

    def memory(self) -> dict | None:
        if not self.device.startswith("cuda"):
            return None
        t = self.torch.cuda
        return {"allocated_mb": t.memory_allocated() // 2**20, "reserved_mb": t.memory_reserved() // 2**20,
                "peak_reserved_mb": t.max_memory_reserved() // 2**20}


# ---------------------------------------------------------------- the service

class Service:
    def __init__(self, models, tokens: Tokens, max_batch: int = 32) -> None:
        self.models, self.tokens, self.max_batch = models, tokens, max_batch
        self.sched = Scheduler()
        self.started = time.time()
        self._lock = threading.Lock()
        self.sites: dict[str, dict] = {}
        self.latency: dict[str, deque] = {p: deque(maxlen=LATENCY_KEEP) for p in wire.PRIOS}
        self.busy: deque = deque()          # (end time, seconds of work) over the last BUSY_WINDOW_S
        self.batches = 0
        self.oom = 0
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._worker, name="infer-gpu", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ---- requests
    def request(self, header: dict, arrays: list) -> tuple[dict, list]:
        op = header.get("op")
        site = self.tokens.site(header.get("token"))
        if site is None:
            return {"ok": False, "kind": "unauthorized", "error": "unknown token"}, []
        if op == "hello":
            return {"ok": True, "proto": wire.PROTO, "device": self.models.device, "models": self.models.info,
                    "max_batch": self.max_batch}, []
        if op == "stats":
            return {"ok": True, **self.stats()}, []
        try:
            req = self._build(op, site, header, arrays)
        except BadRequest as e:
            return {"ok": False, "kind": "bad_request", "error": str(e)}, []
        self.sched.put(req)
        req.done.wait()
        if req.error:
            return {"ok": False, "kind": "failed", "error": req.error}, []
        return req.result

    def _build(self, op: str, site: str, h: dict, arrays: list) -> Request:
        prio = h.get("prio") or "verify"
        if prio not in wire.PRIOS:
            raise BadRequest(f"prio {prio!r}")
        m = self.models
        if op == "detect":
            kind = h.get("model")
            if kind not in getattr(m, "det", {}):
                raise BadRequest(f"model {kind!r} is not loaded")
            if len(arrays) != 1 or arrays[0].dtype != np.uint8 or arrays[0].ndim != 4 or arrays[0].shape[3] != 3:
                raise BadRequest("detect takes one (B, H, W, 3) uint8 array")
            shapes = h.get("orig_shapes") or []
            if len(shapes) != arrays[0].shape[0]:
                raise BadRequest("one orig_shape per frame")
            classes = h.get("classes")
            item = {"n": len(shapes), "conf": float(h.get("conf", 0.25)), "orig_shapes": [tuple(map(int, s)) for s in shapes],
                    "classes": [int(c) for c in classes] if classes is not None else None, "model": kind}
            return Request(op, site, prio, ("detect", kind, *arrays[0].shape[1:3]), item["n"], item, arrays)
        if op in ("reid", "clip_images"):
            need = (*wire.REID_HW, 3) if op == "reid" else (wire.CLIP_SIZE, wire.CLIP_SIZE, 3)
            if (op == "reid" and m.reid is None) or (op == "clip_images" and m.clip is None):
                raise BadRequest(f"{op} model is not loaded")
            if len(arrays) != 1 or arrays[0].dtype != np.uint8 or tuple(arrays[0].shape[1:]) != need:
                raise BadRequest(f"{op} takes one (B, {', '.join(map(str, need))}) uint8 array")
            return Request(op, site, prio, (op,), arrays[0].shape[0], {}, arrays)
        if op == "clip_text":
            text = h.get("text")
            if m.clip is None or not isinstance(text, str) or len(text) > MAX_TEXT:
                raise BadRequest("clip_text takes text (CLIP loaded)")
            return Request(op, site, prio, None, 1, {"text": text}, [])
        raise BadRequest(f"unknown op {op!r}")

    # ---- the GPU thread
    def _worker(self) -> None:
        while not self._stop.is_set():
            batch = self.sched.take(self.max_batch, timeout=1.0)
            if batch:
                self._run(batch)

    def _run(self, batch: list[Request]) -> None:
        t0 = time.perf_counter()
        try:
            results = self._execute(batch)
        except Exception as e:  # noqa: BLE001 - the service stays up; the callers get the error
            if self.models.is_oom(e):
                self.oom += 1
                self.models.free()
                if len(batch) > 1:
                    log.warning("GPU out of memory on a batch of %d requests: one at a time", len(batch))
                    for r in batch:
                        self._run([r])
                    return
                log.warning("GPU out of memory on one %s request of %d frames", batch[0].op, batch[0].n)
            else:
                log.exception("%s batch failed", batch[0].op)
            for r in batch:
                r.error = f"{type(e).__name__}: {e}"[:300]
                r.done.set()
            return
        t1 = time.perf_counter()
        self._account(batch, t0, t1)
        for r, res in zip(batch, results):
            r.result = res
            r.done.set()

    def _execute(self, batch: list[Request]) -> list[tuple[dict, list]]:
        op, m = batch[0].op, self.models
        if op == "detect":
            frames = batch[0].arrays[0] if len(batch) == 1 else np.concatenate([r.arrays[0] for r in batch])
            outs = m.detect(batch[0].header["model"], frames, [r.header for r in batch])
            res = []
            for rows in outs:
                res.append(({"ok": True, "counts": [len(x) for x in rows]},
                            [np.concatenate(rows) if rows else np.zeros((0, 6), np.float32)]))
            return res
        if op in ("reid", "clip_images"):
            x = batch[0].arrays[0] if len(batch) == 1 else np.concatenate([r.arrays[0] for r in batch])
            f = m.reid_features(x) if op == "reid" else m.clip_images(x)
            res, i = [], 0
            for r in batch:
                res.append(({"ok": True}, [f[i:i + r.n]]))
                i += r.n
            return res
        if op == "clip_text":
            return [({"ok": True}, [np.asarray(m.clip_text(batch[0].header["text"]), np.float32)])]
        raise RuntimeError(f"unknown op {op}")

    def _account(self, batch: list[Request], t0: float, t1: float) -> None:
        dt, total = t1 - t0, sum(r.n for r in batch) or 1
        now = time.time()
        with self._lock:
            self.batches += 1
            self.busy.append((now, dt))
            while self.busy and self.busy[0][0] < now - BUSY_WINDOW_S:
                self.busy.popleft()
            for r in batch:
                s = self.sites.setdefault(r.site, {"requests": 0, "frames": 0, "gpu_s": 0.0, "last": None})
                s["requests"] += 1
                s["frames"] += r.n
                s["gpu_s"] += dt * r.n / total
                s["last"] = now
                self.latency[r.prio].append(((t0 - r.t_in) * 1000, (t1 - r.t_in) * 1000))

    def stats(self) -> dict:
        def pct(xs, q):
            return round(sorted(xs)[min(len(xs) - 1, int(q * len(xs)))], 1) if xs else None
        with self._lock:
            now = time.time()
            busy = sum(dt for t, dt in self.busy if t >= now - BUSY_WINDOW_S)
            lat = {p: {"n": len(d), "wait_p50_ms": pct([w for w, _ in d], 0.5), "p50_ms": pct([t for _, t in d], 0.5),
                       "p95_ms": pct([t for _, t in d], 0.95)} for p, d in self.latency.items()}
            sites = {k: {**v, "gpu_s": round(v["gpu_s"], 3)} for k, v in self.sites.items()}
        return {"device": self.models.device, "uptime_s": round(time.time() - self.started), "batches": self.batches,
                "busy": round(min(1.0, busy / BUSY_WINDOW_S), 3), "queues": self.sched.depth(), "latency": lat,
                "sites": sites, "oom": self.oom, "memory": self.models.memory()}


# ---------------------------------------------------------------- the socket server

class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock, service = self.request, self.server.service
        while True:
            try:
                header, arrays = wire.recv(sock)
            except (wire.WireError, OSError, ValueError):
                return
            try:
                out = service.request(header, arrays)
            except Exception as e:  # noqa: BLE001
                log.exception("request failed")
                out = ({"ok": False, "kind": "failed", "error": f"{type(e).__name__}: {e}"[:300]}, [])
            try:
                wire.send(sock, *out)
            except OSError:
                return


class _TCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


if hasattr(socketserver, "UnixStreamServer"):
    class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True


def serve(address: str, service: Service, mode: int = 0o666):
    """Bind address (a socket path or host:port) and return the server; serve_forever() runs it."""
    family, addr = wire.parse_address(address)
    if family == getattr(socket, "AF_UNIX", None) and isinstance(addr, str):
        path = Path(addr)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        server = _UnixServer(str(path), _Handler)
        os.chmod(path, mode)   # instances run as their own uids; the token says who is calling
    else:
        server = _TCPServer(addr, _Handler)
    server.service = service
    return server


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--listen", required=True, help="socket path (/run/axiom-infer/gpu1.sock) or host:port")
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--tokens", type=Path, default=None, help="JSON {token: instance id}; without it any token is accepted")
    ap.add_argument("--models", default=",".join(ALL_MODELS))
    ap.add_argument("--half", default="yolo,ppe", help="models run in fp16 on a GPU (CLIP is always fp16 there)")
    ap.add_argument("--max-batch", type=int, default=32)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.tokens is None:
        log.warning("no --tokens: any token is accepted (development only)")
    models = Models(args.device, {s for s in args.half.split(",") if s}, {s for s in args.models.split(",") if s})
    service = Service(models, Tokens(args.tokens), args.max_batch)
    server = serve(args.listen, service)
    log.info("inference service on %s (%s), models %s", args.listen, args.device, ", ".join(models.info))

    def report():
        while True:
            time.sleep(60)
            s = service.stats()
            log.info("busy %.0f%%, queues %s, verify p95 %s ms, %d Sites", s["busy"] * 100, s["queues"],
                     s["latency"]["verify"]["p95_ms"], len(s["sites"]))
    threading.Thread(target=report, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        service.stop()


if __name__ == "__main__":
    main()
