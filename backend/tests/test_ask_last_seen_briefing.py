"""Ask "when was X last seen" searches events too (newest first), and the briefing always carries PPE and rule summaries.

No Qwen, no Ollama: the embedding, footage index, frame check and tiles are faked.
Run: ..\\.venv\\Scripts\\python.exe tests\\test_ask_last_seen_briefing.py   (from backend/)
"""
import asyncio
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-lastseen-test-")        # never the real DB
os.environ["NVR_RECORDINGS_DIR"] = tempfile.mkdtemp(prefix="nvr-lastseen-rec-")   # never the real recordings
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import assistant, footage  # noqa: E402
from nvr import synopsis as vlm  # noqa: E402
from nvr.db import db  # noqa: E402

CAM = {"host": "127.0.0.1", "onvif_port": 80, "rtsp_port": 554, "username": "u", "password": "", "main_path": "/m",
       "sub_path": "/s", "enabled": 1, "retention_days": None, "scene_notes": "", "retention_policy": None}
PPE_ZONE = {"name": "PPE zone 1", "type": "ppe", "points": [[0, 0], [1, 0], [1, 1]], "required": ["hard_hat", "vest"]}
NOW = dt.datetime(2026, 10, 6, 9, 0).timestamp()


def ts(month, day, h, m=0):
    return dt.datetime(2026, month, day, h, m).timestamp()


def add(cam, start, cls="person", synopsis=None, index=True, **kw):
    f = {"camera_id": cam, "track_id": "t", "camera_class": cls, "start_ts": start, "end_ts": start + 10, "status": "verified",
         "created_at": start, "synopsis": synopsis or f"a {cls} on {cam}"}
    eid = db.execute_insert(f"INSERT INTO events ({','.join(f)}) VALUES ({','.join('?' * len(f))})", list(f.values()))
    if kw:
        db.update_event(eid, **kw)
    if index:
        db.index_event_text(eid, f["synopsis"], cls, None)
    return eid


IDS = {}


def setup_module(_=None):
    db.upsert_camera({"id": "cam1", "name": "Side Yard", "zones": [], **CAM})
    db.upsert_camera({"id": "cam2", "name": "East Door Exit & Bathrooms", "zones": [PPE_ZONE], **CAM})
    # an older, wordier description of a white pickup ranks above the latest sighting by relevance alone
    IDS["sep20"] = add("cam1", ts(9, 20, 11), "vehicle", "A white pickup truck drives in. The white pickup truck parks; "
                                                         "a white pickup truck with a toolbox.")
    IDS["oct5"] = add("cam1", ts(10, 5, 17, 36), "vehicle", "A white Toyota Tundra pickup truck with black wheels drives across "
                                                            "the property from left to right.")
    IDS["sedan"] = add("cam1", ts(10, 6, 8, 10), "vehicle", "A white sedan parks by the gate.")   # newer, but not a pickup
    # Oct 5: 13 PPE violations and 10 long stays (unusual, medium priority) at the east door; one towing rule broken
    day = []
    for i in range(13):
        day.append(add("cam2", ts(10, 5, 7, 10) + i * 3150, "person", f"A worker by the east door {i}", priority="medium",
                       anomaly=0.3, detections={"ppe": {"verdict": "violation", "zone": "PPE zone 1", "dwell_s": 20,
                                                        "violation": ["hard_hat", "vest"] if i % 3 else ["hard_hat"]}},
                       policy={"kind": "ppe", "priority": "medium", "text": "No hard hat or hi-vis vest in PPE zone 'PPE zone 1' (20 s)"}))
    IDS["ppe"] = day
    IDS["long"] = [add("cam2", ts(10, 5, 9) + i * 1800, "person", f"A person stays in the bathroom hallway {i}", priority="medium",
                       anomaly=0.95 - i * 0.001, anomaly_json={"score": 0.95, "reasons": ["Stayed 9 min, longer than 97% of visits"]})
                   for i in range(10)]
    IDS["tow"] = add("cam1", ts(10, 5, 14), "vehicle", "A dark pickup tows a solar light tower out of the yard.", priority="high",
                     policy={"kind": "towing", "priority": "high",
                             "text": "Unknown vehicle towing a solar light tower: not a recognized vehicle to tow a solar light tower "
                                     "(allowed: BIGView truck)"})


# ---------------------------------------------------------------- fakes (no models)

class FakeIndex:
    def search(self, vec, cams, since, until, k):
        return [("cam1", ts(9, 27, 10, 15), 0, 0.31)]   # the footage index's best visual match: Sep 27


class FakeFootage:
    index = FakeIndex()

    async def embed_text(self, text):
        return [0.0]


async def no_embed(text):
    return None


async def footage_yes(image, query):
    return {"matches": True, "confidence": "high", "seen": "a white pickup truck"}


def install_fakes():
    vlm.embed = no_embed
    vlm.footage_match = footage_yes
    footage.tile_jpeg = lambda cam, t, tile: b"jpeg"
    assistant.ctx.footage = FakeFootage()


# ---------------------------------------------------------------- Ask: last seen

def test_last_seen_questions_always_search_events():
    # the trial's plan: only a footage search
    raw = {"calls": [{"tool": "search_footage", "text": "white pickup truck", "since": "2026-09-01 00:00"}]}
    calls = assistant.check_plan(raw, "When was a white pickup truck last seen?", NOW)
    assert [c["tool"] for c in calls] == ["search_events", "search_footage"], calls
    ev = calls[0]["args"]
    assert ev["text"] == "white pickup truck" and ev["newest"] and ev["since"] is None and ev["until"] is None, ev
    assert ev["label"] == "vehicle"
    assert calls[1]["args"]["since"] is None and calls[1]["args"]["newest"]      # all indexed footage, too
    # a planner event search with a guessed window is replaced, not run twice
    raw = {"calls": [{"tool": "search_events", "text": "white pickup", "since": "2026-10-06 00:00"}]}
    calls = assistant.check_plan(raw, "When did the white pickup last come by?", NOW)
    assert [c["tool"] for c in calls] == ["search_events", "search_footage"], calls
    assert calls[0]["args"]["since"] is None and calls[0]["args"]["text"] == "white pickup"
    # a period in the question still applies
    calls = assistant.check_plan({"calls": []}, "When was the white pickup last seen yesterday?", NOW)
    assert calls[0]["args"]["newest"] and calls[0]["args"]["since"] == ts(10, 5, 0), calls
    # other questions are untouched
    for q in ("Show the latest events at the east door", "When did someone go outside?", "Was a boat on any camera today?"):
        assert not any(c["args"].get("newest") for c in assistant.check_plan({"calls": []}, q, NOW)), q


def test_last_seen_results_put_the_latest_event_first():
    install_fakes()
    calls = assistant.check_plan({"calls": [{"tool": "search_footage", "text": "white pickup truck"}]},
                                 "When was a white pickup truck last seen?", NOW)
    refs = assistant.Refs()
    text, summary = asyncio.run(assistant.run_calls(calls, refs, "When was a white pickup truck last seen?"))
    ev_block = text.split("### ")[1]
    assert ev_block.startswith("search_events(") and "newest first" in ev_block.splitlines()[0], ev_block
    lines = [l for l in ev_block.splitlines() if l.startswith("[#")]
    assert lines[0].startswith(f"[#{IDS['oct5']}]") and "Toyota Tundra" in lines[0], lines   # Oct 5 first
    assert lines[1].startswith(f"[#{IDS['sep20']}]"), lines                                # the older one is still there
    assert "[F1] Side Yard, Sun 27 Sep 10:15:00 - CHECKED: yes" in text, text             # footage still searched
    assert str(IDS["oct5"]) in refs.as_dict()["events"]
    assert summary[0]["tool"] == "search_events" and summary[0]["count"] >= 2
    assert "most recent one" in assistant.ANSWER_SYSTEM


# ---------------------------------------------------------------- briefing: PPE and rules can't be crowded out

def test_briefing_facts_include_ppe_and_rules():
    facts, stats, refs = assistant.gather_facts(ts(10, 5, 0), ts(10, 6, 0))
    print("\n----- briefing facts (fixture day) -----\n" + facts + "\n-----")
    lines = facts.splitlines()
    ppe = next(l for l in lines if l.startswith("PPE:"))
    assert ppe.startswith("PPE: 13 violations (people without a hard hat or hi-vis vest) in PPE zone 'PPE zone 1' "
                          "(East Door Exit & Bathrooms), 07:10–17:40"), ppe
    assert "13 without a hard hat, 8 without a hi-vis vest" in ppe, ppe
    examples = [IDS["ppe"][0], IDS["ppe"][6], IDS["ppe"][12]]
    assert ppe.endswith("examples " + " ".join(f"[#{i}]" for i in examples)), ppe
    assert all(str(i) in refs.as_dict()["events"] for i in examples)
    rules = next(l for l in lines if l.startswith("Site rules broken:"))
    assert rules.startswith(f"Site rules broken: towing 1 (high priority) [#{IDS['tow']}]: Unknown vehicle towing") and \
        "PPE 13 (medium priority)" in rules, rules
    attn = lines.index("Needs attention:")
    assert lines.index(ppe) < attn and lines.index(rules) < attn
    listed = [l for l in lines[attn + 1:] if l.startswith("- [#")]
    assert len(listed) == assistant.ATTENTION_MAX, listed
    assert listed[0].startswith(f"- [#{IDS['tow']}]")                      # the broken rule first
    assert sum("Stayed 9 min" in l for l in listed) >= assistant.ATTENTION_EACH
    assert stats["ppe"][0]["count"] == 13 and stats["rules"] == {"towing": 1, "ppe": 13}
    assert any(f.startswith("PPE: 13 violations in 'PPE zone 1'") for f in stats["fixed"]), stats["fixed"]
    assert "PPE violations" in assistant.BRIEFING_SYSTEM and "'PPE:' line" in assistant.BRIEFING_SYSTEM


def test_quiet_ppe_day_says_so():
    facts, stats, _ = assistant.gather_facts(ts(10, 3, 0), ts(10, 4, 0))
    assert "PPE: no violations in the PPE zones." in facts and stats["ppe"] == [] and "Site rules broken" not in facts, facts


if __name__ == "__main__":
    setup_module()
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
