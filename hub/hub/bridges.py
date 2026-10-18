"""Axiom Bridge: a Site's cameras reached by its central recording instance through a PC at the Site (no inbound access).

docs/axiom-bridge-plan.md has the design; the bridge itself is the Go program in bridge/. A Site with cameras and no
way in (Starlink CGNAT, cellular, no static IP) gets a small program on a PC there, the **bridge**: it dials WS /bridge
over TCP 443 and keeps it open. Its Site's central instance (hosts.py) reaches each camera exactly as through port
forwards: the camera's `public_host` is the instance's gateway on its host and each `public_*_port` one of the
instance's listen ports (47000-47999). The host agent (tools/central/axiom_host.py, Relay) accepts that connection and
names the listener on its own WS /host-relay; the hub splices it onto a stream it opens on the bridge, which dials the
camera:

  instance → <gateway>:<listen port> (host agent) → WS /host-relay → hub → WS /bridge → bridge → camera_ip:port

Wire protocol (binding for the bridge, the host agent and this file; the plan's "Wire protocol" section): tunnelproto
framing (JSON text control frames with "t", unknown types and fields ignored; binary frames = >I stream id + up to CHUNK
bytes). `end {id}` = half-close (TCP FIN), `abort {id, reason}` = reset, `credit {id, bytes}` is sent by a receiver
only after the bytes were written on (to the TCP socket; here: to the other connection). Only one side opens streams
per connection: the hub on /bridge, the host on /host-relay; ids odd.

  /bridge      Authorization: Enroll <code> (hello within 15 s, the code is consumed atomically, `enrolled {bridge_id,
               token}`, closed) or Bearer <device token>. bridge → hub: hello, heartbeat, opened, chunks, end, abort,
               credit, probe_result, rotated, ping / pong. hub → bridge: welcome, targets {rev, items}, open, probe,
               rotate, revoked, chunks, end, abort, credit, ping / pong. A bridge that sends `open`, or more than the
               window unacknowledged on a stream, is closed.
  /host-relay  Bearer <host token> (the /host-agent one). host → hub: hello {listeners}, open {id, instance, port,
               peer}, chunks, end, abort, credit, ping. hub → host: opened / abort, chunks, end, credit, pong.

The splice (one hub worker): on the host's `open` the (instance, listen port) is looked up in bridge_targets: the
instance must be on that host and live, the target enabled, its bridge online, under settings.bridge_max_streams and
bridge_open_rate; the bridge gets `open` and must answer `opened` within bridge_open_timeout_s, then the host gets
`opened`. Two pumps per stream: read a chunk, take credit on the destination, send it on, then credit the source, so
the hub holds at most one window per direction per stream.

Who may do what (docs/roles.md): a Site's admins (customer admin and up who see the Site) and hub administrators create
enrollment codes, rename, rotate and retire the Site's bridges and map its cameras to the Site's central instance;
allocating the instance stays with hub administrators (Hosts page). Everyone who sees the Site sees the bridges' status.
Every change is audited. `bridge_offline` alerts go to the Customer and hub administrators (alerts.py).
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import ipaddress
import json
import logging
import re
import secrets
import struct
import time
from urllib.parse import quote

import sqlalchemy as sa
from fastapi import WebSocket, WebSocketDisconnect

from tunnelproto import CHUNK, Stream, chunk, decode, encode

from . import alerts, db, hosts
from .config import settings

log = logging.getLogger("hub.bridges")

PROTO = 1
HEARTBEAT_S = 30
RELAY_PORTS = range(47000, 48000)      # an instance's listen ports on its gateway (axiom_host.py RELAY_PORT_RANGE)
DEFAULT_PORTS = (554, 80, 555)         # RTSP, ONVIF (+ PTZ, events, the camera's web page), Milesight SD replay
MAX_PORTS = 8                          # camera ports per target
MAX_TARGETS = 64                       # cameras per bridge
PORT_LABEL = {554: "RTSP", 80: "ONVIF", 555: "SD replay", 443: "web", 8554: "RTSP", 8080: "web"}
MAX_TEXT = 1024 * 1024                 # a control frame bigger than this closes the connection
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I: read off a screen and typed on the PC
CODE_LEN = 12                          # 60 bits, after the AXB- prefix: single use, valid a day
ROTATE_GRACE_S = 600                   # the previous device token works this long after a rotation
PROBE_TIMEOUT_S = 8.0                  # the bridge dials for 5 s
RELAY_RETRY_S = 60.0                   # a host whose relay ports differ from the hub's is sent set_relay at most this often
BRIDGE_ALERT = "bridge_offline"
FETCH_TIMEOUT_S = 15.0
_RFC1918 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_BAD_CODES_LOGGED: set[str] = set()


class Refused(Exception):
    """A request breaks a rule: `status` (400 / 404 / 409) and a message safe to show."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class BridgeError(Exception):
    """The bridge (or the instance) cannot be asked now: offline, timed out, refused (502)."""


# ---------------------------------------------------------------- enrollment codes

def new_code() -> str:
    """"AXB-ABCD-EFGH-JKLM" (the form the bridge's own help shows): 60 random bits, grouped for reading. Stored hashed
    (normalize_code first)."""
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    return "AXB-" + "-".join(raw[i:i + 4] for i in range(0, CODE_LEN, 4))


def normalize_code(code: str) -> str:
    """What is hashed: letters and digits only, uppercased, without the AXB prefix (a code typed with spaces, without
    dashes or without the prefix still works)."""
    n = re.sub(r"[^A-Za-z0-9]", "", code or "").upper()
    return n[3:] if len(n) == CODE_LEN + 3 and n.startswith("AXB") else n


def code_hash(code: str) -> str:
    return db.token_hash("bridge-code:" + normalize_code(code))


def bridge_url() -> str:
    """Where a bridge dials (the bridge's config.json hub_url)."""
    return hosts._ws_base() + "/bridge"


def install_command(code: str) -> str:
    """What the enrollment dialog shows (hub UI bridges.ts installCommand builds the same text)."""
    return rf".\axiom-bridge.exe install --hub {bridge_url()} --code {code}"


def create_code(loc: dict, name: str | None, user: dict) -> tuple[dict, str]:
    """A single-use enrollment code for this Site (valid settings.bridge_enroll_ttl_s); the code is in this answer only."""
    code = new_code()
    t = time.time()
    row = {"id": db.new_id("bc_"), "code_hash": code_hash(code), "org_id": loc["org_id"], "location_id": loc["id"],
           "name": (name or "").strip()[:120] or None, "created_at": t, "created_by": user.get("id"), "created_by_email": user.get("email"),
           "expires_at": t + settings.bridge_enroll_ttl_s, "used_at": None, "bridge_id": None}
    db.insert(db.bridge_enroll_codes, row)
    hosts._audit(user, loc["org_id"], None, f"Axiom Bridge enrollment code created: {loc['name']}",
                 {"location_id": loc["id"], "code_id": row["id"], "name": row["name"], "expires_at": row["expires_at"]})
    return row, code


def code_out(r: dict) -> dict:
    return {k: r[k] for k in ("id", "name", "created_at", "created_by_email", "expires_at")}


def open_codes(location_id: str) -> list[dict]:
    """The Site's codes that may still enroll a bridge, newest first."""
    return [code_out(r) for r in db.rows(sa.select(db.bridge_enroll_codes).where(
        db.bridge_enroll_codes.c.location_id == location_id, db.bridge_enroll_codes.c.used_at.is_(None),
        db.bridge_enroll_codes.c.expires_at > time.time()).order_by(db.bridge_enroll_codes.c.created_at.desc()))]


def revoke_code(loc: dict, code_id: str, user: dict) -> None:
    row = db.one(sa.select(db.bridge_enroll_codes).where(db.bridge_enroll_codes.c.id == code_id,
                                                         db.bridge_enroll_codes.c.location_id == loc["id"]))
    if not row or row["used_at"] is not None:
        raise Refused(404, "no such enrollment code")
    db.run(sa.delete(db.bridge_enroll_codes).where(db.bridge_enroll_codes.c.id == code_id))
    hosts._audit(user, loc["org_id"], None, f"Axiom Bridge enrollment code revoked: {loc['name']}", {"location_id": loc["id"], "code_id": code_id})


def code_check(code: str) -> dict | None:
    """The code's row when it may enroll right now: known, unused, unexpired, its Site still there."""
    if not code or len(code) > 64:
        return None
    row = db.one(sa.select(db.bridge_enroll_codes).where(db.bridge_enroll_codes.c.code_hash == code_hash(code)))
    if not row or row["used_at"] is not None or row["expires_at"] <= time.time():
        return None
    loc = db.one(sa.select(db.locations).where(db.locations.c.id == row["location_id"], db.locations.c.org_id == row["org_id"]))
    return row if loc else None


def log_bad_code(code: str, ip: str | None) -> None:
    """Once per code: a bridge retrying a dead code every 5 minutes must not fill the log."""
    h = code_hash(code or "")
    if h in _BAD_CODES_LOGGED:
        return
    if len(_BAD_CODES_LOGGED) > 1000:
        _BAD_CODES_LOGGED.clear()
    _BAD_CODES_LOGGED.add(h)
    log.warning("refused a bridge enrollment from %s: unknown, used or expired code", ip or "?")


def _clean_hello(frame: dict) -> dict:
    """What the hub keeps of a bridge's hello (bounded: it is shown on pages)."""
    s = lambda v, n: str(v)[:n] if isinstance(v, (str, int, float)) and not isinstance(v, bool) else None  # noqa: E731
    lan = []
    for e in (frame.get("lan") if isinstance(frame.get("lan"), list) else [])[:16]:
        if isinstance(e, dict) and isinstance(e.get("addr"), str):
            lan.append({"if": s(e.get("if"), 64), "addr": e["addr"][:64]})
    caps = [c[:32] for c in (frame.get("caps") if isinstance(frame.get("caps"), list) else [])[:16] if isinstance(c, str)]
    return {"proto": frame.get("proto") if isinstance(frame.get("proto"), int) else None, "version": s(frame.get("version"), 32),
            "os": s(frame.get("os"), 32), "arch": s(frame.get("arch"), 32), "hostname": s(frame.get("hostname"), 120), "lan": lan, "caps": caps}


def enroll_consume(code: str, hello: dict | None, ip: str | None) -> tuple[dict, str] | None:
    """Use the code (once, atomically) and create the bridge in its Site. Returns (bridge row, device token), or None
    when the code is no longer good."""
    h = code_hash(code)
    now = time.time()
    token = db.new_token()
    info = _clean_hello(hello or {})
    with db.engine().begin() as c:
        row = c.execute(sa.select(db.bridge_enroll_codes).where(db.bridge_enroll_codes.c.code_hash == h)).mappings().first()
        if not row:
            return None
        used = c.execute(sa.update(db.bridge_enroll_codes).where(
            db.bridge_enroll_codes.c.code_hash == h, db.bridge_enroll_codes.c.used_at.is_(None),
            db.bridge_enroll_codes.c.expires_at > now).values(used_at=now))
        if used.rowcount != 1:
            return None
        loc = c.execute(sa.select(db.locations).where(db.locations.c.id == row["location_id"], db.locations.c.org_id == row["org_id"])).mappings().first()
        if not loc:
            raise _Abort()
        b = {"id": db.new_id("br_"), "org_id": row["org_id"], "location_id": row["location_id"],
             "name": row["name"] or info.get("hostname") or "Bridge", "token_hash": db.token_hash(token), "token_prev_hash": None,
             "token_rotated_at": None, "created_at": now, "created_by": row["created_by"], "online": False,
             # seen now: a bridge that enrolls and never comes back raises bridge_offline like one that went away
             "last_seen_at": now, "agent_ip": ip, "hello": info, "stats": None, "targets_rev": 0, "retired_at": None}
        c.execute(db.bridges.insert().values(**b))
        c.execute(sa.update(db.bridge_enroll_codes).where(db.bridge_enroll_codes.c.code_hash == h).values(bridge_id=b["id"]))
        hosts._audit({"id": None, "email": "(Axiom Bridge)"}, row["org_id"], None, f"Axiom Bridge enrolled: {b['name']} into {loc['name']}",
                     {"location_id": row["location_id"], "bridge_id": b["id"], "code_id": row["id"], "hostname": info.get("hostname"),
                      "version": info.get("version"), "ip": ip}, conn=c)
    log.info("bridge %s (%s) enrolled into %s from %s", b["id"], b["name"], row["location_id"], ip)
    return b, token


class _Abort(Exception):
    pass


def enroll(code: str, hello: dict | None, ip: str | None) -> tuple[dict, str] | None:
    try:
        return enroll_consume(code, hello, ip)
    except _Abort:   # rolled back: the code stays unused
        return None


def enroll_undo(code: str, bridge_id: str) -> None:
    """The enrolled frame never reached the bridge: remove the bridge just made and make the code usable again."""
    h = code_hash(code)
    with db.engine().begin() as c:
        row = c.execute(sa.select(db.bridge_enroll_codes).where(db.bridge_enroll_codes.c.code_hash == h)).mappings().first()
        if not row or row["bridge_id"] != bridge_id:
            return
        c.execute(sa.delete(db.bridges).where(db.bridges.c.id == bridge_id))
        c.execute(sa.update(db.bridge_enroll_codes).where(db.bridge_enroll_codes.c.code_hash == h).values(used_at=None, bridge_id=None))
    log.warning("bridge enrollment of %s rolled back: it disconnected before it got its token", bridge_id)


# ---------------------------------------------------------------- bridges and their targets (rows)

def get_bridge(bridge_id: str) -> dict | None:
    return db.one(sa.select(db.bridges).where(db.bridges.c.id == bridge_id))


def site_bridge(location_id: str, bridge_id: str) -> dict:
    b = get_bridge(bridge_id)
    if not b or b["location_id"] != location_id or b["retired_at"]:
        raise Refused(404, "no such bridge")
    return b


def targets_of(bridge_id: str | None = None, *, instance_id: str | None = None, location_id: str | None = None) -> list[dict]:
    q = sa.select(db.bridge_targets).order_by(db.bridge_targets.c.created_at)
    if bridge_id:
        q = q.where(db.bridge_targets.c.bridge_id == bridge_id)
    if instance_id:
        q = q.where(db.bridge_targets.c.instance_id == instance_id)
    if location_id:
        q = q.where(db.bridge_targets.c.location_id == location_id)
    return db.rows(q)


def site_target(location_id: str, bridge_id: str, target_id: str) -> dict:
    t = db.one(sa.select(db.bridge_targets).where(db.bridge_targets.c.id == target_id))
    if not t or t["location_id"] != location_id or t["bridge_id"] != bridge_id:
        raise Refused(404, "no such bridged camera")
    return t


def site_instance(location_id: str) -> dict | None:
    """The Site's central instance that bridged cameras are relayed to (a Site has at most one live one)."""
    return db.one(sa.select(db.central_instances).where(db.central_instances.c.location_id == location_id,
                                                        db.central_instances.c.state.in_(("provisioning", "running", "failed"))))


def listen_ports(ci_id: str) -> set[int]:
    """Every listen port mapped on this instance (enabled or not: a disabled target keeps its ports)."""
    return {p["listen"] for t in targets_of(instance_id=ci_id) for p in t["ports"] if isinstance(p, dict) and isinstance(p.get("listen"), int)}


def relay_address(ci: dict) -> str | None:
    """The instance's gateway on its host (where its relay listens), as its host agent (0.3.0+) last reported it."""
    info = ci.get("info") if isinstance(ci.get("info"), dict) else {}
    gw = info.get("gateway")
    return gw if isinstance(gw, str) and hosts._is_ip(gw) else None


def bridge_fill(ci: dict, kind: str, path: str, raw: bytes | None) -> bytes | None:
    """A camera added or saved on the instance with a LAN address that this Site's Bridge carries for it, and no outside
    address of its own (public_host empty or absent): the relay address and listen ports are filled in on the way
    through, so adding a bridged camera is just its LAN address, username and password (no Apply step). None = leave
    the body as it is (not a camera write, an outside address set by hand, not a bridged address, a port not bridged)."""
    if kind != "camera" or not raw:
        return None
    try:
        body = json.loads(raw.decode())
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict) or (body.get("public_host") or "").strip():
        return None
    host, gw = str(body.get("host") or "").strip(), relay_address(ci)
    if not host or not gw:
        return None
    t = next((t for t in targets_of(instance_id=ci["id"]) if t["enabled"] and t["host"] == host), None)
    if t is None:
        return None
    rtsp, onvif = listen_of(t, _int_or(body.get("rtsp_port"), 554)), listen_of(t, _int_or(body.get("onvif_port"), 80))
    if rtsp is None or onvif is None:
        return None   # central_cameras refuses it with the reason
    web = _int_or(body.get("web_port"), None)
    body |= {"public_host": gw, "public_rtsp_port": rtsp, "public_onvif_port": onvif, "public_replay_port": listen_of(t, 555),
             "public_web_port": listen_of(t, web) if web and web != _int_or(body.get("onvif_port"), 80) else None}
    return json.dumps(body).encode()


def _int_or(v, default):
    try:
        return int(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def check_camera_host(addr: str, bridge: dict | None = None) -> str:
    """A camera's LAN address as the bridge dials it: a private (RFC 1918) IPv4 address, not one of the bridge PC's own
    interface addresses (the bridge refuses the rest too: bridge/internal/policy)."""
    a = (addr or "").strip()
    try:
        ip = ipaddress.ip_address(a)
    except ValueError:
        raise Refused(400, f"{a or 'the address'}: give the camera's LAN IPv4 address, like 192.168.1.50") from None
    if ip.version != 4 or not any(ip in n for n in _RFC1918):
        raise Refused(400, f"{a}: the camera must be at a private LAN address (10.x, 172.16-31.x or 192.168.x)")
    for e in ((bridge or {}).get("hello") or {}).get("lan") or []:
        if str(e.get("addr") or "").split("/")[0] == str(ip):
            raise Refused(400, f"{a} is the bridge PC itself, not a camera")
    return str(ip)


def check_ports(ports) -> list[int]:
    if ports is None:
        return list(DEFAULT_PORTS)
    if not isinstance(ports, list) or not ports:
        raise Refused(400, "give the camera's ports, e.g. 554, 80, 555")
    out: list[int] = []
    for p in ports:
        if not isinstance(p, int) or isinstance(p, bool) or not 1 <= p <= 65535:
            raise Refused(400, f"{p!r} is not a TCP port (1-65535)")
        if p in out:
            raise Refused(400, f"port {p} is listed twice")
        out.append(p)
    if len(out) > MAX_PORTS:
        raise Refused(400, f"at most {MAX_PORTS} ports per camera")
    return out


def _free_listen(c, ci_id: str, n: int) -> list[int]:
    """The n lowest listen ports of RELAY_PORTS no target of this instance uses (in the caller's transaction)."""
    used = set()
    for r in c.execute(sa.select(db.bridge_targets.c.ports).where(db.bridge_targets.c.instance_id == ci_id)):
        used |= {p["listen"] for p in (r.ports or []) if isinstance(p, dict)}
    free = [p for p in RELAY_PORTS if p not in used][:n]
    if len(free) < n:
        raise Refused(409, "this instance has no free relay ports left")
    return free


def _bump_rev(c, bridge_id: str) -> None:
    c.execute(sa.update(db.bridges).where(db.bridges.c.id == bridge_id).values(targets_rev=db.bridges.c.targets_rev + 1))


def targets_frame(bridge_id: str) -> dict:
    """`targets {rev, items}`: the bridge's whole allow-list (enabled targets), sent after welcome and on every change."""
    b = get_bridge(bridge_id) or {}
    items = [{"id": t["id"], "host": t["host"], "ports": [p["port"] for p in t["ports"]]}
             for t in targets_of(bridge_id) if t["enabled"]]
    return {"t": "targets", "rev": int(b.get("targets_rev") or 0), "items": items}


def port_of(target: dict, listen: int) -> int | None:
    """The camera port behind a listen port of this target."""
    return next((p["port"] for p in target["ports"] if p.get("listen") == listen), None)


def listen_of(target: dict, port: int | None) -> int | None:
    """The listen port relaying this camera port (None = not bridged)."""
    return next((p["listen"] for p in target["ports"] if p.get("port") == port), None) if port else None


def relay_ports(ci_id: str) -> list[dict]:
    """What the host is sent (set_relay ports): every mapped listen port of the instance, labelled for its logs."""
    out = []
    for t in targets_of(instance_id=ci_id):
        for p in t["ports"]:
            out.append({"port": p["listen"], "label": f"{t['name']} {PORT_LABEL.get(p['port'], str(p['port']))}"[:80]})
    return sorted(out, key=lambda p: p["port"])


def _loc_name(location_id: str) -> str:
    loc = db.one(sa.select(db.locations.c.name).where(db.locations.c.id == location_id))
    return (loc or {}).get("name") or location_id


_relay_locks: dict[str, asyncio.Lock] = {}
_relay_tried: dict[str, float] = {}


async def sync_relay(ci_id: str, by_user: dict | None = None) -> dict:
    """Send the instance's host its listen ports (set_relay: firewall + listeners). HostError when the host is offline
    or refuses. Returns the result frame."""
    lk = _relay_locks.setdefault(ci_id, asyncio.Lock())
    async with lk:
        ci = hosts.get_instance(ci_id)
        if not ci or ci["state"] in ("deleting", "deleted"):
            raise hosts.HostError("the central instance is gone")
        ports = relay_ports(ci_id)
        _relay_tried[ci_id] = time.time()
        res = await hosts.registry.command(ci["host_id"], "set_relay", {"id": ci_id, "ports": ports})
        if not res.get("ok"):
            detail = str(res.get("detail") or "the host refused")
            if "unknown op" in detail:
                detail = "the host's agent is older than 0.3.0 (no Axiom Bridge relay): update axiom_host.py on it first"
            raise hosts.HostError(detail)
        got = res.get("instance") if isinstance(res.get("instance"), dict) else {}
        if got:   # the gateway and the listeners, until the next heartbeat
            hosts._set(ci_id, info={**(ci.get("info") or {}), **{k: got[k] for k in ("gateway", "relay") if k in got}}, info_at=time.time())
    hosts._audit(by_user or {"id": None, "email": "(automatic: Axiom Bridge relay)"}, ci["org_id"], ci["server_id"],
                 f"Axiom Bridge relay ports: {_loc_name(ci['location_id'])}",
                 {"location_id": ci["location_id"], "instance_id": ci_id, "ports": [p["port"] for p in ports], "automatic": by_user is None})
    return res


_tasks: set[asyncio.Task] = set()


def _spawn(coro, name: str) -> None:
    try:
        t = asyncio.get_running_loop().create_task(coro, name=name)
    except RuntimeError:
        coro.close()
        return
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def on_host_heartbeat(host_id: str, instances: list) -> None:
    """A host's heartbeat: an instance whose reported relay ports differ from the hub's (a host agent restarted with an
    older registry, a set_relay that failed while the host was away) is sent set_relay again, at most every
    RELAY_RETRY_S. Instances of agents older than 0.3.0 report no `relay` and are left alone."""
    now = time.time()
    for inst in instances:
        if not isinstance(inst, dict) or not isinstance(inst.get("relay"), dict) or not isinstance(inst.get("id"), str):
            continue
        ci = hosts.get_instance(inst["id"][:24])
        if not ci or ci["host_id"] != host_id or ci["state"] in ("deleting", "deleted"):
            continue
        have = sorted(p for p in inst["relay"].get("ports") or [] if isinstance(p, int))
        want = sorted(p["port"] for p in relay_ports(ci["id"]))
        if have == want or now - _relay_tried.get(ci["id"], 0.0) < RELAY_RETRY_S:
            continue
        _relay_tried[ci["id"]] = now
        log.info("instance %s: host relays %s, the hub maps %s: sending set_relay", ci["id"], have, want)
        _spawn(_resync(ci["id"]), f"bridge-relay-{ci['id']}")


async def _resync(ci_id: str) -> None:
    try:
        await sync_relay(ci_id)
    except hosts.HostError as e:
        log.warning("instance %s: relay ports not applied: %s", ci_id, e)
    except Exception:
        log.exception("relay sync of %s", ci_id)


async def create_target(loc: dict, bridge: dict, name: str, host: str, ports, camera_id: str | None, user: dict) -> dict:
    """Map one camera behind this bridge to the Site's central instance: listen ports allocated on the instance, its host
    told (set_relay), then the bridge (targets). Nothing is kept when the host refuses or is offline (502)."""
    ci = site_instance(loc["id"])
    if not ci:
        raise Refused(409, "this Site has no central recording instance to relay to: ask Axiom Vision to allocate one")
    name = (name or "").strip()[:120]
    if not name:
        raise Refused(400, "give the camera a name")
    addr = check_camera_host(host, bridge)
    cams = check_ports(ports)
    mine = targets_of(bridge["id"])
    if len(mine) >= MAX_TARGETS:
        raise Refused(409, f"a bridge carries at most {MAX_TARGETS} cameras")
    for t in mine:
        if t["host"] == addr and set(cams) & {p["port"] for p in t["ports"]}:
            raise Refused(409, f"{addr} is already bridged as {t['name']}")
    cid = (camera_id or "").strip()[:64] or None
    now = time.time()
    with db.engine().begin() as c:
        listen = _free_listen(c, ci["id"], len(cams))
        t = {"id": db.new_id("bt_"), "bridge_id": bridge["id"], "org_id": loc["org_id"], "location_id": loc["id"], "instance_id": ci["id"],
             "camera_id": cid, "name": name, "host": addr, "ports": [{"port": p, "listen": lp} for p, lp in zip(cams, listen)],
             "enabled": True, "created_at": now, "updated_at": now, "applied_at": None}
        c.execute(db.bridge_targets.insert().values(**t))
    try:
        await sync_relay(ci["id"], user)
    except hosts.HostError:
        with db.engine().begin() as c:
            c.execute(sa.delete(db.bridge_targets).where(db.bridge_targets.c.id == t["id"]))
        raise
    with db.engine().begin() as c:
        _bump_rev(c, bridge["id"])
    await registry.push_targets(bridge["id"])
    hosts._audit(user, loc["org_id"], ci["server_id"], f"bridged camera mapped: {name} ({addr}) via {bridge['name']}",
                 {"location_id": loc["id"], "bridge_id": bridge["id"], "target_id": t["id"], "instance_id": ci["id"], "host": addr,
                  "ports": t["ports"], "camera_id": cid})
    return t


def _camera_id(name: str, taken: set[str]) -> str:
    """The instance camera id for a new bridged camera: its name as [a-z0-9_] (CameraIn's pattern), made unique."""
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:24] or "cam"
    cid, n = base, 2
    while cid in taken:
        cid, n = f"{base}_{n}", n + 1
    return cid


async def add_to_instance(loc: dict, bridge: dict, target: dict, login: dict, user: dict) -> str:
    """The one-step Add (Shawn, 2026-10-18: mapping a camera means it is to be recorded): create the camera on the
    Site's central instance at its LAN address with the relay address and ports filled in (bridge_fill), through its
    tunnel, and remember which camera the target feeds. `login` = {username, password, main_path?, sub_path?}: the
    password goes straight to the instance (as from its console) and is never stored or logged on the hub."""
    from . import central_cameras   # (central_cameras imports this module)
    from .agents import registry as tunnels
    ci = hosts.get_instance(target["instance_id"])
    if not ci or ci["state"] in ("deleting", "deleted") or not ci["server_id"]:
        raise Refused(409, "the central instance is not running yet")
    conn = tunnels.get(ci["server_id"])
    if conn is None:
        raise BridgeError("the central instance is offline")
    headers = {"x-hub-user": user.get("email") or "hub", "x-hub-role": "admin"}
    try:
        status, raw = await conn.call("GET", "/api/cameras", "", dict(headers), None, FETCH_TIMEOUT_S)
        cams = json.loads(raw.decode() or "null") if status == 200 else None
    except Exception as e:
        raise BridgeError(f"could not read the instance's cameras: {e}") from None
    if not isinstance(cams, list):
        raise BridgeError("could not read the instance's cameras")
    existing = next((c for c in cams if isinstance(c, dict) and c.get("host") == target["host"]), None)
    if existing is not None:
        raise Refused(409, f"the instance already has {existing.get('name') or existing.get('id')} at {target['host']}: use Apply to camera")
    ports = {p["port"] for p in target["ports"]}
    cid = _camera_id(target["name"], {str(c.get("id")) for c in cams if isinstance(c, dict)})
    body = {"id": cid, "name": target["name"], "host": target["host"], "rtsp_port": 554 if 554 in ports else min(ports),
            "onvif_port": 80 if 80 in ports else min(ports - {554} or ports), "username": (login.get("username") or "admin").strip(),
            "password": login.get("password") or "", "main_path": login.get("main_path") or "/main",
            "sub_path": login.get("sub_path") or "/sub", "public_host": ""}
    raw = bridge_fill(ci, "camera", f"/api/cameras/{cid}", json.dumps(body).encode())
    if raw is None:
        raise Refused(409, "the camera's RTSP and ONVIF ports must both be bridged")
    path = f"/api/cameras/{quote(cid, safe='')}"
    try:
        await central_cameras.check(ci, "camera", path, raw)
    except central_cameras.Refused as e:
        raise Refused(e.status, e.detail) from None
    try:
        status, out = await conn.call("PUT", path, "", {**headers, "content-type": "application/json"}, raw, FETCH_TIMEOUT_S)
    except Exception as e:
        raise BridgeError(f"the instance did not take the camera: {e}") from None
    if status >= 400:
        detail = ""
        with contextlib.suppress(ValueError, UnicodeDecodeError, AttributeError, TypeError):
            detail = json.loads(out.decode()).get("detail") or ""
        raise Refused(status if status < 500 else 502, f"the instance refused the camera: {detail or status}")
    now = time.time()
    with db.engine().begin() as c:
        c.execute(sa.update(db.bridge_targets).where(db.bridge_targets.c.id == target["id"]).values(camera_id=cid, applied_at=now, updated_at=now))
    target.update(camera_id=cid, applied_at=now)
    hosts._audit(user, loc["org_id"], ci["server_id"], f"bridged camera added to recording: {target['name']} -> camera {cid}",
                 {"location_id": loc["id"], "bridge_id": bridge["id"], "target_id": target["id"], "instance_id": ci["id"], "camera_id": cid})
    return cid


async def update_target(loc: dict, bridge: dict, target: dict, vals: dict, user: dict) -> dict:
    """Rename, point at another instance camera (Apply), switch off / on, or move to another LAN address. Ports stay."""
    out: dict = {}
    if "name" in vals:
        n = (vals["name"] or "").strip()[:120]
        if not n:
            raise Refused(400, "give the camera a name")
        out["name"] = n
    if "camera_id" in vals:
        out["camera_id"] = (vals["camera_id"] or "").strip()[:64] or None
    if "enabled" in vals:
        out["enabled"] = bool(vals["enabled"])
    if "host" in vals:
        out["host"] = check_camera_host(vals["host"], bridge)
        for t in targets_of(bridge["id"]):
            if t["id"] != target["id"] and t["host"] == out["host"]:
                raise Refused(409, f"{out['host']} is already bridged as {t['name']}")
    changed = {k: v for k, v in out.items() if target.get(k) != v}
    if not changed:
        return target
    with db.engine().begin() as c:
        c.execute(sa.update(db.bridge_targets).where(db.bridge_targets.c.id == target["id"]).values(updated_at=time.time(), **changed))
        if changed.keys() & {"enabled", "host"}:
            _bump_rev(c, bridge["id"])
    if changed.keys() & {"enabled", "host"}:
        registry.abort_target(target["id"], "the bridged camera was changed")
        await registry.push_targets(bridge["id"])
    hosts._audit(user, loc["org_id"], None, f"bridged camera changed: {target['name']} via {bridge['name']}",
                 {"location_id": loc["id"], "bridge_id": bridge["id"], "target_id": target["id"], "from": {k: target.get(k) for k in changed},
                  "to": changed})
    return db.one(sa.select(db.bridge_targets).where(db.bridge_targets.c.id == target["id"])) or target


async def delete_target(loc: dict, bridge: dict, target: dict, user: dict) -> str | None:
    """Unmap a camera: its streams end, the bridge and the host forget it. Returns why the host was not told (it is
    told again on its next heartbeat), or None."""
    with db.engine().begin() as c:
        c.execute(sa.delete(db.bridge_targets).where(db.bridge_targets.c.id == target["id"]))
        _bump_rev(c, bridge["id"])
    registry.abort_target(target["id"], "the bridged camera was removed")
    await registry.push_targets(bridge["id"])
    err = None
    try:
        await sync_relay(target["instance_id"], user)
    except hosts.HostError as e:
        err = str(e)
    hosts._audit(user, loc["org_id"], None, f"bridged camera removed: {target['name']} via {bridge['name']}",
                 {"location_id": loc["id"], "bridge_id": bridge["id"], "target_id": target["id"], "instance_id": target["instance_id"],
                  "host": target["host"], "ports": target["ports"], **({"relay_error": err} if err else {})})
    return err


def rename_bridge(loc: dict, bridge: dict, name: str, user: dict) -> dict:
    n = (name or "").strip()[:120]
    if not n:
        raise Refused(400, "give the bridge a name")
    if n != bridge["name"]:
        db.run(sa.update(db.bridges).where(db.bridges.c.id == bridge["id"]).values(name=n))
        hosts._audit(user, loc["org_id"], None, f"Axiom Bridge renamed: {bridge['name']} -> {n}",
                     {"location_id": loc["id"], "bridge_id": bridge["id"], "from": bridge["name"], "to": n})
    return get_bridge(bridge["id"]) or bridge


async def rotate_token(loc: dict, bridge: dict, user: dict) -> None:
    """A new device token, sent down the live connection (`rotate`); the old one keeps working for ROTATE_GRACE_S so a
    bridge that misses the frame can still reconnect and get the next one. The bridge must be online."""
    conn = registry.bridges.get(bridge["id"])
    if conn is None or not conn.ready:
        raise Refused(409, "the bridge is offline: its token can only be rotated while it is connected")
    token = db.new_token()
    db.run(sa.update(db.bridges).where(db.bridges.c.id == bridge["id"]).values(
        token_hash=db.token_hash(token), token_prev_hash=bridge["token_hash"], token_rotated_at=time.time()))
    try:
        await conn.send({"t": "rotate", "token": token})
    except Exception as e:
        raise BridgeError(f"could not reach the bridge: {e}") from None
    hosts._audit(user, loc["org_id"], None, f"Axiom Bridge token rotated: {bridge['name']}", {"location_id": loc["id"], "bridge_id": bridge["id"]})


async def retire(loc: dict, bridge: dict, user: dict) -> list[str]:
    """Retire a bridge: its token stops working, it is told (`revoked`) and disconnected, its cameras are unmapped and
    their listen ports closed on the host. Returns the instances whose host could not be told (heartbeats retry)."""
    gone = targets_of(bridge["id"])
    with db.engine().begin() as c:
        c.execute(sa.update(db.bridges).where(db.bridges.c.id == bridge["id"]).values(retired_at=time.time(), online=False))
        c.execute(sa.delete(db.bridge_targets).where(db.bridge_targets.c.bridge_id == bridge["id"]))
    alerts.close(alert_subject(bridge), BRIDGE_ALERT)
    await registry.revoke(bridge["id"], "retired at the hub")
    failed = []
    for ci_id in sorted({t["instance_id"] for t in gone}):
        try:
            await sync_relay(ci_id, user)
        except hosts.HostError:
            failed.append(ci_id)
    hosts._audit(user, loc["org_id"], None, f"Axiom Bridge retired: {bridge['name']}",
                 {"location_id": loc["id"], "bridge_id": bridge["id"], "targets": [{"id": t["id"], "name": t["name"], "host": t["host"]} for t in gone]})
    return failed


# ---------------------------------------------------------------- what the pages show

def alert_subject(b: dict) -> dict:
    """alerts.open/close take a server-shaped dict: a bridge alert is the customer's row (site_id = the bridge id)."""
    return {"id": b["id"], "org_id": b["org_id"], "name": b.get("name") or b["id"], "location_id": b.get("location_id"), "bridge": True}


def visible_ids(org_id: str, location_ids: set[str] | None) -> dict[str, dict]:
    """bridge id -> {name, location_id} for the customer's bridges in these Sites (None = every Site): alert rows."""
    q = sa.select(db.bridges.c.id, db.bridges.c.name, db.bridges.c.location_id).where(db.bridges.c.org_id == org_id)
    rows = db.rows(q)
    return {r["id"]: r for r in rows if location_ids is None or r["location_id"] in location_ids}


def bridge_out(b: dict, admin: bool) -> dict:
    hello = b.get("hello") or {}
    stats = b.get("stats") or {}
    conn = registry.bridges.get(b["id"])
    live = conn is not None and conn.ready
    out = {"id": b["id"], "name": b["name"], "location_id": b["location_id"], "online": live, "last_seen_at": b["last_seen_at"],
           "created_at": b["created_at"], "version": hello.get("version"), "os": hello.get("os"), "hostname": hello.get("hostname"),
           "streams": len(conn.legs) if live else 0, "uptime_s": stats.get("uptime_s") if live else None,
           "bytes_up": stats.get("bytes_up"), "bytes_down": stats.get("bytes_down")}
    if admin:
        out |= {"arch": hello.get("arch"), "lan": hello.get("lan") or [], "caps": hello.get("caps") or [], "agent_ip": b.get("agent_ip")}
    return out


def target_out(t: dict, bridge: dict | None, admin: bool) -> dict:
    errs = ((bridge or {}).get("stats") or {}).get("target_errors") or {}
    mine = {k.split(":", 1)[1]: v for k, v in errs.items() if isinstance(k, str) and k.startswith(t["id"] + ":") and isinstance(v, str)}
    out = {"id": t["id"], "bridge_id": t["bridge_id"], "name": t["name"], "enabled": bool(t["enabled"]), "camera_id": t["camera_id"],
           "ports": [{"port": p["port"], "label": PORT_LABEL.get(p["port"])} for p in t["ports"]], "errors": mine,
           "probe": registry.probes.get(t["id"])}
    if admin:
        out |= {"host": t["host"], "ports": [{"port": p["port"], "listen": p["listen"], "label": PORT_LABEL.get(p["port"])} for p in t["ports"]],
                "instance_id": t["instance_id"], "applied_at": t["applied_at"]}
    return out


def site_view(loc: dict, admin: bool) -> dict:
    """GET /api/locations/{id}/bridges: the Site's bridges and bridged cameras (everyone who sees the Site, read-only);
    for its admins also the open enrollment codes, the central instance's relay and the cameras it could feed."""
    rows = db.rows(sa.select(db.bridges).where(db.bridges.c.location_id == loc["id"], db.bridges.c.retired_at.is_(None))
                   .order_by(db.bridges.c.created_at))
    by_id = {b["id"]: b for b in rows}
    out: dict = {"bridges": [bridge_out(b, admin) for b in rows],
                 "targets": [target_out(t, by_id.get(t["bridge_id"]), admin) for t in targets_of(location_id=loc["id"]) if t["bridge_id"] in by_id],
                 "can_manage": admin}
    if admin:
        ci = site_instance(loc["id"])
        info = (ci or {}).get("info") or {}
        relay = info.get("relay") if isinstance(info.get("relay"), dict) else {}
        out |= {"codes": open_codes(loc["id"]), "hub_url": bridge_url(),
                "instance": {"id": ci["id"], "name": ci["name"], "mode": ci["mode"], "server_id": ci["server_id"], "gateway": relay_address(ci),
                             "listening": [p for p in relay.get("listening") or [] if isinstance(p, int)],
                             "relay_errors": {str(k): str(v)[:200] for k, v in (relay.get("errors") or {}).items()} if isinstance(relay.get("errors"), dict) else {},
                             "host_online": hosts.registry.online(ci["host_id"]), "relay_connected": registry.relays.get(ci["host_id"]) is not None}
                if ci else None}
    return out


# ---------------------------------------------------------------- Apply to camera

APPLY_DROP = ("password", "status", "streams", "ptz_config")   # never sent back (read-only, or not ours to send)


async def apply_to_camera(loc: dict, bridge: dict, target: dict, camera_id: str | None, user: dict) -> dict:
    """Write the relay address into the instance's camera: GET /api/cameras through its tunnel (no passwords), then PUT
    that same camera back with only public_host and the public_*_port fields changed. The whole row is sent because the
    server's PUT /api/cameras/{id} resets fields a body leaves out (backend nvr/api.py CameraIn); the password is left
    out, which keeps it (CameraIn.password None = keep). Refused when the camera's RTSP or ONVIF port is not bridged."""
    from . import central_cameras   # (central_cameras imports this module)
    from .agents import registry as tunnels
    ci = hosts.get_instance(target["instance_id"])
    if not ci or ci["state"] in ("deleting", "deleted") or not ci["server_id"]:
        raise Refused(409, "the central instance is not running yet")
    gw = relay_address(ci)
    if not gw:
        raise Refused(409, "the instance's host has not reported its relay address yet (host agent 0.3.0 or later)")
    cid = (camera_id or target.get("camera_id") or "").strip()
    if not cid:
        raise Refused(400, "choose the instance's camera to apply it to")
    conn = tunnels.get(ci["server_id"])
    if conn is None:
        raise BridgeError("the central instance is offline")
    headers = {"x-hub-user": user.get("email") or "hub", "x-hub-role": "admin"}
    try:
        status, raw = await conn.call("GET", "/api/cameras", "", dict(headers), None, FETCH_TIMEOUT_S)
        cams = json.loads(raw.decode() or "null") if status == 200 else None
    except Exception as e:
        raise BridgeError(f"could not read the instance's cameras: {e}") from None
    cam = next((c for c in cams or [] if isinstance(c, dict) and c.get("id") == cid), None) if isinstance(cams, list) else None
    if cam is None:
        raise Refused(404, f"the instance has no camera {cid}: add it on its console first (its LAN address, username and password)")
    rtsp, onvif = listen_of(target, cam.get("rtsp_port") or 554), listen_of(target, cam.get("onvif_port") or 80)
    if rtsp is None or onvif is None:
        missing = [str(p) for p, lp in ((cam.get("rtsp_port") or 554, rtsp), (cam.get("onvif_port") or 80, onvif)) if lp is None]
        raise Refused(409, f"the camera's port {' and '.join(missing)} is not bridged: map it with those ports")
    web = cam.get("web_port")
    sheet = {"public_host": gw, "public_rtsp_port": rtsp, "public_onvif_port": onvif, "public_replay_port": listen_of(target, 555),
             # the web page on its own port only when that port is bridged; else (on the ONVIF port) the server uses ONVIF's
             "public_web_port": listen_of(target, web) if web and web != cam.get("onvif_port") else None}
    body = {k: v for k, v in cam.items() if k not in APPLY_DROP} | sheet
    path = f"/api/cameras/{quote(cid, safe='')}"
    try:
        await central_cameras.check(ci, "camera", path, body)
    except central_cameras.Refused as e:
        raise Refused(e.status, e.detail) from None
    try:
        status, raw = await conn.call("PUT", path, "", {**headers, "content-type": "application/json"}, json.dumps(body).encode(), FETCH_TIMEOUT_S)
    except Exception as e:
        raise BridgeError(f"the instance did not take it: {e}") from None
    if status != 200:
        try:
            why = json.loads(raw.decode() or "null")
            why = why.get("detail") if isinstance(why, dict) else None
        except ValueError:
            why = None
        raise BridgeError(f"the instance refused it ({status}){f': {why}' if why else ''}")
    central_cameras.after_write(ci["server_id"])
    db.run(sa.update(db.bridge_targets).where(db.bridge_targets.c.id == target["id"]).values(camera_id=cid, applied_at=time.time()))
    hosts._audit(user, loc["org_id"], ci["server_id"], f"bridged camera applied: {target['name']} -> camera {cid}",
                 {"location_id": loc["id"], "bridge_id": bridge["id"], "target_id": target["id"], "instance_id": ci["id"], "camera_id": cid, **sheet})
    return {"camera_id": cid, **sheet}


# ---------------------------------------------------------------- the two sockets and the splice

class Leg:
    """One side of a spliced stream: its id on its connection, the window this hub may still send into (Stream:
    take_credit / grant), the chunks it sent that are not passed on yet (Stream.inbox; `held` bytes, at most a window)
    and whether it half-closed."""

    def __init__(self, conn: "_Conn", sid: int) -> None:
        self.conn = conn
        self.id = sid
        self.s = Stream(sid, settings.bridge_window)
        self.held = 0
        self.ended = False
        self.opened = asyncio.Event()
        self.splice: Splice | None = None


class Splice:
    """A host-relay stream joined to a bridge stream; two pumps, one per direction."""

    def __init__(self, relay: Leg, bridge: Leg, target_id: str, instance_id: str) -> None:
        self.relay, self.bridge = relay, bridge
        self.target_id, self.instance_id = target_id, instance_id
        self.closed = False
        self.tasks: list[asyncio.Task] = []
        self._ended = 0                        # pumps that passed their side's end on
        relay.splice = bridge.splice = self

    def start(self) -> None:
        self.tasks = [asyncio.create_task(self._pump(self.relay, self.bridge), name=f"splice-up-{self.relay.id}"),
                      asyncio.create_task(self._pump(self.bridge, self.relay), name=f"splice-down-{self.relay.id}")]

    async def _pump(self, src: Leg, dst: Leg) -> None:
        """src's chunks on to dst under dst's window; src is credited once each chunk is on its way. At src's end, dst
        gets `end` after the last chunk (the same task sends both, so in order)."""
        try:
            while True:
                data = await src.s.read()
                if data is None:
                    break
                await dst.s.take_credit(len(data))
                if self.closed or not await dst.conn.send_chunk(dst, data):
                    return
                src.held -= len(data)
                await src.conn.send({"t": "credit", "id": src.id, "bytes": len(data)})
            if not self.closed and src.ended:
                await dst.conn.send_ordered(dst, {"t": "end", "id": dst.id})
                self._ended += 1
                if self._ended == 2:   # both directions closed and drained
                    self.finish()
        except asyncio.CancelledError:
            raise
        except Exception as e:   # a socket broke mid-send: the stream is reset on both sides
            await self.abort(f"relay failed: {type(e).__name__}")

    def finish(self) -> None:
        """Both sides half-closed and every byte passed on: forget the stream without an abort."""
        if self.closed:
            return
        self.closed = True
        for leg in (self.relay, self.bridge):
            if leg.conn.legs.get(leg.id) is leg:
                del leg.conn.legs[leg.id]

    async def abort(self, reason: str, origin: Leg | None = None) -> None:
        """Reset both sides: the one that did not start it is told (`abort {id, reason}`)."""
        if self.closed:
            return
        self.closed = True
        for leg in (self.relay, self.bridge):
            if leg.conn.legs.get(leg.id) is leg:
                del leg.conn.legs[leg.id]
            await leg.s.abort(reason)
            leg.opened.set()
        for leg in (self.relay, self.bridge):
            if leg is not origin and not leg.conn.closed:
                try:
                    await leg.conn.send({"t": "abort", "id": leg.id, "reason": reason[:200]})
                except Exception:
                    pass
        me = asyncio.current_task()
        for t in self.tasks:
            if t is not me and not t.done():
                t.cancel()


class _Conn:
    """What both sockets share: one writer at a time, the streams by id, and the frames that move a stream."""

    kind = "conn"

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.legs: dict[int, Leg] = {}
        self._lock = asyncio.Lock()
        self.closed = False
        self.last_seen = time.time()
        self.hello: dict = {}

    @property
    def ip(self) -> str | None:
        return self.ws.client.host if self.ws.client else None

    async def send(self, frame: dict) -> None:
        async with self._lock:
            await self.ws.send_text(encode(frame))

    async def send_chunk(self, leg: Leg, data: bytes) -> bool:
        """A chunk of this stream, unless it was reset meanwhile (checked under the writer lock: nothing follows an abort)."""
        async with self._lock:
            if leg.s.aborted is not None:
                return False
            await self.ws.send_bytes(chunk(leg.id, data))
            return True

    async def send_ordered(self, leg: Leg, frame: dict) -> None:
        async with self._lock:
            if leg.s.aborted is None:
                await self.ws.send_text(encode(frame))

    async def close(self, code: int, reason: str) -> None:
        try:
            await self.ws.close(code=code, reason=reason)
        except Exception:
            pass

    async def on_chunk(self, sid: int, data: bytes) -> None:
        leg = self.legs.get(sid)
        if leg is None:
            return   # a stream just reset: its last chunks are dropped
        leg.held += len(data)
        if leg.held > settings.bridge_window:
            await self.over_window(leg)
            return
        leg.s.push(data)

    async def over_window(self, leg: Leg) -> None:
        if leg.splice:
            await leg.splice.abort("more unacknowledged data than the window")

    async def on_stream_frame(self, t: str, frame: dict) -> None:
        sid = frame.get("id")
        leg = self.legs.get(sid) if isinstance(sid, int) and not isinstance(sid, bool) else None
        if leg is None:
            return
        if t == "end":
            leg.ended = True
            leg.s.push(None)
        elif t == "abort":
            if leg.splice:
                await leg.splice.abort(str(frame.get("reason") or f"{self.kind} aborted")[:200], origin=leg)
        elif t == "credit":
            n = frame.get("bytes")
            if isinstance(n, int) and not isinstance(n, bool) and 0 < n <= settings.bridge_window:
                await leg.s.grant(n)

    async def detach(self, reason: str) -> None:
        self.closed = True
        for leg in list(self.legs.values()):
            if leg.splice:
                await leg.splice.abort(reason, origin=leg)
        self.legs.clear()


class BridgeConn(_Conn):
    kind = "bridge"

    def __init__(self, ws: WebSocket, bridge: dict) -> None:
        super().__init__(ws)
        self.bridge = bridge
        self.id = bridge["id"]
        self.ready = False                     # welcome sent: streams may be opened
        self._next_id = 1                      # hub-opened streams are odd
        self._next_probe = 1
        self.opens: collections.deque = collections.deque()   # when the last streams were opened (bridge_open_rate)
        self.probes: dict[int, asyncio.Future] = {}

    def next_id(self) -> int:
        while True:
            sid = self._next_id
            self._next_id = (self._next_id + 2) & 0xFFFFFFFF or 1
            if sid not in self.legs:
                return sid

    def rate_ok(self, now: float) -> bool:
        while self.opens and now - self.opens[0] >= 1.0:
            self.opens.popleft()
        return len(self.opens) < settings.bridge_open_rate

    async def over_window(self, leg: Leg) -> None:
        """A bridge that sends more than the window it was given is broken or hostile: closed (its streams reset)."""
        log.warning("bridge %s: stream %s exceeded the window; closing", self.id, leg.id)
        await self.close(4400, "window exceeded")
        await self.detach("the bridge broke the protocol")


class RelayConn(_Conn):
    kind = "host"

    def __init__(self, ws: WebSocket, host: dict) -> None:
        super().__init__(ws)
        self.host = host
        self.host_id = host["id"]


def _int_id(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 < v <= 0xFFFFFFFF


class BridgeRegistry:
    def __init__(self) -> None:
        self.bridges: dict[str, BridgeConn] = {}
        self.relays: dict[str, RelayConn] = {}
        self.probes: dict[str, dict] = {}      # target id -> its last Test: {at, ports: [{port, ok, ms, error}]}
        self.started_at = time.time()

    def online(self, bridge_id: str) -> bool:
        c = self.bridges.get(bridge_id)
        return c is not None and c.ready

    # ---- /bridge
    async def serve_bridge(self, ws: WebSocket) -> None:
        scheme, _, cred = ws.headers.get("authorization", "").partition(" ")
        cred = cred.strip()
        ip = ws.client.host if ws.client else None
        if scheme == "Enroll" and cred:
            if code_check(cred) is None:
                log_bad_code(cred, ip)
                await ws.close(code=4403, reason="invalid enrollment code")   # before accept: the handshake gets 403
                return
            await ws.accept()
            await self._enroll(ws, cred, ip)
            return
        b = None
        if scheme == "Bearer" and cred:
            h = db.token_hash(cred)
            b = db.one(sa.select(db.bridges).where(db.bridges.c.token_hash == h, db.bridges.c.retired_at.is_(None)))
            if not b:
                prev = db.one(sa.select(db.bridges).where(db.bridges.c.token_prev_hash == h, db.bridges.c.retired_at.is_(None)))
                if prev and prev["token_rotated_at"] and time.time() - prev["token_rotated_at"] < ROTATE_GRACE_S:
                    b = prev
        if not b:
            await ws.close(code=4401, reason="unknown bridge token")
            return
        await ws.accept()
        conn = BridgeConn(ws, b)
        old = self.bridges.get(conn.id)
        if old is not None:
            await old.close(4409, "replaced by a newer connection")
            await old.detach("the bridge reconnected")
        self.bridges[conn.id] = conn
        try:
            await self._bridge_loop(conn)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("bridge %s: connection failed", conn.id)
        finally:
            await self._bridge_gone(conn)

    async def _enroll(self, ws: WebSocket, code: str, ip: str | None) -> None:
        """Wait for the bridge's hello (hostname, version), create it in the code's Site and hand it its device token,
        then close; it reconnects with Bearer (as agents._enroll does for central instances)."""
        hello: dict = {}
        try:
            m = await asyncio.wait_for(ws.receive(), 15)
            if m["type"] == "websocket.disconnect":
                return
            frame = decode(m["text"]) if m.get("text") is not None and len(m["text"]) <= MAX_TEXT else None
            if isinstance(frame, dict) and frame.get("t") == "hello":
                hello = frame
        except asyncio.TimeoutError:
            pass
        except Exception:
            log.exception("bridge enrollment: reading hello")
        got = enroll(code, hello, ip)
        if got is None:
            log_bad_code(code, ip)
            try:
                await ws.close(code=4403, reason="invalid enrollment code")
            except Exception:
                pass
            return
        b, token = got
        try:
            await ws.send_text(encode({"t": "enrolled", "bridge_id": b["id"], "token": token}))
        except Exception:
            enroll_undo(code, b["id"])   # it never got its token: the same code may try again
            return
        try:
            await ws.close(code=1000, reason="enrolled")
        except Exception:
            pass

    async def _bridge_loop(self, conn: BridgeConn) -> None:
        while True:
            m = await conn.ws.receive()
            if m["type"] == "websocket.disconnect":
                return
            raw = m.get("bytes")
            if raw is not None:
                if len(raw) < 4 or len(raw) > CHUNK + 4:
                    await conn.close(4400, "bad binary frame")
                    return
                sid, data = struct.unpack(">I", raw[:4])[0], raw[4:]
                await conn.on_chunk(sid, data)
                if conn.closed:
                    return
                continue
            text = m.get("text") or ""
            if len(text) > MAX_TEXT:
                await conn.close(4400, "frame too large")
                return
            try:
                frame = decode(text)
            except ValueError:
                continue
            if not isinstance(frame, dict):
                continue
            t = frame.get("t")
            if t == "hello":
                await self._on_bridge_hello(conn, frame)
            elif t == "heartbeat":
                self._on_bridge_heartbeat(conn, frame)
            elif t == "opened":
                leg = conn.legs.get(frame.get("id")) if _int_id(frame.get("id")) else None
                if leg is not None:
                    leg.opened.set()
            elif t in ("end", "abort", "credit"):
                await conn.on_stream_frame(t, frame)
            elif t == "probe_result":
                fut = conn.probes.get(frame.get("id")) if _int_id(frame.get("id")) else None
                if fut is not None and not fut.done():
                    fut.set_result(frame)
            elif t == "ping":
                await conn.send({"t": "pong", "ts": frame.get("ts")})
            elif t == "rotated":
                log.info("bridge %s confirmed its token rotation", conn.id)
            elif t == "open":   # only the hub opens streams on /bridge
                log.warning("bridge %s sent open: closing it", conn.id)
                await conn.close(4400, "bridges never open streams")
                return

    async def _on_bridge_hello(self, conn: BridgeConn, frame: dict) -> None:
        conn.hello = _clean_hello(frame)
        conn.last_seen = time.time()
        b = conn.bridge
        db.run(sa.update(db.bridges).where(db.bridges.c.id == conn.id).values(
            online=True, last_seen_at=time.time(), agent_ip=conn.ip, hello=conn.hello))
        conn.bridge = get_bridge(conn.id) or b
        await conn.send({"t": "welcome", "bridge_id": conn.id, "name": b["name"], "site": _loc_name(b["location_id"]),
                         "heartbeat_s": HEARTBEAT_S, "max_streams": settings.bridge_max_streams, "window": settings.bridge_window})
        await conn.send(targets_frame(conn.id))
        conn.ready = True
        alerts.close(alert_subject(b), BRIDGE_ALERT)
        log.info("bridge %s (%s) connected from %s, version %s", conn.id, b["name"], conn.ip, conn.hello.get("version"))

    def _on_bridge_heartbeat(self, conn: BridgeConn, frame: dict) -> None:
        conn.last_seen = time.time()
        num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else None  # noqa: E731
        errs = frame.get("target_errors") if isinstance(frame.get("target_errors"), dict) else {}
        stats = {"streams": num(frame.get("streams")), "bytes_up": num(frame.get("bytes_up")), "bytes_down": num(frame.get("bytes_down")),
                 "uptime_s": num(frame.get("uptime_s")),
                 "target_errors": {str(k)[:40]: str(v)[:200] for k, v in list(errs.items())[:64]}, "at": time.time()}
        db.run(sa.update(db.bridges).where(db.bridges.c.id == conn.id).values(online=True, last_seen_at=time.time(), stats=stats))

    async def _bridge_gone(self, conn: BridgeConn) -> None:
        await conn.detach("the bridge went offline")
        for fut in conn.probes.values():
            if not fut.done():
                fut.set_exception(BridgeError("the bridge disconnected"))
        if self.bridges.get(conn.id) is conn:
            del self.bridges[conn.id]
            db.run(sa.update(db.bridges).where(db.bridges.c.id == conn.id).values(online=False, last_seen_at=time.time()))
            log.info("bridge %s disconnected", conn.id)

    async def push_targets(self, bridge_id: str) -> None:
        conn = self.bridges.get(bridge_id)
        if conn is not None and conn.ready:
            try:
                await conn.send(targets_frame(bridge_id))
            except Exception as e:
                log.info("bridge %s: targets not sent (%s)", bridge_id, e)

    def abort_target(self, target_id: str, reason: str) -> None:
        for conn in list(self.bridges.values()):
            for leg in list(conn.legs.values()):
                if leg.splice and leg.splice.target_id == target_id:
                    _spawn(leg.splice.abort(reason), f"bridge-abort-{target_id}")

    async def revoke(self, bridge_id: str, reason: str) -> None:
        conn = self.bridges.get(bridge_id)
        if conn is None:
            return
        try:
            await conn.send({"t": "revoked", "reason": reason})
        except Exception:
            pass
        await conn.close(4401, "revoked")
        await conn.detach("the bridge was retired")

    async def probe(self, target: dict, port: int) -> dict:
        """One TCP connect from the bridge to the camera port: {port, ok, ms, error}."""
        conn = self.bridges.get(target["bridge_id"])
        if conn is None or not conn.ready:
            raise BridgeError("the bridge is offline")
        pid = conn._next_probe
        conn._next_probe += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        conn.probes[pid] = fut
        try:
            await conn.send({"t": "probe", "id": pid, "target": target["id"], "host": target["host"], "port": port})
            r = await asyncio.wait_for(fut, PROBE_TIMEOUT_S)
        except asyncio.TimeoutError:
            return {"port": port, "ok": False, "ms": None, "error": "no answer from the bridge"}
        except BridgeError as e:
            return {"port": port, "ok": False, "ms": None, "error": str(e)}
        finally:
            conn.probes.pop(pid, None)
        ms = r.get("ms")
        return {"port": port, "ok": r.get("ok") is True, "ms": ms if isinstance(ms, (int, float)) and not isinstance(ms, bool) else None,
                "error": str(r.get("error"))[:200] if r.get("error") else None}

    async def test_target(self, target: dict) -> dict:
        """Probe every port of a target at once (the Test button); kept for the page as the target's last Test."""
        res = await asyncio.gather(*(self.probe(target, p["port"]) for p in target["ports"]))
        out = {"at": time.time(), "ports": list(res)}
        self.probes[target["id"]] = out
        return out

    # ---- /host-relay
    async def serve_relay(self, ws: WebSocket) -> None:
        scheme, _, cred = ws.headers.get("authorization", "").partition(" ")
        host = db.one(sa.select(db.hosts).where(db.hosts.c.token_hash == db.token_hash(cred.strip()))) if scheme == "Bearer" and cred.strip() else None
        if not host:
            await ws.close(code=4401, reason="unknown host token")   # before accept: the handshake is refused (403)
            return
        await ws.accept()
        conn = RelayConn(ws, host)
        old = self.relays.get(conn.host_id)
        if old is not None:
            await old.close(4409, "replaced by a newer connection")
            await old.detach("the host relay reconnected")
        self.relays[conn.host_id] = conn
        log.info("host %s relay connected from %s", conn.host_id, conn.ip)
        try:
            await self._relay_loop(conn)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("host %s relay: connection failed", conn.host_id)
        finally:
            await conn.detach("the host relay disconnected")
            if self.relays.get(conn.host_id) is conn:
                del self.relays[conn.host_id]
                log.info("host %s relay disconnected", conn.host_id)

    async def _relay_loop(self, conn: RelayConn) -> None:
        tasks: set[asyncio.Task] = set()
        try:
            while True:
                m = await conn.ws.receive()
                if m["type"] == "websocket.disconnect":
                    return
                raw = m.get("bytes")
                if raw is not None:
                    if 4 <= len(raw) <= CHUNK + 4:
                        await conn.on_chunk(struct.unpack(">I", raw[:4])[0], raw[4:])
                    continue
                text = m.get("text") or ""
                if len(text) > MAX_TEXT:
                    continue
                try:
                    frame = decode(text)
                except ValueError:
                    continue
                if not isinstance(frame, dict):
                    continue
                t = frame.get("t")
                conn.last_seen = time.time()
                if t == "hello":
                    ls = frame.get("listeners") if isinstance(frame.get("listeners"), list) else []
                    conn.hello = {"version": str(frame.get("version") or "")[:32], "listeners": [x for x in ls[:2000] if isinstance(x, dict)]}
                elif t == "open":
                    task = asyncio.create_task(self._relay_open(conn, frame), name="relay-open")
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                elif t in ("end", "abort", "credit"):
                    await conn.on_stream_frame(t, frame)
                elif t == "ping":
                    await conn.send({"t": "pong", "ts": frame.get("ts")})
        finally:
            for task in tasks:
                task.cancel()

    def _route(self, conn: RelayConn, frame: dict) -> tuple[dict, int, BridgeConn]:
        """(target, camera port, bridge connection) for the host's `open`; Refused(reason) otherwise."""
        iid, port = frame.get("instance"), frame.get("port")
        if not isinstance(iid, str) or not _int_id(port) or port not in RELAY_PORTS:
            raise Refused(400, "unknown listener")
        ci = hosts.get_instance(iid[:24])
        # the host names a listener of one of its own instances, never a camera address and never another host's
        if not ci or ci["host_id"] != conn.host_id or ci["state"] not in ("provisioning", "running", "failed"):
            raise Refused(404, "unknown listener")
        target = next((t for t in targets_of(instance_id=ci["id"]) if port_of(t, port) is not None), None)
        if target is None or not target["enabled"]:
            raise Refused(404, "no bridged camera on that port")
        b = get_bridge(target["bridge_id"])
        bc = self.bridges.get(target["bridge_id"])
        if not b or b["retired_at"] or bc is None or not bc.ready:
            raise Refused(503, "the bridge is offline")
        if len(bc.legs) >= settings.bridge_max_streams:
            raise Refused(503, "the bridge carries as many streams as it may")
        if not bc.rate_ok(time.time()):
            raise Refused(503, "too many new connections to the bridge at once")
        return target, port_of(target, port), bc

    async def _relay_open(self, conn: RelayConn, frame: dict) -> None:
        sid = frame.get("id")
        if not _int_id(sid) or sid % 2 == 0 or sid in conn.legs:
            await self._refuse(conn, sid, "bad stream id")
            return
        rleg = Leg(conn, sid)
        conn.legs[sid] = rleg   # chunks that arrive before `opened` wait here (within the window)
        try:
            target, port, bc = self._route(conn, frame)
        except Refused as e:
            del conn.legs[sid]
            log.info("host %s: open %s:%s refused: %s", conn.host_id, frame.get("instance"), frame.get("port"), e.detail)
            await self._refuse(conn, sid, e.detail)
            return
        bleg = Leg(bc, bc.next_id())
        bc.legs[bleg.id] = bleg
        splice = Splice(rleg, bleg, target["id"], target["instance_id"])
        bc.opens.append(time.time())
        try:
            await bc.send({"t": "open", "id": bleg.id, "target": target["id"], "host": target["host"], "port": port})
            await asyncio.wait_for(bleg.opened.wait(), settings.bridge_open_timeout_s)
        except asyncio.TimeoutError:
            await splice.abort("the bridge did not answer")
            return
        except Exception:
            await splice.abort("the bridge went offline")
            return
        if splice.closed:   # the bridge refused it (its reason went to the host) or it went away meanwhile
            return
        try:
            await conn.send({"t": "opened", "id": sid})
        except Exception:
            await splice.abort("the host relay went away", origin=rleg)
            return
        splice.start()

    @staticmethod
    async def _refuse(conn: RelayConn, sid, reason: str) -> None:
        try:
            await conn.send({"t": "abort", "id": sid, "reason": reason})
        except Exception:
            pass

    async def sweep(self) -> None:
        """Close bridges that stopped heartbeating; open bridge_offline for bridges gone longer than offline_after_s."""
        now = time.time()
        cutoff = now - settings.offline_after_s
        for conn in list(self.bridges.values()):
            if conn.last_seen < cutoff:
                await conn.close(4408, "no heartbeat")
        if now - self.started_at > 120:   # not in the first two minutes after a restart
            for b in db.rows(sa.select(db.bridges).where(db.bridges.c.retired_at.is_(None))):
                if b["id"] in self.bridges or not b["last_seen_at"] or b["last_seen_at"] >= cutoff:
                    continue
                if b["online"]:   # the hub restarted under it: the row still says online
                    db.run(sa.update(db.bridges).where(db.bridges.c.id == b["id"]).values(online=False))
                alerts.open(alert_subject(b), BRIDGE_ALERT, "", {"name": b["name"], "location_id": b["location_id"], "last_seen_at": b["last_seen_at"],
                                                                "text": f"{b['name']} is offline: the cameras behind it are not being recorded"})
        # spent and expired enrollment codes are kept a week for the audit trail's sake, then dropped
        db.run(sa.delete(db.bridge_enroll_codes).where(db.bridge_enroll_codes.c.expires_at < now - 7 * 86400))


registry = BridgeRegistry()
