/**
 * The incident's append-only log, oldest first so the newest line sits at the bottom next to where the operator
 * types the next note. Read-only everywhere (the workstation, the phone sheet, the customer's Site → Alerts):
 * nothing in the log is ever edited or removed, which is the point of it.
 */
import { useEffect, useRef } from "react";
import type { SiteContact } from "../api";
import { fmtTime } from "../api";
import { logText } from "./format";
import type { LogRow } from "./types";

export function IncidentLog({ rows, contacts = [], dispositionLabel, follow = true, showWho = true }: {
  rows: LogRow[]; contacts?: SiteContact[]; dispositionLabel?: (code: string) => string;
  /** keep the newest line in view as rows arrive */
  follow?: boolean;
  /** drop the "who" column (the hub already names SOC staff "SOC" to customers; this hides even that) */
  showWho?: boolean;
}) {
  // scroll the list itself (its own max-height box), never the page: scrollIntoView would drag the column away
  // from the video the operator is watching whenever a teammate logs something
  const box = useRef<HTMLOListElement>(null);
  const sorted = [...rows].sort((a, b) => a.ts - b.ts || a.id - b.id);
  useEffect(() => { const el = box.current; if (follow && el) el.scrollTop = el.scrollHeight; }, [follow, rows.length]);
  if (!sorted.length) return <p className="muted small">Nothing logged yet.</p>;
  return (
    <ol ref={box} className="soc-log" aria-label="Incident log">
      {sorted.map((r) => (
        <li key={r.id} className={`soc-log-${r.action}`}>
          <time className="muted small" dateTime={new Date(r.ts * 1000).toISOString()}>{fmtTime(r.ts)}</time>
          {showWho && <span className="muted small soc-log-who">{r.user_email ?? r.by ?? "hub"}</span>}
          <span className="soc-log-text">{logText(r, contacts, dispositionLabel)}</span>
        </li>
      ))}
    </ol>
  );
}
