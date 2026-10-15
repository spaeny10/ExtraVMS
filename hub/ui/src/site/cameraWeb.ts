/**
 * Site › Settings › Cameras › Camera web pages (CameraWebCard.tsx): pure helpers, kept apart from the component so they
 * are unit-tested. A camera's own web page opens in a new tab at its own name (https://<label>.cam.axiomvision.ai/,
 * hub camweb.py), through its server's tunnel.
 */
import type { Camera, CameraWebSession, Server } from "../api";

export type WebServer = { id: string; name: string; online: boolean; cameras: Pick<Camera, "camera_id" | "name" | "enabled">[] };

/** The Site's cameras by server, as the card lists them: active servers in the Site's order, cameras by name; cameras gone
 *  from their server are left out (there is nothing to open), disabled ones stay (their page still answers). */
export function webCameras(servers: Pick<Server, "id" | "name" | "online" | "retired_at">[],
  registry: Pick<Camera, "server_id" | "camera_id" | "name" | "enabled" | "missing_since" | "server_online">[]): WebServer[] {
  return servers.filter((s) => !s.retired_at).map((s) => ({
    id: s.id, name: s.name,
    online: registry.find((c) => c.server_id === s.id)?.server_online ?? s.online,
    cameras: registry.filter((c) => c.server_id === s.id && !c.missing_since)
      .map((c) => ({ camera_id: c.camera_id, name: c.name || c.camera_id, enabled: c.enabled }))
      .sort((a, b) => a.name.localeCompare(b.name)),
  })).filter((s) => s.cameras.length > 0);
}

/** The user's open camera pages on this Site's servers, newest first. */
export function siteSessions(sessions: CameraWebSession[], serverIds: string[]): CameraWebSession[] {
  return sessions.filter((s) => serverIds.includes(s.server_id)).sort((a, b) => b.created_at - a.created_at);
}

/** "closes in 24 min" / "closes in under a minute" / "closed". */
export function closesIn(expiresAt: number, now: number): string {
  const s = expiresAt - now;
  if (s <= 0) return "closed";
  if (s < 60) return "closes in under a minute";
  const m = Math.round(s / 60);
  return m >= 60 ? `closes in ${Math.floor(m / 60)} h ${m % 60} min` : `closes in ${m} min`;
}

/**
 * A new tab for the camera page, opened inside the click (popup blockers allow only that) and pointed at the link once the
 * hub has made it. The camera's page gets no handle on the hub's tab (opener cleared): it can't navigate it elsewhere.
 * Null when the browser blocked it (the card then shows the link to click).
 */
export function openBlankTab(open: (url: string, target: string) => Window | null = (u, t) => window.open(u, t)): Window | null {
  const w = open("about:blank", "_blank");
  if (w) {
    try { w.opener = null; } catch { /* some browsers make it read-only: the tab still opens */ }
  }
  return w;
}
