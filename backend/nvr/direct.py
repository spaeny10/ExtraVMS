"""Direct-on-LAN: a browser that can reach this server on its own LAN fetches media straight from it instead of
through the hub tunnel (which is capped by the site's upload, ~18 Mbit/s here).

The hub authorises that with a short-lived token it mints (hub/hub/direct.py); this module checks it offline:

    token   = b64url(payload_json) + "." + b64url(HMAC_SHA256(key, payload_b64_ascii))   (b64url without "=" padding)
    payload = {"sid": hub site id, "uid": user id, "email": str, "role": viewer|operator|admin|owner, "exp": unix}
    key     = sha256(hub device token).hexdigest() as UTF-8 bytes

The hub stores only that hash of the device token and this server holds the token itself, so both derive the
same key and no new secret is shared or rotated. After a token rotation, tokens signed with the old key are
refused until the hub mints new ones (the UI then falls back to the hub). Tokens are bearer credentials: they
are never logged or echoed.

Also here: the allowed CORS origins, the self-signed certificate for the LAN HTTPS port, and the `direct`
block of the heartbeat summary.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import subprocess
import time
import urllib.parse
from pathlib import Path

from .config import settings
from .db import db

log = logging.getLogger("nvr.direct")

ROLES = ("viewer", "operator", "admin", "owner")
COOKIE = "direct"
# Origins of the hub UI in development (vite dev server and the hub's own uvicorn); the production origin comes
# from the hub URL. Deliberately not "the LAN origin of the request" or a wildcard: CORS with credentials to
# any page would let any site on the internet read this server through a viewer's browser.
DEV_ORIGINS = ("http://localhost:8000", "http://localhost:5174")


# ---------------------------------------------------------------- tokens

def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _key(device_token: str) -> bytes:
    return hashlib.sha256(device_token.encode()).hexdigest().encode()


def sign(payload: dict, device_token: str) -> str:
    """Mint a token the way the hub does (tests, and a local tool if one is ever needed)."""
    p = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    return f"{p}.{_b64e(hmac.new(_key(device_token), p.encode('ascii'), hashlib.sha256).digest())}"


def verify(token: str | None, now: float | None = None) -> dict | None:
    """The token's claims when it is genuine (signed with this site's current device token), unexpired, for this
    site and carries a known role; otherwise None. Never raises on garbage input."""
    if not token or len(token) > 4096:
        return None
    device_token = db.get_setting("hub_token")
    site_id = db.get_setting("hub_site_id")
    if not device_token or not site_id:  # not enrolled: no hub to vouch for anyone
        return None
    try:
        p, s = token.split(".")
        want = hmac.new(_key(device_token), p.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64d(s), want):
            return None
        claims = json.loads(_b64d(p))
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        return None
    if not isinstance(claims, dict) or not isinstance(claims.get("exp"), (int, float)) or isinstance(claims["exp"], bool):
        return None
    if claims["exp"] <= (now if now is not None else time.time()):
        return None
    if claims.get("sid") != site_id or claims.get("role") not in ROLES:
        return None
    return claims


def token_from(headers, cookies, query) -> tuple[str | None, str | None]:
    """(token, where) from the request: `Authorization: Direct <t>`, then `?direct=<t>` (for <video>/<img> src,
    which cannot set headers), then the `direct` cookie."""
    auth = headers.get("authorization") or ""
    if auth[:7].lower() == "direct ":
        return auth[7:].strip(), "header"
    if query.get("direct"):
        return query.get("direct"), "query"
    if cookies.get(COOKIE):
        return cookies.get(COOKIE), "cookie"
    return None, None


# ---------------------------------------------------------------- CORS

def hub_origin(hub_url: str | None) -> str | None:
    """wss://hub.axiomvision.ai/agent -> https://hub.axiomvision.ai (ws -> http for a dev hub)."""
    if not hub_url:
        return None
    u = urllib.parse.urlsplit(hub_url.strip())
    scheme = {"wss": "https", "ws": "http"}.get(u.scheme, u.scheme)
    if scheme not in ("http", "https") or not u.netloc:
        return None
    return f"{scheme}://{u.netloc}".lower()


def allowed_origins() -> set[str]:
    hub = hub_origin(db.get_setting("hub_url") or settings.hub_url)  # the settings table overrides .env, as in hub_agent
    return {o for o in (hub, *DEV_ORIGINS) if o}


# ---------------------------------------------------------------- LAN addresses and the certificate

def local_ipv4s() -> list[str]:
    """This machine's non-loopback, non-link-local IPv4 addresses (psutil when present: getaddrinfo(hostname)
    returns 127.0.1.1 on Debian-style hosts)."""
    found: list[str] = []
    try:
        import psutil
        for addrs in psutil.net_if_addrs().values():
            found += [a.address for a in addrs if a.family == socket.AF_INET]
    except Exception:  # psutil missing or failing: fall back to resolver + the default-route address
        try:
            found += [a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
        except OSError:
            pass
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))  # no packet is sent; this only picks the outbound interface
                found.append(s.getsockname()[0])
        except OSError:
            pass
    out: list[str] = []
    for a in found:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if not (ip.is_loopback or ip.is_link_local or ip.is_unspecified) and a not in out:
            out.append(a)
    return out


def tls_paths() -> tuple[Path, Path]:
    d = settings.data_dir / "tls"
    return d / "cert.pem", d / "key.pem"


def ensure_cert() -> tuple[Path, Path]:
    """The LAN HTTPS certificate, generated on first start (self-signed, 10 years, CN = hostname, SANs = hostname,
    each local IPv4 and localhost). Kept across restarts so a browser's accepted exception and the fingerprint
    the hub shows stay valid; delete data_dir/tls to make a new one (e.g. after the LAN address changed)."""
    cert, key = tls_paths()
    if cert.exists() and key.exists():
        return cert, key
    cert.parent.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname() or "axiom-vision"
    ips = local_ipv4s()
    try:
        _cert_cryptography(cert, key, host, ips)
    except ImportError:
        _cert_openssl(cert, key, host, ips)
    log.info("direct: generated LAN certificate for %s (%s)", host, ", ".join(ips) or "no LAN IPv4")
    return cert, key


def _cert_cryptography(cert: Path, key: Path, host: str, ips: list[str]) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    k = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host[:64])])
    sans = [x509.DNSName(host), x509.DNSName("localhost"), *(x509.IPAddress(ipaddress.ip_address(i)) for i in ips),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    now = dt.datetime.now(dt.timezone.utc)
    c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=820))
         .add_extension(x509.SubjectAlternativeName(sans), critical=False)
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
         # serverAuth: Apple platforms refuse TLS server certificates without it
         .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
         .sign(k, hashes.SHA256()))
    key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    _restrict(key)


def _cert_openssl(cert: Path, key: Path, host: str, ips: list[str]) -> None:
    san = ",".join([f"DNS:{host}", "DNS:localhost", *(f"IP:{i}" for i in ips), "IP:127.0.0.1"])
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                    "-days", "820", "-subj", f"/CN={host[:64]}", "-addext", f"subjectAltName={san}",
                    "-addext", "basicConstraints=critical,CA:FALSE", "-addext", "extendedKeyUsage=serverAuth",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True, timeout=60)
    _restrict(key)


def _restrict(key: Path) -> None:
    try:
        key.chmod(0o600)  # no-op on Windows; on Linux the service user alone reads it
    except OSError:
        pass


_fp_cache: tuple[float, str | None] = (0.0, None)


def fingerprint() -> str | None:
    """sha256 of the certificate's DER, lowercase hex: what the hub shows so a user can check the warning page."""
    global _fp_cache
    cert, _ = tls_paths()
    try:
        mtime = cert.stat().st_mtime
    except OSError:
        return None
    if _fp_cache[0] == mtime:
        return _fp_cache[1]
    try:
        import ssl
        der = ssl.PEM_cert_to_DER_cert(cert.read_text())
        fp = hashlib.sha256(der).hexdigest()
    except (OSError, ValueError):
        fp = None
    _fp_cache = (mtime, fp)
    return fp


def summary() -> dict | None:
    """Heartbeat `direct` block: where a LAN browser can reach this server, and the certificate's fingerprint.
    LAN entries are plain URL strings; the localhost hint is an object flagged local (only useful to a browser on
    this machine itself)."""
    if not settings.direct_enabled:
        return None
    urls: list = [f"https://{ip}:{settings.https_port}" for ip in local_ipv4s()]
    urls.append({"url": f"http://localhost:{settings.port}", "local": True})
    return {"urls": urls, "fingerprint": fingerprint()}
