/**
 * Where a Site is: address parts, coordinates and links out to map apps, as pure helpers (the Leaflet map itself is
 * map/SiteMap.tsx, a lazy chunk). The hub geocodes (hub/hub/geocode.py, OpenStreetMap Nominatim) and derives the time
 * zone from the point; the UI only shows and forwards what it got.
 */

/** hub/hub/geocode.py parts_from. Every field may be missing (a rural point has no house number, the sea no city). */
export type AddressParts = {
  house_number?: string | null; street?: string | null; city?: string | null; county?: string | null; state?: string | null;
  state_code?: string | null; postcode?: string | null; country?: string | null; country_code?: string | null; display_name?: string | null;
};
/** One /api/geocode suggestion (or the /api/geocode/reverse answer). */
export type GeoHit = { display: string; address_parts: AddressParts; lat: number; lon: number; timezone: string | null; source?: GeocodeSource };
/** What any Site-like payload carries about its place (Site, SocSite, the incident detail's `site`). */
export type Place = { name?: string; address?: string | null; lat?: number | null; lon?: number | null; address_parts?: AddressParts | null; timezone?: string | null };
/** census = the US Census Bureau geocoder (US street addresses), nominatim/geocoder = OSM or a compatible one */
export type GeocodeSource = "nominatim" | "census" | "geocoder" | "marker" | "manual";

export type MapConfig = { tiles: string; attribution: string };
export const OSM_TILES: MapConfig = {
  tiles: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
};
let mapConfig: MapConfig = OSM_TILES;
/** /auth/me carries the hub's tile setting (HUB_MAP_TILES); api.me stores it here for every map on the page. */
export function setMapConfig(c: Partial<MapConfig> | null | undefined) {
  if (c?.tiles) mapConfig = { tiles: c.tiles, attribution: c.attribution ?? "" };
}
export const getMapConfig = () => mapConfig;

export const hasPoint = (p: Place | null | undefined): p is Place & { lat: number; lon: number } =>
  !!p && typeof p.lat === "number" && typeof p.lon === "number" && Number.isFinite(p.lat) && Number.isFinite(p.lon);

const NUMBER_FIRST = new Set(["us", "ca", "gb", "ie", "au", "nz", "fr", "za", "in", "ph", "sg", "my", "lu", "be"]);
const join = (sep: string, xs: (string | null | undefined)[]) => xs.map((x) => (x ?? "").trim()).filter(Boolean).join(sep);

/** "1200 Main Street" (or "Unter den Linden 77" where the number follows the street). */
export function streetLine(p: AddressParts | null | undefined): string {
  if (!p) return "";
  const cc = (p.country_code ?? "").toLowerCase();
  return NUMBER_FIRST.has(cc) || !cc ? join(" ", [p.house_number, p.street]) : join(" ", [p.street, p.house_number]);
}

/** "Augusta, KS 67010" — the line under the street. US/CA/AU use the state's short code when the hub has one. */
export function cityLine(p: AddressParts | null | undefined): string {
  if (!p) return "";
  const cc = (p.country_code ?? "").toLowerCase();
  const region = ["us", "ca", "au"].includes(cc) && p.state_code ? p.state_code : p.state;
  return join(", ", [p.city, join(" ", [region, p.postcode])]);
}

/** One line from parts, as the hub formats it (geocode.format_address). */
export function formatAddress(p: AddressParts | null | undefined): string {
  if (!p) return "";
  const out = join(", ", [streetLine(p), cityLine(p), p.country]);
  return (out || p.display_name || "").slice(0, 200);
}

/** City and state after a Site's name in lists ("Augusta, KS"); "" when unknown. */
export function cityState(p: AddressParts | null | undefined): string {
  if (!p) return "";
  const cc = (p.country_code ?? "").toLowerCase();
  return join(", ", [p.city, ["us", "ca", "au"].includes(cc) && p.state_code ? p.state_code : p.state]);
}

/**
 * What an operator reads to police: the street, the city line, the country when it is not the obvious one, then the
 * point. Falls back to the typed one-line address when the Site has no parts.
 */
export function dispatchLines(p: Place): string[] {
  const parts = p.address_parts;
  const street = streetLine(parts);
  const city = cityLine(parts);
  if (!street && !city) return p.address ? [p.address] : [];
  return [street || (p.address ?? "").split(",")[0], city, (parts?.country_code ?? "").toUpperCase() === "US" ? "" : parts?.country ?? ""].filter(Boolean);
}

export const fmtCoord = (lat: number, lon: number) => `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
export const googleMapsHref = (lat: number, lon: number) => `https://www.google.com/maps/search/?api=1&query=${lat.toFixed(6)},${lon.toFixed(6)}`;
export const appleMapsHref = (lat: number, lon: number, label?: string) =>
  `https://maps.apple.com/?ll=${lat.toFixed(6)},${lon.toFixed(6)}&q=${encodeURIComponent(label || "Site")}`;
export const directionsHref = (lat: number, lon: number) => `https://www.google.com/maps/dir/?api=1&destination=${lat.toFixed(6)},${lon.toFixed(6)}`;

/** The Site settings form's place fields. `autoTz` = the time zone the last pick/drag put there (so a later one may replace it). */
export type PlaceForm = {
  address: string; timezone: string; lat: number | null; lon: number | null; address_parts: AddressParts | null;
  source: GeocodeSource | null; autoTz: string | null;
};

/** May a geocode replace the time zone field? Only when it is empty or still what the previous geocode set. */
export const tzReplaceable = (f: Pick<PlaceForm, "timezone" | "autoTz">) => !f.timezone.trim() || (!!f.autoTz && f.timezone === f.autoTz);

/** A picked suggestion fills the address, its parts, the point and (when replaceable) the time zone. */
export function applyHit(f: PlaceForm, hit: GeoHit, source: GeocodeSource = hit.source ?? "nominatim"): { form: PlaceForm; tzSet: boolean } {
  const replace = !!hit.timezone && tzReplaceable(f);
  return {
    form: {
      ...f, address: (hit.display || formatAddress(hit.address_parts)).slice(0, 200), address_parts: hit.address_parts,
      lat: hit.lat, lon: hit.lon, source, ...(replace ? { timezone: hit.timezone!, autoTz: hit.timezone } : {}),
    },
    tzSet: replace && hit.timezone !== f.timezone,
  };
}

/** A dragged marker moves the point (the address text stays) and, the same way, the time zone. */
export function applyDrag(f: PlaceForm, lat: number, lon: number, timezone: string | null): { form: PlaceForm; tzSet: boolean } {
  const replace = !!timezone && tzReplaceable(f);
  return {
    form: { ...f, lat: round7(lat), lon: round7(lon), source: "marker", ...(replace ? { timezone: timezone!, autoTz: timezone } : {}) },
    tzSet: replace && timezone !== f.timezone,
  };
}
const round7 = (x: number) => Math.round(x * 1e7) / 1e7;

/** The PATCH body's place fields: only what changed from the saved Site. */
export function placePatch(f: PlaceForm, saved: Place): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (f.lat !== (saved.lat ?? null) || f.lon !== (saved.lon ?? null)) {
    out.lat = f.lat;
    out.lon = f.lon;
    if (f.lat != null && f.source) out.geocode_source = f.source;
  }
  if (f.lat != null && JSON.stringify(f.address_parts ?? null) !== JSON.stringify(saved.address_parts ?? null)) out.address_parts = f.address_parts;
  return out;
}

/** Pin status on the Sites map: alerts beat offline beat online; a Site with no servers is "empty". */
export type PinStatus = "alerts" | "offline" | "online" | "empty";
export function pinStatus(s: { open_alerts: number; servers_total: number; servers_online: number }): PinStatus {
  if (s.open_alerts > 0) return "alerts";
  if (s.servers_total === 0) return "empty";
  return s.servers_online < s.servers_total ? "offline" : "online";
}
/** CSS color per status (the toolkit's tokens, with fallbacks for Leaflet's detached DOM). */
export const PIN_COLOUR: Record<PinStatus, string> = {
  alerts: "var(--bad, #ef4444)", offline: "var(--vehicle, #e0a020)", online: "var(--ok, #22c55e)", empty: "var(--muted, #8b96a3)",
};
export const pinColour = (s: { open_alerts: number; servers_total: number; servers_online: number }) => PIN_COLOUR[pinStatus(s)];

/** The Dispatch block as plain text (Copy): name, address lines, the point, for pasting into a CAD or a chat to police. */
export function dispatchText(p: Place): string {
  const lines = [p.name ?? "", ...dispatchLines(p)];
  if (hasPoint(p)) lines.push(`GPS ${fmtCoord(p.lat, p.lon)}`);
  return lines.filter(Boolean).join("\n");
}
