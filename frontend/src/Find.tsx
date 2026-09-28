import { useEffect, useRef, useState } from "react";
import { api, ask, fmtTime, type AskMeta, type AssistantMessage, type AssistantThread, type Camera, type NvrEvent, type ParsedQuery } from "./api";
import { Answer } from "./Ask";
import { ConfidenceSlider, loadNumber, saveNumber } from "./ConfidenceSlider";
import { EventCard } from "./Events";
import { EventDetail } from "./EventDetail";
import { FootageResults } from "./FootageSearch";
import { IdentitiesView } from "./Identities";
import { SkeletonGrid } from "./ui";

const SUGGESTIONS = [
  "What happened overnight?",
  "Anything unusual today?",
  "Did anyone go outside today?",
  "How many people came through the East Door today?",
  "When was the last vehicle in the side yard?",
  "Was any camera offline in the last 24 hours?",
];
const RANGES: [string, number][] = [["Any time", 0], ["Last hour", 1], ["24 h", 24], ["7 days", 168]];
const STATUSES: [string, string][] = [["verified", "Verified"], ["open,pending", "In progress"], ["rejected", "Rejected"], ["", "Everything"]];
const PAGE = 60;

type Pending = { question: string; answer: string; meta?: AskMeta; model?: string; fallback?: string; error?: string };

/** One page for events. With an empty box it browses the latest events (live, older ones as you scroll);
 * typed text searches events by meaning plus footage by looks; Ask has Qwen look things up and answer in
 * words above those same results. One filter bar serves all three. */
export function FindView({ cameras, live }: { cameras: Camera[]; live: NvrEvent | null }) {
  const [q, setQ] = useState("");
  const [submitted, setSubmitted] = useState("");   // the text the results below are for ("" = browsing)
  const [parsed, setParsed] = useState<ParsedQuery | null>(null);  // time window / footage phrase read from it
  // filters shared by browse and search
  const [camera, setCamera] = useState("");
  const [label, setLabel] = useState("");           // "" | person | vehicle
  const [hours, setHours] = useState(24);
  const [day, setDay] = useState("");                // yyyy-mm-dd: that day only (overrides the chips)
  const [minYolo, setMinYolo] = useState(() => loadNumber("minYolo.search"));
  const [status, setStatus] = useState("verified");  // browse only: review the verifier's other outcomes
  const [mode, setMode] = useState<"sightings" | "grouped">(() => (loadNumber("eventsGrouped", 0) === 1 ? "grouped" : "sightings"));
  // results
  const [events, setEvents] = useState<NvrEvent[] | null>(null);
  const [more, setMore] = useState(false);
  const [older, setOlder] = useState(0);  // search matches before the selected time chip's window
  const [busy, setBusy] = useState(false);
  const [nonce, setNonce] = useState(0);
  const [open, setOpen] = useState<number | null>(null);
  const sentinel = useRef<HTMLDivElement>(null);
  const loadingMore = useRef(false);
  // conversation
  const [threads, setThreads] = useState<AssistantThread[]>([]);
  const [threadId, setThreadId] = useState<number | null>(null);
  const [messages, setMessages] = useState<AssistantMessage[]>([]);
  const [pending, setPending] = useState<Pending | null>(null);
  const [showHistory, setShowHistory] = useState(false);
  const answerRef = useRef<HTMLDivElement>(null);
  const name = (id: string) => cameras.find((c) => c.id === id)?.name ?? id;

  /** The time window from the day picker or the chips: [since, until]. */
  const window_ = (): [number | undefined, number | undefined] => {
    if (day) {
      const a = new Date(day + "T00:00:00"), b = new Date(day + "T23:59:59");
      if (Number.isFinite(a.getTime())) return [a.getTime() / 1000, b.getTime() / 1000];
    }
    return [hours ? Date.now() / 1000 - hours * 3600 : undefined, undefined];
  };
  const browsing = !submitted;

  const loadThreads = () => api.assistantThreads().then(setThreads).catch(() => {});
  useEffect(() => {
    loadThreads();
    api.remoteWarm().catch(() => {}); // a cold remote model (if configured) starts loading while you type
  }, []);
  useEffect(() => {
    if (threadId == null) { setMessages([]); return; }
    api.assistantThread(threadId).then((t) => setMessages(t.messages)).catch(() => setMessages([]));
  }, [threadId]);

  // ---- browse: the latest events for the filters, older ones as you scroll
  const browseParams = () => {
    const [since, until] = window_();
    return { camera: camera || undefined, status: status || undefined, label: label || undefined, min_yolo: minYolo || undefined, since, until };
  };
  useEffect(() => {
    if (!browsing || mode !== "sightings") return;
    setBusy(true); setEvents(null);
    api.events({ ...browseParams(), limit: PAGE }).then((r) => { setEvents(r); setMore(r.length === PAGE); }).catch(() => setEvents([])).finally(() => setBusy(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [browsing, mode, camera, status, label, minYolo, hours, day]);

  // a new or updated event from the live feed slots into the browse grid when it matches the filters
  useEffect(() => {
    if (!live || !browsing || mode !== "sightings") return;
    const [since, until] = window_();
    const ok = (!status || status.split(",").includes(live.status)) && (!camera || camera === live.camera_id) && (!label || label === live.camera_class)
      && (!minYolo || live.status === "open" || live.status === "pending" || (live.yolo_conf ?? 0) >= minYolo)
      && (!since || live.start_ts >= since) && (!until || live.start_ts <= until);
    setEvents((prev) => {
      const rest = (prev ?? []).filter((x) => x.id !== live.id);
      return ok ? [live, ...rest].sort((a, b) => b.id - a.id) : rest;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live]);

  const loadMore = async () => {
    if (loadingMore.current || !more || !events?.length) return;
    loadingMore.current = true;
    try {
      const r = await api.events({ ...browseParams(), limit: PAGE, before_id: events[events.length - 1]?.id });
      setEvents((p) => [...(p ?? []), ...r]);
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

  // ---- search
  const search = async (text = q, conf = minYolo) => {
    const t = text.trim();
    if (!t) { clearSearch(); return; }
    setBusy(true);
    setEvents(null); setMore(false);
    // "today", "last night", "past 3 hours"... in the text set the window (over the time chips)
    const p = await api.parseQuery(t).catch(() => null);
    setParsed(p);
    setSubmitted(t);
    setNonce(Date.now());
    const [ws, wu] = window_();
    const s = p?.time_label ? p.since ?? undefined : ws;
    const u = p?.time_label ? p.until ?? undefined : wu;
    try {
      const r = await api.search(p?.text || t, camera || undefined, conf, s, u);
      setEvents(label ? r.filter((e) => e.camera_class === label) : r);
    } catch { setEvents([]); }
    setBusy(false);
    // a time chip is hiding older matches? count them so the page can say so instead of looking empty
    setOlder(0);
    if (s && !p?.time_label && !day) {
      api.search(p?.text || t, camera || undefined, conf).then((all) => setOlder(all.filter((e) => e.start_ts < s).length)).catch(() => {});
    }
  };
  const clearSearch = () => { setSubmitted(""); setParsed(null); setQ(""); setOlder(0); };
  // filters changed: refresh the search results for the current text (browse refreshes through its own effect)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (submitted) search(submitted); }, [camera, hours, day, label]);

  const askNvr = async (text = q) => {
    const question = text.trim();
    if (!question || pending) return;
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
    if (e.key !== "Enter") return;
    e.preventDefault();
    // a question (ends with "?") or Shift+Enter asks Qwen; plain Enter searches instantly
    if (e.shiftKey || q.trim().endsWith("?")) askNvr(); else search();
  };

  const hasConversation = messages.length > 0 || pending;
  const grouped = browsing && mode === "grouped";
  const timeLabel = day ? new Date(day + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" }) : RANGES.find(([, h]) => h === hours)?.[0];
  return (
    <div className="view find">
      <form className="search-bar" onSubmit={(e) => { e.preventDefault(); search(); }}>
        <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={onKey}
          placeholder='Search "white pickup truck", or ask "Was anyone near the trailers after 6pm?"' />
        {submitted && <button type="button" className="ghost" onClick={clearSearch} title="Back to the latest events">✕</button>}
        <button type="submit" disabled={busy || !q.trim()}>{busy && submitted ? "Searching…" : "Search"}</button>
        <button type="button" className="ask-btn" disabled={!!pending || !q.trim()} onClick={() => askNvr()} title="Have Qwen look it up and answer in words (Shift+Enter, or end with ?)">✦ Ask</button>
      </form>
      <div className="toolbar search-filters">
        {browsing && (
          <div className="segmented">
            <button className={mode === "sightings" ? "active" : ""} onClick={() => { setMode("sightings"); saveNumber("eventsGrouped", 0); }} title="Every event as its own card">Events</button>
            <button className={mode === "grouped" ? "active" : ""} onClick={() => { setMode("grouped"); saveNumber("eventsGrouped", 1); }} title="One row per person or vehicle, with all of their sightings">Grouped by who</button>
          </div>
        )}
        {!grouped && (
          <>
            <select value={camera} onChange={(e) => setCamera(e.target.value)}>
              <option value="">All cameras</option>
              {cameras.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
            <select value={label} onChange={(e) => setLabel(e.target.value)}>
              <option value="">Any type</option>
              <option value="person">People</option>
              <option value="vehicle">Vehicles</option>
            </select>
            <div className="segmented" title="Time window (a typed phrase like “last night” wins over these)">
              {RANGES.map(([l, h]) => <button key={l} className={!day && hours === h ? "active" : ""} onClick={() => { setDay(""); setHours(h); }}>{l}</button>)}
            </div>
            <label className="row small" title="One day">
              <input type="date" value={day} max={new Date().toISOString().slice(0, 10)} onChange={(e) => setDay(e.target.value)} />
              {day && <button className="linkish small" onClick={() => setDay("")}>✕</button>}
            </label>
            <ConfidenceSlider value={minYolo} onChange={(v) => { setMinYolo(v); saveNumber("minYolo.search", v); if (submitted) search(submitted, v); }} />
            {browsing && (
              <select value={status} onChange={(e) => setStatus(e.target.value)} title="Review: which of the verifier's outcomes to show">
                {STATUSES.map(([v, l]) => <option key={l} value={v}>{l}</option>)}
              </select>
            )}
          </>
        )}
        <span className="spacer" />
        <button className="ghost small" onClick={() => setShowHistory(!showHistory)}>{showHistory ? "▾" : "▸"} Conversations{threads.length ? ` (${threads.length})` : ""}</button>
        {hasConversation && <button className="ghost small" onClick={() => { setThreadId(null); setPending(null); }}>New conversation</button>}
      </div>
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
          {SUGGESTIONS.map((s) => <button key={s} className="ghost small" onClick={() => askNvr(s)}>✦ {s}</button>)}
        </div>
      )}

      {grouped ? <IdentitiesView cameras={cameras} /> : browsing ? (
        <section>
          <h3>Latest events <span className="muted small">{timeLabel}{camera ? ` · ${name(camera)}` : ""}{status !== "verified" ? ` · ${STATUSES.find(([v]) => v === status)?.[1].toLowerCase()}` : ""}</span></h3>
          {busy && !events && <SkeletonGrid n={6} />}
          {events && events.length === 0 && <div className="empty">No events match these filters{day || hours ? " in this period" : ""}.</div>}
          <div className="event-grid">
            {events?.map((e) => <EventCard key={e.id} e={e} cameraName={name(e.camera_id)} onOpen={() => setOpen(e.id)} />)}
          </div>
          <div ref={sentinel} className="center muted small">{more && events?.length ? "Loading older events…" : events?.length ? "That's everything." : ""}</div>
        </section>
      ) : (
        <>
          <section>
            <h3>Events <span className="muted small">matching "{parsed?.text || submitted}"{parsed?.time_label ? ` · ${parsed.time_label}` : timeLabel ? ` · ${timeLabel}` : ""}{events ? ` · ${events.length}` : ""}</span></h3>
            {busy && !events && <SkeletonGrid n={3} />}
            {events && events.length === 0 && <div className="empty">No matching events {parsed?.time_label ? parsed.time_label : hours || day ? "in this period" : ""}.</div>}
            <div className="event-grid">
              {events?.map((e) => <EventCard key={e.id} e={e} cameraName={name(e.camera_id)} onOpen={() => setOpen(e.id)} />)}
            </div>
            {events && older > 0 && hours > 0 && !day && !parsed?.time_label && (
              <p className="muted small older-hint">
                {older} more before {fmtTime(Date.now() / 1000 - hours * 3600)} ·{" "}
                <button className="ghost small" onClick={() => setHours(0)}>Show any time</button>
              </p>
            )}
          </section>
          {parsed?.footage_text !== null && (
            <section>
              <h3>Footage <span className="muted small">frames that look like "{parsed?.footage_text ?? submitted}"{parsed?.time_label ? ` · ${parsed.time_label}` : ""} · Qwen checks the best 8 · the outline is the part that matched</span></h3>
              <FootageResults q={parsed?.footage_text ?? submitted} nonce={nonce} cameras={cameras} camera={camera} sinceHours={day ? 0 : hours}
                window={parsed?.time_label ? { since: parsed.since, until: parsed.until } : day ? { since: window_()[0] ?? null, until: window_()[1] ?? null } : null} />
            </section>
          )}
          {parsed && parsed.footage_text === null && (
            <p className="muted small">Footage search is for things you can picture ("white van", "open gate"); questions about people are answered from events above.</p>
          )}
        </>
      )}
      {open !== null && <EventDetail id={open} cameraName={name} onClose={() => setOpen(null)} />}
    </div>
  );
}
