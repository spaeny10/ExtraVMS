import { useEffect, useState } from "react";
import { api, fmtTime, type AskMeta, type Briefing, type BriefingSettings, type CiteRefs, type SiteApi } from "./api";
import { useNav } from "./nav";
import { otherLabel, splitCited } from "./plate";
import { PlateBadge, ReadPlate } from "./PlateRead";
import { Skeleton, toast } from "./ui";

export function Answer({ text, meta, model, fallback, onEvent, streaming }: {
  text: string; meta: AskMeta | null; model: string | null; fallback?: string; onEvent: (id: number) => void; streaming?: boolean;
}) {
  const [showCalls, setShowCalls] = useState(false);
  return (
    <div className={`ask-msg assistant${streaming ? " streaming" : ""}`}>
      <div className="ask-text"><Cited text={text} refs={meta?.refs} onEvent={onEvent} /></div>
      {meta?.refs && !streaming && <AnswerRefs text={text} refs={meta.refs} onEvent={onEvent} />}
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

/**
 * The events the lookups found, the ones the answer cites first (in citation order, with the plate or "Read plate"
 * for a vehicle), the rest folded under "Other events searched (N)" (collapsed).
 */
export function AnswerRefs({ text, refs, onEvent, site = api }: { text: string; refs: CiteRefs; onEvent: (id: number) => void; site?: SiteApi }) {
  const ids = Object.keys(refs.events).sort((a, b) => refs.events[b].start_ts - refs.events[a].start_ts);
  const { cited, other } = splitCited(ids, (x) => x, text);
  if (!ids.length) return null;
  const row = (id: string, withPlate: boolean) => {
    const r = refs.events[id];
    return (
      <li key={id}>
        <button className="linkish" onClick={() => onEvent(Number(id))} title={`Event #${id} · ${r.camera} · ${fmtTime(r.start_ts)}`}>
          {r.label === "vehicle" ? "🚗" : "🧍"} {r.camera} {clock(r.start_ts)}
        </button>
        {r.label === "vehicle" && (withPlate
          ? <ReadPlate id={Number(id)} plate={r.plate} region={r.plate_region} site={site} compact />
          : <PlateBadge plate={r.plate} region={r.plate_region} />)}
      </li>
    );
  };
  return (
    <div className="ask-refs small">
      {cited.length > 0 && <ul className="ask-refs-cited">{cited.map((id) => row(id, true))}</ul>}
      {other.length > 0 && (
        <details className="ask-refs-other">
          <summary className="muted">{otherLabel(other.length)}</summary>
          <ul>{other.map((id) => row(id, false))}</ul>
        </details>
      )}
    </div>
  );
}

/** Renders [#123] and [F1] citations as links; ids the lookups didn't return stay plain text. */
export function Cited({ text, refs, onEvent, site = api }: { text: string; refs?: CiteRefs; onEvent: (id: number) => void; site?: SiteApi }) {
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
              {r.snapshot && <img className="cite-preview" src={site.media({ id }, "snapshot.jpg", 320)} alt="" loading="lazy" />}
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
              <img className="cite-preview" src={site.frameUrl(r.camera_id, r.ts, 320)} alt="" loading="lazy" />
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

export function BriefingCard({ onEvent, compact, site = api, readOnly }: {
  onEvent: (id: number) => void; compact?: boolean;
  /** the site whose briefings to show (hub dashboard); default: this site */
  site?: SiteApi;
  /** hide settings and "Generate now" (e.g. a hub viewer without operator rights) */
  readOnly?: boolean;
}) {
  const [list, setList] = useState<Briefing[] | null>(null);
  const [cfg, setCfg] = useState<BriefingSettings | null>(null);
  const [shown, setShown] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [editing, setEditing] = useState(false);
  const load = () => site.briefings(14).then((r) => { setList(r.briefings); setCfg(r.settings); }).catch(() => setList([]));
  useEffect(() => {
    load();
    return site.subscribe(() => {}, (m) => { if (m.type === "briefing") load(); });
  }, []);
  const generate = async () => {
    setBusy(true); setErr("");
    try { await site.generateBriefing(); await load(); setShown(null); toast.success("Briefing written"); }
    catch (e) { setErr(String(e)); toast.error(e); }
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
        {!readOnly && <button className="ghost small" onClick={() => setEditing(!editing)} title="When the daily briefing is written">⚙</button>}
        {!readOnly && <button className="ghost small" disabled={busy} onClick={generate}>{busy ? "Writing…" : "Generate now"}</button>}
      </div>
      {editing && cfg && (
        <form className="row small briefing-settings" onSubmit={async (e) => { e.preventDefault(); setCfg(await site.saveBriefingSettings(cfg)); setEditing(false); }}>
          <label className="row"><input type="checkbox" checked={cfg.enabled} onChange={(e) => setCfg({ ...cfg, enabled: e.target.checked })} /> Write a briefing every day at</label>
          <input type="time" value={cfg.time} onChange={(e) => setCfg({ ...cfg, time: e.target.value })} />
          <span className="muted">covering the time since the previous one (up to 24 h)</span>
          <button type="submit" className="small">Save</button>
        </form>
      )}
      {err && <div className="error small">{err}</div>}
      {!list ? <Skeleton lines={3} /> : !b ? (
        <div className="muted">No briefing yet. The first one is written at {cfg?.time ?? "07:00"}, or press Generate now.</div>
      ) : (
        <>
          <h3 className="briefing-headline">{b.headline}</h3>
          <ul className="briefing-bullets">
            {b.text.split("\n").filter(Boolean).map((line, i) => (
              <li key={i}><Cited text={line.replace(/^-\s*/, "")} refs={b.stats.refs} onEvent={onEvent} site={site} /></li>
            ))}
          </ul>
          {b.model && <div className="muted small"><span className="model-tag">{b.model}</span></div>}
        </>
      )}
    </div>
  );
}
