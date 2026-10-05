/** Customer → AI: the hub's shared model and TURN relay, and each server's use of it. */
import { useCallback, useEffect, useState } from "react";
import { toast } from "@site/ui";
import { type Org, type Usage, api } from "../api";

export function SharedAiBox({ org, canEdit }: { org: Org; canEdit: boolean }) {
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
        {u.configured && u.provider.kind === "site" && <> — served by the GPU at <b>{u.provider.site_name || u.provider.site_id}</b> through its tunnel ({u.provider.online ? "online" : "offline: servers fall back to their local model"})</>}
        {u.configured && u.provider.kind === "url" && <> — served by the hub's own model server</>}.
      </p>
      <label className="row small">
        <input type="checkbox" checked={u.ai_shared} disabled={!canEdit || !u.configured} onChange={async (e) => { try { await api.patchOrg(org.id, { ai_shared: e.target.checked }); load(); toast.success(e.target.checked ? "Servers now use the hub's model" : "Servers are back on their local model"); } catch (err) { toast.error(err); } }} />
        Servers of this customer use the hub's model (their local one stays as a fallback)
      </label>
      {u.sites.length > 0 && (
        <table className="hub-table">
          <thead><tr><th>Server</th><th>Requests ({u.days} d)</th><th>Prompt tokens</th><th>Output tokens</th><th>Avg latency</th><th>Errors</th></tr></thead>
          <tbody>{u.sites.map((s) => <tr key={s.site_id}><td>{s.site_name}</td><td>{s.requests}</td><td>{s.prompt_tokens.toLocaleString()}</td><td>{s.completion_tokens.toLocaleString()}</td><td>{s.latency_ms} ms</td><td>{s.errors}</td></tr>)}</tbody>
        </table>
      )}
    </div>
  );
}
