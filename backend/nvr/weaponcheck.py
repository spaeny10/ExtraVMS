"""A weapon in a synopsis needs a second, full-resolution look before it becomes a fact (and a HIGH alert).

Event 9118: the main model wrote "Another person ... in the foreground holding a black handgun", tagged it `firearm`
and rated the threat high, which made the event High Threat in the UI and a high-priority alert and push on the hub.
That man was not the tracked object: he was only in the downscaled wide frame, his hand about 50 px across, and he
was holding a black microfiber towel.

So, like policy.confirm_towing, a weapon claim is checked before it is stored or published:

1. claims(): the synopsis says firearm / gun / pistol / rifle / knife / weapon (summary, activity, threat reason,
   objects or tags), not counting tools ("nail gun", "glue gun", "heat gun", ...) or negations ("no weapons visible").
2. gather_evidence(): every person in the event at FULL resolution: frames from the event's clip at ~2 fps, YOLO
   (the verifier's model, on the GPU thread) for person boxes, people followed across frames by position AND clothing
   colors (two people who cross are not swapped), up to 4 padded crops (hands included, the person marked with green
   corners) per person, spread over time, largest first. Someone too small to judge, more than 4 people, or a clip
   that could not be read in time leaves the evidence incomplete. No clip: the snapshot and the stored crops (which
   can confirm a weapon but never clear one, since they do not show every person throughout).
3. One strict JSON question per person (TIMEOUT_S for all of them): everything they hold; the held object most like
   a gun or knife; is it a firearm or knife?
4. decide() + apply():
   - confirmed (someone holds a weapon, medium/high confidence): tags kept, summary kept with
     "Weapon confirmed by a second look at full resolution." appended, threat per kept_level() (a firearm: high;
     a knife: the model's own level, so a kitchen knife in use is no alarm);
   - not_confirmed (every person was seen and judged "no" with medium/high confidence and a plausible object): the
     weapon words leave the tags, the threat drops to what is left without the weapon, and the weapon phrase in the
     summary becomes the object ("holding a black towel"), or a note is appended when that can't be done safely;
   - unclear / error (timeout, model down, no evidence): NEVER downgraded. Threat per kept_level() (a firearm:
     high), the wording becomes "possible firearm, unconfirmed" (tag `possible_weapon`) and
     weapon_check.needs_review asks a person to look.
The outcome is synopsis_json["weapon_check"]; the model's own JSON is kept in events.synopsis_original when the text
was changed. The pipeline stores the synopsis only after this, so priority and hub publication see the checked text.
"""
from __future__ import annotations

import asyncio
import copy
import gc
import logging
import re
import time
from pathlib import Path
from typing import Awaitable, Callable

import cv2
import numpy as np

log = logging.getLogger("nvr.weaponcheck")

TASK = "unusual_review"      # the escalation task: the larger remote model when one is configured, else local
PRIORITY = "chat"            # ahead of queued background work: the event's alert waits for this answer
TIMEOUT_S = 120              # all model questions together; past it the claim stays (unconfirmed, high). The 27B
                             # takes ~4 s a person when idle, ~18 s behind other work (dry run on event 9118)
COLLECT_TIMEOUT_S = 45       # decoding the clip + YOLO
FPS = 2.0                    # clip frames per second looked at
MAX_FRAMES = 48              # at most this many frames per clip (spread evenly over a longer one)
BATCH = 8                    # frames per YOLO call (full-resolution frames are large: few in memory at once)
MAX_PERSONS = 4              # people asked about (largest first); more than this and the claim can't be cleared
CROPS_PER_PERSON = 4
MIN_PERSON_PX = 80           # a person shorter than this at full resolution is too small to judge (or to clear)
PERSON_CONF = 0.35
SMALL_CONF = 0.5             # a too-small person counts (the claim can't be cleared) from this YOLO confidence
MAX_GAP_S = 3.0              # a person unseen for longer than this starts a new track
SAME_PERSON = 0.55           # clothing-color similarity (histogram correlation) below this: not the same person
REJOIN_SIMILAR = 0.8         # two tracks never in view together and this alike: one person seen twice
CROP_MAX_PX, CROP_MIN_PX = 896, 448
LEVELS = ["none", "low", "medium", "high"]
RANK = {k: i for i, k in enumerate(LEVELS)}

# ---------------------------------------------------------------- the claim

_FIREARM = r"hand\s?guns?|pistols?|revolvers?|shotguns?|rifles?|firearms?|guns?"
_KNIFE = r"knife|knives|machetes?|daggers?"
NOUN_RE = rf"(?:{_FIREARM}|{_KNIFE}|weapons?)"
WEAPON_RE = re.compile(rf"\b(?:{NOUN_RE}|armed)\b", re.I)
FIREARM_RE = re.compile(rf"^(?:{_FIREARM})$", re.I)
KNIFE_RE = re.compile(rf"^(?:{_KNIFE})$", re.I)
# "nail gun", "hot glue gun", "spray-gun", "putty knife": tools, not weapons ("toy gun" is still checked: at a
# distance a toy and a real gun look alike, and the second look is what tells them apart)
TOOL_BEFORE = re.compile(
    r"\b(?:nail|glue|heat|spray|paint|staple|stapler|caulk|caulking|grease|tape|price|label|labeling|labelling|radar|"
    r"speed|massage|water|squirt|nerf|foam|solder|soldering|rivet|impact|temp|temperature|thermometer|infrared|"
    r"scan|scanner|barcode|bubble|putty|palette|butter|tire|drywall|screw|cement|smoothing)[\s-]+$", re.I)
# "gun-metal gray", "gun safe", "rifles through the glovebox": not a weapon being held
NOT_AFTER = re.compile(r"^(?:[\s-]*(?:metal|safe|safes|store|shop|range|show)\b|\s+(?:through|around)\b)", re.I)
NEGATION = re.compile(r"\b(?:no|not|without|never|nor|none|neither|nothing|cannot|unarmed|lack|lacks|lacking)\b|n't\b|"
                      r"\bfree of\b|\babsence of\b", re.I)
NEG_AFTER = re.compile(r"^\W*(?:or\s+\w+\s+)?(?:is|are|was|were)\s+(?:not\b|\w+n't\b)", re.I)
# a verb of holding after the negation starts a new statement ("with no hurry, holding a handgun", "a man with no hat
# carrying a knife") unless only these words stand between ("not holding", "does not appear to be holding a weapon")
HOLD_RE = re.compile(r"\b(?:holding|holds|held|carrying|carries|carried|wielding|wields|brandishing|brandishes|"
                     r"gripping|grips|clutching|clutches|pointing|points|aiming|aims)\b", re.I)
NEG_GLUE = {"be", "been", "being", "is", "are", "was", "were", "appear", "appears", "appeared", "seem", "seems", "seemed",
            "to", "visibly", "seen", "currently", "clearly", "actually", "obviously", "apparently", "any", "a", "an",
            "the", "one", "person", "people", "individual", "individuals", "anyone", "anybody", "someone", "else",
            "man", "woman", "sign", "signs", "evidence", "indication", "that", "he", "she", "they", "it", "of",
            "observed", "visible", "shown", "can", "could", "found", "detected", "do", "does", "did"}


def _negated(before: str) -> bool:
    """Does a negation in the words just before a weapon word (same clause) apply to it?"""
    words = before.split()[-12:]
    window = " ".join(words)
    negs = list(NEGATION.finditer(window))
    if not negs:
        return False
    after = window[negs[-1].end():]
    if re.search(r"[,:]|\b(?:but|and|while|then|who)\b", after) and HOLD_RE.search(after):
        return False   # "with no hurry, holding a ..." / "no bag but holding a ..."
    hold = HOLD_RE.search(after)
    if hold and any(re.sub(r"\W", "", w).lower() not in NEG_GLUE for w in after[:hold.start()].split()):
        return False   # "a man with no hat holding a ...": the negation was about the hat
    return True


def mentions(text: str | None) -> list[re.Match]:
    """Weapon words in the text that are a claim: not a tool ("nail gun"), not negated ("no weapons visible")."""
    text = text or ""
    out = []
    for m in WEAPON_RE.finditer(text):
        clause = re.split(r"[.;!?]", text[:m.start()])[-1]
        if TOOL_BEFORE.search(clause[-30:]) or NOT_AFTER.search(text[m.end():m.end() + 12]):
            continue
        if _negated(clause) or NEG_AFTER.search(text[m.end():m.end() + 40]):
            continue
        if m.group(0).lower() == "armed" and re.search(r"alarm|system|disarmed|re-armed", " ".join(clause.split()[-5:])
                                                       + text[m.end():m.end() + 20], re.I):
            continue
        out.append(m)
    return out


def is_weapon_tag(tag: str) -> bool:
    return bool(mentions(re.sub(r"[_\-]+", " ", str(tag))))


def claims(s: dict | None) -> list[str]:
    """The weapon words a synopsis claims (lowercase, de-duplicated); [] when it claims none."""
    s = s or {}
    texts = [s.get("summary"), s.get("activity"), s.get("threat_reason")]
    for o in s.get("objects") or []:
        if isinstance(o, dict):
            texts += [o.get("type"), o.get("description")]
    found = [m.group(0).lower() for t in texts if isinstance(t, str) for m in mentions(t)]
    found += [str(t).lower() for t in s.get("tags") or [] if is_weapon_tag(t)]
    return list(dict.fromkeys(found))


def claim_kind(words: list[str]) -> str:
    """'firearm', 'knife' or 'weapon' (the word used in "possible firearm, unconfirmed")."""
    toks = [w for p in words for w in re.findall(r"[a-z]+(?:\s?guns?)?", p)]
    if any(FIREARM_RE.match(w) for w in toks):
        return "firearm"
    if any(KNIFE_RE.match(w) for w in toks):
        return "knife"
    return "weapon"


def _noun_kind(noun: str) -> str:
    return "firearm" if FIREARM_RE.match(noun) else "knife" if KNIFE_RE.match(noun) else "weapon"


# ---------------------------------------------------------------- evidence: every person, full resolution

def yolo_predict(model, images: list) -> list:
    """Person boxes on full-resolution frames with the verifier's YOLO (ultralytics or the Hailo supervisor). GPU thread."""
    from .config import settings
    return model.predict(images, imgsz=settings.yolo_imgsz, conf=settings.yolo_conf, device=settings.yolo_device,
                         verbose=False, classes=[0])


def person_crop(img: np.ndarray, box: list[float]) -> bytes:
    """The person (normalized [l, t, r, b]) padded sideways for outstretched arms and what is in the hands, with
    green corner marks on the person's box: the padding often shows someone else too (event 9118: the other man's
    water bottle), and the model must answer for the marked person. Corners only, so nothing in a hand is covered."""
    h, w = img.shape[:2]
    l, t, r, b = box
    bw, bh = r - l, b - t
    x1, x2 = max(0, int((l - bw * 0.35) * w)), min(w, int((r + bw * 0.35) * w))
    y1, y2 = max(0, int((t - bh * 0.08) * h)), min(h, int((b + bh * 0.08) * h))
    crop = img[y1:max(y2, y1 + 2), x1:max(x2, x1 + 2)]
    longest = max(crop.shape[:2])
    s = CROP_MAX_PX / longest if longest > CROP_MAX_PX else CROP_MIN_PX / longest if longest < CROP_MIN_PX else 1.0
    if s != 1.0:
        crop = cv2.resize(crop, None, fx=s, fy=s, interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
    else:
        crop = crop.copy()   # (a view of the frame otherwise: the marks must not land on it)
    ch, cw = crop.shape[:2]
    bx1, by1 = int((l * w - x1) * cw / max(1, x2 - x1)), int((t * h - y1) * ch / max(1, y2 - y1))
    bx2, by2 = int((r * w - x1) * cw / max(1, x2 - x1)), int((b * h - y1) * ch / max(1, y2 - y1))
    arm, th = max(8, int(0.12 * min(bx2 - bx1, by2 - by1))), max(2, round(max(ch, cw) / 300))
    for (x, y), (dx, dy) in (((bx1, by1), (1, 1)), ((bx2, by1), (-1, 1)), ((bx1, by2), (1, -1)), ((bx2, by2), (-1, -1))):
        cv2.line(crop, (x, y), (x + dx * arm, y), (0, 220, 0), th)
        cv2.line(crop, (x, y), (x, y + dy * arm), (0, 220, 0), th)
    return cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()


def appearance(img: np.ndarray, box: list[float]) -> np.ndarray:
    """A small hue/saturation histogram of the middle of the person's box (clothing colors), to tell two people
    apart where their boxes meet."""
    h, w = img.shape[:2]
    l, t, r, b = box
    bw, bh = r - l, b - t
    x1, x2 = int((l + bw * 0.2) * w), int((r - bw * 0.2) * w)
    y1, y2 = int((t + bh * 0.05) * h), int((b - bh * 0.05) * h)
    reg = img[max(0, y1):max(y2, y1 + 2), max(0, x1):max(x2, x1 + 2)]
    reg = cv2.resize(reg, (32, 64), interpolation=cv2.INTER_AREA)
    hist = cv2.calcHist([cv2.cvtColor(reg, cv2.COLOR_BGR2HSV)], [0, 1], None, [12, 8], [0, 180, 0, 256])
    return cv2.normalize(hist, hist).flatten()


def _similar(d: dict, track: list[dict]) -> float:
    """How much detection `d` looks like the track's last few detections (1.0 without histograms, e.g. in tests).
    Several, not just the last: one box half covered by someone walking past must not set the track's look."""
    hs = [x["hist"] for x in track[-5:] if x.get("hist") is not None]
    if d.get("hist") is None or not hs:
        return 1.0
    return float(cv2.compareHist(d["hist"], np.mean(hs, axis=0).astype(np.float32), cv2.HISTCMP_CORREL))


def _predicted(track: list[dict], t: float) -> list[float]:
    """Where the track's box should be at time t, moving as it last moved (people who cross keep their direction)."""
    last = track[-1]
    if len(track) < 2 or t <= last["t"]:
        return last["box"]
    prev = track[-2]
    dt = last["t"] - prev["t"]
    if dt <= 0:
        return last["box"]
    k = min(t - last["t"], 2.0) / dt
    vx = ((last["box"][0] + last["box"][2]) - (prev["box"][0] + prev["box"][2])) / 2
    vy = ((last["box"][1] + last["box"][3]) - (prev["box"][1] + prev["box"][3])) / 2
    l, tp, r, b = last["box"]
    return [l + vx * k, tp + vy * k, r + vx * k, b + vy * k]


def _detect(predict: Callable[[list], list], imgs: list[tuple[float, np.ndarray]]) -> list[dict]:
    """YOLO on (time, frame) pairs -> person detections with their full-resolution crops."""
    if not imgs:
        return []
    out = []
    for (t, img), res in zip(imgs, predict([i for _, i in imgs])):
        h = img.shape[0]
        for b, c, p in zip(res.boxes.xyxyn.tolist(), res.boxes.cls.tolist(), res.boxes.conf.tolist()):
            if int(c) != 0 or float(p) < PERSON_CONF:
                continue
            box = [float(v) for v in b]
            px_h = int((box[3] - box[1]) * h)
            small = px_h < MIN_PERSON_PX   # kept (so the person counts) but never cropped or asked about
            out.append({"t": round(float(t), 2), "box": box, "conf": float(p), "px_h": px_h, "small": small,
                        "area": (box[2] - box[0]) * (box[3] - box[1]), "hist": appearance(img, box),
                        "crop": None if small else person_crop(img, box)})
    return out


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _near(a, b, k: float = 1.0) -> bool:
    """Box centers within 0.6 widths sideways and 0.4 heights up/down (times k)."""
    ca, cb = ((a[0] + a[2]) / 2, (a[1] + a[3]) / 2), ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
    return (abs(ca[0] - cb[0]) < 0.6 * k * max(a[2] - a[0], b[2] - b[0])
            and abs(ca[1] - cb[1]) < 0.4 * k * max(a[3] - a[1], b[3] - b[1]))


def group_persons(dets: list[dict], frames: int, gap: float = MAX_GAP_S) -> list[list[dict]]:
    """Follow people across the sampled frames: a detection continues the track it overlaps (or is near) where it
    was last seen or was heading, within `gap` seconds, AND looks like (clothing colors); best pairs first, so two
    people who cross are not swapped (event 9118: one man took over the other's place at the tablet). A person
    split in two (half hidden for a moment, or turned around) is joined again (_rejoin). One list per person,
    largest first. A one-frame, low-confidence box among many frames is dropped as noise."""
    tracks: list[list[dict]] = []
    for t in sorted({d["t"] for d in dets}):
        now = [d for d in dets if d["t"] == t]
        pairs = []
        for j, d in enumerate(now):
            for i, tr in enumerate(tracks):
                if t - tr[-1]["t"] > gap:
                    continue
                pred = _predicted(tr, t)
                iou = max(_iou(d["box"], pred), _iou(d["box"], tr[-1]["box"]))
                if iou < 0.15 and not _near(d["box"], pred) and not _near(d["box"], tr[-1]["box"]):
                    continue
                sim = _similar(d, tr)
                if sim < SAME_PERSON:
                    continue
                pairs.append((sim + 0.5 * iou, j, i))
        taken_d, taken_t = set(), set()
        for _, j, i in sorted(pairs, reverse=True):
            if j in taken_d or i in taken_t:
                continue
            tracks[i].append(now[j])
            taken_d.add(j)
            taken_t.add(i)
        for j, d in enumerate(now):
            if j not in taken_d:
                tracks.append([d])
    tracks = _rejoin(tracks, gap)
    keep = [tr for tr in tracks if len(tr) >= 2 or frames < 4 or tr[0]["conf"] >= 0.6]
    return sorted(keep, key=lambda tr: -max(d["area"] for d in tr))


def _fills_gap(frag: list[dict], track: list[dict]) -> bool:
    """A frame or two (`frag`) that sit right where `track` was between its neighboring frames: the same person in
    frames where their box was half covered (someone walked in front) and looked different."""
    if len(frag) > 2 or len(track) < 2:
        return False
    for d in frag:
        before = [x for x in track if x["t"] < d["t"]]
        after = [x for x in track if x["t"] > d["t"]]
        if not before or not after:
            return False
        a, b = before[-1], after[0]
        k = (d["t"] - a["t"]) / max(1e-6, b["t"] - a["t"])
        box = [a["box"][i] + (b["box"][i] - a["box"][i]) * k for i in range(4)]
        if _iou(d["box"], box) < 0.3 and not _near(d["box"], box):
            return False
    return True


def _mean_hist(tr: list[dict]) -> np.ndarray | None:
    hs = [d["hist"] for d in tr if d.get("hist") is not None]
    return np.mean(hs, axis=0).astype(np.float32) if hs else None


def _continues(a: list[dict], b: list[dict], gap: float) -> bool:
    """`b` picks up where `a` left off: starts within `gap` after a's end, where a was heading, and looks like a did
    at the end (event 9118: the man who turned from the tablet and walked toward the camera looked different from
    behind, and his track broke twice)."""
    if not a or not b or not 0 < b[0]["t"] - a[-1]["t"] <= gap:
        return False
    pred = _predicted(a, b[0]["t"])   # (a little looser than frame to frame: the last box may have been half hidden)
    iou = _iou(b[0]["box"], pred)
    if not (iou >= 0.15 or _near(b[0]["box"], pred, 1.5) or _near(b[0]["box"], a[-1]["box"], 1.5)):
        return False
    ha, hb = _mean_hist(a[-3:]), _mean_hist(b[:3])
    # exactly where a was heading: the look may change more (walking out of view, turning toward the camera)
    return ha is None or hb is None or cv2.compareHist(ha, hb, cv2.HISTCMP_CORREL) >= (0.3 if iou >= 0.5 else SAME_PERSON)


def _rejoin(tracks: list[list[dict]], gap: float = MAX_GAP_S) -> list[list[dict]]:
    """Join two tracks of one person, never in the same frame: one continues the other (_continues), one fills a
    frame or two the other missed (_fills_gap: someone walked in front), or the two look alike overall."""
    tracks = [sorted(tr, key=lambda d: d["t"]) for tr in tracks]
    joined = True
    while joined:
        joined = False
        for a in range(len(tracks)):
            for b in range(a + 1, len(tracks)):
                ta, tb = tracks[a], tracks[b]
                if {d["t"] for d in ta} & {d["t"] for d in tb}:
                    continue   # both in one frame: two people
                ha, hb = _mean_hist(ta), _mean_hist(tb)
                alike = ha is not None and hb is not None and cv2.compareHist(ha, hb, cv2.HISTCMP_CORREL) >= REJOIN_SIMILAR
                if not (alike or _fills_gap(ta, tb) or _fills_gap(tb, ta) or _continues(ta, tb, gap)
                        or _continues(tb, ta, gap)):
                    continue
                tracks[a] = sorted(ta + tb, key=lambda d: d["t"])
                del tracks[b]
                joined = True
                break
            if joined:
                break
    return tracks


def pick_crops(track: list[dict], n: int = CROPS_PER_PERSON) -> list[dict]:
    """Up to n detections spread over the person's time in view, the largest / most certain of each stretch (only
    detections big enough to judge)."""
    tr = sorted((d for d in track if not d.get("small")), key=lambda d: d["t"])
    bins = [tr[int(i * len(tr) / n):int((i + 1) * len(tr) / n)] for i in range(n)]
    return [max(b, key=lambda d: d["area"] * d["conf"]) for b in bins if b]


def _persons(dets: list[dict], frames: int, gap: float = MAX_GAP_S) -> tuple[list[dict], int, int]:
    """(people to ask about, people left out because there were too many, people too small to judge). A person
    only ever seen smaller than MIN_PERSON_PX can't be asked about, and can't be ruled out either: the claim may
    have been about them (counted unless it is a faint, passing box)."""
    tracks = group_persons(dets, frames, gap)
    judged = [tr for tr in tracks if any(not d.get("small") for d in tr)]
    small = [tr for tr in tracks if all(d.get("small") for d in tr)
             and (len(tr) >= 2 or frames < 4) and max(d["conf"] for d in tr) >= SMALL_CONF]
    persons = []
    for tr in judged[:MAX_PERSONS]:
        picks = pick_crops(tr)
        persons.append({"crops": [d["crop"] for d in picks], "times": [d["t"] for d in picks],
                        "px_h": max(d["px_h"] for d in tr), "seen": len(tr)})
    return persons, max(0, len(judged) - MAX_PERSONS), len(small)


def clip_evidence(clip: Path, predict: Callable[[list], list], deadline: float | None = None) -> dict:
    """Frames at ~FPS across the clip (PyAV, like verifier.grab_frames), YOLO in small batches, crops kept as JPEG.
    Past `deadline` it stops early with what it has (then incomplete: it can confirm, not clear)."""
    import av
    dets: list[dict] = []
    batch: list[tuple[float, np.ndarray]] = []
    n, step, cut = 0, 1 / FPS, False
    try:
        with av.open(str(clip)) as container:
            stream = container.streams.video[0]
            stream.thread_type = "AUTO"
            dur = float(stream.duration * stream.time_base) if stream.duration else (
                container.duration / 1e6 if container.duration else MAX_FRAMES / FPS)
            count = max(1, min(MAX_FRAMES, int(dur * FPS)))
            targets = [dur * (i + 0.5) / count for i in range(count)]
            step = dur / count
            k, t0 = 0, None
            for frame in container.decode(stream):
                if k >= len(targets):
                    break
                if deadline is not None and time.time() > deadline:
                    cut = True
                    break
                if frame.time is None:
                    continue
                t0 = frame.time if t0 is None else t0   # a clip whose first frame isn't at 0
                t = frame.time - t0
                if t + 1e-3 < targets[k]:
                    continue
                while k < len(targets) and targets[k] <= t + 1e-3:
                    k += 1
                batch.append((t, frame.to_ndarray(format="bgr24")))
                n += 1
                if len(batch) >= BATCH:
                    dets += _detect(predict, batch)
                    batch = []
            dets += _detect(predict, batch)
            batch = []
    finally:
        gc.collect()   # PyAV frames are freed late (see verifier.grab_frames)
    # a long clip is sampled more sparsely: a person may move further between two looked-at frames
    persons, skipped, small = _persons(dets, n, max(MAX_GAP_S, 2.5 * step))
    return {"evidence": "clip", "frames": n, "persons": persons, "skipped": skipped, "too_small": small, "cut": cut,
            "complete": n > 0 and not cut and skipped == 0 and small == 0}


def image_evidence(img: np.ndarray, predict: Callable[[list], list]) -> dict:
    persons, skipped, small = _persons(_detect(predict, [(0.0, img)]), 1)
    return {"evidence": "snapshot", "frames": 1, "persons": persons, "skipped": skipped, "too_small": small,
            "complete": False}


def keyframe_evidence(e: dict) -> dict:
    """The stored crops of the tracked object (no YOLO needed): enough to confirm a weapon, never to clear one."""
    from .config import settings
    d = settings.data_dir / "events" / str(e["id"])
    kf = (e.get("detections") or {}).get("keyframes") or []
    crops = [(d / k["file"]).read_bytes() for k in kf if k.get("kind") == "crop" and (d / k["file"]).exists()][:CROPS_PER_PERSON]
    return {"evidence": "keyframes", "frames": 0, "persons": [{"crops": crops, "times": [], "tracked": True}] if crops else [],
            "skipped": 0, "complete": False}


def gather_evidence(e: dict, predict: Callable[[list], list] | None, deadline: float | None = None) -> dict:
    """Blocking (decode thread): the clip if there is one, else the snapshot, else the stored crops. Stops decoding
    at `deadline` (default: a little inside COLLECT_TIMEOUT_S, so the thread doesn't run on after the wait gave up)."""
    from .config import settings
    deadline = deadline if deadline is not None else time.time() + COLLECT_TIMEOUT_S - 5
    if predict is not None:
        clip = settings.data_dir / (e.get("clip") or "")
        if e.get("clip") and clip.is_file():
            try:
                ev = clip_evidence(clip, predict, deadline)
                if ev["persons"]:
                    return ev
                log.info("event %s: weapon check found nobody in the clip; trying the snapshot and the stored crops", e["id"])
            except Exception as ex:  # noqa: BLE001 - fall back to what else there is
                log.warning("event %s: weapon check could not read the clip (%s: %s)", e["id"], type(ex).__name__, ex)
        snap = settings.data_dir / (e.get("snapshot") or "")
        if e.get("snapshot") and snap.is_file():
            img = cv2.imread(str(snap))
            if img is not None:
                ev = image_evidence(img, predict)
                if ev["persons"]:
                    return ev
    return keyframe_evidence(e)


# ---------------------------------------------------------------- the question

# Everything held, not one object: in event 9118 the man held a red bottle at one moment and the black towel (the
# "handgun") at another, and a single-object answer named only the bottle.
SCHEMA = {
    "type": "object",
    "properties": {
        "held": {"type": "array", "items": {"type": "string"},
                 "description": "everything the marked person holds or carries in their hands across the pictures, a "
                                "few words each with its color (e.g. 'red water bottle', 'black cloth'); empty if nothing"},
        "most_weapon_like": {"type": "string",
                             "description": "of those, the one that could most easily be mistaken for a gun or knife "
                                            "in a small, blurry picture (as named in held), or 'none'"},
        "weapon": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["held", "most_weapon_like", "weapon", "confidence"],
}
SYSTEM = (
    "You double-check one claim from a security camera system: that a person is holding a weapon. Look only at what "
    "the marked person is holding or carrying, in every picture. Many objects look like a gun or knife at low "
    "resolution (phones, towels, cloths, tools, cups, bottles, headphones, bags, glue guns, drills). Answer weapon=yes "
    "only when you can see a firearm or knife (barrel, slide, trigger guard, grip, blade); weapon=no when you can see "
    "that everything held is something else, or the hands are empty; weapon=unclear when you cannot tell. "
    "confidence: low, medium or high. Use American English."
)


def question(person: dict) -> str:
    n = len(person["crops"])
    what = (f"These {n} close-ups show the same person at full camera resolution, in time order; the person is "
            "marked by green corner marks." if n > 1
            else "This close-up shows one person at full camera resolution, marked by green corner marks.")
    return (f"{what} An automatic description said someone in this scene may be holding a weapon. Look only at the "
            "marked person's hands and what they hold or carry (not at anyone else in the picture). Is it a firearm "
            "or knife? Answer as JSON.")


VAGUE = {"", "unknown", "unclear", "object", "an object", "item", "something", "n/a", "na", "none visible", "dark object",
         "black object", "small object", "unidentified object", "unknown object"}
EMPTY = {"nothing", "none", "empty", "empty hands", "nothing visible", "no object", "hands empty"}


def clean_object(raw) -> str:
    s = re.sub(r"\s+", " ", str(raw or "")).strip().strip(".").lower()
    s = re.sub(r"^(?:a|an|the|possibly|likely|probably|maybe)\s+", "", s)
    return s[:60]


def judge_person(ans: dict) -> dict:
    """The model's answer for one person, normalized. `object` is what the claim most likely was: the held object
    most like a weapon, else the only thing held, else 'nothing' (a vague or missing answer stays vague: unclear)."""
    lower = lambda k, ok, dflt: (lambda v: v if v in ok else dflt)(str(ans.get(k, "")).strip().lower())  # noqa: E731
    raw = ans.get("held")
    held = [clean_object(h) for h in (raw if isinstance(raw, list) else [raw] if raw else [])]
    held = [h for h in dict.fromkeys(held) if h and h not in EMPTY]
    like = clean_object(ans.get("most_weapon_like", ans.get("object")))
    if like in EMPTY or like in ("none", "n/a", "na"):
        like = ""
    obj = like or (held[0] if held else "nothing")
    return {"object": obj, "held": held[:6],
            "weapon": lower("weapon", ("yes", "no", "unclear"), "unclear"),
            "confidence": lower("confidence", ("low", "medium", "high"), "low"),
            "held_by": "hand" if held else "none",
            "resembles_weapon": "yes" if like else "no",
            "model": ans.get("_model")}


def plausible(p: dict) -> bool:
    """A "no" names what it is instead: something concrete that is not itself a weapon, or empty hands (and
    nothing else held is a weapon either)."""
    obj = p["object"]
    return (obj in EMPTY or (obj not in VAGUE and not mentions(obj))) and not any(mentions(h) for h in p.get("held") or [])


def is_confirmed(p: dict) -> bool:
    return p["weapon"] == "yes" and p["confidence"] in ("medium", "high")


def decide(persons: list[dict], expected: int, complete: bool, error: str | None) -> str:
    """confirmed | not_confirmed | unclear | error (the last two keep the claim, unconfirmed, at high priority)."""
    if any(is_confirmed(p) for p in persons):
        return "confirmed"
    if error and not persons:
        return "error"
    if (complete and not error and persons and len(persons) == expected
            and all(p["weapon"] == "no" and p["confidence"] in ("medium", "high") and plausible(p) for p in persons)):
        return "not_confirmed"
    return "error" if error else "unclear"


# ---------------------------------------------------------------- rewriting the claim

COLORS = {"black", "white", "gray", "grey", "silver", "red", "blue", "green", "yellow", "orange", "brown", "tan", "dark",
          "pink", "purple", "beige", "navy", "gold", "light"}
SIZES = {"small", "large", "big", "long", "short", "little"}
FILLER = {"and", "or", "of", "in", "on", "at", "to", "with", "his", "her", "their", "its", "is", "was", "that", "which",
          "what", "a", "an", "the", "but", "while", "who"}
# up to three adjectives before the noun ("a small black handgun"), never a filler word ("a man with a gun")
ADJ = rf"(?:(?!(?:{'|'.join(sorted(FILLER))})\b)[a-z]+(?:-[a-z]+)?\s+)"
LEAD = (r"(?:holding|holds|held|carrying|carries|carried|wielding|wields|brandishing|brandishes|gripping|grips|"
        r"clutching|clutches|pointing|points|aiming|aims|with|has|having)")
# "holding (what appears to be) a black handgun", or just "the handgun": replaced from the hedge / article on
PHRASE_RE = re.compile(
    rf"(?:\b{LEAD}\s+(?:up\s+)?)?(?P<hedge>what\s+(?:appears|looks|seems)\s+to\s+be\s+|what\s+looks\s+like\s+)?"
    rf"(?P<art>\b(?:an?|the|his|her|their))\s+(?P<adj>{ADJ}{{0,3}}?)(?P<noun>{NOUN_RE})\b", re.I)
# "a dark object, possibly a handgun" (the wording the synopsis prompt asks for when it is not sure)
MAYBE_RE = re.compile(
    rf"(?P<lead>,?\s*\b(?:possibly|perhaps|maybe|likely|probably)\s+)(?P<art>an?)\s+"
    rf"(?P<adj>{ADJ}{{0,3}}?)(?P<noun>{NOUN_RE})\b", re.I)


def _article(art: str, phrase: str) -> str:
    if art.lower() in ("a", "an"):
        a = "an" if phrase[:1].lower() in "aeiou" else "a"
        return f"{a.capitalize() if art[:1].isupper() else a} {phrase}"
    return f"{art} {phrase}"


def rewrite(text: str, repl: Callable[[re.Match], str]) -> str | None:
    """Replace every weapon claim in `text` through repl(match of PHRASE_RE or MAYBE_RE), or None when one of them
    is not in a simple "holding a black handgun" / "the pistol" phrase (then the caller adds a note instead)."""
    ms = mentions(text)
    if not ms:
        return text
    covered = {}
    for rx in (MAYBE_RE, PHRASE_RE):   # "a dark object, possibly a handgun": the whole aside, not just "a handgun"
        for pm in rx.finditer(text):
            if any(WEAPON_RE.fullmatch(w) for w in pm.group("adj").split()):
                continue
            covered.setdefault(pm.span("noun"), pm)
    if any(m.span() not in covered for m in ms):
        return None
    out, pos = [], 0
    for m in ms:
        pm = covered[m.span()]
        # "holding a black handgun": from the article on; "what appears to be a handgun": from the hedge on;
        # ", possibly a handgun": the whole aside
        start = (pm.start("lead") if pm.re is MAYBE_RE else
                 pm.start("hedge") if pm.group("hedge") else pm.start("art"))
        out += [text[pos:start], repl(pm)]
        pos = pm.end("noun")
        if pm.re is MAYBE_RE and pm.group("lead").lstrip().startswith(",") and text[pos:pos + 1] == ",":
            pos += 1   # the aside's closing comma goes with it

    return "".join(out) + text[pos:]


def as_object(obj: str | None) -> Callable[[re.Match], str]:
    """"holding a black handgun" -> "holding a black towel" (the color kept unless the object names its own); with
    no one object to name (two people, two different things): "holding a black object (not a weapon)"."""
    def f(pm: re.Match) -> str:
        colors = [w for w in pm.group("adj").split() if w.lower() in COLORS]
        if obj is None:
            phrase = " ".join([w for w in pm.group("adj").split() if w.lower() in COLORS | SIZES] + ["object (not a weapon)"])
            if pm.re is MAYBE_RE:   # "a dark object, possibly a handgun" -> "a dark object (not a weapon)"
                return " (not a weapon)"
        else:
            phrase = obj if any(w in COLORS for w in obj.split()) or not colors else " ".join(colors + [obj])
        if pm.re is MAYBE_RE:   # "a dark object, possibly a handgun" -> "a dark object (a black towel)"
            return f" ({_article('a', phrase)})"
        art = pm.group("art")
        return _article("a" if pm.group("hedge") and art.lower() in ("a", "an") else art, phrase)
    return f


def as_unconfirmed(pm: re.Match) -> str:
    """"holding a black handgun" -> "holding a black object (possible firearm, unconfirmed)";
    "a dark object, possibly a handgun" -> "a dark object (possible firearm, unconfirmed)"."""
    note = f"(possible {_noun_kind(pm.group('noun'))}, unconfirmed)"
    if pm.re is MAYBE_RE:
        return f" {note}"
    # only colors and sizes stay ("a possible handgun" -> "an object", not "a possible object")
    phrase = " ".join([w for w in pm.group("adj").split() if w.lower() in COLORS | SIZES] + ["object"])
    return f"{_article(pm.group('art'), phrase)} {note}"


def _claim_colors(text: str) -> set[str]:
    """The colors said of the weapon itself ("a black handgun" -> {"black"})."""
    out = set()
    for m in mentions(text):
        before = re.split(r"[.;!?,]", text[:m.start()])[-1].split()[-3:]
        out |= {w.lower() for w in before if w.lower() in COLORS}
    return out


def _object_for_claim(persons: list[dict], text: str) -> str | None:
    """The one object the claim most likely was: the person whose object could pass for a weapon; ties broken by the
    claim's colour; None when it stays ambiguous (or every hand was empty)."""
    held = [p for p in persons if p["object"] not in EMPTY and plausible(p)]
    if not held:
        return None
    for group in ([p for p in held if p["resembles_weapon"] == "yes"], held):
        objs = list(dict.fromkeys(p["object"] for p in group))
        if len(objs) == 1:
            return objs[0]
        if len(objs) > 1:
            colors = _claim_colors(text)
            hit = [o for o in objs if set(o.split()) & colors]
            if len(hit) == 1:
                return hit[0]
            return None
    return None


def _notable(e: dict) -> str | None:
    """Besides the weapon, is the event worth a medium? (unusual for the camera, a site rule, PPE, the watch list)"""
    pol = e.get("policy")
    if isinstance(pol, dict) and pol.get("text"):
        return pol["text"]
    ppe = (e.get("detections") or {}).get("ppe") or {}
    if ppe.get("verdict") == "violation":
        return "PPE violation"
    if e.get("watched"):
        return f"on the watch list ({e['watched']})"
    reasons = (e.get("anomaly_json") or {}).get("reasons") or []
    return "; ".join(reasons) if reasons else None


SUSPICIOUS_RE = re.compile(
    r"\b(?:loiter\w*|conceal\w*|mask(?:ed)?|balaclava|hood(?:ed|ie)? (?:up|over)|trying (?:the )?(?:door|handle)s?|"
    r"checking (?:car |vehicle |the )?(?:door|handle)s?|pry\w*|forc\w+ (?:entry|open)|break\w* in|broke in|"
    r"trespass\w*|steal\w*|stole|theft|vandal\w*|tamper\w*|fight\w*|struggl\w*|threaten\w*)\b", re.I)


def _suspicious(text: str) -> str | None:
    """Something else suspicious in the description (keeps a cleared weapon claim at medium rather than low)."""
    m = SUSPICIOUS_RE.search(text or "")
    return f"The description also mentions '{m.group(0).lower()}'" if m else None


def kept_level(level: str | None, kind: str) -> str:
    """The threat while a weapon claim stands (confirmed, or not ruled out): never below the model's own, and HIGH
    for a firearm. A knife keeps the model's own level: a kitchen knife cutting a pastry (events 7366, 5474) is a
    knife, not a threat, and the model already judged it in context."""
    level = level if level in RANK else "none"
    return "high" if kind == "firearm" else level


def apply(e: dict, result: dict, check: dict) -> dict:
    """The synopsis with the check's outcome applied (a new dict; `result` is left as the model wrote it)."""
    out = copy.deepcopy(result)
    out["weapon_check"] = check
    verdict, kind = check["verdict"], check["kind"]
    summary = out.get("summary") or ""
    tags = [t for t in out.get("tags") or []]
    level = out.get("threat_level") or "none"
    if verdict == "confirmed":
        out["summary"] = (summary.rstrip() + " " if summary.strip() else "") + "Weapon confirmed by a second look at full resolution."
        out["threat_level"] = kept_level(level, kind)
        if not (out.get("threat_reason") or "").strip():
            out["threat_reason"] = check["reason"]
        return out
    out["tags"] = [t for t in tags if not is_weapon_tag(t)]
    if verdict == "not_confirmed":
        obj = _object_for_claim(check["persons"], summary)
        new = rewrite(summary, as_object(obj))
        if new is None:   # a claim not in a simple phrase ("he is armed"): say what the second look found
            objs = list(dict.fromkeys(h for p in check["persons"] for h in (p.get("held") or [p["object"]]) if h not in EMPTY))
            seen = " or ".join(_article("a", o) for o in objs) if objs else None
            note = f"it appears to be {seen}" if seen else "the hands appear empty"
            new = (summary.rstrip() + " " if summary.strip() else "") + f"(A second look found no weapon: {note}.)"
        out["summary"] = new
        cleared = lambda t, note: rewrite(t, as_object(obj)) or t.rstrip() + note  # noqa: E731
        if out.get("activity") and mentions(out["activity"]):
            out["activity"] = cleared(out["activity"], " (no weapon on a second look)")
        for o in out.get("objects") or []:
            if not isinstance(o, dict):
                continue
            if mentions(o.get("type")):
                o["type"] = obj or "held object"
            if mentions(o.get("description")):
                o["description"] = cleared(o["description"], " (a second look at full resolution found no weapon)")
        reason = out.get("threat_reason") or ""
        if mentions(reason) or not reason.strip():
            # the weapon was the reason: what is left is low, or medium when something else about the event stands out
            other = _notable(e) or _suspicious(" ".join(str(out.get(k) or "") for k in ("summary", "activity")))
            new_level = "medium" if other else "low"
            out["threat_level"] = new_level if RANK[new_level] < RANK.get(level, 0) else level
            out["threat_reason"] = (f"A second look at full resolution found no weapon"
                                    + (f" ({obj})" if obj else "") + "." + (f" {other}." if other and out["threat_level"] == "medium" else ""))
        return out
    # unclear / error: never quietly dropped. High, worded as unconfirmed, and a person is asked to look. The
    # summary is what the hub and the alert show, so it says so even when only another field had the weapon.
    new = rewrite(summary, as_unconfirmed) if mentions(summary) else None
    if new is None:
        new = f"Possible {kind}, unconfirmed (a person needs to look): {summary.strip()}".rstrip(": ")
    out["summary"] = new
    if out.get("activity") and mentions(out["activity"]):
        out["activity"] = rewrite(out["activity"], as_unconfirmed) or out["activity"].rstrip() + " (unconfirmed)"
    for o in out.get("objects") or []:
        if not isinstance(o, dict):
            continue
        if mentions(o.get("type")):
            o["type"] = f"possible {kind}"
        if mentions(o.get("description")):
            o["description"] = rewrite(o["description"], as_unconfirmed) or o["description"].rstrip() + " (unconfirmed)"
    out["tags"] = out["tags"] + (["possible_weapon"] if "possible_weapon" not in out["tags"] else [])
    out["threat_level"] = kept_level(level, kind)
    out["threat_reason"] = f"Possible {kind}, unconfirmed: {check['reason']}"
    return out


# ---------------------------------------------------------------- the whole check

def _reason(verdict: str, kind: str, persons: list[dict], error: str | None, ev: dict) -> str:
    if verdict == "confirmed":
        p = next(p for p in persons if is_confirmed(p))
        return f"Weapon confirmed by a second look at full resolution: {p['object'] or kind} ({p['confidence']} confidence)."
    if verdict == "not_confirmed":
        objs = ", ".join(dict.fromkeys(h for p in persons for h in (p.get("held") or [p["object"]]) if h not in EMPTY))
        return f"A second look at full resolution found no weapon (holding: {objs or 'nothing'})."
    if verdict == "error":
        why = "it timed out" if error == "timeout" else "the model was unavailable" if error else "there was nothing to look at"
        return f"the second look could not run ({why}). A person needs to look."
    if not ev.get("complete"):
        why = ("not everyone in view could be checked" if ev.get("skipped") else
               "someone in view was too small to judge" if ev.get("too_small") else
               "the clip could not be read in time" if ev.get("cut") else
               "there was no full-resolution clip of everyone in view" if ev.get("evidence") != "clip" else "")
    else:
        why = ""
    if not why:
        unsure = [p for p in persons if not (p["weapon"] == "no" and p["confidence"] in ("medium", "high") and plausible(p))]
        why = ("it said " + "; ".join(f"{p['weapon']} ({p['confidence']} confidence, {p['object'] or 'unknown object'})" for p in unsure)
               if unsure else "not every person was answered")
    return f"the second look could not rule it out ({why}). A person needs to look."


async def check_claim(e: dict, result: dict, collect: Callable[[], Awaitable[dict]] | None) -> dict | None:
    """The second look for a synopsis `result` of event `e` (None when it claims no weapon). Never raises."""
    words = claims(result)
    if not words:
        return None
    kind = claim_kind(words)
    t0 = time.time()
    ev: dict = {"evidence": None, "persons": [], "complete": False, "skipped": 0, "too_small": 0, "frames": 0}
    try:
        ev = await asyncio.wait_for(collect(), COLLECT_TIMEOUT_S) if collect else keyframe_evidence(e)
    except Exception as ex:  # noqa: BLE001 - a missing clip must not lose the claim
        log.warning("event %s: weapon check evidence failed (%s: %s)", e.get("id"), type(ex).__name__, ex)
        try:
            ev = keyframe_evidence(e)
        except Exception:  # noqa: BLE001
            pass
    persons, error = [], None
    if not ev.get("persons"):
        error = "no evidence"
    else:
        from .vlmroute import router

        async def ask_all():
            for p in ev["persons"]:
                ans = await router.chat_json(TASK, SYSTEM, question(p), p["crops"], SCHEMA, num_predict=200,
                                             temperature=0.1, priority=PRIORITY)
                persons.append(judge_person(ans))
                if is_confirmed(persons[-1]):
                    break   # one weapon is enough
        try:
            await asyncio.wait_for(ask_all(), TIMEOUT_S)
        except asyncio.TimeoutError:
            error = "timeout"
        except Exception as ex:  # noqa: BLE001 - model down: unconfirmed, kept high
            error = f"{type(ex).__name__}: {str(ex)[:160]}"
    verdict = decide(persons, len(ev.get("persons") or []), bool(ev.get("complete")), error)
    if error == "no evidence":
        error = None   # (the reason says there was nothing to look at)
    check = {"confirmed": verdict == "confirmed", "verdict": verdict, "needs_review": verdict in ("unclear", "error"),
             "kind": kind, "claim": words[:6],
             "persons": [{k: p[k] for k in ("object", "held", "weapon", "confidence", "held_by", "resembles_weapon")}
                         for p in persons],
             "evidence": ev.get("evidence"), "frames": ev.get("frames", 0), "persons_found": len(ev.get("persons") or []),
             "persons_skipped": ev.get("skipped", 0), "persons_too_small": ev.get("too_small", 0),
             "reason": _reason(verdict, kind, persons, error, ev),
             "model": next((p["model"] for p in persons if p.get("model")), None),
             "seconds": round(time.time() - t0, 1), "checked_at": round(time.time(), 1)}
    if error:
        check["error"] = error
    log.info("event %s: weapon claim %s -> %s: %s", e.get("id"), words, verdict, check["reason"])
    return check


async def review(e: dict, result: dict, collect: Callable[[], Awaitable[dict]] | None = None) -> tuple[dict, dict | None]:
    """(synopsis to store, the model's original to keep in synopsis_original or None). A synopsis with no weapon
    claim comes back unchanged. Never raises: an unexpected failure keeps the claim as unconfirmed and high."""
    try:
        check = await check_claim(e, result, collect)
    except Exception as ex:  # noqa: BLE001 - never break the synopsis worker, never drop the claim
        log.exception("event %s: weapon check failed", e.get("id"))
        words = claims(result)
        check = {"confirmed": False, "verdict": "error", "needs_review": True, "kind": claim_kind(words), "claim": words[:6],
                 "persons": [], "evidence": None, "error": f"{type(ex).__name__}: {str(ex)[:160]}",
                 "reason": "the second look could not run (internal error). A person needs to look.",
                 "model": None, "checked_at": round(time.time(), 1)}
    if check is None:
        return result, None
    out = apply(e, result, check)
    return out, (None if check["verdict"] == "confirmed" else result)
