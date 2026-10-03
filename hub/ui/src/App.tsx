/**
 * Hub pages: Home (dashboard), Fleet (site cards), Find, Alerts, Audit, Organisation (members, sites,
 * groups, enrolment), Account. Path routing without a router library (nav.ts).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Dialogs, Icon, OfflineBanner, Toaster, confirmDialog, promptDialog, toast } from "@site/ui";
import { ThemeToggle } from "@site/ThemeToggle";
import { ActionCard } from "./ActionCard";
import { GroupsBox } from "./Groups";
import { HomePage } from "./HomePage";
import { navigate, usePath } from "./nav";
import { type ActionPlan, type Alert, type AuditRow, type Backup, type ClaimPreview, type Digest, type Fleet, type FleetSearch, type Me, type Member, type Org, type PushInfo, type Site, type Usage, ago, api, fleetAsk, fmtTime } from "./api";

const PAGES = [["/", "Home"], ["/fleet", "Fleet"], ["/find", "Find"], ["/alerts", "Alerts"], ["/org", "Organisation"], ["/audit", "Audit"], ["/account", "Account"]] as const;
const ROLES = ["viewer", "operator", "admin", "owner"];

export default function App() {
  const path = usePath();
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const [org, setOrgState] = useState<string>(() => localStorage.getItem("hubOrg") ?? "");
  const setOrg = (id: string) => { setOrgState(id); localStorage.setItem("hubOrg", id); };
  const reload = useCallback(() => api.me().then((m) => { setMe(m); if (!m.orgs.some((o) => o.id === org)) setOrg(m.orgs[0]?.id ?? ""); }).catch(() => setMe(null)), [org]);
  useEffect(() => { reload(); }, [reload]);
  if (me === undefined) return null;
  if (!me) return <><Login onDone={reload} /><Toaster /><Dialogs /></>;
  const orgs = me.orgs;
  const current = orgs.find((o) => o.id === org) ?? orgs[0];
  const page = path.startsWith("/alerts") ? "alerts" : path.startsWith("/find") ? "find" : path.startsWith("/org") ? "org" : path.startsWith("/audit") ? "audit" : path.startsWith("/account") ? "account" : path.startsWith("/fleet") ? "fleet" : "home";
  return (
    <>
      <OfflineBanner />
      <header className="hub-top">
        <span className="brand"><img className="logo" src="/axiom.webp" alt="Axiom Vision" /></span>
        <nav>{PAGES.map(([p, label]) => <a key={p} href={p} className={(p === "/" ? page === "home" : path.startsWith(p)) ? "active" : ""} onClick={(e) => { e.preventDefault(); navigate(p); }}>{label}</a>)}</nav>
        <span className="spacer" />
        {orgs.length > 1 && <select value={current?.id ?? ""} onChange={(e) => setOrg(e.target.value)}>{orgs.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}</select>}
        <span className="muted small">{me.user.email}</span>
        <ThemeToggle />
        <button className="ghost small" onClick={async () => { await api.logout(); setMe(null); }}>Sign out</button>
      </header>
      <main className="hub-page">
        {page === "home" && current && <HomePage org={current} me={me} />}
        {page === "fleet" && <FleetPage org={current} me={me} />}
        {page === "alerts" && current && <AlertsPage org={current} />}
        {page === "find" && current && <FindPage org={current} />}
        {page === "org" && current && <OrgPage org={current} me={me} onChanged={reload} />}
        {page === "audit" && current && <AuditPage org={current} />}
        {page === "account" && <AccountPage me={me} onChanged={reload} />}
        {!current && page !== "account" && <p className="muted">You're not a member of any organisation yet. {me.user.is_super ? "Create one under Organisation." : "Ask an owner to add you."}</p>}
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

// ---------------------------------------------------------------- fleet

function FleetPage({ org, me }: { org: Org | undefined; me: Me }) {
  const [fleet, setFleet] = useState<Fleet | null>(null);
  const [showRetired, setShowRetired] = useState(false);
  const load = useCallback(() => api.fleet(org?.id, showRetired).then(setFleet).catch((e) => toast.error(e)), [org?.id, showRetired]);
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);
  if (!fleet) return null;
  const groups = me.user.is_super && !org ? fleet.orgs : fleet.orgs.filter((g) => !org || g.org.id === org.id);
  return (
    <>
      {groups.map((g) => (
        <section key={g.org.id}>
          <h2>{g.org.name} <span className="muted small">{g.sites.filter((s) => s.online && !s.retired_at).length} of {g.sites.filter((s) => !s.retired_at).length} online{g.open_alerts ? ` · ${g.open_alerts} open alerts` : ""}</span>
            {(g.retired ?? 0) > 0 && <label className="small muted" style={{ marginLeft: 12, fontWeight: 400 }}><input type="checkbox" checked={showRetired} onChange={(e) => setShowRetired(e.target.checked)} /> Show retired ({g.retired})</label>}</h2>
          {g.sites.length === 0 && <p className="muted">No sites yet. Enrol one under Organisation → Add site.</p>}
          <div className="site-grid">{g.sites.map((s) => <SiteCard key={s.id} s={s} now={fleet.now} />)}</div>
          <DigestCard org={g.org} />
        </section>
      ))}
    </>
  );
}

function SiteCard({ s, now }: { s: Site; now: number }) {
  const sm = s.summary ?? {};
  const cams = sm.cameras ?? [];
  const bad = cams.filter((c) => !c.stream_ready || c.problems?.length);
  const today = Object.entries(sm.today ?? {}).map(([k, v]) => `${v} ${k}`).join(" · ");
  const diskDays = sm.disk && sm.bitrate_mbps ? Math.round(sm.disk.free_gb / ((sm.bitrate_mbps * 86400) / 8 / 1000)) : null;
  return (
    <a className={`site-card ${s.online ? "" : "offline"} ${s.retired_at ? "retired" : ""}`} href={`/s/${s.id}/`}>
      <div className="head">
        <span className={`dot ${s.online ? "ok" : "bad"}`} title={s.online ? "Online" : `Offline · last seen ${ago(s.last_seen_at, now)}`} />
        <strong>{s.name}</strong>{s.retired_at ? <span className="alert-kind">retired</span> : null}
        <span className="spacer" />
        <span className="muted small">{s.online ? "online" : `offline · ${ago(s.last_seen_at, now)}`}</span>
      </div>
      {s.location && <div className="loc">{s.location}</div>}
      <div className="stats">
        <div><span>Cameras</span> {cams.length - bad.length}/{cams.length} up</div>
        <div><span>Today</span> {today || "—"}</div>
        <div><span>Disk</span> {sm.disk ? `${sm.disk.free_gb.toLocaleString()} GB free${diskDays != null && isFinite(diskDays) ? ` · ~${diskDays} d` : ""}` : "—"}</div>
        <div><span>AI</span> {sm.yolo_ready ? "YOLO ✓" : "YOLO …"} · {sm.vlm_ready ? "Qwen ✓" : "Qwen …"}{sm.queues?.synopsis ? ` (${sm.queues.synopsis} waiting)` : ""}</div>
        <div><span>Stream</span> {sm.bitrate_mbps != null ? `${sm.bitrate_mbps} Mbps` : "—"}</div>
        <div><span>Version</span> {s.version ?? "—"}{s.clock_skew_s != null && Math.abs(s.clock_skew_s) > 30 ? ` · clock ${s.clock_skew_s > 0 ? "+" : ""}${Math.round(s.clock_skew_s)} s` : ""}</div>
      </div>
      {cams.length > 0 && (
        <div className="cams">{cams.map((c) => <span key={c.id} className={`cam ${!c.stream_ready || c.problems?.length ? "bad" : ""}`} title={(c.problems ?? []).join("; ") || (c.stream_ready ? "streaming" : "no stream")}>
          {c.name}{c.ptz && !c.ptz.at_home ? " ↗" : ""}</span>)}</div>
      )}
      {s.open_alerts > 0 && <div className="alerts">⚠ {s.open_alerts} open alert{s.open_alerts > 1 ? "s" : ""}</div>}
    </a>
  );
}

function DigestCard({ org }: { org: Org }) {
  const [rows, setRows] = useState<Digest[]>([]);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => api.digests(org.id).then(setRows).catch(() => setRows([])), [org.id]);
  useEffect(() => { load(); }, [load]);
  const d = rows[0];
  return (
    <div className="card" style={{ marginTop: 12 }}>
      <div className="row"><h3 style={{ margin: 0 }}>Digest</h3><span className="muted small">{d ? `${d.day}${d.model ? ` · ${d.model}` : ""}` : "none yet"}</span><span className="spacer" />
        <button className="ghost small" disabled={busy} onClick={async () => { setBusy(true); try { await api.digestNow(org.id); await load(); } catch (e) { toast.error(e); } finally { setBusy(false); } }}>Generate now</button></div>
      {d && <pre style={{ whiteSpace: "pre-wrap", margin: "8px 0 0", font: "inherit" }}>{d.text}</pre>}
    </div>
  );
}

// ---------------------------------------------------------------- find / ask across sites

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
    // an instruction ("Migrate Ironsight to Hailo T1") gets a confirmation card instead of going to the sites;
    // if the planner fails the question is simply asked as before
    const plan = await api.actionPlan(org.id, q.trim()).catch(() => null);
    if (plan && plan.action !== "none") { setAction(plan); setAsking(false); return; }
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
        <input style={{ flex: 1, minWidth: 260 }} value={q} onChange={(e) => setQ(e.target.value)} placeholder='Search every site: "white pickup truck", "person at the back door last night"…' />
        <button type="submit" disabled={busy || !q.trim()}>Search</button>
        <button type="button" className="ghost" disabled={asking || !q.trim()} onClick={ask} title="Every site's assistant answers from its own footage. Instructions such as &quot;Migrate Ironsight to Hailo T1&quot; show a confirmation card instead">✦ Ask all sites</button>
      </form>
      {action && <ActionCard key={action.id} org={org.id} plan={action} onClose={() => setAction(null)} />}
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
              <a className="small" href={`/s/${id}/#find`}>Open this site's Find →</a>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------- alerts

const KIND_LABEL: Record<string, string> = { offline: "Site offline", camera_down: "Camera down", disk: "Disk", clock: "Clock skew", event_high: "High priority", event_policy: "Site rule", event_watched: "Watch list" };

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
          <thead><tr><th>When</th><th>Site</th><th>Kind</th><th>What</th><th /></tr></thead>
          <tbody>
            {rows.map((a) => {
              const d = a.detail as Record<string, unknown>;
              const what = a.kind === "camera_down" ? `${d.name ?? a.key}: ${(d.problems as string[] | undefined)?.join("; ") || "no stream"}`
                : a.kind === "clock" ? `${d.skew_s} s off` : a.kind === "disk" ? String(d.message ?? "low space")
                : a.kind === "offline" ? `last seen ${ago(d.last_seen_at as number)}` : `${d.name ? `${d.name} · ` : ""}${d.text ?? d.synopsis ?? ""}`;
              return (
                <tr key={a.id} className={a.closed_at ? "muted" : ""}>
                  <td>{fmtTime(a.opened_at)}</td>
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

// ---------------------------------------------------------------- organisation

function OrgPage({ org, me, onChanged }: { org: Org; me: Me; onChanged: () => void }) {
  const admin = ["admin", "owner"].includes(org.role) || me.user.is_super;
  const [sites, setSites] = useState<Site[]>([]);
  const [members, setMembers] = useState<Member[]>([]);
  const [fleet, setFleet] = useState<Fleet | null>(null);
  const load = useCallback(() => {
    api.sites(org.id).then(setSites).catch((e) => toast.error(e));
    api.fleet(org.id).then(setFleet).catch(() => {});
    if (admin) api.members(org.id).then(setMembers).catch(() => {});
  }, [org.id, admin]);
  useEffect(() => { load(); }, [load]);
  return (
    <>
      <h2>{org.name} <span className="muted small">your role: {org.role}</span></h2>
      {admin && <ClaimBox org={org} onDone={load} />}
      <div className="card">
        <h3>Sites</h3>
        {sites.length === 0 && <p className="muted">None yet.</p>}
        {sites.length > 0 && (
          <table className="hub-table">
            <thead><tr><th>Site</th><th>Location</th><th>Status</th><th>Host</th><th>Version</th><th /></tr></thead>
            <tbody>{sites.map((s) => (
              <tr key={s.id}>
                <td><a href={`/s/${s.id}/`}>{s.name}</a> <span className="muted small">{s.id}</span></td>
                <td>{s.location}</td>
                <td>{s.online ? "online" : `offline · ${ago(s.last_seen_at)}`}{s.retired_at ? <> · <span className="alert-kind">retired</span></> : null}</td>
                <td className="muted small">{s.hostname}</td>
                <td>{s.version}</td>
                <td className="row">
                  {admin && <button className="ghost small" onClick={async () => { const name = await promptDialog("Rename site", { initial: s.name, label: "Name" }); if (name?.trim()) { await api.updateSite(s.id, { name: name.trim() }); load(); } }}>Rename</button>}
                  {admin && <button className="ghost small" onClick={async () => { const loc = await promptDialog("Location", { initial: s.location, label: "Location" }); if (loc != null) { await api.updateSite(s.id, { location: loc.trim() }); load(); } }}>Location</button>}
                  {admin && <button className="ghost small" title="Issue a new device token (the old one stops working after 10 minutes)" onClick={async () => { if (await confirmDialog(`Rotate ${s.name}'s token?`)) { await api.rotateSite(s.id); toast.success("New token sent to the site"); } }}>Rotate token</button>}
                  {admin && <BackupsButton site={s} />}
                  {admin && <button className="ghost small" title={s.retired_at ? "Show it in Fleet, Home, Find and alerts again" : "Hide it from Fleet, Home, Find and alerts (the site keeps running)"}
                    onClick={async () => { if (s.retired_at || await confirmDialog(`Retire ${s.name}?`, { message: "It disappears from Fleet, Home, Find, Ask and alerts. The site, its tunnel and its recordings are untouched; you can restore it here.", confirmLabel: "Retire" })) { await api.retireSite(s.id, !s.retired_at); load(); } }}>{s.retired_at ? "Restore" : "Retire"}</button>}
                  {admin && <button className="ghost small" onClick={async () => { if (await confirmDialog(`Remove ${s.name}?`, { message: "The site is told to unenrol; recordings stay at the site.", confirmLabel: "Remove", danger: true })) { await api.removeSite(s.id); load(); } }}>Remove</button>}
                </td>
              </tr>))}
            </tbody>
          </table>
        )}
      </div>
      {admin && <MembersBox org={org} members={members} sites={sites} onChanged={() => { load(); onChanged(); }} />}
      <GroupsBox org={org} fleet={fleet} canEdit={admin} />
      <SharedAiBox org={org} canEdit={org.role === "owner" || me.user.is_super} />
      {me.user.is_super && <CreateOrgBox onDone={onChanged} />}
    </>
  );
}

function BackupsButton({ site }: { site: Site }) {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<Backup[]>([]);
  const load = () => api.backups(site.id).then(setRows).catch((e) => toast.error(e));
  return (
    <>
      <button className="ghost small" onClick={() => { setOpen(true); load(); }}>Backups</button>
      {open && (
        <div className="modal-backdrop" onClick={() => setOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 640 }}>
            <header className="modal-head"><h2>{site.name} · configuration backups</h2><button className="ghost" onClick={() => setOpen(false)} aria-label="Close">✕</button></header>
            <p className="muted small">Cameras, zones, places, rules, PTZ, neighbours, named people/vehicles, layouts, retention and briefing settings — taken nightly, 30 kept. Camera passwords are not included. Recordings and events stay at the site.</p>
            <div className="row"><button className="ghost small" onClick={async () => { try { await api.backupNow(site.id); toast.success("Backup taken"); load(); } catch (e) { toast.error(e); } }}>Back up now</button></div>
            <table className="hub-table">
              <thead><tr><th>When</th><th>Size</th><th>Cameras</th><th>Identities</th><th>Version</th><th /></tr></thead>
              <tbody>{rows.map((b) => (
                <tr key={b.id}><td>{fmtTime(b.created_at)}</td><td>{(b.bytes / 1024).toFixed(0)} KB</td><td>{b.cameras}</td><td>{b.identities}</td><td>{b.site_version}</td>
                  <td className="row">
                    <a className="small" href={`/api/sites/${site.id}/backups/${b.id}`}>Download</a>
                    <button className="ghost small" onClick={async () => {
                      if (!await confirmDialog(`Restore ${site.name} from ${fmtTime(b.created_at)}?`, { message: "Cameras, zones, rules, topology and layouts on the site are replaced; named people/vehicles are merged by name. Camera passwords must already be set on the site.", confirmLabel: "Restore", danger: true })) return;
                      try { const r = await api.restore(site.id, b.id); toast.success(`Restored: ${Object.entries(r).map(([k, v]) => `${v} ${k}`).join(", ")}`); } catch (e) { toast.error(e); }
                    }}>Restore</button>
                  </td></tr>))}
              </tbody>
            </table>
            {rows.length === 0 && <p className="muted">No backups yet.</p>}
          </div>
        </div>
      )}
    </>
  );
}

function ClaimBox({ org, onDone }: { org: Org; onDone: () => void }) {
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [location, setLocation] = useState("");
  const [preview, setPreview] = useState<ClaimPreview | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const c = code.trim().toUpperCase();
    if (c.replace("-", "").length < 8) { setPreview(null); return; }
    api.claimPreview(c).then((p) => { setPreview(p); if (!name && p.hint?.hostname) setName(p.hint.hostname); }).catch(() => setPreview(null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code]);
  return (
    <div className="card">
      <h3>Add site</h3>
      <p className="muted small">At the site open Settings → System → Cloud hub and type its claim code here. Nothing is port-forwarded: the site is already connected to this hub, waiting.</p>
      <div className="claim-box">
        <label className="field"><span>Claim code</span><input value={code} placeholder="ABCD-EFGH" onChange={(e) => setCode(e.target.value)} style={{ width: 130 }} /></label>
        <label className="field"><span>Site name</span><input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Austin HQ" /></label>
        <label className="field"><span>Location</span><input value={location} onChange={(e) => setLocation(e.target.value)} placeholder="address or city" /></label>
        <button disabled={busy || !preview?.waiting || !name.trim()} onClick={async () => {
          setBusy(true);
          try { await api.claim(org.id, { code: code.trim().toUpperCase(), name: name.trim(), location: location.trim() }); toast.success(`${name.trim()} enrolled`); setCode(""); setName(""); setLocation(""); setPreview(null); onDone(); }
          catch (e) { toast.error(e); } finally { setBusy(false); }
        }}>Enrol site</button>
      </div>
      {preview && (
        <div className="hint-box">
          {preview.waiting ? "✓ A site is waiting with this code" : "That site isn't connected right now (it retries within a minute)"} · {preview.hint?.hostname ?? "unknown host"} · v{preview.hint?.version ?? "?"} · {preview.hint?.cameras?.length ?? 0} cameras
          {preview.hint?.cameras?.length ? `: ${preview.hint.cameras.map((c) => c.name).join(", ")}` : ""}{preview.agent_ip ? ` · from ${preview.agent_ip}` : ""}
        </div>
      )}
    </div>
  );
}

function MembersBox({ org, members, sites, onChanged }: { org: Org; members: Member[]; sites: Site[]; onChanged: () => void }) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("viewer");
  const [password, setPassword] = useState("");
  return (
    <div className="card">
      <h3>Members</h3>
      <table className="hub-table">
        <thead><tr><th>Email</th><th>Role</th><th>Sites</th><th>2FA</th><th>Last sign-in</th><th /></tr></thead>
        <tbody>{members.map((m) => (
          <tr key={m.id}>
            <td>{m.email}</td>
            <td><select value={m.role} onChange={async (e) => { await api.addMember(org.id, { email: m.email, role: e.target.value }); onChanged(); }}>{ROLES.map((r) => <option key={r} value={r}>{r}</option>)}</select></td>
            <td>
              <select value={m.sites.length ? "some" : "all"} onChange={async (e) => { if (e.target.value === "all") { await api.setGrants(org.id, m.id, []); onChanged(); } }}>
                <option value="all">all sites</option><option value="some">only: {m.sites.map((id) => sites.find((s) => s.id === id)?.name ?? id).join(", ") || "choose…"}</option>
              </select>
              {sites.map((s) => <label key={s.id} className="small" style={{ marginLeft: 6 }}><input type="checkbox" checked={m.sites.includes(s.id)} onChange={async (e) => {
                const next = e.target.checked ? [...m.sites, s.id] : m.sites.filter((x) => x !== s.id); await api.setGrants(org.id, m.id, next); onChanged(); }} /> {s.name}</label>)}
            </td>
            <td>{m.totp_enabled ? "on" : "off"}</td>
            <td>{m.last_login_at ? ago(m.last_login_at) : "never"}</td>
            <td><button className="ghost small" onClick={async () => { if (await confirmDialog(`Remove ${m.email} from ${org.name}?`, { danger: true, confirmLabel: "Remove" })) { await api.removeMember(org.id, m.id); onChanged(); } }}>Remove</button></td>
          </tr>))}
        </tbody>
      </table>
      <h4>Add a member</h4>
      <div className="row">
        <input type="email" placeholder="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        <select value={role} onChange={(e) => setRole(e.target.value)}>{ROLES.map((r) => <option key={r} value={r}>{r}</option>)}</select>
        <input type="password" placeholder="initial password (new users only)" value={password} onChange={(e) => setPassword(e.target.value)} />
        <button disabled={!email} onClick={async () => { try { await api.addMember(org.id, { email, role, password: password || undefined }); setEmail(""); setPassword(""); onChanged(); toast.success("Member added"); } catch (e) { toast.error(e); } }}>Add</button>
      </div>
      <p className="muted small">viewer: watch and search · operator: + PTZ, relay, naming, feedback, Ask · admin: + camera settings, rules, retention, members · owner: everything.</p>
    </div>
  );
}

function SharedAiBox({ org, canEdit }: { org: Org; canEdit: boolean }) {
  const [u, setU] = useState<Usage | null>(null);
  const load = useCallback(() => api.usage(org.id).then(setU).catch(() => setU(null)), [org.id]);
  useEffect(() => { load(); }, [load]);
  if (!u) return null;
  return (
    <div className="card">
      <h3>Shared AI &amp; relay</h3>
      <p className="muted small">
        Live video relay (TURN): {u.turn ? "configured" : "not configured on this hub — remote live view needs it"}.
        Shared model: {u.configured ? u.model : "not configured on this hub"}
        {u.configured && u.provider.kind === "site" && <> — served by the GPU at <b>{u.provider.site_name || u.provider.site_id}</b> through its tunnel ({u.provider.online ? "online" : "offline: sites fall back to their local model"})</>}
        {u.configured && u.provider.kind === "url" && <> — served by the hub's own model server</>}.
      </p>
      <label className="row small">
        <input type="checkbox" checked={u.ai_shared} disabled={!canEdit || !u.configured} onChange={async (e) => { try { await api.patchOrg(org.id, { ai_shared: e.target.checked }); load(); toast.success(e.target.checked ? "Sites now use the hub's model" : "Sites are back on their local model"); } catch (err) { toast.error(err); } }} />
        Sites in this organisation use the hub's model (their local one stays as a fallback)
      </label>
      {u.sites.length > 0 && (
        <table className="hub-table">
          <thead><tr><th>Site</th><th>Requests ({u.days} d)</th><th>Prompt tokens</th><th>Output tokens</th><th>Avg latency</th><th>Errors</th></tr></thead>
          <tbody>{u.sites.map((s) => <tr key={s.site_id}><td>{s.site_name}</td><td>{s.requests}</td><td>{s.prompt_tokens.toLocaleString()}</td><td>{s.completion_tokens.toLocaleString()}</td><td>{s.latency_ms} ms</td><td>{s.errors}</td></tr>)}</tbody>
        </table>
      )}
    </div>
  );
}

function CreateOrgBox({ onDone }: { onDone: () => void }) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  return (
    <div className="card">
      <h3>New organisation <span className="muted small">(hub administrator)</span></h3>
      <div className="row">
        <input placeholder="Customer name" value={name} onChange={(e) => { setName(e.target.value); setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "")); }} />
        <input placeholder="slug" value={slug} onChange={(e) => setSlug(e.target.value)} />
        <button disabled={!name || !slug} onClick={async () => { try { await api.createOrg(name, slug); setName(""); setSlug(""); onDone(); toast.success("Organisation created"); } catch (e) { toast.error(e); } }}>Create</button>
      </div>
    </div>
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
  const LABEL: Record<string, string> = { offline: "Site offline", camera_down: "Camera down", disk: "Disk low", clock: "Clock skew", event_high: "High-priority event", event_policy: "Site rule broken", event_watched: "Watched person/vehicle" };
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
  useEffect(() => { api.audit(org.id).then(setRows).catch((e) => toast.error(e)); }, [org.id]);
  return (
    <>
      <h2>Audit <span className="muted small">{org.name} · who did what through the hub</span></h2>
      <table className="hub-table">
        <thead><tr><th>When</th><th>Who</th><th>Site</th><th>Action</th><th>Result</th><th>From</th></tr></thead>
        <tbody>{rows.map((r) => <tr key={r.id}><td>{fmtTime(r.ts)}</td><td>{r.user_email ?? "—"}</td><td className="muted small">{r.site_id ?? ""}</td><td>{r.action}</td><td>{r.status ?? ""}</td><td className="muted small">{r.ip ?? ""}</td></tr>)}</tbody>
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
