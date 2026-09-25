import { confirmDialog, promptDialog, toast, useIsPhone } from "./ui";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, fmtTime, type Camera, type KeptSpan, type Layout, type LayoutConfig, type Lock, type TimelineEvent, UNUSUAL_MIN } from "./api";
import { ConfidenceSlider, loadNumber, saveNumber } from "./ConfidenceSlider";
import { EventDetail } from "./EventDetail";
import type { TimelineFocus } from "./nav";
import { LIVE_LAG, fmtClock, nowS, spanAt, useLatestFrame, type Span } from "./playback";
import { SyncTile, type TileStatus } from "./SyncPlayer";

type Marker = TimelineEvent;
type Filter = {
  person: boolean;
  vehicle: boolean;
  status: "verified" | "active" | "rejected" | "all";
  minThreat: "any" | "low" | "medium" | "high";
  hideFalseAlarms: boolean;
  synopsisOnly: boolean;
  minYolo: number;
};
const DEFAULT_FILTER: Filter = { person: true, vehicle: true, status: "verified", minThreat: "any", hideFalseAlarms: true, synopsisOnly: false, minYolo: 0 };
const LIVE_WINDOW_S = 30;
const THREAT_RANK: Record<string, number> = { none: 0, low: 1, medium: 2, high: 3 };

function matches(e: Marker, f: Filter): boolean {
  if (e.camera_class === "person" ? !f.person : e.camera_class === "vehicle" ? !f.vehicle : false) return false;
  if (f.status === "verified" && e.status !== "verified") return false;
  if (f.status === "active" && e.status !== "open" && e.status !== "pending") return false;
  if (f.status === "rejected" && e.status !== "rejected") return false;
  // priority = max(Qwen threat, how unusual for this camera), so vehicles and unrated events are ranked too
  if (f.minThreat !== "any" && (THREAT_RANK[e.priority ?? e.threat ?? ""] ?? -1) < THREAT_RANK[f.minThreat]) return false;
  if (f.hideFalseAlarms && e.verdict === "false_alarm") return false;
  if (f.synopsisOnly && !e.has_synopsis) return false;
  // Events still being tracked/verified have no YOLO score yet; keep them visible.
  if (f.minYolo > 0 && e.status !== "open" && e.status !== "pending" && (e.yolo_conf ?? 0) < f.minYolo) return false;
  return true;
}

function loadFilter(): Filter {
  try {
    return { ...DEFAULT_FILTER, ...JSON.parse(localStorage.getItem("timelineFilter") ?? "{}") };
  } catch {
    return DEFAULT_FILTER;
  }
}
type Lane = { spans: Span[]; events: Marker[]; kept: KeptSpan[]; locks: Lock[] };
type View = { start: number; end: number };
type Drag =
  | { mode: "scrub"; pointer: number }
  | { mode: "pan"; pointer: number; x0: number; view0: View; moved: boolean; cam: string | null }
  | { mode: "select"; pointer: number; cam: string; t0: number };

const ALL_CAMERAS: LayoutConfig = { visible: null, solo: null };
const FOCUS_PREROLL_S = 5; // start playback this long before a focused event
const MIN_RANGE = 60; // 1 minute
/** touch screen (phone/tablet): changes hints and grab sizes */
const COARSE = typeof matchMedia !== "undefined" && matchMedia("(pointer: coarse)").matches;
const MAX_RANGE = 14 * 86400; // 2 weeks
const STEPS = [1, 5, 10, 30, 60, 300, 600, 900, 1800, 3600, 10800, 21600, 43200, 86400, 172800, 604800];
const SPEEDS = [0.5, 1, 2, 4, 8, 16];
const PRESETS: [string, number][] = [["5m", 300], ["1h", 3600], ["6h", 21600], ["24h", 86400], ["7d", 604800]];
const HEAVY_TILES = 6;
const HOLD_MAX_MS = 3000; // longest the clock waits for a buffering tile

const tzShift = (t: number) => -new Date(t * 1000).getTimezoneOffset() * 60;
const pad = (n: number) => String(n).padStart(2, "0");
const gridCols = (n: number) => (n <= 1 ? 1 : n <= 4 ? 2 : n <= 9 ? 3 : 4);

function fmtTick(t: number, step: number): string {
  const d = new Date(t * 1000);
  if (step >= 86400 || (d.getHours() === 0 && d.getMinutes() === 0 && d.getSeconds() === 0))
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  if (step >= 60) return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function mergeSpans(raw: { start: string; duration: number }[]): Span[] {
  const spans = raw
    .map((s) => {
      const start = Date.parse(s.start) / 1000;
      return { start, end: start + s.duration };
    })
    .sort((a, b) => a.start - b.start);
  const out: Span[] = [];
  for (const s of spans) {
    const last = out[out.length - 1];
    if (last && s.start - last.end < 2) last.end = Math.max(last.end, s.end);
    else out.push({ ...s });
  }
  return out;
}

function clampView(start: number, end: number): View {
  const range = end - start;
  const maxEnd = nowS() + range * 0.15; // allow a little future so "now" isn't glued to the edge
  if (end > maxEnd) return { start: maxEnd - range, end: maxEnd };
  return { start, end };
}

function nextRecording(lanes: Record<string, Lane>, ids: string[], t: number): number | null {
  let best: number | null = null;
  for (const id of ids) {
    const s = lanes[id]?.spans.find((x) => x.start > t);
    if (s && (best == null || s.start < best)) best = s.start;
  }
  return best;
}

function loadConfig(): LayoutConfig {
  try {
    const c = JSON.parse(localStorage.getItem("timelineLayoutConfig") ?? "null");
    return c && typeof c === "object" ? { visible: c.visible ?? null, solo: c.solo ?? null } : ALL_CAMERAS;
  } catch {
    return ALL_CAMERAS;
  }
}

export function TimelineView({ cameras, focus = null, onClearFocus }: { cameras: Camera[]; focus?: TimelineFocus | null; onClearFocus?: () => void }) {
  const [view, setView] = useState<View>(() => clampView(nowS() - 3600, nowS() + 300));
  const [cam, setCam] = useState(cameras[0]?.id ?? "");
  const [lanes, setLanes] = useState<Record<string, Lane>>({});
  const [playhead, setPlayhead] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [hover, setHover] = useState<{ x: number; t: number; cam: string } | null>(null);
  const [message, setMessage] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const [width, setWidth] = useState(1000);
  const [filter, setFilterState] = useState<Filter>(loadFilter);
  const filterActive = JSON.stringify({ ...DEFAULT_FILTER, ...filter }) !== JSON.stringify(DEFAULT_FILTER);
  const setFilter = (patch: Partial<Filter>) => {
    const next = { ...filter, ...patch };
    setFilterState(next);
    try {
      localStorage.setItem("timelineFilter", JSON.stringify(next));
    } catch {
      /* private mode */
    }
  };
  const track = useRef<HTMLDivElement>(null);
  const drag = useRef<Drag | null>(null);
  const [moreOpen, setMoreOpen] = useState(false); // phones only: the layout & filter bars are folded away
  const viewRef = useRef(view);
  const loadedFor = useRef<View | null>(null);   // time window the current lane data covers
  const pendingFocus = useRef<TimelineFocus | null>(null);
  viewRef.current = view;

  const range = view.end - view.start;
  const toX = useCallback((t: number) => ((t - view.start) / range) * width, [view.start, range, width]);
  const toT = useCallback((x: number) => view.start + (x / width) * range, [view.start, range, width]);
  const camName = (id: string) => cameras.find((c) => c.id === id)?.name ?? id;

  useEffect(() => {
    if (!cam && cameras[0]) setCam(cameras[0].id);
  }, [cameras, cam]);

  // ---- layout: which cameras are shown and which one is soloed; named layouts live on the server
  const [layouts, setLayouts] = useState<Layout[]>([]);
  const [layoutId, setLayoutIdState] = useState<number>(() => loadNumber("timelineLayoutId", 0));
  const [config, setConfigState] = useState<LayoutConfig>(loadConfig);
  const setConfig = (next: LayoutConfig | ((c: LayoutConfig) => LayoutConfig)) => {
    setConfigState((prev) => {
      const c = typeof next === "function" ? next(prev) : next;
      try {
        localStorage.setItem("timelineLayoutConfig", JSON.stringify(c));
      } catch {
        /* private mode */
      }
      return c;
    });
  };
  const setLayoutId = (id: number) => {
    setLayoutIdState(id);
    saveNumber("timelineLayoutId", id);
  };
  const reloadLayouts = () => api.layouts().then(setLayouts).catch(() => {});
  useEffect(() => {
    reloadLayouts();
  }, []);

  const allIds = cameras.map((c) => c.id);
  const isPhone = useIsPhone();
  useEffect(() => {  // a phone can't show a grid of full-resolution streams: one camera at a time
    if (isPhone && !config.solo && allIds.length) setConfig((c) => ({ ...c, solo: allIds[0] }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPhone, allIds.join(",")]);
  const visibleIds = allIds.filter((id) => config.visible == null || config.visible.includes(id));
  const solo = config.solo && allIds.includes(config.solo) ? config.solo : null;
  const tileIds = solo ? [solo] : visibleIds;
  const norm = (c: LayoutConfig) => JSON.stringify({ visible: [...(c.visible ?? allIds)].filter((id) => allIds.includes(id)).sort(), solo: c.solo ?? null });
  const savedConfig = layoutId ? layouts.find((l) => l.id === layoutId)?.config : ALL_CAMERAS;
  const dirty = savedConfig ? norm(config) !== norm(savedConfig) : false;

  const toggleVisible = (id: string) => setConfig((c) => {
    const vis = c.visible ?? allIds;
    const on = vis.includes(id);
    return { visible: on ? vis.filter((x) => x !== id) : [...vis, id], solo: on && c.solo === id ? null : c.solo };
  });
  const toggleSolo = (id: string) => setConfig((c) => {
    const vis = c.visible ?? allIds;
    return { visible: vis.includes(id) ? c.visible : [...vis, id], solo: c.solo === id ? null : id };
  });
  const pickLayout = (id: number) => {
    setLayoutId(id);
    setConfig(id ? layouts.find((l) => l.id === id)?.config ?? ALL_CAMERAS : ALL_CAMERAS);
  };
  const saveLayout = async () => {
    const l = layouts.find((x) => x.id === layoutId);
    if (!l) return saveLayoutAs();
    await api.updateLayout(l.id, { name: l.name, config });
    reloadLayouts();
    toast.success(`Layout "${l.name}" saved`);
  };
  const saveLayoutAs = async () => {
    const name = (await promptDialog("Save layout as", { label: "Name, e.g. Doors or Perimeter" }))?.trim();
    if (!name) return;
    try {
      const l = await api.createLayout({ name, config });
      await reloadLayouts();
      setLayoutId(l.id);
      toast.success(`Layout "${name}" saved`);
    } catch (e) {
      toast.error(String(e).includes("409") ? "A layout with that name already exists." : e);
    }
  };
  const renameLayout = async () => {
    const l = layouts.find((x) => x.id === layoutId);
    const name = l && (await promptDialog("Rename layout", { initial: l.name, confirmLabel: "Rename" }))?.trim();
    if (!l || !name) return;
    try {
      await api.updateLayout(l.id, { name, config: l.config });
      reloadLayouts();
      toast.success(`Renamed to "${name}"`);
    } catch (e) {
      toast.error(String(e).includes("409") ? "A layout with that name already exists." : e);
    }
  };
  const deleteLayout = async () => {
    const l = layouts.find((x) => x.id === layoutId);
    if (!l || !await confirmDialog(`Delete the layout "${l.name}"?`, { confirmLabel: "Delete", danger: true })) return;
    await api.deleteLayout(l.id);
    setLayoutId(0);
    reloadLayouts();
    toast.success(`Deleted "${l.name}"`);
  };

  // Track width
  useEffect(() => {
    const el = track.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth || 1000));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Load spans + events for the visible window (plus margin), debounced while zooming/panning.
  const load = useCallback(async (v: View) => {
    const margin = (v.end - v.start) * 0.5;
    const entries = await Promise.all(
      cameras.map(async (c) => {
        try {
          const r = await api.recordings(c.id, v.start - margin, v.end + margin);
          return [c.id, { spans: mergeSpans(r.spans), events: r.events, kept: r.kept ?? [], locks: r.locks ?? [] }] as const;
        } catch {
          return [c.id, { spans: [], events: [], kept: [], locks: [] }] as const;
        }
      }),
    );
    setLanes(Object.fromEntries(entries));
    loadedFor.current = { start: v.start - margin, end: v.end + margin };
  }, [cameras]);
  useEffect(() => {
    const t = setTimeout(() => load(view), 200);
    return () => clearTimeout(t);
  }, [view, load]);
  // Keep the live edge fresh
  useEffect(() => {
    const t = setInterval(() => {
      if (viewRef.current.end > nowS() - 60) load(viewRef.current);
    }, 10000);
    return () => clearInterval(t);
  }, [load]);

  // Mouse wheel: zoom around the cursor; shift+wheel or horizontal swipe pans. Non-passive so the page doesn't scroll.
  useEffect(() => {
    const el = track.current;
    if (!el) return;
    const onWheel = (ev: WheelEvent) => {
      ev.preventDefault();
      const rect = el.getBoundingClientRect();
      const frac = Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width));
      setView((v) => {
        const r = v.end - v.start;
        const horizontal = ev.shiftKey || Math.abs(ev.deltaX) > Math.abs(ev.deltaY);
        if (horizontal) {
          const d = ((ev.shiftKey ? ev.deltaY : ev.deltaX) / rect.width) * r;
          return clampView(v.start + d, v.end + d);
        }
        const scale = ev.deltaMode === 1 ? 40 : 1; // line-based wheels
        const nr = Math.min(MAX_RANGE, Math.max(MIN_RANGE, r * Math.exp(ev.deltaY * scale * 0.0015)));
        const anchor = v.start + frac * r;
        return clampView(anchor - frac * nr, anchor + (1 - frac) * nr);
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  // ---- playback: one shared clock; every visible tile follows it (see SyncTile)
  const clockRef = useRef<number | null>(null);
  const statusRef = useRef<Record<string, TileStatus>>({});
  const [scrubbing, setScrubbing] = useState(false);
  const [scrubT, setScrubT] = useState<number | null>(null);
  const [scrubDir, setScrubDir] = useState<1 | -1>(1);
  const loopState = useRef({ playing, speed, scrubbing, tileIds, lanes });
  loopState.current = { playing, speed, scrubbing, tileIds, lanes };

  const setClock = (t: number) => {
    clockRef.current = t;
    setPlayhead(t);
  };

  const seekTo = (t: number, camId: string | null = null, autoplay = true, opts: { noSkip?: boolean; until?: number } = {}) => {
    t = Math.min(t, nowS() - LIVE_LAG);
    setMessage("");
    if (opts.noSkip && camId) {
      // Jumping to an event: accept a recording that starts inside the event, but never skip past it.
      if (!spanAt(lanes[camId]?.spans, t)) {
        const inside = lanes[camId]?.spans.find((s) => s.end > t && s.start <= (opts.until ?? t));
        if (!inside) {
          setClock(t);
          setPlaying(false);
          setMessage(`No recording on disk for this moment on ${camName(camId)}. It may have expired under the retention policy.`);
          return;
        }
        t = Math.max(t, inside.start);
      }
    } else if (!tileIds.some((id) => spanAt(lanes[id]?.spans, t))) {
      const next = nextRecording(lanes, tileIds, t);
      if (next == null) {
        setClock(t);
        setPlaying(false);
        setMessage(`No recording at ${fmtClock(t)} on the cameras shown`);
        return;
      }
      t = next;
      setMessage(`Skipped a gap to ${fmtClock(t)}`);
    }
    setClock(t);
    setPlaying(autoplay);
  };

  // The clock: advances while playing, holds while every tile with footage is buffering (keeps them together),
  // skips gaps where no shown camera has footage, and never runs ahead of the recording's live edge.
  useEffect(() => {
    let raf = 0;
    let last = performance.now();
    let lastUi = 0;
    let holdSince: number | null = null;
    const loop = (now: number) => {
      const dt = Math.min(0.5, (now - last) / 1000);
      last = now;
      const st = loopState.current;
      let t = clockRef.current;
      if (t != null && st.playing && !st.scrubbing) {
        // Hold while any tile with footage is buffering, so tiles start and stay together,
        // but for at most HOLD_MAX_MS so one slow camera can't stall the others forever.
        const statuses = st.tileIds.map((id) => statusRef.current[id]).filter(Boolean);
        const anyBuffering = statuses.some((s) => s === "buffering");
        if (anyBuffering) holdSince = holdSince ?? now;
        else holdSince = null;
        const hold = anyBuffering && now - (holdSince ?? now) < HOLD_MAX_MS;
        if (!hold) {
          t = Math.min(t + dt * st.speed, nowS() - LIVE_LAG);
          if (st.tileIds.length && !st.tileIds.some((id) => spanAt(st.lanes[id]?.spans, t!))) {
            const next = nextRecording(st.lanes, st.tileIds, t);
            if (next != null) t = next;
          }
          clockRef.current = t;
        }
      }
      if (now - lastUi > 100 && clockRef.current != null && !st.scrubbing) {
        lastUi = now;
        const c = clockRef.current;
        setPlayhead(c);
        const cur = viewRef.current;
        if (st.playing && c > cur.end - (cur.end - cur.start) * 0.05) {
          const r = cur.end - cur.start;
          setView(clampView(c - r * 0.3, c + r * 0.7));
        }
      }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, []);

  // ---- focus on an event (from the event viewer's "Open in Timeline" or a #timeline deep link)
  useEffect(() => {
    if (!focus) return;
    setCam(focus.cam);
    setOpen(null);
    // make sure the event's camera(s) are shown; a journey shows all its cameras side by side
    const cams = focus.members?.length ? [...new Set(focus.members.map((m) => m.cam))] : [focus.cam];
    setConfig((c) => ({
      visible: c.visible ? [...new Set([...c.visible, ...cams])] : c.visible,
      solo: focus.members?.length ? null : c.solo && c.solo !== focus.cam ? focus.cam : c.solo,
    }));
    const span = Math.max(180, focus.end - focus.start + 120);
    const mid = (focus.start + focus.end) / 2;
    setView(clampView(mid - span / 2, mid + span / 2));
    pendingFocus.current = focus;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus?.nonce]);
  useEffect(() => {
    const f = pendingFocus.current;
    const lf = loadedFor.current;
    if (!f || !lf || f.start < lf.start || f.end > lf.end || !lanes[f.cam]) return;
    pendingFocus.current = null;
    const first = f.members?.length ? f.members[0] : { cam: f.cam, start: f.start, end: f.end };
    seekTo(first.start - FOCUS_PREROLL_S, first.cam, true, { noSkip: true, until: first.end });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lanes]);
  const replayFocus = () => {
    if (!focus) return;
    const first = focus.members?.length ? focus.members[0] : { cam: focus.cam, start: focus.start, end: focus.end };
    seekTo(first.start - FOCUS_PREROLL_S, first.cam, true, { noSkip: true, until: first.end });
  };
  const focusMembers = focus ? (focus.members?.length ? focus.members : [{ id: focus.eventId, cam: focus.cam, start: focus.start, end: focus.end }]) : [];

  // ---- journey connectors: lines between linked sightings on different lanes
  const [showJourneys, setShowJourneys] = useState(() => loadNumber("timelineShowJourneys", 0) === 1);
  const laneY = (camId: string): number | null => {
    let y = 26; // axis height
    for (const c of cameras) {
      const h = visibleIds.includes(c.id) ? 34 : 18;
      if (c.id === camId) return visibleIds.includes(c.id) ? y + h / 2 : null;
      y += h;
    }
    return null;
  };
  const connectors = useMemo(() => {
    type Pt = { cam: string; start: number; end: number };
    const segs: { a: Pt; b: Pt; focus: boolean }[] = [];
    const chain = (items: Pt[], isFocus: boolean) => {
      const sorted = [...items].sort((x, y) => x.start - y.start);
      for (let i = 1; i < sorted.length; i++)
        if (sorted[i].cam !== sorted[i - 1].cam) segs.push({ a: sorted[i - 1], b: sorted[i], focus: isFocus });
    };
    if (focusMembers.length > 1) chain(focusMembers, true);
    if (showJourneys) {
      const byJourney: Record<number, Pt[]> = {};
      for (const [camId, lane] of Object.entries(lanes))
        for (const e of lane.events)
          if (e.journey_id) (byJourney[e.journey_id] ??= []).push({ cam: camId, start: e.start_ts, end: e.end_ts ?? e.start_ts });
      Object.values(byJourney).forEach((items) => chain(items, false));
    }
    return segs;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lanes, showJourneys, focus?.nonce]);

  // ---- hover thumbnails (per lane)
  const hoverFrames = useLatestFrame(320);
  const lastScrubT = useRef<number | null>(null);

  const scrubTo = (t: number) => {
    t = Math.min(t, nowS() - LIVE_LAG);
    if (lastScrubT.current != null && Math.abs(t - lastScrubT.current) > 0.01) {
      const dir = t > lastScrubT.current ? 1 : -1;
      if (dir !== scrubDir) setScrubDir(dir);
    }
    lastScrubT.current = t;
    clockRef.current = t;
    setPlayhead(t);
    setScrubT(t);
  };

  // ---- pointer interactions on the track
  const localX = (ev: React.PointerEvent) => ev.clientX - track.current!.getBoundingClientRect().left;

  // ---- locking ranges (Shift+drag) and existing locks
  const [selection, setSelection] = useState<{ cam: string; start: number; end: number } | null>(null);
  const [lockPrompt, setLockPrompt] = useState<{ cam: string; start: number; end: number; note: string } | null>(null);
  const [openLock, setOpenLock] = useState<Lock | null>(null);
  const saveLock = async () => {
    if (!lockPrompt) return;
    await api.createLock({ camera_id: lockPrompt.cam, start_ts: lockPrompt.start, end_ts: lockPrompt.end, note: lockPrompt.note });
    toast.success("Footage locked · kept until you unlock it");
    setLockPrompt(null);
    setSelection(null);
    load(viewRef.current);
  };

  const GRAB_PX = 12;
  const GRAB_TOUCH_PX = 28; // a fingertip is much less precise than a mouse
  const nearPlayhead = (x: number, touch = false) => playhead != null && Math.abs(toX(playhead) - x) <= (touch ? GRAB_TOUCH_PX : GRAB_PX);

  // ---- pinch to zoom (two fingers on the track)
  const pointers = useRef(new Map<number, number>()); // pointerId -> clientX, touch pointers only
  const pinch = useRef<{ d0: number; mid0: number; view0: View } | null>(null);
  const pinchUpdate = () => {
    const xs = [...pointers.current.values()];
    const p0 = pinch.current;
    if (!p0 || xs.length < 2) return;
    const rect = track.current!.getBoundingClientRect();
    const d = Math.max(20, Math.abs(xs[0] - xs[1]));
    const r0 = p0.view0.end - p0.view0.start;
    const nr = Math.min(MAX_RANGE, Math.max(MIN_RANGE, r0 * (p0.d0 / d)));
    const frac = Math.min(1, Math.max(0, (p0.mid0 - rect.left) / rect.width));
    const anchor = p0.view0.start + frac * r0; // the time under the fingers stays put
    const mid = (xs[0] + xs[1]) / 2;
    const shift = -((mid - p0.mid0) / rect.width) * nr; // moving both fingers pans
    setView(clampView(anchor - frac * nr + shift, anchor + (1 - frac) * nr + shift));
  };

  const onPointerDown = (ev: React.PointerEvent) => {
    if (ev.button !== 0) return;
    const touch = ev.pointerType === "touch";
    if (touch) {
      pointers.current.set(ev.pointerId, ev.clientX);
      if (pointers.current.size === 2) {
        // second finger: switch from pan/scrub to pinch zoom
        if (drag.current?.mode === "scrub") { setScrubbing(false); lastScrubT.current = null; }
        drag.current = null;
        const xs = [...pointers.current.values()];
        pinch.current = { d0: Math.max(20, Math.abs(xs[0] - xs[1])), mid0: (xs[0] + xs[1]) / 2, view0: view };
        try { track.current!.setPointerCapture(ev.pointerId); } catch { /* released */ }
        return;
      }
    }
    const target = ev.target as HTMLElement;
    const x = localX(ev);
    const onRuler = Boolean(target.closest(".tl-axis"));
    const grab = onRuler || Boolean(target.closest(".tl-playhead")) || nearPlayhead(x, touch);
    if (!grab && target.closest(".tl-marker")) return; // markers handle their own click
    try {
      track.current!.setPointerCapture(ev.pointerId); // keep receiving moves when the cursor leaves the track
    } catch {
      /* pointer already released */
    }
    hoverFrames.clear();
    const laneEl = target.closest("[data-cam]") as HTMLElement | null;
    if (ev.shiftKey && laneEl?.dataset.cam) {
      // Shift+drag on a lane: select a time range to lock.
      const t0 = Math.min(toT(x), nowS());
      drag.current = { mode: "select", pointer: ev.pointerId, cam: laneEl.dataset.cam, t0 };
      setSelection({ cam: laneEl.dataset.cam, start: t0, end: t0 });
      setLockPrompt(null);
      return;
    }
    if (grab) {
      // Scrub: grab the playhead from the ruler, the handle, or anywhere within GRAB_PX of the line.
      drag.current = { mode: "scrub", pointer: ev.pointerId };
      setScrubbing(true);
      scrubTo(onRuler ? toT(x) : playhead ?? toT(x));
    } else {
      drag.current = { mode: "pan", pointer: ev.pointerId, x0: ev.clientX, view0: view, moved: false, cam: laneEl?.dataset.cam ?? null };
    }
  };

  const onPointerMove = (ev: React.PointerEvent) => {
    if (pointers.current.has(ev.pointerId)) {
      pointers.current.set(ev.pointerId, ev.clientX);
      if (pinch.current) { pinchUpdate(); return; }
    }
    const x = localX(ev);
    const t = toT(x);
    const laneCam = ((ev.target as HTMLElement).closest("[data-cam]") as HTMLElement | null)?.dataset.cam ?? cam;
    setHover({ x, t, cam: laneCam });
    const d = drag.current;
    if (!d) {
      // Hover thumbnail, snapped to whole seconds so nearby positions share cached frames.
      if (t < nowS() - LIVE_LAG) hoverFrames.request(laneCam, Math.round(t));
      else hoverFrames.clear();
      return;
    }
    if (d.mode === "select") {
      const t1 = Math.min(toT(x), nowS());
      setSelection({ cam: d.cam, start: Math.min(d.t0, t1), end: Math.max(d.t0, t1) });
      return;
    }
    if (d.mode === "scrub") {
      scrubTo(toT(x));
    } else {
      const dx = ev.clientX - d.x0;
      if (Math.abs(dx) > 3) d.moved = true;
      if (d.moved) {
        const r = d.view0.end - d.view0.start;
        const shift = (-dx / width) * r;
        setView(clampView(d.view0.start + shift, d.view0.end + shift));
      }
    }
  };

  const onPointerUp = (ev: React.PointerEvent) => {
    pointers.current.delete(ev.pointerId);
    if (pinch.current) {
      if (pointers.current.size < 2) pinch.current = null; // lifting a finger ends the pinch (no tap-to-seek)
      drag.current = null;
      return;
    }
    const d = drag.current;
    drag.current = null;
    if (!d) return;
    if (d.mode === "select") {
      if (selection && selection.end - selection.start >= 1) setLockPrompt({ ...selection, note: "" });
      else setSelection(null);
      return;
    }
    if (d.mode === "scrub") {
      // Tiles keep their preview frame until their video has caught up with the new time.
      lastScrubT.current = null;
      setScrubbing(false);
      if (playhead != null) seekTo(playhead);
    } else if (!d.moved) {
      if (d.cam) setCam(d.cam);
      seekTo(toT(localX(ev)));
    }
  };

  // ---- filtered events (shown cameras only) & stepping between them
  const filtered = useMemo(() => {
    const out: Record<string, Marker[]> = {};
    for (const [id, lane] of Object.entries(lanes)) out[id] = lane.events.filter((e) => matches(e, filter));
    return out;
  }, [lanes, filter]);
  const tileKey = tileIds.join(",");
  const allFiltered = useMemo(
    () => Object.entries(filtered).filter(([id]) => tileIds.includes(id))
      .flatMap(([camId, evs]) => evs.map((e) => ({ ...e, camId }))).sort((a, b) => a.start_ts - b.start_ts),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [filtered, tileKey],
  );
  const inView = allFiltered.filter((e) => e.start_ts >= view.start && e.start_ts <= view.end).length;

  const jumpToEvent = (dir: 1 | -1) => {
    const ref = playhead ?? (view.start + view.end) / 2;
    const target = dir > 0
      ? allFiltered.find((e) => e.start_ts - 2 > ref + 0.5)
      : [...allFiltered].reverse().find((e) => e.start_ts - 2 < ref - 0.5);
    if (!target) {
      setMessage(dir > 0 ? "No later events match the filter in the loaded range" : "No earlier events match the filter in the loaded range");
      return;
    }
    const r = view.end - view.start;
    if (target.start_ts < view.start || target.start_ts > view.end) setView(clampView(target.start_ts - r / 2, target.start_ts + r / 2));
    setCam(target.camId);
    seekTo(target.start_ts - 2, target.camId, true, { noSkip: true, until: target.end_ts ?? target.start_ts });
  };

  // ---- controls
  const zoomTo = (seconds: number) => {
    const center = playhead ?? Math.min(nowS(), (view.start + view.end) / 2);
    setView(clampView(center - seconds * 0.7, center + seconds * 0.3));
  };
  const goLive = () => {
    const r = Math.min(range, 3600);
    setView(clampView(nowS() - r * 0.85, nowS() + r * 0.15));
    seekTo(nowS() - 15);
  };
  // "Live" = playing and within LIVE_WINDOW_S of now (playback trails the recorder by a few seconds)
  const isLive = playing && playhead != null && nowS() - playhead < LIVE_WINDOW_S;
  const togglePlay = () => {
    if (clockRef.current == null) seekTo(playhead ?? nowS() - 60);
    else setPlaying(!playing);
  };

  // Keyboard: space play/pause, arrows step, +/- zoom, [ ] events, 1-9 solo camera N, 0 back to the grid
  const onKey = (ev: React.KeyboardEvent) => {
    if ((ev.target as HTMLElement).closest("input, textarea, select")) return;
    if (ev.key === " ") { ev.preventDefault(); togglePlay(); }
    else if (ev.key === "ArrowLeft") seekTo((playhead ?? nowS()) - (ev.shiftKey ? 60 : 5));
    else if (ev.key === "ArrowRight") seekTo((playhead ?? nowS()) + (ev.shiftKey ? 60 : 5));
    else if (ev.key === "+" || ev.key === "=") zoomTo(Math.max(MIN_RANGE, range / 2));
    else if (ev.key === "-") zoomTo(Math.min(MAX_RANGE, range * 2));
    else if (ev.key === "[") jumpToEvent(-1);
    else if (ev.key === "]") jumpToEvent(1);
    else if (ev.key === "0") setConfig((c) => ({ ...c, solo: null }));
    else if (/^[1-9]$/.test(ev.key) && cameras[+ev.key - 1]) {
      const id = cameras[+ev.key - 1].id;
      setCam(id);
      setConfig((c) => ({ visible: c.visible && !c.visible.includes(id) ? [...c.visible, id] : c.visible, solo: id }));
    }
  };

  // ---- ticks
  const ticks = useMemo(() => {
    const pxPerSec = width / range;
    const step = STEPS.find((s) => s * pxPerSec >= 90) ?? STEPS[STEPS.length - 1];
    const shift = tzShift(view.start);
    const out: { t: number; label: string; major: boolean }[] = [];
    for (let local = Math.ceil((view.start + shift) / step) * step; local - shift <= view.end; local += step) {
      const t = local - shift;
      const d = new Date(t * 1000);
      out.push({ t, label: fmtTick(t, step), major: d.getHours() === 0 && d.getMinutes() === 0 && d.getSeconds() === 0 });
    }
    return out;
  }, [view.start, view.end, width, range]);

  const nowX = toX(nowS());
  const phX = playhead != null ? toX(playhead) : null;
  const cols = gridCols(tileIds.length);
  const previewWidth = solo || tileIds.length === 1 ? 960 : 640;
  const selectedLayout = layouts.find((l) => l.id === layoutId);

  return (
    <div className="view timeline-view" onKeyDown={onKey}>
      <div className="tl-player">
        {tileIds.length === 0 ? (
          <div className="tl-empty muted">All cameras are hidden. Turn one on with 👁 next to its name below.</div>
        ) : (
          <div className="sync-grid" style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
            {tileIds.map((id) => (
              <SyncTile
                key={id}
                cam={id}
                name={camName(id)}
                spans={lanes[id]?.spans}
                clockRef={clockRef}
                playing={playing}
                speed={speed}
                scrubbing={scrubbing}
                scrubT={scrubT}
                previewWidth={previewWidth}
                active={id === cam && tileIds.length > 1}
                soloed={solo === id}
                onSolo={() => toggleSolo(id)}
                onSelect={() => setCam(id)}
                statusRef={statusRef}
              />
            ))}
          </div>
        )}
        {playhead == null && tileIds.length > 0 && (
          <div className="tl-empty muted overlay-hint">{message || (COARSE ? "Tap the timeline to play. Pinch to zoom, drag to pan, drag the playhead to scrub."
                            : "Click the timeline to play. Scroll to zoom, drag to pan, drag the playhead to scrub.")}</div>
        )}
        {scrubbing && playhead != null && (
          <div className="tl-shuttle">
            <span className="tl-shuttle-dir">{scrubDir > 0 ? "▶▶" : "◀◀"}</span>
            {fmtClock(playhead)}
          </div>
        )}
      </div>

      {isPhone && cameras.length > 1 && (
        <div className="tl-cam-picker" role="tablist">
          {cameras.map((c) => (
            <button key={c.id} role="tab" aria-selected={solo === c.id} className={`chip ${solo === c.id ? "on-person" : ""}`}
              onClick={() => setConfig((cfg) => ({ ...cfg, solo: c.id }))}>{c.name}</button>
          ))}
        </div>
      )}
      <div className={`tl-controls ${isPhone ? "phone" : ""}`}>
        <button onClick={() => seekTo((playhead ?? nowS()) - 10)} title="Back 10 s (←)">⏪ 10s</button>
        <button onClick={togglePlay} className="tl-play">{playing ? "⏸ Pause" : "▶ Play"}</button>
        <button onClick={() => seekTo((playhead ?? nowS()) + 10)} title="Forward 10 s (→)">10s ⏩</button>
        <select value={speed} onChange={(e) => setSpeed(+e.target.value)} title="Playback speed">
          {SPEEDS.map((s) => <option key={s} value={s}>{s}×</option>)}
        </select>
        <span className="tl-clock">{playhead != null ? fmtClock(playhead) : "—"}</span>
        <span className="spacer" />
        {message && playhead != null && <span className="muted small">{message}</span>}
        <div className="segmented">
          {PRESETS.map(([label, s]) => (
            <button key={label} className={Math.abs(range - s) / s < 0.05 ? "active" : ""} onClick={() => zoomTo(s)}>{label}</button>
          ))}
        </div>
        <input
          type="datetime-local"
          title="Go to date and time"
          onChange={(e) => {
            const t = new Date(e.target.value).getTime() / 1000;
            if (!Number.isFinite(t)) return;
            const r = Math.min(range, 3600);
            setView(clampView(t - r / 2, t + r / 2));
            seekTo(t);
          }}
        />
        <button className={isLive ? "live-btn on" : "ghost live-btn"} onClick={goLive}
          title={isLive ? "Playing live (recordings reach the Timeline a few seconds behind real time)" : "Jump to live"}>● Live</button>
      </div>

      <button className="ghost small tl-more-toggle" onClick={() => setMoreOpen(!moreOpen)} aria-expanded={moreOpen}>
        {moreOpen ? "▾" : "▸"} Layout & filters{filterActive ? " (filtered)" : ""}
      </button>
      <div className={`tl-layout-bar tl-more ${moreOpen ? "open" : ""}`}>
        <span className="muted small">Layout</span>
        <select value={layoutId} onChange={(e) => pickLayout(+e.target.value)}>
          <option value={0}>All cameras</option>
          {layouts.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
        </select>
        {dirty && <span className="dirty-dot" title="Unsaved changes to this layout">●</span>}
        {layoutId !== 0 && <button className="ghost small" disabled={!dirty} onClick={saveLayout}>Save</button>}
        <button className="ghost small" onClick={saveLayoutAs}>Save as…</button>
        {selectedLayout && <button className="ghost small" onClick={renameLayout}>Rename</button>}
        {selectedLayout && <button className="ghost small" onClick={deleteLayout}>Delete</button>}
        <span className="muted small">
          {solo ? `Isolated: ${camName(solo)} (press 0 or 🔍 to return to the grid)` : `${visibleIds.length} of ${cameras.length} cameras shown`}
        </span>
        {tileIds.length > HEAVY_TILES && <span className="muted small">· {tileIds.length} full-resolution streams at once is heavy; hide some for smoother playback</span>}
      </div>

      <div className={`tl-filters tl-more ${moreOpen ? "open" : ""}`}>
        <span className="muted small">Show</span>
        <button className={`chip ${filter.person ? "on-person" : ""}`} onClick={() => setFilter({ person: !filter.person })}>People</button>
        <button className={`chip ${filter.vehicle ? "on-vehicle" : ""}`} onClick={() => setFilter({ vehicle: !filter.vehicle })}>Vehicles</button>
        <select value={filter.status} onChange={(e) => setFilter({ status: e.target.value as Filter["status"] })} title="Verification status">
          <option value="verified">Verified</option>
          <option value="active">In progress</option>
          <option value="rejected">Rejected</option>
          <option value="all">All statuses</option>
        </select>
        <select value={filter.minThreat} onChange={(e) => setFilter({ minThreat: e.target.value as Filter["minThreat"] })} title="Minimum priority: the higher of Qwen's threat level and how unusual the event is for its camera (time, place, how long it stayed). Your synopsis corrections and false-alarm verdicts override it.">
          <option value="any">Any priority</option>
          <option value="low">Low and above</option>
          <option value="medium">Medium and above</option>
          <option value="high">High only</option>
        </select>
        <ConfidenceSlider value={filter.minYolo} onChange={(v) => setFilter({ minYolo: v })} />
        <label className="row small"><input type="checkbox" checked={filter.hideFalseAlarms} onChange={(e) => setFilter({ hideFalseAlarms: e.target.checked })} /> Hide false alarms</label>
        <label className="row small"><input type="checkbox" checked={filter.synopsisOnly} onChange={(e) => setFilter({ synopsisOnly: e.target.checked })} /> With synopsis only</label>
        <button className="ghost small" onClick={() => { setFilterState(DEFAULT_FILTER); try { localStorage.removeItem("timelineFilter"); } catch { /* ignore */ } }}>Reset</button>
        <label className="row small" title="Draw lines between sightings of the same person on different cameras">
          <input type="checkbox" checked={showJourneys} onChange={(e) => { setShowJourneys(e.target.checked); saveNumber("timelineShowJourneys", e.target.checked ? 1 : 0); }} /> Show journeys
        </label>
        <span className="spacer" />
        <span className="muted small">{inView} matching in view</span>
        <button className="ghost small" onClick={() => jumpToEvent(-1)} title="Previous matching event ([)">◀ Prev event</button>
        <button className="ghost small" onClick={() => jumpToEvent(1)} title="Next matching event (])">Next event ▶</button>
      </div>

      {focus && (
        <div className="focus-bar">
          <span className="focus-dot" />
          {focus.eventId
            ? <span><strong>Event #{focus.eventId}</strong> · {focus.label} · {camName(focus.cam)} · {fmtClock(focus.start)} · {Math.max(1, Math.round(focus.end - focus.start))} s</span>
            : <span><strong>{camName(focus.cam)}</strong> · {fmtClock(focus.start)} · from footage search</span>}
          <span className="spacer" />
          <button className="ghost small" onClick={replayFocus} title={`Play from ${FOCUS_PREROLL_S} s before`}>↺ Replay{focus.eventId ? " event" : ""}</button>
          {focus.eventId ? <button className="ghost small" onClick={() => setOpen(focus.eventId)}>Details</button> : null}
          <button className="ghost small" onClick={() => navigator.clipboard?.writeText(location.href).catch(() => {})} title="Copy a link to this event on the Timeline">Copy link</button>
          <button className="ghost small" onClick={() => onClearFocus?.()} aria-label="Clear focus">✕</button>
        </div>
      )}
      {lockPrompt && (
        <form className="lock-bar" onSubmit={(ev) => { ev.preventDefault(); saveLock(); }}>
          <span>🔒 Lock <strong>{camName(lockPrompt.cam)}</strong> {fmtClock(lockPrompt.start)} → {fmtClock(lockPrompt.end).slice(-8)} ({Math.round(lockPrompt.end - lockPrompt.start)} s)</span>
          <input autoFocus placeholder="Reason (optional)" value={lockPrompt.note} onChange={(e) => setLockPrompt({ ...lockPrompt, note: e.target.value })} />
          <button type="submit">Lock range</button>
          <button type="button" className="ghost" onClick={() => { setLockPrompt(null); setSelection(null); }}>Cancel</button>
        </form>
      )}
      {openLock && (
        <div className="lock-bar">
          <span>🔒 Locked {camName(openLock.camera_id)} {fmtClock(openLock.start_ts)} → {fmtClock(openLock.end_ts).slice(-8)}{openLock.note ? ` · ${openLock.note}` : ""}{openLock.event_id ? ` · event #${openLock.event_id}` : ""}</span>
          <button className="ghost" onClick={() => { setView(clampView(openLock.start_ts - 60, openLock.end_ts + 60)); seekTo(openLock.start_ts, openLock.camera_id, true, { noSkip: true, until: openLock.end_ts }); setOpenLock(null); }}>Play</button>
          <button className="ghost" onClick={async () => {
            if (!await confirmDialog("Unlock this range?", { message: "It will follow the retention policy again.", confirmLabel: "Unlock", danger: true })) return;
            await api.deleteLock(openLock.id); setOpenLock(null); load(viewRef.current); toast.success("Unlocked");
          }}>Unlock</button>
          <button className="ghost" onClick={() => setOpenLock(null)}>Close</button>
        </div>
      )}
      <div className="tl-wrap">
      {hover && !drag.current && hoverFrames.shot && hoverFrames.shot.cam === hover.cam && (
        <div className="tl-thumb" style={{ left: Math.min(Math.max(hover.x + 150 - 160, 0), width + 150 - 320) }}>
          {hoverFrames.shot.url ? <img src={hoverFrames.shot.url} alt="" /> : <div className="tl-thumb-gap">No recording</div>}
          <div className="tl-thumb-label">{camName(hover.cam)} · {fmtClock(hover.t)}</div>
        </div>
      )}
      <div className="tl" tabIndex={0}>
        <div className="tl-names">
          <div className="tl-axis-spacer" />
          {cameras.map((c) => {
            const shown = visibleIds.includes(c.id);
            const dim = solo != null && solo !== c.id;
            return (
              <div key={c.id} className={`tl-name-row ${shown ? "" : "lane-hidden"} ${dim ? "lane-dimmed" : ""} ${c.id === cam ? "active" : ""}`}>
                <button className={`lane-btn lane-eye ${shown ? "on" : ""}`} onClick={() => toggleVisible(c.id)}
                  title={shown ? "Hide this camera" : "Show this camera"} aria-pressed={shown}>{shown ? "👁" : "◌"}</button>
                <button className="tl-name" onClick={() => setCam(c.id)} title={c.name}>{c.name}</button>
                {shown && (
                  <button className={`lane-btn lane-solo ${solo === c.id ? "on" : ""}`} onClick={() => toggleSolo(c.id)}
                    title={solo === c.id ? "Back to all shown cameras" : "Isolate this camera"} aria-pressed={solo === c.id}>🔍</button>
                )}
              </div>
            );
          })}
        </div>
        <div
          ref={track}
          className={`tl-track ${drag.current?.mode === "scrub" ? "scrubbing" : hover && nearPlayhead(hover.x) ? "grab-playhead" : ""}`}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={(ev) => { pointers.current.delete(ev.pointerId); pinch.current = null; drag.current = null; setScrubbing(false); }}
          onPointerLeave={() => {
            if (drag.current) return;
            setHover(null);
            hoverFrames.clear();
          }}
        >
          <div className="tl-axis" title="Click or drag here to scrub">
            {ticks.map((t) => (
              <div key={t.t} className={`tl-tick ${t.major ? "major" : ""}`} style={{ left: toX(t.t) }}>
                <span>{t.label}</span>
              </div>
            ))}
          </div>
          {cameras.map((c) => {
            const lane = lanes[c.id];
            const shown = visibleIds.includes(c.id);
            const dim = solo != null && solo !== c.id;
            if (!shown) return <div key={c.id} className="tl-lane lane-hidden" data-cam={c.id} />;
            return (
              <div key={c.id} className={`tl-lane ${c.id === cam ? "active" : ""} ${dim ? "lane-dimmed" : ""}`} data-cam={c.id}>
                {lane?.spans.map((s, i) => {
                  const x1 = Math.max(-2, toX(s.start));
                  const x2 = Math.min(width + 2, toX(s.end));
                  return x2 > x1 ? <div key={i} className="tl-span" style={{ left: x1, width: Math.max(1, x2 - x1) }} /> : null;
                })}
                {lane?.kept.map((k, i) => {
                  const x1 = Math.max(-2, toX(k.start_ts));
                  const x2 = Math.min(width + 2, toX(k.end_ts));
                  return x2 > x1 ? <div key={`k${i}`} className="tl-kept" style={{ left: x1, width: Math.max(2, x2 - x1) }} title={`AI-kept: ${k.reasons.join(", ")}`} /> : null;
                })}
                {lane?.locks.map((lk) => {
                  const x1 = Math.max(-2, toX(lk.start_ts));
                  const x2 = Math.min(width + 2, toX(lk.end_ts));
                  return x2 > x1 ? (
                    <div key={`l${lk.id}`} className="tl-lock" style={{ left: x1, width: Math.max(3, x2 - x1) }}
                      title={`Locked${lk.note ? `: ${lk.note}` : ""}`}
                      onPointerDown={(ev) => ev.stopPropagation()}
                      onClick={(ev) => { ev.stopPropagation(); setOpenLock(lk); }} />
                  ) : null;
                })}
                {selection && selection.cam === c.id && (
                  <div className="tl-selection" style={{ left: toX(selection.start), width: Math.max(1, toX(selection.end) - toX(selection.start)) }} />
                )}
                {filtered[c.id]?.map((e) => {
                  const x = toX(e.start_ts);
                  if (x < -10 || x > width + 10) return null;
                  const w = Math.max(3, toX(e.end_ts ?? e.start_ts) - x);
                  return (
                    <div
                      key={e.id}
                      className={`tl-marker ${e.camera_class} ${e.status} ${(e.priority ?? e.threat) && (e.priority ?? e.threat) !== "none" ? "threat" : ""}`}
                      style={{ left: x, width: w }}
                      title={`${e.yolo_class ?? e.camera_class} · ${fmtTime(e.start_ts)} · ${e.status}${e.threat ? ` · threat ${e.threat}` : ""}${e.priority && e.priority !== "none" ? ` · priority ${e.priority}` : ""}${(e.anomaly ?? 0) >= UNUSUAL_MIN ? " · unusual" : ""}${e.verdict ? ` · ${e.verdict.replace("_", " ")}` : ""}`}
                      onClick={(ev) => {
                        ev.stopPropagation();
                        setOpen(e.id);
                      }}
                    />
                  );
                })}
                {focusMembers.filter((m) => m.cam === c.id).map((m) => {
                  const x = toX(m.start);
                  if (x < -10 || x > width + 10) return null;
                  const ev = lane?.events.find((e) => e.id === m.id);
                  return (
                    <div key={`focus${m.id}`} className={`tl-marker focus ${ev?.camera_class ?? "person"}`}
                      style={{ left: x, width: Math.max(4, toX(m.end) - x) }}
                      title={`Event #${m.id}`}
                      onClick={(e2) => { e2.stopPropagation(); if (m.id) setOpen(m.id); }} />
                  );
                })}
              </div>
            );
          })}
          {connectors.length > 0 && (
            <svg className="tl-connectors" width={width} height="100%">
              {connectors.map((sg, i) => {
                const ya = laneY(sg.a.cam), yb = laneY(sg.b.cam);
                if (ya == null || yb == null) return null;
                const xa = toX(sg.a.end), xb = toX(sg.b.start);
                if (Math.max(xa, xb) < -20 || Math.min(xa, xb) > width + 20) return null;
                const mx = (xa + xb) / 2;
                return <path key={i} className={sg.focus ? "focus" : ""} d={`M${xa},${ya} C${mx},${ya} ${mx},${yb} ${xb},${yb}`} />;
              })}
            </svg>
          )}
          {nowX >= 0 && nowX <= width && <div className="tl-now" style={{ left: nowX }} title="Now" />}
          {hover && !drag.current && (
            <div className="tl-hover" style={{ left: hover.x }}>
              <span>{fmtClock(hover.t)}</span>
            </div>
          )}
          {phX != null && phX >= -10 && phX <= width + 10 && (
            <div className="tl-playhead" style={{ left: phX }}>
              <div className="tl-handle" title="Drag to scrub" />
              {drag.current?.mode === "scrub" && playhead != null && <span className="tl-scrub-time">{fmtClock(playhead)}</span>}
            </div>
          )}
        </div>
      </div>
      </div>
      <div className="legend muted small">
        <span><i className="sw span" /> recorded</span>
        <span><i className="sw kept" /> AI-kept (past continuous window)</span>
        <span><i className="sw locked" /> locked</span>
        <span><i className="sw person" /> person</span>
        <span><i className="sw vehicle" /> vehicle</span>
        <span>👁 show/hide · 🔍 isolate (1–9, 0 = grid) · double-click a tile to isolate · scroll to zoom · drag to pan · drag the playhead to scrub · Space / ← → / + − / [ ] events · Shift+drag a lane to lock</span>
      </div>
      {open !== null && <EventDetail id={open} cameraName={camName} onClose={() => setOpen(null)} />}
    </div>
  );
}
