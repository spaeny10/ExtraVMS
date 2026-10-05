/** Human names for alert kinds (hub/hub/alerts.py KINDS). */
export const KIND_LABEL: Record<string, string> = {
  offline: "Server offline", camera_down: "Camera down", disk: "Disk low", clock: "Clock skew",
  event_high: "High-priority event", event_policy: "Site rule broken", event_watched: "Watched person/vehicle",
};
