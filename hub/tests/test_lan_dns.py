"""Direct-on-LAN names (lan_dns.py): the label mapping, the authoritative answers (A for private addresses of known
servers only, TXT challenges, SOA/NS, REFUSED outside the zone), malformed packets, UDP and TCP, the rate limit and
the database source. Everything on 127.0.0.1: no real DNS is asked."""
import asyncio
import os
import socket
import struct
import time

import dns.flags
import dns.message
import dns.opcode
import dns.query
import dns.rcode
import dns.rdatatype
import pytest
import sqlalchemy as sa

from hub import db, lan_dns

ZONE = "lan.axiomvision.ai"
SID = "s_d6d429b617ca"
LABEL = "s-d6d429b617ca"
CH = f"_acme-challenge.{LABEL}.{ZONE}"


def zone(**kw) -> lan_dns.Zone:
    src = lan_dns.MemorySource({LABEL}, {CH: ["tok-one", "tok-two"]})
    return lan_dns.Zone(src, ZONE, "ns1.axiomvision.ai", **kw)


def ask(name: str, rdtype: str = "A", z: lan_dns.Zone | None = None, **kw) -> dns.message.Message:
    q = dns.message.make_query(name, rdtype, **kw)
    out = (z or zone()).respond(q.to_wire())
    assert out is not None
    r = dns.message.from_wire(out)
    assert r.id == q.id and r.flags & dns.flags.QR and not r.flags & dns.flags.RA
    return r


def answers(r: dns.message.Message) -> list[str]:
    return sorted(rd.to_text() for rrset in r.answer for rd in rrset)


def test_label_mapping_is_reversible():
    assert lan_dns.label_of(SID) == LABEL and lan_dns.server_of_label(LABEL) == SID
    assert lan_dns.server_of_label(LABEL.upper()) == SID
    assert lan_dns.name_for(SID) == f"{LABEL}.{ZONE}"
    for bad_id in ("", "S_UPPER", "s-dash", "s__double", "_lead", "trail_", "s_" + "a" * 70, "s.dot", None):
        assert lan_dns.label_of(bad_id) is None, bad_id
    for bad_label in ("", "-x", "x-", "a--b", "s_x", "xn--abc", "a" * 64):
        assert lan_dns.server_of_label(bad_label) is None, bad_label


def test_a_records_only_for_private_addresses_of_known_servers():
    for ip in ("10.0.0.1", "172.16.5.4", "172.31.255.255", "192.168.1.5", "100.64.0.1", "100.127.255.254"):
        r = ask(f"{ip.replace('.', '-')}.{LABEL}.{ZONE}")
        assert r.rcode() == dns.rcode.NOERROR and r.flags & dns.flags.AA
        assert answers(r) == [ip]
        assert r.answer[0].ttl == lan_dns.A_TTL
    for ip in ("8-8-8-8", "172-32-0-1", "100-128-0-1", "127-0-0-1", "169-254-1-1", "192-169-1-1", "0-0-0-0"):
        r = ask(f"{ip}.{LABEL}.{ZONE}")
        assert r.rcode() == dns.rcode.NXDOMAIN and not r.answer, ip
        assert r.authority and r.authority[0].rdtype == dns.rdatatype.SOA
    for junk in ("192-168-1", "192-168-1-5-6", "192-168-01-5", "192-168-1-256", "a-b-c-d", "192-168-1-5x"):
        assert ask(f"{junk}.{LABEL}.{ZONE}").rcode() == dns.rcode.NXDOMAIN, junk
    # unknown or malformed server labels, and names deeper than <ip>.<label>
    assert ask(f"192-168-1-5.s-000000000000.{ZONE}").rcode() == dns.rcode.NXDOMAIN
    assert ask(f"192-168-1-5.s_d6d429b617ca.{ZONE}").rcode() == dns.rcode.NXDOMAIN
    assert ask(f"x.192-168-1-5.{LABEL}.{ZONE}").rcode() == dns.rcode.NXDOMAIN
    # other types on an address name: the name exists, no data
    r = ask(f"192-168-1-5.{LABEL}.{ZONE}", "AAAA")
    assert r.rcode() == dns.rcode.NOERROR and not r.answer and r.authority[0].rdtype == dns.rdatatype.SOA
    # 0x20 case randomisation: matched case-insensitively, the question echoed as asked
    r = ask(f"192-168-1-5.{LABEL.upper()}.LaN.AxiomVision.AI")
    assert answers(r) == ["192.168.1.5"] and r.question[0].name.to_text().startswith("192-168-1-5.S-D6D")


def test_refused_outside_the_zone_and_never_recursive():
    for name in ("google.com", "axiomvision.ai", "ns1.axiomvision.ai", "hub.axiomvision.ai", "lan.axiomvision.ai.evil.com",
                 "xlan.axiomvision.ai"):
        r = ask(name)
        assert r.rcode() == dns.rcode.REFUSED and not r.answer and not r.flags & dns.flags.AA, name
    q = dns.message.make_query(f"192-168-1-5.{LABEL}.{ZONE}", "A", rdclass="CH")
    assert dns.message.from_wire(zone().respond(q.to_wire())).rcode() == dns.rcode.REFUSED


def test_soa_ns_and_the_label_itself():
    r = ask(ZONE, "SOA")
    assert r.rcode() == dns.rcode.NOERROR and r.flags & dns.flags.AA
    soa = r.answer[0][0]
    assert soa.mname.to_text() == "ns1.axiomvision.ai." and soa.rname.to_text() == f"hostmaster.{ZONE}." and soa.minimum == lan_dns.NEG_TTL
    assert answers(ask(ZONE, "NS")) == ["ns1.axiomvision.ai."]
    r = ask(ZONE, "A")
    assert r.rcode() == dns.rcode.NOERROR and not r.answer and r.authority
    r = ask(f"{LABEL}.{ZONE}", "A")   # exists (its children do), no records
    assert r.rcode() == dns.rcode.NOERROR and not r.answer
    r = ask(f"{LABEL}.{ZONE}", "CAA")   # Let's Encrypt checks CAA here: an empty answer, not an error
    assert r.rcode() == dns.rcode.NOERROR and not r.answer


def test_txt_challenges():
    r = ask(CH, "TXT")
    assert r.rcode() == dns.rcode.NOERROR and answers(r) == ['"tok-one"', '"tok-two"'] and r.answer[0].ttl == lan_dns.TXT_TTL
    r = ask(CH.upper(), "TXT")
    assert answers(r) == ['"tok-one"', '"tok-two"']
    assert not ask(CH, "A").answer
    r = ask(f"_acme-challenge.s-111111111111.{ZONE}", "TXT")   # unknown server
    assert r.rcode() == dns.rcode.NXDOMAIN
    z = lan_dns.Zone(lan_dns.MemorySource({LABEL}), ZONE, "ns1.axiomvision.ai")   # known server, no order running
    r = ask(CH, "TXT", z)
    assert r.rcode() == dns.rcode.NOERROR and not r.answer


def test_edns_and_opcodes():
    r = ask(f"192-168-1-5.{LABEL}.{ZONE}", use_edns=0, payload=4096)
    assert r.edns == 0 and r.payload == lan_dns.UDP_MAX and answers(r) == ["192.168.1.5"]
    q = dns.message.make_query(ZONE, "SOA")
    q.set_opcode(dns.opcode.NOTIFY)
    assert dns.message.from_wire(zone().respond(q.to_wire())).rcode() == dns.rcode.NOTIMP


def test_malformed_packets_get_no_answer():
    z = zone()
    good = dns.message.make_query(f"192-168-1-5.{LABEL}.{ZONE}", "A").to_wire()
    resp = dns.message.from_wire(z.respond(good))
    junk = [b"", b"\x00", good[:11], good[:-3], good + b"\xff" * 10, os.urandom(40), b"\xff" * 512, b"\x00" * 5000,
            resp.to_wire(),                                             # a response: never answered (reflection loops)
            good[:12] + b"\xc0\x0c" + good[-4:],                         # a compression pointer to itself
            struct.pack(">HHHHHH", 1, 0, 1, 0, 0, 0) + b"\x3f" + b"a" * 10]   # a label running past the end
    for i in range(200):
        junk.append(bytes(b if j != i % len(good) else (b ^ 0xA5) for j, b in enumerate(good)))   # one byte flipped
    for pkt in junk:
        out = z.respond(pkt)   # never raises
        if out is not None:
            dns.message.from_wire(out)   # whatever it says is itself well-formed
    for pkt in junk[:11]:
        assert z.respond(pkt) is None
    # two questions: FORMERR
    q = dns.message.make_query(ZONE, "SOA")
    q.question.append(q.question[0])
    out = z.respond(q.to_wire())
    assert out is None or dns.message.from_wire(out).rcode() == dns.rcode.FORMERR


def test_rate_limit_bucket():
    rl = lan_dns.RateLimit(5)
    t = 1000.0
    assert sum(rl.allow("1.2.3.4", t) for _ in range(30)) == 10   # a burst of 2 x rate
    assert rl.allow("5.6.7.8", t)                                 # another source is unaffected
    assert not rl.allow("1.2.3.4", t + 0.1)
    assert rl.allow("1.2.3.4", t + 0.25)                         # refills at `rate` per second
    small = lan_dns.RateLimit(1, max_keys=50)
    for i in range(500):
        small.allow(f"10.0.{i // 250}.{i % 250}", t)
    assert len(small._b) <= 51


def test_udp_and_tcp_listeners():
    async def go():
        srv = lan_dns.Server(zone(), rate=1000)
        udp, tcp = await srv.start("127.0.0.1", 0)
        try:
            q = dns.message.make_query(f"10-1-2-3.{LABEL}.{ZONE}", "A")
            r = await asyncio.to_thread(dns.query.udp, q, "127.0.0.1", timeout=2, port=udp)
            assert answers(r) == ["10.1.2.3"]
            r = await asyncio.to_thread(dns.query.tcp, dns.message.make_query(CH, "TXT"), "127.0.0.1", timeout=2, port=tcp)
            assert answers(r) == ['"tok-one"', '"tok-two"']
            # junk over UDP does not stop the listener
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.sendto(b"\x00\x01garbage", ("127.0.0.1", udp))
            # junk over TCP closes that connection only
            def bad_tcp():
                with socket.create_connection(("127.0.0.1", tcp), timeout=2) as s:
                    s.sendall(b"\x00\x05hello")
                    s.settimeout(2)
                    assert s.recv(10) == b""
                with socket.create_connection(("127.0.0.1", tcp), timeout=2) as s:
                    s.sendall(b"\xff\xff")   # announces 64 KiB: more than any query
                    assert s.recv(10) == b""
            await asyncio.to_thread(bad_tcp)
            r = await asyncio.to_thread(dns.query.udp, q, "127.0.0.1", timeout=2, port=udp)
            assert answers(r) == ["10.1.2.3"]
            # two queries on one TCP connection
            def two():
                with socket.create_connection(("127.0.0.1", tcp), timeout=2) as s:
                    for name in (f"10-0-0-1.{LABEL}.{ZONE}", f"10-0-0-2.{LABEL}.{ZONE}"):
                        dns.query.send_tcp(s, dns.message.make_query(name, "A"))
                        got, _ = dns.query.receive_tcp(s, time.time() + 2)
                        yield answers(got)[0]
            assert await asyncio.to_thread(lambda: list(two())) == ["10.0.0.1", "10.0.0.2"]
        finally:
            srv.close()

    asyncio.run(go())


def test_rate_limit_drops_udp():
    async def go():
        srv = lan_dns.Server(zone(), rate=2)   # bursts of 4
        udp, _ = await srv.start("127.0.0.1", 0)
        try:
            def burst():
                ok = 0
                for i in range(12):
                    try:
                        dns.query.udp(dns.message.make_query(f"10-0-0-{i}.{LABEL}.{ZONE}", "A"), "127.0.0.1", timeout=0.3, port=udp)
                        ok += 1
                    except dns.exception.Timeout:
                        pass
                return ok
            ok = await asyncio.to_thread(burst)
            assert 4 <= ok <= 8   # the burst, plus what refilled while the drops timed out
        finally:
            srv.close()

    asyncio.run(go())


def test_db_source(client):
    t = time.time()
    with db.engine().begin() as c:
        for sid, retired in (("s_aaaa00000001", None), ("s_aaaa00000002", t)):
            c.execute(sa.delete(db.sites).where(db.sites.c.id == sid))
            c.execute(db.sites.insert().values(id=sid, org_id="o_x", name=sid, location="", token_hash="h" + sid, created_at=t,
                                               online=False, retired_at=retired))
        c.execute(sa.delete(db.lan_challenges))
        c.execute(db.lan_challenges.insert().values(name="_acme-challenge.s-aaaa00000001.lan.axiomvision.ai", value="v", expires_at=t + 60))
        c.execute(db.lan_challenges.insert().values(name="_acme-challenge.s-aaaa00000001.lan.axiomvision.ai", value="old", expires_at=t - 1))
    src = lan_dns.DbSource()
    src.refresh_labels()
    src.refresh_challenges()
    assert src.known("s-aaaa00000001") and not src.known("s-aaaa00000002")   # retired: no names
    assert src.values("_acme-challenge.s-aaaa00000001.lan.axiomvision.ai") == ["v"]
    z = lan_dns.Zone(src, ZONE, "ns1.axiomvision.ai")
    assert answers(ask(f"192-168-0-9.s-aaaa00000001.{ZONE}", z=z)) == ["192.168.0.9"]
    assert ask(f"192-168-0-9.s-aaaa00000002.{ZONE}", z=z).rcode() == dns.rcode.NXDOMAIN
    with db.engine().begin() as c:
        c.execute(sa.delete(db.lan_challenges))


@pytest.mark.parametrize("name", ["", "lan.axiomvision.ai"])
def test_zone_setting(name, monkeypatch):
    from hub.config import settings
    monkeypatch.setattr(settings, "lan_zone", name)
    assert lan_dns.name_for(SID) == (f"{LABEL}.{name}" if name else None)
