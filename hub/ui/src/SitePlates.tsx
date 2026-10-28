/**
 * A Site's Plates tab: the server UI's Plates page (frontend/src/Plates.tsx: plate reads, plate lists, traffic counts)
 * over the Site's online servers that have a traffic camera set up, each through its hub-proxied client (siteApi).
 * Which servers have one is each server's own /api/traffic/status; with several, the page's server picker switches
 * between them (reads, lists and counts are per server). An event opens in HubEventDetail in place, as on Find.
 * A Site without traffic cameras says so; an older server without the traffic API counts as none.
 */
import { useEffect, useMemo, useState } from "react";
import { PlatesView, type PlatesSource } from "@site/Plates";
import type { Site } from "./api";
import { HubEventDetail } from "./HubEventDetail";
import { siteApi } from "./hubSource";

export function SitePlates({ site }: { site: Site }) {
  const servers = useMemo(() => site.servers.filter((s) => !s.retired_at), [site.servers]);
  const online = servers.filter((s) => s.online);
  const onlineKey = online.map((s) => s.id).sort().join(",");
  // server id -> has a traffic camera (missing = still asking)
  const [has, setHas] = useState<Record<string, boolean>>({});
  useEffect(() => {
    let alive = true;
    for (const id of onlineKey ? onlineKey.split(",") : []) {
      siteApi(id).trafficStatus()
        .then((s) => { if (alive) setHas((m) => ({ ...m, [id]: s.cameras.length > 0 })); })
        .catch(() => { if (alive) setHas((m) => ({ ...m, [id]: false })); });
    }
    return () => { alive = false; };
  }, [onlineKey]);

  const namesSig = online.map((s) => `${s.id}:${s.name}`).join(",");
  const withCams = online.filter((s) => has[s.id]).map((s) => s.id).join(",");
  // stable while the same servers have traffic cameras: the page keeps its search when the Site is read again
  const sources = useMemo<PlatesSource[]>(() => online.filter((s) => has[s.id]).map((s) => ({ id: s.id, name: s.name, api: siteApi(s.id) })),
    [withCams, namesSig]); // eslint-disable-line react-hooks/exhaustive-deps

  if (servers.length === 0) return <p className="muted">This site has no servers yet.</p>;
  if (online.length === 0) return <p className="muted">Every server at this site is offline: plate reads show once one reconnects.</p>;
  if (sources.length === 0) {
    const asking = online.some((s) => !(s.id in has));
    return <p className="muted">{asking ? "Looking for traffic cameras…" : "No traffic cameras at this Site."}</p>;
  }
  const offline = servers.filter((s) => !s.online);
  return <>
    {offline.length > 0 && <p className="muted small site-timeline-note">Not included: {offline.map((s) => s.name).join(", ")} (offline).</p>}
    <PlatesView key={site.id} sources={sources}
      renderEvent={(ev) => <HubEventDetail ev={{ server: ev.source, id: ev.id, location: site.id }} cameraName={ev.cameraName} onClose={ev.onClose} />} />
  </>;
}
