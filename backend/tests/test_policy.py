"""Site rules (policy.py) on a throwaway database: who may tow what.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_policy.py   (from backend/)
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-policy-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from nvr import baseline, identities, policy  # noqa: E402
from nvr.db import db  # noqa: E402

RULE = {"kind": "towing", "asset": "solar light tower", "allowed": ["BIGView truck"], "priority": "high"}
CAM = {"id": "cam1", "name": "Side Yard", "host": "127.0.0.1", "onvif_port": 80, "rtsp_port": 554, "username": "u",
       "password": "", "main_path": "/m", "sub_path": "/s", "enabled": 1, "zones": [], "retention_days": None,
       "scene_notes": "", "retention_policy": None, "policies": [RULE]}
NOW = time.time()
rng = np.random.default_rng(3)
D = identities.VEHICLE_DIM


def unit(v):
    return v / np.linalg.norm(v)


BIGVIEW = unit(rng.normal(size=D))


def add(ts, vec, synopsis_json, synopsis="a truck"):
    f = {"camera_id": "cam1", "track_id": "t", "camera_class": "vehicle", "start_ts": ts, "end_ts": ts + 8,
         "status": "verified", "created_at": ts, "yolo_conf": 0.9, "synopsis": synopsis}
    eid = db.execute_insert(f"INSERT INTO events ({','.join(f)}) VALUES ({','.join('?' * len(f))})", list(f.values()))
    db.update_event(eid, synopsis_json=synopsis_json)
    db.set_vec("vehicle_vec", eid, vec)
    return eid


def setup_module():
    db.upsert_camera(CAM)


def test_camera_round_trip_and_prompt():
    cam = db.cameras()[0]
    assert cam["policies"] == [RULE]
    assert "BIGView truck" in policy.prompt_lines(cam) and "solar light tower" in policy.prompt_lines(cam)
    assert policy.labels_needed(cam) == {"vehicle"}
    assert policy.labels_needed({"policies": []}) == set()


OK = {"confirmed": True, "reason": "test: confirmed"}   # the second look (confirm_towing) said yes


def test_unknown_vehicle_towing_breaks_the_rule():
    e = add(NOW - 100, unit(rng.normal(size=D)), {"towing": True, "towed": "solar light tower", "towing_check": OK})
    broken = policy.check(e)
    assert broken and broken["kind"] == "towing" and "not a recognized vehicle" in broken["text"]
    ev = db.event(e)
    assert ev["policy"]["priority"] == "high"
    assert ev["priority"] == "high" and baseline.priority(ev, 0.0) == "high"  # stored, not just computable


def test_known_allowed_vehicle_is_fine():
    a = add(NOW - 300, unit(BIGVIEW + rng.normal(0, 0.05 / np.sqrt(D), D)), {"towing": True, "towed": "tower", "towing_check": OK})
    identities.name_cluster("vehicle", "BIGView truck", [a])
    b = add(NOW - 90, unit(BIGVIEW + rng.normal(0, 0.05 / np.sqrt(D), D)), {"towing": True, "towed": "tower", "towing_check": OK})
    assert policy.check(b) is None
    assert db.event(b)["policy"] is None


def test_not_towing_is_ignored_and_text_fallback_works():
    quiet = add(NOW - 80, unit(rng.normal(size=D)), {"towing": False}, synopsis="A pickup drives past the parked trailers.")
    assert policy.check(quiet) is None
    old = add(NOW - 70, unit(rng.normal(size=D)), {"summary": "x"}, synopsis="A white truck is towing a trailer with a tower.")
    assert policy.model_says_towing(db.event(old))  # older synopses have no towing flag: the wording is the claim ...
    assert policy.check(old) is None                # ... but a claim alone never breaks a rule
    db.update_event(old, synopsis_json={"summary": "x", "towing_check": OK})
    assert policy.check(old) is not None            # confirmed by the second look: it does
    claim_only = add(NOW - 60, unit(rng.normal(size=D)), {"towing": True, "towed": "tower"})
    assert policy.check(claim_only) is None and db.event(claim_only)["policy"] is None


# ---------------------------------------------------------------- the second look at a towing claim (confirm_towing)

RIGHT_TO_LEFT = [[NOW, 0.53, 0.38, 0.67, 0.48, 0.8], [NOW + 1, 0.39, 0.39, 0.50, 0.48, 0.6]]   # event 8194's mower
LEFT_TO_RIGHT = [[NOW, 0.00, 0.31, 0.07, 0.40, 0.5], [NOW + 4, 0.58, 0.35, 0.95, 0.55, 0.7]]   # event 8352's pickup


def claim(path, towed="solar light tower"):
    """A vehicle event whose synopsis says towing, with a wide frame and a crop on disk."""
    e = add(NOW - 50, unit(rng.normal(size=D)), {"summary": "x", "towing": True, "towed": towed})
    db.update_event(e, path=path, detections={"keyframes": [{"file": "wide.jpg", "kind": "wide"},
                                                             {"file": "crop_0.jpg", "kind": "crop"}]})
    d = Path(os.environ["NVR_DATA_DIR"]) / "events" / str(e)
    d.mkdir(parents=True, exist_ok=True)
    (d / "wide.jpg").write_bytes(b"wide")
    (d / "crop_0.jpg").write_bytes(b"crop")
    return e


def fake_model(answer=None, error=None):
    """Replace the model call; records what it was asked."""
    from nvr import vlmroute
    asked = []

    async def chat_json(task, system, text, images, schema, num_predict=300, temperature=0.1, priority="background"):
        asked.append({"task": task, "images": images, "text": text})
        if error:
            raise error
        return {**answer, "_model": "fake-vl"}
    vlmroute.router.chat_json = chat_json
    return asked


def confirm(e):
    return asyncio.run(policy.confirm_towing(e))


def test_mower_deck_is_not_towing():
    e = claim(RIGHT_TO_LEFT, towed="trailer")
    asked = fake_model({"vehicle": "zero-turn mower", "behind": "nothing", "connection": "none visible",
                        "towing": "no", "towed": "", "towed_side": "none"})
    chk = confirm(e)
    assert asked and asked[0]["images"] == [b"wide", b"crop"]            # wide frame first, then the crops
    assert chk["confirmed"] is False and "not towing" in chk["reason"], chk
    assert policy.check(e) is None and db.event(e)["policy"] is None
    s = db.event(e)["synopsis_json"]
    assert s["towing"] is True and s["towing_check"]["confirmed"] is False   # the model's flag stays for display


def test_yes_with_the_object_ahead_of_the_vehicle_is_not_towing():
    """The 27B's mistake on event 8194: 'yes, a trailer' - but the deck is on the side the mower drove toward."""
    e = claim(RIGHT_TO_LEFT, towed="trailer")
    fake_model({"vehicle": "ride-on mower", "behind": "a flat trailer", "connection": "short bar", "towing": "yes",
                "towed": "flat trailer", "towed_side": "left"})
    chk = confirm(e)
    assert chk["confirmed"] is False and "not behind" in chk["reason"] and chk["direction"] == "right to left", chk
    assert policy.check(e) is None


def test_object_merely_nearby_is_not_towing():
    """The 9B's mistake on event 8352: a pickup driving left to right past a light tower in the foreground."""
    e = claim(LEFT_TO_RIGHT)
    fake_model({"vehicle": "white pickup", "behind": "light tower", "connection": "tongue", "towing": "yes",
                "towed": "solar light tower", "towed_side": "nearer"})
    assert confirm(e)["confirmed"] is False and policy.check(e) is None
    fake_model({"vehicle": "white pickup", "behind": "nothing", "connection": "none visible", "towing": "unclear",
                "towed": "", "towed_side": "none"})
    assert confirm(e)["confirmed"] is False and policy.check(e) is None


def test_real_trailer_breaks_the_rule_as_before():
    e = claim(LEFT_TO_RIGHT)
    asked = fake_model({"vehicle": "dark pickup", "behind": "solar light tower on a trailer", "connection": "tongue on the ball hitch",
                        "towing": "yes", "towed": "solar light tower", "towed_side": "left"})
    chk = confirm(e)
    assert asked[0]["task"] == policy.TOWING_TASK
    assert chk["confirmed"] is True and "behind the vehicle" in chk["reason"] and chk["model"] == "fake-vl", chk
    broken = policy.check(e)
    assert broken == {"kind": "towing", "priority": "high",
                      "text": "Unknown vehicle towing a solar light tower: not a recognized vehicle to tow a solar light tower "
                              "(allowed: BIGView truck)"}, broken
    assert db.event(e)["priority"] == "high"
    # hitching up while standing still: the direction can't be checked, the confirmed yes stands
    still = claim([[NOW, 0.4, 0.4, 0.6, 0.5, 0.9], [NOW + 5, 0.41, 0.4, 0.61, 0.5, 0.9]])
    assert confirm(still)["confirmed"] is True and policy.check(still) is not None


def test_model_unavailable_is_not_towing_and_logged():
    import logging
    seen = []
    h = logging.Handler()
    h.emit = lambda rec: seen.append(rec.getMessage())
    logging.getLogger("nvr.policy").addHandler(h)
    try:
        e = claim(LEFT_TO_RIGHT)
        fake_model(error=RuntimeError("no model for unusual_review"))
        chk = confirm(e)
    finally:
        logging.getLogger("nvr.policy").removeHandler(h)
    assert chk["confirmed"] is False and "could not run" in chk["reason"] and "RuntimeError" in chk["error"], chk
    assert any("towing check failed" in m for m in seen), seen
    assert policy.check(e) is None and db.event(e)["policy"] is None


def test_no_claim_no_check():
    quiet = add(NOW - 40, unit(rng.normal(size=D)), {"summary": "x", "towing": False})
    asked = fake_model({"towing": "yes", "towed_side": "left"})
    assert confirm(quiet) is None and not asked and "towing_check" not in db.event(quiet)["synopsis_json"]


def test_recheck_clears_when_rule_removed():
    assert policy.recheck("cam1") >= 2
    db.upsert_camera({**CAM, "policies": []})
    assert policy.recheck("cam1") == 0
    assert all(r["policy"] is None for r in (db.event(x["id"]) for x in db.all("SELECT id FROM events")))


if __name__ == "__main__":
    setup_module()
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
