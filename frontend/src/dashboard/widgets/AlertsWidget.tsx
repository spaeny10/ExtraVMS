import { useEffect, useState } from "react";
import { toast } from "../../ui";
import type { DashboardSource, SourceAlert } from "../source";
import type { Widget } from "../types";

const ago = (ts: number) => {
  const s = Math.max(0, Date.now() / 1000 - ts);
  return s < 90 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
};

/** Open alerts across the viewer's sites, with acknowledge. */
export function AlertsWidget({ widget: w, source }: { widget: Widget<"alerts">; source: DashboardSource }) {
  const [rows, setRows] = useState<SourceAlert[] | null>(null);
  const ex = source.extras;
  const labels = ex?.kindLabels ?? {};
  const load = () => source.extras?.alerts?.().then(setRows).catch(() => setRows([]));
  useEffect(() => {
    if (!source.extras?.alerts) return;
    load();
    const t = setInterval(load, 30000);
    const unsub = source.subscribe((m) => { if (m.type !== "event") load(); });
    return () => { clearInterval(t); unsub(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source]);
  if (!source.extras?.alerts) return <div className="dash-empty muted small">Alerts are a hub feature.</div>;
  const shown = (rows ?? []).filter((a) => !w.props.kinds?.length || w.props.kinds.includes(a.kind)).slice(0, w.props.limit ?? 20);
  return (
    <div className="dash-alerts">
      {rows === null && <div className="muted small">Loading…</div>}
      {rows && shown.length === 0 && <div className="muted small ok-text">✓ No open alerts.</div>}
      {shown.map((a) => (
        <div key={a.id} className={`dash-alert ${a.acked_by ? "acked" : ""}`}>
          <span className={`alert-kind ${a.kind}`}>{labels[a.kind] ?? a.kind}</span>
          <span className="dash-alert-text"><strong>{a.site_name}</strong>{typeof a.detail.text === "string" ? ` · ${a.detail.text}` : typeof a.detail.name === "string" ? ` · ${a.detail.name}` : ""}</span>
          <span className="muted small">{ago(a.opened_at)}</span>
          {a.acked_by ? <span className="muted small">acked</span>
            : ex?.ack && <button className="ghost small" onClick={() => ex.ack!(a.id).then(load).catch((e) => toast.error(e))}>Ack</button>}
        </div>
      ))}
    </div>
  );
}
