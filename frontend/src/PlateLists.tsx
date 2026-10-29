/**
 * Plates › Lists (backend traffic.py "Lists", traffic_api.py): allow / deny / watch plates with their options, and what
 * each entry has seen.
 *  - An entry: a plate or a partial plate (? one character, * any: a police lookout "ABC12?"), a category, a note, an
 *    expiry (24 hours, 7 or 30 days, a date), when it applies (always, during SOC hours, set days and hours in the
 *    Site's zone), what a hit does (the list's own alarm, another priority, or only mark the read), which cameras, and
 *    the vehicle the plate belongs on (another vehicle with that plate raises the cloned-plate alarm; "Use this
 *    vehicle" saves the one Otso saw last).
 *  - The table: each entry's list and category, what a hit does, when, how often it was seen (7 and 30 days) and last,
 *    Otso's description of its vehicle, and a warning when the same plate is on another list that wins (deny before
 *    watch before allow), so the entry does nothing. A row opens: the latest sightings (a click opens the read), the
 *    plate's counted notes, the alarms it raised, the expected vehicle.
 *  - Search, CSV export and import (plate, list, category, note, expires). Changes need the operator role.
 * Pure logic: traffic.ts.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { fmtTime, fmtWhen, type Camera, type SiteApi } from "./api";
import { useQueuedImage } from "./imageQueue";
import { formatPlate } from "./plate";
import {
  type EntryDraft, type ListEntry, type ListEntryDetail, type PlateList, CATEGORIES, DAY_NAMES, NO_ENTRY_DRAFT, PLATE_LISTS, categoryName,
  conflictText, csvToRows, draftToBody, effectText, entriesToCsv, entryToDraft, expectText, expiryText, filterEntries, isPartial, listError,
  listName, onList, plateProblem, scheduleText, seenText, timeAgo, VEHICLE_COLORS,
} from "./traffic";
import { confirmDialog, errorText, toast } from "./ui";

const VEHICLE_KINDS = ["car", "suv", "pickup truck", "van", "box truck", "semi truck", "motorcycle", "bus"];

export function ListsSection({ a, name, cameras, onRead, onEvent }: {
  a: SiteApi; name: (id: string) => string; cameras: Camera[]; onRead: (id: number) => void; onEvent: (id: number) => void;
}) {
  const [entries, setEntries] = useState<ListEntry[] | null>(null);
  const [err, setErr] = useState("");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const [edit, setEdit] = useState<ListEntry | null>(null);
  const [imported, setImported] = useState<{ line: number; plate: string; error?: string }[] | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const load = () => a.plateLists().then((x) => { setEntries(x); setErr(""); }).catch((e) => setErr(errorText(e)));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [a]);
  const shown = useMemo(() => filterEntries(entries ?? [], q), [entries, q]);

  const remove = async (e: ListEntry) => {
    if (!await confirmDialog(`Take ${formatPlate(e.plate)} off the ${e.list} list?`, {
      message: e.list === "allow" ? "Its reads are no longer marked." : "Its reads no longer raise an alarm.", confirmLabel: "Remove", danger: true,
    })) return;
    try {
      await a.deletePlateEntry(e.id);
      toast.success(`${formatPlate(e.plate)} removed from the ${e.list} list`);
      if (open === e.id) setOpen(null);
      load();
    } catch (x) { toast.error(listError(x, errorText)); }
  };
  const exportCsv = () => {
    const url = URL.createObjectURL(new Blob([entriesToCsv(entries ?? [])], { type: "text/csv" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `plate-lists-${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const importCsv = async (f: File) => {
    const rows = csvToRows(await f.text());
    if (!rows.length) { toast.error("No rows in that file"); return; }
    if (!await confirmDialog(`Import ${rows.length} row${rows.length === 1 ? "" : "s"}?`, {
      message: "Columns: plate, list (allow, deny or watch), category, note, expires (YYYY-MM-DD). A plate already on that list is updated.", confirmLabel: "Import",
    })) return;
    try {
      const r = await a.importPlateEntries(rows);
      const bad = r.results.filter((x) => !x.ok);
      toast.success(`${r.added} added or updated${bad.length ? `, ${bad.length} skipped` : ""}`);
      setImported(bad.length ? bad : null);
      load();
    } catch (x) { toast.error(listError(x, errorText)); }
  };

  return (
    <section className="plates-lists">
      <p className="muted small">
        A plate on the deny list raises a high-priority alarm when a camera reads it (a traffic camera, or Otso reading the footage), the watch list a
        medium one; the allow list only marks its reads. Each entry can say when it applies, what a hit does, which cameras, and the vehicle the plate
        belongs on. A plate one character off a traffic read is a possible match. Changes need the operator role.
      </p>
      <EntryForm a={a} cameras={cameras} entries={entries ?? []} onSaved={load} />
      <div className="toolbar plates-lists-tools">
        <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search plate, note, category, vehicle" aria-label="Search the lists" />
        <span className="spacer" />
        <button className="ghost small" disabled={!entries?.length} onClick={exportCsv}>Export CSV</button>
        <button className="ghost small" onClick={() => file.current?.click()}>Import CSV</button>
        <input ref={file} type="file" accept=".csv,text/csv" hidden onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) importCsv(f); }} />
      </div>
      {imported && (
        <details className="small plates-import-errors" open>
          <summary>{imported.length} row{imported.length === 1 ? "" : "s"} not imported</summary>
          <ul>{imported.map((x) => <li key={x.line}>Row {x.line}{x.plate ? ` (${x.plate})` : ""}: {x.error}</li>)}</ul>
          <button className="ghost small" onClick={() => setImported(null)}>Dismiss</button>
        </details>
      )}
      {err && <p className="error small">{err}</p>}
      {entries && entries.length === 0 && <div className="empty">No plates on any list yet.</div>}
      {entries && entries.length > 0 && shown.length === 0 && <div className="empty">Nothing on the lists matches “{q}”.</div>}
      {shown.length > 0 && (
        <table className="table plates-table plates-lists-table">
          <thead><tr><th>Plate</th><th>List</th><th>Note</th><th>Applies</th><th>Seen</th><th /></tr></thead>
          <tbody>
            {shown.map((e) => <EntryRows key={e.id} a={a} e={e} name={name} open={open === e.id} onToggle={() => setOpen(open === e.id ? null : e.id)}
              onEdit={() => setEdit(e)} onRemove={() => remove(e)} onRead={onRead} onEvent={onEvent} onChanged={load} />)}
          </tbody>
        </table>
      )}
      {edit && (
        <div className="modal-backdrop" onClick={() => setEdit(null)}>
          <div className="modal plate-entry-modal" role="dialog" aria-modal="true" aria-label={`Edit ${formatPlate(edit.plate)}`} onClick={(x) => x.stopPropagation()}>
            <header className="modal-head">
              <h2>{formatPlate(edit.plate)} <span className="muted small">· {listName(edit.list)}</span></h2>
              <button className="ghost" onClick={() => setEdit(null)} aria-label="Close">✕</button>
            </header>
            <EntryForm a={a} cameras={cameras} entries={entries ?? []} editing={edit} onSaved={() => { setEdit(null); load(); }} />
          </div>
        </div>
      )}
    </section>
  );
}

/** One entry's row, and its details under it when open. */
function EntryRows({ a, e, name, open, onToggle, onEdit, onRemove, onRead, onEvent, onChanged }: {
  a: SiteApi; e: ListEntry; name: (id: string) => string; open: boolean; onToggle: () => void; onEdit: () => void; onRemove: () => void;
  onRead: (id: number) => void; onEvent: (id: number) => void; onChanged: () => void;
}) {
  const warn = conflictText(e);
  const cams = e.cameras?.length ? e.cameras.map(name).join(", ") : "";
  return <>
    <tr className={`plate-entry${e.expired ? " muted" : ""}${open ? " open" : ""}`} onClick={onToggle} aria-expanded={open} tabIndex={0}
      onKeyDown={(x) => { if (x.key === "Enter" || x.key === " ") { x.preventDefault(); onToggle(); } }}>
      <td>
        <strong className="plate-text">{isPartial(e.plate) ? e.plate : formatPlate(e.plate)}</strong>
        {isPartial(e.plate) && <span className="badge" title="A partial plate: ? is one unknown character, * any number"> partial</span>}
        {e.look && <div className="muted small" title="Otso's description of the vehicle last seen with this plate">{e.look}</div>}
      </td>
      <td>
        <span className={`badge list-badge ${e.list}`}>{listName(e.list)}</span>
        {e.category && <div className="small">{categoryName(e.category)}</div>}
        <div className="muted small">{effectText(e)}</div>
        {warn && <div className={`small ${e.overridden_by ? "warn-text" : "muted"}`}>{e.overridden_by ? "⚠ " : ""}{warn}</div>}
      </td>
      <td>{e.note || <span className="muted small">—</span>}{e.by && <div className="muted small">added by {e.by} · {fmtWhen(e.created_at)}</div>}</td>
      <td className="small">
        {scheduleText(e.schedule)}{cams && <div className="muted">on {cams}</div>}
        <div className="muted">{expiryText(e) === "never" ? "no expiry" : expiryText(e)}</div>
        {e.active === false && !e.expired && <div className="muted">not in force now</div>}
        {e.expect && <div className="muted" title="Another vehicle with this plate raises the cloned-plate alarm">belongs on a {expectText(e.expect)}</div>}
      </td>
      <td className="small">
        {e.last_seen ? <span title={fmtTime(e.last_seen)}>{timeAgo(e.last_seen)}{e.last_camera ? ` · ${name(e.last_camera)}` : ""}</span> : <span className="muted">not seen</span>}
        {e.seen_30d != null && <div className="muted">{seenText(e)}</div>}
      </td>
      <td className="row" onClick={(x) => x.stopPropagation()}>
        <button className="ghost small" onClick={onEdit}>Edit</button>
        <button className="ghost small" onClick={onRemove}>Remove</button>
      </td>
    </tr>
    {open && (
      <tr className="plate-entry-detail"><td colSpan={6}>
        <EntryDetail a={a} id={e.id} name={name} onRead={onRead} onEvent={onEvent} onChanged={onChanged} />
      </td></tr>
    )}
  </>;
}

function EntryDetail({ a, id, name, onRead, onEvent, onChanged }: {
  a: SiteApi; id: number; name: (id: string) => string; onRead: (id: number) => void; onEvent: (id: number) => void; onChanged: () => void;
}) {
  const [d, setD] = useState<ListEntryDetail | null>(null);
  const [err, setErr] = useState("");
  const pull = () => a.plateEntry(id).then((x) => { setD(x); setErr(""); }).catch((e) => setErr(errorText(e)));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { pull(); }, [a, id]);
  const setExpect = async (expect: ListEntryDetail["expect"]) => {
    try {
      await a.editPlateEntry(id, { expect: expect ?? null });
      toast.success(expect ? `Saved: this plate belongs on a ${expectText(expect)}` : "Expected vehicle cleared");
      pull(); onChanged();
    } catch (e) { toast.error(listError(e, errorText)); }
  };
  if (err) return <p className="error small">{err}</p>;
  if (!d) return <p className="muted small">Loading…</p>;
  const latest = d.sightings[0];
  const v = d.vehicle;
  return (
    <div className="plate-entry-body">
      <div className="plate-entry-pic">
        {latest ? <>
          <button className="linkish" onClick={() => onRead(latest.id)} title="Open this read">
            <SightingPicture a={a} id={latest.id} kind="vehicle" />
          </button>
          {latest.images.includes("plate") && <SightingPicture a={a} id={latest.id} kind="plate" />}
          <div className="muted small">Last seen {fmtTime(latest.ts)} · {name(latest.camera_id)}</div>
        </> : <div className="thumb-empty">Not seen in the last 30 days</div>}
      </div>
      <div className="plate-entry-facts">
        {d.look && <p><strong>Otso saw:</strong> {d.look}</p>}
        {d.notes.length > 0 && <ul className="otso-notes">{d.notes.map((n) => <li key={n}>{n}</li>)}</ul>}
        <p className="small">
          <strong>Belongs on:</strong> {d.expect ? `a ${expectText(d.expect)}` : <span className="muted">not set (any vehicle)</span>}{" "}
          {v && expectText(v) !== expectText(d.expect) && (
            <button className="ghost small" onClick={() => setExpect({ vehicle_type: v.vehicle_type, color: v.color, make: v.make, model: v.model })}
              title="Another vehicle with this plate will raise the cloned-plate alarm">Use the vehicle Otso saw ({expectText(v)})</button>
          )}
          {d.expect && <button className="ghost small" onClick={() => setExpect(null)}>Clear</button>}
        </p>
        {conflictText(d) && <p className={`small ${d.overridden_by ? "warn-text" : "muted"}`}>{d.overridden_by ? "⚠ " : ""}{conflictText(d)}</p>}
        {d.sightings.length > 1 && <>
          <h4 className="spaced">Recent sightings</h4>
          <div className="plate-entry-sightings">
            {d.sightings.map((r) => (
              <button key={r.id} className="linkish plate-entry-sighting" onClick={() => onRead(r.id)} title={`${fmtTime(r.ts)} · ${name(r.camera_id)}`}>
                {r.images.includes("plate") ? <SightingPicture a={a} id={r.id} kind="plate" /> : <span className="thumb-empty plate-crop" />}
                <span className="muted small">{fmtWhen(r.ts)}</span>
              </button>
            ))}
          </div>
        </>}
        <h4 className="spaced">Alarms (30 days)</h4>
        {d.alarms.length === 0 ? <p className="muted small">None.</p> : (
          <ul className="plain small">
            {d.alarms.map((x) => (
              <li key={x.id}><button className="linkish" onClick={() => onEvent(x.id)}>{fmtWhen(x.ts)}</button>
                {x.priority && <span className={`badge priority-${x.priority}`}> {x.priority}</span>} {x.text}</li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function SightingPicture({ a, id, kind }: { a: SiteApi; id: number; kind: "vehicle" | "plate" }) {
  const { src, failed, onLoad, onError } = useQueuedImage(a.trafficImage(id, kind), kind === "plate" ? 2 : undefined);
  if (failed || !src) return <span className={`thumb-empty ${kind === "plate" ? "plate-crop" : "vehicle-crop"}`} />;
  return <img className={kind === "plate" ? "plate-crop" : "vehicle-crop big"} src={src} alt="" onLoad={onLoad} onError={onError} />;
}

/** Add an entry (inline), or edit one (`editing`: plate and list fixed). The options sit behind "More options". */
function EntryForm({ a, cameras, entries, editing, onSaved }: {
  a: SiteApi; cameras: Camera[]; entries: ListEntry[]; editing?: ListEntry; onSaved: () => void;
}) {
  const [d, setD] = useState<EntryDraft>(() => (editing ? entryToDraft(editing) : NO_ENTRY_DRAFT));
  const [more, setMore] = useState(!!editing);
  const [busy, setBusy] = useState(false);
  const set = (p: Partial<EntryDraft>) => setD((x) => ({ ...x, ...p }));
  const problem = !editing && d.plate ? plateProblem(d.plate) : null;
  const dup = !editing && d.plate && !problem && onList(entries, d.plate, d.list);
  const hoursBad = d.when === "hours" && (!d.days.length || !d.from || !d.to);
  const save = async () => {
    if (!editing && (!d.plate || problem || dup)) return;
    if (hoursBad) return;
    setBusy(true);
    try {
      const body = draftToBody(d);
      if (editing) {
        const { plate: _p, list: _l, ...changes } = body;
        await a.editPlateEntry(editing.id, changes);
        toast.success(`${formatPlate(editing.plate)} saved`);
      } else {
        await a.addPlateEntry(body);
        toast.success(`${isPartial(body.plate) ? body.plate : formatPlate(body.plate)} added to the ${d.list} list`);
        setD({ ...NO_ENTRY_DRAFT, list: d.list });
        setMore(false);
      }
      onSaved();
    } catch (e) { toast.error(listError(e, errorText)); }
    setBusy(false);
  };
  const toggleDay = (k: number) => set({ days: d.days.includes(k) ? d.days.filter((x) => x !== k) : [...d.days, k].sort() });
  const toggleCam = (id: string) => set({ cameras: d.cameras.includes(id) ? d.cameras.filter((x) => x !== id) : [...d.cameras, id] });
  const defaultEffect = d.list === "allow" ? "only mark the read" : `${d.list === "deny" ? "high" : "medium"}-priority alarm`;
  return (
    <form className={`plates-entry-form${editing ? " editing" : ""}`} onSubmit={(e) => { e.preventDefault(); save(); }}>
      <div className="row plates-add-form">
        {!editing && <>
          <label className="field"><span>Plate</span>
            <input value={d.plate} onChange={(e) => set({ plate: e.target.value })} placeholder="e.g. 1691AJN or ABC12?" autoCapitalize="characters" autoComplete="off" spellCheck={false}
              title="A partial plate: ? for one unknown character, * for any number (34* = starts with 34)" />
          </label>
          <label className="field"><span>List</span>
            <select value={d.list} onChange={(e) => set({ list: e.target.value as PlateList })}>
              {PLATE_LISTS.map((l) => <option key={l} value={l}>{listName(l)}</option>)}
            </select>
          </label>
        </>}
        <label className="field"><span>Category</span>
          <select value={d.category} onChange={(e) => set({ category: e.target.value as EntryDraft["category"] })}>
            <option value="">None</option>
            {CATEGORIES.map((c) => <option key={c} value={c}>{categoryName(c)}</option>)}
          </select>
        </label>
        <label className="field grow"><span>Note</span>
          <input value={d.note} maxLength={200} onChange={(e) => set({ note: e.target.value })} placeholder="who, why (e.g. trespass notice 10/12)" />
        </label>
        <label className="field"><span>Expires</span>
          <select value={d.expiry} onChange={(e) => set({ expiry: e.target.value as EntryDraft["expiry"] })}>
            <option value="">Never</option>
            <option value="24h">In 24 hours</option>
            <option value="7d">In 7 days</option>
            <option value="30d">In 30 days</option>
            <option value="date">On a date…</option>
          </select>
        </label>
        {d.expiry === "date" && <label className="field"><span>Date</span><input type="date" value={d.date} onChange={(e) => set({ date: e.target.value })} /></label>}
        {!editing && <button type="button" className="ghost small" aria-expanded={more} onClick={() => setMore(!more)}>{more ? "Fewer options" : "More options"}</button>}
        <button type="submit" disabled={busy || hoursBad || (!editing && (!d.plate || !!problem || !!dup))}>{busy ? "Saving…" : editing ? "Save" : "Add"}</button>
      </div>
      {problem && <p className="error small">{problem}</p>}
      {dup && <p className="muted small">{formatPlate(d.plate)} is already on the {d.list} list.</p>}
      {more && (
        <div className="plates-entry-options">
          <fieldset><legend>Applies</legend>
            <div className="row small">
              {(["always", "soc", "hours"] as const).map((w) => (
                <label key={w} className="row"><input type="radio" checked={d.when === w} onChange={() => set({ when: w })} />
                  {w === "always" ? "Always" : w === "soc" ? "During SOC hours" : "Set days and hours"}</label>
              ))}
            </div>
            {d.when === "hours" && (
              <div className="row small">
                {DAY_NAMES.map((n, k) => <label key={n} className="row"><input type="checkbox" checked={d.days.includes(k)} onChange={() => toggleDay(k)} />{n}</label>)}
                <label className="row">from <input type="time" value={d.from} onChange={(e) => set({ from: e.target.value })} /></label>
                <label className="row">to <input type="time" value={d.to} onChange={(e) => set({ to: e.target.value })} /></label>
                {d.to <= d.from && <span className="muted">(runs past midnight)</span>}
                {hoursBad && <span className="error">pick days and both times</span>}
              </div>
            )}
            {d.when === "soc" && <p className="muted small">Only while the Site's SOC monitoring is armed (a server without the hub never is).</p>}
          </fieldset>
          <fieldset><legend>On a hit</legend>
            <div className="row small">
              <select value={d.action} onChange={(e) => set({ action: e.target.value as EntryDraft["action"] })} aria-label="On a hit">
                <option value="">The list's own ({defaultEffect})</option>
                <option value="alarm">Raise an alarm</option>
                <option value="mark">Only mark the read</option>
              </select>
              {d.action !== "mark" && (d.action === "alarm" || d.list !== "allow") && (
                <select value={d.priority} onChange={(e) => set({ priority: e.target.value as EntryDraft["priority"] })} aria-label="Priority">
                  <option value="">Priority: the list's</option>
                  <option value="low">Low priority</option>
                  <option value="medium">Medium priority</option>
                  <option value="high">High priority</option>
                </select>
              )}
            </div>
          </fieldset>
          {cameras.length > 1 && (
            <fieldset><legend>Cameras</legend>
              <div className="row small">
                <label className="row"><input type="checkbox" checked={!d.cameras.length} onChange={() => set({ cameras: [] })} />All cameras</label>
                {cameras.map((c) => <label key={c.id} className="row"><input type="checkbox" checked={d.cameras.includes(c.id)} onChange={() => toggleCam(c.id)} />{c.name}</label>)}
              </div>
            </fieldset>
          )}
          <fieldset><legend>Belongs on (cloned-plate alarm)</legend>
            <div className="row small">
              <select value={d.expect.color ?? ""} onChange={(e) => set({ expect: { ...d.expect, color: e.target.value || undefined } })} aria-label="Colour">
                <option value="">Any colour</option>
                {VEHICLE_COLORS.map((c) => <option key={c} value={c.toLowerCase()}>{c}</option>)}
              </select>
              <input value={d.expect.make ?? ""} placeholder="Make (Ford)" maxLength={40} aria-label="Make" onChange={(e) => set({ expect: { ...d.expect, make: e.target.value || undefined } })} />
              <input value={d.expect.model ?? ""} placeholder="Model (F-150)" maxLength={40} aria-label="Model" onChange={(e) => set({ expect: { ...d.expect, model: e.target.value || undefined } })} />
              <select value={d.expect.vehicle_type ?? ""} onChange={(e) => set({ expect: { ...d.expect, vehicle_type: e.target.value || undefined } })} aria-label="Kind of vehicle">
                <option value="">Any kind</option>
                {VEHICLE_KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
              </select>
            </div>
            <p className="muted small">Otso checks each sighting of this plate: a different kind of vehicle, or another make and colour, raises a high alarm. Or open the entry and use the vehicle Otso saw.</p>
          </fieldset>
        </div>
      )}
    </form>
  );
}
