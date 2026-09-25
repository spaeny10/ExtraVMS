"""Minimal RTSP/1.0 client over TCP-interleaved RTP (stdlib only), with Digest/Basic auth."""
from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import urllib.parse


class Rtsp:
    def __init__(self, url: str, user: str, password: str, timeout: float = 10):
        self.url = url
        self.user, self.password = user, password
        parts = urllib.parse.urlsplit(url)
        self.sock = socket.create_connection((parts.hostname, parts.port or 554), timeout=timeout)
        self.cseq = 0
        self.auth: tuple[str, dict] | None = None
        self.session: str | None = None
        self.buf = b""

    # -- auth
    def _auth_header(self, method: str, uri: str) -> str | None:
        if not self.auth:
            return None
        scheme, p = self.auth
        if scheme == "basic":
            return "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        md5 = lambda s: hashlib.md5(s.encode()).hexdigest()
        ha1 = md5(f"{self.user}:{p['realm']}:{self.password}")
        ha2 = md5(f"{method}:{uri}")
        if "qop" in p:
            nc, cnonce = "00000001", os.urandom(8).hex()
            resp = md5(f"{ha1}:{p['nonce']}:{nc}:{cnonce}:auth:{ha2}")
            extra = f', qop=auth, nc={nc}, cnonce="{cnonce}"'
        else:
            resp, extra = md5(f"{ha1}:{p['nonce']}:{ha2}"), ""
        return (f'Digest username="{self.user}", realm="{p["realm"]}", nonce="{p["nonce"]}", '
                f'uri="{uri}", response="{resp}"{extra}')

    # -- IO
    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("RTSP connection closed")
            self.buf += chunk
        head, self.buf = self.buf.split(marker, 1)
        return head

    def _read_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("RTSP connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _read_response(self) -> tuple[int, dict[str, str], bytes]:
        # skip any interleaved RTP that arrives before the response
        while True:
            first = self._read_exact(1)
            if first != b"$":
                self.buf = first + self.buf
                break
            hdr = self._read_exact(3)
            self._read_exact(int.from_bytes(hdr[1:3], "big"))
        head = self._read_until(b"\r\n\r\n").decode(errors="replace")
        lines = head.split("\r\n")
        status = int(lines[0].split()[1])
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        body = self._read_exact(int(headers.get("content-length", 0)))
        return status, headers, body

    def request(self, method: str, uri: str, headers: dict[str, str] | None = None) -> tuple[int, dict, bytes]:
        for attempt in range(2):
            self.cseq += 1
            h = {"CSeq": str(self.cseq), "User-Agent": "NewVMS-probe"}
            if self.session:
                h["Session"] = self.session
            auth = self._auth_header(method, uri)
            if auth:
                h["Authorization"] = auth
            h.update(headers or {})
            msg = f"{method} {uri} RTSP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
            self.sock.sendall(msg.encode())
            status, rh, body = self._read_response()
            if status == 401 and attempt == 0:
                www = rh.get("www-authenticate", "")
                if www.lower().startswith("digest"):
                    self.auth = ("digest", dict(re.findall(r'(\w+)="?([^",]*)"?', www[6:])))
                else:
                    self.auth = ("basic", {})
                continue
            return status, rh, body
        return status, rh, body

    def read_interleaved(self) -> tuple[int, bytes] | None:
        """Return (channel, packet); skips stray RTSP responses (e.g. keepalive replies)."""
        first = self._read_exact(1)
        if first == b"$":
            hdr = self._read_exact(3)
            return hdr[0], self._read_exact(int.from_bytes(hdr[1:3], "big"))
        # an RTSP response (e.g. keepalive reply); consume it
        self.buf = first + self.buf
        self._read_response()
        return None


def play_track(cam: Rtsp, media: str = "application") -> str:
    """DESCRIBE, SETUP the first track of `media` type on interleaved channel 0, PLAY. Returns the SDP section."""
    status, h, sdp = cam.request("DESCRIBE", cam.url, {"Accept": "application/sdp"})
    if status != 200:
        raise ConnectionError(f"DESCRIBE {status}")
    base = h.get("content-base", cam.url)
    section = next((s for s in re.split(r"\r?\nm=", sdp.decode(errors="replace"))[1:] if s.startswith(media)), None)
    if not section:
        raise LookupError(f"no {media} track in SDP")
    control = re.search(r"a=control:(\S+)", section).group(1)
    track_url = control if control.startswith("rtsp://") else base.rstrip("/") + "/" + control
    status, h, _ = cam.request("SETUP", track_url, {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"})
    if status != 200:
        raise ConnectionError(f"SETUP {status}")
    cam.session = h["session"].split(";")[0]
    cam.base = base
    status, _, _ = cam.request("PLAY", base, {"Range": "npt=0.000-"})
    if status != 200:
        raise ConnectionError(f"PLAY {status}")
    return section


def keepalive(cam: Rtsp) -> None:
    """Fire-and-forget GET_PARAMETER; the reply is consumed by read_interleaved()."""
    cam.cseq += 1
    cam.sock.sendall((f"GET_PARAMETER {getattr(cam, 'base', cam.url)} RTSP/1.0\r\nCSeq: {cam.cseq}\r\n"
                      f"Session: {cam.session}\r\n\r\n").encode())


def rtp_payload(pkt: bytes) -> tuple[bool, bytes]:
    cc = pkt[0] & 0x0F
    ext = pkt[0] & 0x10
    marker = bool(pkt[1] & 0x80)
    off = 12 + cc * 4
    if ext:
        ext_len = int.from_bytes(pkt[off + 2:off + 4], "big")
        off += 4 + ext_len * 4
    end = len(pkt)
    if pkt[0] & 0x20:  # padding
        end -= pkt[-1]
    return marker, pkt[off:end]
