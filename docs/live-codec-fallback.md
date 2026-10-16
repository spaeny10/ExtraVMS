# Live view for H.265 cameras: options for later

Status: written down, not built (decision 2026-10-16). Background: docs/competitive-gaps-prompt.md.

## The problem

Live view is WebRTC (WHEP) straight from MediaMTX, never transcoded. When a stream is H.265 and the browser can't
take HEVC over WebRTC, MediaMTX refuses the offer with a codec 400 ("codecs not supported by client"), or the
connection comes up and no frame ever decodes.

- **HD tiles** (`<id>`, or `<id>_hd` on cameras that record their sub stream): WhepPlayer calls `onUnsupported`, and
  the page drops to SD. This works.
- **SD tiles** (`<id>_sub`): no `onUnsupported` is wired, by design, since SD has nowhere lower to go. A codec 400
  retries twice, then falls back to RecordingFollow, which plays the recording a few seconds behind live. The 8 s
  "no frames" check has no callback on SD, so a tile that connects but can't decode stays black.
- The SD source is normally the camera's sub stream, which most cameras send as H.264. It ends up H.265 when the camera's
  sub stream is set to H.265, or when the SD path relays the main stream (`streams.plan()` in backend/nvr/streams.py).

In short, an H.265 sub stream means no real live view in browsers without WebRTC HEVC. The options below are
alternatives, and A and C can be combined.

## Option A: on-demand H.264 transcode path

An `<id>_h264` MediaMTX path, fed by ffmpeg transcoding the sub stream. It runs only while someone watches
(MediaMTX `runOnDemand`, with `runOnDemandCloseAfter` a few seconds), and the browser plays it over the same WHEP route.

- **Reuses:** `sd_ffmpeg_args`, `detect_encoder` and `sd_max` in backend/nvr/api.py (~2245-2293, the q=sd playback
  transcode). The live variant needs RTSP output instead of fMP4 on stdout, and no audio (or Opus/G.711, since WebRTC
  can't play AAC).
- **Limits:** live transcodes and q=sd playback should share one budget (`_sd_active` / `sd_max`): 4 with NVENC, 2 on
  CPU. A grid of nine H.265 cameras would hit that limit, so the client needs to know when the path was refused.
- **Pros:**
  - It works in every browser.
  - No client changes beyond picking the path.
  - Latency stays at WebRTC levels, plus about 200-500 ms for the encoder.
  - Nothing changes in the CSP.
- **Cons:**
  - CPU or GPU load on the NVR for every viewer path. MediaMTX shares one transcode between viewers of the same path.
  - First frame is slower (ffmpeg start plus the first keyframe, about 1-2 s).
  - Another moving part in mediamtx.yml (write_config, tests in test_mediamtx_config_write.py).
  - A CPU-only site can afford very few of these.

## Option B: low-latency HLS (fMP4) through bundled hls.js

Turn on MediaMTX's HLS muxer (`"hls": False` today, backend/nvr/mediamtx.py:248) in low-latency fMP4 mode, kept
on localhost and proxied through the NVR API with the same auth as WHEP (e.g. `/api/hls/<path>/...`). Safari plays it
natively. Other browsers use hls.js, pinned and bundled through Vite with no CDN, feeding the stream to MSE.

- **What it fixes:** browsers that decode HEVC in `<video>` / MSE (`MediaSource.isTypeSupported('video/mp4;
  codecs="hvc1..."')`) but not over WebRTC. Browsers with no HEVC decoder at all still need A.
- **CSP notes (hub/hub/security.py):**
  - hls.js starts its transmux worker from a `blob:` URL by default. Hub pages (`hub_csp`) allow only `worker-src
    'self'`, and the site console under /s/<id>/ (`site_ui_csp`) has `worker-src 'none'`, so the worker would be
    blocked on both. fMP4 needs no transmuxing, so `enableWorker: false` costs little. That is the better choice
    over loosening worker-src.
  - Playlists and segments are fetched with XHR/fetch. `connect-src` already allows `'self' https:` on hub pages and the
    page's own `/s/<id>/` prefix on site pages, so a route under `/api/` works through the tunnel and over direct LAN.
  - MSE plays from a `blob:` media URL, and `media-src 'self' blob:` is already allowed on both page types.
  - Proxied `/s/<id>/api/*` answers carry `PROXY_CSP` (`sandbox; default-src 'none'; ...`), but that policy only
    applies when the answer is opened as a document, not to fetched playlists or segments. The proxy's content-type
    filter turns active types into text/plain, and `application/vnd.apple.mpegurl` is not on the active list, so it
    passes. Check this with a real Safari, which loads the playlist as the `<video>` src.
  - Over direct LAN, playlist and segment URLs need the `?direct=` token like the other media URLs (hybridApi
    MEDIA_KEYS in hub/ui/src/hubSource.ts). Rewritten playlists must keep the token on every URI.
- **Pros:**
  - No server CPU (HLS repackages, it doesn't transcode).
  - Works through restrictive networks (plain HTTPS, no UDP or TURN).
  - Scales to many viewers.
- **Cons:**
  - 1-3 s of latency instead of under 1 s.
  - About 150 KB of hls.js in the bundle.
  - Segment traffic through the hub tunnel counts against the site's uplink, as playback does today.
  - Doesn't help browsers with no HEVC decoder.
  - MediaMTX keeps a muxer per watched path in memory.

## Option C: one-click "switch the sub stream to H.264"

An advisor finding that offers to change the camera itself. `camdevice.set_video(cam, token, {"encoding": "H264"})`
(backend/nvr/camdevice.py:741) already validates against the camera's options. Encoding changes need ONVIF **Media2**:
`plan_video` raises `Invalid` on Media-only cameras. The finding plugs in through `Finding.apply`
(backend/nvr/advisor.py:55) and a new action in `/api/advisor/apply` (backend/nvr/api.py, next to `ptz_return_home` and
`retention_days`). The check belongs where the advisor reads stream tracks (advisor.py ~388, where `_sub` currently
does nothing).

- **Pros:**
  - Fixes the cause for good, at no runtime cost and no latency.
  - Uses code that already exists and is tested.
- **Cons:**
  - Changes camera configuration, so it should need admin (`/api/advisor/apply` is admin in hub roles.py).
  - Not every camera has Media2, and some can't do H.264 on the sub stream at the wanted resolution.
  - The stream reconnects once.
  - Doesn't help an SD path that relays an H.265 main stream.

## Suggested order

1. **C** first: cheapest, and right for most cameras.
2. **A** for cameras that can't change, limited by `sd_max`.
3. **B** only if a customer needs live view on many H.265 cameras at once, or through networks that block WebRTC entirely.

Phase 3 of the live-view plan (codec endpoint plus `canPlayLive`) gives the client what it needs to pick a path
before connecting, so whichever option is built is chosen up front, not after two failures.

## Corrections to docs/competitive-gaps-prompt.md (checked 2026-10-16)

- **Phones don't play every tile** (its Phase 1.3). Only PhoneLive passes `phone`, and it shows one live tile. The
  strip under it is 10 s stills (hub/ui/src/SiteLive.tsx, `SiteTile` / `PhoneLive`).
- **Live viewing is already audited**, too noisily: hub/hub/proxy.py `_audit` writes one generic row per WHEP POST,
  retries included. Direct-LAN viewing isn't audited at all. (WHEP DELETE, added in Phase 1, is not audited.)
- **Line numbers that moved:**
  - The NVR's JSON 404 catch-all (`spa`) is at backend/nvr/api.py ~2675 after Phase 1 (~2638 before), not ~2613.
  - The SD playback transcode is at ~2236-2322.
  - The advisor's `_sub` no-op is at advisor.py ~388.
- **Problems it missed** (fixed in Phase 1 unless noted):
  - A camera with no recording ping-ponged between WebRTC and RecordingFollow every few seconds. It now shows "No
    recording at this time" and retries live every 30 s.
  - WhepPlayer's failure counter never reset, so a disconnect hours later skipped the retry. It now resets once
    frames decode.
  - No WHEP DELETE was sent. Teardown now hangs up the session (`DELETE /api/whep/<path>/<session>`).
  - SD tiles on an H.265 relay stay black: the 8 s "no frames" check has no callback on SD (this doc, and Phase 3).
  - frontend CameraWidget ignored the `_hd` path naming. It now uses `livePath()`.
  - test.ps1 didn't run the hub UI's vitest or typecheck. It does now.
