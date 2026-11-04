"""The camera pages' wildcard certificate (camweb_cert.py): the hub's DNS answers its challenge at the reserved
_camweb label, nothing is ordered until the registrar's CNAME points there, a whole order through the real ACME client
against the fake ACME server (which follows the CNAME the way Let's Encrypt does), renewal and backoff, and the
endpoint Caddy fetches it from: only Caddy's own request on the compose network gets the key."""
import logging
import time

import dns.message
import pytest
import sqlalchemy as sa
from cryptography import x509

from hub import camweb_cert, db, lan_certs, lan_dns
from hub.config import settings
from test_lan_certs import FakeAcme, _dns_check

DOMAIN = "cam.axiomvision.ai"
CADDY = {"host": "hub:8000"}


@pytest.fixture
def wild(client, monkeypatch):
    with db.engine().begin() as c:
        c.execute(sa.delete(db.lan_challenges))
        c.execute(sa.delete(db.kv).where(db.kv.c.key.in_([lan_certs.ACCOUNT_KEY, camweb_cert.KV_KEY])))

    def follow_cname(name: str, value: str) -> bool:   # Let's Encrypt follows the registrar's CNAME into our zone
        if name == f"_acme-challenge.{DOMAIN}" and camweb_cert.cname_of(name) == camweb_cert.challenge_target():
            name = camweb_cert.challenge_target()
        return _dns_check(name, value)

    fake = FakeAcme(follow_cname)
    monkeypatch.setattr(settings, "lan_certs", True)
    monkeypatch.setattr(settings, "camweb_domain", DOMAIN)
    monkeypatch.setattr(settings, "lan_acme_directory", fake.base + "/directory")
    monkeypatch.setattr(lan_certs, "PUBLISH_WAIT_S", 0)
    fake.cname = None
    monkeypatch.setattr(camweb_cert, "cname_of", lambda name: fake.cname if name == f"_acme-challenge.{DOMAIN}" else None)
    lan_certs.set_backend(None)
    camweb_cert.reset()
    yield fake
    lan_certs.set_backend(None)
    camweb_cert.reset()
    fake.close()


def _ask(name: str, rdtype: str = "TXT") -> dns.message.Message:
    src = lan_dns.DbSource()
    src.refresh_labels()
    src.refresh_challenges()
    return dns.message.from_wire(lan_dns.Zone(src).respond(dns.message.make_query(name, rdtype).to_wire()))


def test_names():
    assert camweb_cert.LABEL == "_camweb" and lan_dns.server_of_label("_camweb") is None   # never a server's label


def test_the_zone_answers_the_reserved_challenge_name_only(wild):
    z = settings.lan_zone
    lan_certs.publish(f"_acme-challenge._camweb.{z}", "tok-cam")
    r = _ask(f"_acme-challenge._camweb.{z}")
    assert [rd.strings for rs in r.answer for rd in rs] == [(b"tok-cam",)]
    assert _ask(f"_camweb.{z}", "A").rcode() == dns.rcode.NOERROR and not _ask(f"_camweb.{z}", "A").answer
    assert _ask(f"x._camweb.{z}", "A").rcode() == dns.rcode.NXDOMAIN
    assert _ask(f"1-2-3-4._camweb.{z}", "A").rcode() == dns.rcode.NXDOMAIN   # no addresses under it


def test_off_without_lan_certs_or_a_real_domain(wild, monkeypatch):
    assert camweb_cert.enabled()
    monkeypatch.setattr(settings, "camweb_wildcard", False)
    assert not camweb_cert.enabled() and camweb_cert.check() == "off"
    monkeypatch.setattr(settings, "camweb_wildcard", True)
    monkeypatch.setattr(settings, "lan_certs", False)
    assert not camweb_cert.enabled()
    monkeypatch.setattr(settings, "lan_certs", True)
    monkeypatch.setattr(settings, "camweb_domain", "cam.localhost")
    assert not camweb_cert.enabled()


def test_nothing_is_ordered_until_the_cname_is_there(wild, caplog):
    caplog.set_level(logging.WARNING, "hub.camweb_cert")
    assert camweb_cert.check() == "no-delegation"
    assert wild.orders == 0
    hint = [m for m in caplog.messages if "CNAME" in m]
    assert hint and f"_acme-challenge.{DOMAIN}" in hint[0] and f"_acme-challenge._camweb.{settings.lan_zone}" in hint[0]
    camweb_cert.check()
    assert len([m for m in caplog.messages if "CNAME" in m]) == 1     # the hint at most once a day
    wild.cname = "somewhere.else.example"
    assert camweb_cert.check() == "no-delegation" and wild.orders == 0


def test_order_renewal_and_what_caddy_gets(wild, client):
    wild.cname = camweb_cert.challenge_target()
    assert camweb_cert.check() == "ordered"
    assert wild.orders == 1
    st = camweb_cert.state()
    leaf = x509.load_pem_x509_certificates(st["fullchain"].encode())[0]
    assert leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName) == [f"*.{DOMAIN}"]
    assert not db.rows(sa.select(db.lan_challenges))                  # the TXT is gone once the order is done
    assert camweb_cert.check() == "fresh" and wild.orders == 1        # not due: no second order
    camweb_cert.reset()                                               # a restart: read back from kv
    assert camweb_cert.state()["fullchain"] == st["fullchain"]

    r = client.get(f"/api/camweb/cert?server_name=cabc123.{DOMAIN}", headers=CADDY)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "BEGIN CERTIFICATE" in r.text and "BEGIN PRIVATE KEY" in r.text
    # names the wildcard doesn't cover: 204, Caddy carries on as before
    for name in (DOMAIN, f"a.b.{DOMAIN}", "hub.axiomvision.ai", f"x.{DOMAIN}.evil.example", ""):
        assert client.get("/api/camweb/cert", params={"server_name": name}, headers=CADDY).status_code == 204, name

    # due (inside the renewal window): a new key and certificate
    camweb_cert._save(not_after=time.time() + 10 * lan_certs.DAY, not_before=time.time() - 80 * lan_certs.DAY)
    assert camweb_cert.check() == "ordered" and wild.orders == 2
    assert camweb_cert.state()["key_pem"] != st["key_pem"]


def test_the_key_goes_only_to_caddys_own_request(wild, client):
    wild.cname = camweb_cert.challenge_target()
    assert camweb_cert.check() == "ordered"
    url = f"/api/camweb/cert?server_name=cabc123.{DOMAIN}"
    assert client.get(url, headers=CADDY).status_code == 200
    # anything Caddy proxies from outside carries X-Forwarded-For and a public Host
    assert client.get(url, headers={**CADDY, "x-forwarded-for": "203.0.113.9"}).status_code == 404
    assert client.get(url, headers={**CADDY, "x-forwarded-proto": "https"}).status_code == 404
    assert client.get(url, headers={**CADDY, "forwarded": "for=203.0.113.9"}).status_code == 404
    assert client.get(url, headers={"host": "hub.axiomvision.ai"}).status_code == 404
    assert client.get(url).status_code == 404                         # the test client's own Host


def test_none_yet_or_ending_means_204(wild, client):
    url = f"/api/camweb/cert?server_name=cabc123.{DOMAIN}"
    assert client.get(url, headers=CADDY).status_code == 204          # nothing ordered yet
    wild.cname = camweb_cert.challenge_target()
    assert camweb_cert.check() == "ordered"
    camweb_cert._save(not_after=time.time() + 60)                     # about to end: not handed out
    assert client.get(url, headers=CADDY).status_code == 204
    camweb_cert._save(not_after=time.time() + 30 * lan_certs.DAY, domain="cam.other.example")   # the domain changed
    assert client.get(url, headers=CADDY).status_code == 204


def test_a_failed_order_backs_off(wild):
    wild.cname = camweb_cert.challenge_target()
    wild.fail_dns = True
    assert camweb_cert.check() == "failed"
    st = camweb_cert.state()
    assert st["failures"] == 1 and st["next_order_at"] > time.time() + 3000 and not st.get("fullchain")
    assert camweb_cert.check() == "waiting" and wild.orders == 1      # no retry inside the backoff
    wild.fail_dns = False
    camweb_cert._save(next_order_at=time.time() - 1)
    assert camweb_cert.check() == "ordered" and camweb_cert.state()["failures"] == 0
