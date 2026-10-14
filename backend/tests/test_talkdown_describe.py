"""Talk-down "Describe & warn" (talkdown_describe.py, POST /api/speakers/<id>/describe): Qwen's answer (a fake VLM)
for one person, three people and nobody; sensitive words filtered out with the position-only and generic fallbacks;
Qwen down or too slow; no picture; template filling; the rate limit; operator-only through the tunnel; it never plays;
and the play of the reviewed text returns and logs the exact words.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_talkdown_describe.py   (from backend/)
"""
import asyncio
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("NVR_ALLOWED_HOSTS", "site")  # the test client's Host (lan_guard Host allow-list)
for _var in ("NVR_DATA_DIR", "NVR_RECORDINGS_DIR", "NVR_RUNTIME_DIR"):
    os.environ[_var] = tempfile.mkdtemp(prefix="nvr-describe-test-")   # never the real DB or recordings
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from nvr import hub_agent, talkdown, talkdown_describe as td, vlmroute  # noqa: E402
from nvr.api import app  # noqa: E402
from nvr.db import db  # noqa: E402

LAN = ("192.168.1.9", 5000)
JPEG = b"\xff\xd8fake-jpeg\xff\xd9"
CAM = {"id": "cam1", "name": "Back Wall", "host": "10.0.0.5", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": "cam-pw-" + "q" * 6,
       "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": [], "retention_days": None, "scene_notes": "",
       "retention_policy": None, "synopsis_labels": ["person"], "policies": []}
LEAVE = "this is private property and you are being recorded. Leave now."


def call(method, path, body=None, role=None, user="op@example"):
    async def go():
        tunnel = role is not None
        transport = httpx.ASGITransport(app=hub_agent.as_tunnel(app) if tunnel else app, client=hub_agent.IN_PROCESS_CLIENT if tunnel else LAN)
        headers = {"x-hub-role": role, "x-hub-user": user} if tunnel else {}
        async with httpx.AsyncClient(transport=transport, base_url="http://site") as c:
            return await c.request(method, path, json=body, headers=headers)
    return asyncio.run(go())


class FakeVlm:
    """Stands in for vlmroute.router.chat_json: answers `answer` (a dict, an exception to raise, or a delay in seconds)."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    async def __call__(self, task, system, text, images, schema, num_predict=300, temperature=0.1, priority="background"):
        self.calls.append({"task": task, "system": system, "text": text, "images": images, "schema": schema, "priority": priority})
        if isinstance(self.answer, Exception):
            raise self.answer
        if isinstance(self.answer, (int, float)):
            await asyncio.sleep(self.answer)
            return {}
        return {**self.answer, "_model": "qwen-fake"}


class Patched:
    """The fake VLM and a fake current picture for the duration of a block."""

    def __init__(self, answer, frame=None):
        self.vlm = FakeVlm(answer)
        self.frame = frame if frame is not None else (lambda cam: (JPEG, 1_700_000_000.5))

    def __enter__(self):
        self._orig = (vlmroute.router.chat_json, td.live_frame)
        vlmroute.router.chat_json, td.live_frame = self.vlm, self.frame
        td._last.clear()
        return self.vlm

    def __exit__(self, *exc):
        vlmroute.router.chat_json, td.live_frame = self._orig
        td._last.clear()


def setup():
    db.upsert_camera(CAM)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def speaker(sid="spk1"):
    # nothing listens on this port: a play fails at once, nothing is ever contacted
    return {"id": sid, "name": f"Speaker {sid}", "host": "127.0.0.1", "rtsp_port": free_port(), "onvif_port": 80, "path": "/main",
            "username": "admin", "password": "spk-" + "z" * 8, "codec": "PCMU", "volume_note": "", "camera_ids": ["cam1"], "enabled": True}


# ---------------------------------------------------------------- the guardrails, on their own

def test_one_person_becomes_an_address():
    j = td.judge({"people": 1, "description": "in the red shirt", "position": "by the white truck", "confidence": "high"})
    assert j["people"] == 1 and j["phrase"] == "You in the red shirt by the white truck" and not j["filtered"]
    assert td.fill("leave", j["phrase"]) == f"You in the red shirt by the white truck: {LEAVE}"
    # tidied into "You in ...": a bare clothing phrase, a leading "the person", over-long text
    assert td.clean_description("Red hoodie, carrying a ladder.") == ("in the red hoodie, carrying a ladder", False)
    assert td.clean_description("the person wearing a hi-vis vest") == ("wearing a hi-vis vest", False)
    assert len(td.clean_description("in " + "very " * 20 + "red shirt")[0].split()) == td.DESC_WORDS
    assert td.clean_position("the gate") == ("by the gate", False)
    assert td.clean_position("next to the white truck.") == ("next to the white truck", False)


def test_sensitive_words_are_never_kept():
    for bad in ("the old man in a red shirt", "a young woman with a backpack", "tall person in a black jacket", "in a red shirt, dark-skinned",
                "the heavyset guy carrying a bag", "in a grey hoodie and his blue jeans", "a teenager on a bicycle", "Asian person with a cart",
                "in a black jacket, middle-aged", "bald, in a white t-shirt", "white person in a blue coat", "a girl with a pink bag"):
        d, filtered = td.clean_description(bad)
        assert filtered, bad
        assert not td.sensitive(d), (bad, d)
    # the clothing survives when only the sensitive part is dropped
    assert td.clean_description("the old man in a red shirt") == ("in a red shirt", True)
    assert td.clean_description("in a red shirt, dark-skinned") == ("in a red shirt", True)
    assert td.clean_description("in a grey hoodie and his blue jeans") == ("in a grey hoodie", True)
    # colours of clothes and vehicles are fine; race words are not
    assert td.clean_description("in a white t-shirt and black cap") == ("in a white t-shirt and black cap", False)
    assert td.clean_position("by the white truck") == ("by the white truck", False)
    assert td.clean_position("next to the woman") == ("", True)
    # nothing useful left: the position alone
    j = td.judge({"people": 1, "description": "elderly man, thin", "position": "at the gate", "confidence": "medium"})
    assert j["phrase"] == "You at the gate" and j["filtered"] and j["options"][0]["description"] == ""
    # ...and with no position either: no address, the generic message
    j = td.judge({"people": 1, "description": "young woman", "position": "", "confidence": "medium"})
    assert j["phrase"] is None and j["options"] == [] and j["filtered"]
    assert td.fill("leave", None) == "This is private property and you are being recorded. Leave now."


def test_several_people_and_nobody():
    j = td.judge({"people": 3, "description": "in the red shirt", "position": "by the white truck", "group_position": "the gate", "confidence": "high",
                  "persons": [{"description": "in the red shirt", "position": "by the white truck"},
                              {"description": "with the backpack", "position": "at the gate"},
                              {"description": "the man in a cap", "position": "near the fence"},
                              {"description": "in green", "position": "on the road"}]})
    assert j["people"] == 3 and j["phrase"] == j["group_phrase"] == "You by the gate"
    assert [o["phrase"] for o in j["options"]] == ["You in the red shirt by the white truck", "You with the backpack at the gate",
                                                    "You in a cap near the fence"]   # at most 3, the gendered word dropped
    assert j["filtered"]
    j = td.judge({"people": 2, "description": "in blue", "position": "by the van", "group_position": "", "confidence": "low"})
    assert j["phrase"] == "You there" and j["options"][0]["phrase"] == "You in blue by the van"
    j = td.judge({"people": 0, "description": "", "position": "", "confidence": "high"})
    assert j == {"people": 0, "options": [], "phrase": None, "group_phrase": None, "confidence": "high", "filtered": False}
    try:
        td.judge({"description": "in red"})
        raise AssertionError("an answer without a count is not usable")
    except ValueError:
        pass


def test_templates():
    assert [t[0] for t in td.TEMPLATES] == ["leave", "police", "monitored", "custom"]
    assert td.fill("police", "You by the gate") == "You by the gate: the police have been called. Leave the property immediately."
    assert td.fill("monitored", None) == "This site is monitored by security and you are being recorded. Please leave the area."
    assert td.fill("custom", "You with the ladder") == "You with the ladder: "
    assert td.fill("custom", None) == ""
    assert td.fill("nonsense", None) == td.fill("leave", None)


# ---------------------------------------------------------------- describe(): the picture and the fake VLM

def test_describe_one_person_with_the_prompt_and_picture():
    with Patched({"people": 1, "description": "in the red shirt", "position": "by the white truck", "confidence": "high"}) as vlm:
        r = asyncio.run(td.describe("cam1", "leave"))
    assert r["people"] == 1 and r["error"] is None and r["model"] == "qwen-fake" and r["frame_ts"] == 1_700_000_000.5
    assert r["text"] == f"You in the red shirt by the white truck: {LEAVE}" and r["template"] == "leave"
    assert r["options"] == [{"description": "in the red shirt", "position": "by the white truck", "phrase": "You in the red shirt by the white truck"}]
    assert [t["id"] for t in r["templates"]] == ["leave", "police", "monitored", "custom"]
    c = vlm.calls[0]
    assert c["images"] == [JPEG] and c["priority"] == "chat" and c["schema"]["required"] == ["people", "description", "position", "confidence"]
    for never in ("race", "ethnicity", "skin", "age", "body size", "gender", "disability", "never a name"):
        assert never in c["system"], never


def test_describe_three_people_and_nobody():
    persons = [{"description": "in the red shirt", "position": "by the white truck"}, {"description": "with the backpack", "position": "at the gate"},
               {"description": "in a yellow vest", "position": "on the loading dock"}]
    with Patched({"people": 3, "description": "in the red shirt", "position": "by the white truck", "group_position": "by the gate",
                  "persons": persons, "confidence": "medium"}):
        r = asyncio.run(td.describe("cam1", "police"))
    assert r["people"] == 3 and len(r["options"]) == 3 and r["group_phrase"] == "You by the gate"
    assert r["text"] == "You by the gate: the police have been called. Leave the property immediately."
    with Patched({"people": 0, "description": "", "position": "", "confidence": "high"}):
        r = asyncio.run(td.describe("cam1", "leave"))
    assert r["people"] == 0 and r["options"] == [] and r["error"] is None
    assert r["text"] == "This is private property and you are being recorded. Leave now."


def test_describe_sensitive_answer_falls_back():
    with Patched({"people": 1, "description": "an elderly heavyset man", "position": "beside the old shed", "confidence": "high"}):
        r = asyncio.run(td.describe("cam1", "leave"))
    assert r["people"] == 1 and r["filtered"] and r["options"] == [] and r["phrase"] is None
    assert r["text"] == "This is private property and you are being recorded. Leave now."
    with Patched({"people": 1, "description": "young guy, tall", "position": "by the blue car", "confidence": "high"}):
        r = asyncio.run(td.describe("cam1", "leave"))
    assert r["text"] == f"You by the blue car: {LEAVE}" and r["filtered"]
    for word in ("young", "guy", "tall", "man", "elderly"):
        assert word not in r["text"].lower().split()


def test_describe_qwen_down_slow_or_no_picture_gives_the_generic_message():
    generic = "This is private property and you are being recorded. Leave now."
    with Patched(RuntimeError("no model for talkdown: local Qwen is disabled and the remote is not configured")):
        r = asyncio.run(td.describe("cam1"))
    assert r["people"] is None and r["error"] == "Qwen is unavailable right now." and r["text"] == generic and r["frame_ts"]
    orig = td.TOTAL_S
    td.TOTAL_S = 1.0
    try:
        with Patched(5):   # Qwen answers after 5 s: past the (shortened) limit
            t0 = time.monotonic()
            r = asyncio.run(td.describe("cam1"))
        assert time.monotonic() - t0 < 3 and "did not answer" in r["error"] and r["text"] == generic
    finally:
        td.TOTAL_S = orig
    with Patched({"description": "in red"}):   # no count of people
        r = asyncio.run(td.describe("cam1"))
    assert r["people"] is None and "count" in r["error"] and r["text"] == generic

    def no_frame(cam):
        raise td.FrameError("no keyframe from the live stream in time")
    with Patched({"people": 1, "description": "in red", "position": "", "confidence": "high"}, frame=no_frame) as vlm:
        r = asyncio.run(td.describe("cam1"))
    assert r["people"] is None and r["error"].startswith("No current picture") and r["text"] == generic and r["frame_ts"] is None
    assert vlm.calls == []   # Qwen is not asked without a picture

    def broken(cam):
        raise OSError("disk gone")
    with Patched({}, frame=broken):
        r = asyncio.run(td.describe("cam1"))
    assert r["error"] == "No current picture from this camera." and r["text"] == generic


def test_live_frame_prefers_the_recording_then_the_stream():
    orig = (td.frames.preview_jpeg, td.stream_frame)
    try:
        now = time.time()
        td.frames.preview_jpeg = lambda cam, t, w, exact: (b"rec", now - 1, True)
        td.stream_frame = lambda cam: (_ for _ in ()).throw(AssertionError("the recording was fresh"))
        assert td.live_frame("cam1") == (b"rec", now - 1)
        td.frames.preview_jpeg = lambda cam, t, w, exact: (b"old", now - 60, True)   # the recording stalled: the live stream
        td.stream_frame = lambda cam: (b"live", now)
        assert td.live_frame("cam1") == (b"live", now)

        def fails(cam):
            raise OSError("Input/output error: 'rtsp://nvr:secret-pass@127.0.0.1:8554/cam1'")
        td.frames.preview_jpeg = lambda cam, t, w, exact: None
        td.stream_frame = fails
        try:
            td.live_frame("cam1")
            raise AssertionError("no picture must raise FrameError")
        except td.FrameError as e:
            assert "secret-pass" not in str(e) and "nvr:" not in str(e)
    finally:
        td.frames.preview_jpeg, td.stream_frame = orig


# ---------------------------------------------------------------- the route

def test_route_roles_rate_limit_and_it_never_plays():
    setup()
    db.upsert_speaker(speaker("spk_dw1"))
    plays = db.one("SELECT COUNT(*) AS n FROM talkdown_log")["n"]
    body = {"camera_id": "cam1", "template": "leave"}
    with Patched({"people": 1, "description": "in the red shirt", "position": "by the white truck", "confidence": "high"}):
        assert call("POST", "/api/speakers/spk_dw1/describe", body, role="viewer").status_code == 403
        r = call("POST", "/api/speakers/spk_dw1/describe", body, role="operator")
        assert r.status_code == 200, r.text
        assert r.json()["text"] == f"You in the red shirt by the white truck: {LEAVE}"
        # one per speaker and camera every RATE_S
        r = call("POST", "/api/speakers/spk_dw1/describe", body, role="operator")
        assert r.status_code == 429 and "every 3 s" in r.text
        td._last.clear()
        assert call("POST", "/api/speakers/spk_dw1/describe", {"camera_id": "cam1"}, role="admin").status_code == 200   # template defaults to leave
        td._last.clear()
        assert call("POST", "/api/speakers/spk_dw1/describe", body).status_code == 200    # the server's own page
        td._last.clear()
        assert call("POST", "/api/speakers/spk_dw1/describe", {"camera_id": "cam9"}, role="operator").status_code == 422   # not on this server
        assert call("POST", "/api/speakers/spk_dw1/describe", {"camera_id": "cam1", "template": "shout"}, role="operator").status_code == 422
        assert call("POST", "/api/speakers/spk_dw1/describe", {"camera_id": "../x"}, role="operator").status_code == 422
        # a speaker on another server (a hub camera group): the camera's server still describes its camera
        assert call("POST", "/api/speakers/spk_other/describe", body, role="operator").status_code == 200
    assert db.one("SELECT COUNT(*) AS n FROM talkdown_log")["n"] == plays and not talkdown.manager.busy("spk_dw1")   # nothing played


def test_rate_limit_clock():
    td._last.clear()
    assert not td.rate_limited("spk1", "cam1", now=100.0)
    assert td.rate_limited("spk1", "cam1", now=102.9)
    assert not td.rate_limited("spk1", "cam2", now=102.9)   # another camera
    assert not td.rate_limited("spk_dw2", "cam1", now=102.9)   # another speaker
    assert not td.rate_limited("spk1", "cam1", now=103.1)
    td._last.clear()


def test_playing_the_reviewed_text_logs_the_exact_words():
    setup()
    db.upsert_speaker(speaker("spk_dw2"))
    talkdown._tts["engine"] = "sapi"   # as if the engine were there (the speaker is unreachable: nothing is synthesized)
    spoken = f"You in the red shirt by the white truck: {LEAVE}"
    try:
        r = call("POST", "/api/speakers/spk_dw2/play", {"text": spoken}, role="operator", user="soc@example")
        assert r.status_code == 200, r.text
        assert r.json()["text"] == spoken and r.json()["message"].startswith('"You in the red shirt')
        talkdown.manager.wait("spk_dw2")
        row = db.one("SELECT * FROM talkdown_log WHERE speaker_id='spk_dw2' ORDER BY id DESC LIMIT 1")
        assert row["text"] == spoken and row["by"] == "soc@example" and row["result"] == "failed"
    finally:
        talkdown._tts["engine"] = None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    talkdown.manager.stop_all()
    print("all passed")
