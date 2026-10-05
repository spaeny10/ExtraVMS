/** ICE servers (TURN relay) per server, fetched once per page and shared by every live tile on it. */
import { useEffect, useState } from "react";
import type { DashboardSource } from "./source";

const iceCache = new Map<string, Promise<RTCIceServer[]>>();
export function useIceServers(source: DashboardSource, site: string): RTCIceServer[] | undefined {
  const [ice, setIce] = useState<RTCIceServer[] | undefined>(undefined);
  useEffect(() => {
    if (!site) return;
    if (!iceCache.has(site)) iceCache.set(site, source.iceServers(site).catch(() => []));
    let alive = true;
    iceCache.get(site)!.then((v) => { if (alive) setIce(v); });
    return () => { alive = false; };
  }, [source, site]);
  return ice;
}
