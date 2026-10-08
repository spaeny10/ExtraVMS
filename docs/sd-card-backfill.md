# SD card backfill: recovering footage and events from the cameras after an outage

Status: plan (2026-10-08). Proven by hand on Building 2 East (cam5, Milesight MS-C5366-X12PE): the 10:59:04–11:03:04 gap of 2026-10-08 was replayed from the camera's SD card in full.

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

## Before it is useful at Jetstream HQ
Four of the five cameras have nothing on their cards. Fit cards (or check they're fitted) and switch on recording to them, continuous at main-stream quality if the card is big enough: a 256 GB card holds about 9 days at 2.6 Mbit/s.
