"""The shared inference service (inferd.py) and its stand-ins on the instances (inference.py): the wire format, the
letterbox (pixel for pixel ultralytics'), priority and round-robin scheduling, tokens, the version / weights
handshake, an out-of-memory batch split, an unreachable service (InferUnavailable, the alert, the event put back in
the verify queue), no torch on an instance using the service, and end to end on the CPU: the service's YOLO, PPE,
re-ID and CLIP answers against the same models run locally.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_inference.py   (from backend/)
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-infer-test-")      # never the real DB
os.environ["NVR_RECORDINGS_DIR"] = tempfile.mkdtemp(prefix="nvr-infer-rec-")
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from nvr import inference, inferd, inferwire as wire  # noqa: E402

ASSETS = BACKEND.parent / ".venv" / "Lib" / "site-packages" / "ultralytics" / "assets"


def _assets():
    p = ASSETS if ASSETS.exists() else Path(__import__("ultralytics").__file__).parent / "assets"
    return cv2.imread(str(p / "bus.jpg")), cv2.imread(str(p / "zidane.jpg"))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(service):
    server = inferd.serve("127.0.0.1:0", service)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"127.0.0.1:{server.server_address[1]}"


class FakeModels:
    """No torch: detect answers one box per frame; can run out of memory on batches of more than one request."""
    def __init__(self, info=None, oom_over=None):
        self.device, self.info = "fake", info or {"yolo": {"names": {"0": "person"}, "stride": 32}}
        self.det, self.reid, self.clip = {"yolo": {}}, None, None
        self.oom_over, self.calls = oom_over, []

    def detect(self, kind, frames, items):
        self.calls.append(len(items))
        if self.oom_over and len(items) > self.oom_over:
            raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")
        return [[np.array([[0.1, 0.2, 0.3, 0.4, 0.9, 0]], np.float32) for _ in it["orig_shapes"]] for it in items]

    def is_oom(self, e):
        return "out of memory" in str(e)

    def free(self):
        pass

    def memory(self):
        return None


# ---------------------------------------------------------------- pure parts

def test_wire_roundtrip():
    a, b = socket.socketpair()
    arrays = [np.arange(24, dtype=np.uint8).reshape(2, 3, 4), np.linspace(0, 1, 6, dtype=np.float32).reshape(3, 2),
              np.zeros((0, 6), np.float32)]
    wire.send(a, {"op": "x", "n": 3}, arrays)
    h, got = wire.recv(b)
    assert h == {"op": "x", "n": 3}
    assert all(np.array_equal(x, y) and x.dtype == y.dtype and x.shape == y.shape for x, y in zip(arrays, got))
    a.close()
    try:
        wire.recv(b)
        raise AssertionError("expected WireError")
    except wire.WireError:
        pass
    assert wire.parse_address("127.0.0.1:8770") == (socket.AF_INET, ("127.0.0.1", 8770))
    assert wire.parse_address("/run/axiom-infer/gpu1.sock")[1] == "/run/axiom-infer/gpu1.sock"


def test_letterbox_matches_ultralytics():
    from ultralytics.data.augment import LetterBox
    rng = np.random.default_rng(1)
    for h, w in ((1080, 1920), (1520, 2688), (480, 640), (720, 1280), (1000, 700), (1280, 1280), (301, 517)):
        img = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)
        for imgsz in (1280, 960):
            for auto in (True, False):
                ours = wire.letterbox(img, imgsz, 32, auto)
                theirs = LetterBox((imgsz, imgsz), auto=auto, stride=32)(image=img)
                assert ours.shape == theirs.shape and np.array_equal(ours, theirs), (h, w, imgsz, auto)


def _req(site, prio="verify", key=("detect", "yolo", 736, 1280), n=6):
    return inferd.Request("detect", site, prio, key, n, {}, [])


def test_scheduler_priority_round_robin_batching():
    s = inferd.Scheduler()
    a = [_req("A") for _ in range(4)]
    for r in a:
        s.put(r)
    b, c = _req("B"), _req("C", prio="index", key=("clip_images",), n=40)
    s.put(b)
    s.put(c)
    other = _req("D", key=("detect", "yolo", 1280, 1280))       # another input size: its own batch
    s.put(other)
    first = s.take(12)
    assert first == [a[0], b]                                    # A's oldest, then B in turn; 12 frames is full
    assert s.take(32) == [other]                                 # D's turn (A just went): its size runs alone
    assert s.take(32) == [a[1], a[2], a[3]]                      # then A's backlog fills a batch
    assert s.take(32) == [c]                                     # index waits until verify is empty
    assert s.take(32, timeout=0.01) == []
    # a new verify request jumps ahead of queued index work
    s.put(_req("E", prio="index", key=("clip_images",)))
    v = _req("F")
    s.put(v)
    assert s.take(32) == [v]
    # unbatchable (key None) requests run alone; a request bigger than the budget still runs
    t1, t2 = _req("G", key=None, n=1), _req("H", key=None, n=1)
    s.queues = {p: type(q)() for p, q in s.queues.items()}
    s.put(t1)
    s.put(t2)
    assert s.take(32) == [t1] and s.take(32) == [t2]
    big = _req("I", n=50)
    s.put(big)
    assert s.take(32) == [big]


def test_tokens():
    d = Path(tempfile.mkdtemp())
    f = d / "tokens.json"
    f.write_text(json.dumps({"tok-a": "ci_a"}))
    t = inferd.Tokens(f)
    assert t.site("tok-a") == "ci_a" and t.site("nope") is None and t.site(None) is None
    time.sleep(0.02)
    f.write_text(json.dumps({"tok-b": "ci_b"}))
    os.utime(f, (time.time() + 5, time.time() + 5))
    assert t.site("tok-b") == "ci_b" and t.site("tok-a") is None
    assert inferd.Tokens(None).site("x") == "x" and inferd.Tokens(None).site(None) == "anon"


# ---------------------------------------------------------------- the service over a socket (fake models)

def test_service_handshake_tokens_oom_split():
    d = Path(tempfile.mkdtemp())
    (d / "tokens.json").write_text(json.dumps({"good": "ci_good"}))
    models = FakeModels(oom_over=1)
    service = inferd.Service(models, inferd.Tokens(d / "tokens.json"))
    server, addr = _serve(service)
    try:
        bad = inference.Client([addr], "wrong", retry_s=0.2)
        try:
            bad.call({"op": "stats"})
            raise AssertionError("expected the token to be refused")
        except inference.InferUnavailable as e:
            assert "unknown token" in str(e) and bad.down_since is not None
        cl = inference.Client([addr], "good", retry_s=0.5)
        y = inference.RemoteYOLO("yolo", cl)
        assert y.names == {0: "person"} and y.ready
        img = np.zeros((720, 1280, 3), np.uint8)
        res = y.predict([img, img], imgsz=1280, conf=0.3, classes=[0])
        assert len(res) == 2 and np.allclose(res[0].boxes.xyxyn, [[0.1, 0.2, 0.3, 0.4]])
        assert res[0].boxes.cls.tolist() == [0.0] and abs(res[0].boxes.conf[0] - 0.9) < 1e-6 and res[0].orig_shape == (720, 1280)
        # two Sites' requests in one batch run out of memory: retried one request at a time, both answered
        req = [inferd.Request("detect", s, "verify", ("detect", "yolo", 720, 1280), 1,
                              {"n": 1, "conf": 0.25, "classes": None, "orig_shapes": [(720, 1280)], "model": "yolo"},
                              [np.zeros((1, 720, 1280, 3), np.uint8)]) for s in ("x", "y")]
        models.calls.clear()
        service._run(req)
        assert models.calls == [2, 1, 1] and all(r.result and not r.error for r in req) and service.oom == 1
        st = cl.call({"op": "stats"})[0]
        assert st["sites"]["ci_good"]["frames"] == 2 and st["oom"] == 1 and "verify" in st["latency"]
        assert cl.status()["up"] and cl.status()["calls"] >= 2
    finally:
        server.shutdown()
        server.server_close()
        service.stop()
    # other weights than this server's: refused at the handshake (the event waits, the alert says why)
    service = inferd.Service(FakeModels(info={"yolo": {"sha256": "0" * 64, "names": {}, "stride": 32}}), inferd.Tokens(None))
    server, addr = _serve(service)
    try:
        cl = inference.Client([addr], "t", retry_s=0.2)
        if (inference.ROOT / "models" / inference.settings.yolo_model).exists():
            try:
                cl.call({"op": "stats"})
                raise AssertionError("expected a weights mismatch")
            except inference.InferUnavailable as e:
                assert "weights differ" in str(e)
    finally:
        server.shutdown()
        server.server_close()
        service.stop()


def test_unreachable_fails_over_alerts_and_requeues():
    dead = f"127.0.0.1:{_free_port()}"
    service = inferd.Service(FakeModels(), inferd.Tokens(None))
    server, live = _serve(service)
    try:
        cl = inference.Client([dead, live], "t", retry_s=2)      # the first address is down: the second answers
        assert cl.call({"op": "stats"})[0]["ok"] and cl._next == 1
    finally:
        server.shutdown()
        server.server_close()
        service.stop()
    t = [1000.0]
    cl = inference.Client([dead], "t", retry_s=0.3, clock=lambda: t[0])
    t0 = time.monotonic()
    try:
        inference.RemoteYOLO("yolo", cl).predict([np.zeros((64, 64, 3), np.uint8)])
        raise AssertionError("expected InferUnavailable")
    except inference.InferUnavailable:
        assert time.monotonic() - t0 < 8                           # (Windows takes ~2 s to refuse a TCP connect)
    assert cl.down_since == 1000.0 and not cl.status()["up"] and not inference.RemoteYOLO("yolo", cl).ready
    assert cl.health_alert(now=1010.0) is None                   # not yet: a restart takes a few seconds
    a = cl.health_alert(now=1000.0 + inference.DOWN_ALERT_S + 1)
    assert a["kind"] == "inference_unavailable" and a["since"] == 1000.0
    assert inference.RemoteYOLO("yolo", cl).names[0] == "person"  # COCO names while the service is away

    # the verify worker: the event goes back in the queue (no error written), and comes round again
    from nvr import pipeline

    class P:
        def __init__(self):
            self.verify_q = asyncio.Queue()
            self.verify_done, self.published = 0, []

        async def _verify_retrying_oom(self, event_id):
            raise inference.InferUnavailable("down")

        def publish(self, event_id):
            self.published.append(event_id)

    async def go():
        p = P()
        old = inference.REQUEUE_S
        inference.REQUEUE_S = 0.05
        try:
            await pipeline.Pipeline._verify_one(p, 7)
            assert p.verify_q.empty() and p.published == [] and p.verify_done == 0
            await asyncio.sleep(0.15)
            assert p.verify_q.get_nowait() == 7
        finally:
            inference.REQUEUE_S = old
    asyncio.run(go())


def test_instance_on_the_service_never_loads_torch():
    code = """
import sys
from nvr import inference, ppe, verifier
import nvr.api
m, dev = verifier.load_yolo()
assert isinstance(m, inference.RemoteYOLO) and dev == "remote", (m, dev)
assert isinstance(ppe.Detector()._load(), inference.RemoteYOLO)
assert isinstance(inference.clip_model(), inference.RemoteClip)
assert isinstance(inference.reid_model(), inference.RemoteReID)
bad = [k for k in ("torch", "ultralytics", "open_clip", "torchvision") if k in sys.modules]
assert not bad, bad
print("clean")
"""
    env = {**os.environ, "NVR_INFER_SOCKET": f"127.0.0.1:{_free_port()}", "NVR_INFER_TOKEN": "t"}
    r = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "clean" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------- end to end: the real models, on the CPU

def test_end_to_end_matches_local_models():
    from ultralytics import YOLO

    from nvr.clip import Clip
    from nvr.config import ROOT, settings
    from nvr.reid import ReID
    from nvr.reidprep import person_crop
    if not (ROOT / "models" / settings.yolo_model).exists():
        print("  (no YOLO weights here: skipped)")
        return
    bus, zidane = _assets()
    models = inferd.Models("cpu", set(), {"yolo", "ppe", "reid", "clip"})
    service = inferd.Service(models, inferd.Tokens(None))
    server, addr = _serve(service)
    try:
        cl = inference.Client([addr], "e2e", retry_s=2)
        local = YOLO(str(ROOT / "models" / settings.yolo_model))
        remote = inference.RemoteYOLO("yolo", cl)
        assert remote.names == local.names
        cls = [0, 1, 2, 3, 5, 7]
        for frames in ([bus, bus], [bus, zidane, bus], [zidane]):          # one size (rectangle) and mixed (square)
            mine = remote.predict(frames, imgsz=1280, conf=0.25, classes=cls)
            ref = local.predict(frames, imgsz=1280, conf=0.25, classes=cls, device="cpu", verbose=False)
            for a, b in zip(mine, ref):
                assert len(a.boxes) == len(b.boxes) > 0, (len(a.boxes), len(b.boxes))
                assert np.array_equal(a.boxes.cls, b.boxes.cls.numpy())
                assert np.allclose(a.boxes.xyxyn, b.boxes.xyxyn.numpy(), atol=1e-4)
                assert np.allclose(a.boxes.conf, b.boxes.conf.numpy(), atol=1e-4)
        ppe_w = ROOT / "models" / settings.ppe_model
        if ppe_w.exists():
            lp, rp = YOLO(str(ppe_w)), inference.RemoteYOLO("ppe", cl)
            a = rp.predict([bus], imgsz=settings.ppe_imgsz, conf=0.15)[0]
            b = lp.predict([bus], imgsz=settings.ppe_imgsz, conf=0.15, device="cpu", verbose=False)[0]
            assert len(a.boxes) == len(b.boxes) and np.allclose(a.boxes.xyxyn, b.boxes.xyxyn.numpy(), atol=1e-4)
        people = [r for r in mine[0].boxes.xyxyn[mine[0].boxes.cls == 0]][:3]
        crops = [person_crop(zidane, p) for p in people] + [np.zeros((8, 8, 3), np.uint8)]   # a tiny one is dropped
        ra, la = inference.RemoteReID(cl).embed(crops), ReID("cpu").embed(crops)
        assert ra is not None and float(ra @ la) > 0.9999, float(ra @ la)
        assert inference.RemoteReID(cl).embed([np.zeros((8, 8, 3), np.uint8)]) is None
        lc, rc = Clip("cpu"), inference.RemoteClip(cl)
        imgs = [bus, zidane, bus[:300, :300]]
        a, b = rc.embed_images(imgs), lc.embed_images(imgs)
        assert a.shape == (3, 512) and np.allclose(a, b, atol=1e-4)
        assert rc.embed_images([]).shape == (0, 512)
        t1, t2 = rc.embed_text("a red bus"), lc.embed_text("a red bus")
        assert np.allclose(t1, t2, atol=1e-5)
        st = cl.call({"op": "stats"})[0]
        assert st["sites"]["e2e"]["frames"] >= 9 and st["latency"]["index"]["n"] >= 1
    finally:
        server.shutdown()
        server.server_close()
        service.stop()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
