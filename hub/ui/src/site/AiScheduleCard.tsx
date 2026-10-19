/**
 * Site › Servers › AI (hub ai_schedule.py): when Qwen does all of its work at this Site, what it does now, and how much of
 * it the Site used this month and last. Full-time is the default and the paid tier; "SOC hours" gives full Qwen only while
 * the Site's SOC monitoring is armed (Settings › Monitoring), and outside those hours its servers still run PPE checks and
 * everything a person asks for, while event descriptions catch up when Qwen is idle or are skipped.
 *
 * Hub administrators change it; the Site's customer admins read it ("AI: SOC hours · Full-time AI available"). Nobody else
 * sees the card (the hub answers 403 and it stays hidden). The wording is aiSchedule.ts.
 */
import { useCallback, useEffect, useState } from "react";
import { toast } from "@site/ui";
import { type AiMode, type AiOutside, type AiSchedule, type AiUsageMonth, type Site, api } from "../api";
import { go, settingsHref } from "../nav";
import { MODE_LABEL, OUTSIDE_HELP, OUTSIDE_LABEL, effectiveLine, fmtSeconds, kindLabel, modeLine, outsideLine, untilText, usageShares } from "./aiSchedule";

export function AiScheduleCard({ site }: { site: Site }) {
  const [data, setData] = useState<AiSchedule | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => api.aiSchedule(site.id).then(setData).catch(() => setData(null)), [site.id]);
  // the state changes on its own at the SOC hours' edges: read it again every minute
  useEffect(() => { load(); const t = setInterval(load, 60000); return () => clearInterval(t); }, [load]);
  if (!data) return null;
  const save = async (mode: AiMode, outside?: AiOutside) => {
    setBusy(true);
    try {
      const r = await api.setAiSchedule(site.id, { mode, outside });
      setData(r);
      toast.success(r.mode === "full" ? "Full-time AI" : `SOC hours: ${OUTSIDE_LABEL[r.outside].toLowerCase()} outside them`);
    } catch (e) { toast.error(e); } finally { setBusy(false); }
  };
  const behind = data.servers.filter((s) => s.supported && !s.in_sync);
  const old = data.servers.filter((s) => !s.supported);
  return (
    <div className="card ai-card">
      <div className="row"><h3 style={{ margin: 0 }}>AI</h3><span className="spacer" />
        <span className={`small ${data.effective.qwen === "on" ? "" : "warn-text"}`}>{effectiveLine(data.effective, data.timezone)}</span></div>
      {data.can_edit ? (
        <>
          <div className="field"><span>AI schedule <span className="muted small">hub administrators only: full time is the paid tier</span></span>
            <div className="segmented small-seg" role="radiogroup" aria-label="AI schedule">
              {(["full", "soc"] as AiMode[]).map((m) => (
                <button key={m} type="button" role="radio" aria-checked={data.mode === m} className={data.mode === m ? "active" : ""} disabled={busy}
                  onClick={() => data.mode !== m && save(m)}>{MODE_LABEL[m]}</button>))}
            </div>
          </div>
          {data.mode === "soc" && (
            <div className="field"><span>Outside the SOC hours</span>
              <div className="segmented small-seg" role="radiogroup" aria-label="Event descriptions outside the SOC hours">
                {(["catchup", "skip"] as AiOutside[]).map((o) => (
                  <button key={o} type="button" role="radio" aria-checked={data.outside === o} className={data.outside === o ? "active" : ""} disabled={busy}
                    onClick={() => data.outside !== o && save("soc", o)}>{OUTSIDE_LABEL[o]}</button>))}
              </div>
              <span className="muted small">{OUTSIDE_HELP[data.outside]}</span>
            </div>)}
        </>
      ) : (
        <p className="small" style={{ marginBottom: 4 }}>{modeLine(data.mode)}{data.mode === "soc" && <span className="muted"> · {outsideLine(data.outside)}</span>}</p>
      )}
      {data.mode === "soc" && (
        <p className="muted small">
          Full Qwen while the SOC monitors this Site (<a href={settingsHref(site.id, "monitoring")} onClick={go(settingsHref(site.id, "monitoring"))}>Settings › Monitoring</a>).
          Outside those hours: PPE checks and everything people ask for (Ask, clip chat, Describe &amp; warn, Generate with Qwen).
          {!data.monitored && <strong> The Site isn't monitored, so Qwen is limited around the clock.</strong>}
        </p>)}
      {(behind.length > 0 || old.length > 0) && (
        <p className="small muted">
          {behind.length > 0 && <>{behind.map((s) => s.server_name).join(", ")}: not following it yet ({behind.some((s) => !s.online) ? "offline: takes it when it reconnects" : "being sent"}). </>}
          {old.length > 0 && <>{old.map((s) => s.server_name).join(", ")}: too old for AI schedules (runs Qwen full time).</>}
        </p>)}
      {data.servers.length > 1 && data.servers.some((s) => s.qwen) && (
        <p className="small muted">{data.servers.filter((s) => s.qwen).map((s) => `${s.server_name}: ${s.qwen}${s.until ? ` until ${untilText(s.until, data.timezone)}` : ""}`).join(" · ")}</p>)}
      <div className="ai-usage">
        <Usage title="This month" m={data.usage.this_month} />
        <Usage title="Last month" m={data.usage.last_month} />
      </div>
    </div>
  );
}

/** One month: Qwen time and requests, how much outside the AI schedule, and per kind. Months are UTC. */
function Usage({ title, m }: { title: string; m: AiUsageMonth }) {
  const kinds = usageShares(m.kinds);
  return (
    <div className="ai-usage-month">
      <div className="small"><strong>{title}</strong> <span className="muted">({m.month}, UTC)</span></div>
      <div className="small">{fmtSeconds(m.total.seconds)} of Qwen · {m.total.requests.toLocaleString()} request{m.total.requests === 1 ? "" : "s"}
        {m.out.seconds > 0 && <span className="muted"> · {fmtSeconds(m.out.seconds)} outside the AI schedule</span>}</div>
      {kinds.length === 0 ? <p className="muted small" style={{ margin: 0 }}>No Qwen use reported.</p> : (
        <table className="hub-table small ai-usage-table">
          <thead><tr><th>Kind</th><th>Time</th><th>Requests</th><th>Share</th></tr></thead>
          <tbody>{kinds.map((k) => (
            <tr key={k.kind}>
              <td>{kindLabel(k.kind)}</td>
              <td title={k.out_seconds > 0 ? `${fmtSeconds(k.out_seconds)} of it outside the AI schedule` : undefined}>{fmtSeconds(k.seconds)}</td>
              <td>{k.requests.toLocaleString()}</td>
              <td className="muted">{k.share}%</td>
            </tr>))}
          </tbody>
        </table>)}
    </div>
  );
}
