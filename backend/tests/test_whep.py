"""Remote live view: public ICE candidates added to WHEP answers (no network needed).

Run: ..\\.venv\\Scripts\\python.exe tests\\test_whep.py   (from backend/)
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-whep-test-")  # never the real DB
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr.api import add_candidates, public_ips  # noqa: E402

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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
