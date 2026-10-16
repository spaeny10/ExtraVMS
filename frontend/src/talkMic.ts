/**
 * Hold to Talk in the browser (TalkdownPanel.tsx): the microphone (echo cancellation, noise suppression, automatic
 * gain), captured by an AudioWorklet (talkWorklet.js: a same-origin script, allowed by script-src 'self' on every page
 * this UI runs on) or, where a worklet can't load, a ScriptProcessorNode; downsampled to 8 kHz and cut into 40 ms
 * frames of 16-bit PCM (talkAudio.ts), sent to one WebSocket per speaker (api.talkUrl: the speaker's own server, through
 * the hub when the page is on the hub). One microphone, every target speaker: speakers on different servers each get
 * the same frames. The servers say "live" when their speaker plays it and "ended" when it is over (TalkMessage).
 * Frames captured before a socket opens wait for it (the server buffers them while the speaker connects).
 */
import type { TalkMessage } from "./api";
import { Downsampler, Framer, levelOf, pcm16le } from "./talkAudio";
import { micErrorText, talkCloseText } from "./talkdown";

// its own file next to the bundle (assets/talkWorklet-<hash>.js): never inlined as a data: URL, which script-src 'self' refuses
const WORKLET = new URL("./talkWorklet.js?no-inline", import.meta.url).href;
/** frames kept for a socket that hasn't opened yet (40 ms each: 2 s) */
const PENDING_MAX = 50;
/** after letting go, how long the servers get to play out what they have and say "ended" */
const CLOSE_GRACE_MS = 6000;

export type TalkTarget = { name: string; url: string };
export type TalkHandlers = {
  /** the first speaker plays it */
  onLive: () => void;
  /** the microphone's level of each 40 ms frame, 0..1 (talkAudio.levelOf) */
  onLevel: (level: number) => void;
  /** every socket has closed; `problems`: what went wrong, per speaker ("Gate: the speaker refused…"), empty when fine */
  onEnd: (problems: string[]) => void;
};
export type TalkSession = { release: () => void };

/** What this browser offers for talking (talkdown.micProblem decides). */
export function browserMic() {
  return {
    secure: typeof window !== "undefined" && window.isSecureContext,
    getUserMedia: typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia,
    audio: typeof AudioContext !== "undefined",
    webSocket: typeof WebSocket !== "undefined",
  };
}

type Sock = { target: TalkTarget; ws: WebSocket | null; pending: ArrayBuffer[]; closed: boolean; explained: boolean };

/**
 * Start talking: called in the press's event handler (the AudioContext is made there, so browsers let it run). The
 * microphone first: the speakers are only called once it is on, so a refused permission never interrupts a message
 * playing on them. release() ends it whenever: before the microphone answered, while connecting, or live.
 */
export function startTalk(targets: TalkTarget[], h: TalkHandlers): TalkSession {
  const ctx = new AudioContext({ latencyHint: "interactive" });
  ctx.resume().catch(() => {});
  let released = false;
  let finished = false;
  let live = false;
  let stream: MediaStream | null = null;
  let grace: ReturnType<typeof setTimeout> | null = null;
  const problems: string[] = [];
  const socks: Sock[] = [];

  const stopCapture = () => {
    stream?.getTracks().forEach((t) => t.stop());
    stream = null;
    ctx.close().catch(() => {});
  };
  const finish = () => {
    if (finished) return;
    finished = true;
    if (grace) clearTimeout(grace);
    stopCapture();
    socks.forEach((s) => { if (!s.closed) { s.closed = true; s.ws?.close(); } });
    h.onEnd(problems);
  };
  const checkDone = () => { if (socks.every((s) => s.closed)) finish(); };
  const say = (s: Sock, text: string) => { s.explained = true; problems.push(targets.length > 1 ? `${s.target.name}: ${text}` : text); };

  const open = (target: TalkTarget): Sock => {
    const s: Sock = { target, ws: null, pending: [], closed: false, explained: false };
    const ws = new WebSocket(target.url);
    s.ws = ws;
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      s.pending.forEach((f) => ws.send(f));
      s.pending = [];
      if (released) ws.send(JSON.stringify({ type: "end" }));
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data !== "string") return;
      let m: TalkMessage;
      try { m = JSON.parse(ev.data); } catch { return; }
      if (m.type !== "talk") return;
      if (m.state === "live" && !live) { live = true; h.onLive(); }
      else if (m.state === "refused") say(s, m.error);
      else if (m.state === "ended") {
        s.explained = true;
        if (m.result === "failed") say(s, m.error ?? "the speaker failed");
        else if (m.result === "replaced") say(s, "another message took over the speaker");
        else if (m.reason === "idle") say(s, "the audio stopped arriving");
      }
    };
    ws.onclose = (ev) => {
      s.closed = true;
      const why = s.explained ? null : talkCloseText(ev.code);
      if (why) say(s, why);
      checkDone();
    };
    return s;
  };

  const send = (frame: ArrayBuffer) => {
    for (const s of socks) {
      if (s.closed || !s.ws) continue;
      if (s.ws.readyState === WebSocket.OPEN) s.ws.send(frame);
      else if (s.ws.readyState === WebSocket.CONNECTING && s.pending.length < PENDING_MAX) s.pending.push(frame);
    }
  };

  const capture = async (media: MediaStream) => {
    const down = new Downsampler(ctx.sampleRate);
    const framer = new Framer();
    const onSamples = (block: Float32Array) => {
      if (released) return;
      for (const f of framer.push(down.push(block))) {
        h.onLevel(levelOf(f));
        send(pcm16le(f));
      }
    };
    const src = ctx.createMediaStreamSource(media);
    try {
      await ctx.audioWorklet.addModule(WORKLET);
      const node = new AudioWorkletNode(ctx, "talk-capture", { numberOfInputs: 1, numberOfOutputs: 0, channelCount: 1, channelCountMode: "explicit" });
      node.port.onmessage = (e: MessageEvent<Float32Array>) => onSamples(e.data);
      src.connect(node);
    } catch {
      if (released) return;
      // no worklet here (an old browser, a policy that blocks it): the deprecated main-thread processor still works
      const sp = ctx.createScriptProcessor(1024, 1, 1);
      sp.onaudioprocess = (e) => onSamples(new Float32Array(e.inputBuffer.getChannelData(0)));
      src.connect(sp);
      sp.connect(ctx.destination);   // it only runs when connected; its output is silence
    }
  };

  const session: TalkSession = {
    release: () => {
      if (released) return;
      released = true;
      stopCapture();
      if (!socks.length) { finish(); return; }   // the microphone hasn't answered yet: nothing to close
      for (const s of socks) if (s.ws?.readyState === WebSocket.OPEN) s.ws.send(JSON.stringify({ type: "end" }));
      grace = setTimeout(finish, CLOSE_GRACE_MS);
    },
  };

  (async () => {
    let media: MediaStream;
    try {
      media = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
    } catch (e) {
      if (!released) problems.push(micErrorText(e));
      finish();
      return;
    }
    if (released) {   // let go while the browser asked: nothing was called
      media.getTracks().forEach((t) => t.stop());
      return;
    }
    stream = media;
    targets.forEach((t) => socks.push(open(t)));
    if (!socks.length) { finish(); return; }
    try {
      await capture(media);
    } catch (e) {
      if (released) return;   // let go while it was starting: the context is closed, nothing to report
      problems.push(`The microphone could not be read (${(e as Error)?.message ?? "unknown error"})`);
      session.release();
    }
  })();

  return session;
}
