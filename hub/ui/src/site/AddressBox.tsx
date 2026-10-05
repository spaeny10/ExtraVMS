/**
 * Site › Settings › General: the address as a lookup. Typing (4+ characters, 400 ms pause) asks the hub's geocoder for
 * suggestions; picking one fills the one-line address, its parts, the point and, when the time zone field is empty or
 * still what the previous lookup put there, the time zone. "Locate" looks up the text as typed. Below, a map with a
 * draggable pin (moving it changes the point and the time zone the same way, with Undo), the coordinates with a copy
 * button, and links out to Google and Apple Maps. Saving is the Settings card's Save.
 */
import { useEffect, useRef, useState } from "react";
import { toast } from "@site/ui";
import { api } from "../api";
import { SiteMap } from "../map/LazyMap";
import { type GeoHit, type PlaceForm, appleMapsHref, applyDrag, applyHit, fmtCoord, googleMapsHref } from "../place";

const MIN_CHARS = 4;
const DEBOUNCE_MS = 400;

export function AddressBox({ f, setF, editable, name }: { f: PlaceForm; setF: (f: PlaceForm) => void; editable: boolean; name: string }) {
  const [hits, setHits] = useState<GeoHit[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [busy, setBusy] = useState(false);
  const [typed, setTyped] = useState(false);       // suggestions only follow what the person types, not a picked address
  const [tzNote, setTzNote] = useState<string | null>(null);
  const [undo, setUndo] = useState<PlaceForm | null>(null);   // the form before the pin was first dragged away from the address
  const seq = useRef(0);
  const fRef = useRef(f);
  fRef.current = f;

  useEffect(() => {
    if (!typed || f.address.trim().length < MIN_CHARS) { setHits([]); return; }
    const n = ++seq.current;
    const t = setTimeout(() => {
      api.geocode(f.address).then((r) => { if (n === seq.current) { setHits(r); setOpen(true); setActive(-1); } })
        .catch(() => { if (n === seq.current) setHits([]); });   // a slow or down geocoder just means no suggestions
    }, DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [f.address, typed]);

  const pick = (h: GeoHit) => {
    const { form, tzSet } = applyHit(fRef.current, h);
    setF(form);
    setTzNote(tzSet ? "Time zone set from the address" : null);
    setUndo(null);
    setTyped(false); setOpen(false); setHits([]);
  };
  const locate = async () => {
    if (!f.address.trim()) return;
    setBusy(true);
    try {
      const r = await api.geocode(f.address);
      if (r[0]) pick(r[0]);
      else toast.info("No match for that address. Try adding the city, or drag the pin on the map.");
    } catch (e) { toast.error(e); } finally { setBusy(false); }
  };
  const drag = async (lat: number, lon: number) => {
    const before = fRef.current;
    let tz: string | null = null;
    try { tz = (await api.geocodeTimezone(lat, lon)).timezone; } catch { /* the hub fills an empty one on save */ }
    const { form, tzSet } = applyDrag(fRef.current, lat, lon, tz);
    setUndo((u) => u ?? before);
    setF(form);
    if (tzSet) setTzNote("Time zone set from the map pin");
  };
  const onKey = (e: React.KeyboardEvent) => {
    if (!open || hits.length === 0) { if (e.key === "Enter") { e.preventDefault(); locate(); } return; }
    if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => (a + 1) % hits.length); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => (a <= 0 ? hits.length - 1 : a - 1)); }
    else if (e.key === "Enter") { e.preventDefault(); pick(hits[Math.max(active, 0)]); }
    else if (e.key === "Escape") { setOpen(false); }
  };
  const copy = async () => {
    if (f.lat == null || f.lon == null) return;
    try { await navigator.clipboard.writeText(fmtCoord(f.lat, f.lon)); toast.success("Coordinates copied"); } catch { toast.info(fmtCoord(f.lat, f.lon)); }
  };
  const located = f.lat != null && f.lon != null;

  return (
    <div className="address-box">
      <div className="field">
        <span>Address</span>
        <div className="row address-row">
          <div className="address-input">
            <input value={f.address} disabled={!editable} maxLength={200} placeholder="Street, city, state or postal code"
              role="combobox" aria-expanded={open && hits.length > 0} aria-controls="address-suggestions" aria-autocomplete="list"
              aria-activedescendant={active >= 0 ? `addr-hit-${active}` : undefined}
              onChange={(e) => { setF({ ...f, address: e.target.value }); setTyped(true); }}
              onKeyDown={onKey} onFocus={() => hits.length && setOpen(true)} onBlur={() => setTimeout(() => setOpen(false), 150)} />
            {open && hits.length > 0 && (
              <ul id="address-suggestions" className="address-suggestions" role="listbox">
                {hits.map((h, i) => (
                  <li key={`${h.lat},${h.lon},${i}`} id={`addr-hit-${i}`} role="option" aria-selected={i === active} className={i === active ? "active" : ""}
                    onMouseDown={(e) => { e.preventDefault(); pick(h); }}>
                    <span>{h.display}</span>
                    {h.timezone && <span className="muted small">{h.timezone}</span>}
                  </li>
                ))}
              </ul>
            )}
          </div>
          {editable && <button className="ghost" disabled={busy || !f.address.trim()} onClick={locate} title="Look up this address and put it on the map">{busy ? "Locating…" : "Locate"}</button>}
        </div>
      </div>
      {tzNote && <p className="muted small address-note">🕒 {tzNote}{f.timezone ? `: ${f.timezone}` : ""}</p>}
      {located ? (
        <>
          <SiteMap pins={[{ id: "site", lat: f.lat!, lon: f.lon!, title: name }]} height={260} draggable={editable} onDrag={drag} label={`Map of ${name}`} />
          {undo && (
            <p className="small address-note">Marker moved from the address. <button className="linkish" onClick={() => { setF(undo); setUndo(null); setTzNote(null); }}>Undo</button></p>
          )}
          <div className="row coord-row small">
            <span className="muted">Coordinates</span>
            <code>{fmtCoord(f.lat!, f.lon!)}</code>
            <button className="ghost small" onClick={copy} title="Copy the coordinates">Copy</button>
            <span className="spacer" />
            <a href={googleMapsHref(f.lat!, f.lon!)} target="_blank" rel="noopener noreferrer">Open in Google Maps ↗</a>
            <a href={appleMapsHref(f.lat!, f.lon!, name)} target="_blank" rel="noopener noreferrer">Open in Apple Maps ↗</a>
          </div>
          {editable && <p className="muted small address-note">Drag the pin to the entrance responders should use.</p>}
        </>
      ) : (
        <p className="muted small address-note">Not on the map yet{editable ? ": pick a suggestion or press Locate." : "."}</p>
      )}
    </div>
  );
}
