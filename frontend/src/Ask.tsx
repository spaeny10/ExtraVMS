import { useEffect, useRef, useState } from "react";
import {
  api, ask, fmtTime, frameUrl, media, subscribe,
  type AskMeta, type AssistantMessage, type AssistantThread, type Briefing, type BriefingSettings, type Camera, type CiteRefs,
} from "./api";
import { EventDetail } from "./EventDetail";
import { useNav } from "./nav";

const SUGGESTIONS = [
  "What happened overnight?",
  "Anything unusual today?",
  "Did anyone go outside today?",
  "How many people came through the East Door today, by hour?",
  "When was the last vehicle in the side yard?",
  "Was any camera offline in the last 24 hours?",
];

type Pending = { question: string; answer: string; meta?: AskMeta; model?: string; fallback?: string; error?: string };

/** Ask the NVR: questions about the whole site, answered from its own events, footage and recordings. */
export function AskView({ cameras }: { cameras: Camera[] }) {
  const [threads, setThreads] = useState<AssistantThread[]>([]);
  const [threadId, setThreadId] = useState<number | null>(null);
  const [messages, setMessages] = useState<AssistantMessage[]>([]);
  const [pending, setPending] = useState<Pending | null>(null);
  const [q, setQ] = useState("");
  const [openEvent, setOpenEvent] = useState<number | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const name = (id: string) => cameras.find((c) => c.id === id)?.name ?? id;

  const loadThreads = () => api.assistantThreads().then(setThreads).catch(() => {});
  useEffect(() => {
    loadThreads();
    api.remoteWarm().catch(() => {}); // a cold remote model (if configured) starts loading while you type
  }, []);
  useEffect(() => {
    if (threadId == null) { setMessages([]); return; }
    api.assistantThread(threadId).then((t) => setMessages(t.messages)).catch(() => setMessages([]));
  }, [threadId]);
  useEffect(() => { bottom.current?.scrollIntoView({ block: "end" }); }, [messages, pending?.answer]);

  const send = async (text: string) => {
    const question = text.trim();
    if (!question || pending) return;
    setQ("");
    setPending({ question, answer: "" });
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

  const remove = async (id: number) => {
    await api.deleteThread(id);
    if (id === threadId) setThreadId(null);
    loadThreads();
  };

  return (
    <div className="view ask-view">
      <BriefingCard onEvent={setOpenEvent} />
      <div className="ask-layout">
        <aside className="ask-threads">
          <button className="ghost" onClick={() => { setThreadId(null); setPending(null); }}>+ New conversation</button>
          {threads.map((t) => (
            <div key={t.id} className={`ask-thread ${t.id === threadId ? "active" : ""}`}>
              <button className="linkish" onClick={() => { setThreadId(t.id); setPending(null); }} title={t.title}>
                <span className="ask-thread-title">{t.title}</span>
                <span className="muted small">{fmtTime(t.updated_at)}</span>
              </button>
              <button className="ghost small" aria-label="Delete conversation" onClick={() => remove(t.id)}>✕</button>
            </div>
          ))}
        </aside>
        <section className="ask-chat">
          {messages.length === 0 && !pending && (
            <div className="ask-empty">
              <p className="muted">Ask about anything the cameras saw. Answers come from the NVR's events, journeys, unusual activity, recordings and footage search, with links to the evidence.</p>
              <div className="ask-suggestions">
                {SUGGESTIONS.map((s) => <button key={s} className="ghost small" onClick={() => send(s)}>{s}</button>)}
              </div>
            </div>
          )}
          {messages.map((m) => m.role === "user"
            ? <div key={m.id} className="ask-msg user">{m.content}</div>
            : <Answer key={m.id} text={m.content} meta={m.calls} model={m.model} onEvent={setOpenEvent} />)}
          {pending && (
            <>
              <div className="ask-msg user">{pending.question}</div>
              {pending.error
                ? <div className="ask-msg assistant error">{pending.error}</div>
                : <Answer text={pending.answer || (pending.meta ? "…" : "Working out what to look up…")} meta={pending.meta ?? null}
                    model={pending.model ?? null} fallback={pending.fallback} onEvent={setOpenEvent} streaming />}
            </>
          )}
          <div ref={bottom} />
          <form className="ask-input" onSubmit={(e) => { e.preventDefault(); send(q); }}>
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. Was anyone near the trailers after 6pm?" disabled={!!pending} />
            <button type="submit" disabled={!!pending || !q.trim()}>{pending ? "Thinking…" : "Ask"}</button>
          </form>
        </section>
      </div>
      {openEvent !== null && <EventDetail id={openEvent} cameraName={name} onClose={() => setOpenEvent(null)} />}
    </div>
  );
}

function Answer({ text, meta, model, fallback, onEvent, streaming }: {
  text: string; meta: AskMeta | null; model: string | null; fallback?: string; onEvent: (id: number) => void; streaming?: boolean;
}) {
  const [showCalls, setShowCalls] = useState(false);
  return (
    <div className={`ask-msg assistant${streaming ? " streaming" : ""}`}>
      <div className="ask-text"><Cited text={text} refs={meta?.refs} onEvent={onEvent} /></div>
      <div className="ask-meta muted small">
        {meta?.calls?.length ? (
          <button className="linkish small" onClick={() => setShowCalls(!showCalls)}>
            {showCalls ? "▾" : "▸"} Looked up: {meta.calls.map((c) => `${c.tool.replace("_", " ")} → ${c.count}`).join(", ")}
          </button>
        ) : null}
        {model && <span className="model-tag" title="Which Qwen model wrote this answer">{model}</span>}
        {(fallback || meta?.fallback) && <span title="The larger remote model wasn't ready, so the local model answered">local model ({fallback || meta?.fallback})</span>}
      </div>
      {showCalls && meta && (
        <ul className="ask-calls small">
          {meta.calls.map((c, i) => <li key={i}><code>{c.label}</code> → {c.count}</li>)}
          {meta.planner && <li className="muted">planned by {meta.planner}</li>}
        </ul>
      )}
    </div>
  );
}

/** Renders [#123] and [F1] citations as links; ids the lookups didn't return stay plain text. */
export function Cited({ text, refs, onEvent }: { text: string; refs?: CiteRefs; onEvent: (id: number) => void }) {
  const { openInTimeline } = useNav();
  const parts = text.split(/(\[#\d+\]|\[F\d+\])/g);
  return (
    <>
      {parts.map((p, i) => {
        const ev = /^\[#(\d+)\]$/.exec(p);
        if (ev && refs?.events[ev[1]]) {
          const r = refs.events[ev[1]];
          const id = Number(ev[1]);
          return (
            <button key={i} className="cite" onClick={() => onEvent(id)} title={`Event #${id} · ${r.camera} · ${fmtTime(r.start_ts)}`}>
              {r.label === "vehicle" ? "🚗" : "🧍"} {r.camera} {clock(r.start_ts)}
              {r.snapshot && <img className="cite-preview" src={media({ id }, "snapshot.jpg")} alt="" loading="lazy" />}
            </button>
          );
        }
        const fm = /^\[(F\d+)\]$/.exec(p);
        if (fm && refs?.footage[fm[1]]) {
          const r = refs.footage[fm[1]];
          return (
            <button key={i} className="cite footage" title={`Footage · ${r.camera} · ${fmtTime(r.ts)} (open on the Timeline)`}
              onClick={() => openInTimeline({ id: 0, camera_id: r.camera_id, start_ts: r.ts, end_ts: r.ts + 5, camera_class: "moment" })}>
              ▶ {r.camera} {clock(r.ts)}
              <img className="cite-preview" src={frameUrl(r.camera_id, r.ts, 320)} alt="" loading="lazy" />
            </button>
          );
        }
        return <span key={i}>{p}</span>;
      })}
    </>
  );
}

const clock = (ts: number) => {
  const d = new Date(ts * 1000);
  const today = new Date();
  const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === today.toDateString() ? time : `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${time}`;
};

export function BriefingCard({ onEvent, compact }: { onEvent: (id: number) => void; compact?: boolean }) {
  const [list, setList] = useState<Briefing[] | null>(null);
  const [cfg, setCfg] = useState<BriefingSettings | null>(null);
  const [shown, setShown] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [editing, setEditing] = useState(false);
  const load = () => api.briefings(14).then((r) => { setList(r.briefings); setCfg(r.settings); }).catch(() => setList([]));
  useEffect(() => {
    load();
    return subscribe(() => {}, (m) => { if (m.type === "briefing") load(); });
  }, []);
  const generate = async () => {
    setBusy(true); setErr("");
    try { await api.generateBriefing(); await load(); setShown(null); }
    catch (e) { setErr(String(e)); }
    setBusy(false);
  };
  const b = list?.find((x) => x.id === shown) ?? list?.[0];
  return (
    <div className={`briefing ${compact ? "compact" : ""}`}>
      <div className="briefing-head">
        <span className="muted small">☀ Briefing{b ? ` · ${fmtTime(b.period_start)} – ${fmtTime(b.period_end)}` : ""}</span>
        <span className="spacer" />
        {list && list.length > 1 && (
          <select className="small" value={b?.id} onChange={(e) => setShown(Number(e.target.value))} aria-label="Earlier briefings">
            {list.map((x) => <option key={x.id} value={x.id}>{fmtTime(x.created_at)}</option>)}
          </select>
        )}
        <button className="ghost small" onClick={() => setEditing(!editing)} title="When the daily briefing is written">⚙</button>
        <button className="ghost small" disabled={busy} onClick={generate}>{busy ? "Writing…" : "Generate now"}</button>
      </div>
      {editing && cfg && (
        <form className="row small briefing-settings" onSubmit={async (e) => { e.preventDefault(); setCfg(await api.saveBriefingSettings(cfg)); setEditing(false); }}>
          <label className="row"><input type="checkbox" checked={cfg.enabled} onChange={(e) => setCfg({ ...cfg, enabled: e.target.checked })} /> Write a briefing every day at</label>
          <input type="time" value={cfg.time} onChange={(e) => setCfg({ ...cfg, time: e.target.value })} />
          <span className="muted">covering the time since the previous one (up to 24 h)</span>
          <button type="submit" className="small">Save</button>
        </form>
      )}
      {err && <div className="error small">{err}</div>}
      {!list ? <div className="muted">Loading…</div> : !b ? (
        <div className="muted">No briefing yet. The first one is written at {cfg?.time ?? "07:00"}, or press Generate now.</div>
      ) : (
        <>
          <h3 className="briefing-headline">{b.headline}</h3>
          <ul className="briefing-bullets">
            {b.text.split("\n").filter(Boolean).map((line, i) => (
              <li key={i}><Cited text={line.replace(/^-\s*/, "")} refs={b.stats.refs} onEvent={onEvent} /></li>
            ))}
          </ul>
          {b.model && <div className="muted small"><span className="model-tag">{b.model}</span></div>}
        </>
      )}
    </div>
  );
}
