# Shared inference service: plan (proposed 2026-10-27)

Today every central recording instance (one per Site) loads its own copy of YOLO11s, the PPE detector, OSNet re-ID
and OpenCLIP onto its GPU. Measured on fred 2026-10-27, that costs 1.0-3.8 GB of GPU memory per Site whatever its
camera count, so a 24 GB card (A10, L4) holds 5 Sites at the default 4 GB share. The 2,200-camera BOM
(docs/server-bom-2200-cameras.pdf) puts ~122 Sites on a server with 3x L4: the per-Site design needs ~24 cards for
that.

The GPU itself is nearly idle. YOLO runs only per event (7-13 frames), never continuously:

| Measured on fred's A10G, 2026-10-27 | |
|---|---|
| Bingo (busiest Site) | 2,784 events/day; 1,225 in its worst hour |
| GPU time per event, all models | ~0.24 s (5-min nvidia-smi dmon, 14 events) |
| A10G busy, 3 Sites | ~1 % |
| YOLO11s @1280, one process, fp32 | 85 frames/s (batching doesn't help: CPU preprocessing is the limit) |
| YOLO11s @1280, fp16, batch 16 | 113 frames/s |

So one card at half load verifies ~7,000 events/hour. Memory, not compute, is what runs out.

**The change:** one inference service per GPU on each recording host holds every model once; the Sites' instances
send it frames and get back the same answers they compute today. Instances stop using CUDA at all.

## Goals

- 120+ Sites per 3x L4 server, limited by events/hour, CPU and RAM, not by models in GPU memory.
- Verification results unchanged (checked by replaying real events, below).
- One busy Site can't starve the others' alarm verification.
- Restarting or upgrading the models never interrupts recording.
- On-site NVRs (desktop, Qwenbot, T-304, Hailo lite boxes) keep their local models: nothing changes for them.

## What moves to the service

Every GPU user in backend/nvr already sits behind a small model object, and the Hailo work proved the stand-in
approach (detector.HailoSupervisor, hailo.HailoYOLO answer `.names` / `.predict` like ultralytics):

| Model | Used by | Call surface to keep |
|---|---|---|
| YOLO11s (COCO) | verifier.Verifier._predict, merge.footage_check, weaponcheck.yolo_predict, ppe (extra person boxes) | `.names`, `.predict(images, imgsz, conf, classes, ...)` -> results with `.boxes.xyxyn / .cls / .conf` (ndarrays) |
| PPE YOLOv8s @960 | ppe.Detector.detect | the same |
| OSNet x0.25 re-ID | reid.ReID.embed (verifier, journeys, identities) | `.embed(crops) -> 512-d mean vector` |
| OpenCLIP ViT-B-16 fp16 | footage index (continuous, every camera), vehicle fingerprints, footage search text | `.embed_images(bgr list)`, `.embed_text(str)` |

All of these already run on the pipeline's single `gpu` executor thread (pipeline.py:90), so a remote call drops in
where a local one runs now, with no change to the pipeline's ordering.

Not moving: the SD playback transcode (api.py `sd_ffmpeg_args`, NVENC + `-hwaccel cuda`), which is on-demand and
capped by `sd_max`. Instances keep the GPU for video only (`--gpus device=N` with
`NVIDIA_DRIVER_CAPABILITIES=video,utility`); without it they fall back to libx264 on the CPU, as a CPU instance does
now.

The footage index is the one continuous GPU load: up to one frame per 5 s per camera, five CLIP crops each
(footage.py SAMPLE_S, tile_crops), skipped when nothing moves. At the BOM's ~550 cameras per server that is at most
~550 crops/s at 224 px. Not measured yet (phase 0).

## Design

**Process.** `python -m nvr.inferd` in the same image as the instance (axiom/instance), so the weights,
preprocessing and ultralytics version are identical by construction and there is one image to build. axiom_host runs
one container per GPU (`axiom-infer-gpu1`, `--gpus device=1`, restart always). Models load at startup; YOLO and PPE
stay resident; CLIP and OSNet too (CLIP fp16 ~0.3 GB, OSNet ~10 MB).

**Transport.** HTTP over a Unix socket, bind-mounted read-only into each instance (`/run/axiom-infer/gpu1.sock`).
No network port, nothing reachable from outside the host. The body is raw uint8 frames (a small header and
the arrays), not JPEG. That saves an encode/decode per frame; over a local socket a 1280 px frame is a couple of
milliseconds.

**Preprocessing moves to the instance.** The benchmark showed the CPU letterbox, not the GPU, limits one process to
85 frames/s. The instance resizes and letterboxes to the model's input (it has the CPU cores, spread across 120
processes) and the service only stacks, runs fp16 and does NMS. Boxes come back normalized to the original frame,
as HailoYOLO already does (hailo.py maps letterboxed boxes back).

**Batching.** The service gathers requests for the same model for up to ~5 ms or 16 frames and runs them as one
batch.

**Fairness and priority.** Three queues, served strictly in order:
1. `verify`: verifier YOLO, PPE and re-ID for a fresh event (alarms wait on this);
2. `background`: re-verify, merge checks, weapon checks, vehicle fingerprints, journeys backfill;
3. `index`: footage CLIP.

Within each queue, round-robin by Site: a Bingo burst takes its turn alongside a quiet Site's single event, and
can't push it back by a minute. Index work only runs when the first two are empty, with a cap of ~1 batch per Site in
flight.

**Identity and accounting.** axiom_host gives each instance an `NVR_INFER_TOKEN` (env file, like the enroll token).
The service maps token -> instance id for fairness and per-Site GPU-seconds. That counts toward
ai-schedule metering if we ever bill for it, and toward placement. An instance can't pass itself off as another Site.

**Client.** backend/nvr/inference.py: `RemoteYOLO(kind)`, `RemoteClip`, `RemoteReID`, chosen when
`NVR_INFER_SOCKET` is set: in verifier.load_yolo, Pipeline.get_clip, the ReID constructions, ppe.Detector._load. In
remote mode the instance never imports torch / ultralytics / open_clip. That also saves the ~0.5 GB CUDA
context per Site and an estimated ~1 GB of RAM per Site (to measure; RAM was 1.0-2.6 GB per Site on fred).

**Failure handling.** Like HailoSupervisor: a timed-out or refused call retries with backoff. After ~30 s down the
instance raises a new health alert `inference_unavailable`. Events wait in the verify queue rather than being
marked `error` (new behaviour, to build: today a failed verify is an `error`), and they verify once the service is
back. If the host has a second GPU
service, the client fails over to it. The existing `detector_stalled` alert still catches anything stuck.
gpumem.py's out-of-memory handling moves into the service: a batch that runs out of memory is split and retried.

**Upgrades.** Restarting the service pauses verification for the few seconds models take to load; recording, live
view and playback carry on. The service and instances check a protocol version and model hashes in a handshake, so
an instance on a newer image waits (with the alert) rather than getting answers from a different model.

## Hub and axiom_host

- **host.json:** `"inference": "shared"` (default `"per_instance"` until migrated). axiom_host starts the service per
  eligible GPU (the existing `gpu_prefer` rule; never the vLLM A40), and new instances are created with the socket
  mount, token and video-only GPU instead of a compute GPU and a share.
- **New commands:** `infer_status` and `restart_infer` (with `image` for upgrades). `capacity.work` gains per-service
  numbers: queue depths, frames/s, p95 verify latency, GPU busy %, memory, per-instance GPU-seconds over the last hour.
- **Placement:** on a shared host, Sites are placed by CPU, RAM and disk, plus inference headroom: the last hour's
  peak GPU busy % on the host's services, compared against a ceiling (e.g. 60 %). GPU shares (just built) stay for
  per-instance hosts and for the migration.
- **Hosts page:** each service's load, verify latency and the busiest Sites by GPU time.
- **Alerts:** `inference_unavailable` (health kind, per Site); `inference_slow` (host-level) when p95 verify latency
  passes ~10 s, meaning add a GPU or move Sites.

## Checking that results don't change

tools/replay_verification.py already re-runs the verifier over a camera's past events in dry-run mode. Phase 0 runs
it over Bingo's and Jetstream's last week (local fp32 vs service fp16) and compares verdicts, matched classes and box
IoU. The bar: no verdict changes beyond what two local runs differ by. If fp16 changes verdicts, the service runs
YOLO in fp32: 85 vs 113 frames/s, still far beyond the load.

## Phases

0. **Measure (no production change).** Done 2026-10-27: see Phase 0 results below. In a scratch container on fred's A10G:
   - Throughput of each model at batch sizes 1-16 in fp16 and fp32, with preprocessing outside the timed loop.
   - CLIP crops/s, to size the footage index.
   - The fp16 parity replay.
   - RAM of an instance with torch never imported.

   The output is a capacity table per card: events/hour, cameras of index.
1. **Service + client** in backend/nvr, with unit tests against a fake socket and an end-to-end test that runs a
   local inferd on the CPU. On-site NVRs untouched (no `NVR_INFER_SOCKET`). Built 2026-10-27, not deployed:
   - **The modules:**
     - `inferwire.py`: the wire format, plus letterbox / CLIP / re-ID frame preparation. The letterbox is
       identical to ultralytics' pixel for pixel.
     - `inferd.py`: the service (`python -m nvr.inferd --listen ... --device ... --tokens ...`).
     - `inference.py`: `RemoteYOLO`, `RemoteReID` and `RemoteClip`, plus the `reid_model()` / `clip_model()`
       factories.
     - `reidprep.py`: re-ID's crop helpers without torch.
     - clip.py now imports torch only when a model loads.
   - **Settings:** `NVR_INFER_SOCKET` (one address, or two comma-separated for failover), `NVR_INFER_TOKEN`,
     `NVR_INFER_TIMEOUT_S`, `NVR_INFER_RETRY_S`.
   - **Server behaviour:**
     - /api/system reports `inference`.
     - Health alert `inference_unavailable` after 30 s down; while down, YOLO reports not ready, so
       `detector_stalled` stays quiet.
     - The verify worker (`Pipeline._verify_one`) puts an event back in the queue after 15 s instead of marking it
       an error.
   - **Transport:** a Unix socket in production. Also `host:port` TCP: Windows has no Unix sockets, and the tests
     run there.
   - **Priorities:** YOLO, PPE, re-ID and CLIP text use `verify`; CLIP images (the footage index, vehicle
     fingerprints) use `index`. Nothing uses `background` yet: merge checks, weapon checks and re-verify run at
     `verify`. Worth wiring once we see them compete.
   - **tests/test_inference.py:**
     - wire and letterbox parity;
     - scheduling (priority, round robin, batching, the budget);
     - tokens, the weights handshake and an OOM batch split;
     - failover, the alert and the requeue;
     - an instance on the service imports no torch / ultralytics / open_clip at all;
     - end to end on the CPU, where the service's YOLO (one-size and mixed-size calls), PPE, re-ID and CLIP match
       the local models (boxes within 1e-4, embeddings within 1e-4).
   - **GPU smoke test on the desktop (fp16):**
     - The service found the same 5 boxes as local fp32 YOLO, within 0.0004.
     - A 10-frame event's round trip was 212 ms over Windows loopback TCP, letterboxing included. Measure again on
       a Unix socket on fred; if the frame bytes dominate, send JPEG or shared memory instead.
2. **axiom_host** (0.4.0): run and supervise the services, create instances in shared mode, report service work,
   switch an existing instance between modes (one recreate, ~30 s recording gap). PROTOCOL.md / README.
3. **Hub**: shared-mode placement, Hosts page service panel, the two alerts, per-Site GPU-seconds.
4. **Migrate fred**, one Site at a time, each with a day of watching verify latency and verdicts: Test-Site,
   Jetstream, then Bingo. The per-instance models free up as each one moves.
5. **Load test before the 120 Sites:** replay Bingo's worst hour (1,225 events) concurrently as if from 20 Sites
   and confirm verify latency stays under the alarm budget. This tells us how many Bingo-class Sites a card takes in
   practice.

## Phase 0 results (fred, 2026-10-27)

Run in throwaway containers from the live image (axiom/instance:9b0dada) on the A10G, next to the three running
Sites. Each container ran as its Site's own user, with that Site's data and recordings mounted read-only and a copy of
its database. Nothing on the Sites changed. Scripts:
`p0_bench.py`, `p0_parity.py` and `p0_ram.py`, kept in the session scratchpad for now (not in the repo).

**GPU time per frame**, model only. Bingo's real 2688x1520 frames were letterboxed on the CPU first, outside the timer
(as the instances would do):

| Model | Input | fp32, batch 1 | fp32, batch 16 | fp16, batch 16 | fp16 throughput |
|---|---|---|---|---|---|
| YOLO11s | 1280x736 | 7.7 ms | 7.2 ms | 4.2 ms | ~240 frames/s |
| PPE YOLOv8s | 960x544 | 5.1 ms | 3.7 ms | 2.2 ms | ~450 frames/s |
| OSNet re-ID | 256x128 crop | | 0.36 ms (b32) | 0.10 ms (b128) | ~10,000 crops/s |
| OpenCLIP ViT-B-16 (fp16 already) | 224x224 crop | | | 0.88 ms (b160) | ~1,130 crops/s |

- **Batching only pays in fp16.** fp32 is flat from batch 1 to 32; fp16 at batch 1 is no faster than fp32.
  The service batches and runs fp16.
- **The whole predict call today:** ultralytics' predict, preprocessing included, is 13.1 ms per frame (YOLO) and
  7.4 ms (PPE). So about half of today's time is CPU work that moves to the instances.
- **CPU cost per frame in the instance:**
  - YOLO letterbox: 6.0 ms, plus 5.8 ms to stack the frame into the model's layout.
  - PPE: 4.6 ms plus 3.2 ms.
  - CLIP: 8.3 ms per crop (squashing a 4 MP frame to 224).
  - Re-ID: 0.5 ms per crop.
- **GPU memory, all four models in one process** after these batches (up to 32 frames): 3.3 GB peak allocated,
  5.8 GB reserved by torch, 6.1 GB as nvidia-smi counts the process. Budget ~6 GB per card for the service.
- **fp16 vs fp32, real events replayed** (verify, dry run, frames from the recording; each event run twice in
  fp32 and once in fp16):

  | Site | Events | Verdict changed | Class changed | Hit count changed | Confidence difference |
  |---|---|---|---|---|---|
  | Bingo (last 6 days, 150 verified + 150 rejected, random) | 300 | 0 | 0 | 3 | mean 0.001, max 0.084 |
  | Jetstream (last 14 days, all) | 100 (6 had no recording) | 0 | 0 | 0 | max 0.001 |

  The two fp32 runs were identical. On Bingo, 5 of 300 events decide differently today than when they first ran,
  in both precisions. That comes from the parked-vehicle memory and rule changes since, not from precision.
  **fp16 is safe.**
- **RAM the instances would no longer need** (one process, measured step by step):

  | Step | RSS added |
  |---|---|
  | Import torch | 480 MB |
  | CUDA init | 125 MB |
  | YOLO on the GPU, one predict | 710 MB |
  | CLIP | 435 MB |
  | Re-ID | ~6 MB |
  | **Total** | **~1.75 GB per Site** |

  The live servers' processes were 2.3, 4.9 and 3.4 GB. Across 122 Sites that is about 210 GB of RAM per server.

## Sizing for the BOM

GPU time per event in the service: YOLO on ~10 frames is ~42 ms in fp16, plus ~9 ms of PPE on PPE Sites, plus
re-ID (well under 1 ms): **~50 ms**. One A10-class card therefore verifies ~70,000 events/hour flat out. Today's
per-process setup measured ~0.24 s per event (~7,000/hour at half load), so batching and fp16 are worth ~5x on
paper. Phase 5's load test confirms it. Until then, plan on **~20,000 events/hour per card**.

Per 3x L4 server (~122 Sites, ~550 cameras; the L4 is the A10's successor in the same class, measure one when it
arrives):

| Load | Estimate | Share of one card |
|---|---|---|
| Verification | 2,800 events/h if the average Site sees a fifth of Bingo's traffic (~23 events/h) | ~15 % at the planning rate |
| Footage index, worst case (motion everywhere, never skipped) | 550 cameras x 5 crops / 5 s = 550 crops/s | ~50 % |
| Footage index, typical | Skipping still frames cuts this to a fraction | Not measured per camera |
| GPU memory | ~6 GB per service | 18 GB of each card's 24 GB stays free |
| Instance CPU (preprocessing) | ~0.12 CPU-s per event, plus up to ~4.6 cores for CLIP crops at the index's worst case | Fine on the BOM's CPUs |

**One card could carry a whole server's AI.** Two cards give a spare; three leave room for Bingo-class Sites, for
a card failing, and for growth.

The footage index, not verification, is the larger GPU load. It is also the one that can wait: it runs
last in the queue. The next measurement is its real crops/s per camera on a busy Site.

## Open questions (for Shawn)

1. fp16 for YOLO: phase 0 found no verdict or class changes in 400 events and ~1.7x the throughput (recommended: yes).
2. Meter GPU-seconds per Site now (for pricing like the Otso hours) or only for placement?
3. The alarm budget for verify latency under load (proposed: p95 under 10 s from event close to verdict).
