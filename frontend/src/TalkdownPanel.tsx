/**
 * Talk-down on a live tile (backend talkdown.py): the Talk-down button opens this panel UNDER the tile (never over the
 * picture: the operator watches the person while speaking to them), on the desktop and the phone alike. Pick the speaker
 * (or "All speakers" when the camera has several) and how many are online; one tap on a preset plays it (the first
 * messages of the server's library, each with an icon and accent by name; the rest under More…); Custom speech
 * (Broadcast, Enter) when the server has a text-to-speech engine; Hold to Talk (the browser's microphone, live:
 * talkMic.ts); Load audio file… for a one-off recording that is not kept; AI Describe & warn. While anything plays, one
 * bar says what, how far along, and Stop all; idle, the last result. Then each speaker's state from real data (its last
 * check and its status). The logic is talkdown.ts.
 * On the hub a camera's speakers may be on other servers than the camera (a Site's camera groups): each speaker brings
 * its own server's client, and play / stop / talk / status / the message library go to the speaker's server, not the
 * camera's. Messages are per server: with speakers on several servers the presets are those every one of them has (by name).
 * Describe & warn (with text-to-speech): Qwen looks at this camera's current picture (on the camera's server) and says
 * how to address the person ("You in the red shirt by the white truck"); the operator picks a template, checks and edits
 * the text, and plays it like typed text. Nothing plays until they press Play.
 * Deterrence: when the camera has a strobe, siren or light (read on the camera's server), its buttons and Stop sit
 * under the speakers' bar (DeterrencePanel.tsx), so a warning and the siren are one panel apart.
 */
import { useEffect, useReducer, useRef, useState } from "react";
import { api, type PlayRequest, type SiteApi, type SpeakerStatus, type TalkdownLibrary } from "./api";
import { ALL, DESCRIBE_EVERY_MS, type SpeakerHealth, type TalkSpeaker, TALK_IDLE, WARN_IDLE, asOf, clientsOf, clock, defaultChoice, describeNote,
  fileToBase64, isActive, lastResult, messageOn, micProblem, onlineCount, playOutcome, playableMessages, playingBar, presetLook, serversOf, speakerKey,
  speakerState, splitPresets, talkLabel, talkReducer, targets, uploadProblem, warnChoices, warnReducer } from "./talkdown";
import { browserMic, startTalk, type TalkSession } from "./talkMic";
import { DeterrenceControl } from "./DeterrencePanel";
import { Icon, errorText, toast } from "./ui";

const POLL_MS = 1000;
const TICK_MS = 250;   // the playing bar and the talk clock move between polls

export function TalkdownPanel({ speakers, site = api, cameraId, deter = true, onClose }: {
  speakers: TalkSpeaker[];
  /** the camera's server (default: this server); its writes go through the hub on the hub. A speaker that brings its
   *  own server's client (`site` on the speaker: the hub) is reached through that instead. */
  site?: SiteApi;
  /** the camera the panel is on (on `site`): what Describe & warn looks at; absent = no Describe & warn */
  cameraId?: string;
  /** the viewer may switch the camera's deterrence outputs (shown only when the camera has some) */
  deter?: boolean;
  onClose: () => void;
}) {
  // per server ("" = `site`): its message library, or why it couldn't be read
  const [libs, setLibs] = useState<Record<string, TalkdownLibrary>>({});
  const [libErrors, setLibErrors] = useState<Record<string, string>>({});
  const [choice, setChoice] = useState(() => defaultChoice(speakers));
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [warn, dispatch] = useReducer(warnReducer, WARN_IDLE);
  const [talk, talkDispatch] = useReducer(talkReducer, TALK_IDLE);
  const [coolUntil, setCoolUntil] = useState(0);
  const [status, setStatus] = useState<{ at: number; by: Record<string, SpeakerStatus>; health: Record<string, SpeakerHealth> }>({ at: Date.now(), by: {}, health: {} });
  const [now, setNow] = useState(Date.now());
  const fileRef = useRef<HTMLInputElement>(null);
  const meterRef = useRef<HTMLSpanElement>(null);
  const session = useRef<TalkSession | null>(null);
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
      .then((list) => list.map((s) => ({ key: speakerKey({ id: s.id, server: srv || undefined }), status: s.status, health: { check: s.check, checked_at: s.checked_at } })))
      .catch(() => null)))
      .then((per) => {
        if (!alive || !per.some((x) => x)) return;
        const rows = per.flatMap((x) => x ?? []).filter((r) => want.has(r.key));
        setStatus({ at: Date.now(), by: Object.fromEntries(rows.map((r) => [r.key, r.status])), health: Object.fromEntries(rows.map((r) => [r.key, r.health])) });
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

  const statuses = speakers.map((s) => status.by[speakerKey(s)]);
  const anyActive = statuses.some(isActive);
  const talking = talk.stage !== "idle";
  useEffect(() => {   // the bar's progress and the talk clock between polls; the talk's 60 s cap
    if (!anyActive && !talking) return;
    const t = setInterval(() => { const n = Date.now(); setNow(n); talkDispatch({ type: "tick", now: n }); }, TICK_MS);
    return () => clearInterval(t);
  }, [anyActive, talking]);
  // let go (or the cap): the servers play out what they have, then the session ends itself (onEnd)
  useEffect(() => { if (talk.stage === "closing") session.current?.release(); }, [talk.stage]);
  useEffect(() => () => session.current?.release(), []);   // the panel closed while talking

  const tg = targets(choice, speakers);
  const tgServers = serversOf(tg);
  const first = tgServers[0] ?? "";
  const lib: TalkdownLibrary | undefined = libs[first];
  const libError = tgServers.map((srv) => libErrors[srv]).find((e) => e) ?? "";
  const loaded = tgServers.every((srv) => libs[srv] || libErrors[srv]);
  const { presets, more } = splitPresets(playableMessages(libs, tgServers));
  const tts = tgServers.length > 0 && tgServers.every((srv) => libs[srv]?.tts.available);
  const sinceFetch = (now - status.at) / 1000;
  const bar = playingBar(statuses, Math.max(0, sinceFetch));
  const last = bar ? null : lastResult(tg.map((s) => status.by[speakerKey(s)]));
  const { online, total } = onlineCount(speakers, status.health);
  const types = lib?.types ?? [".mp3", ".wav", ".m4a", ".ogg"];

  /** Play on every target speaker, each through its own server; `what` may differ per speaker (a string: why not). */
  const play = async (what: PlayRequest | ((s: TalkSpeaker) => PlayRequest | string)) => {
    if (!tg.length) return;
    setBusy(true);
    const results = await Promise.all(tg.map((s) => {
      const w = typeof what === "function" ? what(s) : what;
      if (typeof w === "string") return Promise.resolve({ speaker: s.name, ok: false, error: w });
      return siteOf(s.server ?? "").playSpeaker(s.id, w)
        .then((r) => ({ speaker: s.name, ok: true, replaced: r.replaced, tts: r.tts }))
        .catch((e) => ({ speaker: s.name, ok: false, error: errorText(e) }));
    }));
    setBusy(false);
    const { ok, failed } = playOutcome(results);
    if (ok) toast.success(ok);
    failed.forEach((f) => toast.error(f));
    return failed.length < results.length;
  };
  /** Stop all: every speaker of this camera that is playing (whatever the choice), and a live talk from here. */
  const stopAll = async () => {
    if (talk.stage === "opening" || talk.stage === "live") talkDispatch({ type: "release" });
    const busyOnes = speakers.filter((s) => isActive(status.by[speakerKey(s)]));
    await Promise.all((busyOnes.length ? busyOnes : tg).map((s) => siteOf(s.server ?? "").stopSpeaker(s.id).catch((e) => toast.error(`${s.name}: ${errorText(e)}`))));
  };
  /** A message on each target's server: that id on the first server, the message of the same name elsewhere. */
  const playMessage = (m: { id: number; name: string }) => play((s) => {
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

  /** Press: the microphone, then a socket per target speaker. A browser that can't talk says why instead. */
  const startTalking = () => {
    if (talk.stage !== "idle" || !tg.length) return;
    const problem = micProblem(browserMic());
    if (problem) { talkDispatch({ type: "failed", note: problem }); return; }
    talkDispatch({ type: "press", now: Date.now() });
    setNow(Date.now());
    session.current = startTalk(tg.map((s) => ({ name: s.name, url: siteOf(s.server ?? "").talkUrl(s.id) })), {
      onLive: () => talkDispatch({ type: "live", now: Date.now() }),
      onLevel: (l) => { if (meterRef.current) meterRef.current.style.transform = `scaleX(${l.toFixed(3)})`; },
      onEnd: (problems) => {   // said once, under the button (each speaker's own reason)
        session.current = null;
        talkDispatch({ type: "ended", note: problems.length ? problems.join(" · ") : null });
      },
    });
  };
  const letGo = () => talkDispatch({ type: "release" });

  const halt = (e: React.SyntheticEvent) => e.stopPropagation();   // not a paint stroke, zoom or PTZ drag on the picture
  return (
    <div className="talk-panel" role="region" aria-label="Talk-down" onPointerDown={halt} onClick={halt} onDoubleClick={halt} onWheel={halt}
      onKeyDown={(e) => { if (e.key === "Escape") onClose(); }}>
      <div className="talk-head">
        <span className="talk-title"><Icon name="speaker" size={18} /> Talk-down</span>
        {speakers.length > 1 ? (
          <select value={choice} onChange={(e) => setChoice(e.target.value)} aria-label="Speaker" disabled={talking}>
            <option value={ALL}>All speakers ({speakers.length})</option>
            {speakers.map((s) => <option key={speakerKey(s)} value={speakerKey(s)}>{s.name}</option>)}
          </select>
        ) : <span className="talk-speaker">{speakers[0]?.name}</span>}
        <span className={`talk-online ${online < total ? "some-off" : ""}`} title="Speakers whose last check answered">
          <span className={`dot ${online === total ? "ok" : online ? "warn" : "bad"}`} /> {online}/{total} online
        </span>
        <span className="spacer" />
        <button className="ghost talk-close" onClick={onClose} aria-label="Close talk-down" title="Close (Esc); a message playing keeps playing">
          <Icon name="x" size={18} />
        </button>
      </div>

      {libError && <div className="talk-error">{libError}</div>}
      {!loaded && !libError && <div className="talk-dim">Loading messages…</div>}
      {loaded && lib && (
        <div className="talk-presets" role="group" aria-label="Messages: one tap plays">
          {presets.map((m) => {
            const look = presetLook(m.name);
            const on = !!bar && bar.message === m.name;
            return (
              <button key={m.id} className={`talk-preset tone-${look.tone} ${on ? "playing" : ""}`} disabled={busy || !tg.length}
                aria-label={`Play ${m.name}${tg.length > 1 ? ` on ${tg.length} speakers` : ""}`}
                title={m.text ?? (m.duration_s ? `${Math.round(m.duration_s)} s recording` : "recording")} onClick={() => playMessage(m)}>
                <Icon name={look.icon} size={22} />
                <span>{m.name}</span>
              </button>
            );
          })}
          {!!more.length && (
            <select className="talk-more" value="" aria-label="More messages: picking one plays it" disabled={busy || !tg.length}
              onChange={(e) => { const m = more.find((x) => String(x.id) === e.target.value); if (m) playMessage(m); }}>
              <option value="">More…</option>
              {more.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
            </select>
          )}
          {!presets.length && (tgServers.length > 1
            ? <span className="talk-dim">No message is on all of these speakers' servers: pick one speaker{tts ? ", or type one below" : ""}.</span>
            : <span className="talk-dim">No messages yet{lib.tts.available ? ": type one below" : ""}. Add them under Settings › Cameras › Speakers.</span>)}
        </div>
      )}

      {tts && (
        <form className="talk-say" onSubmit={say}>
          <input value={text} maxLength={500} placeholder="Custom speech…" aria-label="Custom speech" onChange={(e) => setText(e.target.value)} />
          <button disabled={busy || !text.trim()} aria-label="Broadcast the custom speech"><Icon name="send" size={16} /> Broadcast</button>
        </form>
      )}

      <div className="talk-tools">
        <button type="button" className={`talk-act talk-hold ${talking ? "on" : ""}`} aria-pressed={talking} disabled={!tg.length}
          aria-label={talking ? talkLabel(talk, now) : `Hold to talk${tg.length > 1 ? ` on ${tg.length} speakers` : ""}`}
          title="Press and hold to speak through the speaker (Space while focused); let go to stop. At most 60 s."
          onPointerDown={(e) => { if (e.button !== 0) return; e.currentTarget.setPointerCapture(e.pointerId); startTalking(); }}
          onPointerUp={letGo} onPointerCancel={letGo} onLostPointerCapture={letGo} onBlur={letGo}
          onKeyDown={(e) => { if (e.key === " ") { e.preventDefault(); if (!e.repeat) startTalking(); } }}
          onKeyUp={(e) => { if (e.key === " ") { e.preventDefault(); letGo(); } }}
          onContextMenu={(e) => e.preventDefault()}>
          <Icon name="mic" size={18} />
          <span>{talkLabel(talk, now)}</span>
          {talk.stage === "live" && <span className="talk-meter" aria-hidden="true"><span ref={meterRef} /></span>}
        </button>
        <button type="button" className="talk-act" disabled={busy || !tg.length} onClick={() => fileRef.current?.click()}
          title={`Play an audio file once (${types.join(", ")}, at most 2 MB, ${lib?.max_s ?? 120} s); it is not kept`}>
          <Icon name="file" size={18} /> <span>Load audio file…</span>
        </button>
        <input ref={fileRef} type="file" hidden accept={types.join(",")} onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; sendFile(f); }} />
        {tts && cameraId && (
          <button type="button" className="talk-act" disabled={busy || cooling || warn.stage === "looking" || !tg.length} onClick={describe}
            title="Qwen looks at this camera now and suggests how to address the person; you check the text before anything plays">
            <Icon name="eye" size={18} /> <span>AI Describe &amp; warn</span>
          </button>
        )}
      </div>
      {talk.stage === "idle" && talk.note && <div className="talk-error" role="status">{talk.note}</div>}

      {warn.stage === "looking" && <div className="talk-dim" aria-live="polite">Looking at the camera…</div>}
      {warn.stage === "ready" && (
        <div className="talk-warn" aria-label="Describe and warn">
          <div className={warn.answer.people === null ? "talk-error" : "talk-dim"} aria-live="polite">
            {describeNote(warn.answer)}{warn.answer.frame_ts ? ` · ${asOf(warn.answer.frame_ts)}` : ""}
            {!warn.answer.people && " · the general message is below"}
          </div>
          {!!choices.length && (
            <div className="talk-chips" role="listbox" aria-label="Who to address">
              {choices.map((c) => (
                <button key={c.phrase} role="option" aria-selected={c.phrase === warn.phrase} className={`talk-chip ${c.phrase === warn.phrase ? "on" : ""}`}
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
          <div className="talk-row">
            <button disabled={busy || !warn.text.trim()} onClick={playWarn}
              title={tg.length > 1 ? `Speak this on ${tg.length} speakers` : "Speak this"}><Icon name="play" size={16} /> Play</button>
            <button className="ghost" disabled={busy} onClick={() => dispatch({ type: "close" })}>Cancel</button>
            <span className="spacer" />
            <button className="ghost" disabled={busy || cooling} onClick={describe} title="Describe the camera's picture again">Look again</button>
          </div>
        </div>
      )}

      {/* announced when what plays changes, not every tick of its clock */}
      <span className="sr-only" aria-live="polite">{bar ? (bar.live ? "Live talk on the speaker" : `Playing ${bar.message}`) : last?.text ?? ""}</span>
      <div>
        {bar ? (
          <div className="talk-bar">
            <div className="talk-bar-main">
              <div className="talk-bar-line">
                <span className="talk-rec" aria-hidden="true" />
                <strong>{bar.connecting ? "CONNECTING" : bar.live ? "LIVE" : "BROADCASTING"}</strong>
                <span className="talk-bar-msg">{bar.live ? "Live talk" : `'${bar.message}'`}</span>
                <span className="talk-bar-time">
                  {!bar.connecting && <>· {clock(bar.elapsed)}{bar.total ? ` / ${clock(Math.ceil(bar.total))}` : ""}</>}
                  {bar.speakers > 1 && ` · ${bar.speakers} speakers`}
                </span>
              </div>
              {bar.fraction !== null && <div className="talk-progress"><span style={{ width: `${Math.round(bar.fraction * 1000) / 10}%` }} /></div>}
            </div>
            <button className="talk-stopall" onClick={stopAll} aria-label="Stop all speakers"><Icon name="stop" size={16} /> {speakers.length > 1 ? "STOP ALL" : "STOP"}</button>
          </div>
        ) : last ? <div className={last.failed ? "talk-error" : "talk-dim"}>{last.text}</div> : null}
      </div>

      {deter && cameraId && <DeterrenceControl cameraId={cameraId} site={site} embedded />}

      <div className="talk-hw">
        <div className="talk-hw-title">Hardware state</div>
        <ul>
          {speakers.map((s) => {
            const k = speakerKey(s);
            const st = speakerState(status.by[k], status.health[k], s.online);
            return (
              <li key={k} className={`talk-hw-${st.tone}`}>
                <span className={`dot ${{ playing: "ok", connecting: "ok", ready: "ok", offline: "bad", failed: "bad", unknown: "idle" }[st.tone]}`} />
                <span className="talk-hw-name">{s.name}</span>
                <span className="talk-hw-state">{st.text}</span>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}
