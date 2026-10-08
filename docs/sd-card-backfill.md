# SD card backfill: recovering footage and events from the cameras after an outage

Status: phases 0 and 1 built (2026-10-08, not deployed); phases 2-4 planned. Proven by hand on Building 2 East (cam5, Milesight MS-C5366-X12PE): the 10:59:04–11:03:04 gap of 2026-10-08 was replayed from the camera's SD card in full.

## What the test showed

| Finding | Detail |
|---|---|
| Profile G is supported | All five Milesight cameras at Jetstream HQ answer the ONVIF recording, search and replay services. |
| Only one camera records to its card | cam5 holds 2026-09-14 → now (video, audio, metadata). cam1 reports "no record files"; cam2–cam4 report an empty recording. No card, or recording to it is off. |
| Replay is exact | `PLAY` with `Range: clock=20261008T155903Z-20261008T160304Z` and `Require: onvif-replay` returned 15:59:04 → 16:03:04 UTC, one frame per 100 ms, no jumps, stopping at the requested end. |
| Timestamps are trustworthy | Every frame carries the ONVIF RTP header extension (0xABAC) with its NTP wall-clock time, so restored footage lands at the right second. |
| Real time only | `Rate-Control: no` and `Scale: 4.0` were ignored (`Scale: 1.000000`). A 10-minute gap takes 10 minutes per camera. |
| Keep-alive is required | Without one the camera ends the session after ~65 s; `GET_PARAMETER` every 20 s keeps it open. |
| Main stream quality | H.265 2592×1520, ~2.6 Mbit/s (79 MB for 4 minutes). |
| Separate port | Replay is `rtsp://<camera>:555/onvifreplay`, not the live RTSP port. |
| Off-the-shelf players can't do it | ffmpeg and MediaMTX can't send `Require: onvif-replay` with a clock range, so the server needs its own small replay client (the test client is ~250 lines). |

## Design

### 1. Know which cameras can be recovered
- Per camera, every hour: `GetRecordingSummary` / `FindRecordings` → "SD card: recording, holds Sept 14 → now" or "no recording on the camera". Shown in Settings → Cameras and on the hub's Site › Servers.
- An alert `camera_sd_not_recording` when a camera that had SD footage stops adding to it (card full, failed or removed).
- Optional: switch on recording to the card over ONVIF (`CreateRecordingJob`) where the camera allows it; otherwise the camera's web page.

### 2. Find the gaps
- From the server's own recording segments per camera (the same listing the Timeline uses): any hole over 20 s in the last N hours (default 72, capped by what the card holds).
- Each gap gets a cause when known (server restart, link down, video-service reload) and a state: `waiting`, `recovering`, `recovered`, `partly recovered`, `not on the card`.

### 3. Fetch the footage (backend `sdbackfill.py`)
- The replay client from the test, hardened: Digest auth, TCP-interleaved RTP, keep-alive, H.264 and H.265 depacketizing, ONVIF timestamps, reconnect and resume from the last good second.
- One replay session per camera at a time (cameras limit sessions; the live stream keeps running alongside). Cameras of a server run in parallel.
- Output: the restored video is cut into segments named and formatted like the server's own recordings and written into the camera's recording folder, so the Timeline, playback, export and retention treat it as ordinary footage. Restored spans are recorded in a table (`camera, from, to, source='sd', state`) and shown on the Timeline in a different shade with "Recovered from the camera's SD card".
- Order: newest gap first; a gap shorter than 20 s is ignored.

### 4. Rebuild the events
- The replay also carries the camera's **metadata track**: the same ONVIF object and motion stream the server reads live. Feeding it through the existing ingest and tracker in "replay mode" opens events exactly as live, then the normal verification (YOLO), synopsis (Qwen), PPE checks, site rules and journeys run on the restored footage.
- Restored events are marked "recovered after outage" with their real times. They never push live notifications (they are old news) but appear in Find, Ask, the Timeline and alert history; a site rule broken during the outage opens an alert marked as recovered so the SOC sees it.
- Fallback when a camera's replay has no metadata track: run the server's motion and YOLO sampling over the restored video at 2 frames per second.

### 5. When it runs
- Automatically: after the server starts, after `site_link_down` clears, after the video service restarts, and once an hour as a sweep.
- By hand: "Recover from SD card" on a Timeline gap and on the camera in Settings.

### 6. Central recording and cellular sites
- VPN mode: nothing extra; the instance reaches port 555 like any other port.
- Port-forward mode: a third forward per camera (replay port 555) on the Peplink, a `public_replay_port` on the camera, and the Peplink sheet lists it. The replay URL the camera returns is rewritten like the other ONVIF URLs.
- Data: replay is main-stream quality (~1.2 GB per camera-hour at 2.6 Mbit/s). A per-Site daily backfill budget (GB) with an alert when it's used up; small gaps first.

### 7. Hub
- Site › Servers: SD status per camera.
- Timeline: recovered spans shaded; gaps still waiting show "recovering from SD card".
- Health: `camera_sd_not_recording`, and "backfill budget used up" for cellular Sites.

## Phases

0. **Remaining checks (half a day):** (a) segments written by the server are picked up by the video service's playback listing; (b) replaying the metadata track gives the same object data as live; (c) a gap of an hour or more, and a reconnect in the middle; (d) audio, kept or dropped.
1. **Footage:** replay client, gap finder, backfill worker, Timeline shading, SD status per camera. Manual trigger only.
2. **Events:** metadata replay into the tracker, restored events marked, no live push.
3. **Automatic:** triggers, the hourly sweep, the SD alert.
4. **Central and hub:** port-forward replay port, data budget, hub UI.

## Phase 0 results (2026-10-08, cam5)

| Check | Result |
|---|---|
| (a) The video service picks up the server's own segments | Yes. A segment written in MediaMTX's own layout (below) into a scratch MediaMTX 1.21's record folder is listed at the right start and length and served by `/get` (fMP4 and MP4); retention's trimmer and PyAV read it too. MediaMTX joins two segments into one listing entry only when they carry the same stream id with consecutive segment numbers (`mtxi`), so a restored segment is its own entry next to the live ones; the Timeline merges entries under 2 s apart and the player moves on to the next chunk at the boundary. |
| Timing | The card holds the same encoded stream as the live recording (frame sizes equal to the byte). Camera NTP time plus the metadata reader's clock offset puts each restored frame within 26 ms of where MediaMTX put the same frame live (589 of 590 frames matched). |
| (b) Metadata | Not available from this camera: the replay SDP has a video track only (no audio, no metadata), and `FindEvents` returns only `RecordingHistory` states. Events can't be rebuilt from the camera's analytics here; phase 2 needs the fallback (motion + YOLO over the restored video). cam5's live metadata produced no object events in the last 7 days either (motion arrives over PullPoint). |
| (c) Long replay with a reconnect | 02:00–02:32 (32 min) replayed with the connection cut at 15 min: one reconnect, resumed from the GOP in progress (02:15:00.8), 1,924 s wall clock. Four 10-minute segments, 19,192 frames, exactly the 19,192 frames the live recording has over that span: each matched one-to-one (no duplicates, no holes, no frame interval over 100 ms), all decode cleanly, and MediaMTX lists the four as one 1,919 s entry. |
| (d) Audio | The live recording has the camera's G.711 audio (stored as LPCM); the replay has none, so restored segments are video only. Matching the live layout with a silent track was tested and changes nothing (MediaMTX still lists the two streams apart), so audio is dropped when the replay has none and kept as LPCM when it has G.711 / L16. |
| Other | The replay starts at the keyframe nearest the requested start (seen both just before and just after it) and the GOP is 2 s, so a restored gap can begin up to 2 s late (never overlapping the live segment before it). The ONVIF C/D flags are not usable on this camera (D is set on every packet): keyframes come from the NAL types and holes from the timestamps. |

## Phase 1 as built

- `backend/nvr/sdreplay.py`: the replay client (Digest, TCP interleaved, `GET_PARAMETER` every 20 s, H.264 / H.265 depacketizing with loss handling, ONVIF timestamps, G.711 to PCM) and `fetch_range`, which reconnects and resumes from the GOP in progress: the new session starts 4 s early and frames before the resume point are skipped, so nothing is written twice and nothing is skipped. Port-forward mode uses `public_replay_port`.
- `backend/nvr/fmp4mux.py`: writes segments exactly as MediaMTX does (ftyp, moov with mvhd length, hvc1/avc1, `mtxi` with a stream id per run, 1 s fragments, parameter sets in band), named by their start, published by a hard link that fails if the name exists (never overwritten), with the file's modification time set to its end like MediaMTX's.
- `backend/nvr/sdbackfill.py`: SD status per camera (hourly, read-only ONVIF), the gap finder, the job worker (one session per camera, cameras in parallel, newest first) and the `restored_spans` table.
- API: `GET /api/cameras/{id}/sd`, `GET /api/sd/gaps`, `POST /api/sd/recover` (admin through the hub), `restored` and `sd_card` in `/api/recordings/{id}`, `status.sd` in `/api/cameras`.
- UI: Settings → Cameras shows the SD status with a Check button; the Timeline shades recovered and running jobs and offers "recover" on a gap the card covers (admins).

## Before it is useful at Jetstream HQ
Four of the five cameras have nothing on their cards. Fit cards (or check they're fitted) and switch on recording to them, continuous at main-stream quality if the card is big enough: a 256 GB card holds about 9 days at 2.6 Mbit/s.
