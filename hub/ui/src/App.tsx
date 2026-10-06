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
import { AllSitesPage } from "./AllSitesPage";
import { CustomerPage } from "./customer/CustomerPage";
import { UndoButton } from "./customer/FleetActionsPage";
import { FindPage } from "./FindPage";
import { HomePage } from "./HomePage";
import { InvitePage } from "./InvitePage";
import { type CustomerTab, fullBleed, go, matchRoute, navigate, topNav, usePath } from "./nav";
import { SitePage } from "./SitePage";
import { SitesPage } from "./SitesPage";
import { type AuditRow, type HubAdmin, type Me, type Org, type PushInfo, ago, api, fmtTime } from "./api";
import { ALL_CUSTOMERS } from "./hubAdmin";
import { KIND_LABEL } from "./labels";
import { isSocUser, socLanding } from "./access";
import { SocHeader } from "./soc/SocHeader";
import { SocRouter } from "./soc/SocRouter";
import { SocStreamProvider } from "./soc/useSocStream";

export default function App() {
  const path = usePath();
  const route = matchRoute(path);
  const phone = useIsPhone();
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const [org, setOrgState] = useState<string>(() => localStorage.getItem("hubOrg") ?? "");
  const setOrg = useCallback((id: string) => { setOrgState(id); localStorage.setItem("hubOrg", id); }, []);
  // "All customers" (hub administrators): a mode on top of the active customer rather than a fake org id, so the
  // per-customer pages keep a real customer (the last one picked) and hubOrg never holds something reload() rejects
  const [allMode, setAllState] = useState<boolean>(() => localStorage.getItem("hubAllCustomers") === "1");
  const setAll = useCallback((on: boolean) => { setAllState(on); if (on) localStorage.setItem("hubAllCustomers", "1"); else localStorage.removeItem("hubAllCustomers"); }, []);
  // opening a Site (or a customer's header) from All customers: that customer becomes the active one
  const openCustomer = useCallback((id: string) => { setAll(false); setOrg(id); }, [setAll, setOrg]);
  const reload = useCallback(() => api.me().then((m) => { setMe(m); if (!m.orgs.some((o) => o.id === org)) setOrg(m.orgs[0]?.id ?? ""); }).catch(() => setMe(null)), [org, setOrg]);
  useEffect(() => { reload(); }, [reload]);
  // aliases and incomplete paths (/org/…, /sites/:id) are replaced by their canonical form, not pushed
  useEffect(() => { if (route.redirect) navigate(route.redirect + location.search + location.hash, true); }, [route.redirect]);
  // inside a Site you're in exactly one customer (SitePage's onOrg switches to it if needed), so "all" ends there
  useEffect(() => { if (allMode && (route.page === "site" || route.page === "server")) setAll(false); }, [allMode, route.page, setAll]);
  // SOC staff with no customer of their own have nothing on Home: `/` opens their console (only the bare `/`, so a
  // stray path that falls back to Home doesn't bounce them)
  const landing = me ? socLanding(me) : null;
  useEffect(() => { if (landing && path === "/") navigate(landing, true); }, [landing, path]);
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
  const soc = isSocUser(me);
  const all = allMode && me.user.is_super;   // a stored "all" from a hub admin since demoted is ignored
  // a Site belongs to one customer: switching customer while inside one goes back to the Sites list
  const pickOrg = (id: string) => {
    if (id === ALL_CUSTOMERS) { setAll(true); navigate("/sites"); return; }
    setAll(false); setOrg(id);
    if (page === "site" || page === "server") navigate("/sites");
  };
  // one SOC socket and alarm ringer for the header and the page, only while a SOC page is open (leaving the console
  // closes the socket and silences the alarm; the provider stays mounted so going back reconnects without a remount)
  return (
    <SocStreamProvider me={me} enabled={page === "soc" && soc}>
      <OfflineBanner />
      <header className="hub-top">
        <span className="brand"><img className="logo" src="/axiom.webp" alt="Axiom Vision" /></span>
        {/* inside a Site the customer-wide Find/Alerts step aside for the Site's own tabs (nav.ts topNav) */}
        <nav>{topNav(page, soc).map((n) => (
          <a key={n.path} href={n.path} title={n.title} className={n.pages.includes(page) ? "active" : ""} onClick={go(n.path)}>
            <span className="tab-icon"><Icon name={n.icon} size={20} /></span>{n.label}
          </a>
        ))}</nav>
        <span className="spacer" />
        {/* the SOC works across customers: its pages swap the picker for presence and the alarm sound */}
        {page === "soc" && soc && <SocHeader me={me} />}
        {page !== "soc" && (orgs.length > 1 || (me.user.is_super && orgs.length > 0)) && (
          <label className="customer-pick"><span className="muted small">Customer</span>
            {/* "All customers" is a Sites-list view: other pages work in one customer, so the picker names that one there */}
            <select value={all && page === "sites" ? ALL_CUSTOMERS : current?.id ?? ""} onChange={(e) => pickOrg(e.target.value)}>
              {me.user.is_super && <option value={ALL_CUSTOMERS}>All customers</option>}
              {orgs.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
            </select>
          </label>
        )}
        {!phone && <span className="muted small">{me.user.email}</span>}
        <ThemeToggle />
        <button className="ghost small" onClick={async () => { await api.logout(); setMe(null); }}>Sign out</button>
      </header>
      <main className={`hub-page ${fullBleed(page) ? "wide" : ""} ${page === "soc" ? "soc-wide" : ""}`}>
        {page === "home" && current && <HomePage org={current} me={me} />}
        {page === "sites" && all && <AllSitesPage onOpenCustomer={openCustomer} />}
        {page === "sites" && !all && current && <SitesPage org={current} me={me} />}
        {(page === "site" || page === "server") && current && route.siteId && (
          <SitePage key={route.siteId} org={current} me={me} siteId={route.siteId} tab={route.tab ?? "live"} section={route.section} serverId={route.serverId} onOrg={openCustomer} />
        )}
        {page === "alerts" && current && <AlertsPage org={current} />}
        {page === "find" && current && <FindPage org={current} />}
        {page === "customer" && current && <CustomerPage org={current} me={me} tab={(route.tab ?? "sites") as CustomerTab} onChanged={reload} />}
        {page === "audit" && current && <AuditPage org={current} />}
        {page === "account" && <AccountPage me={me} onChanged={reload} />}
        {page === "soc" && (soc ? <SocRouter me={me} route={route} /> : <p className="muted">You're not in the SOC. Ask a hub administrator.</p>)}
        {!current && page !== "account" && page !== "soc" && !landing && <p className="muted">You're not a member of any customer yet. {me.user.is_super ? "Create one under Customer." : "Ask an owner to add you."}</p>}
      </main>
      <Toaster />
      <Dialogs />
    </SocStreamProvider>
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
  const LABEL = KIND_LABEL;
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
      <table className="hub-table stack">
        <thead><tr><th>When</th><th>Who</th><th>Site · Server</th><th>Action</th><th>Result</th><th>From</th></tr></thead>
        <tbody>{rows.map((r) => <tr key={r.id}><td>{fmtTime(r.ts)}</td><td>{r.user_email ?? "—"}</td><td className="muted small">{[r.location_name, r.site_id].filter(Boolean).join(" · ")}</td>
          <td className="wide">{r.action}{r.undo_until ? <> <UndoButton org={org} id={r.id} label={r.action} onDone={load} /></> : null}</td>
          <td data-label={r.status != null ? "Result" : undefined}>{r.status ?? ""}</td><td className="muted small">{r.ip ?? ""}</td></tr>)}</tbody>
      </table>
      {rows.length === 0 && <p className="muted">Nothing yet.</p>}
    </>
  );
}

// ---------------------------------------------------------------- account

/**
 * Asks again for the password (and, with two-factor on, a current authenticator code) before a two-factor change:
 * a signed-in session alone can't turn two-factor off or move it to another phone.
 */
function ReauthForm({ needCode, submitLabel, danger, onSubmit, onCancel }: {
  needCode: boolean; submitLabel: string; danger?: boolean;
  onSubmit: (password: string, code: string) => Promise<void>; onCancel: () => void;
}) {
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <form style={{ marginTop: 8 }} onSubmit={async (e) => {
      e.preventDefault();
      setBusy(true);
      try { await onSubmit(password, code.trim()); } catch (err) { toast.error(err); } finally { setBusy(false); }
    }}>
      <label className="field"><span>Current password</span><input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} autoFocus /></label>
      {needCode && <label className="field"><span>Code from your authenticator app</span><input inputMode="numeric" autoComplete="one-time-code" placeholder="123456" value={code} onChange={(e) => setCode(e.target.value)} /></label>}
      <div className="row">
        <button type="submit" className={danger ? "danger" : undefined} disabled={busy || !password || (needCode && !code.trim())}>{submitLabel}</button>
        <button type="button" className="ghost" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

function AccountPage({ me, onChanged }: { me: Me; onChanged: () => void }) {
  const [setup, setSetup] = useState<{ secret: string; uri: string } | null>(null);
  const [code, setCode] = useState("");
  const [asking, setAsking] = useState<null | "off" | "setup">(null);
  return (
    <>
      <h2>Account <span className="muted small">{me.user.email}</span></h2>
      <div className="card">
        <h3>Two-factor sign-in</h3>
        {asking ? (
          <ReauthForm needCode={me.user.totp_enabled} danger={asking === "off"}
            submitLabel={asking === "off" ? "Turn off" : "Continue"} onCancel={() => setAsking(null)}
            onSubmit={async (password, c) => {
              if (asking === "off") {
                await api.totpDisable(password, c);
                setAsking(null); onChanged(); toast.success("Two-factor sign-in is off");
              } else {
                setSetup(await api.totpSetup(password, c)); setCode(""); setAsking(null);
              }
            }} />
        ) : setup ? (
          <div>
            <p className="small">Add this to your authenticator app (Google Authenticator, 1Password, Authy…), then enter a code:</p>
            <code style={{ wordBreak: "break-all" }}>{setup.uri}</code>
            <p className="muted small">Secret: {setup.secret}</p>
            {me.user.totp_enabled && <p className="muted small">Your current authenticator keeps working until you confirm a code from the new one.</p>}
            <div className="row"><input placeholder="123456" inputMode="numeric" autoComplete="one-time-code" value={code} onChange={(e) => setCode(e.target.value)} /><button onClick={async () => { try { await api.totpEnable(code); setSetup(null); onChanged(); toast.success("Two-factor sign-in is on"); } catch (e) { toast.error(e); } }}>Confirm</button>
              <button className="ghost" onClick={() => setSetup(null)}>Cancel</button></div>
          </div>
        ) : me.user.totp_enabled ? (
          <div className="row"><span>Authenticator app: <strong>on</strong></span><span className="spacer" />
            <button className="ghost small" onClick={() => setAsking("setup")}>Move to a new device…</button>
            <button className="ghost small" onClick={() => setAsking("off")}>Turn off…</button></div>
        ) : (
          <div className="row"><span>Authenticator app: off</span><button className="ghost small" onClick={() => setAsking("setup")}>Set up…</button></div>
        )}
      </div>
      <PushCard />
      {me.user.is_super && <HubAdminsBox me={me} onChanged={onChanged} />}
      <div className="card">
        <h3>Password</h3>
        <button className="ghost small" onClick={async () => {
          const cur = await promptDialog("Current password", { label: "Current password" }); if (!cur) return;
          const next = await promptDialog("New password", { label: "At least 10 characters" }); if (!next) return;
          try { await api.password(cur, next); toast.success("Password changed. Your other devices are signed out."); } catch (e) { toast.error(e); }
        }}>Change password…</button>
      </div>
    </>
  );
}

/**
 * Hub administrators (users.is_super): owners of every customer, not listed among any customer's members. Granting
 * needs an existing account; the hub refuses to remove the last one (409), and only hub admins see this box.
 */
// toast.error shows the server's `detail`: 404 says to invite the person first, 409 that they're the last admin
function HubAdminsBox({ me, onChanged }: { me: Me; onChanged: () => void }) {
  const [admins, setAdmins] = useState<HubAdmin[]>([]);
  const [recent, setRecent] = useState<AuditRow[]>([]);
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => {
    api.hubAdmins().then(setAdmins).catch((e) => toast.error(e));
    api.hubAudit(10).then(setRecent).catch(() => setRecent([]));
  }, []);
  useEffect(() => { load(); }, [load]);
  const add = async () => {
    setBusy(true);
    try { const a = await api.addHubAdmin(email.trim()); setEmail(""); toast.success(`${a.email} is now a hub administrator`); load(); }
    catch (e) { toast.error(e); } finally { setBusy(false); }
  };
  const remove = async (a: HubAdmin) => {
    const self = a.id === me.user.id;
    const msg = self ? "Stop being a hub administrator? You keep only the customers you're a member of." : `Remove ${a.email} as a hub administrator? They keep only the customers they're a member of.`;
    if (!(await confirmDialog(msg, { danger: true, confirmLabel: "Remove" }))) return;
    try { await api.removeHubAdmin(a.id); toast.success(`${a.email} is no longer a hub administrator`); if (self) onChanged(); else load(); }
    catch (e) { toast.error(e); }
  };
  return (
    <div className="card">
      <h3>Hub administrators</h3>
      <p className="muted small" style={{ marginTop: 0 }}>Owners of every customer on this hub: they see all Sites and manage everything, without being listed as members.</p>
      <table className="hub-table stack">
        <thead><tr><th>Email</th><th>2FA</th><th>Last sign-in</th><th /></tr></thead>
        <tbody>{admins.map((a) => (
          <tr key={a.id}><td className="lead">{a.email}{a.id === me.user.id ? <span className="muted small"> (you)</span> : null}</td><td data-label="2FA">{a.totp_enabled ? "on" : "off"}</td>
            <td data-label="Last sign-in">{a.last_login_at ? ago(a.last_login_at) : "never"}</td>
            <td className="acts"><button className="ghost small" onClick={() => remove(a)}>Remove</button></td></tr>))}
        </tbody>
      </table>
      <form className="row" style={{ marginTop: 8 }} onSubmit={(e) => { e.preventDefault(); if (email.trim()) add(); }}>
        <input type="email" placeholder="email of an existing account" value={email} onChange={(e) => setEmail(e.target.value)} />
        <button type="submit" className="small" disabled={busy || !email.trim()}>Make hub administrator</button>
      </form>
      <p className="muted small">The person must already have an account; invite them to a customer first.</p>
      {recent.length > 0 && (
        <details className="small"><summary className="muted">Recent changes</summary>
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>{recent.map((r) => <li key={r.id}>{fmtTime(r.ts)} · {r.user_email ?? "—"} · {r.action}</li>)}</ul>
        </details>
      )}
    </div>
  );
}
