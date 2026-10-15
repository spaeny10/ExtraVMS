/**
 * Site › Settings › Cameras › Camera web pages: open a camera's own settings page from the hub (hub camweb.py), through
 * its server's tunnel, without port forwarding. Shown to the Site's admins and hub administrators only (access.ts
 * canOpenCameraWeb; the hub checks the same). Each opening is a session of its own (audited) that closes after 30 minutes
 * without use and 2 hours after opening at the latest; the user's open ones are listed here with Close. The pure parts
 * are cameraWeb.ts.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "@site/ui";
import { type Camera, type CameraWebSession, type Site, api } from "../api";
import { closesIn, openBlankTab, siteSessions, webCameras } from "./cameraWeb";

export function CameraWebCard({ site }: { site: Site }) {
  const [registry, setRegistry] = useState<Camera[] | null>(null);
  const [sessions, setSessions] = useState<CameraWebSession[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [blocked, setBlocked] = useState<{ url: string; name: string } | null>(null);
  const [now, setNow] = useState(Date.now() / 1000);
  const servers = useMemo(() => site.servers.filter((s) => !s.retired_at), [site.servers]);
  const loadSessions = useCallback(() => api.cameraWebSessions().then(setSessions).catch(() => setSessions([])), []);
  useEffect(() => { api.locationCameras(site.id).then(setRegistry).catch(() => setRegistry([])); loadSessions(); }, [site.id, loadSessions]);
  useEffect(() => { const t = setInterval(() => setNow(Date.now() / 1000), 30_000); return () => clearInterval(t); }, []);
  const list = useMemo(() => webCameras(servers, registry ?? []), [servers, registry]);
  const mine = siteSessions(sessions, servers.map((s) => s.id)).filter((s) => s.expires_at > now);
  const multi = list.length > 1;

  const open = async (serverId: string, cameraId: string, name: string) => {
    const tab = openBlankTab();
    setBusy(`${serverId}/${cameraId}`);
    setBlocked(null);
    try {
      const r = await api.openCameraWeb(serverId, cameraId);
      if (tab) tab.location.href = r.url;
      else setBlocked({ url: r.url, name });
      loadSessions();
    } catch (e) {
      tab?.close();
      toast.error(e);
    } finally {
      setBusy(null);
    }
  };
  const close = async (s: CameraWebSession) => {
    try { await api.closeCameraWeb(s.id); toast.success(`${s.camera_name}: camera page closed`); loadSessions(); } catch (e) { toast.error(e); }
  };

  return (
    <div className="card">
      <h3 style={{ margin: 0 }}>Camera web pages</h3>
      <p className="muted small">
        Opens the camera's own settings page in a new tab, through its server's connection to the hub (no port forwarding).
        You log in with the camera's own login; the page closes after 30 minutes without use, and 2 hours after opening at the latest.
      </p>
      {blocked && (
        <p className="small">The browser blocked the new tab: <a href={blocked.url} target="_blank" rel="noopener noreferrer" onClick={() => setBlocked(null)}>open {blocked.name}'s page ↗</a> (the link works once, for a minute).</p>
      )}
      {registry === null ? <p className="muted small">Loading…</p>
        : list.length === 0 ? <p className="muted small">This site's servers haven't reported any cameras yet.</p>
        : list.map((srv) => (
          <div key={srv.id} className="cam-web-server">
            {(multi || !srv.online) && <div className="small"><strong>{srv.name}</strong>{!srv.online && <span className="muted"> · offline</span>}</div>}
            <ul className="cam-web-list">
              {srv.cameras.map((c) => {
                const key = `${srv.id}/${c.camera_id}`;
                return (
                  <li key={key} className="row">
                    <span className={c.enabled ? "" : "muted"}>{c.name}{c.enabled ? "" : " (disabled)"}</span>
                    <span className="spacer" />
                    <button className="ghost small" disabled={!srv.online || busy === key} onClick={() => open(srv.id, c.camera_id, c.name)}
                      title={srv.online ? "Opens the camera's own settings page in a new tab" : "The camera's server is offline"}>
                      {busy === key ? "Opening…" : "Open camera web page ↗"}
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      {mine.length > 0 && (
        <>
          <h4 className="spaced" style={{ marginBottom: 4 }}>Your open camera pages</h4>
          <ul className="cam-web-list">
            {mine.map((s) => (
              <li key={s.id} className="row">
                <span>{s.camera_name}{multi && s.server_name ? <span className="muted small"> · {s.server_name}</span> : null}</span>
                <span className="muted small">{s.entered_at ? closesIn(s.expires_at, now) : "link not opened yet"}</span>
                <span className="spacer" />
                <button className="ghost small" onClick={() => close(s)}>Close</button>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
