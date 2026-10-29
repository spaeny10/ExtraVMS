# SOC: what's next

The SOC layer (overnight alarm monitoring on the hub: `hub/hub/soc.py`, `soc_api.py`, `soc_reports.py`, the `/soc`
workstation) shipped in stages 1–5 on 2026-10-04. This file holds what is planned next. The first item is new; the
rest is stage 6 as it was left.

## 1. Vehicles at an incident (planned 2026-10-29)

**Goal.** When an operator opens an incident, they see which vehicles came and went around it, with what we already
know about each one. For example:

> Arrived 11:02 PM in a silver Chevrolet Silverado, ABC 1234, coming toward the camera. Seen here 4 times this month,
> always after 10 PM. Left 11:19 PM.

Today an operator has to open the Site's Plates tab and line up times by hand.

**What's already there.** Since 2026-10-29, the traffic camera side of a server has:
- each vehicle's plate (the camera's, or Otso's when it is sure) and Otso's own description (`otso_look`);
- counted notes about a plate (`traffic.plate_notes`);
- plate pattern alarms (circling, new plate, a regular at an unusual time) that already reach the SOC queue as events.

What's missing is linking those to an incident's time window.

**Design**
- **Server.** A new `GET /api/traffic/around?ts=&before_s=600&after_s=300` (viewer). It returns the passes in that
  window on the server's traffic cameras, one per pass, with:
  - the plate (camera's or Otso's) and Otso's description, or else the camera's colour, type and make;
  - direction, camera, read id, time;
  - `plate_notes(...)["notes"]`.

  Counted in code, never by the model, like Ask Otso's notes. Reads that aren't a vehicle are left out. At most 20
  passes, plates first.
- **Sites without a traffic camera.** The same route also returns the window's verified vehicle events that carry a
  plate (`events.plate`, read by Qwen from the footage, plate.py), so a Site with only regular cameras gets what it can.
- **Hub.** When an incident opens (and again when an event is added more than 5 minutes after the last fetch), the hub
  asks every server of the Site through the tunnel. Like Site Ask, it drops a server that hasn't answered within the
  time limit. It keeps the answer on the incident (`incidents.vehicles` JSON, with the time fetched). The workstation
  doesn't wait for this: the card fills in when the answer arrives (an `incident_updated` frame).
- **Workstation.** An incident pane card, "Vehicles around this":
  - one line per vehicle: time, arrived or left (from the direction), plate and description, and the first note;
  - a vehicle's plate and crop pictures through the existing `/api/traffic/reads/{id}/image/{kind}` route on that server;
  - a click opens the read on the Site's Plates tab;
  - "Add to watch list" (operator) and "Ask Otso" stay where they are, on the read.

  A plate pattern alarm in the incident is shown at the top of the card.
- **Otso's one-liner.** The incident's title for operators can take the arriving vehicle when there is exactly one
  ("Person at Gate 2, arrived in a silver Silverado ABC 1234"). The workstation does this in code, not the model.
- **Reports.** A true-alarm incident's report lists the vehicles, the evidence a customer or the police asks for first.
- **Audit.** Viewing the card isn't logged separately: opening the incident already is. Adding a plate to a list is
  audited on the server as today.

**Open questions for Shawn**
- **Window size:** 10 minutes before, 5 after?
- **Customer reports:** should the vehicle list go into customer-facing reports, or stay with operators only?
- **Without a traffic camera:** at a Site with none, is a plate Qwen read from event footage enough to show, or should
  the card stay hidden?

**Tests.** Server: the window, one row per pass, noise left out, notes, the event fallback. Hub: fetch on open and on a
late event, a slow server dropped, the card's data on the incident, roles. UI: the card's lines (pure function, vitest).

## 2. Stage 6 (left from the first plan)
- Speaker / talk-down from the workstation (site-side talk-down shipped separately, see docs/roles.md *Act*).
- Email and SMS to customers.
- The `on_level3` hook (customer contact at escalation level 3).
- A PushCard checkbox for `soc_incident` pushes.
- A hub-side store for operator layouts.
- The first real incident is still to be watched end to end with real motion at an armed Site.
