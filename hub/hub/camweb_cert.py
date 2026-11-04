"""One Let's Encrypt wildcard certificate for every camera web page: *.<camweb_domain> (*.cam.axiomvision.ai).

Before this, Caddy got a certificate per camera name on demand (camweb.py's tls-ok). Every camera opened for the first
time was a new certificate, and Let's Encrypt allows 50 a week for axiomvision.ai, shared with the servers' LAN
certificates: at 2,000 servers with ~20 cameras each, one install day could use the week up. One wildcard covers
every camera label (they are one label deep: c<12 base32>.cam.axiomvision.ai) and renews as one certificate.

How it is obtained (DNS-01, through the hub's own DNS like the LAN certificates, lan_certs.py):
  * cam.axiomvision.ai's DNS is at the registrar, so one record there hands its challenge to the hub:
        _acme-challenge.cam.axiomvision.ai.  CNAME  _acme-challenge._camweb.lan.axiomvision.ai.
    Let's Encrypt follows the CNAME; `python -m hub dns` answers the TXT (lan_dns.py; "_camweb" can never be a
    server's label, labels have no "_").
  * The hub checks that record before every order (delegation_ok) and orders nothing until it is there: it logs, at
    most once a day, the record to add. So this is on whenever LAN certificates are (settings.camweb_wildcard).
  * The key is made here (the hub terminates camera pages anyway) and kept with the chain in kv (camweb:wildcard);
    renewal at lan_certs.renew_at (30 days before a 90-day certificate ends), failures back off 1 h ... 24 h.

How Caddy uses it: `get_certificate http http://hub:8000/api/camweb/cert` on the *.cam site. Caddy asks at each TLS
handshake it has nothing cached for (it doesn't cache these), so a renewed certificate is used at once. 204 = none
here yet, and Caddy carries on as before: on-demand issuance gated by tls-ok. The answer holds the private key, so it
is given only to Caddy's own request on the compose network: Host exactly hub:8000 and no X-Forwarded-* (every request
Caddy proxies from outside carries X-Forwarded-For, and its Host is a public name).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Callable

import sqlalchemy as sa
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Request, Response

from . import db, lan_certs, lan_dns
from .config import settings

log = logging.getLogger("hub.camweb_cert")

KV_KEY = "camweb:wildcard"
LABEL = lan_dns.CAMWEB_LABEL      # the challenge's place in the LAN zone; never a server label (labels have no "_")
CHECK_S = 3600.0
HINT_EVERY_S = 86400.0
MIN_LEFT_S = 3600.0               # a certificate this close to its end isn't handed out (Caddy falls back to on-demand)
RESOLVERS = ("1.1.1.1", "8.8.8.8")

router = APIRouter()
_state: dict | None = None        # the kv row, read once and kept in step with every save
_hint_at = 0.0


# ---------------------------------------------------------------- names

def domain() -> str:
    from . import camweb   # camweb imports agents; only needed at call time
    return camweb.domain()


def challenge_target() -> str:
    """Where the registrar's CNAME points: _acme-challenge._camweb.<lan zone>."""
    return f"{lan_dns.CHALLENGE}.{LABEL}.{lan_dns.zone()}"


def challenge_name() -> str:
    """What Let's Encrypt looks up for *.<domain>."""
    return f"{lan_dns.CHALLENGE}.{domain()}"


def enabled() -> bool:
    d = domain()
    return bool(settings.camweb_wildcard and settings.lan_certs and lan_dns.zone() and "." in d and not d.endswith(".localhost"))


# ---------------------------------------------------------------- state

def state() -> dict:
    global _state
    if _state is None:
        row = db.one(sa.select(db.kv).where(db.kv.c.key == KV_KEY))
        _state = dict(row["value"]) if row and isinstance(row["value"], dict) else {}
    return _state


def _save(**values) -> None:
    global _state
    st = {**state(), **values, "updated_at": time.time()}
    with db.engine().begin() as c:
        c.execute(sa.delete(db.kv).where(db.kv.c.key == KV_KEY))
        c.execute(db.kv.insert().values(key=KV_KEY, value=st))
    _state = st


def reset() -> None:
    """Forget the in-memory copy (tests; the next state() reads kv again)."""
    global _state, _hint_at
    _state, _hint_at = None, 0.0


def due(now: float) -> bool:
    st = state()
    if not st.get("fullchain") or st.get("domain") != domain():
        return True
    return now >= lan_certs.renew_at(st.get("not_before") or st["not_after"] - 90 * lan_certs.DAY, st["not_after"])


def bundle_for(server_name: str, now: float | None = None) -> str | None:
    """The chain and key (PEM) for a camera name, or None (not under the domain, none yet, or about to end)."""
    now = time.time() if now is None else now
    d = domain()
    name = (server_name or "").lower().rstrip(".")
    if not name.endswith("." + d) or "." in name[: -len(d) - 1] or not name[: -len(d) - 1]:
        return None       # the wildcard covers exactly one label below the domain
    st = state()
    if not st.get("fullchain") or not st.get("key_pem") or st.get("domain") != d or now > st["not_after"] - MIN_LEFT_S:
        return None
    return st["fullchain"].rstrip("\n") + "\n" + st["key_pem"]


# ---------------------------------------------------------------- the registrar's CNAME

def cname_of(name: str) -> str | None:
    """The CNAME target of a name, asked of public resolvers (what Let's Encrypt will see), or None."""
    import dns.resolver
    r = dns.resolver.Resolver(configure=False)
    r.nameservers = list(RESOLVERS)
    r.lifetime = 8.0
    try:
        ans = r.resolve(name, "CNAME")
    except Exception:   # NXDOMAIN, no CNAME, timeout: not delegated (yet)
        return None
    return ans[0].target.to_text(omit_final_dot=True).lower() if len(ans) else None


def delegation_ok(now: float) -> bool:
    global _hint_at
    if cname_of(challenge_name()) == challenge_target():
        return True
    if now - _hint_at >= HINT_EVERY_S:
        _hint_at = now
        log.warning("camera pages certificate: waiting for the DNS record at the registrar: %s. CNAME %s. "
                    "(until then each camera page gets its own certificate, as before)", challenge_name(), challenge_target())
    return False


# ---------------------------------------------------------------- ordering

def new_key_and_csr(name: str) -> tuple[str, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, name)]))
           .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
           .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode("ascii")
    return key_pem, csr.public_bytes(serialization.Encoding.PEM)


def _mapped(fn: Callable[[str, str], None]) -> Callable[[str, str], None]:
    """Publish / unpublish the challenge at the CNAME's target instead of the name Let's Encrypt starts from."""
    want = challenge_name()

    def call(name: str, value: str) -> None:
        name = name.lower().rstrip(".")
        fn(challenge_target() if name == want else name, value)
    return call


def order_once(now: float | None = None) -> bool:
    """Order the wildcard (blocking: run it in a thread). True when a certificate was stored."""
    now = time.time() if now is None else now
    name = f"*.{domain()}"
    key_pem, csr = new_key_and_csr(name)
    try:
        chain = lan_certs.acme_backend().order(csr, _mapped(lan_certs.publish), _mapped(lan_certs.unpublish))
        leaf = x509.load_pem_x509_certificates(chain.encode("ascii"))[0]
        sans = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
        if [s.lower() for s in sans] != [name]:
            raise RuntimeError(f"the issued certificate names {sans}, not {name}")
        nb, na = leaf.not_valid_before_utc.timestamp(), leaf.not_valid_after_utc.timestamp()
        _save(domain=domain(), key_pem=key_pem, fullchain=chain, not_before=nb, not_after=na, issued_at=time.time(),
              failures=0, last_error=None, next_order_at=None)
        log.info("camera pages certificate: issued %s until %s", name, dt.datetime.fromtimestamp(na, dt.timezone.utc).date())
        return True
    except Exception as e:
        n = int(state().get("failures") or 0) + 1
        wait = lan_certs.BACKOFF_S[min(n, len(lan_certs.BACKOFF_S)) - 1]
        reason = (str(e) or type(e).__name__).replace("\n", " ")[:300]
        _save(failures=n, last_error=reason, next_order_at=time.time() + wait)
        log.warning("camera pages certificate: order failed (%d in a row, next try in %.0f h): %s", n, wait / 3600, reason)
        return False


def check(now: float | None = None) -> str:
    """One pass (blocking). Returns what it did: off | fresh | waiting | no-delegation | ordered | failed."""
    now = time.time() if now is None else now
    if not enabled():
        return "off"
    if not due(now):
        return "fresh"
    nxt = state().get("next_order_at")
    if nxt and now < nxt:
        return "waiting"
    if not delegation_ok(now):
        return "no-delegation"
    return "ordered" if order_once(now) else "failed"


async def run() -> None:
    """Every hour: order or renew when due (the first pass shortly after the hub starts)."""
    await asyncio.sleep(30)
    while True:
        try:
            await asyncio.to_thread(check)
        except Exception:
            log.exception("camera pages certificate check")
        await asyncio.sleep(CHECK_S)


# ---------------------------------------------------------------- Caddy's certificate manager

def _from_caddy(request: Request) -> bool:
    """Caddy's own get_certificate request on the compose network, never anything it proxies from outside."""
    h = request.headers
    if (h.get("host") or "").lower() != settings.camweb_cert_host.lower():
        return False
    return not any(k in h for k in ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded"))


@router.get("/api/camweb/cert", include_in_schema=False)
async def cert_for_caddy(request: Request, server_name: str = "") -> Response:
    if not _from_caddy(request):
        return Response(status_code=404)
    pem = bundle_for(server_name) if enabled() else None
    if pem is None:
        return Response(status_code=204)   # Caddy carries on without us: on-demand issuance gated by tls-ok
    return Response(pem, media_type="application/x-pem-file", headers={"Cache-Control": "no-store"})
