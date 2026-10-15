"""The session cookie over HTTPS is __Host-hub_session (auth.COOKIE): Secure, host-only, Path=/, so a camera's web page
on a sibling host (camweb) can't plant or overwrite it; the old name is cleared and never read. Run in a fresh
interpreter: COOKIE is fixed when hub.auth is imported, and the test suite runs with HUB_COOKIE_SECURE=0."""
import os
import subprocess
import sys
from pathlib import Path

PROBE = r'''
from starlette.responses import Response
from starlette.requests import Request
from hub import auth
assert auth.COOKIE == "__Host-hub_session", auth.COOKIE
r = Response(); auth.set_cookie(r, "s" * 40)
cookies = [v.decode() for k, v in r.raw_headers if k == b"set-cookie"]
new = next(c for c in cookies if c.startswith("__Host-hub_session="))
low = new.lower()
assert "secure" in low and "path=/" in low and "domain=" not in low and "httponly" in low, new
assert any(c.startswith("hub_session=") and "max-age=0" in c.lower() for c in cookies), cookies   # old name cleared
scope = {"type": "http", "headers": [(b"cookie", b"hub_session=planted")], "method": "GET", "path": "/"}
assert auth.session_of(Request(scope)) is None   # a tossed old-style cookie is never read
print("ok")
'''


def test_https_session_cookie_is_host_prefixed():
    env = {**os.environ, "HUB_COOKIE_SECURE": "1"}
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=Path(__file__).resolve().parents[1], env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stderr[-2000:]
