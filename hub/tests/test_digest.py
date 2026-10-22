"""The customer digest (digest.py): grouped by Site with each server under it, "Needs your attention" first, weapons
worded from the second look's verdict (confirmed only with HIGH confidence; a model can't upgrade one), event
references stored with their server, 12-hour times in the Site's timezone, the per-Site PPE option and who may set it.

Servers are faked at the tunnel (registry.by_site -> an object with call()); the shared model is faked too.
Event 22909 (2026-10-21): a medium-confidence "holstered handgun held in a hand" the old digest called "Confirmed"."""
import asyncio
import datetime as dt
import json
import re
import time
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from hub import db, digest, vlm_proxy
from hub.agents import registry
from hub.config import settings
from test_access import _login, server

PW = "digest-pass-1234"
TZ = "America/Chicago"
NOW = dt.datetime(2026, 10, 22, 7, 30, tzinfo=ZoneInfo(TZ)).timestamp()


def at(h, m, day=21):
    return dt.datetime(2026, 10, day, h, m, tzinfo=ZoneInfo(TZ)).timestamp()


def weapon(eid, cam, camera, ts, verdict, conf, obj):
    return {"id": eid, "camera_id": cam, "camera": camera, "label": "person", "start_ts": ts, "priority": "high", "reviewed": False,
            "verdict": verdict, "confirmed": verdict == "confirmed", "needs_review": verdict != "confirmed", "kind": "firearm",
            "confidence": conf, "object": obj, "contradiction": None}


def att(eid, cam, camera, ts, w=None, synopsis=None):
    return {"id": eid, "camera_id": cam, "camera": camera, "label": "person", "start_ts": ts, "priority": "high", "watched": None,
            "policy": None, "weapon": w, "synopsis": None if w else synopsis}


def facts(**kw):
    return {"version": 1, "counts": {"person": 12, "vehicle": 3}, "weapons": [], "attention": [], "unusual": [], "rules": [],
            "ppe": {"count": 0, "zones": [], "missing": {}}, **kw}


HOLSTER = weapon(22909, "door", "South Exterior Door", at(16, 17), "unclear", "medium", "holstered handgun")
PISTOL = weapon(46773, "sw", "SW Corner PTZ", at(16, 19), "confirmed", "high", "black pistol")
# stored "confirmed" by a server still on the old rule (medium): the hub must not call it confirmed either
OLDRULE = weapon(46780, "sw", "SW Corner PTZ", at(17, 2), "confirmed", "medium", "handgun")
LONG = {"id": 501, "camera_id": "yard", "camera": "Side Yard", "label": "person", "start_ts": at(14, 5), "score": 0.97,
        "reasons": ["Stayed 45 min, longer than 100% of visits"], "synopsis": "Two men talked by the fence."}
FENCE = att(19997, "walk", "Warehouse Walk Way", at(15, 8), synopsis="A man climbed the fence by the warehouse.")


class FakeServer:
    def __init__(self, facts=None, briefing=None):
        self.facts, self.briefing, self.asked = facts, briefing, []
        self.site = None   # (registry.refresh_location looks at it)

    async def call(self, method, path, query="", headers=None, body=None, timeout=15.0):
        self.asked.append((path, query))
        if path == digest.FACTS_PATH and self.facts is not None:
            return 200, json.dumps(self.facts).encode()
        if path == "/api/briefings" and self.briefing:
            return 200, json.dumps({"briefings": [self.briefing]}).encode()
        return 404, b'{"detail": "Not Found"}'


@pytest.fixture(scope="module")
def world(client, superuser):
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": "Digest Co", "slug": "digest-co"}).json()["id"]
    whq = root.post(f"/api/orgs/{oid}/locations", json={"name": "World Headquarters", "timezone": TZ}).json()
    dc1 = root.post(f"/api/orgs/{oid}/locations", json={"name": "DC1", "timezone": TZ}).json()
    s = {n: server(oid, n, whq["id"]) for n in ("TSS-DT", "T-304")}
    s |= {n: server(oid, n, dc1["id"]) for n in ("DC1 Server", "DC1-Kanop")}
    q = {"id": db.new_id("s_"), "org_id": oid, "name": "Qwenbot", "location": "", "token_hash": db.token_hash(db.new_token()),
         "created_at": time.time(), "online": False, "location_id": None}   # a server in no Site
    db.insert(db.sites, q)
    s["Qwenbot"] = q
    ids = {}
    for who, role in (("sadm", "admin"), ("view", "viewer"), ("rview", "viewer")):
        ids[who] = root.post(f"/api/orgs/{oid}/members", json={"email": f"{who}@digest.example", "role": role, "password": PW}).json()["id"]
    for who in ("sadm", "rview"):   # only World Headquarters
        assert root.put(f"/api/orgs/{oid}/members/{ids[who]}/access", json={"all_sites": False, "location_ids": [whq["id"]]}).status_code == 200
    return {"root": root, "oid": oid, "whq": whq, "dc1": dc1, "s": s,
            **{who: _login(client, f"{who}@digest.example", PW) for who in ids}}


def servers(world, monkeypatch, **override):
    """The five servers online, answering with their facts (override per server name)."""
    s = world["s"]
    fakes = {
        "TSS-DT": FakeServer(facts(weapons=[HOLSTER], attention=[att(22909, "door", "South Exterior Door", HOLSTER["start_ts"], HOLSTER)],
                                   unusual=[LONG], ppe={"count": 74, "missing": {"hard hat": 60, "hi-vis vest": 30},
                                                        "zones": [{"zone": "East Door", "camera": "East Door", "count": 40, "example": 777},
                                                                  {"zone": "Side Yard", "camera": "Side Yard", "count": 34, "example": 778}]})),
        "T-304": FakeServer(facts(attention=[FENCE])),
        "DC1 Server": FakeServer(facts()),
        "DC1-Kanop": FakeServer(facts()),
        "Qwenbot": FakeServer(facts(weapons=[PISTOL, OLDRULE], attention=[att(46773, "sw", "SW Corner PTZ", PISTOL["start_ts"], PISTOL),
                                                                          att(46780, "sw", "SW Corner PTZ", OLDRULE["start_ts"], OLDRULE)])),
    }
    fakes.update(override)
    for name, f in fakes.items():
        monkeypatch.setitem(registry.by_site, s[name]["id"], f)
    return fakes


def _no_model(monkeypatch):
    monkeypatch.setattr(vlm_proxy, "configured", lambda: False)


def test_grouped_by_site_attention_first(world, monkeypatch):
    servers(world, monkeypatch)
    _no_model(monkeypatch)
    data = asyncio.run(digest.collect(world["oid"], NOW))
    text, refs, sections = digest.compose(data)
    lines = text.splitlines()
    assert lines[0] == "**Needs your attention**"
    q_when = digest.when(OLDRULE["start_ts"], None, NOW)   # (no Site): the hub's own timezone
    # unconfirmed weapons first (newest first), then the confirmed one, then the rest
    assert lines[1] == (f"- (no Site) › Qwenbot · SW Corner PTZ, {q_when}: Possible handgun seen on a second look "
                        "(medium confidence), unconfirmed: needs review [#46780]")
    assert lines[2] == ("- World Headquarters › TSS-DT · South Exterior Door, yesterday 4:17 PM: Possible holstered handgun "
                        "seen on a second look (medium confidence), unconfirmed: needs review [#22909]")
    assert lines[3].startswith("- (no Site) › Qwenbot · SW Corner PTZ, ") and lines[3].endswith(
        ": Black pistol confirmed by a second look (high confidence) [#46773]")
    assert lines[4] == ("- World Headquarters › T-304 · Warehouse Walk Way, yesterday 3:08 PM: A man climbed the fence by the "
                        "warehouse. [#19997]")
    assert len(re.findall(r"confirmed by a second look", text)) == 1           # only the high-confidence one
    # then one section per Site, servers under it; servers without a Site under "(no Site)", last
    order = [text.index(h) for h in ("**DC1**", "**World Headquarters**", "**(no Site)**")]   # Sites by name
    assert order == sorted(order) and order[0] > text.index("[#19997]")
    whq = text[order[1]:order[2]]
    assert "- T-304: 1 alert listed above; 12 person, 3 vehicle." in whq
    assert ("- TSS-DT: 1 alert listed above; unusual at Side Yard, yesterday 2:05 PM: Stayed 45 min, the longest visit on "
            "record [#501]; 12 person, 3 vehicle.") in whq
    assert "- PPE: 74 violations (missing hard hat / hi-vis vest), mostly East Door (40), Side Yard (34)." in whq
    assert text[order[0]:order[1]].strip() == "**DC1**\n- Quiet: DC1 Server, DC1-Kanop"   # quiet servers: one line per Site
    assert "World Headquarters · TSS-DT" not in text and "100%" not in text and "16:17" not in text
    # each Site's own part, for the Site page
    sec = {x["name"]: x for x in sections}
    assert set(sec) == {"World Headquarters", "DC1", "(no Site)"} and sec["(no Site)"]["location_id"] is None
    assert sec["World Headquarters"]["text"].startswith("**Needs your attention**\n- World Headquarters › TSS-DT")
    assert "Qwenbot" not in sec["World Headquarters"]["text"] and "Needs your attention" not in sec["DC1"]["text"]


def test_refs_carry_their_server(world, monkeypatch):
    servers(world, monkeypatch)
    _no_model(monkeypatch)
    text, refs, sections = digest.compose(asyncio.run(digest.collect(world["oid"], NOW)))
    s = world["s"]
    assert [r["event_id"] for r in refs] == [int(n) for n in digest.REF_RE.findall(text)]   # in the text's order
    r = next(r for r in refs if r["event_id"] == 22909)
    assert r == {"server_id": s["TSS-DT"]["id"], "event_id": 22909, "camera_id": "door", "location_id": world["whq"]["id"]}
    assert next(r for r in refs if r["event_id"] == 501)["camera_id"] == "yard"
    q = next(r for r in refs if r["event_id"] == 46773)
    assert q["server_id"] == s["Qwenbot"]["id"] and q["location_id"] is None
    whq = next(x for x in sections if x["location_id"] == world["whq"]["id"])
    assert {r["server_id"] for r in whq["refs"]} == {s["TSS-DT"]["id"], s["T-304"]["id"]}
    assert [r["event_id"] for r in whq["refs"]] == [int(n) for n in digest.REF_RE.findall(whq["text"])]


def test_reviewed_alerts_leave_the_attention_list(world, monkeypatch):
    servers(world, monkeypatch)
    _no_model(monkeypatch)
    t304 = world["s"]["T-304"]
    with db.engine().begin() as c:
        aid = c.execute(db.alerts.insert().values(org_id=world["oid"], site_id=t304["id"], kind="event_high", key="19997",
                                                  opened_at=NOW - 3600, closed_at=NOW - 60, acked_by="u", acked_at=NOW - 60,
                                                  detail={"id": 19997})).inserted_primary_key[0]
    try:
        text, _, _ = digest.compose(asyncio.run(digest.collect(world["oid"], NOW)))
        assert "[#19997]" not in text and "**DC1**" in text
        assert "- Quiet: T-304" in text   # nothing else to say about it now
    finally:
        db.run(sa.delete(db.alerts).where(db.alerts.c.id == aid))


def test_weapon_wording_is_enforced():
    w = {22909: HOLSTER, 46773: PISTOL, 46780: OLDRULE}
    f = digest.enforce_weapon_wording
    assert f("Confirmed holstered handgun observed on a man entering the South Exterior Door [#22909]. 74 people.", w) == (
        "Possible holstered handgun seen on a second look (medium confidence), unconfirmed: needs review [#22909]. 74 people.")
    assert f("Firearm confirmed at the SW Corner PTZ [#46773].", w) == "Firearm confirmed at the SW Corner PTZ [#46773]."
    assert f("A firearm was confirmed [#46780].", w).startswith("Possible handgun seen on a second look (medium confidence)")
    assert f("Three more alerts flagged possible weapons. The yard was quiet.", w) == "The yard was quiet."   # no event: dropped
    assert f("He used a nail gun on the fence.", w) == "He used a nail gun on the fence."
    assert digest.weapon_phrase({"verdict": "error", "kind": "knife"}) == "Possible knife, unconfirmed: needs review"
    assert not digest.weapon_confirmed(OLDRULE) and digest.weapon_confirmed(PISTOL)


def test_times_and_phrasing():
    assert digest.when(at(16, 17), TZ, NOW) == "yesterday 4:17 PM"
    assert digest.when(at(6, 2, day=22), TZ, NOW) == "today 6:02 AM"
    assert digest.when(at(0, 5, day=19), TZ, NOW) == "Oct 19, 12:05 AM"
    assert digest.when(at(12, 0), "Not/AZone", NOW).endswith("M")             # an unknown zone: the hub's own, still 12-hour
    assert digest.twelve_hour("at 16:17 and 9:05 AM, 00:30") == "at 4:17 PM and 9:05 AM, 12:30 AM"
    assert digest.fix_longest("Two men stayed with a duration longer than 100% of previous visits.") == \
        "Two men stayed with the longest visit on record."
    assert digest.fix_longest("Stayed 45 min, longer than 100% of visits") == "Stayed 45 min, the longest visit on record"
    assert digest.fix_longest("Stayed 9 min, longer than 97% of visits") == "Stayed 9 min, longer than 97% of visits"


def test_model_lines_are_checked_and_refs_stored(world, monkeypatch):
    servers(world, monkeypatch)
    asked = []

    async def complete(messages, max_tokens=500, temperature=0.2, schema=None):
        asked.append(messages)
        user = messages[-1]["content"]
        keys = dict((name, key) for key, name in re.findall(r"^(S\d+): server (.+?) at Site", user, re.M))
        lines = {
            "TSS-DT": "Confirmed holstered handgun observed on a man entering the South Exterior Door at 16:17 [#22909]; "
                      "two men stayed with a duration longer than 100% of previous visits [#501].",
            "T-304": "Busy afternoon on the walkway; see [#99999] for the delivery.",   # not one of its events
            "Qwenbot": "Firearm confirmed on a person at the SW Corner PTZ [#46773]. Three more alerts flagged possible weapons.",
        }
        return json.dumps({"servers": [{"key": keys[n], "text": t} for n, t in lines.items() if n in keys]})
    monkeypatch.setattr(vlm_proxy, "configured", lambda: True)
    monkeypatch.setattr(vlm_proxy, "complete", complete)
    row = asyncio.run(digest.generate(world["oid"]))
    text, data = row["text"], row["data"]
    user = asked[0][-1]["content"]
    assert "DC1 Server" not in user and "PPE" not in user                  # quiet servers aren't asked about; PPE isn't the model's
    assert "holstered" not in user and "Weapon" not in user                 # nor are weapons
    assert "Confirmed holstered" not in text and "longer than 100%" not in text and "16:17" not in text
    assert ("- TSS-DT: Possible holstered handgun seen on a second look (medium confidence), unconfirmed: needs review "
            "[#22909]; two men stayed with the longest visit on record [#501]; 1 alert listed above." in text)
    assert "- T-304: Busy afternoon on the walkway; see for the delivery; 1 alert listed above." in text   # [#99999] dropped
    assert "- Qwenbot: Firearm confirmed on a person at the SW Corner PTZ [#46773]; 2 alerts listed above." in text
    assert "Three more alerts" not in text
    assert row["model"] == settings.vllm_model and data["format"] == 2
    assert [r["event_id"] for r in data["refs"]] == [int(n) for n in digest.REF_RE.findall(text)]
    assert all(r["server_id"] for r in data["refs"])
    # what the UI gets back
    got = world["root"].get(f"/api/orgs/{world['oid']}/digests").json()[0]
    assert got["id"] == row["id"] and got["data"]["refs"] == data["refs"] and got["data"]["sections"]


def test_server_without_facts_uses_its_briefing(world, monkeypatch):
    old = FakeServer(briefing={"headline": "Confirmed handgun at the gate.", "text": "Quiet otherwise."})
    servers(world, monkeypatch, **{"DC1-Kanop": old})
    _no_model(monkeypatch)
    text, _, _ = digest.compose(asyncio.run(digest.collect(world["oid"], NOW)))
    assert [p for p, _ in old.asked] == [digest.FACTS_PATH, "/api/briefings"]
    assert "handgun at the gate" not in text and "- DC1-Kanop: 12 person" not in text
    assert "- Quiet: DC1 Server" in text and "- DC1-Kanop: " in text


def test_ppe_option(world, monkeypatch):
    servers(world, monkeypatch)
    _no_model(monkeypatch)
    whq = world["whq"]["id"]
    assert world["root"].get(f"/api/locations/{whq}").json()["digest_ppe"] is True   # on by default
    r = world["sadm"].patch(f"/api/locations/{whq}", json={"digest_ppe": False})
    assert r.status_code == 200 and r.json()["digest_ppe"] is False
    assert db.one(sa.select(db.locations.c.digest_ppe).where(db.locations.c.id == whq))["digest_ppe"] is False
    try:
        data = asyncio.run(digest.collect(world["oid"], NOW))
        text, _, sections = digest.compose(data)
        assert "PPE" not in text and next(g for g in data["locations"] if g["id"] == whq)["digest_ppe"] is False
        # the Site's name and other settings were left alone; null changes nothing
        assert world["sadm"].patch(f"/api/locations/{whq}", json={"digest_ppe": None}).json()["digest_ppe"] is False
    finally:
        assert world["sadm"].patch(f"/api/locations/{whq}", json={"digest_ppe": True}).json()["digest_ppe"] is True
    text, _, _ = digest.compose(asyncio.run(digest.collect(world["oid"], NOW)))
    assert text.count("- PPE: ") == 1


def test_ppe_option_permissions(world):
    whq, dc1 = world["whq"]["id"], world["dc1"]["id"]
    assert world["view"].patch(f"/api/locations/{whq}", json={"digest_ppe": False}).status_code == 403    # viewers: no
    assert world["sadm"].patch(f"/api/locations/{dc1}", json={"digest_ppe": False}).status_code == 403    # not their Site
    assert world["rview"].patch(f"/api/locations/{whq}", json={"digest_ppe": False}).status_code == 403
    assert db.one(sa.select(db.locations.c.digest_ppe).where(db.locations.c.id == dc1))["digest_ppe"] is True
    assert world["root"].patch(f"/api/locations/{dc1}", json={"digest_ppe": False}).status_code == 200    # hub admin: yes
    assert world["root"].patch(f"/api/locations/{dc1}", json={"digest_ppe": True}).json()["digest_ppe"] is True
    assert world["view"].get(f"/api/locations/{whq}").json()["digest_ppe"] is True                      # everyone reads it


def test_scoped_digest_keeps_only_visible_sites(world, monkeypatch):
    servers(world, monkeypatch)
    _no_model(monkeypatch)
    row = asyncio.run(digest.generate(world["oid"]))
    mine = world["rview"].get(f"/api/orgs/{world['oid']}/digests").json()[0]
    assert mine["id"] == row["id"] and mine["scoped"] is True and mine["model"] is None
    assert "Qwenbot" not in mine["text"] and "DC1" not in mine["text"] and "**World Headquarters**" in mine["text"]
    s = world["s"]
    assert {r["server_id"] for r in mine["data"]["refs"]} <= {s["TSS-DT"]["id"], s["T-304"]["id"]}
    assert [x["name"] for x in mine["data"]["sections"]] == ["World Headquarters"]
