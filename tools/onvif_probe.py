"""ONVIF capability probe (stdlib only).

Reports what a camera actually supports so the NVR pipeline can be tuned to it:
device info, services, media profiles, RTSP URIs, metadata configurations
(Profile M), analytics modules, event topics, and optionally live events.

Usage:
    python tools/onvif_probe.py --discover
    python tools/onvif_probe.py                     # camera 1 from .env
    python tools/onvif_probe.py --camera 2 --listen 60 --ffprobe
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from nvr.onvif_soap import (  # noqa: E402
    ACTION_PULL, ACTION_UNSUB, Onvif, OnvifError, children, escape, find, find_all, local, simple_items, text,
)

# --------------------------------------------------------------------------- probe steps

SERVICE_KEYS = {
    "http://www.onvif.org/ver10/device/wsdl": "device",
    "http://www.onvif.org/ver10/media/wsdl": "media",
    "http://www.onvif.org/ver20/media/wsdl": "media2",
    "http://www.onvif.org/ver10/events/wsdl": "events",
    "http://www.onvif.org/ver20/analytics/wsdl": "analytics",
    "http://www.onvif.org/ver20/imaging/wsdl": "imaging",
    "http://www.onvif.org/ver20/ptz/wsdl": "ptz",
    "http://www.onvif.org/ver10/recording/wsdl": "recording",
    "http://www.onvif.org/ver10/search/wsdl": "search",
    "http://www.onvif.org/ver10/replay/wsdl": "replay",
    "http://www.onvif.org/ver10/deviceIO/wsdl": "deviceio",
}


def sync_clock(cam: Onvif) -> dict:
    body = cam.call(cam.device_url, "<tds:GetSystemDateAndTime/>", auth=False)
    utc = find(body, "UTCDateTime")
    info = {"ntp": text(body, "DateTimeType"), "tz": text(body, "TZ")}
    if utc is not None:
        d, t = find(utc, "Date"), find(utc, "Time")
        cam_time = dt.datetime(
            int(text(d, "Year")), int(text(d, "Month")), int(text(d, "Day")),
            int(text(t, "Hour")), int(text(t, "Minute")), int(text(t, "Second")),
            tzinfo=dt.timezone.utc,
        )
        cam.clock_offset = cam_time - dt.datetime.now(dt.timezone.utc)
        info["camera_utc"] = cam_time.isoformat()
        info["offset_seconds"] = round(cam.clock_offset.total_seconds(), 1)
    return info


def device_info(cam: Onvif) -> dict:
    body = cam.call(cam.device_url, "<tds:GetDeviceInformation/>")
    return {k: text(body, k) for k in ("Manufacturer", "Model", "FirmwareVersion", "SerialNumber", "HardwareId")}


def services(cam: Onvif) -> dict:
    body = cam.call(cam.device_url, "<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability></tds:GetServices>")
    out = {}
    for svc in find_all(body, "Service"):
        ns, xaddr = text(svc, "Namespace"), text(svc, "XAddr")
        ver = find(svc, "Version")
        version = f"{text(ver, 'Major')}.{text(ver, 'Minor')}" if ver is not None else None
        key = SERVICE_KEYS.get(ns, ns)
        out[key] = {"xaddr": xaddr, "version": version}
        cam.services[key] = xaddr
    return out


def profiles_media1(cam: Onvif) -> list[dict]:
    url = cam.service("media")
    if not url:
        return []
    body = cam.call(url, "<trt:GetProfiles/>")
    result = []
    for p in find_all(body, "Profiles"):
        token = p.get("token")
        venc = next((c for c in p if local(c) == "VideoEncoderConfiguration"), None)
        meta = next((c for c in p if local(c) == "MetadataConfiguration"), None)
        vac = next((c for c in p if local(c) == "VideoAnalyticsConfiguration"), None)
        entry = {"token": token, "name": text(p, "Name")}
        if venc is not None:
            entry["video"] = {
                "encoding": text(venc, "Encoding"),
                "width": text(find(venc, "Resolution"), "Width"),
                "height": text(find(venc, "Resolution"), "Height"),
                "fps": text(venc, "FrameRateLimit"),
                "bitrate_kbps": text(venc, "BitrateLimit"),
                "gop": text(venc, "GovLength"),
            }
        if meta is not None:
            entry["metadata"] = {
                "token": meta.get("token"),
                "name": text(meta, "Name"),
                "analytics": text(meta, "Analytics"),
                "events_filter": find(meta, "Events") is not None,
                "ptz_status": find(meta, "PTZStatus") is not None,
            }
        if vac is not None:
            entry["analytics_config_token"] = vac.get("token")
        try:
            uri_body = cam.call(url, (
                "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
                "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
                f"<trt:ProfileToken>{escape(token)}</trt:ProfileToken></trt:GetStreamUri>"
            ))
            entry["rtsp_uri"] = text(uri_body, "Uri")
        except OnvifError as e:
            entry["rtsp_uri_error"] = str(e)
        result.append(entry)
    return result


def profiles_media2(cam: Onvif) -> list[dict]:
    url = cam.service("media2")
    if not url:
        return []
    body = cam.call(url, "<tr2:GetProfiles><tr2:Type>All</tr2:Type></tr2:GetProfiles>")
    result = []
    for p in find_all(body, "Profiles"):
        cfg = find(p, "Configurations")
        present = sorted({local(c) for c in cfg}) if cfg is not None else []
        meta = next((c for c in cfg if local(c) == "Metadata"), None) if cfg is not None else None
        entry = {"token": p.get("token"), "name": text(p, "Name"), "configurations": present}
        if meta is not None:
            entry["metadata"] = {"token": meta.get("token"), "analytics": text(meta, "Analytics")}
        result.append(entry)
    return result


def metadata_configs(cam: Onvif) -> list[dict]:
    url = cam.service("media")
    if not url:
        return []
    body = cam.call(url, "<trt:GetMetadataConfigurations/>")
    return [
        {"token": m.get("token"), "name": text(m, "Name"), "analytics": text(m, "Analytics"),
         "session_timeout": text(m, "SessionTimeout")}
        for m in find_all(body, "Configurations")
    ]


def analytics(cam: Onvif, config_tokens: set[str]) -> dict:
    url = cam.service("analytics")
    if not url:
        return {"available": False}
    out: dict = {"available": True, "modules": {}, "rules": {}}
    try:
        body = cam.call(url, "<tan:GetSupportedMetadata/>")
        out["supported_metadata"] = [
            {"type": m.get("Type"), "object_classes": [c.text for c in find_all(m, "Type") if c.text]}
            for m in find_all(body, "AnalyticsModule")
        ]
    except OnvifError as e:
        out["supported_metadata_error"] = str(e)
    for token in sorted(config_tokens):
        tok = escape(token)
        for kind, op in (("modules", "GetAnalyticsModules"), ("rules", "GetRules")):
            try:
                body = cam.call(url, f"<tan:{op}><tan:ConfigurationToken>{tok}</tan:ConfigurationToken></tan:{op}>")
                tag = "AnalyticsModule" if kind == "modules" else "Rule"
                out[kind][token] = [
                    {"name": m.get("Name"), "type": m.get("Type"), "params": simple_items(m)}
                    for m in find_all(body, tag)
                ]
            except OnvifError as e:
                out[kind][token] = f"error: {e}"
    return out


def event_topics(cam: Onvif) -> list[dict]:
    url = cam.service("events")
    if not url:
        return []
    body = cam.call(url, "<tev:GetEventProperties/>")
    topic_set = find(body, "TopicSet")
    topics: list[dict] = []

    def walk(el: ET.Element, path: list[str]) -> None:
        for child in el:
            name = local(child)
            if name == "MessageDescription":
                continue
            child_path = path + [name]
            if any(k.endswith("topic") and v == "true" for k, v in child.attrib.items()):
                desc = find(child, "MessageDescription")
                topics.append({
                    "topic": "/".join(child_path),
                    "source": [i.get("Name") for i in find_all(find(desc, "Source"), "SimpleItemDescription")] if desc is not None and find(desc, "Source") is not None else [],
                    "data": [i.get("Name") for i in find_all(find(desc, "Data"), "SimpleItemDescription")] if desc is not None and find(desc, "Data") is not None else [],
                })
            walk(child, child_path)

    if topic_set is not None:
        walk(topic_set, [])
    return topics


def listen_events(cam: Onvif, seconds: int) -> list[dict]:
    url = cam.service("events")
    if not url:
        print("  no events service")
        return []
    body = cam.call(url, "<tev:CreatePullPointSubscription><tev:InitialTerminationTime>PT"
                         f"{seconds + 60}S</tev:InitialTerminationTime></tev:CreatePullPointSubscription>")
    address = text(find(body, "SubscriptionReference"), "Address")
    if not address:
        raise OnvifError("no subscription address returned")
    wsa = lambda action: (f'<wsa:Action>{action}</wsa:Action><wsa:To>{escape(address)}</wsa:To>'
                          f'<wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID>')
    events: list[dict] = []
    deadline = dt.datetime.now() + dt.timedelta(seconds=seconds)
    print(f"  subscribed at {address}; trigger motion / walk past the camera...")
    try:
        while dt.datetime.now() < deadline:
            resp = cam.call(address, "<tev:PullMessages><tev:Timeout>PT5S</tev:Timeout>"
                                     "<tev:MessageLimit>100</tev:MessageLimit></tev:PullMessages>",
                            header=wsa(ACTION_PULL), timeout=15)
            for n in find_all(resp, "NotificationMessage"):
                msg = find(n, "Message")
                inner = find(msg, "Message") if msg is not None else None
                ev = {
                    "topic": (text(n, "Topic") or ""),
                    "utc": inner.get("UtcTime") if inner is not None else None,
                    "op": inner.get("PropertyOperation") if inner is not None else None,
                    "source": simple_items(find(inner, "Source")),
                    "data": simple_items(find(inner, "Data")),
                }
                events.append(ev)
                print(f"  {ev['utc']} {ev['op'] or '':9} {ev['topic']}  src={ev['source']} data={ev['data']}")
    finally:
        try:
            cam.call(address, "<wsnt:Unsubscribe/>", header=wsa(ACTION_UNSUB), timeout=5)
        except OnvifError:
            pass
    return events


def ffprobe_streams(uri: str, user: str, password: str) -> list[dict] | str:
    exe = shutil.which("ffprobe")
    if not exe:
        return "ffprobe not found on PATH"
    parts = urllib.parse.urlsplit(uri)
    netloc = f"{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(password, safe='')}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    authed = urllib.parse.urlunsplit(parts._replace(netloc=netloc))
    proc = subprocess.run(
        [exe, "-v", "error", "-rtsp_transport", "tcp", "-timeout", "10000000",
         "-show_streams", "-of", "json", authed],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        return proc.stderr.replace(password, "***").strip()[:500]
    return [
        {k: s.get(k) for k in ("index", "codec_type", "codec_name", "codec_tag_string", "width", "height",
                               "r_frame_rate", "sample_rate")}
        for s in json.loads(proc.stdout).get("streams", [])
    ]


# --------------------------------------------------------------------------- discovery

def discover(wait: float = 3.0) -> list[dict]:
    msg = f"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
<e:Header><w:MessageID>uuid:{uuid.uuid4()}</w:MessageID>
<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>
<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.5)
    sock.sendto(msg.encode(), ("239.255.255.250", 3702))
    found: dict[str, dict] = {}
    end = dt.datetime.now() + dt.timedelta(seconds=wait)
    while dt.datetime.now() < end:
        try:
            data, (ip, _) = sock.recvfrom(65535)
        except socket.timeout:
            continue
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            continue
        scopes = (text(root, "Scopes") or "").split()
        found[ip] = {
            "ip": ip,
            "xaddrs": (text(root, "XAddrs") or "").split(),
            "name": next((urllib.parse.unquote(s.rsplit("/", 1)[-1]) for s in scopes if "/name/" in s), None),
            "hardware": next((urllib.parse.unquote(s.rsplit("/", 1)[-1]) for s in scopes if "/hardware/" in s), None),
            "profiles": sorted({s.rsplit("/", 1)[-1] for s in scopes if "/Profile/" in s}),
        }
    sock.close()
    return sorted(found.values(), key=lambda d: socket.inet_aton(d["ip"]))


# --------------------------------------------------------------------------- main

def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return {**env, **{k: v for k, v in os.environ.items() if k.startswith("CAMERA_")}}


def step(report: dict, key: str, fn, *args):
    try:
        report[key] = fn(*args)
        return report[key]
    except OnvifError as e:
        report[key] = {"error": str(e)}
        print(f"  [{key}] ERROR: {e}")
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--discover", action="store_true", help="WS-Discovery scan of the LAN and exit")
    ap.add_argument("--camera", type=int, default=1, help="camera index in .env (CAMERA_<n>_HOST, ...)")
    ap.add_argument("--listen", type=int, default=0, metavar="SEC", help="pull live ONVIF events for SEC seconds")
    ap.add_argument("--ffprobe", action="store_true", help="inspect RTSP tracks (checks for metadata/data track)")
    ap.add_argument("--out", type=Path, default=ROOT / "probe_report.json")
    args = ap.parse_args()

    if args.discover:
        devices = discover()
        print(json.dumps(devices, indent=2) if devices else "No ONVIF devices answered WS-Discovery.")
        return 0

    env = load_env(ROOT / ".env")
    n = args.camera
    host = env.get(f"CAMERA_{n}_HOST")
    if not host:
        print(f"CAMERA_{n}_HOST not set. Copy .env.example to .env and fill it in.")
        return 2
    cam = Onvif(host, int(env.get(f"CAMERA_{n}_ONVIF_PORT", "80")),
                env.get(f"CAMERA_{n}_USER", "admin"), env.get(f"CAMERA_{n}_PASS", ""))

    report: dict = {"host": host, "probed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    print(f"Probing {cam.device_url}")
    step(report, "clock", sync_clock, cam)
    if step(report, "device", device_info, cam) is None:
        print("Cannot authenticate / reach device service; stopping.")
        args.out.write_text(json.dumps(report, indent=2))
        return 1
    step(report, "services", services, cam)
    m1 = step(report, "profiles_media1", profiles_media1, cam) or []
    step(report, "profiles_media2", profiles_media2, cam)
    step(report, "metadata_configurations", metadata_configs, cam)
    vac_tokens = {p["analytics_config_token"] for p in m1 if p.get("analytics_config_token")}
    step(report, "analytics", analytics, cam, vac_tokens)
    topics = step(report, "event_topics", event_topics, cam) or []

    if args.ffprobe:
        report["rtsp_tracks"] = {
            p["token"]: ffprobe_streams(p["rtsp_uri"], cam.user, cam.password)
            for p in m1 if p.get("rtsp_uri")
        }
    if args.listen:
        print(f"\nListening for events ({args.listen}s)...")
        step(report, "live_events", listen_events, cam, args.listen)

    args.out.write_text(json.dumps(report, indent=2))

    # ---- summary
    d = report.get("device", {})
    print(f"\n{d.get('Manufacturer')} {d.get('Model')}  fw {d.get('FirmwareVersion')}")
    print(f"Clock offset vs this PC: {report.get('clock', {}).get('offset_seconds')} s")
    print("Services:", ", ".join(sorted(cam.services)))
    for p in m1:
        v = p.get("video", {})
        meta = "  +metadata" if p.get("metadata") else ""
        print(f"  profile {p['token']:<12} {v.get('encoding')} {v.get('width')}x{v.get('height')} "
              f"@{v.get('fps')}fps{meta}  {p.get('rtsp_uri', p.get('rtsp_uri_error'))}")
    an = report.get("analytics", {})
    if an.get("supported_metadata"):
        for m in an["supported_metadata"]:
            print(f"  Profile M metadata: {m['type']} classes={m['object_classes']}")
    print(f"Event topics: {len(topics)}")
    for t in topics:
        print(f"  {t['topic']}  data={t['data']}")
    print(f"\nFull report: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
