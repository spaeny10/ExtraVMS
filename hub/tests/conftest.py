"""Hub tests run on a throwaway SQLite database and an in-process app."""
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="hub-test-")
os.environ["HUB_DATABASE_URL"] = f"sqlite:///{Path(TMP, 'hub.db').as_posix()}"
os.environ["HUB_SECRET"] = "test-secret"
os.environ["HUB_COOKIE_SECURE"] = "0"
os.environ["HUB_PUBLIC_URL"] = "http://testserver"
os.environ["HUB_SOC_LOOPS"] = "0"   # no background escalation racing the tests: they call soc.escalate_once(now)
os.environ["HUB_GEOCODE_BACKFILL"] = "0"   # no geocoder calls from tests (test_geocode drives geocode.backfill with a fake transport)
os.environ["NVR_DATA_DIR"] = str(Path(TMP, "site"))   # for tests that import the site package
# ...and the rest of the site's folders and its MediaMTX: never the live server's (recordings, runtime config, index)
os.environ["NVR_RECORDINGS_DIR"] = str(Path(TMP, "site-recordings"))
os.environ["NVR_RUNTIME_DIR"] = str(Path(TMP, "site-runtime"))
os.environ["NVR_FOOTAGE_INDEX_DIR"] = str(Path(TMP, "site-index"))
os.environ["NVR_BACKUP_DIR"] = str(Path(TMP, "site-backups"))
os.environ["NVR_MEDIAMTX_API"] = "http://127.0.0.1:9"
os.environ["NVR_MEDIAMTX_PLAYBACK"] = "http://127.0.0.1:9"
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tunnelproto"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from hub import auth, db  # noqa: E402
from hub.api import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    db.engine()
    with TestClient(app, base_url="http://testserver") as c:
        yield c


@pytest.fixture(scope="session")
def superuser(client):
    u = auth.create_user("root@example.com", "correct horse battery", is_super=True)
    return {"email": u["email"], "password": "correct horse battery", "id": u["id"]}


def login(client, email, password, totp=None):
    r = client.post("/auth/login", json={"email": email, "password": password, **({"totp": totp} if totp else {})})
    assert r.status_code == 200, r.text
    return r.json()
