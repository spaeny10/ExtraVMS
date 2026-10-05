"""soc.armed_now / next_change are pure functions of a Site row and a time: weekly windows in the Site's timezone
(overnight ones spill into the next day), holidays, the manual override and its expiry, unmonitored Sites and the
"no schedule = armed around the clock" rule. Week of 2026-10-05 (Monday) in America/Chicago."""
import datetime as dt
from zoneinfo import ZoneInfo

from hub import soc

CHI = ZoneInfo("America/Chicago")
WEEKNIGHTS = [{"dow": [0, 1, 2, 3, 4], "from": "18:00", "to": "06:00"}]   # Monday..Friday evenings to the next morning


def at(y, m, d, h=0, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=CHI).timestamp()


def site(**kw):
    return {"id": "l_test", "monitored": True, "timezone": "America/Chicago", "arm_schedule": WEEKNIGHTS, "arm_holidays": [],
            "arm_override": None, **kw}


def test_overnight_window_across_midnight():
    loc = site()
    assert soc.armed_now(loc, at(2026, 10, 5, 17, 59)) == (False, "disarmed_schedule")
    assert soc.armed_now(loc, at(2026, 10, 5, 18, 0)) == (True, "schedule")       # from is inclusive
    assert soc.armed_now(loc, at(2026, 10, 5, 23, 30)) == (True, "schedule")
    assert soc.armed_now(loc, at(2026, 10, 6, 3, 0)) == (True, "schedule")        # Monday's window, after midnight
    assert soc.armed_now(loc, at(2026, 10, 6, 6, 0)) == (False, "disarmed_schedule")   # to is exclusive
    assert soc.armed_now(loc, at(2026, 10, 6, 12, 0)) == (False, "disarmed_schedule")
    assert soc.armed_now(loc, at(2026, 10, 10, 3, 0)) == (True, "schedule")       # Saturday morning: Friday's window
    assert soc.armed_now(loc, at(2026, 10, 10, 20, 0)) == (False, "disarmed_schedule")   # Saturday isn't listed
    assert soc.armed_now(loc, at(2026, 10, 11, 3, 0)) == (False, "disarmed_schedule")    # nor is its spill-over
    assert soc.armed_now(loc, at(2026, 10, 5, 3, 0)) == (False, "disarmed_schedule")     # Sunday night: not listed
    # the same instant read in another timezone is a different wall-clock time
    assert soc.armed_now({**loc, "timezone": "UTC"}, at(2026, 10, 5, 12, 0)) == (False, "disarmed_schedule")   # 17:00 UTC
    assert soc.armed_now({**loc, "timezone": "UTC"}, at(2026, 10, 5, 13, 0)) == (True, "schedule")             # 18:00 UTC


def test_same_day_window_and_across_dst():
    loc = site(arm_schedule=[{"dow": [5], "from": "22:00", "to": "02:00"}, {"dow": [6], "from": "09:00", "to": "17:00"}])
    # US daylight saving ends 2026-11-01 02:00: Saturday's window still means 22:00 to 02:00 on the wall clock
    assert soc.armed_now(loc, at(2026, 10, 31, 23, 0)) == (True, "schedule")
    assert soc.armed_now(loc, at(2026, 11, 1, 1, 30)) == (True, "schedule")
    assert soc.armed_now(loc, at(2026, 11, 1, 2, 30)) == (False, "disarmed_schedule")
    assert soc.armed_now(loc, at(2026, 11, 1, 10, 0)) == (True, "schedule")
    assert soc.armed_now(loc, at(2026, 11, 1, 17, 0)) == (False, "disarmed_schedule")
    assert soc.next_change(loc, at(2026, 10, 31, 21, 0)) == {"at": at(2026, 10, 31, 22, 0), "armed": True}


def test_holidays():
    christmas = {"date": "2026-12-25", "name": "Christmas", "armed": True}
    loc = site(arm_holidays=[christmas])
    assert soc.armed_now(loc, at(2026, 12, 25, 12, 0)) == (True, "holiday")     # closed all day: armed all day
    assert soc.armed_now(loc, at(2026, 12, 24, 12, 0)) == (False, "disarmed_schedule")
    off = site(arm_holidays=[{**christmas, "armed": False}])
    assert soc.armed_now(off, at(2026, 12, 25, 23, 0)) == (False, "holiday")    # disarmed beats the weeknight window
    assert soc.armed_now(off, at(2026, 12, 26, 3, 0)) == (True, "schedule")     # the next date is the schedule's again
    part = site(arm_holidays=[{**christmas, "from": "08:00", "to": "20:00"}])
    assert soc.armed_now(part, at(2026, 12, 25, 10, 0)) == (True, "holiday")
    assert soc.armed_now(part, at(2026, 12, 25, 21, 0)) == (False, "holiday")
    # a holiday-only Site (no weekly windows) that is otherwise armed around the clock
    always = site(arm_schedule=[], arm_holidays=[{**christmas, "armed": False}])
    assert soc.armed_now(always, at(2026, 12, 24, 12, 0)) == (True, "always")
    assert soc.next_change(always, at(2026, 12, 20, 12, 0)) == {"at": at(2026, 12, 25), "armed": False}
    assert soc.next_change(always, at(2026, 12, 25, 12, 0)) == {"at": at(2026, 12, 26), "armed": True}


def test_override_and_expiry():
    now = at(2026, 10, 5, 12, 0)   # disarmed by schedule
    loc = site(arm_override={"mode": "arm", "until": now + 3600, "by": "op@example.com", "reason": "contractor on site"})
    assert soc.armed_now(loc, now) == (True, "override")
    assert soc.armed_now(loc, now + 3601) == (False, "disarmed_schedule")    # expired: the schedule again
    assert soc.next_change(loc, now) == {"at": now + 3600, "armed": False}
    # an override beats a holiday too
    dis = site(arm_override={"mode": "disarm", "until": at(2026, 12, 25, 15, 0)}, arm_holidays=[{"date": "2026-12-25", "armed": True}])
    assert soc.armed_now(dis, at(2026, 12, 25, 14, 0)) == (False, "override")
    assert soc.armed_now(dis, at(2026, 12, 25, 16, 0)) == (True, "holiday")


def test_unmonitored_and_always():
    assert soc.armed_now(site(monitored=False), at(2026, 10, 5, 23, 0)) == (False, "unmonitored")
    assert soc.next_change(site(monitored=False), at(2026, 10, 5, 23, 0)) is None
    assert soc.armed_now(site(arm_schedule=[]), at(2026, 10, 5, 12, 0)) == (True, "always")
    assert soc.armed_now(site(arm_schedule=None), at(2026, 10, 5, 12, 0)) == (True, "always")
    assert soc.next_change(site(arm_schedule=[]), at(2026, 10, 5, 12, 0)) is None
    # no timezone (or an unknown one) reads as UTC rather than failing
    assert soc.armed_now(site(timezone=None), at(2026, 10, 5, 13, 0)) == (True, "schedule")
    assert soc.armed_now(site(timezone="Mars/Olympus"), at(2026, 10, 5, 13, 0)) == (True, "schedule")


def test_next_change():
    loc = site()
    assert soc.next_change(loc, at(2026, 10, 5, 12, 0)) == {"at": at(2026, 10, 5, 18, 0), "armed": True}
    assert soc.next_change(loc, at(2026, 10, 5, 23, 0)) == {"at": at(2026, 10, 6, 6, 0), "armed": False}
    # Friday night's window ends Saturday 06:00; the next one starts Monday 18:00
    assert soc.next_change(loc, at(2026, 10, 10, 7, 0)) == {"at": at(2026, 10, 12, 18, 0), "armed": True}
    # back-to-back windows don't count as a change
    joined = site(arm_schedule=[{"dow": [0], "from": "18:00", "to": "00:00"}, {"dow": [1], "from": "00:00", "to": "06:00"}])
    assert soc.next_change(joined, at(2026, 10, 5, 19, 0)) == {"at": at(2026, 10, 6, 6, 0), "armed": False}


def test_cached_armed_now_and_vocabulary():
    soc.invalidate()
    assert soc.armed_now(site(arm_schedule=[])) == (True, "always")
    assert soc.armed_now(site(monitored=False)) == (True, "always")   # cached per Site id for up to 30 s ...
    soc.invalidate()
    assert soc.armed_now(site(monitored=False)) == (False, "unmonitored")   # ... until the config changes
    soc.invalidate()
    assert soc.needs_four_eyes("true_alarm_dispatched", "low") and soc.needs_four_eyes("no_action", "high")
    assert not soc.needs_four_eyes("no_action", "medium") and not soc.needs_four_eyes("false_alarm", "high")
    assert set(soc.sla()) == {"high", "medium", "low"} and soc.sla()["high"]["claim_s"] == 60
