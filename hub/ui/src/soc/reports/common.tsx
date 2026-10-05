/**
 * Shared by the SOC reports: the time-range picker, the customer list, saving a client-side export, copy to clipboard.
 */
import { useEffect, useState } from "react";
import { toast } from "@site/ui";
import { type Me, type Org, api } from "../../api";
import { type Range, type RangeChoice, RANGES, fromLocalInput, reportRange, toLocalInput } from "../format";

export type RangeValue = { choice: RangeChoice; custom: Partial<Range> };
export const DEFAULT_RANGE: RangeValue = { choice: "24h", custom: {} };

/**
 * This shift · 24 h · 7 d · 30 d · custom. The range is recomputed from the choice when a report loads (not kept as
 * numbers), so "24 h" means the last 24 hours at the moment of each refresh.
 */
export function RangePicker({ value, onChange }: { value: RangeValue; onChange: (v: RangeValue) => void }) {
  const cur = reportRange(value.choice, Date.now() / 1000, value.custom);
  return (
    <div className="row soc-range">
      <div className="segmented" role="radiogroup" aria-label="Period">
        {RANGES.map(([id, label]) => (
          <button key={id} role="radio" aria-checked={value.choice === id} className={value.choice === id ? "active" : ""}
            onClick={() => onChange({ choice: id, custom: id === "custom" && !value.custom.since ? { since: cur.since, until: cur.until } : value.custom })}>{label}</button>
        ))}
      </div>
      {value.choice === "custom" && (<>
        <label className="small">From <input type="datetime-local" value={value.custom.since ? toLocalInput(value.custom.since) : ""}
          onChange={(e) => onChange({ ...value, custom: { ...value.custom, since: fromLocalInput(e.target.value) ?? undefined } })} /></label>
        <label className="small">To <input type="datetime-local" value={value.custom.until ? toLocalInput(value.custom.until) : ""}
          onChange={(e) => onChange({ ...value, custom: { ...value.custom, until: fromLocalInput(e.target.value) ?? undefined } })} /></label>
      </>)}
    </div>
  );
}

export const rangeOf = (v: RangeValue) => reportRange(v.choice, Date.now() / 1000, v.custom);

/**
 * The customers a report can be narrowed to: every customer for hub administrators (GET /api/orgs), otherwise the
 * ones the SOC role makes visible, which the hub already lists in `me.orgs`.
 */
export function useCustomers(me: Me): Org[] {
  const [orgs, setOrgs] = useState<Org[]>(me.orgs);
  useEffect(() => {
    if (!me.user.is_super) { setOrgs(me.orgs); return; }
    api.orgs().then(setOrgs).catch(() => setOrgs(me.orgs));
  }, [me]);
  return [...orgs].sort((a, b) => a.name.localeCompare(b.name));
}

export function CustomerSelect({ orgs, value, onChange, all = "All customers" }: { orgs: Org[]; value: string; onChange: (v: string) => void; all?: string | null }) {
  return (
    <select aria-label="Customer" value={value} onChange={(e) => onChange(e.target.value)}>
      {all != null && <option value="">{all}</option>}
      {orgs.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
    </select>
  );
}

/** Count breakdowns ("By priority: high 2 · medium 1"), codes in plain words. */
export function Breakdowns({ lines }: { lines: { key: string; label: string; parts: [string, number][] }[] }) {
  return (
    <dl className="soc-breakdowns">
      {lines.map((l) => (<div key={l.key}><dt>{l.label}</dt><dd>{l.parts.map(([k, n]) => `${k.replace(/_/g, " ")} ${n}`).join(" · ")}</dd></div>))}
    </dl>
  );
}

/** Save text the page built itself (a CSV of the table on screen) as a file: nothing is fetched. */
export function saveText(name: string, text: string, type = "text/csv;charset=utf-8") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function copyText(text: string) {
  try { await navigator.clipboard.writeText(text); toast.success("Copied"); }
  catch { toast.error("The browser didn't allow copying: select the text and copy it instead"); }
}

/** "2026-10-04" for a file name. */
export const fileDate = (ts: number) => new Date(ts * 1000).toISOString().slice(0, 10);

/** A report request that hasn't landed on the hub yet answers 404/405: say so plainly instead of a raw error. */
export function reportError(e: unknown): string {
  const s = String(e instanceof Error ? e.message : e);
  // FastAPI's own answers for a route it doesn't have (a real "not found" from the route carries its own words)
  if (/^(Error: )?(404|405)\b/.test(s) && /"detail"\s*:\s*"(Not Found|Method Not Allowed)"/.test(s)) return "This hub doesn't have SOC reports yet (update the hub).";
  if (/^(Error: )?403\b/.test(s)) return "You can't see this report.";
  const m = /"detail"\s*:\s*"([^"]+)"/.exec(s);
  return m ? m[1] : s.replace(/^Error:\s*/, "");
}

/** Load a report whenever `key` changes; the latest request wins (a slow older one can't overwrite it). */
export function useReport<T>(load: () => Promise<T>, key: string): { data: T | null; error: string | null; loading: boolean; reload: () => void } {
  const [state, setState] = useState<{ data: T | null; error: string | null; loading: boolean }>({ data: null, error: null, loading: true });
  const [n, setN] = useState(0);
  useEffect(() => {
    let live = true;
    setState((s) => ({ ...s, loading: true }));
    load().then((data) => { if (live) setState({ data, error: null, loading: false }); })
      .catch((e) => { if (live) setState({ data: null, error: reportError(e), loading: false }); });
    return () => { live = false; };
    // `load` is a fresh closure every render; `key` names what it depends on
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, n]);
  return { ...state, reload: () => setN((x) => x + 1) };
}
