/**
 * Dispatch: where to send help, in the form an operator reads to police over the phone. The Site's street, city line
 * and coordinates, Copy (all of it as text) and Directions; with `map`, a small fixed map with the pin.
 * `place` is the incident detail's `site` (or the Site payload while that loads).
 */
import { toast } from "@site/ui";
import { SiteMap } from "../map/LazyMap";
import { type Place, directionsHref, dispatchLines, dispatchText, fmtCoord, hasPoint } from "../place";
import { settingsHref } from "../nav";

export function Dispatch({ place, siteId, map = false, className = "" }: { place: Place | null | undefined; siteId: string; map?: boolean; className?: string }) {
  if (!place) return null;
  const lines = dispatchLines(place);
  const located = hasPoint(place);
  const copy = async () => {
    const text = dispatchText(place);
    try { await navigator.clipboard.writeText(text); toast.success("Address copied"); } catch { toast.info(text); }
  };
  return (
    <section className={`soc-dispatch ${map && located ? "with-map" : ""} ${className}`} aria-label="Dispatch address">
      <div className="soc-dispatch-text">
        <div className="row soc-dispatch-head">
          <h3>Dispatch</h3>
          <span className="spacer" />
          {(lines.length > 0 || located) && <button className="ghost small" onClick={copy} title="Copy the address and coordinates">Copy</button>}
          {located && <a className="small" href={directionsHref(place.lat, place.lon)} target="_blank" rel="noopener noreferrer">Directions ↗</a>}
        </div>
        {lines.length > 0 ? (
          <address className="soc-dispatch-addr">{lines.map((l, n) => <div key={n} className={n === 0 ? "lead" : ""}>{l}</div>)}</address>
        ) : (
          <p className="muted small">No address on file. <a href={settingsHref(siteId, "general")} target="_blank" rel="noreferrer">Site settings</a></p>
        )}
        {located ? <div className="small"><span className="muted">GPS</span> <code>{fmtCoord(place.lat, place.lon)}</code></div>
          : lines.length > 0 && <div className="muted small">Not on the map yet.</div>}
      </div>
      {map && located && <SiteMap pins={[{ id: "site", lat: place.lat, lon: place.lon, title: place.name }]} height={160} interactive={false} zoom={15} className="soc-dispatch-map" label={`Map of ${place.name ?? "the site"}`} />}
    </section>
  );
}
