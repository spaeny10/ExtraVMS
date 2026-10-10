"""Minimal ONVIF SOAP client (stdlib only; the scan uses httpx): WS-UsernameToken digest auth, fault parsing,
XML helpers.

Port-forward mode: a camera reached through a router's port forwards (a central recording server pulling a
site's cameras over the internet) has an outside address (`public_host`, `public_rtsp_port`,
`public_onvif_port`) distinct from the one it believes it has. Every URL the camera hands back (GetServices /
GetCapabilities XAddrs, the PullPoint SubscriptionReference, GetStreamUri, PTZ / imaging / device-IO service
addresses) names its internal address, e.g. http://192.168.105.12:80/onvif/Events/SubManager_1, so `rewrite`
maps it to the outside one before it is used. With the public fields empty nothing changes.

Also the unicast ONVIF scan (`scan_subnet`): WS-Discovery multicast does not cross a VPN tunnel.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import ipaddress
import logging
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

log = logging.getLogger("nvr.onvif")

ENVELOPE = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
 xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
 xmlns:tr2="http://www.onvif.org/ver20/media/wsdl"
 xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
 xmlns:tan="http://www.onvif.org/ver20/analytics/wsdl"
 xmlns:tt="http://www.onvif.org/ver10/schema"
 xmlns:wsa="http://www.w3.org/2005/08/addressing"
 xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
 xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"
 xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl"
 xmlns:tmd="http://www.onvif.org/ver10/deviceIO/wsdl">
<s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>"""

WSSE = (
    '<wsse:Security s:mustUnderstand="1" '
    'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
    'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
    "<wsse:UsernameToken><wsse:Username>{user}</wsse:Username>"
    '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>'
    '<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce}</wsse:Nonce>'
    "<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>"
)

ACTION_PULL = "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesRequest"
ACTION_UNSUB = "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeRequest"


# --------------------------------------------------------------------------- XML helpers

def local(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def find_all(el: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in el.iter() if local(e) == name]


def find(el: ET.Element | None, name: str) -> ET.Element | None:
    if el is None:
        return None
    return next((e for e in el.iter() if local(e) == name and e is not el), None)


def text(el: ET.Element | None, name: str, default: str | None = None) -> str | None:
    found = find(el, name)
    return found.text.strip() if found is not None and found.text else default


def children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if local(c) == name]


def simple_items(el: ET.Element | None) -> dict[str, str]:
    if el is None:
        return {}
    return {i.get("Name"): i.get("Value") for i in find_all(el, "SimpleItem")}


# --------------------------------------------------------------------------- SOAP client

class OnvifError(Exception):
    pass


class Onvif:
    def __init__(self, host: str, port: int, user: str, password: str, timeout: float = 10, cam: dict | None = None):
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.timeout = timeout
        self.device_url = f"http://{host}:{port}/onvif/device_service"
        self.clock_offset = dt.timedelta(0)
        self.services: dict[str, str] = {}
        self.cam = cam    # the camera row, for port-forward address rewriting (None = use URLs as given)

    @classmethod
    def for_camera(cls, cam: dict, timeout: float = 10) -> "Onvif":
        """The client for a camera row: its outside address in port-forward mode, and every URL the camera
        returns rewritten to that address before it is called."""
        host, onvif_port, _ = outside(cam)
        return cls(host, onvif_port, cam["username"], cam["password"], timeout=timeout, cam=cam)

    def rewrite(self, url: str | None) -> str | None:
        return rewrite(url, self.cam)

    def _security(self) -> str:
        nonce = os.urandom(16)
        created = (dt.datetime.now(dt.timezone.utc) + self.clock_offset).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        digest = hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()
        return WSSE.format(
            user=escape(self.user),
            digest=base64.b64encode(digest).decode(),
            nonce=base64.b64encode(nonce).decode(),
            created=created,
        )

    def call(self, url: str, body: str, auth: bool = True, header: str = "", timeout: float | None = None) -> ET.Element:
        hdr = (self._security() if auth else "") + header
        data = ENVELOPE.format(header=hdr, body=body).encode()
        url = rewrite(url, self.cam)   # a camera-reported address, in port-forward mode (unchanged otherwise)
        req =urllib.request.Request(url, data, {"Content-Type": "application/soap+xml; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise OnvifError(f"HTTP {e.code}: {self._fault(e.read()) or e.reason}") from None
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            raise OnvifError(f"connection failed: {e}") from None
        root = ET.fromstring(raw)
        fault = self._fault(raw)
        if fault:
            raise OnvifError(fault)
        body_el = next((e for e in root if local(e) == "Body"), root)
        return body_el

    @staticmethod
    def _fault(raw: bytes) -> str | None:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return None
        fault = next((e for e in root.iter() if local(e) == "Fault"), None)
        if fault is None:
            return None
        reason = text(fault, "Text") or text(fault, "faultstring") or "SOAP fault"
        subcodes = [v.text for v in find_all(fault, "Value") if v.text]
        return f"{reason} ({' / '.join(subcodes)})" if subcodes else reason

    def service(self, key: str) -> str | None:
        return self.services.get(key)


# --------------------------------------------------------------------------- shared helpers

SERVICE_KEYS = {
    "http://www.onvif.org/ver10/device/wsdl": "device",
    "http://www.onvif.org/ver10/media/wsdl": "media",
    "http://www.onvif.org/ver20/media/wsdl": "media2",
    "http://www.onvif.org/ver10/events/wsdl": "events",
    "http://www.onvif.org/ver20/ptz/wsdl": "ptz",
    "http://www.onvif.org/ver20/imaging/wsdl": "imaging",
    "http://www.onvif.org/ver10/deviceIO/wsdl": "deviceio",
    "http://www.onvif.org/ver20/analytics/wsdl": "analytics",
}


def clock_offset(body: ET.Element | None) -> dt.timedelta | None:
    """GetSystemDateAndTime reply -> camera clock minus this machine's clock (None without a UTCDateTime)."""
    utc = find(body, "UTCDateTime")
    if utc is None:
        return None
    d, t = find(utc, "Date"), find(utc, "Time")
    try:
        cam_time = dt.datetime(int(text(d, "Year")), int(text(d, "Month")), int(text(d, "Day")),
                               int(text(t, "Hour")), int(text(t, "Minute")), int(text(t, "Second")), tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None
    return cam_time - dt.datetime.now(dt.timezone.utc)


def sync_clock(cam: Onvif) -> dt.timedelta:
    """Learn the camera's clock offset (unauthenticated) so WS-Security digests aren't rejected."""
    offset = clock_offset(cam.call(cam.device_url, "<tds:GetSystemDateAndTime/>", auth=False))
    if offset is not None:
        cam.clock_offset = offset
    return cam.clock_offset


# GetCapabilities (older cameras without GetServices): capability element -> service key
CAPABILITY_KEYS = {"Device": "device", "Media": "media", "Events": "events", "PTZ": "ptz", "Imaging": "imaging",
                   "DeviceIO": "deviceio", "Analytics": "analytics"}


def parse_services(body: ET.Element) -> dict[str, str]:
    """GetServices reply -> {service key: XAddr} (unknown namespaces keep their namespace as key)."""
    out = {}
    for s in find_all(body, "Service"):
        ns, addr = text(s, "Namespace"), text(s, "XAddr")
        if ns and addr:
            out[SERVICE_KEYS.get(ns, ns)] = addr
    return out


def parse_capabilities(body: ET.Element) -> dict[str, str]:
    """GetCapabilities reply -> {service key: XAddr} (DeviceIO sits under Capabilities/Extension)."""
    out: dict[str, str] = {}
    for el in body.iter():
        key = CAPABILITY_KEYS.get(local(el))
        addr = next((c.text.strip() for c in el if local(c) == "XAddr" and c.text), None)
        if key and addr and key not in out:
            out[key] = addr
    return out


def discover_services(cam: Onvif) -> dict[str, str]:
    """GetServices (GetCapabilities on a camera without it) -> cam.services keyed by SERVICE_KEYS, every
    address rewritten to the camera's outside address in port-forward mode."""
    try:
        found = parse_services(cam.call(cam.device_url, "<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability></tds:GetServices>"))
    except OnvifError as e:
        if "NotAuthorized" in str(e) or "401" in str(e):
            raise   # GetCapabilities would be refused the same way (clock or password)
        found = {}
    if not found:
        found = parse_capabilities(cam.call(cam.device_url, "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>"))
    for k, addr in found.items():
        cam.services[k] = cam.rewrite(addr)
    return cam.services


# --------------------------------------------------------------------------- port-forward mode

def outside(cam: dict) -> tuple[str, int, int]:
    """(host, ONVIF port, RTSP port) this server connects to: the outside address in port-forward mode, else
    the camera's own. A public port left empty means the same port number outside as inside."""
    return (cam.get("public_host") or cam["host"],
            int(cam.get("public_onvif_port") or cam.get("onvif_port") or 80),
            int(cam.get("public_rtsp_port") or cam.get("rtsp_port") or 554))


def _private_ip(host: str | None) -> bool:
    try:
        ip = ipaddress.ip_address((host or "").strip("[]"))
    except ValueError:
        return False
    return ip.is_private or ip.is_link_local


# names that only resolve on a LAN (RFC 6762 .local, RFC 8375 .home.arpa, ICANN's .internal, common router suffixes)
LAN_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localdomain", ".localhost", ".home", ".intranet", ".corp")


def public_address(host: str | None) -> bool:
    """A public IP (not private, CGNAT 100.64/10, loopback, link-local or reserved) or a DNS name that looks public
    (dotted, not a LAN-only suffix). Names are not resolved: this runs on every URL a camera returns."""
    h = (host or "").strip().strip("[]").rstrip(".").lower()
    if not h:
        return False
    try:
        return ipaddress.ip_address(h).is_global
    except ValueError:
        pass
    return "." in h and not h.endswith(LAN_SUFFIXES) and h != "localhost"


def auto_forward(cam: dict | None) -> bool:
    """A camera added by its public address (a public IP or a public-looking DNS name) with no outside address set:
    the private addresses it reports are taken to be its own LAN address behind a port forward. Conservative: a
    LAN name (cam1.lan, nvr.local, a single label), a CGNAT / VPN address (100.64/10) or a private one never is."""
    return bool(cam) and not cam.get("public_host") and public_address(cam.get("host"))


def rewrite(url: str | None, cam: dict | None) -> str | None:
    """A URL the camera returned, pointed at its outside address (port-forward mode). The host becomes
    public_host; an http(s) URL's port becomes public_onvif_port (the one ONVIF forward: device, media, events,
    PTZ, imaging, device-IO and the subscription manager all share it), an rtsp(s) URL's port public_rtsp_port
    (whatever it reported, 554 or another). Path, query and any user info are kept.
    Without public_host, a camera added by its public address (auto_forward) that reports a private host has
    that host replaced by the one we reach it at, with the ONVIF / RTSP ports we connect to (outside(): an outside
    port set without an outside host counts; SD replay URLs keep their port or take public_replay_port). Anything else, or a URL of another scheme, comes back unchanged."""
    if not url or not cam:
        return url
    try:
        parts = urllib.parse.urlsplit(url.strip())
        reported = parts.port
    except ValueError:
        return url
    scheme = parts.scheme.lower()
    if cam.get("public_host"):
        host = cam["public_host"]
        if scheme in ("http", "https"):
            port = cam.get("public_onvif_port") or reported
        elif scheme in ("rtsp", "rtsps"):
            port = cam.get("public_rtsp_port") or reported
        else:
            return url
    elif auto_forward(cam) and _private_ip(parts.hostname) and parts.hostname != cam["host"]:
        # Added by its public address and reporting a private one: the same camera behind a port forward. Point
        # its URLs at the address and ports we reach it at (event 2026-10-08: 5001.bigview.ai:8082 answered, then
        # handed back http://192.168.50.37:80/onvif/Events and every ONVIF call timed out).
        # the ports we connect to, as outside() picks them (outside ports set without an outside host count)
        host, onvif_port, rtsp_port = outside(cam)
        if scheme in ("http", "https"):
            port = onvif_port or reported
        elif scheme in ("rtsp", "rtsps"):
            # SD replay has its own port (555 on Milesight): keep it unless an outside replay port is set
            replay = "replay" in (parts.path or "").lower()
            port = (cam.get("public_replay_port") or reported) if replay else (rtsp_port or reported)
        else:
            return url
    else:
        return url
    netloc = host + (f":{int(port)}" if port else "")
    if "@" in parts.netloc:
        netloc = parts.netloc.rsplit("@", 1)[0] + "@" + netloc
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


# --------------------------------------------------------------------------- unicast scan

SCAN_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")]
SCAN_MIN_PREFIX = 22          # at most a /22 (1,022 addresses)
SCAN_PORTS = [80, 8000, 8080]
SCAN_CONCURRENCY = 64
SCAN_TIMEOUT_S = 1.5


def scan_targets(subnet: str) -> list[str]:
    """Host addresses of `subnet`. ValueError unless it is IPv4, private (RFC 1918) or carrier-grade NAT
    (100.64.0.0/10), and no larger than a /22."""
    try:
        net = ipaddress.ip_network(str(subnet).strip(), strict=False)
    except ValueError:
        raise ValueError(f"{str(subnet)[:50]!r} is not a subnet like 10.20.7.0/24") from None
    if net.version != 4:
        raise ValueError("only IPv4 subnets can be scanned")
    if net.prefixlen < SCAN_MIN_PREFIX:
        raise ValueError(f"/{net.prefixlen} is too large: scan at most a /{SCAN_MIN_PREFIX} (1,022 addresses)")
    if not any(net.subnet_of(p) for p in SCAN_NETS):
        raise ValueError("only private (10.x, 172.16-31.x, 192.168.x) or carrier-grade NAT (100.64.x) subnets can be scanned")
    return [str(h) for h in net.hosts()] or [str(net.network_address)]


def parse_device_info(body: ET.Element | None) -> dict:
    """GetDeviceInformation reply -> {manufacturer, model, firmware} (missing ones left out)."""
    out = {"manufacturer": text(body, "Manufacturer"), "model": text(body, "Model"), "firmware": text(body, "FirmwareVersion")}
    return {k: v for k, v in out.items() if v}


def _soap_body(raw: bytes) -> tuple[ET.Element | None, bool]:
    """(Body element, is a fault) of a SOAP reply; (None, False) when it is not a SOAP envelope at all."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None, False
    if local(root) != "Envelope":
        return None, False
    body = next((e for e in root if local(e) == "Body"), root)
    return body, any(local(e) == "Fault" for e in body.iter())


async def probe(client, address: str, port: int, username: str | None = None, password: str | None = None) -> dict | None:
    """One host:port: an ONVIF device answers GetSystemDateAndTime (unauthenticated by the spec). Its model comes
    from GetDeviceInformation, tried without credentials, then with `username`/`password` when given (never
    stored or logged). None when nothing ONVIF answers there. `client`: an httpx.AsyncClient."""
    import httpx
    url = f"http://{address}:{port}/onvif/device_service"
    hdrs = {"Content-Type": "application/soap+xml; charset=utf-8"}

    async def post(body: str, header: str = ""):
        return await client.post(url, content=ENVELOPE.format(header=header, body=body).encode(), headers=hdrs)

    try:
        r = await post("<tds:GetSystemDateAndTime/>")
    except (httpx.HTTPError, OSError):
        return None
    body, _fault = _soap_body(r.content)
    if body is None and not (r.status_code == 401 and r.headers.get("www-authenticate")):
        return None   # a web server, but not an ONVIF device service
    out: dict = {"address": address, "port": port, "onvif": True, "needs_auth": False}
    offset = clock_offset(body) or dt.timedelta(0)
    try:
        r = await post("<tds:GetDeviceInformation/>")
        body, fault = _soap_body(r.content)
        if r.status_code == 200 and body is not None and not fault:
            return {**out, **parse_device_info(body)}
    except (httpx.HTTPError, OSError):
        pass
    out["needs_auth"] = True
    if username:
        cam = Onvif(address, port, username, password or "")
        cam.clock_offset = offset
        try:
            r = await post("<tds:GetDeviceInformation/>", header=cam._security())
            body, fault = _soap_body(r.content)
            if r.status_code == 200 and body is not None and not fault:
                return {**out, "needs_auth": False, **parse_device_info(body)}
            out["auth_failed"] = True
        except (httpx.HTTPError, OSError):
            pass
    return out


async def scan_subnet(subnet: str, ports: list[int] | None = None, username: str | None = None, password: str | None = None,
                      concurrency: int = SCAN_CONCURRENCY, timeout: float = SCAN_TIMEOUT_S) -> list[dict]:
    """Probe every host of `subnet` (see scan_targets) on `ports` for an ONVIF device service, `concurrency` at a
    time with `timeout` per request. Returns [{address, port, onvif, needs_auth, manufacturer?, model?,
    firmware?, auth_failed?}] sorted by address."""
    import httpx
    hosts = scan_targets(subnet)
    ports = list(dict.fromkeys(ports or SCAN_PORTS))
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=0)
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout), limits=limits, trust_env=False, follow_redirects=False) as client:
        async def one(address: str, port: int) -> dict | None:
            async with sem:
                return await probe(client, address, port, username, password)
        results = await asyncio.gather(*(one(a, p) for a in hosts for p in ports))
    found = sorted((r for r in results if r), key=lambda r: (ipaddress.ip_address(r["address"]), r["port"]))
    log.info("ONVIF scan of %s on ports %s: %d device(s)", subnet, ports, len(found))
    return found


def parse_duration(s: str | None) -> float:
    """xsd:duration like PT1S / PT00H00M10S / PT2.5S -> seconds (0 if missing)."""
    if not s or not s.startswith("PT"):
        return 0.0
    import re
    total = 0.0
    for num, unit in re.findall(r"([0-9.]+)([HMS])", s[2:]):
        total += float(num) * {"H": 3600, "M": 60, "S": 1}[unit]
    return total
