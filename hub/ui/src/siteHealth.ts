/**
 * The Site header's one health pill (SitePage HealthPill): "3/3 servers · 13/14 cameras" with a dot that is green when
 * all is well, amber when something is degraded (a camera down, a server whose link to the site's cameras is down,
 * weak cellular at the address) and red when a server is offline; and the count on the Alerts tab. Pure, so it is
 * unit-tested (siteHealth.test.ts); the panel the pill opens lists the details.
 */
import type { DirectState } from "./direct";

export type HealthLevel = "ok" | "warn" | "bad";

export type ServerHealth = {
  id: string; name: string; online: boolean;
  /** how this browser reaches it (direct.ts): straight from the server on its LAN, through the hub, being checked, or
   *  reachable directly once the user accepts its certificate */
  route: DirectState;
  /** "cert" and the user hasn't said "Don't offer again" for it */
  offer: boolean;
  /** the server's heartbeat says every camera is unreachable at once (summary.site_link_down) */
  linkDown?: boolean;
};

export type HealthInput = {
  servers: ServerHealth[];
  serversOnline: number; serversTotal: number;
  camerasOnline: number; camerasTotal: number;
  /** the best carrier's overall CoverageMap score at the address (0–10), null = not known or not shown to this user */
  cellularScore?: number | null;
};

export type SiteHealth = {
  level: HealthLevel;
  /** "3/3 servers · 13/14 cameras" */
  text: string;
  /** the narrow-screen text: the part that matters ("1/3 servers" when one is offline, else "13/14 cameras") */
  short: string;
  /** the one extra word the pill shows when it matters: "link down", "weak cellular" (null: nothing) */
  note: string | null;
  /** what is wrong, worst first, for the tooltip and the screen reader ("1 server offline", "1 camera down") */
  reasons: string[];
  /** servers that could connect directly once their certificate is accepted (still offered) */
  offers: number;
};

/** Below this the cellular at the address counts as weak (coverage.ts scoreClass: under 4 is "poor"). */
export const WEAK_CELLULAR = 4;

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
const ofTotal = (n: number, total: number, word: string) => `${n}/${total} ${word}${total === 1 ? "" : "s"}`;

export function siteHealth(h: HealthInput): SiteHealth {
  const offline = Math.max(0, h.serversTotal - h.serversOnline);
  const down = Math.max(0, h.camerasTotal - h.camerasOnline);
  const linkDown = h.servers.filter((s) => s.online && s.linkDown);
  const weak = h.cellularScore != null && isFinite(h.cellularScore) && h.cellularScore < WEAK_CELLULAR;
  const reasons: string[] = [];
  if (offline) reasons.push(`${plural(offline, "server")} offline`);
  if (linkDown.length) reasons.push(`link to the cameras down at ${linkDown.map((s) => s.name).join(", ")}`);
  if (down) reasons.push(`${plural(down, "camera")} down`);
  if (weak) reasons.push("weak cellular at the address");
  const level: HealthLevel = offline ? "bad" : linkDown.length || down || weak ? "warn" : "ok";
  const servers = h.serversTotal ? ofTotal(h.serversOnline, h.serversTotal, "server") : "No servers";
  const cameras = h.camerasTotal ? ofTotal(h.camerasOnline, h.camerasTotal, "camera") : "no cameras";
  return {
    level,
    text: `${servers} · ${cameras}`,
    short: offline || !h.camerasTotal ? servers : cameras,
    note: linkDown.length ? "link down" : weak ? "weak cellular" : null,
    reasons,
    offers: h.servers.filter((s) => s.online && s.offer).length,
  };
}

/** The pill's accessible name: "Site health: 3/3 servers · 13/14 cameras. 1 camera down. Show details" */
export function healthLabel(s: SiteHealth): string {
  const issues = s.reasons.length ? ` ${s.reasons.map((r) => r[0].toUpperCase() + r.slice(1)).join(". ")}.` : " All well.";
  const offers = s.offers ? ` ${plural(s.offers, "server")} can connect directly.` : "";
  return `Site health: ${s.text}.${issues}${offers} Show details`;
}

/** "N servers can connect directly (faster)" for the panel (null: none offered). */
export const offersText = (n: number) => (n > 0 ? `${plural(n, "server")} can connect directly (faster)` : null);

/** The Alerts tab's count: null when there is nothing open, "999+" past that. */
export function alertBadge(open: number | null | undefined): string | null {
  if (!open || !isFinite(open) || open <= 0) return null;
  return open > 999 ? "999+" : String(Math.floor(open));
}
