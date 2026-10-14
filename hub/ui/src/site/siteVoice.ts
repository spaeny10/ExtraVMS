/**
 * Pure helpers for a Site's talk-down voice on the hub (Site › Settings › Cameras, CamerasBox VoiceCard; hub
 * talkdown_voice.py): which server answers a Preview, and the per-server line under the voice ("installed ✓",
 * "not installed → Windows voice", offline / not applied yet / too old). Who would speak is @site/talkdownVoice predict,
 * the servers' own fallback order.
 */
import { type VoiceSetting, predict, sameSetting } from "@site/talkdownVoice";
import type { SiteTalkdownVoice, SiteVoiceServer } from "../api";

/**
 * The server a Preview of `voice` is asked from: an online one that has it installed (the sample is the voice itself),
 * else an online one that takes voices at all (its fallback speaks and the preview says so); null = none online.
 */
export function previewServer(servers: SiteVoiceServer[], voice: string | null): SiteVoiceServer | null {
  const live = servers.filter((s) => s.online && s.supported);
  return live.find((s) => predict(voice, s).ok) ?? live[0] ?? null;
}

/** One server's line: who would speak `draft` there and whether it has taken the saved setting yet. `ok`: null = not applicable. */
export function serverLine(s: SiteVoiceServer, draft: string | null, saved: VoiceSetting | null): { text: string; ok: boolean | null } {
  if (!s.supported) return { text: "its version has no talk-down voices: update the server", ok: null };
  const p = predict(draft, s);
  let text = p.text;
  if (saved && !s.in_sync && !sameSetting(s.setting, saved)) text += s.online ? " · applying…" : " · offline: takes it when it reconnects";
  return { text, ok: p.ok };
}

/** "Saved: 2 servers took it" / "… Bravo didn't answer: it gets it on its next heartbeat". */
export function pushText(pushed: NonNullable<SiteTalkdownVoice["pushed"]>): string {
  const ok = pushed.filter((p) => p.ok).length;
  const failed = pushed.filter((p) => !p.ok && !p.skipped).map((p) => p.server_name);
  let t = ok ? `${ok} server${ok === 1 ? "" : "s"} took it` : "no server online took it yet";
  if (failed.length) t += `; ${failed.join(", ")} didn't answer: ${failed.length === 1 ? "it gets" : "they get"} it on the next heartbeat`;
  return t;
}
