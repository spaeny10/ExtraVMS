/**
 * Shift reports: the hub writes one at each shift end (counts plus a short text from the shared model, or a plain
 * fallback when no model answers) and keeps them. Pick one to read its text and numbers; copy the text for a handover
 * message. Supervisors can write one now, for the shift so far or a chosen period.
 */
import { useEffect, useState } from "react";
import { toast } from "@site/ui";
import type { Me } from "../../api";
import { isSocSupervisor } from "../../access";
import { fromLocalInput, keyLabel, numberTiles } from "../format";
import { breakdowns } from "./normalize";
import { socReportsApi } from "../socApi";
import type { ShiftReport as Report } from "../types";
import { Breakdowns, copyText, reportError, useReport } from "./common";

const span = (r: Pick<Report, "period_start" | "period_end">) => {
  const a = new Date(r.period_start * 1000), b = new Date(r.period_end * 1000);
  const day: Intl.DateTimeFormatOptions = { weekday: "short", month: "short", day: "numeric" };
  const hm: Intl.DateTimeFormatOptions = { hour: "2-digit", minute: "2-digit" };
  return `${a.toLocaleDateString(undefined, day)} ${a.toLocaleTimeString(undefined, hm)} – ${a.toDateString() === b.toDateString() ? "" : `${b.toLocaleDateString(undefined, day)} `}${b.toLocaleTimeString(undefined, hm)}`;
};

export function ShiftReport({ me }: { me: Me }) {
  const sup = isSocSupervisor(me);
  const list = useReport(() => socReportsApi.shifts(60), "list");
  const reports = [...(list.data ?? [])].sort((a, b) => b.period_end - a.period_end || b.id - a.id);
  const [selected, setSelected] = useState<number | null>(null);
  const [full, setFull] = useState<Report | null>(null);
  const [busy, setBusy] = useState(false);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const id = selected ?? reports[0]?.id ?? null;

  // the list may carry the text already; the single report is fetched anyway (a list may be trimmed for size)
  useEffect(() => {
    if (id == null) { setFull(null); return; }
    let live = true;
    setFull(reports.find((r) => r.id === id) ?? null);
    socReportsApi.shift(id).then((r) => { if (live) setFull(r); }).catch(() => {});
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, list.data]);

  const generate = async () => {
    const s = fromLocalInput(start), e = fromLocalInput(end);
    if (s != null && e != null && e <= s) { toast.error("The end is before the start"); return; }
    setBusy(true);
    try {
      const r = await socReportsApi.generateShift({ ...(s != null ? { start: s } : {}), ...(e != null ? { end: e } : {}) });
      toast.success("Shift report written");
      setSelected(r.id ?? null);
      list.reload();
    } catch (err) { toast.error(reportError(err)); } finally { setBusy(false); }
  };

  const tiles = numberTiles(full?.data ?? null);
  const lines = breakdowns(full?.data ?? null, keyLabel);
  return (
    <section className="card soc-report">
      <div className="row soc-report-head">
        <h2>Shift reports</h2>
        {sup && (
          <div className="row soc-generate">
            <label className="small">From <input type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} /></label>
            <label className="small">To <input type="datetime-local" value={end} onChange={(e) => setEnd(e.target.value)} /></label>
            <button className="small" disabled={busy} onClick={generate} title="Blank: the last completed shift">{busy ? "Writing…" : "Generate now"}</button>
          </div>
        )}
      </div>
      {list.error ? <p className="muted">{list.error}</p> : list.loading && !list.data ? <p className="muted">Loading…</p> : reports.length === 0 ? (
        <p className="muted">No shift reports yet. The hub writes one at the end of each shift{sup ? "; Generate now writes one for the last completed shift, or the period you pick" : ""}.</p>
      ) : (
        <div className="soc-shifts">
          <ul className="soc-shift-list" aria-label="Shift reports">
            {reports.map((r) => (
              <li key={r.id}>
                <button className={`soc-shift-item ${r.id === id ? "active" : ""}`} aria-current={r.id === id} onClick={() => setSelected(r.id)}>
                  <span>{span(r)}</span>
                  <span className="muted small">written {new Date(r.created_at * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</span>
                </button>
              </li>
            ))}
          </ul>
          <div className="soc-shift-body">
            {!full ? <p className="muted">Loading…</p> : (<>
              <div className="row soc-section-head">
                <h3>{span(full)}</h3>
                <button className="ghost small" disabled={!full.text} onClick={() => copyText(full.text ?? "")}>Copy</button>
                {full.model && <span className="muted small" title="Written by">{full.model}</span>}
              </div>
              {tiles.length > 0 && <div className="soc-tiles small">{tiles.map((t) => <div key={t.key} className="soc-tile"><span className="soc-tile-value">{t.value}</span><span className="soc-tile-name">{t.label}</span></div>)}</div>}
              {lines.length > 0 && <Breakdowns lines={lines} />}
              {full.text ? <pre className="soc-report-text">{full.text}</pre> : <p className="muted">This report has no text.</p>}
            </>)}
          </div>
        </div>
      )}
    </section>
  );
}
