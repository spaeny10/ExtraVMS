"""fMP4 segments with more than one track (cameras with audio): parse per-track timescales and trim with every
track's decode time rebased in its own clock. The fixture is a 6 s two-track fragmented MP4 made with ffmpeg
(video timescale 10240, audio 8000). Run: ..\\.venv\\Scripts\\python.exe tests\\test_fmp4.py  (from backend/)"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["NVR_DATA_DIR"] = tempfile.mkdtemp(prefix="nvr-fmp4-test-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import fmp4  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "av_2track.mp4"


def test_parse_knows_every_track():
    s = fmp4.parse(FIX)
    assert s.timescales == {1: 10240, 2: 8000}, s.timescales
    assert s.timescale == 10240                      # the video track leads
    assert len(s.fragments) >= 5 and s.fragments[0].keyframe
    assert all(len(f.tfdts) == 2 for f in s.fragments)   # one tfdt per track in every moof
    assert 5.0 <= s.duration <= 6.5


def test_trim_rebases_both_tracks_from_zero():
    s = fmp4.parse(FIX)
    out = fmp4.trim(s, 1000.0, [(1002.0, 1004.5)], Path(tempfile.mkdtemp()))
    assert len(out) == 1
    path, start, end = out[0]
    assert 1000.5 <= start <= 1002.5 and end > start + 1.5
    t = fmp4.parse(path)
    assert t.timescales == s.timescales and t.fragments and t.fragments[0].t == 0.0
    # every track's first decode time is at (or within one audio frame of) zero and nothing wrapped around
    # (the old bug packed a negative into an unsigned field)
    data = path.read_bytes()
    for pos, v1, track in t.fragments[0].tfdts:
        val = struct.unpack(">Q" if v1 else ">I", data[pos:pos + (8 if v1 else 4)])[0]
        assert 0 <= val <= 0.1 * t.timescales[track], (track, val)
    for f in t.fragments[1:]:
        for pos, v1, track in f.tfdts:
            val = struct.unpack(">Q" if v1 else ">I", data[pos:pos + (8 if v1 else 4)])[0]
            assert 0 < val < 10 * t.timescales[track], (track, val)
    if shutil.which("ffprobe"):
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,start_time", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True)
        assert "video" in r.stdout and "audio" in r.stdout and not r.stderr.strip(), (r.stdout, r.stderr)


def test_single_track_still_trims():
    """A video-only segment (no tfdts list, as older code produced) still rebases via the lead track."""
    s = fmp4.parse(FIX)
    for f in s.fragments:
        f.tfdts = []                                   # pretend the parser knew only the lead track
    out = fmp4.trim(s, 0.0, [(2.0, 3.0)], Path(tempfile.mkdtemp()))
    assert out and fmp4.parse(out[0][0]).fragments[0].t == 0.0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
