"""Ask about this clip, beyond the clip (clipask.py, identities.same_subject, assistant tool same_subject, the clip chat
route): the intent router, the same-person / same-vehicle search on synthetic fingerprints (same / possible / left out,
windows, camera scope, a group anchor, the cap), plate matches, the evidence text and refs, and POST
/api/events/{id}/chat with a fake Qwen: frames for clip questions, search evidence and refs for the others, refs kept in
the chat history and with a saved note. Throwaway database; no models, no network.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_clip_ask.py   (from backend/)
"""
import asyncio
import datetime as dt
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-clipask-test-")  # never the real DB
os.environ["NVR_RECORDINGS_DIR"] = tempfile.mkdtemp(prefix="nvr-clipask-rec-")
os.environ["NVR_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="nvr-clipask-run-")
os.environ["NVR_HUB_ENABLED"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import numpy as np  # noqa: E402

from nvr import aischedule, api, assistant, clipask, identities, vlmroute  # noqa: E402
from nvr.db import db  # noqa: E402

CAM = {"host": "127.0.0.1", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": "", "main_path": "/m",
       "sub_path": "/s", "enabled": 1, "zones": [], "retention_days": None, "scene_notes": "", "retention_policy": None}
CAMS = [("cam1", "Side Yard"), ("cam2", "East Door"), ("cam3", "Gate"), ("cam4", "Saturday Market")]
NOW = time.time()
DAY = dt.datetime.combine(dt.date.today() - dt.timedelta(days=1), dt.time()).timestamp()   # yesterday 00:00 local
H = 3600
rng = np.random.default_rng(11)


def unit(v):
    return v / np.linalg.norm(v)


def at_sim(base, s, dim=None):
    """A unit vector whose cosine with `base` is exactly s."""
    r = rng.normal(size=dim or len(base))
    r = unit(r - (r @ base) * base)
    return (s * base + np.sqrt(1 - s * s) * r).astype(np.float32)


def add(cam, ts, cls="person", vec=None, **kw):
    f = {"camera_id": cam, "track_id": "t", "camera_class": cls, "start_ts": ts, "end_ts": ts + 12, "status": "verified",
         "created_at": ts, "yolo_conf": 0.9, "yolo_class": cls, "synopsis": f"a {cls} at {cam}", "snapshot": "snapshot.jpg", **kw}
    eid = db.execute_insert(f"INSERT INTO events ({','.join(f)}) VALUES ({','.join('?' * len(f))})", list(f.values()))
    if vec is not None:
        db.set_vec(identities.VEC_TABLE[cls], eid, vec)
    return eid


P = unit(rng.normal(size=512))
W = unit(rng.normal(size=identities.VEHICLE_DIM))
ids: dict[str, int] = {}


def setup_module(_=None):
    for cid, name in CAMS:
        db.upsert_camera({"id": cid, "name": name, **CAM})
    ids["anchor"] = add("cam1", DAY + 10 * H, vec=P.astype(np.float32), journey_id=7)
    ids["s1"] = add("cam1", DAY + 11 * H, vec=at_sim(P, 0.92))
    ids["s2"] = add("cam1", DAY + 12 * H, vec=at_sim(P, 0.86))
    ids["p1"] = add("cam1", DAY + 13 * H, vec=at_sim(P, 0.80))
    ids["n1"] = add("cam1", DAY + 14 * H, vec=at_sim(P, 0.60))
    ids["s3"] = add("cam2", DAY + 11.5 * H, vec=at_sim(P, 0.95))                         # another camera
    ids["j1"] = add("cam3", DAY + 10.3 * H, vec=at_sim(P, 0.30), journey_id=7)           # confirmed journey
    ids["masked"] = add("cam1", DAY + 15 * H, vec=at_sim(P, 0.95), status="masked")
    ids["rejected"] = add("cam1", DAY + 15.5 * H, vec=at_sim(P, 0.95), status="rejected")
    ids["false_alarm"] = add("cam1", DAY + 16 * H, vec=at_sim(P, 0.95), feedback='{"verdict": "false_alarm"}')
    ids["group_old_vec"] = add("cam1", DAY + 16.5 * H, vec=at_sim(P, 0.95), people=2)
    ids["nofp"] = add("cam1", DAY + 17 * H)
    ids["day_before"] = add("cam1", DAY - 15 * H, vec=at_sim(P, 0.93))                    # the day before, 09:00
    ids["group"] = add("cam1", DAY + 9 * H, people=3)
    # vehicles on cam2
    ids["v_anchor"] = add("cam2", DAY + 8 * H, "vehicle", vec=W.astype(np.float32), plate="ABC1234", plate_raw="ABC-1234")
    ids["v_plate"] = add("cam2", DAY + 8.5 * H, "vehicle", plate="ABC1234")              # same plate, no fingerprint
    ids["v_other_plate"] = add("cam2", DAY + 9 * H, "vehicle", vec=at_sim(W, 0.97), plate="XYZ999")
    ids["v_same"] = add("cam2", DAY + 9.5 * H, "vehicle", vec=at_sim(W, 0.90))
    ids["v_possible"] = add("cam2", DAY + 10 * H, "vehicle", vec=at_sim(W, 0.84))
    ids["v_none"] = add("cam2", DAY + 10.5 * H, "vehicle", vec=at_sim(W, 0.75))
    ids["v_plate_only"] = add("cam2", DAY + 7 * H, "vehicle", plate="QRS55")
    ids["v_plate_only2"] = add("cam2", DAY + 7.5 * H, "vehicle", plate="QRS55")


def by_id(r):
    return {m["event_id"]: m for m in r["matches"]}


# ---------------------------------------------------------------- the intent router

def route(q):
    return clipask.route(q, CAMS, "cam1")


def test_clip_questions_stay_on_the_frames():
    for q in ["What is he carrying?", "What color is the car?", "Is the door open?", "What is the person doing?",
              "Describe their clothing and anything they carry.", "Which way did they come from and leave?",
              "Is anything suspicious here?"]:
        r = route(q)
        assert r["mode"] == "clip" and not r["same"] and not r["general"], (q, r)


def test_questions_beyond_the_clip_search():
    r = route("How many times did this person show up on this camera today?")
    assert r["mode"] == "search" and r["same"] and not r["general"] and r["scope"] == "camera"
    assert {"count", "recur", "period"} <= set(r["signals"])
    r = route("Where else did they go?")
    assert r["mode"] == "search" and r["same"] and r["scope"] == "all"
    r = route("Is he on other cameras?")
    assert r["same"] and r["scope"] == "all"
    r = route("Did this truck come back?")
    assert r["mode"] == "search" and r["same"] and r["scope"] == "camera"
    r = route("When did they first arrive today?")
    assert r["same"] and "first_last" in r["signals"]
    r = route("Has this guy ever been here before?")
    assert r["same"] and r["ever"]


def test_before_after_and_other_people_use_the_camera_events():
    r = route("What happened before this?")
    assert r["mode"] == "search" and r["general"] and r["context"] == "before" and not r["same"]
    assert route("What happened after?")["context"] == "after"
    for q in ["How many people came through here today?", "Did anyone else come by?"]:
        r = route(q)
        assert r["general"] and not r["same"], (q, r)


def test_mixed_and_unsure_questions_get_both():
    r = route("What is he carrying, and was he here earlier?")
    assert r["mode"] == "both" and r["same"]
    r = route("When did he leave?")   # the frame, or the site? unsure: frames plus the search
    assert r["mode"] == "both" and "weak" in r["signals"]


def test_a_named_camera_scopes_the_search_and_is_not_a_clip_word():
    r = route("Did he go to the East Door?")
    assert r["scope"] == "named" and r["cameras"] == ["cam2"] and r["same"]
    assert r["mode"] == "search", r   # "door" in a camera's name is not a door to look at
    # a day name inside a camera's name is not a period
    assert "period" not in route("Was he at Saturday Market?")["signals"]


def test_instructions_get_the_canned_reply_but_past_questions_search():
    intent = route("Tell me when he first showed up")
    assert intent["mode"] != "clip" and not api.is_instruction("Tell me when he first showed up", intent)
    q = "let me know if he comes back"
    assert api.is_instruction(q, route(q))
    q = "Keep an eye out for him"
    assert api.is_instruction(q, route(q))
    q = "Tell me when he came back and alert me"
    assert api.is_instruction(q, route(q))   # "alert me" is still an instruction
    assert not api.is_instruction("What is he holding?", route("What is he holding?"))


# ---------------------------------------------------------------- same person / vehicle

def test_same_person_on_this_camera_for_the_day():
    r = identities.same_subject(ids["anchor"], DAY, DAY + 24 * H, ["cam1"])
    m = by_id(r)
    assert r["kind"] == "person" and r["reason"] is None
    assert m[ids["anchor"]]["match"] == "this"
    assert m[ids["s1"]]["match"] == "same" and abs(m[ids["s1"]]["score"] - 0.92) < 1e-3
    assert m[ids["s2"]]["match"] == "same" and m[ids["p1"]]["match"] == "possible"
    assert r["same"] == 2 and r["possible"] == 1 and not r["capped"]
    for k in ("n1", "masked", "rejected", "false_alarm", "group_old_vec", "nofp", "day_before", "s3", "j1"):
        assert ids[k] not in m, k
    assert r["excluded"] == {"masked": 1, "rejected": 1, "groups": 2, "false_alarms": 1, "no_fingerprint": 1}, r["excluded"]
    assert [x["start_ts"] for x in r["matches"]] == sorted(x["start_ts"] for x in r["matches"])
    assert set(m[ids["s1"]]) >= {"event_id", "camera_id", "start_ts", "match", "score"}


def test_all_cameras_and_the_journey():
    r = identities.same_subject(ids["anchor"], DAY, DAY + 24 * H, None)
    m = by_id(r)
    assert m[ids["s3"]]["match"] == "same" and m[ids["s3"]]["camera_id"] == "cam2"
    assert m[ids["j1"]]["match"] == "same" and m[ids["j1"]]["via"] == "journey"   # 0.30 by appearance, but confirmed
    assert r["same"] == 4 and r["possible"] == 1


def test_window_decides():
    r = identities.same_subject(ids["anchor"], DAY - 24 * H, DAY + 24 * H, ["cam1"])
    assert by_id(r)[ids["day_before"]]["match"] == "same" and r["same"] == 3
    r = identities.same_subject(ids["anchor"], DAY + 11.5 * H, DAY + 24 * H, ["cam1"])
    assert ids["s1"] not in by_id(r) and ids["anchor"] in by_id(r)   # "this" always comes back


def test_group_anchor_and_no_fingerprint():
    r = identities.same_subject(ids["group"], DAY, DAY + 24 * H, ["cam1"])
    assert r["reason"] == "group" and r["people"] == 3 and not r["matches"]
    assert "group of 3 people" in assistant.same_subject_lines(r)[0]
    r = identities.same_subject(ids["nofp"], DAY, DAY + 24 * H, ["cam1"])
    assert r["reason"] == "no_fingerprint"


def test_cap():
    r = identities.same_subject(ids["anchor"], DAY, DAY + 24 * H, None, limit=2)
    assert r["capped"] and r["same"] == 4 and r["possible"] == 1
    listed = [x for x in r["matches"] if x["match"] != "this"]
    assert len(listed) == 2 and all(x["match"] == "same" for x in listed)
    # the most similar first: the journey match (score 0.30) is certain but least similar, so it is the one left out
    assert {x["event_id"] for x in listed} == {ids["s3"], ids["s1"]}
    assert any(line.startswith("Capped: 5 matches found") for line in assistant.same_subject_lines(r))


def test_vehicle_plate_and_appearance():
    r = identities.same_subject(ids["v_anchor"], DAY, DAY + 24 * H, ["cam2"])
    m = by_id(r)
    assert r["kind"] == "vehicle"
    assert m[ids["v_plate"]]["match"] == "same" and m[ids["v_plate"]]["via"] == "plate" and m[ids["v_plate"]]["score"] is None
    assert ids["v_other_plate"] not in m and r["excluded"]["other_plate"] == 3   # 0.97 alike, but another plate (and the two QRS55)
    assert m[ids["v_same"]]["match"] == "same" and m[ids["v_possible"]]["match"] == "possible"
    assert ids["v_none"] not in m
    # a plate alone (no fingerprint) still finds the same plate
    r = identities.same_subject(ids["v_plate_only"], DAY, DAY + 24 * H, ["cam2"])
    assert by_id(r)[ids["v_plate_only2"]]["via"] == "plate" and r["same"] == 1
    lines = assistant.same_subject_lines(identities.same_subject(ids["v_anchor"], DAY, DAY + 24 * H, ["cam2"]))
    assert any(line.startswith("SAME (same license plate)") for line in lines)


def test_tool_lines_and_refs():
    refs = assistant.Refs()
    a = {"event_id": ids["anchor"], "camera": "cam1", "since": DAY, "until": DAY + 24 * H}
    lines, n = asyncio.run(assistant.t_same_subject(a, refs))
    assert n == 3 and a["_result"]["same"] == 2
    assert "Certain appearances including this clip: 3." in lines[0]
    assert sum(line.startswith("SAME") for line in lines) == 2 and sum(line.startswith("POSSIBLE") for line in lines) == 1
    assert sum(line.startswith("THIS") for line in lines) == 1
    assert set(refs.events) == {ids["anchor"], ids["s1"], ids["s2"], ids["p1"]}
    assert "same_subject" in assistant.TOOL_FUNCS and "same_subject" not in assistant.TOOLS   # the planner never picks it


# ---------------------------------------------------------------- window and evidence

def test_window():
    e = db.event(ids["anchor"])
    w = clipask.window(e, "How many times did he come?", route("How many times did he come?"), NOW)
    assert w["since"] == DAY and w["until"] == DAY + 24 * H and "clip's day" in w["label"]
    w = clipask.window(e, "has he ever been here", route("has he ever been here"), NOW)
    assert w["since"] == (dt.datetime.fromtimestamp(DAY) - dt.timedelta(days=clipask.EVER_DAYS)).timestamp() and w["until"] == NOW
    w = clipask.window(e, "how many times in the last 3 days", route("how many times in the last 3 days"), NOW)
    assert abs(w["since"] - (NOW - 3 * 86400)) < 1 and w["until"] == NOW


def test_evidence_text_and_refs():
    e = db.event(ids["anchor"])
    q = "How many times did this person show up on this camera?"
    ev = asyncio.run(clipask.evidence(e, q, route(q), NOW))
    assert "[#" not in ev["text"] and "Certain appearances including this clip: 3." in ev["text"]
    assert "Period searched: the clip's day" in ev["text"] and "Side Yard (this clip's camera)" in ev["text"]
    got = {r["event_id"]: r["match"] for r in ev["refs"]}
    assert got == {ids["anchor"]: "this", ids["s1"]: "same", ids["s2"]: "same", ids["p1"]: "possible"}
    assert all({"event_id", "camera_id", "start_ts", "match", "score"} <= set(r) for r in ev["refs"])
    assert [r["start_ts"] for r in ev["refs"]] == sorted(r["start_ts"] for r in ev["refs"])
    q = "Where else did they go?"
    ev = asyncio.run(clipask.evidence(e, q, route(q), NOW))
    assert {r["event_id"] for r in ev["refs"] if r["match"] == "same"} == {ids["s1"], ids["s2"], ids["s3"], ids["j1"]}


def test_group_anchor_falls_back_to_the_cameras_events():
    e = db.event(ids["group"])
    q = "How many times did they come?"   # no period named: the clip's day (yesterday)
    ev = asyncio.run(clipask.evidence(e, q, route(q), NOW))
    assert "group of 3 people" in ev["text"] and "Sightings:" in ev["text"]   # count_events ran instead
    assert ev["refs"] and all(r["match"] in ("found", "this") for r in ev["refs"])
    assert ids["s1"] in {r["event_id"] for r in ev["refs"]}   # the camera's people that day


def test_before_context_lists_the_camera_events_before_the_clip():
    e = db.event(ids["s2"])   # 12:00; the 30 minutes before it hold nothing, 11:00 is s1
    q = "What happened before this?"
    with mock.patch.object(clipask, "CONTEXT_S", 2 * H):
        ev = asyncio.run(clipask.evidence(e, q, route(q), NOW))
    found = {r["event_id"] for r in ev["refs"]}
    assert ids["s1"] in found and ids["anchor"] in found and ids["p1"] not in found


# ---------------------------------------------------------------- the route, with a fake Qwen

class FakeQwen:
    def __init__(self, answer="Three times today."):
        self.answer, self.calls = answer, []

    async def stream(self, task, messages, num_predict, temperature, priority):
        self.calls.append({"task": task, "messages": messages})
        for word in self.answer.split(" "):
            yield "delta", word + " "


async def fake_frames(e, at):
    return [(float(i), b"\xff\xd8jpeg", f"chat_{i}.jpg") for i in range(4)]


def call(method, path, body=None):
    async def go():
        transport = httpx.ASGITransport(app=api.app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8080") as c:
            return await c.request(method, path, json=body)
    return asyncio.run(go())


def chat(eid, message, qwen):
    api.state.pipeline = SimpleNamespace(vlm_ready=True, cameras={}, chat_frames=fake_frames, subscribers=set(),
                                         reindex=lambda _id: asyncio.sleep(0), publish=lambda *_: None)
    with mock.patch.object(vlmroute.router, "stream", qwen.stream):
        r = call("POST", f"/api/events/{eid}/chat", {"message": message})
    assert r.status_code == 200, r.text
    return [json.loads(line) for line in r.text.splitlines() if line.strip()]


def test_chat_route_beyond_the_clip_returns_refs_and_keeps_them():
    q = FakeQwen()
    chunks = chat(ids["s1"], "How many times did this person show up on this camera?", q)
    kinds = [c["type"] for c in chunks]
    assert kinds[0] == "user" and "frames" not in kinds and "refs" in kinds and kinds[-1] == "done", kinds
    refs = next(c for c in chunks if c["type"] == "refs")["refs"]
    assert chunks[-1]["refs"] == refs
    matches = {r["event_id"]: r["match"] for r in refs}
    assert matches[ids["s1"]] == "this" and matches[ids["anchor"]] == "same"   # 0.92 to the anchor
    assert set(matches.values()) <= {"this", "same", "possible"}
    msgs = q.calls[0]["messages"]
    assert q.calls[0]["task"] == "chat" and aischedule.kind_of("chat") == "ask"   # interactive: metered as Ask
    assert "SAME" in msgs[1]["content"] and "Search results:" in msgs[1]["content"] and "images" not in msgs[1]
    assert "Count only THIS and SAME as certain" in msgs[0]["content"]
    history = call("GET", f"/api/events/{ids['s1']}/chat").json()
    answer = history[-1]
    assert answer["role"] == "assistant" and answer["content"] == "Three times today." and answer["refs"] == refs
    assert history[-2]["refs"] == []
    # Save as note keeps working, refs and all
    saved = call("PUT", f"/api/events/{ids['s1']}/chat/{answer['id']}/saved?saved=true").json()
    assert saved[-1]["saved"] == 1 and saved[-1]["refs"] == refs


def test_chat_route_clip_question_uses_the_frames_only():
    q = FakeQwen("A ladder.")
    chunks = chat(ids["s2"], "What is he carrying?", q)
    kinds = [c["type"] for c in chunks]
    assert kinds == ["user", "frames", "delta", "delta", "done"], kinds
    assert len(chunks[1]["frames"]) == 4 and chunks[-1]["refs"] == []
    msgs = q.calls[0]["messages"]
    assert len(msgs[1]["images"]) == 4 and "Search results" not in msgs[1]["content"]
    assert "Count only THIS and SAME" not in msgs[0]["content"]
    assert call("GET", f"/api/events/{ids['s2']}/chat").json()[-1]["refs"] == []


def test_chat_route_mixed_question_gets_two_frames_and_the_search():
    q = FakeQwen("A ladder; yes, at 11:00.")
    chunks = chat(ids["s2"], "What is he carrying, and was he here earlier?", q)
    frames = next(c for c in chunks if c["type"] == "frames")["frames"]
    assert [f["t"] for f in frames] == [0.0, 3.0]   # the first and the last of the overview
    msgs = q.calls[0]["messages"]
    assert len(msgs[1]["images"]) == 2 and "Search results:" in msgs[1]["content"]
    assert any(c["type"] == "refs" and c["refs"] for c in chunks)


def test_chat_route_instructions_are_still_canned():
    q = FakeQwen()
    chunks = chat(ids["s2"], "Keep an eye out for him", q)
    assert "Grouped by who" in "".join(c.get("text", "") for c in chunks) and not q.calls
    chunks = chat(ids["s2"], "Tell me when he first showed up", q)
    assert q.calls and any(c["type"] == "refs" for c in chunks)


def test_chat_history_storage_roundtrip():
    refs = [{"event_id": 1, "camera_id": "cam1", "start_ts": 1.0, "match": "same", "score": 0.9}]
    mid = db.add_chat(ids["n1"], "assistant", "x", refs=refs)
    db.add_chat(ids["n1"], "user", "y")
    rows = db.chat(ids["n1"])
    assert rows[0]["id"] == mid and rows[0]["refs"] == refs and rows[1]["refs"] == []


if __name__ == "__main__":
    setup_module()
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
