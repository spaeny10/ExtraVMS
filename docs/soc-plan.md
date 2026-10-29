# SOC: what's next

The SOC layer (overnight alarm monitoring on the hub: `hub/hub/soc.py`, `soc_api.py`, `soc_reports.py`, the `/soc`
workstation) shipped in stages 1–5 on 2026-10-04. This file holds what is planned next. The first item is new; the
rest is stage 6 as it was left.

## 1. Vehicles at an incident (built 2026-10-29)

**Goal.** When an operator opens an incident, they see which vehicles came and went around it, with what we already
know about each one: "11:02 PM, ABC 1234, silver Chevrolet Silverado pickup truck, toward the camera (reported). Seen
4 times since Oct 3." Before this, an operator had to open the Site's Plates tab and line up times by hand.

**How it works**
- **Server** (`traffic.around`, `GET /api/traffic/around?since=&until=&at=`, viewer, at most 6 hours). Two kinds of
  rows, shown oldest first. At most 20: those with a plate first, then the nearest to `at` (the incident's first event).
  The answer's `total` says how many there were. On cam8's highway that is ~360 in 15 minutes, and the card says
  "20 of 362". Two kinds:
  - **Traffic cameras:** one row per pass. The plate is the camera's, or Otso's when it is sure. The description is
    Otso's, or else the camera's colour, type and make, marked as the camera's guess. Also the direction and the first
    3 of the plate's counted notes (`plate_notes`). Reads that aren't a vehicle are left out.
  - **Plates read from footage:** verified events on other cameras whose plate Qwen read from the footage, marked
    "from footage", with the first sentence of the description and a count of other events with that plate in 30 days.
    Events on traffic cameras are left out: their plate is a traffic read's, already listed. A Site with only regular
    cameras still gets the card.
- **Hub** (`soc_vehicles.py`, `GET /api/soc/incidents/{id}/vehicles`, SOC staff only).
  - **When it asks.** When the card is shown, not when the incident opens, so a vehicle leaving in the 5 minutes after
    still appears. The card asks again every minute while the incident is open, and when an event joins.
  - **Who it asks.** Every server of the Site, through the tunnel. A server that doesn't answer in 10 s, is offline or
    is too old is named on the card.
  - **What it keeps.** The last answer goes on the incident (`incidents.vehicles`, not in queue rows). When no server
    answers, the card shows that answer, marked "as of".
- **Workstation.** The "Vehicles around this" card under the triggering event:
  - the incident's plate alarms first (a list hit, or circling / new plate / unusual time);
  - a summary line ("3 vehicles, 2 with a plate, from 10:52 PM to 11:19 PM", in the Site's time zone);
  - one row per vehicle: its plate crop (or the vehicle, or the event's snapshot), the time, the plate and who read it,
    the description and direction, the notes, and a Timeline link at that moment.

  "Add to watch list" and "Ask Otso" stay on the read, in the Site's Plates tab.
- **Who sees it.** Operators only. It is not in customer-facing reports or pushes. Viewing isn't logged separately,
  because opening the incident already is.

**Changed from the design**
- **Fetched on view, not at open.** The hub asks when the card is shown and keeps the last answer, instead of fetching
  when the incident opens. This is simpler, and the "5 minutes after" window fills in on its own.
- **No change to the incident title.** It was to take the arriving vehicle. Instead the card's summary line says what
  came. Directions say "toward the camera" or "going away" (reported), not "arrived" or "left": a camera can face
  either way.

**Decided with Shawn (2026-10-29)**
- **Window:** 10 minutes before the incident's first event, 5 minutes after its last.
- **Who sees it:** SOC operators and supervisors only, never in customer-facing reports.
- **No traffic camera:** show plates Qwen read from event footage, marked as such.

**Tests.**
- **Server** (`test_traffic.test_vehicles_around_an_incident`): one row per pass, the camera's or Otso's plate and
  description, notes, noise left out, a footage plate (not a traffic read's own event), the 6-hour limit.
- **Hub** (`test_soc_vehicles.py`): the window, the tunnel call, camera names, the kept answer and stale, offline, old
  and slow servers, kept out of queue rows, operators only.
- **UI** (`soc/vehicles.test.ts`): the card's words.

## 2. Stage 6 (left from the first plan)
- Speaker / talk-down from the workstation (site-side talk-down shipped separately, see docs/roles.md *Act*).
- Email and SMS to customers.
- The `on_level3` hook (customer contact at escalation level 3).
- A PushCard checkbox for `soc_incident` pushes.
- A hub-side store for operator layouts.
- The first real incident is still to be watched end to end with real motion at an armed Site.
