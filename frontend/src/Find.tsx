import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { api, ask, fmtTime, type AskMeta, type AssistantMessage, type AssistantThread, type Camera, type NvrEvent, type ParsedQuery } from "./api";
import { Answer } from "./Ask";
import { ActionCard, type ActionPlanCore } from "./ActionCard";
import { ConfidenceSlider, loadNumber, saveNumber } from "./ConfidenceSlider";
import { EventCard } from "./Events";
import { EventDetail } from "./EventDetail";
import { FindSummary } from "./FindSummary";
import {
  DEFAULT_VIEW_KEY, FALLBACK_VIEW, FLAGS, PRESETS, PRIORITIES, SUGGESTIONS, buildQuery, defaultViewId, eventQuery, filterWindow,
  fromSaved, hourGroups, matchesFilters, parseQuery, placeNames, resolveFilters, sameFilters, toSaved,
  type FindFilters, type FindFlag, type FindMode, type FindView as View, type UrlState,
} from "./findViews";
import { FootageResults } from "./FootageSearch";
import { IdentitiesView } from "./Identities";
import { promptDialog, SkeletonGrid, toast } from "./ui";

const RANGES: [string, number][] = [["Any time", 0], ["Last hour", 1], ["24 h", 24], ["7 days", 168], ["30 days", 720]];
const STATUSES: [string, string][] = [["verified", "Verified"], ["open,pending", "In progress"], ["rejected", "Rejected"], ["", "Everything"]];
const PAGE = 60;
const SEARCH_PAGE = 100;
const THREADS_CAP = 30;  // /api/assistant/threads returns at most this many by default
const RANK: Record<string, number> = { none: 0, low: 1, medium: 2, high: 3 };

type Pending = { question: string; answer: string; meta?: AskMeta; model?: string; fallback?: string; error?: string };

const readDefault = () => { try { return localStorage.getItem(DEFAULT_VIEW_KEY); } catch { return null; } };
const writeDefault = (id: string) => { try { localStorage.setItem(DEFAULT_VIEW_KEY, id); } catch { /* private mode */ } };
/** The page's own query string only: under the hub the path is /s/<site>/ and must stay as it is. */
const writeUrl = (query: string, push: boolean) => {
  const url = `?${query}${location.hash}`;
  if (`?${location.search.slice(1)}${location.hash}` === url) return;
  try { (push ? history.pushState : history.replaceState).call(history, null, "", url); } catch { /* sandboxed */ }
};

/** One page for events. With an empty box it browses the latest events for the active view (live, older ones
 * as you scroll); typed text searches events by meaning plus footage by looks; Ask has Qwen look things up and
 * answer in words above those same results. Views (presets per role, plus saved ones) set the filters. */
/** Text that reads as a site action (backend site_actions.py) goes to the planner on plain Enter too. */
const INSTRUCTION = /^(please\s+)?(rename|set\s+(the\s+)?retention|keep\s+\d+|lock|protect|stop\s+describing|start\s+describing|describe\s+only)\b/i;

export function FindView({ cameras, live }: { cameras: Camera[]; live: NvrEvent | null }) {
  // ---- views and filters (URL > starred default > Attention)
  const initial = useMemo<UrlState & { id: string }>(() => {
    const u = parseQuery(typeof location === "undefined" ? "" : location.search);
    return { ...u, id: u.view ?? readDefault() ?? FALLBACK_VIEW };
  }, []);
  const storedYolo = () => loadNumber("minYolo.search");
  const presetOf = (id: string) => PRESETS.find((v) => v.id === id);
  const [saved, setSaved] = useState<View[] | null>(null);       // null until loaded
  const [viewId, setViewId] = useState(initial.id);
  const [filters, setFilters] = useState<FindFilters>(() => ({ ...resolveFilters(presetOf(initial.id) ?? presetOf(FALLBACK_VIEW), storedYolo()), ...initial.patch }));
  const [mode, setMode] = useState<"sightings" | "grouped">(() => (initial.mode ?? presetOf(initial.id)?.mode) === "grouped" ? "grouped" : "sightings");
  const [defaultId, setDefaultId] = useState(readDefault);
  // a saved view named in the URL (or starred) is only known once the saved views load
  const [ready, setReady] = useState(() => !!presetOf(initial.id));
  const views = useMemo(() => [...PRESETS, ...(saved ?? [])], [saved]);
  const view = views.find((v) => v.id === viewId) ?? presetOf(FALLBACK_VIEW)!;
  const viewFilters = resolveFilters(view, filters.minYolo);  // the slider never counts as an unsaved change
  const viewMode: FindMode = view.mode;
  const findMode: FindMode = mode === "grouped" ? "grouped" : "events";
  const dirty = !sameFilters(filters, viewFilters) || findMode !== viewMode;
  const setF = (patch: Partial<FindFilters>) => setFilters((f) => ({ ...f, ...patch }));
  const { camera, label, hours, day, minYolo, status } = filters;

  const [q, setQ] = useState(initial.q);
  const [submitted, setSubmitted] = useState("");   // the text the results below are for ("" = browsing)
  const [parsed, setParsed] = useState<ParsedQuery | null>(null);  // time window / footage phrase read from it
  const [ppeZone, setPpeZone] = useState("");       // summary strip: one PPE zone only (not part of a view)
  // results
  const [events, setEvents] = useState<NvrEvent[] | null>(null);
  const [more, setMore] = useState(false);
  const [older, setOlder] = useState(0);  // search matches before the selected time chip's window
  const [busy, setBusy] = useState(false);
  const [nonce, setNonce] = useState(0);
  const [open, setOpen] = useState<number | null>(null);
  const [focus, setFocus] = useState<number | null>(null);  // keyboard: index into events
  const sentinel = useRef<HTMLDivElement>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const loadingMore = useRef(false);
  // conversation
  const [threads, setThreads] = useState<AssistantThread[]>([]);
  const [threadId, setThreadId] = useState<number | null>(null);
  const [messages, setMessages] = useState<AssistantMessage[]>([]);
  const [pending, setPending] = useState<Pending | null>(null);
  const [showHistory, setShowHistory] = useState(false);
  const answerRef = useRef<HTMLDivElement>(null);
  const name = (id: string) => cameras.find((c) => c.id === id)?.name ?? id;
  const browsing = !submitted;

  useEffect(() => {
    api.findViews().then((r) => r.views.map(fromSaved)).catch(() => [] as View[]).then((list) => {
      setSaved(list);
      if (!ready) {  // the URL / star named a saved view: apply it now (or fall back to Attention)
        const v = list.find((x) => x.id === initial.id);
        if (!v) setViewId(FALLBACK_VIEW);
        setFilters({ ...resolveFilters(v ?? presetOf(FALLBACK_VIEW), storedYolo()), ...initial.patch });
        setMode((initial.mode ?? v?.mode) === "grouped" ? "grouped" : "sightings");
        setReady(true);
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Switch view: its filters replace the current ones (search text stays). */
  const chooseView = (v: View) => {
    setViewId(v.id);
    setFilters(resolveFilters(v, minYolo));
    setMode(v.mode === "grouped" ? "grouped" : "sightings");
    setPpeZone("");
    pushNext.current = true;
    if (v.focusSearch) setTimeout(() => searchInput.current?.focus(), 0);
  };
  // URL state: a view switch is a history entry (back/forward), filter tweaks replace it
  const pushNext = useRef(false);
  useEffect(() => {
    if (!ready) return;
    writeUrl(buildQuery(view.id, filters, viewFilters, submitted, findMode, viewMode), pushNext.current);
    pushNext.current = false;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, viewId, filters, submitted, mode]);
  useEffect(() => {
    const on = () => {
      const u = parseQuery(location.search);
      const v = views.find((x) => x.id === (u.view ?? defaultViewId(views, readDefault())));
      if (!v) return;
      setViewId(v.id);
      setFilters({ ...resolveFilters(v, minYolo), ...u.patch });
      setMode((u.mode ?? v.mode) === "grouped" ? "grouped" : "sightings");
      if (u.q) { setQ(u.q); search(u.q); } else clearSearch();
    };
    window.addEventListener("popstate", on);
    return () => window.removeEventListener("popstate", on);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [views, minYolo]);

  const persistSaved = async (list: View[]) => {
    try {
      const r = await api.saveFindViews(list.map((v) => toSaved(v.id, v.name, resolveFilters(v), v.mode)));
      setSaved(r.views.map(fromSaved));
      return true;
    } catch (e) { toast.error(e); return false; }
  };
  const saveCurrent = async (asNew: boolean) => {
    const nm = asNew ? await promptDialog("Save this view", { label: "Name", initial: view.builtin ? `${view.name} (mine)` : view.name, confirmLabel: "Save" }) : view.name;
    if (!nm?.trim()) return;
    const id = asNew ? `v${Date.now().toString(36)}` : view.id;
    const v: View = { id, name: nm.trim().slice(0, 60), icon: "★", filters: { ...filters }, mode: findMode, builtin: false };
    const list = asNew ? [...(saved ?? []), v] : (saved ?? []).map((x) => (x.id === id ? v : x));
    if (await persistSaved(list)) { setViewId(id); toast.success(`Saved "${v.name}"`); }
  };
  const deleteSaved = async (v: View) => {
    if (await persistSaved((saved ?? []).filter((x) => x.id !== v.id))) {
      if (defaultId === v.id) { writeDefault(FALLBACK_VIEW); setDefaultId(FALLBACK_VIEW); }
      if (viewId === v.id) chooseView(presetOf(FALLBACK_VIEW)!);
    }
  };
  const star = (v: View) => { writeDefault(v.id); setDefaultId(v.id); toast.success(`"${v.name}" opens by default`); };

  const loadThreads = () => api.assistantThreads().then(setThreads).catch(() => {});
  useEffect(() => {
    loadThreads();
    api.remoteWarm().catch(() => {}); // a cold remote model (if configured) starts loading while you type
    // a question handed over from the Home dashboard's Ask box
    let handed: string | null = null;
    try { handed = sessionStorage.getItem("findAsk"); sessionStorage.removeItem("findAsk"); } catch { /* ignore */ }
    if (handed) askNvr(handed);
    else if (initial.q) search(initial.q);  // a link with ?q=…
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    if (threadId == null) { setMessages([]); return; }
    api.assistantThread(threadId).then((t) => setMessages(t.messages)).catch(() => setMessages([]));
  }, [threadId]);

  // ---- browse: the latest events for the filters, older ones as you scroll
  const browseParams = () => ({ ...eventQuery(filters), ppe_zone: ppeZone || undefined, sort: filters.sort });
  const filtersKey = JSON.stringify(filters) + ppeZone;
  useEffect(() => {
    if (!ready || !browsing || mode !== "sightings") return;
    setBusy(true); setEvents(null); setFocus(null);
    api.events({ ...browseParams(), limit: PAGE }).then((r) => { setEvents(r); setMore(r.length === PAGE); }).catch(() => setEvents([])).finally(() => setBusy(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, browsing, mode, filtersKey]);

  // a new or updated event from the live feed slots into the browse grid when it matches the filters
  useEffect(() => {
    if (!live || !browsing || mode !== "sightings") return;
    const ok = matchesFilters(live, filters) && (!ppeZone || live.detections?.ppe?.zone === ppeZone);
    const byPriority = filters.sort === "priority";
    setEvents((prev) => {
      const rest = (prev ?? []).filter((x) => x.id !== live.id);
      if (!ok) return rest;
      return [live, ...rest].sort((a, b) => (byPriority ? (RANK[b.priority ?? "none"] ?? 0) - (RANK[a.priority ?? "none"] ?? 0) : 0) || b.id - a.id);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live]);

  useEffect(() => {
    const on = (ev: Event) => { const id = (ev as CustomEvent<number>).detail; setEvents((prev) => prev && prev.filter((x) => x.id !== id)); };
    window.addEventListener("nvr:event_removed", on);
    return () => window.removeEventListener("nvr:event_removed", on);
  }, []);

  const loadMore = async () => {
    if (loadingMore.current || !more || !events?.length) return;
    loadingMore.current = true;
    try {
      // newest-first pages by id (live inserts don't shift it); priority order pages by offset
      const page = filters.sort === "priority" ? { offset: events.length } : { before_id: events[events.length - 1]?.id };
      const r = await api.events({ ...browseParams(), limit: PAGE, ...page });
      setEvents((p) => { const seen = new Set((p ?? []).map((x) => x.id)); return [...(p ?? []), ...r.filter((x) => !seen.has(x.id))]; });
      setMore(r.length === PAGE);
    } finally { loadingMore.current = false; }
  };
  useEffect(() => {
    const el = sentinel.current;
    if (!el || !browsing || mode !== "sightings") return;
    const io = new IntersectionObserver((entries) => { if (entries[0].isIntersecting) loadMore(); }, { rootMargin: "600px" });
    io.observe(el);
    return () => io.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events, more, browsing, mode]);

  // ---- search: same filters as browse (status aside: search looks through every outcome)
  const searchQuery = (f: FindFilters) => ({ ...eventQuery(f), status: undefined, ppe_zone: ppeZone || undefined });
  const search = async (text = q, f = filters) => {
    const t = text.trim();
    if (!t) { clearSearch(); return; }
    setBusy(true);
    setEvents(null); setMore(false); setFocus(null);
    // "today", "last night", "past 3 hours"... in the text set the window (over the time chips)
    const p = await api.parseQuery(t).catch(() => null);
    setParsed(p);
    setSubmitted(t);
    setNonce(Date.now());
    const [ws, wu] = filterWindow(f);
    const s = p?.time_label ? p.since ?? undefined : ws;
    const u = p?.time_label ? p.until ?? undefined : wu;
    try {
      // "what happened overnight?" has nothing to search by meaning: list that period's events, newest first
      const r = p?.listing
        ? await api.events({ ...searchQuery(f), since: s, until: u, limit: SEARCH_PAGE })
        : await api.search(p?.text || t, { ...searchQuery(f), since: s, until: u, limit: SEARCH_PAGE });
      setEvents(r); setMore(r.length === SEARCH_PAGE);
    } catch { setEvents([]); }
    setBusy(false);
    // a time chip is hiding older matches? count them so the page can say so instead of looking empty
    setOlder(0);
    if (s && !p?.time_label && !f.day && !p?.listing) {
      api.search(p?.text || t, { ...searchQuery(f), since: undefined, until: undefined, limit: 200 })
        .then((all) => setOlder(all.filter((e) => e.start_ts < s).length)).catch(() => {});
    }
  };
  const searchMore = async () => {
    if (!events?.length || busy) return;
    const [ws, wu] = filterWindow(filters);
    const s = parsed?.time_label ? parsed.since ?? undefined : ws;
    const u = parsed?.time_label ? parsed.until ?? undefined : wu;
    setBusy(true);
    try {
      const r = parsed?.listing
        ? await api.events({ ...searchQuery(filters), since: s, until: u, limit: SEARCH_PAGE, before_id: events[events.length - 1].id })
        : await api.search(parsed?.text || submitted, { ...searchQuery(filters), since: s, until: u, limit: SEARCH_PAGE, offset: events.length });
      setEvents((p) => [...(p ?? []), ...r]); setMore(r.length === SEARCH_PAGE);
    } catch { setMore(false); }
    setBusy(false);
  };
  const clearSearch = () => { setSubmitted(""); setParsed(null); setQ(""); setOlder(0); };
  // filters changed: refresh the search results for the current text (browse refreshes through its own effect)
  const searchKey = JSON.stringify({ ...filters, status: "", sort: "" }) + ppeZone;
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (submitted) search(submitted); }, [searchKey]);

  const [action, setAction] = useState<ActionPlanCore | null>(null);
  const askNvr = async (text = q) => {
    const question = text.trim();
    if (!question || pending) return;
    // an instruction ("Rename cam3 to Loading Dock", "Lock Side Yard footage 3-4 pm today") gets a confirmation
    // card instead of an answer; if the planner fails, the question is simply asked as before
    const planned = await api.assistantPlan(question).catch(() => null);
    if (planned && planned.action !== "none") { setAction(planned as ActionPlanCore); setQ(""); return; }
    setAction(null);
    setQ("");
    search(question);
    setPending({ question, answer: "" });
    setTimeout(() => answerRef.current?.scrollIntoView({ block: "start", behavior: "smooth" }), 50);
    let tid = threadId;
    try {
      await ask(tid, question, (c) => {
        if (c.type === "thread") tid = c.thread_id;
        else if (c.type === "calls") setPending((p) => p && { ...p, meta: { calls: c.calls, refs: c.refs, planner: c.planner } });
        else if (c.type === "model") setPending((p) => p && { ...p, model: c.model });
        else if (c.type === "fallback") setPending((p) => p && { ...p, fallback: c.reason });
        else if (c.type === "delta") setPending((p) => p && { ...p, answer: p.answer + c.text });
        else if (c.type === "error") setPending((p) => p && { ...p, error: c.error });
      });
    } catch (e) {
      setPending((p) => p && { ...p, error: String(e) });
      return;
    }
    if (tid != null) {
      const t = await api.assistantThread(tid);
      setMessages(t.messages);
      if (tid !== threadId) setThreadId(tid);
    }
    setPending((p) => (p?.error ? p : null));
    loadThreads();
  };

  const onKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") { e.currentTarget.blur(); return; }
    if (e.key !== "Enter") return;
    e.preventDefault();
    // a question (ends with "?"), an instruction ("Rename cam3 to …") or Shift+Enter asks; plain Enter searches instantly
    if (e.shiftKey || q.trim().endsWith("?") || INSTRUCTION.test(q.trim())) askNvr(); else search();
  };

  // ---- keyboard: "/" focuses search, j/k (or ←/→) move between cards, Enter opens (Esc closes the detail)
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if (open !== null || e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
      if (e.key === "/") { e.preventDefault(); searchInput.current?.focus(); return; }
      if (!events?.length || mode === "grouped") return;
      const step = e.key === "j" || e.key === "ArrowRight" ? 1 : e.key === "k" || e.key === "ArrowLeft" ? -1 : 0;
      if (step) {
        e.preventDefault();
        const next = Math.max(0, Math.min(events.length - 1, focus === null ? 0 : focus + step));
        setFocus(next);
        document.querySelector(`[data-event-id="${events[next].id}"]`)?.scrollIntoView({ block: "nearest" });
      } else if (e.key === "Enter" && focus !== null && events[focus]) {
        e.preventDefault();
        setOpen(events[focus].id);
      }
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [events, focus, open, mode]);

  const hasConversation = messages.length > 0 || pending;
  const grouped = browsing && mode === "grouped";
  const places = placeNames(cameras, camera);
  if (filters.place && !places.includes(filters.place)) places.unshift(filters.place);
  const showSummary = !grouped && (filters.flags.includes("ppe") || filters.flags.includes("rule"));
  const suggestions = view.suggestions ?? SUGGESTIONS;
  const timeLabel = day ? new Date(day + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" }) : RANGES.find(([, h]) => h === hours)?.[0];
  const filterLabel = [
    camera ? name(camera) : "",
    filters.attention ? "needs attention" : "",
    filters.priority ? `${filters.priority}+ priority` : "",
    ...filters.flags.map((f) => FLAGS.find((x) => x.id === f)?.label.replace(/^[^\p{L}]+/u, "").toLowerCase() ?? f),
    filters.place ? `in ${filters.place}` : "",
    ppeZone ? `PPE zone ${ppeZone}` : "",
    browsing && status !== "verified" ? STATUSES.find(([v]) => v === status)?.[1].toLowerCase() ?? "" : "",
  ].filter(Boolean).join(" · ");
  const toggleFlag = (f: FindFlag) => setF({ flags: filters.flags.includes(f) ? filters.flags.filter((x) => x !== f) : [...filters.flags, f] });
  const card = (e: NvrEvent, i: number) => <EventCard key={e.id} e={e} cameraName={name(e.camera_id)} onOpen={() => { setFocus(i); setOpen(e.id); }} focused={focus === i} />;
  const identityHours = day ? 0 : hours >= 168 ? 168 : hours >= 72 ? 72 : hours >= 24 ? 24 : 0;
  return (
    <div className="view find">
      <div className="find-views" role="tablist" aria-label="Views">
        {views.map((v) => (
          <span key={v.id} className={`find-view ${v.id === viewId ? "active" : ""}`}>
            <button role="tab" aria-selected={v.id === viewId} className="linkish" onClick={() => chooseView(v)} title={v.title ?? v.name}>{v.icon} {v.name}</button>
            <button className={`linkish star ${defaultId === v.id || (!defaultId && v.id === FALLBACK_VIEW) ? "on" : ""}`}
              onClick={() => star(v)} title={defaultId === v.id ? "Opens by default" : "Open this view by default"} aria-label={`Open ${v.name} by default`}>★</button>
            {!v.builtin && <button className="linkish small" onClick={() => deleteSaved(v)} title="Delete this saved view" aria-label={`Delete ${v.name}`}>✕</button>}
          </span>
        ))}
        <button className="ghost small" onClick={() => saveCurrent(true)} title="Save the current filters as a view of your own">+ Save current…</button>
        {dirty && (
          <span className="muted small find-dirty">
            unsaved changes ·{" "}
            {!view.builtin && <><button className="linkish small" onClick={() => saveCurrent(false)}>Save</button> · </>}
            {view.builtin && <><button className="linkish small" onClick={() => saveCurrent(true)}>Save as…</button> · </>}
            <button className="linkish small" onClick={() => chooseView(view)}>Reset</button>
          </span>
        )}
      </div>
      <form className="search-bar" onSubmit={(e) => { e.preventDefault(); search(); }}>
        <input ref={searchInput} autoFocus value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={onKey}
          placeholder='Search "white pickup truck", or ask "Was anyone near the trailers after 6pm?"  ( / )' />
        {submitted && <button type="button" className="ghost" onClick={clearSearch} title="Back to the latest events">✕</button>}
        <button type="submit" disabled={busy || !q.trim()}>{busy && submitted ? "Searching…" : "Search"}</button>
        <button type="button" className="ask-btn" disabled={!!pending || !q.trim()} onClick={() => askNvr()} title="Have Qwen look it up and answer in words (Shift+Enter, or end with ?)">✦ Ask</button>
      </form>
      <div className="toolbar search-filters">
        {browsing && (
          <div className="segmented">
            <button className={mode === "sightings" ? "active" : ""} onClick={() => setMode("sightings")} title="Every event as its own card">Events</button>
            <button className={mode === "grouped" ? "active" : ""} onClick={() => setMode("grouped")} title="One row per person or vehicle, with all of their sightings">Grouped by who</button>
          </div>
        )}
        {!grouped && (
          <>
            <select value={camera} onChange={(e) => setF({ camera: e.target.value })}>
              <option value="">All cameras</option>
              {cameras.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
            <select value={label} onChange={(e) => setF({ label: e.target.value })}>
              <option value="">Any type</option>
              <option value="person">People</option>
              <option value="vehicle">Vehicles</option>
            </select>
            <div className="segmented" title="Time window (a typed phrase like “last night” wins over these)">
              {RANGES.map(([l, h]) => <button key={l} className={!day && hours === h ? "active" : ""} onClick={() => setF({ day: "", hours: h })}>{l}</button>)}
            </div>
            <label className="row small" title="One day">
              <input type="date" value={day} max={new Date().toISOString().slice(0, 10)} onChange={(e) => setF({ day: e.target.value })} />
              {day && <button className="linkish small" onClick={() => setF({ day: "" })}>✕</button>}
            </label>
            <select value={filters.priority} onChange={(e) => setF({ priority: e.target.value as FindFilters["priority"] })}
              title="Minimum priority: the higher of Qwen's threat level and how unusual the event is for its camera">
              {PRIORITIES.map(([v, l]) => <option key={l} value={v}>{l}</option>)}
            </select>
            {places.length > 0 && (
              <select value={filters.place} onChange={(e) => setF({ place: e.target.value })} title="Walked into this named area">
                <option value="">Any place</option>
                {places.map((p) => <option key={p} value={p}>📍 {p}</option>)}
              </select>
            )}
            {browsing && (
              <div className="segmented" title="Order of the list">
                <button className={filters.sort === "newest" ? "active" : ""} onClick={() => setF({ sort: "newest" })}>Newest</button>
                <button className={filters.sort === "priority" ? "active" : ""} onClick={() => setF({ sort: "priority" })}>Priority</button>
              </div>
            )}
            <ConfidenceSlider value={minYolo} onChange={(v) => { setF({ minYolo: v }); saveNumber("minYolo.search", v); }} />
            {browsing && (
              <select value={status} onChange={(e) => setF({ status: e.target.value })} title="Review: which of the verifier's outcomes to show">
                {STATUSES.map(([v, l]) => <option key={l} value={v}>{l}</option>)}
              </select>
            )}
          </>
        )}
        <span className="spacer" />
        <button className="ghost small" onClick={() => setShowHistory(!showHistory)}>{showHistory ? "▾" : "▸"} Conversations{threads.length ? ` (${threads.length >= THREADS_CAP ? `${THREADS_CAP}+` : threads.length})` : ""}</button>
        {hasConversation && <button className="ghost small" onClick={() => { setThreadId(null); setPending(null); }}>New conversation</button>}
      </div>
      {!grouped && (
        <div className="find-flags" role="group" aria-label="Flags">
          {filters.attention && (
            <button className="chip on" onClick={() => setF({ attention: false })} title="Priority medium+, a broken rule, unusual or watched · click to drop">⚑ Needs attention ✕</button>
          )}
          {FLAGS.map((f) => (
            <button key={f.id} className={`chip ${filters.flags.includes(f.id) ? "on" : ""}`} aria-pressed={filters.flags.includes(f.id)}
              onClick={() => toggleFlag(f.id)} title={`${f.title}${filters.flags.length ? " (all chosen flags must hold)" : ""}`}>{f.label}</button>
          ))}
        </div>
      )}
      {showHistory && (
        <div className="find-threads">
          {threads.length === 0 && <span className="muted small">No conversations yet.</span>}
          {threads.map((t) => (
            <span key={t.id} className={`chip ${t.id === threadId ? "on-person" : ""}`}>
              <button className="linkish" onClick={() => { setThreadId(t.id); setPending(null); setShowHistory(false); }} title={fmtTime(t.updated_at)}>{t.title}</button>
              <button className="linkish small" aria-label="Delete conversation" onClick={async () => { await api.deleteThread(t.id); if (t.id === threadId) setThreadId(null); loadThreads(); }}>✕</button>
            </span>
          ))}
        </div>
      )}

      {showSummary && (
        <FindSummary filters={filters} cameraName={name} zone={ppeZone} refresh={live?.policy ? live.id : undefined}
          onCamera={(c) => setF({ camera: c })} onDay={(d) => setF({ day: d })} onZone={setPpeZone} />
      )}

      {action && (
        <ActionCard key={action.id} plan={action} kind="site action" onClose={() => setAction(null)}
          onExecute={() => api.assistantExecute(action.id)} />
      )}

      {hasConversation && (
        <section className="find-answer" ref={answerRef}>
          {messages.map((m) => m.role === "user"
            ? <div key={m.id} className="ask-msg user">{m.content}</div>
            : <Answer key={m.id} text={m.content} meta={m.calls} model={m.model} onEvent={setOpen} />)}
          {pending && (
            <>
              <div className="ask-msg user">{pending.question}</div>
              {pending.error
                ? <div className="ask-msg assistant error">{pending.error}</div>
                : <Answer text={pending.answer || (pending.meta ? "…" : "Working out what to look up…")} meta={pending.meta ?? null}
                    model={pending.model ?? null} fallback={pending.fallback} onEvent={setOpen} streaming />}
            </>
          )}
        </section>
      )}

      {browsing && !hasConversation && (
        <div className="ask-suggestions" title="Search finds events by meaning and footage by looks; Ask has Qwen look things up and answer with links to the evidence">
          {suggestions.map((s) => <button key={s} className="ghost small" onClick={() => askNvr(s)}>✦ {s}</button>)}
        </div>
      )}

      {grouped ? <IdentitiesView key={`${camera}|${identityHours}`} cameras={cameras} initialCamera={camera} initialHours={identityHours} /> : browsing ? (
        <section>
          <h3>{view.name} <span className="muted small">{timeLabel}{filterLabel ? ` · ${filterLabel}` : ""}{filters.sort === "priority" ? " · most important first" : ""}</span></h3>
          {busy && !events && <SkeletonGrid n={6} />}
          {events && events.length === 0 && <div className="empty">No events match these filters{day || hours ? " in this period" : ""}.</div>}
          {filters.sort === "newest" ? (() => {
            let i = 0;
            return hourGroups(events ?? []).map((g) => (
              <Fragment key={g.key}>
                <h4 className="find-hour">{g.label} <span className="muted small">· {g.events.length} {g.events.length === 1 ? "event" : "events"}</span></h4>
                <div className="event-grid">{g.events.map((e) => card(e, i++))}</div>
              </Fragment>
            ));
          })() : <div className="event-grid">{events?.map(card)}</div>}
          <div ref={sentinel} className="center muted small">{more && events?.length ? "Loading older events…" : events?.length ? "That's everything." : ""}</div>
        </section>
      ) : (
        <>
          <section>
            <h3>Events <span className="muted small">{parsed?.listing ? "" : `matching "${parsed?.text || submitted}"`}{parsed?.time_label ? ` · ${parsed.time_label}` : timeLabel ? ` · ${timeLabel}` : ""}{filterLabel ? ` · ${filterLabel}` : ""}{events ? ` · ${events.length}${more ? "+" : ""}` : ""}</span></h3>
            {busy && !events && <SkeletonGrid n={3} />}
            {events && events.length === 0 && <div className="empty">No {parsed?.listing ? "" : "matching "}events {parsed?.time_label ? parsed.time_label : hours || day ? "in this period" : ""}.</div>}
            <div className="event-grid">{events?.map(card)}</div>
            {more && events && events.length > 0 && <div className="center"><button className="ghost" disabled={busy} onClick={searchMore}>{busy ? "Loading…" : `Show more matches`}</button></div>}
            {events && older > 0 && hours > 0 && !day && !parsed?.time_label && (
              <p className="muted small older-hint">
                {older} more before {fmtTime(Date.now() / 1000 - hours * 3600)} ·{" "}
                <button className="ghost small" onClick={() => setF({ hours: 0 })}>Show any time</button>
              </p>
            )}
          </section>
          {parsed?.footage_text !== null && (
            <section>
              <h3>Footage <span className="muted small">frames that look like "{parsed?.footage_text ?? submitted}"{parsed?.time_label ? ` · ${parsed.time_label}` : ""} · Qwen checks the best 8 · the outline is the part that matched</span></h3>
              <FootageResults q={parsed?.footage_text ?? submitted} nonce={nonce} cameras={cameras} camera={camera} sinceHours={day ? 0 : hours}
                window={parsed?.time_label ? { since: parsed.since, until: parsed.until } : day ? { since: filterWindow(filters)[0] ?? null, until: filterWindow(filters)[1] ?? null } : null} />
            </section>
          )}
          {parsed && parsed.footage_text === null && (
            <p className="muted small">Footage search is for things you can picture ("white van", "open gate"); {parsed.listing ? "this question is answered from the events above and by Ask" : "questions about people are answered from events above"}.</p>
          )}
        </>
      )}
      {open !== null && <EventDetail id={open} cameraName={name} onClose={() => setOpen(null)} />}
    </div>
  );
}
