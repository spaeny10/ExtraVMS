"""Trusted Direct-on-LAN certificate (lan_cert.py): key + CSR, installing the hub's chain, renewal timing, the
heartbeat fields, the Host allow-list for this server's own names, the HTTPS listener swapping certificates per SNI
(and picking up a renewal without a restart), and the agent's lan_cert frames. A test CA stands in for Let's Encrypt.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_lan_cert.py   (from backend/)
"""
import asyncio
import datetime as dt
import os
import socket
import ssl
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-lancert-test-")   # never the real DB or its certificates
os.environ["NVR_RECORDINGS_DIR"] = tempfile.mkdtemp(prefix="nvr-lancert-rec-")
os.environ["NVR_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="nvr-lancert-runtime-")
os.environ["NVR_HUB_URL"] = "wss://hub.axiomvision.ai/agent"
os.environ.setdefault("NVR_ALLOWED_HOSTS", "lan")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tunnelproto"))

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from nvr import api, direct, hub_agent, lan_cert, lan_guard  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402

NAME = "s-d6d429b617ca.lan.axiomvision.ai"
DAY = 86400.0

CA_KEY = ec.generate_private_key(ec.SECP256R1())
_ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test LAN CA")])
_now = dt.datetime.now(dt.timezone.utc)
CA = (x509.CertificateBuilder().subject_name(_ca_name).issuer_name(_ca_name).public_key(CA_KEY.public_key())
      .serial_number(1).not_valid_before(_now - dt.timedelta(days=1)).not_valid_after(_now + dt.timedelta(days=3650))
      .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(CA_KEY, hashes.SHA256()))
CA_FILE = Path(tempfile.mkdtemp()) / "ca.pem"
CA_FILE.write_bytes(CA.public_bytes(serialization.Encoding.PEM))


def issue(csr_pem: str, days: float = 90, age_days: float = 0, names: list[str] | None = None) -> str:
    """What the hub's ACME order returns: leaf (for the CSR's key) + CA."""
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    sans = names or csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    nb = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=age_days, hours=1)
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([])).issuer_name(CA.subject).public_key(csr.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(nb).not_valid_after(nb + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in sans]), critical=False)
            .sign(CA_KEY, hashes.SHA256()))
    return (leaf.public_bytes(serialization.Encoding.PEM) + CA.public_bytes(serialization.Encoding.PEM)).decode()


def reset() -> None:
    for p in lan_cert.paths():
        p.unlink(missing_ok=True)


def test_key_and_csr():
    reset()
    pem = lan_cert.csr(NAME)
    csr = x509.load_pem_x509_csr(pem.encode())
    assert csr.is_signature_valid
    assert csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName) == [f"*.{NAME}"]
    assert csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == f"*.{NAME}"
    _, key, new = lan_cert.paths()
    assert new.exists() and not key.exists()
    if os.name == "posix":
        assert new.stat().st_mode & 0o777 == 0o600
    # the pending key is reused until a chain arrives (an answer after a reconnect still fits)
    again = x509.load_pem_x509_csr(lan_cert.csr(NAME).encode())
    assert again.public_key().public_numbers() == csr.public_key().public_numbers()
    assert not list(new.parent.glob("*.tmp"))


def test_install_checks_name_key_and_dates():
    reset()
    pem = lan_cert.csr(NAME)
    other_key_csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([])).add_extension(
        x509.SubjectAlternativeName([x509.DNSName(f"*.{NAME}")]), critical=False).sign(ec.generate_private_key(ec.SECP256R1()), hashes.SHA256())
    for bad, why in ((issue(pem, names=["*.s-other0000001.lan.axiomvision.ai"]), "other name"),
                     (issue(other_key_csr.public_bytes(serialization.Encoding.PEM).decode()), "other key"),
                     (issue(pem, days=10, age_days=20), "expired"),
                     ("junk", "not PEM")):
        try:
            lan_cert.install(bad, NAME)
            raise AssertionError(f"installed: {why}")
        except ValueError:
            pass
    crt, key, new = lan_cert.paths()
    assert not crt.exists() and not key.exists() and new.exists()
    chain = issue(pem)
    info = lan_cert.install(chain, NAME)
    assert info["name"] == NAME and info["not_after"] > time.time() + 89 * DAY
    assert crt.exists() and key.exists() and not new.exists()
    if os.name == "posix":
        assert key.stat().st_mode & 0o777 == 0o600
    assert lan_cert.install(chain, NAME)["name"] == NAME   # the same chain delivered again: fine (installed key)


def test_needs_and_renewal():
    reset()
    now = time.time()
    assert lan_cert.needs(NAME, now)                       # nothing installed
    assert not lan_cert.needs(None, now)                   # the hub offered nothing
    lan_cert.install(issue(lan_cert.csr(NAME), days=90, age_days=50), NAME)
    assert not lan_cert.needs(NAME, now)                   # 40 days left
    assert lan_cert.needs(NAME, now + 11 * DAY)            # 29 days left: due
    assert lan_cert.needs("s-other0000001.lan.axiomvision.ai", now)   # the hub names another suffix now
    settings.direct_enabled = False
    try:
        assert not lan_cert.needs(NAME, now + 11 * DAY)
    finally:
        settings.direct_enabled = True
    lan_cert.install(issue(lan_cert.csr(NAME), days=6), NAME)   # short-lived: renewed with a third left
    assert not lan_cert.needs(NAME, now + 3.5 * DAY) and lan_cert.needs(NAME, now + 4.1 * DAY)


def test_summary_fields():
    reset()
    assert lan_cert.summary_fields() == {"cert": "self", "lan_name_suffix": None, "not_after": None}
    assert direct.summary()["cert"] == "self"
    lan_cert.install(issue(lan_cert.csr(NAME)), NAME)
    f = lan_cert.summary_fields()
    assert f["cert"] == "le" and f["lan_name_suffix"] == NAME and f["not_after"] > time.time() + 89 * DAY
    s = direct.summary()
    assert {k: s[k] for k in ("cert", "lan_name_suffix", "not_after")} == f and s["urls"] and "fingerprint" in s


def test_host_allow_list():
    reset()
    own = f"192-168-1-5.{NAME}"
    assert not lan_guard.host_allowed(own)                 # no certificate yet: not a name of ours
    lan_cert.install(issue(lan_cert.csr(NAME)), NAME)
    assert lan_guard.host_allowed(own) and lan_guard.host_allowed(f"{own}:8443") and lan_guard.host_allowed(f"10-0-0-7.{NAME.upper()}")
    for bad in (f"192-168-1-5.s-other0000001.lan.axiomvision.ai", NAME, f"x.192-168-1-5.{NAME}", f"evil.{NAME}",
                f"192-168-01-5.{NAME}", f"999-1-1-1.{NAME}", f"192-168-1-5.{NAME}.evil.com", f"192-168-1-5{NAME}"):
        assert not lan_guard.host_allowed(bad), bad


def _peer(port: int, sni: str | None, verify: bool = False) -> tuple[bytes, bytes]:
    """(the certificate the listener presented, the answer to GET /api/direct/probe with Host = sni)."""
    ctx = ssl.create_default_context(cafile=str(CA_FILE)) if verify else ssl._create_unverified_context()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with ctx.wrap_socket(raw, server_hostname=sni) as s:
            der = s.getpeercert(binary_form=True)
            s.sendall(f"GET /api/direct/probe HTTP/1.1\r\nHost: {sni or '127.0.0.1'}:{port}\r\nConnection: close\r\n\r\n".encode())
            data = b""
            while chunk := s.recv(4096):
                data += chunk
    return der, data.split(b"\r\n", 1)[0]


def _leaf_der() -> bytes:
    return x509.load_pem_x509_certificates(lan_cert.paths()[0].read_bytes())[0].public_bytes(serialization.Encoding.DER)


def test_https_listener_swaps_certificates_per_sni():
    reset()
    self_cert, _ = direct.ensure_cert()
    self_der = ssl.PEM_cert_to_DER_cert(self_cert.read_text())
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    old = settings.https_port, settings.host
    settings.https_port, settings.host = port, "127.0.0.1"
    name = f"192-168-1-5.{NAME}"
    try:
        server = api._https_server()

        async def go():
            task = asyncio.create_task(api._serve_https(server))
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.05)
            try:
                # no LAN certificate yet: everyone gets the self-signed one
                der, _ = await asyncio.to_thread(_peer, port, name)
                assert der == self_der
                lan_cert.install(issue(lan_cert.csr(NAME)), NAME)
                first = _leaf_der()
                # the LAN name: the trusted certificate, verified against the CA and the name; the Host is accepted
                der, status = await asyncio.to_thread(_peer, port, name, True)
                assert der == first and b" 204" in status
                # by IP address (no SNI) or another name: the self-signed one, as before
                der, _ = await asyncio.to_thread(_peer, port, None)
                assert der == self_der
                der, _ = await asyncio.to_thread(_peer, port, "192-168-1-5.s-other0000001.lan.axiomvision.ai")
                assert der == self_der
                # renewed (new key): the next connection gets it, no restart
                await asyncio.sleep(0.05)   # a different mtime even on coarse clocks
                lan_cert.install(issue(lan_cert.csr(NAME)), NAME)
                assert _leaf_der() != first
                der, status = await asyncio.to_thread(_peer, port, name, True)
                assert der == _leaf_der() and b" 204" in status
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, 10)

        asyncio.run(go())
    finally:
        settings.https_port, settings.host = old


def test_broken_pair_keeps_the_last_good_context():
    reset()
    lan_cert.install(issue(lan_cert.csr(NAME)), NAME)
    good = lan_cert.context()
    assert good is not None
    crt, key, _ = lan_cert.paths()
    k = ec.generate_private_key(ec.SECP256R1())   # a key that doesn't match the chain (an install caught midway)
    key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    assert lan_cert.context() is good


class FakeAgent(hub_agent.HubAgent):
    def __init__(self):
        super().__init__(None, SimpleNamespace())
        self.sent: list[dict] = []

    async def _send(self, frame: dict) -> None:
        self.sent.append(frame)


def test_agent_frames():
    reset()
    db.set_setting("hub_lan_retry_at", None)
    a = FakeAgent()

    async def go():
        assert not await a.lan_cert_check()                  # no offer from this hub
        a._lan_name = NAME
        assert await a.lan_cert_check()
        assert a.sent[-1]["t"] == "lan_csr" and "CERTIFICATE REQUEST" in a.sent[-1]["csr"]
        assert not await a.lan_cert_check()                  # not again within LAN_CERT_MIN_ASK_S
        await a._on_message({"t": "lan_cert", "status": "pending"})
        await a._on_message({"t": "lan_cert", "status": "issued", "fullchain": issue(a.sent[-1]["csr"])})
        assert lan_cert.summary_fields()["cert"] == "le"
        assert not await a.lan_cert_check(time.time() + hub_agent.LAN_CERT_MIN_ASK_S + 1)   # fresh: nothing to ask
        # later: no CSR before retry_after, even when one is due
        reset()
        await a._on_message({"t": "lan_cert", "status": "later", "retry_after": 7200})
        now = time.time() + hub_agent.LAN_CERT_MIN_ASK_S + 1
        assert not await a.lan_cert_check(now)
        assert await a.lan_cert_check(now + 7200)
        # a chain that isn't ours is not installed
        await a._on_message({"t": "lan_cert", "status": "issued", "fullchain": "junk"})
        assert lan_cert.summary_fields()["cert"] == "self"
        await a._on_message({"t": "lan_cert", "status": "refused", "retry_after": 5, "reason": "x"})
        assert float(db.get_setting("hub_lan_retry_at")) >= time.time() + 590   # at least 10 minutes

    asyncio.run(go())
    db.set_setting("hub_lan_retry_at", None)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
