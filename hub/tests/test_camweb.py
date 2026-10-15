"""Camera web pages (camweb.py): who may open one, the one-time link, the camera name's own cookie and its session
(expiry, extension, close), what the camera's answer may carry, the hub cookie never reaching a camera, camera names
never reaching the hub, Caddy's on-demand TLS ask, the audit trail, and forwarding over a (fake) tunnel, WebSockets
included."""
import asyncio
import json
import time

import sqlalchemy as sa
from fastapi.testclient import TestClient
from tunnelproto import Stream

from hub import agents, auth, camweb, cameras, db
from hub.agents import registry
from hub.config import settings
from test_access import _login, server
from test_soc_roles import PW, soc_setup


class CamConn:
    """A server answering /api/camweb/<camera> like backend/nvr/camweb.py: echoes what it was sent; some paths answer
    a redirect with cookies, nothing (a hung camera) or the server's own error. WebSockets echo."""

    def __init__(self, s: dict, location: dict | None = None, caps=("events", "camweb")):
        self.site_id, self.site, self.last_seen = s["id"], s, 1e12
        self.location = location
        self.subscribers: set = set()
        self.hello = {"caps": list(caps)}
        self.opened: list[dict] = []
        self.bodies: dict[int, bytearray] = {}
        self.streams: dict[int, tuple[Stream, dict]] = {}
        self.ws_in: list[bytes] = []
        self.aborted: list[str] = []
        self.asm: dict[int, agents.WsAssembler] = {}
        self.n = 0

    def caps(self):
        return list(self.hello["caps"])

    async def open(self, method, path, query, headers, body=True, ws=False):
        self.n += 1
        s = Stream(self.n * 2 + 1)
        req = {"method": method, "path": path, "query": query, "headers": dict(headers), "body": body, "ws": ws}
        self.opened.append(req)
        self.streams[s.id] = (s, req)
        if ws:
            s.status, s.headers = 101, ({"sec-websocket-protocol": headers["sec-websocket-protocol"].split(",")[0].strip()}
                                        if headers.get("sec-websocket-protocol") else {})
            s.head.set()
        elif not body:
            self._answer(s, req, b"")
        return s

    async def send_body(self, s, data):
        self.bodies.setdefault(s.id, bytearray()).extend(data)

    async def send(self, frame):
        if frame.get("t") == "end" and frame["id"] in self.streams:
            s, req = self.streams[frame["id"]]
            if not req["ws"]:
                self._answer(s, req, bytes(self.bodies.get(s.id, b"")))

    async def send_bytes(self, data):
        sid, payload = int.from_bytes(data[:4], "big"), data[4:]
        self.ws_in.append(payload)
        s, _ = self.streams[sid]
        got = self.asm.setdefault(sid, agents.WsAssembler()).feed(payload)
        if got:   # echo, framed like the server does
            binary, msg = got
            for piece in agents.ws_pieces(b"echo:" + msg if binary else "echo:" + msg.decode()):
                s.push(piece)

    def finish(self, s):
        pass

    async def abort(self, s, reason):
        self.aborted.append(reason)
        await s.abort(reason)

    def _answer(self, s, req, body: bytes):
        path = req["headers"].get("x-camweb-path")
        if path == "/hang":
            return
        if path == "/login":
            s.status, s.headers = 302, {"location": "/doc/page/main.asp", "content-type": "text/html",
                                        "x-camweb-cookies": json.dumps(["SID=abc; Path=/; Domain=cam.testserver; HttpOnly",
                                                                        "axcam=stolen; Path=/", "hub_session=fixated; Path=/; Domain=testserver",
                                                                        "lang=en"]),
                                        "access-control-allow-origin": "*", "strict-transport-security": "max-age=1; includeSubDomains",
                                        "www-authenticate": 'Digest realm="cam", Basic realm="cam"'}
            out = b""
        elif path == "/unreachable":
            s.status, s.headers = 502, {"content-type": "text/plain", "x-camweb-error": "1"}
            out = b"the camera did not answer (ConnectError)"
        else:
            s.status, s.headers = 200, {"content-type": "text/html; charset=utf-8", "x-frame-options": "DENY"}
            out = json.dumps({**{k: req[k] for k in ("method", "path", "query")}, "headers": req["headers"], "body": body.decode()}).encode()
        s.head.set()
        s.push(out)
        s.push(None)


def _setup(client, superuser, slug: str, caps=("events", "camweb")):
    """A customer with a Site, its server (connected through a CamConn) with two cameras, and members of each role."""
    root = _login(client, superuser["email"], superuser["password"])
    oid = root.post("/api/orgs", json={"name": f"{slug} Co", "slug": slug}).json()["id"]
    yard = root.post(f"/api/orgs/{oid}/locations", json={"name": f"{slug} Yard"}).json()
    srv = server(oid, f"{slug} NVR", yard["id"])
    cameras.sync(srv, [{"id": "cam1", "name": "Gate"}, {"id": "cam2", "name": "Dock"}])
    conn = CamConn(srv, {"id": yard["id"], "name": yard["name"]}, caps)
    registry.by_site[srv["id"]] = conn
    users = {"root": root}
    for role in ("viewer", "operator", "admin", "owner"):
        email = f"{role}@{slug}.example"
        r = root.post(f"/api/orgs/{oid}/members", json={"email": email, "role": role, "password": "cam-pass-12345", "all_sites": True})
        assert r.status_code == 200, r.text
        users[role] = _login(client, email, "cam-pass-12345")
    return {"org": oid, "yard": yard, "srv": srv, "conn": conn, **users}


def _open(c, srv, cam="cam1"):
    return c.post(f"/api/servers/{srv['id']}/cameras/{cam}/web-session")


def _browser(url: str) -> tuple[TestClient, str]:
    """A browser tab on the camera's name, and the enter path of the link."""
    origin, _, rest = url.partition("/__axiom/")
    return TestClient(client_app(), base_url=origin), "/__axiom/" + rest


def client_app():
    from hub.api import app
    return app


def _enter(url: str) -> TestClient:
    tab, path = _browser(url)
    r = tab.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/", r.text
    return tab


def _audit(server_id, prefix):
    return [r for r in db.rows(sa.select(db.audit_log).where(db.audit_log.c.site_id == server_id).order_by(db.audit_log.c.id))
            if r["action"].startswith(prefix)]


def test_who_may_open(client, superuser):
    w = _setup(client, superuser, "cw-perm")
    srv = w["srv"]
    try:
        assert _open(w["viewer"], srv).status_code == 403
        assert _open(w["operator"], srv).status_code == 403
        for who in ("admin", "owner", "root"):
            r = _open(w[who], srv)
            assert r.status_code == 200, (who, r.text)
            body = r.json()
            label = camweb.label_for(srv["id"], "cam1")
            assert body["host"] == f"{label}.cam.testserver" and body["url"].startswith(f"http://{label}.cam.testserver/__axiom/enter?k=")
        assert _open(w["admin"], srv, "nope").status_code == 404
        # another customer's admin
        other = _setup(client, superuser, "cw-perm-other")
        try:
            assert _open(other["admin"], srv).status_code == 403
        finally:
            registry.by_site.pop(other["srv"]["id"], None)
        # an admin restricted to another Site of the customer
        root = w["root"]
        far = root.post(f"/api/orgs/{w['org']}/locations", json={"name": "cw-perm Far"}).json()
        r = root.post(f"/api/orgs/{w['org']}/members", json={"email": "far@cw-perm.example", "role": "admin", "password": "cam-pass-12345",
                                                             "all_sites": False, "location_ids": [far["id"]]})
        assert r.status_code == 200, r.text
        assert _open(_login(client, "far@cw-perm.example", "cam-pass-12345"), srv).status_code == 403
        # the label is stable and differs per camera
        assert camweb.label_for(srv["id"], "cam1") == camweb.label_for(srv["id"], "cam1") != camweb.label_for(srv["id"], "cam2")
        # offline, or a server too old to pass camera pages
        registry.by_site.pop(srv["id"])
        assert _open(w["admin"], srv).status_code == 503
        registry.by_site[srv["id"]] = CamConn(srv, caps=("events",))
        assert _open(w["admin"], srv).status_code == 409
    finally:
        registry.by_site.pop(srv["id"], None)


def test_soc_staff_are_not_site_admins(client, superuser):
    s = soc_setup(client, superuser, "cw-soc")
    srv = s["srv"]
    cameras.sync(srv, [{"id": "cam1", "name": "Gate"}])
    registry.by_site[srv["id"]] = CamConn(srv)
    try:
        assert _open(s["sup"], srv).status_code == 403    # widened to admin for monitoring: doesn't count here
        assert _open(s["op"], srv).status_code == 403
        assert _open(s["view"], srv).status_code == 403
        assert _open(s["adm"], srv).status_code == 200
    finally:
        registry.by_site.pop(srv["id"], None)


def test_one_time_key_and_cookie_per_camera(client, superuser):
    w = _setup(client, superuser, "cw-key")
    srv, conn = w["srv"], w["conn"]
    try:
        url = _open(w["admin"], srv).json()["url"]
        tab, path = _browser(url)
        r = tab.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        sc = r.headers["set-cookie"]
        assert sc.startswith("axcam=") and "HttpOnly" in sc and "SameSite=Lax" in sc and "Path=/" in sc and "domain" not in sc.lower()
        assert tab.get("/").status_code == 200
        # the key works once
        again, _ = _browser(url)
        assert again.get(path, follow_redirects=False).status_code == 403
        assert again.get("/").status_code == 403 and "open it again from the hub" in again.get("/").text
        # ... and only for a minute
        url2 = _open(w["admin"], srv, "cam2").json()["url"]
        db.run(sa.update(db.cam_web_sessions).where(db.cam_web_sessions.c.camera_id == "cam2", db.cam_web_sessions.c.closed_at.is_(None))
               .values(key_expires_at=time.time() - 1))
        late, p2 = _browser(url2)
        assert late.get(p2, follow_redirects=False).status_code == 403
        # camera 1's key on camera 2's name: refused, and camera 1's session cookie doesn't open camera 2
        url3 = _open(w["admin"], srv, "cam2").json()["url"]
        cam2_host = url3.split("//")[1].split("/")[0]
        wrong = TestClient(client_app(), base_url=f"http://{cam2_host}")
        url4 = _open(w["admin"], srv).json()["url"]
        assert wrong.get("/__axiom/" + url4.partition("/__axiom/")[2], follow_redirects=False).status_code == 403
        tab1 = _enter(url4)
        token = tab1.cookies.get("axcam")
        assert wrong.get("/", headers={"cookie": f"axcam={token}"}).status_code == 403
        assert tab1.get("/").status_code == 200
        # no cookie at all: the small page, never the camera
        before = len(conn.opened)
        r = TestClient(client_app(), base_url=f"http://{cam2_host}").get("/anything")
        assert r.status_code == 403 and "Session expired" in r.text and len(conn.opened) == before
        assert "script" not in r.headers["content-security-policy"] or "default-src 'none'" in r.headers["content-security-policy"]
    finally:
        registry.by_site.pop(srv["id"], None)


def test_expiry_extension_close_and_audit(client, superuser):
    w = _setup(client, superuser, "cw-life")
    srv = w["srv"]
    t = db.cam_web_sessions
    try:
        url = _open(w["admin"], srv).json()["url"]
        tab = _enter(url)
        sid = db.one(sa.select(t).where(t.c.server_id == srv["id"], t.c.closed_at.is_(None)))["id"]
        # extended on use: a session close to its idle end gets another 30 minutes
        db.run(sa.update(t).where(t.c.id == sid).values(expires_at=time.time() + 120))
        assert tab.get("/").status_code == 200
        assert db.one(sa.select(t).where(t.c.id == sid))["expires_at"] > time.time() + settings.camweb_idle_s - 10
        # ... but never past 2 h after it was opened
        db.run(sa.update(t).where(t.c.id == sid).values(created_at=time.time() - settings.camweb_max_s + 30, expires_at=time.time() + 120))
        assert tab.get("/").status_code == 200
        assert w["admin"].get("/api/camweb/sessions").json()[0]["expires_at"] <= time.time() + 31
        assert db.one(sa.select(t).where(t.c.id == sid))["expires_at"] <= time.time() + 31
        db.run(sa.update(t).where(t.c.id == sid).values(created_at=time.time() - settings.camweb_max_s - 1))
        r = tab.get("/")
        assert r.status_code == 403 and "Session expired" in r.text
        row = db.one(sa.select(t).where(t.c.id == sid))
        assert row["closed_at"] and row["close_reason"] == "expired"

        # idle: the sweeper ends it
        url = _open(w["admin"], srv).json()["url"]
        tab = _enter(url)
        sid2 = db.one(sa.select(t).where(t.c.server_id == srv["id"], t.c.closed_at.is_(None)))["id"]
        db.run(sa.update(t).where(t.c.id == sid2).values(expires_at=time.time() - 1))
        assert camweb.sweep() >= 1
        assert db.one(sa.select(t).where(t.c.id == sid2))["close_reason"] == "expired" and tab.get("/").status_code == 403

        # closed from the hub (the list of open pages) ...
        url = _open(w["admin"], srv).json()["url"]
        tab = _enter(url)
        mine = w["admin"].get("/api/camweb/sessions").json()
        assert [m["camera_name"] for m in mine] == ["Gate"] and mine[0]["server_name"] == "cw-life NVR"
        assert w["viewer"].post(f"/api/camweb/sessions/{mine[0]['id']}/close").status_code == 404   # not theirs
        assert w["admin"].post(f"/api/camweb/sessions/{mine[0]['id']}/close").status_code == 200
        assert tab.get("/").status_code == 403 and w["admin"].get("/api/camweb/sessions").json() == []
        # ... or from the camera's name
        tab = _enter(_open(w["admin"], srv).json()["url"])
        r = tab.get("/__axiom/close")
        assert r.status_code == 200 and "Max-Age=0" in r.headers["set-cookie"]
        assert tab.get("/").status_code == 403
        # opening the same camera again replaces the open session
        a = _open(w["admin"], srv).json()["id"]
        b = _open(w["admin"], srv).json()["id"]
        assert db.one(sa.select(t).where(t.c.id == a))["close_reason"] == "replaced" and db.one(sa.select(t).where(t.c.id == b))["closed_at"] is None

        rows = _audit(srv["id"], "camera web page")
        kinds = [r["action"].split(":")[0] for r in rows]
        assert {"camera web page opened", "camera web page entered", "camera web page closed", "camera web page expired"} <= set(kinds)
        entered = next(r for r in rows if r["action"].startswith("camera web page entered"))
        assert entered["user_email"] == "admin@cw-life.example" and entered["action"] == "camera web page entered: Gate"
        d = entered["detail"]
        assert d["location_id"] == w["yard"]["id"] and d["camweb"]["camera_id"] == "cam1" and d["camweb"]["location_name"] == "cw-life Yard"
        closed = [r for r in rows if r["action"].startswith("camera web page closed")]
        assert all("duration_s" in r["detail"]["camweb"] for r in closed)
        # the customer's audit log shows them
        assert any(r["action"].startswith("camera web page") for r in w["root"].get(f"/api/audit?org={w['org']}").json())
    finally:
        registry.by_site.pop(srv["id"], None)


def test_revoked_admin_loses_the_page(client, superuser):
    w = _setup(client, superuser, "cw-revoke")
    srv = w["srv"]
    try:
        tab = _enter(_open(w["admin"], srv).json()["url"])
        assert tab.get("/").status_code == 200
        uid = auth.user_by_email("admin@cw-revoke.example")["id"]
        db.run(sa.update(db.memberships).where(db.memberships.c.user_id == uid).values(role="viewer"))
        camweb._checked.clear()   # as if the re-check interval had passed
        r = tab.get("/")
        assert r.status_code == 403 and "no longer manage" in r.text
    finally:
        registry.by_site.pop(srv["id"], None)


def test_forwarding_headers_and_rewriting(client, superuser):
    w = _setup(client, superuser, "cw-fwd")
    srv, conn = w["srv"], w["conn"]
    try:
        tab = _enter(_open(w["admin"], srv).json()["url"])
        host = tab.base_url.host
        # the hub's own cookie (if a browser ever had one here), our session cookie and x-hub-* never reach the server
        tab.cookies.set("hub_session", "hub-secret", domain=host)
        tab.cookies.set("SID", "camera-own", domain=host)
        r = tab.get("/cgi-bin/a%2Fb.cgi?x=1&y=%20", headers={"authorization": "Basic dTpw", "x-hub-user": "evil@example.com",
                                                             "x-forwarded-for": "1.2.3.4", "x-camweb-path": "/etc/passwd"})
        assert r.status_code == 200, r.text
        sent = r.json()
        h = sent["headers"]
        assert sent["path"] == "/api/camweb/cam1" and sent["method"] == "GET" and sent["query"] == "x=1&y=%20"
        assert h["x-camweb-path"] == "/cgi-bin/a%2Fb.cgi"     # the raw path, as the browser sent it
        assert h["cookie"] == "SID=camera-own" and "hub-secret" not in json.dumps(h) and "axcam" not in h["cookie"]
        assert h["authorization"] == "Basic dTpw"             # the camera's own login
        assert h["x-hub-internal"] == "camweb" and h["x-hub-user"] == "admin@cw-fwd.example" and h["x-hub-site"] == srv["id"]
        assert h["x-camweb-host"] == f"http://{host}" and "x-forwarded-for" not in h and "host" not in h
        # the camera's own headers mostly pass (it may forbid framing); the hub's CSP doesn't apply here
        assert r.headers["x-frame-options"] == "DENY" and "content-security-policy" not in r.headers

        r = tab.get("/login", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/doc/page/main.asp"
        cookies = r.headers.get_list("set-cookie")
        assert cookies == ["SID=abc; Path=/; HttpOnly", "lang=en"]   # Domain dropped; ours and the hub's names refused
        assert "access-control-allow-origin" not in r.headers and "strict-transport-security" not in r.headers
        assert r.headers["www-authenticate"] == 'Digest realm="cam", Basic realm="cam"'
        assert camweb.rewrite_set_cookie("a=1; Domain=.axiomvision.ai; SameSite=None", True) == "a=1; SameSite=None; Secure"
        assert camweb.rewrite_set_cookie("__Host-axcam=x; Path=/", True) is None
        assert camweb.rewrite_set_cookie("bad\r\nheader=1", True) is None

        # the server can't reach the camera: a page of ours, with its reason
        r = tab.get("/unreachable")
        assert r.status_code == 502 and "ConnectError" in r.text and "text/html" in r.headers["content-type"]

        # an upload streams down (and Content-Length passes for cameras that can't take chunked bodies)
        r = tab.post("/upload.cgi", content=b"x" * 200_000, headers={"content-type": "application/octet-stream"})
        assert r.status_code == 200 and len(r.json()["body"]) == 200_000 and r.json()["headers"]["content-length"] == "200000"
        settings.camweb_max_body_mb, old = 0, settings.camweb_max_body_mb
        try:
            assert tab.post("/upload.cgi", content=b"x" * 10).status_code == 413
        finally:
            settings.camweb_max_body_mb = old
        # another page on a "same-site" name can't make changes here
        r = tab.post("/set.cgi", content=b"a=1", headers={"origin": "http://cother1234567.cam.testserver"})
        assert r.status_code == 403
        assert tab.post("/set.cgi", content=b"a=1", headers={"origin": f"http://{host}"}).status_code == 200
        # a hung camera: 504 after the first-byte timeout
        settings.camweb_first_byte_s, old = 0.3, settings.camweb_first_byte_s
        try:
            assert tab.get("/hang").status_code == 504 and "timeout" in conn.aborted
        finally:
            settings.camweb_first_byte_s = old
    finally:
        registry.by_site.pop(srv["id"], None)


def test_camera_names_never_reach_the_hub(client, superuser):
    w = _setup(client, superuser, "cw-iso")
    srv = w["srv"]
    try:
        label = camweb.label_for(srv["id"], "cam1")
        tab = TestClient(client_app(), base_url=f"http://{label}.cam.testserver")
        tab.cookies.set("hub_session", "x", domain=f"{label}.cam.testserver")
        for path in ("/api/orgs", "/auth/me", "/s/" + srv["id"] + "/api/system", "/", "/login"):
            r = tab.get(path)
            assert r.status_code == 403 and "Session expired" in r.text, path   # camweb's page, not the hub's route
        assert TestClient(client_app(), base_url="http://cam.testserver").get("/api/orgs").status_code == 403
        # a camera page's script calling the hub with the user's hub session
        r = w["admin"].get("/auth/me", headers={"origin": f"http://{label}.cam.testserver"})
        assert r.status_code == 403
        r = w["admin"].post(f"/api/servers/{srv['id']}/cameras/cam1/web-session", headers={"origin": f"http://{label}.cam.testserver"})
        assert r.status_code == 403
        assert w["admin"].get("/auth/me").status_code == 200
        # the hub's own cookie is host-only: no Domain attribute, so a browser never sends it to camera names
        r = TestClient(client_app(), base_url="http://testserver").post("/auth/login", json={"email": "admin@cw-iso.example", "password": "cam-pass-12345"})
        assert "domain=" not in r.headers["set-cookie"].lower()
    finally:
        registry.by_site.pop(srv["id"], None)


def test_tls_ok(client, superuser):
    w = _setup(client, superuser, "cw-tls")
    srv = w["srv"]
    try:
        label = camweb.label_for(srv["id"], "cam2")
        camweb._labels.clear()
        camweb._labels_at = 0
        assert client.get(f"/api/camweb/tls-ok?domain={label}.cam.testserver").status_code == 200   # rebuilt from the registry
        assert client.get("/api/camweb/tls-ok?domain=cabcdefghijkl.cam.testserver").status_code == 404
        assert client.get("/api/camweb/tls-ok?domain=testserver").status_code == 404
        assert client.get(f"/api/camweb/tls-ok?domain={label}.evil.example").status_code == 404
        assert client.get("/api/camweb/tls-ok").status_code == 404
        db.run(sa.update(db.sites).where(db.sites.c.id == srv["id"]).values(retired_at=time.time()))
        assert client.get(f"/api/camweb/tls-ok?domain={label}.cam.testserver").status_code == 404
    finally:
        registry.by_site.pop(srv["id"], None)


def test_websocket_passthrough(client, superuser):
    w = _setup(client, superuser, "cw-ws")
    srv, conn = w["srv"], w["conn"]
    try:
        tab = _enter(_open(w["admin"], srv).json()["url"])
        host = tab.base_url.host
        origin = {"origin": f"http://{host}"}
        with tab.websocket_connect(f"ws://{host}/ws/live?ch=1", headers={**origin, "sec-websocket-protocol": "jsonrpc"}) as ws:
            ws.send_text("hello")
            assert ws.receive_text() == "echo:hello"
            big = bytes(range(256)) * 400
            ws.send_bytes(big)
            assert ws.receive_bytes() == b"echo:" + big
        req = conn.opened[-1]
        assert req["ws"] and req["path"] == "/api/camweb/cam1" and req["query"] == "ch=1"
        assert req["headers"]["x-camweb-path"] == "/ws/live" and req["headers"]["sec-websocket-protocol"] == "jsonrpc"
        assert "sec-websocket-key" not in req["headers"] and "axcam" not in req["headers"].get("cookie", "")
        # another origin, or no session: refused at the handshake
        for headers, c in ((({"origin": "http://evil.example"}), tab), (origin, TestClient(client_app(), base_url=f"http://{host}"))):
            try:
                with c.websocket_connect(f"ws://{host}/ws/live", headers=headers):
                    raise AssertionError("should have been refused")
            except Exception as e:
                assert getattr(e, "code", None) in (4403, 4401), repr(e)
    finally:
        registry.by_site.pop(srv["id"], None)


def test_helpers():
    assert camweb.label_of("cabcdefghijkl.cam.testserver") == "cabcdefghijkl"
    assert camweb.label_of("CABCDEFGHIJKL.cam.testserver:443") == "cabcdefghijkl"
    assert camweb.label_of("x.cabcdefghijkl.cam.testserver") == ""
    assert camweb.label_of("evil.cam.testserver") == ""
    assert camweb.label_of("testserver") is None and camweb.label_of("cabcdefghijkl.cam.testserver.evil.example") is None
    pieces = agents.ws_pieces(b"z" * 200_000)
    asm = agents.WsAssembler()
    assert [asm.feed(p) for p in pieces][-1] == (True, b"z" * 200_000) and len(pieces) == 4
    assert agents.WsAssembler().feed(agents.ws_pieces("")[0]) == (False, b"")
    settings.camweb_domain = "cam.axiomvision.ai"
    try:
        assert camweb.domain() == "cam.axiomvision.ai"
    finally:
        settings.camweb_domain = ""
    old = settings.public_url
    settings.public_url = "https://hub.axiomvision.ai"
    try:
        assert camweb.domain() == "cam.axiomvision.ai" and camweb.origin_for("cabc").startswith("https://cabc.cam.axiomvision.ai")
    finally:
        settings.public_url = old
    settings.cookie_secure = True
    try:
        c = camweb._session_cookie("tok").decode()
        assert c.startswith("__Host-axcam=tok; Path=/; HttpOnly; SameSite=Lax; Secure") and "Domain" not in c
    finally:
        settings.cookie_secure = False
    asyncio.run(asyncio.sleep(0))
