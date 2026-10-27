"""A central instance's share of a shared GPU (gpumem.py) without a GPU: the torch cap and fraction, applying it on a
fake torch, out-of-memory recognition, the `gpu_memory_full` health alert and the verify worker's one retry.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_gpu_share.py   (from backend/)
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-gpushare-test-")      # never the real DB
os.environ["NVR_RECORDINGS_DIR"] = tempfile.mkdtemp(prefix="nvr-gpushare-rec-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import detector, gpumem  # noqa: E402
from nvr.config import settings  # noqa: E402


class FakeCuda:
    def __init__(self):
        self.fraction, self.emptied, self.initialized = None, 0, True

    def is_available(self):
        return True

    def is_initialized(self):
        return self.initialized

    def get_device_properties(self, idx):
        return types.SimpleNamespace(total_memory=23028 * 2**20)

    def set_per_process_memory_fraction(self, f, idx):
        self.fraction = (f, idx)

    def empty_cache(self):
        self.emptied += 1

    def memory_reserved(self, idx):
        return 1500 * 2**20

    def max_memory_reserved(self, idx):
        return 2600 * 2**20


def _fake_torch():
    t = types.ModuleType("torch")
    t.cuda = FakeCuda()
    return t


def _reset():
    gpumem._oom.update(count=0, last=None)
    gpumem._cap_mb = None


def test_cap_and_fraction():
    assert gpumem.torch_cap_mb(4096) == 4096 - gpumem.CONTEXT_MB
    assert gpumem.torch_cap_mb(600) == gpumem.MIN_TORCH_MB          # never below what the model needs
    assert abs(gpumem.fraction(4096, 23028) - 3584 / 23028) < 1e-9
    assert gpumem.fraction(99999, 23028) == 1.0                     # a share bigger than the card: no cap
    assert gpumem.fraction(4096, 0) == 1.0


def test_apply_caps_torch_only_with_a_share_on_cuda():
    _reset()
    saved = (settings.gpu_mem_mb, settings.yolo_device, sys.modules.get("torch"))
    fake = _fake_torch()
    sys.modules["torch"] = fake
    try:
        settings.gpu_mem_mb, settings.yolo_device = 0, "cuda:0"     # no share: untouched
        gpumem.apply()
        assert fake.cuda.fraction is None
        settings.gpu_mem_mb, settings.yolo_device = 4096, "cpu"     # no GPU: untouched
        gpumem.apply()
        assert fake.cuda.fraction is None
        settings.gpu_mem_mb, settings.yolo_device = 4096, "cuda:0"
        gpumem.apply()
        assert fake.cuda.fraction == (3584 / 23028, 0) and gpumem._cap_mb == 3584
        s = gpumem.status()
        assert s == {"share_mb": 4096, "cap_mb": 3584, "reserved_mb": 1500, "peak_mb": 2600, "oom": 0, "oom_last": None}
        fake.cuda.initialized = False                               # status never starts CUDA itself
        assert gpumem.status() is None
    finally:
        settings.gpu_mem_mb, settings.yolo_device = saved[0], saved[1]
        if saved[2] is None:
            sys.modules.pop("torch", None)
        else:
            sys.modules["torch"] = saved[2]
        _reset()


def test_oom_recognised_counted_and_alerted():
    _reset()

    class OutOfMemoryError(RuntimeError):
        pass

    assert gpumem.is_oom(OutOfMemoryError("CUDA out of memory. Tried to allocate 20.00 MiB"))
    assert gpumem.is_oom(RuntimeError("CUDA error: out of memory"))
    assert not gpumem.is_oom(RuntimeError("HAILO_TIMEOUT(4)")) and not gpumem.is_oom(ValueError("out of memory"))
    assert gpumem.health_alert() is None
    gpumem.note_oom(RuntimeError("CUDA out of memory"))
    a = gpumem.health_alert()
    assert a["kind"] == "gpu_memory_full" and a["count"] == 1 and "share of the GPU" in a["text"]
    assert gpumem.health_alert(now=a["since"] + gpumem.OOM_ALERT_S + 1) is None    # cleared after a quiet spell
    # the detector's list (/api/system, the hub heartbeat) carries it
    p = types.SimpleNamespace(verifier=None, verify_stall={})
    assert any(x["kind"] == "gpu_memory_full" for x in detector.health_alerts(p))
    _reset()


def test_verify_retries_once_after_oom():
    from nvr import pipeline

    _reset()
    calls = []

    class P:
        def __init__(self, fail_times, error="CUDA out of memory. Tried to allocate 64.00 MiB"):
            self.fail_times, self.error = fail_times, error

        async def _verify(self, eid):
            calls.append(eid)
            if len(calls) <= self.fail_times:
                raise RuntimeError(self.error)

    run = lambda p: asyncio.run(pipeline.Pipeline._verify_retrying_oom(p, 7))  # noqa: E731
    run(P(1))                                                    # one OOM: freed, tried again, verified
    assert calls == [7, 7] and gpumem._oom["count"] == 1
    calls.clear()
    try:
        run(P(2))                                                # still out of memory: the worker marks it an error
        raise AssertionError("expected the second OOM to propagate")
    except RuntimeError as e:
        assert gpumem.is_oom(e) and calls == [7, 7] and gpumem._oom["count"] == 2
    calls.clear()
    try:
        run(P(1, "boom"))                                        # any other error: no retry
        raise AssertionError("expected boom")
    except RuntimeError as e:
        assert str(e) == "boom" and calls == [7] and gpumem._oom["count"] == 2
    _reset()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
