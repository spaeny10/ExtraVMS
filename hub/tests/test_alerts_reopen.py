"""An acknowledged event alert stays closed: the same site event is re-published many times (heartbeat attention
lists, synopsis, feedback, lock), and each used to open (and push) the alert again. Condition alerts (disk, offline)
still re-open when the condition returns. Events marked false alarm, or ranked priority none, raise nothing."""
import time

import sqlalchemy as sa

from hub import alerts, db
from test_access import server


def _rows(site, kind=None):
    q = sa.select(db.alerts).where(db.alerts.c.site_id == site["id"]).order_by(db.alerts.c.id)
    if kind:
        q = q.where(db.alerts.c.kind == kind)
    return db.rows(q)


def _event(eid, **kw):
    return {"type": "event", "event": {"id": eid, "status": "verified", "camera_id": "cam1", "priority": "high",
                                       "camera_class": "person", "start_ts": time.time(), "synopsis": "someone at the gate", **kw}}


def _org(client, superuser, slug):
    from test_access import _login
    root = _login(client, superuser["email"], superuser["password"])
    return root.post("/api/orgs", json={"name": slug, "slug": slug}).json()["id"]


def test_acked_event_alert_does_not_reopen(client, superuser):
    oid = _org(client, superuser, "reopen-co")
    s = server(oid, "Gate")
    opened = []
    prev, alerts.on_open = alerts.on_open, lambda org_id, site, kind, detail: opened.append((kind, detail.get("id")))
    try:
        alerts.on_event(s, _event(101))
        rows = _rows(s)
        assert [(r["kind"], r["key"]) for r in rows] == [("event_high", "101")] and opened == [("event_high", 101)]
        alerts.ack(rows[0]["id"], "u_someone")
        # the synopsis lands, someone locks it, the heartbeat lists it as attention: still one closed row, no push
        alerts.on_event(s, _event(101, synopsis="a person in a red coat"))
        alerts.on_event(s, _event(101, locked=True))
        alerts.on_heartbeat(s, {"attention": [{"id": 101, "camera_id": "cam1", "priority": "high", "label": "person"}]}, 0)
        rows = _rows(s)
        assert len(rows) == 1 and rows[0]["closed_at"] is not None and opened == [("event_high", 101)]
        # an open one isn't duplicated either
        alerts.on_event(s, _event(102))
        alerts.on_event(s, _event(102))
        assert len(_rows(s)) == 2 and len(opened) == 2
        # past the TTL the old row no longer counts (the sweeper has long since expired it)
        db.run(sa.update(db.alerts).where(db.alerts.c.key == "101", db.alerts.c.site_id == s["id"])
               .values(opened_at=time.time() - alerts.EVENT_TTL_S - 5))
        alerts.on_event(s, _event(101))
        assert [r["key"] for r in _rows(s)].count("101") == 2
    finally:
        alerts.on_open = prev


def test_condition_alerts_still_reopen(client, superuser):
    oid = _org(client, superuser, "reopen-cond")
    s = server(oid, "Disk box")
    assert alerts.open(s, "disk", "", {"message": "low"}) is True
    assert alerts.open(s, "disk", "", {"message": "low"}) is False   # one open row
    alerts.close(s, "disk")
    assert alerts.open(s, "disk", "", {"message": "low again"}) is True
    assert len(_rows(s, "disk")) == 2


def test_false_alarm_and_priority_none_raise_nothing(client, superuser):
    oid = _org(client, superuser, "reopen-fa")
    s = server(oid, "Yard")
    alerts.on_event(s, _event(201, feedback={"verdict": "false_alarm"}))
    alerts.on_event(s, _event(202, feedback='{"verdict": "false_alarm"}'))   # stored JSON text
    alerts.on_event(s, _event(203, priority="none", policy={"text": "no one in the yard after 22:00"}))
    alerts.on_heartbeat(s, {"attention": [{"id": 204, "camera_id": "cam1", "priority": "none", "watched": "Bob"}]}, 0)
    assert _rows(s) == []
    # a correct verdict (or none) still alerts
    alerts.on_event(s, _event(205, feedback={"verdict": "correct"}))
    alerts.on_event(s, _event(206, priority="low", policy={"text": "no one in the yard after 22:00"}))
    assert {(r["kind"], r["key"]) for r in _rows(s)} == {("event_high", "205"), ("event_policy", "206")}


def test_detector_health_alerts_follow_the_heartbeat(client, superuser):
    """A server whose Hailo is gone (YOLO on the CPU) or whose verify queue stopped moving: one open alert per kind
    while the heartbeat lists it, closed when it no longer does."""
    oid = _org(client, superuser, "detector-co")
    s = server(oid, "Hailo box")
    fb = {"kind": "detector_fallback", "text": "Hailo accelerator not found; detection is running on the CPU",
          "since": 1000.0, "error": "HAILO_OUT_OF_PHYSICAL_DEVICES(74)"}
    st = {"kind": "detector_stalled", "text": "Event verification has stalled: 2883 events waiting", "since": 900.0, "queue": 2883}
    alerts.on_heartbeat(s, {"health_alerts": [fb, st, {"kind": "something_else"}]}, 0)
    alerts.on_heartbeat(s, {"health_alerts": [fb, st]}, 0)
    rows = [r for r in _rows(s) if r["closed_at"] is None]
    assert sorted(r["kind"] for r in rows) == ["detector_fallback", "detector_stalled"]
    d = {r["kind"]: r["detail"] for r in rows}
    assert d["detector_fallback"]["text"] == fb["text"] and d["detector_fallback"]["error"] == fb["error"]
    assert d["detector_stalled"]["text"].startswith("Event verification has stalled") and d["detector_stalled"]["queue"] == 2883
    alerts.on_heartbeat(s, {"health_alerts": [fb]}, 0)                 # the queue moves again
    assert [r["kind"] for r in _rows(s) if r["closed_at"] is None] == ["detector_fallback"]
    alerts.on_heartbeat(s, {}, 0)                                      # the Hailo is back (or an older server)
    assert [r for r in _rows(s) if r["closed_at"] is None] == []
    assert "detector_fallback" in alerts.KINDS and "detector_stalled" in alerts.KINDS
    assert not set(alerts.HEALTH_KINDS) & set(alerts.EVENT_KINDS)
