"""Who may do what through the proxy. Roles nest: viewer < operator < admin < owner.

viewer   watch: every GET, the live WebSocket, WHEP signaling, media, playback, frames
operator act: PTZ / relay / presets, naming people and vehicles, feedback and synopsis edits, locks,
         Ask and clip chat, reprocess, briefings on demand
admin    configure: cameras, zones, retention, topology, remote AI, backups, site rules
owner    everything an admin can, plus org members and billing (hub-side only)
Unknown non-GET routes need admin, so a new site endpoint is never accidentally open to viewers.
"""
from __future__ import annotations

import re

ROLES = ["viewer", "operator", "admin", "owner"]
RANK = {r: i for i, r in enumerate(ROLES)}

# (methods, path regex, minimum role) — first match wins
RULES: list[tuple[set[str], re.Pattern, str]] = [
    ({"GET", "HEAD"}, re.compile(r".*"), "viewer"),
    ({"POST"}, re.compile(r"^/api/whep/"), "viewer"),
    ({"POST"}, re.compile(r"^/api/(assistant/ask|assistant/plan|assistant/retrieve|assistant/warm|remote/warm|query/parse)$"), "viewer"),
    ({"POST"}, re.compile(r"^/api/assistant/execute$"), "operator"),   # site actions; the site checks each verb's own role
    ({"POST"}, re.compile(r"^/api/events/\d+/chat$"), "viewer"),
    ({"POST"}, re.compile(r"^/api/footage/verify$"), "viewer"),
    ({"PUT"}, re.compile(r"^/api/cameras/[^/]+/ptz/config$"), "admin"),                # return-home timer etc.: configuration
    ({"POST", "PUT", "DELETE"}, re.compile(r"^/api/cameras/[^/]+/(ptz|relay)(/|$)"), "operator"),
    ({"POST", "PUT", "DELETE"}, re.compile(r"^/api/identities(/|$)"), "operator"),
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
    ({"PUT", "POST", "DELETE"}, re.compile(r"^/api/cameras(/|$)"), "admin"),
    ({"PUT"}, re.compile(r"^/api/(retention|topology|remote|briefings/settings)(/|$)"), "admin"),
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
