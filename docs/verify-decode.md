# Verify decode cost: measured and cut (2026-10-28)

Follow-up to the shared inference plan's phase 5 load test (docs/shared-inference-plan.md). Phase 5 found a verify
costs 3.5-4.3 CPU-seconds per event, nearly all of it decoding frames from the camera's 4 MP recording in the
instance; the GPU's part is ~55 ms. At Bingo's peak that is ~1.2 cores per Site. 40 such Sites used 52 cores, past
fred's 40 physical cores, while the A10G was only 63 % busy.

This note covers where the decode time goes, what cuts it without changing a single pixel, and what doesn't.

## How it was measured

Everything ran in throwaway containers on fred, from the live image (axiom/instance:f575d27), as Bingo's own user
(20001) with:
- its data and recordings mounted read-only, and a copy of its database;
- no network, `--cpus 8` (the instances' own limit), and GPU 1 (the A10G) for NVDEC only.

Nothing on the live Sites or the live inference service changed.

For each event:
1. Run `Verifier.verify` (dry run) with `frames.recording_frames` patched to record the targets and stop. These are
   the exact frame times verify asks for: 6 samples, plus a pre-roll frame for vehicles.
2. Decode those targets with each method, one event at a time.
3. Measure process CPU time (all threads), wall time, and frames decoded.
4. Take an MD5 of every returned BGR frame, so methods that must match the production code pixel for pixel are
   checked rather than assumed.

The scripts live in the session scratchpad (`dec/`):
- `dec_prof.py`
- `nv_diff.py`
- `nv_tput.py`
- `nv_mem.py`
- `sub_parity.py`
- `run.sh`

The results are in `/srv/axiom/decprof/out` on fred.

Bingo's recording is HEVC Main, 2688x1520, yuvj420p, **30 fps with a keyframe every 2 s** (60 frames per GOP),
~3.1 Mbit/s.

## Where the time goes (production code)

Sample: 150 events from Bingo's worst hour, 2026-10-21 17:00 UTC.

| | Per event |
|---|---|
| Targets asked for | 6.9 |
| Frames decoded to reach them | 70 (each target needs its keyframe and every frame after it up to the target) |
| Seeks | 2.2 |
| BGR conversion of the 6.9 frames handed out | 0.21 CPU-s |
| Opening, seeking and demuxing, no decode | 0.09 CPU-s |
| **Total** | **3.70 CPU-s, 1.59 s wall** |

Decoding one 4 MP HEVC frame on one thread costs ~20 ms of CPU. 70 frames should therefore cost ~1.4 CPU-s, but
production spends 3.7.

**The 2.5x is FFmpeg's frame threading.** `frames._decode_segment` set `thread_type = "AUTO"`. On a big host
FFmpeg starts ~16 frame threads, whatever the container's CPU limit. Each thread decodes ahead, and every seek or
early stop discards that work. Thread start and sync add more.

The same frames, decoded on one thread, cost 1.43-1.50 CPU-s and less wall time (1.27-1.35 s), because the
container's 8 CPUs were oversubscribed.

| Threads (same 60 events) | CPU-s per event | Wall |
|---|---|---|
| AUTO (production) | 3.59 | 1.53 s |
| FRAME x4 | 2.67 | 1.63 s |
| FRAME x2 | 1.73 | 1.12 s |
| SLICE x4 | 1.43 | 1.27 s (the camera's stream has no slices to split) |
| **1** | **1.43-1.50** | **1.27-1.35 s** |

## What was tried

| Method | CPU-s per event | Wall | Frames vs production |
|---|---|---|---|
| Production (frame threads AUTO) | 3.59 | 1.53 s | |
| One decoder thread | 1.43 | 1.27 s | 60/60 identical |
| **One thread + only the GOPs that hold a target** | **1.16** | **1.00 s** | 60/60 identical |
| NVDEC, frames downloaded as decoded | 0.71 | 0.82 s | 60/60 identical (after the chroma fix below) |
| **NVDEC, frames kept on the card, only the handed-out ones downloaded** | **0.59** | **0.58 s** | 60/60 identical |
| Nearest keyframe only (I-frames) | 0.73 | 0.64 s | different frames: 3.8 of 6.9 targets within 1 s at all |
| Reduced-resolution decode | n/a | | FFmpeg's HEVC decoder has no lowres. NVDEC's own `resize` changes pixels like the sub-stream case below, for ~0.1 CPU-s at most |

**Only the GOPs with a target.** The rewrite reads packets (cheap) from the first target's keyframe on, and decodes
only:
- the GOPs that hold a target;
- from each keyframe to the first frame at or past its target.

The old code decoded on through any gap shorter than 3 s, and 0.6 s past every target. This cuts frames decoded
from 70 to 52.

**NVDEC is pixel-exact.** HEVC decoding is bit-exact by specification: NVDEC's Y and UV planes equal the CPU
decoder's. Its frames come out as nv12, though, and swscale's nv12→BGR path rounds chroma differently from its
yuvj420p path (mean 0.2, max 44 levels apart). Splitting the interleaved chroma back into planes first
(`frames._bgr`) gives byte-identical BGR.

**NVDEC on the card** (A10G, 4 MP HEVC):
- **Throughput:** ~1,200 frames/s for the whole card, saturated at ~4 concurrent decoders (decoder 93 % busy).
  That is ~80,000 verifies/h at 52 frames each, about what the card's compute verifies anyway (phase 5: ~45,000/h
  at the 60 % busy ceiling). NVDEC is a separate engine from the SMs YOLO runs on.
- **GPU memory:** ~350 MB per decoding process (its CUDA context), returned as soon as the segment is closed. So
  it costs concurrent decodes x 350 MB per card, not Sites x 350 MB.
- **Its own CUDA context:** FFmpeg must not share the device's primary context. Once torch has made that context
  (a server running its own models), FFmpeg's attempt fails with "Operation not supported" (Errno 95). The first
  replay hit this, and the fallback put the rest on the CPU. `primary_ctx=0` fixes it at the same cost (0.60 CPU-s
  per event).
- **Host package:** fred's driver is the headless package, which has no `libnvcuvid` (`libnvidia-decode-580-server`).
  No container can use NVDEC until that is installed. For the test, the matching .deb (580.178.04) was unpacked into
  `/srv/axiom/decprof/lib` and mounted only into the scratch containers. The host's packages are unchanged.

## Verifying from a smaller picture (the sub stream)

Bingo's camera lists a 704x576 (D1) sub stream, and only the main stream is recorded. Verdict parity from a real
sub stream can't be replayed, so `sub_parity.py` scaled each recorded 4 MP frame down (INTER_AREA) before verify.
This is the optimistic case: a real sub stream is also encoded separately, at a fraction of the bit rate.

The sample was 300 events from Bingo's last 6 days: 150 verified and 150 rejected, chosen at random. Own YOLO,
fp32. The "full" run disagreed with Bingo's original verdict on 1 event.

| Frames given to verify | Verdicts changed | Classes changed | Hit counts changed |
|---|---|---|---|
| 1920x1080 | 2 (both rejected → verified) | 14 | 14 |
| 1280x720 | 2 (both rejected → verified) | 11 | 15 |
| 704x576 (D1, squeezed) | 2 (both rejected → verified) | 34 | 17 |

**Any pre-scaling changes verdicts.** Even 1080p, which YOLO shrinks to 1280 anyway, flipped 2 vehicle rejections
(81038, 73596) to verified. The verifier's own letterbox scales with INTER_LINEAR, and a different resample of the
same picture is enough to tip borderline vehicles.

On top of that:
- the sub stream isn't recorded, so recording it would be needed;
- PPE, the weapon check, plates and Qwen's keyframe crops want full resolution.

**Not recommended.** The decode savings above come without any of this.

## Verdict replay with the new code

`sub_parity.py` was run again on the same 300 events (149 verified, 151 rejected), with:
- the branch's frames.py mounted over the image's;
- `NVR_DECODE_HWACCEL=cuda`;
- own YOLO in fp32 on the same card, so torch shares the process with NVDEC.

Compared with the production-code run, **all 300 match**: verdict, class, hit count, confidence and rejection
reason. There were no NVDEC fallbacks.

The frame hashes already said as much. In three 150-event runs (the worst hour twice, the last 6 days once), the
new code returned byte-identical frames for every event, on the CPU and on NVDEC.

## Built (this branch, not deployed)

**`frames._decode_segment`:**
- One decoder thread, and only the GOPs that hold a target.
- The output is the same: the nearest frame within `tol` per target, in time order.
- Covered by `test_only_gops_with_a_target_are_decoded_and_the_frames_are_the_nearest`, which compares against a
  full decode with and without B-frames, for sparse, dense and random targets.

**`settings.decode_hwaccel`** (`NVR_DECODE_HWACCEL`):
- `"cuda"` decodes recorded frames on NVDEC. Frames stay on the card until handed out, and `frames._bgr` gives the
  CPU's exact BGR.
- Default off.
- Any NVDEC failure hands the segment to the CPU and keeps the CPU for 10 minutes (`HW_OFF_S`). This is tested by
  forcing a bad device.

**`verifier.grab_frames` and `weaponcheck.clip_evidence`** (legacy clip files) drop frame threads too.

Everything that reads the recording through `frames.iter_recording` gets the change:
- verification (samples and the path extension);
- PPE;
- the weapon check;
- re-verify.

The same code runs on-site. A CPU-only box should see a similar cut (about a third of the CPU-seconds on fred), but this wasn't measured on one.

## Before / after, CPU-seconds per event

Decode only, measured; all with byte-identical frames.

| Sample | Production | Branch, CPU | Branch, NVDEC |
|---|---|---|---|
| Bingo's worst hour, 150 events (verify samples) | 3.70 (1.59 s wall) | **1.17** (1.01 s) | **0.60** (0.61 s) |
| Bingo's last 6 days, 150 random events | 3.20 (1.50 s) | **1.06** (0.92 s) | **0.63** (0.62 s) |
| Path extension of verified events (production only, not in phase 5's dry run), 15 events | +2.77 | +1.05 (prototype) | +0.64 (prototype) |

Phase 5's planning figure was ~1.2 cores per Bingo-class Site at its peak (~3.7 CPU-s per verify). With the branch
that becomes **~0.45 cores on the CPU alone**, or **~0.25 with NVDEC**. The second number is mostly the instance's
other per-event work, such as letterboxing (~12 ms per frame).

Fred's 40 physical cores would then carry ~90 Bingo-class Sites at their peak on the CPU path. With NVDEC, the
GPU, not the CPU, becomes the limit again.

## To deploy (needs Shawn's OK)

1. **The CPU change:** a new instance image, then `restart-instance --image` per Site, as in phase 4. Nothing else
   changes. On-site servers get it with their next deploy.
2. **NVDEC on fred:**
   1. `apt install libnvidia-decode-580-server=580.178.04-0ubuntu0.24.04.1`. It must match the apt-held driver.
      The instances' containers already ask for the `video` capability.
   2. Set `NVR_DECODE_HWACCEL=cuda` in the GPU instances' env. axiom_host would write it, like `inference.env`;
      not built yet.
   3. Budget ~350 MB of the card per Site decoding at that moment.
3. **The BOM's "Instance CPU" line** in docs/shared-inference-plan.md becomes ~1.1 CPU-s per event on the CPU path
   (~0.6 with NVDEC), instead of the ~3.7 phase 5 measured.
