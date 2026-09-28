import { useEffect, useState } from "react";
import { BriefingCard } from "../../Ask";
import type { DashboardSource, SourceDigest } from "../source";
import type { Widget } from "../types";

/** The organisation digest, or one site's own daily briefing (through that site's API). */
export function BriefingWidget({ widget: w, source }: { widget: Widget<"briefing">; source: DashboardSource }) {
  const p = w.props;
  if (p.source === "site") {
    if (!p.site) return <div className="dash-empty muted small">Choose a site in ⚙ settings.</div>;
    const site = source.sites().find((s) => s.id === p.site);
    return (
      <div className="dash-briefing">
        {site && source.sites().length > 1 && <div className="muted small">{site.name}</div>}
        <BriefingCard site={source.siteApi(p.site)} compact readOnly={!source.extras?.briefingEditable}
          onEvent={(id) => { const e = { site_id: p.site, id }; if (source.openEvent) source.openEvent(e); else location.href = source.eventHref(e); }} />
      </div>
    );
  }
  return <DigestBody source={source} />;
}

function DigestBody({ source }: { source: DashboardSource }) {
  const [d, setD] = useState<SourceDigest | undefined>(undefined);
  useEffect(() => {
    let alive = true;
    const load = () => source.extras?.digest?.().then((v) => { if (alive) setD(v); }).catch(() => { if (alive) setD(null); });
    load();
    const t = setInterval(load, 10 * 60 * 1000);
    return () => { alive = false; clearInterval(t); };
  }, [source]);
  if (!source.extras?.digest) return <div className="dash-empty muted small">No digest on this site; switch the widget to a site briefing.</div>;
  if (d === undefined) return <div className="muted small">Loading…</div>;
  if (!d) return <div className="dash-empty muted small">No digest yet. One is written every morning; the Fleet page can generate one now.</div>;
  return (
    <div className="dash-briefing">
      <div className="muted small">Digest · {d.day}{d.model ? ` · ${d.model}` : ""}</div>
      <pre className="dash-pre">{d.text}</pre>
    </div>
  );
}
