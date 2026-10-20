"""The built-in siren (siren.py): the length to the sample for any duration, both G.711 laws (the same bytes as
encoding the PCM), the level (loud, never clipped), the wail/yelp sweep between 600 and 1400 Hz, the seamless loop and
the fades at the ends.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_siren.py   (from backend/)
"""
import math
import os
import sys
import tempfile
from array import array
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="nvr-siren-test-"))
for k, sub in (("NVR_DATA_DIR", "data"), ("NVR_RECORDINGS_DIR", "recordings"), ("NVR_RUNTIME_DIR", "runtime")):
    os.environ[k] = str(TMP / sub)   # talkdown (the G.711 encoder) loads the config: never the real server's
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import siren, talkdown  # noqa: E402

FS = 32768


def samples(seconds) -> array:
    a = array("h")
    a.frombytes(siren.pcm(seconds))
    if sys.byteorder == "big":
        a.byteswap()
    return a


def dbfs(x: float) -> float:
    return 20 * math.log10(x / FS)


def test_length_to_the_sample_and_both_laws():
    for secs in (1, 2.5, 10, 7.33, siren.TILE_S, siren.TILE_S * 3 + 0.01, 120):
        n = round(secs * siren.RATE)
        assert len(siren.pcm(secs)) == 2 * n
        for codec in ("PCMU", "PCMA"):
            g = siren.g711(secs, codec)
            assert len(g) == n, (secs, codec)
            assert g == talkdown.pcm_to_g711(siren.pcm(secs), codec)   # the speaker's law, from the same PCM
    assert siren.g711(1, "PCMU") != siren.g711(1, "PCMA")
    assert len(siren.g711(500, "PCMU")) == siren.MAX_S * siren.RATE   # at most MAX_S
    assert siren.g711(0, "PCMU") == b""


def test_level_is_loud_and_never_clips():
    a = samples(siren.TILE_S * 2)
    peak = max(max(a), -min(a))
    rms = math.sqrt(sum(x * x for x in a) / len(a))
    assert -1.5 <= dbfs(peak) <= -0.5, dbfs(peak)          # about -1 dBFS: as loud as it goes without clipping
    assert dbfs(rms) > -6.5, dbfs(rms)                       # and loud all the way, not a quiet tone with one peak
    assert peak < 32767


def zero_crossing_hz(a, start: int, n: int) -> float:
    ups = sum(1 for i in range(start + 1, start + n) if a[i - 1] < 0 <= a[i])
    return ups * siren.RATE / n


def test_it_sweeps_wail_then_yelp():
    a = siren.tile()
    assert len(a) == round(siren.TILE_S * siren.RATE)
    wail = round(siren.WAIL_S * siren.RATE)
    w = round(0.1 * siren.RATE)
    # the wail: low at its start, high in its middle (1.6 s), low again at its end
    assert zero_crossing_hz(a, 0, w) < 800 and zero_crossing_hz(a, wail // 2 - w // 2, w) > 1250 and zero_crossing_hz(a, wail - w, w) < 800
    # the yelp: five sweeps a second, each 600 -> 1400 -> 600 Hz
    y = round(siren.YELP_S * siren.RATE)
    q = y // 8
    for k in range(siren.YELP_COUNT):
        s0 = wail + k * y
        assert zero_crossing_hz(a, s0 + y // 2 - q // 2, q) > zero_crossing_hz(a, s0, q) + 300


def test_it_loops_without_a_seam_and_fades_at_the_ends():
    t = siren.tile()
    steps = [abs(t[i] - t[i - 1]) for i in range(1, len(t))]
    seam = abs(t[0] - t[-1])
    assert seam <= max(steps)   # the end runs into the start like any two samples inside the tile
    # repeated, the tile is the siren itself (no gap, no restart click), so any length loops
    a = samples(siren.TILE_S * 3)
    n = len(t)
    fade_in, fade_out = round(siren.FADE_IN_S * siren.RATE), round(siren.FADE_OUT_S * siren.RATE)
    assert a[fade_in:n] == t[fade_in:] and a[n:2 * n] == t and a[2 * n:3 * n - fade_out] == t[:n - fade_out]
    # faded in and out: silent at the very ends, full by the end of the fade
    assert a[0] == 0 and a[-1] == 0 and max(abs(x) for x in a[:fade_in // 4]) < FS * 0.3


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
