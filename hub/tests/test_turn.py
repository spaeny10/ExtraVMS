import base64
import hashlib
import hmac
import time

from hub import turn
from hub.config import settings


def test_credentials_match_coturn_use_auth_secret():
    settings.turn_host, settings.turn_secret, settings.turn_port, settings.turn_tls_port = "turn.example", "s3cret", 3478, 5349
    c = turn.mint("site:s_1", 3600)
    assert c["urls"] == ["turn:turn.example:3478?transport=udp", "turn:turn.example:3478?transport=tcp", "turns:turn.example:5349?transport=tcp"]
    expiry, scope = c["username"].split(":", 1)
    assert scope == "site:s_1" and int(expiry) - time.time() > 3500
    expected = base64.b64encode(hmac.new(b"s3cret", c["username"].encode(), hashlib.sha1).digest()).decode()
    assert c["credential"] == expected
    assert turn.ice_servers("u:1", 60)[0]["username"].endswith(":u:1")
    settings.turn_secret = ""
    assert turn.mint("x", 1) is None and turn.ice_servers("x", 1) == []
