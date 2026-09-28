import { useEffect, useState } from "react";
import { EventCard } from "../../Events";
import { eventInScope, type DashboardSource } from "../source";
import type { FleetEvent, Widget } from "../types";

/** Latest events across the chosen sites, cameras or group, kept fresh from the fleet socket. */
export function EventsWidget({ widget: w, source }: { widget: Widget<"events">; source: DashboardSource }) {
  const p = w.props;
  const [events, setEvents] = useState<FleetEvent[] | null>(null);
  const [offline, setOffline] = useState<string[]>([]);
  const [err, setErr] = useState("");
  const limit = p.limit ?? 20;
  const key = JSON.stringify(p);

  useEffect(() => {
    let alive = true;
    setEvents(null); setErr("");
    source.events(p).then((r) => { if (!alive) return; setEvents(r.events.slice(0, limit)); setOffline(r.offline); })
      .catch((e) => { if (alive) { setErr(String(e)); setEvents([]); } });
    const unsub = source.subscribe((m) => {
      if (m.type === "event_removed") { setEvents((prev) => prev && prev.filter((x) => !(x.id === m.id && x.site_id === m.site_id))); return; }
      if (m.type !== "event") return;
      const e = m.event;
      if (!eventInScope(p, source.groups(), m.site_id, e.camera_id, e.camera_class)) return;
      setEvents((prev) => {
        const rest = (prev ?? []).filter((x) => !(x.id === e.id && x.site_id === m.site_id));
        if (e.status === "rejected" || e.status === "masked") return rest;
        return [{ ...e, site_id: m.site_id, site_name: m.site_name }, ...rest].sort((a, b) => b.start_ts - a.start_ts).slice(0, limit);
      });
    });
    return () => { alive = false; unsub(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source, key]);

  const names = new Map(source.cameras().map((c) => [`${c.site}/${c.id}`, c.name]));
  const siteNames = new Map(source.sites().map((s) => [s.id, s.name]));
  const multi = source.sites().length > 1;
  return (
    <div className="dash-events">
      {err && <div className="error small">{err}</div>}
      {offline.length > 0 && <div className="muted small">Offline: {offline.map((s) => siteNames.get(s) ?? s).join(", ")}</div>}
      {events === null && <div className="muted small">Loading…</div>}
      {events && events.length === 0 && <div className="muted small">No events yet.</div>}
      {events?.map((e) => (
        <EventCard key={`${e.site_id}-${e.id}`} e={e} cameraName={names.get(`${e.site_id}/${e.camera_id}`) ?? e.camera_id}
          site={source.siteApi(e.site_id)} siteName={multi ? e.site_name : undefined}
          onOpen={() => { if (source.openEvent) source.openEvent(e); else location.href = source.eventHref(e); }} />
      ))}
    </div>
  );
}
