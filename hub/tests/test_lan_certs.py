"""Direct-on-LAN certificates (lan_certs.py): CSR checks (only the server's own wildcard), renewal timing, the order
policy (one in flight, backoff, reissue gap, weekly cap, failure brake) and a whole order through the real ACME client
against a fake ACME server on 127.0.0.1, whose DNS-01 check asks the hub's own zone (lan_dns.py) for the TXT value.
Never Let's Encrypt: conftest points HUB_LAN_ACME_DIRECTORY at a dead address, and these tests at the fake."""
import asyncio
import base64
import datetime as dt
import hashlib
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dns.message
import josepy as jose
import pytest
import sqlalchemy as sa
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

from hub import agents, db, lan_certs, lan_dns
from hub.config import settings

SID = "s_1ancert00001"
NAME = f"s-1ancert00001.{settings.lan_zone}"
WILD = f"*.{NAME}"


def make_csr(names=(WILD,), cn: str | None = WILD, key=None, extra_ip: str | None = None):
    key = key or ec.generate_private_key(ec.SECP256R1())
    sans: list = [x509.DNSName(n) for n in names]
    if extra_ip:
        import ipaddress
        sans.append(x509.IPAddress(ipaddress.ip_address(extra_ip)))
    b = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)] if cn else []))
    if sans:
        b = b.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    return b.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode(), key


# ---------------------------------------------------------------- CSR and renewal timing

def test_csr_only_for_the_servers_own_wildcard():
    pem, key = make_csr()
    assert lan_certs.validate_csr(pem, SID).public_key().public_numbers() == key.public_key().public_numbers()
    assert lan_certs.validate_csr(make_csr(cn=None)[0], SID)   # no subject at all is fine too
    assert lan_certs.validate_csr(make_csr(key=rsa.generate_private_key(public_exponent=65537, key_size=2048))[0], SID)
    bad = {
        "another server's name": make_csr((f"*.s-0ther0000001.{settings.lan_zone}",), cn=None)[0],
        "the bare name": make_csr((NAME,), cn=None)[0],
        "one address name": make_csr((f"192-168-1-5.{NAME}",), cn=None)[0],
        "an extra name": make_csr((WILD, "hub.axiomvision.ai"))[0],
        "an IP SAN": make_csr(extra_ip="192.168.1.5")[0],
        "a CN that differs": make_csr(cn="hub.axiomvision.ai")[0],
        "no SAN": make_csr(names=(), cn=WILD)[0],
        "other zone": make_csr(("*.s-1ancert00001.lan.example.com",), cn=None)[0],
        "small RSA": make_csr(key=rsa.generate_private_key(public_exponent=65537, key_size=1024))[0],
        "secp521r1": make_csr(key=ec.generate_private_key(ec.SECP521R1()))[0],
        "not PEM": "hello",
        "not a string": None,
        "huge": "-----BEGIN CERTIFICATE REQUEST-----\n" + "A" * 9000,
        "garbage PEM": "-----BEGIN CERTIFICATE REQUEST-----\nAAAA\n-----END CERTIFICATE REQUEST-----\n",
    }
    for why, pem in bad.items():
        with pytest.raises(lan_certs.CsrError):
            lan_certs.validate_csr(pem, SID)
            pytest.fail(why)
    # a signature that doesn't match the request
    good, _ = make_csr()
    der = bytearray(x509.load_pem_x509_csr(good.encode()).public_bytes(serialization.Encoding.DER))
    der[-5] ^= 0x55
    tampered = x509.load_der_x509_csr(bytes(der)).public_bytes(serialization.Encoding.PEM).decode()
    with pytest.raises(lan_certs.CsrError):
        lan_certs.validate_csr(tampered, SID)
    with pytest.raises(lan_certs.CsrError):   # an id that can't be a DNS label has no name at all
        lan_certs.validate_csr(good, "s-dash")


def test_renew_at_follows_the_lifetime():
    d = lan_certs.DAY
    assert lan_certs.renew_at(0, 90 * d) == 60 * d       # 90 days: renew with 30 left
    assert lan_certs.renew_at(0, 45 * d) == 30 * d       # 45 days: with 15 left
    assert lan_certs.renew_at(0, 6 * d) == 4 * d         # 6 days: with 2 left
    assert lan_certs.renew_at(0, 365 * d) == 335 * d     # never more than 30 days ahead


def test_policy():
    now, d = 1_800_000_000.0, lan_certs.DAY
    fresh = {"fullchain": "PEM", "key_sha256": "k1", "not_before": now - 10 * d, "not_after": now + 80 * d,
             "next_order_at": now - 8 * d}
    p = lambda row, key="k1", **kw: lan_certs.policy(row, key, now, kw.get("flight", False), kw.get("week", 0),  # noqa: E731
                                                      kw.get("failed", 0), kw.get("oldest"), kw.get("cap", 30))
    assert p(fresh) == ("issued", 0)                                     # the stored chain, no order
    assert p(None) == ("order", 0)
    assert p(None, flight=True) == ("pending", 0)                        # one order per server at a time
    assert p(fresh, "k2") == ("order", 0)                                # lost its key: a new one after the gap
    assert p({**fresh, "next_order_at": now + 3600}, "k2") == ("later", 3600)
    due = {**fresh, "not_before": now - 61 * d, "not_after": now + 29 * d}
    assert p(due) == ("order", 0)                                        # inside the renewal window
    assert p({"failures": 3, "next_order_at": now + 4 * 3600})[0] == "later"   # backoff after failures
    assert p(None, failed=lan_certs.FAILURE_BRAKE) == ("later", 3600)    # many failures lately: everyone waits
    what, wait = p(None, week=30, oldest=now - 6 * d)                    # weekly cap: until the oldest drops out
    assert what == "later" and wait == pytest.approx(d)
    assert p(None, week=30, oldest=now - 7 * d + 60)[1] == 3600          # ... but at least an hour
    assert p(None, week=29) == ("order", 0)


def test_welcome_offer(monkeypatch):
    class C:
        site_id, site, hello = SID, {"id": SID, "retired_at": None}, {"caps": ["lan_cert"]}

        def caps(self):
            return self.hello["caps"]
    c = C()
    monkeypatch.setattr(settings, "lan_certs", False)
    assert lan_certs.welcome(c) == {}
    monkeypatch.setattr(settings, "lan_certs", True)
    assert lan_certs.welcome(c) == {"lan_cert": {"name": NAME}}
    c.hello = {"caps": ["events"]}          # an older server: no offer
    assert lan_certs.welcome(c) == {}
    c.hello, c.site = {"caps": ["lan_cert"]}, {"id": SID, "retired_at": 1.0}
    assert lan_certs.welcome(c) == {}
    monkeypatch.setattr(settings, "lan_zone", "")
    c.site = {"id": SID, "retired_at": None}
    assert lan_certs.welcome(c) == {}


# ---------------------------------------------------------------- a fake ACME server

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class FakeAcme:
    """Just enough RFC 8555 for acme.client.ClientV2: nonces, account, one order at a time with one dns-01
    authorization, finalize, certificate. It doesn't check JWS signatures; it does check the TXT value through the
    hub's DNS answers (dns_check) when the challenge is answered."""

    def __init__(self, dns_check, lifetime_days: float = 90) -> None:
        self.dns_check, self.lifetime = dns_check, lifetime_days
        self.ca_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Fake ACME CA")])
        now = dt.datetime.now(dt.timezone.utc)
        self.ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(self.ca_key.public_key())
                   .serial_number(1).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=3650))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(self.ca_key, hashes.SHA256()))
        self.thumb: str | None = None
        self.accounts = self.orders = self.challenges = 0
        self.order: dict = {}
        self.fail_dns = False
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status: int, body=None, headers=None, ctype="application/json"):
                data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else b"")
                self.send_response(status)
                self.send_header("Replay-Nonce", _b64(hashlib.sha256(str(time.time_ns()).encode()).digest()[:16]))
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_HEAD(self):
                self._send(200)

            def do_GET(self):
                if self.path == "/directory":
                    b = fake.base
                    return self._send(200, {"newNonce": b + "/nonce", "newAccount": b + "/acct", "newOrder": b + "/new-order",
                                            "revokeCert": b + "/revoke", "keyChange": b + "/key", "meta": {"termsOfService": b + "/tos"}})
                self._send(404, {"type": "urn:ietf:params:acme:error:malformed", "detail": "no"}, ctype="application/problem+json")

            def do_POST(self):
                jws = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                prot = json.loads(_unb64(jws["protected"]))
                payload = json.loads(_unb64(jws["payload"])) if jws["payload"] else None
                try:
                    self._send(*fake.handle(self.path, prot, payload))
                except Exception as e:   # surface fake-server bugs as a failed order, not a hang
                    self._send(500, {"type": "urn:ietf:params:acme:error:serverInternal", "detail": repr(e)},
                               ctype="application/problem+json")

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.http.shutdown()

    def _order_json(self) -> dict:
        o = self.order
        out = {"status": o["status"], "identifiers": o["identifiers"], "authorizations": [self.base + "/authz/1"],
               "finalize": self.base + "/finalize/1", "expires": "2099-01-01T00:00:00Z"}
        if o["status"] == "valid":
            out["certificate"] = self.base + "/cert/1"
        return out

    def _authz_json(self) -> dict:
        o = self.order
        ch = {"type": "dns-01", "url": self.base + "/chall/1", "token": o["token"], "status": o["authz"]}
        if o["authz"] == "invalid":
            ch["error"] = {"type": "urn:ietf:params:acme:error:dns", "detail": "no TXT record found"}
        return {"identifier": {"type": "dns", "value": o["identifiers"][0]["value"].removeprefix("*.")}, "wildcard": True,
                "status": o["authz"], "expires": "2099-01-01T00:00:00Z", "challenges": [ch]}

    def handle(self, path: str, prot: dict, payload):
        if path == "/acct":
            self.accounts += 1
            self.thumb = _b64(jose.JWK.from_json(prot["jwk"]).thumbprint())
            return 201, {"status": "valid", "contact": payload.get("contact", [])}, {"Location": self.base + "/acct/1"}
        assert prot.get("kid") == self.base + "/acct/1", "requests after the account carry its kid"
        if path == "/new-order":
            self.orders += 1
            self.order = {"status": "pending", "identifiers": payload["identifiers"], "authz": "pending",
                          "token": _b64(hashlib.sha256(str(time.time_ns()).encode()).digest())}
            return 201, self._order_json(), {"Location": self.base + "/order/1"}
        if path == "/authz/1":
            return 200, self._authz_json()
        if path == "/chall/1":
            self.challenges += 1
            want = _b64(hashlib.sha256(f"{self.order['token']}.{self.thumb}".encode()).digest())
            ok = not self.fail_dns and self.dns_check("_acme-challenge." + self.order["identifiers"][0]["value"].removeprefix("*."), want)
            self.order["authz"] = "valid" if ok else "invalid"
            self.order["status"] = "ready" if ok else "invalid"
            return 200, {"type": "dns-01", "url": self.base + "/chall/1", "token": self.order["token"],
                         "status": "valid" if ok else "invalid"}, {"Link": f'<{self.base}/authz/1>;rel="up"'}
        if path == "/finalize/1":
            csr = x509.load_der_x509_csr(_unb64(payload["csr"]))
            now = dt.datetime.now(dt.timezone.utc)
            leaf = (x509.CertificateBuilder().subject_name(x509.Name([])).issuer_name(self.ca.subject)
                    .public_key(csr.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(now - dt.timedelta(hours=1)).not_valid_after(now + dt.timedelta(days=self.lifetime))
                    .add_extension(csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value, critical=False)
                    .sign(self.ca_key, hashes.SHA256()))
            self.order["pem"] = (leaf.public_bytes(serialization.Encoding.PEM) + self.ca.public_bytes(serialization.Encoding.PEM))
            self.order["status"] = "valid"
            return 200, self._order_json()
        if path == "/order/1":
            return 200, self._order_json()
        if path == "/cert/1":
            return 200, self.order["pem"], None, "application/pem-certificate-chain"
        return 404, {"type": "urn:ietf:params:acme:error:malformed", "detail": path}, None, "application/problem+json"


def _dns_check(name: str, value: str) -> bool:
    """What a resolver would get from `python -m hub dns` right now: the DbSource view of the database."""
    src = lan_dns.DbSource()
    src.refresh_labels()
    src.refresh_challenges()
    out = lan_dns.Zone(src).respond(dns.message.make_query(name, "TXT").to_wire())
    r = dns.message.from_wire(out)
    return any(rd.strings == (value.encode(),) for rrset in r.answer for rd in rrset)


class Conn:
    def __init__(self, sid: str) -> None:
        self.site_id, self.site, self.hello, self.frames = sid, {"id": sid, "retired_at": None}, {"caps": ["lan_cert"]}, []

    def caps(self):
        return self.hello["caps"]

    async def send(self, frame: dict) -> None:
        self.frames.append(frame)


@pytest.fixture
def acme(client, monkeypatch):
    t = time.time()
    with db.engine().begin() as c:
        for table in (db.lan_certs, db.lan_cert_orders, db.lan_challenges):
            c.execute(sa.delete(table))
        c.execute(sa.delete(db.kv).where(db.kv.c.key == lan_certs.ACCOUNT_KEY))
        c.execute(sa.delete(db.sites).where(db.sites.c.id == SID))
        c.execute(db.sites.insert().values(id=SID, org_id="o_x", name="LAN cert test", location="", token_hash="h-lan", created_at=t, online=True))
    fake = FakeAcme(_dns_check)
    monkeypatch.setattr(settings, "lan_certs", True)
    monkeypatch.setattr(settings, "lan_acme_directory", fake.base + "/directory")
    monkeypatch.setattr(lan_certs, "PUBLISH_WAIT_S", 0)
    lan_certs.set_backend(None)
    yield fake
    lan_certs.set_backend(None)
    lan_certs._in_flight.clear()
    agents.registry.by_site.pop(SID, None)
    fake.close()


async def _csr(conn: Conn, pem: str) -> dict:
    n = len(conn.frames)
    await lan_certs.on_csr(conn, {"t": "lan_csr", "csr": pem})
    assert len(conn.frames) == n + 1
    return conn.frames[-1]


async def _drain() -> None:
    while lan_certs._tasks:
        await asyncio.gather(*list(lan_certs._tasks))


def test_order_end_to_end(acme, caplog):
    conn = Conn(SID)
    agents.registry.by_site[SID] = conn   # the issued chain goes down this server's tunnel
    pem, key = make_csr()
    caplog.set_level(logging.DEBUG)

    async def go():
        first = await _csr(conn, pem)
        assert first == {"t": "lan_cert", "status": "pending"}
        assert (await _csr(conn, pem))["status"] == "pending"   # one order at a time: no second one
        await _drain()
        issued = conn.frames[-1]
        assert issued["status"] == "issued" and issued["t"] == "lan_cert"
        chain = x509.load_pem_x509_certificates(issued["fullchain"].encode())
        assert chain[0].public_key().public_numbers() == key.public_key().public_numbers()
        assert chain[0].extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName) == [WILD]
        assert acme.accounts == 1 and acme.orders == 1 and acme.challenges == 1
        row = db.one(sa.select(db.lan_certs).where(db.lan_certs.c.server_id == SID))
        assert row["failures"] == 0 and row["not_after"] > time.time() + 89 * lan_certs.DAY and row["fullchain"] == issued["fullchain"]
        assert db.rows(sa.select(db.lan_challenges)) == []   # the TXT is gone once the order is done
        assert db.one(sa.select(db.lan_cert_orders))["ok"] is True
        # the same key again (e.g. after a reconnect): the stored chain, no new order
        again = await _csr(conn, pem)
        assert again["status"] == "issued" and again["fullchain"] == issued["fullchain"] and acme.orders == 1
        # another key while the certificate is fresh: not before the reissue gap
        later = await _csr(conn, make_csr()[0])
        assert later["status"] == "later" and later["retry_after"] > lan_certs.DAY
        # due for renewal: a new order with a new key; the account is reused
        db.run(sa.update(db.lan_certs).where(db.lan_certs.c.server_id == SID).values(
            not_after=time.time() + 20 * lan_certs.DAY, next_order_at=time.time() - 1))
        pem2, key2 = make_csr()
        assert (await _csr(conn, pem2))["status"] == "pending"
        await _drain()
        assert conn.frames[-1]["status"] == "issued" and acme.accounts == 1 and acme.orders == 2
        leaf = x509.load_pem_x509_certificates(conn.frames[-1]["fullchain"].encode())[0]
        assert leaf.public_key().public_numbers() == key2.public_key().public_numbers()

    asyncio.run(go())
    acct = db.one(sa.select(db.kv).where(db.kv.c.key == lan_certs.ACCOUNT_KEY))["value"]
    assert acct["uri"] == acme.base + "/acct/1" and "PRIVATE KEY" in acct["key_pem"]
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "PRIVATE KEY" not in logged and acct["key_pem"][40:80] not in logged


def test_failed_validation_backs_off(acme):
    conn = Conn(SID)
    acme.fail_dns = True
    pem, _ = make_csr()

    async def go():
        assert (await _csr(conn, pem))["status"] == "pending"
        await _drain()
        row = db.one(sa.select(db.lan_certs).where(db.lan_certs.c.server_id == SID))
        assert row["failures"] == 1 and row["fullchain"] is None and row["last_error"]
        assert 3500 < row["next_order_at"] - time.time() <= 3600   # an hour, then 2, 4 ... 24
        assert db.one(sa.select(db.lan_cert_orders))["ok"] is False
        r = await _csr(conn, pem)
        assert r["status"] == "later" and 3500 < r["retry_after"] <= 3601
        assert acme.orders == 1
        # the next failures back off further
        db.run(sa.update(db.lan_certs).where(db.lan_certs.c.server_id == SID).values(next_order_at=time.time() - 1))
        assert (await _csr(conn, pem))["status"] == "pending"
        await _drain()
        row = db.one(sa.select(db.lan_certs).where(db.lan_certs.c.server_id == SID))
        assert row["failures"] == 2 and 7100 < row["next_order_at"] - time.time() <= 7200

    asyncio.run(go())


def test_the_txt_is_served_while_the_order_runs(acme, monkeypatch):
    """The fake server's check is the hub's own DNS answer: with the TXT withheld from the zone, validation fails."""
    seen = []
    monkeypatch.setattr(lan_certs, "publish", lambda name, value: seen.append(name))   # never reaches lan_challenges
    conn = Conn(SID)

    async def go():
        assert (await _csr(conn, make_csr()[0]))["status"] == "pending"
        await _drain()

    asyncio.run(go())
    row = db.one(sa.select(db.lan_certs).where(db.lan_certs.c.server_id == SID))
    assert seen == [f"_acme-challenge.{NAME}"] and row["failures"] == 1 and not row["fullchain"]


def test_limits_and_refusals(acme, caplog):
    conn = Conn(SID)

    async def go():
        bad = await _csr(conn, make_csr((f"*.s-0ther0000001.{settings.lan_zone}",), cn=None)[0])
        assert bad["status"] == "refused" and bad["retry_after"] >= 3600
        # weekly cap: logged, and everyone waits
        settings.lan_cert_weekly_cap, cap = 3, settings.lan_cert_weekly_cap
        try:
            with db.engine().begin() as c:
                for i in range(3):
                    c.execute(db.lan_cert_orders.insert().values(server_id=f"s_other{i}", at=time.time() - 3600, ok=True))
            caplog.set_level(logging.WARNING, "hub.lan_certs")
            r = await _csr(conn, make_csr()[0])
            assert r["status"] == "later" and r["retry_after"] > 6 * lan_certs.DAY
            assert any("weekly cap" in m for m in caplog.messages)
        finally:
            settings.lan_cert_weekly_cap = cap
        # failure brake
        with db.engine().begin() as c:
            c.execute(sa.delete(db.lan_cert_orders))
            for i in range(lan_certs.FAILURE_BRAKE):
                c.execute(db.lan_cert_orders.insert().values(server_id=f"s_other{i}", at=time.time() - 60, ok=False))
        r = await _csr(conn, make_csr()[0])
        assert r["status"] == "later" and r["retry_after"] == 3601
        assert acme.orders == 0
        # off at the hub, or retired: later, no order
        settings.lan_certs = False
        try:
            assert (await _csr(conn, make_csr()[0]))["status"] == "later"
        finally:
            settings.lan_certs = True
        conn.site = {"id": SID, "retired_at": 1.0}
        assert (await _csr(conn, make_csr()[0]))["status"] == "later"

    asyncio.run(go())


def test_sweep_clears_leftovers(acme):
    now = time.time()
    with db.engine().begin() as c:
        c.execute(db.lan_challenges.insert().values(name="_acme-challenge.x", value="old", expires_at=now - 1))
        c.execute(db.lan_challenges.insert().values(name="_acme-challenge.x", value="new", expires_at=now + 60))
        c.execute(db.lan_cert_orders.insert().values(server_id=SID, at=now - 15 * lan_certs.DAY, ok=True))
        c.execute(db.lan_cert_orders.insert().values(server_id=SID, at=now - 1, ok=True))
    lan_certs.sweep()
    assert [r["value"] for r in db.rows(sa.select(db.lan_challenges))] == ["new"]
    assert len(db.rows(sa.select(db.lan_cert_orders))) == 1


def test_agents_route_lan_csr(acme, monkeypatch):
    """The tunnel loop hands lan_csr frames to lan_certs and the welcome carries the offer."""
    import inspect
    src = inspect.getsource(agents.AgentRegistry._loop) + inspect.getsource(agents.AgentRegistry._on_hello)
    assert 'elif t == "lan_csr"' in src and "lan_certs.on_csr(conn, frame)" in src and "lan_certs.welcome(conn)" in src
