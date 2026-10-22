"""Events keep no clip.mp4 (2026-10-22): verification, the weapon check and PPE read the camera's recording segments
directly (frames.iter_recording), retention's keep rules protect what the clips did, and nvr.clip_cleanup removes the
old clips the recording still covers. Real (tiny, generated) segments in throwaway data / recordings / runtime dirs.
Run: ..\\.venv\\Scripts\\python.exe tests\\test_no_clips.py   (from backend/), or under pytest."""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-noclips-data-")   # never the real DB
os.environ["NVR_RECORDINGS_DIR"] = tempfile.mkdtemp(prefix="nvr-noclips-rec-")
os.environ["NVR_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="nvr-noclips-run-")
os.environ["NVR_BACKUP_DIR"] = tempfile.mkdtemp(prefix="nvr-noclips-bak-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import av  # noqa: E402
import cv2  # noqa: E402
import numpy as np  # noqa: E402

from nvr import clip_cleanup, frames, keep, verifier, weaponcheck as wc  # noqa: E402
from nvr import pipeline as pl  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402
from nvr.fmp4 import segment_name  # noqa: E402

REC = settings.recordings_dir
DATA = settings.data_dir
CAM = {"id": "cam1", "name": "Yard", "host": "10.0.0.5", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": "p",
       "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": [], "retention_days": None, "scene_notes": "",
       "retention_policy": None, "policies": []}
db.upsert_camera(CAM)
for _cam in ("cc", "kf"):
    db.upsert_camera({**CAM, "id": _cam, "name": _cam})
FPS = 10
T0 = float(int(time.time()) - 6 * 3600)   # whole seconds: segment names keep microseconds, but stay simple


def frame_img(i: int, w=320, h=180, people=False, n=1) -> np.ndarray:
    """Frame i: its index as a gray level (frame time is readable from the pixels), and optionally two 'people'."""
    img = np.full((h, w, 3), gray(i), np.uint8)
    if people:
        x_red = int(40 + 200 * i / max(1, n - 1))
        x_blue = int(240 - 200 * i / max(1, n - 1))
        img[:] = 90
        cv2.rectangle(img, (x_red, 20), (x_red + 30, 160), (0, 0, 255), -1)
        cv2.rectangle(img, (x_blue, 25), (x_blue + 30, 165), (255, 0, 0), -1)
    return img


def make_segment(cam: str, start: float, seconds: float, gop: int = FPS, people=False) -> Path:
    """A recording segment as MediaMTX names it (start time in the file name)."""
    d = REC / cam
    d.mkdir(parents=True, exist_ok=True)
    path = d / segment_name(start)
    c = av.open(str(path), "w")
    s = c.add_stream("libx264", rate=FPS)
    s.width, s.height, s.pix_fmt = 320, 180, "yuv420p"
    s.options = {"g": str(gop), "keyint_min": str(gop), "sc_threshold": "0", "bf": "0", "qp": "0"}
    n = int(seconds * FPS)
    for i in range(n):
        for p in s.encode(av.VideoFrame.from_ndarray(frame_img(i, people=people, n=n), format="bgr24")):
            c.mux(p)
    for p in s.encode():
        c.mux(p)
    c.close()
    os.utime(path, (start + seconds, start + seconds))   # its last write: where retention and clip_cleanup end it
    return path


PERIOD = 14


def gray(i: int) -> int:
    return 16 + (i * 16) % 224   # 16 levels a frame: the YUV round trip moves a level by ~4


def at(img: np.ndarray) -> int:
    """The frame index a decoded frame shows (mod PERIOD)."""
    v = float(np.median(img))
    return min(range(PERIOD), key=lambda i: abs(gray(i) - v))


# ---------------------------------------------------------------- frames from the recording

def test_recording_frames_across_segments_with_seeks_and_gaps():
    make_segment("cam1", T0, 20)                # [T0, T0+20)
    make_segment("cam1", T0 + 30, 10)           # a 10 s gap, then [T0+30, T0+40)
    targets = [T0 + 1.0, T0 + 1.2, T0 + 1.5, T0 + 12.0, T0 + 25.0, T0 + 31.0, T0 + 39.5, T0 + 60.0, T0 - 5]
    got = frames.recording_frames("cam1", targets)
    assert set(got) == {T0 + 1.0, T0 + 1.2, T0 + 1.5, T0 + 12.0, T0 + 31.0, T0 + 39.5}, sorted(got)   # gap / after / before: none
    assert at(got[T0 + 1.0]) == 10 and at(got[T0 + 1.5]) == 1 and at(got[T0 + 12.0]) == 8   # the right frames
    assert at(got[T0 + 31.0]) == 10                                          # second segment's own clock
    assert got[T0 + 12.0].shape == (180, 320, 3)                             # full resolution
    order = [t for t, _ in frames.iter_recording("cam1", list(reversed(targets)))]
    assert order == sorted(order), "time order"


def test_long_gop_far_targets_do_not_seek_forever():
    make_segment("cam2", T0, 30, gop=200)       # one keyframe for the whole segment
    t0 = time.time()
    got = frames.recording_frames("cam2", [T0 + 2, T0 + 15, T0 + 28])
    assert set(got) == {T0 + 2, T0 + 15, T0 + 28} and at(got[T0 + 28]) == 0
    assert time.time() - t0 < 20


def test_grab_frames_reads_a_recording_source_or_a_clip():
    rec = verifier.grab_frames(frames.Recording("cam1"), T0 - 5, [T0 + 3.0, T0 + 4.0])
    assert {k: at(v) for k, v in rec.items()} == {T0 + 3.0: 2, T0 + 4.0: 12}
    clip = make_segment("tmpclip", T0, 5)
    old = verifier.grab_frames(clip, T0, [T0 + 2.0])   # a clip file: decoded from its start, as before
    assert at(old[T0 + 2.0]) in (5, 6)


def test_event_frames_prefers_the_recording_and_falls_back_to_a_legacy_clip():
    eid = db.create_event(camera_id="cam1", track_id="lf", camera_class="person", start_ts=T0 + 200, end_ts=T0 + 203,
                          status="verified", clip_start=T0 + 195)
    d = verifier.event_dir(eid)
    legacy = make_segment("tmpclip2", T0 + 195, 10)
    (d / "clip.mp4").write_bytes(legacy.read_bytes())
    db.update_event(eid, clip=f"events/{eid}/clip.mp4")
    got = verifier.event_frames(db.event(eid), [T0 + 200.0])   # nothing recorded at T0+200 on cam1: the clip
    assert at(got[T0 + 200.0]) in (7, 8)   # the clip: decoded from its own start (within a frame)
    got = verifier.event_frames({"camera_id": "cam1", "start_ts": T0 + 5, "clip": None}, [T0 + 5.0])
    assert at(got[T0 + 5.0]) == 8


# ---------------------------------------------------------------- the pipeline keeps no clip

class CapturingVerifier:
    model = None

    def __init__(self):
        self.sources = []

    def verify(self, e, src, clip_start, zl):
        self.sources.append((src, clip_start))
        return {"status": "rejected", "yolo_class": None, "yolo_conf": None, "yolo_hits": 0, "clip_start": clip_start,
                "detections": {"samples": []}}


def test_verify_reads_the_recording_and_stores_no_clip():
    p = pl.Pipeline()
    p.verifier = CapturingVerifier()
    p.cameras = {"cam1": CAM}
    eid = db.create_event(camera_id="cam1", track_id="v1", camera_class="person", camera_conf=0.9, start_ts=T0 + 5,
                          end_ts=T0 + 8, path=[[T0 + 5, 0.1, 0.1, 0.2, 0.5, 0.9]], status="pending")
    asyncio.run(p._verify(eid))
    (src, clip_start), = p.verifier.sources
    assert isinstance(src, frames.Recording) and src.camera_id == "cam1" and clip_start == T0 + 5 - settings.clip_pre_roll
    e = db.event(eid)
    assert e["status"] == "rejected" and e["clip"] is None and e["clip_start"] == clip_start
    assert not (DATA / "events" / str(eid) / "clip.mp4").exists()


def test_verify_without_recording_is_an_error_not_a_crash():
    class NoFrames(CapturingVerifier):
        def verify(self, e, src, clip_start, zl):
            return {"status": "error", "error": "no frames decoded from recording"}
    p = pl.Pipeline()
    p.verifier = NoFrames()
    p.cameras = {"cam1": CAM}
    eid = db.create_event(camera_id="cam1", track_id="v2", camera_class="person", camera_conf=0.9, start_ts=T0 + 5,
                          end_ts=T0 + 8, path=[[T0 + 5, 0.1, 0.1, 0.2, 0.5, 0.9]], status="pending")
    asyncio.run(p._verify(eid))
    e = db.event(eid)
    assert e["status"] == "error" and e["error"] == "no frames decoded from recording" and e["clip"] is None


# ---------------------------------------------------------------- the weapon check reads the recording

class _Boxes:
    def __init__(self, rows):
        a = np.array(rows, dtype=float).reshape(-1, 6)
        self.xyxyn, self.conf, self.cls = a[:, :4], a[:, 4], a[:, 5]


class _Res:
    def __init__(self, rows):
        self.boxes = _Boxes(rows)


def fake_yolo(imgs):
    out = []
    for img in imgs:
        h, w = img.shape[:2]
        rows = []
        for color in ((0, 0, 255), (255, 0, 0)):
            ys, xs = np.nonzero(cv2.inRange(img, np.array(color) - 40, np.array(color) + 40))
            if len(xs) > 20:
                rows.append([xs.min() / w, ys.min() / h, (xs.max() + 1) / w, (ys.max() + 1) / h, 0.9, 0])
        out.append(_Res(rows))
    return out


def test_weapon_evidence_from_the_recording():
    make_segment("cam3", T0, 12, people=True)
    e = {"id": 777, "camera_id": "cam3", "start_ts": T0 + 2, "end_ts": T0 + 8, "clip": None, "snapshot": None,
         "detections": {}}
    old = wc.MIN_PERSON_PX
    wc.MIN_PERSON_PX = 60   # the generated 'people' are 140 px tall on a 180 px frame
    try:
        ev = wc.gather_evidence(e, fake_yolo, deadline=time.time() + 60)
    finally:
        wc.MIN_PERSON_PX = old
    assert ev["evidence"] == "recording" and ev["frames"] >= 10 and len(ev["persons"]) == 2, ev
    # nothing recorded for the event: no video, the stored crops (no clip to fall back to)
    ev = wc.gather_evidence({**e, "camera_id": "nocam"}, fake_yolo, deadline=time.time() + 60)
    assert ev["evidence"] == "keyframes"


# ---------------------------------------------------------------- keep rules

def kept_reasons(**cols) -> list[str]:
    s = T0 - 30 * 86400
    eid = db.create_event(camera_id="cam1", track_id=f"k{time.time()}", camera_class=cols.pop("camera_class", "vehicle"),
                          start_ts=s, end_ts=s + 5, status=cols.pop("status", "verified"))
    if cols:
        db.update_event(eid, **cols)
    windows, _ = keep.keep_windows(CAM, s - 1, s + 6, keep.site_policy())
    db.execute("DELETE FROM events WHERE id=?", [eid])
    return sorted({r for w in windows for r in w["reasons"]})


def test_keep_rules_cover_what_clips_protected():
    assert kept_reasons() == []   # a plain verified vehicle outside detect-only zones
    assert kept_reasons(synopsis_json={"weapon_check": {"verdict": "confirmed"}}) == ["weapon:confirmed"]
    assert kept_reasons(synopsis_json={"weapon_check": {"verdict": "unclear"}}) == ["weapon:review"]
    assert kept_reasons(synopsis_json={"weapon_check": {"verdict": "not_confirmed"}}) == []
    assert kept_reasons(priority="high") == ["priority:high"]
    assert kept_reasons(priority="medium") == []
    assert kept_reasons(threat="high") == ["threat:high"]          # without a synopsis (qwen_analyzed doesn't apply)
    assert kept_reasons(policy={"kind": "towing", "text": "towed", "priority": "high"}) == ["site_rule"]
    assert kept_reasons(policy={"kind": "ppe", "text": "no hard hat", "priority": "medium"}) == ["ppe_violation", "site_rule"]
    assert kept_reasons(watched="White truck") == ["watched"]
    assert kept_reasons(detections={"ppe": {"verdict": "violation"}}) == ["ppe_violation"]
    assert kept_reasons(plate="ABC123") == ["plate"]
    # a false alarm keeps its feedback reason, not the alert reasons
    assert kept_reasons(priority="high", feedback={"verdict": "false_alarm"}) == ["feedback"]
    # scores: weapons above everything but locks; the window takes the highest
    assert keep.SCORES["weapon:confirmed"] > keep.SCORES["weapon:review"] > keep.SCORES["priority:high"] >= keep.SCORES["threat:high"]
    assert keep.SCORES["ppe_violation"] > keep.SCORES["person+synopsis"] and keep.SCORES["plate"] > keep.SCORES["person"]
    assert all(keep.SCORES[r] < keep.LOCKED_SCORE for r in keep.SCORES)


def test_keep_rules_can_be_switched_off():
    db.set_setting("retention_policy", {"keep": {"weapon_check": False, "plates": False}})
    try:
        assert kept_reasons(synopsis_json={"weapon_check": {"verdict": "confirmed"}}) == []
        assert kept_reasons(plate="ABC123") == []
        assert kept_reasons(priority="high") == ["priority:high"]
    finally:
        db.set_setting("retention_policy", {})


def test_default_policy_lists_the_new_rules():
    assert {"weapon_check", "high_priority", "ppe_violations", "plates"} <= set(keep.DEFAULT_POLICY["keep"])
    assert all(keep.site_policy()["keep"][k] for k in ("weapon_check", "high_priority", "ppe_violations", "plates"))


# ---------------------------------------------------------------- clip_cleanup

def legacy_event(cam: str, start: float, status="verified", clip=True, chat=True) -> int:
    eid = db.create_event(camera_id=cam, track_id=f"c{time.time()}", camera_class="person", start_ts=start,
                          end_ts=start + 4, status=status)
    d = DATA / "events" / str(eid)
    d.mkdir(parents=True, exist_ok=True)
    for name, data in (("snapshot.jpg", b"s" * 10), ("wide.jpg", b"w" * 10), ("crop_0.jpg", b"c" * 10), ("ppe.jpg", b"p" * 10),
                       ("thumb_320.jpg", b"t" * 10)):
        (d / name).write_bytes(data)
    if clip:
        (d / "clip.mp4").write_bytes(b"x" * 1000)
        db.update_event(eid, clip=f"events/{eid}/clip.mp4")
    if chat:
        (d / "chat_1000_640.jpg").write_bytes(b"q" * 100)
    return eid


def test_clip_cleanup_deletes_only_what_the_recording_covers():
    base = T0 + 3600
    make_segment("cc", base, 60)                  # [base, base+60)
    make_segment("cc", base + 60.5, 60)           # half a second later: one continuous span with the first
    make_segment("cc", base + 300, 30)            # [base+300, base+330)
    covered = legacy_event("cc", base + 50)       # span [45, 57]: across the first two segments
    partial = legacy_event("cc", base + 125)      # span [120, 132]: the recording ends at 120.5
    gone = legacy_event("cc", base + 200)         # nothing recorded
    pending = legacy_event("cc", base + 310, status="pending")
    no_clip_chat = legacy_event("cc", base + 315, clip=False)   # a cached chat frame only: covered, goes
    stale = db.create_event(camera_id="cc", track_id="stale", camera_class="person", start_ts=base + 320, end_ts=base + 321,
                            status="verified", clip="events/999999/clip.mp4")
    orphan = DATA / "events" / "999998"
    orphan.mkdir(parents=True, exist_ok=True)
    (orphan / "clip.mp4").write_bytes(b"o" * 50)

    dry = clip_cleanup.scan(db, camera="cc", apply=False)
    assert (dry.folders, dry.covered, dry.no_recording, dry.unfinished, dry.stale_rows) == (5, 2, 2, 1, 1), dry
    assert dry.covered_bytes == 1000 + 100 + 100 and dry.deleted == 0
    assert (DATA / "events" / str(covered) / "clip.mp4").exists(), "a dry run deletes nothing"
    text = clip_cleanup.render(dry, False)
    assert "recording covers the event (deletable):" in text and "dry run: nothing deleted" in text

    rep = clip_cleanup.scan(db, camera="cc", apply=True)
    assert rep.deleted == 2 and rep.freed == 1200 and rep.nulled == 2   # covered's clip + stale row
    d = DATA / "events" / str(covered)
    assert not (d / "clip.mp4").exists() and not (d / "chat_1000_640.jpg").exists()
    assert all((d / n).exists() for n in ("snapshot.jpg", "wide.jpg", "crop_0.jpg", "ppe.jpg", "thumb_320.jpg"))
    assert db.event(covered)["clip"] is None and db.event(stale)["clip"] is None
    assert not (DATA / "events" / str(no_clip_chat) / "chat_1000_640.jpg").exists()
    for kept in (partial, gone, pending):
        assert (DATA / "events" / str(kept) / "clip.mp4").exists() and db.event(kept)["clip"] is not None
    assert (orphan / "clip.mp4").exists() and rep.orphans >= 1
    assert "deleted the media of 2 events" in clip_cleanup.render(rep, True)
    again = clip_cleanup.scan(db, camera="cc", apply=True)
    assert again.covered == 0 and again.deleted == 0


def test_clip_cleanup_counts_kept_footage_as_recording():
    base = T0 + 7200
    seg = make_segment("kf", base, 20)
    db.execute("INSERT INTO kept_footage (camera_id, start_ts, end_ts, file, reasons, score, bytes, created_at) "
               "VALUES ('kf', ?, ?, ?, '[\"person\"]', 50, 1, ?)", [base, base + 20, str(seg), time.time()])
    os.utime(seg, (time.time(), time.time()))   # trimmed long after: its mtime says nothing about its end
    eid = legacy_event("kf", base + 8)
    later = legacy_event("kf", base + 40)       # after the kept file's end: not covered, though the mtime is later
    rep = clip_cleanup.scan(db, camera="kf", apply=True)
    assert rep.deleted == 1 and db.event(eid)["clip"] is None and db.event(later)["clip"] is not None


def test_clip_cleanup_cli_dry_run_reads_only():
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = clip_cleanup.main(["--camera", "cc"])
    assert code == 0 and "dry run" in buf.getvalue()


def test_clip_cleanup_merge_and_cover():
    spans = clip_cleanup.merge_intervals([(0, 10), (10.5, 20), (30, 40)])
    assert spans == [(0, 20), (30, 40)]
    assert clip_cleanup.covers(spans, 2, 18) and not clip_cleanup.covers(spans, 15, 32) and clip_cleanup.covers(spans, 31, 41)
    assert not clip_cleanup.covers([], 0, 1)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
