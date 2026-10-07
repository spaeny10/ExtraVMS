"""The second, full-resolution look at a weapon in a synopsis (weaponcheck.py), on a throwaway database with a fake
model and fake evidence. Event 9118: a black microfiber towel was described as "a black handgun".

Run: ..\\.venv\\Scripts\\python.exe tests\\test_weaponcheck.py   (from backend/)
"""
import asyncio
import copy
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-weapon-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from nvr import vlmroute, weaponcheck as wc  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402

DATA = Path(os.environ["NVR_DATA_DIR"])

# event 9118 as the model first wrote it
SYN_9118 = {
    "summary": "A man with a beard, wearing a black t-shirt, khaki pants, and a blue baseball cap, entered through the "
               "South Exterior Door. Another person wearing an orange and blue cap is visible in the foreground holding "
               "a black handgun.",
    "objects": [{"type": "person", "description": "Male with a beard holding a clear water bottle."},
                {"type": "person", "description": "Male in an orange and blue cap, standing in the foreground, holding "
                                                  "a black handgun in his right hand."}],
    "activity": "The primary subject walked toward the camera. A second individual is standing in the foreground "
                "holding a firearm.",
    "threat_level": "high", "tags": ["person", "firearm", "indoor", "kitchen"], "towing": False, "towed": "",
    "model": "qwen3.8:27b",
}
QUIET = {"summary": "A man in a gray hoodie walked from the parking lot to the front door and went inside.",
         "objects": [{"type": "person", "description": "Man in a gray hoodie and jeans."}],
         "activity": "Walked in through the front door.", "threat_level": "none", "threat_reason": "",
         "tags": ["person", "entry"], "towing": False, "towed": ""}
EVENT = {"id": 9118, "camera_class": "person", "detections": {"keyframes": []}}


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- the claim

def words(text):
    return [m.group(0).lower() for m in wc.mentions(text)]


def test_mentions_and_negations():
    for text, want in [
        ("holding a black handgun", ["handgun"]),
        ("a man with a pistol near the gate", ["pistol"]),
        ("a dark object, possibly a handgun", ["handgun"]),
        ("an armed man at the door", ["armed"]),
        ("carrying a rifle and a knife", ["rifle", "knife"]),
        ("a toy gun", ["gun"]),                                   # a toy is exactly what the second look is for
        ("He walked past with no hurry, holding a handgun", ["handgun"]),
        ("no bag but holding a pistol", ["pistol"]),
        ("A man with no hat carrying a knife", ["knife"]),
        ("The man is not wearing a mask and holds a pistol", ["pistol"]),
        ("a putty knife and a kitchen knife", ["knife"]),          # the second one
    ]:
        assert words(text) == want, (text, words(text))
    for text in ["no weapon visible", "not a gun", "No weapons, knives or guns were seen.",
                 "neither a gun nor a knife", "There is no indication of a weapon", "without a weapon", "unarmed",
                 "does not appear to be holding a weapon", "There is no indication that anyone is holding a weapon",
                 "no one holding a weapon", "not holding a gun, nor a knife", "a gun is not visible",
                 "weapons were not seen", "nothing resembling a weapon", "cannot confirm a weapon",
                 "the dark object was a microfiber towel, not a weapon.", "doesn't have a weapon",
                 "the alarm system was armed", "a person rifles through the car", "a gun-metal gray sedan",
                 "a gun safe in the corner", "", None]:
        assert words(text) == [], (text, words(text))


def test_tools_are_not_weapons():
    for text in ["a nail gun", "a hot glue gun on the bench", "using a heat gun", "a spray-gun", "a staple gun",
                 "a caulk gun", "a putty knife", "a water gun", "a nerf gun", "a butter knife", "a radar gun"]:
        assert words(text) == [], text


def test_claims_cover_every_field_and_tags():
    assert wc.claims(QUIET) == [] and wc.claims(None) == [] and wc.claims({}) == []
    assert wc.claims(SYN_9118) == ["handgun", "firearm"]
    assert wc.claims({"summary": "A man at the door.", "activity": "He is holding a knife."}) == ["knife"]
    assert wc.claims({"summary": "A man.", "objects": [{"type": "handgun", "description": "small, black"}]}) == ["handgun"]
    assert wc.claims({"summary": "A man.", "threat_reason": "He is armed."}) == ["armed"]
    assert wc.claims({"summary": "A man.", "tags": ["person", "firearm"]}) == ["firearm"]
    assert wc.claims({"summary": "A man.", "tags": ["weapon_visible"]}) == ["weapon_visible"]
    assert wc.claims({"summary": "A man.", "tags": ["no_weapon", "nail_gun", "person"]}) == []
    assert wc.claims({"summary": "A man.", "threat_reason": "Routine; the dark object was a towel, not a weapon."}) == []


def test_claim_kind():
    assert wc.claim_kind(["handgun", "firearm"]) == "firearm"
    assert wc.claim_kind(["hand gun"]) == "firearm"
    assert wc.claim_kind(["knife"]) == "knife"
    assert wc.claim_kind(["weapon"]) == "weapon" and wc.claim_kind(["armed"]) == "weapon"
    assert wc.claim_kind(["knife", "pistol"]) == "firearm"


# ---------------------------------------------------------------- the answers and the verdict

def person(weapon="no", conf="high", held=("black microfiber towel",), like="black microfiber towel"):
    return wc.judge_person({"held": list(held), "most_weapon_like": like, "weapon": weapon, "confidence": conf,
                            "_model": "fake-vl"})


def test_judge_person_normalizes():
    p = person()
    assert p["object"] == "black microfiber towel" and p["held"] == ["black microfiber towel"] and p["model"] == "fake-vl"
    assert p["held_by"] == "hand" and p["resembles_weapon"] == "yes"
    p = wc.judge_person({"held": [], "most_weapon_like": "none", "weapon": "no", "confidence": "high"})
    assert p["object"] == "nothing" and p["held_by"] == "none" and wc.plausible(p)
    p = wc.judge_person({"held": ["A red water bottle.", "a shoe"], "most_weapon_like": "none", "weapon": "NO",
                         "confidence": "High"})
    assert p["object"] == "red water bottle" and p["held"] == ["red water bottle", "shoe"]
    assert p["weapon"] == "no" and p["confidence"] == "high" and p["resembles_weapon"] == "no"
    bad = wc.judge_person({"weapon": "maybe", "confidence": "sure"})   # nonsense: unclear, low
    assert bad["weapon"] == "unclear" and bad["confidence"] == "low"
    assert not wc.plausible(wc.judge_person({"held": ["dark object"], "most_weapon_like": "dark object",
                                             "weapon": "no", "confidence": "high"}))   # vague: not an answer
    assert not wc.plausible(person(held=("phone", "pistol"), like="phone"))           # a weapon among the things held


def test_decide_all_outcomes():
    towel, empty = person(), wc.judge_person({"held": [], "most_weapon_like": "none", "weapon": "no", "confidence": "medium"})
    gun = person("yes", "high", ("black pistol",), "black pistol")
    assert wc.decide([towel, gun], 2, True, None) == "confirmed"
    assert wc.decide([gun], 3, False, "timeout") == "confirmed"          # one weapon seen is enough
    assert wc.decide([person("yes", "low")], 1, True, None) == "unclear"  # a low-confidence yes confirms nothing
    assert wc.decide([towel, empty], 2, True, None) == "not_confirmed"
    assert wc.decide([towel], 2, True, None) == "unclear"                 # not everyone was answered
    assert wc.decide([towel], 1, False, None) == "unclear"                # incomplete evidence never clears
    assert wc.decide([person("no", "low")], 1, True, None) == "unclear"
    assert wc.decide([person("unclear", "medium")], 1, True, None) == "unclear"
    assert wc.decide([towel], 2, True, "timeout") == "error"
    assert wc.decide([], 1, True, "RuntimeError: down") == "error"
    assert wc.decide([], 0, False, "no evidence") == "error"


# ---------------------------------------------------------------- the rewritten text and the threat

def check(verdict, persons, kind="firearm", reason="test"):
    return {"verdict": verdict, "confirmed": verdict == "confirmed", "needs_review": verdict in ("unclear", "error"),
            "kind": kind, "claim": ["handgun"], "persons": persons, "reason": reason}


def test_not_confirmed_rewrites_every_field_consistently():
    out = wc.apply(EVENT, SYN_9118, check("not_confirmed", [person(held=("water bottle",), like="none"), person()]))
    assert "holding a black microfiber towel." in out["summary"] and "handgun" not in out["summary"]
    assert out["activity"].endswith("holding a black microfiber towel.")
    assert out["objects"][1]["type"] == "person"                         # a person stays a person
    assert "holding a black microfiber towel in his right hand" in out["objects"][1]["description"]
    assert out["objects"][0] == SYN_9118["objects"][0]                   # untouched
    assert "firearm" not in out["tags"] and "possible_weapon" not in out["tags"] and "person" in out["tags"]
    assert out["threat_level"] == "low" and "found no weapon" in out["threat_reason"]
    assert wc.claims({k: v for k, v in out.items() if k != "weapon_check"}) == []   # nothing left to alarm on
    assert SYN_9118["summary"].endswith("a black handgun.")                # the model's own text is not modified
    # the claim's color picks the person: a red bottle and a black cloth, the claim said "black"
    out = wc.apply(EVENT, SYN_9118, check("not_confirmed", [person(held=("red water bottle",), like="red water bottle"),
                                                            person(held=("black cloth",), like="black cloth")]))
    assert out["summary"].endswith("holding a black cloth.")
    # two people, two candidates, nothing to choose by: no weapon word left, no guess either
    out = wc.apply(EVENT, SYN_9118, check("not_confirmed", [person(held=("shoe",), like="none"),
                                                            person(held=("water bottle",), like="none")]))
    assert out["summary"].endswith("holding a black object (not a weapon).") and "handgun" not in out["summary"]
    assert "holding an object (not a weapon)" in out["activity"]


def test_rewrite_shapes():
    towel = wc.as_object("black towel")
    assert wc.rewrite("a dark object, possibly a handgun, near the door", towel) == "a dark object (a black towel) near the door"
    assert wc.rewrite("holding what appears to be a pistol", towel) == "holding a black towel"
    assert wc.rewrite("A handgun is visible in his hand.", towel) == "A black towel is visible in his hand."
    assert wc.rewrite("holding a phone and a gun", wc.as_object("stapler")) == "holding a phone and a stapler"
    assert wc.rewrite("a small silver revolver", wc.as_object("phone")) == "a silver phone"
    assert wc.rewrite("He is armed.", towel) is None                     # not a simple phrase: the caller adds a note
    assert wc.rewrite("No weapons here.", towel) == "No weapons here."  # nothing claimed: unchanged


def test_unclear_wording_and_priority():
    out = wc.apply(EVENT, SYN_9118, check("unclear", [person("unclear", "low")], reason="it could not tell"))
    assert out["summary"].endswith("holding a black object (possible firearm, unconfirmed).")
    assert out["activity"].endswith("holding an object (possible firearm, unconfirmed).")
    assert "(possible firearm, unconfirmed)" in out["objects"][1]["description"] and out["objects"][1]["type"] == "person"
    assert out["threat_level"] == "high" and out["threat_reason"].startswith("Possible firearm, unconfirmed: ")
    assert "possible_weapon" in out["tags"] and "firearm" not in out["tags"]
    # a firearm the model itself rated low is still HIGH until someone looks
    low = {**SYN_9118, "summary": "A man holding a dark object, possibly a handgun.", "threat_level": "low"}
    out = wc.apply(EVENT, low, check("error", []))
    assert out["summary"] == "A man holding a dark object (possible firearm, unconfirmed)." and out["threat_level"] == "high"
    # the weapon only in a tag: the summary (what the hub and the alert show) still says so
    tagged = {**QUIET, "tags": ["person", "firearm"]}
    out = wc.apply(EVENT, tagged, check("unclear", []))
    assert out["summary"].startswith("Possible firearm, unconfirmed (a person needs to look): A man in a gray hoodie")
    assert out["threat_level"] == "high"
    # a kitchen knife the model judged harmless keeps its level (still worded as unconfirmed, still to review)
    knife = {**QUIET, "summary": "He cuts a pastry with a knife.", "threat_level": "none"}
    out = wc.apply(EVENT, knife, check("unclear", [], kind="knife"))
    assert out["summary"] == "He cuts a pastry with an object (possible knife, unconfirmed)." and out["threat_level"] == "none"
    assert out["weapon_check"]["needs_review"]


def test_confirmed_keeps_the_claim():
    gun = person("yes", "high", ("black pistol",), "black pistol")
    out = wc.apply(EVENT, {**SYN_9118, "threat_level": "medium"}, check("confirmed", [gun], reason="Weapon confirmed"))
    assert "holding a black handgun" in out["summary"] and out["summary"].endswith("Weapon confirmed by a second look at full resolution.")
    assert out["threat_level"] == "high" and "firearm" in out["tags"] and out["threat_reason"] == "Weapon confirmed"
    knife = {**QUIET, "summary": "He cuts a pastry with a knife.", "threat_level": "none"}
    out = wc.apply(EVENT, knife, check("confirmed", [person("yes", "high", ("kitchen knife",), "kitchen knife")], kind="knife"))
    assert out["threat_level"] == "none"                                 # a knife keeps the model's own level


def test_not_confirmed_threat_rules():
    towel = [person()]
    # never raised: the model's "none" stays none
    out = wc.apply(EVENT, {**SYN_9118, "threat_level": "none"}, check("not_confirmed", towel))
    assert out["threat_level"] == "none"
    # unusual for the camera / a site rule: medium, not low
    out = wc.apply({**EVENT, "anomaly_json": {"reasons": ["unusual at 3 AM"]}}, SYN_9118, check("not_confirmed", towel))
    assert out["threat_level"] == "medium" and "unusual at 3 AM" in out["threat_reason"]
    out = wc.apply({**EVENT, "policy": {"text": "Entered through Back Door: not a recognized person"}}, SYN_9118,
                   check("not_confirmed", towel))
    assert out["threat_level"] == "medium"
    # something else suspicious in the description: medium
    masked = {**SYN_9118, "activity": "A masked man loitering by the cars, holding a pistol."}
    out = wc.apply(EVENT, masked, check("not_confirmed", towel))
    assert out["threat_level"] == "medium"
    # the threat had another reason than the weapon: that level stays
    other = {**SYN_9118, "threat_reason": "Forced the side door open."}
    out = wc.apply(EVENT, other, check("not_confirmed", towel))
    assert out["threat_level"] == "high" and out["threat_reason"] == "Forced the side door open."


# ---------------------------------------------------------------- review(): the whole check, fake model and evidence

def fake_model(answers=None, error=None, delay=0.0):
    """Replace the model call; answers are given out in order (the last one repeats). Records what was asked."""
    asked = []

    async def chat_json(task, system, text, images, schema, num_predict=300, temperature=0.1, priority="background"):
        asked.append({"task": task, "images": len(images), "text": text, "priority": priority})
        if delay:
            await asyncio.sleep(delay)
        if error:
            raise error
        a = answers[min(len(asked), len(answers)) - 1]
        return {**a, "_model": "fake-vl"}
    vlmroute.router.chat_json = chat_json
    return asked


def evidence(n=2, complete=True, kind="clip"):
    return {"evidence": kind, "frames": 48, "skipped": 0, "too_small": 0, "complete": complete,
            "persons": [{"crops": [b"jpg1", b"jpg2"], "times": [1.0, 2.0]} for _ in range(n)]}


def collector(ev):
    calls = []

    async def collect():
        calls.append(1)
        return ev
    collect.calls = calls
    return collect


TOWEL = {"held": ["black microfiber towel"], "most_weapon_like": "black microfiber towel", "weapon": "no", "confidence": "high"}
BOTTLE = {"held": ["water bottle"], "most_weapon_like": "none", "weapon": "no", "confidence": "high"}
GUN = {"held": ["black pistol"], "most_weapon_like": "black pistol", "weapon": "yes", "confidence": "high"}
UNSURE = {"held": ["dark object"], "most_weapon_like": "dark object", "weapon": "unclear", "confidence": "low"}


def test_review_towel_is_not_confirmed():
    asked = fake_model([BOTTLE, TOWEL])
    out, original = run(wc.review(EVENT, SYN_9118, collector(evidence(2))))
    w = out["weapon_check"]
    assert w["verdict"] == "not_confirmed" and not w["confirmed"] and not w["needs_review"]
    assert [p["object"] for p in w["persons"]] == ["water bottle", "black microfiber towel"]
    assert w["model"] == "fake-vl" and w["evidence"] == "clip" and w["persons_found"] == 2 and w["kind"] == "firearm"
    assert "holding a black microfiber towel." in out["summary"] and out["threat_level"] == "low"
    assert original == SYN_9118                                          # kept for synopsis_original
    assert len(asked) == 2 and all(a["task"] == wc.TASK and a["priority"] == "chat" and a["images"] == 2 for a in asked)
    assert "green corner marks" in asked[0]["text"]


def test_review_confirmed_stops_at_the_first_weapon():
    asked = fake_model([GUN, BOTTLE])
    out, original = run(wc.review(EVENT, SYN_9118, collector(evidence(3))))
    assert out["weapon_check"]["verdict"] == "confirmed" and out["weapon_check"]["confirmed"]
    assert out["threat_level"] == "high" and "holding a black handgun" in out["summary"] and "firearm" in out["tags"]
    assert original is None and len(asked) == 1


def test_review_unclear_stays_high():
    fake_model([BOTTLE, UNSURE])
    out, original = run(wc.review(EVENT, SYN_9118, collector(evidence(2))))
    w = out["weapon_check"]
    assert w["verdict"] == "unclear" and w["needs_review"] and "could not rule it out" in w["reason"]
    assert out["threat_level"] == "high" and "(possible firearm, unconfirmed)" in out["summary"] and original == SYN_9118
    # every "no", but only the stored crops (not everyone in view): never cleared
    fake_model([BOTTLE])
    out, _ = run(wc.review(EVENT, SYN_9118, collector(evidence(1, complete=False, kind="keyframes"))))
    assert out["weapon_check"]["verdict"] == "unclear" and out["threat_level"] == "high"


def test_review_timeout_is_unconfirmed_high_not_dropped():
    old = wc.TIMEOUT_S
    wc.TIMEOUT_S = 0.2
    try:
        fake_model([BOTTLE], delay=5)
        t0 = time.time()
        out, original = run(wc.review(EVENT, SYN_9118, collector(evidence(2))))
        assert time.time() - t0 < 3                                      # the time budget holds
    finally:
        wc.TIMEOUT_S = old
    w = out["weapon_check"]
    assert w["verdict"] == "error" and w["error"] == "timeout" and w["needs_review"] and "timed out" in w["reason"]
    assert out["threat_level"] == "high" and "(possible firearm, unconfirmed)" in out["summary"] and original == SYN_9118


def test_review_slow_evidence_is_bounded():
    old = wc.COLLECT_TIMEOUT_S
    wc.COLLECT_TIMEOUT_S = 0.2

    async def slow():
        await asyncio.sleep(5)
    try:
        asked = fake_model([BOTTLE])
        out, _ = run(wc.review(EVENT, SYN_9118, slow))                    # no stored crops either: nothing to look at
    finally:
        wc.COLLECT_TIMEOUT_S = old
    assert out["weapon_check"]["verdict"] == "error" and out["threat_level"] == "high" and asked == []
    assert "nothing to look at" in out["weapon_check"]["reason"]


def test_review_model_error_keeps_the_claim():
    fake_model(error=RuntimeError("no model for unusual_review"))
    out, original = run(wc.review(EVENT, SYN_9118, collector(evidence(1))))
    w = out["weapon_check"]
    assert w["verdict"] == "error" and "RuntimeError" in w["error"] and "model was unavailable" in w["reason"]
    assert out["threat_level"] == "high" and "possible_weapon" in out["tags"] and original == SYN_9118


def test_review_internal_failure_never_raises():
    def broken():
        raise ValueError("bug")
    fake_model([BOTTLE])
    orig = wc.check_claim

    async def boom(*a):
        raise ValueError("bug")
    wc.check_claim = boom
    wc.log.disabled = True   # (the expected traceback)
    try:
        out, original = run(wc.review(EVENT, SYN_9118, broken))
    finally:
        wc.check_claim = orig
        wc.log.disabled = False
    assert out["weapon_check"]["verdict"] == "error" and out["threat_level"] == "high" and original == SYN_9118


def test_no_weapon_passes_through_untouched():
    asked = fake_model([GUN])
    collect = collector(evidence(2))
    quiet = copy.deepcopy(QUIET)
    out, original = run(wc.review(EVENT, quiet, collect))
    assert out is quiet and out == QUIET and "weapon_check" not in out and original is None
    assert asked == [] and collect.calls == []                          # no model call, no clip decoded
    for text in ["He walked by with no weapon visible.", "Used a nail gun on the fence."]:
        out, original = run(wc.review(EVENT, {**QUIET, "summary": text}, collect))
        assert "weapon_check" not in out and original is None
    assert asked == [] and collect.calls == []


# ---------------------------------------------------------------- evidence from a real (synthetic) clip

class Boxes:
    def __init__(self, rows):
        a = np.array(rows, dtype=np.float32).reshape(-1, 6)
        self.xyxyn, self.conf, self.cls = a[:, :4], a[:, 4], a[:, 5]


class Res:
    def __init__(self, rows):
        self.boxes = Boxes(rows)


def fake_yolo(imgs):
    """'People' are solid red / blue / green rectangles; boxes from their pixels (like YOLO, normalized)."""
    out = []
    for img in imgs:
        h, w = img.shape[:2]
        rows = []
        for color in ((0, 0, 255), (255, 0, 0), (0, 255, 0)):
            mask = cv2.inRange(img, np.array(color) - 40, np.array(color) + 40)
            ys, xs = np.nonzero(mask)
            if len(xs) > 20:
                rows.append([xs.min() / w, ys.min() / h, (xs.max() + 1) / w, (ys.max() + 1) / h, 0.9, 0])
        out.append(Res(rows))
    return out


def make_clip(path: Path, small=False, seconds=8, fps=10):
    """Two 'people' who swap places (red walks right, blue walks left; they cross mid-clip), plus an optional third
    one far away (too small to judge)."""
    w, h = 640, 360
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    n = seconds * fps
    for i in range(n):
        img = np.full((h, w, 3), 90, np.uint8)
        x_red = int(100 + 380 * i / (n - 1))
        x_blue = int(480 - 380 * i / (n - 1))
        cv2.rectangle(img, (x_red, 60), (x_red + 60, 300), (0, 0, 255), -1)
        cv2.rectangle(img, (x_blue, 70), (x_blue + 60, 310), (255, 0, 0), -1)
        if small:
            cv2.rectangle(img, (20, 20), (30, 60), (0, 255, 0), -1)
        vw.write(img)
    vw.release()


def test_clip_evidence_follows_each_person():
    clip = DATA / "clip_two.mp4"
    make_clip(clip)
    ev = wc.clip_evidence(clip, fake_yolo)
    assert ev["evidence"] == "clip" and ev["frames"] == 16 and ev["complete"] and ev["skipped"] == 0
    assert len(ev["persons"]) == 2                                       # crossing did not swap or split them
    for per in ev["persons"]:
        assert len(per["crops"]) == 4 and per["times"] == sorted(per["times"]) and per["seen"] >= 15
        crops = [cv2.imdecode(np.frombuffer(c, np.uint8), cv2.IMREAD_COLOR) for c in per["crops"]]
        assert all(c is not None and max(c.shape[:2]) >= wc.CROP_MIN_PX for c in crops)
        mid = crops[0][crops[0].shape[0] // 2, crops[0].shape[1] // 2]
        colors = {("red" if mid[2] > 150 else "blue") for mid in
                  (c[c.shape[0] // 2, c.shape[1] // 2] for c in crops)}
        assert len(colors) == 1, "one track mixed two people"
        green = [(c[:, :, 1] > 180) & (c[:, :, 0] < 80) & (c[:, :, 2] < 80) for c in crops]
        assert all(g.sum() > 20 for g in green)                          # the corner marks are drawn


def test_too_small_or_cut_short_is_incomplete():
    clip = DATA / "clip_small.mp4"
    make_clip(clip, small=True)
    ev = wc.clip_evidence(clip, fake_yolo)
    assert len(ev["persons"]) == 2 and ev["too_small"] == 1 and not ev["complete"]
    assert "too small to judge" in wc._reason("unclear", "firearm", [], None, ev)
    ev = wc.clip_evidence(DATA / "clip_two.mp4", fake_yolo, deadline=time.time() - 1)
    assert ev["cut"] and not ev["complete"] and ev["frames"] == 0
    old = wc.MAX_PERSONS
    wc.MAX_PERSONS = 1
    try:
        ev = wc.clip_evidence(DATA / "clip_two.mp4", fake_yolo)
    finally:
        wc.MAX_PERSONS = old
    assert len(ev["persons"]) == 1 and ev["skipped"] == 1 and not ev["complete"]


def test_gather_evidence_falls_back():
    eid = 4242
    d = DATA / "events" / str(eid)
    d.mkdir(parents=True, exist_ok=True)
    img = np.full((360, 640, 3), 90, np.uint8)
    cv2.rectangle(img, (100, 60), (160, 300), (0, 0, 255), -1)
    cv2.imwrite(str(d / "snapshot.jpg"), img)
    (d / "crop_0.jpg").write_bytes(b"crop")
    e = {"id": eid, "clip": f"events/{eid}/clip.mp4", "snapshot": f"events/{eid}/snapshot.jpg",
         "detections": {"keyframes": [{"file": "crop_0.jpg", "kind": "crop"}, {"file": "wide.jpg", "kind": "wide"}]}}
    ev = wc.gather_evidence(e, fake_yolo)                                # no clip on disk: the snapshot
    assert ev["evidence"] == "snapshot" and len(ev["persons"]) == 1 and not ev["complete"]
    ev = wc.gather_evidence(e, None)                                     # no YOLO: the stored crops
    assert ev["evidence"] == "keyframes" and ev["persons"][0]["crops"] == [b"crop"] and not ev["complete"]
    import shutil
    shutil.copy(DATA / "clip_two.mp4", d / "clip.mp4")
    ev = wc.gather_evidence(e, fake_yolo)
    assert ev["evidence"] == "clip" and len(ev["persons"]) == 2 and ev["complete"]


# ---------------------------------------------------------------- the pipeline stores only the checked synopsis

def test_pipeline_stores_only_the_checked_synopsis():
    from nvr import mediamtx, synopsis as vlm
    from nvr.pipeline import Pipeline
    mediamtx.recording_spans = lambda *a, **k: []
    db.upsert_camera({"id": "cam3", "name": "Kitchen", "host": "10.0.0.5", "onvif_port": 80, "rtsp_port": 554,
                      "username": "u", "password": "p", "main_path": "/main", "sub_path": "/sub", "enabled": 1,
                      "zones": [], "retention_days": None, "scene_notes": "", "retention_policy": None, "policies": []})
    now = time.time()
    eid = db.create_event(camera_id="cam3", track_id="t9118", camera_class="person", camera_conf=0.9, start_ts=now - 30,
                          end_ts=now - 10, path=[[now - 30, 0.3, 0.3, 0.6, 0.7, 0.9], [now - 10, 0.4, 0.3, 0.7, 0.7, 0.9]],
                          status="verified")
    d = DATA / "events" / str(eid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "crop_0.jpg").write_bytes(b"crop")
    db.update_event(eid, detections={"keyframes": [{"file": "crop_0.jpg", "kind": "crop"}]})

    async def fake_synopsis(e, camera, images, examples):
        return {**copy.deepcopy(SYN_9118), "_model": "qwen3.8:27b"}

    async def no_embed(text):
        return None
    vlm.synopsis, vlm.embed = fake_synopsis, no_embed
    writes = []
    real_update = db.update_event

    def spy(event_id, **fields):
        if event_id == eid and "synopsis" in fields:
            writes.append(fields)
        return real_update(event_id, **fields)
    db.update_event = spy
    asked = fake_model([BOTTLE, TOWEL])
    p = Pipeline()
    p.weapon_evidence = lambda e: collector(evidence(2))()
    try:
        run(p._synopsis(eid))
    finally:
        db.update_event = real_update
    assert len(writes) == 1 and "handgun" not in writes[0]["synopsis"]  # the first and only write is already checked
    assert writes[0]["threat"] == "low" and len(asked) == 2
    e = db.event(eid)
    assert e["synopsis"].endswith("holding a black microfiber towel.") and e["threat"] == "low"
    assert e["synopsis_json"]["weapon_check"]["verdict"] == "not_confirmed"
    assert e["synopsis_original"]["summary"].endswith("holding a black handgun.")
    assert e["priority"] in (None, "none", "low")                       # no high-priority alert for the hub


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
