/** Human names for alert kinds (hub/hub/alerts.py KINDS). */
export const KIND_LABEL: Record<string, string> = {
  offline: "Server offline", camera_down: "Camera down", disk: "Disk low", clock: "Clock skew",
  detector_fallback: "Detection on CPU", detector_stalled: "Verification stalled", vlm_fallback_active: "AI on fallback model",
  event_high: "High-priority event", event_policy: "Site rule broken", event_watched: "Watched person/vehicle",
  // one alert instead of a camera_down per camera when the server can reach none of the Site's cameras
  site_link_down: "Site link down",
  // the server's database / data disk can't be written: events are not being stored
  storage_failing: "Storage failing",
  // database writes failed with a storage error ("disk I/O error", "disk is full") in the last 15 minutes
  db_errors: "Database write errors",
  // an Axiom Bridge at the Site stopped heartbeating: the cameras behind it are not recorded
  bridge_offline: "Bridge offline",
  // hub administrators only: a central recording host stopped heartbeating
  host_offline: "Central host offline",
  // hub administrators only: the CoverageMap monthly unit budget is used up (automatic coverage refreshes stopped)
  coverage_budget: "Coverage budget reached",
  // hub administrators only: a central host's YOLO verify queue or shared Qwen (vLLM) keeps growing
  host_queue_growing: "Central host queue growing",
  // hub administrators only: a central instance's data area (database, event media) is 90 % full or more
  instance_data_full: "Central data storage full",
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
