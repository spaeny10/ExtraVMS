#!/usr/bin/env python3
"""axiom-host: provisions and reports Axiom Vision central recording instances on one Docker host.

One instance = one customer Site's recording server (the ordinary site server in the `axiom/instance` image),
in its own container, Docker network, Unix uid, storage quota (its own ZFS datasets, or an XFS project) and
nftables egress allow-list. The hub drives this agent over a WebSocket (see PROTOCOL.md); the same operations
work from the command line.

    axiom_host.py create-instance --id acme-gate --location loc_123 --name "Acme Gate · Central" \
        --mode vpn --subnet 10.20.7.0/24 --quota-gb 4000 --gpu 1 --enroll-token-file /root/t [--dry-run]
    axiom_host.py delete-instance --id acme-gate --keep-data | --purge
    axiom_host.py set-quota --id acme-gate --quota-gb 6000
    axiom_host.py set-camera-network --id acme-gate --subnet 192.168.105.0/24 --public-ip 203.0.113.7 \
        --host cam1.example.net [--clear subnets|public-ips|hosts|all] [--dry-run]
    axiom_host.py restart-instance --id acme-gate [--recreate] [--image axiom/instance:2026.10.06]
    axiom_host.py list | capacity | render-firewall | apply-firewall | reconcile | finish-enroll --id ...
    axiom_host.py run --hub wss://hub.axiomvision.ai/host-agent --token-file /etc/axiom/host-token

Python 3.12, standard library plus `websockets` (agent mode only) and `certifi` (optional, for the CA bundle).
Docker, nft, zfs and xfs_quota are driven through their command-line tools. Run as root (systemd unit in systemd/).
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import copy
import ipaddress
import json
import logging
import os
import random
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, NamedTuple

VERSION = "0.1.0"
PROTO = 1
log = logging.getLogger("axiom-host")

ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,30}[a-z0-9])?$")   # the hub sends e.g. ci_1a2b3c4d5e6f
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{8,512}$")
ZFS_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*(?:/[A-Za-z0-9][A-Za-z0-9_.:-]*)*$")   # no @snap, #bookmark
QUOTA_MODES = ("auto", "zfs", "xfs", "none")
MODES = ("vpn", "forward")
# An instance's camera allow-list: LAN/VPN subnets (any protocol), public IPs and DNS names (TCP only)
CAMERA_KEYS = ("subnets", "public_ips", "hosts")
MAX_CAMERA_ENTRIES = 32                         # all three lists together, per instance
CAMERA_NETS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",   # RFC 1918
                                                       "100.64.0.0/10"))                                 # carrier-grade NAT

DEFAULTS: dict[str, Any] = {
    "root": "/srv/axiom",                       # instances/<id>/, recordings/<id>/, registry.json, firewall.nft
    "image": "axiom/instance:latest",
    "pool": "10.200.0.0/16",                    # instance networks: one /28 each (4096 instances)
    "vllm_container": "axiom-vllm",             # tools/central/ai/compose.yml container_name
    "vlm_url": "http://vllm:8000/v1",           # "vllm" = the alias the vLLM container gets on each instance network
    "vlm_model": "",                            # empty = AXIOM_VLM_MODEL from ai_env
    "ai_env": "/etc/axiom/ai.env",              # VLLM_API_KEY and AXIOM_VLM_MODEL (shared with the compose file)
    "dns": ["1.1.1.1", "9.9.9.9"],              # the only resolvers instances may query (passed as --dns)
    "hub_url": "wss://hub.axiomvision.ai/agent",  # what instances dial; its host's addresses are allowed out
    "hub_ips": [],                              # fixed hub addresses; [] = resolve the hub host names (every 10 min)
    "hub_tcp_ports": ["443", "3478"],           # HTTPS/WSS and TURN over TCP (hub/coturn)
    "hub_udp_ports": ["3478", "49152-49252"],   # TURN and its relay range (hub/coturn/turnserver.conf)
    "forward_tcp_ports": [],                    # allowed ports on camera public IPs / DNS names ([] = any TCP)
    "ai_network": "10.201.0.0/24",              # the axiom-ai Docker network (ai/compose.yml): never a camera network
    "fusionhub_network": "10.19.0.0/24",        # FusionHub's LAN side (PEPLINK.md): never a camera network
    "mem_gb": 8,
    "cpus": 4,
    "uid_base": 20000,                          # instance uid = uid_base + slot
    "project_base": 70000,                      # XFS project id = project_base + slot
    "instance_dir_quota_gb": 50,                # ZFS mode: quota of each instances/<id> dataset (database, event media)
    "tmpfs_mb": 1024,
    "shm_mb": 1024,
    "pids_limit": 4096,
    "log_max_size": "50m",
    "log_max_file": 5,
    "read_only": True,
}

PROBE = ("import json,urllib.request as u\n"
         "g=lambda p:json.load(u.urlopen('http://127.0.0.1:8080'+p,timeout=8))\n"
         "h=g('/api/hub')\n"
         "try:\n c=g('/api/cameras'); n=len(c) if isinstance(c,list) else len(c.get('cameras',[]))\n"
         "except Exception:\n n=None\n"
         "print(json.dumps({'enrolled':bool(h.get('enrolled')),'site_id':h.get('site_id'),'cameras':n}))\n")


class OpError(ValueError):
    """A request the agent refuses (bad arguments, unknown instance, conflict). The detail is safe to show."""


class CmdError(RuntimeError):
    def __init__(self, argv: list[str], rc: int, err: str) -> None:
        self.argv, self.rc, self.err = argv, rc, err
        super().__init__(f"{' '.join(argv[:4])}... exited {rc}: {err.strip()[-400:]}")


class Result(NamedTuple):
    rc: int
    out: str
    err: str


# ---------------------------------------------------------------- side effects

class Exec:
    """Every command and file change goes through here. dry_run records the plan and changes nothing; read-only
    queries (docker ps, nvidia-smi, findmnt) still run so a dry run sees the real host."""

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self.plan: collections.deque = collections.deque(maxlen=None if dry_run else 200)   # agent: bounded

    def run(self, argv: list[str], *, check: bool = True, input: str | None = None, timeout: float = 600,
            display: list[str] | None = None) -> Result:
        self.plan.append({"cmd": display or argv})
        if self.dry_run:
            return Result(0, "", "")
        r = self._run(argv, input, timeout)
        if check and r.rc != 0:
            raise CmdError(display or argv, r.rc, r.err)
        return r

    def query(self, argv: list[str], timeout: float = 30) -> Result:
        return self._run(argv, None, timeout)

    def _run(self, argv: list[str], input: str | None, timeout: float) -> Result:
        try:
            p = subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return Result(127, "", f"{argv[0]}: not found")
        except subprocess.TimeoutExpired:
            return Result(124, "", f"{argv[0]}: timed out after {timeout:.0f} s")
        return Result(p.returncode, p.stdout, p.stderr)

    def write(self, path: Path, text: str, mode: int = 0o600, display: str | None = None) -> None:
        self.plan.append({"file": str(path), "mode": oct(mode), "content": text if display is None else display})
        if self.dry_run:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)

    def mkdir(self, path: Path, mode: int, uid: int | None = None) -> None:
        self.plan.append({"mkdir": str(path), "mode": oct(mode), **({"owner": f"{uid}:{uid}"} if uid is not None else {})})
        if self.dry_run:
            return
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)
        if uid is not None and hasattr(os, "chown"):
            os.chown(path, uid, uid)

    def rmtree(self, path: Path) -> None:
        self.plan.append({"rmtree": str(path)})
        if not self.dry_run and path.exists():
            shutil.rmtree(path)


# ---------------------------------------------------------------- pure helpers (tested directly)

def parse_nvidia_smi(text: str) -> list[dict]:
    """`nvidia-smi --query-gpu=index,name,memory.total,memory.used,utilization.gpu --format=csv,noheader,nounits`."""
    def num(s: str) -> float | int | None:
        s = s.strip()
        try:
            v = float(s)
        except ValueError:
            return None   # "[N/A]", "[Not Supported]"
        return int(v) if v.is_integer() else v

    gpus = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5 or not parts[0].isdigit():
            continue
        gpus.append({"index": int(parts[0]), "name": ", ".join(parts[1:-3]), "mem_total_mb": num(parts[-3]),
                     "mem_used_mb": num(parts[-2]), "util": num(parts[-1])})
    return gpus


def parse_meminfo(text: str) -> dict:
    kb = {}
    for line in text.splitlines():
        k, _, v = line.partition(":")
        if v.strip().endswith("kB"):
            kb[k.strip()] = int(v.split()[0])
    return {"total": round(kb.get("MemTotal", 0) / 1e6, 1), "free": round(kb.get("MemAvailable", kb.get("MemFree", 0)) / 1e6, 1)}


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            if v and not v.startswith("<"):   # "<generate: ...>" = a placeholder from the example file
                out[k.strip()] = v
    return out


def _env_value(k: str, v: str) -> str:
    # docker --env-file takes the rest of the line verbatim (no quoting, no escapes): refuse anything multi-line
    if "\n" in v or "\r" in v or "\0" in v:
        raise OpError(f"{k} must be a single line")
    return f"{k}={v}"


def render_env(rec: dict, *, enroll_token: str | None, vlm_key: str | None, mask: bool = False) -> str:
    """instance.env (docker --env-file). mask=True replaces secrets for dry-run output."""
    secret = (lambda s, what: f"<{what}: {len(s)} chars>") if mask else (lambda s, what: s)
    lines = [f"# Axiom Vision central instance {rec['id']}: written by axiom_host.py {VERSION}. Changes are lost on",
             "# set-quota / recreate; edit the registry through axiom_host.py instead.",
             _env_value("NVR_INSTANCE_NAME", rec["name"]),
             _env_value("NVR_HUB_URL", rec["hub_url"])]
    if enroll_token:
        lines.append("# one-time enrollment into the hub Site; ignored once enrolled and removed from this file then")
        lines.append(_env_value("NVR_HUB_ENROLL_TOKEN", secret(enroll_token, "enroll token")))
    lines += ["# reached only through the hub tunnel: no Direct-on-LAN, and the API listens on loopback inside the container",
              "NVR_DIRECT_ENABLED=0", "NVR_HOST=127.0.0.1",
              "# no local Ollama: every Qwen task goes to the host's shared vLLM",
              "NVR_LOCAL_VLM_ENABLED=0", "NVR_OLLAMA_EXE=/nonexistent"]
    if rec.get("vlm_url") and rec.get("vlm_model") and vlm_key:
        lines += [_env_value("NVR_REMOTE_VLM_URL", rec["vlm_url"]),
                  _env_value("NVR_REMOTE_VLM_KEY", secret(vlm_key, "vLLM key")),
                  _env_value("NVR_REMOTE_VLM_MODEL", rec["vlm_model"]),
                  # vLLM: turn Qwen3 thinking off through the chat template (some builds reject reasoning_effort "none")
                  _env_value("NVR_REMOTE_VLM_NO_THINK", "chat_template")]
    else:
        lines.append("# no host vLLM configured: the instance uses whatever shared AI the hub pushes")
    if rec.get("gpu") is None:
        lines += ["# no GPU assigned: YOLO on the CPU (same settings as tools/deploy_site.sh --cpu)",
                  "NVR_YOLO_DEVICE=cpu", "NVR_YOLO_MODEL=yolo11n.pt", "NVR_YOLO_IMGSZ=640", "NVR_VERIFY_FRAMES=4",
                  "NVR_FOOTAGE_INDEX_ENABLED=0"]
    else:
        lines += [f"# the container sees only host GPU {rec['gpu']} (docker --gpus device={rec['gpu']}), as cuda:0",
                  "NVR_YOLO_DEVICE=cuda:0"]
    lines += ["NVR_RECORDINGS_DIR=/recordings", "NVR_DATA_DIR=/data", "NVR_RUNTIME_DIR=/data/runtime",
              "NVR_BACKUP_DIR=/data/backups", "NVR_FOOTAGE_INDEX_DIR=/data/index", "NVR_MEDIAMTX_EXE=/opt/nvr/bin/mediamtx/mediamtx"]
    return "\n".join(lines) + "\n"


def scrub_enroll_token(text: str) -> str:
    out = [ln for ln in text.splitlines()
           if not ln.startswith("NVR_HUB_ENROLL_TOKEN=") and "one-time enrollment into the hub Site" not in ln]
    return "\n".join(out) + "\n"


def _nft_set(items: list[str]) -> str:
    return "{ " + ", ".join(items) + " }"


def render_firewall(instances: list[dict], *, pool: str, hub_ips: list[str], dns: list[str],
                    hub_tcp_ports: list[str], hub_udp_ports: list[str], forward_tcp_ports: list[str],
                    source: str = "registry.json") -> str:
    """The whole `inet axiom` table, replaced atomically by one `nft -f` (create-if-missing, delete, define)."""
    hub = sorted(set(hub_ips), key=lambda a: ipaddress.ip_address(a))
    dns = sorted(set(dns), key=lambda a: ipaddress.ip_address(a))
    L = ["#!/usr/sbin/nft -f",
         f"# Axiom Vision central host: per-instance egress isolation. Generated by axiom_host.py {VERSION} from {source}.",
         "# Do not edit: create/delete-instance and apply-firewall replace this whole table in one transaction.",
         "table inet axiom",
         "delete table inet axiom",
         "table inet axiom {"]
    if hub:
        L.append(f"\tset hub4 {{ type ipv4_addr; elements = {_nft_set(hub)} }}")
    if dns:
        L.append(f"\tset dns4 {{ type ipv4_addr; elements = {_nft_set(dns)} }}")
    L += ["",
          "\t# Instance -> the host itself (gateway address, public IP, sshd, this agent): never.",
          "\tchain input {",
          "\t\ttype filter hook input priority filter - 10; policy accept;",
          f"\t\tip saddr {pool} ct state established,related accept",
          f"\t\tip saddr {pool} counter drop comment \"instance to host\"",
          "\t}",
          "",
          "\t# Runs before Docker's own iptables-nft FORWARD rules (priority filter). Our drop is final; our accept",
          "\t# only ends this chain, Docker's chains still decide (and masquerade) afterwards.",
          "\tchain forward {",
          "\t\ttype filter hook forward priority filter - 10; policy accept;",
          f"\t\tip saddr != {pool} ip daddr != {pool} accept",
          "\t\tct state established,related accept",
          "\t\tct state invalid drop"]
    for rec in sorted(instances, key=lambda r: r["slot"]):
        L.append(f"\t\tip saddr {rec['ip']} jump {_chain(rec)}")
    L += [f"\t\tip saddr {pool} counter drop comment \"unregistered address in the instance pool (incl. vLLM)\"",
          f"\t\tip daddr {pool} counter drop comment \"nothing opens connections into an instance\"",
          "\t}"]
    for rec in sorted(instances, key=lambda r: r["slot"]):
        L += ["", f"\t# {rec['id']} ({rec['mode']}): {rec['name']}", f"\tchain {_chain(rec)} {{",
              f"\t\tip daddr {rec['vllm_ip']} tcp dport 8000 accept comment \"shared vLLM on this instance's network\""]
        if dns:
            L.append("\t\tip daddr @dns4 meta l4proto { tcp, udp } th dport 53 accept")
        if hub:
            L.append(f"\t\tip daddr @hub4 tcp dport {_nft_set(hub_tcp_ports)} accept comment \"hub tunnel, TURN/TCP\"")
            L.append(f"\t\tip daddr @hub4 udp dport {_nft_set(hub_udp_ports)} accept comment \"TURN\"")
        L += _camera_rules(rec, forward_tcp_ports)
        L += ["\t\tcounter drop comment \"everything else, incl. other instances and other sites\"", "\t}"]
    L.append("}")
    return "\n".join(L) + "\n"


def _camera_rules(rec: dict, forward_tcp_ports: list[str]) -> list[str]:
    """The instance's camera allow-list: its subnets (LAN / SpeedFusion VPN, any protocol), then its public IPs and
    the last resolved addresses of its DNS names (port forwards, TCP only)."""
    L = []
    subnets = sorted(set(rec.get("subnets") or []), key=ipaddress.ip_network)
    if subnets:
        L.append(f"\t\tip daddr {_nft_set(subnets)} accept comment \"camera networks (LAN, SpeedFusion VPN)\"")
    host_ips = rec.get("host_ips") or {}
    for name in rec.get("hosts") or []:
        got = host_ips.get(name) or []
        L.append(f"\t\t# {name}: {', '.join(got) if got else 'not resolved yet (nothing allowed for it)'}")
    tcp = set(rec.get("public_ips") or [])
    for name in rec.get("hosts") or []:
        tcp.update(host_ips.get(name) or [])
    if tcp:
        ports = f" tcp dport {_nft_set(forward_tcp_ports)}" if forward_tcp_ports else " meta l4proto tcp"
        L.append(f"\t\tip daddr {_nft_set(sorted(tcp, key=ipaddress.ip_address))}{ports} accept "
                 "comment \"remote cameras: public IPs and DNS names (port forwards, TCP)\"")
    return L


def _chain(rec: dict) -> str:
    return "inst_" + rec["id"].replace("-", "_")


def gb_bytes(gb: float) -> int:
    """quota_gb is decimal GB (like the server's own disk figures); zfs's G suffix is GiB, so quotas go in bytes."""
    return int(round(float(gb) * 1e9))


def parse_zfs_get(text: str) -> dict[str, dict[str, int | None]]:
    """`zfs get -Hp -o name,property,value <props> <datasets>` -> {dataset: {property: int or None}}.
    -p prints exact byte counts; "-" (not applicable) becomes None, and quota 0 means no quota."""
    out: dict[str, dict[str, int | None]] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        try:
            v: int | None = int(parts[2])
        except ValueError:
            v = None
        out.setdefault(parts[0], {})[parts[1]] = v
    return out


def zfs_child(parent: str | None, iid: str) -> str:
    """The dataset name of instance `iid` under `parent`. Refuses anything that is not exactly one level below
    `parent` with a leaf matching the instance id rule, so no caller can ever name the parent itself, a sibling
    tree, a snapshot or a pool root."""
    if not parent or not ZFS_NAME_RE.fullmatch(parent):
        raise OpError(f"no usable ZFS parent dataset ({parent!r}) for instance {iid!r}")
    if not ID_RE.fullmatch(iid):
        raise OpError(f"refusing ZFS dataset for instance id {iid!r}")
    return f"{parent}/{iid}"


def check_zfs_child(name: str, parent: str | None, iid: str) -> str:
    """`name` must be exactly zfs_child(parent, iid). Used before every zfs set/destroy on a registry entry."""
    if not isinstance(name, str) or name != zfs_child(parent, iid):
        raise OpError(f"refusing to touch ZFS dataset {name!r}: it is not {parent}/{iid}, the dataset of instance {iid}")
    return name


def _ip_list(v: Any) -> list[str]:
    if v is None or v == "":
        return []
    items = v if isinstance(v, list) else str(v).split(",")
    return [str(i).strip() for i in items if str(i).strip()]


def _is_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def is_hostname(name: str) -> bool:
    """A DNS name like cam1.example.net: two or more labels of letters, digits and '-' (not at either end of a
    label), at most 253 characters, the last label not all digits (so it can never be an IP address)."""
    if not isinstance(name, str) or not 1 <= len(name) <= 253:
        return False
    labels = name.split(".")
    if len(labels) < 2 or labels[-1].isdigit():
        return False
    return all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", lb) for lb in labels)


Forbidden = list[tuple[ipaddress.IPv4Network, str]]   # (network, what it is) no camera entry may touch


def camera_addr_ok(addr: str, forbidden: Forbidden) -> bool:
    """A single camera address (a public IP, or what a camera DNS name resolved to) the firewall may open over TCP."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.version == 4 and not (ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_link_local
                                    or ip.is_reserved) and not any(ip in n for n, _ in forbidden)


def check_camera_network(subnets: Any, public_ips: Any, hosts: Any, forbidden: Forbidden) -> dict[str, list[str]]:
    """Validate and normalize an instance's camera allow-list. Raises OpError with a message safe to show.

    subnets: IPv4 networks inside 10/8, 172.16/12, 192.168/16 or 100.64/10, /16 or smaller, no host bits set.
    public_ips: IPv4 addresses that are not loopback, link-local, multicast, unspecified or reserved.
    hosts: DNS names (dynamic DNS), lowercased. None of them may touch a `forbidden` network (instance pool, AI
    network, hub addresses). At most MAX_CAMERA_ENTRIES entries in all; duplicates are dropped."""
    out: dict[str, list[str]] = {"subnets": [], "public_ips": [], "hosts": []}
    for s in _ip_list(subnets):
        try:
            n = ipaddress.ip_network(s, strict=True)
        except ValueError as e:
            raise OpError(f"subnet {s}: {e}") from None
        if n.version != 4:
            raise OpError(f"subnet {s}: IPv4 only")
        if n.prefixlen < 16:
            raise OpError(f"subnet {s}: broader than a /16")
        if not any(n.subnet_of(c) for c in CAMERA_NETS):
            raise OpError(f"subnet {s}: must be a private network (inside 10.0.0.0/8, 172.16.0.0/12 or 192.168.0.0/16) "
                          "or carrier-grade NAT (100.64.0.0/10)")
        for f, what in forbidden:
            if n.overlaps(f):
                raise OpError(f"subnet {s} overlaps {what}")
        if str(n) not in out["subnets"]:
            out["subnets"].append(str(n))
    for p in _ip_list(public_ips):
        try:
            ip = ipaddress.ip_address(p)
        except ValueError as e:
            raise OpError(f"public_ip {p}: {e}") from None
        if not camera_addr_ok(str(ip), []):
            raise OpError(f"public_ip {p}: not a usable camera address")
        for f, what in forbidden:
            if ip in f:
                raise OpError(f"public_ip {p} is inside {what}")
        if str(ip) not in out["public_ips"]:
            out["public_ips"].append(str(ip))
    for h in _ip_list(hosts):
        name = h.lower().rstrip(".")
        if _is_ip(name):
            raise OpError(f"host {h}: an IP address; list it under public IPs")
        if not is_hostname(name) or name.endswith(".localhost"):
            raise OpError(f"host {h}: not a valid DNS name (e.g. cam1.example.net)")
        if name not in out["hosts"]:
            out["hosts"].append(name)
    n = sum(len(v) for v in out.values())
    if n > MAX_CAMERA_ENTRIES:
        raise OpError(f"{n} camera addresses: at most {MAX_CAMERA_ENTRIES} per instance")
    return out


def resolve_ipv4(name: str) -> list[str]:
    """The IPv4 addresses of a DNS name (OSError when it does not resolve)."""
    return sorted({sa[0] for *_, sa in socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_STREAM)},
                  key=ipaddress.ip_address)


def camera_summary(rec: dict) -> str:
    items = [*rec.get("subnets", []), *rec.get("public_ips", []), *rec.get("hosts", [])]
    return ", ".join(items) if items else "none"


# ---------------------------------------------------------------- the host

class Host:
    def __init__(self, cfg: dict | None = None, exe: Exec | None = None,
                 resolver: Callable[[str], list[str]] | None = None) -> None:
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.exe = exe or Exec()
        self.resolver = resolver or resolve_ipv4    # DNS name -> IPv4 addresses (OSError = does not resolve)
        self.root = Path(self.cfg["root"])
        self.pool = ipaddress.ip_network(self.cfg["pool"])
        self._lock = threading.RLock()
        self._depth = 0
        self._inst_locks: dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
        self.probe_cache: dict[str, dict] = {}       # id -> {"at", "cameras", "enrolled"}
        self.du_cache: dict[str, float] = {}         # id -> used GB (hosts without XFS or ZFS quotas)

    # -- registry
    @property
    def registry_path(self) -> Path:
        return self.root / "registry.json"

    def load(self) -> dict:
        try:
            reg = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            reg = {}
        reg.setdefault("version", 1)
        reg.setdefault("next_slot", 0)
        reg.setdefault("hub_ips", [])
        reg.setdefault("instances", {})
        for rec in reg["instances"].values():   # registries from before camera networks: same allow-list as before
            for k in CAMERA_KEYS:
                rec.setdefault(k, [])
            rec.setdefault("host_ips", {})
        return reg

    def save(self, reg: dict) -> None:
        if self.exe.dry_run:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.registry_path.with_name("registry.json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(reg, f, indent=1, sort_keys=True)
        os.replace(tmp, self.registry_path)

    @contextlib.contextmanager
    def locked(self):
        """In-process lock plus an flock on <root>/.lock, so the CLI and the running agent never interleave."""
        with self._lock:
            self._depth += 1
            fh = None
            try:
                if self._depth == 1 and not self.exe.dry_run:   # flock is per open file: take it once per thread
                    try:
                        import fcntl
                    except ImportError:   # Windows (tests)
                        fcntl = None
                    if fcntl:
                        self.root.mkdir(parents=True, exist_ok=True)
                        fh = open(self.root / ".lock", "a")
                        fcntl.flock(fh, fcntl.LOCK_EX)
                yield
            finally:
                self._depth -= 1
                if fh:
                    fh.close()

    # -- paths and names
    def inst_dir(self, iid: str) -> Path:
        return self.root / "instances" / iid

    def rec_dir(self, iid: str) -> Path:
        return self.root / "recordings" / iid

    def env_path(self, iid: str) -> Path:
        return self.inst_dir(iid) / "instance.env"

    @staticmethod
    def cname(iid: str) -> str:
        return f"axiom-{iid}"

    netname = cname

    def slot_net(self, slot: int) -> ipaddress.IPv4Network:
        net = ipaddress.ip_network(f"{self.pool.network_address + 16 * slot}/28")
        if not net.subnet_of(self.pool):
            raise OpError(f"instance pool {self.pool} is full")
        return net

    # -- host facts
    def fs_info(self) -> tuple[str, str, str]:
        """(mount point, fs type, options) of the filesystem holding root."""
        probe = self.root if self.root.exists() else self.root.parent
        r = self.exe.query(["findmnt", "-n", "-o", "TARGET,FSTYPE,OPTIONS", "--target", str(probe)])
        parts = r.out.split()
        return (parts[0], parts[1], parts[2]) if r.rc == 0 and len(parts) >= 3 else ("", "", "")

    def zfs_dataset_at(self, path: Path) -> str | None:
        """The ZFS dataset mounted exactly at `path` (not merely somewhere above it), or None."""
        r = self.exe.query(["findmnt", "-n", "-o", "FSTYPE,SOURCE", "--mountpoint", str(path)])
        lines = [ln.split() for ln in r.out.splitlines() if ln.strip()] if r.rc == 0 else []
        if lines and len(lines[-1]) >= 2 and lines[-1][0] == "zfs" and ZFS_NAME_RE.fullmatch(lines[-1][1]):
            return lines[-1][1]   # the last line is the topmost mount
        return None

    def zfs_parents(self) -> dict[str, str | None]:
        """{"recordings": dataset at <root>/recordings, "instance": dataset at <root>/instances} (None = not ZFS)."""
        return {"recordings": self.zfs_dataset_at(self.root / "recordings"),
                "instance": self.zfs_dataset_at(self.root / "instances")}

    def zfs_get(self, names: list[str], props: tuple[str, ...]) -> dict[str, dict[str, int | None]]:
        if not names:
            return {}
        r = self.exe.query(["zfs", "get", "-Hp", "-o", "name,property,value", ",".join(props), *names])
        return parse_zfs_get(r.out)   # a missing dataset is an error line on stderr; the others still parse

    def zfs_owned(self, rec: dict, kind: str, parents: dict[str, str | None] | None = None) -> str | None:
        """The registry's dataset of `kind` for this instance, checked against what is mounted now."""
        name = (rec.get("datasets") or {}).get(kind)
        if not name:
            return None
        return check_zfs_child(name, (parents or self.zfs_parents())[kind], rec["id"])

    def zfs_set_quota_cmds(self, rec: dict, parents: dict[str, str | None] | None = None) -> list[list[str]]:
        parents = parents or self.zfs_parents()
        cmds = []
        for kind, gb in (("recordings", rec["quota_gb"]), ("instance", rec.get("instance_quota_gb"))):
            ds = self.zfs_owned(rec, kind, parents)
            if ds and gb:
                cmds.append(["zfs", "set", f"quota={gb_bytes(gb)}", ds])
        return cmds

    def quota_mode_auto(self) -> str:
        if self.zfs_dataset_at(self.root / "recordings"):
            return "zfs"
        _, fstype, opts = self.fs_info()
        return "xfs" if fstype == "xfs" and ({"prjquota", "pquota"} & set(opts.split(","))) else "none"

    def gpus(self) -> list[dict]:
        r = self.exe.query(["nvidia-smi", "--query-gpu=index,name,memory.total,memory.used,utilization.gpu",
                            "--format=csv,noheader,nounits"])
        return parse_nvidia_smi(r.out) if r.rc == 0 else []

    def ai_env(self) -> dict[str, str]:
        return read_env_file(Path(self.cfg["ai_env"]))

    def hub_hosts(self, reg: dict) -> list[str]:
        urls = [self.cfg["hub_url"], *(r.get("hub_url", "") for r in reg["instances"].values())]
        return sorted({h for u in urls if (h := urllib.parse.urlsplit(u).hostname)})

    def resolve_hub(self, reg: dict) -> list[str]:
        if self.cfg["hub_ips"]:
            return sorted(self.cfg["hub_ips"], key=ipaddress.ip_address)
        ips: set[str] = set()
        for host in self.hub_hosts(reg):
            try:
                ipaddress.ip_address(host)
                ips.add(host)
                continue
            except ValueError:
                pass
            try:
                ips.update(self.resolver(host))
            except OSError as e:
                log.warning("could not resolve %s (%s); keeping the last known addresses", host, e)
        return sorted(ips, key=ipaddress.ip_address) or list(reg.get("hub_ips", []))

    def forbidden(self, reg: dict, hub_ips: list[str] | None = None) -> Forbidden:
        """Networks no camera entry may touch: the instance pool, the AI network and the hub's addresses."""
        out: Forbidden = [(self.pool, f"the instance pool {self.pool}")]
        ai = ipaddress.ip_network(self.cfg["ai_network"])
        out.append((ai, f"the AI network {ai}"))
        if self.cfg.get("fusionhub_network"):
            fh = ipaddress.ip_network(self.cfg["fusionhub_network"])
            out.append((fh, f"the FusionHub network {fh}"))
        for ip in sorted({*(reg.get("hub_ips", []) if hub_ips is None else hub_ips), *self.cfg["hub_ips"]}):
            with contextlib.suppress(ValueError):
                out.append((ipaddress.ip_network(f"{ip}/32"), f"the hub address {ip}"))
        return out

    def resolve_camera_hosts(self, reg: dict, hub_ips: list[str] | None = None) -> dict[str, dict[str, list[str]]]:
        """{instance id: {DNS name: IPv4 addresses}} for every instance's camera host names. A name that does not
        resolve keeps its last known addresses; resolved addresses a camera may not have (loopback, the instance
        pool, the AI network, the hub...) are left out."""
        bad = self.forbidden(reg, hub_ips)
        out: dict[str, dict[str, list[str]]] = {}
        for iid, rec in reg["instances"].items():
            known = rec.get("host_ips") or {}
            got: dict[str, list[str]] = {}
            for name in rec.get("hosts") or []:
                try:
                    ips = set(self.resolver(name))
                except OSError as e:
                    log.warning("%s: could not resolve camera host %s (%s); keeping the last known addresses", iid, name, e)
                    ips = set(known.get(name) or [])
                ok = {ip for ip in ips if camera_addr_ok(ip, bad)}
                if ips - ok:
                    log.warning("%s: camera host %s resolves to %s: not allowed, left out", iid, name, ", ".join(sorted(ips - ok)))
                got[name] = sorted(ok, key=ipaddress.ip_address)
            out[iid] = got
        return out

    def dns_changed(self, reg: dict) -> str:
        """Why the firewall needs re-applying after a fresh lookup of the hub and camera host names ('' = it doesn't)."""
        ips = self.resolve_hub(reg)
        if ips != reg.get("hub_ips"):
            return f"hub addresses changed {reg.get('hub_ips')} -> {ips}"
        cams = self.resolve_camera_hosts(reg, ips)
        for iid, rec in reg["instances"].items():
            if cams.get(iid, {}) != (rec.get("host_ips") or {}):
                return f"{iid}: camera host addresses changed {rec.get('host_ips') or {}} -> {cams.get(iid, {})}"
        return ""

    # -- validation
    def _validate_create(self, a: dict, reg: dict) -> dict:
        iid = str(a.get("id") or "")
        if not ID_RE.fullmatch(iid):
            raise OpError("id must be 1-32 lowercase letters, digits, '-' or '_' (not at either end)")
        if iid in reg["instances"]:
            raise OpError(f"instance {iid} already exists")
        if any(_chain(r) == _chain({"id": iid}) for r in reg["instances"].values()):
            raise OpError(f"id {iid} is too similar to an existing instance's ('-' and '_' read the same)")
        loc = str(a.get("location") or a.get("location_id") or "")
        if not loc or len(loc) > 64 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", loc):
            raise OpError("location (the hub location_id) is required")
        name = str(a.get("name") or "").strip()
        if not name or len(name) > 100 or any(c in name for c in "\r\n\0"):
            raise OpError("name is required (one line, at most 100 characters)")
        mode = a.get("mode") or "vpn"
        if mode not in MODES:
            raise OpError("mode must be vpn or forward")
        # the camera allow-list: the hub's single subnet / public_ip, or the lists; a DNS name sent as public_ip
        # (the hub accepts one for a forward-mode Site) is a camera host name
        public: list[str] = []
        names = [*_ip_list(a.get("hosts"))]
        for p in [*_ip_list(a.get("public_ip")), *_ip_list(a.get("public_ips"))]:
            (public if _is_ip(p) else names).append(p)
        cams = self._validate_cameras({"subnets": [*_ip_list(a.get("subnet")), *_ip_list(a.get("subnets"))],
                                       "public_ips": public, "hosts": names}, reg)
        if not any(cams.values()):
            raise OpError("give at least one camera address: --subnet (the camera LAN or VPN subnet, e.g. 10.20.7.0/24), "
                          "--public-ip (a site router's public IP) or --host (its dynamic DNS name)")
        try:
            quota = float(a.get("quota_gb"))
        except (TypeError, ValueError):
            raise OpError("quota_gb is required (GB)") from None
        if not 10 <= quota <= 10_000_000:
            raise OpError("quota_gb must be between 10 and 10,000,000")
        gpu = a.get("gpu")
        if gpu in (None, "", "none", "cpu"):
            gpu = None
        else:
            try:
                gpu = int(gpu)
            except (TypeError, ValueError):
                raise OpError("gpu must be a GPU index from nvidia-smi or 'none'") from None
            if not self.exe.dry_run:
                known = [g["index"] for g in self.gpus()]
                if gpu not in known:
                    raise OpError(f"GPU {gpu} not found (nvidia-smi lists {known or 'none'})")
        token = a.get("enroll_token") or None
        if token is not None and not TOKEN_RE.fullmatch(str(token)):
            raise OpError("enroll_token has unexpected characters")
        hub_url = a.get("hub_url") or self.cfg["hub_url"]
        if urllib.parse.urlsplit(hub_url).scheme not in ("wss", "ws") or not urllib.parse.urlsplit(hub_url).hostname:
            raise OpError("hub_url must be a wss:// URL")
        mem_gb = float(a.get("mem_gb") or self.cfg["mem_gb"])
        cpus = float(a.get("cpus") or self.cfg["cpus"])
        if not (1 <= mem_gb <= 512 and 0.5 <= cpus <= 128):
            raise OpError("mem_gb must be 1-512 and cpus 0.5-128")
        qm = a.get("quota_mode") or "auto"
        if qm not in QUOTA_MODES:
            raise OpError("quota_mode must be auto, zfs, xfs or none")
        qm = self.quota_mode_auto() if qm == "auto" else qm
        ai = self.ai_env()
        for p in (self.inst_dir(iid), self.rec_dir(iid)):
            if p.exists() and any(p.iterdir()):
                raise OpError(f"{p} holds data from an earlier instance {iid}; remove it or choose another id")
        zfs: dict = {}
        if qm == "zfs":
            parents = self.zfs_parents()
            if not parents["recordings"]:
                raise OpError(f"quota_mode zfs needs a ZFS dataset mounted at {self.root / 'recordings'} (see README)")
            try:
                inst_q = float(self.cfg["instance_dir_quota_gb"])
            except (TypeError, ValueError):
                inst_q = 0.0
            if not 1 <= inst_q <= 10_000_000:
                raise OpError("host.json instance_dir_quota_gb must be between 1 and 10,000,000")
            datasets = {"recordings": zfs_child(parents["recordings"], iid),
                        "instance": zfs_child(parents["instance"], iid) if parents["instance"] else None}
            for ds in filter(None, datasets.values()):
                if self.exe.query(["zfs", "list", "-H", "-o", "name", ds]).rc == 0:
                    raise OpError(f"ZFS dataset {ds} already exists (data kept from an earlier instance {iid}?); "
                                  "destroy it or choose another id")
            zfs = {"datasets": datasets, "instance_quota_gb": inst_q}
        return {"id": iid, "location_id": loc, "name": name, "mode": mode, **cams, "host_ips": {},
                "quota_gb": quota, "gpu": gpu, "mem_gb": mem_gb, "cpus": cpus, "quota_mode": qm, **zfs,
                "image": a.get("image") or self.cfg["image"], "hub_url": hub_url,
                "vlm_url": a.get("vlm_url") or self.cfg["vlm_url"],
                "vlm_model": a.get("vlm_model") or self.cfg["vlm_model"] or ai.get("AXIOM_VLM_MODEL", ""),
                "_token": token, "_vlm_key": a.get("vlm_key") or ai.get("VLLM_API_KEY", "")}

    def _validate_cameras(self, a: dict, reg: dict, exclude: str | None = None) -> dict[str, list[str]]:
        """check_camera_network plus: no other instance on this host has an overlapping subnet, the same public IP
        (or one inside this instance's subnets) or the same DNS name; one site's cameras are never another's."""
        cams = check_camera_network(a.get("subnets"), a.get("public_ips"), a.get("hosts"), self.forbidden(reg))
        mine = [ipaddress.ip_network(s) for s in cams["subnets"]]
        for other in reg["instances"].values():
            if other["id"] == exclude:
                continue
            theirs = [ipaddress.ip_network(s) for s in other.get("subnets", [])]
            for s in mine:
                if any(s.overlaps(o) for o in theirs):
                    raise OpError(f"subnet {s} overlaps {other['id']}'s camera subnet: every site needs its own")
                if any(ipaddress.ip_address(p) in s for p in other.get("public_ips", [])):
                    raise OpError(f"subnet {s} contains a public IP of {other['id']}")
            for p in cams["public_ips"]:
                if p in other.get("public_ips", []):
                    raise OpError(f"public IP {p} already used by {other['id']}")
                if any(ipaddress.ip_address(p) in o for o in theirs):
                    raise OpError(f"public IP {p} is inside {other['id']}'s camera subnet")
            for h in cams["hosts"]:
                if h in other.get("hosts", []):
                    raise OpError(f"host {h} already used by {other['id']}")
        return cams

    # -- docker / quota / firewall building blocks
    def docker_run_argv(self, rec: dict) -> list[str]:
        c = self.cfg
        uid = rec["uid"]
        argv = ["docker", "run", "-d", "--name", self.cname(rec["id"]), "--hostname", rec["id"].replace("_", "-"),
                "--label", f"axiom.instance={rec['id']}", "--label", f"axiom.location={rec['location_id']}",
                "--network", self.netname(rec["id"]), "--ip", rec["ip"]]
        for d in c["dns"]:
            argv += ["--dns", d]
        argv += ["--env-file", str(self.env_path(rec["id"])),
                 "--user", f"{uid}:{uid}",
                 "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                 "--memory", f"{rec['mem_gb']:g}g", "--memory-swap", f"{rec['mem_gb']:g}g", "--cpus", f"{rec['cpus']:g}",
                 "--pids-limit", str(c["pids_limit"]), "--ulimit", "nofile=65536:65536", "--shm-size", f"{c['shm_mb']}m",
                 "--restart", "unless-stopped", "--init", "--stop-timeout", "30",
                 "--log-driver", "json-file", "--log-opt", f"max-size={c['log_max_size']}",
                 "--log-opt", f"max-file={c['log_max_file']}",
                 "--mount", f"type=bind,src={self.inst_dir(rec['id']) / 'data'},dst=/data",
                 "--mount", f"type=bind,src={self.rec_dir(rec['id'])},dst=/recordings"]
        if c["read_only"]:
            argv += ["--read-only", "--tmpfs", f"/tmp:rw,nosuid,nodev,size={c['tmpfs_mb']}m"]
        if rec.get("gpu") is not None:
            argv += ["--gpus", f"device={rec['gpu']}"]
        argv.append(rec["image"])
        return argv

    def network_argv(self, rec: dict) -> list[str]:
        return ["docker", "network", "create", "--driver", "bridge", "--subnet", rec["net_subnet"],
                "--gateway", rec["gateway"], "--opt", f"com.docker.network.bridge.name={rec['bridge']}",
                "--opt", "com.docker.network.bridge.enable_icc=true",
                "--label", f"axiom.instance={rec['id']}", self.netname(rec["id"])]

    def vllm_connect_argv(self, rec: dict) -> list[str]:
        return ["docker", "network", "connect", "--ip", rec["vllm_ip"], "--alias", "vllm",
                self.netname(rec["id"]), self.cfg["vllm_container"]]

    def xfs_cmds(self, rec: dict, mount: str, *, setup: bool) -> list[list[str]]:
        pid = rec["project_id"]
        kib = int(rec["quota_gb"] * 1e9 / 1024)   # quota_gb is decimal GB, like the server's own disk figures
        cmds = []
        if setup:
            for p in (self.rec_dir(rec["id"]), self.inst_dir(rec["id"]) / "data"):
                cmds.append(["xfs_quota", "-x", "-c", f"project -s -p {p} {pid}", mount])
        cmds.append(["xfs_quota", "-x", "-c", f"limit -p bsoft=0 bhard={kib}k {pid}", mount])
        return cmds

    def firewall_text(self, reg: dict, hub_ips: list[str] | None = None) -> str:
        return render_firewall(list(reg["instances"].values()), pool=str(self.pool),
                               hub_ips=hub_ips if hub_ips is not None else reg.get("hub_ips", []),
                               dns=self.cfg["dns"], hub_tcp_ports=self.cfg["hub_tcp_ports"],
                               hub_udp_ports=self.cfg["hub_udp_ports"],
                               forward_tcp_ports=self.cfg["forward_tcp_ports"], source=str(self.registry_path))

    def apply_firewall(self, reg: dict, *, resolve: bool = True) -> str:
        """Resolve the hub and camera host names (unless resolve=False: the last known addresses), then write
        firewall.nft and load it with one `nft -f` (atomic: on failure the kernel keeps the previous ruleset)."""
        if resolve:
            reg["hub_ips"] = self.resolve_hub(reg)
            for iid, got in self.resolve_camera_hosts(reg).items():
                reg["instances"][iid]["host_ips"] = got
        if not reg["hub_ips"]:
            log.warning("no hub address known: instances cannot reach the hub until it resolves")
        text = self.firewall_text(reg)
        path = self.root / "firewall.nft"
        self.exe.write(path, text, 0o600)
        self.exe.run(["nft", "-f", str(path)])
        return text

    # -- operations
    def create_instance(self, a: dict) -> dict:
        with self.locked():
            reg = self.load()
            spec = self._validate_create(a, reg)
            token, vlm_key = spec.pop("_token"), spec.pop("_vlm_key")
            slot = reg["next_slot"]
            net = self.slot_net(slot)
            hosts = list(net.hosts())
            rec = {**spec, "slot": slot, "net_subnet": str(net), "gateway": str(hosts[0]), "ip": str(hosts[1]),
                   "vllm_ip": str(hosts[2]), "bridge": f"axb{slot}", "uid": self.cfg["uid_base"] + slot,
                   "project_id": self.cfg["project_base"] + slot, "enroll_pending": bool(token),
                   "created_at": int(time.time())}
            iid = rec["id"]
            warnings = []
            if not (rec["vlm_model"] and vlm_key):
                warnings.append(f"no vLLM key/model in {self.cfg['ai_env']}: the instance uses the hub's shared AI")
            if rec["quota_mode"] == "none":
                warnings.append("no ZFS dataset or XFS project quota here: quota_gb is NOT enforced (see README)")
            ds = rec.get("datasets") or {}
            if rec["quota_mode"] == "zfs" and not ds.get("instance"):
                warnings.append(f"{self.root / 'instances'} is not a ZFS dataset: the instance's data/ has no quota")
            undo: list[Callable[[], None]] = []

            def zfs_create(name: str, gb: float) -> None:
                # a child of the dataset mounted at <root>/recordings (or /instances): it inherits the mountpoint
                # <parent mountpoint>/<id>, compression and recordsize, and zfs mounts it on creation
                self.exe.run(["zfs", "create", "-o", f"quota={gb_bytes(gb)}", name])
                undo.append(lambda: self.exe.run(["zfs", "destroy", name], check=False))

            try:
                if ds.get("instance"):
                    zfs_create(ds["instance"], rec["instance_quota_gb"])
                self.exe.mkdir(self.inst_dir(iid), 0o711)   # on ZFS: sets mode/owner of the new mountpoint
                if not ds.get("instance"):
                    undo.append(lambda: self.exe.rmtree(self.inst_dir(iid)))
                self.exe.mkdir(self.inst_dir(iid) / "data", 0o700, rec["uid"])
                if ds.get("recordings"):
                    zfs_create(ds["recordings"], rec["quota_gb"])
                self.exe.mkdir(self.rec_dir(iid), 0o700, rec["uid"])
                if not ds.get("recordings"):
                    undo.append(lambda: self.exe.rmtree(self.rec_dir(iid)))
                if rec["quota_mode"] == "xfs":
                    mount = self.fs_info()[0] or str(self.root)
                    for cmd in self.xfs_cmds(rec, mount, setup=True):
                        self.exe.run(cmd)
                self.exe.write(self.env_path(iid), render_env(rec, enroll_token=token, vlm_key=vlm_key), 0o600,
                               display=render_env(rec, enroll_token=token, vlm_key=vlm_key, mask=True))
                self.exe.run(self.network_argv(rec))
                undo.append(lambda: self.exe.run(["docker", "network", "rm", self.netname(iid)], check=False))
                reg["instances"][iid] = rec
                reg["next_slot"] = slot + 1
                fw = self.apply_firewall(reg)    # rules exist before the container's first packet
                self.save(reg)
                undo.append(lambda: self._forget(iid))
                r = self.exe.run(self.vllm_connect_argv(rec), check=False)
                if r.rc != 0:
                    warnings.append(f"vLLM container not connected yet ({r.err.strip()[:120]}); reconcile retries")
                undo.append(lambda: self.exe.run(["docker", "rm", "-f", self.cname(iid)], check=False))
                self.exe.run(self.docker_run_argv(rec))
            except Exception:
                if not self.exe.dry_run:
                    for f in reversed(undo):
                        with contextlib.suppress(Exception):
                            f()
                raise
            out = {"detail": f"created {iid}" + ("".join(f"; {w}" for w in warnings)), "instance": self.view(rec)}
            if self.exe.dry_run:
                out["dry_run"] = {"docker_run": shlex.join(self.docker_run_argv(rec)),
                                  "network": shlex.join(self.network_argv(rec)),
                                  "env": render_env(rec, enroll_token=token, vlm_key=vlm_key, mask=True),
                                  "firewall": fw, "steps": list(self.exe.plan)}
            return out

    def _forget(self, iid: str) -> None:
        reg = self.load()
        if reg["instances"].pop(iid, None):
            self.save(reg)
            self.apply_firewall(reg, resolve=False)

    def _get(self, reg: dict, a: dict) -> dict:
        iid = str(a.get("id") or "")
        if iid not in reg["instances"]:
            raise OpError(f"no instance {iid!r} on this host")
        return reg["instances"][iid]

    def delete_instance(self, a: dict) -> dict:
        # Footage is kept unless the caller says otherwise: the hub sends `purge` (true = delete), the CLI
        # `keep_data`. Neither = keep.
        keep = bool(a["keep_data"]) if "keep_data" in a else not bool(a.get("purge"))
        problems: list[str] = []
        with self.locked():
            reg = self.load()
            rec = self._get(reg, a)
            iid = rec["id"]
            # ZFS purge: exactly <recordings parent>/<id> and <instances parent>/<id>, never -r, never a parent.
            # Checked before anything is touched, so a name that does not match refuses the whole delete.
            destroy: dict[Path, str] = {}
            if rec["quota_mode"] == "zfs" and not keep:
                parents = self.zfs_parents()
                for kind, path in (("recordings", self.rec_dir(iid)), ("instance", self.inst_dir(iid))):
                    if (name := self.zfs_owned(rec, kind, parents)) is not None:
                        destroy[path] = name
            if not keep:
                for p in (self.inst_dir(iid), self.rec_dir(iid)):
                    if p not in destroy and p.resolve().parent.parent != self.root.resolve():
                        raise OpError(f"refusing to remove {p}")
            with self._inst_locks[iid]:
                self.exe.run(["docker", "rm", "-f", self.cname(iid)], check=False)
                self.exe.run(["docker", "network", "disconnect", "-f", self.netname(iid), self.cfg["vllm_container"]],
                             check=False)
                self.exe.run(["docker", "network", "rm", self.netname(iid)], check=False)
                del reg["instances"][iid]
                self.apply_firewall(reg)
                self.save(reg)
                if rec["quota_mode"] == "xfs":
                    mount = self.fs_info()[0] or str(self.root)
                    self.exe.run(["xfs_quota", "-x", "-c", f"limit -p bsoft=0 bhard=0 {rec['project_id']}", mount],
                                 check=False)
                if not keep:
                    for p in (self.rec_dir(iid), self.inst_dir(iid)):
                        if p in destroy:
                            try:
                                self.exe.run(["zfs", "destroy", destroy[p]])
                            except CmdError as e:   # e.g. snapshots exist: left for an admin, not forced
                                problems.append(f"could not destroy ZFS dataset {destroy[p]} ({e.err.strip()[-200:]}); "
                                                "remove it by hand")
                        else:
                            self.exe.rmtree(p)
                else:   # kept ZFS datasets stay as they are, quota included
                    env = self.env_path(iid)
                    if env.exists() and not self.exe.dry_run:
                        self.exe.write(env, scrub_enroll_token(env.read_text(encoding="utf-8")), 0o600)
        self.probe_cache.pop(iid, None)
        kept = f"; data kept in {self.inst_dir(iid)} and {self.rec_dir(iid)}" if keep else "; data removed"
        if problems:
            kept = "; " + "; ".join(problems)
        return {"detail": f"deleted {iid}{kept}"}

    def set_quota(self, a: dict) -> dict:
        try:
            quota = float(a.get("quota_gb"))
        except (TypeError, ValueError):
            raise OpError("quota_gb is required (GB)") from None
        if not 10 <= quota <= 10_000_000:
            raise OpError("quota_gb must be between 10 and 10,000,000")
        with self.locked():
            reg = self.load()
            rec = self._get(reg, a)
            zfs_rec = self.zfs_owned(rec, "recordings") if rec["quota_mode"] == "zfs" else None
            # ZFS: quota_gb limits the recordings dataset alone (the instance dir has its own), so compare with that
            used = self.zfs_used_gb([zfs_rec]) if zfs_rec else self.used_gb(rec)
            if used is not None and quota < used and not a.get("force"):
                raise OpError(f"{rec['id']} already uses {used:.0f} GB; a {quota:.0f} GB quota would make it delete "
                              "footage at once (pass force to do it anyway)")
            rec["quota_gb"] = quota
            if rec["quota_mode"] == "xfs":
                mount = self.fs_info()[0] or str(self.root)
                for cmd in self.xfs_cmds(rec, mount, setup=False):
                    self.exe.run(cmd)
            if zfs_rec:
                self.exe.run(["zfs", "set", f"quota={gb_bytes(quota)}", zfs_rec])
            self.save(reg)
        note = "" if rec["quota_mode"] in ("xfs", "zfs") else " (not enforced: no ZFS dataset or XFS project quota)"
        return {"detail": f"{rec['id']} quota {quota:g} GB{note}", "instance": self.view(rec)}

    def set_camera_network(self, a: dict) -> dict:
        """Replace the instance's camera allow-list. Each of subnets / public_ips / hosts that is sent (a list, or a
        comma-separated string; [] empties it) replaces that list; one left out (or null) stays as it is. The
        firewall is regenerated and loaded like on create/delete. Idempotent: the same request again changes nothing
        (it re-resolves the DNS names)."""
        with self.locked():
            reg = self.load()
            rec = self._get(reg, a)
            cur = {k: list(rec.get(k) or []) for k in CAMERA_KEYS}
            want = {k: cur[k] if a.get(k) is None else a[k] for k in CAMERA_KEYS}
            cams = self._validate_cameras(want, reg, exclude=rec["id"])
            path = self.root / "firewall.nft"
            before = self.firewall_text(reg)
            rec.update(cams)
            rec["host_ips"] = {n: ips for n, ips in (rec.get("host_ips") or {}).items() if n in cams["hosts"]}
            try:
                fw = self.apply_firewall(reg)
            except CmdError:
                if not self.exe.dry_run:   # nft kept the old ruleset; keep the file (loaded at boot) in step with it
                    with contextlib.suppress(OSError):
                        self.exe.write(path, before, 0o600)
                raise
            self.save(reg)
        unchanged = " (unchanged)" if cams == cur else ""
        warn = "" if any(cams.values()) else "; no camera addresses: the instance can reach no camera"
        unresolved = [n for n in cams["hosts"] if not rec["host_ips"].get(n)]
        if unresolved:
            warn += f"; not resolved yet: {', '.join(unresolved)} (retried every 10 minutes)"
        out = {"detail": f"{rec['id']} cameras: {camera_summary(rec)}{unchanged}{warn}", "instance": self.view(rec)}
        if self.exe.dry_run:
            out["dry_run"] = {"firewall": fw, "steps": list(self.exe.plan)}
        return out

    def restart_instance(self, a: dict) -> dict:
        with self.locked():
            reg = self.load()
            rec = self._get(reg, a)
            if a.get("image"):
                rec["image"] = str(a["image"])
                self.save(reg)
        with self._inst_locks[rec["id"]]:
            if a.get("recreate") or a.get("image"):
                self.exe.run(["docker", "rm", "-f", self.cname(rec["id"])], check=False)
                self.exe.run(self.docker_run_argv(rec))
                what = "recreated"
            else:
                self.exe.run(["docker", "restart", "-t", "30", self.cname(rec["id"])])
                what = "restarted"
        return {"detail": f"{what} {rec['id']}", "instance": self.view(rec)}

    def list_instances(self, a: dict | None = None) -> dict:
        reg = self.load()
        states = self.docker_states()
        views = [self.view(r, states) for r in sorted(reg["instances"].values(), key=lambda r: r["slot"])]
        return {"detail": f"{len(views)} instance(s)", "instances": views}

    def dispatch(self, op: str, args: dict) -> dict:
        ops = {"create_instance": self.create_instance, "delete_instance": self.delete_instance,
               "set_quota": self.set_quota, "set_camera_network": self.set_camera_network,
               "restart_instance": self.restart_instance, "list": self.list_instances}
        if op not in ops:
            raise OpError(f"unknown op {op!r}")
        if not isinstance(args, dict):
            raise OpError("args must be an object")
        if args.get("dry_run") and op != "list":
            return Host(self.cfg, Exec(dry_run=True), self.resolver).dispatch(op, {k: v for k, v in args.items() if k != "dry_run"})
        return ops[op](args)

    # -- status
    def docker_states(self) -> dict[str, str]:
        r = self.exe.query(["docker", "ps", "-a", "--filter", "label=axiom.instance",
                            "--format", '{{.Label "axiom.instance"}}\t{{.State}}'])
        out = {}
        for line in r.out.splitlines():
            iid, _, state = line.partition("\t")
            if iid:
                out[iid] = state.strip()
        return out

    def zfs_used_gb(self, names: list[str]) -> float | None:
        """Sum of `zfs get -Hp used` over the datasets (data + snapshots + children), None if any is unreadable."""
        props = self.zfs_get(names, ("used",))
        vals = [props.get(n, {}).get("used") for n in names]
        if not names or any(v is None for v in vals):
            return None
        return round(sum(vals) / 1e9, 1)

    def used_gb(self, rec: dict) -> float | None:
        if rec.get("quota_mode") == "zfs":   # recordings dataset + instance dir dataset
            names = [n for n in (rec.get("datasets") or {}).values() if n]
            return self.zfs_used_gb(names) if names else None
        if rec.get("quota_mode") == "xfs" and hasattr(os, "statvfs"):
            try:   # on a project-quota directory XFS reports the project's own usage and limit
                st = os.statvfs(self.rec_dir(rec["id"]))
                return round((st.f_blocks - st.f_bfree) * st.f_frsize / 1e9, 1)
            except OSError:
                return None
        return self.du_cache.get(rec["id"])

    def refresh_du(self) -> None:
        for rec in self.load()["instances"].values():
            if rec.get("quota_mode") in ("xfs", "zfs"):   # exact usage is free to read there
                continue
            r = self.exe.query(["du", "-s", "-B1", str(self.rec_dir(rec["id"])), str(self.inst_dir(rec["id"]) / "data")],
                               timeout=1800)
            if r.rc == 0:
                self.du_cache[rec["id"]] = round(sum(int(l.split()[0]) for l in r.out.splitlines() if l.strip()) / 1e9, 1)

    def view(self, rec: dict, states: dict[str, str] | None = None) -> dict:
        probe = self.probe_cache.get(rec["id"], {})
        state = (states or {}).get(rec["id"], "missing") if states is not None else ("planned" if self.exe.dry_run else "created")
        return {"id": rec["id"], "location_id": rec["location_id"], "name": rec["name"], "state": state,
                "cameras": probe.get("cameras"), "quota_gb": rec["quota_gb"], "used_gb": self.used_gb(rec),
                "gpu": rec.get("gpu"), "mode": rec["mode"], "subnets": rec.get("subnets", []),
                "public_ips": rec.get("public_ips", []), "hosts": rec.get("hosts", []),
                "host_ips": rec.get("host_ips", {}), "quota_mode": rec.get("quota_mode"),
                "enroll_pending": rec.get("enroll_pending", False), "mem_gb": rec["mem_gb"], "cpus": rec["cpus"],
                "image": rec["image"], "network": rec["net_subnet"]}

    def capacity(self) -> dict:
        reg = self.load()
        insts = list(reg["instances"].values())
        try:
            load = [round(x, 2) for x in os.getloadavg()]
        except (AttributeError, OSError):
            load = None
        try:
            ram = parse_meminfo(Path("/proc/meminfo").read_text())
        except OSError:
            ram = {"total": None, "free": None}
        gpus = self.gpus()
        for g in gpus:
            g["instances"] = sum(1 for r in insts if r.get("gpu") == g["index"])
        disks = []
        if self.root.exists():
            mode = self.quota_mode_auto()
            disk = self.zfs_disk() if mode == "zfs" else None
            if disk is None:
                du = shutil.disk_usage(self.root)
                _, fstype, _ = self.fs_info()
                disk = {"path": str(self.root), "total_gb": round(du.total / 1e9, 1), "free_gb": round(du.free / 1e9, 1),
                        "fs": fstype or None}
            disks.append({**disk, "project_quota": mode == "xfs", "quota_mode": mode})
        return {"cpus": os.cpu_count(), "load": load, "ram_gb": ram, "gpus": gpus, "disks": disks,
                "instances": len(insts),
                "allocated": {"quota_gb": sum(r["quota_gb"] for r in insts), "mem_gb": sum(r["mem_gb"] for r in insts),
                              "cpus": sum(r["cpus"] for r in insts)}}

    def zfs_disk(self) -> dict | None:
        """Disk entry for a ZFS host. statvfs (shutil.disk_usage) is wrong here: on a dataset it reports only that
        dataset's own data plus the free space (children not counted) and, under a quota, the quota. Total = the
        pool's root dataset used + available (usable space after raidz parity); free = `available` of the dataset
        mounted at <root>/recordings, i.e. what new instance datasets can still get from the pool."""
        ds = self.zfs_dataset_at(self.root / "recordings")
        if not ds:
            return None
        pool = ds.split("/")[0]
        p = self.zfs_get(list(dict.fromkeys([pool, ds])), ("used", "available"))
        used, avail, free = p.get(pool, {}).get("used"), p.get(pool, {}).get("available"), p.get(ds, {}).get("available")
        if used is None or avail is None or free is None:
            return None
        return {"path": str(self.root), "total_gb": round((used + avail) / 1e9, 1), "free_gb": round(free / 1e9, 1),
                "fs": "zfs", "dataset": ds}

    # -- upkeep (agent loop, `reconcile`, boot)
    def probe(self, rec: dict) -> dict | None:
        r = self.exe.query(["docker", "exec", self.cname(rec["id"]), "python", "-c", PROBE], timeout=30)
        if r.rc != 0:
            return None
        try:
            p = json.loads(r.out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return None
        self.probe_cache[rec["id"]] = {**p, "at": time.time()}
        return p

    def finish_enroll(self, rec: dict, probe: dict | None = None) -> bool:
        """Once the instance reports enrolled, drop the (now spent) token from instance.env. The running container
        keeps it in its environment until the next recreate; the server ignores it once enrolled."""
        p = probe or self.probe(rec)
        if not p or not p.get("enrolled"):
            return False
        with self.locked():
            reg = self.load()
            cur = reg["instances"].get(rec["id"])
            if not cur or not cur.get("enroll_pending"):
                return False
            env = self.env_path(rec["id"])
            if env.exists():
                self.exe.write(env, scrub_enroll_token(env.read_text(encoding="utf-8")), 0o600)
            cur["enroll_pending"] = False
            self.save(reg)
        log.info("%s enrolled (site %s); enrollment token removed from instance.env", rec["id"], p.get("site_id"))
        return True

    def ensure_vllm(self, reg: dict) -> list[str]:
        """Re-attach the vLLM container to every instance network (a compose recreate drops manual connects)."""
        r = self.exe.query(["docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}", self.cfg["vllm_container"]])
        if r.rc != 0:
            return []
        try:
            attached = set(json.loads(r.out or "{}"))
        except ValueError:
            return []
        fixed = []
        for rec in reg["instances"].values():
            if self.netname(rec["id"]) not in attached:
                if self.exe.run(self.vllm_connect_argv(rec), check=False).rc == 0:
                    fixed.append(rec["id"])
        return fixed

    def reconcile(self, *, resolve: bool = True) -> dict:
        """Boot and periodic repair: firewall, quotas, networks, vLLM attachment, missing containers."""
        with self.locked():
            reg = self.load()
            old = list(reg.get("hub_ips", []))
            self.apply_firewall(reg, resolve=resolve)
            self.save(reg)
            mount = None
            parents = None
            states = self.docker_states()
            nets = set(self.exe.query(["docker", "network", "ls", "--format", "{{.Name}}"]).out.split())
            created = []
            for rec in reg["instances"].values():
                if rec["quota_mode"] == "xfs":
                    mount = mount or self.fs_info()[0] or str(self.root)
                    for cmd in self.xfs_cmds(rec, mount, setup=False):
                        self.exe.run(cmd, check=False)
                if rec["quota_mode"] == "zfs":   # quotas persist in the pool; this only repairs hand edits
                    parents = parents or self.zfs_parents()
                    try:
                        for cmd in self.zfs_set_quota_cmds(rec, parents):
                            self.exe.run(cmd, check=False)
                    except OpError as e:
                        log.warning("%s: %s", rec["id"], e)
                if self.netname(rec["id"]) not in nets:
                    self.exe.run(self.network_argv(rec), check=False)
                if rec["id"] not in states:
                    self.exe.run(self.docker_run_argv(rec), check=False)
                    created.append(rec["id"])
            vllm = self.ensure_vllm(reg)
        return {"detail": "reconciled", "hub_ips": reg["hub_ips"], "hub_ips_changed": old != reg["hub_ips"],
                "containers_started": created, "vllm_attached": vllm}


# ---------------------------------------------------------------- agent mode

def ssl_context(url: str, insecure: bool = False) -> ssl.SSLContext | None:
    u = urllib.parse.urlsplit(url)
    if u.scheme == "ws":
        if u.hostname not in ("localhost", "127.0.0.1", "::1") and not insecure:
            raise SystemExit("refusing a plain ws:// hub other than localhost (use wss://, or --insecure for a lab)")
        return None
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        log.warning("certifi not installed: using the system CA store")
        ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class Agent:
    HEARTBEAT_S = 30
    MAINT_S = 60
    HUB_REFRESH_S = 600
    PROBE_S = 300
    DU_S = 1800

    def __init__(self, host: Host, hub_url: str, token: str, insecure: bool = False) -> None:
        self.host, self.hub_url, self.token, self.insecure = host, hub_url, token, insecure
        self.ws: Any = None
        self.outbox: collections.deque = collections.deque(maxlen=200)   # results that could not be sent yet
        self._send_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    def hello(self) -> dict:
        return {"t": "hello", "proto": PROTO, "hostname": socket.gethostname(), "version": VERSION,
                "capacity": self.host.capacity()}

    def heartbeat(self) -> dict:
        return {"t": "heartbeat", "capacity": self.host.capacity(), "instances": self.host.list_instances()["instances"]}

    async def send(self, frame: dict) -> None:
        ws = self.ws
        if ws is None:
            if frame.get("t") == "result":
                self.outbox.append(frame)
            return
        try:
            async with self._send_lock:
                await ws.send(json.dumps(frame, separators=(",", ":")))
        except Exception as e:
            log.info("send failed (%s); %s", e, "queued" if frame.get("t") == "result" else "dropped")
            if frame.get("t") == "result":
                self.outbox.append(frame)

    async def execute(self, frame: dict) -> dict:
        cid, op, args = frame.get("id"), frame.get("op"), frame.get("args")
        args = {} if args is None else args
        if not isinstance(cid, int) or isinstance(cid, bool):
            return {"t": "result", "id": cid, "ok": False, "detail": "cmd needs an integer id"}
        log.info("cmd %s: %s %s", cid, op, args.get("id", "") if isinstance(args, dict) else "")
        try:
            res = await asyncio.to_thread(self.host.dispatch, str(op), args)
            return {"t": "result", "id": cid, "ok": True, **res}
        except (OpError, CmdError) as e:
            log.warning("cmd %s (%s) failed: %s", cid, op, e)
            return {"t": "result", "id": cid, "ok": False, "detail": str(e)}
        except Exception as e:
            log.exception("cmd %s (%s) crashed", cid, op)
            return {"t": "result", "id": cid, "ok": False, "detail": f"internal error: {type(e).__name__}: {e}"}

    async def handle(self, raw: str | bytes) -> None:
        try:
            frame = json.loads(raw)
        except ValueError:
            log.warning("hub sent a non-JSON frame")
            return
        if not isinstance(frame, dict):
            return
        t = frame.get("t")
        if t == "cmd":
            task = asyncio.create_task(self._run_cmd(frame))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif t == "ping":
            await self.send({"t": "pong"})
        elif t == "error":
            log.warning("hub: %s", frame.get("detail") or frame)
        else:
            log.debug("hub frame %s ignored", t)

    async def _run_cmd(self, frame: dict) -> None:
        await self.send(await self.execute(frame))

    async def serve(self, ws: Any) -> None:
        """One connection: hello, queued results, then commands until the socket closes."""
        self.ws = ws
        try:
            await ws.send(json.dumps(await asyncio.to_thread(self.hello)))
            while self.outbox:
                await self.send(self.outbox.popleft())
            beat = asyncio.create_task(self._heartbeats())
            try:
                async for raw in ws:
                    await self.handle(raw)
            finally:
                beat.cancel()
        finally:
            self.ws = None

    async def _heartbeats(self) -> None:
        while True:
            await asyncio.sleep(self.HEARTBEAT_S)
            try:
                await self.send(await asyncio.to_thread(self.heartbeat))
            except Exception:
                log.exception("heartbeat failed")

    async def maintenance(self) -> None:
        last_hub = last_du = 0.0
        while True:
            try:
                now = time.time()
                reg = self.host.load()
                await asyncio.to_thread(self.host.ensure_vllm, reg)
                for rec in reg["instances"].values():
                    cached = self.host.probe_cache.get(rec["id"], {})
                    if rec.get("enroll_pending") or now - cached.get("at", 0) > self.PROBE_S:
                        p = await asyncio.to_thread(self.host.probe, rec)
                        if p and rec.get("enroll_pending"):
                            await asyncio.to_thread(self.host.finish_enroll, rec, p)
                if now - last_hub > self.HUB_REFRESH_S:   # hub and camera DNS names (dynamic DNS)
                    last_hub = now
                    why = await asyncio.to_thread(self.host.dns_changed, reg)
                    if why:
                        log.info("%s: re-applying the firewall", why)
                        await asyncio.to_thread(self.host.reconcile)
                if now - last_du > self.DU_S:
                    last_du = now
                    await asyncio.to_thread(self.host.refresh_du)
            except Exception:
                log.exception("maintenance pass failed")
            await asyncio.sleep(self.MAINT_S)

    async def run_forever(self) -> None:
        try:
            from websockets.asyncio.client import connect
            from websockets.exceptions import InvalidStatus
        except ImportError:
            raise SystemExit("agent mode needs websockets>=13 (pip install websockets certifi)")
        ctx = ssl_context(self.hub_url, self.insecure)
        maint = asyncio.create_task(self.maintenance())
        backoff = 1.0
        try:
            while True:
                started = time.monotonic()
                try:
                    async with connect(self.hub_url, additional_headers={"Authorization": f"Bearer {self.token}"},
                                       ssl=ctx, open_timeout=20, ping_interval=20, ping_timeout=40, close_timeout=5,
                                       max_size=2 ** 20, user_agent_header=f"axiom-host/{VERSION}") as ws:
                        log.info("connected to %s", self.hub_url)
                        await self.serve(ws)
                    log.info("hub closed the connection")
                except InvalidStatus as e:
                    code = e.response.status_code
                    if code in (401, 403):
                        log.error("hub refused the host token (HTTP %s); retrying in 5 min", code)
                        await asyncio.sleep(300)
                        continue
                    log.warning("hub answered HTTP %s", code)
                except (OSError, asyncio.TimeoutError) as e:
                    log.warning("hub unreachable: %s", e)
                except Exception as e:
                    log.warning("connection lost: %s: %s", type(e).__name__, e)
                if time.monotonic() - started > 60:
                    backoff = 1.0
                delay = random.uniform(0, backoff)   # full jitter
                backoff = min(backoff * 2, 60.0)
                await asyncio.sleep(max(delay, 0.5))
        finally:
            maint.cancel()


# ---------------------------------------------------------------- CLI

def load_config(path: str | None, root: str | None) -> dict:
    cfg: dict = {}
    p = Path(path or "/etc/axiom/host.json")
    if p.exists():
        cfg = json.loads(p.read_text(encoding="utf-8"))
    elif path:
        raise SystemExit(f"config {p} not found")
    if root:
        cfg["root"] = root
    return cfg


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False) if not isinstance(obj, str) else obj, end="" if isinstance(obj, str) else "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="axiom_host.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", help="host config JSON (default /etc/axiom/host.json if present)")
    ap.add_argument("--root", help="instance root (default /srv/axiom)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create-instance", help="create and start one Site's instance")
    c.add_argument("--id", required=True)
    c.add_argument("--location", required=True, help="hub location_id of the Site")
    c.add_argument("--name", required=True, help='display name, e.g. "Acme Gate · Central"')
    c.add_argument("--mode", choices=MODES, default="vpn", help="how the site connects (Peplink sheet); the cameras "
                   "allowed are the union of --subnet, --public-ip and --host whatever the mode")
    c.add_argument("--subnet", action="append", help="camera LAN / VPN subnet, any protocol (repeat or comma-separate)")
    c.add_argument("--public-ip", action="append", help="a site router's public IP (port forwards, TCP)")
    c.add_argument("--host", action="append", help="a site router's dynamic DNS name (port forwards, TCP)")
    c.add_argument("--quota-gb", type=float, required=True)
    c.add_argument("--gpu", default="none", help="nvidia-smi index for YOLO, or 'none' for CPU")
    c.add_argument("--enroll-token", help="one-time hub enrollment token (visible in ps; prefer --enroll-token-file)")
    c.add_argument("--enroll-token-file")
    c.add_argument("--hub-url")
    c.add_argument("--vlm-url")
    c.add_argument("--vlm-model")
    c.add_argument("--mem-gb", type=float)
    c.add_argument("--cpus", type=float)
    c.add_argument("--image")
    c.add_argument("--quota-mode", choices=QUOTA_MODES, default="auto")
    c.add_argument("--dry-run", action="store_true", help="print the plan (commands, env, firewall); change nothing")

    d = sub.add_parser("delete-instance", help="stop and remove an instance; say what happens to its footage")
    d.add_argument("--id", required=True)
    dk = d.add_mutually_exclusive_group(required=True)
    dk.add_argument("--keep-data", action="store_true", help="leave recordings and database on disk")
    dk.add_argument("--purge", action="store_true", help="delete recordings and database (cannot be undone)")
    d.add_argument("--dry-run", action="store_true")

    q = sub.add_parser("set-quota")
    q.add_argument("--id", required=True)
    q.add_argument("--quota-gb", type=float, required=True)
    q.add_argument("--force", action="store_true", help="allow a quota below current usage")
    q.add_argument("--dry-run", action="store_true")

    cn = sub.add_parser("set-camera-network", help="replace an instance's camera allow-list; lists not named stay as they are")
    cn.add_argument("--id", required=True)
    cn.add_argument("--subnet", action="append", help="LAN / VPN subnet, any protocol (repeat or comma-separate)")
    cn.add_argument("--public-ip", action="append", help="public IP behind port forwards, TCP (repeatable)")
    cn.add_argument("--host", action="append", help="dynamic DNS name behind port forwards, TCP (repeatable)")
    cn.add_argument("--clear", action="append", choices=("subnets", "public-ips", "hosts", "all"),
                    help="empty that list (given values for it still apply)")
    cn.add_argument("--dry-run", action="store_true", help="print the new ruleset; change nothing")

    r = sub.add_parser("restart-instance")
    r.add_argument("--id", required=True)
    r.add_argument("--recreate", action="store_true", help="remove and re-run the container (new image/env)")
    r.add_argument("--image")
    r.add_argument("--dry-run", action="store_true")

    sub.add_parser("list")
    sub.add_parser("capacity")
    f = sub.add_parser("render-firewall", help="print the nftables ruleset for the current registry")
    f.add_argument("--hub-ip", action="append", help="use these hub addresses instead of resolving")
    f.add_argument("--offline", action="store_true", help="use the last resolved hub addresses")
    af = sub.add_parser("apply-firewall", help="render and load the ruleset (systemd: before docker.service)")
    af.add_argument("--offline", action="store_true", help="use the last resolved hub addresses (early boot)")
    sub.add_parser("reconcile", help="re-apply firewall, quotas, networks, vLLM links; start missing containers")
    fe = sub.add_parser("finish-enroll", help="remove the spent enrollment token once the instance is enrolled")
    fe.add_argument("--id", required=True)

    a = sub.add_parser("run", help="agent mode: dial the hub and execute its commands")
    a.add_argument("--hub", default="wss://hub.axiomvision.ai/host-agent")
    a.add_argument("--token-file", default="/etc/axiom/host-token")
    a.add_argument("--insecure", action="store_true", help="lab only: skip TLS verification / allow ws://")

    ns = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if ns.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    cfg = load_config(ns.config, ns.root)
    host = Host(cfg, Exec(dry_run=getattr(ns, "dry_run", False)))

    try:
        if ns.cmd == "create-instance":
            token = ns.enroll_token
            if ns.enroll_token_file:
                token = Path(ns.enroll_token_file).read_text(encoding="utf-8").strip()
            args = {"id": ns.id, "location": ns.location, "name": ns.name, "mode": ns.mode,
                    "subnet": [s for v in ns.subnet or [] for s in v.split(",")],
                    "public_ip": [s for v in ns.public_ip or [] for s in v.split(",")],
                    "hosts": [s for v in ns.host or [] for s in v.split(",")],
                    "quota_gb": ns.quota_gb, "gpu": ns.gpu, "enroll_token": token, "hub_url": ns.hub_url,
                    "vlm_url": ns.vlm_url, "vlm_model": ns.vlm_model, "mem_gb": ns.mem_gb, "cpus": ns.cpus,
                    "image": ns.image, "quota_mode": ns.quota_mode}
            _print(host.create_instance(args))
        elif ns.cmd == "delete-instance":
            _print(host.delete_instance({"id": ns.id, "keep_data": not ns.purge}) | _plan(host))
        elif ns.cmd == "set-quota":
            _print(host.set_quota({"id": ns.id, "quota_gb": ns.quota_gb, "force": ns.force}) | _plan(host))
        elif ns.cmd == "set-camera-network":
            out = host.set_camera_network(camera_args(ns))
            fw = out.pop("dry_run", {}).get("firewall")
            _print(out)
            if fw:
                _print("\n# dry run: nothing changed. The ruleset this would load:\n" + fw)
        elif ns.cmd == "restart-instance":
            _print(host.restart_instance({"id": ns.id, "recreate": ns.recreate, "image": ns.image}) | _plan(host))
        elif ns.cmd == "list":
            _print(host.list_instances()["instances"])
        elif ns.cmd == "capacity":
            _print(host.capacity())
        elif ns.cmd == "render-firewall":
            reg = host.load()
            ips = ns.hub_ip if ns.hub_ip else (reg["hub_ips"] if ns.offline else host.resolve_hub(reg))
            _print(host.firewall_text(reg, ips))
        elif ns.cmd == "apply-firewall":
            with host.locked():
                reg = host.load()
                host.apply_firewall(reg, resolve=not ns.offline)
                host.save(reg)
            _print({"detail": "firewall applied", "hub_ips": reg["hub_ips"]})
        elif ns.cmd == "reconcile":
            _print(host.reconcile())
        elif ns.cmd == "finish-enroll":
            rec = host._get(host.load(), {"id": ns.id})
            done = host.finish_enroll(rec)
            _print({"detail": "enrollment finished, token removed" if done else "not enrolled yet (or already finished)"})
        elif ns.cmd == "run":
            token = Path(ns.token_file).read_text(encoding="utf-8").strip()
            if not token:
                raise SystemExit(f"{ns.token_file} is empty")
            asyncio.run(Agent(host, ns.hub, token, ns.insecure).run_forever())
    except (OpError, CmdError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def camera_args(ns: argparse.Namespace) -> dict:
    """set-camera-network flags -> op args: a list given replaces it, --clear empties it, neither keeps it (None)."""
    clear = set(ns.clear or [])
    args: dict[str, Any] = {"id": ns.id}
    for key, flag, given in (("subnets", "subnets", ns.subnet), ("public_ips", "public-ips", ns.public_ip),
                             ("hosts", "hosts", ns.host)):
        vals = [s.strip() for v in given or [] for s in v.split(",") if s.strip()]
        args[key] = vals if vals or flag in clear or "all" in clear else None
    return args


def _plan(host: Host) -> dict:
    return {"dry_run": {"steps": list(host.exe.plan)}} if host.exe.dry_run else {}


if __name__ == "__main__":
    sys.exit(main())
