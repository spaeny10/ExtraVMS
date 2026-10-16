"""Trusted HTTPS for Direct-on-LAN: a Let's Encrypt certificate per server for *.<label>.<lan_zone>.

The names (<a-b-c-d>.<label>.<zone> -> a.b.c.d) and the DNS server that answers them are in lan_dns.py. Here, the
certificates, through the server's tunnel (agents.py):

  welcome   {"lan_cert": {"name": "<label>.<zone>"}} to servers whose hello lists "lan_cert", when settings.lan_certs
  server    makes its own key (it never leaves the server) and sends {"t": "lan_csr", "csr": PEM}
  hub       validate_csr: exactly one name, *.<that server's own label>.<zone>, a sound key and signature; then
            policy(): the stored certificate when it is for this key and not due (no order), else an ACME order
            (DNS-01: the TXT goes in lan_challenges, which `python -m hub dns` serves) unless a limit says later
  hub       {"t": "lan_cert", "status": "issued", "fullchain": PEM} | "pending" (an order runs; the chain follows on
            this tunnel, or on the server's next CSR after a reconnect) | "later" / "refused" with retry_after seconds

Limits (Let's Encrypt allows 50 certificates per registered domain per week, shared with the camera web pages):
  * one order in flight per server; renew once less than min(30 days, a third of the lifetime) is left (renew_at)
  * after an order: none for this server for REISSUE_GAP_S (a server that lost its key waits), after a failure a
    backoff of 1 h, 2 h, 4 h ... 24 h (BACKOFF_S)
  * settings.lan_cert_weekly_cap certificates per 7 days over all servers (running orders count), logged when hit
  * FAILURE_BRAKE failed orders in an hour (e.g. the delegation is broken) pause all orders for that hour

The ACME account key is made once and kept in the kv table (like the VAPID key, push.py); it is never logged. The
directory is settings.lan_acme_directory (Let's Encrypt production by default; tests point it at a fake server).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import time
from typing import Callable, Protocol

import sqlalchemy as sa
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from . import db, lan_dns
from .config import settings

log = logging.getLogger("hub.lan_certs")

DAY = 86400.0
RENEW_BEFORE_S = 30 * DAY
REISSUE_GAP_S = 2 * DAY
BACKOFF_S = (3600.0, 2 * 3600.0, 4 * 3600.0, 8 * 3600.0, 16 * 3600.0, DAY)
FAILURE_BRAKE = 10
WEEK_S = 7 * DAY
ORDER_TIMEOUT_S = 600.0
CHALLENGE_TTL_S = 1800.0
PUBLISH_WAIT_S = 5.0          # the DNS process re-reads challenges every 2 s: let it see the TXT before LE looks
MAX_CSR = 8192
ACCOUNT_KEY = "lan_acme:account"

_in_flight: set[str] = set()
_tasks: set[asyncio.Task] = set()
_cap_logged_at = 0.0


class CsrError(ValueError):
    pass


# ---------------------------------------------------------------- the offer and the CSR

def enabled() -> bool:
    return bool(settings.lan_certs and lan_dns.zone())


def welcome(conn) -> dict:
    """What the welcome frame says about LAN certificates: {"lan_cert": {"name"}} or nothing."""
    site = conn.site or {}
    name = lan_dns.name_for(conn.site_id)
    if not enabled() or not name or site.get("retired_at") or "lan_cert" not in conn.caps():
        return {}
    return {"lan_cert": {"name": name}}


def key_sha256(public_key) -> str:
    return hashlib.sha256(public_key.public_bytes(serialization.Encoding.DER,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()


def validate_csr(pem: str, server_id: str) -> x509.CertificateSigningRequest:
    """The CSR when it asks for exactly *.<this server's label>.<zone> with a sound key and a valid signature."""
    name = lan_dns.name_for(server_id)
    if not name:
        raise CsrError("this server has no LAN name")
    if not isinstance(pem, str) or len(pem) > MAX_CSR or "-----BEGIN CERTIFICATE REQUEST-----" not in pem:
        raise CsrError("not a PEM certificate request")
    try:
        csr = x509.load_pem_x509_csr(pem.encode("ascii"))
    except (ValueError, UnicodeError) as e:
        raise CsrError("unreadable certificate request") from e
    if not csr.is_signature_valid:
        raise CsrError("bad signature")
    key = csr.public_key()
    if isinstance(key, ec.EllipticCurvePublicKey):
        if key.curve.name not in ("secp256r1", "secp384r1"):
            raise CsrError("unsupported curve")
    elif isinstance(key, rsa.RSAPublicKey):
        if not 2048 <= key.key_size <= 4096:
            raise CsrError("unsupported RSA key size")
    else:
        raise CsrError("unsupported key type")
    want = f"*.{name}"
    try:
        san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound as e:
        raise CsrError("no subjectAltName") from e
    dns_names = [n.lower() for n in san.get_values_for_type(x509.DNSName)]
    if dns_names != [want] or len(list(san)) != 1:
        raise CsrError(f"the request must name exactly {want}")
    for attr in csr.subject:
        if attr.oid != x509.NameOID.COMMON_NAME or str(attr.value).lower() != want:
            raise CsrError(f"the subject may only be CN={want}")
    return csr


def renew_at(not_before: float, not_after: float) -> float:
    """When a certificate is due: min(30 days, a third of its lifetime) before it ends (90 days -> 30, 6 -> 2)."""
    return not_after - min(RENEW_BEFORE_S, max(0.0, not_after - not_before) / 3)


# ---------------------------------------------------------------- state

def _row(server_id: str) -> dict | None:
    return db.one(sa.select(db.lan_certs).where(db.lan_certs.c.server_id == server_id))


def _save(server_id: str, **values) -> None:
    values["updated_at"] = time.time()
    with db.engine().begin() as c:
        if c.execute(sa.select(db.lan_certs.c.server_id).where(db.lan_certs.c.server_id == server_id)).first() is None:
            c.execute(db.lan_certs.insert().values(**{"server_id": server_id, "failures": 0, **values}))
        else:
            c.execute(sa.update(db.lan_certs).where(db.lan_certs.c.server_id == server_id).values(**values))


def counts(now: float) -> tuple[int, int, float | None]:
    """(certificates issued or being ordered in the last 7 days, orders failed in the last hour, oldest of the former)."""
    with db.engine().connect() as c:
        week = sa.and_(db.lan_cert_orders.c.at > now - WEEK_S, sa.or_(db.lan_cert_orders.c.ok.is_(None), db.lan_cert_orders.c.ok == True))  # noqa: E712
        issued = c.execute(sa.select(sa.func.count()).select_from(db.lan_cert_orders).where(week)).scalar() or 0
        oldest = c.execute(sa.select(sa.func.min(db.lan_cert_orders.c.at)).where(week)).scalar()
        failed = c.execute(sa.select(sa.func.count()).select_from(db.lan_cert_orders).where(
            db.lan_cert_orders.c.at > now - 3600, db.lan_cert_orders.c.ok == False)).scalar() or 0  # noqa: E712
    return int(issued), int(failed), oldest


def policy(row: dict | None, key_hash: str, now: float, in_flight: bool, issued_week: int, failed_hour: int,
           oldest_week: float | None, cap: int) -> tuple[str, float]:
    """What to do with a valid CSR: ("issued", 0) = send the stored chain, ("pending", 0) = an order already runs,
    ("order", 0) = start one, ("later", seconds) = not now."""
    row = row or {}
    if row.get("fullchain") and row.get("key_sha256") == key_hash and row.get("not_after") and \
            now < renew_at(row.get("not_before") or row["not_after"] - 90 * DAY, row["not_after"]):
        return "issued", 0.0
    if in_flight:
        return "pending", 0.0
    if row.get("next_order_at") and now < row["next_order_at"]:
        return "later", row["next_order_at"] - now
    if failed_hour >= FAILURE_BRAKE:
        return "later", 3600.0
    if issued_week >= cap:
        return "later", max(3600.0, (oldest_week or now) + WEEK_S - now)
    return "order", 0.0


# ---------------------------------------------------------------- the tunnel side

async def on_csr(conn, frame: dict) -> None:
    """A server asked for its certificate. Never raises (the tunnel must stay up whatever it sent)."""
    try:
        reply = await _on_csr(conn, frame)
    except Exception:
        log.exception("lan cert: CSR from %s", conn.site_id)
        reply = {"status": "later", "retry_after": 3600, "reason": "hub error"}
    try:
        await conn.send({"t": "lan_cert", **reply})
    except Exception:
        pass


async def _on_csr(conn, frame: dict) -> dict:
    global _cap_logged_at
    sid = conn.site_id
    if not enabled() or not lan_dns.name_for(sid) or (conn.site or {}).get("retired_at"):
        return {"status": "later", "retry_after": DAY, "reason": "LAN certificates are off at this hub"}
    pem = frame.get("csr")
    try:
        csr = validate_csr(pem, sid)
    except CsrError as e:
        log.warning("lan cert: refused a certificate request from %s: %s", sid, e)
        return {"status": "refused", "retry_after": 6 * 3600, "reason": str(e)[:200]}
    kh = key_sha256(csr.public_key())
    now = time.time()
    row = _row(sid)
    issued, failed, oldest = counts(now)
    cap = max(0, int(settings.lan_cert_weekly_cap))
    what, wait = policy(row, kh, now, sid in _in_flight, issued, failed, oldest, cap)
    if what == "issued":
        return {"status": "issued", "fullchain": row["fullchain"]}
    if what == "pending":
        return {"status": "pending"}
    if what == "later":
        if issued >= cap and now - _cap_logged_at > 6 * 3600:
            _cap_logged_at = now
            log.warning("lan cert: the weekly cap of %d certificates is reached; %s and others wait (HUB_LAN_CERT_WEEKLY_CAP)", cap, sid)
        return {"status": "later", "retry_after": int(wait) + 1}
    _in_flight.add(sid)
    with db.engine().begin() as c:
        oid = c.execute(db.lan_cert_orders.insert().values(server_id=sid, at=now, ok=None)).inserted_primary_key[0]
    task = asyncio.create_task(_issue(sid, pem.encode("ascii"), kh, oid), name=f"lan-cert-{sid}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"status": "pending"}


async def _issue(sid: str, csr_pem: bytes, kh: str, order_id: int) -> None:
    """Run one ACME order in a thread, store the result, hand the chain to the server if it is connected."""
    ok = False
    try:
        chain = await asyncio.wait_for(asyncio.to_thread(acme_backend().order, csr_pem, publish, unpublish), ORDER_TIMEOUT_S + 60)
        leaf = x509.load_pem_x509_certificates(chain.encode("ascii"))[0]
        if key_sha256(leaf.public_key()) != kh:
            raise RuntimeError("the issued certificate is for another key")
        nb, na = leaf.not_valid_before_utc.timestamp(), leaf.not_valid_after_utc.timestamp()
        now = time.time()
        _save(sid, key_sha256=kh, fullchain=chain, not_before=nb, not_after=na, issued_at=now,
              next_order_at=now + REISSUE_GAP_S, failures=0, last_error=None)
        ok = True
        log.info("lan cert: issued *.%s until %s", lan_dns.name_for(sid), dt.datetime.fromtimestamp(na, dt.timezone.utc).date())
        from .agents import registry   # agents imports this module
        conn = registry.get(sid)
        if conn is not None:
            try:
                await conn.send({"t": "lan_cert", "status": "issued", "fullchain": chain})
            except Exception:
                pass   # its next CSR (after the reconnect) gets the stored chain
    except Exception as e:
        row = _row(sid) or {}
        n = int(row.get("failures") or 0) + 1
        wait = BACKOFF_S[min(n, len(BACKOFF_S)) - 1]
        reason = (str(e) or type(e).__name__).replace("\n", " ")[:300]
        _save(sid, failures=n, last_error=reason, next_order_at=time.time() + wait)
        log.warning("lan cert: order for %s failed (%d in a row, next try in %.0f h): %s", sid, n, wait / 3600, reason)
    finally:
        _in_flight.discard(sid)
        db.run(sa.update(db.lan_cert_orders).where(db.lan_cert_orders.c.id == order_id).values(ok=ok))


# ---------------------------------------------------------------- DNS-01 values

def publish(name: str, value: str) -> None:
    name = name.lower().rstrip(".")
    with db.engine().begin() as c:
        c.execute(sa.delete(db.lan_challenges).where(db.lan_challenges.c.name == name, db.lan_challenges.c.value == value))
        c.execute(db.lan_challenges.insert().values(name=name, value=value, expires_at=time.time() + CHALLENGE_TTL_S))


def unpublish(name: str, value: str) -> None:
    db.run(sa.delete(db.lan_challenges).where(db.lan_challenges.c.name == name.lower().rstrip("."), db.lan_challenges.c.value == value))


def sweep() -> None:
    """Challenges an interrupted order left behind (hub restart mid-order), and order rows older than the window."""
    now = time.time()
    db.run(sa.delete(db.lan_challenges).where(db.lan_challenges.c.expires_at < now))
    db.run(sa.delete(db.lan_cert_orders).where(db.lan_cert_orders.c.at < now - 2 * WEEK_S))


# ---------------------------------------------------------------- ACME

class AcmeBackend(Protocol):
    def order(self, csr_pem: bytes, publish: Callable[[str, str], None], unpublish: Callable[[str, str], None]) -> str:
        """Obtain a certificate for the CSR (blocking); returns the full chain PEM, leaf first."""
        ...


_backend: AcmeBackend | None = None


def acme_backend() -> AcmeBackend:
    global _backend
    if _backend is None:
        _backend = LetsEncrypt(settings.lan_acme_directory, settings.lan_acme_email)
    return _backend


def set_backend(b: AcmeBackend | None) -> None:
    """Tests swap in a fake; None = the configured directory again."""
    global _backend
    _backend = b


class LetsEncrypt:
    """ACME v2 with the certbot project's `acme` client (JWS, nonces, polling). Blocking: run it in a thread."""

    def __init__(self, directory: str, email: str = "") -> None:
        self.directory = directory
        self.email = (email or "").strip()

    def _account(self):
        """(client, account JWK): the kv account for this directory, registered on first use."""
        import josepy as jose
        from acme import client, errors, messages

        from . import __version__
        row = db.one(sa.select(db.kv).where(db.kv.c.key == ACCOUNT_KEY))
        acct = row["value"] if row and isinstance(row["value"], dict) and row["value"].get("directory") == self.directory else None
        if acct is None:   # first use, or another directory (staging <-> production): a new account
            k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            acct = {"directory": self.directory, "uri": None, "created_at": time.time(),
                    "key_pem": k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()).decode()}
        jwk = jose.JWKRSA(key=serialization.load_pem_private_key(acct["key_pem"].encode(), password=None))
        net = client.ClientNetwork(jwk, user_agent=f"axiom-vision-hub/{__version__}", timeout=30)
        acme = client.ClientV2(client.ClientV2.get_directory(self.directory, net), net=net)
        if acct.get("uri"):
            net.account = messages.RegistrationResource(uri=acct["uri"], body=messages.Registration())
        else:
            try:
                regr = acme.new_account(messages.NewRegistration.from_data(email=self.email or None, terms_of_service_agreed=True))
            except errors.ConflictError as e:   # the key is registered already (an earlier run that didn't save the uri)
                regr = messages.RegistrationResource(uri=e.location, body=messages.Registration())
                net.account = regr
            acct["uri"] = regr.uri
            with db.engine().begin() as c:
                c.execute(sa.delete(db.kv).where(db.kv.c.key == ACCOUNT_KEY))
                c.execute(db.kv.insert().values(key=ACCOUNT_KEY, value=acct))
            log.info("lan cert: ACME account registered at %s", self.directory)
        return acme, jwk

    def order(self, csr_pem: bytes, publish: Callable[[str, str], None], unpublish: Callable[[str, str], None]) -> str:
        from acme import challenges, messages
        acme, jwk = self._account()
        orderr = acme.new_order(csr_pem)
        published: list[tuple[str, str]] = []
        try:
            answers = []
            for authz in orderr.authorizations:
                if authz.body.status == messages.STATUS_VALID:
                    continue   # validated recently for this account: nothing to prove
                ch = next((c for c in authz.body.challenges if isinstance(c.chall, challenges.DNS01)), None)
                if ch is None:
                    raise RuntimeError("the ACME server offered no dns-01 challenge")
                name = f"_acme-challenge.{authz.body.identifier.value}"   # a wildcard's authorization names its base
                value = ch.chall.validation(jwk)
                publish(name, value)
                published.append((name, value))
                answers.append((ch, ch.chall.response(jwk)))
            if answers and PUBLISH_WAIT_S:
                time.sleep(PUBLISH_WAIT_S)
            for ch, resp in answers:
                acme.answer_challenge(ch, resp)
            done = acme.poll_and_finalize(orderr, deadline=dt.datetime.now() + dt.timedelta(seconds=ORDER_TIMEOUT_S))
            return done.fullchain_pem
        finally:
            for name, value in published:
                try:
                    unpublish(name, value)
                except Exception:
                    pass   # expires_at clears it (sweep)
