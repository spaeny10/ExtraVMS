/**
 * The server UI's event viewer (clip, synopsis, lock / watch / feedback / Ask) opened in place on a hub page, for one
 * server's event: what a click on an event does in the server's own UI. Media and actions go to the event's server
 * through its tunnel (siteApi). The viewer's "Open in Timeline" is the one way from here to the Site's Timeline, so
 * this provides the NavContext it calls (timelineTargetHref). Centred dialog on desktop, docked drawer on a phone.
 */
import { useEffect, useMemo, useState } from "react";
import { EventDetail } from "@site/EventDetail";
import { NavContext, type TimelineTarget } from "@site/nav";
import { camKey } from "@site/playback";
import { useIsPhone } from "@site/ui";
import { type Org, type Site, api } from "./api";
import { siteApi } from "./hubSource";
import { type EventRef, timelineTargetHref } from "./eventOpen";
import { navigate } from "./nav";

export function HubEventDetail({ ev, cameraName, onClose }: { ev: EventRef; cameraName: (cam: string) => string; onClose: () => void }) {
  const isPhone = useIsPhone();
  const nav = useMemo(() => ({
    openInTimeline: (t: TimelineTarget) => {
      const href = timelineTargetHref(ev.location, ev.server, t);
      // a Site Timeline is a hub page (stay in the app); a server without a Site has only its console
      if (ev.location) navigate(href); else location.href = href;
    },
  }), [ev.location, ev.server]);
  return (
    <NavContext.Provider value={nav}>
      {/* keyed by server too: the same id on another server is another event */}
      <EventDetail key={`${ev.server}/${ev.id}`} id={ev.id} site={siteApi(ev.server)} cameraName={cameraName} onClose={onClose}
        variant={isPhone ? "drawer" : "modal"} />
    </NavContext.Provider>
  );
}

/**
 * Camera names keyed camKey(server, camera) from the servers' heartbeat summaries, for pages whose rows carry camera
 * ids only (alerts): the Site's own servers inside a Site page, else one /api/fleet read, made only once `wanted`
 * (the first time a viewer opens) so the list itself costs nothing extra.
 */
export function useCameraNames(org: Org, site: Site | undefined, wanted: boolean): Map<string, string> {
  const [fleetSites, setFleetSites] = useState<Site[] | null>(null);
  useEffect(() => {
    if (site || !wanted || fleetSites) return;
    api.fleet(org.id).then((f) => setFleetSites(f.orgs.find((o) => o.org.id === org.id)?.locations ?? [])).catch(() => setFleetSites([]));
  }, [org.id, site, wanted, fleetSites]);
  return useMemo(() => {
    const names = new Map<string, string>();
    for (const s of site ? [site] : fleetSites ?? []) for (const v of s.servers) for (const c of v.summary?.cameras ?? []) names.set(camKey(v.id, c.id), c.name);
    return names;
  }, [site, fleetSites]);
}
