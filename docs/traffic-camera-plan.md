# Traffic cameras: plate reads, lists, traffic data

Milesight traffic cameras (TS-series, LPR licensed) read plates and vehicle attributes on the camera and push each read
over HTTP. This plan takes that data into Axiom: every read stored and searchable, plates on events, lists that alert the
SOC, traffic counts, and camera setup from the Camera tab.

## The camera (measured 2026-10-28)

TSS-DT cam8 "Traffic Camera", 192.168.105.62: Milesight **TS4441-X36RPE** (the login page says MS-C4441-X36RPE),
firmware T_61.8.0.5_USA, device name "Traffic Sensing PTZ", 36x PTZ, LPR licence active (United States). No SD card.
Ports 80/443/554. Radar settings exist but report no version (no radar fitted). Its clock was on Pacific; set to Central
(CST6CDT via NTP) with camdevice.set_time.

**API** (operator.cgi, digest auth, `backend/nvr/milesight.py`): about 130 get actions answer (list in the capture
scratchpad). The traffic ones: `get.lpr.settings` (lprEnable, country, attribute switches), `get.lpr.post` (3 HTTP
slots, TCP 3344, MQTT, each with an eventMask), `get.cap.lpr`, `get.lpr.related.info`, `get.vehicle.count.setting`,
`get.line.cross.vehicle.count.setting`, `get.congestion.detection.setting`, `get.violation.management`,
`get.lprwhite/black/visitor.settings`, `get.lpr.evidencecameras`, `get.radar.configuration`, `get.storage.settings`.
A set posts the whole get object back with fields changed (as the camera's own pages do).

**Push eventMask bits:** 1 plate read, 4 attributes, 8 violation, 16 vehicle counting, 32 / 64 / 128 black / white /
visitor list, 256 line-cross counting, 512 congestion.

**ONVIF events:** RuleEngine/LPRDetector/{BlackList, WhiteList, VisitorList, SpeedMonitoring, ReverseDriving, NoPlates,
Attributes} carry only ObjectId / IsAlarm, never the plate. Plate text only comes over the push.

**Push payload** (default JSON template, `v2.get.lpr.post.template`): event_type, device_name, mac_address, sn, time
(camera local time, no zone), detection_region(_name), trigger_line_state, license_plate ("No Plates" when none),
plate_confidence, country_region, plate_type, plate_color, vehicle_type, vehicle_color, vehicle_brand, direction
(Approach / Away / "-"), alarm_type, speed_threshold, speed, data_list {detection_trigger, alarm_input_no.,
attributes_rule}, resolution_width/height, plate box coordinate_x1..y2, vehicle_tracking_box_x1..y2, base64 JPEGs
license_plate_snapshot, vehicle_snapshot, full_snapshot, violation_snapshot, evidence_snapshot0/1 ("-" when absent).
The status report (every statusReportInterval, 300 s) is just `{"device_name": "..."}`. Requests come from curl, with
no auth header unless the slot has a user and password.

## Capture test (2026-10-28, 16:07-16:29, Hwy 54 at SW Diamond Rd)

- 534 reads in 22 minutes (~1,450 per hour), all event_type "Regular", plus 5 status reports exactly 300 s apart.
  Every push arrived within 1 s of the camera's timestamp; none failed. Payloads ~530 KB: full 1080p snapshot ~323 KB,
  vehicle crop ~47 KB, plate crop ~2 KB (too small to check a plate by eye).
- Plates: 307 of 534 reads had plate text, 201 at confidence >= 0.6, 90 at >= 0.8. After re-aiming at rear plates
  (the last 10 minutes), 118 of 177 had plate text and 92 were >= 0.6. Kansas issues **rear plates only**, so a camera
  must see vehicles going away; approaching traffic gives vehicle attributes and counts, not plates.
- Spot checks: 1691AJN (white GMC Sierra) and 0742AGP (black Buick) right, Kansas right; 547344 (white fleet pickup)
  plate right but the camera said Ohio and "Approach" for a rear view.
- Multiple reads of one pass are rare: 307 plate reads came from 289 passes (15 with more than one read, e.g. 671AED /
  3671AED / 387AED). The camera already suppresses repeats for 4 s.
- State (`country_region`) is a weak hint: KS 78, CA 58, OK 48 on a Kansas road. Direction is unreliable. Vehicle type
  and colour are usable (SUV / car / truck = 95 %). Brand is fit for search only: 67 blank, some impossible ("Holden",
  "NIO", "Haval").

## Design decisions

- **Storage:** keep the vehicle crop and plate crop of every read (~50 KB), not the full snapshot: the main stream is
  recorded, so a full frame comes from the recording. At this road's rate the full snapshots alone would be ~11 GB/day.
- **Time:** the camera's local time is converted with the camera's own zone (read from ONVIF, cached); reads whose
  time is more than 10 minutes from the receive time use the receive time.
- **No SD card, no resend:** the camera retransmits only with an SD card, so a read pushed while the server is down is
  lost. Recommend an SD card in every traffic camera; the Camera tab warns without one.
- **Passes:** reads of one camera less than 6 s apart whose plates share at least 4 characters in order are one pass;
  the best read is the pass's plate, the other spellings stay searchable.
- **State and direction are hints:** stored and shown as "reported", never used to reject a plate.
- **Central-hosted Sites:** the camera pushes to an address on its own network; a Site on fred needs the push through
  Axiom Bridge (later).

## Build

**Status: all four phases built 2026-10-28, not yet deployed or committed.**
- Backend: `backend/nvr/traffic.py` (parse, time, passes, events, lists, search, counts, Ask evidence, retention,
  Milesight setup) and `traffic_api.py` (routes). Wiring: `db.py` (vehicle_reads, plate_lists, cameras.traffic),
  `config.py` (traffic_* settings), `api.py`, `pipeline.py`, `policy.py`, `summary.py`, `retrieve.py`, `assistant.py`,
  `siteconfig.py`.
- Hub: `roles.py`, `alerts.py` (traffic_push_silent) and `site_ask.py` (plate evidence).
- UI: the server's Plates page (`frontend/src/Plates.tsx`, `traffic.ts`), Camera › Traffic (`TrafficSetup.tsx`), and
  the hub's Site › Plates tab (`hub/ui/src/SitePlates.tsx`).
- Tests: `backend/tests/test_traffic.py`, using the camera's real get.* answers and a real push, scrubbed, in
  `tests/fixtures/milesight_ts4441`. Frontend: `traffic.test.ts`.

Decided while building:
- **List alarms skip YOLO.** A deny / watch alarm event is created verified: the camera saw the vehicle, and a rejected
  verification must not lose the alarm. `policy.check` keeps a `plate_list` rule when Otso's description runs.
- **Late read or zone change.** A read two hours late looks the same as a camera whose zone moved two hours. Only a
  camera that can resend (SD card and automatic retransmission, recorded at setup) has late reads; otherwise the offset
  is learned again.
- **No secret in backups.** The camera's `traffic` column (with its push secret) is left out of config exports; after a
  restore, set the camera up again.
- **Plate reads in Ask.** Ask (server and hub Site Ask) gets the reads for a question naming a plate, or asking for one
  ("tag of the white truck?"), as their own evidence kind `plate`.
- **Parked vehicles are one pass.** Same type and colour, box IoU >= 0.8, within 30 s. Pointed at a car lot, cam8 re-read
  one car 56 times a minute.
- **Read plate on demand.** The camera's reader misses plates its own crops show (plates ~60 px wide in its 1080p
  frame). An operator can have Otso read the crop; the answer is kept beside the camera's and is searchable.
- **Pictures from the recording.** With "optimal push" on, the camera sent the vehicle picture with fewer reads, then
  with none. The server cuts it from the recording. The offset between a read's time and the recording's timeline is
  steady within a 10-minute recording segment but changes from one segment to the next: each segment's start time is
  the wall clock when MediaMTX opened it. On cam8 it was -0.4 s, +1.5 s, then -0.5 s, and a fixed shift showed empty
  road or the wrong car. So it is learned per segment from reads whose plate crop matches the recording strongly: 0.93
  or more, and at least 2 such matches agreeing within 0.5 s. Right frames scored
  0.97-0.99; one wrong frame scored 0.89, and on its own it would have set the whole run wrong. Reads wait until their
  run's offset is known instead of guessing. The read panel says the picture is from the recording and links to
  the Timeline at that moment.
- **Every recorded frame is matched, starting near the last offset.** At dawn on 2026-10-29 only 2 of 40 vehicles got
  a picture. A passing plate is in its box for one frame (0.98-1.00 there, under 0.75 a frame either side), and the
  search looked every 0.25 s, so it found one only by luck; with little traffic no segment got its 2 matches. The
  camera's clock also drifts, gaining ~0.13 s an hour (the offset -0.5 s at 18:00, -2.2 s by 07:30). Now every frame
  is matched, the frames streamed with only the best one kept. A segment not learned yet is searched first ±1.5 s
  around the nearest learned offset (within 6 hours), and only then wide (fresh reads only). "One moment" means the
  strong frames lie within 0.5 s, since a slow car can match on several frames in a row. The morning's reads: 24 of
  25 placed at -2.14 to -2.26 s, about 1 s each. New reads are cut before the start-up backlog, and an older read
  rests only in proportion to the video it read: behind 253 backfilled reads at 5 s each, new ones went stale.
- **Read plate uses a less strict instruction.** The strict one (never guess) refused every soft but readable plate,
  even with the camera's crop enlarged: 3416AFR, 51121 and 087 MAB all came back legible=false. Read plate is asked
  for by a person, with the camera's crop and read beside the answer, so it uses `plate.ON_REQUEST_SYSTEM`. That reads
  3416AFR and 51121 right and 087 MAB as "087 MAU" (its B half hidden), all at medium. A medium answer shows as
  "Otso: probably ...". Otso's state is kept only at high, because it named a wrong state every time. Background plate
  reads keep the strict instruction.
- **Ask Otso: a plate's history, only when asked.** Read plate became Ask Otso, which reads the plate and adds a few
  notes about it, for example: "Seen 64 times since Nov 3, 2025, 61 of them this year", "Before this, last seen
  yesterday at 8:19 AM, heading away", "Usually on weekdays around 8:15 AM (8:10-8:20 AM), heading away" and "About 5
  times a week over the last 8 weeks". It also flags one plate on different kinds of vehicle, and gives counts per
  camera. The notes appear only when a person clicks the button (Shawn), and are kept with Otso's answer.
  - **Counted, not written by the model** (`traffic.plate_notes`): a model asked for counts makes them up. One sighting
    is one pass.
  - **Habits are not claimed from too little.** A habit needs sightings on 2 or more days, and "on weekdays" needs 5.
  - **Ask gets counted totals for plate questions.** Ask's plate evidence is a sample: the latest 15 reads. Asked "how
    many plates today?", Otso counted the sample and said 16 when there were 1,079. Now every plate question gets a
    counted totals note per camera first (`traffic.ask_totals`: vehicles, those with a plate, different plates, noise
    left out), and a named plate gets its count of passes.
  - **Year totals outlive the 30-day reads.** Before reads are pruned, each sighting's time of day, direction and
    vehicle type is kept per plate and day in `plate_days`, without pictures, for `traffic_history_days` (400).
- **Reads that aren't a vehicle are hidden.** At night cam8 read the car lot's sign as plates ("556BE", up to ~6 a
  minute). The rule: no vehicle type, and no plate or one under 0.7 (`traffic_noise_conf`). Those reads are kept but
  hidden from the reads list (unless "Show reads with no vehicle" is ticked, or in a plate search), from counts and
  totals, and from Ask, and no picture is cut for them. The sign scored 0.3-0.6; the only real plates without a vehicle
  type scored 0.88 and 0.99. The camera couldn't fix this itself: its region already left the sign's plate box out, and
  its confidence level would also drop real night plates, which score 0.34-0.48.

### Phase 1: plate reads in Axiom (site server)
- Push receiver `POST /api/traffic/push/{camera}/{secret}`: per-camera secret, no login (the camera can't log in),
  body limit, status reports mark the camera alive.
- `vehicle_reads` table: time (UTC), camera, plate (normalized) + raw, confidence, state hint, plate colour, vehicle
  type / colour / brand, direction, boxes, pass id, crops on disk, event id.
- Pass merging; the pass's best read links to a vehicle event of that camera at that moment, which then carries the
  plate (plate_source "camera"), so Otso doesn't read plates on cameras that read their own.
- Plate search with near matches (one character off), filters (camera, time, type, colour, list), the reads in Site
  Ask and the hub's search.
- Health: alert `traffic_push_silent` after 15 minutes without a push from a camera that is set up.
- Retention: reads and crops follow the Site's retention.

### Phase 2: lists and alerts
- Allow / deny / watch lists per Site, entries with a note and an optional expiry; exact and near (one character off)
  matches.
- A deny or watch hit makes an Axiom event (kind "plate list") with the crops, so it reaches the hub and the SOC like
  any alarm; a near match is "possible".

### Phase 3: traffic data
- Counts per hour by camera, direction, vehicle type, from the reads themselves (no camera setup).
- The camera's own counting / congestion / violation pushes stored as they come (once lines are drawn on the camera).

### Phase 4: setup from Axiom
- A Traffic section on the Camera tab: plate reading on/off, the push pointed at this server (URL with the secret),
  push health, time zone check, SD card warning.
