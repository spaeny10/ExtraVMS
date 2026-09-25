import { useEffect, useRef, useState } from "react";
import { api, ask, fmtTime, type AskMeta, type AssistantMessage, type AssistantThread, type Camera, type NvrEvent, type ParsedQuery } from "./api";
import { Answer } from "./Ask";
import { ConfidenceSlider, loadNumber, saveNumber } from "./ConfidenceSlider";
import { EventCard } from "./Events";
import { EventDetail } from "./EventDetail";
import { FootageResults } from "./FootageSearch";
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

type Pending = { question: string; answer: string; meta?: AskMeta; model?: string; fallback?: string; error?: string };

/** One box for finding things. Search shows matching events and footage at once; Ask has Qwen look things up
 * and answer in words, with links to the evidence, above those same results. */
export function FindView({ cameras }: { cameras: Camera[] }) {
  const [q, setQ] = useState("");
  const [submitted, setSubmitted] = useState("");   // the text the results below are for
  const [parsed, setParsed] = useState<ParsedQuery | null>(null);  // time window / footage phrase read from it
  const [camera, setCamera] = useState("");
  const [hours, setHours] = useState(24);
  const [minYolo, setMinYolo] = useState(() => loadNumber("minYolo.search"));
  const [events, setEvents] = useState<NvrEvent[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [nonce, setNonce] = useState(0);
  const [open, setOpen] = useState<number | null>(null);
  // conversation
  const [threads, setThreads] = useState<AssistantThread[]>([]);
  const [threadId, setThreadId] = useState<number | null>(null);
  const [messages, setMessages] = useState<AssistantMessage[]>([]);
  const [pending, setPending] = useState<Pending | null>(null);
  const [showHistory, setShowHistory] = useState(false);
  const answerRef = useRef<HTMLDivElement>(null);
  const name = (id: string) => cameras.find((c) => c.id === id)?.name ?? id;
  const since = () => (hours ? Date.now() / 1000 - hours * 3600 : undefined);

  const loadThreads = () => api.assistantThreads().then(setThreads).catch(() => {});
  useEffect(() => {
    loadThreads();
    api.remoteWarm().catch(() => {}); // a cold remote model (if configured) starts loading while you type
  }, []);
  useEffect(() => {
    if (threadId == null) { setMessages([]); return; }
    api.assistantThread(threadId).then((t) => setMessages(t.messages)).catch(() => setMessages([]));
  }, [threadId]);

  const search = async (text = q, conf = minYolo) => {
    const t = text.trim();
    if (!t) return;
    setBusy(true);
    setEvents(null);
    // "today", "last night", "past 3 hours"... in the text set the window (over the time chips)
    const p = await api.parseQuery(t).catch(() => null);
    setParsed(p);
    setSubmitted(t);
    setNonce(Date.now());
    const s = p?.time_label ? p.since ?? undefined : since();
    const u = p?.time_label ? p.until ?? undefined : undefined;
    try { setEvents(await api.search(p?.text || t, camera || undefined, conf, s, u)); }
    catch { setEvents([]); }
    setBusy(false);
  };
  // filters changed: refresh the results for the current text
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (submitted) search(submitted); }, [camera, hours]);

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
  return (
    <div className="view find">
      <form className="search-bar" onSubmit={(e) => { e.preventDefault(); search(); }}>
        <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={onKey}
          placeholder='Search "white pickup truck", or ask "Was anyone near the trailers after 6pm?"' />
        <button type="submit" disabled={busy || !q.trim()}>{busy ? "Searching…" : "Search"}</button>
        <button type="button" className="ask-btn" disabled={!!pending || !q.trim()} onClick={() => askNvr()} title="Have Qwen look it up and answer in words (Shift+Enter, or end with ?)">✦ Ask</button>
      </form>
      <div className="toolbar search-filters">
        <select value={camera} onChange={(e) => setCamera(e.target.value)}>
          <option value="">All cameras</option>
          {cameras.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        <div className="segmented">
          {RANGES.map(([l, h]) => <button key={l} className={hours === h ? "active" : ""} onClick={() => setHours(h)}>{l}</button>)}
        </div>
        <ConfidenceSlider value={minYolo} onChange={(v) => { setMinYolo(v); saveNumber("minYolo.search", v); if (submitted) search(submitted, v); }} />
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

      {!submitted && !hasConversation && (
        <div className="ask-empty">
          <p className="muted">Search finds events by meaning (synopses, notes, labels) and any recorded frame by what it looks like. Ask has Qwen look things up across events, journeys, unusual activity, recordings and footage, and answer with links to the evidence.</p>
          <div className="ask-suggestions">
            {SUGGESTIONS.map((s) => <button key={s} className="ghost small" onClick={() => askNvr(s)}>{s}</button>)}
          </div>
        </div>
      )}

      {submitted && (
        <>
          <section>
            <h3>Events <span className="muted small">matching "{parsed?.text || submitted}"{parsed?.time_label ? ` · ${parsed.time_label}` : ""}{events ? ` · ${events.length}` : ""}</span></h3>
            {busy && !events && <SkeletonGrid n={3} />}
            {events && events.length === 0 && <div className="empty">No matching events {parsed?.time_label ? parsed.time_label : hours ? "in this period" : ""}.</div>}
            <div className="event-grid">
              {events?.map((e) => <EventCard key={e.id} e={e} cameraName={name(e.camera_id)} onOpen={() => setOpen(e.id)} />)}
            </div>
          </section>
          {parsed?.footage_text !== null && (
            <section>
              <h3>Footage <span className="muted small">frames that look like "{parsed?.footage_text ?? submitted}"{parsed?.time_label ? ` · ${parsed.time_label}` : ""} · Qwen checks the best 8 · the outline is the part that matched</span></h3>
              <FootageResults q={parsed?.footage_text ?? submitted} nonce={nonce} cameras={cameras} camera={camera} sinceHours={hours}
                window={parsed?.time_label ? { since: parsed.since, until: parsed.until } : null} />
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
