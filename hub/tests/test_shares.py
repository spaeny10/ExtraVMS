"""Shared export links (shares.py): made from a finished hub export by an operator, a public page and files at
/x/<token> (no login), an optional passcode with a rate limit, the access log, revoke and expiry deleting the files,
and nothing reachable without the token."""
import json
import time

import sqlalchemy as sa
from fastapi.testclient import TestClient

from hub import db, exports, shares
from hub.config import settings
from test_access import _login, server


def _hub_export(tmp_path, location_id: str, user_id: str) -> exports.HubExport:
    job = exports.HubExport(id="feedfacecafe", user_id=user_id, by="root@example.com", location_id=location_id, site="Share Site",
                            parts=[], start=time.time() - 600, end=time.time() - 300, state="done")
    d = exports.job_dir(job.id)
    d.mkdir(parents=True, exist_ok=True)
    for name, data in (("TSS_cam2.mp4", b"\x00\x00\x00\x18ftypmp42" + b"x" * 5000), ("report.html", b"<html><img src='a.jpg'></html>"),
                       ("checksums.txt", b"abc  TSS_cam2.mp4\n"), ("export_Share-Site.zip", b"PK\x03\x04zip")):
        (d / name).write_bytes(data)
    job.files = [{"name": "TSS_cam2.mp4", "bytes": 5012, "sha256": "abc", "kind": "video"},
                 {"name": "report.html", "bytes": 30, "sha256": "def", "kind": "report"}]
    job.zip_name = "export_Share-Site.zip"
    job.events = [{"id": 7, "server": "TSS", "camera": "Porch", "what": "person", "start_ts": job.start + 30, "end_ts": job.start + 41,
                   "priority": "high", "synopsis": "Two men <b>carry</b> a box off the porch.", "rule": "", "focus": False},
                  {"id": 8, "server": "TSS", "camera": "Gate", "what": "vehicle", "start_ts": job.start + 90, "end_ts": None,
                   "priority": "none", "synopsis": "", "rule": "No hard hat in PPE zone 1", "focus": False}]
    exports._jobs[job.id] = job
    exports._save(job)
    return job


def _wait_ready(c, link_id):
    for _ in range(100):
        st = c.get(f"/api/shares/{link_id}").json()
        if st["state"] != "copying":
            return st
        time.sleep(0.05)
    raise AssertionError("share not ready")


def test_share_link_lifecycle(client, superuser, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "exports_dir", tmp_path)
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": "Share Co", "slug": "share-co"}).json()["id"]
    s = server(oid, "Share Server")
    root.post(f"/api/orgs/{oid}/members", json={"email": "viewer@share.example", "role": "viewer", "password": "viewer-pass-123"})
    viewer = _login(client, "viewer@share.example", "viewer-pass-123")
    job = _hub_export(tmp_path, s["location_id"], superuser["id"])

    # a viewer can't share (someone else's export doesn't even exist for them); an operator (here the owner) can
    body = {"source": {"kind": "hub", "id": job.id}, "days": 7, "passcode": "4321", "note": "For the insurer"}
    assert viewer.post("/api/shares", json=body).status_code == 404
    assert viewer.post("/api/shares", json={"source": {"kind": "server", "id": "abcdefabcdef", "server": s["id"]}, "days": 7}).status_code == 403
    assert root.post("/api/shares", json={**body, "days": 3}).status_code == 422
    r = root.post("/api/shares", json=body)
    assert r.status_code == 200, r.text
    made = r.json()
    url = made["url"]
    assert "/x/" in url and made["expires_at"] > time.time() + 6.9 * 86400 and made["passcode"] is True
    path = url.split("://", 1)[-1].split("/", 1)[1]
    path = "/" + path
    st = _wait_ready(root, made["id"])
    assert st["state"] == "ready" and st["files"] == 2
    row = db.one(sa.select(db.share_links).where(db.share_links.c.id == made["id"]))
    assert row["token_hash"] != path.rsplit("/", 1)[-1]          # only the hash is kept

    anon = TestClient(client.app, base_url="http://testserver")
    # passcode first: the page asks, files refuse
    page = anon.get(path)
    assert page.status_code == 200 and "protected" in page.text and "noindex" in page.headers["x-robots-tag"]
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert anon.get(path + "/f/TSS_cam2.mp4").status_code == 404
    assert anon.post(path, data={"passcode": "0000"}).status_code == 403
    r = anon.post(path, data={"passcode": "4321"}, follow_redirects=False)
    assert r.status_code == 303
    page = anon.get(path)
    assert "For the insurer" in page.text and "TSS_cam2.mp4" in page.text and "Download all" in page.text
    # what happened: Otso's descriptions on the page, escaped, with the camera, priority and a broken rule
    assert "What happened" in page.text and "2 events" in page.text and "TSS · Porch · person" in page.text
    assert "Two men &lt;b&gt;carry&lt;/b&gt; a box off the porch." in page.text and "<b>carry" not in page.text
    assert "high priority" in page.text and "Rule: No hard hat in PPE zone 1" in page.text and "No description." in page.text
    assert anon.get(path + "/f/events.json").status_code == 404          # kept for the page, not a file of the link
    v = anon.get(path + "/f/TSS_cam2.mp4", headers={"range": "bytes=0-99"})
    assert v.status_code == 206 and len(v.content) == 100
    z = anon.get(path + "/f/export_Share-Site.zip?dl=1")
    assert z.status_code == 200 and "attachment" in z.headers["content-disposition"]
    rep = anon.get(path + "/f/report.html")
    assert "sandbox" in rep.headers["content-security-policy"]
    assert anon.get(path + "/f/job.json").status_code == 404           # only the export's files
    assert anon.get(path.replace(path[-6:], "AAAAAA")).status_code == 404

    log = root.get(f"/api/shares/{made['id']}/access").json()
    assert {a["action"] for a in log} >= {"view", "download", "passcode-failed"}
    listed = root.get(f"/api/locations/{s['location_id']}/shares").json()
    assert listed[0]["id"] == made["id"] and listed[0]["views"] >= 1 and listed[0]["downloads"] >= 1 and "token_hash" not in listed[0]
    assert viewer.get(f"/api/locations/{s['location_id']}/shares").status_code == 403

    # revoke: gone at once, files deleted
    assert root.delete(f"/api/shares/{made['id']}").json()["status"] == "revoked"
    assert anon.get(path).status_code == 404 and not shares.link_dir(made["id"]).exists()
    audit = [r["action"] for r in db.rows(sa.select(db.audit_log).where(db.audit_log.c.org_id == oid))]
    assert "share link created" in audit and "share link revoked" in audit

    # expiry: the sweep deletes the files of a link past its time
    r2 = root.post("/api/shares", json={"source": {"kind": "hub", "id": job.id}, "days": 1}).json()
    _wait_ready(root, r2["id"])
    path2 = "/" + r2["url"].split("://", 1)[-1].split("/", 1)[1]
    assert anon.get(path2).status_code == 200                        # no passcode: straight in
    db.run(sa.update(db.share_links).where(db.share_links.c.id == r2["id"]).values(expires_at=time.time() - 1))
    shares.sweep()
    assert anon.get(path2).status_code == 404 and not shares.link_dir(r2["id"]).exists()
    assert root.get(f"/api/shares/{r2['id']}").json()["status"] == "expired"


def test_a_server_export_s_events_are_on_the_hub_clock():
    part = {"server_name": "DC1", "offset": 12.0, "remote": {"events": [{"id": 3, "camera": "Porch", "start_ts": 1012.0, "end_ts": 1020.0, "focus": True}]}}
    assert exports.part_events(part) == [{"id": 3, "camera": "Porch", "start_ts": 1000.0, "end_ts": 1008.0, "focus": False, "server": "DC1"}]
    assert exports.part_events({"server_name": "Old", "offset": 0, "remote": {}}) == []     # a server before events


def test_no_events_no_section():
    assert shares.events_html([], True) == ""
    many = [{"camera": "C", "start_ts": 0, "synopsis": f"s{i}"} for i in range(shares.EVENTS_SHOWN + 3)]
    assert "3 more in the Report" in shares.events_html(many, True)


def test_passcode_tries_are_limited(client, superuser, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "exports_dir", tmp_path)
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": "Share Co 2", "slug": "share-co-2"}).json()["id"]
    s = server(oid, "Share Server 2")
    job = _hub_export(tmp_path, s["location_id"], superuser["id"])
    made = root.post("/api/shares", json={"source": {"kind": "hub", "id": job.id}, "days": 7, "passcode": "secret-code"}).json()
    _wait_ready(root, made["id"])
    path = "/" + made["url"].split("://", 1)[-1].split("/", 1)[1]
    anon = TestClient(client.app, base_url="http://testserver")
    codes = [anon.post(path, data={"passcode": f"wrong{i}"}).status_code for i in range(6)]
    assert codes[:5] == [403] * 5 and codes[5] == 429
    assert anon.post(path, data={"passcode": "secret-code"}).status_code == 429   # even the right one, for a while
