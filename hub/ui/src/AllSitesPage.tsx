/**
 * All customers (hub administrators only): every customer's Site cards, grouped under the customer's name, from one
 * /api/hub/sites call. Opening a Site first makes its customer the active one, so the breadcrumb, Customer admin and
 * the other per-customer pages follow it.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "@site/ui";
import { type HubSitesOrg, api } from "./api";
import { groupByCustomer } from "./hubAdmin";
import { ServerCard } from "./servers";
import { SiteCard } from "./SitesPage";

export function AllSitesPage({ onOpenCustomer }: { onOpenCustomer: (orgId: string) => void }) {
  const [rows, setRows] = useState<HubSitesOrg[] | null>(null);
  const [now, setNow] = useState(() => Date.now() / 1000);
  const [showRetired, setShowRetired] = useState(false);
  const [q, setQ] = useState("");
  const load = useCallback(() => api.hubSites(showRetired).then((r) => { setRows(r); setNow(Date.now() / 1000); }).catch((e) => toast.error(e)), [showRetired]);
  useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t); }, [load]);
  const groups = useMemo(() => groupByCustomer(rows ?? [], q), [rows, q]);
  if (!rows) return null;
  const online = groups.reduce((n, g) => n + g.servers_online, 0);
  const total = groups.reduce((n, g) => n + g.servers_total, 0);
  return (
    <>
      <h2>Sites <span className="muted small">all customers · {rows.length} customer{rows.length === 1 ? "" : "s"} · {online} of {total} servers online</span>
        <label className="small muted" style={{ marginLeft: 12, fontWeight: 400 }}><input type="checkbox" checked={showRetired} onChange={(e) => setShowRetired(e.target.checked)} /> Show retired</label></h2>
      <div className="row" style={{ marginBottom: 12 }}>
        <input type="search" placeholder="Filter by customer, site or address" value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 360, width: "100%" }} />
      </div>
      {groups.length === 0 && <p className="muted">{q ? "Nothing matches." : "No customers yet. Create one under Customer."}</p>}
      {groups.map((g) => (
        <section key={g.org.id} className="customer-group">
          <h3>
            <a href="/sites" onClick={(e) => { e.preventDefault(); onOpenCustomer(g.org.id); }}>{g.org.name}</a>{" "}
            <span className="muted small">{g.servers_online} of {g.servers_total} servers online{g.open_alerts ? ` · ${g.open_alerts} open alerts` : ""}</span>
          </h3>
          {g.sites.length === 0 && g.unassigned.length === 0 && <p className="muted small">No sites yet.</p>}
          <div className="site-grid">
            {g.sites.map((s) => <SiteCard key={s.id} s={s} now={now} noEvents onOpen={() => onOpenCustomer(g.org.id)} />)}
          </div>
          {g.unassigned.length > 0 && (
            <>
              <p className="muted small">Unassigned servers (not in any site)</p>
              <div className="site-grid">{g.unassigned.map((s) => <ServerCard key={s.id} s={s} now={now} />)}</div>
            </>
          )}
        </section>
      ))}
    </>
  );
}
