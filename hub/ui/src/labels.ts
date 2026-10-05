/** Human names for alert kinds (hub/hub/alerts.py KINDS). */
export const KIND_LABEL: Record<string, string> = {
  offline: "Server offline", camera_down: "Camera down", disk: "Disk low", clock: "Clock skew",
  detector_fallback: "Detection on CPU", detector_stalled: "Verification stalled",
  event_high: "High-priority event", event_policy: "Site rule broken", event_watched: "Watched person/vehicle",
};

/**
 * "Site · Server · Camera" for a fan-out result. The Site is left out inside a Site page (the page says it), the
 * server when its Site has only one (one-server Sites are usually named after the box, so it would read twice), and
 * any part equal to the one before it (a Site whose server count is unknown but shares the server's name).
 */
export function whereLabel(p: { site?: string | null; server?: string | null; camera?: string | null }, opts: { showSite?: boolean; serverCount?: number } = {}): string {
  const parts: string[] = [];
  const add = (x: string | null | undefined) => { if (x && x !== parts[parts.length - 1]) parts.push(x); };
  if (opts.showSite !== false) add(p.site);
  if (opts.serverCount !== 1) add(p.server);
  add(p.camera);
  return parts.join(" · ");
}
