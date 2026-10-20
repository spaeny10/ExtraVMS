/* Settings › Cameras › Edit › Rules & speakers: the camera's automatic deterrence rules (backend deterrence.py) and the
   one-click "use this camera's own speaker for talk-down". A rule fires on a verified event (people and/or vehicles,
   anywhere or in one named zone, always or only while the Site's SOC monitoring is armed): the camera's strobe, siren
   or light for some seconds, and optionally a talk-down message on a linked speaker, then waits its cooldown. No rules
   = off, the default. The rules save with their own button (a PUT of the whole list); the checks are deterrence.ts.
   With them, the camera's siren setting: which way "Siren" sounds (automatic: the camera's own siren only when it can make a
   sound, else the built-in siren on a linked talk-down speaker; or pinned to one of them), and which way it goes now. */
import { useEffect, useState } from "react";
import { api, type Camera, type Speaker } from "./api";
import { DETER_ACTIONS, MAX_COOLDOWN_S, MAX_RULES, MAX_S, type DeterAction, type DeterRule, type DeterRules, type SirenSource, actionName, armedText,
  errorStatus, newRule, normalizeRules, offerCameraSpeaker, ruleProblems, rulesDirty, rulesHaveNothing, rulesProblems, sirenSourceChoices,
  sirenSourceName, sirenWayText } from "./deterrence";
import { errorText, toast } from "./ui";

export function DeterrenceRulesEditor({ camera }: { camera: Camera }) {
  const [data, setData] = useState<DeterRules | null>(null);
  const [edit, setEdit] = useState<DeterRule[]>([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [source, setSource] = useState<SirenSource>("auto");
  const [seq, setSeq] = useState(0);   // bumped to read again (after asking the camera again)
  const take = (r: DeterRules) => { setData(r); setEdit(r.rules); setSource(r.config?.siren_source ?? "auto"); };
  useEffect(() => {
    let alive = true;
    api.deterrenceRules(camera.id).then((r) => { if (alive) take(r); })
      .catch((e) => { if (alive) setErr(errorStatus(e) === 503 ? "The camera hasn't been asked what it can drive yet; open this tab again in a few seconds." : errorText(e)); });
    return () => { alive = false; };
  }, [camera.id, seq]);

  if (err) return <p className="small error">{err}</p>;
  if (!data) return <p className="muted small">Reading the rules…</p>;
  if (rulesHaveNothing(data) && !data.rules.length) {
    return <p className="muted small">This camera has no strobe, siren or light, and no talk-down speaker is linked to it: there is nothing for a rule to do.</p>;
  }
  const armed = armedText(data.armed);
  const problems = rulesProblems(edit, data.caps);
  const saved = data.config?.siren_source ?? "auto";
  const sourceDirty = !!data.config && source !== saved;
  const dirty = rulesDirty(data.rules, edit) || sourceDirty;
  const set = (i: number, p: Partial<DeterRule>) => setEdit(edit.map((r, k) => (k === i ? { ...r, ...p } : r)));
  const recheck = async () => {
    setBusy(true);
    try {
      await api.deterrence(camera.id, true);
      setSeq((n) => n + 1);
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  const save = async () => {
    setBusy(true);
    try {
      take(await api.saveDeterrenceRules(camera.id, normalizeRules(edit), sourceDirty ? { siren_source: source } : undefined));
      toast.success(`${camera.name}: deterrence rules saved`);
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <p className="small"><span className={`dot ${armed.tone}`} aria-hidden="true" /> {armed.text}</p>
      {data.config && (
        <div className="row small">
          <span>Siren sounds through</span>
          <select value={source} aria-label="Siren sounds through" onChange={(e) => setSource(e.target.value as SirenSource)}
            title="Automatic: the camera's own siren only when it can make a sound (a Milesight with a sound file loaded), else the built-in siren on a talk-down speaker linked to the camera">
            {sirenSourceChoices(data.siren, saved).map((s) => <option key={s} value={s}>{sirenSourceName(s)}</option>)}
          </select>
          <span className="muted">{sourceDirty ? "Save to apply." : sirenWayText(data.siren)}</span>
          <button className="ghost small" disabled={busy} onClick={recheck}
            title="Ask the camera again what it can drive (after loading a siren sound file on it)">Check again</button>
        </div>
      )}
      {!edit.length && <p className="muted small">No rules: nothing switches on by itself on this camera.</p>}
      {edit.map((r, i) => (
        <RuleCard key={i} r={r} data={data} problems={ruleProblems(r, data.caps)} onChange={(p) => set(i, p)}
          onRemove={() => setEdit(edit.filter((_, k) => k !== i))} />
      ))}
      <div className="row">
        <button className="ghost small" disabled={edit.length >= MAX_RULES} onClick={() => setEdit([...edit, newRule(edit)])}
          title={edit.length >= MAX_RULES ? `At most ${MAX_RULES} rules per camera` : "A rule for people, only while the Site is armed; pick what it switches on"}>+ Rule</button>
        <span className="spacer" />
        {dirty && <button className="ghost small" disabled={busy} onClick={() => { setEdit(data.rules); setSource(saved); }}>Undo</button>}
        <button className="small" disabled={busy || !dirty || problems.length > 0} onClick={save}
          title={problems.length ? problems.join("; ") : undefined}>{busy ? "Saving…" : "Save rules"}</button>
      </div>
      <p className="hint small">Rules fire only on verified events (YOLO confirmed the person or vehicle). After firing, a rule waits its cooldown before it fires again.</p>
    </>
  );
}

/** A number field's value: a cleared field is NaN in the draft (flagged by ruleProblems) and shows empty. */
const num = (v: number) => (Number.isFinite(v) ? v : "");

function RuleCard({ r, data, problems, onChange, onRemove }: {
  r: DeterRule; data: DeterRules; problems: string[]; onChange: (p: Partial<DeterRule>) => void; onRemove: () => void;
}) {
  const flip = <T,>(list: T[], v: T, on: boolean) => (on ? [...list.filter((x) => x !== v), v] : list.filter((x) => x !== v));
  // a zone or speaker the camera no longer has stays listed, so the select shows what is saved
  const zones = [...new Set([...data.zones, ...(r.zone ? [r.zone] : [])])];
  return (
    <div className={`deter-rule ${r.enabled ? "" : "off"}`}>
      <div className="row">
        <label className="row small" title="A rule switched off keeps its settings"><input type="checkbox" checked={r.enabled} onChange={(e) => onChange({ enabled: e.target.checked })} /> On</label>
        <input value={r.name} maxLength={60} aria-label="Rule name" placeholder="Rule name" onChange={(e) => onChange({ name: e.target.value })} />
        <span className="spacer" />
        <button className="ghost small" title="Remove rule" aria-label={`Remove ${r.name}`} onClick={onRemove}>✕</button>
      </div>
      <div className="row small">
        <span>When</span>
        {(["person", "vehicle"] as const).map((l) => (
          <label key={l} className="row small"><input type="checkbox" checked={r.labels.includes(l)} onChange={(e) => onChange({ labels: flip(r.labels, l, e.target.checked) })} />
            {l === "person" ? "People" : "Vehicles"}</label>
        ))}
        <select value={r.zone ?? ""} aria-label="Where" onChange={(e) => onChange({ zone: e.target.value || null })}>
          <option value="">anywhere in the picture</option>
          {zones.map((z) => <option key={z} value={z}>in {z}</option>)}
        </select>
        <select value={r.when} aria-label="When" onChange={(e) => onChange({ when: e.target.value as DeterRule["when"] })}>
          <option value="armed">only while the Site is armed</option>
          <option value="always">at any time</option>
        </select>
      </div>
      <div className="row small">
        <span>Switch on</span>
        {DETER_ACTIONS.map((a: DeterAction) => (
          <label key={a} className={`row small ${data.caps[a] ? "" : "muted"}`} title={data.caps[a] ? undefined : `This camera has no ${actionName(a).toLowerCase()}`}>
            <input type="checkbox" checked={r.actions.includes(a)} disabled={!data.caps[a] && !r.actions.includes(a)}
              onChange={(e) => onChange({ actions: flip(r.actions, a, e.target.checked) })} /> {actionName(a)}
          </label>
        ))}
        <span>for</span>
        <input type="number" min={1} max={MAX_S} value={num(r.seconds)} aria-label="Seconds on" onChange={(e) => onChange({ seconds: e.target.value === "" ? NaN : +e.target.value })} />
        <span>s</span>
      </div>
      {(data.speakers.length > 0 || r.speaker_id) && (
        <div className="row small">
          <span>Talk-down</span>
          <select value={r.speaker_id ?? ""} aria-label="Speaker" onChange={(e) => onChange({ speaker_id: e.target.value || null, ...(e.target.value ? {} : { message_id: null }) })}>
            <option value="">none</option>
            {[...data.speakers, ...(r.speaker_id && !data.speakers.some((s) => s.id === r.speaker_id) ? [{ id: r.speaker_id, name: r.speaker_id }] : [])]
              .map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
          {r.speaker_id && (
            <select value={r.message_id ?? ""} aria-label="Message" onChange={(e) => onChange({ message_id: e.target.value === "" ? null : Number(e.target.value) })}>
              <option value="">pick a message…</option>
              {data.messages.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
            </select>
          )}
        </div>
      )}
      <div className="row small">
        <span>Then wait</span>
        <input type="number" min={0} max={MAX_COOLDOWN_S} value={num(r.cooldown_s)} aria-label="Cooldown seconds" onChange={(e) => onChange({ cooldown_s: e.target.value === "" ? NaN : +e.target.value })} />
        <span>s before firing again</span>
      </div>
      {problems.map((p) => <p key={p} className="small error">{p}</p>)}
    </div>
  );
}

/**
 * "Use this camera's speaker for talk-down": a camera with an audio output (as the deterrence probe found) and no
 * speaker on its address linked to it gets a button that adds one (same host, ports and login, copied on the server;
 * the camera stays a camera). `speakers`: the speakers linked to this camera.
 */
export function CameraSpeakerOffer({ camera, speakers, onAdded }: { camera: Camera; speakers: Speaker[]; onAdded: () => void }) {
  const [audio, setAudio] = useState<{ audio_outputs: number } | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let alive = true;
    api.deterrence(camera.id).then((d) => { if (alive) setAudio(d); }).catch(() => {});   // not probed / none: no offer
    return () => { alive = false; };
  }, [camera.id]);
  if (!offerCameraSpeaker(audio, speakers, camera)) return null;
  const add = async () => {
    setBusy(true);
    try {
      const s = await api.cameraToSpeaker(camera.id, { disable_camera: false });
      toast.success(`Speaker ${s.id} added for ${camera.name}`);
      onAdded();
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="row">
      <button className="ghost small" disabled={busy} onClick={add}
        title="Adds a speaker on this camera's address and login, linked to this camera; the camera keeps recording">{busy ? "Adding…" : "Use this camera's speaker for talk-down"}</button>
      <span className="muted small">The camera has an audio output.</span>
    </div>
  );
}
