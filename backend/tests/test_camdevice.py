"""Camera settings over ONVIF (camdevice.py, device_api.py) against fake cameras on 127.0.0.1 that answer like the
real ones: a Milesight (Media only, analytics rules with Enable / FieldEnable parameters, G.711 audio, clock 45 min
ahead), the Uniview TIC2A32SA thermal (Media2 only, two video sources, PCMU audio, refuses POSIX time zones) and the
Tonmind IP speaker (no video). The fakes check the WS-Security digest against their own (wrong) clock, record every
request, and keep what is written so a re-read shows it.

Covered: read-all with unsupported sections, time NTP + manual bodies (TZ string, UTC fields), the step from this
server's clock, gov seconds -> frames, values outside the camera's options refused before any write, read-modify-write
keeping unknown fields, Media2 attributes, audio -> AAC clearing the playback transcode cache, "Fix clocks" results,
analytics rule toggles (QName prefixes kept), imaging, reboot, roles through the tunnel, no credentials in errors.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_camdevice.py   (from backend/)
"""
import asyncio
import base64
import datetime as dt
import hashlib
import os
import re
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="nvr-camdevice-test-"))
for k, sub in (("NVR_DATA_DIR", "data"), ("NVR_RECORDINGS_DIR", "recordings"), ("NVR_RUNTIME_DIR", "runtime")):
    os.environ[k] = str(TMP / sub)   # never the real server's database, recordings or mediamtx.yml
os.environ["NVR_ALLOWED_HOSTS"] = "site"   # the test client's Host (lan_guard Host allow-list)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tunnelproto"))

import httpx  # noqa: E402

from nvr import api, camdevice, hub_agent  # noqa: E402
from nvr import onvif_soap as soap  # noqa: E402
from nvr.api import app  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402

for d in (settings.data_dir, settings.recordings_dir, settings.runtime_dir):
    v = str(d).replace("\\", "/").lower()
    assert "nvr-camdevice-test-" in v and "e:/nvr" not in v and "d:/nvr" not in v and "newvms/runtime" not in v, d

PW = "cam-" + "k" * 9   # a stand-in camera password: must never come back from the API
LAN = ("192.168.1.9", 5000)
TT = "http://www.onvif.org/ver10/schema"
NS = ('xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
      'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tr2="http://www.onvif.org/ver20/media/wsdl" '
      'xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl" xmlns:tan="http://www.onvif.org/ver20/analytics/wsdl" '
      f'xmlns:tt="{TT}" xmlns:ttx="{TT}" xmlns:ter="http://www.onvif.org/ver10/error" xmlns:ms="urn:example:milesight"')


def envelope(body: str) -> bytes:
    return f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope {NS}><s:Body>{body}</s:Body></s:Envelope>'.encode()


def fault(reason: str, sub: str = "ter:ActionNotSupported") -> bytes:
    return envelope(f"<s:Fault><s:Code><s:Value>s:Receiver</s:Value><s:Subcode><s:Value>{sub}</s:Value></s:Subcode></s:Code>"
                    f'<s:Reason><s:Text xml:lang="en">{reason}</s:Text></s:Reason></s:Fault>')


def frag(el: ET.Element) -> str:
    """An element of a request as XML (the fake keeps what was written, as a camera would)."""
    return ET.tostring(el, encoding="unicode")


def local(el):
    return el.tag.rsplit("}", 1)[-1]


def first(el, name):
    return next((e for e in el.iter() if local(e) == name and e is not el), None)


def val(el, name):
    f = first(el, name)
    return f.text.strip() if f is not None and f.text else None


ME_VE = {  # Milesight Media configurations, with fields this server never touches (they must come back unchanged)
    "VE_1": ('<trt:Configuration token="VE_1"><tt:Name>VE_1</tt:Name><tt:UseCount>1</tt:UseCount><tt:Encoding>H264</tt:Encoding>'
             '<tt:Resolution><tt:Width>2592</tt:Width><tt:Height>1520</tt:Height></tt:Resolution><tt:Quality>4</tt:Quality>'
             '<tt:RateControl><tt:FrameRateLimit>25</tt:FrameRateLimit><tt:EncodingInterval>1</tt:EncodingInterval>'
             '<tt:BitrateLimit>6144</tt:BitrateLimit></tt:RateControl><tt:H264><tt:GovLength>100</tt:GovLength>'
             '<tt:H264Profile>High</tt:H264Profile></tt:H264><tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type>'
             '<tt:IPv4Address>239.6.6.6</tt:IPv4Address></tt:Address><tt:Port>5560</tt:Port><tt:TTL>5</tt:TTL>'
             '<tt:AutoStart>false</tt:AutoStart></tt:Multicast><tt:SessionTimeout>PT60S</tt:SessionTimeout>'
             '<tt:Extension><ms:SmartStream>keep-me</ms:SmartStream></tt:Extension></trt:Configuration>'),
    "VE_2": ('<trt:Configuration token="VE_2"><tt:Name>VE_2</tt:Name><tt:UseCount>1</tt:UseCount><tt:Encoding>H264</tt:Encoding>'
             '<tt:Resolution><tt:Width>1280</tt:Width><tt:Height>720</tt:Height></tt:Resolution><tt:Quality>4</tt:Quality>'
             '<tt:RateControl><tt:FrameRateLimit>15</tt:FrameRateLimit><tt:EncodingInterval>1</tt:EncodingInterval>'
             '<tt:BitrateLimit>1024</tt:BitrateLimit></tt:RateControl><tt:H264><tt:GovLength>60</tt:GovLength>'
             '<tt:H264Profile>Main</tt:H264Profile></tt:H264><tt:SessionTimeout>PT60S</tt:SessionTimeout></trt:Configuration>'),
}
ME_VE_OPTIONS = ('<trt:GetVideoEncoderConfigurationOptionsResponse><trt:Options><tt:QualityRange><tt:Min>1</tt:Min><tt:Max>6</tt:Max></tt:QualityRange>'
                 '<tt:H264>' + "".join(f"<tt:ResolutionsAvailable><tt:Width>{w}</tt:Width><tt:Height>{h}</tt:Height></tt:ResolutionsAvailable>"
                                       for w, h in ((2592, 1520), (1920, 1080), (1280, 720)))
                 + '<tt:GovLengthRange><tt:Min>1</tt:Min><tt:Max>250</tt:Max></tt:GovLengthRange><tt:FrameRateRange><tt:Min>1</tt:Min><tt:Max>25</tt:Max></tt:FrameRateRange>'
                 '<tt:EncodingIntervalRange><tt:Min>1</tt:Min><tt:Max>1</tt:Max></tt:EncodingIntervalRange><tt:H264ProfilesSupported>Main</tt:H264ProfilesSupported>'
                 '<tt:H264ProfilesSupported>High</tt:H264ProfilesSupported></tt:H264><tt:Extension><tt:H264>'
                 '<tt:ResolutionsAvailable><tt:Width>2592</tt:Width><tt:Height>1520</tt:Height></tt:ResolutionsAvailable>'
                 '<tt:BitrateRange><tt:Min>64</tt:Min><tt:Max>16384</tt:Max></tt:BitrateRange></tt:H264></tt:Extension></trt:Options>'
                 '</trt:GetVideoEncoderConfigurationOptionsResponse>')
UV_VE = {  # Uniview TIC2A32SA (Media2): ve0/ve1 the visible sensor, ve2 the thermal one
    "ve0": ('<tr2:Configurations token="ve0" GovLength="50" Profile="Main"><tt:Name>ve0</tt:Name><tt:UseCount>1</tt:UseCount>'
            '<tt:Encoding>H265</tt:Encoding><tt:Resolution><tt:Width>2688</tt:Width><tt:Height>1520</tt:Height></tt:Resolution>'
            '<tt:RateControl ConstantBitRate="false"><tt:FrameRateLimit>25.000000</tt:FrameRateLimit><tt:BitrateLimit>4096</tt:BitrateLimit></tt:RateControl>'
            '<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type><tt:IPv4Address>0.0.0.0</tt:IPv4Address></tt:Address><tt:Port>0</tt:Port>'
            '<tt:TTL>0</tt:TTL><tt:AutoStart>false</tt:AutoStart></tt:Multicast><tt:Quality>3</tt:Quality></tr2:Configurations>'),
    "ve1": ('<tr2:Configurations token="ve1" GovLength="25" Profile="Main"><tt:Name>ve1</tt:Name><tt:UseCount>1</tt:UseCount>'
            '<tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>720</tt:Width><tt:Height>576</tt:Height></tt:Resolution>'
            '<tt:RateControl ConstantBitRate="false"><tt:FrameRateLimit>25</tt:FrameRateLimit><tt:BitrateLimit>512</tt:BitrateLimit></tt:RateControl>'
            '<tt:Quality>3</tt:Quality></tr2:Configurations>'),
    "ve2": ('<tr2:Configurations token="ve2" GovLength="250" Profile="Main"><tt:Name>ve2</tt:Name><tt:UseCount>1</tt:UseCount>'
            '<tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>1280</tt:Width><tt:Height>960</tt:Height></tt:Resolution>'
            '<tt:RateControl ConstantBitRate="true"><tt:FrameRateLimit>25</tt:FrameRateLimit><tt:BitrateLimit>2048</tt:BitrateLimit></tt:RateControl>'
            '<tt:Quality>3</tt:Quality></tr2:Configurations>'),
}


def uv_options(token: str) -> str:
    res = ((2688, 1520), (1920, 1080), (1280, 720)) if token == "ve0" else ((720, 576), (640, 360)) if token == "ve1" else ((1280, 960),)
    rs = "".join(f"<tt:ResolutionsAvailable><tt:Width>{w}</tt:Width><tt:Height>{h}</tt:Height></tt:ResolutionsAvailable>" for w, h in res)
    one = lambda enc: (f'<tr2:Options GovLengthRange="1 250" FrameRatesSupported="25 20 15 12 10 5 1" ProfilesSupported="Main" ConstantBitRateSupported="true">'  # noqa: E731
                       f'<tt:Encoding>{enc}</tt:Encoding><tt:QualityRange><tt:Min>1</tt:Min><tt:Max>5</tt:Max></tt:QualityRange>{rs}'
                       f'<tt:BitrateRange><tt:Min>128</tt:Min><tt:Max>8192</tt:Max></tt:BitrateRange></tr2:Options>')
    return f"<tr2:GetVideoEncoderConfigurationOptionsResponse>{one('H264')}{one('H265')}</tr2:GetVideoEncoderConfigurationOptionsResponse>"


ME_RULES = ('<tan:GetRulesResponse>'
            '<tan:Rule Name="MyCellMotionDetectorRule" Type="ttx:CellMotionDetector"><tt:Parameters><tt:SimpleItem Name="MinCount" Value="5"/>'
            '<tt:SimpleItem Name="AlarmOnDelay" Value="1000"/><tt:SimpleItem Name="ActiveCells" Value="3P8A4A=="/></tt:Parameters></tan:Rule>'
            '<tan:Rule Name="MyLineDetectorRule1" Type="ttx:LineDetector"><tt:Parameters><tt:SimpleItem Name="Enable" Value="{line}"/>'
            '<tt:SimpleItem Name="Sensitivity" Value="50"/><tt:SimpleItem Name="Direction" Value="2"/>'
            '<tt:ElementItem Name="Segments"><tt:Polyline><tt:Point x="0.1" y="0.5"/><tt:Point x="0.9" y="0.5"/></tt:Polyline></tt:ElementItem></tt:Parameters></tan:Rule>'
            '<tan:Rule Name="MyFieldDetectorRule1" Type="ttx:FieldDetector"><tt:Parameters><tt:SimpleItem Name="FieldEnable" Value="{field}"/>'
            '<tt:SimpleItem Name="Sensitivity" Value="50"/></tt:Parameters></tan:Rule></tan:GetRulesResponse>')


class FakeCamera:
    """An ONVIF device service on 127.0.0.1. kind: "milesight" | "thermal" | "speaker". Its clock runs `offset` seconds
    ahead of this machine's; authenticated calls must carry a digest of PW created within 5 s of the camera's clock."""

    def __init__(self, kind: str, offset: float = 0.0, reject_tz: bool = False):
        self.kind, self.offset, self.reject_tz = kind, offset, reject_tz
        self.tz = "UTC+06:00:00" if kind == "milesight" else "CST6"
        self.local_off = -6 * 3600
        self.mode = "Manual"
        self.dst = "false"
        self.ntp = ["old.ntp.example"]
        self.ve = dict(ME_VE if kind == "milesight" else UV_VE if kind == "thermal" else {})
        self.ae = {"AE_1": "G711", "a0": "PCMU"}
        self.ae_rate = {"AE_1": (64, 8), "a0": (64, 8)}
        self.rules = {"line": "false", "field": "false"}
        self.imaging = ('<tt:BacklightCompensation><tt:Mode>OFF</tt:Mode></tt:BacklightCompensation><tt:Brightness>50</tt:Brightness>'
                        '<tt:ColorSaturation>50</tt:ColorSaturation><tt:Contrast>50</tt:Contrast><tt:Exposure><tt:Mode>AUTO</tt:Mode>'
                        '<tt:MaxExposureTime>33333</tt:MaxExposureTime></tt:Exposure><tt:IrCutFilter>AUTO</tt:IrCutFilter>'
                        '<tt:Sharpness>50</tt:Sharpness><tt:WideDynamicRange><tt:Mode>OFF</tt:Mode></tt:WideDynamicRange>')
        self.requests: list[tuple[str, str, str]] = []   # (path, action, body xml)
        self.lock = threading.Lock()
        cam = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                code, out = cam.handle(self.path, raw)
                self.send_response(code)
                self.send_header("Content-Type", "application/soap+xml")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def row(self, cid: str, name: str) -> dict:
        return {"id": cid, "name": name, "host": "127.0.0.1", "onvif_port": self.port, "rtsp_port": 554, "username": "admin",
                "password": PW, "main_path": "/main", "sub_path": "/sub", "enabled": 1, "zones": [], "retention_days": None,
                "scene_notes": "", "retention_policy": None, "synopsis_labels": ["person"], "policies": []}

    def actions(self, name: str | None = None) -> list[str]:
        return [a for _, a, _ in self.requests if name is None or a == name]

    def bodies(self, name: str) -> list[str]:
        return [b for _, a, b in self.requests if a == name]

    def now(self) -> float:
        return time.time() + self.offset

    def auth_ok(self, root: ET.Element) -> bool:
        tok = first(root, "UsernameToken")
        if tok is None:
            return False
        created, nonce, digest = val(tok, "Created"), val(tok, "Nonce"), val(tok, "Password")
        want = base64.b64encode(hashlib.sha1(base64.b64decode(nonce) + created.encode() + PW.encode()).digest()).decode()
        t = dt.datetime.strptime(created[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()
        return digest == want and val(tok, "Username") == "admin" and abs(t - self.now()) < 5

    def svc(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/onvif/{name}"

    def handle(self, path: str, raw: bytes) -> tuple[int, bytes]:
        root = ET.fromstring(raw)
        body = next(e for e in root if local(e) == "Body")
        req = next(iter(body))
        action = local(req)
        with self.lock:
            self.requests.append((path, action, raw.decode()))
        if action != "GetSystemDateAndTime" and not self.auth_ok(root):
            return 400, fault("Sender not Authorized", "ter:NotAuthorized")
        fn = getattr(self, f"do_{action}", None)
        if fn is None:
            return 500, fault(f"{action} not supported")
        return fn(req)

    # ---- device
    def do_GetServices(self, req):
        names = {"milesight": [("ver10/device", "device_service"), ("ver10/media", "media"), ("ver20/imaging", "imaging"),
                               ("ver20/analytics", "analytics"), ("ver10/events", "event")],
                 "thermal": [("ver10/device", "device_service"), ("ver20/media", "media2"), ("ver20/imaging", "imaging")],
                 "speaker": [("ver10/device", "device_service"), ("ver10/media", "media")]}[self.kind]
        svcs = "".join(f"<tds:Service><tds:Namespace>http://www.onvif.org/{ns}/wsdl</tds:Namespace><tds:XAddr>{self.svc(p)}</tds:XAddr>"
                       "<tds:Version><tt:Major>2</tt:Major><tt:Minor>60</tt:Minor></tds:Version></tds:Service>" for ns, p in names)
        return 200, envelope(f"<tds:GetServicesResponse>{svcs}</tds:GetServicesResponse>")

    def do_GetDeviceInformation(self, req):
        m = {"milesight": ("Milesight Technology Co.,Ltd.", "MS-C5383-PD"), "thermal": ("UNIVIEW", "TIC2A32SA"),
             "speaker": ("Tonmind", "IP Speaker")}[self.kind]
        return 200, envelope(f"<tds:GetDeviceInformationResponse><tds:Manufacturer>{m[0]}</tds:Manufacturer><tds:Model>{m[1]}</tds:Model>"
                             "<tds:FirmwareVersion>61.8.0.5</tds:FirmwareVersion><tds:SerialNumber>X</tds:SerialNumber>"
                             "<tds:HardwareId>V1</tds:HardwareId></tds:GetDeviceInformationResponse>")

    def do_GetSystemDateAndTime(self, req):
        def dtx(t):
            d = dt.datetime.fromtimestamp(t, dt.timezone.utc)
            return (f"<tt:Time><tt:Hour>{d.hour}</tt:Hour><tt:Minute>{d.minute}</tt:Minute><tt:Second>{d.second}</tt:Second></tt:Time>"
                    f"<tt:Date><tt:Year>{d.year}</tt:Year><tt:Month>{d.month}</tt:Month><tt:Day>{d.day}</tt:Day></tt:Date>")
        now = self.now()
        return 200, envelope(f"<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:DateTimeType>{self.mode}</tt:DateTimeType>"
                             f"<tt:DaylightSavings>{self.dst}</tt:DaylightSavings><tt:TimeZone><tt:TZ>{self.tz}</tt:TZ></tt:TimeZone>"
                             f"<tt:UTCDateTime>{dtx(now)}</tt:UTCDateTime><tt:LocalDateTime>{dtx(now + self.local_off)}</tt:LocalDateTime>"
                             "</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>")

    def do_GetNTP(self, req):
        if self.kind == "speaker":
            return 500, fault(f"GetNTP not supported (user admin password {PW})")   # a careless camera echoing its login
        items = "".join(f"<tt:NTPManual><tt:Type>DNS</tt:Type><tt:DNSname>{s}</tt:DNSname></tt:NTPManual>" for s in self.ntp)
        return 200, envelope(f"<tds:GetNTPResponse><tds:NTPInformation><tt:FromDHCP>false</tt:FromDHCP>{items}</tds:NTPInformation></tds:GetNTPResponse>")

    def do_SetNTP(self, req):
        self.ntp = [val(m, "DNSname") or val(m, "IPv4Address") for m in req if local(m) == "NTPManual"]
        return 200, envelope("<tds:SetNTPResponse/>")

    def do_SetSystemDateAndTime(self, req):
        if self.reject_tz and first(req, "TimeZone") is not None:
            return 500, fault("Invalid TimeZone", "ter:InvalidArgVal")
        self.mode = val(req, "DateTimeType")
        self.dst = val(req, "DaylightSavings")
        if first(req, "TZ") is not None:
            self.tz = val(req, "TZ")
            self.local_off = -5 * 3600 if self.tz.startswith("CST6CDT") else self.local_off
        utc = first(req, "UTCDateTime")
        if utc is not None:
            t = dt.datetime(int(val(utc, "Year")), int(val(utc, "Month")), int(val(utc, "Day")), int(val(utc, "Hour")),
                            int(val(utc, "Minute")), int(val(utc, "Second")), tzinfo=dt.timezone.utc).timestamp()
            self.offset = t - time.time()
        elif self.mode == "NTP" and self.kind == "thermal":
            pass   # its NTP client runs later: the clock stays off until then
        return 200, envelope("<tds:SetSystemDateAndTimeResponse/>")

    def do_SystemReboot(self, req):
        return 200, envelope("<tds:SystemRebootResponse><tds:Message>Rebooting in 5 seconds</tds:Message></tds:SystemRebootResponse>")

    # ---- media
    def do_GetProfiles(self, req):
        if self.kind == "speaker":
            return 200, envelope('<trt:GetProfilesResponse><trt:Profiles token="audio"><tt:Name>audio</tt:Name>'
                                 '<tt:AudioEncoderConfiguration token="AE_1"><tt:Name>a</tt:Name></tt:AudioEncoderConfiguration>'
                                 '</trt:Profiles></trt:GetProfilesResponse>')
        if self.kind == "milesight":
            def prof(tok, ve, ae=""):
                return (f'<trt:Profiles token="{tok}" fixed="true"><tt:Name>{tok}</tt:Name><tt:VideoSourceConfiguration token="VSC">'
                        f'<tt:Name>VSC</tt:Name><tt:SourceToken>VideoSource_1</tt:SourceToken></tt:VideoSourceConfiguration>'
                        f'{ME_VE[ve].replace("trt:Configuration", "tt:VideoEncoderConfiguration")}{ae}'
                        f'<tt:VideoAnalyticsConfiguration token="VideoAnalyticsToken"><tt:Name>VA</tt:Name></tt:VideoAnalyticsConfiguration></trt:Profiles>')
            return 200, envelope("<trt:GetProfilesResponse>" + prof("Profile_1", "VE_1", '<tt:AudioEncoderConfiguration token="AE_1"><tt:Name>a</tt:Name></tt:AudioEncoderConfiguration>')
                                 + prof("Profile_2", "VE_2") + "</trt:GetProfilesResponse>")
        def prof2(tok, source, ve, audio=""):   # Media2: the configurations of each profile under Configurations
            cfg = UV_VE[ve].replace("tr2:Configurations", "tr2:VideoEncoder")
            return (f'<tr2:Profiles token="{tok}" fixed="true"><tr2:Name>{tok}</tr2:Name><tr2:Configurations>'
                    f'<tr2:VideoSource token="vsc_{source[-1]}"><tt:Name>vsc</tt:Name><tt:UseCount>2</tt:UseCount><tt:SourceToken>{source}</tt:SourceToken>'
                    f'<tt:Bounds x="0" y="0" width="2688" height="1520"/></tr2:VideoSource>{cfg}{audio}</tr2:Configurations></tr2:Profiles>')
        audio = '<tr2:AudioEncoder token="a0"><tt:Name>a0</tt:Name><tt:UseCount>1</tt:UseCount><tt:Encoding>PCMU</tt:Encoding></tr2:AudioEncoder>'
        return 200, envelope("<tr2:GetProfilesResponse>" + prof2("media_profile1_1", "VideoSource_1", "ve0", audio)
                             + prof2("media_profile1_2", "VideoSource_1", "ve1") + prof2("media_profile2_1", "VideoSource_2", "ve2")
                             + "</tr2:GetProfilesResponse>")

    def do_GetVideoEncoderConfigurations(self, req):
        tok = val(req, "ConfigurationToken")
        return 200, envelope(f"<tr2:GetVideoEncoderConfigurationsResponse>{self.ve[tok]}</tr2:GetVideoEncoderConfigurationsResponse>")

    def do_GetVideoEncoderConfiguration(self, req):
        tok = val(req, "ConfigurationToken")
        return 200, envelope(f"<trt:GetVideoEncoderConfigurationResponse>{self.ve[tok]}</trt:GetVideoEncoderConfigurationResponse>")

    def do_GetVideoEncoderConfigurationOptions(self, req):
        if self.kind == "milesight":
            return 200, envelope(ME_VE_OPTIONS)
        return 200, envelope(uv_options(val(req, "ConfigurationToken")))

    def do_SetVideoEncoderConfiguration(self, req):
        cfg = next(e for e in req if local(e) == "Configuration")
        tag = "trt:Configuration" if self.kind == "milesight" else "tr2:Configurations"
        x = frag(cfg)
        # stored as the camera will hand it out again (its own element name)
        x = re.sub(r"^<[\w]+:Configuration", f"<{tag}", x)
        x = re.sub(r"</[\w]+:Configuration>$", f"</{tag}>", x)
        self.ve[cfg.get("token")] = x
        return 200, envelope("<trt:SetVideoEncoderConfigurationResponse/>")

    def do_GetAudioEncoderConfigurations(self, req):
        toks = ["a0"] if self.kind == "thermal" else ["AE_1"]
        ns = "tr2" if self.kind == "thermal" else "trt"
        items = "".join(f'<{ns}:Configurations token="{t}"><tt:Name>{t}</tt:Name><tt:UseCount>1</tt:UseCount><tt:Encoding>{self.ae[t]}</tt:Encoding>'
                        + ("" if ns == "tr2" else "<tt:Multicast><tt:Port>0</tt:Port></tt:Multicast>")
                        + f"<tt:Bitrate>{self.ae_rate[t][0]}</tt:Bitrate><tt:SampleRate>{self.ae_rate[t][1]}</tt:SampleRate>"
                        + ("" if ns == "tr2" else "<tt:SessionTimeout>PT60S</tt:SessionTimeout>") + f"</{ns}:Configurations>" for t in toks)
        return 200, envelope(f"<{ns}:GetAudioEncoderConfigurationsResponse>{items}</{ns}:GetAudioEncoderConfigurationsResponse>")

    def do_GetAudioEncoderConfigurationOptions(self, req):
        if self.kind == "thermal":
            return 200, envelope("<tr2:GetAudioEncoderConfigurationOptionsResponse>"
                                 "<tr2:Options><tt:Encoding>PCMU</tt:Encoding><tt:BitrateList><tt:Items>64</tt:Items></tt:BitrateList><tt:SampleRateList><tt:Items>8</tt:Items></tt:SampleRateList></tr2:Options>"
                                 "<tr2:Options><tt:Encoding>MP4A-LATM</tt:Encoding><tt:BitrateList><tt:Items>32 48 64</tt:Items></tt:BitrateList><tt:SampleRateList><tt:Items>16</tt:Items></tt:SampleRateList></tr2:Options>"
                                 "</tr2:GetAudioEncoderConfigurationOptionsResponse>")
        return 200, envelope("<trt:GetAudioEncoderConfigurationOptionsResponse><trt:Options>"
                             "<tt:Options><tt:Encoding>G711</tt:Encoding><tt:BitrateList><tt:Items>64</tt:Items></tt:BitrateList><tt:SampleRateList><tt:Items>8</tt:Items></tt:SampleRateList></tt:Options>"
                             "<tt:Options><tt:Encoding>AAC</tt:Encoding><tt:BitrateList><tt:Items>16</tt:Items><tt:Items>32</tt:Items><tt:Items>64</tt:Items></tt:BitrateList>"
                             "<tt:SampleRateList><tt:Items>8</tt:Items><tt:Items>16</tt:Items></tt:SampleRateList></tt:Options>"
                             "</trt:Options></trt:GetAudioEncoderConfigurationOptionsResponse>")

    def do_SetAudioEncoderConfiguration(self, req):
        cfg = next(e for e in req if local(e) == "Configuration")
        t = cfg.get("token")
        self.ae[t] = val(cfg, "Encoding")
        self.ae_rate[t] = (int(val(cfg, "Bitrate")), int(val(cfg, "SampleRate")))
        return 200, envelope("<trt:SetAudioEncoderConfigurationResponse/>")

    # ---- imaging
    def do_GetImagingSettings(self, req):
        return 200, envelope(f"<timg:GetImagingSettingsResponse><timg:ImagingSettings>{self.imaging}</timg:ImagingSettings></timg:GetImagingSettingsResponse>")

    def do_GetOptions(self, req):
        r = "<tt:Min>0</tt:Min><tt:Max>100</tt:Max>"
        return 200, envelope(f"<timg:GetOptionsResponse><timg:ImagingOptions><tt:Brightness>{r}</tt:Brightness><tt:ColorSaturation>{r}</tt:ColorSaturation>"
                             f"<tt:Contrast>{r}</tt:Contrast><tt:IrCutFilterModes>ON</tt:IrCutFilterModes><tt:IrCutFilterModes>OFF</tt:IrCutFilterModes>"
                             f"<tt:IrCutFilterModes>AUTO</tt:IrCutFilterModes><tt:Sharpness>{r}</tt:Sharpness></timg:ImagingOptions></timg:GetOptionsResponse>")

    def do_SetImagingSettings(self, req):
        el = next(e for e in req if local(e) == "ImagingSettings")
        self.imaging = "".join(frag(c) for c in el)
        return 200, envelope("<timg:SetImagingSettingsResponse/>")

    # ---- analytics
    def do_GetRules(self, req):
        return 200, envelope(ME_RULES.format(**self.rules))

    def do_ModifyRules(self, req):
        for rule in (e for e in req if local(e) == "Rule"):
            assert rule.get("Type", "").startswith("ttx:"), rule.attrib   # the QName prefix must still resolve
            for item in rule.iter():
                if local(item) == "SimpleItem" and item.get("Name") == "Enable":
                    self.rules["line"] = item.get("Value")
                if local(item) == "SimpleItem" and item.get("Name") == "FieldEnable":
                    self.rules["field"] = item.get("Value")
        return 200, envelope("<tan:ModifyRulesResponse/>")


class StreamsStub:
    def __init__(self):
        self.asked: list[str] = []

    def check_soon(self, camera_id: str) -> None:
        self.asked.append(camera_id)


class MetaStub:
    def __init__(self):
        self.resets = 0

    def reset_clock(self):
        self.resets += 1


class IngestStub:
    def __init__(self):
        self.meta = MetaStub()


def call(method, path, body=None, role=None, user="admin@example"):
    async def go():
        tunnel = role is not None
        transport = httpx.ASGITransport(app=hub_agent.as_tunnel(app) if tunnel else app, client=hub_agent.IN_PROCESS_CLIENT if tunnel else LAN)
        headers = {"x-hub-role": role, "x-hub-user": user} if tunnel else {}
        async with httpx.AsyncClient(transport=transport, base_url="http://site", timeout=60) as c:
            return await c.request(method, path, json=body, headers=headers)
    return asyncio.run(go())


ME = FakeCamera("milesight", offset=45 * 60)            # cam3 / cam4: about 45 minutes ahead
UV = FakeCamera("thermal", offset=3600, reject_tz=True)  # the thermal camera: an hour off, refuses POSIX zones
SPK = FakeCamera("speaker")
db.upsert_camera(ME.row("cam3", "East Door"))
db.upsert_camera(UV.row("thermal", "Thermal"))
db.upsert_camera(SPK.row("spk", "Speaker"))
DEAD = {**ME.row("dead", "Gone"), "onvif_port": 9}      # nothing listens there
db.upsert_camera(DEAD)
api.state.streams = StreamsStub()
api.state.ingests = {"cam3": IngestStub(), "thermal": IngestStub()}
db.set_setting("camera_time", {"ntp_server": "pool.ntp.org", "timezone": "America/Chicago"})


def test_posix_tz_and_bodies():
    assert camdevice.posix_tz("America/Chicago") == "CST6CDT,M3.2.0,M11.1.0"
    assert camdevice.posix_tz("Asia/Kolkata") == "IST-5:30" and camdevice.posix_tz("UTC") == "UTC0"
    assert camdevice.posix_tz("CST6CDT,M3.2.0,M11.1.0") == "CST6CDT,M3.2.0,M11.1.0"   # a POSIX rule as given
    assert camdevice.posix_tz("Nowhere/Land") is None and camdevice.posix_tz("../../etc/passwd") is None
    # NTP: FromDHCP off, each server by its type
    b = camdevice.ntp_body(["pool.ntp.org", "192.168.105.1", "fe80::1"])
    assert "<tds:FromDHCP>false</tds:FromDHCP>" in b and "<tt:DNSname>pool.ntp.org</tt:DNSname>" in b
    assert "<tt:Type>IPv4</tt:Type><tt:IPv4Address>192.168.105.1</tt:IPv4Address>" in b and "<tt:IPv6Address>fe80::1</tt:IPv6Address>" in b
    # Manual: this server's UTC now, Date before Time (tt:DateTime), the zone and daylight saving from the rule
    t = dt.datetime(2026, 10, 15, 3, 4, 5, tzinfo=dt.timezone.utc).timestamp()
    b = camdevice.datetime_body("Manual", "CST6CDT,M3.2.0,M11.1.0", now=t)
    assert ("<tds:DateTimeType>Manual</tds:DateTimeType><tds:DaylightSavings>true</tds:DaylightSavings>"
            "<tds:TimeZone><tt:TZ>CST6CDT,M3.2.0,M11.1.0</tt:TZ></tds:TimeZone>") in b
    assert ("<tds:UTCDateTime><tt:Date><tt:Year>2026</tt:Year><tt:Month>10</tt:Month><tt:Day>15</tt:Day></tt:Date>"
            "<tt:Time><tt:Hour>3</tt:Hour><tt:Minute>4</tt:Minute><tt:Second>5</tt:Second></tt:Time></tds:UTCDateTime>") in b
    b = camdevice.datetime_body("NTP", "IST-5:30")
    assert "<tds:DaylightSavings>false</tds:DaylightSavings>" in b and "UTCDateTime" not in b
    assert "TimeZone" not in camdevice.datetime_body("NTP", None)
    # no zone sent: the camera's own daylight-saving setting goes back unchanged (false would switch it off)
    assert "<tds:DaylightSavings>true</tds:DaylightSavings>" in camdevice.datetime_body("Manual", None, dst=True)


def test_read_all():
    r = call("GET", "/api/cameras/cam3/device")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["reachable"] and d["info"]["model"] == "MS-C5383-PD" and d["info"]["manufacturer"].startswith("Milesight")
    t = d["time"]
    assert t["supported"] and t["mode"] == "Manual" and t["timezone"] == "UTC+06:00:00" and t["ntp_servers"] == ["old.ntp.example"]
    assert 2690 < t["offset_s"] < 2710, t["offset_s"]
    # camera local = UTC-6, Chicago in October is UTC-5 (daylight saving): flagged
    if camdevice.utc_offset_s("America/Chicago") == -5 * 3600:
        assert t["zone_check"] == {"zone": "America/Chicago", "expected_s": -18000, "camera_s": -21600, "ok": False}
    v = d["video"]
    assert v["supported"] and v["media"] == "media" and [e["token"] for e in v["encoders"]] == ["VE_1", "VE_2"]
    main = v["encoders"][0]
    assert (main["width"], main["height"], main["fps"], main["bitrate_kbps"], main["gov_length"], main["gov_seconds"]) == (2592, 1520, 25, 6144, 100, 4.0)
    assert main["source"] == "VideoSource_1" and main["profiles"] == ["Profile_1"]
    h264 = main["options"]["encodings"][0]
    assert h264["encoding"] == "H264" and h264["fps"] == {"min": 1, "max": 25} and h264["gov"] == {"min": 1, "max": 250}
    assert h264["bitrate"] == {"min": 64, "max": 16384} and {"width": 1920, "height": 1080} in h264["resolutions"]
    a = d["audio"]["encoders"][0]
    assert a["encoding"] == "G711" and a["sample_rate"] == 8 and {o["encoding"] for o in a["options"]} == {"G711", "AAC"}
    assert d["imaging"]["sources"][0]["brightness"] == {"value": 50, "range": {"min": 0, "max": 100}}
    assert d["imaging"]["sources"][0]["ir_cut"] == {"value": "AUTO", "modes": ["ON", "OFF", "AUTO"]}
    rules = {x["name"]: x for x in d["analytics"]["configurations"][0]["rules"]}
    assert rules["MyCellMotionDetectorRule"]["toggle"] is None and rules["MyCellMotionDetectorRule"]["type"] == "CellMotionDetector"
    assert rules["MyLineDetectorRule1"]["toggle"] == "Enable" and rules["MyLineDetectorRule1"]["enabled"] is False
    assert rules["MyFieldDetectorRule1"]["toggle"] == "FieldEnable"
    assert d["capabilities"] == {"time": True, "video": True, "audio": True, "imaging": True, "analytics": True, "reboot": True}
    assert d["defaults"]["ntp_server"] == "pool.ntp.org" and d["defaults"]["timezone"] == "America/Chicago"
    assert d["defaults"]["posix"] == "CST6CDT,M3.2.0,M11.1.0"
    assert not [a for a in ME.actions() if a.startswith("Set")], "reading must not write"


def test_read_all_thermal_and_speaker():
    d = call("GET", "/api/cameras/thermal/device").json()
    v = d["video"]
    assert v["media"] == "media2" and v["sources"] == ["VideoSource_1", "VideoSource_2"]
    ve0 = next(e for e in v["encoders"] if e["token"] == "ve0")
    assert ve0["encoding"] == "H265" and ve0["gov_length"] == 50 and ve0["gov_seconds"] == 2.0 and ve0["fps"] == 25 and ve0["cbr"] is False
    assert {o["encoding"] for o in ve0["options"]["encodings"]} == {"H264", "H265"}
    assert ve0["options"]["encodings"][0]["fps_list"] == [1, 5, 10, 12, 15, 20, 25]
    assert next(e for e in v["encoders"] if e["token"] == "ve2")["source"] == "VideoSource_2"
    assert d["audio"]["encoders"][0]["encoding"] == "G711" and d["audio"]["encoders"][0]["encoding_raw"] == "PCMU"
    assert [s["source"] for s in d["imaging"]["sources"]] == ["VideoSource_1", "VideoSource_2"]
    assert d["analytics"] == {"supported": False, "reason": "the camera has no ONVIF analytics service"}
    # the speaker: no video, no imaging; its GetNTP fault echoes its password, which never comes back
    r = call("GET", "/api/cameras/spk/device")
    d = r.json()
    assert d["video"] == {"supported": False, "reason": "the camera lists no video encoder"}
    assert d["imaging"]["supported"] is False and d["audio"]["supported"] is True
    assert d["time"]["supported"] and d["time"]["ntp"] is None and "***" in d["time"]["ntp_error"]
    assert PW not in r.text
    # a camera that doesn't answer at all: every section says so, nothing raises
    d = call("GET", "/api/cameras/dead/device").json()
    assert d["reachable"] is False and d["video"]["supported"] is False and "connection failed" in d["time"]["reason"]


def test_set_time_ntp_steps_a_far_off_clock():
    ME.requests.clear()
    api.state.ingests["cam3"].meta.resets = 0
    r = call("PUT", "/api/cameras/cam3/device/time", {"mode": "ntp", "ntp_servers": ["pool.ntp.org", "192.168.105.1"], "timezone": "America/Chicago"})
    assert r.status_code == 200, r.text
    t = r.json()
    sets = ME.bodies("SetSystemDateAndTime")
    assert "<tt:DNSname>pool.ntp.org</tt:DNSname>" in ME.bodies("SetNTP")[0] and "<tds:FromDHCP>false</tds:FromDHCP>" in ME.bodies("SetNTP")[0]
    assert "<tds:DateTimeType>NTP</tds:DateTimeType>" in sets[0] and "<tt:TZ>CST6CDT,M3.2.0,M11.1.0</tt:TZ>" in sets[0]
    assert "<tds:DaylightSavings>true</tds:DaylightSavings>" in sets[0]
    # still 45 min off after switching to NTP: set from this server's clock, then back on NTP
    assert "<tds:DateTimeType>Manual</tds:DateTimeType>" in sets[1] and "<tds:UTCDateTime>" in sets[1]
    assert "<tds:DateTimeType>NTP</tds:DateTimeType>" in sets[2] and "TimeZone" not in sets[2]
    assert all("<tds:DaylightSavings>true</tds:DaylightSavings>" in x for x in sets)   # the step keeps daylight saving on
    assert abs(t["offset_s"]) < 3 and t["mode"] == "NTP" and t["timezone"] == "CST6CDT,M3.2.0,M11.1.0"
    assert t["ntp_servers"] == ["pool.ntp.org", "192.168.105.1"] and any("set from this server" in w for w in t["warnings"])
    assert api.state.ingests["cam3"].meta.resets == 1   # detections are re-timed against the new clock
    if camdevice.utc_offset_s("America/Chicago") == -5 * 3600:
        assert t["zone_check"]["ok"] is True
    # bad input: refused before the camera is asked
    ME.requests.clear()
    assert call("PUT", "/api/cameras/cam3/device/time", {"mode": "ntp", "ntp_servers": ["pool.ntp.org"], "timezone": "Mars/Base"}).status_code == 422
    assert call("PUT", "/api/cameras/cam3/device/time", {"mode": "ntp", "ntp_servers": ["bad host!"]}).status_code == 422
    assert call("PUT", "/api/cameras/cam3/device/time", {"mode": "ntp", "ntp_servers": []}).status_code == 422
    assert not ME.actions("SetNTP") and not ME.actions("SetSystemDateAndTime")


def test_sync_now_and_a_refused_zone():
    UV.offset = 3600
    UV.requests.clear()
    r = call("POST", "/api/cameras/thermal/device/time/sync-now")
    assert r.status_code == 200, r.text
    assert abs(r.json()["offset_s"]) < 3
    b = UV.bodies("SetSystemDateAndTime")
    assert len(b) == 1 and "<tds:DateTimeType>Manual</tds:DateTimeType>" in b[0] and "TimeZone" not in b[0]   # it was Manual: stays
    # the thermal camera refuses POSIX zones: its clock is set without one, with a warning
    UV.offset = 3600
    UV.requests.clear()
    r = call("PUT", "/api/cameras/thermal/device/time", {"mode": "manual_sync", "timezone": "America/Chicago"})
    assert r.status_code == 200, r.text
    t = r.json()
    assert abs(t["offset_s"]) < 3 and t["timezone"] == "CST6" and "refused the time zone" in t["warnings"][0]
    b = UV.bodies("SetSystemDateAndTime")
    assert "<tt:TZ>CST6CDT" in b[0] and "TimeZone" not in b[1] and "<tds:UTCDateTime>" in b[1]


def test_video_gov_seconds_and_read_modify_write():
    ME.requests.clear()
    api.state.streams.asked.clear()
    r = call("PUT", "/api/cameras/cam3/device/video/VE_1", {"gov_seconds": 1})
    assert r.status_code == 200, r.text
    assert r.json()["gov_length"] == 25 and r.json()["gov_seconds"] == 1.0
    body = ME.bodies("SetVideoEncoderConfiguration")[0]
    for kept in ("keep-me", "239.6.6.6", "<tt:H264Profile>High</tt:H264Profile>", "PT60S", "<tt:Quality>4</tt:Quality>",
                 "<tt:BitrateLimit>6144</tt:BitrateLimit>", "<tt:Width>2592</tt:Width>", "<tt:EncodingInterval>1</tt:EncodingInterval>"):
        assert kept in body, kept
    assert "<tt:GovLength>25</tt:GovLength>" in body and "<trt:ForcePersistence>true</trt:ForcePersistence>" in body
    assert 'token="VE_1"' in body and "<trt:SetVideoEncoderConfiguration><trt:Configuration " in body
    assert 'xmlns:tt="http://www.onvif.org/ver10/schema"' in body or "<tt:" in body
    assert api.state.streams.asked == ["cam3"]   # the stream check runs again
    # a new frame rate with seconds: frames at the new rate; frame rate alone keeps the interval in seconds
    r = call("PUT", "/api/cameras/cam3/device/video/VE_1", {"fps": 15, "gov_seconds": 2})
    assert (r.json()["fps"], r.json()["gov_length"]) == (15, 30)
    r = call("PUT", "/api/cameras/cam3/device/video/VE_1", {"fps": 20})
    assert (r.json()["fps"], r.json()["gov_length"], r.json()["gov_seconds"]) == (20, 40, 2.0)
    r = call("PUT", "/api/cameras/cam3/device/video/VE_1", {"bitrate_kbps": 4096, "resolution": "1920x1080"})
    assert (r.json()["bitrate_kbps"], r.json()["width"], r.json()["height"]) == (4096, 1920, 1080)
    assert "keep-me" in ME.ve["VE_1"]


def test_video_refused_before_any_write():
    ME.requests.clear()
    for body, says in (({"fps": 60}, "outside"), ({"bitrate_kbps": 99999}, "kbps"), ({"resolution": "640x480"}, "not offered"),
                       ({"gov_seconds": 20}, "outside"), ({"resolution": "huge"}, "not a resolution"),
                       ({"encoding": "H265"}, "web page"), ({}, "nothing to change")):
        r = call("PUT", "/api/cameras/cam3/device/video/VE_2", body)
        assert r.status_code == 422 and says in r.text, (body, r.status_code, r.text)
    assert call("PUT", "/api/cameras/cam3/device/video/nope", {"fps": 10}).status_code == 409
    assert call("PUT", "/api/cameras/cam3/device/video/VE_2", {"fps": 500}).status_code == 422   # the API's own bound
    assert not ME.actions("SetVideoEncoderConfiguration")


def test_video_media2():
    UV.requests.clear()
    r = call("PUT", "/api/cameras/thermal/device/video/ve0", {"fps": 20, "gov_seconds": 1})
    assert r.status_code == 200, r.text
    body = UV.bodies("SetVideoEncoderConfiguration")[0]
    assert "<tr2:SetVideoEncoderConfiguration><tr2:Configuration " in body and 'GovLength="20"' in body and 'Profile="Main"' in body
    assert "<tt:FrameRateLimit>20</tt:FrameRateLimit>" in body and "<tt:Encoding>H265</tt:Encoding>" in body
    assert "ForcePersistence" not in body and "<tt:IPv4Address>0.0.0.0</tt:IPv4Address>" in body   # Media2: always persistent
    assert (r.json()["fps"], r.json()["gov_length"]) == (20, 20)
    # Media2: the encoding can change (to what the options list), with a resolution it offers
    r = call("PUT", "/api/cameras/thermal/device/video/ve0", {"encoding": "H264"})
    assert r.status_code == 200 and r.json()["encoding"] == "H264", r.text
    assert call("PUT", "/api/cameras/thermal/device/video/ve0", {"encoding": "JPEG"}).status_code == 422
    assert call("PUT", "/api/cameras/thermal/device/video/ve2", {"fps": 18}).status_code == 422   # not in FrameRatesSupported


def test_audio_to_aac_clears_the_transcode_cache():
    api._audio_tracks["cam3"] = (time.time(), True)
    ME.requests.clear()
    r = call("PUT", "/api/cameras/cam3/device/audio/AE_1", {"encoding": "AAC"})
    assert r.status_code == 200, r.text
    assert r.json()["encoding"] == "AAC" and (r.json()["bitrate"], r.json()["sample_rate"]) == (64, 8)
    body = ME.bodies("SetAudioEncoderConfiguration")[0]
    assert "<tt:Encoding>AAC</tt:Encoding>" in body and "<tt:SessionTimeout>PT60S</tt:SessionTimeout>" in body and "ForcePersistence>true" in body
    assert "cam3" not in api._audio_tracks
    assert call("PUT", "/api/cameras/cam3/device/audio/AE_1", {"encoding": "AAC", "sample_rate": 48}).status_code == 422
    assert call("PUT", "/api/cameras/cam3/device/audio/AE_1", {"encoding": "OPUS"}).status_code == 422
    # Media2 spells AAC as MP4A-LATM and only offers 16 kHz for it: the nearest is taken
    UV.requests.clear()
    r = call("PUT", "/api/cameras/thermal/device/audio/a0", {"encoding": "AAC"})
    assert r.status_code == 200 and r.json()["encoding"] == "AAC" and r.json()["sample_rate"] == 16, r.text
    assert "<tt:Encoding>MP4A-LATM</tt:Encoding>" in UV.bodies("SetAudioEncoderConfiguration")[0]


def test_imaging():
    ME.requests.clear()
    r = call("PUT", "/api/cameras/cam3/device/imaging/VideoSource_1", {"brightness": 60, "ir_cut": "ON"})
    assert r.status_code == 200, r.text
    assert r.json()["brightness"]["value"] == 60 and r.json()["ir_cut"]["value"] == "ON"
    body = ME.bodies("SetImagingSettings")[0]
    assert "<tt:MaxExposureTime>33333</tt:MaxExposureTime>" in body and "<tt:Brightness>60</tt:Brightness>" in body
    assert "<timg:VideoSourceToken>VideoSource_1</timg:VideoSourceToken>" in body
    ME.requests.clear()
    assert call("PUT", "/api/cameras/cam3/device/imaging/VideoSource_1", {"brightness": 150}).status_code == 422
    assert not ME.actions("SetImagingSettings")


def test_analytics_rules():
    r = call("PUT", "/api/cameras/cam3/device/analytics", {"configuration": "VideoAnalyticsToken", "rule": "MyLineDetectorRule1", "enabled": True})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True and ME.rules["line"] == "true"
    body = ME.bodies("ModifyRules")[0]
    assert 'xmlns:ttx="http://www.onvif.org/ver10/schema"' in body and 'Type="ttx:LineDetector"' in body
    assert "<tt:Point" in body or "Point" in body   # the line itself is sent back unchanged
    r = call("PUT", "/api/cameras/cam3/device/analytics", {"configuration": "VideoAnalyticsToken", "rule": "MyFieldDetectorRule1", "enabled": True})
    assert r.status_code == 200 and ME.rules["field"] == "true"
    r = call("PUT", "/api/cameras/cam3/device/analytics", {"configuration": "VideoAnalyticsToken", "rule": "MyCellMotionDetectorRule", "enabled": False})
    assert r.status_code == 422 and "web page" in r.text


def test_fix_clocks():
    ME.offset, UV.offset = 2700, 3600
    r = call("POST", "/api/cameras/device/time", {"camera_ids": ["cam3", "thermal", "dead"], "mode": "ntp",
                                                  "ntp_servers": ["192.168.105.1"], "timezone": "America/Chicago"})
    assert r.status_code == 200, r.text
    res = {x["camera_id"]: x for x in r.json()["results"]}
    assert [x["camera_id"] for x in r.json()["results"]] == ["cam3", "thermal", "dead"]
    assert res["cam3"]["ok"] and abs(res["cam3"]["offset_s"]) < 3
    assert res["thermal"]["ok"] and abs(res["thermal"]["offset_s"]) < 3 and any("time zone" in w for w in res["thermal"]["warnings"])
    assert not res["dead"]["ok"] and "connection failed" in res["dead"]["error"]
    assert db.get_setting("camera_time") == {"ntp_server": "192.168.105.1", "timezone": "America/Chicago"}
    assert call("GET", "/api/cameras/device/time").json()["ntp_server"] == "192.168.105.1"
    assert call("POST", "/api/cameras/device/time", {"camera_ids": ["nope"], "ntp_servers": ["pool.ntp.org"]}).status_code == 422


def test_reboot():
    ME.requests.clear()
    r = call("POST", "/api/cameras/cam3/device/reboot")
    assert r.status_code == 200 and r.json() == {"ok": True, "message": "Rebooting in 5 seconds"}
    assert ME.actions("SystemReboot") == ["SystemReboot"]


def test_roles_through_the_tunnel():
    ME.requests.clear()
    assert call("GET", "/api/cameras/cam3/device", role="viewer").status_code == 403
    assert call("GET", "/api/cameras/cam3/device", role="operator").status_code == 200
    for role in ("viewer", "operator"):
        assert call("PUT", "/api/cameras/cam3/device/time", {"mode": "manual_sync"}, role=role).status_code == 403
        assert call("POST", "/api/cameras/cam3/device/reboot", role=role).status_code == 403
        assert call("PUT", "/api/cameras/cam3/device/video/VE_1", {"fps": 10}, role=role).status_code == 403
        assert call("POST", "/api/cameras/device/time", {"ntp_servers": ["pool.ntp.org"]}, role=role).status_code == 403
    assert not [a for a in ME.actions() if a.startswith("Set") or a == "SystemReboot"]
    r = call("POST", "/api/cameras/cam3/device/time/sync-now", role="admin")
    assert r.status_code == 200, r.text
    assert call("GET", "/api/cameras/nope/device", role="admin").status_code == 404


def test_clean():
    cam = {"password": PW}
    assert camdevice.clean(f"connection failed: http://admin:{PW}@10.0.0.5/onvif", cam) == "connection failed: http://10.0.0.5/onvif"
    assert camdevice.clean(f"bad login {PW}", cam) == "bad login ***"


def test_soap_client_keeps_the_raw_reply():
    c = soap.Onvif.for_camera(ME.row("cam3", "x"))
    soap.sync_clock(c)
    assert b"GetSystemDateAndTimeResponse" in c.last_raw


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
