"""The verifier's YOLO on a Hailo site, with a CPU fallback, and the detector health checks the hub alerts on.

A Hailo-8 that drops off the PCI bus (a reboot once left a site with `HAILO_OUT_OF_PHYSICAL_DEVICES`) must not
silently stop verification. HailoSupervisor stands in for the model (the slice of ultralytics.YOLO the verifier,
merge.py and ppe.py call: .names, .predict, .to) and:

  * at startup: if the Hailo model cannot be built (no device, no hailo_platform, a missing HEF...), logs an ERROR
    and runs ultralytics on the CPU with the matching .pt (cpu_weights_for); with no CPU weights either the service
    stays up and detection is reported down (ready False);
  * at runtime: after FAIL_LIMIT consecutive Hailo inference exceptions it does the same, live, and the frames in
    hand are run on the CPU;
  * while on the fallback: a daemon thread tries the Hailo again every NVR_HAILO_RETRY_S (default 600 s) and switches
    back on success. Building the Hailo model can block; it never runs on the pipeline's YOLO thread.

health_alerts() is the list /api/system, /api/home and the hub heartbeat carry: `detector_fallback` while the Hailo
is unavailable and `detector_stalled` when the verify queue has had work for STALL_AFTER_S with no event finished
(stall_check, a pure function), whatever the cause.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("nvr.detector")

FAIL_LIMIT = 3                 # consecutive Hailo inference exceptions before the live fallback
STALL_AFTER_S = 15 * 60        # verify queue non-empty with no completion for this long: stalled
CPU_LABEL = "cpu (hailo unavailable)"
DOWN_LABEL = "none (hailo unavailable)"
FALLBACK_TEXT = "Hailo accelerator not found; detection is running on the CPU"
DOWN_TEXT = "Hailo accelerator not found and no CPU model to fall back to; events are not being verified"


def cpu_weights_for(model_name: str, models_dir: Path) -> Path | None:
    """The ultralytics .pt to run on the CPU instead of a Hailo .hef: the same network under ultralytics' name
    (yolov11s.hef -> yolo11s.pt, yolov8s.hef -> yolov8s.pt), else yolo11s.pt, else yolo11n.pt; the first that
    exists in models_dir, or None."""
    stem = Path(model_name).stem.lower()
    names = []
    m = re.match(r"^yolo(?:v)?(\d+)([a-z]*)", stem)
    if m:
        n, size = int(m.group(1)), m.group(2)
        names.append(f"yolo{n}{size}.pt" if n >= 11 else f"yolov{n}{size}.pt")   # ultralytics drops the v from 11 on
    names.append(f"{stem}.pt")
    names += ["yolo11s.pt", "yolo11n.pt"]
    for name in dict.fromkeys(names):
        if (models_dir / name).exists():
            return models_dir / name
    return None


class CpuModel:
    """ultralytics YOLO pinned to the CPU: callers pass device=settings.yolo_device ("hailo"), which torch refuses."""

    def __init__(self, model) -> None:
        self.model = model
        self.names = model.names

    def predict(self, images, **kw):
        kw["device"] = "cpu"
        return self.model.predict(images, **kw)

    __call__ = predict

    def to(self, device) -> "CpuModel":
        return self


def load_cpu_model(weights: Path) -> CpuModel:
    from ultralytics import YOLO  # heavy import, only on the fallback
    m = YOLO(str(weights))
    m.to("cpu")
    return CpuModel(m)


class HailoSupervisor:
    """The Hailo model with a CPU fallback and recovery (see the module docstring). Factories are injected so tests
    run without the hardware: hailo_factory() -> a HailoYOLO-like model (raises when the Hailo is unavailable),
    cpu_factory() -> a model, or None when there are no CPU weights."""

    def __init__(self, hailo_factory: Callable, cpu_factory: Callable, retry_s: float = 600.0,
                 fail_limit: int = FAIL_LIMIT, clock: Callable[[], float] = time.time, start_thread: bool = True) -> None:
        self.hailo_factory, self.cpu_factory = hailo_factory, cpu_factory
        self.retry_s, self.fail_limit, self.clock = retry_s, fail_limit, clock
        self.start_thread = start_thread
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.model = None             # what predict runs on now
        self.on_hailo = False
        self.device = DOWN_LABEL
        self.fallback: dict | None = None
        self.failures = 0             # consecutive Hailo inference exceptions
        self.switches: list[str] = []  # "fallback" / "recovered", for tests and the log
        try:
            self._use_hailo(self.hailo_factory())
        except Exception as e:
            log.error("Hailo YOLO failed to initialize: %s; falling back to the CPU", _reason(e))
            self._fall_back(_reason(e))

    # ---- the ultralytics surface
    @property
    def names(self):
        m = self.model
        if m is not None:
            return m.names
        from .hailo import NAMES   # parsing-only module: no hailo_platform import
        return NAMES

    @property
    def ready(self) -> bool:
        return self.model is not None

    def to(self, device) -> "HailoSupervisor":
        return self

    def predict(self, images, **kw):
        with self._lock:
            model, on_hailo = self.model, self.on_hailo
        if model is None:
            raise RuntimeError(f"no detector: {self.fallback.get('error') if self.fallback else 'not loaded'}")
        if not on_hailo:
            return model.predict(images, **kw)
        try:
            res = model.predict(images, **kw)
        except Exception as e:
            with self._lock:
                if self.model is not model:     # switched meanwhile: run on whatever is current
                    pass
                else:
                    self.failures += 1
                    if self.failures < self.fail_limit:
                        raise
                    log.error("Hailo inference failed %d times in a row (%s); switching to the CPU", self.failures, _reason(e))
                    self._fall_back(f"inference failed {self.failures} times: {_reason(e)}")
                    _close(model)
            if self.model is None:
                raise
            return self.predict(images, **kw)
        self.failures = 0
        return res

    __call__ = predict

    # ---- switching
    def _use_hailo(self, model) -> None:
        with self._lock:
            old = self.model if not self.on_hailo else None
            self.model, self.on_hailo = model, True
            self.device = getattr(model, "device_name", None) or "hailo-8"
            self.fallback, self.failures = None, 0
        if old is not None:
            del old   # the CPU model goes with its last reference

    def _fall_back(self, error: str) -> None:
        with self._lock:
            cpu = None
            try:
                cpu = self.cpu_factory()
            except Exception as e:
                log.error("CPU YOLO fallback failed to load: %s", _reason(e))
            if cpu is None:
                log.error("No CPU YOLO weights to fall back to: detection is down until the Hailo is back")
            self.model, self.on_hailo, self.failures = cpu, False, 0
            self.device = CPU_LABEL if cpu is not None else DOWN_LABEL
            self.fallback = {"wanted": "hailo", "using": "cpu" if cpu is not None else None,
                             "since": round(self.clock(), 1), "error": error[:300]}
            self.switches.append("fallback")
        self._start_retry()

    def retry_once(self) -> bool:
        """One attempt to bring the Hailo back (the retry thread calls this). True when it is in use again."""
        if self.fallback is None:
            return True
        try:
            model = self.hailo_factory()
        except Exception as e:
            log.info("Hailo still unavailable: %s (next try in %.0f s)", _reason(e), self.retry_s)
            with self._lock:
                if self.fallback is not None:
                    self.fallback["last_retry"] = round(self.clock(), 1)
            return False
        self._use_hailo(model)
        with self._lock:
            self.switches.append("recovered")
        log.info("Hailo YOLO is back on %s; detection switched back from the CPU", self.device)
        return True

    def _start_retry(self) -> None:
        if not self.start_thread or self.retry_s <= 0 or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._retry_loop, name="hailo-retry", daemon=True)
        self._thread.start()

    def _retry_loop(self) -> None:
        while self.fallback is not None:
            if self._wake.wait(self.retry_s):
                return   # close()
            try:
                if self.retry_once():
                    return
            except Exception:   # never let the thread die with the fallback still on
                log.exception("Hailo retry failed")

    def status(self) -> dict | None:
        """yolo_fallback for /api/system: None while on the Hailo."""
        with self._lock:
            return dict(self.fallback) if self.fallback else None

    def close(self) -> None:
        self._wake.set()
        with self._lock:
            if self.on_hailo:
                _close(self.model)


def _close(model) -> None:
    try:
        if model is not None and hasattr(model, "close"):
            model.close()
    except Exception:
        pass


def _reason(e: BaseException) -> str:
    s = str(e).strip()
    return f"{type(e).__name__}: {s}" if s else type(e).__name__


# ---------------------------------------------------------------- health

def stall_check(prev: dict | None, queue_size: int, completions: int, now: float,
                after_s: float = STALL_AFTER_S) -> tuple[dict, bool]:
    """Verify-queue progress, as a pure function. prev/returned state: {"since": ts the queue has had work without a
    completion, or None; "completions": count seen then}. Stalled: work waiting for after_s and nothing finished."""
    prev = prev or {"since": None, "completions": completions}
    if queue_size <= 0:
        return {"since": None, "completions": completions}, False
    if prev["since"] is None or completions != prev["completions"]:
        return {"since": now, "completions": completions}, False
    return prev, now - prev["since"] >= after_s


def yolo_status(pipeline) -> dict:
    """yolo_ready / yolo_device / yolo_fallback as /api/system, /api/home and the heartbeat report them."""
    from .config import settings
    v = getattr(pipeline, "verifier", None)
    model = getattr(v, "model", None)
    ready = v is not None and bool(getattr(model, "ready", True))
    fb = model.status() if isinstance(model, HailoSupervisor) else None
    return {"yolo_ready": ready, "yolo_device": getattr(v, "device", None) or settings.yolo_device, "yolo_fallback": fb}


def health_alerts(pipeline) -> list[dict]:
    """Detector problems for the Home page, Settings -> System and the hub (alerts.py opens/closes by kind)."""
    out = []
    st = yolo_status(pipeline)
    fb = st["yolo_fallback"]
    if fb:
        out.append({"kind": "detector_fallback", "text": FALLBACK_TEXT if fb.get("using") else DOWN_TEXT,
                    "since": fb.get("since"), "error": fb.get("error")})
    stall = getattr(pipeline, "verify_stall", None) or {}
    if stall.get("stalled"):
        n = stall.get("queue", 0)
        out.append({"kind": "detector_stalled", "text": f"Event verification has stalled: {n} event{'' if n == 1 else 's'} waiting",
                    "since": stall.get("since"), "queue": n})
    return out
