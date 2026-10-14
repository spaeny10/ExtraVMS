"""Every writing route of the site API must have an explicit role rule (so a new endpoint is never opened to
viewers by accident), and the rules say what we mean."""
import re

from hub.roles import RULES, allows, required_role


def test_role_table_reads_right():
    assert required_role("GET", "/api/events") == "viewer"
    assert required_role("POST", "/api/whep/cam1") == "viewer"
    assert required_role("POST", "/api/assistant/ask") == "viewer"
    assert required_role("POST", "/api/assistant/retrieve") == "viewer"   # the hub's Site Ask: read-only evidence
    assert required_role("POST", "/api/cameras/cam1/ptz/move") == "operator"
    assert required_role("POST", "/api/cameras/cam1/relay") == "operator"
    assert required_role("PUT", "/api/events/12/feedback") == "operator"
    assert required_role("PUT", "/api/cameras/cam1") == "admin"
    assert required_role("PUT", "/api/hub") == "owner"
    assert required_role("PUT", "/api/find/views") == "operator"          # Find saved views
    assert required_role("GET", "/api/events/summary") == "viewer"
    assert required_role("POST", "/api/some/new/thing") == "admin"     # unknown writes need admin
    # the system optimizer: hiding a suggestion is an operator's call, applying one changes configuration
    assert required_role("POST", "/api/advisor/dismiss") == "operator"
    assert required_role("POST", "/api/advisor/undismiss") == "operator"
    assert required_role("POST", "/api/advisor/apply") == "admin"
    assert required_role("PUT", "/api/cameras/cam1/ptz/config") == "admin"
    assert required_role("POST", "/api/cameras/cam1/ptz/presets") == "operator"
    # asking a camera which streams it serves re-plans its SD live source (mediamtx.yml): configuration
    assert required_role("POST", "/api/cameras/cam1/streams/check") == "admin"
    # SD card recovery writes footage into the recordings: configuration; its status and gaps are watching
    assert required_role("POST", "/api/sd/recover") == "admin"
    assert required_role("GET", "/api/sd/gaps") == "viewer" and required_role("GET", "/api/cameras/cam1/sd") == "viewer"
    # talk-down: playing and stopping a message is an operator's act; speakers, Check and the message library are configuration
    assert required_role("POST", "/api/speakers/spk1/play") == "operator" and required_role("POST", "/api/speakers/spk1/stop") == "operator"
    assert required_role("PUT", "/api/speakers/spk1") == "admin" and required_role("POST", "/api/speakers/spk1/check") == "admin"
    assert required_role("POST", "/api/talkdown/messages") == "admin" and required_role("DELETE", "/api/talkdown/messages/3") == "admin"
    assert required_role("POST", "/api/cameras/cam7/to-speaker") == "admin"
    assert required_role("GET", "/api/speakers/spk1/status") == "viewer"
    # Describe & warn: Qwen describes who is on the camera, the operator plays the reviewed text (the same act as play)
    assert required_role("POST", "/api/speakers/spk1/describe") == "operator"
    assert required_role("PUT", "/api/speakers/spk1/describe") == "admin" and required_role("POST", "/api/speakers/spk1/describe/x") == "admin"
    # the talk-down voice: an operator previews it (in the browser, never on a speaker) and reads it; setting it is configuration.
    # The operator rules come before the /api/(speakers|talkdown) admin catch-all, and nothing near them opens up.
    assert required_role("POST", "/api/talkdown/preview") == "operator"
    assert required_role("GET", "/api/talkdown/voice") == "operator" and required_role("GET", "/api/talkdown/voices") == "operator"
    assert required_role("HEAD", "/api/talkdown/voices") == "operator"
    assert required_role("PUT", "/api/talkdown/voice") == "admin" and required_role("POST", "/api/talkdown/voice") == "admin"
    assert required_role("PUT", "/api/talkdown/preview") == "admin" and required_role("POST", "/api/talkdown/preview/x") == "admin"
    assert required_role("GET", "/api/talkdown/messages") == "viewer" and required_role("GET", "/api/talkdown/voicesx") == "viewer"
    assert allows("owner", "admin") and allows("operator", "viewer") and not allows("viewer", "operator")


def test_every_site_write_route_is_covered():
    from nvr.api import app as site_app
    from nvr.config import settings
    for d in (settings.data_dir, settings.recordings_dir, settings.runtime_dir):   # conftest: temp folders, never the live server's
        v = str(d).replace("\\", "/").lower()
        assert "hub-test-" in v and "e:/nvr" not in v and "d:/nvr" not in v, d
    unmatched = []
    for r in site_app.routes:
        methods = getattr(r, "methods", None) or set()
        path = getattr(r, "path", "")
        if not path.startswith("/api/") or path == "/api/hub":
            continue
        for m in methods - {"GET", "HEAD", "OPTIONS"}:
            concrete = re.sub(r"\{[^}]+\}", "1", path)  # any path parameter: numeric ids and camera/preset tokens alike
            if not any(m in ms and rx.search(concrete) for ms, rx, _ in RULES):
                unmatched.append(f"{m} {path}")
    assert not unmatched, "add these to hub/hub/roles.py RULES: " + ", ".join(sorted(unmatched))
