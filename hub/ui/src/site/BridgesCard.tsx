/**
 * Site › Servers › Axiom Bridge (hub bridges.py): the small program on a PC at the Site that lets the Site's central
 * recording instance reach cameras with no way in (Starlink, cellular). Everyone who sees the Site sees its bridges and
 * bridged cameras read-only (online, streams, the last errors). The Site's admins and hub administrators also enroll a
 * bridge (a one-time code and the command to run on the PC), rename, rotate or retire it, and map its cameras to the
 * Site's central instance ("Bridged cameras": Test, Apply to camera, the settings sheet, Remove). Allocating the instance
 * stays on the Hosts page (hub administrators). The pure parts are bridges.ts.
 */
import { useCallback, useEffect, useState } from "react";
import { confirmDialog, promptDialog, toast } from "@site/ui";
import { type BridgeTarget, type NewBridgeCode, type Site, type SiteBridge, type SiteBridges, ago, api, fmtTime } from "../api";
import {
  type BridgeRelease, bridgeStatus, fmtBytes, installCommand, mapFormError, oneLineInstall, parsePorts, portsText, probeText,
  relaySheet, sheetText, targetErrorText,
} from "./bridges";

const copy = (t: string) => navigator.clipboard?.writeText(t).then(() => toast.success("Copied"), () => toast.error("Copy failed: select and copy it by hand"));

export function BridgesCard({ site }: { site: Site }) {
  const [data, setData] = useState<SiteBridges | null>(null);
  const [cameras, setCameras] = useState<{ id: string; name: string }[]>([]);
  const [issued, setIssued] = useState<NewBridgeCode | null>(null);
  const [mapping, setMapping] = useState<SiteBridge | null>(null);
  const [sheet, setSheet] = useState<BridgeTarget | null>(null);
  const load = useCallback(() => api.siteBridges(site.id).then(setData).catch(() => setData(null)), [site.id]);
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);
  const admin = !!data?.can_manage;
  // the instance's cameras (Apply to camera's choice): its server's cameras as the hub's registry lists them
  useEffect(() => {
    if (!admin) return;
    api.locationCentral(site.id).then((c) => setCameras(c.instances.find((i) => i.id === data?.instance?.id)?.cameras ?? [])).catch(() => setCameras([]));
  }, [admin, site.id, data?.instance?.id]);
  if (!data || (!data.bridges.length && !admin)) return null;
  const loc = site.id;
  const act = (f: () => Promise<unknown>, done?: string) => async () => { try { await f(); if (done) toast.success(done); load(); } catch (e) { toast.error(e); } };
  const newCode = async () => {
    const name = await promptDialog("Enroll an Axiom Bridge", { label: "The PC's name (blank = its computer name)", confirmLabel: "Make a code",
      message: "A one-time code, valid for 24 hours, and the command that installs the bridge on a Windows PC at this Site." });
    if (name == null) return;
    try { setIssued(await api.newBridgeCode(loc, name.trim() || null)); load(); } catch (e) { toast.error(e); }
  };
  const inst = data.instance;
  return (
    <div className="card bridges-card">
      <div className="row"><h3 style={{ margin: 0 }}>Axiom Bridge</h3><span className="spacer" />
        {admin && <button className="small" onClick={newCode}>Enroll a bridge…</button>}</div>
      <p className="muted small">A PC at the Site dials the hub and carries the central recording instance's connections to the cameras it is given: no port forwarding, no static IP.</p>
      {issued && <CodeBox code={issued} hubUrl={data.hub_url ?? issued.hub_url} onClose={() => setIssued(null)} />}
      {admin && inst === null && <p className="small bad">This Site has no central recording instance yet: bridged cameras need one (ask Axiom Vision).</p>}
      {admin && inst && !inst.relay_connected && <p className="small bad">The instance's host relay is not connected to the hub: bridged cameras cannot be reached until it is.</p>}
      {data.bridges.length === 0 ? <p className="muted small">No bridge enrolled yet.</p> : (
        <div className="bridge-list">{data.bridges.map((b) => (
          <div key={b.id} className={`bridge-row ${b.online ? "" : "offline"}`}>
            <div className="row">
              <span className={`dot ${b.online ? "ok" : "bad"}`} title={b.online ? "Online" : b.last_seen_at ? `Offline · last seen ${ago(b.last_seen_at)}` : "Never connected"} />
              <strong>{b.name}</strong>
              <span className="muted small">{bridgeStatus(b)}</span>
              <span className="spacer" />
              {admin && <>
                <button className="ghost small" disabled={!inst} title={inst ? "Relay a camera behind this bridge to the central instance" : "No central instance"} onClick={() => setMapping(b)}>Add a camera…</button>
                <button className="ghost small" onClick={async () => {
                  const n = (await promptDialog("Rename bridge", { initial: b.name, confirmLabel: "Rename" }))?.trim();
                  if (n) await act(() => api.renameBridge(loc, b.id, n))();
                }}>Rename</button>
                <button className="ghost small" disabled={!b.online} title={b.online ? "A new device token, sent to the bridge" : "The bridge is offline"}
                  onClick={act(() => api.rotateBridge(loc, b.id), `${b.name}: token rotated`)}>Rotate token</button>
                <button className="ghost small" onClick={async () => {
                  if (await confirmDialog(`Retire ${b.name}?`, { message: "Its token stops working, its cameras are unmapped and stop recording through it. To use the PC again, enroll it with a new code.", confirmLabel: "Retire", danger: true }))
                    await act(() => api.retireBridge(loc, b.id), `${b.name} retired`)();
                }}>Retire…</button>
              </>}
            </div>
            <div className="muted small">
              {[b.hostname, b.version ? `version ${b.version}` : null, b.os ? `${b.os}${b.arch ? `/${b.arch}` : ""}` : null,
                b.bytes_down != null || b.bytes_up != null ? `${fmtBytes(b.bytes_down)} from cameras · ${fmtBytes(b.bytes_up)} to them` : null,
                admin && b.lan?.length ? `LAN ${b.lan.map((l) => l.addr).join(", ")}` : null].filter(Boolean).join(" · ")}
            </div>
          </div>))}
        </div>
      )}
      {admin && (data.codes ?? []).length > 0 && (
        <div className="small">
          <strong>Unused enrollment codes</strong>
          {(data.codes ?? []).map((c) => (
            <div key={c.id} className="row">
              <span>{c.name ?? "(the PC's name)"}</span><span className="muted">made {ago(c.created_at)}{c.created_by_email ? ` by ${c.created_by_email}` : ""} · valid until {fmtTime(c.expires_at)}</span>
              <span className="spacer" />
              <button className="ghost small" onClick={act(() => api.revokeBridgeCode(loc, c.id), "Code revoked")}>Revoke</button>
            </div>))}
        </div>
      )}
      {data.targets.length > 0 && (
        <>
          <h4>Bridged cameras{inst ? <span className="muted small"> → {inst.name}</span> : null}</h4>
          <table className="hub-table small stack">
            <thead><tr><th>Camera</th><th>Bridge</th>{admin && <th>LAN address</th>}<th>Ports</th><th>Status</th>{admin && <th />}</tr></thead>
            <tbody>{data.targets.map((t) => (
              <TargetRow key={t.id} t={t} bridge={data.bridges.find((b) => b.id === t.bridge_id)} admin={admin} loc={loc} cameras={cameras}
                gateway={inst?.gateway ?? null} onChanged={load} onSheet={() => setSheet(t)} />))}
            </tbody>
          </table>
        </>
      )}
      {mapping && <MapDialog loc={loc} bridge={mapping} cameras={cameras} onClose={() => setMapping(null)} onDone={() => { setMapping(null); load(); }} />}
      {sheet && <SheetDialog t={sheet} gateway={inst?.gateway ?? null} onClose={() => setSheet(null)} />}
    </div>
  );
}

function TargetRow({ t, bridge, admin, loc, cameras, gateway, onChanged, onSheet }: {
  t: BridgeTarget; bridge: SiteBridge | undefined; admin: boolean; loc: string; cameras: { id: string; name: string }[]; gateway: string | null;
  onChanged: () => void; onSheet: () => void;
}) {
  const [cam, setCam] = useState(t.camera_id ?? "");
  const [busy, setBusy] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const run = (what: string, f: () => Promise<void>) => async () => { setBusy(what); try { await f(); } catch (e) { toast.error(e); } finally { setBusy(null); } };
  const errs = targetErrorText(t);
  const probe = probeText(t.probe);
  const test = run("test", async () => {
    const r = await api.testBridgeCamera(loc, t.bridge_id, t.id);
    const bad = r.ports.filter((p) => !p.ok);
    if (bad.length) toast.error(`${t.name}: ${probeText(r)}`); else toast.success(`${t.name}: ${probeText(r)}`);
    onChanged();
  });
  const apply = run("apply", async () => {
    const name = cameras.find((c) => c.id === cam)?.name ?? cam;
    if (!(await confirmDialog(`Point ${name} at the bridge?`, {
      message: `The instance's camera ${name} is reached through ${bridge?.name ?? "the bridge"} from now on: its outside address becomes the relay (${gateway ?? "the instance's gateway"}) with the ports below. Its password and other settings stay as they are.`,
      confirmLabel: "Apply" }))) return;
    const r = await api.applyBridgeCamera(loc, t.bridge_id, t.id, cam || null);
    toast.success(`${name}: ${r.public_host} RTSP ${r.public_rtsp_port} · ONVIF ${r.public_onvif_port}${r.public_replay_port ? ` · SD ${r.public_replay_port}` : ""}`);
    onChanged();
  });
  const remove = run("remove", async () => {
    if (!(await confirmDialog(`Unmap ${t.name}?`, { message: "The instance stops reaching this camera through the bridge (its recording stops until it is mapped again).", confirmLabel: "Unmap", danger: true }))) return;
    const r = await api.unmapBridgeCamera(loc, t.bridge_id, t.id);
    toast.success(r.relay_pending ? `${t.name} unmapped (the host is told when it is back)` : `${t.name} unmapped`);
    onChanged();
  });
  return (
    <tr className={t.enabled ? "" : "muted"}>
      <td className="lead">{t.name}{!t.enabled && " (off)"}{t.applied_at ? <span className="muted"> · applied {ago(t.applied_at)}</span> : null}</td>
      <td data-label="Bridge">{bridge?.name ?? "—"}{bridge && !bridge.online ? <span className="bad"> (offline)</span> : null}</td>
      {admin && <td data-label="LAN address"><code>{t.host}</code></td>}
      <td data-label="Ports">{portsText(t.ports)}</td>
      <td data-label="Status" className={errs ? "bad" : ""}>{errs ?? (probe ? <span title={t.probe ? `Tested ${fmtTime(t.probe.at)}` : undefined}>{probe}</span> : <span className="muted">—</span>)}</td>
      {admin && (
        <td><div className="central-row-actions">
          <button className="ghost small" disabled={!bridge?.online || !!busy} title={bridge?.online ? "The bridge connects to each port once" : "The bridge is offline"} onClick={test}>{busy === "test" ? "Testing…" : "Test"}</button>
          {!t.camera_id && <button className="small" disabled={!!busy} title="Add this camera to the central recording (its username and password go to the instance only)" onClick={() => setAdding(true)}>Add to recording…</button>}
          {(t.camera_id || cameras.length > 0) && <>
            <select className="small" value={cam} onChange={(e) => setCam(e.target.value)} aria-label="The instance's camera">
              <option value="">Instance camera…</option>
              {cameras.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              {cam && !cameras.some((c) => c.id === cam) && <option value={cam}>{cam}</option>}
            </select>
            <button className="ghost small" disabled={!cam || !!busy} title="Write the relay address and ports into an existing camera of the instance" onClick={apply}>{busy === "apply" ? "Applying…" : "Apply to camera"}</button>
          </>}
          <button className="ghost small" onClick={onSheet}>Settings…</button>
          <button className="ghost small" disabled={!!busy} onClick={remove}>Unmap…</button>
        </div>
        {adding && <AddLoginDialog loc={loc} t={t} onClose={() => setAdding(false)} onDone={() => { setAdding(false); onChanged(); }} />}</td>
      )}
    </tr>
  );
}

/** Add an already mapped camera to the central recording: its login, passed through to the instance. */
function AddLoginDialog({ loc, t, onClose, onDone }: { loc: string; t: BridgeTarget; onClose: () => void; onDone: () => void }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const r = await api.addBridgeCameraToRecording(loc, t.bridge_id, t.id, { username: username.trim() || "admin", password });
      toast.success(`${t.name} added to recording (camera ${r.camera_id})`);
      onDone();
    } catch (er) { toast.error(er); } finally { setBusy(false); }
  };
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form className="modal central-dialog central-form" onClick={(e) => e.stopPropagation()} onSubmit={submit} style={{ maxWidth: 420 }}>
        <header className="modal-head"><h2>Add {t.name} to recording</h2><button type="button" className="ghost" onClick={onClose} aria-label="Close">✕</button></header>
        <p className="muted small">{t.host}, through the bridge. The password goes straight to the recording instance; the hub doesn't keep it.</p>
        <label className="field"><span>Username</span><input value={username} autoComplete="off" onChange={(e) => setUsername(e.target.value)} /></label>
        <label className="field"><span>Password</span><input type="password" value={password} autoComplete="new-password" autoFocus onChange={(e) => setPassword(e.target.value)} /></label>
        <div className="row">
          <button type="submit" disabled={busy || !password}>{busy ? "Adding…" : "Add camera"}</button>
          <button type="button" className="ghost" onClick={onClose}>Cancel</button>
        </div>
      </form>
    </div>
  );
}

/** The one-time code, shown once, with the command to run on the PC. */
function CodeBox({ code, hubUrl, onClose }: { code: NewBridgeCode; hubUrl: string; onClose: () => void }) {
  const cmd = installCommand(hubUrl, code.code);
  const [release, setRelease] = useState<BridgeRelease | null | undefined>(undefined);
  useEffect(() => { api.bridgeRelease().then(setRelease); }, []);
  const oneLine = release === undefined ? null : oneLineInstall(location.origin, hubUrl, code.code, release);
  return (
    <div className="card token-box">
      <div className="row"><h4 style={{ margin: 0 }}>Enrollment code{code.name ? ` for ${code.name}` : ""}</h4><span className="spacer" /><button className="ghost small" onClick={onClose}>Done</button></div>
      <p className="small">Shown only now: the hub keeps a hash. It enrolls one bridge, until {fmtTime(code.expires_at)}.</p>
      <ol className="small">
        <li>Code: <code className="token">{code.code}</code> <button className="ghost small" onClick={() => copy(code.code)}>Copy</button></li>
        {oneLine ? (
          <li>On the Site's PC (Windows 10 or later, always on, on the cameras' LAN), open PowerShell <b>as administrator</b> and paste this one line: it downloads Axiom Bridge {release?.version} from this hub, checks its SHA-256, installs the service and enrolls it.<br />
            <code className="wrap-code">{oneLine}</code> <button className="ghost small" onClick={() => copy(oneLine)}>Copy</button>
            <details className="small"><summary>Or with the exe copied over by hand</summary>
              In PowerShell opened as administrator in its folder: <code>{cmd}</code> <button className="ghost small" onClick={() => copy(cmd)}>Copy</button></details></li>
        ) : (
          <li>On the Site's PC (Windows 10 or later, always on, on the cameras' LAN), copy <code>axiom-bridge.exe</code> to a folder and run, in PowerShell opened as administrator in that folder:<br />
            <code>{cmd}</code> <button className="ghost small" onClick={() => copy(cmd)}>Copy</button></li>
        )}
        <li>It installs the AxiomBridge service and appears here within a minute. Then add its cameras (Add a camera…): name, LAN address and login, and the hub does the rest.</li>
      </ol>
    </div>
  );
}

/**
 * Add a camera behind the bridge in one step (Shawn, 2026-10-18): its name, LAN address and login. The hub maps it
 * (relay ports on the instance's host, the bridge told) and adds it to the instance's recording with the relay address
 * filled in. Unticked, it only maps it (for a camera added on the instance's console, then Apply to camera).
 */
function MapDialog({ loc, bridge, onClose, onDone }: { loc: string; bridge: SiteBridge; cameras: { id: string; name: string }[]; onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState("");
  const [host, setHost] = useState("");
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [ports, setPorts] = useState("554, 80, 555");
  const [record, setRecord] = useState(true);
  const [busy, setBusy] = useState(false);
  const err = mapFormError({ name, host, ports }, (bridge.lan ?? []).map((l) => l.addr)) ?? (record && !password ? "enter the camera's password" : null);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const p = parsePorts(ports);
    if (!p.ports) return;
    setBusy(true);
    try {
      const t = await api.mapBridgeCamera(loc, bridge.id, { name: name.trim(), host: host.trim(), ports: p.ports,
        ...(record ? { login: { username: username.trim() || "admin", password } } : {}) });
      if (!record) toast.success(`${t.name} mapped: ${portsText(t.ports)}`);
      else if (t.added) toast.success(`${t.name} added: recording through ${bridge.name}`);
      else toast.error(`${t.name} is mapped, but not added to recording: ${t.add_error ?? "unknown error"}. Fix it and use Apply to camera, or add it on the instance's console.`);
      onDone();
    } catch (er) { toast.error(er); } finally { setBusy(false); }
  };
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form className="modal central-dialog central-form" onClick={(e) => e.stopPropagation()} onSubmit={submit} style={{ maxWidth: 520 }}>
        <header className="modal-head"><h2>Add a camera via {bridge.name}</h2><button type="button" className="ghost" onClick={onClose} aria-label="Close">✕</button></header>
        <label className="field"><span>Name</span><input value={name} maxLength={120} placeholder="Gate" onChange={(e) => setName(e.target.value)} /></label>
        <label className="field"><span>Camera's LAN address <span className="muted small">a fixed address on the PC's network</span></span>
          <input value={host} placeholder="192.168.1.50" onChange={(e) => setHost(e.target.value)} /></label>
        <div className="row" style={{ gap: 8 }}>
          <label className="field" style={{ flex: 1 }}><span>Username</span><input value={username} autoComplete="off" disabled={!record} onChange={(e) => setUsername(e.target.value)} /></label>
          <label className="field" style={{ flex: 1 }}><span>Password</span><input type="password" value={password} autoComplete="new-password" disabled={!record} onChange={(e) => setPassword(e.target.value)} /></label>
        </div>
        <details className="small"><summary>Ports and options</summary>
          <label className="field"><span>Camera ports <span className="muted small">RTSP, ONVIF (also PTZ, events, its web page), SD replay</span></span>
            <input value={ports} onChange={(e) => setPorts(e.target.value)} /></label>
          <label className="row small" style={{ gap: 6 }}><input type="checkbox" checked={record} onChange={(e) => setRecord(e.target.checked)} />
            Add it to the central recording now (untick to only relay it, and add it on the instance's console later)</label>
        </details>
        <p className="muted small">The bridge relays only this camera's ports. The password goes straight to the recording instance; the hub doesn't keep it.</p>
        <div className="row">
          <button type="submit" disabled={busy || !!err}>{busy ? "Adding…" : record ? "Add camera" : "Map"}</button>
          <button type="button" className="ghost" onClick={onClose}>Cancel</button>
          {err && <span className="muted small">{err}</span>}
        </div>
      </form>
    </div>
  );
}

/** The relay settings for the instance's camera, for entering by hand (what Apply to camera writes). */
function SheetDialog({ t, gateway, onClose }: { t: BridgeTarget; gateway: string | null; onClose: () => void }) {
  const rows = relaySheet(t, gateway);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal central-dialog" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 480 }}>
        <header className="modal-head"><h2>{t.name}: camera settings</h2><button type="button" className="ghost" onClick={onClose} aria-label="Close">✕</button></header>
        <p className="small">On the instance's console, the camera keeps its LAN address <code>{t.host}</code>, username and password; its outside address is the relay:</p>
        <table className="hub-table small"><tbody>{rows.map((r) => <tr key={r.field}><th>{r.label}</th><td><code>{r.value}</code></td></tr>)}</tbody></table>
        <div className="row"><button className="ghost small" onClick={() => copy(sheetText(rows))}>Copy</button><span className="spacer" /><button onClick={onClose}>Done</button></div>
      </div>
    </div>
  );
}
