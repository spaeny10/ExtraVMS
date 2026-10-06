"""Unicast ONVIF scan (POST /api/cameras/scan): which subnets may be scanned, the probe against fake devices on
127.0.0.1 (an open camera, one that wants credentials, a plain web server, a closed port), and the route.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_onvif_scan.py   (from backend/)
"""
import asyncio
import base64
import hashlib
import os
import socket
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-scan-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from nvr import onvif_soap  # noqa: E402
from nvr.onvif_soap import local, text  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "onvif"
PASSWORD = "s3cret-pass"


def test_scan_targets():
    assert len(onvif_soap.scan_targets("10.20.7.0/24")) == 254
    assert onvif_soap.scan_targets("10.20.7.9/24")[0] == "10.20.7.1"          # host bits allowed
    assert len(onvif_soap.scan_targets("192.168.0.0/22")) == 1022
    assert onvif_soap.scan_targets("100.64.3.17/32") == ["100.64.3.17"]       # CGNAT, one address
    assert len(onvif_soap.scan_targets("172.31.255.0/28")) == 14
    for bad, why in (("10.0.0.0/21", "too large"), ("8.8.8.0/24", "private"), ("100.128.0.0/24", "private"),
                     ("127.0.0.0/24", "private"), ("fd00::/120", "IPv4"), ("10.20.7.0/33", "not a subnet"), ("garbage", "not a subnet")):
        try:
            onvif_soap.scan_targets(bad)
        except ValueError as e:
            assert why in str(e), (bad, str(e))
        else:
            raise AssertionError(f"{bad} accepted")


class Device(BaseHTTPRequestHandler):
    """mode "open": answers everything; "auth": GetDeviceInformation needs a valid WS-Security digest; "web": no ONVIF."""
    mode = "open"
    seen_passwords: list[str] = []

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.mode == "web":
            return self._send(404, b"<html><body>Not Found</body></html>", "text/html")
        root = ET.fromstring(raw)
        op = local(next(iter(next(e for e in root if local(e) == "Body"))))
        if op == "GetSystemDateAndTime":
            return self._send(200, (FIX / "GetSystemDateAndTimeResponse.xml").read_bytes())
        if op == "GetDeviceInformation":
            if self.mode == "auth" and not self._authorized(root):
                return self._send(400, (FIX / "NotAuthorizedFault.xml").read_bytes())
            return self._send(200, (FIX / "GetDeviceInformationResponse.xml").read_bytes())
        return self._send(400, (FIX / "NotAuthorizedFault.xml").read_bytes())

    def _authorized(self, root) -> bool:
        user, digest, nonce, created = text(root, "Username"), text(root, "Password"), text(root, "Nonce"), text(root, "Created")
        if not (user and digest and nonce and created):
            return False
        want = base64.b64encode(hashlib.sha1(base64.b64decode(nonce) + created.encode() + PASSWORD.encode()).digest()).decode()
        return user == "admin" and digest == want

    def _send(self, code, body, ctype="application/soap+xml"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _server(mode: str):
    handler = type(f"Device_{mode}", (Device,), {"mode": mode})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _probe_all(servers, closed, **cred):
    async with httpx.AsyncClient(timeout=1.5, trust_env=False) as c:
        out = {m: await onvif_soap.probe(c, "127.0.0.1", s.server_address[1], **cred) for m, s in servers.items()}
        out["closed"] = await onvif_soap.probe(c, "127.0.0.1", closed, **cred)
        return out


def test_probe():
    servers = {m: _server(m) for m in ("open", "auth", "web")}
    try:
        r = asyncio.run(_probe_all(servers, _closed_port()))
        assert r["open"] == {"address": "127.0.0.1", "port": servers["open"].server_address[1], "onvif": True, "needs_auth": False,
                             "manufacturer": "Hikvision", "model": "DS-2CD2387G2-LU", "firmware": "V5.7.15 build 230920"}, r["open"]
        assert r["auth"]["onvif"] and r["auth"]["needs_auth"] and "model" not in r["auth"] and not r["auth"].get("auth_failed")
        assert r["web"] is None and r["closed"] is None
        # with the camera's credentials the model is read (digest checked by the fake device)
        r = asyncio.run(_probe_all(servers, _closed_port(), username="admin", password=PASSWORD))
        assert r["auth"]["needs_auth"] is False and r["auth"]["model"] == "DS-2CD2387G2-LU", r["auth"]
        r = asyncio.run(_probe_all(servers, _closed_port(), username="admin", password="wrong"))
        assert r["auth"]["needs_auth"] is True and r["auth"]["auth_failed"] is True
    finally:
        for s in servers.values():
            s.shutdown()


def test_scan_subnet_bounded_and_sorted():
    calls, live = [], {"active": 0, "peak": 0}

    async def fake_probe(client, address, port, username=None, password=None):
        calls.append((address, port))
        live["active"] += 1
        live["peak"] = max(live["peak"], live["active"])
        await asyncio.sleep(0.001)
        live["active"] -= 1
        if address in ("10.20.7.11", "10.20.7.2") and port == 80:
            return {"address": address, "port": port, "onvif": True, "needs_auth": address == "10.20.7.2"}
        return None

    orig = onvif_soap.probe
    onvif_soap.probe = fake_probe
    try:
        found = asyncio.run(onvif_soap.scan_subnet("10.20.7.0/24", [80, 8080, 80], concurrency=16))
    finally:
        onvif_soap.probe = orig
    assert len(calls) == 254 * 2 and live["peak"] <= 16                       # duplicate port dropped, bounded
    assert [d["address"] for d in found] == ["10.20.7.2", "10.20.7.11"]       # numeric order, not text order


def test_route():
    from fastapi import HTTPException

    from nvr import api
    from nvr.db import db
    db.upsert_camera({"id": "gate", "name": "Gate", "host": "10.20.7.11", "onvif_port": 80, "rtsp_port": 554, "username": "admin",
                      "password": "", "main_path": "/main", "sub_path": "/sub"})
    got = {}

    async def fake_scan(subnet, ports, username, password):
        got.update(subnet=subnet, ports=ports, username=username, password=password)
        onvif_soap.scan_targets(subnet)
        return [{"address": "10.20.7.11", "port": 80, "onvif": True, "needs_auth": False},
                {"address": "10.20.7.12", "port": 80, "onvif": True, "needs_auth": True}]

    orig = onvif_soap.scan_subnet
    onvif_soap.scan_subnet = fake_scan
    try:
        r = asyncio.run(api.scan_cameras(api.ScanIn(subnet="10.20.7.0/24")))
        assert got["ports"] == [80, 8000, 8080] and got["username"] is None
        assert r["devices"][0]["camera_id"] == "gate" and "camera_id" not in r["devices"][1]
        asyncio.run(api.scan_cameras(api.ScanIn(subnet="10.20.7.0/24", ports=[8000], username="admin", password="x")))
        assert got["ports"] == [8000] and got["username"] == "admin" and got["password"] == "x"
        try:
            asyncio.run(api.scan_cameras(api.ScanIn(subnet="8.8.8.0/24")))
        except HTTPException as e:
            assert e.status_code == 422 and "private" in e.detail
        else:
            raise AssertionError("public subnet accepted")
    finally:
        onvif_soap.scan_subnet = orig
    import pydantic
    for bad in ({"ports": [0]}, {"ports": []}, {"ports": list(range(1, 9))}):
        try:
            api.ScanIn(subnet="10.0.0.0/24", **bad)
        except pydantic.ValidationError:
            continue
        raise AssertionError(f"accepted {bad}")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
