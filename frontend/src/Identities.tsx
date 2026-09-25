import { useEffect, useState } from "react";
import { api, fmtTime, media, type Camera, type IdentitiesResult, type IdentityCluster } from "./api";
import { EventCard } from "./Events";
import { EventDetail } from "./EventDetail";
import { confirmDialog, toast } from "./ui";

type Kind = "person" | "vehicle";
const RANGES: [string, number][] = [["Today", 0], ["24 h", 24], ["3 days", 72], ["7 days", 168]];

const dayStart = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d.getTime() / 1000; };
const clock = (ts: number) => new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
const mins = (s: number) => (s < 90 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`);

/** Repeated sightings grouped into people and vehicles (re-ID / CLIP fingerprints), with naming. */
export function IdentitiesView({ cameras }: { cameras: Camera[] }) {
  const [kind, setKind] = useState<Kind>("person");
  const [hours, setHours] = useState(0);
  const [cam, setCam] = useState("");
  const [r, setR] = useState<IdentitiesResult | null>(null);
  const [err, setErr] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [naming, setNaming] = useState<{ key: string; name: string; notes: string } | null>(null);
  const [limit, setLimit] = useState(25);
  const name = (id: string) => cameras.find((c) => c.id === id)?.name ?? id;

  const load = () => {
    const since = hours ? Date.now() / 1000 - hours * 3600 : dayStart();
    api.identities(kind, since, cam || undefined).then((x) => { setR(x); setErr(""); }).catch((e) => setErr(String(e)));
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [kind, hours, cam]);

  const saveName = async (c: IdentityCluster) => {
    if (!naming?.name.trim()) return;
    try {
      await api.nameIdentity({ kind, name: naming.name, notes: naming.notes, event_ids: c.events.map((e) => e.id) });
      toast.success(`Named "${naming.name.trim()}" · will be recognised from now on`);
      setNaming(null);
      load();
    } catch (e) { toast.error(e); }
  };
  const forget = async (c: IdentityCluster) => {
    if (!c.identity_id || !await confirmDialog(`Forget the name "${c.name}"?`, { message: "Future sightings will be unnamed again.", confirmLabel: "Forget", danger: true })) return;
    await api.deleteIdentity(c.identity_id);
    toast.success(`Forgot "${c.name}"`);
    load();
  };

  return (
    <div className="identities">
      <div className="toolbar">
        <div className="segmented">
          <button className={kind === "person" ? "active" : ""} onClick={() => setKind("person")}>People</button>
          <button className={kind === "vehicle" ? "active" : ""} onClick={() => setKind("vehicle")}>Vehicles</button>
        </div>
        <div className="segmented">
          {RANGES.map(([l, hrs]) => <button key={l} className={hours === hrs ? "active" : ""} onClick={() => setHours(hrs)}>{l}</button>)}
        </div>
        <select value={cam} onChange={(e) => setCam(e.target.value)}>
          <option value="">All cameras</option>
          {cameras.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        {r && <span className="muted small">{r.clusters.length} {kind === "person" ? "people" : "vehicles"} from {r.sightings} sightings</span>}
      </div>
      <p className="muted small">
        Sightings are matched by appearance{kind === "person" ? " (clothing, build) and confirmed cross-camera journeys" : " (colour, shape)"}, so the same
        {kind === "person" ? " person" : " vehicle"} in different clothes or lighting may appear twice. Name one and it will be recognised next time.
      </p>
      {err && <div className="error">{err}</div>}
      {r && r.clusters.length === 0 && <div className="empty">No verified {kind === "person" ? "people" : "vehicles"} in this period.</div>}
      <div className="identity-list">
        {r?.clusters.slice(0, limit).map((c) => {
          const cover = c.events.find((e) => e.id === c.cover) ?? c.events[0];
          const isOpen = expanded === c.key;
          return (
            <div key={c.key} className={`identity ${c.name ? "named" : ""}`}>
              <button className="identity-cover" onClick={() => setOpen(cover.id)} title="Open the clearest sighting">
                {cover.snapshot ? <img src={media(cover, "snapshot.jpg")} alt="" loading="lazy" /> : <div className="thumb-empty">{kind}</div>}
              </button>
              <div className="identity-body">
                <div className="identity-head">
                  <strong>{c.name ?? (kind === "person" ? `Person ${c.key.slice(1)}` : `Vehicle ${c.key.slice(1)}`)}</strong>
                  {c.name && <span className="badge journey-badge" title={`Recognised (similarity ${c.name_sim})`}>known</span>}
                  {c.priority !== "none" && <span className={`badge threat-${c.priority}`}>{c.priority}</span>}
                  {c.unusual && <span className="badge unusual-badge">⚠ Unusual</span>}
                  {!c.fingerprinted && <span className="muted small" title="An older event with no saved crops">no fingerprint</span>}
                </div>
                <div className="muted small">
                  {c.sightings} sighting{c.sightings === 1 ? "" : "s"} · {clock(c.first_ts)}{c.sightings > 1 ? `–${clock(c.last_ts)}` : ""} · {mins(c.on_site_s)} on camera · {c.cameras.join(", ")}
                </div>
                {c.description && <p className="identity-desc">{c.description}</p>}
                <div className="row small">
                  <button className="ghost small" onClick={() => setExpanded(isOpen ? null : c.key)}>{isOpen ? "Hide sightings" : "Show sightings"}</button>
                  {c.name
                    ? <button className="ghost small" onClick={() => forget(c)}>Forget name</button>
                    : <button className="ghost small" onClick={() => setNaming(naming?.key === c.key ? null : { key: c.key, name: "", notes: "" })}>Name…</button>}
                </div>
                {naming?.key === c.key && (
                  <form className="row small identity-name" onSubmit={(e) => { e.preventDefault(); saveName(c); }}>
                    <input autoFocus placeholder={kind === "person" ? "e.g. Shawn" : "e.g. UPS truck"} value={naming.name} onChange={(e) => setNaming({ ...naming, name: e.target.value })} />
                    <input placeholder="notes, e.g. owner / weekly delivery (used in Qwen's prompts)" value={naming.notes} onChange={(e) => setNaming({ ...naming, notes: e.target.value })} />
                    <button type="submit" className="small">Save</button>
                  </form>
                )}
                {isOpen && (
                  <div className="event-grid identity-events">
                    {c.events.map((e) => (
                      <EventCard key={e.id} e={{ ...e, camera_class: kind, status: "verified", threat: null, yolo_conf: null, yolo_hits: null,
                        camera_conf: null, clip: null, error: null, track_id: "" } as never} cameraName={name(e.camera_id)} onOpen={() => setOpen(e.id)} />
                    ))}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
      {r && r.clusters.length > limit && <div className="center"><button className="ghost" onClick={() => setLimit(limit + 25)}>Show {Math.min(25, r.clusters.length - limit)} more</button></div>}
      {r && r.named.length > 0 && (
        <details className="muted small identity-known">
          <summary>Known {kind === "person" ? "people" : "vehicles"} ({r.named.length})</summary>
          <ul className="plain">
            {r.named.map((n) => <li key={n.id}><strong>{n.name}</strong>{n.notes ? ` — ${n.notes}` : ""} · learned from {n.sightings} sightings · {fmtTime(n.updated_at)}
              <button className="linkish small" onClick={async () => { await api.deleteIdentity(n.id); load(); }}>forget</button></li>)}
          </ul>
        </details>
      )}
      {open !== null && <EventDetail id={open} cameraName={name} onClose={() => setOpen(null)} />}
    </div>
  );
}
