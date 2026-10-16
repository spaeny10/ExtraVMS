"""Names for Direct-on-LAN with a trusted certificate, and the authoritative DNS server that answers them.

A browser on a server's LAN fetches media from https://<a-b-c-d>.<label>.<zone>:8443 (zone = settings.lan_zone,
lan.axiomvision.ai), which resolves to the private address a.b.c.d. The server holds a Let's Encrypt certificate for
*.<label>.<zone> (lan_certs.py), so any LAN address it gets (DHCP) is covered by the one certificate and the page
stays "Secure". Names (all lowercase, case-insensitive on the wire):

  <label>               the server id with "_" -> "-": s_d6d429b617ca -> s-d6d429b617ca. Server ids are lowercase
                        letters, digits and single "_" (db.new_id), never "-", so the mapping is one-to-one and
                        server_of_label() reverses it; ids of any other shape get no label (and no certificate).
  <a-b-c-d>.<label>     A a.b.c.d when a.b.c.d is private (10/8, 172.16/12, 192.168/16, 100.64/10; no leading zeros)
                        and <label> is an enrolled, non-retired server; NXDOMAIN otherwise.
  _acme-challenge.<label>   TXT: the DNS-01 values of that server's running ACME order (lan_challenges).
  <zone>                SOA and NS (settings.lan_dns_ns); <label> itself exists with no records.

Authoritative only: never recursive, REFUSED for names outside the zone, NXDOMAIN for unknown names in it, no answer
at all to malformed packets or responses. UDP and TCP, answers of a few hundred bytes at most, a token bucket per
source address (settings.lan_dns_rate; over it a UDP query is dropped and a TCP connection closed). Queries are logged
at debug level only.

`python -m hub dns` runs it as its own process (the `dns` service in docker-compose.yml, so a port 53 that cannot be
bound never stops the hub itself). It answers from memory: the servers and pending challenges are re-read from the
hub's database every few seconds (DbSource), never per query.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time

import dns.exception
import dns.flags
import dns.message
import dns.name
import dns.opcode
import dns.rcode
import dns.rdataclass
import dns.rdatatype
import dns.rrset

from .config import settings

log = logging.getLogger("hub.lan_dns")

PRIVATE = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"))
CHALLENGE = "_acme-challenge"
A_TTL = 3600          # an address name never changes what it points at
TXT_TTL = 30
NEG_TTL = 60          # SOA minimum: how long a resolver may cache "no such name" (a challenge appears seconds later)
UDP_MAX = 1232        # the EDNS payload we answer with at most (DNS flag day 2020)
MAX_QUERY = 4096      # bigger UDP packets / TCP messages are not queries
TCP_IDLE_S = 10.0
TCP_MAX_MESSAGES = 50
TCP_MAX_CONNS = 200
LABELS_REFRESH_S = 30.0
CHALLENGES_REFRESH_S = 2.0

_ID = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_LABEL = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_OCTET = re.compile(r"^(?:0|[1-9][0-9]{0,2})$")


# ---------------------------------------------------------------- names

def zone() -> str:
    return (settings.lan_zone or "").strip().strip(".").lower()


def label_of(server_id: str | None) -> str | None:
    """The DNS label for a server id (s_d6d429b617ca -> s-d6d429b617ca), or None when the id can't be one."""
    sid = (server_id or "").strip()
    if not _ID.match(sid) or len(sid) > 63:
        return None
    label = sid.replace("_", "-")
    return None if label[2:4] == "--" else label   # "xx--" is reserved for IDNA (xn--)


def server_of_label(label: str) -> str | None:
    """label_of reversed (case-insensitive): s-d6d429b617ca -> s_d6d429b617ca; None for anything label_of never makes."""
    label = (label or "").lower()
    if not _LABEL.match(label) or len(label) > 63:
        return None
    sid = label.replace("-", "_")
    return sid if label_of(sid) == label else None


def name_for(server_id: str | None) -> str | None:
    """<label>.<zone>: the suffix of the server's LAN names; its certificate is for *.<this>. None when off."""
    label, z = label_of(server_id), zone()
    return f"{label}.{z}" if label and z else None


def ip_of_label(label: str) -> str | None:
    """a-b-c-d -> a.b.c.d when that is a private IPv4 address (canonical decimal, no leading zeros)."""
    parts = (label or "").split("-")
    if len(parts) != 4 or not all(_OCTET.match(p) and int(p) <= 255 for p in parts):
        return None
    ip = ipaddress.IPv4Address(".".join(parts))
    return str(ip) if any(ip in n for n in PRIVATE) else None


def dashed(ip: str) -> str:
    return ip.replace(".", "-")


# ---------------------------------------------------------------- what the zone holds

class MemorySource:
    """Servers (labels) and challenge TXT values held in memory; DbSource fills one from the database."""

    def __init__(self, labels: set[str] | None = None, txt: dict[str, list[str]] | None = None) -> None:
        self.labels: set[str] = set(labels or ())
        self.txt: dict[str, list[str]] = dict(txt or {})   # lowercase name without the final dot -> values

    def known(self, label: str) -> bool:
        return label in self.labels

    def values(self, name: str) -> list[str]:
        return self.txt.get(name, [])


class DbSource(MemorySource):
    """Re-reads the hub's database in the background: enrolled, non-retired servers every LABELS_REFRESH_S and the
    unexpired challenges every CHALLENGES_REFRESH_S. Its own engine, without the hub's schema upgrade (the hub process
    owns that); a table the hub hasn't created yet reads as empty."""

    def __init__(self, url: str | None = None) -> None:
        super().__init__()
        import sqlalchemy as sa
        u = url or settings.database_url
        kw = {"connect_args": {"check_same_thread": False}} if u.startswith("sqlite") else {"pool_pre_ping": True}
        self._engine = sa.create_engine(u, future=True, **kw)

    def refresh_labels(self) -> None:
        import sqlalchemy as sa

        from . import db
        with self._engine.connect() as c:
            ids = [r.id for r in c.execute(sa.select(db.sites.c.id).where(db.sites.c.retired_at.is_(None)))]
        self.labels = {lb for lb in map(label_of, ids) if lb}

    def refresh_challenges(self) -> None:
        import sqlalchemy as sa

        from . import db
        txt: dict[str, list[str]] = {}
        with self._engine.connect() as c:
            for r in c.execute(sa.select(db.lan_challenges.c.name, db.lan_challenges.c.value)
                               .where(db.lan_challenges.c.expires_at > time.time())):
                txt.setdefault(r.name.lower().rstrip("."), []).append(r.value)
        self.txt = txt

    async def run(self) -> None:
        next_labels = 0.0
        while True:
            try:
                if time.monotonic() >= next_labels:
                    await asyncio.to_thread(self.refresh_labels)
                    next_labels = time.monotonic() + LABELS_REFRESH_S
                await asyncio.to_thread(self.refresh_challenges)
            except Exception as e:   # database restarting: keep answering from what we have
                log.warning("lan dns: reading the hub database failed: %s", e)
            await asyncio.sleep(CHALLENGES_REFRESH_S)


# ---------------------------------------------------------------- rate limit

class RateLimit:
    """A token bucket per source address: `rate` per second, bursts of 2 x rate. Bounded memory: past max_keys the
    full buckets (idle sources) are forgotten, and if that isn't enough, all of them."""

    def __init__(self, rate: float, max_keys: int = 10000) -> None:
        self.rate = max(0.1, float(rate))
        self.burst = 2 * self.rate
        self.max_keys = max_keys
        self._b: dict[str, tuple[float, float]] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        tokens, at = self._b.get(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - at) * self.rate)
        ok = tokens >= 1.0
        self._b[key] = (tokens - 1.0 if ok else tokens, now)
        if len(self._b) > self.max_keys:
            self._b = {k: v for k, v in self._b.items() if v[0] + (now - v[1]) * self.rate < self.burst}
            if len(self._b) > self.max_keys:
                self._b.clear()
        return ok


# ---------------------------------------------------------------- answering

class Zone:
    """Answers one query (wire format in, wire format out, or None = say nothing)."""

    def __init__(self, source: MemorySource, zone_name: str | None = None, ns: str | None = None) -> None:
        self.source = source
        self.zone = dns.name.from_text(zone_name or zone())
        self.ns = dns.name.from_text(ns or settings.lan_dns_ns)
        self.serial = int(time.time()) & 0x7FFFFFFF

    def _soa(self) -> dns.rrset.RRset:
        return dns.rrset.from_text(self.zone, NEG_TTL, "IN", "SOA",
                                   f"{self.ns.to_text()} hostmaster.{self.zone.to_text()} {self.serial} 3600 600 86400 {NEG_TTL}")

    def _ns(self) -> dns.rrset.RRset:
        return dns.rrset.from_text(self.zone, 3600, "IN", "NS", self.ns.to_text())

    def respond(self, wire: bytes, udp: bool = True) -> bytes | None:
        if len(wire) < 12 or len(wire) > MAX_QUERY:
            return None
        try:
            q = dns.message.from_wire(wire)
        except Exception:   # anything dnspython can't parse (truncated, bad labels, TSIG, junk): no answer
            return None
        if q.flags & dns.flags.QR:
            return None   # a response, not a query: never answer those (reflection loops)
        try:
            r = dns.message.make_response(q, recursion_available=False, our_payload=UDP_MAX)
            self._fill(q, r)
            limit = 65535 if not udp else (max(512, min(q.payload, UDP_MAX)) if q.edns >= 0 else 512)
            try:
                return r.to_wire(max_size=limit)
            except dns.exception.TooBig:
                r.answer.clear()
                r.authority.clear()
                r.additional.clear()
                r.flags |= dns.flags.TC
                return r.to_wire(max_size=limit)
        except Exception:
            log.debug("lan dns: could not answer a query", exc_info=True)
            return None

    def _fill(self, q: dns.message.Message, r: dns.message.Message) -> None:
        if q.opcode() != dns.opcode.QUERY:
            r.set_rcode(dns.rcode.NOTIMP)
            return
        if len(q.question) != 1:
            r.set_rcode(dns.rcode.FORMERR)
            return
        question = q.question[0]
        qname, qtype = question.name, question.rdtype
        if question.rdclass not in (dns.rdataclass.IN, dns.rdataclass.ANY) or not qname.is_subdomain(self.zone):
            r.set_rcode(dns.rcode.REFUSED)
            return
        r.flags |= dns.flags.AA
        rel = [lb.decode("ascii", "replace").lower() for lb in qname.relativize(self.zone).labels if lb]
        any_type = qtype == dns.rdatatype.ANY
        if not rel:   # the apex
            if qtype == dns.rdatatype.SOA or any_type:
                r.answer.append(self._soa())
            if qtype == dns.rdatatype.NS or any_type:
                r.answer.append(self._ns())
            if not r.answer:
                r.authority.append(self._soa())
            log.debug("lan dns: %s %s -> apex", qname, dns.rdatatype.to_text(qtype))
            return
        server = rel[-1]
        if not self.source.known(server) or len(rel) > 2:
            return self._nx(r, qname, qtype)
        if len(rel) == 1:   # <label> itself: exists (names below it do), no records
            r.authority.append(self._soa())
            return
        sub = rel[0]
        if sub == CHALLENGE:
            values = self.source.values(f"{CHALLENGE}.{server}.{self.zone.to_text(omit_final_dot=True)}")
            if (qtype == dns.rdatatype.TXT or any_type) and values:
                r.answer.append(dns.rrset.from_text(qname, TXT_TTL, "IN", "TXT", *(f'"{v}"' for v in values[:4])))
            else:
                r.authority.append(self._soa())
            log.debug("lan dns: %s %s -> %d challenge value(s)", qname, dns.rdatatype.to_text(qtype), len(values))
            return
        ip = ip_of_label(sub)
        if ip is None:
            return self._nx(r, qname, qtype)
        if qtype == dns.rdatatype.A or any_type:
            r.answer.append(dns.rrset.from_text(qname, A_TTL, "IN", "A", ip))
        else:
            r.authority.append(self._soa())
        log.debug("lan dns: %s %s -> %s", qname, dns.rdatatype.to_text(qtype), ip)

    def _nx(self, r: dns.message.Message, qname, qtype) -> None:
        r.set_rcode(dns.rcode.NXDOMAIN)
        r.authority.append(self._soa())
        log.debug("lan dns: %s %s -> NXDOMAIN", qname, dns.rdatatype.to_text(qtype))


# ---------------------------------------------------------------- serving

class _Udp(asyncio.DatagramProtocol):
    def __init__(self, zone_: Zone, limit: RateLimit) -> None:
        self.zone, self.limit = zone_, limit
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            if not self.limit.allow(addr[0]):
                return
            out = self.zone.respond(data, udp=True)
            if out and self.transport is not None:
                self.transport.sendto(out, addr)
        except Exception:   # never let one packet take the listener down
            log.debug("lan dns: udp packet from %s", addr[0], exc_info=True)

    def error_received(self, exc) -> None:   # ICMP unreachable for an earlier answer: nothing to do
        pass


class Server:
    """The UDP and TCP listeners of one Zone."""

    def __init__(self, zone_: Zone, rate: float | None = None) -> None:
        self.zone = zone_
        self.limit = RateLimit(settings.lan_dns_rate if rate is None else rate)
        self.udp: asyncio.DatagramTransport | None = None
        self.tcp: asyncio.AbstractServer | None = None
        self._conns = 0

    async def start(self, host: str, port: int) -> tuple[int, int]:
        """Listen; returns the (udp, tcp) ports actually bound (port 0 = any, for tests)."""
        loop = asyncio.get_running_loop()
        self.udp, _ = await loop.create_datagram_endpoint(lambda: _Udp(self.zone, self.limit), local_addr=(host, port))
        udp_port = self.udp.get_extra_info("sockname")[1]
        self.tcp = await asyncio.start_server(self._tcp, host, port or udp_port)
        return udp_port, self.tcp.sockets[0].getsockname()[1]

    async def _tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = (writer.get_extra_info("peername") or ("?",))[0]
        self._conns += 1
        try:
            if self._conns > TCP_MAX_CONNS:
                return
            for _ in range(TCP_MAX_MESSAGES):
                n = int.from_bytes(await asyncio.wait_for(reader.readexactly(2), TCP_IDLE_S), "big")
                if n < 12 or n > MAX_QUERY:
                    return
                data = await asyncio.wait_for(reader.readexactly(n), TCP_IDLE_S)
                if not self.limit.allow(peer):
                    return
                out = self.zone.respond(data, udp=False)
                if out is None:
                    return
                writer.write(len(out).to_bytes(2, "big") + out)
                await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, OSError):
            pass
        except Exception:
            log.debug("lan dns: tcp connection from %s", peer, exc_info=True)
        finally:
            self._conns -= 1
            writer.close()

    def close(self) -> None:
        if self.udp is not None:
            self.udp.close()
        if self.tcp is not None:
            self.tcp.close()


async def serve_forever() -> None:
    """`python -m hub dns`: the zone from settings, answered from the hub's database."""
    if not zone():
        log.warning("lan dns: HUB_LAN_ZONE is empty; nothing to serve")
        while True:
            await asyncio.sleep(3600)
    source = DbSource()
    refresher = asyncio.create_task(source.run(), name="lan-dns-refresh")
    srv = Server(Zone(source))
    udp, tcp = await srv.start(settings.lan_dns_host, settings.lan_dns_port)
    log.info("lan dns: authoritative for %s (NS %s) on %s udp/%s tcp/%s, %.0f queries/s per source",
             zone(), settings.lan_dns_ns, settings.lan_dns_host, udp, tcp, settings.lan_dns_rate)
    try:
        await refresher
    finally:
        srv.close()
