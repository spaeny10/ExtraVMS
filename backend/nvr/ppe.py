"""PPE compliance: people who stay in a "PPE required" zone without a hard hat and/or hi-vis vest.

The operator paints a zone of type "ppe" on a camera (Settings -> Cameras -> Zones) and ticks what it requires:
    {"name": "Yard", "type": "ppe", "points": [[x, y], ...], "required": ["hard_hat", "vest"],
     "min_dwell_s": 5, "grace_s": 3}
A verified person whose feet were inside it for at least min_dwell_s is checked on a few recorded frames from
grace_s after they walked in (time to put a hat on at the gate):

1. A small PPE detector (YOLOv8s, classes hard hat / no helmet / vest / no vest / worker ...) runs on those frames.
   Each item gets one of present / missing / unclear from the boxes over the person's head and torso.
2. Only when an item is missing or unclear does Qwen look at crops of the person (task "ppe"). Qwen can overrule
   a missing item (it sees the hat) and settles an unclear one. Detector-confident "present" stands: Qwen is not
   asked about people who are plainly compliant, which keeps the GPU budget to the doubtful cases.

The result is stored in events.detections["ppe"]; policy.check turns a violation into the event's broken site rule
(kind "ppe", medium priority by default), which lifts the priority, puts it under Needs attention, into the digest,
the hub alerts and the search index ("no hard hat"). The frame is marked and becomes the event snapshot.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import cv2
import numpy as np

from . import zones
from .config import ROOT, settings

log = logging.getLogger("nvr.ppe")

ITEMS = ("hard_hat", "vest")
ITEM_WORDS = {"hard_hat": "hard hat", "vest": "hi-vis vest"}
TAGS = {"hard_hat": "no hard hat", "vest": "no hi-vis vest"}
MAX_GAP_S = 2.0       # path samples further apart than this don't count as continuous presence
MARGIN = 0.1          # a frame decides an item only if one side beats the other by this much

# Detector class names (several public PPE models) -> (item, worn?) or "person"
_CLASS_MAP = {
    "hard hat": ("hard_hat", True), "hardhat": ("hard_hat", True), "helmet": ("hard_hat", True),
    "no helmet": ("hard_hat", False), "no hardhat": ("hard_hat", False), "no hard hat": ("hard_hat", False),
    "vest": ("vest", True), "safety vest": ("vest", True),
    "no vest": ("vest", False), "no safety vest": ("vest", False),
    "worker": "person", "person": "person",
}


def canonical(name: str):
    return _CLASS_MAP.get(name.lower().replace("_", " ").replace("-", " ").strip())


# ---------------------------------------------------------------- zones and dwell

def ppe_zones(zone_list: list[dict] | None) -> list[dict]:
    return [z for z in zones.normalize(zone_list) if z["type"] == "ppe" and z.get("required")]


def dwell(path: list, poly: list) -> dict | None:
    """Time the track's feet spent inside poly: {seconds, first, last} (camera-clock epoch), or None if never in."""
    inside = [p[0] for p in path or [] if zones.point_in_polygon(*zones.foot(p[1:5]), poly)]
    if not inside:
        return None
    secs, prev_in, prev_ts = 0.0, False, None
    for p in path:
        now_in = zones.point_in_polygon(*zones.foot(p[1:5]), poly)
        if now_in and prev_in and p[0] - prev_ts <= MAX_GAP_S:
            secs += p[0] - prev_ts
        prev_in, prev_ts = now_in, p[0]
    return {"seconds": round(secs, 2), "first": inside[0], "last": inside[-1]}


def plan(event: dict, zone_list: list[dict] | None) -> list[dict]:
    """PPE zones this person stayed in long enough to be checked: [{zone, required, dwell_s, window: [t0, t1]}]."""
    if event.get("camera_class") != "person" or event.get("status") != "verified":
        return []
    out = []
    for z in ppe_zones(zone_list):
        d = dwell(event.get("path") or [], z["points"])
        if not d or d["seconds"] < float(z.get("min_dwell_s", settings.ppe_min_dwell_s)):
            continue
        t0 = d["first"] + float(z.get("grace_s", settings.ppe_grace_s))
        if t0 >= d["last"]:
            t0 = d["first"]
        out.append({"zone": (z.get("name") or "PPE zone").strip(), "required": list(z["required"]),
                    "priority": z.get("priority") or "medium", "dwell_s": d["seconds"], "window": [round(t0, 3), round(d["last"], 3)]})
    return out


# ---------------------------------------------------------------- detector

class Detector:
    """The PPE YOLO model, loaded on first use on the YOLO GPU (call from the pipeline's GPU thread)."""

    def __init__(self, weights: Path | None = None) -> None:
        self.weights = weights or ROOT / "models" / settings.ppe_model
        self._model = None

    @property
    def available(self) -> bool:
        return self._model is not None or self.weights.exists()

    def _load(self):
        if self._model is None:
            from ultralytics import YOLO
            self._model = YOLO(str(self.weights))
            self._model.to(settings.torch_device)
            log.info("PPE model %s loaded on %s (%s)", self.weights.name, settings.torch_device,
                     ", ".join(self._model.names.values()))
        return self._model

    def detect(self, images: list[np.ndarray]) -> list[list[dict]]:
        """Per image: [{kind: (item, worn) | "person", conf, box (normalized l,t,r,b)}] for the classes we use."""
        if not images:
            return []
        m = self._load()
        res = m.predict(images, imgsz=settings.ppe_imgsz, conf=0.15, device=settings.torch_device, verbose=False)
        out = []
        for r in res:
            boxes = []
            for b, c, p in zip(r.boxes.xyxyn.tolist(), r.boxes.cls.tolist(), r.boxes.conf.tolist()):
                kind = canonical(m.names[int(c)])
                if kind:
                    boxes.append({"kind": kind, "conf": round(float(p), 3), "box": [round(float(v), 4) for v in b]})
            out.append(boxes)
        return out


def item_scores(boxes: list[dict], person: list[float]) -> dict:
    """Best (worn, not_worn) confidence per item among boxes over this person: hats on the top of the box,
    vests on the torso."""
    l, t, r, b = person
    w, h = r - l, b - t
    out = {i: [0.0, 0.0] for i in ITEMS}
    for x in boxes:
        if x["kind"] == "person":
            continue
        item, worn = x["kind"]
        cx, cy = (x["box"][0] + x["box"][2]) / 2, (x["box"][1] + x["box"][3]) / 2
        if not (l - 0.1 * w <= cx <= r + 0.1 * w):
            continue
        if item == "hard_hat" and not (t - 0.1 * h <= cy <= t + 0.4 * h):
            continue
        if item == "vest" and not (t <= cy <= t + 0.8 * h):
            continue
        k = 0 if worn else 1
        out[item][k] = max(out[item][k], x["conf"])
    return out


def frame_call(scores: list[float], thr: float) -> str:
    worn, not_worn = scores
    if worn >= thr and worn >= not_worn + MARGIN:
        return "present"
    if not_worn >= thr and not_worn >= worn + MARGIN:
        return "missing"
    return "unclear"


def combine_frames(calls: list[str]) -> str:
    """One answer per item from the per-frame calls: unanimous (ignoring unclear frames) or a 2:1 majority."""
    p, m = calls.count("present"), calls.count("missing")
    if p and not m:
        return "present"
    if m and not p:
        return "missing"
    if p >= 2 * m and p:
        return "present"
    if m >= 2 * p and m:
        return "missing"
    return "unclear"


def assess(frames: list[dict], required: list[str], thr: float | None = None) -> dict:
    """frames: [{ts, box, scores: {item: [worn, not_worn]}}] -> {item: present|missing|unclear} for the required items."""
    thr = settings.ppe_conf if thr is None else thr
    return {i: combine_frames([frame_call(f["scores"][i], thr) for f in frames]) if frames else "unclear" for i in required}


# ---------------------------------------------------------------- Qwen confirmation

SCHEMA = {"type": "object", "properties": {
    "head": {"type": "string"},
    "hard_hat": {"type": "string", "enum": ["yes", "no", "unclear"]},
    "torso": {"type": "string"},
    "hi_vis_vest": {"type": "string", "enum": ["yes", "no", "unclear"]}},
    "required": ["head", "hard_hat", "torso", "hi_vis_vest"]}
SYSTEM = ("You check one person in workplace safety camera crops for protective equipment. First describe what is on the "
          "person's head (bare, cap, beanie, hood, hard hat...), then whether it is a hard hat: a rigid plastic safety helmet "
          "with a brim or peak. A baseball cap, beanie, hood, turban or sun hat is NOT a hard hat; a hard hat held in the hand "
          "or hanging from a belt is NOT worn. Then describe the upper-body clothing and whether it is high-visibility: a "
          "fluorescent yellow/orange/lime vest, jacket or shirt with reflective strips counts; plain colored clothing, an "
          "orange overall without reflective strips, or a vest carried in the hand does not. Answer unclear when the head or "
          "torso is hidden, cut off or too small to judge.")
VLM_KEYS = {"hard_hat": "hard_hat", "vest": "hi_vis_vest"}


def parse_vlm(answer: dict) -> dict:
    """Qwen's JSON -> {item: present|missing|unclear, "head", "torso"} (anything malformed is unclear)."""
    word = {"yes": "present", "no": "missing"}
    out = {i: word.get(str(answer.get(k, "")).strip().lower(), "unclear") for i, k in VLM_KEYS.items()}
    out["head"] = str(answer.get("head", ""))[:160]
    out["torso"] = str(answer.get("torso", ""))[:160]
    return out


VLM_TIMEOUT_S = 45


async def confirm_with_vlm(crops: list[bytes]) -> dict | None:
    """Ask Qwen about the person in these crops; None if it can't answer (down, timeout, no crops)."""
    from .vlmroute import router
    if not crops:
        return None
    n = len(crops)
    text = ("Check this person's hard hat and hi-vis vest." if n == 1 else
            f"These {n} crops show the same person at different moments. Check their hard hat and hi-vis vest.")
    try:
        # runs inside verification: never hold the verify queue up long behind other Qwen work
        r = await asyncio.wait_for(router.chat_json("ppe", SYSTEM, text, crops, SCHEMA, num_predict=220, temperature=0.1),
                                   VLM_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 - the detector's answer stands
        log.warning("PPE check by Qwen failed: %s", e)
        return None
    return {**parse_vlm(r), "model": r.get("_model")}


def decide(required: list[str], detector: dict, vlm: dict | None, vlm_first: bool = False) -> dict:
    """Final per-item answer and the verdict.
    detector present            -> present (Qwen not consulted)
    detector missing / unclear  -> Qwen's yes/no decides; Qwen unclear keeps the detector's answer
    no Qwen answer              -> the detector's answer
    vlm_first (ppe_vlm_all): Qwen's yes/no decides every item, the detector only fills in where Qwen can't tell."""
    items, overruled = {}, []
    for i in required:
        d = detector.get(i, "unclear")
        v = (vlm or {}).get(i, "unclear")
        items[i] = d if v == "unclear" or (d == "present" and not vlm_first) else v
        if d != items[i]:
            overruled.append(i)
    missing = [i for i in required if items[i] == "missing"]
    verdict = "violation" if missing else "compliant" if all(items[i] == "present" for i in required) else "unclear"
    return {"items": items, "violation": missing, "verdict": verdict, "overruled": overruled}


def needs_vlm(detector: dict) -> bool:
    return settings.ppe_vlm_all or any(v != "present" for v in detector.values())


# ---------------------------------------------------------------- frames for the check (GPU thread)

def pick_times(event: dict, window: list[float], n: int) -> tuple[list[dict], list[float]]:
    """Verification samples inside the window (camera clock) with YOLO's person box, plus extra camera-clock
    times to decode when there are fewer than n of them."""
    det = event.get("detections") or {}
    shift = det.get("time_shift_s") or 0.0
    t0, t1 = window
    have = [s for s in det.get("samples", []) if s.get("match") and t0 - 0.3 <= s["ts"] + shift <= t1 + 0.3]
    if len(have) > n:
        have = [have[round(i * (len(have) - 1) / (n - 1))] for i in range(n)] if n > 1 else have[:1]
    extra = []
    if len(have) < n:
        k = n - len(have)
        cand = [t0 + (t1 - t0) * (i + 0.5) / k for i in range(k)] if t1 > t0 else [t0]
        taken = [s["ts"] + shift for s in have]
        extra = [t for t in cand if all(abs(t - x) > 0.4 for x in taken)]
    return have, extra


def person_box(event: dict, cam_ts: float, coco_boxes: list[dict] | None) -> list[float] | None:
    """Box of the event's person at camera time cam_ts: the YOLO person box overlapping the camera's box."""
    from .verifier import box_at, iou, center_inside
    cb = box_at(event.get("path") or [], cam_ts, tol=1.0)
    if cb is None:
        return None
    best, score = None, 0.0
    for b in coco_boxes or []:
        s = iou(cb, b["box"])
        if (s >= 0.2 or center_inside(cb, b["box"]) or center_inside(b["box"], cb)) and s >= score:
            best, score = b["box"], s
    return list(best) if best else list(cb)


def crop_jpeg(img: np.ndarray, box: list[float], size: int = 512) -> bytes:
    h, w = img.shape[:2]
    l, t, r, b = box
    pw, ph = (r - l) * 0.15, (b - t) * 0.08
    x1, y1, x2, y2 = int(max(0, l - pw) * w), int(max(0, t - ph) * h), int(min(1, r + pw) * w), int(min(1, b + ph) * h)
    crop = img[y1:max(y2, y1 + 2), x1:max(x2, x1 + 2)]
    s = size / max(crop.shape[:2])
    if s < 1:
        crop = cv2.resize(crop, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    return cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()


def mark(img: np.ndarray, box: list[float], text: str, bad: bool) -> np.ndarray:
    out = img.copy()
    h, w = out.shape[:2]
    x1, y1, x2, y2 = int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h)
    color = (40, 40, 230) if bad else (80, 200, 80)
    th = max(2, w // 640)
    cv2.rectangle(out, (x1, y1), (x2, y2), color, th + 1)
    scale = max(0.7, w / 1600)
    (tw, tht), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, th)
    ty = max(tht + 8, y1 - 8)
    cv2.rectangle(out, (x1, ty - tht - 6), (x1 + tw + 8, ty + 4), color, -1)
    cv2.putText(out, text, (x1 + 4, ty), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), th, cv2.LINE_AA)
    return out


def detect_event(detector: Detector, coco_model, event: dict, check: dict) -> dict:
    """GPU thread: decode the frames for one planned zone check and run the PPE detector on them.
    Returns {frames: [{ts, box, scores}], detector: {item: call}, crops: [jpeg], images: {ts: ndarray}}."""
    from .verifier import grab_frames
    clip = settings.data_dir / (event.get("clip") or "")
    if not event.get("clip") or not clip.exists():
        return {"frames": [], "detector": {i: "unclear" for i in check["required"]}, "crops": [], "images": {}, "error": "no clip"}
    shift = (event.get("detections") or {}).get("time_shift_s") or 0.0
    clip_start = event.get("clip_start") or event["start_ts"] - settings.clip_pre_roll
    have, extra = pick_times(event, check["window"], settings.ppe_frames)
    targets = [s["ts"] for s in have] + [t - shift for t in extra]   # frame time
    imgs = grab_frames(clip, clip_start, targets) if targets else {}
    boxes_by_ts: dict[float, list[float]] = {s["ts"]: s["match"]["box"] for s in have}
    extra_ts = [t - shift for t in extra if t - shift in imgs]
    if extra_ts and coco_model is not None:
        res = coco_model.predict([imgs[t] for t in extra_ts], imgsz=settings.yolo_imgsz, conf=settings.yolo_conf,
                                 device=settings.yolo_device, verbose=False, classes=[0])
        for t, r in zip(extra_ts, res):
            coco = [{"box": [float(v) for v in b]} for b in r.boxes.xyxyn.tolist()]
            if (pb := person_box(event, t + shift, coco)) is not None:
                boxes_by_ts[t] = pb
    else:
        for t in extra_ts:
            if (pb := person_box(event, t + shift, None)) is not None:
                boxes_by_ts[t] = pb
    ts_list = sorted(t for t in boxes_by_ts if t in imgs)
    dets = detector.detect([imgs[t] for t in ts_list])
    # keep the exact frame time as the key: it indexes `imgs` below (rounding it broke the lookup)
    frames = [{"ts": t, "box": [round(v, 4) for v in boxes_by_ts[t]], "scores": item_scores(d, boxes_by_ts[t])}
              for t, d in zip(ts_list, dets)]
    det_calls = assess(frames, check["required"])
    area = lambda f: (f["box"][2] - f["box"][0]) * (f["box"][3] - f["box"][1])
    crops = [crop_jpeg(imgs[f["ts"]], f["box"]) for f in sorted(frames, key=area, reverse=True)[:3]] if needs_vlm(det_calls) else []
    return {"frames": frames, "detector": det_calls, "crops": crops,
            "images": {f["ts"]: imgs[f["ts"]] for f in frames}}


def describe(result: dict) -> str:
    """'No hard hat in PPE zone 'Yard' (12 s)' / 'No hard hat or hi-vis vest ...'."""
    missing = [ITEM_WORDS[i] for i in result["violation"]]
    what = " or ".join(missing) if len(missing) <= 2 else ", ".join(missing)
    return f"No {what} in PPE zone '{result['zone']}' ({result['dwell_s']:.0f} s)"


def tags(result: dict | None) -> list[str]:
    if not result or result.get("verdict") != "violation":
        return []
    return ["ppe violation", *(TAGS[i] for i in result["violation"])]


def save_marked(event_id: int, result: dict, images: dict) -> str | None:
    """Mark the person on the clearest frame (a violation: also the event's snapshot). Returns the file name."""
    from .verifier import event_dir
    frames = result.get("frames") or []
    if not frames:
        return None
    area = lambda f: (f["box"][2] - f["box"][0]) * (f["box"][3] - f["box"][1])
    f = max((f for f in frames if f["ts"] in images), key=area, default=None)
    if f is None:
        return None
    bad = result["verdict"] == "violation"
    text = ("No " + " / no ".join(ITEM_WORDS[i] for i in result["violation"])) if bad else \
        "PPE ok" if result["verdict"] == "compliant" else "PPE unclear"
    img = mark(images[f["ts"]], f["box"], text, bad)
    d = event_dir(event_id)
    cv2.imwrite(str(d / "ppe.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if bad:
        cv2.imwrite(str(d / "snapshot.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return "ppe.jpg"


async def check_event(event: dict, zone_list: list[dict] | None, run_gpu, detector: Detector, coco_model,
                      vlm_ready: bool = True) -> dict | None:
    """Run the check for every PPE zone the person stayed in; returns the stored result (worst zone first) or None.
    run_gpu(fn, *args) runs fn on the YOLO GPU thread and returns an awaitable."""
    checks = plan(event, zone_list)
    if not checks:
        return None
    if not detector.available:
        return {"verdict": "unavailable", "zone": checks[0]["zone"], "required": checks[0]["required"],
                "dwell_s": checks[0]["dwell_s"], "violation": [], "error": f"PPE model missing: models/{settings.ppe_model}",
                "checked_at": round(time.time(), 1)}
    results = []
    for c in checks:
        t0 = time.time()
        found = await run_gpu(detect_event, detector, coco_model, event, c)
        vlm = None
        if found["crops"] and settings.ppe_vlm_confirm and vlm_ready:
            vlm = await confirm_with_vlm(found["crops"])
        final = decide(c["required"], found["detector"], vlm, vlm_first=settings.ppe_vlm_all)
        res = {"zone": c["zone"], "required": c["required"], "priority": c["priority"], "dwell_s": c["dwell_s"],
               "window": c["window"], **final, "detector": found["detector"], "vlm": vlm,
               "frames": found["frames"], "model": settings.ppe_model, "seconds": round(time.time() - t0, 2),
               "checked_at": round(time.time(), 1), **({"error": found["error"]} if found.get("error") else {})}
        res["_images"] = found["images"]
        results.append(res)
    rank = {"violation": 0, "unclear": 1, "compliant": 2}
    results.sort(key=lambda r: rank.get(r["verdict"], 3))
    best = results[0]
    if len(results) > 1:
        best["other_zones"] = [{k: r[k] for k in ("zone", "verdict", "violation")} for r in results[1:]]
    return best
