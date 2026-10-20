"""The built-in siren: what a talk-down speaker sounds when deterrence's "siren" goes through it (deterrence.py), made
here in code (no audio file, nothing to license).

The sound, 8 kHz mono like everything a speaker's G.711 backchannel takes (talkdown.RATE):
  a police-style "wail then yelp", one TILE_S cycle repeated for as long as asked:
    wail  WAIL_S (3.2 s): one slow sweep up from LOW_HZ to HIGH_HZ and back down (a raised-cosine glide, 1.6 s each way)
    yelp  YELP_COUNT fast sweeps of YELP_S (16 x 0.2 s: 5 a second), the same 600 -> 1400 -> 600 Hz glide
  every sweep starts and ends at LOW_HZ with a whole number of cycles of the tone, so the tile ends exactly where it
  began (same phase, same frequency): repeated, it loops without a seam or click for any length.
  The tone is the fundamental plus 30 % of its second harmonic (at most 2.8 kHz: under the 4 kHz Nyquist limit of 8 kHz,
  so nothing aliases): a brassier, more piercing sound on a small horn speaker than a pure sine.
  Level: peaks at PEAK (about -1 dBFS) and about -5 dBFS RMS: as loud as G.711 carries it without clipping.
  The whole siren fades in over FADE_IN_S and out over FADE_OUT_S (no click at the start or end); it is exactly
  round(seconds * RATE) samples long.

pcm() gives 16-bit little-endian PCM; g711() the same in a speaker's law (talkdown.pcm_to_g711, the codec talkdown picks
from the speaker's SDP).
"""
from __future__ import annotations

import math
import sys
from array import array

RATE = 8000
LOW_HZ = 600.0
HIGH_HZ = 1400.0
WAIL_S = 3.2
YELP_S = 0.2
YELP_COUNT = 16
TILE_S = WAIL_S + YELP_S * YELP_COUNT      # 6.4 s
HARMONIC2 = 0.3
PEAK = 0.89                                 # of full scale: about -1 dBFS
FADE_IN_S = 0.02
FADE_OUT_S = 0.05
MAX_S = 120

_tile: array | None = None


def _sweep(n: int) -> list[float]:
    """Phases (radians) of one LOW -> HIGH -> LOW raised-cosine glide over n samples. The mean frequency times the length
    is a whole number of cycles (WAIL_S and YELP_S are chosen so), so the phase ends on a multiple of 2 pi."""
    mid, amp, T = (LOW_HZ + HIGH_HZ) / 2, (HIGH_HZ - LOW_HZ) / 2, n / RATE
    # f(t) = mid - amp cos(2 pi t / T)  ->  phase(t) = 2 pi (mid t - amp T / (2 pi) sin(2 pi t / T))
    return [2 * math.pi * (mid * (i / RATE) - amp * T / (2 * math.pi) * math.sin(2 * math.pi * i / n)) for i in range(n)]


def tile() -> array:
    """One TILE_S cycle as 16-bit samples (built once)."""
    global _tile
    if _tile is None:
        phases = _sweep(round(WAIL_S * RATE))
        for _ in range(YELP_COUNT):
            phases += _sweep(round(YELP_S * RATE))
        wave = [math.sin(p) + HARMONIC2 * math.sin(2 * p) for p in phases]
        scale = PEAK * 32767 / max(abs(min(wave)), max(wave))
        _tile = array("h", (round(w * scale) for w in wave))
    return _tile


def samples(seconds: float) -> array:
    """The siren for `seconds` (at most MAX_S) as 16-bit samples: the tile repeated, faded in and out."""
    n = max(0, round(min(float(seconds), MAX_S) * RATE))
    t = tile()
    out = t * (n // len(t) + 1)
    del out[n:]
    fin, fout = min(n, round(FADE_IN_S * RATE)), min(n, round(FADE_OUT_S * RATE))
    for i in range(fin):
        out[i] = round(out[i] * i / fin)
    for i in range(fout):
        out[n - 1 - i] = round(out[n - 1 - i] * i / fout)
    return out


def pcm(seconds: float) -> bytes:
    """16-bit little-endian PCM, exactly round(seconds * RATE) samples."""
    return _le(samples(seconds))


_tile_law: dict[str, bytes] = {}


def g711(seconds: float, codec: str = "PCMU") -> bytes:
    """The siren in the speaker's G.711 law (PCMU / PCMA): one byte per sample, exactly round(seconds * RATE) bytes;
    the same bytes as encoding pcm(seconds), but the tile is encoded once per law and only the faded ends anew."""
    from .talkdown import pcm_to_g711   # late: talkdown imports this module
    t = tile()
    if codec not in _tile_law:
        _tile_law[codec] = pcm_to_g711(_le(t), codec)
    n = max(0, round(min(float(seconds), MAX_S) * RATE))
    body = (_tile_law[codec] * (n // len(t) + 1))[:n]
    fin, fout = min(n, round(FADE_IN_S * RATE)), min(n, round(FADE_OUT_S * RATE))
    if not n:
        return b""
    faded = samples(seconds)
    head = pcm_to_g711(_le(faded[:fin]), codec)
    tail = pcm_to_g711(_le(faded[n - fout:]), codec)
    return head + body[fin:n - fout] + tail if fin + fout <= n else pcm_to_g711(_le(faded), codec)


def _le(a: array) -> bytes:
    if sys.byteorder == "big":
        a = array("h", a)
        a.byteswap()
    return a.tobytes()
