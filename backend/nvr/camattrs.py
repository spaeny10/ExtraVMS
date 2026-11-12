"""What the camera says about an object beyond its box: vehicle type and colour, clothing colours, helmet and hat.

ONVIF's analytics schema carries these inside a metadata Object's Appearance (Milesight MS-C8477-HPG1 firmware 63.8,
2026-11-12, about 10 documents a second):
    VehicleInfo/Type "Car" | "Truck" ...          VehicleInfo/Color/ColorCluster/Color@ColorName "White"
    HumanBody/Clothing/Tops|Bottoms/Color/ColorCluster/Color@ColorName "Green" | "Black" (Likelihood 0.00 on bottoms)
    HumanFace/Accessory/Helmet/Wear "True" | "False"     HumanFace/Accessory/Hat/Wear
The camera sends them now and then, on the object's own box or as an Object of its own (same ObjectId, no box).
They are collected per track as weighted votes, {key: {value: [count, weight]}}, and stored on the event
(events.camera_attrs); best() turns them into one answer.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from .onvif_soap import local

KEYS = ("vehicle_type", "vehicle_color", "top_color", "bottom_color", "helmet", "hat")
PERSON_KEYS = {"top_color", "bottom_color", "helmet", "hat"}
VEHICLE_KEYS = {"vehicle_type", "vehicle_color"}
UNRATED = 0.5   # a value the camera sent with Likelihood 0 (or none): it doesn't rate it, it still said it

# nearest-name for cameras that send a colour as RGB without a ColorName
_PALETTE = {"black": (0, 0, 0), "white": (255, 255, 255), "gray": (128, 128, 128), "red": (200, 0, 0),
            "orange": (255, 140, 0), "yellow": (240, 220, 0), "green": (0, 140, 0), "blue": (0, 60, 220),
            "purple": (128, 0, 128), "brown": (130, 80, 30), "pink": (255, 150, 180)}
_WORDS = {"grey": "gray"}


def _child(el: ET.Element | None, *names: str) -> ET.Element | None:
    """el/names[0]/names[1]... by local name (direct children at each step)."""
    for n in names:
        if el is None:
            return None
        el = next((c for c in el if local(c) == n), None)
    return el


def likelihood(el: ET.Element) -> float:
    """An element's Likelihood as 0..1. The 8477 sends some as percent (Likelihood="53.00" on a Human, 2026-11-12)."""
    try:
        lk = float(el.get("Likelihood", 0))
    except ValueError:
        return 0.0
    return min(lk / 100 if lk > 1 else max(lk, 0.0), 1.0)


def _weight(el: ET.Element) -> float:
    lk = likelihood(el)
    return lk if lk > 0 else UNRATED


def _color(el: ET.Element | None) -> tuple[str, float] | None:
    """The most likely colour of a Color element (ColorCluster/Color), as a lower-case name."""
    if el is None:
        return None
    best = None
    for c in (e for e in el.iter() if local(e) == "Color" and e is not el):
        name = (c.get("ColorName") or "").strip().lower()
        if not name and (c.get("Colorspace") or "").upper() in ("RGB", "") and c.get("X") is not None:
            try:
                rgb = tuple(float(c.get(k, 0)) for k in ("X", "Y", "Z"))
            except ValueError:
                continue
            name = min(_PALETTE, key=lambda n: sum((a - b) ** 2 for a, b in zip(_PALETTE[n], rgb)))
        if not name:
            continue
        cand = (_WORDS.get(name, name), _weight(c))
        if best is None or cand[1] > best[1]:
            best = cand
    return best


def _wear(el: ET.Element | None) -> tuple[str, float] | None:
    w = _child(el, "Wear")
    if w is None or not w.text:
        return None
    v = {"true": "yes", "false": "no"}.get(w.text.strip().lower())
    return (v, _weight(w)) if v else None


def from_object(obj: ET.Element) -> dict[str, tuple[str, float]]:
    """{key: (value, weight)} from one metadata Object's Appearance."""
    app = _child(obj, "Appearance")
    if app is None:
        return {}
    out: dict[str, tuple[str, float]] = {}
    veh = _child(app, "VehicleInfo")
    if veh is not None:
        t = _child(veh, "Type")
        if t is not None and t.text and t.text.strip():
            out["vehicle_type"] = (t.text.strip().lower(), _weight(t))
        if (c := _color(_child(veh, "Color"))):
            out["vehicle_color"] = c
    clothing = _child(app, "HumanBody", "Clothing")
    if clothing is not None:
        for part, key in (("Tops", "top_color"), ("Bottoms", "bottom_color")):
            if (c := _color(_child(clothing, part, "Color"))):
                out[key] = c
    acc = _child(app, "HumanFace", "Accessory")
    if acc is not None:
        for part, key in (("Helmet", "helmet"), ("Hat", "hat")):
            if (w := _wear(_child(acc, part))):
                out[key] = w
    return out


def add(votes: dict, attrs: dict[str, tuple[str, float]], label: str | None = None) -> None:
    """Count one sighting's attributes into votes (in place). label: only the keys that fit a person / vehicle."""
    allowed = PERSON_KEYS if label == "person" else VEHICLE_KEYS if label == "vehicle" else set(KEYS)
    for key, (value, weight) in attrs.items():
        if key not in allowed or not value:
            continue
        slot = votes.setdefault(key, {}).setdefault(value, [0, 0.0])
        slot[0] += 1
        slot[1] = round(slot[1] + weight, 3)


def merge(a: dict | None, b: dict | None) -> dict | None:
    """Two events' votes as one (merge.py folds events together)."""
    out: dict = {}
    for votes in (a or {}, b or {}):
        for key, values in votes.items():
            for value, (n, w) in values.items():
                slot = out.setdefault(key, {}).setdefault(value, [0, 0.0])
                slot[0] += n
                slot[1] = round(slot[1] + w, 3)
    return out or None


def best(votes: dict | None, key: str) -> dict | None:
    """{value, n (sightings of it), share (of the weight for this key)} for the most weighted value, or None."""
    values = (votes or {}).get(key) or {}
    if not values:
        return None
    total = sum(w for _n, w in values.values()) or 1.0
    value, (n, w) = max(values.items(), key=lambda kv: (kv[1][1], kv[1][0]))
    return {"value": value, "n": n, "share": round(w / total, 2)}


def firm(votes: dict | None, key: str, min_n: int = 3, min_share: float = 0.7) -> str | None:
    """The value only when the camera said it often and mostly the same way (search text, PPE)."""
    b = best(votes, key)
    return b["value"] if b and b["n"] >= min_n and b["share"] >= min_share else None


def describe(votes: dict | None, label: str | None) -> str | None:
    """"white truck" / "green top, black bottoms" for search and the event page; None when the camera said nothing."""
    if not votes:
        return None
    if label == "vehicle":
        color, kind = best(votes, "vehicle_color"), best(votes, "vehicle_type")
        words = [color["value"] if color else None, kind["value"] if kind else ("vehicle" if color else None)]
        return " ".join(w for w in words if w) or None
    parts = []
    for key, word in (("top_color", "top"), ("bottom_color", "bottoms")):
        b = best(votes, key)
        if b:
            parts.append(f"{b['value']} {word}")
    helmet = firm(votes, "helmet")
    if helmet == "yes":
        parts.append("wearing a hard hat")
    elif helmet == "no" and firm(votes, "hat") == "yes":
        parts.append("wearing a hat")
    return ", ".join(parts) or None


def ppe_calls(votes: dict | None, required: list[str]) -> dict:
    """The camera's helmet answer as a PPE call for hard_hat: present / missing / unclear (it says nothing of vests)."""
    out = {}
    for item in required:
        if item == "hard_hat":
            v = firm(votes, "helmet")
            out[item] = "present" if v == "yes" else "missing" if v == "no" else "unclear"
        else:
            out[item] = "unclear"
    return out
