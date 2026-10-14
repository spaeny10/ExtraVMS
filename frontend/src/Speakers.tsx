/**
 * Settings › Cameras › Speakers (backend talkdown.py, speakers_api.py): IP speakers played over their ONVIF RTSP
 * backchannel, each bound to any number of this server's cameras; the bound cameras' live tiles get the 🔊 Talk-down
 * button. Check asks the speaker (DESCRIBE) whether it offers a backchannel and which codecs. Below: the talk-down
 * voice (tts.py: set per Site on the hub when this server is enrolled, here otherwise) and the message library
 * (uploaded recordings, and texts spoken by the server's text-to-speech engine when it has one).
 */
import { useEffect, useRef, useState } from "react";
import { api, type Camera, type Speaker, type SpeakerCodec, type TalkdownLibrary, type TalkdownVoiceState, type TalkdownVoices } from "./api";
import { fileToBase64, uploadProblem } from "./talkdown";
import { SPEED_MAX, SPEED_MIN, type VoiceSetting, enginesText, optionText, predict, sameSetting, speedText, voiceName, voiceOptions } from "./talkdownVoice";
import { confirmDialog, errorText, promptDialog, toast } from "./ui";
import { VoicePreview } from "./VoicePreview";

type Draft = Omit<Speaker, "check" | "checked_at" | "status"> & { password?: string };
const blank: Draft = { id: "", name: "", host: "", rtsp_port: 554, onvif_port: 80, path: "/main", username: "admin", codec: "PCMU",
  volume_note: "", camera_ids: [], enabled: true };

/** "PCMU, PCMA, G726-16 · sends PCMU", or what went wrong, for the last Check. */
export function checkText(s: Pick<Speaker, "check" | "checked_at">): string {
  const c = s.check;
  if (!c || !s.checked_at) return "not checked";
  if (c.ok) return `backchannel ${c.codecs.join(", ")} · sends ${c.codec}`;
  return c.error ?? (c.online ? "answers, no backchannel" : "no answer");
}

export function SpeakersSection({ cameras }: { cameras: Camera[] }) {
  const [list, setList] = useState<Speaker[] | null>(null);
  const [edit, setEdit] = useState<Draft | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const reload = () => api.speakers().then(setList).catch((e) => { setList([]); toast.error(e); });
  useEffect(() => { reload(); }, []);
  const exists = (id: string) => !!list?.some((s) => s.id === id);
  const camName = (id: string) => cameras.find((c) => c.id === id)?.name ?? id;

  const save = async () => {
    if (!edit) return;
    setErr("");
    try {
      await api.saveSpeaker({ ...edit, password: edit.password || undefined });
      toast.success(`Speaker "${edit.name}" saved`);
      setEdit(null);
      reload();
    } catch (e) {
      setErr(errorText(e));
    }
  };
  const check = async (s: Speaker) => {
    setBusy(s.id);
    try {
      const r = await api.checkSpeaker(s.id);
      if (r.ok) toast.success(`${s.name}: backchannel ${r.codecs.join(", ")}`);
      else toast.error(`${s.name}: ${r.error ?? "no backchannel"}`);
      reload();
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(null);
    }
  };
  const remove = async (s: Speaker) => {
    if (!await confirmDialog(`Delete the speaker "${s.name}"?`, { message: "Its cameras lose the Talk-down button.", confirmLabel: "Delete", danger: true })) return;
    try {
      await api.deleteSpeaker(s.id);
      reload();
    } catch (e) {
      toast.error(e);
    }
  };
  const nextId = () => { let n = 1; while (exists(`spk${n}`)) n++; return `spk${n}`; };
  const toggleCam = (id: string, on: boolean) => edit && setEdit({ ...edit, camera_ids: on ? [...edit.camera_ids.filter((c) => c !== id), id] : edit.camera_ids.filter((c) => c !== id) });

  return (
    <section className="speakers">
      <div className="toolbar">
        <h3>Speakers</h3>
        <span className="muted small">IP speakers for talk-down, played from the live view of the cameras they are bound to.</span>
        <span className="spacer" />
        <button onClick={() => setEdit({ ...blank, id: nextId() })}>Add speaker</button>
      </div>
      {list && list.length > 0 && (
        <table className="table">
          <thead><tr><th>Speaker</th><th>Address</th><th>Bound cameras</th><th>Backchannel</th><th>Now</th><th /></tr></thead>
          <tbody>
            {list.map((s) => (
              <tr key={s.id} className={s.enabled ? "" : "muted"}>
                <td><strong>{s.name}</strong> <span className="muted small">{s.id}</span>{s.volume_note && <div className="muted small">{s.volume_note}</div>}</td>
                <td>{s.host}:{s.rtsp_port}{s.path}</td>
                <td>{s.camera_ids.length ? s.camera_ids.map(camName).join(", ") : <span className="muted small">none: no Talk-down button anywhere</span>}</td>
                <td><span className={`dot ${s.check?.ok ? "ok" : s.checked_at ? "bad" : "idle"}`} /> <span className="small" title={s.checked_at ? `Checked ${new Date(s.checked_at * 1000).toLocaleString()}` : undefined}>{checkText(s)}</span>{" "}
                  <button className="ghost small" disabled={busy === s.id} onClick={() => check(s)} title="Ask the speaker now (RTSP DESCRIBE with the backchannel header)">{busy === s.id ? "…" : "Check"}</button></td>
                <td className="small">{s.status.state === "idle" ? <span className="muted">idle</span> : `playing '${s.status.message}'`}</td>
                <td className="row">
                  <button className="ghost small" onClick={() => setEdit({ ...s })}>Edit</button>
                  <button className="ghost small" onClick={() => remove(s)}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {list && list.length === 0 && <p className="muted small">No speakers yet. Add an ONVIF speaker (Profile T audio output, e.g. a Tonmind IP speaker) and bind it to the cameras that see its area.</p>}
      <VoiceSection />
      <MessageLibrary />
      {edit && (
        <div className="modal-backdrop" onClick={() => setEdit(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <h2>{exists(edit.id) ? `Edit ${edit.name}` : "Add speaker"}</h2>
              <button className="ghost" onClick={() => setEdit(null)} aria-label="Close">✕</button>
            </header>
            <div className="form">
              <div className="row">
                <Field label="ID"><input value={edit.id} disabled={exists(edit.id)} onChange={(e) => setEdit({ ...edit, id: e.target.value })} /></Field>
                <Field label="Name"><input value={edit.name} placeholder="e.g. Back wall speaker" onChange={(e) => setEdit({ ...edit, name: e.target.value })} /></Field>
              </div>
              <Field label="Host / IP"><input value={edit.host} onChange={(e) => setEdit({ ...edit, host: e.target.value })} /></Field>
              <div className="row">
                <Field label="RTSP port"><input type="number" value={edit.rtsp_port} onChange={(e) => setEdit({ ...edit, rtsp_port: +e.target.value })} /></Field>
                <Field label="ONVIF port"><input type="number" value={edit.onvif_port} onChange={(e) => setEdit({ ...edit, onvif_port: +e.target.value })} /></Field>
                <Field label="RTSP path"><input value={edit.path} onChange={(e) => setEdit({ ...edit, path: e.target.value })} /></Field>
              </div>
              <div className="row">
                <Field label="Username"><input value={edit.username} onChange={(e) => setEdit({ ...edit, username: e.target.value })} /></Field>
                <Field label="Password"><input type="password" placeholder={exists(edit.id) ? "unchanged" : ""} autoComplete="new-password" value={edit.password ?? ""}
                  onChange={(e) => setEdit({ ...edit, password: e.target.value })} /></Field>
              </div>
              <div className="row">
                <Field label="Codec">
                  <select value={edit.codec} onChange={(e) => setEdit({ ...edit, codec: e.target.value as SpeakerCodec })}>
                    <option value="PCMU">G.711 µ-law (PCMU)</option>
                    <option value="PCMA">G.711 A-law (PCMA)</option>
                  </select>
                </Field>
                <Field label="Enabled"><input type="checkbox" checked={edit.enabled} onChange={(e) => setEdit({ ...edit, enabled: e.target.checked })} /></Field>
              </div>
              <Field label="Volume note"><input value={edit.volume_note} maxLength={200} placeholder="e.g. volume 80% on the speaker's web page"
                onChange={(e) => setEdit({ ...edit, volume_note: e.target.value })} /></Field>
              <div className="field">
                <span>Bound cameras</span>
                <div className="speaker-cams">
                  {cameras.filter((c) => c.enabled || edit.camera_ids.includes(c.id)).map((c) => (
                    <label key={c.id} className="row small">
                      <input type="checkbox" checked={edit.camera_ids.includes(c.id)} onChange={(e) => toggleCam(c.id, e.target.checked)} />
                      {c.name} <span className="muted">{c.id}</span>
                    </label>
                  ))}
                </div>
                <span className="small">Each bound camera's live view (here and on the hub) gets the 🔊 Talk-down button, so the operator watches the person while speaking. A camera may have several speakers: its panel then offers each one and All speakers.</span>
              </div>
              <span className="small">The speaker must offer an ONVIF audio backchannel on RTSP (sendonly audio, G.711). Save, then Check.</span>
              {err && <p className="error">{err}</p>}
              <div className="row"><button onClick={save}>Save</button></div>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

/**
 * The talk-down voice: typed messages, Describe & warn and library texts without their own voice speak with it. A
 * hub-enrolled server shows it read-only (the hub sets it per Site and pushes it here); a server without a hub edits it.
 * Hidden on an older server, or for a hub viewer (reading it needs operator).
 */
function VoiceSection() {
  const [state, setState] = useState<TalkdownVoiceState | null>(null);
  const [voices, setVoices] = useState<TalkdownVoices | null>(null);
  const [draft, setDraft] = useState<VoiceSetting | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    Promise.all([api.talkdownVoice(), api.talkdownVoices()])
      .then(([s, v]) => { setState(s); setVoices(v); setDraft(s.setting); })
      .catch(() => setState(null));
  }, []);
  if (!state || !voices || !draft) return null;
  const here = { name: "this server", voices: voices.voices.map((v) => v.id), engines: voices.engines };
  const editable = !state.managed;
  const pred = predict(draft.voice, here);
  const save = async () => {
    setBusy(true);
    try {
      const s = await api.setTalkdownVoice(draft);
      setState(s); setDraft(s.setting);
      toast.success(`Talk-down voice: ${voiceName(s.setting.voice)}`);
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="talk-voice">
      <h3 className="spaced">Talk-down voice</h3>
      <p className="muted small">
        Typed messages, Describe &amp; warn and library texts speak with this voice.{" "}
        {state.managed ? "This server is managed by the hub: the voice is set per Site on the hub (Site › Settings › Cameras)." : ""}
      </p>
      <div className="row talk-voice-form">
        <label className="field">
          <span>Voice</span>
          <select value={draft.voice ?? ""} disabled={!editable} onChange={(e) => setDraft({ ...draft, voice: e.target.value || null })}>
            {voiceOptions([here]).map((o) => <option key={o.id ?? ""} value={o.id ?? ""}>{optionText(o, 1)}</option>)}
          </select>
        </label>
        <label className="field">
          <span>Speed · {speedText(draft.speed)}</span>
          <input type="range" min={SPEED_MIN} max={SPEED_MAX} step={0.05} value={draft.speed} disabled={!editable}
            onChange={(e) => setDraft({ ...draft, speed: +e.target.value })} />
        </label>
        <label className="row small">
          <input type="checkbox" checked={draft.chime} disabled={!editable} onChange={(e) => setDraft({ ...draft, chime: e.target.checked })} /> Attention chime first
        </label>
      </div>
      <p className="small">
        {pred.ok ? `Speaks with ${voiceName(pred.voice)}.` : `${voiceName(draft.voice)}: ${pred.text}.`}{" "}
        <span className="muted">Engines here: {enginesText(voices.engines)}.
          {!voices.engines.piper && " Piper isn't installed: run tools/install_piper_voices.py on this server."}
          {voices.engines.piper && !voices.voices.length && ` No Piper voice yet: run tools/install_piper_voices.py (folder ${voices.voices_dir}).`}</span>
      </p>
      <div className="row">
        <VoicePreview requested={draft.voice} preview={() => api.previewVoice({ voice: draft.voice, speed: draft.speed, chime: draft.chime })} />
        {editable && <button className="small" disabled={busy || sameSetting(draft, state.setting)} onClick={save}>Save</button>}
        {state.set_by && <span className="muted small">set by {state.set_by}{state.set_at ? ` · ${new Date(state.set_at * 1000).toLocaleString()}` : ""}</span>}
      </div>
    </div>
  );
}

/** The talk-down message library: play order as listed; texts need the server's text-to-speech engine. */
function MessageLibrary() {
  const [lib, setLib] = useState<TalkdownLibrary | null>(null);
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const [voice, setVoice] = useState("");   // a new text's own voice; "" = the Site's
  const [installed, setInstalled] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const reload = () => api.talkdownMessages().then(setLib).catch((e) => toast.error(e));
  useEffect(() => { reload(); }, []);
  useEffect(() => { api.talkdownVoices().then((v) => setInstalled(v.voices.map((x) => x.id))).catch(() => setInstalled([])); }, []);
  const addText = async () => {
    setBusy(true);
    try {
      await api.addTalkdownMessage({ name: name.trim() || text.trim().slice(0, 40), text, ...(voice ? { voice } : {}) });
      setName(""); setText("");
      reload();
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  const upload = async (f: File | undefined) => {
    if (!f) return;
    const problem = uploadProblem(f.name, f.size, lib?.max_upload, lib?.types);
    if (problem) { toast.error(problem); return; }
    setBusy(true);
    try {
      const m = await api.addTalkdownMessage({ name: name.trim() || f.name.replace(/\.[^.]+$/, ""), upload: { filename: f.name, data_b64: await fileToBase64(f) } });
      toast.success(`"${m.name}" added (${Math.round(m.duration_s ?? 0)} s)`);
      setName("");
      reload();
    } catch (e) {
      toast.error(e);
    } finally {
      setBusy(false);
    }
  };
  const rename = async (id: number, old: string) => {
    const n = (await promptDialog("Rename message", { initial: old, confirmLabel: "Rename" }))?.trim();
    if (!n || n === old) return;
    try { await api.renameTalkdownMessage(id, n); reload(); } catch (e) { toast.error(e); }
  };
  const remove = async (id: number, n: string) => {
    if (!await confirmDialog(`Delete the message "${n}"?`, { confirmLabel: "Delete", danger: true })) return;
    try { await api.deleteTalkdownMessage(id); reload(); } catch (e) { toast.error(e); }
  };
  if (!lib) return null;
  return (
    <div className="talk-library">
      <h3 className="spaced">Talk-down messages</h3>
      <p className="muted small">
        {lib.tts.available ? `Texts are spoken by this server's text-to-speech (${lib.tts.engine}); operators can also type a message on the live view.`
          : "This server has no text-to-speech engine: text messages can't be played and the live view has no Say field. Upload recordings instead."}
        {" "}Recordings: {lib.types.join(", ")}, at most {Math.round(lib.max_upload / (1024 * 1024))} MB; anything past {lib.max_s} s is cut.
      </p>
      <ul className="talk-lib-list">
        {lib.messages.map((m) => (
          <li key={m.id} className={`row small ${m.available ? "" : "muted"}`}>
            <strong>{m.name}</strong>
            <span className="muted">{m.kind === "tts" ? `“${m.text}”${m.available ? "" : " (needs text-to-speech)"}` : `recording · ${Math.round(m.duration_s ?? 0)} s`}</span>
            {m.kind === "tts" && (m.voice || m.speed) && <span className="chip small" title="This text's own voice (others use the Site's)">
              {voiceName(m.voice ?? null) === "Automatic" ? "Site voice" : voiceName(m.voice ?? null)}{m.speed ? ` · ${m.speed}×` : ""}</span>}
            <span className="spacer" />
            <button className="ghost small" onClick={() => rename(m.id, m.name)}>Rename</button>
            <button className="ghost small" onClick={() => remove(m.id, m.name)}>Delete</button>
          </li>
        ))}
      </ul>
      <div className="row talk-lib-add">
        <input value={name} placeholder="Name, e.g. Leave now" maxLength={80} onChange={(e) => setName(e.target.value)} />
        {lib.tts.available && <input value={text} placeholder="Text to speak" maxLength={500} onChange={(e) => setText(e.target.value)} />}
        {lib.tts.available && installed.length > 0 && (
          <select value={voice} onChange={(e) => setVoice(e.target.value)} title="The voice of this text (others use the Site's talk-down voice)">
            <option value="">Site voice</option>
            {installed.map((id) => <option key={id} value={id}>{voiceName(id)}</option>)}
          </select>
        )}
        {lib.tts.available && <button className="small" disabled={busy || !text.trim()} onClick={addText}>Add text</button>}
        <button className="ghost small" disabled={busy} onClick={() => fileRef.current?.click()}>Upload recording…</button>
        <input ref={fileRef} type="file" hidden accept={lib.types.join(",")} onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; upload(f); }} />
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
    </label>
  );
}
