"""Central recording: what the Site's admins may do with a central instance's cameras, and the firewall that follows.

A central instance (hosts.py) runs with its own API on loopback (NVR_DIRECT_ENABLED=0): every camera change reaches it
through the hub, either the console proxy (proxy.py, /s/<server>/api/...) or a fleet action (fleet_actions.py). Before
such a request goes down the tunnel, check() applies, for everyone (hub administrators too):

  camera limit   central_instances.camera_limit (NULL = none): a camera that is new or switched on again is refused
                 (409) once the instance has that many enabled cameras (the hub's camera registry, plus cameras
                 accepted in the last moments that no list shows yet). Lowering the limit keeps existing cameras.
  addresses      where the instance connects to each camera: its `public_host` (port forwarding) or else its `host`.
                 A private address (10/8, 172.16/12, 192.168/16, 100.64/10) must be inside one of the instance's Site
                 networks (400): one customer never reaches another's VPN subnet or the host's own networks. A public
                 IP or DNS name is allowed, unless it is the hub's, a host's, the datacenter's or the FusionHub's own
                 address, or another live instance's camera address (409), or a name that spells out an IP such as
                 10.20.8.1.nip.io (400). The host checks what names resolve to again (axiom_host.py).

Guarded requests: PUT /api/cameras/<id> (create or update), POST /api/cameras, POST /api/config/import and
/api/config/merge (cameras in a backup or a camera handoff), and a backup restored from the hub (backups.restore). After a camera change succeeds (and whenever a central
server's heartbeat shows its camera list changed), sync() reads the instance's GET /api/cameras over its tunnel (that
route never returns passwords), refreshes the hub's camera registry, recomputes the automatic addresses (the public
IPs and DNS names its enabled cameras use) and, when the firewall (Site networks + automatic) differs from what the
host has, sends set_camera_network: debounced per instance (SYNC_DELAY_S), audited. Disabling or removing the last
camera at an address closes it again. Request bodies may carry camera passwords: they are parsed here and never logged
or stored.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import time
from urllib.parse import unquote, urlsplit

import sqlalchemy as sa

from . import cameras, db, hosts
from .config import settings

log = logging.getLogger("hub.central_cameras")

SYNC_DELAY_S = 10.0         # a burst of camera changes (an import, a few edits) makes one firewall change
PENDING_TTL_S = 120.0       # a camera accepted through the hub counts against the limit until a camera list shows it
FETCH_TIMEOUT_S = 15.0
SYNC_USER = {"id": None, "email": "(automatic: camera addresses)"}
FETCH_HEADERS = {"x-hub-user": "hub (camera addresses)", "x-hub-role": "viewer"}

_CAMERA = re.compile(r"^/api/cameras/([^/]+)/?$")
_CONFIG = {"/api/config/import": "import", "/api/config/merge": "merge"}


class Refused(Exception):
    """The camera change breaks a rule: `status` (400 / 409) and a message safe to show."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ---------------------------------------------------------------- which requests

def write_kind(method: str, path: str) -> str | None:
    """"camera", "import" or "merge" for a request that adds or changes cameras; None for anything else."""
    m = method.upper()
    p = path.rstrip("/") or path
    if m == "PUT" and _CAMERA.match(path):
        return "camera"
    if m == "POST" and p == "/api/cameras":
        return "camera"
    if m == "POST" and p in _CONFIG:
        return _CONFIG[p]
    return None


def changes_cameras(method: str, path: str) -> bool:
    """write_kind, or a camera removed (DELETE /api/cameras/<id>): the firewall may follow."""
    return write_kind(method, path) is not None or (method.upper() == "DELETE" and bool(_CAMERA.match(path)))


def instance_for_server(server_id: str | None) -> dict | None:
    """The live central instance whose server this is (None for every other server)."""
    if not server_id:
        return None
    return db.one(sa.select(db.central_instances).where(db.central_instances.c.server_id == server_id,
                                                        db.central_instances.c.state.in_(hosts.LIVE_STATES)))


# ---------------------------------------------------------------- the rules

def _cams_in(kind: str, path: str, body) -> list[dict]:
    """[{id, enabled, host, public_host, has_public}] the request writes (never the password)."""
    if isinstance(body, (bytes, bytearray)):
        try:
            body = json.loads(body.decode() or "null")
        except (ValueError, UnicodeDecodeError):
            raise Refused(400, "the request body is not valid JSON") from None
    if not isinstance(body, dict):
        raise Refused(400, "the request body is not valid")
    if kind == "camera":
        m = _CAMERA.match(path)
        items = [{**body, "id": unquote(m.group(1)) if m else body.get("id")}]
    else:
        data = body.get("data")
        items = data.get("cameras") if isinstance(data, dict) else None
        if items is None:
            return []
        if not isinstance(items, list):
            raise Refused(400, "the cameras in the request are not a list")
    out = []
    for c in items:
        if not isinstance(c, dict):
            raise Refused(400, "a camera in the request is not valid")
        host, pub = c.get("host"), c.get("public_host")
        if not isinstance(host, (str, type(None))) or not isinstance(pub, (str, type(None))):
            raise Refused(400, "a camera address must be text")
        enabled = camera_enabled(c.get("enabled", True))
        if enabled is None:
            raise Refused(400, "a camera's enabled must be true or false")
        out.append({"id": str(c.get("id") or ""), "enabled": enabled,
                    "host": (host or "").strip(), "public_host": (pub or "").strip(), "has_public": "public_host" in c})
    return out


def camera_enabled(v) -> bool | None:
    """A camera's `enabled` as the server reads it (backend/nvr/siteconfig.py camera_enabled: keep the two the same):
    true / false, 1 / 0 or those as text; None for anything else (refused, so the hub never counts a camera as off
    that the server switches on)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in ("1", "true", "0", "false"):
        return v.strip().lower() in ("1", "true")
    return None


def _norm(addr: str) -> str:
    return addr.strip().lower().rstrip(".")


def _host_of(addr: str | None) -> str:
    """The address in a setting that may be a bare IP or name, name:port or a URL."""
    a = (addr or "").strip()
    if "://" in a:
        return _norm(urlsplit(a).hostname or "")
    if a.count(":") == 1:
        a = a.split(":", 1)[0]
    return _norm(a)


def _hub_addresses() -> tuple[set[str], set[str]]:
    """(IPs, names) of the hub's own systems: its public name, the datacenter IP, every host's address and the
    FusionHub (the hub's fusionhub_address and each host's own)."""
    ips: set[str] = set()
    names: set[str] = set()
    found = [urlsplit(settings.public_url).hostname, settings.datacenter_ip, settings.fusionhub_address]
    for h in db.rows(sa.select(db.hosts.c.agent_ip, db.hosts.c.fusionhub)):
        found += [h["agent_ip"], h["fusionhub"]]
    for a in found:
        a = _host_of(a)
        if a:
            (ips if hosts._is_ip(a) else names).add(a)
    return ips, names


def _others(ci_id: str) -> list[dict]:
    """Every other live instance's camera addresses: its firewall lists, plus `resolved`, the addresses its host last
    said its DNS names resolve to (the host compares resolved addresses again: axiom_host.resolve_camera_hosts)."""
    out = []
    for o, fw in hosts.taken_elsewhere(ci_id):
        got = (o.get("info") or {}).get("host_ips") if isinstance(o.get("info"), dict) else None
        resolved = {ip for ips in (got.values() if isinstance(got, dict) else []) if isinstance(ips, list) for ip in ips}
        out.append({**fw, "resolved": resolved})
    return out


# a DNS name that spells out an IP (wildcard DNS such as nip.io / sslip.io: 10.20.8.1.nip.io, 10-20-8-1.sslip.io)
_DOTTED_QUAD = re.compile(r"(?:^|\.)(?:\d{1,3}\.){3}\d{1,3}(?:\.|$)")
_DASHED_QUAD = re.compile(r"(?:^|[^0-9])\d{1,3}-\d{1,3}-\d{1,3}-\d{1,3}(?:[^0-9]|$)")


def _embeds_ip(name: str) -> bool:
    return bool(_DOTTED_QUAD.search(name) or any(_DASHED_QUAD.search(lb) for lb in name.split(".")))


def classify(addr: str) -> tuple[str, str]:
    """("private" | "public_ip" | "host", normalized address); Refused (400) for anything a camera can't be at."""
    a = _norm(addr)
    try:
        ip = ipaddress.ip_address(a)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.version != 4:
            raise Refused(400, f"{addr}: a camera needs an IPv4 address or a DNS name here")
        if any(ip in n for n in hosts._CAMERA_NETS):
            return "private", str(ip)
        # the same rule as a public IP in the Site networks (hosts.check_camera_network)
        if ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_link_local or ip.is_reserved:
            raise Refused(400, f"{addr} is not a usable camera address")
        return "public_ip", str(ip)
    if not hosts._is_hostname(a) or a.endswith(".localhost"):
        raise Refused(400, f"{addr} is not an IP address or a DNS name like cam1.example.net")
    if _embeds_ip(a):
        raise Refused(400, f"{addr} spells out an IP address: use the address itself, or the router's own DNS name")
    return "host", a


def _check_address(addr: str, site: dict, hub: tuple[set[str], set[str]], others: list[dict]) -> tuple[str, str]:
    kind, a = classify(addr)
    if kind == "private":
        ip = ipaddress.ip_address(a)
        nets = site.get("subnets") or []
        if any(ip in ipaddress.ip_network(s) for s in nets):
            return kind, a
        if nets:
            raise Refused(400, f"{a} is outside this Site's camera network {' / '.join(nets)}")
        raise Refused(400, f"{a} is a private address and this Site has no camera network for it: use the router's public "
                           "address (port forwarding), or ask Axiom Vision to add the Site's network")
    if a in (hub[0] if kind == "public_ip" else hub[1]):
        raise Refused(409, f"{a} is an address of Axiom Vision's own systems, not a camera")
    for fw in others:
        if a in fw["public_ips" if kind == "public_ip" else "hosts"] or (kind == "public_ip" and a in fw.get("resolved", ())):
            raise Refused(409, f"{a} is already a camera address of another Site")
    return kind, a


_pending: dict[str, dict[str, float]] = {}     # server id -> {camera id (or "merge:<id>"): until}


def _counted(server_id: str) -> set[str]:
    now = time.time()
    rows = db.rows(sa.select(db.cameras.c.camera_id).where(db.cameras.c.server_id == server_id, db.cameras.c.enabled == True,  # noqa: E712
                                                            db.cameras.c.missing_since.is_(None)))
    pend = {k for k, until in (_pending.get(server_id) or {}).items() if until > now}
    return {r["camera_id"] for r in rows} | pend


def release(server_id: str | None, keys: list[str] | None) -> None:
    """The guarded request failed: its cameras no longer count."""
    if server_id and keys:
        mine = _pending.get(server_id) or {}
        for k in keys:
            mine.pop(k, None)


def limit_text(limit: int) -> str:
    return f"This Site's central recording allows {limit} camera{'' if limit == 1 else 's'}; ask Axiom Vision to raise it."


async def check(ci: dict, kind: str, path: str, body) -> list[str]:
    """Refused unless the request keeps to the camera limit and the address rules. Returns the keys of the cameras it
    adds (they count against the limit until a camera list shows them; release() them if the request fails)."""
    server_id = ci["server_id"] or ""
    cams = _cams_in(kind, path, body)
    if not cams:
        return []
    stored: dict[str, dict] | None = None
    if kind == "camera" and not cams[0]["has_public"]:
        # PUT without public_host keeps the stored one: where the instance really connects
        live = await live_cameras(server_id)
        stored = {str(c.get("id")): c for c in live or [] if isinstance(c, dict)}
    site = hosts.camera_network(ci)
    hub = _hub_addresses()
    others = _others(ci["id"])
    union = hosts.firewall(ci)
    added: set[str] = set()
    for c in cams:
        addr = c["public_host"]
        if not addr and stored is not None and not c["has_public"]:
            addr = str((stored.get(c["id"]) or {}).get("public_host") or "")
        addr = addr or c["host"]
        if not addr:
            continue   # the server refuses a camera without an address itself
        k, a = _check_address(addr, site, hub, others)
        if k != "private" and a not in union["public_ips"] and a not in union["hosts"]:
            added.add(a)
    n = sum(len(v) for v in union.values()) + len(added)
    if added and n > hosts.MAX_CAMERA_ENTRIES:
        raise Refused(409, f"this Site's central recording would reach {n} camera addresses (at most {hosts.MAX_CAMERA_ENTRIES}): "
                           "put cameras behind one router address, or ask Axiom Vision")
    counted = _counted(server_id)
    new = []
    for c in cams:
        if not c["enabled"]:
            continue
        key = f"merge:{c['id']}" if kind == "merge" else c["id"]
        if kind == "merge" or c["id"] not in counted:
            new.append(key)
    limit = ci.get("camera_limit")
    if new and limit is not None and len(counted) + len(set(new)) > limit:
        raise Refused(409, limit_text(limit))
    if new:
        until = time.time() + PENDING_TTL_S
        _pending.setdefault(server_id, {}).update({k: until for k in new})
    return new


# ---------------------------------------------------------------- keeping the firewall in step

async def live_cameras(server_id: str) -> list[dict] | None:
    """The instance's GET /api/cameras (public fields: no passwords), or None when it can't be read now."""
    from .agents import registry   # (agents imports this module)
    conn = registry.get(server_id) if server_id else None
    if conn is None:
        return None
    try:
        status, raw = await conn.call("GET", "/api/cameras", "", dict(FETCH_HEADERS), None, FETCH_TIMEOUT_S)
        data = json.loads(raw.decode() or "null") if status == 200 else None
    except Exception as e:   # offline, timed out, junk: the next trigger tries again
        log.info("camera list of %s: %s", server_id, e)
        return None
    return data if isinstance(data, list) else None


def auto_of(cams: list[dict], ci: dict) -> tuple[dict, list[str]]:
    """({public_ips, hosts} the enabled cameras connect to, [addresses left out and why]). Private addresses are the
    Site networks' business; an address the rules refuse (another Site's, the hub's) is never opened."""
    out: dict[str, list[str]] = {"public_ips": [], "hosts": []}
    skipped: list[str] = []
    hub = _hub_addresses()
    others = _others(ci["id"])
    for c in cams:
        if not isinstance(c, dict) or c.get("enabled") in (False, 0):
            continue
        addr = str(c.get("public_host") or c.get("host") or "").strip()
        if not addr:
            continue
        try:
            kind, a = classify(addr)
            if kind == "private":
                continue
            _check_address(a, {}, hub, others)
        except Refused as e:
            skipped.append(e.detail)
            continue
        key = "public_ips" if kind == "public_ip" else "hosts"
        if a not in out[key]:
            out[key].append(a)
    return out, skipped


def _registry_sync(server_id: str, cams: list[dict], read_at: float) -> None:
    """The camera list read at `read_at` into the hub's registry. A camera accepted through the hub stops counting as
    pending once the list shows it, or when it was accepted before the read began (the list then has it, or its
    request failed); one accepted while the list was being read still counts."""
    server = db.one(sa.select(db.sites).where(db.sites.c.id == server_id))
    if not server:
        return
    on = [{"id": c.get("id"), "name": c.get("name")} for c in cams if isinstance(c, dict) and c.get("enabled") not in (False, 0)]
    off = [{"id": c.get("id"), "name": c.get("name")} for c in cams if isinstance(c, dict) and c.get("enabled") in (False, 0)]
    cameras.sync(server, on, full=True, disabled=off, source="api")
    mine = _pending.get(server_id) or {}
    listed = {str(c["id"]) for c in on}
    for k, until in list(mine.items()):
        if until - PENDING_TTL_S < read_at or k in listed:
            mine.pop(k, None)
    if not mine:
        _pending.pop(server_id, None)


async def sync(ci_id: str) -> str:
    """Read the instance's cameras, store its automatic addresses and send the host the firewall if it changed.
    Returns what happened: offline | unchanged | stored | legacy | pushed | failed | invalid | gone."""
    ci = hosts.get_instance(ci_id)
    if not ci or ci["state"] not in hosts.LIVE_STATES or ci["state"] == "deleting" or not ci["server_id"]:
        return "gone"
    read_at = time.time()
    cams = await live_cameras(ci["server_id"])
    if cams is not None:
        try:
            _registry_sync(ci["server_id"], cams, read_at)
        except Exception:
            log.exception("camera registry of %s", ci["server_id"])
    async with hosts.network_lock(ci_id):
        ci = hosts.get_instance(ci_id)
        if not ci or ci["state"] not in hosts.LIVE_STATES or ci["state"] == "deleting":
            return "gone"
        if cams is None:
            # the instance can't be asked now: still send what waits for the host (new Site networks, a failed
            # push), with the camera addresses as last known
            cn0 = ci.get("camera_network") if isinstance(ci.get("camera_network"), dict) else {}
            if hosts.applied_state(ci)[0] != "pending" and not cn0.get("sync_error"):
                return "offline"
            auto, skipped = hosts.auto_addresses(ci), []
        else:
            auto, skipped = auto_of(cams, ci)
        if skipped:
            log.warning("central instance %s: camera addresses not opened: %s", ci_id, "; ".join(skipped)[:500])
        site = hosts.camera_network(ci)
        state, applied = hosts.applied_state(ci)
        cn = dict(ci["camera_network"]) if isinstance(ci.get("camera_network"), dict) else dict(site)
        cn |= {**site, "auto": auto}
        if state == "legacy":   # from before automatic addresses: shown, never pushed until an administrator saves
            if cn != ci.get("camera_network"):
                hosts._set(ci_id, camera_network=cn)
            return "legacy"
        try:
            union = hosts.check_camera_network(*(hosts.merge_network(site, auto)[k] for k in hosts.CAMERA_KEYS))
        except ValueError as e:
            hosts._set(ci_id, camera_network={**cn, "sync_error": f"too many camera addresses: {e}"})
            log.warning("central instance %s: firewall not changed: %s", ci_id, e)
            return "invalid"
        if state == "ok" and hosts.same_network(applied, union):
            cn.pop("sync_error", None)
            if cn != ci.get("camera_network"):
                hosts._set(ci_id, camera_network=cn)
                return "stored"
            return "unchanged"
        try:
            res = await hosts.registry.command(ci["host_id"], "set_camera_network", {"id": ci_id, **union})
            err = None if res.get("ok") else str(res.get("detail") or "the host refused")
        except hosts.HostError as e:
            res, err = {}, str(e)
        if err:
            hosts._set(ci_id, camera_network={**cn, "sync_error": err[:300]})
            log.warning("central instance %s: camera addresses not applied: %s", ci_id, err)
            return "failed"
        cn.pop("sync_error", None)
        vals: dict = {"camera_network": {**cn, "applied": union}}
        if isinstance(res.get("instance"), dict):
            vals |= {"info": {**(ci.get("info") or {}), "host_ips": res["instance"].get("host_ips")}, "info_at": time.time()}
        hosts._set(ci_id, **vals)
    loc = db.one(sa.select(db.locations.c.name).where(db.locations.c.id == ci["location_id"])) or {"name": ci["location_id"]}
    hosts._audit(SYNC_USER, ci["org_id"], ci["server_id"], f"central recording camera addresses: {loc['name']}",
                 {"location_id": ci["location_id"], "instance_id": ci_id, "from": applied, "to": union, "auto": auto, "automatic": True})
    log.info("central instance %s: firewall now %s", ci_id, union)
    return "pushed"


_waiting: set[str] = set()
_running: set[str] = set()
_again: set[str] = set()
_tasks: set[asyncio.Task] = set()


def schedule(ci_id: str) -> None:
    """Sync the instance in SYNC_DELAY_S; triggers until then make one sync, a trigger during one makes another."""
    if ci_id in _waiting:
        return
    if ci_id in _running:
        _again.add(ci_id)
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return   # no loop (a command-line tool): the hub's sweep picks it up
    _waiting.add(ci_id)
    t = loop.create_task(_later(ci_id), name=f"central-cameras-{ci_id}")
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def _later(ci_id: str) -> None:
    try:
        while True:
            await asyncio.sleep(SYNC_DELAY_S)
            _waiting.discard(ci_id)
            _running.add(ci_id)
            try:
                await sync(ci_id)
            except Exception:
                log.exception("camera address sync of %s", ci_id)
            finally:
                _running.discard(ci_id)
            if ci_id not in _again:
                return
            _again.discard(ci_id)
            _waiting.add(ci_id)
    finally:
        _waiting.discard(ci_id)


def after_write(server_id: str | None) -> None:
    """A camera change went through to this server: if it is a central instance, sync its firewall soon."""
    try:
        ci = instance_for_server(server_id)
        if ci:
            schedule(ci["id"])
    except Exception:
        log.exception("after camera change on %s", server_id)


_seen: dict[str, frozenset] = {}


def on_heartbeat(server_id: str, summary: dict) -> None:
    """A server's heartbeat: when its camera list (ids, enabled) changed since the last one, and it is a central
    instance, sync. The first heartbeat after a hub start counts as a change."""
    cams = summary.get("cameras")
    if not isinstance(cams, list):
        return
    off = summary.get("disabled") if isinstance(summary.get("disabled"), list) else []
    sig = frozenset([("on", str(c.get("id") if isinstance(c, dict) else c)) for c in cams] +
                    [("off", str(c.get("id") if isinstance(c, dict) else c)) for c in off])
    if _seen.get(server_id) == sig:
        return
    _seen[server_id] = sig
    after_write(server_id)


def sweep() -> None:
    """Every 30 s (api._sweeper): instances whose firewall waits to be sent (set from the command line) or whose last
    sync failed, on an online host, are synced again."""
    for ci in db.rows(sa.select(db.central_instances).where(db.central_instances.c.state.in_(("provisioning", "running", "failed")),
                                                            db.central_instances.c.server_id.is_not(None))):
        cn = ci.get("camera_network")
        if not isinstance(cn, dict) or "applied" not in cn:
            continue
        if (cn.get("applied") is None or cn.get("sync_error")) and hosts.registry.online(ci["host_id"]):
            schedule(ci["id"])
