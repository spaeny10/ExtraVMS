"""Ask about this clip: chat frames come from the event's clip where it covers them, else from the recording (retention
trims a kept event's clip.mp4; the event viewer plays the recording around the event, so `at` can lie outside the clip).
Throwaway data, recordings and runtime dirs; the recording is a stub of frames.preview_jpeg.
Run: ..\\.venv\\Scripts\\python.exe tests\\test_chat_frames.py   (from backend/)"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-chatframes-test-")  # never the real DB
os.environ["NVR_RECORDINGS_DIR"] = tempfile.mkdtemp(prefix="nvr-chatframes-rec-")
os.environ["NVR_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="nvr-chatframes-run-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from nvr import api, mediamtx  # noqa: E402
from nvr import pipeline as pl  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402
from nvr.pipeline import Pipeline  # noqa: E402

mediamtx.recording_spans = lambda *a, **k: []
db.upsert_camera({"id": "cam1", "name": "Yard", "host": "10.0.0.5", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": "p",
                  "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": [], "retention_days": None, "scene_notes": "", "retention_policy": None, "policies": []})
START = time.time() - 3 * 86400
PRE = settings.clip_pre_roll


def jpeg(color: int) -> bytes:
    return cv2.imencode(".jpg", np.full((360, 640, 3), color, np.uint8))[1].tobytes()


class Recording:
    """frames.preview_jpeg stub: footage only inside [lo, hi] (epoch); remembers the times asked for."""
    def __init__(self, lo: float, hi: float):
        self.lo, self.hi, self.asked = lo, hi, []

    def __call__(self, cam, t, width=960, exact=False):
        self.asked.append(t)
        return (jpeg(200), t, False) if self.lo <= t <= self.hi else None


def make_event(clip: bool) -> dict:
    eid = db.create_event(camera_id="cam1", track_id=str(time.time()), camera_class="person", camera_conf=0.9, start_ts=START,
                          end_ts=START + 20, path=[[START, 0.3, 0.3, 0.4, 0.6, 0.9]], status="verified")
    if clip:
        d = Path(os.environ["NVR_DATA_DIR"]) / "events" / str(eid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "clip.mp4").write_bytes(b"stub")   # grab_frames is stubbed: only its existence matters
        db.update_event(eid, clip=f"events/{eid}/clip.mp4")
    return db.event(eid)


def run(e, at, rec, clip_frames=None):
    old = (pl.frames.preview_jpeg, pl.grab_frames)
    pl.frames.preview_jpeg = rec
    asked_clip = []

    def grab(path, clip_start, targets):
        asked_clip.extend(targets)
        return {t: np.full((360, 640, 3), 50, np.uint8) for t in targets if clip_frames is None or clip_frames(t)}
    pl.grab_frames = grab
    try:
        got = asyncio.run(Pipeline().chat_frames(e, at))
    finally:
        pl.frames.preview_jpeg, pl.grab_frames = old
    return got, asked_clip


def test_no_clip_reads_the_recording():
    e = make_event(clip=False)
    rec = Recording(START - 60, START + 60)
    got, asked_clip = run(e, None, rec)
    assert len(got) == settings.chat_frames and not asked_clip
    # clip time counts from clip_start (start - pre-roll), so the overview runs from the event start to its end
    assert [t for t, _, _ in got][0] == PRE and [t for t, _, _ in got][-1] == PRE + 20
    assert rec.asked[0] == START and abs(rec.asked[-1] - (START + 20)) < 1e-6
    assert all(name.startswith("chat_") and data[:2] == b"\xff\xd8" for _, data, name in got)


def test_moment_before_the_clip_comes_from_the_recording():
    e = make_event(clip=True)
    rec = Recording(START - 60, START + 60)
    got, asked_clip = run(e, -6.0, rec)   # the viewer's playhead 6 s before clip time 0 (11 s before the event)
    times = [t for t, _, _ in got]
    assert min(times) < 0 and not asked_clip   # every moment asked for lies before the clip
    assert any(abs(t - (START - PRE - 6.0)) < 1e-6 for t in rec.asked)


def test_inside_the_clip_uses_the_clip_only():
    e = make_event(clip=True)
    rec = Recording(START - 60, START + 60)
    got, asked_clip = run(e, 10.0, rec)
    assert len(got) == settings.chat_frames and not rec.asked and asked_clip


def test_outside_both_falls_back_to_the_clip_edge():
    e = make_event(clip=True)
    rec = Recording(0, 1)   # nothing recorded around the event any more
    got, _ = run(e, -6.0, rec)
    assert len(got) == settings.chat_frames   # the clip's first frame, labelled with the time asked for, as before


def test_nothing_at_all_returns_no_frames():
    e = make_event(clip=False)
    got, _ = run(e, None, Recording(0, 1))
    assert got == []


def test_event_detail_names_clip_start():
    api.state.pipeline = Pipeline()
    e = make_event(clip=False)
    out = asyncio.run(api.get_event(e["id"]))
    assert out["clip_start"] == START - PRE
    db.update_event(e["id"], clip_start=START - 2)
    assert asyncio.run(api.get_event(e["id"]))["clip_start"] == START - 2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
