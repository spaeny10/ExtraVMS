"""Camera settings over ONVIF (Settings › Cameras › Camera): what is usually changed on the camera's own web page,
made from here: its clock (NTP servers, time zone, a one-off set from this server's clock), its video encoders
(resolution, frame rate, bitrate, keyframe interval), its audio encoder (G.711 -> AAC), imaging, its analytics rules
(switching a rule with an Enable parameter on or off) and a reboot. device_api.py serves it.

Every write is a read-modify-write of the camera's own XML: the configuration GetVideoEncoderConfiguration (etc.)
returned is copied, the fields asked for are changed and the whole element is sent back, so whatever else the camera
keeps there (vendor extensions, multicast, session timeout, H.264 profile) is unchanged. Values are checked against
the camera's own options (GetVideoEncoderConfigurationOptions ...) before anything is written.

Media2 is preferred over Media for encoders when the camera has both: Media can only say H.264, MPEG-4 or JPEG, and
some cameras report an H.265 stream there as H.264 (a write back would switch it to H.264). A dual-sensor camera
(the Uniview TIC2A32SA: visible + thermal, Media2 only) lists each sensor's encoders; `source` says which.

Time zones are POSIX TZ strings on the camera ("CST6CDT,M3.2.0,M11.1.0"); an IANA name ("America/Chicago") is
converted from the time zone database (posix_tz). Some cameras use their own notation (Milesight reports
"UTC+06:00:00"): a camera that refuses the POSIX string gets its clock set without the zone, and says so (warning).

Blocking (urllib): call from a worker thread. Camera errors are passed on verbatim, without credentials (clean()).
"""
from __future__ import annotations

import copy
import datetime as dt
import ipaddress
import logging
import os
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from . import onvif_soap as soap
from .onvif_soap import OnvifError, find, find_all, local, text

log = logging.getLogger("nvr.camdevice")

CALL_TIMEOUT_S = 6
NS = {"tds": "http://www.onvif.org/ver10/device/wsdl", "trt": "http://www.onvif.org/ver10/media/wsdl",
      "tr2": "http://www.onvif.org/ver20/media/wsdl", "tt": "http://www.onvif.org/ver10/schema",
      "timg": "http://www.onvif.org/ver20/imaging/wsdl", "tan": "http://www.onvif.org/ver20/analytics/wsdl"}
for _p, _u in NS.items():
    ET.register_namespace(_p, _u)   # a configuration sent back uses the usual prefixes, not ns0:
ENVELOPE_PREFIXES = {"s", "tds", "trt", "tr2", "tev", "tan", "tt", "wsa", "wsnt", "tptz", "timg", "tmd"}

# the encodings as shown and asked for; what the camera calls them is kept for writing back
VIDEO_ENCODINGS = {"H264": "H264", "H.264": "H264", "AVC": "H264", "H265": "H265", "H.265": "H265", "HEVC": "H265",
                   "JPEG": "JPEG", "MJPEG": "JPEG", "MPEG4": "MPEG4", "MPEG-4": "MPEG4"}
AUDIO_ENCODINGS = {"G711": "G711", "PCMU": "G711", "PCMA": "G711", "G.711": "G711", "AAC": "AAC", "MP4A-LATM": "AAC",
                   "MPEG4-GENERIC": "AAC", "G726": "G726", "G.726": "G726"}
# a rule parameter that switches it on or off: Enable, Enabled, FieldEnable (Milesight), IsEnabled
TOGGLE_PARAM = re.compile(r"^(is)?\w*enabled?$", re.I)
TRUE_FALSE = {"true": True, "false": False, "1": True, "0": False}
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-:]{0,252}$")
POSIX_TZ_RE = re.compile(r"^[A-Za-z<>+\-0-9:,./]{2,64}$")
IANA_RE = re.compile(r"^(UTC|[A-Z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){1,2})$")


class Unsupported(OnvifError):
    """The camera has no such service or setting (answered 409, shown as "Not supported by this camera")."""


class Invalid(ValueError):
    """A value outside what the camera offers: refused before anything is written (answered 422)."""


# --------------------------------------------------------------------------- helpers

def clean(msg: str, cam: dict | None) -> str:
    """A camera's error message without credentials: user:password@ in URLs, and the camera's password anywhere."""
    s = re.sub(r"(//)[^/@\s]+@", r"\1", str(msg))
    pw = (cam or {}).get("password")
    if pw and len(pw) >= 3:
        s = s.replace(pw, "***")
    return s[:500]


def _num(s: str | None) -> float | None:
    try:
        return float(s) if s not in (None, "") else None
    except ValueError:
        return None


def _int(s: str | None) -> int | None:
    v = _num(s)
    return int(round(v)) if v is not None else None


def _fmt(v: float) -> str:
    """A number as XML text: 25 not 25.0."""
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def _range(el: ET.Element | None) -> dict | None:
    if el is None:
        return None
    lo, hi = _num(text(el, "Min")), _num(text(el, "Max"))
    return {"min": lo, "max": hi} if lo is not None and hi is not None else None


def _items(el: ET.Element | None) -> list[float]:
    """IntList / FloatList values: <Items>8 16</Items> or repeated <Items>."""
    if el is None:
        return []
    out: list[float] = []
    for i in find_all(el, "Items"):
        for part in (i.text or "").split():
            v = _num(part)
            if v is not None:
                out.append(v)
    return out


def _child(el: ET.Element | None, name: str) -> ET.Element | None:
    if el is None:
        return None
    return next((c for c in el if local(c) == name), None)


def _set_text(el: ET.Element | None, name: str, value: str) -> bool:
    """Set the text of `el`'s first descendant called `name`; False when there is none (nothing is added)."""
    found = find(el, name)
    if found is None:
        return False
    found.text = value
    return True


def xml_of(el: ET.Element, tag: str | None = None, nsmap: dict[str, str] | None = None) -> str:
    """An element of a camera's reply as XML to send back (renamed to `tag`). QName values the camera wrote with its own
    prefixes (a rule's Type="ns2:CellMotionDetector") keep working: the prefix is declared from the camera's reply."""
    el = copy.deepcopy(el)
    if tag:
        el.tag = tag
    out = ET.tostring(el, encoding="unicode")
    if nsmap:
        used = set(re.findall(r'(?:Type|type)="([A-Za-z_][\w.\-]*):', out))
        extra = "".join(f' xmlns:{p}="{escape(nsmap[p])}"' for p in sorted(used)
                        if p in nsmap and p not in ENVELOPE_PREFIXES and f"xmlns:{p}=" not in out)
        if extra:
            out = re.sub(r"^(<[^\s>/]+)", lambda m: m.group(1) + extra, out, count=1)
    return out


def raw_nsmap(raw: bytes) -> dict[str, str]:
    """The prefixes a reply declares (ElementTree drops them; a QName attribute value still names one)."""
    return {p.decode(): u.decode() for p, u in re.findall(rb'xmlns:([\w.\-]+)="([^"]*)"', raw or b"")}


def connect(cam: dict, timeout: float = CALL_TIMEOUT_S) -> soap.Onvif:
    """A client for the camera with its services discovered (clock offset learned first, so the digest is accepted
    by a camera whose clock is far off: the very cameras whose clocks need fixing)."""
    c = soap.Onvif.for_camera(cam, timeout=timeout)
    try:
        soap.sync_clock(c)
    except OnvifError:
        pass   # some cameras refuse it unauthenticated; the digest may still be accepted
    soap.discover_services(c)
    c.services.setdefault("device", c.device_url)
    return c


def media(c: soap.Onvif) -> tuple[str, str]:
    """("media2" | "media", its address): Media2 first (H.265, dual sensors)."""
    if c.service("media2"):
        return "media2", c.service("media2")
    if c.service("media"):
        return "media", c.service("media")
    raise Unsupported("the camera reports no ONVIF media service")


# camera locks: one write per camera at a time (two Applies pressed together would read the same old XML)
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def lock(camera_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(camera_id, threading.Lock())


# --------------------------------------------------------------------------- time zones

def posix_tz(name: str | None) -> str | None:
    """An IANA zone ("America/Chicago") -> its POSIX TZ rule ("CST6CDT,M3.2.0,M11.1.0"), from the footer of the zone's
    TZif file (tzdata package, else the system zone files). A POSIX string is returned as given; None if unknown."""
    if not name:
        return None
    name = name.strip()
    if name.upper() in ("UTC", "ETC/UTC", "GMT"):
        return "UTC0"
    if not IANA_RE.match(name) or ".." in name:
        return name if POSIX_TZ_RE.match(name) and re.match(r"^[A-Za-z<]", name) and re.search(r"\d", name) else None
    data = None
    try:
        from importlib import resources
        data = resources.files("tzdata.zoneinfo").joinpath(*name.split("/")).read_bytes()
    except Exception:  # noqa: BLE001 - no tzdata package, or no such zone in it
        import zoneinfo
        for base in zoneinfo.TZPATH:
            path = os.path.join(base, *name.split("/"))
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    data = f.read()
                break
    if not data or not data.startswith(b"TZif"):
        return None
    footer = data.rstrip(b"\n").rsplit(b"\n", 1)[-1].decode("ascii", "replace").strip()
    return footer if footer and POSIX_TZ_RE.match(footer) else None


def utc_offset_s(zone: str | None, at: float | None = None) -> int | None:
    """The zone's offset from UTC at `at` (now), daylight saving included; None for an unknown zone."""
    if not zone:
        return None
    try:
        import zoneinfo
        z = zoneinfo.ZoneInfo(zone)
    except Exception:  # noqa: BLE001
        return None
    off = dt.datetime.fromtimestamp(at or time.time(), z).utcoffset()
    return int(off.total_seconds()) if off is not None else None


# Windows zone names -> IANA (the common ones: this server's own zone, as a default)
WINDOWS_ZONES = {
    "Eastern Standard Time": "America/New_York", "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver", "US Mountain Standard Time": "America/Phoenix",
    "Pacific Standard Time": "America/Los_Angeles", "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu", "Atlantic Standard Time": "America/Halifax",
    "Newfoundland Standard Time": "America/St_Johns", "Central Standard Time (Mexico)": "America/Mexico_City",
    "Canada Central Standard Time": "America/Regina", "US Eastern Standard Time": "America/Indianapolis",
    "GMT Standard Time": "Europe/London", "W. Europe Standard Time": "Europe/Berlin", "Romance Standard Time": "Europe/Paris",
    "Central Europe Standard Time": "Europe/Budapest", "Central European Standard Time": "Europe/Warsaw",
    "E. Europe Standard Time": "Europe/Chisinau", "FLE Standard Time": "Europe/Kiev", "GTB Standard Time": "Europe/Bucharest",
    "South Africa Standard Time": "Africa/Johannesburg", "Arabian Standard Time": "Asia/Dubai", "India Standard Time": "Asia/Kolkata",
    "China Standard Time": "Asia/Shanghai", "Singapore Standard Time": "Asia/Singapore", "Tokyo Standard Time": "Asia/Tokyo",
    "AUS Eastern Standard Time": "Australia/Sydney", "E. Australia Standard Time": "Australia/Brisbane",
    "W. Australia Standard Time": "Australia/Perth", "New Zealand Standard Time": "Pacific/Auckland",
    "SA Pacific Standard Time": "America/Bogota", "E. South America Standard Time": "America/Sao_Paulo", "UTC": "UTC",
}


def server_zone() -> str | None:
    """This server's own IANA zone, best effort (TZ, the Windows registry, /etc/timezone, /etc/localtime)."""
    tz = os.environ.get("TZ", "")
    if IANA_RE.match(tz) and "/" in tz:
        return tz
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation") as k:
                return WINDOWS_ZONES.get(winreg.QueryValueEx(k, "TimeZoneKeyName")[0].strip("\x00 "))
        except OSError:
            return None
    try:
        with open("/etc/timezone", encoding="utf-8") as f:
            v = f.read().strip()
            if IANA_RE.match(v):
                return v
    except OSError:
        pass
    real = os.path.realpath("/etc/localtime")
    if "zoneinfo/" in real:
        v = real.split("zoneinfo/", 1)[1]
        return v if IANA_RE.match(v) else None
    return None


_gateway: tuple[float, str | None] | None = None


def default_gateway() -> str | None:
    """This server's IPv4 default gateway (cached 10 minutes): offered as an NTP server for cameras that can't
    reach the internet (only if the router answers NTP)."""
    global _gateway
    if _gateway and time.time() - _gateway[0] < 600:
        return _gateway[1]
    gw = None
    try:
        if sys.platform == "win32":
            out = subprocess.run(["route", "print", "-4", "0.0.0.0"], capture_output=True, text=True, timeout=3,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            m = re.search(r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)\s", out, re.M)
            gw = m.group(1) if m else None
        else:
            with open("/proc/net/route", encoding="ascii") as f:
                for line in f.read().splitlines()[1:]:
                    parts = line.split()
                    if len(parts) > 2 and parts[1] == "00000000":
                        gw = str(ipaddress.IPv4Address(bytes.fromhex(parts[2])[::-1]))
                        break
    except (OSError, ValueError, subprocess.SubprocessError):
        gw = None
    _gateway = (time.time(), gw)
    return gw


# --------------------------------------------------------------------------- device information and time

def read_info(c: soap.Onvif) -> dict:
    return soap.parse_device_info(c.call(c.device_url, "<tds:GetDeviceInformation/>"))


def _datetime(el: ET.Element | None) -> dt.datetime | None:
    if el is None:
        return None
    d, t = find(el, "Date"), find(el, "Time")
    try:
        return dt.datetime(int(text(d, "Year")), int(text(d, "Month")), int(text(d, "Day")),
                           int(text(t, "Hour")), int(text(t, "Minute")), int(text(t, "Second")))
    except (TypeError, ValueError):
        return None


def parse_time(body: ET.Element, now: float | None = None, zone: str | None = None) -> dict:
    """GetSystemDateAndTime reply -> {mode, dst, timezone, camera_utc, offset_s, local_minus_utc_s, zone_check}.
    offset_s: the camera's clock minus this server's (positive = the camera is ahead). zone_check (with `zone`): does
    the camera's local time differ from its UTC by what that zone says now (a wrong zone or daylight saving shows
    here, the camera's UTC being right)."""
    now = time.time() if now is None else now
    utc = _datetime(find(body, "UTCDateTime"))
    loc = _datetime(find(body, "LocalDateTime"))
    out = {"mode": text(body, "DateTimeType"), "dst": (text(body, "DaylightSavings") or "").lower() == "true",
           "timezone": text(find(body, "TimeZone"), "TZ"), "camera_utc": None, "offset_s": None, "local_minus_utc_s": None,
           "camera_local": loc.isoformat() if loc else None, "zone_check": None}
    if utc:
        cam_ts = utc.replace(tzinfo=dt.timezone.utc).timestamp()
        out["camera_utc"] = utc.replace(tzinfo=dt.timezone.utc).isoformat().replace("+00:00", "Z")
        out["offset_s"] = round(cam_ts - now, 1)
        if loc:
            out["local_minus_utc_s"] = int(round((loc - utc).total_seconds() / 60.0)) * 60
    expected = utc_offset_s(zone, now)
    if expected is not None and out["local_minus_utc_s"] is not None:
        out["zone_check"] = {"zone": zone, "expected_s": expected, "camera_s": out["local_minus_utc_s"],
                             "ok": abs(expected - out["local_minus_utc_s"]) < 120}
    return out


def parse_ntp(body: ET.Element) -> dict:
    """GetNTP reply -> {from_dhcp, servers (the manual ones), dhcp_servers}."""
    info = find(body, "NTPInformation")

    def addr(el: ET.Element) -> str | None:
        return text(el, "DNSname") or text(el, "IPv4Address") or text(el, "IPv6Address")
    manual = [a for a in (addr(e) for e in find_all(info, "NTPManual")) if a] if info is not None else []
    dhcp = [a for a in (addr(e) for e in find_all(info, "NTPFromDHCP")) if a] if info is not None else []
    return {"from_dhcp": (text(info, "FromDHCP") or "").lower() == "true", "servers": manual, "dhcp_servers": dhcp}


def read_time(c: soap.Onvif, zone: str | None = None) -> dict:
    try:
        body = c.call(c.device_url, "<tds:GetSystemDateAndTime/>")
    except OnvifError:
        body = c.call(c.device_url, "<tds:GetSystemDateAndTime/>", auth=False)
    out = parse_time(body, zone=zone)
    try:
        out["ntp"] = parse_ntp(c.call(c.device_url, "<tds:GetNTP/>"))
    except OnvifError as e:
        out["ntp"] = None
        out["ntp_error"] = clean(str(e), c.cam)
    out["ntp_servers"] = (out["ntp"] or {}).get("servers") or (out["ntp"] or {}).get("dhcp_servers") or []
    out["supported"] = True
    return out


def ntp_body(servers: list[str]) -> str:
    items = []
    for s in servers:
        try:
            ip = ipaddress.ip_address(s)
            kind = "IPv4" if ip.version == 4 else "IPv6"
            items.append(f"<tds:NTPManual><tt:Type>{kind}</tt:Type><tt:{kind}Address>{escape(s)}</tt:{kind}Address></tds:NTPManual>")
        except ValueError:
            items.append(f"<tds:NTPManual><tt:Type>DNS</tt:Type><tt:DNSname>{escape(s)}</tt:DNSname></tds:NTPManual>")
    return f"<tds:SetNTP><tds:FromDHCP>false</tds:FromDHCP>{''.join(items)}</tds:SetNTP>"


def datetime_body(mode: str, tz: str | None, now: float | None = None, dst: bool = False) -> str:
    """SetSystemDateAndTime: NTP or Manual; a Manual set carries this server's UTC time now. With a zone, DaylightSavings
    is true when it has daylight-saving rules (the POSIX rule has a second part); `tz` None leaves the zone out and
    sends `dst` (the camera's own setting: the element is required, and false would switch its daylight saving off)."""
    dst = bool(tz and "," in tz) if tz else dst
    zone = f"<tds:TimeZone><tt:TZ>{escape(tz)}</tt:TZ></tds:TimeZone>" if tz else ""
    utc = ""
    if mode == "Manual":
        t = dt.datetime.fromtimestamp(time.time() if now is None else now, dt.timezone.utc)
        utc = (f"<tds:UTCDateTime><tt:Date><tt:Year>{t.year}</tt:Year><tt:Month>{t.month}</tt:Month><tt:Day>{t.day}</tt:Day></tt:Date>"
               f"<tt:Time><tt:Hour>{t.hour}</tt:Hour><tt:Minute>{t.minute}</tt:Minute><tt:Second>{t.second}</tt:Second></tt:Time></tds:UTCDateTime>")
    return (f"<tds:SetSystemDateAndTime><tds:DateTimeType>{mode}</tds:DateTimeType>"
            f"<tds:DaylightSavings>{'true' if dst else 'false'}</tds:DaylightSavings>{zone}{utc}</tds:SetSystemDateAndTime>")


def _resync(c: soap.Onvif) -> None:
    """The camera's clock just moved: learn the new offset, or the next digest is refused as stale."""
    try:
        soap.sync_clock(c)
    except OnvifError:
        c.clock_offset = dt.timedelta(0)   # set from this server's clock: no offset left


def _current_dst(c: soap.Onvif) -> bool:
    try:
        return parse_time(c.call(c.device_url, "<tds:GetSystemDateAndTime/>", auth=False))["dst"]
    except OnvifError:
        return False


def _set_datetime(c: soap.Onvif, mode: str, tz: str | None, warnings: list[str]) -> None:
    """SetSystemDateAndTime; a camera that refuses the zone (its own TZ notation) is set without it. Without a zone the
    camera's own daylight-saving setting is sent back as it is."""
    try:
        c.call(c.device_url, datetime_body(mode, tz, dst=False if tz else _current_dst(c)))
    except OnvifError as e:
        if not tz or "NotAuthorized" in str(e):
            raise
        log.info("SetSystemDateAndTime with TZ %s refused (%s): again without the time zone", tz, e)
        c.call(c.device_url, datetime_body(mode, None, dst=_current_dst(c)))
        warnings.append(f"The camera refused the time zone {tz} ({clean(str(e), c.cam)}); its own time zone was kept. "
                        "Set the zone on the camera's web page.")
    _resync(c)   # set by hand, or the camera's NTP client stepped it at once


STEP_S = 5   # a camera this far off after switching to NTP is set from this server's clock at once


def set_time(cam: dict, mode: str, ntp_servers: list[str], tz: str | None, zone: str | None = None) -> dict:
    """mode "ntp": SetNTP (these servers, DHCP off) and SetSystemDateAndTime NTP with the zone; a camera still more than
    STEP_S off is then set from this server's clock (Manual) and put back on NTP, so it is right at once instead of
    whenever its NTP client next runs. mode "manual_sync": SetSystemDateAndTime Manual with this server's UTC time now
    (a camera that can't reach an NTP server). Returns the time as read back, with `warnings`."""
    for s in ntp_servers:
        if not HOST_RE.match(s):
            raise Invalid(f"{s[:60]!r} is not an NTP server name or address")
    if mode == "ntp" and not ntp_servers:
        raise Invalid("name at least one NTP server")
    warnings: list[str] = []
    with lock(cam["id"]):
        c = connect(cam)
        if mode == "ntp":
            c.call(c.device_url, ntp_body(ntp_servers))
            _set_datetime(c, "NTP", tz, warnings)
            off = read_time(c).get("offset_s")
            if off is not None and abs(off) > STEP_S:
                _set_datetime(c, "Manual", None, warnings)
                _set_datetime(c, "NTP", None, warnings)
                warnings.append(f"The camera was {abs(off):.0f} s off: set from this server's clock, then back on NTP.")
        else:
            _set_datetime(c, "Manual", tz, warnings)
        _resync(c)
        out = read_time(c, zone)
    return {**out, "warnings": warnings}


def sync_now(cam: dict, zone: str | None = None) -> dict:
    """Set the camera's clock from this server's now (Manual). A camera that was on NTP is put back on NTP after, so
    it is right at once and keeps syncing."""
    warnings: list[str] = []
    with lock(cam["id"]):
        c = connect(cam)
        before = read_time(c)
        _set_datetime(c, "Manual", None, warnings)
        if (before.get("mode") or "").upper() == "NTP":
            _set_datetime(c, "NTP", None, warnings)
        _resync(c)
        out = read_time(c, zone)
    return {**out, "warnings": warnings}


def reboot(cam: dict) -> str:
    with lock(cam["id"]):
        c = connect(cam)
        body = c.call(c.device_url, "<tds:SystemReboot/>")
    return text(body, "Message") or "rebooting"


# --------------------------------------------------------------------------- profiles

def parse_media_profiles(body: ET.Element) -> list[dict]:
    """GetProfiles (Media or Media2) -> [{token, name, source, video_encoder, audio_encoder, analytics}] (configuration
    tokens; None where the profile has none)."""
    out = []
    for prof in find_all(body, "Profiles"):
        def tok(*names: str) -> str | None:
            # the configuration with a token: Media2's Metadata has an <Analytics>true</Analytics> flag of the same name
            for n in names:
                el = next((e for e in find_all(prof, n) if e is not prof and e.get("token")), None)
                if el is not None:
                    return el.get("token")
            return None
        vsc = find(prof, "VideoSourceConfiguration")
        if vsc is None:
            vsc = find(prof, "VideoSource")
        source = (text(vsc, "SourceToken") or vsc.get("token")) if vsc is not None else None
        name = soap.children(prof, "Name")
        out.append({"token": prof.get("token") or "", "name": (name[0].text or "").strip() if name else "", "source": source,
                    "video_encoder": tok("VideoEncoderConfiguration", "VideoEncoder"),
                    "audio_encoder": tok("AudioEncoderConfiguration", "AudioEncoder"),
                    "analytics": tok("VideoAnalyticsConfiguration", "Analytics")})
    return out


def read_profiles(c: soap.Onvif) -> tuple[str, list[dict]]:
    kind, url = media(c)
    body = c.call(url, "<tr2:GetProfiles><tr2:Type>All</tr2:Type></tr2:GetProfiles>" if kind == "media2" else "<trt:GetProfiles/>")
    return kind, parse_media_profiles(body)


# --------------------------------------------------------------------------- video encoders

def _get_video_config(c: soap.Onvif, kind: str, url: str, token: str) -> ET.Element:
    t = escape(token)
    if kind == "media2":
        body = c.call(url, f"<tr2:GetVideoEncoderConfigurations><tr2:ConfigurationToken>{t}</tr2:ConfigurationToken></tr2:GetVideoEncoderConfigurations>")
        els = [e for e in find_all(body, "Configurations") if e.get("token") == token] or find_all(body, "Configurations")
    else:
        body = c.call(url, f"<trt:GetVideoEncoderConfiguration><trt:ConfigurationToken>{t}</trt:ConfigurationToken></trt:GetVideoEncoderConfiguration>")
        els = find_all(body, "Configuration")
    if not els:
        raise OnvifError(f"the camera returned no video encoder configuration {token}")
    return els[0]


def _get_video_options(c: soap.Onvif, kind: str, url: str, token: str, profile: str | None = None) -> ET.Element:
    ns = "tr2" if kind == "media2" else "trt"
    p = f"<{ns}:ProfileToken>{escape(profile)}</{ns}:ProfileToken>" if profile else ""
    return c.call(url, f"<{ns}:GetVideoEncoderConfigurationOptions><{ns}:ConfigurationToken>{escape(token)}</{ns}:ConfigurationToken>"
                       f"{p}</{ns}:GetVideoEncoderConfigurationOptions>")


def _gov_el(cfg: ET.Element) -> ET.Element | None:
    """Media's GovLength element (inside H264 / MPEG4 / H265); Media2 has an attribute instead."""
    return find(cfg, "GovLength")


def parse_video_config(cfg: ET.Element) -> dict:
    rc = find(cfg, "RateControl")
    res = find(cfg, "Resolution")
    enc_raw = text(cfg, "Encoding") or cfg.get("Encoding") or ""
    gov = _int(cfg.get("GovLength")) if cfg.get("GovLength") else _int(text(cfg, "GovLength"))
    fps = _num(text(rc, "FrameRateLimit"))
    return {"token": cfg.get("token") or "", "name": text(cfg, "Name") or "",
            "encoding": VIDEO_ENCODINGS.get(enc_raw.strip().upper(), enc_raw.strip()), "encoding_raw": enc_raw.strip(),
            "width": _int(text(res, "Width")), "height": _int(text(res, "Height")),
            "fps": fps if fps is None or not fps.is_integer() else int(fps),
            "bitrate_kbps": _int(text(rc, "BitrateLimit")), "gov_length": gov,
            "gov_seconds": round(gov / fps, 2) if gov and fps else None,
            "quality": _num(text(cfg, "Quality")),
            "cbr": (rc.get("ConstantBitRate") or "").lower() == "true" if rc is not None and rc.get("ConstantBitRate") else None,
            "profile": cfg.get("Profile") or text(find(cfg, "H264"), "H264Profile")}


def _resolutions(el: ET.Element | None) -> list[dict]:
    if el is None:
        return []
    out = []
    for r in soap.children(el, "ResolutionsAvailable"):
        w, h = _int(text(r, "Width")), _int(text(r, "Height"))
        if w and h:
            out.append({"width": w, "height": h})
    return out


def parse_video_options(body: ET.Element, kind: str) -> dict:
    """GetVideoEncoderConfigurationOptions -> {quality, encodings: [{encoding, encoding_raw, resolutions, fps: {min, max}
    or None, fps_list, bitrate: {min, max} or None, gov: {min, max} or None}]}."""
    encs: list[dict] = []
    quality = None
    if kind == "media2":
        for o in find_all(body, "Options"):
            raw = (text(o, "Encoding") or o.get("Encoding") or "").strip()
            if not raw:
                continue
            gov = [_int(x) for x in (o.get("GovLengthRange") or "").split()]
            fps_list = sorted({v for v in (_num(x) for x in (o.get("FrameRatesSupported") or "").split()) if v})
            quality = quality or _range(_child(o, "QualityRange"))
            encs.append({"encoding": VIDEO_ENCODINGS.get(raw.upper(), raw), "encoding_raw": raw,
                         "resolutions": _resolutions(o), "fps_list": fps_list,
                         "fps": {"min": fps_list[0], "max": fps_list[-1]} if fps_list else None,
                         "bitrate": _range(_child(o, "BitrateRange")),
                         "gov": {"min": gov[0], "max": gov[-1]} if len(gov) >= 2 and None not in gov else None})
    else:
        opts = find(body, "Options")
        quality = _range(_child(opts, "QualityRange"))
        ext = _child(opts, "Extension")
        for name in ("H264", "H265", "MPEG4", "JPEG"):
            o = _child(opts, name)
            if o is None:
                continue
            x = _child(ext, name)
            encs.append({"encoding": name, "encoding_raw": name, "resolutions": _resolutions(o), "fps_list": [],
                         "fps": _range(_child(o, "FrameRateRange")), "bitrate": _range(_child(x, "BitrateRange")),
                         "gov": _range(_child(o, "GovLengthRange"))})
    return {"quality": quality, "encodings": encs}


def _stream_of(profile_tokens: list[str], cam: dict) -> str | None:
    """"main" / "sub" when one of these profiles is the stream this server records or shows (streams.py's probe)."""
    from . import streams
    st = streams.state_of(cam)
    for p in st.get("profiles") or []:
        if p.get("token") in profile_tokens:
            if streams.same_path(cam.get("main_path"), p.get("path")):
                return "main"
            if streams.same_path(cam.get("sub_path"), p.get("path")) or streams.same_path(streams.plan(cam).get("sub_path"), p.get("path")):
                return "sub"
    return None


def read_video(c: soap.Onvif, cam: dict, profiles: list[dict] | None = None, kind: str | None = None) -> dict:
    if profiles is None:
        kind, profiles = read_profiles(c)
    _, url = media(c)
    items: dict[str, dict] = {}
    for p in profiles:
        tok = p.get("video_encoder")
        if not tok:
            continue
        if tok in items:
            items[tok]["profiles"].append(p["token"])
            continue
        cfg = parse_video_config(_get_video_config(c, kind, url, tok))
        try:
            opts = parse_video_options(_get_video_options(c, kind, url, tok, p["token"]), kind)
        except OnvifError as e:
            opts = {"quality": None, "encodings": [], "error": clean(str(e), cam)}
        items[tok] = {**cfg, "token": tok, "profiles": [p["token"]], "profile_name": p.get("name"), "source": p.get("source"),
                      "media": kind, "options": opts}
    out = list(items.values())
    for v in out:
        v["stream"] = _stream_of(v["profiles"], cam)
    if not out:
        raise Unsupported("the camera lists no video encoder")
    sources = {v["source"] for v in out if v.get("source")}
    return {"supported": True, "media": kind, "encoders": out, "sources": sorted(sources)}


def _pick(opts: dict, encoding: str) -> dict | None:
    return next((e for e in opts.get("encodings") or [] if e["encoding"] == encoding), None)


def _in_range(v: float, r: dict | None) -> bool:
    return r is None or (r["min"] - 1e-6 <= v <= r["max"] + 1e-6)


def plan_video(cur: dict, opts: dict, kind: str, changes: dict) -> dict:
    """The values to write, checked against the camera's options: {encoding?, width?, height?, fps?, bitrate_kbps?,
    gov_length?}. gov_seconds becomes frames at the new frame rate (round(seconds x fps)). Invalid for anything the
    camera does not offer; nothing is written then."""
    new: dict = {}
    encoding = cur["encoding"]
    if changes.get("encoding") and VIDEO_ENCODINGS.get(str(changes["encoding"]).upper(), changes["encoding"]) != cur["encoding"]:
        encoding = VIDEO_ENCODINGS.get(str(changes["encoding"]).upper(), str(changes["encoding"]))
        if kind != "media2":
            raise Invalid("this camera's encoding can only be changed on its web page (it has no ONVIF Media2 service)")
        if not _pick(opts, encoding):
            raise Invalid(f"the camera offers {', '.join(e['encoding'] for e in opts.get('encodings') or []) or 'no other encoding'} for this stream, not {encoding}")
        new["encoding"] = _pick(opts, encoding)["encoding_raw"]
    o = _pick(opts, encoding)
    if o is None and opts.get("encodings"):
        raise Invalid(f"the camera lists no options for {encoding}")
    o = o or {"resolutions": [], "fps": None, "fps_list": [], "bitrate": None, "gov": None}
    res = changes.get("resolution")
    if res:
        m = re.fullmatch(r"\s*(\d{2,5})\s*[x×]\s*(\d{2,5})\s*", str(res))
        if not m:
            raise Invalid(f"{str(res)[:20]!r} is not a resolution like 1920x1080")
        w, h = int(m.group(1)), int(m.group(2))
        if o["resolutions"] and {"width": w, "height": h} not in o["resolutions"]:
            raise Invalid(f"{w}×{h} is not offered for {encoding}: " + ", ".join(f"{r['width']}×{r['height']}" for r in o["resolutions"]))
        if (w, h) != (cur["width"], cur["height"]):
            new["width"], new["height"] = w, h
    elif "encoding" in new and o["resolutions"] and {"width": cur["width"], "height": cur["height"]} not in o["resolutions"]:
        raise Invalid(f"{cur['width']}×{cur['height']} is not offered for {encoding}: choose a resolution too")
    fps = cur.get("fps")
    if changes.get("fps") is not None:
        fps = float(changes["fps"])
        if o["fps_list"] and not any(abs(fps - f) < 0.01 for f in o["fps_list"]):
            raise Invalid(f"{_fmt(fps)} fps is not offered: " + ", ".join(_fmt(f) for f in o["fps_list"]))
        if not _in_range(fps, o["fps"]):
            raise Invalid(f"{_fmt(fps)} fps is outside what the camera offers ({_fmt(o['fps']['min'])}–{_fmt(o['fps']['max'])})")
        if fps != cur.get("fps"):
            new["fps"] = fps
    if changes.get("bitrate_kbps") is not None:
        b = int(changes["bitrate_kbps"])
        if not _in_range(b, o["bitrate"]):
            raise Invalid(f"{b} kbps is outside what the camera offers ({_fmt(o['bitrate']['min'])}–{_fmt(o['bitrate']['max'])} kbps)")
        if b != cur.get("bitrate_kbps"):
            new["bitrate_kbps"] = b
    gov = None
    if changes.get("gov_seconds") is not None:
        if not fps:
            raise Invalid("the camera doesn't say its frame rate, so the keyframe interval can't be set in seconds")
        gov = max(1, int(round(float(changes["gov_seconds"]) * float(fps))))
    elif changes.get("gov_length") is not None:
        gov = int(changes["gov_length"])
    elif "fps" in new and cur.get("gov_seconds"):
        gov = max(1, int(round(cur["gov_seconds"] * fps)))   # a new frame rate keeps the interval in seconds
    if gov is not None:
        if not _in_range(gov, o["gov"]):
            r = o["gov"]
            raise Invalid(f"a keyframe every {gov} frames is outside what the camera offers ({_fmt(r['min'])}–{_fmt(r['max'])} frames"
                          + (f", {r['min'] / fps:.1f}–{r['max'] / fps:.1f} s at {_fmt(fps)} fps)" if fps else ")"))
        if gov != cur.get("gov_length"):
            new["gov_length"] = gov
    return new


def apply_video(cfg: ET.Element, new: dict, kind: str) -> ET.Element:
    """A copy of the camera's configuration element with `new` written in (everything else as the camera sent it)."""
    el = copy.deepcopy(cfg)
    if "encoding" in new:
        enc = _child(el, "Encoding")
        if enc is not None:
            enc.text = new["encoding"]
        else:
            el.set("Encoding", new["encoding"])
    if "width" in new:
        if not (_set_text(el, "Width", str(new["width"])) and _set_text(el, "Height", str(new["height"]))):
            raise Invalid("the camera's configuration has no resolution to change")
    if "fps" in new and not _set_text(find(el, "RateControl"), "FrameRateLimit", _fmt(new["fps"])):
        raise Invalid("the camera's configuration has no frame rate to change")
    if "bitrate_kbps" in new and not _set_text(find(el, "RateControl"), "BitrateLimit", str(new["bitrate_kbps"])):
        raise Invalid("the camera's configuration has no bitrate to change")
    if "gov_length" in new:
        if el.get("GovLength") is not None:
            el.set("GovLength", str(new["gov_length"]))
        elif _gov_el(el) is not None:
            _gov_el(el).text = str(new["gov_length"])
        elif kind == "media2":
            el.set("GovLength", str(new["gov_length"]))
        else:
            raise Invalid("the camera's configuration has no keyframe interval to change")
    return el


def set_video(cam: dict, token: str, changes: dict) -> tuple[dict, dict]:
    """Change one video encoder. Returns (the encoder as read back, what was written)."""
    with lock(cam["id"]):
        c = connect(cam)
        kind, url = media(c)
        kind_p, profiles = read_profiles(c)
        prof = next((p for p in profiles if p.get("video_encoder") == token), None)
        if prof is None:
            raise Unsupported(f"the camera has no video encoder {token}")
        cfg_el = _get_video_config(c, kind, url, token)
        cur = parse_video_config(cfg_el)
        opts = parse_video_options(_get_video_options(c, kind, url, token, prof["token"]), kind)
        new = plan_video(cur, opts, kind, changes)
        if not new:
            raise Invalid("nothing to change: the camera already has these values")
        el = apply_video(cfg_el, new, kind)
        if kind == "media2":
            body = f"<tr2:SetVideoEncoderConfiguration>{xml_of(el, '{%s}Configuration' % NS['tr2'])}</tr2:SetVideoEncoderConfiguration>"
        else:
            body = (f"<trt:SetVideoEncoderConfiguration>{xml_of(el, '{%s}Configuration' % NS['trt'])}"
                    "<trt:ForcePersistence>true</trt:ForcePersistence></trt:SetVideoEncoderConfiguration>")
        c.call(url, body)
        video = read_video(c, cam, profiles, kind_p)
    return next((v for v in video["encoders"] if v["token"] == token), video["encoders"][0]), new


# --------------------------------------------------------------------------- audio encoders

def parse_audio_config(cfg: ET.Element) -> dict:
    raw = (text(cfg, "Encoding") or "").strip()
    return {"token": cfg.get("token") or "", "name": text(cfg, "Name") or "", "encoding": AUDIO_ENCODINGS.get(raw.upper(), raw),
            "encoding_raw": raw, "bitrate": _int(text(cfg, "Bitrate")), "sample_rate": _int(text(cfg, "SampleRate"))}


def parse_audio_options(body: ET.Element) -> list[dict]:
    """GetAudioEncoderConfigurationOptions -> [{encoding, encoding_raw, bitrates, sample_rates}] (kbps, kHz)."""
    out = []
    for o in find_all(body, "Options"):
        enc = _child(o, "Encoding")
        if enc is None or not (enc.text or "").strip():
            continue
        raw = enc.text.strip()
        out.append({"encoding": AUDIO_ENCODINGS.get(raw.upper(), raw), "encoding_raw": raw,
                    "bitrates": sorted({int(v) for v in _items(_child(o, "BitrateList"))}),
                    "sample_rates": sorted({int(v) for v in _items(_child(o, "SampleRateList"))})})
    return out


def _audio_configs(c: soap.Onvif, kind: str, url: str) -> list[ET.Element]:
    body = c.call(url, "<tr2:GetAudioEncoderConfigurations/>" if kind == "media2" else "<trt:GetAudioEncoderConfigurations/>")
    return find_all(body, "Configurations")


def _audio_options(c: soap.Onvif, kind: str, url: str, token: str) -> list[dict]:
    t = escape(token)
    ns = "tr2" if kind == "media2" else "trt"
    return parse_audio_options(c.call(url, f"<{ns}:GetAudioEncoderConfigurationOptions><{ns}:ConfigurationToken>{t}</{ns}:ConfigurationToken></{ns}:GetAudioEncoderConfigurationOptions>"))


def read_audio(c: soap.Onvif, cam: dict, profiles: list[dict] | None = None) -> dict:
    kind, url = media(c)
    used = {}
    for p in profiles or []:
        if p.get("audio_encoder"):
            used.setdefault(p["audio_encoder"], []).append(p["token"])
    out = []
    for el in _audio_configs(c, kind, url):
        cfg = parse_audio_config(el)
        try:
            opts, err = _audio_options(c, kind, url, cfg["token"]), None
        except OnvifError as e:
            opts, err = [], clean(str(e), cam)
        out.append({**cfg, "profiles": used.get(cfg["token"], []), "media": kind, "options": opts,
                    **({"options_error": err} if err else {})})
    if not out:
        raise Unsupported("the camera has no audio encoder")
    return {"supported": True, "media": kind, "encoders": out}


def _nearest(v: int | None, allowed: list[int]) -> int | None:
    if not allowed:
        return v
    if v in allowed:
        return v
    return min(allowed, key=lambda a: abs(a - (v or 0)))


def plan_audio(cur: dict, opts: list[dict], changes: dict) -> dict:
    """{encoding_raw, bitrate, sample_rate} to write: the encoding as the camera spells it; a bitrate or sample rate
    not given stays if the new encoding allows it, else the nearest it does."""
    want = AUDIO_ENCODINGS.get(str(changes.get("encoding") or cur["encoding"]).upper(), str(changes.get("encoding") or cur["encoding"]))
    o = next((x for x in opts if x["encoding"] == want), None)
    if o is None:
        raise Invalid(f"the camera offers {', '.join(sorted({x['encoding'] for x in opts})) or 'no options'} for audio, not {want}")
    br, sr = changes.get("bitrate"), changes.get("sample_rate")
    if br is not None and o["bitrates"] and int(br) not in o["bitrates"]:
        raise Invalid(f"{br} kbps is not offered for {want}: " + ", ".join(map(str, o["bitrates"])))
    if sr is not None and o["sample_rates"] and int(sr) not in o["sample_rates"]:
        raise Invalid(f"{sr} kHz is not offered for {want}: " + ", ".join(map(str, o["sample_rates"])))
    new = {"encoding_raw": o["encoding_raw"] if want != cur["encoding"] else cur["encoding_raw"] or o["encoding_raw"],
           "bitrate": int(br) if br is not None else _nearest(cur.get("bitrate"), o["bitrates"]),
           "sample_rate": int(sr) if sr is not None else _nearest(cur.get("sample_rate"), o["sample_rates"])}
    if (new["encoding_raw"] == cur["encoding_raw"] and new["bitrate"] == cur.get("bitrate")
            and new["sample_rate"] == cur.get("sample_rate")):
        return {}
    return new


def apply_audio(cfg: ET.Element, new: dict) -> ET.Element:
    el = copy.deepcopy(cfg)
    if not _set_text(el, "Encoding", new["encoding_raw"]):
        raise Invalid("the camera's audio configuration has no encoding to change")
    if new.get("bitrate") is not None:
        _set_text(el, "Bitrate", str(new["bitrate"]))
    if new.get("sample_rate") is not None:
        _set_text(el, "SampleRate", str(new["sample_rate"]))
    return el


def set_audio(cam: dict, token: str, changes: dict) -> tuple[dict, dict]:
    with lock(cam["id"]):
        c = connect(cam)
        kind, url = media(c)
        cfg_el = next((e for e in _audio_configs(c, kind, url) if e.get("token") == token), None)
        if cfg_el is None:
            raise Unsupported(f"the camera has no audio encoder {token}")
        cur = parse_audio_config(cfg_el)
        new = plan_audio(cur, _audio_options(c, kind, url, token), changes)
        if not new:
            raise Invalid("nothing to change: the camera already has these values")
        el = apply_audio(cfg_el, new)
        if kind == "media2":
            body = f"<tr2:SetAudioEncoderConfiguration>{xml_of(el, '{%s}Configuration' % NS['tr2'])}</tr2:SetAudioEncoderConfiguration>"
        else:
            body = (f"<trt:SetAudioEncoderConfiguration>{xml_of(el, '{%s}Configuration' % NS['trt'])}"
                    "<trt:ForcePersistence>true</trt:ForcePersistence></trt:SetAudioEncoderConfiguration>")
        c.call(url, body)
        _, profiles = read_profiles(c)
        audio = read_audio(c, cam, profiles)
    return next((a for a in audio["encoders"] if a["token"] == token), audio["encoders"][0]), new


# --------------------------------------------------------------------------- imaging

IMAGING_FIELDS = {"brightness": "Brightness", "contrast": "Contrast", "saturation": "ColorSaturation", "sharpness": "Sharpness"}


def parse_imaging(settings: ET.Element | None, options: ET.Element | None) -> dict:
    out: dict = {}
    for key, name in IMAGING_FIELDS.items():
        el = _child(settings, name)
        if el is not None:
            out[key] = {"value": _num(el.text), "range": _range(_child(options, name))}
    ir = _child(settings, "IrCutFilter")
    if ir is not None:
        modes = [m.text.strip() for m in soap.children(options, "IrCutFilterModes") if m.text] if options is not None else []
        out["ir_cut"] = {"value": (ir.text or "").strip(), "modes": modes}
    return out


def _imaging_settings(c: soap.Onvif, url: str, source: str) -> ET.Element:
    body = c.call(url, f"<timg:GetImagingSettings><timg:VideoSourceToken>{escape(source)}</timg:VideoSourceToken></timg:GetImagingSettings>")
    el = find(body, "ImagingSettings")
    if el is None:
        raise OnvifError("the camera returned no imaging settings")
    return el


def read_imaging(c: soap.Onvif, cam: dict, sources: list[str]) -> dict:
    url = c.service("imaging")
    if not url:
        raise Unsupported("the camera has no ONVIF imaging service")
    if not sources:
        raise Unsupported("the camera lists no video source")
    out = []
    for s in sources:
        settings = _imaging_settings(c, url, s)
        try:
            opts = find(c.call(url, f"<timg:GetOptions><timg:VideoSourceToken>{escape(s)}</timg:VideoSourceToken></timg:GetOptions>"), "ImagingOptions")
        except OnvifError:
            opts = None
        out.append({"source": s, **parse_imaging(settings, opts)})
    return {"supported": True, "sources": out}


def set_imaging(cam: dict, source: str, changes: dict) -> tuple[dict, dict]:
    with lock(cam["id"]):
        c = connect(cam)
        url = c.service("imaging")
        if not url:
            raise Unsupported("the camera has no ONVIF imaging service")
        settings = _imaging_settings(c, url, source)
        try:
            opts = find(c.call(url, f"<timg:GetOptions><timg:VideoSourceToken>{escape(source)}</timg:VideoSourceToken></timg:GetOptions>"), "ImagingOptions")
        except OnvifError:
            opts = None
        cur = parse_imaging(settings, opts)
        el = copy.deepcopy(settings)
        new: dict = {}
        for key, name in IMAGING_FIELDS.items():
            if changes.get(key) is None:
                continue
            if key not in cur:
                raise Invalid(f"the camera doesn't expose {key} over ONVIF")
            v = float(changes[key])
            if not _in_range(v, cur[key]["range"]):
                r = cur[key]["range"]
                raise Invalid(f"{key} {_fmt(v)} is outside what the camera offers ({_fmt(r['min'])}–{_fmt(r['max'])})")
            if v != cur[key]["value"]:
                _child(el, name).text = _fmt(v)
                new[key] = v
        if changes.get("ir_cut"):
            if "ir_cut" not in cur:
                raise Invalid("the camera doesn't expose its IR cut filter (day / night) over ONVIF")
            v = str(changes["ir_cut"]).upper()
            if cur["ir_cut"]["modes"] and v not in cur["ir_cut"]["modes"]:
                raise Invalid(f"day / night mode {v} is not offered: " + ", ".join(cur["ir_cut"]["modes"]))
            if v != cur["ir_cut"]["value"]:
                _child(el, "IrCutFilter").text = v
                new["ir_cut"] = v
        if not new:
            raise Invalid("nothing to change: the camera already has these values")
        c.call(url, f"<timg:SetImagingSettings><timg:VideoSourceToken>{escape(source)}</timg:VideoSourceToken>"
                    f"{xml_of(el, '{%s}ImagingSettings' % NS['timg'])}<timg:ForcePersistence>true</timg:ForcePersistence></timg:SetImagingSettings>")
        out = read_imaging(c, cam, [source])["sources"][0]
    return out, new


# --------------------------------------------------------------------------- analytics rules

def parse_rule(rule: ET.Element) -> dict:
    params = soap.simple_items(_child(rule, "Parameters"))
    toggle = next((k for k, v in params.items() if k and TOGGLE_PARAM.match(k) and (v or "").strip().lower() in TRUE_FALSE), None)
    rtype = rule.get("Type") or ""
    return {"name": rule.get("Name") or "", "type": rtype.rsplit(":", 1)[-1], "type_qname": rtype,
            "params": {k: v for k, v in params.items() if k}, "toggle": toggle,
            "enabled": TRUE_FALSE.get((params.get(toggle) or "").strip().lower()) if toggle else None}


def _rules(c: soap.Onvif, url: str, config: str) -> list[ET.Element]:
    body = c.call(url, f"<tan:GetRules><tan:ConfigurationToken>{escape(config)}</tan:ConfigurationToken></tan:GetRules>")
    return find_all(body, "Rule")


def read_analytics(c: soap.Onvif, cam: dict, profiles: list[dict]) -> dict:
    url = c.service("analytics")
    if not url:
        raise Unsupported("the camera has no ONVIF analytics service")
    configs = list(dict.fromkeys(p["analytics"] for p in profiles if p.get("analytics")))
    if not configs:
        raise Unsupported("no media profile has an analytics configuration")
    out = []
    for cfg in configs:
        try:
            rules = [parse_rule(r) for r in _rules(c, url, cfg)]
            out.append({"configuration": cfg, "rules": rules, "sources": sorted({p["source"] for p in profiles if p.get("analytics") == cfg and p.get("source")})})
        except OnvifError as e:
            out.append({"configuration": cfg, "rules": [], "error": clean(str(e), cam)})
    return {"supported": True, "configurations": out}


def set_rule(cam: dict, config: str, name: str, enabled: bool) -> dict:
    """Switch an analytics rule on or off through its Enable-like parameter (ModifyRules with the whole rule)."""
    with lock(cam["id"]):
        c = connect(cam)
        url = c.service("analytics")
        if not url:
            raise Unsupported("the camera has no ONVIF analytics service")
        rule_el = next((r for r in _rules(c, url, config) if r.get("Name") == name), None)
        nsmap = raw_nsmap(c.last_raw)
        if rule_el is None:
            raise Unsupported(f"the camera has no rule {name!r}")
        rule = parse_rule(rule_el)
        if not rule["toggle"]:
            raise Invalid(f"the rule {name!r} has no on/off parameter over ONVIF: change it on the camera's web page")
        if rule["enabled"] == enabled:
            raise Invalid(f"the rule {name!r} is already {'on' if enabled else 'off'}")
        el = copy.deepcopy(rule_el)
        old = rule["params"][rule["toggle"]].strip().lower()
        value = ("1" if enabled else "0") if old in ("1", "0") else ("true" if enabled else "false")
        for item in find_all(el, "SimpleItem"):
            if item.get("Name") == rule["toggle"]:
                item.set("Value", value)
        c.call(url, f"<tan:ModifyRules><tan:ConfigurationToken>{escape(config)}</tan:ConfigurationToken>"
                    f"{xml_of(el, '{%s}Rule' % NS['tan'], nsmap)}</tan:ModifyRules>")
        after = next((parse_rule(r) for r in _rules(c, url, config) if r.get("Name") == name), None)
    return after or rule


# --------------------------------------------------------------------------- everything at once

def _section(fn, cam: dict) -> dict:
    try:
        return fn()
    except Unsupported as e:
        return {"supported": False, "reason": clean(str(e), cam)}
    except OnvifError as e:
        return {"supported": False, "error": True, "reason": clean(str(e), cam)}
    except ET.ParseError as e:
        return {"supported": False, "error": True, "reason": f"unexpected reply: {e}"}


def read_all(cam: dict, zone: str | None = None) -> dict:
    """{info, time, video, audio, imaging, analytics, capabilities}: each section on its own ({supported: false,
    reason} when the camera can't do it or refused). A camera that can't be reached at all: every section says why."""
    try:
        c = connect(cam)
    except OnvifError as e:
        reason = clean(str(e), cam)
        down = {"supported": False, "error": True, "reason": reason}
        return {"reachable": False, "error": reason, "info": {}, "time": down, "video": down, "audio": down,
                "imaging": down, "analytics": down, "reboot": down, "capabilities": {}}
    info = _section(lambda: {"supported": True, **read_info(c)}, cam)
    tm = _section(lambda: read_time(c, zone), cam)
    try:
        kind, profiles = read_profiles(c)
        perr = None
    except OnvifError as e:
        kind, profiles, perr = None, [], e
    if perr is not None:
        reason = clean(str(perr), cam)
        unsup = isinstance(perr, Unsupported)
        nomedia = {"supported": False, "reason": reason, **({} if unsup else {"error": True})}
        video = audio = analytics = nomedia
        sources: list[str] = []
    else:
        video = _section(lambda: read_video(c, cam, profiles, kind), cam)
        audio = _section(lambda: read_audio(c, cam, profiles), cam)
        analytics = _section(lambda: read_analytics(c, cam, profiles), cam)
        sources = list(dict.fromkeys(p["source"] for p in profiles if p.get("source")))
    imaging = _section(lambda: read_imaging(c, cam, sources), cam)
    out = {"reachable": True, "info": info, "time": tm, "video": video, "audio": audio, "imaging": imaging,
           "analytics": analytics, "reboot": {"supported": True}, "services": sorted(k for k in c.services if "/" not in k)}
    out["capabilities"] = {k: bool(out[k].get("supported")) for k in ("time", "video", "audio", "imaging", "analytics", "reboot")}
    return out
