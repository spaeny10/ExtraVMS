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

## PPE compliance (hard hat / hi-vis vest)

- **Set up:** Cameras → Zones → *PPE required*: paint the area (a yard, a job-site gate) and tick what it requires (hard hat,
  hi-vis vest). Optional: *after N s* (default 5) is how long a person must stay inside before they are checked. PPE zones never
  filter detections. Nothing extra runs on cameras without one.
- **How it checks** (`backend/nvr/ppe.py`): after YOLO verifies a person whose feet stayed in the zone for the dwell time, up to 4
  recorded frames from 3 s after they walked in go through a PPE detector (`models/ppe_yolov8s.pt`, YOLOv8s, Apache-2.0, from
  Hugging Face `killuminati1/construction-ppe-yolov8`). Each item is *worn*, *missing* or *unclear*. Only when an item is missing
  or unclear does Qwen look at crops of the person (task `ppe`, a strict "a cap is not a hard hat" prompt) and decide it.
- **A violation** becomes a broken site rule (medium priority): the event card shows 🦺 *No hard hat*, the marked frame becomes
  the snapshot, and it appears under Needs attention, in the digest, hub alerts and push. Search finds it ("no hard hat",
  "ppe violation"); the synopsis states it. Event → Details → Verification shows the detector's and Qwen's answers.
- **Settings:** `NVR_PPE_MODEL`, `NVR_PPE_CONF` (0.4), `NVR_PPE_MIN_DWELL_S` (5), `NVR_PPE_GRACE_S` (3), `NVR_PPE_VLM_CONFIRM`
  (on), `NVR_PPE_VLM_ALL` (off: when on, Qwen checks every person in the zone, ~1 s each, and also catches caps the detector
  takes for hard hats).
- **Measured** on 153 people in 44 public construction photos: detector alone ~90% correct per item; detector + Qwen on doubt
  93-94% with almost no false alarms (hat 0.97 / vest 1.0 precision for "missing"); Qwen on everyone 96-97%. The detector's
  typical mistakes are caps and beanies read as hard hats and plain orange overalls read as vests.
- The check runs on new events only; painting a zone does not re-check past footage.

## Hailo-8 servers

A lite site (no NVIDIA GPU) with a Hailo-8 M.2/PCIe accelerator runs YOLO verification on the Hailo; PPE, CLIP and
re-ID stay on torch on the CPU (`settings.torch_device`).

```bash
bash /opt/nvr/tools/deploy_site.sh --hailo          # new site: --cpu plus the Hailo
bash /opt/nvr/tools/hailo_setup.sh --env && systemctl restart nvr   # existing site
```

`tools/hailo_setup.sh` (root, Ubuntu 24.04, idempotent, public sources only, no Hailo login) builds the `hailo_pci`
driver with DKMS for every installed kernel (6.17 and 7.0 tested), installs the firmware, builds HailoRT 4.24.0 and
`hailortcli` into `/usr/local` and the `hailo_platform` Python bindings into the venv, downloads the Model Zoo v2.19.0
`yolov11s.hef` (Hailo-8, COCO, NMS in the HEF) to `models/`, and with `--env` sets `NVR_YOLO_DEVICE=hailo`,
`NVR_YOLO_MODEL=yolov11s.hef`. HailoRT 4.24 is the last line for Hailo-8 (5.x is Hailo-10/15 only); a HEF must come
from the Model Zoo release that matches it.

- `nvr/hailo.py` `HailoYOLO` answers `.names` and `.predict(...)` like ultralytics: letterbox to the HEF's 640x640,
  one frame at a time on a device that stays open, the NMS output parsed and mapped back to the original frame.
- Settings → System shows `YOLO · yolov11s.hef on hailo-8` and the median ms per frame; Optimize my system flags a CPU
  site whose YOLO is slow (and suggests the Hailo).
- The service holds the device: stop `nvr` before `hailortcli run` or another HailoRT program.
- **Measured** on hailo-t1 (Ryzen 5 3501U, Hailo-8 M.2), 6 frames of a 1080p clip: yolov11s on the Hailo 22 ms a frame
  (28 ms from 4K; 10 ms of it on the device), yolov8s 17 ms; on the CPU yolo11n 55-62 ms, yolo11s 150 ms. A whole
  6-frame `verify()` (decode included) 1.4 s on the Hailo vs 2.3 s with CPU yolo11n. Boxes match CPU yolo11s to ~0.005.

## Hub: Customers, Sites, Servers, Cameras

The hub (`hub/`, https://hub.axiomvision.ai) groups everything as **Customer › Site › Server › Camera**:

- **Customer**: a company with its own users (code and API: `org`). A user has one role per customer
  (owner, admin, operator, viewer) and picks the customer in the header.
- **Site**: a physical place, e.g. "Austin HQ" (code: `locations`, `/api/locations/...`; UI: `/sites/<id>`). A Site
  holds one or more servers and shows them as one: a combined Live grid, Timeline, Find and Alerts across its servers.
- **Server**: one NVR box running this repo, with its own device token, tunnel and console at `/s/<server id>/`. For
  historical reasons the hub's table, its `/api/sites/{id}` routes (alias `/api/servers/{id}`) and every `site_id` in
  the agent protocol, dashboards and camera groups mean a *server*.
- **Camera**: a camera on one server; the hub keeps a registry of them from the servers' heartbeats.

**Who sees what**: each member has either **All sites** (every Site of the customer, including ones added later) or
an explicit list of Sites; an empty list means nothing, never "everything". Set it under Customer → Members. An admin
can only grant Sites they can see themselves.

**Invites**: Customer → Invites makes a link with a role and All sites or a Site list (optionally locked to one email,
with a label and an expiry). The hub emails nothing: copy the link and send it. Opening `/invite/<code>` signs the
person in, or creates their account, and adds them; accepting never narrows an existing member's role or Sites.

**Where things are**: Sites lists every Site with its servers and rollups; a Site's tabs are Live, Timeline, Find,
Alerts, Servers and Settings. Customer → Sites creates, renames and deletes Sites; Customer → Servers enrols servers
("Add server" with the claim code from the server's Settings → System → Cloud hub), retires and restores them; a
server's panel (Site → Servers → the server) moves it to another Site. Customer → Actions (`/customer/actions`) is the
Fleet actions reference page (below). Deployment and upgrades: `hub/DEPLOY.md`.

## Fleet actions

The hub's Find page Ask box also takes instructions. Type one instead of a question and a confirmation card appears;
nothing happens until Confirm. **Customer → Actions** (`/customer/actions`; old `/org/actions` links redirect; also
linked from the Ask box as "What can I ask the hub to do?") lists every instruction with examples, what moves and what
stays, the safety rules and the last 50 actions with Undo. That page, the planner's prompt and JSON schema, and the card's options are all generated
from one registry, `VERBS` in `hub/hub/fleet_actions.py` (served at `GET /api/orgs/{org}/actions/reference`), so they
cannot drift; a test parses every example on the page.

| Verb | Example | Who |
| --- | --- | --- |
| move_cameras | "Move the front door camera from Ironsight to Qwenbot" | admin |
| migrate_site | "Migrate Ironsight to Hailo T1" (type the site name) | admin |
| retire_site | "Retire Ironsight" (type the site name) | admin |
| set_retention | "Set Qwenbot to 7 days of recording" | admin |
| rename_camera | "Rename cam3 on Hailo T1 to Loading Dock" | admin |
| add_camera | "Add 192.168.105.19 to Hailo T1 as Front Door" | admin |
| set_synopsis_labels | "Stop describing vehicles on cam2" | admin |
| lock_footage | "Lock Side Yard footage 3-4 pm today" | operator |
| quiet_alerts | "Quiet alerts tonight", "Mute alerts at Hailo T1 for 2 hours" | admin |

- **Reading the text**: questions ("how many people today?") are never actions and go to the sites as before. An
  instruction goes to the shared AI with a strict JSON schema and the org's real site and camera names, or to a rule
  parser for every verb when the shared AI is not configured. Names are matched on the hub. A name that doesn't
  match, an unclear reading, or a site that is offline puts a question or blocker on the card and leaves Confirm
  disabled. Times ("3-4 pm today", "until 7am") are read in the site's time zone (`tz_offset_s` in `/api/system`).
- **The card** lists what moves, what stays, **capacity after** (the destination's total Mbps; about how many days of
  continuous footage fit: free disk plus its continuous footage, minus the free-space floor, at that rate, against
  its retention policy; detection device, current YOLO ms per frame and camera count) and warnings (over 60 Mbps, CPU
  detection with more than 4 cameras, fewer days than the policy, a camera already at that address, a camera offline
  at the source). Ticks: **Copy event history** (on for migrate, off for move) and **Skip the stream check**. Migrate
  and retire need the source site's name typed (`confirm_name`; a mismatch is a 400). add_camera has a password field
  (plus optional user, paths, ports): the password is sent only in the Confirm call, forwarded to the site's
  `PUT /api/cameras/{id}` and never stored, logged or audited.
- **Moving cameras**: the source hands the cameras over with their passwords and what it learned about them
  (`GET /api/config/handoff`, served only down its tunnel with `x-hub-internal: handoff`): the "what's normal"
  baseline per label and hour, parked-spot memory (`parked:<cam>`) and operator synopsis corrections. Named people
  and vehicles travel with their re-ID / vehicle fingerprints. The destination's `POST /api/config/merge` adds the
  cameras and seeds that state without replacing its own (its baseline wins once it has more days; parked spots and
  corrections are added, kept in settings `baseline_seeds` and `correction_seed:<cam>`). The hub then polls the
  destination's `/api/cameras` until MediaMTX has every stream (up to 60 s). If one never comes up, the merged cameras
  are removed again (`DELETE /api/cameras/{id}?purge=true`), the source is untouched, and the card says e.g. "Hailo T1
  could not reach 192.168.105.19 within 60 s: check VLAN/firewall". Only then does the source disable them (never
  delete: events reference the camera).
- **Event history** (when ticked): 200 events at a time through the tunnel (`GET/POST /api/config/history`, tunnel
  only), with new ids, the camera id remapped and an `events.migrated_from` marker; snapshots and crops follow
  (`POST /api/config/history/files`); clips never do. The destination indexes them for Find in the background. The
  history also stays at the source.
- **References follow the camera**: hub dashboard widgets and event-feed camera lists, camera groups, open event
  alerts (when their event was copied; camera-down alerts are closed since the stream is proven), and the source's
  saved Find views for that camera (added to the destination's views). The result lines say what was updated.
- **Migrate** moves every enabled camera, then **retires** the source: hidden from Fleet, Home, Find, Ask, the digest
  and alerts. Sites → "Show retired" lists it, its page still opens, Customer → Servers → Restore brings it back.
- **Undo**: every executed action writes one Audit row ("fleet action: ...") with its outcome and a reverse plan
  (`detail.reverse`). For 24 hours the result lines, the Audit page and the Fleet actions page offer **Undo**
  (`POST /api/orgs/{org}/actions/undo/{audit_id}`): move the cameras back, restore the site, the previous retention,
  name or labels, remove the added camera or the lock, turn alerts back on. Copied history stays where it was copied.
- **Quiet alerts** keeps event alerts (high priority, broken rules, watched people) and their push notifications
  from opening until the given time (hub `kv` entry `alerts_mute:<org>`); health alerts still open.
- API: `POST /api/orgs/{org}/actions/plan {"text"}` returns `{"action":"none"}` or a plan with its card (no side
  effects; plans last 10 minutes). `POST /api/orgs/{org}/actions/execute {"plan_id", "confirm_name"?, "options"?,
  "camera"?}`. Instead of a `plan_id` you can send `{"plan": {"action", "source_site", "target_site", "cameras", "days",
  "new_name", "host", "labels", "label_mode", "time_from", "time_to", "day", "until", "copy_history"}}`.
- Camera stream settings (fps, bitrate) are not pushed to cameras: there is no ONVIF endpoint for that yet.

**On one site**: the site's own Find → Ask takes rename_camera, set_retention, set_synopsis_labels and lock_footage
("Rename cam3 to Loading Dock", "Set retention to 7 days", "Stop describing vehicles on cam2", "Lock Side Yard footage
3-4 pm today") with the same card (`frontend/src/ActionCard.tsx`, which the hub UI imports as `@site/ActionCard`),
through `POST /api/assistant/plan` and `/api/assistant/execute` (`backend/nvr/site_actions.py`, rules only). Through
the hub, lock_footage needs operator and the others admin, as the matching site endpoints do.

## Camera notes (Milesight MS-C5367-X23PE, firmware 61.8.0.5-r6)

- Object metadata (Human / Vehicle, confidence, box, track ID) is on the `/main` RTSP metadata track.
  Firmware r3 did not send it. `GetSupportedMetadata` still lists no classes; ignore it.
- `/main` is H.265 even though ONVIF reports H.264. `/sub` (H.264 640×480) is used for browser live view.
- Keep the camera on NTP: event times come from the camera clock.

## Ports

8080 UI/API · 8554 RTSP · 8889 WebRTC · 8888 HLS · 9996 playback · 9997 MediaMTX API (localhost) · 11435 Ollama (localhost)
