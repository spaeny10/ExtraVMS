"""Who may do what through the proxy. Roles nest: viewer < operator < admin < owner.

viewer   watch: every GET (but the talk-down voice, a camera's own settings and its deterrence), the live WebSocket, WHEP
         signaling (offer and hang-up), media, playback, frames
operator act: PTZ / relay / presets, talk-down (play / stop a message on a speaker, Hold to Talk, Describe & warn, the voice and its
         preview), deterrence (a camera's strobe, siren, light and relays, and reading what it has), naming people and
         vehicles, reading a vehicle's plate, feedback and synopsis edits, locks, Ask and clip chat, reprocess, briefings on demand
admin    configure: cameras (and their own settings over ONVIF: clock, encoders, imaging, rules, reboot; their automatic
         deterrence rules; a Milesight's white light, warning light, siren volume and one-click disarm), speakers and
         their message library, the talk-down voice, zones, retention, topology, remote AI, backups, site rules, SD card recovery
owner    everything an admin can, plus org members and billing (hub-side only)
Unknown non-GET routes need admin, so a new site endpoint is never accidentally open to viewers.
"""
from __future__ import annotations

import re

ROLES = ["viewer", "operator", "admin", "owner"]
RANK = {r: i for i, r in enumerate(ROLES)}

# (methods, path regex, minimum role) — first match wins
RULES: list[tuple[set[str], re.Pattern, str]] = [
    ({"GET", "HEAD"}, re.compile(r"^/api/talkdown/voices?$"), "operator"),   # the talk-down voice and the installed voices
    # a camera's own settings over ONVIF (its NTP servers, firmware, encoders): operator to read; every write is admin
    # under the /api/cameras rule below (camera time, encoders, imaging, analytics rules, reboot, "Fix clocks")
    ({"GET", "HEAD"}, re.compile(r"^/api/cameras/([^/]+/)?device(/|$)"), "operator"),
    # deterrence (backend deterrence_api.py): what a camera can set off, what runs, its log and rules; operator, as acting is
    ({"GET", "HEAD"}, re.compile(r"^/api/cameras/[^/]+/deterrence(/|$)"), "operator"),
    ({"GET", "HEAD"}, re.compile(r".*"), "viewer"),
    ({"POST"}, re.compile(r"^/api/whep/"), "viewer"),
    ({"DELETE"}, re.compile(r"^/api/whep/[^/]+/[^/]+$"), "viewer"),   # ends the viewer's own live session (WhepPlayer teardown)
    ({"POST"}, re.compile(r"^/api/(assistant/ask|assistant/plan|assistant/retrieve|assistant/warm|remote/warm|query/parse)$"), "viewer"),
    ({"POST"}, re.compile(r"^/api/assistant/execute$"), "operator"),   # site actions; the site checks each verb's own role
    ({"POST"}, re.compile(r"^/api/events/\d+/chat$"), "viewer"),
    ({"POST"}, re.compile(r"^/api/footage/verify$"), "viewer"),
    ({"PUT"}, re.compile(r"^/api/cameras/[^/]+/ptz/config$"), "admin"),                # return-home timer etc.: configuration
    ({"POST", "PUT", "DELETE"}, re.compile(r"^/api/cameras/[^/]+/(ptz|relay)(/|$)"), "operator"),
    # deterrence: strobe / siren / light / relay / stop, the same act as Hold to Talk; its rules (PUT .../deterrence/rules)
    # are configuration: admin under the /api/cameras rule below
    ({"POST"}, re.compile(r"^/api/cameras/[^/]+/deterrence$"), "operator"),
    ({"POST"}, re.compile(r"^/api/speakers/[^/]+/(play|stop|describe|talk)$"), "operator"),   # talk-down on a bound camera's live view; Describe & warn; talk: Hold to Talk, a WebSocket checked as a POST
    ({"POST"}, re.compile(r"^/api/talkdown/preview$"), "operator"),       # a voice sample in the browser, never on a speaker
    ({"PUT"}, re.compile(r"^/api/talkdown/voice$"), "admin"),             # the voice (the hub pushes its Site setting)
    ({"PUT", "POST", "DELETE"}, re.compile(r"^/api/(speakers|talkdown)(/|$)"), "admin"),  # speakers, Check, the message library
    ({"POST", "PUT", "DELETE"}, re.compile(r"^/api/identities(/|$)"), "operator"),
    ({"POST"}, re.compile(r"^/api/events/\d+/plate$"), "operator"),   # Read plate: one Qwen look at a vehicle's plate, like naming it
    ({"PUT", "POST", "DELETE"}, re.compile(r"^/api/events/\d+/(feedback|synopsis|synopsis/generate|synopsis/revert|reprocess|identity|lock|watch)(/|$)"), "operator"),
    ({"PUT", "DELETE"}, re.compile(r"^/api/events/\d+/chat(/|$)"), "operator"),        # keep/forget clip-chat notes
    ({"POST"}, re.compile(r"^/api/links/\d+/reject$"), "operator"),                       # "not the same person"
    ({"POST", "DELETE"}, re.compile(r"^/api/(locks|layouts|dashboards)(/|$)"), "operator"),
    ({"POST"}, re.compile(r"^/api/remote/test$"), "admin"),
    ({"POST"}, re.compile(r"^/api/config/(import|merge)$"), "admin"),
    ({"POST"}, re.compile(r"^/api/config/history(/files)?$"), "admin"),   # tunnel-only on the site; the proxy refuses it anyway
    ({"POST"}, re.compile(r"^/api/advisor/(dismiss|undismiss)$"), "operator"),   # hide / restore a suggestion
    ({"POST"}, re.compile(r"^/api/advisor/apply$"), "admin"),       # changes retention days or a PTZ return-home timer
    ({"PUT"}, re.compile(r"^/api/(layouts|dashboards)/"), "operator"),
    ({"PUT"}, re.compile(r"^/api/find/views$"), "operator"),                      # Find's saved views
    ({"POST"}, re.compile(r"^/api/briefings/generate$"), "operator"),
    ({"DELETE"}, re.compile(r"^/api/assistant/threads/"), "operator"),
    ({"POST"}, re.compile(r"^/api/journeys/\d+/regenerate$"), "operator"),
    ({"POST"}, re.compile(r"^/api/(baseline/rebuild|journeys/relink|backup)$"), "admin"),
    ({"POST"}, re.compile(r"^/api/sd/recover$"), "admin"),   # replays footage from a camera's SD card into its recordings
    ({"PUT", "POST", "DELETE"}, re.compile(r"^/api/cameras(/|$)"), "admin"),
    ({"PUT"}, re.compile(r"^/api/(retention|topology|remote|briefings/settings)(/|$)"), "admin"),
    ({"PUT"}, re.compile(r"^/api/ai-settings$"), "admin"),   # verification and synopsis thresholds
    ({"PUT"}, re.compile(r"^/api/hub$"), "owner"),   # hub-managed; effectively blocked (see proxy)
]


def required_role(method: str, path: str) -> str:
    m = method.upper()
    for methods, rx, role in RULES:
        if m in methods and rx.search(path):
            return role
    return "admin"


def allows(role: str, needed: str) -> bool:
    return RANK.get(role, -1) >= RANK[needed]
