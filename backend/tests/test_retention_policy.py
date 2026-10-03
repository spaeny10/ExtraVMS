"""The free-space floor follows the disk: 200 GB on a big recordings disk, 10% of a small one, unless the site set
its own. Run as a script (test.ps1) or under pytest."""
import os
import sys
import tempfile
from collections import namedtuple
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-policy-test-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import keep  # noqa: E402
from nvr.db import db  # noqa: E402

Usage = namedtuple("Usage", "total used free")


def _with_disk(total_gb):
    keep.shutil.disk_usage = lambda path: Usage(int(total_gb * 1e9), 0, int(total_gb * 1e9))


def test_floor_follows_disk_size():
    db.set_setting("retention_policy", {"continuous_days": 5})
    _with_disk(8000)
    assert keep.site_policy()["min_free_gb"] == 200          # big disk: the classic default
    _with_disk(251)
    assert keep.site_policy()["min_free_gb"] == 25           # 250 GB SSD: 10%
    _with_disk(60)
    assert keep.site_policy()["min_free_gb"] == 10           # never below 10 GB
    assert keep.site_policy()["continuous_days"] == 5        # the rest of the site policy is untouched


def test_explicit_floor_wins():
    _with_disk(251)
    db.set_setting("retention_policy", {"min_free_gb": 80})
    assert keep.site_policy()["min_free_gb"] == 80
    db.set_setting("retention_policy", {"continuous_days": 3})


def test_unreadable_disk_falls_back_to_default():
    def boom(path):
        raise OSError("no such disk")
    keep.shutil.disk_usage = boom
    assert keep.default_min_free_gb() == 200


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
