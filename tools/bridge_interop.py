#!/usr/bin/env python3
"""Axiom Bridge interop check on this desktop: the real Go bridge, the real hub code and the real host-agent Relay
against each other (each was unit-tested only against fakes of the others). Not part of test.ps1.

    .venv\\Scripts\\python.exe tools\\bridge_interop.py [--camera 192.168.105.12] [--keep]

What runs, all on 127.0.0.1 and in temp directories (nothing installed, no production hub, DB or host):
  hub        hub.api.app under uvicorn on a free port, a throwaway SQLite database (as hub/tests/conftest.py)
  host       tools/central/axiom_host.py: Host (commands that need root, nft or Docker are faked), Agent on /host-agent
             (so the hub's set_relay reaches the real set_relay), and Relay on /host-relay with the instance's gateway
             and address injected as 127.0.0.1
  bridge     bridge/: `go build` of cmd/axiom-bridge (or bridge/dist/axiom-bridge.exe), `run --dir <temp> --code
             <code> --hub ws://127.0.0.1:<port>/bridge` in the foreground (not the Windows service)
  camera     a real camera on this LAN, picked from the desktop NVR's camera list (one read-only GET of
             http://127.0.0.1:8080/api/cameras). Only read-only requests reach it: RTSP OPTIONS and ONVIF
             GetSystemDateAndTime, never authenticated.

Prints PASS / FAIL per step and exits 1 if any failed. Everything is stopped and removed at the end (--keep leaves the
temp directory for reading the logs).
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{f'  ({detail})' if detail else ''}", flush=True)
    return ok


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def local_addresses() -> set[str]:
    out = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            out.add(info[4][0])
    except OSError:
        pass
    return out


def pick_camera(wanted: str | None) -> dict:
    """A camera of the desktop NVR (read-only GET): enabled, at a private address that is not this PC's."""
    with urllib.request.urlopen("http://127.0.0.1:8080/api/cameras", timeout=10) as r:   # noqa: S310 (localhost)
        cams = json.load(r)
    own = local_addresses()
    for c in cams:
        host = str(c.get("host") or "")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            continue
        if wanted and host != wanted:
            continue
        if ip.version == 4 and ip.is_private and host not in own and c.get("enabled") in (1, True):
            return {"id": c["id"], "name": c.get("name") or c["id"], "host": host}
    raise SystemExit(f"no usable camera in the desktop NVR's list{f' at {wanted}' if wanted else ''}")


# ---------------------------------------------------------------- the pieces

def setup_env(tmp: Path, port: int) -> None:
    """The hub's settings, exactly like hub/tests/conftest.py (read when hub.config is imported)."""
    os.environ.update({
        "HUB_DATABASE_URL": f"sqlite:///{(tmp / 'hub.db').as_posix()}", "HUB_SECRET": "interop-secret", "HUB_COOKIE_SECURE": "0",
        "HUB_PUBLIC_URL": f"http://127.0.0.1:{port}", "HUB_SOC_LOOPS": "0", "HUB_GEOCODE_BACKFILL": "0",
        "HUB_LAN_ACME_DIRECTORY": "http://127.0.0.1:9/no-acme", "HUB_LAN_CERTS": "0", "HUB_COVERAGEMAP_KEY": "",
        "NVR_DATA_DIR": str(tmp / "site"), "NVR_RECORDINGS_DIR": str(tmp / "site-rec"), "NVR_RUNTIME_DIR": str(tmp / "site-rt"),
    })
    for p in (ROOT / "hub", ROOT / "tunnelproto", ROOT / "tools" / "central"):
        sys.path.insert(0, str(p))


def start_hub(port: int):
    import uvicorn
    from hub.api import app
    loop = asyncio.new_event_loop()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=lambda: loop.run_until_complete(server.serve()), daemon=True)
    t.start()
    end = time.time() + 20
    while not server.started:
        if time.time() > end:
            raise RuntimeError("the hub did not start")
        time.sleep(0.05)
    return server, t


def build_bridge(tmp: Path) -> Path:
    exe = tmp / "axiom-bridge.exe"
    if shutil.which("go"):
        r = subprocess.run(["go", "build", "-o", str(exe), "./cmd/axiom-bridge"], cwd=ROOT / "bridge", capture_output=True, text=True)
        if r.returncode == 0:
            return exe
        print(r.stderr)
    dist = ROOT / "bridge" / "dist" / "axiom-bridge.exe"
    if not dist.exists():
        subprocess.run(["powershell", "-NoProfile", "-File", str(ROOT / "bridge" / "build.ps1")], cwd=ROOT / "bridge", check=True)
    return dist


class Bridge:
    """The bridge process in the foreground, its output in a log file."""

    def __init__(self, exe: Path, home: Path, hub: str) -> None:
        self.exe, self.home, self.hub = exe, home, hub
        self.proc: subprocess.Popen | None = None
        self.log = home.parent / "bridge-stdout.log"

    def start(self, code: str | None = None) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        args = [str(self.exe), "run", "--dir", str(self.home), "--hub", self.hub, "--log-level", "debug"]
        if code:
            args += ["--code", code]
        f = open(self.log, "ab")
        self.proc = subprocess.Popen(args, stdout=f, stderr=subprocess.STDOUT, cwd=str(self.home),
                                     creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def tail(self, n: int = 15) -> str:
        try:
            return "\n".join(self.log.read_text(errors="replace").splitlines()[-n:])
        except OSError:
            return ""


async def until(pred, timeout: float, step_s: float = 0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        await asyncio.sleep(step_s)
    return None


async def rtsp_options(port: int, cam: str) -> str:
    """RTSP OPTIONS through the relay listener: the status line of the answer ("" when none came)."""
    r, w = await asyncio.open_connection("127.0.0.1", port)
    try:
        w.write(f"OPTIONS rtsp://{cam}:554/ RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: axiom-bridge-interop\r\n\r\n".encode())
        await w.drain()
        head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 15)
        return head.split(b"\r\n", 1)[0].decode(errors="replace")
    finally:
        w.close()


SOAP = ('<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
        '<GetSystemDateAndTime xmlns="http://www.onvif.org/ver10/device/wsdl"/></s:Body></s:Envelope>')


async def onvif_time(port: int, cam: str) -> bytes:
    """ONVIF GetSystemDateAndTime (no authentication: the one request ONVIF answers anonymously); the whole reply, read
    until the camera closes (Connection: close: its FIN comes back through the bridge and the hub as `end`)."""
    body = SOAP.encode()
    r, w = await asyncio.open_connection("127.0.0.1", port)
    try:
        w.write((f"POST /onvif/device_service HTTP/1.1\r\nHost: {cam}\r\nContent-Type: application/soap+xml; charset=utf-8\r\n"
                 f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
        await w.drain()
        return await asyncio.wait_for(r.read(), 20)
    finally:
        w.close()


async def refused(port: int) -> str:
    """A connection to the relay listener that must not get through: how it ended."""
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
    except OSError as e:
        return f"connect refused ({type(e).__name__})"
    try:
        w.write(b"OPTIONS rtsp://x/ RTSP/1.0\r\nCSeq: 1\r\n\r\n")
        await w.drain()
        data = await asyncio.wait_for(r.read(), 20)
        return "closed" if not data else f"GOT DATA {data[:40]!r}"
    except (ConnectionError, OSError) as e:
        return f"reset ({type(e).__name__})"
    finally:
        w.close()


# ---------------------------------------------------------------- the run

async def run(args, tmp: Path, port: int, cam: dict) -> None:
    import httpx
    import sqlalchemy as sa
    from websockets.asyncio.client import connect

    import axiom_host as ah
    from hub import auth, bridges, db, hosts

    base = f"http://127.0.0.1:{port}"
    ws_base = f"ws://127.0.0.1:{port}"
    # ---- hub: a hub administrator, a customer and Site, a host, and the Site's bridge-mode central instance
    auth.create_user("interop@example.com", "interop password 1", is_super=True)
    hub_http = httpx.Client(base_url=base, timeout=60)

    async def api(method: str, path: str, **kw):
        """The hub's API off this loop (the host agent and the Relay run on it and must keep answering)."""
        return await asyncio.to_thread(hub_http.request, method, path, **kw)
    assert hub_http.post("/auth/login", json={"email": "interop@example.com", "password": "interop password 1"}).status_code == 200
    org = hub_http.post("/api/orgs", json={"name": "Interop Co", "slug": "interop-co"}).json()
    loc = hub_http.post(f"/api/orgs/{org['id']}/locations", json={"name": "Interop Farm"}).json()
    host_row, host_token = hosts.create_host("Interop host", None, None)
    ci_id = db.new_id("ci_")
    t = time.time()
    db.insert(db.central_instances, {
        "id": ci_id, "host_id": host_row["id"], "location_id": loc["id"], "org_id": org["id"], "server_id": None, "name": "Interop Central",
        "mode": "bridge", "subnet": None, "public_ip": None, "site_number": None, "quota_gb": 100, "gpu": None, "state": "running",
        "last_error": None, "created_at": t, "created_by": None, "updated_at": t, "ready_at": t, "info": {"gateway": "127.0.0.1"},
        "info_at": t, "camera_limit": None,
        "camera_network": {"subnets": [], "public_ips": [], "hosts": [], "auto": {"public_ips": [], "hosts": []},
                           "applied": {"subnets": [], "public_ips": [], "hosts": []}}})
    step("hub up with a temp database; customer, Site, host and bridge-mode instance seeded", True, f"{base}, instance {ci_id}")

    # ---- host: the real Host / Agent / Relay; only root, nft and Docker are faked
    class LocalExec(ah.Exec):
        def _run(self, argv, input, timeout):   # nft -f, docker, xfs_quota...: recorded (Exec.plan), never run
            return ah.Result(0, "", "")

    host = ah.Host({"root": str(tmp / "host-root"), "hub_ips": ["127.0.0.1"], "ai_env": str(tmp / "no-ai.env")}, LocalExec())
    host.create_instance({"id": ci_id, "location": loc["id"], "name": "Interop Central", "mode": "bridge", "quota_gb": 100,
                          "gpu": "none", "quota_mode": "none", "hub_url": ws_base + "/agent"})
    agent = ah.Agent(host, ws_base + "/host-agent", host_token)
    relay = ah.Relay(host, ws_base + "/host-agent", host_token, addr_of=lambda rec: ("127.0.0.1", "127.0.0.1"))
    kills: list[str] = []
    real_kill = relay._kill

    async def kill(st, reason, tell_hub):
        if not st.closed:
            kills.append(reason)
        await real_kill(st, reason, tell_hub)
    relay._kill = kill
    loop = asyncio.get_running_loop()
    host.on_relay_change = lambda: loop.call_soon_threadsafe(relay.schedule_reload)

    async def agent_socket():
        async with connect(ws_base + "/host-agent", additional_headers={"Authorization": f"Bearer {host_token}"}) as ws:
            await agent.serve(ws)
    tasks = [asyncio.create_task(agent_socket()), asyncio.create_task(relay.run_forever())]
    ok = await until(lambda: hosts.registry.online(host_row["id"]) and host_row["id"] in bridges.registry.relays and host.relay_connected, 20)
    step("host agent on /host-agent and its Relay on /host-relay connected", bool(ok))

    bridge = Bridge(build_bridge(tmp), tmp / "bridge-home", ws_base + "/bridge")
    try:
        # ---- bridge: a code from the real API, enrollment and connection by the real exe
        code = (await api("POST", f"/api/locations/{loc['id']}/bridges/codes", json={"name": "Interop PC"})).json()
        bridge.start(code["code"])
        bid = await until(lambda: next((b["id"] for b in db.rows(sa.select(db.bridges.c.id).where(db.bridges.c.location_id == loc["id"]))), None), 30)
        online = bid and await until(lambda: bridges.registry.online(bid), 30)
        hello = (bridges.get_bridge(bid) or {}).get("hello") if bid else None
        if not step("bridge enrolled with the code and connected (hello / welcome / targets)", bool(online),
                    f"{bid}, version {(hello or {}).get('version')}, lan {[x['addr'] for x in (hello or {}).get('lan') or []]}"):
            print(bridge.tail())
            return

        # ---- a camera mapped through the real API: listen ports, set_relay to the real host, targets to the bridge
        r = await api("POST", f"/api/locations/{loc['id']}/bridges/{bid}/targets", json={"name": cam["name"], "host": cam["host"], "ports": [554, 80]})
        if not step("camera mapped (POST targets: set_relay reached the host agent)", r.status_code == 200, f"{r.status_code} {r.text[:200]}"):
            return
        tgt = r.json()
        listen = {p["port"]: p["listen"] for p in tgt["ports"]}
        rec = host.load()["instances"][ci_id]
        nft = (tmp / "host-root" / "firewall.nft").read_text()
        step("host registry and firewall carry the relay ports", sorted(p["port"] for p in rec["relay_ports"]) == sorted(listen.values())
             and f"tcp dport {{ {', '.join(str(p) for p in sorted(listen.values()))} }}" in nft, f"listen ports {listen}")
        ok = await until(lambda: set(listen.values()) <= set((host.relay_status.get(ci_id) or {}).get("listening") or []), 10)
        step("Relay listening on the instance's gateway (127.0.0.1)", bool(ok), str(host.relay_status.get(ci_id)))

        # ---- real traffic: instance socket → Relay → hub → bridge → camera and back
        try:
            line = await rtsp_options(listen[554], cam["host"])
            step("RTSP OPTIONS through the relay", line.startswith("RTSP/1.0 200") or line.startswith("RTSP/1.0 401"), line)
        except Exception as e:
            step("RTSP OPTIONS through the relay", False, f"{type(e).__name__}: {e}; relay kills {kills[-3:]}")
        try:
            resp = await onvif_time(listen[80], cam["host"])
            step("ONVIF GetSystemDateAndTime through the relay (answer read to the camera's close)",
                 b"GetSystemDateAndTimeResponse" in resp, f"{len(resp)} bytes, {resp.split(b'\r\n', 1)[0].decode(errors='replace')}")
        except Exception as e:
            step("ONVIF GetSystemDateAndTime through the relay", False, f"{type(e).__name__}: {e}; relay kills {kills[-3:]}")
        await asyncio.sleep(0.5)
        step("streams closed cleanly on every side", not relay.streams and not bridges.registry.relays[host_row["id"]].legs
             and not bridges.registry.bridges[bid].legs, f"relay {list(relay.streams)}, kills {kills}")

        # ---- Test (probe) through the real endpoint
        r = await api("POST", f"/api/locations/{loc['id']}/bridges/{bid}/targets/{tgt['id']}/test")
        ports = r.json().get("ports", []) if r.status_code == 200 else []
        step("Test: the bridge's probe of each port", r.status_code == 200 and ports and all(p["ok"] for p in ports), r.text[:200])

        # ---- the bridge's heartbeat counts the bytes
        stats = await until(lambda: ((bridges.get_bridge(bid) or {}).get("stats") or {}) if ((bridges.get_bridge(bid) or {}).get("stats") or {}).get("bytes_up") else None, 45, 1.0)
        step("bridge heartbeat counts the stream bytes", bool(stats), str({k: (stats or {}).get(k) for k in ("streams", "bytes_up", "bytes_down", "uptime_s")}))

        # ---- the bridge goes away: new connections are refused with the hub's reason; it comes back
        bridge.stop()
        gone = await until(lambda: not bridges.registry.online(bid), 15)
        n = len(kills)
        how = await refused(listen[554])
        step("bridge stopped: a new connection is reset with \"the bridge is offline\"", bool(gone) and "the bridge is offline" in kills[n:],
             f"{how}; reasons {kills[n:]}")
        bridge.start()
        back = await until(lambda: bridges.registry.online(bid), 40)
        line = ""
        if back:
            try:
                line = await rtsp_options(listen[554], cam["host"])
            except Exception as e:
                line = f"{type(e).__name__}: {e}"
        step("bridge restarted (its saved token): reconnects, and the camera answers again", bool(back) and line.startswith("RTSP/1.0"), line or bridge.tail(5))

        # ---- switched off, then unmapped: nothing gets through any more
        r = await api("PATCH", f"/api/locations/{loc['id']}/bridges/{bid}/targets/{tgt['id']}", json={"enabled": False})
        n = len(kills)
        how = await refused(listen[554])
        step("target switched off: a new connection is aborted by the hub", r.status_code == 200 and "no bridged camera on that port" in kills[n:],
             f"{how}; reasons {kills[n:]}")
        r = await api("DELETE", f"/api/locations/{loc['id']}/bridges/{bid}/targets/{tgt['id']}")
        await until(lambda: not (host.relay_status.get(ci_id) or {}).get("listening"), 10)
        how = await refused(listen[554])
        step("target unmapped: set_relay closed the listener, the connection gets nowhere", r.status_code == 200 and not how.startswith("GOT"),
             f"{how}; host relay ports {host.load()['instances'][ci_id]['relay_ports']}")
    finally:
        bridge.stop()
        for t_ in tasks:
            t_.cancel()
        for srv, _ in relay.servers.values():
            srv.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        hub_http.close()
        if args.keep or any(not ok for _, ok, _ in RESULTS):
            print("--- bridge output (last lines) ---")
            print(bridge.tail(25))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--camera", help="the camera's LAN address (default: the first usable one in the desktop NVR's list)")
    ap.add_argument("--keep", action="store_true", help="leave the temp directory (databases, logs) for reading")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="bridge-interop-"))
    port = free_port()
    server = thread = None
    try:
        cam = pick_camera(args.camera)
        step("camera picked from the desktop NVR's list (read-only GET)", True, f"{cam['id']} {cam['name']} at {cam['host']}")
        setup_env(tmp, port)
        server, thread = start_hub(port)
        asyncio.run(run(args, tmp, port, cam))
    except Exception as e:
        step("the run itself", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        if server is not None:
            server.should_exit = True
            thread.join(timeout=10)
        try:
            from hub import db
            db.reset_engine()
        except Exception:
            pass
        if args.keep:
            print(f"temp directory kept: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed" + (f": {', '.join(failed)}" if failed else ""))
    return 1 if failed or not RESULTS else 0


if __name__ == "__main__":
    sys.exit(main())
