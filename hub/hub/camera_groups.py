"""Named camera groups of a Site ("North yard", "Perimeter") and the talk-down speakers bound to them.

A group is a Site concept, not a server's: it may hold cameras of any of the Site's servers, by the keys the Site's
Live and Timeline use ("<server>/<camera>"). Shared by everyone who sees the Site (listed for all of them), created,
renamed, edited and deleted by the Site's admins (customer admins and up, hub administrators, SOC supervisors where
the Site's customer is monitored: auth.membership). Site rules will name groups later; talk-down uses them now.

Talk-down: a server keeps its own speaker -> camera bindings (backend speakers_api.py, reported in the heartbeat
summary's `speakers`). The hub adds bindings to groups (site_speaker_groups: the speaker by its server and its id on
that server), so a speaker talks on every camera of a bound group, whatever server the camera is on; membership
changes follow automatically. speakers_by_camera() is the union per camera key: direct bindings plus group bindings.
A speaker only ever plays on its own server: the hub UI sends play / stop to /s/<speaker's server>/api/speakers/...

Keys are checked at save: a malformed key, or one naming a server outside the Site, is a 400 (the client is confused);
a camera the hub's registry has never seen on a Site server is dropped quietly (a camera removed while the editor was
open). Stored keys of a server that later leaves the Site stay in the row but are left out of every answer.
"""
from __future__ import annotations

import time

import sqlalchemy as sa

from . import db

MAX_GROUPS = 100          # per Site
MAX_KEYS = 500            # cameras in one group (a Site's cameras across all its servers)
KEY_LEN = 120             # "<server id>/<camera id>"
NAME_LEN = 60


class NameTaken(ValueError):
    pass


class TooMany(ValueError):
    pass


class Unknown(LookupError):
    pass


# ---------------------------------------------------------------- the Site's servers and cameras

def site_servers(location_id: str) -> dict[str, dict]:
    """The Site's servers by id (retired ones included: they still belong to it until moved or removed)."""
    t = db.sites.c
    return {r["id"]: r for r in db.rows(sa.select(t.id, t.name, t.summary, t.retired_at).where(t.location_id == location_id))}


def known_cameras(server_ids) -> dict[str, set[str]]:
    """server id -> every camera id the hub's registry has seen on it (missing and disabled ones too)."""
    ids = list(server_ids)
    out: dict[str, set[str]] = {s: set() for s in ids}
    if ids:
        for r in db.rows(sa.select(db.cameras.c.server_id, db.cameras.c.camera_id).where(db.cameras.c.server_id.in_(ids))):
            out[r["server_id"]].add(r["camera_id"])
    return out


def clean_keys(keys: list[str], servers: dict[str, dict], known: dict[str, set[str]]) -> list[str]:
    """The keys as stored: de-duplicated, in the order sent, unknown cameras of the Site's servers dropped.
    ValueError for a malformed key, one of a server outside the Site, or too many."""
    if len(keys) > MAX_KEYS:
        raise ValueError(f"at most {MAX_KEYS} cameras in a group")
    out = []
    for k in dict.fromkeys(keys):
        if not isinstance(k, str) or not k or len(k) > KEY_LEN or k.count("/") != 1:
            raise ValueError(f"not a camera key: {str(k)[:KEY_LEN]!r} (expected <server>/<camera>)")
        server, cam = k.split("/")
        if not server or not cam:
            raise ValueError(f"not a camera key: {k!r} (expected <server>/<camera>)")
        if server not in servers:
            raise ValueError(f"{k}: that server isn't in this site")
        if cam in known.get(server, ()):
            out.append(k)
    return out


def _in_site(keys, servers) -> list[str]:
    return [k for k in keys or [] if isinstance(k, str) and k.split("/", 1)[0] in servers]


# ---------------------------------------------------------------- groups

def _bound(location_id: str) -> dict[int, list[dict]]:
    """group id -> the speakers bound to it ({server_id, speaker_id})."""
    out: dict[int, list[dict]] = {}
    t = db.site_speaker_groups.c
    for r in db.rows(sa.select(db.site_speaker_groups).where(t.location_id == location_id).order_by(t.server_id, t.speaker_id)):
        out.setdefault(r["group_id"], []).append({"server_id": r["server_id"], "speaker_id": r["speaker_id"]})
    return out


def _public(r: dict, servers: dict[str, dict], bound: dict[int, list[dict]]) -> dict:
    return {"id": r["id"], "name": r["name"], "cameras": _in_site(r["cameras"], servers), "created_at": r["created_at"],
            "updated_at": r["updated_at"], "created_by": r.get("created_by"),
            "speakers": [b for b in bound.get(r["id"], []) if b["server_id"] in servers]}


def _rows(location_id: str) -> list[dict]:
    rows = db.rows(sa.select(db.site_camera_groups).where(db.site_camera_groups.c.location_id == location_id))
    rows.sort(key=lambda r: (r["name"].lower(), r["id"]))
    return rows


def list_for(location_id: str, servers: dict[str, dict] | None = None) -> list[dict]:
    """The Site's groups by name, each with its cameras (of the Site's current servers) and bound speakers."""
    servers = site_servers(location_id) if servers is None else servers
    bound = _bound(location_id)
    return [_public(r, servers, bound) for r in _rows(location_id)]


def get(location_id: str, group_id: int) -> dict | None:
    """One group of this Site (another Site's is None)."""
    t = db.site_camera_groups.c
    return db.one(sa.select(db.site_camera_groups).where(t.id == group_id, t.location_id == location_id))


def public(location_id: str, group_id: int) -> dict:
    servers = site_servers(location_id)
    return _public(get(location_id, group_id), servers, _bound(location_id))


def _name_taken(location_id: str, name: str, exclude: int | None = None) -> bool:
    t = db.site_camera_groups.c
    q = sa.select(t.id).where(t.location_id == location_id, sa.func.lower(t.name) == name.lower())
    if exclude is not None:
        q = q.where(t.id != exclude)
    return db.one(q) is not None


def _clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("a group needs a name")
    if len(name) > NAME_LEN:
        raise ValueError(f"a group name is at most {NAME_LEN} characters")
    return name


def create(u: dict, loc: dict, name: str, cameras: list[str]) -> dict:
    """NameTaken / TooMany / ValueError (bad name or keys)."""
    name = _clean_name(name)
    servers = site_servers(loc["id"])
    keys = clean_keys(cameras, servers, known_cameras(servers))
    if _name_taken(loc["id"], name):
        raise NameTaken("a group with that name already exists")
    n = db.one(sa.select(sa.func.count().label("n")).select_from(db.site_camera_groups)
               .where(db.site_camera_groups.c.location_id == loc["id"]))
    if n and n["n"] >= MAX_GROUPS:
        raise TooMany(f"at most {MAX_GROUPS} camera groups per site: delete one first")
    now = time.time()
    row = {"location_id": loc["id"], "org_id": loc["org_id"], "name": name, "cameras": keys, "created_at": now, "updated_at": now,
           "created_by": u.get("email")}
    with db.engine().begin() as c:
        gid = int(c.execute(db.site_camera_groups.insert().values(**row)).inserted_primary_key[0])
    return _public({**row, "id": gid}, servers, {})


def update(row: dict, name: str | None, cameras: list[str] | None) -> tuple[dict, dict]:
    """Rename and/or replace the members (None = keep). (group, change) where change says what moved for the audit."""
    servers = site_servers(row["location_id"])
    vals: dict = {}
    if name is not None:
        vals["name"] = _clean_name(name)
        if vals["name"] != row["name"] and _name_taken(row["location_id"], vals["name"], exclude=row["id"]):
            raise NameTaken("a group with that name already exists")
    if cameras is not None:
        vals["cameras"] = clean_keys(cameras, servers, known_cameras(servers))
    before = _in_site(row["cameras"], servers)
    if vals:
        db.run(sa.update(db.site_camera_groups).where(db.site_camera_groups.c.id == row["id"]).values(**vals, updated_at=time.time()))
    after = vals.get("cameras", before)
    change = {"renamed_from": row["name"] if vals.get("name", row["name"]) != row["name"] else None,
              "added": [k for k in after if k not in before], "removed": [k for k in before if k not in after]}
    return public(row["location_id"], row["id"]), change


def delete(row: dict) -> int:
    """The group and its speaker bindings; returns how many speakers were bound to it."""
    with db.engine().begin() as c:
        n = c.execute(sa.delete(db.site_speaker_groups).where(db.site_speaker_groups.c.group_id == row["id"])).rowcount
        c.execute(sa.delete(db.site_camera_groups).where(db.site_camera_groups.c.id == row["id"]))
    return n or 0


def drop_location(conn, location_id: str) -> None:
    """The Site is deleted: its groups and the speaker bindings to them go with it (they describe this place only)."""
    conn.execute(sa.delete(db.site_speaker_groups).where(db.site_speaker_groups.c.location_id == location_id))
    conn.execute(sa.delete(db.site_camera_groups).where(db.site_camera_groups.c.location_id == location_id))


# ---------------------------------------------------------------- talk-down speakers

def summary_speakers(servers: dict[str, dict]) -> list[dict]:
    """The Site's speakers as their servers' heartbeat summaries list them (enabled ones; no address or credentials),
    server by server (by name), in each server's own order. Retired servers are left out (their tiles are too)."""
    out = []
    for s in sorted((s for s in servers.values() if not s.get("retired_at")), key=lambda s: (s["name"].casefold(), s["id"])):
        for sp in ((s.get("summary") or {}).get("speakers") or []):
            if not isinstance(sp, dict) or not sp.get("id"):
                continue
            out.append({"server_id": s["id"], "server_name": s["name"], "id": str(sp["id"]), "name": str(sp.get("name") or sp["id"]),
                        "camera_ids": [str(c) for c in sp.get("camera_ids") or []], "online": sp.get("online"),
                        "playing": sp.get("playing")})
    return out


def speaker_groups(location_id: str) -> dict[tuple[str, str], list[int]]:
    """(server id, speaker id) -> the ids of the groups it is bound to."""
    out: dict[tuple[str, str], list[int]] = {}
    for gid, refs in _bound(location_id).items():
        for b in refs:
            out.setdefault((b["server_id"], b["speaker_id"]), []).append(gid)
    return {k: sorted(v) for k, v in out.items()}


def speakers_by_camera(speakers: list[dict], groups: list[dict]) -> dict[str, list[dict]]:
    """Camera key -> the speakers that talk on it: each speaker's direct cameras (on its own server) plus every camera
    of each group it is bound to (any server of the Site). `speakers`: summary_speakers() rows with `group_ids`;
    `groups`: [{id, cameras}]. A speaker is listed once per camera, in `speakers` order, with how it got there
    (`via`: "camera" and/or the group ids)."""
    members = {g["id"]: g.get("cameras") or [] for g in groups}
    out: dict[str, list[dict]] = {}
    for sp in speakers:
        via: dict[str, list] = {}
        for cam in sp.get("camera_ids") or []:
            via.setdefault(f"{sp['server_id']}/{cam}", []).append("camera")
        for gid in sp.get("group_ids") or []:
            for key in members.get(gid, []):
                via.setdefault(key, []).append(gid)
        for key, how in via.items():
            out.setdefault(key, []).append({"server_id": sp["server_id"], "speaker_id": sp["id"], "name": sp["name"],
                                            "online": sp.get("online"), "via": how})
    return out


def site_speakers(location_id: str) -> dict:
    """{speakers: [... with group_ids and cameras (every key it talks on)], by_camera}: the Settings list and the union."""
    servers = site_servers(location_id)
    groups = list_for(location_id, servers)
    bound = speaker_groups(location_id)
    speakers = [{**sp, "group_ids": bound.get((sp["server_id"], sp["id"]), [])} for sp in summary_speakers(servers)]
    by_camera = speakers_by_camera(speakers, groups)
    for sp in speakers:
        sp["cameras"] = [k for k, lst in by_camera.items() if any(x["server_id"] == sp["server_id"] and x["speaker_id"] == sp["id"] for x in lst)]
    return {"speakers": speakers, "by_camera": by_camera}


def set_speaker_groups(location_id: str, server_id: str, speaker_id: str, group_ids: list[int]) -> tuple[list[int], list[int]]:
    """Bind the speaker to exactly these groups of the Site. Unknown: a group that isn't this Site's. (added, removed)."""
    wanted = list(dict.fromkeys(group_ids))
    have = {g["id"] for g in _rows(location_id)}
    bad = [g for g in wanted if g not in have]
    if bad:
        raise Unknown(f"no camera group {bad[0]} in this site")
    t = db.site_speaker_groups
    with db.engine().begin() as c:
        before = {r["group_id"] for r in c.execute(sa.select(t.c.group_id).where(
            t.c.server_id == server_id, t.c.speaker_id == speaker_id)).mappings()}
        # bindings left by a server that moved here from another Site go too: the speaker's groups are this Site's now
        c.execute(sa.delete(t).where(t.c.server_id == server_id, t.c.speaker_id == speaker_id))
        for gid in wanted:
            c.execute(t.insert().values(location_id=location_id, server_id=server_id, speaker_id=speaker_id, group_id=gid))
    return [g for g in wanted if g not in before], sorted(before - set(wanted))
