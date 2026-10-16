/**
 * Live-tile timing from the hub's pages to the hub (POST /api/telemetry/live, hub telemetry.py): the site UI's WhepPlayer
 * hands every report to the reporter registered here (frontend whepTiming.ts). The NVR's own UI registers none.
 */
import { type LiveReport, setLiveReporter } from "@site/whepTiming";
import { sendLiveReport } from "./api";

/** The server id of a hub client's base ("/s/<id>", which the direct client keeps too), or null. */
export function serverOfBase(base: string): string | null {
  return /^\/s\/([A-Za-z0-9_-]{1,64})$/.exec(base)?.[1] ?? null;
}

/** The beacon's body for a report, or null when it names no server of this hub (a client on another base). */
export function liveBody(r: LiveReport): Record<string, unknown> | null {
  const server = serverOfBase(r.base);
  if (!server || r.route === "local") return null;
  return {
    event: r.event, server, camera: r.camera, path: r.path, quality: r.quality, route: r.route, attempt: r.attempt,
    stages: r.stages, ttff_ms: r.ttff_ms, duration_s: r.duration_s, ice_timed_out: r.ice_timed_out,
  };
}

/** Once, at startup (main.tsx). */
export function installLiveTelemetry(): void {
  setLiveReporter((r) => { const b = liveBody(r); if (b) sendLiveReport(b); });
}
