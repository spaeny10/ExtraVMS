"""The Hosts page at fleet scale (hub administrators): what needs attention, and the host and instance lists filtered,
sorted and paged at the hub (GET /api/hub/hosts, /api/hub/central, /api/hub/fleet-summary in api.py).

Plain Python over the host_out / instance_out dicts hosts.py already builds: a hundred hosts and a few hundred live
instances at most (site numbers 1-250 cap the live instances), all read in one pass per request.

What needs attention (`problems`), per instance:
  down      not running: failed, its container not running on the host, or "running" with its server not connected
            to the hub while the host is online (an offline host is its own line)
  storage   using STORAGE_FULL of its quota or more
  data      its data area (/data: database, event media; host agent 0.3.2+) using DATA_FULL of its own quota or more
            (ZFS: the instances/<id> dataset). When it fills up every database write fails; instances without a data
            quota of their own (XFS, no quota) or not reporting it never have this problem
  queue     its YOLO verify queue above hosts.GROW_MIN events (the growing alert's floor), or growing over 15 minutes
  image     its container runs another Docker image ID than the one its host creates new instances from (image_id
            and capacity.image_id, agent 0.3.1+). Tags are never compared: instances run git-rev tags
            (axiom/instance:2096ed5) while host.json's `latest` is retagged to each new build. Either ID unknown = no problem
  cameras   cameras not streaming while its server is online
and per host:
  offline   its agent is not connected
  agent     its agent is older than the newest agent in the fleet
  queue     an open host_queue_growing alert
Instances being removed (deleting, deleted) have no problems.
"""
from __future__ import annotations

import re

from . import hosts

STORAGE_FULL = 0.9
DATA_FULL = 0.85             # below the instance_data_full alert's 90 % (hosts.DATA_OPEN): the list warns first
INSTANCE_PROBLEMS = ("down", "storage", "data", "queue", "image", "cameras")
HOST_PROBLEMS = ("offline", "agent", "queue")
INSTANCE_SORTS = ("created", "site", "customer", "host", "storage", "cameras", "queue")
HOST_SORTS = ("name", "load", "free", "instances")
# the order a sort gets when none is asked for: the biggest first for numbers, A-Z for names, the newest first
_DESC_FIRST = {"created", "storage", "cameras", "queue", "load", "free", "instances"}


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def version_key(v: str | None) -> tuple:
    """"0.10.1" sorts after "0.9.3": numbers compared as numbers, the rest as text."""
    return tuple((1, int(p), "") if p.isdigit() else (0, 0, p) for p in re.findall(r"\d+|[A-Za-z]+", v or ""))


def natural_key(name: str | None) -> tuple:
    """"rack 2" before "rack 10": names compared with their numbers as numbers, the text without case."""
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.findall(r"\d+|\D+", (name or "").casefold()))


def newest(values, key) -> str | None:
    vals = [v for v in values if isinstance(v, str) and v]
    return max(vals, key=key) if vals else None


# ---------------------------------------------------------------- attention

def storage_frac(c: dict) -> float | None:
    used, quota = _num(c.get("used_gb")), _num(c.get("quota_gb"))
    return used / quota if used is not None and quota and quota > 0 else None


def data_frac(c: dict) -> float | None:
    """The data area's use of its own quota (hosts.data_frac), None when unknown or it has none."""
    return hosts.data_frac(c)


def verify_q(c: dict) -> float | None:
    return _num((c.get("work") or {}).get("verify_q"))


def instance_problems(c: dict) -> list[str]:
    st = c.get("state")
    if st in ("deleting", "deleted"):
        return []
    out = []
    host_up = c.get("host_online") is not False
    if st == "failed" or (st == "running" and host_up and (c.get("host_state") not in (None, "running") or not c.get("server_online"))):
        out.append("down")
    frac = storage_frac(c)
    if frac is not None and frac >= STORAGE_FULL:
        out.append("storage")
    dfrac = data_frac(c)
    if dfrac is not None and dfrac >= DATA_FULL:
        out.append("data")
    q = verify_q(c)
    if (q is not None and q > hosts.GROW_MIN) or c.get("queue_growing"):
        out.append("queue")
    if c.get("image_id") and c.get("host_image_id") and c["image_id"] != c["host_image_id"]:
        out.append("image")
    if c.get("server_online") and (c.get("camera_count") or 0) > (c.get("cameras_online") or 0):
        out.append("cameras")
    return out


def host_problems(h: dict, newest_agent: str | None = None) -> list[str]:
    out = []
    if not h.get("online"):
        out.append("offline")
    if h.get("version") and newest_agent and version_key(h["version"]) < version_key(newest_agent):
        out.append("agent")
    if h.get("queue_alerts"):
        out.append("queue")
    return out


def annotate_instances(items: list[dict]) -> list[dict]:
    """Each instance with its `problems` (in place)."""
    for c in items:
        c["problems"] = instance_problems(c)
    return items


def annotate_hosts(items: list[dict]) -> list[dict]:
    agent = newest((h.get("version") for h in items), version_key)
    for h in items:
        h["problems"] = host_problems(h, agent)
    return items


# ---------------------------------------------------------------- filters, sorting, paging

def _has(needle: str, *hay) -> bool:
    return any(needle in str(x).casefold() for x in hay if x is not None)


def match_instance(c: dict, q: str | None = None, state: str | None = None, host: str | None = None, mode: str | None = None,
                   problem: str | None = None) -> bool:
    """q: in the Site, customer, host or server name (or the site number); problem: one of INSTANCE_PROBLEMS or any."""
    if state and c.get("state") != state:
        return False
    if host and c.get("host_id") != host:
        return False
    if mode and c.get("mode") != mode:
        return False
    probs = c.get("problems") or []
    if problem and not (probs if problem == "any" else problem in probs):
        return False
    needle = (q or "").strip().casefold()
    return not needle or _has(needle, c.get("location_name"), c.get("location_id"), c.get("org_name"), c.get("host_name"),
                              c.get("name"), c.get("site_number"))


def host_load(h: dict) -> float | None:
    """The host's 1-minute load per core (0..1 and up), None when not reported."""
    cap = h.get("capacity") or {}
    load = cap.get("load")
    load = load[0] if isinstance(load, list) and load else load
    cpus = _num(cap.get("cpus"))
    return _num(load) / cpus if _num(load) is not None and cpus else None


def match_host(h: dict, q: str | None = None, status: str | None = None) -> bool:
    """status: online, offline, agent (behind the newest), or attention (any problem)."""
    probs = h.get("problems") or []
    if status == "online" and not h.get("online"):
        return False
    if status == "offline" and h.get("online"):
        return False
    if status == "agent" and "agent" not in probs:
        return False
    if status == "attention" and not probs:
        return False
    needle = (q or "").strip().casefold()
    return not needle or _has(needle, h.get("name"), h.get("hostname"), h.get("agent_ip"))


_INSTANCE_KEYS = {
    "created": lambda c: _num(c.get("created_at")),
    "site": lambda c: natural_key(c.get("location_name") or c.get("location_id")),
    "customer": lambda c: natural_key(c.get("org_name") or c.get("org_id")),
    "host": lambda c: natural_key(c.get("host_name") or c.get("host_id")),
    "storage": storage_frac,
    "cameras": lambda c: _num(c.get("camera_count")),
    "queue": verify_q,
}
_HOST_KEYS = {
    "name": lambda h: natural_key(h.get("name")),
    "load": host_load,
    "free": lambda h: _num(h.get("room_gb")),
    "instances": lambda h: _num(h.get("instances")),
}


def _sorted(items: list[dict], key, order: str, tie) -> list[dict]:
    """By key in `order`, ties by `tie` (A-Z); rows without a value come last either way."""
    known = [x for x in items if key(x) is not None]
    unknown = [x for x in items if key(x) is None]
    known.sort(key=tie)
    known.sort(key=key, reverse=order == "desc")   # stable: equal keys keep the A-Z order
    return known + sorted(unknown, key=tie)


def sort_instances(items: list[dict], sort: str | None = None, order: str | None = None) -> list[dict]:
    sort = sort if sort in _INSTANCE_KEYS else "created"
    return _sorted(items, _INSTANCE_KEYS[sort], order or ("desc" if sort in _DESC_FIRST else "asc"), _INSTANCE_KEYS["site"])


def sort_hosts(items: list[dict], sort: str | None = None, order: str | None = None) -> list[dict]:
    sort = sort if sort in _HOST_KEYS else "name"
    return _sorted(items, _HOST_KEYS[sort], order or ("desc" if sort in _DESC_FIRST else "asc"), _HOST_KEYS["name"])


def page(items: list, limit: int | None, offset: int = 0) -> tuple[list, int]:
    """(items[offset:offset + limit], how many there are in all); limit None = all from offset."""
    offset = max(0, offset or 0)
    return (items[offset:] if limit is None else items[offset:offset + limit]), len(items)


# ---------------------------------------------------------------- the attention strip

def summary(host_items: list[dict], instance_items: list[dict]) -> dict:
    """The counts behind the Hosts page's attention strip (both lists annotated). `healthy`: nothing to show."""
    live = [c for c in instance_items if c.get("state") != "deleted"]
    hcount = {k: sum(1 for h in host_items if k in (h.get("problems") or [])) for k in HOST_PROBLEMS}
    icount = {k: sum(1 for c in live if k in (c.get("problems") or [])) for k in INSTANCE_PROBLEMS}
    down = sum(max(0, (c.get("camera_count") or 0) - (c.get("cameras_online") or 0)) for c in live if "cameras" in (c.get("problems") or []))
    return {
        "hosts": {"total": len(host_items), "online": sum(1 for h in host_items if h.get("online")), **hcount},
        "instances": {"total": len(live), "any": sum(1 for c in live if c.get("problems")), **icount},
        "cameras_down": down,
        "newest_agent": newest((h.get("version") for h in host_items), version_key),
        # whether any instance and its host both report image IDs (agent 0.3.1+): otherwise "image" can never count
        "images_reported": any(c.get("image_id") and c.get("host_image_id") for c in live),
        "healthy": not any(hcount.values()) and not any(icount.values()),
        # every host's id and name (the Instances tab's host filter), by name
        "host_index": [{"id": h["id"], "name": h["name"], "online": bool(h.get("online"))} for h in sort_hosts(list(host_items))],
    }
