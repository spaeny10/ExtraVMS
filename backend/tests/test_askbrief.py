"""The situation brief for a clip Ask (askbrief.py), built around event 377 (2026-11-05): a person crosses a dark
basement a minute after the switch feeding other cameras lost power, and comes back later with the lights on. The brief
says the clip is black-and-white and the later events are in color, which cameras stopped or came back and when
(from the recordings index), and the camera's other events in the hour; chat_stream puts it in front of the model.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_askbrief.py   (from backend/)
"""
import asyncio
import datetime as dt
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = DATA = tempfile.mkdtemp(prefix="nvr-askbrief-test-")
os.environ["NVR_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="nvr-askbrief-runtime-")
os.environ["NVR_RECORDINGS_DIR"] = tempfile.mkdtemp(prefix="nvr-askbrief-rec-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from nvr import askbrief, recindex, synopsis, vlmroute  # noqa: E402
from nvr.db import db  # noqa: E402

CAM = {"host": "127.0.0.1", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": "", "main_path": "/m",
       "sub_path": "/s", "enabled": 1, "zones": [], "retention_days": None, "scene_notes": "", "retention_policy": None}
NOW = float(int(time.time()))
T = NOW - 1800                      # the event: half an hour ago, so the hour after it runs up to now
M = 60.0


def clock(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).astimezone().strftime("%H:%M")


def picture(path: Path, gray: bool) -> None:
    rng = np.random.default_rng(1)
    if gray:
        g = rng.integers(20, 200, size=(360, 640), dtype=np.uint8)
        img = cv2.merge([g, g, g])
        cv2.rectangle(img, (200, 100), (330, 300), (0, 200, 0), 2)    # the detection box drawn in color
    else:
        img = rng.integers(0, 255, size=(360, 640, 3), dtype=np.uint8)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)


def add(cam: str, ts: float, gray: bool | None = None, **kw) -> dict:
    f = {"camera_id": cam, "track_id": "t", "camera_class": "person", "start_ts": ts, "end_ts": ts + 6, "status": "verified",
         "created_at": ts, "yolo_conf": 0.9, "yolo_class": "person", "synopsis": None, **kw}
    eid = db.execute_insert(f"INSERT INTO events ({','.join(f)}) VALUES ({','.join('?' * len(f))})", list(f.values()))
    if gray is not None:
        rel = f"events/{eid}/snapshot.jpg"
        picture(Path(DATA) / rel, gray)
        db.execute("UPDATE events SET snapshot=? WHERE id=?", [rel, eid])
    return db.one("SELECT * FROM events WHERE id=?", [eid])


def record(cam: str, *spans: tuple[float, float]) -> None:
    """Index rows for `cam`: one-minute files over each span."""
    rows = []
    for s, e in spans:
        t = s
        while t < e:
            rows.append((cam, f"{cam}-{t:.0f}.mp4", t, min(t + 60, e), 1000, t))
            t += 60
    with db.lock:
        db.conn.executemany("INSERT INTO rec_segments (camera_id, name, start_ts, end_ts, size, mtime) VALUES (?,?,?,?,?,?)", rows)
        db.conn.commit()
    recindex._ready.add(cam)


ev: dict = {}


def setup_module(_=None):
    for cid, name in [("cam1", "Test Cam Central"), ("cam2", "Thermal – Visible"), ("cam3", "Yard"), ("cam4", "Gate")]:
        db.upsert_camera({"id": cid, "name": name, **CAM})
    record("cam1", (T - 2 * 3600, NOW))                     # this camera: recording all along
    record("cam2", (T - 2 * 3600, T - 1 * M))               # switch gone a minute before the event, not back
    # cam3: nothing at all; cam4: a 10-minute gap after the event, and a 30 s blip that isn't an outage
    record("cam4", (T - 2 * 3600, T + 5 * M), (T + 15 * M, T + 20 * M), (T + 20.5 * M, NOW))
    ev["this"] = add("cam1", T, gray=True, synopsis="A person in a light hooded top walks toward the camera.")
    ev["box"] = add("cam1", T + 28 * M, gray=False, synopsis="A male in a tan hooded sweatshirt walks toward the camera "
                    "carrying a rectangular object in his right hand. " + "The room is lit. " * 30)
    ev["cable"] = add("cam1", T + 29 * M, gray=False, synopsis="A male carries a large yellow spool of cable.")
    ev["earlier"] = add("cam1", T - 50 * M, gray=True)                         # no synopsis yet
    ev["rejected"] = add("cam1", T + 3 * M, status="rejected", synopsis="not a person")
    ev["masked"] = add("cam1", T + 4 * M, status="masked", synopsis="hidden")
    ev["other_cam"] = add("cam4", T + 2 * M, synopsis="someone at the gate")
    ev["too_old"] = add("cam1", T - 3 * 3600, synopsis="hours before")


def brief(**kw) -> str:
    return asyncio.run(askbrief.build(ev["this"], now=NOW, **kw))


def test_is_gray_tells_night_mode_from_color():
    p, q = Path(DATA) / "g.jpg", Path(DATA) / "c.jpg"
    picture(p, True)
    picture(q, False)
    assert askbrief.is_gray(p) is True             # the colored detection box doesn't make it a color picture
    assert askbrief.is_gray(q) is False
    assert askbrief.is_gray(Path(DATA) / "missing.jpg") is None


def test_recording_lines():
    t = T
    lo, hi = t - 3600, t + 3600
    def lines(spans, now=hi):
        return askbrief.recording_lines([(t + a, t + b) for a, b in spans], lo, hi, t, now)
    assert lines([(-3600, 3600)]) == []                                           # recorded all hour: nothing to say
    assert lines([(-3600, -60)]) == [f"stopped recording at {clock(t - 60)} (1 min before this event) and has not recorded since"]
    assert lines([(-3600, -600), (300, 3600)], now=hi + 9999) == [
        f"stopped recording at {clock(t - 600)} (10 min before this event) and started again at {clock(t + 300)} (5 min after this event)"]
    assert lines([(420, 3600)]) == [f"was not recording until {clock(t + 420)} (7 min after this event)"]
    assert lines([(-3600, -600), (-540, 3600)]) == []                             # a minute's gap isn't an outage
    assert lines([(-3600, 1380)], now=hi + 9999) == [                             # the window ended before now
        f"stopped recording at {clock(t + 1380)} (23 min after this event) and had not recorded again by {clock(hi)}"]
    assert lines([]) == ["recorded nothing in this hour"]


def test_the_brief_for_event_377():
    b = brief()
    print(b)
    assert b.startswith("Situation brief")
    # the picture: dark here, lit in the later events
    assert "black-and-white" in b and "night (infrared)" in b
    assert f"events at {clock(T + 28 * M)} and {clock(T + 29 * M)} are in color" in b
    # recording: the camera that went with the switch, the dead one, the one that came back; not the blip, not cam1
    assert f"Thermal – Visible stopped recording at {clock(T - M)} (1 min before this event) and has not recorded since" in b
    assert "Yard recorded nothing in this hour" in b
    assert f"Gate stopped recording at {clock(T + 5 * M)} (5 min after this event) and started again at {clock(T + 15 * M)}" in b
    assert "This camera" not in b.split("- This camera, before")[0].split("Recording on this server")[1]
    # this camera's events in the hour, nearest first; rejected, masked, other cameras' and older ones left out
    after = b.split("after this event (nearest first):")[1]
    assert after.index("rectangular object") < after.index("yellow spool")
    assert "..." in after and "The room is lit. " * 20 not in after                # long synopses are cut
    assert "(no description yet)" in b.split("before this event (nearest first):")[1]
    for gone in ("not a person", "hidden", "someone at the gate", "hours before"):
        assert gone not in b, gone


def test_without_the_event_list():
    b = brief(events=False)
    assert "black-and-white" in b and "Thermal – Visible" in b
    assert "rectangular object" not in b and "nearest first" not in b


def test_nothing_to_say_is_none_and_errors_never_raise():
    quiet = add("cam1", NOW - 10 * 86400)              # long ago: no events, no recording, no snapshot
    db.execute("UPDATE cameras SET enabled=0 WHERE id IN ('cam2', 'cam3', 'cam4')")
    try:
        assert asyncio.run(askbrief.build(quiet, events=False, now=NOW)) == "Situation brief (this server, the hour " \
            "either side of the event; local times):\n- Recording on this server:\n  This camera recorded nothing in this hour"
        assert asyncio.run(askbrief.build({"id": 1}, now=NOW)) is None      # a broken event: logged, no brief
    finally:
        db.execute("UPDATE cameras SET enabled=1")


def test_chat_stream_puts_the_brief_before_the_question():
    seen = {}

    async def fake_stream(task, messages, num_predict, temperature, priority):
        seen["messages"] = messages
        yield "delta", "ok"

    real = vlmroute.router.stream
    vlmroute.router.stream = fake_stream
    try:
        async def go():
            return [c async for c in synopsis.chat_stream(dict(ev["this"], path=[], rules=[]), {"name": "Test Cam Central"}, [],
                                                          [], "Is anything suspicious here?", brief="Situation brief: X")]
        assert asyncio.run(go()) == ["ok"]
    finally:
        vlmroute.router.stream = real
    msgs = seen["messages"]
    assert "Check before you agree" in msgs[0]["content"] and "never name the camera's analytics rules" in msgs[0]["content"].lower()
    assert msgs[1]["content"].endswith("Situation brief: X")
    assert msgs[-1]["content"] == "Is anything suspicious here?"


if __name__ == "__main__":
    setup_module()
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
