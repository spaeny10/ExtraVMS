import { useCallback, useEffect, useState } from "react";
import { api, subscribe, type Camera, type NvrEvent } from "./api";
import { EventsView } from "./Events";
import { CamerasView, LiveView, SystemView } from "./Views";
import { Dialogs, Icon, OfflineBanner, Toaster } from "./ui";
import { FindView } from "./Find";
import { HomeView } from "./Home";
import { ThemeToggle } from "./ThemeToggle";
import { TimelineView } from "./Timeline";
import { NavContext, parseTimelineHash, timelineHash, type TimelineFocus, type TimelineTarget } from "./nav";

const TABS = ["Home", "Live", "Events", "Find", "Timeline", "Settings"] as const;
type Tab = (typeof TABS)[number];
const TAB_ICON: Record<Tab, string> = { Home: "home", Live: "live", Events: "events", Find: "find", Timeline: "timeline", Settings: "settings" };

/** "#ask", "#live"... (home-screen shortcuts) open that tab */
const hashTab = (): Tab | null => {
  const h = location.hash.toLowerCase();
  if (h === "#ask" || h === "#search") return "Find";
  if (h === "#cameras" || h === "#system") return "Settings";
  return TABS.find((t) => h === `#${t.toLowerCase()}`) ?? null;
};

/** Cameras and System are configuration, used rarely: one tab, two sections. */
function SettingsView({ cameras, port, reload }: { cameras: Camera[]; port: number; reload: () => void }) {
  const [section, setSection] = useState<"cameras" | "system">(() => (location.hash.toLowerCase() === "#system" ? "system" : "cameras"));
  return (
    <div className="view settings">
      <div className="segmented settings-tabs">
        <button className={section === "cameras" ? "active" : ""} onClick={() => setSection("cameras")}><Icon name="camera" size={16} /> Cameras</button>
        <button className={section === "system" ? "active" : ""} onClick={() => setSection("system")}><Icon name="settings" size={16} /> System</button>
      </div>
      {section === "cameras" ? <CamerasView cameras={cameras} port={port} reload={reload} /> : <SystemView />}
    </div>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>(() => {
    try {
      const saved = localStorage.getItem("tab") as Tab | null;
      return hashTab() || (saved && TABS.includes(saved) ? saved : "Home");
    } catch {
      return "Home";
    }
  });
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [port, setPort] = useState(8889);
  const [recent, setRecent] = useState<NvrEvent[]>([]);
  const [live, setLive] = useState<NvrEvent | null>(null);
  const [focus, setFocus] = useState<TimelineFocus | null>(null);

  const choose = useCallback((t: Tab) => {
    setTab(t);
    if (t !== "Timeline") {
      setFocus(null); // an event focus only applies to the visit it was opened for
      if (location.hash.startsWith("#timeline")) history.replaceState(null, "", location.pathname);
    }
    try {
      localStorage.setItem("tab", t);
    } catch {
      /* private mode */
    }
  }, []);

  /** Jump to an event on the Timeline (from any event viewer or a deep link). */
  const openInTimeline = useCallback((e: TimelineTarget) => {
    const m = e.members?.length ? e.members : null;
    setFocus({
      eventId: e.id,
      cam: m ? m[0].cam : e.camera_id,
      start: m ? Math.min(...m.map((x) => x.start)) : e.start_ts,
      end: m ? Math.max(...m.map((x) => x.end)) : e.end_ts ?? e.start_ts,
      label: m ? `journey across ${new Set(m.map((x) => x.cam)).size} cameras` : e.camera_class ?? "event",
      nonce: Date.now(),
      members: m ?? undefined,
    });
    choose("Timeline");
    history.replaceState(null, "", timelineHash(e.camera_id, e.id, !!m, e.start_ts));
  }, [choose]);

  // Deep link: #timeline?cam=cam1&event=123
  useEffect(() => {
    const target = parseTimelineHash(location.hash);
    if (target?.event) {
      const id = target.event;
      // a journey link restores all its sightings, not just the one event
      Promise.all([api.event(id), target.journey ? api.eventJourney(id).catch(() => null) : null])
        .then(([e, j]) => openInTimeline(j ? { ...e, members: j.events.map((x) => ({ id: x.id, cam: x.camera_id, start: x.start_ts, end: x.end_ts ?? x.start_ts })) } : e))
        .catch(() => {});
    }
    else if (target?.t && target.cam) openInTimeline({ id: 0, camera_id: target.cam, start_ts: target.t, end_ts: target.t + 5, camera_class: "moment" });
    else if (target) choose("Timeline");
  }, [openInTimeline, choose]);

  const loadCameras = useCallback(() => api.cameras().then(setCameras).catch(() => {}), []);

  useEffect(() => {
    loadCameras();
    api.system().then((s) => setPort(s.webrtc_port)).catch(() => {});
    api.events({ limit: 20, status: "open,pending,verified" }).then(setRecent).catch(() => {});
    const t = setInterval(loadCameras, 15000);
    const unsub = subscribe((e) => {
      setLive(e);
      setRecent((prev) => {
        // A rejection replaces the card in place (shown faded as "YOLO did not confirm") rather than
        // vanishing, so a detection never quietly disappears. New rejected events aren't added.
        if (e.status === "rejected" || e.status === "masked") return prev.map((x) => (x.id === e.id ? e : x));
        return [e, ...prev.filter((x) => x.id !== e.id)].slice(0, 30);
      });
    });
    return () => {
      clearInterval(t);
      unsub();
    };
  }, [loadCameras]);

  const online = cameras.filter((c) => c.status?.stream_ready).length;
  const active = recent.filter((e) => e.status === "open" || e.status === "pending").length;

  return (
    <NavContext.Provider value={{ openInTimeline }}>
    <div className="app">
      <OfflineBanner />
      <header className="topbar">
        <div className="brand">
          <span className="logo" /> NewVMS
        </div>
        <nav>
          {TABS.map((t) => (
            <button key={t} className={tab === t ? "active" : ""} onClick={() => choose(t)}>
              <Icon name={TAB_ICON[t]} className="tab-icon" size={20} />
              <span className="tab-label">{t}</span>
            </button>
          ))}
        </nav>
        <div className="top-status">
          <span><span className={`dot ${online === cameras.length && online > 0 ? "ok" : "bad"}`} /> {online}/{cameras.length} cameras</span>
          {active > 0 && <span className="badge status-open">{active} tracking</span>}
          <ThemeToggle />
        </div>
      </header>
      <main>
        {tab === "Home" && <HomeView cameras={cameras} onGo={choose} />}
        {tab === "Live" && <LiveView cameras={cameras.filter((c) => c.enabled)} port={port} recent={recent} />}
        {tab === "Events" && <EventsView cameras={cameras} live={live} />}
        {tab === "Find" && <FindView cameras={cameras} />}
        {tab === "Timeline" && <TimelineView cameras={cameras} focus={focus} onClearFocus={() => { setFocus(null); history.replaceState(null, "", location.pathname); }} />}
        {tab === "Settings" && <SettingsView cameras={cameras} port={port} reload={loadCameras} />}
      </main>
      <Toaster />
      <Dialogs />
    </div>
    </NavContext.Provider>
  );
}
