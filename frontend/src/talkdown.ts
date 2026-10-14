/**
 * Talk-down panel logic (TalkdownPanel.tsx), kept apart from the component so it is unit-tested: which speakers a camera
 * has, which speakers a choice in the panel addresses ("All speakers" = every one bound to the camera), the live status
 * line, and the checks on a one-off file before it is sent. On the hub a camera's speakers may be on several servers
 * (a Site's camera groups): each speaker carries its own server, and messages are per server (matched by name).
 * Describe & warn: the state of the reviewed message (warnReducer) between Qwen's answer and Play.
 */
import type { DescribeAnswer, SiteApi, SpeakerStatus, TalkdownLibrary, TalkdownMessage } from "./api";
import { type Spoken, voiceName } from "./talkdownVoice";

/** A speaker as a live tile knows it: GET /api/speakers on the server's own UI, the heartbeat summary on the hub. */
export type TalkSpeaker = {
  id: string; name: string; online?: boolean | null;
  /** the hub: the speaker's own server (ids are unique per server only) and its client, which play / stop / status
   *  use whatever server the camera is on. Absent on a server's own UI: the panel's `site`. */
  server?: string; site?: SiteApi;
};
/** A speaker's identity in the panel (its choice value and status key): server/id on the hub, the id on a server. */
export const speakerKey = (s: Pick<TalkSpeaker, "id" | "server">): string => (s.server ? `${s.server}/${s.id}` : s.id);
/** The servers some speakers are on, first seen first ("" = the panel's own `site`). */
export const serversOf = (speakers: TalkSpeaker[]): string[] => [...new Set(speakers.map((s) => s.server ?? ""))];
/** Where a speaker's calls go: per server ("" included), the client its speakers bring, else `fallback` (the panel's
 *  `site`, which on the hub is the camera's server: never used for a speaker that names its own). */
export function clientsOf<T>(speakers: { server?: string; site?: T }[], fallback: T): (server: string) => T {
  const m = new Map(speakers.map((s) => [s.server ?? "", s.site ?? fallback]));
  return (server) => m.get(server) ?? fallback;
}
/** The panel's "All speakers" choice. */
export const ALL = "__all";
export const UPLOAD_TYPES = [".mp3", ".wav", ".m4a", ".ogg"];
export const MAX_UPLOAD = 2 * 1024 * 1024;

/** camera id -> the speakers bound to it (enabled ones only), in speaker order. */
export function speakersByCamera(speakers: { id: string; name: string; camera_ids: string[]; enabled?: boolean; online?: boolean | null }[]): Record<string, TalkSpeaker[]> {
  const out: Record<string, TalkSpeaker[]> = {};
  for (const s of speakers) {
    if (s.enabled === false) continue;
    for (const cam of s.camera_ids ?? []) (out[cam] ??= []).push({ id: s.id, name: s.name, online: s.online });
  }
  return out;
}

/** Where the panel starts: the one speaker, or "All speakers" when the camera has several. */
export const defaultChoice = (speakers: TalkSpeaker[]): string => (speakers.length === 1 ? speakerKey(speakers[0]) : ALL);

/** The speakers a choice addresses: one, or every one for "All speakers" (an unknown choice: none). */
export function targets(choice: string, speakers: TalkSpeaker[]): TalkSpeaker[] {
  return choice === ALL ? speakers : speakers.filter((s) => speakerKey(s) === choice);
}

const sameName = (a: string, b: string) => a.trim().toLowerCase() === b.trim().toLowerCase();

/**
 * The messages a choice can play: the first target server's library (its available messages), keeping only those every
 * other target server has too under the same name (each server has its own library; the default ones match). Empty
 * until the first server's library is loaded.
 */
export function playableMessages(libs: Record<string, TalkdownLibrary | undefined>, servers: string[]): TalkdownMessage[] {
  const [first, ...rest] = servers;
  const own = libs[first ?? ""]?.messages.filter((m) => m.available) ?? [];
  return own.filter((m) => rest.every((srv) => messageOn(libs[srv], m) !== null));
}

/** The id of message `m` (of another server's library) in `lib`: an available message of the same name, or null. */
export function messageOn(lib: TalkdownLibrary | undefined, m: Pick<TalkdownMessage, "name">): number | null {
  return lib?.messages.find((x) => x.available && sameName(x.name, m.name))?.id ?? null;
}

export const isActive = (st: SpeakerStatus | null | undefined): boolean => st?.state === "playing" || st?.state === "connecting";

/**
 * The live status line: "Playing 'Leave now' · 4 s", "Connecting…", then how the last one ended. `sinceFetch` is the
 * seconds since the status was read, so the counter keeps running between polls.
 */
export function statusText(st: SpeakerStatus | null | undefined, sinceFetch = 0): string {
  if (!st) return "…";
  if (st.state === "connecting") return `Connecting to play '${st.message}'…`;
  if (st.state === "playing") {
    const s = Math.max(0, Math.floor((st.elapsed_s ?? 0) + sinceFetch));
    return `Playing '${st.message}' · ${st.duration_s ? `${Math.min(s, Math.ceil(st.duration_s))} / ${Math.ceil(st.duration_s)} s` : `${s} s`}`;
  }
  const l = st.last;
  if (!l) return "Ready";
  if (l.result === "failed") return `Failed: ${l.error ?? "unknown error"}`;
  return `${{ played: "Played", stopped: "Stopped", replaced: "Replaced" }[l.result]} '${l.message}'`;
}

/** Why this file can't be sent as a one-off message, or null. */
export function uploadProblem(name: string, size: number, max = MAX_UPLOAD, types = UPLOAD_TYPES): string | null {
  const ext = name.includes(".") ? name.slice(name.lastIndexOf(".")).toLowerCase() : "";
  if (!types.includes(ext)) return `Choose an ${types.map((t) => t.slice(1)).join(", ")} file`;
  if (size === 0) return "The file is empty";
  if (size > max) return `The file is larger than ${Math.round(max / (1024 * 1024))} MB`;
  return null;
}

/** The toast after playing on one or several speakers: what started, and each speaker that refused. A text spoken by
 *  a fallback (the Site's voice isn't installed on that server: the answer's `tts`) says who speaks instead. */
export function playOutcome(results: { speaker: string; ok: boolean; error?: string; replaced?: string | null; tts?: Pick<Spoken, "fallback" | "label" | "requested"> | null }[]): { ok: string | null; failed: string[] } {
  const good = results.filter((r) => r.ok);
  const failed = results.filter((r) => !r.ok).map((r) => `${r.speaker}: ${r.error ?? "failed"}`);
  const replaced = good.filter((r) => r.replaced).map((r) => `'${r.replaced}'`);
  const fb = good.map((r) => r.tts).find((t) => t?.fallback);
  const ok = !good.length ? null
    : (good.length === 1 ? `Playing on ${good[0].speaker}` : `Playing on ${good.length} speakers`) + (replaced.length ? ` (replaced ${[...new Set(replaced)].join(", ")})` : "")
      + (fb ? ` · spoken by ${fb.label} (${voiceName(fb.requested)} isn't installed there)` : "");
  return { ok, failed };
}

/** A File as base64 (the server takes JSON only). */
export function fileToBase64(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).replace(/^data:[^,]*,/, ""));
    r.onerror = () => reject(r.error ?? new Error("could not read the file"));
    r.readAsDataURL(file);
  });
}

// ---------------------------------------------------------------- Describe & warn

/** At most one Describe & warn this often from a panel (the server refuses faster ones too). */
export const DESCRIBE_EVERY_MS = 3000;

/** The message: "<phrase>: <template text>", or the template text alone (capitalised) without a phrase; the same rule
 *  as the server's talkdown_describe.fill, so picking another template or person here gives what the server would. */
export function fillTemplate(body: string, phrase: string | null): string {
  if (phrase) return `${phrase}: ${body}`;
  return body.charAt(0).toUpperCase() + body.slice(1);
}

/** idle (the button) → looking (waiting for Qwen) → ready (template, person, the editable text) → Play or Cancel → idle. */
export type Warn =
  | { stage: "idle" }
  | { stage: "looking" }
  | { stage: "ready"; answer: DescribeAnswer; template: string; phrase: string | null; text: string; edited: boolean };
export type WarnAction =
  | { type: "look" } | { type: "answer"; answer: DescribeAnswer } | { type: "failed" } | { type: "close" }
  | { type: "template"; id: string } | { type: "pick"; phrase: string | null } | { type: "edit"; text: string };
export const WARN_IDLE: Warn = { stage: "idle" };

/** Picking a template or a person refills the text (an explicit choice: it replaces edits); typing marks it edited.
 *  An answer that arrives after Cancel is dropped. */
export function warnReducer(s: Warn, a: WarnAction): Warn {
  switch (a.type) {
    case "look":
      return { stage: "looking" };
    case "answer":
      return s.stage === "looking"
        ? { stage: "ready", answer: a.answer, template: a.answer.template, phrase: a.answer.phrase, text: a.answer.text, edited: false }
        : s;
    case "failed":
    case "close":
      return WARN_IDLE;
    case "template":
    case "pick": {
      if (s.stage !== "ready") return s;
      const template = a.type === "template" ? a.id : s.template;
      const phrase = a.type === "pick" ? a.phrase : s.phrase;
      const body = s.answer.templates.find((t) => t.id === template)?.text ?? "";
      return { ...s, template, phrase, text: fillTemplate(body, phrase), edited: false };
    }
    case "edit":
      return s.stage === "ready" ? { ...s, text: a.text, edited: true } : s;
  }
}

/** What Qwen saw, in a few words: "Nobody visible right now", "1 person", "3 people (Qwen isn't sure)", or the error. */
export function describeNote(a: DescribeAnswer): string {
  if (a.people === null) return a.error ?? "The camera could not be described";
  if (a.people === 0) return "Nobody visible right now";
  const n = a.people === 1 ? "1 person" : `${a.people} people`;
  return a.confidence === "low" ? `${n} (Qwen isn't sure)` : n;
}

/** The people to address when several are in view: everyone (the group phrase) and each one Qwen described (at most 3). */
export function warnChoices(a: DescribeAnswer): { label: string; phrase: string }[] {
  if (!a.people || a.people < 2) return [];
  const each = a.options.map((o) => ({ label: o.phrase.replace(/^You /, ""), phrase: o.phrase }));
  return [{ label: "Everyone", phrase: a.group_phrase ?? "You there" }, ...each];
}

/** "as of 3:21:05 PM": when the picture Qwen looked at was taken. */
export const asOf = (ts: number): string =>
  `as of ${new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit" })}`;
