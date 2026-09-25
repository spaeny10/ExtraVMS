"""Minimal ONVIF SOAP client (stdlib only): WS-UsernameToken digest auth, fault parsing, XML helpers."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import os
import socket
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

ENVELOPE = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
 xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
 xmlns:tr2="http://www.onvif.org/ver20/media/wsdl"
 xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
 xmlns:tan="http://www.onvif.org/ver20/analytics/wsdl"
 xmlns:tt="http://www.onvif.org/ver10/schema"
 xmlns:wsa="http://www.w3.org/2005/08/addressing"
 xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2">
<s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>"""

WSSE = (
    '<wsse:Security s:mustUnderstand="1" '
    'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
    'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
    "<wsse:UsernameToken><wsse:Username>{user}</wsse:Username>"
    '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>'
    '<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce}</wsse:Nonce>'
    "<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>"
)

ACTION_PULL = "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesRequest"
ACTION_UNSUB = "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeRequest"


# --------------------------------------------------------------------------- XML helpers

def local(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def find_all(el: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in el.iter() if local(e) == name]


def find(el: ET.Element | None, name: str) -> ET.Element | None:
    if el is None:
        return None
    return next((e for e in el.iter() if local(e) == name and e is not el), None)


def text(el: ET.Element | None, name: str, default: str | None = None) -> str | None:
    found = find(el, name)
    return found.text.strip() if found is not None and found.text else default


def children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if local(c) == name]


def simple_items(el: ET.Element | None) -> dict[str, str]:
    if el is None:
        return {}
    return {i.get("Name"): i.get("Value") for i in find_all(el, "SimpleItem")}


# --------------------------------------------------------------------------- SOAP client

class OnvifError(Exception):
    pass


class Onvif:
    def __init__(self, host: str, port: int, user: str, password: str, timeout: float = 10):
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.timeout = timeout
        self.device_url = f"http://{host}:{port}/onvif/device_service"
        self.clock_offset = dt.timedelta(0)
        self.services: dict[str, str] = {}

    def _security(self) -> str:
        nonce = os.urandom(16)
        created = (dt.datetime.now(dt.timezone.utc) + self.clock_offset).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        digest = hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()
        return WSSE.format(
            user=escape(self.user),
            digest=base64.b64encode(digest).decode(),
            nonce=base64.b64encode(nonce).decode(),
            created=created,
        )

    def call(self, url: str, body: str, auth: bool = True, header: str = "", timeout: float | None = None) -> ET.Element:
        hdr = (self._security() if auth else "") + header
        data = ENVELOPE.format(header=hdr, body=body).encode()
        req = urllib.request.Request(url, data, {"Content-Type": "application/soap+xml; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise OnvifError(f"HTTP {e.code}: {self._fault(e.read()) or e.reason}") from None
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            raise OnvifError(f"connection failed: {e}") from None
        root = ET.fromstring(raw)
        fault = self._fault(raw)
        if fault:
            raise OnvifError(fault)
        body_el = next((e for e in root if local(e) == "Body"), root)
        return body_el

    @staticmethod
    def _fault(raw: bytes) -> str | None:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return None
        fault = next((e for e in root.iter() if local(e) == "Fault"), None)
        if fault is None:
            return None
        reason = text(fault, "Text") or text(fault, "faultstring") or "SOAP fault"
        subcodes = [v.text for v in find_all(fault, "Value") if v.text]
        return f"{reason} ({' / '.join(subcodes)})" if subcodes else reason

    def service(self, key: str) -> str | None:
        return self.services.get(key)
