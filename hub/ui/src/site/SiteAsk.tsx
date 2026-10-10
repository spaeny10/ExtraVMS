/**
 * A Site's Ask tab: one answer for the whole Site (every server's cameras), in conversations private to the user
 * (hub/hub/site_ask.py). Desktop: conversations on the left, the conversation and the question box on the right.
 * Wide screens (>1100 px) add a References column (a third of what the conversation list leaves): the evidence of the
 * selected answer as thumbnail cards, cited first. Medium widths open it as a drawer over the conversation, phones as a
 * full-width sheet, both from the "References" button. Phones (≤700 px): the conversations open as a drawer from the
 * "Conversations" button.
 *  - the answer streams in; citations [#123] / [F1] are chips: an event opens the in-place viewer (HubEventDetail,
 *    with the right server), a footage moment the Site's Timeline;
 *  - the References panel follows the latest answer; clicking an answer (or a chip, or its Sources line) selects it.
 *    Hovering a chip of the selected answer highlights (and scrolls to) its card; hovering a card highlights its chips;
 *  - each answer has a Sources disclosure: the merged evidence by camera and time (the server is named only there);
 *    with the References column on screen it is one line pointing there;
 *  - instructions ("Quiet alerts tonight") are not asked: a note links to Customer › Actions with the text prefilled;
 *    requests Ask can't do ("Alert me when someone enters") get the hub's note saying so (where alert rules live);
 *  - ?q=… (Find's "Ask this site" hint) prefills the box; sending is the user's action.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Icon, confirmDialog, promptDialog, toast, useIsPhone } from "@site/ui";
import { camKey } from "@site/playback";
import { type Site, type SiteAskChunk, type SiteAskSource, type SiteAskSources, type SiteAskThread, type SiteAskThreadFull, ago, api, fmtTime, siteAsk } from "../api";
import { cameraNameFor } from "../eventOpen";
import { HubEventDetail } from "../HubEventDetail";
import { mediaApi } from "../hubSource";
import { go, navigate, siteAskHref } from "../nav";
import { siteTimelineHref } from "../timelineLink";
import { ACTIONS_PATH } from "../customer/fleetActions";
import {
  type AnswerKey, type Inline, type References, SUGGESTIONS, askQuery, buildReferences, citedSource, clockIn, groupSources, orderThreads, parseAnswer,
  pickAnswer, refCountsLine, sendKey, serversLine,
} from "./siteAskData";

type Pending = {
  question: string; answer: string; status?: string; sources?: SiteAskSources; model?: string | null; fallback?: string; error?: string;
};
/** A question that wasn't asked: an instruction (href: Customer › Actions) or a request Ask can't do (message). */
type Note = { question: string; href?: string; message?: string };
type Open = { server: string; id: number };
/** The citation under the pointer: from a chip (the panel scrolls to its card) or from a card (its chips light up). */
type Hot = { ref: string; from: "chip" | "card" };
/** The answer the References panel shows. */
type Selected = { key: AnswerKey; text: string; sources: SiteAskSources | null; asked?: string; status?: string; streaming: boolean };

/** Wide enough for the References column (hub.css: the three-column grid starts at 1101 px). */
const WIDE = "(min-width: 1101px)";

function useMedia(query: string): boolean {
  const [on, setOn] = useState(() => typeof matchMedia !== "undefined" && matchMedia(query).matches);
  useEffect(() => {
    const mq = matchMedia(query);
    const change = () => setOn(mq.matches);
    change();
    mq.addEventListener("change", change);
    return () => mq.removeEventListener("change", change);
  }, [query]);
  return on;
}

/** "429 {"detail":"…"}" → "…" */
const errorText = (e: unknown) => {
  const s = e instanceof Error ? e.message : String(e);
  const m = /^\d{3} (\{.*\})$/s.exec(s);
  if (m) { try { return String(JSON.parse(m[1]).detail ?? s); } catch { /* not JSON */ } }
  return s;
};

/** The element fills the window below where it starts (the log scrolls inside it); `bottom` leaves room for the phone tab bar. */
function useFillHeight(bottom: number) {
  const ref = useRef<HTMLDivElement>(null);
  const [h, setH] = useState<number | null>(null);
  useLayoutEffect(() => {
    const fit = () => {
      const el = ref.current;
      if (!el) return;
      const top = el.getBoundingClientRect().top + window.scrollY;
      setH(Math.max(360, window.innerHeight - top - bottom));
    };
    fit();
    addEventListener("resize", fit);
    return () => removeEventListener("resize", fit);
  }, [bottom]);
  return { ref, height: h };
}

export function SiteAsk({ site, threadId }: { site: Site; threadId?: string }) {
  const isPhone = useIsPhone();
  const tid = threadId && /^\d+$/.test(threadId) ? Number(threadId) : null;
  const [threads, setThreads] = useState<SiteAskThread[] | null>(null);
  const [thread, setThread] = useState<SiteAskThreadFull | null>(null);
  const [missing, setMissing] = useState(false);
  const [pending, setPending] = useState<Pending | null>(null);
  const [notes, setNotes] = useState<Note[]>([]);
  const [text, setText] = useState(() => askQuery(location.search));
  const [drawer, setDrawer] = useState(false);
  const [open, setOpen] = useState<Open | null>(null);
  const [pick, setPick] = useState<AnswerKey | null>(null);
  const [hot, setHot] = useState<Hot | null>(null);
  const [refsOpen, setRefsOpen] = useState(false);
  const wide = useMedia(WIDE);
  const loaded = useRef<number | null>(null);
  const abort = useRef<AbortController | null>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const log = useRef<HTMLDivElement>(null);
  const fill = useFillHeight(isPhone ? 70 : 16);

  const loadThreads = useCallback(() => api.siteAskThreads(site.id).then((t) => setThreads(orderThreads(t))).catch(() => setThreads([])), [site.id]);
  useEffect(() => { void loadThreads(); }, [loadThreads]);
  useEffect(() => () => abort.current?.abort(), []);

  // the conversation in the URL (one this page just wrote is already loaded)
  useEffect(() => {
    setNotes([]); setMissing(false); setPick(null); setHot(null);
    if (tid == null) { setThread(null); loaded.current = null; return; }
    if (loaded.current === tid) return;
    let live = true;
    api.siteAskThread(site.id, tid)
      .then((t) => { if (live) { loaded.current = t.id; setThread(t); } })
      .catch(() => { if (live) { setThread(null); setMissing(true); } });
    return () => { live = false; };
  }, [site.id, tid]);

  // keep the newest message in view while it is written (unless the user scrolled up to read)
  const msgCount = thread?.messages.length ?? 0;
  useEffect(() => {
    const el = log.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 160) el.scrollTop = el.scrollHeight;
  }, [msgCount, pending?.answer, pending?.status, notes.length]);
  useEffect(() => { const el = log.current; if (el) el.scrollTop = el.scrollHeight; }, [thread?.id]);

  const newConversation = () => {
    if (pending) return;
    setDrawer(false);
    navigate(siteAskHref(site.id));
    setTimeout(() => input.current?.focus(), 0);
  };

  const send = async (raw?: string) => {
    const q = (raw ?? text).trim();
    if (!q || pending) return;
    setText(""); setDrawer(false); setPick(null); setHot(null);   // the panel follows the new answer
    const startTid = tid != null && thread?.id === tid ? tid : null;
    let newTid: number | null = startTid;
    let instruction: Note | null = null;
    const ctrl = new AbortController();
    abort.current = ctrl;
    setPending({ question: q, answer: "", status: "Asking…" });
    const on = (c: SiteAskChunk) => {
      switch (c.type) {
        case "thread": newTid = c.thread_id; break;
        case "status": setPending((p) => p && { ...p, status: c.text }); break;
        case "sources": { const { type: _t, ...src } = c; setPending((p) => p && { ...p, sources: src, status: "Writing the answer…" }); break; }
        case "model": setPending((p) => p && { ...p, model: c.model }); break;
        case "fallback": setPending((p) => p && { ...p, fallback: c.reason }); break;
        case "delta": setPending((p) => p && { ...p, answer: p.answer + c.text, status: undefined }); break;
        case "instruction": instruction = { question: q, href: c.href }; break;
        case "unsupported": instruction = { question: q, message: c.message }; break;
        case "error": setPending((p) => p && { ...p, error: c.error }); break;
        default: break;
      }
    };
    try {
      await siteAsk(site.id, { question: q, thread_id: startTid }, on, ctrl.signal);
    } catch (e) {
      if (!ctrl.signal.aborted) setPending((p) => p && { ...p, error: errorText(e) });
    }
    abort.current = null;
    if (ctrl.signal.aborted) return;
    if (instruction) {
      const note: Note = instruction;
      setNotes((n) => [...n, note]);
      setPending(null);
      return;
    }
    if (newTid != null) {
      try {
        const t = await api.siteAskThread(site.id, newTid);
        loaded.current = t.id;
        setThread(t);
      } catch { /* the list below still shows it */ }
      if (newTid !== startTid) navigate(siteAskHref(site.id, newTid), startTid == null);
    }
    setPending((p) => (p?.error ? p : null));
    void loadThreads();
  };

  const rename = async (t: SiteAskThread) => {
    const title = await promptDialog("Rename conversation", { initial: t.title, label: "Name" });
    if (!title?.trim()) return;
    try {
      const full = await api.renameSiteAskThread(site.id, t.id, title.trim());
      if (thread?.id === t.id) setThread(full);
      void loadThreads();
    } catch (e) { toast.error(e); }
  };
  const remove = async (t: SiteAskThread) => {
    if (!(await confirmDialog("Delete this conversation?", { message: t.title, confirmLabel: "Delete", danger: true }))) return;
    try {
      await api.deleteSiteAskThread(site.id, t.id);
      if (t.id === tid) { loaded.current = null; navigate(siteAskHref(site.id), true); }
      void loadThreads();
    } catch (e) { toast.error(e); }
  };

  // camera names for the viewer's title, from the evidence on the page
  const allSources = useMemo(() => [...(thread?.messages ?? []).flatMap((m) => m.sources?.items ?? []), ...(pending?.sources?.items ?? [])],
    [thread, pending?.sources]);
  const camNames = useMemo(() => {
    const m = new Map<string, string>();
    for (const x of allSources) if (x.camera_id && x.camera) m.set(camKey(x.server_id, x.camera_id), x.camera);
    return m;
  }, [allSources]);

  const messages = useMemo(() => (thread && thread.id === tid ? thread.messages : []), [thread, tid]);
  const empty = !messages.length && !pending && !notes.length;
  const title = (thread && thread.id === tid ? thread.title : null) ?? (tid != null && !missing ? "…" : "New conversation");

  // ---- the References panel: the selected answer (by default the latest) and its evidence
  const writing = !!pending && !pending.error;
  const selKey = pickAnswer(messages, pick, writing);
  const answers = messages.filter((m) => m.role === "assistant").length + (writing ? 1 : 0);
  const sel: Selected | null = useMemo(() => {
    if (selKey === "pending") {
      return pending ? { key: selKey, text: pending.answer, sources: pending.sources ?? null, asked: pending.question, status: pending.status, streaming: true } : null;
    }
    const i = messages.findIndex((m) => m.id === selKey);
    if (i < 0) return null;
    const asked = messages[i - 1]?.role === "user" ? messages[i - 1].content : undefined;
    return { key: selKey!, text: messages[i].content, sources: messages[i].sources, asked, streaming: false };
  }, [selKey, messages, pending]);
  const refs = useMemo(() => buildReferences(sel?.sources?.items, sel?.text ?? ""), [sel?.sources, sel?.text]);
  const chipHot = useCallback((ref: string | null) => setHot(ref ? { ref, from: "chip" } : null), []);
  const cardHot = useCallback((ref: string | null) => setHot(ref ? { ref, from: "card" } : null), []);
  const openEvent = useCallback((o: Open) => { if (isPhone) setRefsOpen(false); setOpen(o); }, [isPhone]);
  useEffect(() => { if (wide) setRefsOpen(false); }, [wide]);
  useEffect(() => {
    if (!refsOpen) return;
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape" && !open) setRefsOpen(false); };
    addEventListener("keydown", esc);
    return () => removeEventListener("keydown", esc);
  }, [refsOpen, open]);
  const answerProps = (key: AnswerKey) => {
    const on = key === selKey;
    return { selected: on && answers > 1, onSelect: () => setPick(key), hot: on && hot ? hot.ref : null, onHot: on ? chipHot : undefined, compact: wide };
  };
  const refsCount = refs.cards.length + refs.rows.filter((x) => x.kind === "journey").length;

  return (
    <div className="site-ask" ref={fill.ref} style={fill.height ? { height: fill.height } : undefined}>
      <aside className={`sa-threads ${drawer ? "open" : ""}`} aria-label="Conversations">
        <div className="sa-threads-head">
          <strong>Conversations</strong>
          <span className="spacer" />
          <button className="small" onClick={newConversation} disabled={!!pending} title="Start a new conversation">+ New</button>
          <button className="ghost small sa-drawer-close" onClick={() => setDrawer(false)} aria-label="Close conversations"><Icon name="x" size={16} /></button>
        </div>
        {threads === null ? <p className="muted small">Loading…</p>
          : threads.length === 0 ? <p className="muted small">No conversations yet.</p>
          : (
            <ul className="sa-thread-list">
              {threads.map((t) => {
                const href = siteAskHref(site.id, t.id);
                return (
                  <li key={t.id} className={t.id === tid ? "active" : ""}>
                    <a href={href} onClick={(e) => { setDrawer(false); go(href)(e); }} title={t.title}>
                      <span className="sa-thread-title">{t.title}</span>
                      <span className="muted small">{ago(t.updated_at)}</span>
                    </a>
                    <button className="linkish small" onClick={() => void rename(t)} title="Rename" aria-label={`Rename ${t.title}`}>✎</button>
                    <button className="linkish small" onClick={() => void remove(t)} title="Delete" aria-label={`Delete ${t.title}`}>✕</button>
                  </li>
                );
              })}
            </ul>
          )}
        <p className="muted small sa-private">Your conversations are private: only you can see them.</p>
      </aside>
      {drawer && <div className="sa-veil" onClick={() => setDrawer(false)} />}

      <section className="sa-main">
        <div className="sa-bar">
          <button className="ghost small sa-bar-phone" onClick={() => setDrawer(true)} aria-expanded={drawer}>☰ Conversations</button>
          <span className="sa-bar-title muted small">{title}</span>
          <button className={`ghost small sa-refs-toggle${refsOpen ? " active" : ""}`} onClick={() => setRefsOpen(!refsOpen)} aria-expanded={refsOpen}
            aria-controls="sa-refs" title="The events the selected answer relies on">
            References{refsCount > 0 && <span className="sa-refs-count">{refsCount}</span>}
          </button>
          <button className="ghost small sa-bar-phone" onClick={newConversation} disabled={!!pending}>+ New</button>
        </div>
        <div className="sa-log" ref={log} aria-live="polite">
          {missing && <p className="muted">That conversation isn't available (it was deleted, or it isn't yours). <a href={siteAskHref(site.id)} onClick={go(siteAskHref(site.id))}>Start a new one</a></p>}
          {empty && !missing && (
            <div className="sa-empty">
              <h3>Ask {site.name}</h3>
              <p className="muted">One answer from every camera at this site, with links to the events it is based on. Follow-up questions keep the conversation going.</p>
              <div className="ask-suggestions">
                {SUGGESTIONS.map((s) => <button key={s} className="ghost small" onClick={() => void send(s)}>✦ {s}</button>)}
              </div>
              <p className="muted small">Instructions such as “Quiet alerts tonight” run from <a href={ACTIONS_PATH} onClick={go(ACTIONS_PATH)}>Customer › Actions</a>.</p>
            </div>
          )}
          {messages.map((m, i) => (m.role === "user"
            ? <div key={m.id} className="ask-msg user">{m.content}</div>
            : <AskAnswer key={m.id} siteId={site.id} tz={site.timezone} text={m.content} sources={m.sources} model={m.model} onOpen={setOpen}
                asked={messages[i - 1]?.role === "user" ? messages[i - 1].content : undefined} {...answerProps(m.id)} />))}
          {notes.map((n, i) => (
            <div key={`n${i}`} className="sa-pair">
              <div className="ask-msg user">{n.question}</div>
              {n.href
                ? <div className="ask-msg assistant instruction-note">
                    That reads as an instruction, so it wasn't asked. Instructions run from{" "}
                    <a href={n.href} onClick={go(n.href)}>Customer › Actions</a> (your text is filled in there; nothing runs until you press Plan).
                  </div>
                : <div className="ask-msg assistant instruction-note">{n.message}</div>}
            </div>
          ))}
          {pending && (
            <>
              <div className="ask-msg user">{pending.question}</div>
              {pending.error
                ? <div className="ask-msg assistant error">{pending.error}</div>
                : <AskAnswer siteId={site.id} tz={site.timezone} text={pending.answer} status={pending.status} sources={pending.sources ?? null}
                    model={pending.model ?? null} fallback={pending.fallback} onOpen={setOpen} asked={pending.question} streaming {...answerProps("pending")} />}
            </>
          )}
        </div>
        <form className="sa-input" onSubmit={(e) => { e.preventDefault(); void send(); }}>
          <textarea ref={input} value={text} onChange={(e) => setText(e.target.value)} maxLength={1000} autoFocus={!isPhone}
            rows={Math.min(6, Math.max(1, text.split("\n").length))} aria-label="Your question"
            placeholder={messages.length ? "Ask a follow-up…" : "Ask about this site, e.g. “Did anyone come in after hours?”"}
            onKeyDown={(e) => { if (sendKey({ key: e.key, shiftKey: e.shiftKey, isComposing: e.nativeEvent.isComposing })) { e.preventDefault(); void send(); } }} />
          <button type="submit" disabled={!text.trim() || !!pending} title="Send (Enter). Shift+Enter: new line">{pending ? "…" : "Send"}</button>
        </form>
      </section>

      <ReferencesPanel siteId={site.id} tz={site.timezone} sel={sel} refs={refs} hot={hot} onHot={cardHot} onOpen={openEvent}
        open={refsOpen} onClose={() => setRefsOpen(false)} />

      {open && <HubEventDetail ev={{ server: open.server, id: open.id, location: site.id }} cameraName={cameraNameFor(camNames, open.server)} onClose={() => setOpen(null)} />}
    </div>
  );
}

function AskAnswer({ siteId, tz, text, status, sources, model, fallback, onOpen, asked, streaming, selected, onSelect, hot, onHot, compact }: {
  siteId: string; tz: string | null; text: string; status?: string; sources: SiteAskSources | null; model: string | null; fallback?: string;
  onOpen: (o: Open) => void; asked?: string; streaming?: boolean;
  /** marked as the answer the References panel shows (only when there is more than one) */
  selected: boolean; onSelect: () => void;
  /** the citation highlighted from a References card; onHot only for the selected answer */
  hot: string | null; onHot?: (ref: string | null) => void;
  /** the References column is on screen: the Sources disclosure is one line pointing there */
  compact: boolean;
}) {
  const [show, setShow] = useState(false);
  const blocks = useMemo(() => parseAnswer(text), [text]);
  const items = useMemo(() => sources?.items ?? [], [sources]);
  const sightings = items.filter((x) => x.kind !== "note" && x.kind !== "briefing").length;
  const counts = useMemo(() => refCountsLine(buildReferences(items, "")), [items]);
  const plain = fallback || sources?.fallback;
  const inline = (parts: Inline[]) => parts.map((p, i) => {
    if (p.t === "text") return <span key={i}>{p.text}</span>;
    if (p.t === "bold") return <strong key={i}>{p.text}</strong>;
    const src = citedSource(p.ref, items);
    return src ? <Cite key={i} src={src} siteId={siteId} tz={tz} onOpen={onOpen} hot={hot === p.ref} onHot={onHot} />
      : <span key={i}>{p.footage ? `[${p.ref}]` : `[#${p.ref}]`}</span>;
  });
  return (
    <div className={`ask-msg assistant${streaming ? " streaming" : ""}${selected ? " selected" : ""}`} onClick={onSelect}>
      <div className="ask-text">
        {!text && status ? <p className="muted">{status}</p> : blocks.map((b, i) => (b.type === "p"
          ? <p key={i}>{inline(b.parts)}</p>
          : <ul key={i}>{b.items.map((it, j) => <li key={j}>{inline(it)}</li>)}</ul>))}
      </div>
      {(sources || model || plain) && (
        <div className="ask-meta muted small">
          {sources && (compact
            ? <button className="linkish small" onClick={onSelect} title={`${serversLine(sources.servers)}. The events are listed in the References column.`}>
                Sources: {counts} · see References →
              </button>
            : <button className="linkish small" onClick={() => setShow(!show)} aria-expanded={show}>
                {show ? "▾" : "▸"} Sources ({sightings}) · {serversLine(sources.servers)}
              </button>
          )}
          {model && <span className="model-tag" title="The shared AI model that wrote this answer">{model}</span>}
          {plain && <span title={String(plain)}>plain summary (the AI was unavailable)</span>}
        </div>
      )}
      {show && sources && !compact && <SourcesList sources={sources} siteId={siteId} onOpen={onOpen} asked={asked} />}
    </div>
  );
}

function Cite({ src, siteId, tz, onOpen, hot, onHot }: {
  src: SiteAskSource; siteId: string; tz: string | null; onOpen: (o: Open) => void; hot: boolean; onHot?: (ref: string | null) => void;
}) {
  const when = src.ts ? clockIn(src.ts, tz) : "";
  const hover = onHot && src.ref
    ? { onMouseEnter: () => onHot(src.ref!), onMouseLeave: () => onHot(null), onFocus: () => onHot(src.ref!), onBlur: () => onHot(null) }
    : {};
  if (src.kind === "footage" && src.camera_id && src.ts) {
    const href = siteTimelineHref(siteId, src.server_id, src.camera_id, null, src.ts);
    return (
      <button className={`cite footage${hot ? " hot" : ""}`} onClick={() => navigate(href)} {...hover}
        title={`Footage · ${src.camera} · ${fmtTime(src.ts)} (open on the Timeline)`}>
        ▶ {src.camera} {when}
      </button>
    );
  }
  if (src.event_id == null) return <span>{src.camera} {when}</span>;
  const id = src.event_id;
  return (
    <button className={`cite${hot ? " hot" : ""}`} onClick={() => onOpen({ server: src.server_id, id })} {...hover}
      title={`${src.kind === "journey" ? "Journey" : "Event"} #${id} · ${src.camera ?? ""} · ${src.ts ? fmtTime(src.ts) : ""}`}>
      {src.kind === "journey" ? "↔" : src.label === "vehicle" ? "🚗" : "🧍"} {src.kind === "journey" ? (src.cameras ?? []).join(" → ") || src.camera : src.camera} {when}
      {src.snapshot && <img className="cite-preview" src={mediaApi(src.server_id).media({ id }, "snapshot.jpg", 320)} alt="" loading="lazy" />}
    </button>
  );
}

function SourcesList({ sources, siteId, onOpen, asked }: { sources: SiteAskSources; siteId: string; onOpen: (o: Open) => void; asked?: string }) {
  const groups = useMemo(() => groupSources(sources.items), [sources.items]);
  const w = sources.window;
  const trouble = sources.servers.filter((s) => s.status !== "ok");
  return (
    <div className="sa-sources small">
      {w && <p className="muted">Period checked{w.label ? ` (${w.label})` : ""}: {fmtTime(w.from)} – {fmtTime(w.to)}</p>}
      {sources.question && sources.question.trim().toLowerCase() !== asked?.trim().toLowerCase() && <p className="muted">Looked up as: “{sources.question}”</p>}
      {groups.length === 0 && <p className="muted">Nothing matched.</p>}
      {groups.map((g) => (
        <div key={g.camera} className="sa-src-group">
          <div className="sa-src-cam">{g.camera}</div>
          <ul>
            {g.items.map((x, i) => {
              const label = (x.synopsis || x.text || x.kind).slice(0, 160);
              const tag = x.ref ? (x.ref.startsWith("F") ? `[${x.ref}]` : `#${x.ref}`) : "";
              return (
                <li key={`${x.server_id}/${x.ref ?? i}`}>
                  {x.ts != null && <span className="sa-src-time">{fmtTime(x.ts)}</span>}
                  {tag && <code>{tag}</code>}
                  {x.event_id != null
                    ? <button className="linkish" onClick={() => onOpen({ server: x.server_id, id: x.event_id! })}>{label}</button>
                    : x.kind === "footage" && x.camera_id && x.ts
                      ? <a href={siteTimelineHref(siteId, x.server_id, x.camera_id, null, x.ts)} onClick={go(siteTimelineHref(siteId, x.server_id, x.camera_id, null, x.ts))}>{label}</a>
                      : <span>{label}</span>}
                  {(x.earlier || x.later) && <span className="muted"> ({x.earlier ? "before" : "after"} the period asked about)</span>}
                  <span className="muted"> · {x.server_name}</span>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
      {sources.dropped > 0 && <p className="muted">…and {sources.dropped} more sightings that were not used.</p>}
      {trouble.length > 0 && (
        <p className="muted">Not checked: {trouble.map((s) => `${s.server_name} (${s.status === "offline" ? `offline${s.last_seen_at ? `, last seen ${ago(s.last_seen_at)}` : ""}` : s.error ?? s.status})`).join("; ")}</p>
      )}
    </div>
  );
}

const tagOf = (x: SiteAskSource) => (x.ref ? (x.ref.startsWith("F") ? `[${x.ref}]` : `#${x.ref}`) : "");
const labelIcon = (x: SiteAskSource) => (x.kind === "footage" ? "▶" : x.label === "vehicle" ? "🚗" : x.label === "person" ? "🧍" : "●");

/**
 * The References column / drawer / sheet: the selected answer's evidence. Event and footage cards (cited first, in
 * citation order, then newest first), then journeys, recording gaps and briefings as one-line rows.
 */
function ReferencesPanel({ siteId, tz, sel, refs, hot, onHot, onOpen, open, onClose }: {
  siteId: string; tz: string | null; sel: Selected | null; refs: References; hot: Hot | null; onHot: (ref: string | null) => void;
  onOpen: (o: Open) => void; open: boolean; onClose: () => void;
}) {
  const list = useRef<HTMLDivElement>(null);
  // a chip under the pointer: bring its card into view (only when the panel is on screen)
  useEffect(() => {
    const el = list.current;
    if (!hot || hot.from !== "chip" || !el || !el.getClientRects().length) return;
    el.querySelector(`[data-ref="${CSS.escape(hot.ref)}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [hot]);
  // another answer: back to its first card
  useEffect(() => { if (list.current) list.current.scrollTop = 0; }, [sel?.key]);

  const sources = sel?.sources ?? null;
  const asked = sources?.question || sel?.asked;
  const trouble = sources?.servers.some((s) => s.status !== "ok") ? serversLine(sources.servers) : null;
  const hotRef = hot?.ref ?? null;
  const hover = (x: SiteAskSource) => (x.ref ? { onMouseEnter: () => onHot(x.ref!), onMouseLeave: () => onHot(null) } : {});
  const goFootage = (x: SiteAskSource) => { if (x.camera_id && x.ts != null) navigate(siteTimelineHref(siteId, x.server_id, x.camera_id, null, x.ts)); };

  return (
    <aside id="sa-refs" className={`sa-refs${open ? " open" : ""}`} aria-label="References">
      <div className="sa-refs-head">
        <div className="sa-refs-title">
          <strong>References</strong>
          <span className="spacer" />
          <button className="ghost small sa-refs-close" onClick={onClose} aria-label="Close references"><Icon name="x" size={16} /></button>
        </div>
        {sel && asked && <div className="muted small sa-refs-for" title={asked}>for: “{asked}”</div>}
        {sel && (sources
          ? <div className="muted small">{refCountsLine(refs)}{refs.cited.size > 0 && ` · ${refs.cited.size} cited`}</div>
          : sel.streaming && <div className="muted small">{sel.status ?? "Looking through the site's cameras…"}</div>)}
        {trouble && <div className="muted small">{trouble}</div>}
      </div>
      <div className="sa-refs-list" ref={list}>
        {!sel && <p className="muted small sa-refs-empty">Events the answer relies on appear here.</p>}
        {sel && !sources && !sel.streaming && <p className="muted small sa-refs-empty">This answer has no saved references.</p>}
        {sources && !refs.cards.length && !refs.rows.length && <p className="muted small sa-refs-empty">No events matched this question.</p>}
        {refs.cards.map((x) => {
          const cited = !!x.ref && refs.cited.has(x.ref);
          const event = x.kind === "event" && x.event_id != null;
          const thumb = event
            ? (x.snapshot ? mediaApi(x.server_id).media({ id: x.event_id! }, "snapshot.jpg", 320) : null)
            : mediaApi(x.server_id).frameUrl(x.camera_id!, x.ts!, 320);
          const pri = x.priority === "high" || x.priority === "medium" ? x.priority : null;
          const body = (x.synopsis || x.text || "").trim();
          return (
            <button key={`${x.server_id}/${x.ref ?? x.event_id ?? x.ts}`} type="button" data-ref={x.ref}
              className={`sa-ref-card${cited ? " cited" : ""}${x.ref && x.ref === hotRef ? " hot" : ""}`}
              onClick={() => (event ? onOpen({ server: x.server_id, id: x.event_id! }) : goFootage(x))} {...hover(x)}
              title={event ? `Open event ${tagOf(x)} · ${x.camera ?? ""} · ${x.ts ? fmtTime(x.ts) : ""}` : `Open the Timeline at ${x.ts ? fmtTime(x.ts) : ""}`}>
              <span className="sa-ref-thumb">
                <span className="sa-ref-ph" aria-hidden>{labelIcon(x)}</span>
                {thumb && <img src={thumb} alt="" loading="lazy" onError={(e) => { e.currentTarget.style.visibility = "hidden"; }} />}
                {x.kind === "footage" && <span className="sa-ref-kind">Footage</span>}
              </span>
              <span className="sa-ref-body">
                <span className="sa-ref-top">
                  {x.ref && <code className="sa-ref-tag">{tagOf(x)}</code>}
                  <span className="sa-ref-cam">{x.camera ?? "Camera"}</span>
                  {x.ts != null && <span className="sa-ref-time">{clockIn(x.ts, tz)}</span>}
                </span>
                {(pri || x.earlier || x.later) && (
                  <span className="sa-ref-badges">
                    {pri && <span className={`badge threat-${pri}`}>{pri}</span>}
                    {(x.earlier || x.later) && <span className="muted small">{x.earlier ? "before" : "after"} the period asked about</span>}
                  </span>
                )}
                {body && <span className="sa-ref-text">{body}</span>}
              </span>
            </button>
          );
        })}
        {refs.rows.length > 0 && (
          <ul className="sa-ref-rows small">
            {refs.rows.map((x, i) => {
              const cls = `sa-ref-row${x.ref && refs.cited.has(x.ref) ? " cited" : ""}${x.ref && x.ref === hotRef ? " hot" : ""}`;
              const when = x.ts != null ? clockIn(x.ts, tz) : "";
              const inner = x.kind === "journey"
                ? <>↔ <span className="sa-ref-row-text">{(x.cameras ?? []).join(" → ") || x.camera}{x.text ? ` · ${x.text}` : ""}</span></>
                : x.kind === "gap"
                  ? <>⏸ <span className="sa-ref-row-text">{x.camera ?? "A camera"} not recording{x.minutes ? ` · ${x.minutes} min` : ""}{x.ongoing ? " · ongoing" : ""}</span></>
                  : x.kind === "briefing"
                    ? <>☰ <span className="sa-ref-row-text">Briefing: {x.text}</span></>
                    : <>● <span className="sa-ref-row-text">{[x.camera, x.synopsis || x.text].filter(Boolean).join(" · ")}</span></>;
              const content = <>{x.ref && <code className="sa-ref-tag">{tagOf(x)}</code>}{inner}{when && <span className="sa-ref-time">{when}</span>}</>;
              const click = x.kind === "journey" && x.event_id != null ? () => onOpen({ server: x.server_id, id: x.event_id! })
                : x.kind === "gap" && x.camera_id && x.ts != null ? () => goFootage(x) : null;
              return (
                <li key={`${x.server_id}/${x.ref ?? `${x.kind}${i}`}`} data-ref={x.ref}>
                  {click
                    ? <button type="button" className={cls} onClick={click} {...hover(x)} title={x.text ?? undefined}>{content}</button>
                    : <div className={cls} title={x.text ?? undefined}>{content}</div>}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </aside>
  );
}
