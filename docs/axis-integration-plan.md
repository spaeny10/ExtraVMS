# AXIS integration plan

Probe of TSS-DT cam9, "AXIS Test", 192.168.105.115, on 2026-11-06. It used only anonymous requests (VAPIX `basicdeviceinfo` and `apidiscovery`, WS-Discovery, port checks) plus what the NVR had already learned with its stored credentials (ONVIF profiles, events, PTZ, SD card). Nothing on the camera was changed.

## What the camera is

| | |
|---|---|
| Model | AXIS M4327-P Panoramic Camera, 6 MP indoor fisheye (ARTPEC-8, 182° view). Serial B8A44F916D1B |
| Firmware | AXIS OS **11.4.63**, built April 2023: two years old (see Tier 1, item 6) |
| Open ports | 80, 443 (HTTP/2), 554. No SSH or MQTT broker. The camera is an MQTT *client*. |
| On-board analytics (datasheet) | AXIS Object Analytics: humans and vehicles (car, bus, truck, bike); line crossing, object in area, crossline counting, occupancy; up to 10 scenarios. AXIS Scene Metadata, Video Motion Detection, tampering alarm |
| VAPIX APIs | **58**, listed **without a login** by `/axis-cgi/apidiscovery.cgi`. Notable: analytics-metadata-config, event-streaming-over-websocket, event-mqtt-bridge, mqtt-client, recording / recording-export, disk-properties, ptz-control, light-control, io-port-management, supervised-io, network-speaker-pairing, sip, audio-device-control, signed-video, ntp, time-service, fwmgr, systemready, temperaturecontrol, stream-profiles, packagemanager (ACAP) |
| ONVIF media | 24 profiles: H.264 + MJPEG of each view. 2160×2160 overview circle, 2560×1440 ×3, 2368×1184 ×2, 1920×1440, 2560×960 double panorama, 1920×1080 ×4 view areas. **Nothing smaller than 1080p**, so SD gets a 2560×1440 panorama, a different picture from the recorded circle |
| ONVIF events | Subscription works, but every PullMessages failed with 400 InvalidArgs. AXIS names the subscription by a reference parameter that we didn't send. **Fixed in code (ingest.reference_headers), not deployed yet** |
| Object metadata | **None.** The ONVIF profile streams carry no metadata track, so we get no people or vehicles from the camera itself |
| PTZ | Digital PTZ (pan/tilt inside a view area), no presets |
| SD card | Supported, inserted, not recording |
| Outputs | No relays, light or siren on this model |
| Clock | 62 s off: NTP not set |

Why there's no video today: the camera record still has the default paths `/main` and `/sub`, and the AXIS answers 404 on both. The NVR's own probe found the right paths and offers them as a one-click fix in Settings. That fix is not applied automatically.

## How deep we go: three tiers

### Tier 1: every AXIS works on add, with the camera's own detections (about 1–2 weeks)

**Status 2026-11-06: built** (backend/nvr/axis.py, the Camera tab's AXIS section, tests/test_axis.py):
- Items 1–4 and 6 are in.
- Item 5 is the Camera tab's existing Time section (ONVIF), which works for any camera.
- "Set up for Axiom" turns on the camera's scene metadata producer and switches the stream paths. If the camera won't start video and metadata in one session within 20 s, it falls back to video only, so recording never stops.
- Not yet checked on the real camera:
  - The live AXIS Object Analytics event fields. The parser accepts `active` or `State` for on/off, and the class from the event's data or else the scenario's classes.
  - Whether AXIS OS 11.4 sends `analytics=polygon` alongside video.

1. **Recognize AXIS before login.** `basicdeviceinfo getAllUnrestrictedProperties` and `apidiscovery getApiList` both answer anonymously.
   - On add (and on Scan), store brand, model, firmware and the API list. That tells us what the camera can do without trial and error.
   - Scan currently shows AXIS as "needs auth" with no model.
2. **AXIS stream paths, filled in automatically.** Use VAPIX RTSP (`/axis-media/media.amp?camera=1&videocodec=h264&resolution=WxH`) instead of the ONVIF profile URIs:
   - We choose the SD resolution ourselves. For example 1024×1024 of the *same* fisheye circle, not a different panorama.
   - Add `&analytics=polygon` to the main path, so video and object metadata arrive in one session. Our metadata reader already reads the main path's application track. Verify that both work together on this firmware.
   - When the paths are still the defaults and the camera is an AXIS, fill them in when the camera is added instead of waiting for a click.
3. **Turn on AXIS Scene Metadata.** Use `analyticsmetadataconfig.cgi`: `listProducers`, then `setEnabledProducers` with `AnalyticsSceneDescription` (or `VideoMotionTracker`) on channel 1.
   - This changes the camera's configuration, so it gets a button on the Camera tab, like our Milesight clock fix.
   - The frames are ONVIF `tt:Frame` with Human / Vehicle / Bike / HumanFace / LicensePlate classes, which `parse_metadata` and `tracker.CLASS_MAP` already understand. Add `bike` and `humanface`.
   - The metadata also carries clothing colors (`bd:Clothing` tops/bottoms) and vehicle type and color. Store them on the event so Find and Otso can use them without spending a Qwen call: "man in a red top", "white truck".
4. **AXIS Object Analytics scenarios as rule events.** AOA fires ONVIF events per scenario (`tnsaxis:CameraApplicationPlatform/ObjectAnalytics/Device1ScenarioN`).
   - Map them in `ruleevents.TYPE_MAP` the way the Milesight rules are, so the scenario's name is the rule name.
   - Use the scenario's object class (Human / Vehicle) for "Open events for".
   - This rides on the PullMessages fix above.
5. **Set the clock.** Set NTP through the `ntp` / `time-service` API, or ONVIF SetSystemDateAndTime (camdevice already does this for Milesight).
6. **Firmware advisor.** Compare the firmware with the current AXIS OS release (`fwmgr` reports the version) and flag old ones. 11.4 is from 2023. Upgrades stay a human decision.

### Tier 2: what makes us better with AXIS than a generic ONVIF VMS (about 3–5 weeks)

7. **Push events.**
   - **WebSocket:** `wss://<cam>/vapix/ws-data-stream?sources=events`, then `events:configure` with topic filters. It is lower latency than pulling every 10 s, and the server sends only the topics we ask for. Keep ONVIF pull as the fallback.
   - **MQTT event bridge:** the camera *dials out* to a broker. This is the right pattern for CGNAT, Starlink and cellular sites (see Axiom Bridge): point it at the hub, and events arrive with no inbound connection to the site.
8. **Fill recording gaps from the SD card.** `recording` / `recording-export` pull the camera's own recording for a time range.
   - After an outage between the camera and the NVR, the NVR fetches the missing span and fills the timeline.
   - Today's TSS switch outage would *not* have been covered, because the camera lost power as well. A dropped link, a reboot of the NVR, or a WAN cut to the central server would be.
   - SD health (`disk-properties`) goes to the advisor.
9. **Fisheye views.**
   - Offer the M4327-P's view areas (`camera=2..5`, quad, panorama) as extra cameras on the same device (streams.view `sources` already supports this pattern).
   - Use `ptz-control` digital PTZ in Live.
   - Later, dewarp client-side so one recording serves every view.
10. **AXIS speakers and audio for Talk-down.** AXIS network speakers (C1310-E, C1410, C1610) are very common in the US.
    - Add VAPIX audio transmit (`/axis-cgi/audio/transmit.cgi`) as a second path next to our ONVIF backchannel.
    - Use `network-speaker-pairing` to find which speaker belongs to which camera.
    - Use `sip` for SOC voice later.
11. **AXIS deterrence and I/O.**
    - Sirens and strobes (D4100-E, D4200), illuminators and I/O: map `light-control`, `io-port-management` and `supervised-io` into the deterrence manager, as we did for the Milesight CQ8231.
    - Supervised inputs report cut or tampered wiring. Raise those as SOC alarms.
12. **Health in the advisor:**
    - `systemready`
    - `temperaturecontrol`
    - the tampering alarm topic
    - streaming-indicator state
    - stream profiles that drift from what we set

### Tier 3: partner level (ongoing)

13. **Signed video.** AXIS signs the stream (`signed-video`). Verify the signature on export and print it on the evidence report. That gives chain of custody, which customers and police value, and few small VMSs do it.
14. **Our own ACAP.** Same idea as the Milesight push agent: an ACAP app on ARTPEC-8 that dials the hub, carries events and metadata, and runs our checks at the edge. The packagemanager API is present, so installs can be done remotely.
15. **AXIS Technology Integration Partner (TIP) program.** Join to get compatibility listing, test hardware and early firmware. Separately, ONVIF Profile M conformance on our side covers the metadata path for every brand.

## Order I'd build in

1. Deploy the events fix (done in code).
2. Tier 1, items 1–3: recognize AXIS, set its paths automatically, scene metadata. That turns an AXIS from "no video, no detections" into Milesight-level on add.
3. Items 4–6.
4. Items 8 (SD gap fill) and 10 (AXIS speakers), because they feed the SOC and Talk-down work directly.
5. Item 7 (MQTT/WebSocket), together with Axiom Bridge.

## Open questions for Shawn

- May we change camera settings automatically on AXIS cameras? That covers enabling scene metadata, NTP, and the stream profile for SD. Or should each one be a button the installer presses, as on Milesight today?
- Is there an AXIS speaker or siren on hand to test items 10–11?
- Is the TSS AXIS staying, so it can be the test camera? It's a fisheye. A bullet or dome with AOA would cover the most common install too.

Sources: [AXIS M4327-P datasheet](https://axis.com/dam/public/6c/33/bc/datasheet-axis-m4327-p-panoramic-camera-en-US-470462.pdf), [Scene metadata over RTSP](https://developer.axis.com/analytics/axis-scene-metadata/how-to-guides/scene-metadata-over-rtsp/), [AXIS Scene Metadata](https://www.axis.com/en-in/developer-community/scene-metadata-integration), [Event streaming over WebSocket](https://developer.axis.com/vapix/network-video/event-streaming-over-websocket/), [MQTT Event Bridge](https://developer.axis.com/vapix/network-video/mqtt-event-bridge/)
