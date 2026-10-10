"""Day names and "last week" in questions (assistant.time_window, retrieve.contextualize): "Did the cleaning lady come
on Tuesday?" then "What about last week?" searched Oct 1-8 (a rolling 7 days, this Tuesday included) and answered
with this week's Tuesday again. Also: "in the last week" stays rolling, "since Monday", "Saturday or Sunday",
"Tuesday night", day names in camera names, the Site's time zone, and follow-ups that keep "last week".

Run: ..\\.venv\\Scripts\\python.exe tests\\test_time_window.py   (from backend/)
"""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

for k in ("NVR_DATA_DIR", "NVR_RECORDINGS_DIR", "NVR_RUNTIME_DIR"):   # never the real server's folders
    os.environ[k] = tempfile.mkdtemp(prefix="nvr-timewindow-test-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import assistant as A  # noqa: E402
from nvr import retrieve as R  # noqa: E402

NOW = dt.datetime(2026, 10, 8, 16, 0).timestamp()   # Thursday


def day(m, d, h=0):
    return dt.datetime(2026, m, d, h).timestamp()


def win(q):
    w = A.time_window(q, NOW)
    return (w["since"], w["until"], w["label"]) if w else None


def test_day_names():
    assert win("Did the cleaning lady come on Tuesday?") == (day(10, 6), day(10, 7), "Tuesday, Oct 6")
    assert win("Did anyone come in on Sunday?") == (day(10, 4), day(10, 5), "Sunday, Oct 4")
    assert win("on Thursday")[0] == day(10, 8)                        # today counts
    assert win("last Thursday")[:2] == (day(10, 1), day(10, 2))       # "last" skips today
    assert win("Did she come Tuesday last week?") == (day(9, 29), day(9, 30), "Tuesday last week, Sep 29")
    assert win("last week's Tuesday")[0] == day(9, 29)
    assert win("this Monday")[0] == day(10, 5)
    assert win("this Friday") is None                                  # later this week


def test_last_week_is_the_calendar_week():
    assert win("What happened last week?") == (day(9, 28), day(10, 5), "last week")
    assert win("previous week")[:2] == (day(9, 28), day(10, 5))
    assert win("past week")[0] == NOW - 7 * 86400                      # "past week" stays rolling
    assert win("last 2 weeks")[0] == NOW - 14 * 86400
    assert win("overnight")[:2] == (day(10, 7, 18), day(10, 8, 7))     # unchanged


def test_labels_read_back_as_the_same_day():
    for q in ("Did she come Tuesday last week?", "last Thursday", "this Monday", "on Tuesday", "Saturday or Sunday",
              "Tuesday night", "since Monday", "in the last week"):
        w = A.time_window(q, NOW)
        back = A.time_window(f"Did she come {w['label']}?", NOW)
        assert (back["since"], back["until"]) == (w["since"], w["until"]), w["label"]
        assert back["text"] == "Did she come", back["text"]          # the label's date is never searched for


def test_in_the_last_week_is_rolling():
    for q in ("Was anyone here in the last week?", "over the last week", "during the past week", "in the previous week"):
        assert win(q) == (NOW - 7 * 86400, None, "past week"), q
    assert win("Was anyone here last week?")[:2] == (day(9, 28), day(10, 5))     # bare "last week": the calendar week


def test_more_day_phrases():
    assert win("Has anyone come since Monday?") == (day(10, 5), None, "since Monday, Oct 5")
    assert win("Was the gate open Saturday or Sunday?")[:2] == (day(10, 3), day(10, 5))
    assert win("on Sunday or Saturday")[:2] == (day(10, 3), day(10, 5))
    assert win("Anyone at the dock Tuesday night?") == (day(10, 6, 18), day(10, 7, 6), "Tuesday night, Oct 6")
    assert win("Did the truck come this Friday?") is None                     # later this week: no window
    assert win("previous Tuesday") == win("last Tuesday")
    assert win("last week on Tuesday")[:2] == win("Tuesday last week")[:2] == (day(9, 29), day(9, 30))
    assert win("Does the van come every Tuesday?") is None                    # not one Tuesday
    assert win("Was the van here every Tuesday since yesterday?")[:2] == (day(10, 7), day(10, 8))


def test_day_names_in_camera_names_are_not_times():
    assert win("Was anyone at the Saturday Market camera?") is None
    assert win("Busy at Saturday Market yesterday?")[:2] == (day(10, 7), day(10, 8))
    w = A.time_window("was the sunday gate busy yesterday?", NOW, cameras=["Sunday Gate"])
    assert (w["since"], w["label"]) == (day(10, 7), "yesterday")
    assert A.time_window("was the sunday gate busy?", NOW, cameras=["Sunday Gate"]) is None
    assert A.time_window("was the sunday gate busy?", NOW, cameras=[])["since"] == day(10, 4)   # no names known: a day


def test_the_sites_time_zone():
    now = dt.datetime(2026, 10, 8, 23, 30, tzinfo=ZoneInfo("America/Chicago")).timestamp()   # Oct 9 04:30 UTC
    chi = A.time_window("yesterday", now, tz="America/Chicago")
    utc = A.time_window("yesterday", now, tz="UTC")
    assert chi["since"] == dt.datetime(2026, 10, 7, tzinfo=ZoneInfo("America/Chicago")).timestamp()
    assert utc["since"] == dt.datetime(2026, 10, 8, tzinfo=dt.timezone.utc).timestamp()
    assert A.time_window("on Tuesday", now, tz="America/Chicago")["label"] == "Tuesday, Oct 6"
    # the Site's zone set for a request (as retrieve does) is the default
    token = A.SITE_TZ.set(ZoneInfo("UTC"))
    try:
        assert A.time_window("yesterday", now)["since"] == utc["since"]
    finally:
        A.SITE_TZ.reset(token)


def test_follow_ups():
    hist = [{"role": "user", "content": "Did the cleaning lady come on Tuesday?"}, {"role": "assistant", "content": "Yes."}]
    q = R.contextualize("What about last week?", hist, NOW)
    assert q == "Did the cleaning lady come last week?", q
    assert win(q)[:2] == (day(9, 28), day(10, 5))
    q = R.contextualize("and Friday?", hist, NOW)
    assert win(q)[:2] == (day(10, 2), day(10, 3)), q
    assert q == "Did the cleaning lady come Friday?", q                  # no date in the text that is searched for
    assert R.contextualize("what about the white van?", hist, NOW) == "the white van Tuesday?"


def test_follow_ups_keep_last_week():
    hist = [{"role": "user", "content": "Did the cleaning lady come Tuesday last week?"}, {"role": "assistant", "content": "Yes."}]
    q = R.contextualize("and Wednesday?", hist, NOW)
    assert q == "Did the cleaning lady come Wednesday last week?", q
    assert win(q)[:2] == (day(9, 30), day(10, 1))                        # that week's Wednesday, not yesterday
    hist = [{"role": "user", "content": "Was the van here last week?"}, {"role": "assistant", "content": "Yes."}]
    assert win(R.contextualize("and Friday?", hist, NOW))[:2] == (day(10, 2), day(10, 3))
    hist = [{"role": "user", "content": "Was the van here this week?"}, {"role": "assistant", "content": "Yes."}]
    assert R.contextualize("and Monday?", hist, NOW) == "Was the van here Monday?"      # "this week" is no "last week"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
