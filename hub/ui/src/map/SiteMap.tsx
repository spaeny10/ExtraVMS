/**
 * A Leaflet map of one or more Site pins. Its own chunk (LazyMap.tsx loads it with React.lazy), Leaflet's CSS with it,
 * so pages without a map never download either. Tiles and attribution come from the hub (place.ts getMapConfig).
 * Pins are CSS div-icons (no marker images to bundle); titles are text nodes, never HTML.
 */
import { useEffect, useRef } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { getMapConfig } from "../place";

export type MapPin = { id: string; lat: number; lon: number; colour?: string; title?: string };
export type SiteMapProps = {
  pins: MapPin[]; height: number;
  /** false: a picture of the place (no pan/zoom/keyboard), e.g. the SOC's Dispatch block */
  interactive?: boolean;
  /** the single pin can be dragged; onDrag gets where it was dropped */
  draggable?: boolean; onDrag?: (lat: number, lon: number) => void;
  onPinClick?: (pin: MapPin) => void;
  /** zoom for a single pin (several pins fit the view) */
  zoom?: number; className?: string; label?: string;
};

function icon(colour: string | undefined, big: boolean) {
  const el = document.createElement("span");
  el.className = "map-pin-dot";
  if (colour) el.style.setProperty("--pin", colour);
  return L.divIcon({ className: `map-pin${big ? " big" : ""}`, html: el, iconSize: big ? [26, 34] : [20, 26], iconAnchor: big ? [13, 34] : [10, 26], tooltipAnchor: [0, big ? -30 : -22] });
}

export default function SiteMap({ pins, height, interactive = true, draggable = false, onDrag, onPinClick, zoom = 16, className, label }: SiteMapProps) {
  const box = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const layer = useRef<L.LayerGroup | null>(null);
  const cb = useRef({ onDrag, onPinClick });
  cb.current = { onDrag, onPinClick };
  const lastFit = useRef<string>("");   // the places the view was last fitted to ("" = a new map: fit on the next draw)

  useEffect(() => {
    if (!box.current) return;
    const m = L.map(box.current, {
      zoomControl: interactive, dragging: interactive, scrollWheelZoom: false, doubleClickZoom: interactive, boxZoom: interactive,
      keyboard: interactive, touchZoom: interactive, attributionControl: true,
    });
    const cfg = getMapConfig();
    L.tileLayer(cfg.tiles, { maxZoom: 19, attribution: cfg.attribution }).addTo(m);
    m.attributionControl.setPrefix(false);   // keep only the tile provider's credit
    if (interactive) {   // wheel zoom only once the map has been clicked, so scrolling the page never gets stuck in it
      m.on("click", () => m.scrollWheelZoom.enable());
      m.on("mouseout", () => m.scrollWheelZoom.disable());
    }
    layer.current = L.layerGroup().addTo(m);
    lastFit.current = "";
    map.current = m;
    const ro = new ResizeObserver(() => m.invalidateSize());
    ro.observe(box.current);
    return () => { ro.disconnect(); m.remove(); map.current = null; layer.current = null; };
  }, [interactive]);

  // pins: redraw on any change; refit only when the set of places changed (not on every 15 s refresh)
  const fitKey = pins.map((p) => `${p.id}@${p.lat.toFixed(5)},${p.lon.toFixed(5)}`).join("|");
  useEffect(() => {
    const m = map.current, g = layer.current;
    if (!m || !g) return;
    g.clearLayers();
    for (const p of pins) {
      const mk = L.marker([p.lat, p.lon], { icon: icon(p.colour, pins.length === 1), draggable: draggable && pins.length === 1, keyboard: interactive, title: "" });
      if (p.title) {
        const t = document.createElement("span");
        t.textContent = p.title;
        mk.bindTooltip(t, { direction: "top" });
      }
      if (draggable) mk.on("dragend", () => { const ll = mk.getLatLng(); cb.current.onDrag?.(ll.lat, ll.lng); });
      mk.on("click", () => cb.current.onPinClick?.(p));
      mk.addTo(g);
    }
    if (fitKey === lastFit.current || pins.length === 0) { if (pins.length === 0) m.setView([20, 0], 1); return; }
    const first = !lastFit.current;
    lastFit.current = fitKey;
    // a moved pin (a drag, a new address) keeps the zoom the person chose; the first view uses `zoom`
    if (pins.length === 1) m.setView([pins[0].lat, pins[0].lon], first ? zoom : Math.max(m.getZoom(), 12));
    else m.fitBounds(L.latLngBounds(pins.map((p) => [p.lat, p.lon] as [number, number])), { padding: [30, 30], maxZoom: 15 });
  }, [fitKey, pins, draggable, interactive, zoom]);

  return <div ref={box} className={`site-map ${interactive ? "" : "static"} ${className ?? ""}`} style={{ height }} role="img" aria-label={label ?? "Map"} />;
}
