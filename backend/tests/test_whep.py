"""Remote live view: public ICE candidates added to WHEP answers, and the session's Location / DELETE (hang-up) proxied
to MediaMTX (a fake one: no network or MediaMTX needed).

Run: ..\\.venv\\Scripts\\python.exe tests\\test_whep.py   (from backend/)
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-whep-test-")  # never the real DB
# ...nor the running server's recordings or runtime folder (mediamtx.yml)
os.environ["NVR_RECORDINGS_DIR"] = tempfile.mkdtemp(prefix="nvr-whep-rec-")
os.environ["NVR_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="nvr-whep-runtime-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tunnelproto"))

import httpx  # noqa: E402

from nvr import api, hub_agent, mediamtx  # noqa: E402
from nvr.api import add_candidates, public_ips, whep_location  # noqa: E402
from nvr.config import settings  # noqa: E402

# shaped like a real MediaMTX v1.21 answer: candidates only in the first (BUNDLE) section, then end-of-candidates
ANSWER = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\na=group:BUNDLE 0 1\r\n"
          "m=video 9 UDP/TLS/RTP/SAVPF 108\r\nc=IN IP4 0.0.0.0\r\na=setup:active\r\n"
          "a=candidate:2002031858 1 udp 2130706431 192.168.105.105 8189 typ host ufrag U\r\n"
          "a=candidate:3400000786 1 tcp 1671430143 192.168.105.105 8189 typ host tcptype passive ufrag U\r\n"
          "a=end-of-candidates\r\n"
          "m=audio 9 UDP/TLS/RTP/SAVPF 0\r\nc=IN IP4 0.0.0.0\r\na=setup:active\r\n")


def test_public_ips_skip_private_and_parse_hosts():
    assert public_ips(["192.168.105.50:8080", "localhost:8080", "10.1.2.3", "[::1]:8080"]) == []
    assert public_ips(["203.0.113.7:8080", "8.8.8.8", "8.8.8.8:443"]) == ["8.8.8.8"]  # 203.0.113/24 is documentation space
    assert public_ips(["nonexistent.invalid:8080"]) == []


def test_add_candidates_next_to_mediamtx_ones():
    out = add_candidates(ANSWER, ["8.8.4.4"], 8189)
    video, audio = out.split("m=")[1:]
    assert "udp 1694498815 8.8.4.4 8189 typ host\r\n" in video and "tcp 1518280447 8.8.4.4 8189 typ host tcptype passive" in video
    assert video.index("8.8.4.4") < video.index("a=end-of-candidates") and "192.168.105.105" in video  # LAN kept
    assert "8.8.4.4" not in audio                       # bundled section: no candidates, as MediaMTX sends it
    assert out.replace("\r\n", "").count("\n") == 0 and out.endswith("\r\n")  # CRLF throughout, trailing CRLF kept


SESSION = "0b6c1f8e-2d3a-4e5f-8a9b-1c2d3e4f5a6b"


class FakeMediaMTX:
    """MediaMTX's WebRTC listener as the WHEP proxy calls it: records each request, answers like v1.x does."""

    def __init__(self, location: str | None = f"/cam1_sub/whep/{SESSION}", delete_status: int = 200):
        self.location, self.delete_status, self.seen = location, delete_status, []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.seen.append((req.method, req.url.path, req.headers.get("authorization")))
        if req.method == "POST":
            return httpx.Response(201, content=ANSWER.encode(), headers={"content-type": "application/sdp",
                                                                          **({"location": self.location} if self.location else {})})
        return httpx.Response(self.delete_status)


def call(mtx: FakeMediaMTX, method: str, url: str, tunnel: bool = False, **kw) -> httpx.Response:
    """One request to the NVR app (as the LAN UI, or through the hub tunnel), its MediaMTX calls answered by `mtx`."""
    real = httpx.AsyncClient

    class Client(real):   # the proxy's own clients get the fake; ours (below) passes its transport explicitly
        def __init__(self, *a, **k):
            k.setdefault("transport", httpx.MockTransport(mtx.handler))
            super().__init__(*a, **k)

    async def go():
        app = hub_agent.as_tunnel(api.app) if tunnel else api.app
        async with real(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://localhost:8080") as c:
            return await c.request(method, url, **kw)
    httpx.AsyncClient = Client
    try:
        return asyncio.run(go())
    finally:
        httpx.AsyncClient = real


def test_whep_location_rewritten_to_this_server():
    assert whep_location("cam1_sub", f"/cam1_sub/whep/{SESSION}") == f"/api/whep/cam1_sub/{SESSION}"
    assert whep_location("cam1_sub", f"cam1_sub/whep/{SESSION}") == f"/api/whep/cam1_sub/{SESSION}"
    assert whep_location("cam1_sub", f"http://127.0.0.1:8889/cam1_sub/whep/{SESSION}") == f"/api/whep/cam1_sub/{SESSION}"
    for bad in (None, "", f"/cam2/whep/{SESSION}", "/cam1_sub/whep/not-a-uuid", f"/cam1_sub/whep/{SESSION}/x",
                f"/cam1_sub/whep/{SESSION.upper()}", f"/cam1_sub/whip/{SESSION}", "/cam1_sub/whep/../../api/cameras"):
        assert whep_location("cam1_sub", bad) is None, bad


def test_offer_answer_carries_the_session_location():
    settings.rtsp_auth = True
    mtx = FakeMediaMTX()
    r = call(mtx, "POST", "/api/whep/cam1_sub", headers={"Content-Type": "application/sdp"}, content=b"v=0")
    assert r.status_code == 201 and r.headers["location"] == f"/api/whep/cam1_sub/{SESSION}"
    assert r.text.startswith("v=0")
    assert mtx.seen == [("POST", "/cam1_sub/whep", mediamtx.reader_auth_header()["Authorization"])]
    # an answer without a session (or with one we can't vouch for) passes no Location on
    for loc in (None, "/elsewhere/whep/x"):
        r = call(FakeMediaMTX(location=loc), "POST", "/api/whep/cam1_sub", headers={"Content-Type": "application/sdp"}, content=b"v=0")
        assert r.status_code == 201 and "location" not in r.headers, loc


def test_hang_up_proxied_to_mediamtx_with_the_reader_password():
    settings.rtsp_auth = True
    mtx = FakeMediaMTX()
    r = call(mtx, "DELETE", f"/api/whep/cam1_sub/{SESSION}", headers={"Origin": "http://localhost:8080"})
    assert r.status_code == 200
    assert mtx.seen == [("DELETE", f"/cam1_sub/whep/{SESSION}", mediamtx.reader_auth_header()["Authorization"])]
    # through the hub tunnel too; MediaMTX's 404 (session already gone) is passed on, its 5xx becomes 502
    assert call(FakeMediaMTX(delete_status=404), "DELETE", f"/api/whep/cam1/{SESSION}", tunnel=True).status_code == 404
    assert call(FakeMediaMTX(delete_status=500), "DELETE", f"/api/whep/cam1/{SESSION}").status_code == 502


def test_hang_up_refuses_odd_paths_and_session_ids():
    for url in (f"/api/whep/BAD!/{SESSION}", f"/api/whep/{'a' * 41}/{SESSION}", "/api/whep/cam1/abc",
                f"/api/whep/cam1/{SESSION.upper()}", f"/api/whep/cam1/{SESSION}x", "/api/whep/cam1/%2e%2e"):
        mtx = FakeMediaMTX()
        r = call(mtx, "DELETE", url)
        assert r.status_code in (400, 404, 405) and mtx.seen == [], (url, r.status_code)
    # a page from another site may not hang up this server's sessions (lan_guard)
    mtx = FakeMediaMTX()
    assert call(mtx, "DELETE", f"/api/whep/cam1/{SESSION}", headers={"Origin": "https://evil.example"}).status_code == 403
    assert mtx.seen == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
