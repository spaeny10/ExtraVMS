"""Central recording: datacenter hosts and the server instances the hub places on them (hub administrators only).

A host is one machine (Docker on Ubuntu, GPUs shared by containers) running the `axiom-host` agent
(tools/central/axiom_host.py). The agent dials `WS /host-agent` with `Authorization: Bearer <host token>` (hashed at
rest like a server's device token) and keeps the socket open. Protocol (JSON text frames, proto 1):

  host -> hub  {"t": "hello", "proto": 1, "hostname", "version", "capacity": {cpus, load, ram_gb: {total, free},
                 gpus: [{index, name, mem_total_mb, mem_used_mb, util}], disks: [{path, total_gb, free_gb}], instances: int}}
               {"t": "heartbeat", "capacity": {...}, "instances": [{id, location_id, name, state, quota_gb, used_gb, gpu, mode}]}
               {"t": "result", "id": int, "ok": bool, "detail": str, "instance"?: {...}}
               {"t": "ping"}                                   -> hub answers {"t": "pong"}
  hub -> host  {"t": "cmd", "id": int, "op": create_instance | delete_instance | set_quota | restart_instance | list,
                 "args": {...}}
    create_instance  {id, location, name, mode, subnet, public_ip, quota_gb, gpu, enroll_token, hub_url, vlm_url}
    delete_instance  {id, purge}        purge false = the recordings and database stay on the host
    set_quota        {id, quota_gb}
    restart_instance {id}
    list             {}

A central instance is one isolated server container recording one customer Site's cameras, over SpeedFusion (mode
vpn: the BR1's LAN is 10.20.<site_number>.0/24) or port forwards locked to the datacenter's IP (mode forward: the
Site's public IP). provision() places it (host with room, GPU), mints a single-use enrollment token bound to the
Site, and asks the host to create it; the instance then dials /agent with `Authorization: Enroll <token>` and
becomes an ordinary server of that Site (agents.py -> enroll_check / enroll_consume). Every change is audited.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import time

import sqlalchemy as sa
from fastapi import WebSocket, WebSocketDisconnect

from . import alerts, db
from .config import settings

log = logging.getLogger("hub.hosts")

PROTO = 1
OPS = ("create_instance", "delete_instance", "set_quota", "restart_instance", "list")
STATES = ("provisioning", "running", "failed", "deleting", "deleted")
LIVE_STATES = ("provisioning", "running", "failed", "deleting")   # everything but deleted: holds its site number
MODES = ("vpn", "forward")
SITE_NUMBERS = range(1, 251)
# Port-forward mode: camera k is forwarded from these outside ports (+k) on the BR1 (the Peplink sheet, hub UI central.ts)
RTSP_BASE, ONVIF_BASE = 5540, 8080
HUB_ORG = "_hub"            # org_id of hub-level alert rows (host_offline): no customer ever matches it
MAX_FRAME = 256 * 1024      # a host frame bigger than this is dropped (capacity and instance lists are small)
_BAD_ENROLL_LOGGED: set[str] = set()


class HostError(Exception):
    """A host command could not be carried out: host offline, disconnected, timed out, or a bad request."""


class Conflict(Exception):
    """Provisioning refused for a reason the caller can fix (409)."""


# ---------------------------------------------------------------- the /host-agent socket

class HostConn:
    def __init__(self, ws: WebSocket, host: dict) -> None:
        self.ws = ws
        self.host = host
        self.host_id = host["id"]
        self.hello: dict = {}
        self.last_seen = time.time()
        self.pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._lock = asyncio.Lock()

    @property
    def ip(self) -> str | None:
        return self.ws.client.host if self.ws.client else None

    async def send(self, frame: dict) -> None:
        async with self._lock:
            await self.ws.send_text(json.dumps(frame, separators=(",", ":")))

    async def command(self, op: str, args: dict, timeout: float) -> dict:
        cid = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[cid] = fut
        try:
            await self.send({"t": "cmd", "id": cid, "op": op, "args": args})
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise HostError(f"the host did not answer {op} within {int(timeout)} s")
        except HostError:
            raise
        except Exception as e:   # the socket broke while sending
            raise HostError(f"could not reach the host: {e}")
        finally:
            self.pending.pop(cid, None)

    def fail_pending(self, reason: str) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(HostError(reason))
        self.pending.clear()


class HostRegistry:
    def __init__(self) -> None:
        self.by_host: dict[str, HostConn] = {}
        self.started_at = time.time()

    def online(self, host_id: str) -> bool:
        return host_id in self.by_host

    async def serve(self, ws: WebSocket) -> None:
        scheme, _, cred = ws.headers.get("authorization", "").partition(" ")
        host = db.one(sa.select(db.hosts).where(db.hosts.c.token_hash == db.token_hash(cred.strip()))) if scheme == "Bearer" and cred.strip() else None
        if not host:
            await ws.close(code=4401, reason="unknown host token")   # before accept: the handshake is refused (403)
            return
        await ws.accept()
        conn = HostConn(ws, host)
        old = self.by_host.get(conn.host_id)
        if old is not None:
            try:
                await old.ws.close(code=4409, reason="replaced by a newer connection")
            except Exception:
                pass
        self.by_host[conn.host_id] = conn
        try:
            await self._loop(conn)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("host %s: connection failed", conn.host_id)
        finally:
            await self._detach(conn)

    async def _loop(self, conn: HostConn) -> None:
        while True:
            m = await conn.ws.receive()
            if m["type"] == "websocket.disconnect":
                return
            text = m.get("text")
            if text is None or len(text) > MAX_FRAME:
                continue
            try:
                frame = json.loads(text)
            except ValueError:
                continue
            if not isinstance(frame, dict):
                continue
            t = frame.get("t")
            conn.last_seen = time.time()
            if t == "hello":
                self._on_hello(conn, frame)
            elif t == "heartbeat":
                self._on_heartbeat(conn, frame)
            elif t == "result":
                fut = conn.pending.get(frame.get("id")) if isinstance(frame.get("id"), int) else None
                if fut is not None and not fut.done():
                    fut.set_result(frame)
            elif t == "ping":
                await conn.send({"t": "pong"})

    def _on_hello(self, conn: HostConn, frame: dict) -> None:
        conn.hello = frame
        vals = {"online": True, "last_seen_at": time.time(), "agent_ip": conn.ip,
                "hostname": str(frame.get("hostname") or "")[:120] or None, "version": str(frame.get("version") or "")[:32] or None}
        if isinstance(frame.get("capacity"), dict):
            vals["capacity"] = frame["capacity"]
        db.run(sa.update(db.hosts).where(db.hosts.c.id == conn.host_id).values(**vals))
        alerts.close(_alert_subject(conn.host), "host_offline")
        log.info("host %s (%s) connected from %s", conn.host_id, conn.host["name"], conn.ip)

    def _on_heartbeat(self, conn: HostConn, frame: dict) -> None:
        vals: dict = {"online": True, "last_seen_at": time.time()}
        if isinstance(frame.get("capacity"), dict):
            vals["capacity"] = frame["capacity"]
        db.run(sa.update(db.hosts).where(db.hosts.c.id == conn.host_id).values(**vals))
        now = time.time()
        for inst in (frame.get("instances") or [])[:1000] if isinstance(frame.get("instances"), list) else []:
            if not isinstance(inst, dict) or not inst.get("id"):
                continue
            info = {k: inst.get(k) for k in ("state", "used_gb", "quota_gb", "gpu", "mode", "location_id", "name")}
            # only this host's own instances: a host can never report on (or overwrite) another host's rows
            db.run(sa.update(db.central_instances).where(db.central_instances.c.id == str(inst["id"])[:24],
                                                         db.central_instances.c.host_id == conn.host_id).values(info=info, info_at=now))

    async def _detach(self, conn: HostConn) -> None:
        conn.fail_pending("the host disconnected")
        if self.by_host.get(conn.host_id) is conn:
            del self.by_host[conn.host_id]
            db.run(sa.update(db.hosts).where(db.hosts.c.id == conn.host_id).values(online=False, last_seen_at=time.time()))
            log.info("host %s disconnected", conn.host_id)

    async def command(self, host_id: str, op: str, args: dict, timeout: float | None = None) -> dict:
        """Send one command and wait for its result frame ({"t": "result", "id", "ok", "detail", "instance"?})."""
        if op not in OPS:
            raise HostError(f"unknown host command {op}")
        conn = self.by_host.get(host_id)
        if conn is None:
            raise HostError("the host is offline")
        return await conn.command(op, args, timeout or (settings.host_create_timeout_s if op == "create_instance" else settings.host_command_timeout_s))

    async def disconnect(self, host_id: str, reason: str) -> None:
        conn = self.by_host.get(host_id)
        if conn is not None:
            try:
                await conn.ws.close(code=4401, reason=reason)
            except Exception:
                pass

    async def sweep(self) -> None:
        """Close hosts that stopped heartbeating; open host_offline for hosts gone longer than offline_after_s."""
        cutoff = time.time() - settings.offline_after_s
        for conn in list(self.by_host.values()):
            if conn.last_seen < cutoff:
                try:
                    await conn.ws.close(code=4408, reason="no heartbeat")
                except Exception:
                    pass
        if time.time() - self.started_at > 120:   # not in the first two minutes after a restart
            for h in db.rows(sa.select(db.hosts).where(db.hosts.c.online == False)):  # noqa: E712
                if h["last_seen_at"] and h["last_seen_at"] < cutoff and h["id"] not in self.by_host:
                    alerts.open(_alert_subject(h), "host_offline", "", {"name": h["name"], "last_seen_at": h["last_seen_at"]})


registry = HostRegistry()


def _alert_subject(host: dict) -> dict:
    """alerts.open/close take a server-shaped dict: a host alert is a hub-level row (org HUB_ORG, site_id = host id)."""
    return {"id": host["id"], "org_id": HUB_ORG, "name": host.get("name") or host["id"], "location_id": None, "host": True}


# ---------------------------------------------------------------- hosts (hub administrators)

def create_host(name: str, notes: str | None, fusionhub: str | None) -> tuple[dict, str]:
    token = db.new_token()
    row = {"id": db.new_id("h_"), "name": name.strip()[:120], "token_hash": db.token_hash(token), "created_at": time.time(), "online": False,
           "last_seen_at": None, "hostname": None, "version": None, "capacity": None, "notes": notes, "fusionhub": fusionhub, "agent_ip": None}
    db.insert(db.hosts, row)
    return row, token


async def rotate_host_token(host_id: str) -> str:
    """A new token; the live connection is closed (the agent must be restarted with the new token file)."""
    token = db.new_token()
    db.run(sa.update(db.hosts).where(db.hosts.c.id == host_id).values(token_hash=db.token_hash(token)))
    await registry.disconnect(host_id, "token rotated")
    return token


async def delete_host(host_id: str) -> None:
    n = db.one(sa.select(sa.func.count().label("n")).select_from(db.central_instances).where(
        db.central_instances.c.host_id == host_id, db.central_instances.c.state.in_(LIVE_STATES)))["n"]
    if n:
        raise Conflict(f"the host still has {n} instance{'s' if n > 1 else ''}: remove them first")
    with db.engine().begin() as c:
        c.execute(sa.delete(db.hosts).where(db.hosts.c.id == host_id))
        c.execute(sa.delete(db.alerts).where(db.alerts.c.org_id == HUB_ORG, db.alerts.c.site_id == host_id))
    await registry.disconnect(host_id, "host removed at the hub")


def install_command(token_file: str = "/etc/axiom/host-token") -> str:
    return f"python3 axiom_host.py run --hub {host_agent_url()} --token-file {token_file}"


def _ws_base() -> str:
    base = settings.public_url.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):]
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):]
    return base


def host_agent_url() -> str:
    return _ws_base() + "/host-agent"


def hub_agent_url() -> str:
    """Where a central instance's hub agent dials (the same /agent every server uses)."""
    return _ws_base() + "/agent"


def host_out(h: dict, instances: list[dict] | None = None, open_alerts: dict | None = None) -> dict:
    mine = [i for i in (instances or []) if i["host_id"] == h["id"] and i["state"] in LIVE_STATES]
    return {"id": h["id"], "name": h["name"], "created_at": h["created_at"], "online": bool(h["online"]) and registry.online(h["id"]),
            "last_seen_at": h["last_seen_at"], "hostname": h["hostname"], "version": h["version"], "capacity": h["capacity"] or None,
            "notes": h["notes"], "fusionhub": h["fusionhub"], "agent_ip": h["agent_ip"], "instances": len(mine),
            "quota_gb": sum(int(i["quota_gb"] or 0) for i in mine),
            "offline_since": (open_alerts or {}).get(h["id"])}


def list_hosts() -> list[dict]:
    hosts = db.rows(sa.select(db.hosts).order_by(db.hosts.c.name))
    inst = db.rows(sa.select(db.central_instances).where(db.central_instances.c.state.in_(LIVE_STATES)))
    open_alerts = {r["site_id"]: r["opened_at"] for r in db.rows(sa.select(db.alerts.c.site_id, db.alerts.c.opened_at).where(
        db.alerts.c.org_id == HUB_ORG, db.alerts.c.kind == "host_offline", db.alerts.c.closed_at.is_(None)))}
    return [host_out(h, inst, open_alerts) for h in hosts]


# ---------------------------------------------------------------- placement

def _model_re(model: str) -> re.Pattern | None:
    m = (model or "").strip()
    # "A10" matches "NVIDIA A10" and "A10G" but not "A100" or "A1000"
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(m)}(?![0-9])", re.I) if m else None


def _gpus(capacity: dict | None) -> list[dict]:
    out = []
    for g in (capacity or {}).get("gpus") or []:
        if isinstance(g, dict) and isinstance(g.get("index"), int):
            out.append(g)
    return out


def _mem_frac(g: dict) -> float:
    total = float(g.get("mem_total_mb") or 0)
    return float(g.get("mem_used_mb") or 0) / total if total > 0 else 1.0


def pick_gpu(capacity: dict | None, assigned: dict[int, int] | None = None) -> int | None:
    """The host GPU a new instance's YOLO runs on: a `central_gpu_prefer` (A10) card if the host has one, else the
    least used card that isn't `central_gpu_avoid` (the A40 serves vLLM), else the least used card at all; None = no
    GPU reported. Ties go to the card with the fewest instances already, then the least memory in use."""
    gpus = _gpus(capacity)
    if not gpus:
        return None
    assigned = assigned or {}
    key = lambda g: (assigned.get(g["index"], 0), _mem_frac(g), g["index"])  # noqa: E731
    prefer, avoid = _model_re(settings.central_gpu_prefer), _model_re(settings.central_gpu_avoid)
    pool = [g for g in gpus if prefer and prefer.search(str(g.get("name") or ""))]
    if not pool:
        pool = [g for g in gpus if not (avoid and avoid.search(str(g.get("name") or "")))] or gpus
    return min(pool, key=key)["index"]


def _assigned_gpus(host_id: str) -> dict[int, int]:
    out: dict[int, int] = {}
    for i in db.rows(sa.select(db.central_instances.c.gpu).where(db.central_instances.c.host_id == host_id,
                                                                  db.central_instances.c.state.in_(LIVE_STATES))):
        if i["gpu"] is not None:
            out[i["gpu"]] = out.get(i["gpu"], 0) + 1
    return out


def host_room(host: dict) -> dict:
    """What a host has left: free GPU memory (MB), its live instances, and disk room (GB) = the largest disk's free
    space minus what its instances may still grow into (quota - used)."""
    cap = host.get("capacity") or {}
    gpu_free = sum(max(0.0, float(g.get("mem_total_mb") or 0) - float(g.get("mem_used_mb") or 0)) for g in _gpus(cap))
    disks = [d for d in cap.get("disks") or [] if isinstance(d, dict)]
    disk_free = max((float(d.get("free_gb") or 0) for d in disks), default=0.0)
    inst = db.rows(sa.select(db.central_instances).where(db.central_instances.c.host_id == host["id"],
                                                         db.central_instances.c.state.in_(LIVE_STATES)))
    committed = 0.0
    for i in inst:
        used = float(((i.get("info") or {}).get("used_gb")) or 0)
        committed += max(0.0, float(i["quota_gb"] or 0) - used)
    return {"gpu_free_mb": gpu_free, "instances": len(inst), "disk_free_gb": disk_free, "room_gb": disk_free - committed}


def pick_host(quota_gb: int) -> dict | None:
    """Auto placement: among online hosts with disk room for the quota, the most free GPU memory, then the fewest
    instances."""
    best, best_key = None, None
    for h in db.rows(sa.select(db.hosts)):
        if not registry.online(h["id"]):
            continue
        r = host_room(h)
        if r["room_gb"] < quota_gb:
            continue
        k = (-r["gpu_free_mb"], r["instances"], h["name"])
        if best_key is None or k < best_key:
            best, best_key = h, k
    return best


def default_subnet(n: int) -> str:
    return f"10.20.{n}.0/24"


def _free_site_number(c) -> int:
    taken = {r[0] for r in c.execute(sa.select(db.central_instances.c.site_number).where(db.central_instances.c.site_number.is_not(None)))}
    for n in SITE_NUMBERS:
        if n not in taken:
            return n
    raise Conflict("every site number (1-250) is in use")


_HOSTNAME = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


def _check_public(addr: str) -> str:
    a = addr.strip()
    try:
        ip = ipaddress.ip_address(a)
    except ValueError:
        ip = None
    if ip is not None:
        if not ip.is_global:
            raise ValueError("the Site's public IP must be a public address")
        return str(ip)
    if not _HOSTNAME.match(a) or "." not in a:
        raise ValueError("the Site's public address must be an IP address or a DNS name")
    return a.lower()


def _check_subnet(subnet: str) -> str:
    try:
        net = ipaddress.ip_network(subnet.strip(), strict=False)
    except ValueError:
        raise ValueError("the camera subnet must look like 10.20.7.0/24")
    if net.version != 4 or not net.is_private or net.prefixlen < 16 or net.prefixlen > 29:
        raise ValueError("the camera subnet must be a private IPv4 network between /16 and /29")
    return str(net)


# ---------------------------------------------------------------- provisioning

def _audit(user: dict | None, org_id: str | None, site_id: str | None, action: str, detail: dict, conn=None) -> None:
    vals = {"ts": time.time(), "user_id": (user or {}).get("id"), "user_email": (user or {}).get("email") or "(central host)",
            "org_id": org_id, "site_id": site_id, "action": action, "method": None, "path": None, "status": None, "ip": None, "detail": detail}
    if conn is not None:
        conn.execute(db.audit_log.insert().values(**vals))
    else:
        db.insert(db.audit_log, vals)


def get_instance(ci_id: str) -> dict | None:
    return db.one(sa.select(db.central_instances).where(db.central_instances.c.id == ci_id))


def _set(ci_id: str, **vals) -> None:
    db.run(sa.update(db.central_instances).where(db.central_instances.c.id == ci_id).values(updated_at=time.time(), **vals))


def mint_enroll_token(ci: dict) -> str:
    """A single-use token bound to this instance's customer and Site (hashed at rest, settings.central_enroll_ttl_s).
    Any earlier unused token for the instance stops working."""
    token = db.new_token()
    t = time.time()
    with db.engine().begin() as c:
        c.execute(sa.delete(db.central_enroll_tokens).where(db.central_enroll_tokens.c.instance_id == ci["id"],
                                                            db.central_enroll_tokens.c.used_at.is_(None)))
        c.execute(db.central_enroll_tokens.insert().values(token_hash=db.token_hash(token), instance_id=ci["id"], org_id=ci["org_id"],
                                                           location_id=ci["location_id"], created_at=t,
                                                           expires_at=t + settings.central_enroll_ttl_s, used_at=None, server_id=None))
    return token


_tasks: set[asyncio.Task] = set()


async def provision(location_id: str, host_id: str | None, mode: str, subnet: str | None, public_ip: str | None, quota_gb: int,
                    by_user: dict | None, *, name: str | None = None, gpu: int | None = None, wait: bool = False) -> dict:
    """Place a central instance for a Site and ask its host to create it. Returns the new row at once (state
    provisioning); the host's answer arrives in the background (wait=True: before returning). ValueError = bad input,
    LookupError = unknown Site or host, Conflict = cannot be placed now."""
    loc = db.one(sa.select(db.locations).where(db.locations.c.id == location_id))
    if not loc:
        raise LookupError("unknown site")
    if mode not in MODES:
        raise ValueError("mode must be vpn or forward")
    if not isinstance(quota_gb, int) or quota_gb < 10:   # axiom_host refuses less than 10 GB
        raise ValueError("the storage quota must be at least 1 GB")
    public = _check_public(public_ip) if public_ip and public_ip.strip() else None
    if mode == "forward" and not public:
        raise ValueError("port-forward mode needs the Site's public IP (the BR1's WAN address)")
    net = _check_subnet(subnet) if subnet and subnet.strip() else None
    live = db.one(sa.select(db.central_instances).where(db.central_instances.c.location_id == location_id,
                                                        db.central_instances.c.state.in_(LIVE_STATES)))
    if live:
        raise Conflict(f"this Site already has a central instance ({live['state']}): remove it first")
    if host_id:
        host = db.one(sa.select(db.hosts).where(db.hosts.c.id == host_id))
        if not host:
            raise LookupError("unknown host")
        if not registry.online(host_id):
            raise Conflict(f"host {host['name']} is offline")
    else:
        host = pick_host(quota_gb)
        if not host:
            raise Conflict("no online host has room for that quota")
    if gpu is None:
        gpu = pick_gpu(host.get("capacity"), _assigned_gpus(host["id"]))
    elif gpu not in {g["index"] for g in _gpus(host.get("capacity"))}:
        raise ValueError(f"host {host['name']} has no GPU {gpu}")
    others = db.rows(sa.select(db.central_instances.c.subnet).where(db.central_instances.c.state.in_(LIVE_STATES),
                                                                    db.central_instances.c.subnet.is_not(None)))
    ci = None
    for _ in range(5):   # two provisions at once may pick the same number: the unique index sends one round again
        try:
            with db.engine().begin() as c:
                n = _free_site_number(c)
                sub = net or (default_subnet(n) if mode == "vpn" else None)
                if sub:
                    mine = ipaddress.ip_network(sub)
                    for o in others:
                        if ipaddress.ip_network(o["subnet"]).overlaps(mine):
                            raise Conflict(f"{sub} overlaps another Site's camera subnet ({o['subnet']})")
                t = time.time()
                ci = {"id": db.new_id("ci_"), "host_id": host["id"], "location_id": location_id, "org_id": loc["org_id"], "server_id": None,
                      "name": (name or "").strip()[:120] or "Central", "mode": mode, "subnet": sub, "public_ip": public, "site_number": n,
                      "quota_gb": quota_gb, "gpu": gpu, "state": "provisioning", "last_error": None, "created_at": t,
                      "created_by": (by_user or {}).get("id"), "updated_at": t, "ready_at": None, "info": None, "info_at": None}
                c.execute(db.central_instances.insert().values(**ci))
            break
        except sa.exc.IntegrityError:
            ci = None
            continue
    if ci is None:
        raise Conflict("could not assign a site number; try again")
    token = mint_enroll_token(ci)
    args = {"id": ci["id"], "location": location_id, "name": ci["name"], "mode": mode, "subnet": ci["subnet"], "public_ip": public,
            "quota_gb": quota_gb, "gpu": gpu, "enroll_token": token, "hub_url": hub_agent_url(), "vlm_url": settings.central_vlm_url}
    _audit(by_user, loc["org_id"], None, f"central recording provisioning: {loc['name']} on {host['name']}",
           {"location_id": location_id, "instance_id": ci["id"], "host_id": host["id"], "mode": mode, "subnet": ci["subnet"],
            "public_ip": public, "site_number": ci["site_number"], "quota_gb": quota_gb, "gpu": gpu})
    task = asyncio.create_task(_create(ci, args, loc, host), name=f"central-create-{ci['id']}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    if wait:
        await task
    return get_instance(ci["id"]) or ci


async def _create(ci: dict, args: dict, loc: dict, host: dict) -> None:
    try:
        res = await registry.command(host["id"], "create_instance", args, settings.host_create_timeout_s)
        ok, detail = bool(res.get("ok")), str(res.get("detail") or "")
    except HostError as e:
        res, ok, detail = {}, False, str(e)
    except Exception as e:   # never leave the row in provisioning because of a bug here
        log.exception("create_instance %s", ci["id"])
        res, ok, detail = {}, False, f"internal error: {e}"
    cur = get_instance(ci["id"]) or {}
    if ok:
        vals: dict = {"ready_at": time.time()}
        if isinstance(res.get("instance"), dict):
            vals |= {"info": res["instance"], "info_at": time.time()}
        _set(ci["id"], **vals)
        log.info("central instance %s created on %s", ci["id"], host["name"])
        return
    if cur.get("state") == "provisioning":   # an instance that already enrolled clearly works: keep it running
        _set(ci["id"], state="failed", last_error=(detail or "the host refused")[:2000])
    _audit(None, loc["org_id"], None, f"central recording failed: {loc['name']} on {host['name']}",
           {"location_id": loc["id"], "instance_id": ci["id"], "host_id": host["id"], "error": detail[:500]})
    log.warning("central instance %s on %s failed: %s", ci["id"], host["name"], detail)


async def deprovision(ci_id: str, by_user: dict | None, purge: bool = False, force: bool = False) -> dict:
    """Delete the instance on its host (recordings kept unless purge) and retire its server. HostError when the host
    is offline or refuses, unless force (then the hub forgets it anyway; the container may be left on the host)."""
    ci = get_instance(ci_id)
    if not ci:
        raise LookupError("unknown central instance")
    if ci["state"] == "deleted":
        return ci
    loc = db.one(sa.select(db.locations).where(db.locations.c.id == ci["location_id"])) or {"name": ci["location_id"], "org_id": ci["org_id"]}
    before = ci["state"]
    _set(ci_id, state="deleting")
    err = None
    try:
        res = await registry.command(ci["host_id"], "delete_instance", {"id": ci_id, "purge": bool(purge)})
        if not res.get("ok"):
            err = str(res.get("detail") or "the host refused")
    except HostError as e:
        err = str(e)
    if err and not force:
        _set(ci_id, state=before, last_error=f"delete failed: {err}"[:2000])
        raise HostError(err)
    with db.engine().begin() as c:
        c.execute(sa.update(db.central_instances).where(db.central_instances.c.id == ci_id).values(
            state="deleted", site_number=None, updated_at=time.time(), last_error=f"removed without the host: {err}"[:2000] if err else None))
        c.execute(sa.delete(db.central_enroll_tokens).where(db.central_enroll_tokens.c.instance_id == ci_id,
                                                            db.central_enroll_tokens.c.used_at.is_(None)))
    if ci["server_id"]:
        from . import fleet_actions   # (fleet_actions imports the tunnel registry, which imports this module)
        fleet_actions.retire(ci["server_id"], True)
    _audit(by_user, ci["org_id"], ci["server_id"], f"central recording removed: {loc['name']}{' (recordings deleted)' if purge else ''}",
           {"location_id": ci["location_id"], "instance_id": ci_id, "host_id": ci["host_id"], "purge": bool(purge), "forced": bool(err)})
    return get_instance(ci_id) or ci


async def set_quota(ci_id: str, quota_gb: int, by_user: dict | None) -> dict:
    ci = get_instance(ci_id)
    if not ci or ci["state"] == "deleted":
        raise LookupError("unknown central instance")
    if quota_gb < 10:
        raise ValueError("the storage quota must be at least 1 GB")
    res = await registry.command(ci["host_id"], "set_quota", {"id": ci_id, "quota_gb": quota_gb})
    if not res.get("ok"):
        raise HostError(str(res.get("detail") or "the host refused"))
    _set(ci_id, quota_gb=quota_gb)
    loc = db.one(sa.select(db.locations.c.name).where(db.locations.c.id == ci["location_id"])) or {"name": ci["location_id"]}
    _audit(by_user, ci["org_id"], ci["server_id"], f"central recording quota: {loc['name']} {ci['quota_gb']} -> {quota_gb} GB",
           {"location_id": ci["location_id"], "instance_id": ci_id, "from": ci["quota_gb"], "to": quota_gb})
    return get_instance(ci_id) or ci


# ---------------------------------------------------------------- enrollment (agents.py, Authorization: Enroll <token>)

def enroll_check(token: str) -> dict | None:
    """The token row when `token` may enroll right now: known, unused, unexpired, its instance not being removed."""
    if not token or len(token) > 200:
        return None
    row = db.one(sa.select(db.central_enroll_tokens).where(db.central_enroll_tokens.c.token_hash == db.token_hash(token)))
    if not row or row["used_at"] is not None or row["expires_at"] <= time.time():
        return None
    ci = get_instance(row["instance_id"])
    if not ci or ci["state"] in ("deleting", "deleted") or ci["location_id"] != row["location_id"] or ci["org_id"] != row["org_id"]:
        return None
    loc = db.one(sa.select(db.locations).where(db.locations.c.id == row["location_id"], db.locations.c.org_id == row["org_id"]))
    return row if loc else None


def log_bad_enroll(token: str, ip: str | None) -> None:
    """Once per token: a retrying instance with a dead token must not fill the log."""
    h = db.token_hash(token or "")
    if h in _BAD_ENROLL_LOGGED:
        return
    if len(_BAD_ENROLL_LOGGED) > 1000:
        _BAD_ENROLL_LOGGED.clear()
    _BAD_ENROLL_LOGGED.add(h)
    log.warning("refused an enrollment from %s: unknown, used or expired token", ip or "?")


def enroll_consume(token: str, hello: dict | None, agent_ip: str | None) -> tuple[dict, str] | None:
    """Use the token (once, atomically) and create the server row in the token's customer and Site, named after the
    instance. Returns (server row, device token) or None when the token is no longer good."""
    hello = hello or {}
    h = db.token_hash(token)
    now = time.time()
    device = db.new_token()
    with db.engine().begin() as c:
        row = c.execute(sa.select(db.central_enroll_tokens).where(db.central_enroll_tokens.c.token_hash == h)).mappings().first()
        if not row:
            return None
        used = c.execute(sa.update(db.central_enroll_tokens).where(
            db.central_enroll_tokens.c.token_hash == h, db.central_enroll_tokens.c.used_at.is_(None),
            db.central_enroll_tokens.c.expires_at > now).values(used_at=now))
        if used.rowcount != 1:
            return None
        ci = c.execute(sa.select(db.central_instances).where(db.central_instances.c.id == row["instance_id"])).mappings().first()
        loc = c.execute(sa.select(db.locations).where(db.locations.c.id == row["location_id"], db.locations.c.org_id == row["org_id"])).mappings().first()
        if not ci or not loc or ci["state"] in ("deleting", "deleted"):
            raise _Abort()
        site = {"id": db.new_id("s_"), "org_id": row["org_id"], "name": ci["name"], "location": "", "token_hash": db.token_hash(device),
                "token_prev_hash": None, "token_rotated_at": None, "created_at": now, "last_seen_at": None, "online": False,
                "version": str(hello.get("site_version") or "")[:32] or None, "summary": None, "clock_skew_s": None, "agent_ip": agent_ip,
                "hostname": str(hello.get("hostname") or "")[:120] or None, "location_id": row["location_id"]}
        c.execute(db.sites.insert().values(**site))
        c.execute(sa.update(db.central_enroll_tokens).where(db.central_enroll_tokens.c.token_hash == h).values(server_id=site["id"]))
        c.execute(sa.update(db.central_instances).where(db.central_instances.c.id == ci["id"]).values(
            server_id=site["id"], state="running", last_error=None, updated_at=now))
        _audit(None, row["org_id"], site["id"], f"central instance enrolled: {ci['name']} into {loc['name']}",
               {"location_id": row["location_id"], "instance_id": ci["id"], "host_id": ci["host_id"]}, conn=c)
    log.info("central instance %s enrolled as server %s in %s", ci["id"], site["id"], row["location_id"])
    return site, device


def enroll_undo(token: str, server_id: str) -> None:
    """The enrolled frame never reached the instance: remove the server just made and make the token usable again."""
    h = db.token_hash(token)
    with db.engine().begin() as c:
        row = c.execute(sa.select(db.central_enroll_tokens).where(db.central_enroll_tokens.c.token_hash == h)).mappings().first()
        if not row or row["server_id"] != server_id:
            return
        c.execute(sa.delete(db.sites).where(db.sites.c.id == server_id))
        c.execute(sa.update(db.central_enroll_tokens).where(db.central_enroll_tokens.c.token_hash == h).values(used_at=None, server_id=None))
        c.execute(sa.update(db.central_instances).where(db.central_instances.c.id == row["instance_id"], db.central_instances.c.server_id == server_id)
                  .values(server_id=None, state="provisioning", updated_at=time.time()))
    log.warning("central enrollment of %s rolled back: the instance disconnected before it got its token", server_id)


class _Abort(Exception):
    pass


def enroll(token: str, hello: dict | None, agent_ip: str | None) -> tuple[dict, str] | None:
    try:
        return enroll_consume(token, hello, agent_ip)
    except _Abort:   # rolled back: the token stays unused
        return None


# ---------------------------------------------------------------- what the pages show

def _names(rows: list[dict]) -> dict:
    locs = {r["location_id"] for r in rows}
    orgs = {r["org_id"] for r in rows}
    hosts = {r["host_id"] for r in rows}
    servers = {r["server_id"] for r in rows if r["server_id"]}
    return {
        "loc": {r["id"]: r["name"] for r in db.rows(sa.select(db.locations.c.id, db.locations.c.name).where(db.locations.c.id.in_(locs)))} if locs else {},
        "org": {r["id"]: r["name"] for r in db.rows(sa.select(db.orgs.c.id, db.orgs.c.name).where(db.orgs.c.id.in_(orgs)))} if orgs else {},
        "host": {r["id"]: r for r in db.rows(sa.select(db.hosts).where(db.hosts.c.id.in_(hosts)))} if hosts else {},
        "server": {r["id"]: r for r in db.rows(sa.select(db.sites.c.id, db.sites.c.online, db.sites.c.last_seen_at).where(db.sites.c.id.in_(servers)))} if servers else {},
    }


def phase(ci: dict) -> str:
    """provisioning (the host is creating it) -> waiting_enroll (created, not dialed in yet) -> running; or failed,
    deleting, deleted."""
    if ci["state"] == "provisioning" and ci.get("ready_at"):
        return "waiting_enroll"
    return ci["state"]


def instance_out(ci: dict, names: dict, hub_admin: bool) -> dict:
    info = ci.get("info") or {}
    host = names["host"].get(ci["host_id"]) or {}
    server = names["server"].get(ci["server_id"]) if ci["server_id"] else None
    gpu_name = next((g.get("name") for g in _gpus(host.get("capacity")) if g["index"] == ci["gpu"]), None)
    out = {"id": ci["id"], "location_id": ci["location_id"], "location_name": names["loc"].get(ci["location_id"]),
           "org_id": ci["org_id"], "org_name": names["org"].get(ci["org_id"]), "server_id": ci["server_id"],
           "server_online": bool(server and server["online"]), "name": ci["name"], "mode": ci["mode"], "subnet": ci["subnet"],
           "public_ip": ci["public_ip"], "site_number": ci["site_number"], "quota_gb": ci["quota_gb"],
           "used_gb": info.get("used_gb") if isinstance(info.get("used_gb"), (int, float)) else None,
           "state": ci["state"], "phase": phase(ci), "created_at": ci["created_at"], "ready_at": ci.get("ready_at"), "info_at": ci.get("info_at"),
           "peplink": peplink(ci, host)}
    if hub_admin:
        out |= {"host_id": ci["host_id"], "host_name": host.get("name"), "host_online": registry.online(ci["host_id"]),
                "gpu": ci["gpu"], "gpu_name": gpu_name, "last_error": ci["last_error"], "host_state": info.get("state")}
    return out


def peplink(ci: dict, host: dict) -> dict:
    """What the Peplink settings sheet needs (hub UI central.ts lays it out)."""
    gw = None
    if ci.get("subnet"):
        try:
            gw = str(next(ipaddress.ip_network(ci["subnet"]).hosts()))
        except (ValueError, StopIteration):
            gw = None
    return {"mode": ci["mode"], "subnet": ci.get("subnet"), "lan_gateway": gw, "public_ip": ci.get("public_ip"),
            "fusionhub": (host or {}).get("fusionhub") or settings.fusionhub_address or None,
            "datacenter_ip": settings.datacenter_ip or None, "rtsp_base": RTSP_BASE, "onvif_base": ONVIF_BASE}


def instances(where=None, hub_admin: bool = True, include_deleted: bool = False) -> list[dict]:
    q = sa.select(db.central_instances).order_by(db.central_instances.c.created_at.desc())
    if where is not None:
        q = q.where(where)
    if not include_deleted:
        q = q.where(db.central_instances.c.state != "deleted")
    rows = db.rows(q)
    names = _names(rows)
    return [instance_out(r, names, hub_admin) for r in rows]


def by_server(server_ids: list[str]) -> dict[str, dict]:
    """server id -> {id, mode, quota_gb, used_gb, site_number, state} for the server cards (Customer › Servers usage)."""
    if not server_ids:
        return {}
    rows = db.rows(sa.select(db.central_instances).where(db.central_instances.c.server_id.in_(server_ids),
                                                         db.central_instances.c.state != "deleted"))
    out = {}
    for r in rows:
        info = r.get("info") or {}
        out[r["server_id"]] = {"id": r["id"], "mode": r["mode"], "quota_gb": r["quota_gb"], "site_number": r["site_number"], "state": r["state"],
                               "used_gb": info.get("used_gb") if isinstance(info.get("used_gb"), (int, float)) else None}
    return out
