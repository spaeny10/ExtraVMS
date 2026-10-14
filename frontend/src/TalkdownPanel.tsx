/**
 * Talk-down on a live tile (backend talkdown.py): the 🔊 Talk-down button opens this panel ON the picture, and the
 * video keeps playing under it, so the operator watches the person while speaking to them. Pick the speaker (or "All
 * speakers" when the camera has several), a message from the server's library, Play / Stop, the live status of each
 * speaker, Say (typed text) when the server has a text-to-speech engine, and File… for a one-off recording that is
 * not kept. Stop stops the speakers the current choice addresses. The logic is talkdown.ts.
 * On the hub a camera's speakers may be on other servers than the camera (a Site's camera groups): each speaker brings
 * its own server's client, and play / stop / status / the message library go to the speaker's server, not the camera's.
 * Messages are per server: with speakers on several servers the list holds those every one of them has (by name).
 * Describe & warn (with text-to-speech): Qwen looks at this camera's current picture (on the camera's server) and says
 * how to address the person ("You in the red shirt by the white truck"); the operator picks a template, checks and edits
 * the text, and plays it like typed text. Nothing plays until they press Play.
 */
import { useEffect, useReducer, useRef, useState } from "react";
import { api, type PlayRequest, type SiteApi, type SpeakerStatus, type TalkdownLibrary } from "./api";
import { ALL, DESCRIBE_EVERY_MS, type TalkSpeaker, WARN_IDLE, asOf, clientsOf, defaultChoice, describeNote, fileToBase64, isActive, messageOn, playOutcome,
  playableMessages, serversOf, speakerKey, statusText, targets, uploadProblem, warnChoices, warnReducer } from "./talkdown";
import { errorText, toast } from "./ui";

const POLL_MS = 1000;

export function TalkdownPanel({ speakers, site = api, cameraId, onClose }: {
  speakers: TalkSpeaker[];
  /** the camera's server (default: this server); its writes go through the hub on the hub. A speaker that brings its
   *  own server's client (`site` on the speaker: the hub) is reached through that instead. */
  site?: SiteApi;
  /** the camera the panel is on (on `site`): what Describe & warn looks at; absent = no Describe & warn */
  cameraId?: string;
  onClose: () => void;
}) {
  // per server ("" = `site`): its message library, or why it couldn't be read
  const [libs, setLibs] = useState<Record<string, TalkdownLibrary>>({});
  const [libErrors, setLibErrors] = useState<Record<string, string>>({});
  const [choice, setChoice] = useState(() => defaultChoice(speakers));
  // the picked message, in the first target server's library (messages are per server)
  const [msg, setMsg] = useState<{ server: string; id: number } | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [warn, dispatch] = useReducer(warnReducer, WARN_IDLE);
  const [coolUntil, setCoolUntil] = useState(0);
  const [status, setStatus] = useState<{ at: number; by: Record<string, SpeakerStatus> }>({ at: Date.now(), by: {} });
  const fileRef = useRef<HTMLInputElement>(null);
  const keys = speakers.map(speakerKey).join(",");
  const serverList = serversOf(speakers).join(",");
  // each server's client, read when the effects below run (the hub rebuilds the speakers array on every poll)
  const clients = useRef(clientsOf(speakers, site));
  clients.current = clientsOf(speakers, site);
  const siteOf = (srv: string) => clients.current(srv);

  useEffect(() => {
    let alive = true;
    serverList.split(",").forEach((srv) => siteOf(srv).talkdownMessages()
      .then((l) => { if (alive) setLibs((m) => ({ ...m, [srv]: l })); })
      .catch((e) => { if (alive) setLibErrors((m) => ({ ...m, [srv]: errorText(e) })); }));
    return () => { alive = false; };
  }, [site, serverList]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    let alive = true;
    const want = new Set(keys.split(","));
    const pull = () => Promise.all(serverList.split(",").map((srv) => siteOf(srv).speakers()
      .then((list) => list.map((s) => [speakerKey({ id: s.id, server: srv || undefined }), s.status] as const)).catch(() => null)))
      .then((per) => {
        if (alive && per.some((x) => x)) setStatus({ at: Date.now(), by: Object.fromEntries(per.flatMap((x) => x ?? []).filter(([k]) => want.has(k))) });
      });
    pull();
    const t = setInterval(pull, POLL_MS);
    return () => { alive = false; clearInterval(t); };
  }, [site, keys, serverList]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (choice !== ALL && !speakers.some((s) => speakerKey(s) === choice)) setChoice(defaultChoice(speakers)); }, [choice, speakers]);
  useEffect(() => {   // Describe & warn is offered again DESCRIBE_EVERY_MS after the last one
    if (coolUntil <= Date.now()) return;
    const t = setTimeout(() => setCoolUntil(0), coolUntil - Date.now());
    return () => clearTimeout(t);
  }, [coolUntil]);

  const tg = targets(choice, speakers);
  const tgServers = serversOf(tg);
  const first = tgServers[0] ?? "";
  const lib: TalkdownLibrary | undefined = libs[first];
  const libError = tgServers.map((srv) => libErrors[srv]).find((e) => e) ?? "";
  const loaded = tgServers.every((srv) => libs[srv] || libErrors[srv]);
  const messages = playableMessages(libs, tgServers);
  const picked = msg && msg.server === first ? messages.find((m) => m.id === msg.id) : undefined;
  const msgList = `${first}|${messages.map((m) => m.id).join(",")}`;
  // the first message until one is picked, and again when the choice moves to a server whose list hasn't it
  useEffect(() => { if (!picked && messages.length) setMsg({ server: first, id: messages[0].id }); }, [msgList]); // eslint-disable-line react-hooks/exhaustive-deps
  const tts = tgServers.length > 0 && tgServers.every((srv) => libs[srv]?.tts.available);
  const anyActive = tg.some((s) => isActive(status.by[speakerKey(s)]));
  const sinceFetch = (Date.now() - status.at) / 1000;

  /** Play on every target speaker, each through its own server; `what` may differ per speaker (a string: why not). */
  const play = async (what: PlayRequest | ((s: TalkSpeaker) => PlayRequest | string)) => {
    if (!tg.length) return;
    setBusy(true);
    const results = await Promise.all(tg.map((s) => {
      const w = typeof what === "function" ? what(s) : what;
      if (typeof w === "string") return Promise.resolve({ speaker: s.name, ok: false, error: w });
      return siteOf(s.server ?? "").playSpeaker(s.id, w)
        .then((r) => ({ speaker: s.name, ok: true, replaced: r.replaced }))
        .catch((e) => ({ speaker: s.name, ok: false, error: errorText(e) }));
    }));
    setBusy(false);
    const { ok, failed } = playOutcome(results);
    if (ok) toast.success(ok);
    failed.forEach((f) => toast.error(f));
    return failed.length < results.length;
  };
  const stop = async () => {
    await Promise.all(tg.map((s) => siteOf(s.server ?? "").stopSpeaker(s.id).catch((e) => toast.error(`${s.name}: ${errorText(e)}`))));
  };
  /** The picked message on each target's server: that id on the first server, the message of the same name elsewhere. */
  const playPicked = (m: { id: number; name: string }) => play((s) => {
    const srv = s.server ?? "";
    const id = srv === first ? m.id : messageOn(libs[srv], m);
    return id === null ? `no message '${m.name}' on its server` : { message_id: id };
  });
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
  const cooling = coolUntil > Date.now();
  /** Qwen describes the camera's current picture (the camera's server; the speaker id keys its rate limit). Never plays. */
  const describe = async () => {
    if (!cameraId || !tg.length || cooling) return;
    const template = warn.stage === "ready" ? warn.template : undefined;
    dispatch({ type: "look" });
    setCoolUntil(Date.now() + DESCRIBE_EVERY_MS);
    try {
      dispatch({ type: "answer", answer: await site.describeSpeaker(tg[0].id, { camera_id: cameraId, template }) });
    } catch (e) {
      dispatch({ type: "failed" });
      toast.error(e);
    }
  };
  /** The reviewed text, spoken on the chosen speaker(s) like typed text: what is logged and audited is exactly this. */
  const playWarn = async () => {
    if (warn.stage === "ready" && warn.text.trim() && await play({ text: warn.text.trim() })) dispatch({ type: "close" });
  };
  const choices = warn.stage === "ready" ? warnChoices(warn.answer) : [];

  const halt = (e: React.SyntheticEvent) => e.stopPropagation();   // not a paint stroke, zoom or PTZ drag on the picture
  return (
    <div className="talk-panel" role="dialog" aria-label="Talk-down" onPointerDown={halt} onClick={halt} onDoubleClick={halt} onWheel={halt}
      onKeyDown={(e) => { if (e.key === "Escape") onClose(); }}>
      <div className="talk-head">
        <strong>🔊 Talk-down</strong>
        {speakers.length > 1 ? (
          <select value={choice} onChange={(e) => setChoice(e.target.value)} aria-label="Speaker">
            <option value={ALL}>All speakers ({speakers.length})</option>
            {speakers.map((s) => <option key={speakerKey(s)} value={speakerKey(s)}>{s.name}</option>)}
          </select>
        ) : <span className="talk-speaker">{speakers[0]?.name}</span>}
        <span className="spacer" />
        <button className="ghost small" onClick={onClose} aria-label="Close talk-down" title="Close (Esc); a message playing keeps playing">✕</button>
      </div>
      {libError && <div className="talk-error">{libError}</div>}
      {!loaded && !libError && <div className="talk-dim">Loading messages…</div>}
      {loaded && lib && (
        <div className="talk-msgs" role="listbox" aria-label="Messages">
          {messages.map((m) => (
            <button key={m.id} role="option" aria-selected={m.id === picked?.id} className={`talk-msg ${m.id === picked?.id ? "on" : ""}`}
              title={m.text ?? (m.duration_s ? `${Math.round(m.duration_s)} s recording` : "recording")}
              onClick={() => setMsg({ server: first, id: m.id })} onDoubleClick={() => playPicked(m)}>
              {m.name}
            </button>
          ))}
          {!messages.length && (tgServers.length > 1
            ? <span className="talk-dim">No message is on all of these speakers' servers: pick one speaker{tts ? ", or type one below" : ""}.</span>
            : <span className="talk-dim">No messages yet{lib.tts.available ? ": type one below" : ""}. Add them under Settings › Cameras › Speakers.</span>)}
        </div>
      )}
      <div className="talk-actions">
        <button className="small" disabled={busy || !picked} onClick={() => picked && playPicked(picked)}
          title="Play the selected message (double-click a message to play it at once)">▶ Play</button>
        <button className="small talk-stop" disabled={!anyActive} onClick={stop} title={tg.length > 1 ? "Stop on every speaker" : "Stop"}>■ Stop</button>
        <span className="spacer" />
        <button className="ghost small" disabled={busy} onClick={() => fileRef.current?.click()} title={`Play an audio file once (${(lib?.types ?? [".mp3", ".wav", ".m4a", ".ogg"]).join(", ")}, at most 2 MB, ${lib?.max_s ?? 120} s); it is not kept`}>File…</button>
        <input ref={fileRef} type="file" hidden accept={(lib?.types ?? [".mp3", ".wav", ".m4a", ".ogg"]).join(",")}
          onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; sendFile(f); }} />
      </div>
      {tts && (
        <form className="talk-say" onSubmit={say}>
          <input value={text} maxLength={500} placeholder="Type what to say…" aria-label="Say" onChange={(e) => setText(e.target.value)} />
          <button className="small" disabled={busy || !text.trim()}>Say</button>
        </form>
      )}
      {tts && cameraId && (warn.stage === "idle" ? (
        <div className="talk-actions">
          <button className="small" disabled={busy || cooling} onClick={describe}
            title="Qwen looks at this camera now and suggests how to address the person; you check the text before anything plays">👁 Describe &amp; warn</button>
        </div>
      ) : warn.stage === "looking" ? (
        <div className="talk-dim" aria-live="polite">Looking at the camera…</div>
      ) : (
        <div className="talk-warn" aria-label="Describe and warn">
          <div className={warn.answer.people === null ? "talk-error" : "talk-dim"} aria-live="polite">
            {describeNote(warn.answer)}{warn.answer.frame_ts ? ` · ${asOf(warn.answer.frame_ts)}` : ""}
            {!warn.answer.people && " · the general message is below"}
          </div>
          {!!choices.length && (
            <div className="talk-msgs" role="listbox" aria-label="Who to address">
              {choices.map((c) => (
                <button key={c.phrase} role="option" aria-selected={c.phrase === warn.phrase} className={`talk-msg ${c.phrase === warn.phrase ? "on" : ""}`}
                  onClick={() => dispatch({ type: "pick", phrase: c.phrase })}>{c.label}</button>
              ))}
            </div>
          )}
          <select value={warn.template} aria-label="Template" onChange={(e) => dispatch({ type: "template", id: e.target.value })}>
            {warn.answer.templates.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
          <textarea value={warn.text} rows={3} maxLength={500} aria-label="Message to speak" placeholder="Type what to say…"
            onChange={(e) => dispatch({ type: "edit", text: e.target.value })} />
          <div className="talk-dim">Check the description before playing{warn.answer.filtered ? " (some details were left out)" : ""}.</div>
          <div className="talk-actions">
            <button className="small" disabled={busy || !warn.text.trim()} onClick={playWarn}
              title={tg.length > 1 ? `Speak this on ${tg.length} speakers` : "Speak this"}>▶ Play</button>
            <button className="ghost small" disabled={busy} onClick={() => dispatch({ type: "close" })}>Cancel</button>
            <span className="spacer" />
            <button className="ghost small" disabled={busy || cooling} onClick={describe} title="Describe the camera's picture again">Look again</button>
          </div>
        </div>
      ))}
      <div className="talk-status" aria-live="polite">
        {tg.map((s) => {
          const st = status.by[speakerKey(s)];
          return (
            <div key={speakerKey(s)} className={st?.last?.result === "failed" && !isActive(st) ? "talk-error" : ""}>
              <span className={`dot ${isActive(st) ? "ok" : s.online === false ? "bad" : "idle"}`} title={s.online === false ? "The speaker did not answer its last check" : undefined} />{" "}
              {tg.length > 1 && <>{s.name}: </>}{statusText(st, sinceFetch)}
            </div>
          );
        })}
      </div>
    </div>
  );
}
