/**
 * A Site's Find tab: the server UI's FindView (views, filters, infinite browse, search by meaning, grouped by hour)
 * over every server of the Site, through a FindSource (frontend/src/findSource.ts) like the Timeline's apiFor/mediaFor:
 *  - browse and search: the hub's /api/locations/{id}/find/events|search (find.py), merged across the Site's servers
 *    with a per-server cursor for infinite scroll; live updates from the fleet socket (this Site's servers only);
 *  - cameras keyed camKey(server, camera) and named "Server · Camera" when the Site has more than one server;
 *  - saved views per Site on the hub (/find-views; operators and up save them), the starred default per Site in this
 *    browser;
 *  - media through mediaApi(server) (direct on the LAN when possible), a click opens HubEventDetail in place;
 *  - Ask is the hub's: every server's assistant answers (questions only); an instruction is not planned here, a note
 *    links to Customer › Actions with it prefilled (fleetAskPanel).
 * Per-server features are hidden in this cut: identities (grouped by who), footage look-alike search, the compliance
 * summary strip and the server assistant's threads / plans (features.assistant false: FindView never calls the
 * server's /api/assistant/plan or /execute from here).
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { Camera as ServerCam, NvrEvent, ParsedQuery } from "@site/api";
import { FindView, type ExternalAsk } from "@site/Find";
import type { FindSource } from "@site/findSource";
import { camKey } from "@site/playback";
import { type Org, type Site, api, subscribeFleet } from "./api";
import { useDirectVersion } from "./direct";
import { cameraNameFor } from "./eventOpen";
import { FleetAskResults, useFleetAsk } from "./fleetAskPanel";
import { useWhere } from "./FindPage";
import { HubEventDetail } from "./HubEventDetail";
import { mediaApi, siteApi } from "./hubSource";
import { hubEventKey, hubFindParams, newerAcross, siteFindCameras, siteFindHandoff } from "./siteFindData";

const CAMERA_REFRESH_MS = 60000;
type Tagged = NvrEvent & { site_id?: string };
const serverOfEvent = (e: NvrEvent) => (e as Tagged).site_id ?? "";

export function SiteFind({ org, site }: { org: Org; site: Site }) {
  const servers = useMemo(() => site.servers.filter((s) => !s.retired_at), [site.servers]);
  const online = useMemo(() => servers.filter((s) => s.online), [servers]);
  const onlineKey = online.map((s) => s.id).sort().join(",");
  const serversRef = useRef(servers);
  serversRef.current = servers;
  // media straight from servers this browser reaches on its LAN (re-rendered when that changes)
  useDirectVersion(online);

  // ---- cameras of each online server (zones included, for the named places); refreshed every minute
  const [byServer, setByServer] = useState<Record<string, ServerCam[]>>({});
  useEffect(() => {
    const ids = onlineKey ? onlineKey.split(",") : [];
    let alive = true;
    const pull = () => ids.forEach((id) => siteApi(id).cameras()
      .then((cams) => { if (alive) setByServer((m) => (JSON.stringify(m[id]) === JSON.stringify(cams) ? m : { ...m, [id]: cams })); })
      .catch(() => {}));
    pull();
    const t = setInterval(pull, CAMERA_REFRESH_MS);
    return () => { alive = false; clearInterval(t); };
  }, [onlineKey]);
  const namesSig = servers.map((s) => `${s.id}:${s.name}`).join(",");
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const cameras = useMemo(() => siteFindCameras(servers, byServer), [namesSig, byServer]);
  /** the viewer's title: the camera's own name (the Site page already says where) */
  const camNames = useMemo(() => {
    const m = new Map<string, string>();
    for (const s of servers) for (const c of byServer[s.id] ?? []) m.set(camKey(s.id, c.id), c.name);
    return m;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [namesSig, byServer]);
  const camNamesRef = useRef(camNames);
  camNamesRef.current = camNames;

  // ---- saved views: who may save them is the hub's answer (read with the list)
  const [canEdit, setCanEdit] = useState(false);
  const canEditRef = useRef(canEdit);
  canEditRef.current = canEdit;

  // one source per Site: FindView reloads its list when the source changes, so nothing here depends on polled data
  const source = useMemo<FindSource>(() => ({
    cameraKey: (e) => camKey(serverOfEvent(e), e.camera_id),
    eventKey: (e) => hubEventKey(e as Tagged),
    newer: newerAcross,
    events: (q, cursor, limit) => api.locationFindEvents(site.id, hubFindParams(q, cursor as string | null, limit))
      .then((r) => ({ events: r.events as NvrEvent[], next: r.next })),
    search: (text, q, cursor, limit) => api.locationFindSearch(site.id, { q: text, ...hubFindParams(q, cursor as string | null, limit) })
      .then((r) => ({ events: r.events as NvrEvent[], next: r.next })),
    // the time phrase ("last night") is read by one of the Site's servers, as the server UI does
    parseQuery: (text) => {
      const s = serversRef.current.find((x) => x.online);
      return s ? siteApi(s.id).parseQuery(text).catch(() => null as ParsedQuery | null) : Promise.resolve(null);
    },
    views: {
      list: () => api.locationFindViews(site.id).then((r) => { setCanEdit(r.can_edit); return r.views; }),
      save: (views) => api.saveLocationFindViews(site.id, views).then((r) => r.views),
      get canEdit() { return canEditRef.current; },
      defaultKey: `find.${site.id}.defaultView`,
    },
    mediaFor: (e) => mediaApi(serverOfEvent(e)),
    renderDetail: (o, onClose) => {
      const server = o.e ? serverOfEvent(o.e) : "";
      if (!server) return null;   // only this Site's tagged events open here (the hub has no assistant links)
      return <HubEventDetail ev={{ server, id: o.id, location: site.id }} cameraName={cameraNameFor(camNamesRef.current, server)} onClose={onClose} />;
    },
    subscribe: (on) => subscribeFleet(org.id, (m) => {
      if (!serversRef.current.some((s) => s.id === m.site_id)) return;   // this Site's servers only
      if (m.type === "event") on.event({ ...m.event, site_id: m.site_id, site_name: m.site_name } as NvrEvent);
      else if (m.type === "event_removed") on.removed(hubEventKey({ site_id: m.site_id, id: m.id }));
    }),
    features: { assistant: false, footage: false, identities: false, summary: false },
  }), [site.id, org.id]);

  // ---- Ask: the hub's, scoped to this Site
  const { label } = useWhere(org, site);
  const fa = useFleetAsk(org, label, site.id);
  const [handoff] = useState(() => siteFindHandoff(location.search) ?? undefined);
  const [lastAsk, setLastAsk] = useState("");
  const ask: ExternalAsk = {
    run: (text) => { setLastAsk(text); void fa.ask(text); },
    busy: fa.asking,
    label: "Ask this site",
    title: "Every server's assistant answers from its own footage (Shift+Enter, or end with ?). Instructions such as \"Quiet alerts tonight\" run from Customer › Actions",
    panel: <FleetAskResults answers={fa.answers} instruction={fa.instruction} onAskAnyway={() => void fa.ask(lastAsk, true)} />,
  };

  const offline = servers.filter((s) => !s.online);
  if (servers.length === 0) return <p className="muted">This site has no servers yet.</p>;
  return (
    <>
      {offline.length > 0 && (
        <p className="muted small site-timeline-note">
          {offline.length === servers.length ? "Every server at this site is offline: nothing to search until one reconnects." : `Not included: ${offline.map((s) => s.name).join(", ")} (offline).`}
        </p>
      )}
      <FindView key={site.id} cameras={cameras} source={source} ask={ask} handoff={handoff} />
    </>
  );
}
