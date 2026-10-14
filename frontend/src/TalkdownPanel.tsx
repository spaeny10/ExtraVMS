/**
 * Talk-down on a live tile (backend talkdown.py): the 🔊 Talk-down button opens this panel ON the picture, and the
 * video keeps playing under it, so the operator watches the person while speaking to them. Pick the speaker (or "All
 * speakers" when the camera has several), a message from the server's library, Play / Stop, the live status of each
 * speaker, Say (typed text) when the server has a text-to-speech engine, and File… for a one-off recording that is
 * not kept. Stop stops the speakers the current choice addresses. The logic is talkdown.ts.
 */
import { useEffect, useRef, useState } from "react";
import { api, type PlayRequest, type SiteApi, type SpeakerStatus, type TalkdownLibrary } from "./api";
import { ALL, type TalkSpeaker, defaultChoice, fileToBase64, isActive, playOutcome, statusText, targets, uploadProblem } from "./talkdown";
import { errorText, toast } from "./ui";

const POLL_MS = 1000;

export function TalkdownPanel({ speakers, site = api, onClose }: {
  speakers: TalkSpeaker[];
  /** the camera's server (default: this server); its writes go through the hub on the hub */
  site?: SiteApi;
  onClose: () => void;
}) {
  const [lib, setLib] = useState<TalkdownLibrary | null>(null);
  const [libError, setLibError] = useState("");
  const [choice, setChoice] = useState(() => defaultChoice(speakers));
  const [msgId, setMsgId] = useState<number | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<{ at: number; by: Record<string, SpeakerStatus> }>({ at: Date.now(), by: {} });
  const fileRef = useRef<HTMLInputElement>(null);
  const ids = speakers.map((s) => s.id).join(",");

  useEffect(() => {
    site.talkdownMessages().then((l) => {
      setLib(l);
      setMsgId((m) => m ?? l.messages.find((x) => x.available)?.id ?? null);
    }).catch((e) => setLibError(errorText(e)));
  }, [site]);
  useEffect(() => {
    let alive = true;
    const want = new Set(ids.split(","));
    const pull = () => site.speakers().then((list) => {
      if (alive) setStatus({ at: Date.now(), by: Object.fromEntries(list.filter((s) => want.has(s.id)).map((s) => [s.id, s.status])) });
    }).catch(() => {});
    pull();
    const t = setInterval(pull, POLL_MS);
    return () => { alive = false; clearInterval(t); };
  }, [site, ids]);
  useEffect(() => { if (choice !== ALL && !speakers.some((s) => s.id === choice)) setChoice(defaultChoice(speakers)); }, [choice, speakers]);

  const tg = targets(choice, speakers);
  const anyActive = tg.some((s) => isActive(status.by[s.id]));
  const sinceFetch = (Date.now() - status.at) / 1000;

  const play = async (what: PlayRequest) => {
    if (!tg.length) return;
    setBusy(true);
    const results = await Promise.all(tg.map((s) => site.playSpeaker(s.id, what)
      .then((r) => ({ speaker: s.name, ok: true, replaced: r.replaced }))
      .catch((e) => ({ speaker: s.name, ok: false, error: errorText(e) }))));
    setBusy(false);
    const { ok, failed } = playOutcome(results);
    if (ok) toast.success(ok);
    failed.forEach((f) => toast.error(f));
    return failed.length < results.length;
  };
  const stop = async () => {
    await Promise.all(tg.map((s) => site.stopSpeaker(s.id).catch((e) => toast.error(`${s.name}: ${errorText(e)}`))));
  };
  const sendFile = async (file: File | undefined) => {
    if (!file) return;
    const problem = uploadProblem(file.name, file.size, lib?.max_upload, lib?.types);
    if (problem) { toast.error(problem); return; }
    try {
      await play({ upload: { filename: file.name, data_b64: await fileToBase64(file) } });
    } catch (e) {
      toast.error(e);
    }
  };
  const say = async (e: React.FormEvent) => {
    e.preventDefault();
    if (text.trim() && await play({ text: text.trim() })) setText("");
  };

  const messages = lib?.messages.filter((m) => m.available) ?? [];
  const halt = (e: React.SyntheticEvent) => e.stopPropagation();   // not a paint stroke, zoom or PTZ drag on the picture
  return (
    <div className="talk-panel" role="dialog" aria-label="Talk-down" onPointerDown={halt} onClick={halt} onDoubleClick={halt} onWheel={halt}
      onKeyDown={(e) => { if (e.key === "Escape") onClose(); }}>
      <div className="talk-head">
        <strong>🔊 Talk-down</strong>
        {speakers.length > 1 ? (
          <select value={choice} onChange={(e) => setChoice(e.target.value)} aria-label="Speaker">
            <option value={ALL}>All speakers ({speakers.length})</option>
            {speakers.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        ) : <span className="talk-speaker">{speakers[0]?.name}</span>}
        <span className="spacer" />
        <button className="ghost small" onClick={onClose} aria-label="Close talk-down" title="Close (Esc); a message playing keeps playing">✕</button>
      </div>
      {libError && <div className="talk-error">{libError}</div>}
      {!lib && !libError && <div className="talk-dim">Loading messages…</div>}
      {lib && (
        <div className="talk-msgs" role="listbox" aria-label="Messages">
          {messages.map((m) => (
            <button key={m.id} role="option" aria-selected={m.id === msgId} className={`talk-msg ${m.id === msgId ? "on" : ""}`}
              title={m.text ?? (m.duration_s ? `${Math.round(m.duration_s)} s recording` : "recording")}
              onClick={() => setMsgId(m.id)} onDoubleClick={() => play({ message_id: m.id })}>
              {m.name}
            </button>
          ))}
          {!messages.length && <span className="talk-dim">No messages yet{lib.tts.available ? ": type one below" : ""}. Add them under Settings › Cameras › Speakers.</span>}
        </div>
      )}
      <div className="talk-actions">
        <button className="small" disabled={busy || msgId === null || !messages.some((m) => m.id === msgId)} onClick={() => msgId !== null && play({ message_id: msgId })}
          title="Play the selected message (double-click a message to play it at once)">▶ Play</button>
        <button className="small talk-stop" disabled={!anyActive} onClick={stop} title={tg.length > 1 ? "Stop on every speaker" : "Stop"}>■ Stop</button>
        <span className="spacer" />
        <button className="ghost small" disabled={busy} onClick={() => fileRef.current?.click()} title={`Play an audio file once (${(lib?.types ?? [".mp3", ".wav", ".m4a", ".ogg"]).join(", ")}, at most 2 MB, ${lib?.max_s ?? 120} s); it is not kept`}>File…</button>
        <input ref={fileRef} type="file" hidden accept={(lib?.types ?? [".mp3", ".wav", ".m4a", ".ogg"]).join(",")}
          onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; sendFile(f); }} />
      </div>
      {lib?.tts.available && (
        <form className="talk-say" onSubmit={say}>
          <input value={text} maxLength={500} placeholder="Type what to say…" aria-label="Say" onChange={(e) => setText(e.target.value)} />
          <button className="small" disabled={busy || !text.trim()}>Say</button>
        </form>
      )}
      <div className="talk-status" aria-live="polite">
        {tg.map((s) => {
          const st = status.by[s.id];
          return (
            <div key={s.id} className={st?.last?.result === "failed" && !isActive(st) ? "talk-error" : ""}>
              <span className={`dot ${isActive(st) ? "ok" : s.online === false ? "bad" : "idle"}`} title={s.online === false ? "The speaker did not answer its last check" : undefined} />{" "}
              {tg.length > 1 && <>{s.name}: </>}{statusText(st, sinceFetch)}
            </div>
          );
        })}
      </div>
    </div>
  );
}
