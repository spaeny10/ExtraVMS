"""This server's share of a GPU it shares with other servers (central instances: several Sites on one card).

NVR_GPU_MEM_MB (axiom_host.py passes it to every central instance with a GPU) is everything this process may hold on
its card: the CUDA context, the models and the frames in flight. torch's allocator is capped at that less
CONTEXT_MB (the context and NVENC sessions are outside its count) with set_per_process_memory_fraction, so a busy
Site runs out of memory in its own process instead of taking its neighbours' (an out-of-memory error there frees
torch's cache, the event is tried once more, and `gpu_memory_full` is raised for OOM_ALERT_S). 0 = no cap: a site
box with a GPU of its own.

status() is /api/system `gpu_mem` (the host collects it with the instance's work numbers): the cap, what torch holds
now and its peak since start, in MB, and the out-of-memory count. It never initialises CUDA itself.
"""
from __future__ import annotations

import logging
import sys
import threading
import time

from .config import settings

log = logging.getLogger("nvr.gpumem")

CONTEXT_MB = 512          # the CUDA context, cuDNN handles and NVENC sessions: outside torch's allocator
MIN_TORCH_MB = 512        # never cap torch below this (a share that small could not hold the YOLO model)
OOM_ALERT_S = 15 * 60     # `gpu_memory_full` stays up this long after the last out-of-memory error
OOM_TEXT = "GPU memory full: this server reached its share of the GPU; events may fail to verify"

_lock = threading.Lock()
_oom = {"count": 0, "last": None}
_cap_mb: int | None = None


def torch_cap_mb(share_mb: int) -> int:
    """torch's part of the share (the rest is the CUDA context's)."""
    return max(MIN_TORCH_MB, int(share_mb) - CONTEXT_MB)


def fraction(share_mb: int, total_mb: float) -> float:
    """set_per_process_memory_fraction's argument: torch's part of the share over the card's memory, at most 1."""
    if total_mb <= 0:
        return 1.0
    return min(1.0, torch_cap_mb(share_mb) / total_mb)


def _device_index(device: str) -> int | None:
    if not device.startswith("cuda"):
        return None
    _, _, idx = device.partition(":")
    return int(idx) if idx.isdigit() else 0


def apply() -> None:
    """At startup, before any model loads: cap torch on this server's GPU at its share (no-op without one)."""
    global _cap_mb
    share = int(settings.gpu_mem_mb or 0)
    idx = _device_index(settings.torch_device)
    if share <= 0 or idx is None:
        return
    try:
        import torch
        if not torch.cuda.is_available():
            return
        total = torch.cuda.get_device_properties(idx).total_memory / 2**20
        torch.cuda.set_per_process_memory_fraction(fraction(share, total), idx)
        _cap_mb = torch_cap_mb(share)
        log.info("GPU %d: this server's share is %d MB of %d MB (torch capped at %d MB)", idx, share, total, _cap_mb)
    except Exception:  # noqa: BLE001 - an uncapped server still records and verifies
        log.exception("could not cap GPU memory at %d MB", share)


def is_oom(e: BaseException) -> bool:
    """A CUDA out-of-memory error (torch.OutOfMemoryError, or the RuntimeError older torch and cuDNN raise)."""
    if type(e).__name__ == "OutOfMemoryError":
        return True
    return isinstance(e, RuntimeError) and "out of memory" in str(e).lower()


def note_oom(e: BaseException | None = None) -> None:
    """Count one out-of-memory error and give torch's cached blocks back, so the retry has room."""
    with _lock:
        _oom["count"] += 1
        _oom["last"] = time.time()
    share = f"share {settings.gpu_mem_mb} MB" if settings.gpu_mem_mb else "no share set"
    log.warning("GPU out of memory (%s): %s", share, str(e or "")[:200])
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass


def status() -> dict | None:
    """{share_mb, cap_mb, reserved_mb, peak_mb, oom, oom_last}, or None on a server without CUDA in use."""
    torch = sys.modules.get("torch")
    idx = _device_index(settings.torch_device)
    if torch is None or idx is None:
        return None
    try:
        if not torch.cuda.is_initialized():
            return None
        mb = lambda b: round(b / 2**20)  # noqa: E731
        reserved, peak = mb(torch.cuda.memory_reserved(idx)), mb(torch.cuda.max_memory_reserved(idx))
    except Exception:  # noqa: BLE001
        return None
    with _lock:
        oom = dict(_oom)
    return {"share_mb": int(settings.gpu_mem_mb or 0) or None, "cap_mb": _cap_mb, "reserved_mb": reserved,
            "peak_mb": peak, "oom": oom["count"], "oom_last": oom["last"]}


def health_alert(now: float | None = None) -> dict | None:
    """`gpu_memory_full` while an out-of-memory error happened in the last OOM_ALERT_S."""
    with _lock:
        last, n = _oom["last"], _oom["count"]
    if last is None or (now or time.time()) - last > OOM_ALERT_S:
        return None
    return {"kind": "gpu_memory_full", "text": OOM_TEXT, "since": last, "count": n,
            "share_mb": int(settings.gpu_mem_mb or 0) or None}
