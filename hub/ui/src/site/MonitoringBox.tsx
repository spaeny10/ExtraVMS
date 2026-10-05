/**
 * Site → Settings → Monitoring: whether the SOC watches this Site, and when (weekly windows in the Site's timezone,
 * holidays, arm/disarm now). The hub decides what is armed (`armed`, `reason`, `next_change` on GET); monitoring.ts
 * only previews the draft and works out "until the next scheduled change" for an override.
 *
 * Editors (canEditMonitoring: customer admins, SOC supervisors) change the schedule and Save it with one PUT; people
 * who may arm (customer operators and up, SOC operators) get Arm now / Disarm now; everyone else reads the state.
 * Disarmed hours don't drop anything: events still reach the Site's Timeline and customer alerts, just not the SOC.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { promptDialog, toast } from "@site/ui";
import { type ArmHoliday, type Monitoring, type MonitoringConfig, type Site, ago, api, fmtTime } from "../api";
import { go, settingsHref } from "../nav";
import { DAYS, type OverrideDuration, OVERRIDE_DURATIONS, REASON_LABEL, type Week, copyDay, fmtInZone, fromWeek, isArmedAt, isOvernight, isTime, mergeSpans, overrideUntil, toWeek, validTimeZone, windowLabel } from "./monitoring";

type Draft = { monitored: boolean; week: Week; holidays: ArmHoliday[]; group: string };
const draftOf = (m: Monitoring): Draft => ({ monitored: m.monitored, week: toWeek(m.arm_schedule), holidays: m.arm_holidays.map((h) => ({ ...h })), group: m.soc_group_minutes == null ? "" : String(m.soc_group_minutes) });
const configOf = (d: Draft): MonitoringConfig => ({
  monitored: d.monitored, arm_schedule: fromWeek(d.week),
  arm_holidays: [...d.holidays].filter((h) => h.date).sort((a, b) => a.date.localeCompare(b.date))
    .map((h) => (h.armed && h.from && h.to ? { date: h.date, name: h.name.trim(), armed: true, from: h.from, to: h.to } : { date: h.date, name: h.name.trim(), armed: h.armed })),
  soc_group_minutes: d.group.trim() === "" ? null : Math.max(1, Math.round(Number(d.group))),
});

export function MonitoringBox({ site, canEdit, canArm }: { site: Site; canEdit: boolean; canArm: boolean }) {
  const [server, setServer] = useState<Monitoring | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const tz = validTimeZone(site.timezone) ? site.timezone : null;

  const load = useCallback((initial: boolean) => api.monitoring(site.id).then((m) => {
    setServer(m); setError(null);
    if (initial) setDraft(draftOf(m));
  }).catch((e: Error) => { if (initial) setError(e.message.startsWith("404") ? "SOC monitoring isn't available on this hub yet." : e.message); }), [site.id]);
  // the armed state changes on its own (schedule, override expiry): refresh it, never the draft being edited
  useEffect(() => { load(true); const t = setInterval(() => load(false), 30000); return () => clearInterval(t); }, [load]);

  const config = useMemo(() => (draft ? configOf(draft) : null), [draft]);
  if (error) return <div className="card"><p className="muted" style={{ margin: 0 }}>{error}</p></div>;
  if (!server || !draft || !config) return <div className="card"><p className="muted" style={{ margin: 0 }}>Loading…</p></div>;

  const savedConfig = configOf(draftOf(server));
  const dirty = JSON.stringify(config) !== JSON.stringify(savedConfig);
  const badGroup = draft.group.trim() !== "" && !(Number(draft.group) >= 1 && Number(draft.group) <= 240);
  // the hub says what this user may do here (SOC roles widen membership server-side); the props are the fallback
  const mayEdit = server.can_configure ?? canEdit;
  const mayArm = server.can_arm ?? canArm;
  const editable = mayEdit && !!tz;
  const save = async () => {
    setBusy(true);
    try { const m = await api.setMonitoring(site.id, config); setServer(m); setDraft(draftOf(m)); toast.success(m.monitored ? "Monitoring saved" : "Saved: the SOC doesn't monitor this Site"); }
    catch (e) { toast.error(e); } finally { setBusy(false); }
  };
  const setWeek = (week: Week) => setDraft({ ...draft, week });
  const setDay = (d: number, f: (s: Week[number]) => Week[number]) => setWeek(draft.week.map((s, i) => (i === d ? f(s) : s)));

  return (
    <div className="soc-settings">
      <ArmCard site={site} m={server} tz={tz} canArm={mayArm} onChanged={setServer} />
      {mayEdit && (
        <div className="card">
          <label className="row monitor-toggle">
            <input type="checkbox" checked={draft.monitored} disabled={!tz} onChange={(e) => setDraft({ ...draft, monitored: e.target.checked })} />
            <strong>This Site is monitored by the SOC</strong>
          </label>
          {!tz && (
            <p className="hint-box">Set the Site's time zone in <a href={settingsHref(site.id)} onClick={go(settingsHref(site.id))}>General</a> first: arming hours are wall-clock times at the Site.</p>
          )}
          <fieldset className="monitor-editor" disabled={!editable}>
            <h4>Armed hours <span className="muted small">{tz ? `(${tz})` : ""}</span></h4>
            {fromWeek(draft.week).length === 0 && <p className="muted small">No windows: while monitored, the Site is armed around the clock.</p>}
            <div className="arm-week">
              {DAYS.map((name, d) => (
                <div key={name} className="arm-day">
                  <span className="arm-day-name">{name}</span>
                  <div className="arm-spans">
                    {draft.week[d].map((s, k) => (
                      <span key={k} className="arm-span">
                        <input type="time" value={s.from} aria-label={`${name} from`} onChange={(e) => setDay(d, (l) => l.map((x, j) => (j === k ? { ...x, from: e.target.value } : x)))} />
                        <span aria-hidden="true">→</span>
                        <input type="time" value={s.to} aria-label={`${name} to`} onChange={(e) => setDay(d, (l) => l.map((x, j) => (j === k ? { ...x, to: e.target.value } : x)))} />
                        <span className="muted small">{isTime(s.from) && isTime(s.to) && isOvernight(s) ? (windowLabel(s) === "all day" ? "all day" : "next day") : ""}</span>
                        <button type="button" className="ghost small" aria-label="Remove window" onClick={() => setDay(d, (l) => l.filter((_, j) => j !== k))}>✕</button>
                      </span>
                    ))}
                    <button type="button" className="ghost small" onClick={() => setDay(d, (l) => [...l, { from: "18:00", to: "06:00" }])}>+ window</button>
                  </div>
                </div>
              ))}
            </div>
            <div className="row">
              <button type="button" className="ghost small" onClick={() => setWeek(copyDay(draft.week, 0, [1, 2, 3, 4]))}>Copy Mon → Tue–Fri</button>
              <button type="button" className="ghost small" onClick={() => setWeek(copyDay(draft.week, 0, [5, 6]))}>Copy Mon → weekend</button>
              <button type="button" className="ghost small" title="Join overlapping windows" onClick={() => setWeek(draft.week.map(mergeSpans))}>Tidy</button>
            </div>

            <h4>Holidays <span className="muted small">replace the weekly hours for that date</span></h4>
            {draft.holidays.length === 0 && <p className="muted small">None.</p>}
            {draft.holidays.map((h, i) => {
              const setH = (patch: Partial<ArmHoliday>) => setDraft({ ...draft, holidays: draft.holidays.map((x, j) => (j === i ? { ...x, ...patch } : x)) });
              const mode = !h.armed ? "off" : h.from || h.to ? "hours" : "on";
              return (
                <div key={i} className="row holiday-row">
                  <input type="date" value={h.date} aria-label="Date" onChange={(e) => setH({ date: e.target.value })} />
                  <input placeholder="Name (e.g. Christmas)" value={h.name} maxLength={80} onChange={(e) => setH({ name: e.target.value })} />
                  <select value={mode} onChange={(e) => setH(e.target.value === "off" ? { armed: false, from: undefined, to: undefined } : e.target.value === "on" ? { armed: true, from: undefined, to: undefined } : { armed: true, from: "00:00", to: "23:59" })}>
                    <option value="on">Armed all day</option>
                    <option value="off">Disarmed all day</option>
                    <option value="hours">Armed from–to</option>
                  </select>
                  {mode === "hours" && <>
                    <input type="time" value={h.from ?? ""} aria-label="Armed from" onChange={(e) => setH({ from: e.target.value })} />
                    <input type="time" value={h.to ?? ""} aria-label="Armed to" onChange={(e) => setH({ to: e.target.value })} />
                  </>}
                  <button type="button" className="ghost small" aria-label="Remove holiday" onClick={() => setDraft({ ...draft, holidays: draft.holidays.filter((_, j) => j !== i) })}>✕</button>
                </div>
              );
            })}
            <button type="button" className="ghost small" onClick={() => setDraft({ ...draft, holidays: [...draft.holidays, { date: "", name: "", armed: true }] })}>+ holiday</button>

            <h4>Grouping</h4>
            <label className="row small">Events at this Site within
              <input type="number" min={1} max={240} style={{ width: 72 }} placeholder="default" value={draft.group} onChange={(e) => setDraft({ ...draft, group: e.target.value })} />
              minutes of each other join one incident</label>
          </fieldset>
          <div className="row" style={{ marginTop: 12 }}>
            <button disabled={!dirty || busy || badGroup || !tz} onClick={save}>Save</button>
            {dirty && <button className="ghost small" disabled={busy} onClick={() => setDraft(draftOf(server))}>Discard changes</button>}
            {dirty && tz && <DraftPreview config={config} tz={tz} />}
          </div>
        </div>
      )}
    </div>
  );
}

/** What the unsaved schedule would mean right now, so an editor sees the effect before saving. */
function DraftPreview({ config, tz }: { config: MonitoringConfig; tz: string }) {
  const now = Date.now() / 1000;
  const s = isArmedAt({ ...config, arm_override: null }, now, tz);
  return <span className="muted small">With these changes: {config.monitored ? (s.armed ? "armed now" : "disarmed now") : "not monitored"}</span>;
}

/** The server's armed state, the override in force, and Arm now / Disarm now / Clear override. */
function ArmCard({ site, m, tz, canArm, onChanged }: { site: Site; m: Monitoring; tz: string | null; canArm: boolean; onChanged: (m: Monitoring) => void }) {
  const [dur, setDur] = useState<OverrideDuration>("next");
  const [busy, setBusy] = useState(false);
  // an expired override stays stored until someone clears or replaces it; only a live one is worth showing
  const o = m.arm_override && (m.override_active ?? m.arm_override.until > Date.now() / 1000) ? m.arm_override : null;
  const when = (ts: number) => (tz ? fmtInZone(ts, tz) : fmtTime(ts));
  const act = async (mode: "arm" | "disarm") => {
    if (!tz) return;
    const reason = await promptDialog(mode === "arm" ? `Arm ${site.name} now` : `Disarm ${site.name} now`, { label: "Reason (kept in the audit log)", confirmLabel: mode === "arm" ? "Arm" : "Disarm" });
    if (reason == null) return;
    if (!reason.trim()) { toast.error("A reason is required"); return; }
    setBusy(true);
    try { onChanged(await api.arm(site.id, { mode, until: overrideUntil(dur, m, Date.now() / 1000, tz), reason: reason.trim() })); toast.success(mode === "arm" ? "Armed" : "Disarmed"); }
    catch (e) { toast.error(e); } finally { setBusy(false); }
  };
  const clear = async () => {
    setBusy(true);
    try { onChanged(await api.clearArm(site.id)); toast.success("Back on the schedule"); } catch (e) { toast.error(e); } finally { setBusy(false); }
  };
  return (
    <div className={`card arm-card ${m.monitored ? (m.armed ? "armed" : "disarmed") : ""}`}>
      <div className="row">
        <span className={`dot ${m.monitored && m.armed ? "ok" : ""}`} />
        <strong>{!m.monitored ? "Not monitored by the SOC" : m.armed ? "Armed" : "Disarmed"}</strong>
        {m.monitored && m.reason && <span className="muted small">{REASON_LABEL[m.reason] ?? m.reason}</span>}
        {m.monitored && <span className="muted small">{m.next_change ? `· ${m.next_change.armed ? "arms" : "disarms"} ${when(m.next_change.at)}` : "· no change scheduled"}</span>}
      </div>
      {m.monitored && o && (
        <p className="small" style={{ margin: "8px 0 0" }}>
          Manually {o.mode === "arm" ? "armed" : "disarmed"} {o.by ? `by ${o.by} ` : ""}{ago(o.at)}, until {when(o.until)}{o.reason ? `: “${o.reason}”` : ""}
        </p>
      )}
      {m.monitored && canArm && (
        <div className="row" style={{ marginTop: 8 }}>
          <button className="small" disabled={busy || !tz} onClick={() => act("arm")}>Arm now</button>
          <button className="small" disabled={busy || !tz} onClick={() => act("disarm")}>Disarm now</button>
          <select value={dur} disabled={busy || !tz} onChange={(e) => setDur(e.target.value as OverrideDuration)} aria-label="For how long">
            {OVERRIDE_DURATIONS.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
          </select>
          {m.arm_override && <button className="ghost small" disabled={busy} onClick={clear}>Clear override</button>}
        </div>
      )}
    </div>
  );
}
