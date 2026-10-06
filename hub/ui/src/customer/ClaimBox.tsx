/**
 * Customer → Add server: enroll a waiting server by its claim code into an existing Site, or into a new one. The new
 * Site is created only on Enroll (with a server waiting), then picked, so a failed claim retried doesn't create it twice.
 */
import { useEffect, useState } from "react";
import { toast } from "@site/ui";
import { type ClaimPreview, type Org, type Site, api } from "../api";

const NEW = "__new__";

export function ClaimBox({ org, sites, onDone, initialSite }: { org: Org; sites: Site[]; onDone: () => void; initialSite?: string }) {
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [siteId, setSiteId] = useState(initialSite ?? "");
  const [newSite, setNewSite] = useState({ name: "", address: "" });
  const [preview, setPreview] = useState<ClaimPreview | null>(null);
  const [busy, setBusy] = useState(false);
  // default to the only Site, or to "New site…" when there is none yet
  useEffect(() => { if (!siteId) setSiteId(sites.length === 1 ? sites[0].id : sites.length === 0 ? NEW : ""); }, [sites, siteId]);
  useEffect(() => {
    const c = code.trim().toUpperCase();
    if (c.replace("-", "").length < 8) { setPreview(null); return; }
    api.claimPreview(c).then((p) => { setPreview(p); if (!name && p.hint?.hostname) setName(p.hint.hostname); }).catch(() => setPreview(null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code]);
  const isNew = siteId === NEW;
  const ready = !busy && !!preview?.waiting && !!name.trim() && (isNew ? !!newSite.name.trim() : !!siteId);
  const enrol = async () => {
    setBusy(true);
    try {
      const loc = isNew ? await api.createLocation(org.id, { name: newSite.name.trim(), address: newSite.address.trim() }) : sites.find((s) => s.id === siteId);
      if (!loc) throw new Error("Choose a site");
      if (isNew) { setSiteId(loc.id); onDone(); }   // the Sites list now has it, whatever the claim does
      // `location` (the server's own note) gets the Site's address so older pages that still read it show something sensible
      await api.claim(org.id, { code: code.trim().toUpperCase(), name: name.trim(), location: loc.address ?? "", location_id: loc.id });
      toast.success(`${name.trim()} enrolled in ${loc.name}`);
      setCode(""); setName(""); setPreview(null); setNewSite({ name: "", address: "" }); setSiteId(loc.id);
      onDone();
    } catch (e) { toast.error(e); } finally { setBusy(false); }
  };
  return (
    <div className="card">
      <h3>Add server</h3>
      <p className="muted small">On the server open Settings → System → Cloud hub and type its claim code here. Nothing is port-forwarded: the server is already connected to this hub, waiting.</p>
      <div className="claim-box">
        <label className="field"><span>Claim code</span><input value={code} placeholder="ABCD-EFGH" onChange={(e) => setCode(e.target.value)} style={{ width: 130 }} /></label>
        <label className="field"><span>Server name</span><input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Main NVR" /></label>
        <label className="field"><span>Site</span>
          <select value={siteId} onChange={(e) => setSiteId(e.target.value)}>
            {sites.length !== 1 && <option value="" disabled>Choose…</option>}
            {sites.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            <option value={NEW}>New site…</option>
          </select>
        </label>
        {isNew && <label className="field"><span>New site name</span><input value={newSite.name} maxLength={120} onChange={(e) => setNewSite({ ...newSite, name: e.target.value })} placeholder="e.g. Austin HQ" /></label>}
        {isNew && <label className="field"><span>Address</span><input value={newSite.address} maxLength={200} onChange={(e) => setNewSite({ ...newSite, address: e.target.value })} placeholder="address or city" /></label>}
        <button disabled={!ready} onClick={enrol}>Enroll server</button>
      </div>
      {preview && (
        <div className="hint-box">
          {preview.waiting ? "✓ A server is waiting with this code" : "That server isn't connected right now (it retries within a minute)"} · {preview.hint?.hostname ?? "unknown host"} · v{preview.hint?.version ?? "?"} · {preview.hint?.cameras?.length ?? 0} cameras
          {preview.hint?.cameras?.length ? `: ${preview.hint.cameras.map((c) => c.name).join(", ")}` : ""}{preview.agent_ip ? ` · from ${preview.agent_ip}` : ""}
        </div>
      )}
    </div>
  );
}
