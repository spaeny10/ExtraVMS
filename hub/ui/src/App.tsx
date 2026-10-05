/**
 * Hub shell: header (nav, Customer picker), routing (nav.ts matchRoute) and the smaller pages: Find, Alerts, Audit,
 * Account. Sites live in SitesPage/SitePage, Customer admin in customer/. /fleet (the old flat server list) redirects
 * to /sites; its server cards are kept under Customer → Servers.
 * Hierarchy: Customer (wire: org) › Site (wire: location) › Server (wire: site) › Camera.
 */
import { useCallback, useEffect, useState } from "react";
import { Dialogs, Icon, OfflineBanner, Toaster, confirmDialog, promptDialog, toast, useIsPhone } from "@site/ui";
import { ThemeToggle } from "@site/ThemeToggle";
import { AlertsPage } from "./AlertsPage";
import { CustomerPage } from "./customer/CustomerPage";
import { UndoButton } from "./customer/FleetActionsPage";
import { FindPage } from "./FindPage";
import { HomePage } from "./HomePage";
import { InvitePage } from "./InvitePage";
import { type CustomerTab, type Page, go, matchRoute, navigate, usePath } from "./nav";
import { SitePage } from "./SitePage";
import { SitesPage } from "./SitesPage";
import { type AuditRow, type Me, type Org, type PushInfo, api, fmtTime } from "./api";

/** Header nav: path, label, icon (phone tab bar), and the pages that light it up. */
const NAV: { path: string; label: string; icon: string; pages: Page[] }[] = [
  { path: "/", label: "Home", icon: "home", pages: ["home"] },
  { path: "/sites", label: "Sites", icon: "grid", pages: ["sites", "site", "server"] },
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
  // the invite page is public: it shows who invited you before any sign-in, and joins a signed-in account in one click
  if (route.page === "invite" && route.code) {
    return (
      <>
        <InvitePage code={route.code} me={me} onSignedOut={() => setMe(null)}
          onJoined={async (orgId) => { await reload(); if (orgId) setOrg(orgId); navigate("/sites"); }} />
        <Toaster /><Dialogs />
      </>
    );
  }
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
      <p className="muted small" style={{ margin: "12px 0 0" }}>Have an invite link? Open it in this browser to join.</p>
    </form>
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
