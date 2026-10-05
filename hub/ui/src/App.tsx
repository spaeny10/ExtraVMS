/**
 * Hub shell: header (nav, Customer picker), routing (nav.ts matchRoute) and the smaller pages: Find, Alerts, Audit,
 * Account and the old Fleet list. Sites live in SitesPage/SitePage, Customer admin in customer/.
 * Hierarchy: Customer (wire: org) › Site (wire: location) › Server (wire: site) › Camera.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Dialogs, Icon, OfflineBanner, Toaster, confirmDialog, promptDialog, toast, useIsPhone } from "@site/ui";
import { ThemeToggle } from "@site/ThemeToggle";
import { ActionCard } from "@site/ActionCard";
import { CustomerPage } from "./customer/CustomerPage";
import { UndoButton } from "./customer/FleetActionsPage";
import { HomePage } from "./HomePage";
import { type CustomerTab, type Page, go, matchRoute, navigate, usePath } from "./nav";
import { SitePage } from "./SitePage";
import { DigestCard, SitesPage } from "./SitesPage";
import { ServerCard } from "./servers";
import { type ActionPlan, type Alert, type AuditRow, type Fleet, type FleetSearch, type Me, type Org, type PushInfo, ago, api, fleetAsk, fmtTime } from "./api";

/** Header nav: path, label, icon (phone tab bar), and the pages that light it up. */
const NAV: { path: string; label: string; icon: string; pages: Page[] }[] = [
  { path: "/", label: "Home", icon: "home", pages: ["home"] },
  { path: "/sites", label: "Sites", icon: "grid", pages: ["sites", "site", "server", "fleet"] },
  { path: "/find", label: "Find", icon: "find", pages: ["find"] },
  { path: "/alerts", label: "Alerts", icon: "alert", pages: ["alerts"] },
  { path: "/customer", label: "Customer", icon: "settings", pages: ["customer"] },
  { path: "/audit", label: "Audit", icon: "events", pages: ["audit"] },
  { path: "/account", label: "Account", icon: "user", pages: ["account"] },
];

export default function App() {
  const path = usePath();
  const route = matchRoute(path);
  const phone = useIsPhone();
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const [org, setOrgState] = useState<string>(() => localStorage.getItem("hubOrg") ?? "");
  const setOrg = useCallback((id: string) => { setOrgState(id); localStorage.setItem("hubOrg", id); }, []);
  const reload = useCallback(() => api.me().then((m) => { setMe(m); if (!m.orgs.some((o) => o.id === org)) setOrg(m.orgs[0]?.id ?? ""); }).catch(() => setMe(null)), [org, setOrg]);
  useEffect(() => { reload(); }, [reload]);
  // aliases and incomplete paths (/org/…, /sites/:id) are replaced by their canonical form, not pushed
  useEffect(() => { if (route.redirect) navigate(route.redirect + location.search + location.hash, true); }, [route.redirect]);
  if (me === undefined) return null;
  if (!me) return <><Login onDone={reload} /><Toaster /><Dialogs /></>;
  const orgs = me.orgs;
  const current = orgs.find((o) => o.id === org) ?? orgs[0];
  const page = route.page;
  // a Site belongs to one customer: switching customer while inside one goes back to the Sites list
  const pickOrg = (id: string) => { setOrg(id); if (page === "site" || page === "server") navigate("/sites"); };
  return (
    <>
      <OfflineBanner />
      <header className="hub-top">
        <span className="brand"><img className="logo" src="/axiom.webp" alt="Axiom Vision" /></span>
        <nav>{NAV.map((n) => (
          <a key={n.path} href={n.path} className={n.pages.includes(page) ? "active" : ""} onClick={go(n.path)}>
            <span className="tab-icon"><Icon name={n.icon} size={20} /></span>{n.label}
          </a>
        ))}</nav>
        <span className="spacer" />
        {orgs.length > 1 && (
          <label className="customer-pick"><span className="muted small">Customer</span>
            <select value={current?.id ?? ""} onChange={(e) => pickOrg(e.target.value)}>{orgs.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}</select>
          </label>
        )}
        {!phone && <span className="muted small">{me.user.email}</span>}
        <ThemeToggle />
        <button className="ghost small" onClick={async () => { await api.logout(); setMe(null); }}>Sign out</button>
      </header>
      <main className="hub-page">
        {page === "home" && current && <HomePage org={current} me={me} />}
        {page === "sites" && current && <SitesPage org={current} me={me} />}
        {(page === "site" || page === "server") && current && route.siteId && (
          <SitePage key={route.siteId} org={current} me={me} siteId={route.siteId} tab={route.tab ?? "live"} serverId={route.serverId} onOrg={setOrg} />
        )}
        {page === "fleet" && <FleetPage org={current} me={me} />}
        {page === "alerts" && current && <AlertsPage org={current} />}
        {page === "find" && current && <FindPage org={current} />}
        {page === "customer" && current && <CustomerPage org={current} me={me} tab={(route.tab ?? "sites") as CustomerTab} onChanged={reload} />}
        {page === "audit" && current && <AuditPage org={current} />}
        {page === "account" && <AccountPage me={me} onChanged={reload} />}
        {!current && page !== "account" && <p className="muted">You're not a member of any customer yet. {me.user.is_super ? "Create one under Customer." : "Ask an owner to add you."}</p>}
      </main>
      <Toaster />
      <Dialogs />
    </>
  );
}

// ---------------------------------------------------------------- login

function Login({ onDone }: { onDone: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [needTotp, setNeedTotp] = useState(false);
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const r = await api.login(email, password, needTotp ? totp : undefined);
      if (r.totp_required) { setNeedTotp(true); return; }
      onDone();
      const next = new URLSearchParams(location.search).get("next");
      if (next && next.startsWith("/")) location.href = next;
    } catch (err) { toast.error(err); } finally { setBusy(false); }
  };
  return (
    <form className="login-box" onSubmit={submit}>
      <h1>Axiom Vision</h1>
      <label className="field"><span>Email</span><input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus /></label>
      <label className="field"><span>Password</span><input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
      {needTotp && <label className="field"><span>Authenticator code</span><input inputMode="numeric" autoComplete="one-time-code" value={totp} onChange={(e) => setTotp(e.target.value)} autoFocus /></label>}
      <button type="submit" disabled={busy || !email || !password}>Sign in</button>
    </form>
  );
}

// ---------------------------------------------------------------- fleet (the old flat server list; /sites replaces it)

function FleetPage({ org, me }: { org: Org | undefined; me: Me }) {
  const [fleet, setFleet] = useState<Fleet | null>(null);
  const [showRetired, setShowRetired] = useState(false);
  const load = useCallback(() => api.fleet(org?.id, showRetired).then(setFleet).catch((e) => toast.error(e)), [org?.id, showRetired]);
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);
  if (!fleet) return null;
  const groups = me.user.is_super && !org ? fleet.orgs : fleet.orgs.filter((g) => !org || g.org.id === org.id);
  return (
    <>
      <p className="muted small">The flat server list. <a href="/sites" onClick={go("/sites")}>Sites</a> groups servers by place.</p>
      {groups.map((g) => (
        <section key={g.org.id}>
          <h2>{g.org.name} <span className="muted small">{g.sites.filter((s) => s.online && !s.retired_at).length} of {g.sites.filter((s) => !s.retired_at).length} online{g.open_alerts ? ` · ${g.open_alerts} open alerts` : ""}</span>
            {(g.retired ?? 0) > 0 && <label className="small muted" style={{ marginLeft: 12, fontWeight: 400 }}><input type="checkbox" checked={showRetired} onChange={(e) => setShowRetired(e.target.checked)} /> Show retired ({g.retired})</label>}</h2>
          {g.sites.length === 0 && <p className="muted">No servers yet. Enrol one under Customer → Servers.</p>}
          <div className="site-grid">{g.sites.map((s) => <ServerCard key={s.id} s={s} now={fleet.now} />)}</div>
          <DigestCard org={g.org} />
        </section>
      ))}
    </>
  );
}

// ---------------------------------------------------------------- find / ask across servers

function FindPage({ org }: { org: Org }) {
  const [q, setQ] = useState(() => new URLSearchParams(location.search).get("q") ?? "");
  const fromUrl = useRef(!!new URLSearchParams(location.search).get("q"));
  const [res, setRes] = useState<FleetSearch | null>(null);
  const [busy, setBusy] = useState(false);
  const [answers, setAnswers] = useState<Record<string, { name: string; text: string; error?: string; done?: boolean }>>({});
  const [asking, setAsking] = useState(false);
  const [action, setAction] = useState<Exclude<ActionPlan, { action: "none" }> | null>(null);
  const search = async () => {
    if (!q.trim()) return;
    setBusy(true); setAnswers({}); setAction(null);
    try { setRes(await api.fleetSearch(org.id, q.trim())); } catch (e) { toast.error(e); } finally { setBusy(false); }
  };
  // arrived from a dashboard Ask box: run the question once, then drop it from the URL
  useEffect(() => {
    if (fromUrl.current) { fromUrl.current = false; history.replaceState(null, "", "/find"); ask(); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const ask = async () => {
    if (!q.trim()) return;
    setAsking(true); setRes(null); setAnswers({}); setAction(null);
    // an instruction ("Migrate Ironsight to Hailo T1") gets a confirmation card instead of going to the servers;
    // if the planner fails the question is simply asked as before
    const plan = await api.actionPlan(org.id, q.trim()).catch(() => null);
    if (plan && plan.action !== "none") { setAction(plan as Exclude<ActionPlan, { action: "none" }>); setAsking(false); return; }
    try {
      await fleetAsk(org.id, q.trim(), (c) => {
        const site = c.site as string | undefined;
        if (c.type === "sites") { const init: typeof answers = {}; for (const s of c.sites as { site: string; site_name: string }[]) init[s.site] = { name: s.site_name, text: "" }; setAnswers(init); return; }
        if (!site) return;
        setAnswers((a) => {
          const cur = a[site] ?? { name: String(c.site_name ?? site), text: "" };
          if (c.type === "delta") return { ...a, [site]: { ...cur, text: cur.text + String(c.text ?? "") } };
          if (c.type === "error") return { ...a, [site]: { ...cur, error: String(c.error), done: true } };
          if (c.type === "site_done" || c.type === "done") return { ...a, [site]: { ...cur, done: true } };
          return a;
        });
      });
    } catch (e) { toast.error(e); } finally { setAsking(false); }
  };
  return (
    <>
      <h2>Find across {org.name}</h2>
      <form className="row" onSubmit={(e) => { e.preventDefault(); search(); }}>
        <input style={{ flex: 1, minWidth: 260 }} value={q} onChange={(e) => setQ(e.target.value)} placeholder='Search every server: "white pickup truck", "person at the back door last night"…' />
        <button type="submit" disabled={busy || !q.trim()}>Search</button>
        <button type="button" className="ghost" disabled={asking || !q.trim()} onClick={ask} title="Every server's assistant answers from its own footage. Instructions such as &quot;Migrate Ironsight to Hailo T1&quot; show a confirmation card instead">✦ Ask all servers</button>
      </form>
      <p className="small" style={{ margin: "4px 0 0" }}><a href="/customer/actions" onClick={go("/customer/actions")}>What can I ask the hub to do?</a></p>
      {action && (
        <ActionCard key={action.id} plan={action} helpHref="/customer/actions" onClose={() => setAction(null)}
          onExecute={(x) => api.actionExecute(org.id, action.id, x)}
          onUndo={(r) => api.actionUndo(org.id, r.audit_id!)} />
      )}
      {res && (
        <>
          <p className="muted small">{res.sites.map((s) => `${s.site_name}: ${s.events} events, ${s.footage} moments${s.error ? ` (${s.error})` : ""}`).join(" · ")}{res.offline.length ? ` · offline: ${res.offline.join(", ")}` : ""}</p>
          {res.events.length === 0 && res.footage.length === 0 && <p className="muted">Nothing matched.</p>}
          <div className="site-grid">
            {res.events.map((e) => (
              <a key={`${e.site_id}-${e.id}`} className="site-card" href={`/s/${e.site_id}/#timeline?cam=${e.camera_id}&event=${e.id}`}>
                <div className="head"><strong>{e.site_name}</strong><span className="spacer" /><span className="muted small">{fmtTime(e.start_ts)}</span></div>
                <div className="row" style={{ alignItems: "flex-start" }}>
                  {e.snapshot ? <img src={`/s/${e.site_id}/api/events/${e.id}/media/snapshot.jpg`} alt="" style={{ width: 140, borderRadius: 6 }} /> : null}
                  <div className="small">{e.synopsis || `${e.camera_class} · ${e.camera_id}`}</div>
                </div>
              </a>
            ))}
          </div>
          {res.footage.length > 0 && (
            <>
              <h3>Footage moments</h3>
              <div className="row">{res.footage.map((m, i) => <a key={i} className="chip" href={`/s/${m.site_id}/#timeline?cam=${m.camera_id}&t=${Math.round(m.ts)}`}>{m.site_name} · {m.camera_id} · {fmtTime(m.ts)}</a>)}</div>
            </>
          )}
        </>
      )}
      {Object.keys(answers).length > 0 && (
        <div className="site-grid" style={{ marginTop: 12 }}>
          {Object.entries(answers).map(([id, a]) => (
            <div key={id} className="site-card">
              <div className="head"><strong>{a.name}</strong><span className="spacer" /><span className="muted small">{a.done ? "" : "thinking…"}</span></div>
              {a.error ? <p className="small" style={{ color: "var(--bad)" }}>{a.error}</p> : <pre style={{ whiteSpace: "pre-wrap", font: "inherit", margin: "6px 0 0" }}>{a.text || (a.done ? "No answer." : "")}</pre>}
              <a className="small" href={`/s/${id}/#find`}>Open this server's Find →</a>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------- alerts

const KIND_LABEL: Record<string, string> = { offline: "Server offline", camera_down: "Camera down", disk: "Disk", clock: "Clock skew", event_high: "High priority", event_policy: "Site rule", event_watched: "Watch list" };

function AlertsPage({ org }: { org: Org }) {
  const [rows, setRows] = useState<Alert[]>([]);
  const [showClosed, setShowClosed] = useState(false);
  const load = useCallback(() => api.alerts(org.id, !showClosed).then(setRows).catch((e) => toast.error(e)), [org.id, showClosed]);
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);
  const link = (a: Alert) => {
    const d = a.detail as { id?: number; camera_id?: string; name?: string };
    return d.id && d.camera_id ? `/s/${a.site_id}/#timeline?cam=${d.camera_id}&event=${d.id}` : a.kind === "camera_down" ? `/s/${a.site_id}/#cameras` : `/s/${a.site_id}/`;
  };
  return (
    <>
      <h2>Alerts <span className="muted small">{org.name}</span></h2>
      <label className="row small"><input type="checkbox" checked={showClosed} onChange={(e) => setShowClosed(e.target.checked)} /> Include closed</label>
      {rows.length === 0 ? <p className="muted">Nothing open.</p> : (
        <table className="hub-table">
          <thead><tr><th>When</th><th>Site</th><th>Server</th><th>Kind</th><th>What</th><th /></tr></thead>
          <tbody>
            {rows.map((a) => {
              const d = a.detail as Record<string, unknown>;
              const what = a.kind === "camera_down" ? `${d.name ?? a.key}: ${(d.problems as string[] | undefined)?.join("; ") || "no stream"}`
                : a.kind === "clock" ? `${d.skew_s} s off` : a.kind === "disk" ? String(d.message ?? "low space")
                : a.kind === "offline" ? `last seen ${ago(d.last_seen_at as number)}` : `${d.name ? `${d.name} · ` : ""}${d.text ?? d.synopsis ?? ""}`;
              return (
                <tr key={a.id} className={a.closed_at ? "muted" : ""}>
                  <td>{fmtTime(a.opened_at)}</td>
                  <td>{a.location_name ?? "—"}</td>
                  <td>{a.site_name}</td>
                  <td><span className={`alert-kind ${a.kind}`}>{KIND_LABEL[a.kind] ?? a.kind}</span></td>
                  <td><a href={link(a)}>{what}</a></td>
                  <td>{!a.closed_at && <button className="ghost small" onClick={async () => { await api.ack(a.id); load(); }}>Ack</button>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </>
  );
}

function PushCard() {
  const [info, setInfo] = useState<PushInfo | null>(null);
  const [kinds, setKinds] = useState<string[]>(["offline", "event_policy", "event_watched", "event_high"]);
  const load = () => api.pushInfo().then(setInfo).catch(() => setInfo(null));
  useEffect(() => { load(); }, []);
  const supported = "serviceWorker" in navigator && "PushManager" in window && window.isSecureContext;
  const enable = async () => {
    try {
      const reg = await navigator.serviceWorker.ready;
      const perm = await Notification.requestPermission();
      if (perm !== "granted") { toast.error("Notifications were not allowed by the browser"); return; }
      const key = Uint8Array.from(atob(info!.public_key.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(info!.public_key.length / 4) * 4, "=")), (c) => c.charCodeAt(0));
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
      await api.pushSubscribe(sub.toJSON(), kinds);
      toast.success("This browser will be notified"); load();
    } catch (e) { toast.error(e); }
  };
  const disable = async () => {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (sub) { await api.pushUnsubscribe(sub.endpoint); await sub.unsubscribe(); }
    toast.success("Notifications off on this browser"); load();
  };
  const LABEL: Record<string, string> = { offline: "Server offline", camera_down: "Camera down", disk: "Disk low", clock: "Clock skew", event_high: "High-priority event", event_policy: "Site rule broken", event_watched: "Watched person/vehicle" };
  return (
    <div className="card">
      <h3>Notifications</h3>
      {!supported ? <p className="muted small">This browser can't receive push notifications here (needs HTTPS and a modern browser).</p> : (
        <>
          <div className="row">{(info?.kinds ?? Object.keys(LABEL)).map((k) => <label key={k} className="small row"><input type="checkbox" checked={kinds.includes(k)} onChange={(e) => setKinds((ks) => e.target.checked ? [...ks, k] : ks.filter((x) => x !== k))} /> {LABEL[k] ?? k}</label>)}</div>
          <div className="row" style={{ marginTop: 8 }}>
            <button className="small" onClick={enable} disabled={!info}>Notify this browser</button>
            <button className="ghost small" onClick={disable}>Turn off here</button>
            <span className="muted small">{info?.subscriptions.length ?? 0} browser(s) subscribed</span>
          </div>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- audit

function AuditPage({ org }: { org: Org }) {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const load = useCallback(() => api.audit(org.id).then(setRows).catch((e) => toast.error(e)), [org.id]);
  useEffect(() => { load(); }, [load]);
  return (
    <>
      <h2>Audit <span className="muted small">{org.name} · who did what through the hub</span></h2>
      <table className="hub-table">
        <thead><tr><th>When</th><th>Who</th><th>Site · Server</th><th>Action</th><th>Result</th><th>From</th></tr></thead>
        <tbody>{rows.map((r) => <tr key={r.id}><td>{fmtTime(r.ts)}</td><td>{r.user_email ?? "—"}</td><td className="muted small">{[r.location_name, r.site_id].filter(Boolean).join(" · ")}</td>
          <td>{r.action}{r.undo_until ? <> <UndoButton org={org} id={r.id} label={r.action} onDone={load} /></> : null}</td>
          <td>{r.status ?? ""}</td><td className="muted small">{r.ip ?? ""}</td></tr>)}</tbody>
      </table>
      {rows.length === 0 && <p className="muted">Nothing yet.</p>}
    </>
  );
}

// ---------------------------------------------------------------- account

function AccountPage({ me, onChanged }: { me: Me; onChanged: () => void }) {
  const [setup, setSetup] = useState<{ secret: string; uri: string } | null>(null);
  const [code, setCode] = useState("");
  return (
    <>
      <h2>Account <span className="muted small">{me.user.email}</span></h2>
      <div className="card">
        <h3>Two-factor sign-in</h3>
        {me.user.totp_enabled ? (
          <div className="row"><span>Authenticator app: <strong>on</strong></span><button className="ghost small" onClick={async () => { if (await confirmDialog("Turn off two-factor sign-in?", { danger: true, confirmLabel: "Turn off" })) { await api.totpDisable(); onChanged(); } }}>Turn off</button></div>
        ) : setup ? (
          <div>
            <p className="small">Add this to your authenticator app (Google Authenticator, 1Password, Authy…), then enter a code:</p>
            <code style={{ wordBreak: "break-all" }}>{setup.uri}</code>
            <p className="muted small">Secret: {setup.secret}</p>
            <div className="row"><input placeholder="123456" value={code} onChange={(e) => setCode(e.target.value)} /><button onClick={async () => { try { await api.totpEnable(code); setSetup(null); onChanged(); toast.success("Two-factor sign-in is on"); } catch (e) { toast.error(e); } }}>Confirm</button></div>
          </div>
        ) : (
          <div className="row"><span>Authenticator app: off</span><button className="ghost small" onClick={() => api.totpSetup().then(setSetup).catch((e) => toast.error(e))}>Set up</button></div>
        )}
      </div>
      <PushCard />
      <div className="card">
        <h3>Password</h3>
        <button className="ghost small" onClick={async () => {
          const cur = await promptDialog("Current password", { label: "Current password" }); if (!cur) return;
          const next = await promptDialog("New password", { label: "At least 10 characters" }); if (!next) return;
          try { await api.password(cur, next); toast.success("Password changed"); } catch (e) { toast.error(e); }
        }}>Change password…</button>
      </div>
    </>
  );
}
