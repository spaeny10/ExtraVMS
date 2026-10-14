"""A Site's talk-down voice (talkdown_voice.py): read by everyone who sees the Site (with each server's status: which
voices it has, whether it took the Site's), set by its admins only, audited, pushed at once to every connected server of
the Site through the tunnel (PUT /api/talkdown/voice as admin; a server too old for it is skipped, a failing one is
reported); a heartbeat reporting another voice gets the Site's pushed again, at most once per REPUSH_S for one value;
deleting the Site deletes its voice."""
import asyncio
import json

import pytest
import sqlalchemy as sa

from hub import agents, db, talkdown_voice
from hub.agents import registry
from test_access import _login, server

PASS = "voice-pass-123"
URL = "/api/locations/{}/talkdown-voice"


def _tv(setting=None, voices=(), engines=None):
    return {"setting": setting or {"voice": None, "speed": 1.0, "chime": False}, "voices": list(voices),
            "engines": engines or {"piper": "piper-tts", "sapi": True, "espeak": None},
            "effective": {"engine": "piper", "voice": (list(voices) or [None])[0], "requested": None, "fallback": False, "reason": None, "label": "x"}}


def _summary(s: dict, summary: dict) -> None:
    db.run(sa.update(db.sites).where(db.sites.c.id == s["id"]).values(summary=summary))


class VoiceConn:
    """A connected server answering PUT /api/talkdown/voice like backend speakers_api.put_voice; remembers each call."""

    def __init__(self, s: dict, fail: str | None = None, status: int = 200):
        self.site_id, self.site, self.summary, self.location = s["id"], s, None, None
        self.calls: list[tuple[str, str, dict, dict | None]] = []
        self.fail, self.status = fail, status

    async def call(self, method, path, query="", headers=None, body=None, timeout=15.0):
        self.calls.append((method, path, headers or {}, json.loads(body) if body else None))
        if self.fail:
            raise RuntimeError(self.fail)
        if self.status != 200:
            return self.status, json.dumps({"detail": "needs the admin role"}).encode()
        b = json.loads(body)
        return 200, json.dumps({"setting": b, "managed": True, "effective": {"engine": "piper", "voice": b["voice"], "requested": b["voice"],
                                                                            "fallback": False, "reason": None, "label": "x"}}).encode()


@pytest.fixture(scope="module")
def world(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": "Voice Co", "slug": "voice-co"}).json()["id"]
    yard = root.post(f"/api/orgs/{oid}/locations", json={"name": "Voice Yard"}).json()
    a, b, c = server(oid, "Alpha", yard["id"]), server(oid, "Bravo", yard["id"]), server(oid, "Charlie (old)", yard["id"])
    d = server(oid, "Delta (offline)", yard["id"])
    other = server(oid, "Elsewhere")
    _summary(a, {"talkdown_voice": _tv(voices=["en_US-amy-medium", "en_GB-alan-medium"])})
    _summary(b, {"talkdown_voice": _tv(voices=[], engines={"piper": None, "sapi": True, "espeak": None})})
    _summary(c, {"speakers": []})   # an older server: no talkdown_voice in its summary
    _summary(d, {"talkdown_voice": _tv(voices=["en_US-amy-medium"])})
    for s in (a, b, c, d, other):
        s.update(db.one(sa.select(db.sites).where(db.sites.c.id == s["id"])))
    conns = {"a": VoiceConn(a), "b": VoiceConn(b, fail="site did not answer in time"), "c": VoiceConn(c), "other": VoiceConn(other)}
    for k, conn in conns.items():
        registry.by_site[conn.site_id] = conn
    users = {}
    for role in ("viewer", "operator", "admin"):
        email = f"{role}@voice.example"
        r = root.post(f"/api/orgs/{oid}/members", json={"email": email, "role": role, "password": PASS, "all_sites": False,
                                                       "location_ids": [yard["id"]]})
        assert r.status_code == 200, r.text
        users[role] = _login(client, email, PASS)
    foreign = root.post("/api/orgs", json={"name": "Other Voice Co", "slug": "other-voice-co"}).json()["id"]
    root.post(f"/api/orgs/{foreign}/members", json={"email": "fx@voice.example", "role": "admin", "password": PASS})
    yield {"root": root, "oid": oid, "yard": yard, "a": a, "b": b, "c": c, "d": d, "other": other, "conns": conns, **users,
           "outsider": _login(client, "fx@voice.example", PASS)}
    for conn in conns.values():
        registry.by_site.pop(conn.site_id, None)


def test_read_permissions_and_server_status(world):
    url = URL.format(world["yard"]["id"])
    j = world["viewer"].get(url).json()
    assert j["setting"] is None and j["updated_by"] is None and j["can_edit"] is False and j["can_preview"] is False
    assert world["operator"].get(url).json()["can_preview"] is True and world["operator"].get(url).json()["can_edit"] is False
    assert world["admin"].get(url).json()["can_edit"] is True
    by = {s["server_name"]: s for s in j["servers"]}
    assert list(by) == ["Alpha", "Bravo", "Charlie (old)", "Delta (offline)"]   # by name; another Site's server isn't here
    assert by["Alpha"]["supported"] and by["Alpha"]["online"] and by["Alpha"]["voices"] == ["en_US-amy-medium", "en_GB-alan-medium"]
    assert by["Bravo"]["engines"] == {"piper": None, "sapi": True, "espeak": None} and by["Bravo"]["voices"] == []
    assert by["Charlie (old)"]["supported"] is False and by["Charlie (old)"]["setting"] is None
    assert by["Delta (offline)"]["online"] is False and by["Delta (offline)"]["supported"]
    assert not any(s["in_sync"] for s in j["servers"])   # nothing set yet
    assert world["outsider"].get(url).status_code == 403
    assert world["admin"].get(URL.format(world["other"]["location_id"])).status_code == 403   # a Site they aren't granted


def test_set_push_and_audit(world):
    url = URL.format(world["yard"]["id"])
    body = {"voice": "en_US-amy-medium", "speed": 0.9, "chime": True}
    for c in (world["viewer"], world["operator"], world["outsider"]):
        assert c.put(url, json=body).status_code == 403
    assert world["admin"].put(url, json={**body, "voice": "../x"}).status_code == 422
    assert world["admin"].put(url, json={**body, "speed": 2}).status_code == 422
    conns = world["conns"]
    r = world["admin"].put(url, json=body)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["setting"] == body and j["updated_by"] == "admin@voice.example"
    pushed = {p["server_name"]: p for p in j["pushed"]}
    # Alpha took it; Bravo didn't answer; Charlie is too old (nothing sent); Delta is offline; Elsewhere is another Site
    assert pushed["Alpha"]["ok"] and pushed["Alpha"]["effective"]["voice"] == "en_US-amy-medium"
    assert not pushed["Bravo"]["ok"] and "did not answer" in pushed["Bravo"]["error"]
    assert pushed["Charlie (old)"]["skipped"] and not conns["c"].calls and not conns["other"].calls
    assert "Delta (offline)" not in pushed
    method, path, headers, sent = conns["a"].calls[-1]
    assert (method, path, sent) == ("PUT", "/api/talkdown/voice", body)
    assert headers["x-hub-role"] == "admin" and headers["x-hub-user"] == "admin@voice.example"
    # Alpha's status shows it at once (its stored summary is updated); Bravo is behind
    st = {s["server_name"]: s for s in j["servers"]}
    assert st["Alpha"]["in_sync"] and st["Alpha"]["setting"] == body and not st["Bravo"]["in_sync"]
    assert world["viewer"].get(url).json()["setting"] == body
    rows = db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id == world["oid"], db.audit_log.c.action.like("talk-down voice%")))
    assert [x["action"] for x in rows] == ["talk-down voice set: en_US-amy-medium (Voice Yard)"]
    d = rows[0]["detail"]
    assert d["pushed"] == [world["a"]["id"]] and d["failed"] == [world["b"]["id"]] and d["before"] is None and d["speed"] == 0.9
    # the same value again: pushed again (a retry), not audited again
    assert world["admin"].put(url, json=body).status_code == 200
    assert len(db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id == world["oid"], db.audit_log.c.action.like("talk-down voice%")))) == 1
    # a hub administrator may set it too; automatic is a voice of null
    r = world["root"].put(url, json={"voice": None, "speed": 1.0, "chime": False})
    assert r.status_code == 200 and r.json()["setting"] == {"voice": None, "speed": 1.0, "chime": False}
    world["admin"].put(url, json=body)


def _beat(conn, summary: dict) -> None:
    """What agents._loop does on a heartbeat (the talk-down part), then waits for the push it started."""
    async def go():
        conn.summary = summary
        agents._talkdown_voice(conn)
        if talkdown_voice._tasks:
            await asyncio.gather(*list(talkdown_voice._tasks))
    asyncio.run(go())


def test_heartbeat_repush(world, monkeypatch):
    site = {"voice": "en_US-amy-medium", "speed": 0.9, "chime": True}
    assert talkdown_voice.get(world["yard"]["id"])["voice"] == "en_US-amy-medium"
    b = VoiceConn(world["b"])   # Bravo answers again
    registry.by_site[b.site_id] = b
    world["conns"]["b"] = b
    talkdown_voice._last.pop(b.site_id, None)
    # it reports another voice: the Site's is pushed (as "hub")
    _beat(b, {"talkdown_voice": _tv()})
    assert len(b.calls) == 1 and b.calls[0][3] == site and b.calls[0][2]["x-hub-user"] == "hub"
    # still the old value on the next heartbeat (it hasn't applied yet): not again within REPUSH_S
    _beat(b, {"talkdown_voice": _tv()})
    assert len(b.calls) == 1
    # ...but after REPUSH_S it is
    t0 = talkdown_voice._last[b.site_id][1]
    monkeypatch.setattr(talkdown_voice.time, "time", lambda: t0 + talkdown_voice.REPUSH_S + 1)
    _beat(b, {"talkdown_voice": _tv()})
    assert len(b.calls) == 2
    monkeypatch.undo()
    # in step: nothing to do (speed compared rounded, the default fields filled in)
    _beat(b, {"talkdown_voice": _tv(setting={"voice": "en_US-amy-medium", "speed": 0.900001, "chime": True})})
    assert len(b.calls) == 2
    # the Site changes its voice: the PUT pushes it to Bravo directly; a stale heartbeat from before the change doesn't
    # push the value just pushed again within REPUSH_S
    world["admin"].put(URL.format(world["yard"]["id"]), json={**site, "speed": 1.1})
    n = len(b.calls)
    assert b.calls[-1][3]["speed"] == 1.1
    _beat(b, {"talkdown_voice": _tv(setting=site)})
    assert len(b.calls) == n
    # a value other than the one last pushed goes at once (e.g. the server moved here from another Site)
    talkdown_voice.put(world["yard"], {**site, "speed": 1.2}, "test")
    _beat(b, {"talkdown_voice": _tv(setting={**site, "speed": 1.1})})
    assert len(b.calls) == n + 1 and b.calls[-1][3]["speed"] == 1.2
    # a server that was offline at the change gets it on its first heartbeat
    d = VoiceConn(world["d"])
    registry.by_site[d.site_id] = d
    world["conns"]["d"] = d
    _beat(d, {"talkdown_voice": _tv(voices=["en_US-amy-medium"])})
    assert [c[3]["speed"] for c in d.calls] == [1.2]
    # an older server, a minimal heartbeat, a Site that never set a voice: nothing is pushed
    c = world["conns"]["c"]
    _beat(c, {"speakers": []})
    assert not c.calls
    other = world["conns"]["other"]
    _beat(other, {"talkdown_voice": _tv()})
    assert not other.calls
    assert talkdown_voice.needs_push(b, None) is False
    world["admin"].put(URL.format(world["yard"]["id"]), json=site)


class PreviewConn(VoiceConn):
    """The proxy side: POST /api/talkdown/preview answers a WAV with the X-TTS-* headers."""

    async def request(self, method, path, query, headers, body):
        from tunnelproto import Stream
        self.calls.append((method, path, headers, json.loads(body) if body else None))
        s = Stream(len(self.calls))
        s.status, s.headers = 200, {"content-type": "audio/wav", "x-tts-engine": "piper", "x-tts-voice": "en_US-amy-medium",
                                    "x-tts-fallback": "0", "cache-control": "no-store", "set-cookie": "x=1"}
        s.head.set()
        s.push(b"RIFF....WAVEfmt ")
        s.push(None)
        return s

    async def send(self, frame):
        pass

    def finish(self, s):
        pass

    async def abort(self, s, reason):
        await s.abort(reason)


def test_preview_through_the_proxy(world):
    """The hub UI's Preview: an operator of the Site gets the server's WAV with who spoke it; a viewer can't."""
    conn = PreviewConn(world["a"])
    registry.by_site[conn.site_id] = conn
    try:
        base = f"/s/{world['a']['id']}/api/talkdown"
        assert world["viewer"].post(f"{base}/preview", json={}).status_code == 403
        assert world["viewer"].get(f"{base}/voices").status_code == 403
        r = world["operator"].post(f"{base}/preview", json={"voice": "en_US-amy-medium", "speed": 1.1})
        assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and r.content.startswith(b"RIFF")
        assert (r.headers["x-tts-engine"], r.headers["x-tts-voice"], r.headers["x-tts-fallback"]) == ("piper", "en_US-amy-medium", "0")
        assert "set-cookie" not in r.headers
        method, path, headers, body = conn.calls[-1]
        assert (method, path, body, headers["x-hub-role"]) == ("POST", "/api/talkdown/preview", {"voice": "en_US-amy-medium", "speed": 1.1}, "operator")
        assert world["operator"].put(f"{base}/voice", json={"voice": None}).status_code == 403   # setting it: admin
    finally:
        registry.by_site[conn.site_id] = world["conns"]["a"]


def test_clean_and_key():
    assert talkdown_voice.clean({"voice": "a b", "speed": "x", "chime": 1}) == {"voice": None, "speed": 1.0, "chime": True}
    assert talkdown_voice.clean({"speed": 0.1}) == {"voice": None, "speed": 0.7, "chime": False}
    assert talkdown_voice.clean(None) == {"voice": None, "speed": 1.0, "chime": False}
    assert talkdown_voice.key({"voice": "v", "speed": 1.004}) == ("v", 1.0, False)


def test_site_delete_drops_the_voice(world):
    root, oid = world["root"], world["oid"]
    gone = root.post(f"/api/orgs/{oid}/locations", json={"name": "Short voice"}).json()
    assert root.put(URL.format(gone["id"]), json={"voice": "en_GB-alan-medium", "speed": 1.0, "chime": False}).status_code == 200
    assert root.put(URL.format(gone["id"]), json={"voice": "en_GB-alan-medium"}).json()["pushed"] == []   # no servers
    assert root.delete(f"/api/locations/{gone['id']}").status_code == 200
    assert not db.rows(sa.select(db.site_talkdown_voice).where(db.site_talkdown_voice.c.location_id == gone["id"]))
