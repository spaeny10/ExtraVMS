/** Customer → New customer (hub administrators only). */
import { useState } from "react";
import { toast } from "@site/ui";
import { api } from "../api";

export function CreateOrgBox({ onDone }: { onDone: () => void }) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  return (
    <div className="card">
      <h3>New customer <span className="muted small">(hub administrator)</span></h3>
      <div className="row">
        <input placeholder="Customer name" value={name} onChange={(e) => { setName(e.target.value); setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "")); }} />
        <input placeholder="slug" value={slug} onChange={(e) => setSlug(e.target.value)} />
        <button disabled={!name || !slug} onClick={async () => { try { await api.createOrg(name, slug); setName(""); setSlug(""); onDone(); toast.success("Customer created"); } catch (e) { toast.error(e); } }}>Create</button>
      </div>
    </div>
  );
}
