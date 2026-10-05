"""Hailo -> CPU YOLO fallback and detector health (detector.py) without the hardware: CPU weight derivation, the
startup fallback when the Hailo cannot be opened, the live fallback after repeated inference failures, the retry
switching back, /api/system's yolo_* fields and the stalled-verify-queue check.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_detector_fallback.py   (from backend/)
"""
import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-detector-test-")      # never the real DB
os.environ["NVR_RECORDINGS_DIR"] = tempfile.mkdtemp(prefix="nvr-detector-rec-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import detector, hailo, verifier  # noqa: E402
from nvr.config import settings  # noqa: E402

OUT_OF_DEVICES = "Failed to create vdevice. there are not enough free devices. requested: 1, found: 0 (HAILO_OUT_OF_PHYSICAL_DEVICES(74))"


class FakeHailo:
    names = hailo.NAMES
    device_name = "hailo-8"

    def __init__(self, fail=False):
        self.fail, self.calls, self.closed = fail, 0, False

    def predict(self, images, **kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError("HAILO_TIMEOUT(4)")
        return ["hailo"] * len(images)

    def close(self):
        self.closed = True


class FakeCpu:
    names = hailo.NAMES

    def __init__(self):
        self.devices = []

    def predict(self, images, **kw):
        self.devices.append(kw.get("device"))
        return ["cpu"] * len(images)


def raising(msg=OUT_OF_DEVICES):
    def f():
        raise RuntimeError(msg)
    return f


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_cpu_weights_follow_the_hef():
    d = Path(tempfile.mkdtemp(prefix="nvr-models-"))
    assert detector.cpu_weights_for("yolov11s.hef", d) is None              # nothing there: detection down
    (d / "yolo11n.pt").touch()
    assert detector.cpu_weights_for("yolov11s.hef", d).name == "yolo11n.pt"
    (d / "yolo11s.pt").touch()
    assert detector.cpu_weights_for("yolov11s.hef", d).name == "yolo11s.pt"
    assert detector.cpu_weights_for("yolov8s.hef", d).name == "yolo11s.pt"  # no yolov8s.pt: the default
    (d / "yolov8s.pt").touch()
    assert detector.cpu_weights_for("yolov8s.hef", d).name == "yolov8s.pt"
    (d / "yolo11m.pt").touch()
    assert detector.cpu_weights_for("yolov11m.hef", d).name == "yolo11m.pt"
    assert detector.cpu_weights_for("custom_net.hef", d).name == "yolo11s.pt"


def test_cpu_model_is_pinned_to_the_cpu():
    m = detector.CpuModel(FakeCpu())
    assert m.predict([1, 2], device="hailo", conf=0.25) == ["cpu", "cpu"] and m.model.devices == ["cpu"]
    assert m.to("hailo") is m and m.names == hailo.NAMES


def test_startup_falls_back_when_the_hailo_is_missing():
    cpu = FakeCpu()
    s = detector.HailoSupervisor(raising(), lambda: detector.CpuModel(cpu), clock=Clock(), start_thread=False)
    assert s.ready and not s.on_hailo and s.device == "cpu (hailo unavailable)"
    st = s.status()
    assert st["wanted"] == "hailo" and st["using"] == "cpu" and st["since"] == 1000.0
    assert "HAILO_OUT_OF_PHYSICAL_DEVICES" in st["error"]
    assert s.predict([1], device="hailo") == ["cpu"] and cpu.devices == ["cpu"]
    assert s.names == hailo.NAMES and s.switches == ["fallback"]
    # an import error of hailo_platform is a fallback like any other
    s2 = detector.HailoSupervisor(lambda: __import__("hailo_platform_not_installed"), lambda: FakeCpu(), start_thread=False)
    assert s2.ready and "ModuleNotFoundError" in s2.status()["error"]


def test_startup_without_cpu_weights_keeps_running_with_detection_down():
    s = detector.HailoSupervisor(raising(), lambda: None, start_thread=False)
    assert not s.ready and s.device == "none (hailo unavailable)" and s.status()["using"] is None
    try:
        s.predict([1])
        raise AssertionError("no model: predict must raise (the event goes to error, the service stays up)")
    except RuntimeError as e:
        assert "no detector" in str(e)
    assert s.names == hailo.NAMES                                            # merge / ppe still read class names


def test_runtime_fallback_after_three_failures():
    h, cpu = FakeHailo(), FakeCpu()
    s = detector.HailoSupervisor(lambda: h, lambda: detector.CpuModel(cpu), clock=Clock(), start_thread=False)
    assert s.on_hailo and s.status() is None and s.predict([1]) == ["hailo"]
    h.fail = True
    for _ in range(2):
        try:
            s.predict([1])
            raise AssertionError("first failures are the caller's")
        except RuntimeError as e:
            assert "HAILO_TIMEOUT" in str(e)
        assert s.on_hailo
    assert s.predict([1, 2]) == ["cpu", "cpu"]                                # the third one runs on the CPU
    assert not s.on_hailo and h.closed and s.device == "cpu (hailo unavailable)"
    assert "inference failed 3 times" in s.status()["error"]
    # a success in between resets the count
    h2 = FakeHailo()
    s2 = detector.HailoSupervisor(lambda: h2, lambda: FakeCpu(), start_thread=False)
    for fail in (True, True, False, True, True):
        h2.fail = fail
        try:
            s2.predict([1])
        except RuntimeError:
            pass
    assert s2.on_hailo


def test_retry_switches_back_when_the_hailo_returns():
    clock = Clock()
    present = {"yes": False}

    def factory():
        if not present["yes"]:
            raise RuntimeError(OUT_OF_DEVICES)
        return FakeHailo()

    s = detector.HailoSupervisor(factory, lambda: FakeCpu(), clock=clock, start_thread=False)
    clock.t = 1600
    assert s.retry_once() is False and s.status()["last_retry"] == 1600 and not s.on_hailo
    present["yes"] = True
    assert s.retry_once() is True
    assert s.on_hailo and s.device == "hailo-8" and s.status() is None and s.switches == ["fallback", "recovered"]
    assert s.predict([1]) == ["hailo"]


def test_retry_thread_never_blocks_inference():
    gate, present = threading.Event(), {"yes": False}

    def slow_factory():
        if not present["yes"]:
            raise RuntimeError(OUT_OF_DEVICES)
        gate.wait(5)                     # HailoRT taking its time to open the device
        return FakeHailo()

    s = detector.HailoSupervisor(slow_factory, lambda: FakeCpu(), retry_s=0.05)
    try:
        present["yes"] = True
        time.sleep(0.2)                  # the retry thread is now inside the slow factory
        t0 = time.perf_counter()
        assert s.predict([1]) == ["cpu"] and time.perf_counter() - t0 < 0.5
        gate.set()
        for _ in range(100):
            if s.on_hailo:
                break
            time.sleep(0.02)
        assert s.on_hailo and s.status() is None
    finally:
        gate.set()
        s.close()


def test_verifier_falls_back_with_the_settings():
    saved = settings.yolo_device, settings.yolo_model, settings.hailo_retry_s, hailo.HailoYOLO, detector.load_cpu_model
    try:
        settings.yolo_device, settings.yolo_model, settings.hailo_retry_s = "hailo", "yolov11s.hef", 0

        def no_device(weights):
            raise RuntimeError(OUT_OF_DEVICES)
        hailo.HailoYOLO = no_device
        loaded = []
        detector.load_cpu_model = lambda pt: loaded.append(pt.name) or detector.CpuModel(FakeCpu())
        mdir = verifier.ROOT / "models"
        want = detector.cpu_weights_for("yolov11s.hef", mdir)
        v = verifier.Verifier()
        if want is None:
            assert not v.model.ready and v.device == "none (hailo unavailable)"
        else:
            assert loaded == [want.name] and v.device == "cpu (hailo unavailable)" and v.model.ready
            assert v._predict([1]) == ["cpu"] and list(v.frame_ms)
    finally:
        settings.yolo_device, settings.yolo_model, settings.hailo_retry_s, hailo.HailoYOLO, detector.load_cpu_model = saved


def test_stall_check_is_a_pure_function_of_queue_completions_and_time():
    st, stalled = detector.stall_check(None, 0, 10, 0)
    assert not stalled and st["since"] is None
    st, stalled = detector.stall_check(st, 5, 10, 100)        # work arrives
    assert not stalled and st["since"] == 100
    st, stalled = detector.stall_check(st, 40, 10, 100 + 899)
    assert not stalled
    st, stalled = detector.stall_check(st, 2883, 10, 100 + 900)
    assert stalled                                             # 15 min with work and nothing finished
    st, stalled = detector.stall_check(st, 2883, 11, 1500)     # one finished: the clock restarts
    assert not stalled and st["since"] == 1500
    st, stalled = detector.stall_check(st, 0, 11, 9000)        # queue drained
    assert not stalled and st["since"] is None
    st, _ = detector.stall_check(st, 1, 11, 9000)
    assert detector.stall_check(st, 1, 11, 9000 + 3600)[1]
    assert not detector.stall_check(st, 1, 11, 9000 + 60, after_s=120)[1]


def _pipeline(model=None, ready=True):
    from nvr.pipeline import Pipeline
    p = Pipeline()
    if model is not None:
        v = verifier.Verifier.__new__(verifier.Verifier)
        v.model, v._device = model, None
        p.verifier = v
    return p


def test_pipeline_raises_and_clears_the_stalled_alert():
    p = _pipeline(detector.HailoSupervisor(lambda: FakeHailo(), lambda: FakeCpu(), start_thread=False))
    for i in range(3):
        p.verify_q.put_nowait(i)
    assert not p.check_verify_stall(now=1000)
    assert not p.check_verify_stall(now=1000 + 600)
    assert p.check_verify_stall(now=1000 + 901)
    alerts = detector.health_alerts(p)
    assert [a["kind"] for a in alerts] == ["detector_stalled"]
    assert alerts[0]["text"] == "Event verification has stalled: 3 events waiting" and alerts[0]["since"] == 1000
    p.verify_done += 1
    assert not p.check_verify_stall(now=2000) and detector.health_alerts(p) == []
    # YOLO not loaded yet (still starting): a queue waiting for it is not a stall
    q = _pipeline()
    q.verify_q.put_nowait(1)
    q.check_verify_stall(now=0)
    assert not q.check_verify_stall(now=5000)


def test_system_payload_reports_the_fallback():
    from nvr import api
    saved = getattr(api.state, "pipeline", None)
    try:
        api.state.pipeline = _pipeline(detector.HailoSupervisor(raising(), lambda: FakeCpu(), clock=Clock(1234.5), start_thread=False))
        s = asyncio.run(api.system())
        assert s["yolo_ready"] is True and s["yolo_device"] == "cpu (hailo unavailable)"
        assert s["yolo_fallback"] == {"wanted": "hailo", "using": "cpu", "since": 1234.5, "error": s["yolo_fallback"]["error"]}
        assert "HAILO_OUT_OF_PHYSICAL_DEVICES" in s["yolo_fallback"]["error"]
        assert s["health_alerts"] == [{"kind": "detector_fallback", "text": "Hailo accelerator not found; detection is running on the CPU",
                                       "since": 1234.5, "error": s["yolo_fallback"]["error"]}]
        # nothing can detect: not ready, and the alert says so
        api.state.pipeline = _pipeline(detector.HailoSupervisor(raising(), lambda: None, start_thread=False))
        s = asyncio.run(api.system())
        assert s["yolo_ready"] is False and s["yolo_fallback"]["using"] is None
        assert s["health_alerts"][0]["kind"] == "detector_fallback" and "not being verified" in s["health_alerts"][0]["text"]
        # on the Hailo (or a CUDA / CPU site): no fallback, no alerts
        api.state.pipeline = _pipeline(detector.HailoSupervisor(lambda: FakeHailo(), lambda: FakeCpu(), start_thread=False))
        s = asyncio.run(api.system())
        assert s["yolo_ready"] is True and s["yolo_device"] == "hailo-8" and s["yolo_fallback"] is None and s["health_alerts"] == []
        plain = verifier.Verifier.__new__(verifier.Verifier)
        plain.model, plain._device = FakeCpu(), "cuda:0"
        api.state.pipeline = _pipeline()
        api.state.pipeline.verifier = plain
        s = asyncio.run(api.system())
        assert s["yolo_ready"] is True and s["yolo_device"] == "cuda:0" and s["yolo_fallback"] is None
        api.state.pipeline = _pipeline()                       # still loading
        s = asyncio.run(api.system())
        assert s["yolo_ready"] is False and s["yolo_fallback"] is None
    finally:
        api.state.pipeline = saved


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
