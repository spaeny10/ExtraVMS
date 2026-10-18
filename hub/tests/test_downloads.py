"""/downloads/: the Axiom Bridge release the enrollment dialog's one-line install fetches (no sign-in)."""
import json

from hub.config import settings


def test_downloads_serves_the_release_and_nothing_else(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "downloads_dir", tmp_path)
    (tmp_path / "bridge" / "0.1.0").mkdir(parents=True)
    (tmp_path / "bridge" / "0.1.0" / "axiom-bridge.exe").write_bytes(b"MZ fake exe")
    (tmp_path / "bridge" / "latest.json").write_text(json.dumps({"version": "0.1.0"}))
    (tmp_path / "secret.txt").write_text("outside a product folder is fine to serve, but not above the root")
    client.cookies.clear()   # public: no sign-in

    r = client.get("/downloads/bridge/latest.json")
    assert r.status_code == 200 and r.json() == {"version": "0.1.0"} and r.headers["cache-control"] == "no-cache"
    r = client.get("/downloads/bridge/0.1.0/axiom-bridge.exe")
    assert r.status_code == 200 and r.content == b"MZ fake exe"
    assert r.headers["content-type"] == "application/octet-stream" and "immutable" in r.headers["cache-control"]
    assert 'filename="axiom-bridge.exe"' in r.headers["content-disposition"]

    for bad in ("/downloads/bridge/0.1.0/missing.exe", "/downloads/bridge/0.1.0", "/downloads/",
                "/downloads/bridge/%2e%2e/%2e%2e/etc/passwd", "/downloads/bridge/..%2f..%2fhub.db",
                "/downloads/a/b/c/d", "/downloads/.hidden"):
        assert client.get(bad).status_code == 404, bad
