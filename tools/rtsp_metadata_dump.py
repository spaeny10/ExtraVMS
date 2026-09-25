"""Dump the ONVIF metadata (application/data) RTP track of an RTSP stream (stdlib only).

Shows exactly what the camera puts in its Profile M / metadata stream:
per-frame objects (id, class, bounding box) and embedded events.

Usage:
    python tools/rtsp_metadata_dump.py                  # camera 1, /main, 30 s
    python tools/rtsp_metadata_dump.py --path /sub --seconds 60
"""
from __future__ import annotations

import argparse
import re
import socket
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from onvif_probe import ROOT, find, find_all, load_env, local  # noqa: E402
from nvr.rtsp_client import Rtsp, rtp_payload  # noqa: E402


def summarize(xml: bytes) -> str:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        return f"  (unparseable XML: {e})"
    lines = []
    for frame in find_all(root, "Frame"):
        objs = []
        for obj in children_named(frame, "Object"):
            box = find(obj, "BoundingBox")
            cls = [c.text for c in find_all(obj, "Type") if c.text] or \
                  [c.text for c in find_all(obj, "ClassCandidate") if c.text]
            likely = [c.get("Likelihood") for c in find_all(obj, "Type") if c.get("Likelihood")]
            b = ({k: box.get(k) for k in ("left", "top", "right", "bottom")} if box is not None else {})
            objs.append(f"id={obj.get('ObjectId')} class={cls or '?'} p={likely or '?'} box={b}")
        lines.append(f"  Frame {frame.get('UtcTime')}: {len(objs)} object(s)")
        lines += [f"     {o}" for o in objs]
    for n in find_all(root, "NotificationMessage"):
        topic = next((e.text for e in n.iter() if local(e) == "Topic"), "")
        data_el = find(n, "Data")
        data = {i.get("Name"): i.get("Value") for i in find_all(data_el if data_el is not None else n, "SimpleItem")}
        lines.append(f"  Event {topic} {data}")
    return "\n".join(lines) or f"  (other: {[local(c) for c in root]})"


def children_named(el: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in el.iter() if local(e) == name and e is not el]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera", type=int, default=1)
    ap.add_argument("--path", default="/main")
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--out", type=Path, default=ROOT / "metadata_dump.xml")
    args = ap.parse_args()

    env = load_env(ROOT / ".env")
    n = args.camera
    host = env.get(f"CAMERA_{n}_HOST")
    if not host:
        print(f"CAMERA_{n}_HOST not set in .env")
        return 2
    url = f"rtsp://{host}:{env.get(f'CAMERA_{n}_RTSP_PORT', '554')}{args.path}"
    cam = Rtsp(url, env.get(f"CAMERA_{n}_USER", "admin"), env.get(f"CAMERA_{n}_PASS", ""))

    status, h, sdp = cam.request("DESCRIBE", url, {"Accept": "application/sdp"})
    if status != 200:
        print(f"DESCRIBE failed: {status}")
        return 1
    sdp_text = sdp.decode(errors="replace")
    base = h.get("content-base", url)
    sections = re.split(r"\r?\nm=", sdp_text)
    meta = next((s for s in sections[1:] if s.startswith("application")), None)
    if not meta:
        print("No application/metadata track in SDP:\n" + sdp_text)
        return 1
    print("Metadata track SDP:\n  m=" + meta.strip().replace("\n", "\n  "))
    control = re.search(r"a=control:(\S+)", meta).group(1)
    track_url = control if control.startswith("rtsp://") else base.rstrip("/") + "/" + control

    status, h, _ = cam.request("SETUP", track_url, {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"})
    if status != 200:
        print(f"SETUP failed: {status}")
        return 1
    cam.session = h["session"].split(";")[0]
    status, _, _ = cam.request("PLAY", base, {"Range": "npt=0.000-"})
    if status != 200:
        print(f"PLAY failed: {status}")
        return 1

    print(f"\nReading metadata for {args.seconds}s — walk / drive through the scene...\n")
    cam.sock.settimeout(5)
    docs, current = [], b""
    end = time.time() + args.seconds
    last_keepalive = time.time()
    with args.out.open("wb") as out:
        while time.time() < end:
            if time.time() - last_keepalive > 20:
                cam.cseq += 1
                cam.sock.sendall((f"GET_PARAMETER {base} RTSP/1.0\r\nCSeq: {cam.cseq}\r\n"
                                  f"Session: {cam.session}\r\n\r\n").encode())
                last_keepalive = time.time()
            try:
                item = cam.read_interleaved()
            except socket.timeout:
                continue
            if not item or item[0] != 0:
                continue
            marker, payload = rtp_payload(item[1])
            current += payload
            if marker:
                docs.append(current)
                out.write(current + b"\n")
                print(summarize(current))
                current = b""
    try:
        cam.request("TEARDOWN", base)
    except Exception:
        pass
    print(f"\n{len(docs)} metadata documents; raw XML saved to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
