"""Day names and "last week" in questions (assistant.time_window, retrieve.contextualize): "Did the cleaning lady come
on Tuesday?" then "What about last week?" searched Oct 1-8 (a rolling 7 days, this Tuesday included) and answered
with this week's Tuesday again.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_time_window.py   (from backend/)
"""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

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
    for q in ("Did she come Tuesday last week?", "last Thursday", "this Monday", "on Tuesday"):
        w = A.time_window(q, NOW)
        assert A.time_window(f"Did she come {w['label']}?", NOW)["since"] == w["since"], w["label"]


def test_follow_ups():
    hist = [{"role": "user", "content": "Did the cleaning lady come on Tuesday?"}, {"role": "assistant", "content": "Yes."}]
    q = R.contextualize("What about last week?", hist, NOW)
    assert q == "Did the cleaning lady come last week?", q
    assert win(q)[:2] == (day(9, 28), day(10, 5))
    q = R.contextualize("and Friday?", hist, NOW)
    assert win(q)[:2] == (day(10, 2), day(10, 3)), q


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
