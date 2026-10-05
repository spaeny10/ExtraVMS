/** Pure helpers for invite links (Customer → Invites and the public /invite/<code> page); unit-tested in invites.test.ts. */
import { accessLabel } from "./access";

/**
 * What an invite grants, for people who don't know Site ids: "All sites", the Site names, or "No sites". The preview
 * carries `locations` (names); a pending row carries `location_ids`, named from `sites` (a Site deleted since shows its id).
 */
export function inviteAccessSummary(i: { all_sites: boolean; location_ids?: string[]; locations?: { id: string; name: string }[] }, sites: { id: string; name: string }[] = []): string {
  return accessLabel({ all_sites: i.all_sites, location_ids: i.location_ids ?? (i.locations ?? []).map((l) => l.id) }, [...(i.locations ?? []), ...sites]);
}

/** "in 6 d", "in 5 h", "in 20 min", "expired". */
export function expiresIn(ts: number, now = Date.now() / 1000): string {
  const s = ts - now;
  if (s <= 0) return "expired";
  return s < 3600 ? `in ${Math.max(1, Math.round(s / 60))} min` : s < 172800 ? `in ${Math.round(s / 3600)} h` : `in ${Math.round(s / 86400)} d`;
}

/** The hub builds the link from its PUBLIC_URL; when that isn't configured the link is a bare path, so anchor it here. */
export const absoluteUrl = (url: string, origin: string) => (/^https?:\/\//.test(url) ? url : `${origin}${url.startsWith("/") ? "" : "/"}${url}`);

const message = (e: unknown) => String(e instanceof Error ? e.message : e);
/** HTTP status of an api.ts error ("404 {…}"), or 0 for a network error. */
export const httpStatus = (e: unknown) => Number(/^(?:Error: )?(\d{3}) /.exec(message(e))?.[1] ?? 0);
const detail = (e: unknown) => /"detail"\s*:\s*"([^"]+)"/.exec(message(e))?.[1];
const sentence = (s: string) => `${s.charAt(0).toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? "" : "."}`;

/** A sentence for the accept page. 403 and 422 keep the hub's wording: it names the (masked) address or the rule. */
export function inviteErrorText(e: unknown): string {
  switch (httpStatus(e)) {
    case 404: return "This invite link has expired, was already used, or was revoked. Ask whoever sent it for a new one.";
    case 403: return sentence(detail(e) ?? "this invite is for a different account");
    case 401: return "Wrong email, password or code.";
    case 429: return "Too many attempts from this network. Try again in 15 minutes.";
    case 422: return sentence(detail(e) ?? "check the email and password");
    case 0: return "Can't reach the hub. Check your connection and try again.";
    default: return detail(e) ?? message(e);
  }
}
