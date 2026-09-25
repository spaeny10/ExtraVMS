import { useEffect, useRef, useState } from "react";
import {
  api, askClip, fmtDuration, fmtTime, media,
  type ChatMessage, type Feedback, type Journey, type NvrEvent, type Synopsis, type Threat, type Verdict,
} from "./api";
import { StatusBadge, placeholder } from "./Events";
import { useNav } from "./nav";

const REASONS = ["Wrong object", "Missed detail", "Made something up", "Threat too high", "Threat too low", "Too vague"];
const VERDICTS: [Verdict, string][] = [["correct", "Correct detection"], ["false_alarm", "False alarm"], ["wrong_class", "Wrong class"]];
const THREATS: Threat[] = ["none", "low", "medium", "high"];

export function EventDetail({ id: initialId, cameraName, onClose }: { id: number; cameraName: (id: string) => string; onClose: () => void }) {
  const [id, setId] = useState(initialId); // the viewer can step to another camera's sighting of the same person
  useEffect(() => setId(initialId), [initialId]);
  const [e, setE] = useState<NvrEvent | null>(null);
  const [tab, setTab] = useState<"details" | "chat">("details");
  const [chat, setChat] = useState<ChatMessage[]>([]);
  const video = useRef<HTMLVideoElement>(null);
  const { openInTimeline } = useNav();

  useEffect(() => {
    api.event(id).then(setE);
    api.chat(id).then(setChat).catch(() => {});
  }, [id]);
  useEffect(() => {
    const onKey = (k: KeyboardEvent) => k.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  // Poll while the pipeline is still working on this event.
  useEffect(() => {
    if (!e || (e.status !== "open" && e.status !== "pending" && !(e.status === "verified" && !e.synopsis && e.camera_class === "person"))) return;
    const t = setInterval(() => api.event(id).then(setE), 3000);
    return () => clearInterval(t);
  }, [e, id]);

  if (!e) return null;
  const crops = e.detections?.keyframes?.filter((k) => k.kind === "crop") ?? [];
  const seek = (t: number) => {
    if (video.current) {
      video.current.currentTime = t;
      video.current.pause();
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(ev) => ev.stopPropagation()}>
        <header className="modal-head">
          <div>
            <h2>{cameraName(e.camera_id)} · {e.yolo_class ?? e.camera_class}</h2>
            <span className="muted">{fmtTime(e.start_ts)} · {fmtDuration(e)} · event #{e.id}</span>
          </div>
          <div className="row">
            <StatusBadge e={e} />
            {e.feedback?.verdict === "false_alarm" && <span className="badge status-error">Marked false alarm</span>}
            <button className="ghost" onClick={() => { openInTimeline(e); onClose(); }} title="Show this event on the Timeline, playing from just before it">⏱ Open in Timeline</button>
            <LockControl e={e} onChange={() => api.event(e.id).then(setE)} />
            <button className="ghost" onClick={() => api.reprocess(e.id).then(onClose)} title="Run YOLO verification and the synopsis again">Reprocess</button>
            <button className="ghost" onClick={onClose} aria-label="Close">✕</button>
          </div>
        </header>
        <div className="modal-grid">
          <div>
            {e.clip ? (
              <video ref={video} className="clip" src={media(e, "clip.mp4")} poster={e.snapshot ? media(e, "snapshot.jpg") : undefined} controls autoPlay muted />
            ) : e.snapshot ? (
              <>
                <img className="clip" src={media(e, "snapshot.jpg")} alt="" />
                {e.status !== "open" && e.status !== "pending" && (
                  <p className="muted small">The clip for this event has expired under the retention policy; only the snapshot remains.</p>
                )}
              </>
            ) : (
              <div className="clip thumb-empty">No media yet</div>
            )}
            {crops.length > 0 && (
              <div className="crops">
                {crops.map((k) => <img key={k.file} src={media(e, k.file)} alt="" />)}
              </div>
            )}
          </div>
          <aside className="detail-side">
            <div className="segmented">
              <button className={tab === "details" ? "active" : ""} onClick={() => setTab("details")}>Details</button>
              <button className={tab === "chat" ? "active" : ""} onClick={() => setTab("chat")}>
                Ask about this clip{chat.length ? ` (${chat.filter((m) => m.role === "user").length})` : ""}
              </button>
            </div>
            {tab === "details" ? (
              <>
                <JourneySection e={e} cameraName={cameraName} onShow={(eid) => setId(eid)}
                  onOpenTimeline={(members) => { openInTimeline({ ...e, members }); onClose(); }} />
                <Details e={e} setE={setE} notes={chat.filter((m) => m.saved)} onUnsave={(m) => api.saveNote(e.id, m.id, false).then(setChat)} seek={seek} />
              </>
            ) : (
              <ChatPanel e={e} chat={chat} setChat={setChat} video={video} seek={seek} />
            )}
          </aside>
        </div>
      </div>
    </div>
  );
}

/** Cross-camera journey: the same person seen on neighbouring cameras (re-ID + Qwen confirmed). */
function JourneySection({ e, cameraName, onShow, onOpenTimeline }: {
  e: NvrEvent; cameraName: (id: string) => string; onShow: (id: number) => void;
  onOpenTimeline: (members: { id: number; cam: string; start: number; end: number }[]) => void;
}) {
  const [j, setJ] = useState<Journey | null>(null);
  const load = () => api.eventJourney(e.id).then(setJ).catch(() => setJ(null));
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [e.id, e.journey_id]);
  if (!j) return null;
  const first = j.events[0]?.start_ts ?? 0;
  const linkInto = (id: number) => j.links.find((l) => l.b === id);
  return (
    <section className="journey">
      <div className="section-head">
        <h3>🔗 Seen on other cameras</h3>
        <button className="ghost small" onClick={() => onOpenTimeline(j.events.map((m) => ({ id: m.id, cam: m.camera_id, start: m.start_ts, end: m.end_ts ?? m.start_ts })))}
          title="Play the whole journey on the Timeline with these cameras side by side">⏱ Open journey on Timeline</button>
      </div>
      <p>{j.synopsis ?? <span className="muted">{j.dirty ? "Qwen will describe the whole journey shortly…" : "No journey description."}</span>}</p>
      <ol className="journey-list">
        {j.events.map((m) => {
          const link = linkInto(m.id);
          return (
            <li key={m.id} className={m.id === e.id ? "current" : ""}>
              <button className="journey-thumb" onClick={() => m.id !== e.id && onShow(m.id)} title={m.id === e.id ? "This event" : "Show this sighting"}>
                {m.snapshot ? <img src={media(m, "snapshot.jpg")} alt="" /> : <span className="thumb-empty">{m.camera_class}</span>}
              </button>
              <div className="journey-info">
                <div><strong>{cameraName(m.camera_id)}</strong> <span className="muted small">{fmtTime(m.start_ts)}{m.start_ts > first ? ` · +${Math.round(m.start_ts - first)} s` : ""}</span></div>
                {link && (
                  <div className="small muted">
                    Same person ({link.confidence ?? "?"}, re-ID {link.sim.toFixed(2)}, {link.gap_s >= 0 ? `${Math.round(link.gap_s)} s walk` : "overlapping views"}): {link.reason}
                    <button className="linkish" onClick={async () => { await api.rejectLink(link.id); load(); }}>Not the same person</button>
                  </div>
                )}
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

/** Lock keeps this event's footage (with padding) forever, exempt from retention. */
function LockControl({ e, onChange }: { e: NvrEvent; onChange: () => void }) {
  const [asking, setAsking] = useState(false);
  const [note, setNote] = useState("");
  if (e.lock) {
    return (
      <button className="ghost lock-on" title={`Locked${e.lock.note ? `: ${e.lock.note}` : ""}. Footage is kept until unlocked.`}
        onClick={() => { if (confirm("Unlock? The footage will follow the normal retention policy again.")) api.unlockEvent(e.id).then(onChange); }}>
        🔒 Locked
      </button>
    );
  }
  if (asking) {
    return (
      <form className="row lock-form" onSubmit={(ev) => { ev.preventDefault(); api.lockEvent(e.id, note).then(() => { setAsking(false); onChange(); }); }}>
        <input autoFocus placeholder="Reason (optional), e.g. police report #123" value={note} onChange={(ev) => setNote(ev.target.value)} />
        <button type="submit">Lock</button>
        <button type="button" className="ghost" onClick={() => setAsking(false)}>Cancel</button>
      </form>
    );
  }
  return <button className="ghost" onClick={() => setAsking(true)} title="Keep this footage forever, regardless of retention">🔓 Lock</button>;
}

/* ------------------------------------------------------------------ details tab */

function Details({ e, setE, notes, onUnsave, seek }: {
  e: NvrEvent; setE: (e: NvrEvent) => void; notes: ChatMessage[]; onUnsave: (m: ChatMessage) => void; seek: (t: number) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [showOriginal, setShowOriginal] = useState(false);
  const s = e.synopsis_json;
  const canGenerate = Boolean(e.detections?.keyframes?.length);

  return (
    <>
      <section>
        <div className="section-head">
          <h3>Synopsis {e.corrected_at ? <span className="badge">Corrected</span> : null}</h3>
          <div className="row">
            {s && !editing && <button className="ghost small" onClick={() => setEditing(true)}>Edit</button>}
            {!s && !editing && <button className="ghost small" onClick={() => setEditing(true)}>Write one</button>}
            {canGenerate && !editing && (
              <button className="ghost small" onClick={() => {
                if (e.corrected_at && !confirm("Replace your corrected synopsis with a new Qwen synopsis?")) return;
                api.generateSynopsis(e.id).then(() => api.event(e.id).then(setE));
              }}>{s ? "Regenerate" : "Generate with Qwen"}</button>
            )}
          </div>
        </div>
        {editing ? (
          <SynopsisEditor initial={s ?? blankSynopsis(e)} onCancel={() => setEditing(false)} onSave={async (next) => {
            setE(await api.correctSynopsis(e.id, next));
            setEditing(false);
          }} />
        ) : (
          <>
            <p>{e.synopsis ?? <span className="muted">{placeholder(e)}</span>}</p>
            {s?.activity && <p className="muted">{s.activity}</p>}
            {s?.model && <p className="muted small model-tag" title="Which Qwen model wrote this synopsis">by {s.model}</p>}
            {s?.threat_reason && <p><strong>Threat ({s.threat_level}):</strong> {s.threat_reason}</p>}
            {e.anomaly_json?.reasons?.length ? (
              <p className="unusual-why" title="From what this camera normally sees (learned from the last 4 weeks)">
                <strong>⚠ Why this stands out:</strong> {e.anomaly_json.reasons.join("; ")}.
                {e.priority && e.priority !== "none" && <span className="muted"> Priority: {e.priority}.</span>}
              </p>
            ) : null}
            {s?.objects?.length ? (
              <ul className="plain">
                {s.objects.map((o, i) => <li key={i}><strong>{o.type}</strong> — {o.description}</li>)}
              </ul>
            ) : null}
            {s?.tags?.length ? <div className="tags">{s.tags.map((t) => <span key={t} className="tag">{t}</span>)}</div> : null}
            {e.synopsis_original && (
              <div className="original">
                <button className="linkish" onClick={() => setShowOriginal(!showOriginal)}>{showOriginal ? "Hide" : "Show"} Qwen's original</button>
                {showOriginal && (
                  <div className="muted small">
                    <p>{e.synopsis_original.summary} <em>(threat: {e.synopsis_original.threat_level})</em></p>
                    <button className="ghost small" onClick={async () => setE(await api.revertSynopsis(e.id))}>Revert to original</button>
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </section>

      <FeedbackBar e={e} setE={setE} />

      {notes.length > 0 && (
        <section>
          <h3>Notes</h3>
          {notes.map((m) => (
            <div key={m.id} className="note">
              <p>{m.content}</p>
              <div className="row small muted">
                {m.at != null && <button className="linkish" onClick={() => seek(m.at!)}>at {m.at.toFixed(1)}s</button>}
                <button className="linkish" onClick={() => onUnsave(m)}>Remove note</button>
              </div>
            </div>
          ))}
        </section>
      )}

      <section>
        <h3>Verification</h3>
        <table className="kv">
          <tbody>
            <tr><td>Camera</td><td>{e.camera_class} · {((e.camera_conf ?? 0) * 100).toFixed(0)}% · track {e.track_id}</td></tr>
            <tr><td>YOLO</td><td>{e.yolo_class ?? "—"} {e.yolo_conf != null && `· ${(e.yolo_conf * 100).toFixed(0)}%`}</td></tr>
            <tr><td>Agreement</td><td>{e.yolo_hits ?? 0} of {e.detections?.samples?.length ?? 0} frames (need {e.detections?.needed ?? "—"})</td></tr>
            {e.rules?.length ? <tr><td>Rules</td><td>{[...new Set(e.rules.map((r) => r.topic.split("/").slice(-2, -1)[0] || r.topic))].join(", ")}</td></tr> : null}
          </tbody>
        </table>
      </section>
    </>
  );
}

function blankSynopsis(e: NvrEvent): Synopsis {
  return { summary: "", activity: "", objects: [{ type: e.yolo_class ?? e.camera_class, description: "" }], threat_level: "none", threat_reason: "", tags: [] };
}

function SynopsisEditor({ initial, onSave, onCancel }: { initial: Synopsis; onSave: (s: Synopsis) => Promise<void>; onCancel: () => void }) {
  const [s, setS] = useState<Synopsis>({ ...initial, objects: initial.objects.map((o) => ({ ...o })) });
  const [tags, setTags] = useState(initial.tags.join(", "));
  const [busy, setBusy] = useState(false);
  const setObj = (i: number, patch: Partial<Synopsis["objects"][number]>) =>
    setS({ ...s, objects: s.objects.map((o, j) => (j === i ? { ...o, ...patch } : o)) });
  const save = async () => {
    setBusy(true);
    try {
      await onSave({ ...s, tags: tags.split(",").map((t) => t.trim()).filter(Boolean), objects: s.objects.filter((o) => o.type.trim()) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="form">
      <label className="field"><span>Summary</span><textarea rows={4} value={s.summary} onChange={(ev) => setS({ ...s, summary: ev.target.value })} /></label>
      <label className="field"><span>Activity</span><input value={s.activity} onChange={(ev) => setS({ ...s, activity: ev.target.value })} /></label>
      <div className="field">
        <span>Objects</span>
        {s.objects.map((o, i) => (
          <div key={i} className="row obj-row">
            <input className="obj-type" value={o.type} placeholder="type" onChange={(ev) => setObj(i, { type: ev.target.value })} />
            <input className="obj-desc" value={o.description} placeholder="description" onChange={(ev) => setObj(i, { description: ev.target.value })} />
            <button className="ghost small" onClick={() => setS({ ...s, objects: s.objects.filter((_, j) => j !== i) })} aria-label="Remove">✕</button>
          </div>
        ))}
        <button className="ghost small" onClick={() => setS({ ...s, objects: [...s.objects, { type: "", description: "" }] })}>Add object</button>
      </div>
      <div className="row">
        <label className="field"><span>Threat</span>
          <select value={s.threat_level} onChange={(ev) => setS({ ...s, threat_level: ev.target.value as Threat })}>
            {THREATS.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </label>
        <label className="field grow"><span>Threat reason</span><input value={s.threat_reason ?? ""} onChange={(ev) => setS({ ...s, threat_reason: ev.target.value })} /></label>
      </div>
      <label className="field"><span>Tags (comma separated)</span><input value={tags} onChange={(ev) => setTags(ev.target.value)} /></label>
      <p className="muted small">Your correction replaces the synopsis in the UI and search. Recent corrections from this camera are shown to Qwen as examples for future events.</p>
      <div className="row">
        <button onClick={save} disabled={busy || !s.summary.trim()}>{busy ? "Saving…" : "Save correction"}</button>
        <button className="ghost" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

function FeedbackBar({ e, setE }: { e: NvrEvent; setE: (e: NvrEvent) => void }) {
  const fb: Feedback = e.feedback ?? {};
  const [note, setNote] = useState(fb.note ?? "");
  const send = async (patch: Feedback) => setE(await api.feedback(e.id, patch));
  const toggleReason = (r: string) => {
    const reasons = new Set(fb.reasons ?? []);
    if (reasons.has(r)) reasons.delete(r);
    else reasons.add(r);
    send({ reasons: [...reasons] });
  };
  return (
    <section className="feedback">
      <h3>Feedback</h3>
      {e.synopsis && (
        <div className="row">
          <span className="muted small">Synopsis</span>
          <button className={`ghost small ${fb.rating === "up" ? "on" : ""}`} onClick={() => send({ rating: fb.rating === "up" ? null : "up" })} aria-label="Good synopsis">👍</button>
          <button className={`ghost small ${fb.rating === "down" ? "on" : ""}`} onClick={() => send({ rating: fb.rating === "down" ? null : "down" })} aria-label="Bad synopsis">👎</button>
        </div>
      )}
      {fb.rating === "down" && (
        <div className="chips">
          {REASONS.map((r) => (
            <button key={r} className={`chip ${fb.reasons?.includes(r) ? "on" : ""}`} onClick={() => toggleReason(r)}>{r}</button>
          ))}
        </div>
      )}
      <div className="row">
        <span className="muted small">Detection</span>
        {VERDICTS.map(([v, label]) => (
          <button key={v} className={`ghost small ${fb.verdict === v ? "on" : ""}`} onClick={() => send({ verdict: fb.verdict === v ? null : v })}>{label}</button>
        ))}
      </div>
      {fb.verdict === "wrong_class" && (
        <div className="row">
          <span className="muted small">Actually a</span>
          <select value={fb.correct_class ?? ""} onChange={(ev) => send({ correct_class: ev.target.value || null })}>
            <option value="">choose…</option>
            {["person", "vehicle", "animal", "shadow / light", "vegetation", "other"].map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
      )}
      <div className="row">
        <input className="grow" placeholder="Note (optional, searchable)" value={note} onChange={(ev) => setNote(ev.target.value)}
          onBlur={() => note !== (fb.note ?? "") && send({ note })} onKeyDown={(ev) => ev.key === "Enter" && (ev.target as HTMLInputElement).blur()} />
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ chat tab */

const SUGGESTIONS = ["What is the person doing?", "Describe their clothing and anything they carry.", "Which way did they come from and leave?", "Is anything suspicious here?"];

function ChatPanel({ e, chat, setChat, video, seek }: {
  e: NvrEvent; chat: ChatMessage[]; setChat: (c: ChatMessage[]) => void;
  video: React.RefObject<HTMLVideoElement | null>; seek: (t: number) => void;
}) {
  const [q, setQ] = useState("");
  const [atMoment, setAtMoment] = useState(false);
  const [now, setNow] = useState(0);
  const [pending, setPending] = useState<{ q: string; at: number | null; answer: string; frames: { file: string; t: number }[] } | null>(null);
  const [err, setErr] = useState("");
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const v = video.current;
    if (!v) return;
    const tick = () => setNow(v.currentTime);
    v.addEventListener("timeupdate", tick);
    return () => v.removeEventListener("timeupdate", tick);
  }, [video]);
  useEffect(() => {
    // Block body on purpose: newer browsers return a Promise from scrollIntoView, and React
    // would try to call an effect's return value as its cleanup.
    end.current?.scrollIntoView({ block: "end" });
  }, [chat, pending]);

  const ask = async (text: string) => {
    if (!text.trim() || pending) return;
    const at = atMoment ? Math.round(now * 10) / 10 : null;
    setErr("");
    setQ("");
    setPending({ q: text, at, answer: "", frames: [] });
    try {
      await askClip(e.id, text, at, (c) => {
        if (c.type === "frames") setPending((p) => p && { ...p, frames: c.frames });
        if (c.type === "delta") setPending((p) => p && { ...p, answer: p.answer + c.text });
        if (c.type === "error") setErr(c.error);
      });
    } catch (ex) {
      setErr(String(ex));
    }
    setChat(await api.chat(e.id));
    setPending(null);
  };

  if (!e.clip) return <p className="muted">Chat needs the event's recording, which isn't available.</p>;

  return (
    <div className="chat">
      <div className="chat-log">
        {chat.length === 0 && !pending && (
          <div className="muted small">
            <p>Ask Qwen about this clip. It looks at frames spread across the event, or with <em>focus on current moment</em> at frames around the video's playhead.</p>
            <div className="chips">
              {SUGGESTIONS.map((s) => <button key={s} className="chip" onClick={() => ask(s)}>{s}</button>)}
            </div>
          </div>
        )}
        {chat.map((m) => (
          <Bubble key={m.id} role={m.role} text={m.content} at={m.at} frames={m.frames} e={e} seek={seek}
            action={m.role === "assistant" ? (
              <button className="linkish" onClick={() => api.saveNote(e.id, m.id, !m.saved).then(setChat)}>{m.saved ? "Saved as note ✓" : "Save as note"}</button>
            ) : null} />
        ))}
        {pending && (
          <>
            <Bubble role="user" text={pending.q} at={pending.at} frames={[]} e={e} seek={seek} />
            <Bubble role="assistant" text={pending.answer || (pending.frames.length ? "Looking at the frames…" : "Pulling frames from the clip…")} at={null} frames={pending.frames} e={e} seek={seek} />
          </>
        )}
        {err && <p className="error small">{err}</p>}
        <div ref={end} />
      </div>
      <form className="chat-input" onSubmit={(ev) => { ev.preventDefault(); ask(q); }}>
        <label className="row small muted">
          <input type="checkbox" checked={atMoment} onChange={(ev) => setAtMoment(ev.target.checked)} />
          Focus on current moment ({now.toFixed(1)}s)
        </label>
        <div className="row">
          <input className="grow" value={q} onChange={(ev) => setQ(ev.target.value)} placeholder="Ask about this clip…" disabled={Boolean(pending)} />
          <button type="submit" disabled={!q.trim() || Boolean(pending)}>Ask</button>
        </div>
        {chat.some((m) => !m.saved) && !pending && (
          <button type="button" className="linkish small" onClick={() => api.clearChat(e.id).then(setChat)}>Clear conversation (keeps saved notes)</button>
        )}
      </form>
    </div>
  );
}

function Bubble({ role, text, at, frames, e, seek, action }: {
  role: string; text: string; at: number | null; frames: { file: string; t: number }[]; e: NvrEvent; seek: (t: number) => void; action?: React.ReactNode;
}) {
  return (
    <div className={`bubble ${role}`}>
      {at != null && role === "user" && <button className="linkish small" onClick={() => seek(at)}>@ {at.toFixed(1)}s</button>}
      <p>{text}</p>
      {frames.length > 0 && (
        <div className="bubble-frames">
          {frames.map((f) => (
            <img key={f.file} src={media(e, f.file)} alt={`t=${f.t}s`} title={`Jump to ${f.t.toFixed(1)}s`} onClick={() => seek(f.t)} />
          ))}
        </div>
      )}
      {action && <div className="small">{action}</div>}
    </div>
  );
}
