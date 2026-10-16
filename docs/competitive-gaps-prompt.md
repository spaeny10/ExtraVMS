# Competitive gaps: implementation prompt

Paste the prompt below into a Claude Code session opened at the repo root.

Background: in October 2026 we compared Axiom's live view with Spot AI, VideoLoft and Frigate, using REA browser captures and Chrome DevTools recordings. Findings:

- Spot AI uses LiveKit WebRTC on its appliance. It has adaptive quality, stays at 854×480 even in single-camera view, takes roughly 6–7 s to the first frame, keeps camera stills in cloud storage, and audits live viewing.
- VideoLoft uses low-latency HLS (hls.js loaded from a CDN with no integrity hash) with 1–3 s of latency.
- Frigate uses go2rtc, sending fMP4 over WebSocket to the browser's Media Source API (MSE).

The file:line references below came from a read-only survey of this repo. Verify them before editing.

```text
You're working on Axiom Vision. Repo layout:
- hub/: the cloud hub. Python (uvicorn), behind Caddy; React UI in hub/ui.
- backend/: the on-site NVR. Python; runs MediaMTX, writing its config from backend/nvr/mediamtx.py.
- frontend/: shared React components (WhepPlayer, LivePlayer, ui.tsx).
Live view is WebRTC over WHEP. Recordings are fMP4 served from MediaMTX's playback server via /api/playback. The hub reaches each site's API through /s/{site_id}/api/... over the agent tunnel (hub/hub/proxy.py).

Goal: close the gaps found in a competitive teardown (Spot AI, VideoLoft, Frigate) so Axiom is clearly the best on live-view speed, reliability and quality. Work in the phases below, in order. Each phase is its own commit with tests. At the end of each phase, stop and give a short report: what changed, test results, anything uncertain.

Ground rules
- The file:line references below came from a read-only survey. Verify each one before editing; if the code differs, follow the code and say so.
- Keep the security posture. Production CSP is: script-src 'self'; worker-src 'self'; media-src 'self' blob: https:. No third-party scripts or CDNs; bundle every dependency.
- Run .\test.ps1 (backend test_*.py scripts, frontend vitest and build, hub pytest, hub/ui build) after each phase. Add or extend tests for every behaviour change. Don't skip or weaken existing tests.
- Don't change auth, roles, or recording and retention behaviour unless a phase requires it.

PHASE 1: Quick fixes
1. The hub catch-all returns HTML for API paths. The SPA fallback @app.get("/{full_path:path}") in hub/hub/api.py (~2394-2405) serves index.html with status 200 for unknown /api/* routes. Return a JSON 404 for any path starting with api/, matching what backend/nvr/api.py (~2613-2615) already does. Add a hub pytest for it.
2. OfflineBanner probes the wrong endpoint. In frontend/src/ui.tsx (~221) it fetches `${BASE}/api/system`; on hub pages BASE is "", so it hits the hub, which has no /api/system. Give the hub a cheap health endpoint (e.g. /api/health returning JSON), or skip the probe on hub pages. Also, only treat the probe as healthy if the response is JSON.
3. Phones ignore the live-tile limit. In SiteLive.tsx and dashboard/Dashboard.tsx, `phone || budget.playing` lets phones play every tile. Phones should get a lower limit (start at 4, as a named constant), not none.
4. Playback and frame 404s. When /api/playback or /api/frame returns 404 ("no recording for that range"), the UI should show a clear "No recording at this time" state instead of retrying. Check LivePlayer.tsx (RecordingFollow gives up after 3 errors) and the tile still/poster code.

PHASE 2: Measure time to first frame (TTFF)
In frontend/src/WhepPlayer.tsx, record per session: offer created, WHEP POST sent, answer received, ICE connected, first decoded frame (use requestVideoFrameCallback where available, else poll getStats framesDecoded > 0). Also record the path used: direct LAN (hub/ui/src/direct.ts) or through the hub tunnel. Report it all to a lightweight NVR or hub endpoint (keep a small rolling buffer and expose p50/p95 per site in an admin view or API). No third-party analytics. Add a vitest for the timing state machine.

PHASE 3: Automatic SD/HD switching
Today SD/HD is manual only (SiteLive.tsx ~193-206, ~673-674). Competitors either adapt automatically (Spot uses LiveKit) or stay at 480p.
- Switch a tile to <id>_hd automatically when it's focused, full-screen, or rendered larger than about 720 px wide, and getStats shows a healthy link (low packet loss, no freezes, few dropped frames).
- Drop back to SD when loss, freezes or dropped frames cross thresholds. Add hysteresis and a cooldown so it doesn't flap.
- Count HD tiles against a separate, smaller limit.
- A manual SD/HD choice overrides automatic switching for that tile until the page reloads.
- Make the switch seamless: start the new WHEP session, swap the stream when its first frame decodes, then close the old one.
- Unit-test the decision logic with synthetic stats.

PHASE 4: H.265 / codec resilience (the biggest gap)
Problem: live view is never transcoded. When a stream is H.265 and the browser can't negotiate HEVC over WebRTC, MediaMTX returns a codec-related 400. WhepPlayer only calls onUnsupported if one is wired, and SiteLive.tsx (~174) wires it only for HD tiles. SD tiles retry twice, then fall back to RecordingFollow, which runs seconds behind live.
1. Choose the transport before connecting instead of failing twice. Add an NVR endpoint returning each path's track codecs, read from MediaMTX's control API (paths/get). The client compares that with its own capability (RTCRtpReceiver.getCapabilities('video') for H.265 over WebRTC, and MediaSource.isTypeSupported for hvc1/hev1 over MSE).
2. Fallback chain: WebRTC (WHEP) → low-latency HLS with fMP4 through MSE → on-demand H.264 transcode → stills.
   - Enable MediaMTX low-latency HLS in fMP4 (currently "hls": False in backend/nvr/mediamtx.py ~248). Keep it bound to localhost, proxy it through the NVR API with the same auth as WHEP, and make it work through the hub tunnel and over direct LAN.
   - Bundle hls.js (pinned version) through Vite; no CDN. hls.js workers use blob: URLs, which our CSP's worker-src 'self' blocks. Either set enableWorker: false, or make a deliberate, minimal CSP change, and explain which you chose.
   - Last resort before stills: an H.264 live transcode for the sub stream, reusing the existing ffmpeg/nvenc setup and the concurrent-transcode limits from the q=sd playback path (backend/nvr/api.py ~2213-2292).
3. Wire onUnsupported for SD tiles as well as HD.
4. Have the advisor (advisor.py ~376) flag cameras whose sub stream is H.265 and offer a one-click switch to H.264 over ONVIF, where the camera supports it.
Tests: config generation (test_mediamtx_config_write.py), codec-detection endpoint, transport-selection logic (vitest), auth on the HLS proxy route.

PHASE 5: Hub-cached stills and push updates
1. The site agent pushes the latest JPEG for each camera to the hub every 30-60 s (configurable) and on events. The hub stores only the latest one per camera and serves it with sensible cache headers. Use it for grid posters and site lists, and when a site is offline (Spot does this with cloud stills; we currently 404 through the tunnel). Respect existing roles.
2. Replace the 15 s polling of /api/locations/{id} (hub/ui/src/SitePage.tsx ~39-45, plus SitesPage, HomePage, AlertsPage and AllSites) with push updates over /api/fleet/ws, which already sends site_online/site_offline. Add camera-status changes. Keep a slow fallback poll (about 2 min) for when the socket is down.

PHASE 6: Audit live viewing
Check whether live-view sessions are already audited. If not, record them (user, org, site, camera, start, end/duration, path direct/tunnel, transport webrtc/hls/transcode) in the existing audit system (/api/audit, /api/hub/audit), visible in the audit UI. Batch writes so viewing a grid doesn't flood the log.

Optional, if time allows: raise MAX_LIVE (currently 9) dynamically for SD sub-streams when the client keeps up (low dropped frames and decode time), or prototype a server-side mosaic stream (one composed stream for N cameras) for large walls. Give a recommendation with numbers before building either.

When all phases are done, summarize the before/after TTFF numbers and list any follow-ups.
```
