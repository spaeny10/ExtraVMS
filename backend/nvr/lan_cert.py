"""Trusted HTTPS on the LAN port: a Let's Encrypt certificate for *.<label>.lan.axiomvision.ai, obtained by the hub.

The hub answers <a-b-c-d>.<label>.lan.axiomvision.ai with the private address a.b.c.d (hub/hub/lan_dns.py), so a hub
page on this server's LAN can fetch media from https://192-168-1-5.<label>.lan.axiomvision.ai:8443 with a certificate
the browser trusts (no "accept the certificate" step, no "Not secure"). One wildcard covers every LAN address this
server gets. The key is made here and never leaves this machine: the hub only sees the CSR and sends back the chain.

  hub welcome  {"lan_cert": {"name": "<label>.<zone>"}}   (hubs with HUB_LAN_CERTS on; hub_agent.py)
  needs()      no valid certificate for that name, or it is due: min(30 days, a third of its lifetime) before it ends
  csr()        key in lan.key.new (made once per order, 0600), CSR for exactly *.<name>
  install()    the chain the hub returns (lan_cert frame): its leaf must be for that key and name; then lan.key.new
               becomes lan.key and the chain lan.crt, next to the self-signed cert.pem/key.pem (direct.py)

The HTTPS listener (api._https_server) keeps the self-signed certificate as its default and swaps in this one per
connection (sni_callback) when the browser asks for a name under the certificate's suffix: a renewed certificate is
picked up at the next connection, no restart. Connections by IP address (no SNI) get the self-signed one as before.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import os
import re
import ssl
import time
from pathlib import Path

from .config import settings

log = logging.getLogger("nvr.lan_cert")

RENEW_BEFORE_S = 30 * 86400.0
_IPV4_LABEL = re.compile(r"^(?:0|[1-9][0-9]{0,2})(?:-(?:0|[1-9][0-9]{0,2})){3}$")

_info_cache: tuple[tuple, dict | None] = ((), None)
_ctx_cache: tuple[tuple, ssl.SSLContext | None] = ((), None)


def paths() -> tuple[Path, Path, Path]:
    """(lan.crt, lan.key, lan.key.new) in data_dir/tls."""
    d = settings.data_dir / "tls"
    return d / "lan.crt", d / "lan.key", d / "lan.key.new"


def _restrict(p: Path) -> None:
    try:
        p.chmod(0o600)   # no-op on Windows; on Linux the service user alone reads it
    except OSError:
        pass


def _write_private(p: Path, data: bytes) -> None:
    """A key file created 0600 from the start (no moment in which another user could read it)."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    _restrict(tmp)
    os.replace(tmp, p)


def _mtimes(*ps: Path) -> tuple:
    out = []
    for p in ps:
        try:
            st = p.stat()
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def _wildcard_base(cert) -> str | None:
    from cryptography import x509
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        return None
    for n in names:
        if n.startswith("*.") and n.count(".") >= 3:
            return n[2:].lower().rstrip(".")
    return None


def current() -> dict | None:
    """The installed certificate: {"name": its wildcard's base, "not_before", "not_after"} (unix seconds), or None.
    Cached until lan.crt changes."""
    global _info_cache
    crt, _, _ = paths()
    key = _mtimes(crt)
    if _info_cache[0] == key:
        return _info_cache[1]
    info = None
    try:
        from cryptography import x509
        leaf = x509.load_pem_x509_certificates(crt.read_bytes())[0]
        name = _wildcard_base(leaf)
        if name:
            info = {"name": name, "not_before": leaf.not_valid_before_utc.timestamp(), "not_after": leaf.not_valid_after_utc.timestamp()}
    except (OSError, ValueError, IndexError, ImportError):
        info = None
    _info_cache = (key, info)
    return info


def renew_at(info: dict) -> float:
    """min(30 days, a third of the lifetime) before the end: 90-day certificates at 30 days left, 6-day ones at 2."""
    return info["not_after"] - min(RENEW_BEFORE_S, max(0.0, info["not_after"] - info["not_before"]) / 3)


def valid(name: str | None = None, now: float | None = None) -> dict | None:
    """The installed certificate when it is unexpired (and for `name`, when given)."""
    info = current()
    now = time.time() if now is None else now
    if not info or not info["not_before"] <= now < info["not_after"]:
        return None
    if name is not None and info["name"] != name.lower():
        return None
    return info


def needs(name: str | None, now: float | None = None) -> bool:
    """Should this server ask the hub for a certificate for `name` (the hub's welcome) now?"""
    if not name or not settings.direct_enabled:
        return False
    now = time.time() if now is None else now
    info = valid(name, now)
    return info is None or now >= renew_at(info)


def csr(name: str) -> str:
    """A CSR (PEM) for exactly *.<name>, signed with the pending key (lan.key.new, made here when missing: an order
    that comes back after a reconnect still matches it)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    _, _, new = paths()
    key = None
    try:
        key = serialization.load_pem_private_key(new.read_bytes(), password=None)
    except (OSError, ValueError, TypeError):
        pass
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        key = ec.generate_private_key(ec.SECP256R1())
        _write_private(new, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
    wild = f"*.{name.lower()}"
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, wild)] if len(wild) <= 64 else [])
    req = (x509.CertificateSigningRequestBuilder().subject_name(subject)
           .add_extension(x509.SubjectAlternativeName([x509.DNSName(wild)]), critical=False)
           .sign(key, hashes.SHA256()))
    return req.public_bytes(serialization.Encoding.PEM).decode("ascii")


def install(fullchain: str, name: str, now: float | None = None) -> dict:
    """Store the chain the hub sent: its leaf must be unexpired, for *.<name> and for the pending key (or, a chain
    delivered again, for the installed one). ValueError otherwise; nothing is changed then."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    crt, key_path, new = paths()
    now = time.time() if now is None else now
    try:
        chain = x509.load_pem_x509_certificates(fullchain.encode("ascii"))
    except (ValueError, UnicodeError, AttributeError) as e:
        raise ValueError("not a PEM certificate chain") from e
    leaf = chain[0]
    if _wildcard_base(leaf) != name.lower():
        raise ValueError(f"the certificate is not for *.{name}")
    if not leaf.not_valid_before_utc.timestamp() <= now < leaf.not_valid_after_utc.timestamp():
        raise ValueError("the certificate is not valid now")
    spki = lambda k: k.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)  # noqa: E731
    want = spki(leaf.public_key())
    for candidate, pending in ((new, True), (key_path, False)):
        try:
            k = serialization.load_pem_private_key(candidate.read_bytes(), password=None)
        except (OSError, ValueError, TypeError):
            continue
        if spki(k.public_key()) != want:
            continue
        crt.parent.mkdir(parents=True, exist_ok=True)
        tmp = crt.with_name(crt.name + ".tmp")
        tmp.write_bytes("".join(c.public_bytes(serialization.Encoding.PEM).decode("ascii") for c in chain).encode("ascii"))
        if pending:
            os.replace(new, key_path)   # a moment where key and chain differ: the listener keeps its last good pair
            _restrict(key_path)
        os.replace(tmp, crt)
        info = current()
        log.info("direct: installed the LAN certificate for *.%s (until %s)", name,
                 dt.datetime.fromtimestamp(info["not_after"], dt.timezone.utc).date() if info else "?")
        return info or {}
    raise ValueError("the certificate is not for this server's key")


# ---------------------------------------------------------------- serving it

def host_suffix(now: float | None = None) -> str | None:
    """The suffix of the names this server answers to with a trusted certificate (<label>.<zone>), or None."""
    info = valid(None, now)
    return info["name"] if info else None


def is_own_name(host: str) -> bool:
    """<a-b-c-d>.<this server's suffix> (a single dashed-IPv4 label under it): one of this server's LAN names."""
    suffix = host_suffix()
    host = (host or "").lower().rstrip(".")
    if not suffix or not host.endswith("." + suffix):
        return False
    label = host[: -len(suffix) - 1]
    if not _IPV4_LABEL.match(label):
        return False
    try:
        ipaddress.IPv4Address(label.replace("-", "."))
    except ValueError:
        return False
    return True


def context() -> ssl.SSLContext | None:
    """An SSLContext with the installed certificate, rebuilt when lan.crt or lan.key changes; None without a valid
    one. A pair that doesn't load (mid-install) keeps the previous context."""
    global _ctx_cache
    crt, key, _ = paths()
    stamp = _mtimes(crt, key)
    if _ctx_cache[0] == stamp:
        return _ctx_cache[1] if valid() else None
    if not valid():
        return None
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(crt), str(key))
    except (OSError, ssl.SSLError, ValueError) as e:
        log.warning("direct: the LAN certificate does not load (%s); keeping the previous one", e)
        return _ctx_cache[1]
    _ctx_cache = (stamp, ctx)
    return ctx


def sni_callback(ssl_obj, server_name: str | None, _default_ctx) -> None:
    """ssl.SSLContext.sni_callback of the HTTPS listener: a browser asking for one of this server's LAN names gets
    the Let's Encrypt certificate; everyone else (an IP address, another name) the default self-signed one."""
    try:
        if server_name:
            suffix = host_suffix()
            if suffix and server_name.lower().rstrip(".").endswith("." + suffix):
                ctx = context()
                if ctx is not None:
                    ssl_obj.context = ctx
    except Exception:   # never fail a handshake over this: the self-signed certificate still works
        log.debug("direct: sni callback", exc_info=True)
    return None


def summary_fields() -> dict:
    """The heartbeat `direct` block's certificate fields: {"cert": "le"|"self", "lan_name_suffix", "not_after"}."""
    info = valid()
    if info:
        return {"cert": "le", "lan_name_suffix": info["name"], "not_after": round(info["not_after"])}
    return {"cert": "self", "lan_name_suffix": None, "not_after": None}
