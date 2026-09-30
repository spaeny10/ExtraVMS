# Axiom Vision — AI-first NVR

MediaMTX records IP cameras 24/7. The camera's own ONVIF Profile M analytics metadata starts events,
YOLO checks each camera detection against the recorded video, and Qwen2.5-VL writes a synopsis of
every verified event, which you can then search in plain language.

```
camera ──RTSP main (H.265 + ONVIF metadata)──► MediaMTX ──► 24/7 fMP4 on D:\NVR\recordings
   │                                               │  └──► RTSP proxy ──► metadata reader ──► tracker ──► event (open)
   │                                               └──► WebRTC (sub stream) ──► web UI live view
   └──ONVIF PullPoint (intrusion, line crossing, motion...)──► attached to the active tracks
                                                            event closes ──► clip from MediaMTX playback
                                                                         ──► YOLO11 (GPU 0): verified / rejected
                                                                         ──► Qwen2.5-VL via Ollama (GPU 1): synopsis
                                                                         ──► SQLite FTS5 + sqlite-vec search index
```

## Run

```powershell
.\run.ps1          # backend on http://localhost:8080; starts MediaMTX and a dedicated Ollama
```

The first run downloads `yolo11s.pt`, `qwen2.5vl:7b` and `nomic-embed-text`.

Frontend development (hot reload, proxies /api to :8080):

```powershell
cd frontend; npm run dev      # http://localhost:5173
cd frontend; npm run build    # production build served by the backend
```

## Layout

| Path | What |
|---|---|
| `backend/nvr/config.py` | All settings (override with `NVR_*` in `.env`) |
| `backend/nvr/mediamtx.py` | Generates `runtime/mediamtx.yml` from the camera table, supervises MediaMTX, playback API |
| `backend/nvr/ingest.py` | Metadata RTP reader (via MediaMTX) and ONVIF PullPoint event puller, one thread each per camera |
| `backend/nvr/tracker.py` | Camera ObjectId tracks become events; zone filter; rule events attached |
| `backend/nvr/verifier.py` | Clip → frames at track timestamps → YOLO → IoU/centre match with the camera boxes |
| `backend/nvr/synopsis.py` | Dedicated `ollama serve` on :11435 pinned to GPU 1; Qwen JSON synopsis; embeddings |
| `backend/nvr/pipeline.py` | Queues and workers; crash recovery |
| `backend/nvr/retention.py` | Per-camera max age plus free-space floor for recordings; separate retention for event clips |
| `backend/nvr/api.py` | REST + WebSocket API; serves `frontend/dist` |
| `tools/onvif_probe.py` | Camera capability probe (`--listen 60 --ffprobe`) |
| `tools/rtsp_metadata_dump.py` | Prints the camera's Profile M metadata stream |

Storage: recordings on `D:\NVR\recordings`; database, event clips and snapshots in `data/`;
models in `models/` and Ollama's model store.

## Operator feedback loop

- **Correct a synopsis:** Event → Details → *Edit*. The correction replaces the synopsis in the UI and
  search; Qwen's first version is kept (`synopsis_original`) and can be restored.
- **Learn from corrections:** the 3 most recent corrections on a camera go into its synopsis prompt as
  examples.
- **Scene notes** (Cameras → Edit): standing facts about the view ("the trailers are our solar light
  towers; highway traffic is routine"). They're included in every synopsis and chat prompt for that camera.
- **Feedback:** 👍/👎 with reasons on the synopsis; a detection verdict (correct / false alarm / wrong class,
  plus the real class). System tab shows totals; `/api/feedback/export` downloads a JSONL dataset for
  tuning thresholds or later fine-tuning YOLO or Qwen.
- **Ask about this clip:** chat with Qwen about an event. Each question sends 4 frames: spread across the
  event, or around the video playhead with *focus on current moment*. Chat jumps ahead of queued
  synopses on GPU 1. Answers can be saved as notes, which are searchable.
- Qwen runs automatically only for `synopsis_labels` (person); *Generate with Qwen* runs it for any event.

## Retention (AI-first)

- **Continuous window:** every camera keeps `continuous_days` (default 10) of full 24/7 footage.
- **After the window,** each 10-minute segment is *curated*, keeping footage (±30 s) around:
  - people;
  - Qwen-analyzed events (threat level raises priority);
  - camera rule events (intrusion, line crossing…);
  - events with operator feedback;
  - vehicles in detect-only zones;
  - locked ranges.
- **How it's kept:** segments are trimmed losslessly at fragment level (`backend/nvr/fmp4.py`), and the trimmed files stay valid
  MediaMTX segments, so playback, the timeline, scrubbing and previews keep working. Everything else is deleted.
  Segments still being processed (verification or synopsis pending) wait up to 2 extra days.
- **When space runs low:** kept footage stays until the disk falls below `min_free_gb`. Then the lowest-importance, oldest kept
  files go first. **Locked footage is never deleted.** Continuous footage is only deleted as a last resort,
  which raises an alert on the System tab.
- **Configuration:** site policy on System → Retention, per-camera override under Cameras → Edit → Retention. Lock an
  event from its detail view, or Shift+drag a timeline lane to lock a range.
- **Dry run:** `NVR_RETENTION_DRY_RUN=1` logs every decision without deleting anything.
  `GET /api/retention/preview?camera=cam1` shows what the next 24 h of age-outs would keep or delete.

## Camera notes (Milesight MS-C5367-X23PE, firmware 61.8.0.5-r6)

- Object metadata (Human / Vehicle, confidence, box, track ID) is on the `/main` RTSP metadata track.
  Firmware r3 did not send it. `GetSupportedMetadata` still lists no classes; ignore it.
- `/main` is H.265 even though ONVIF reports H.264. `/sub` (H.264 640×480) is used for browser live view.
- Keep the camera on NTP: event times come from the camera clock.

## Ports

8080 UI/API · 8554 RTSP · 8889 WebRTC · 8888 HLS · 9996 playback · 9997 MediaMTX API (localhost) · 11435 Ollama (localhost)
