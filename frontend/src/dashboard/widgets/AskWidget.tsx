import { useState } from "react";
import type { DashboardSource } from "../source";
import type { Widget } from "../types";

/** A question box: opens Find with the question so Qwen answers there. */
export function AskWidget({ widget: w, source }: { widget: Widget<"ask">; source: DashboardSource }) {
  const [q, setQ] = useState("");
  const href = source.extras?.askHref;
  if (!href) return <div className="dash-empty muted small">Ask is not available here.</div>;
  return (
    <form className="dash-ask" onSubmit={(e) => { e.preventDefault(); if (q.trim()) location.href = href(q.trim()); }}>
      <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={w.props.placeholder || 'Ask every site: "Was anyone in the yard after 6pm?"'} />
      <button type="submit" className="ask-btn" disabled={!q.trim()}>✦ Ask</button>
    </form>
  );
}
