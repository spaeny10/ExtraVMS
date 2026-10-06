#!/usr/bin/env python3
"""axiom-host: provisions and reports Axiom Vision central recording instances on one Docker host.

One instance = one customer Site's recording server (the ordinary site server in the `axiom/instance` image),
in its own container, Docker network, Unix uid, XFS project quota and nftables egress allow-list. The hub
drives this agent over a WebSocket (see PROTOCOL.md); the same operations work from the command line.

    axiom_host.py create-instance --id acme-gate --location loc_123 --name "Acme Gate · Central" \
        --mode vpn --subnet 10.20.7.0/24 --quota-gb 4000 --gpu 1 --enroll-token-file /root/t [--dry-run]
    axiom_host.py delete-instance --id acme-gate --keep-data | --purge
    axiom_host.py set-quota --id acme-gate --quota-gb 6000
    axiom_host.py restart-instance --id acme-gate [--recreate] [--image axiom/instance:2026.10.06]
    axiom_host.py list | capacity | render-firewall | apply-firewall | reconcile | finish-enroll --id ...
    axiom_host.py run --hub wss://hub.axiomvision.ai/host-agent --token-file /etc/axiom/host-token

Python 3.12, standard library plus `websockets` (agent mode only) and `certifi` (optional, for the CA bundle).
Docker, nft and xfs_quota are driven through their command-line tools. Run as root (systemd unit in systemd/).
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
MODES = ("vpn", "forward")

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
    "forward_tcp_ports": [],                    # forward mode: allowed ports on the site's public IP ([] = any TCP)
    "mem_gb": 8,
    "cpus": 4,
    "uid_base": 20000,                          # instance uid = uid_base + slot
    "project_base": 70000,                      # XFS project id = project_base + slot
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
              "NVR_BACKUP_DIR=/data/backups", "NVR_MEDIAMTX_EXE=/opt/nvr/bin/mediamtx/mediamtx"]
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
        if rec["mode"] == "vpn":
            L.append(f"\t\tip daddr {_nft_set(rec['subnets'])} accept comment \"site camera LAN via FusionHub\"")
        else:
            ports = f" tcp dport {_nft_set(forward_tcp_ports)}" if forward_tcp_ports else " meta l4proto tcp"
            L.append(f"\t\tip daddr {_nft_set(rec['public_ips'])}{ports} accept comment \"site public IP (port forwards)\"")
        L += ["\t\tcounter drop comment \"everything else, incl. other instances and other sites\"", "\t}"]
    L.append("}")
    return "\n".join(L) + "\n"


def _chain(rec: dict) -> str:
    return "inst_" + rec["id"].replace("-", "_")


def _ip_list(v: Any) -> list[str]:
    if v is None or v == "":
        return []
    items = v if isinstance(v, list) else str(v).split(",")
    return [str(i).strip() for i in items if str(i).strip()]


# ---------------------------------------------------------------- the host

class Host:
    def __init__(self, cfg: dict | None = None, exe: Exec | None = None) -> None:
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.exe = exe or Exec()
        self.root = Path(self.cfg["root"])
        self.pool = ipaddress.ip_network(self.cfg["pool"])
        self._lock = threading.RLock()
        self._depth = 0
        self._inst_locks: dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
        self.probe_cache: dict[str, dict] = {}       # id -> {"at", "cameras", "enrolled"}
        self.du_cache: dict[str, float] = {}         # id -> used GB (non-XFS hosts)

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

    def quota_mode_auto(self) -> str:
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
                for *_, sa in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM):
                    ips.add(sa[0])
            except OSError as e:
                log.warning("could not resolve %s (%s); keeping the last known addresses", host, e)
        return sorted(ips, key=ipaddress.ip_address) or list(reg.get("hub_ips", []))

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
        subnets, public_ips = [], []
        if mode == "vpn":
            for s in _ip_list(a.get("subnet")):
                try:
                    n = ipaddress.ip_network(s, strict=True)
                except ValueError as e:
                    raise OpError(f"subnet {s}: {e}") from None
                if n.version != 4 or not n.is_private or n.prefixlen < 16:
                    raise OpError(f"subnet {s}: must be a private IPv4 network no larger than a /16")
                if n.overlaps(self.pool):
                    raise OpError(f"subnet {s} overlaps the instance pool {self.pool}")
                subnets.append(str(n))
            if not subnets:
                raise OpError("vpn mode needs --subnet (the site's camera LAN, e.g. 10.20.7.0/24)")
            for other in reg["instances"].values():
                for s in subnets:
                    if any(ipaddress.ip_network(s).overlaps(ipaddress.ip_network(o)) for o in other.get("subnets", [])):
                        raise OpError(f"subnet {s} overlaps {other['id']}'s site subnet: every site needs its own")
        else:
            for p in _ip_list(a.get("public_ip")):
                try:
                    ip = ipaddress.ip_address(p)
                except ValueError as e:
                    raise OpError(f"public_ip {p}: {e}") from None
                if ip.version != 4 or ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_link_local \
                        or ip in self.pool:
                    raise OpError(f"public_ip {p}: not a usable site address")
                public_ips.append(str(ip))
            if not public_ips:
                raise OpError("forward mode needs --public-ip (the site router's public address)")
            for other in reg["instances"].values():
                if set(public_ips) & set(other.get("public_ips", [])):
                    raise OpError(f"public IP already used by {other['id']}")
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
        if qm not in ("auto", "xfs", "none"):
            raise OpError("quota_mode must be auto, xfs or none")
        ai = self.ai_env()
        for p in (self.inst_dir(iid), self.rec_dir(iid)):
            if p.exists() and any(p.iterdir()):
                raise OpError(f"{p} holds data from an earlier instance {iid}; remove it or choose another id")
        return {"id": iid, "location_id": loc, "name": name, "mode": mode, "subnets": subnets, "public_ips": public_ips,
                "quota_gb": quota, "gpu": gpu, "mem_gb": mem_gb, "cpus": cpus,
                "quota_mode": self.quota_mode_auto() if qm == "auto" else qm,
                "image": a.get("image") or self.cfg["image"], "hub_url": hub_url,
                "vlm_url": a.get("vlm_url") or self.cfg["vlm_url"],
                "vlm_model": a.get("vlm_model") or self.cfg["vlm_model"] or ai.get("AXIOM_VLM_MODEL", ""),
                "_token": token, "_vlm_key": a.get("vlm_key") or ai.get("VLLM_API_KEY", "")}

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
        if resolve:
            reg["hub_ips"] = self.resolve_hub(reg)
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
                warnings.append("no XFS project quota on this filesystem: quota_gb is NOT enforced (see README)")
            undo: list[Callable[[], None]] = []
            try:
                self.exe.mkdir(self.inst_dir(iid), 0o711)
                undo.append(lambda: self.exe.rmtree(self.inst_dir(iid)))
                self.exe.mkdir(self.inst_dir(iid) / "data", 0o700, rec["uid"])
                self.exe.mkdir(self.rec_dir(iid), 0o700, rec["uid"])
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
        with self.locked():
            reg = self.load()
            rec = self._get(reg, a)
            iid = rec["id"]
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
                    for p in (self.inst_dir(iid), self.rec_dir(iid)):
                        if p.resolve().parent.parent != self.root.resolve():
                            raise OpError(f"refusing to remove {p}")
                        self.exe.rmtree(p)
                else:
                    env = self.env_path(iid)
                    if env.exists() and not self.exe.dry_run:
                        self.exe.write(env, scrub_enroll_token(env.read_text(encoding="utf-8")), 0o600)
        self.probe_cache.pop(iid, None)
        kept = f"; data kept in {self.inst_dir(iid)} and {self.rec_dir(iid)}" if keep else "; data removed"
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
            used = self.used_gb(rec)
            if used is not None and quota < used and not a.get("force"):
                raise OpError(f"{rec['id']} already uses {used:.0f} GB; a {quota:.0f} GB quota would make it delete "
                              "footage at once (pass force to do it anyway)")
            rec["quota_gb"] = quota
            if rec["quota_mode"] == "xfs":
                mount = self.fs_info()[0] or str(self.root)
                for cmd in self.xfs_cmds(rec, mount, setup=False):
                    self.exe.run(cmd)
            self.save(reg)
        note = "" if rec["quota_mode"] == "xfs" else " (not enforced: no XFS project quota on this host)"
        return {"detail": f"{rec['id']} quota {quota:g} GB{note}", "instance": self.view(rec)}

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
               "set_quota": self.set_quota, "restart_instance": self.restart_instance, "list": self.list_instances}
        if op not in ops:
            raise OpError(f"unknown op {op!r}")
        if not isinstance(args, dict):
            raise OpError("args must be an object")
        if args.get("dry_run") and op != "list":
            return Host(self.cfg, Exec(dry_run=True)).dispatch(op, {k: v for k, v in args.items() if k != "dry_run"})
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

    def used_gb(self, rec: dict) -> float | None:
        if rec.get("quota_mode") == "xfs" and hasattr(os, "statvfs"):
            try:   # on a project-quota directory XFS reports the project's own usage and limit
                st = os.statvfs(self.rec_dir(rec["id"]))
                return round((st.f_blocks - st.f_bfree) * st.f_frsize / 1e9, 1)
            except OSError:
                return None
        return self.du_cache.get(rec["id"])

    def refresh_du(self) -> None:
        for rec in self.load()["instances"].values():
            if rec.get("quota_mode") == "xfs":
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
                "public_ips": rec.get("public_ips", []), "quota_mode": rec.get("quota_mode"),
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
            du = shutil.disk_usage(self.root)
            _, fstype, _ = self.fs_info()
            disks.append({"path": str(self.root), "total_gb": round(du.total / 1e9, 1), "free_gb": round(du.free / 1e9, 1),
                          "fs": fstype or None, "project_quota": self.quota_mode_auto() == "xfs"})
        return {"cpus": os.cpu_count(), "load": load, "ram_gb": ram, "gpus": gpus, "disks": disks,
                "instances": len(insts),
                "allocated": {"quota_gb": sum(r["quota_gb"] for r in insts), "mem_gb": sum(r["mem_gb"] for r in insts),
                              "cpus": sum(r["cpus"] for r in insts)}}

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
            states = self.docker_states()
            nets = set(self.exe.query(["docker", "network", "ls", "--format", "{{.Name}}"]).out.split())
            created = []
            for rec in reg["instances"].values():
                if rec["quota_mode"] == "xfs":
                    mount = mount or self.fs_info()[0] or str(self.root)
                    for cmd in self.xfs_cmds(rec, mount, setup=False):
                        self.exe.run(cmd, check=False)
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
                if now - last_hub > self.HUB_REFRESH_S:
                    last_hub = now
                    ips = await asyncio.to_thread(self.host.resolve_hub, reg)
                    if ips != reg.get("hub_ips"):
                        log.info("hub addresses changed %s -> %s: re-applying the firewall", reg.get("hub_ips"), ips)
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
    c.add_argument("--mode", choices=MODES, default="vpn")
    c.add_argument("--subnet", action="append", help="vpn mode: the site's camera LAN (repeat or comma-separate)")
    c.add_argument("--public-ip", action="append", help="forward mode: the site router's public IP")
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
    c.add_argument("--quota-mode", choices=("auto", "xfs", "none"), default="auto")
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
                    "quota_gb": ns.quota_gb, "gpu": ns.gpu, "enroll_token": token, "hub_url": ns.hub_url,
                    "vlm_url": ns.vlm_url, "vlm_model": ns.vlm_model, "mem_gb": ns.mem_gb, "cpus": ns.cpus,
                    "image": ns.image, "quota_mode": ns.quota_mode}
            _print(host.create_instance(args))
        elif ns.cmd == "delete-instance":
            _print(host.delete_instance({"id": ns.id, "keep_data": not ns.purge}) | _plan(host))
        elif ns.cmd == "set-quota":
            _print(host.set_quota({"id": ns.id, "quota_gb": ns.quota_gb, "force": ns.force}) | _plan(host))
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


def _plan(host: Host) -> dict:
    return {"dry_run": {"steps": list(host.exe.plan)}} if host.exe.dry_run else {}


if __name__ == "__main__":
    sys.exit(main())
