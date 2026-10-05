/** The Site map, loaded on first use (its own chunk with Leaflet and its CSS). Same props as SiteMap. */
import { Suspense, lazy } from "react";
import type { SiteMapProps } from "./SiteMap";

export type { MapPin } from "./SiteMap";
const SiteMapChunk = lazy(() => import("./SiteMap"));

export function SiteMap(p: SiteMapProps) {
  return (
    <Suspense fallback={<div className={`site-map loading ${p.className ?? ""}`} style={{ height: p.height }} aria-busy="true" />}>
      <SiteMapChunk {...p} />
    </Suspense>
  );
}
