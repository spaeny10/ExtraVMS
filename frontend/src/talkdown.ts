/**
 * Talk-down panel logic (TalkdownPanel.tsx), kept apart from the component so it is unit-tested: which speakers a camera
 * has, which speakers a choice in the panel addresses ("All speakers" = every one bound to the camera), the live status
 * line, and the checks on a one-off file before it is sent. On the hub a camera's speakers may be on several servers
 * (a Site's camera groups): each speaker carries its own server, and messages are per server (matched by name).
 */
import type { SiteApi, SpeakerStatus, TalkdownLibrary, TalkdownMessage } from "./api";

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

/** The toast after playing on one or several speakers: what started, and each speaker that refused. */
export function playOutcome(results: { speaker: string; ok: boolean; error?: string; replaced?: string | null }[]): { ok: string | null; failed: string[] } {
  const good = results.filter((r) => r.ok);
  const failed = results.filter((r) => !r.ok).map((r) => `${r.speaker}: ${r.error ?? "failed"}`);
  const replaced = good.filter((r) => r.replaced).map((r) => `'${r.replaced}'`);
  const ok = !good.length ? null
    : (good.length === 1 ? `Playing on ${good[0].speaker}` : `Playing on ${good.length} speakers`) + (replaced.length ? ` (replaced ${[...new Set(replaced)].join(", ")})` : "");
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
