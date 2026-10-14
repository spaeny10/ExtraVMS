/**
 * The talk-down voice (backend tts.py; on the hub, talkdown_voice.py): the Piper voice catalog, the voice setting and the
 * pure logic of the two voice sections (a server's Settings › Speakers, the hub's Site › Settings › Cameras): which
 * servers have a voice installed, who would speak it on each (the server's own fallback order: tts.plan), the labels.
 * The catalog is backend tts.CATALOG (backend test_tts checks that the ids and labels agree).
 */

/** {voice, speed, chime}: voice null = automatic (the server's first installed voice, in catalog order). */
export type VoiceSetting = { voice: string | null; speed: number; chime: boolean };
export type CatalogVoice = { id: string; name: string; label: string; gender: "female" | "male" };
/** Who spoke (or would speak) a text: backend tts.Spoken.public. */
export type Spoken = { engine: string; voice: string | null; requested: string | null; fallback: boolean; reason: string | null; label: string };
/** The engines a server has: Piper ("piper-tts" in-process or "piper-binary"), Windows SAPI, espeak (its exe). null = not probed yet. */
export type VoiceEngines = { piper?: string | null; sapi?: boolean | null; espeak?: string | null };
/** What a server says about its voices: the hub's per-server status, or one server's own /api/talkdown/voices. */
export type ServerVoices = { voices: string[]; engines: VoiceEngines };

export const SPEED_MIN = 0.7;
export const SPEED_MAX = 1.3;
export const DEFAULT_VOICE: VoiceSetting = { voice: null, speed: 1, chime: false };
export const PREVIEW_MAX = 300;

/** Good English voices of rhasspy/piper-voices (Hugging Face), the automatic choice's preference order. */
export const VOICE_CATALOG: CatalogVoice[] = [
  { id: "en_US-lessac-medium", name: "Lessac", label: "US English · female · clear", gender: "female" },
  { id: "en_US-amy-medium", name: "Amy", label: "US English · female · warm", gender: "female" },
  { id: "en_US-kristin-medium", name: "Kristin", label: "US English · female · calm", gender: "female" },
  { id: "en_US-hfc_female-medium", name: "HFC female", label: "US English · female · bright", gender: "female" },
  { id: "en_US-ryan-high", name: "Ryan", label: "US English · male · deep", gender: "male" },
  { id: "en_US-joe-medium", name: "Joe", label: "US English · male · firm", gender: "male" },
  { id: "en_US-hfc_male-medium", name: "HFC male", label: "US English · male · steady", gender: "male" },
  { id: "en_GB-alan-medium", name: "Alan", label: "British English · male · measured", gender: "male" },
  { id: "en_GB-jenny_dioco-medium", name: "Jenny", label: "British English · female · crisp", gender: "female" },
];
const BY_ID = new Map(VOICE_CATALOG.map((v) => [v.id, v]));

/** "Amy" for a catalog voice, the id for any other one, "Automatic" for null. */
export function voiceName(id: string | null | undefined): string {
  if (!id) return "Automatic";
  return BY_ID.get(id)?.name ?? id;
}

/** "Amy · US English · female · warm" (a catalog voice), the id otherwise, or what automatic means. */
export function voiceLabel(id: string | null | undefined): string {
  if (!id) return "Automatic · the best voice each server has";
  const c = BY_ID.get(id);
  return c ? `${c.name} · ${c.label}` : id;
}

export function clampSpeed(s: number): number {
  if (!Number.isFinite(s)) return 1;
  return Math.round(Math.min(SPEED_MAX, Math.max(SPEED_MIN, s)) * 100) / 100;
}

/** "1.0× · normal", "0.8× · slower", "1.2× · faster". */
export function speedText(s: number): string {
  const v = clampSpeed(s);
  return `${v.toFixed(Math.round(v * 100) % 10 ? 2 : 1)}× · ${v === 1 ? "normal" : v < 1 ? "slower" : "faster"}`;
}

const lang = (id: string) => id.split("-")[0].split("_")[0];

/** The server's own fallback order among its installed Piper voices (backend tts._piper_order): same language, then
 *  same gender as the voice asked for, then catalog order (the installed list comes catalog-first). */
function piperOrder(voice: string | null, installed: string[]): string[] {
  const ordered = [...installed].sort((a, b) => {
    const ia = BY_ID.has(a) ? VOICE_CATALOG.findIndex((v) => v.id === a) : VOICE_CATALOG.length;
    const ib = BY_ID.has(b) ? VOICE_CATALOG.findIndex((v) => v.id === b) : VOICE_CATALOG.length;
    return ia - ib || a.localeCompare(b);
  });
  if (!voice) return ordered;
  const want = ordered.includes(voice) ? [voice] : [];
  const l = voice.includes("-") ? lang(voice) : "";
  const g = BY_ID.get(voice)?.gender;
  const rank = (id: string) => (l && lang(id) === l ? 0 : 2) + (g && BY_ID.get(id)?.gender === g ? 0 : 1);
  const rest = ordered.filter((id) => id !== voice);
  return [...want, ...rest.map((id, i) => ({ id, i })).sort((a, b) => rank(a.id) - rank(b.id) || a.i - b.i).map((x) => x.id)];
}

/** Who would speak `voice` on a server: kind "voice" (the voice itself), "piper" (another installed voice), "sapi",
 *  "espeak" or "none"; `ok` = the voice itself (or automatic picking a Piper voice). */
export type Prediction = { kind: "voice" | "piper" | "sapi" | "espeak" | "none"; voice: string | null; ok: boolean; text: string };

export function predict(voice: string | null, srv: ServerVoices): Prediction {
  const e = srv.engines ?? {};
  const order = e.piper ? piperOrder(voice, srv.voices ?? []) : [];
  if (order.length) {
    const first = order[0];
    if (!voice || first === voice) return { kind: "voice", voice: first, ok: true, text: voice ? "installed ✓" : `${voiceName(first)} ✓` };
    return { kind: "piper", voice: first, ok: false, text: `not installed → ${voiceName(first)}` };
  }
  const why = voice && !e.piper && (srv.voices ?? []).includes(voice) ? "Piper not installed" : voice ? "not installed" : "no Piper voice";
  if (e.sapi) return { kind: "sapi", voice: null, ok: false, text: `${why} → Windows voice` };
  if (e.espeak) return { kind: "espeak", voice: null, ok: false, text: `${why} → ${e.espeak}` };
  return { kind: "none", voice: null, ok: false, text: e.sapi === null || e.sapi === undefined ? "not reported yet" : "no text-to-speech" };
}

/** The dropdown: automatic, the catalog, then any other voice a server has; each with the servers lacking it. */
export type VoiceOption = { id: string | null; label: string; missing: string[]; installed: number };

export function voiceOptions(servers: (ServerVoices & { name: string })[]): VoiceOption[] {
  const extra = [...new Set(servers.flatMap((s) => s.voices ?? []))].filter((id) => !BY_ID.has(id)).sort();
  const ids = [...VOICE_CATALOG.map((v) => v.id), ...extra];
  const auto: VoiceOption = { id: null, label: voiceLabel(null), missing: [], installed: servers.length };
  return [auto, ...ids.map((id) => {
    const missing = servers.filter((s) => !(s.voices ?? []).includes(id)).map((s) => s.name);
    return { id, label: voiceLabel(id), missing, installed: servers.length - missing.length };
  })];
}

/** The dropdown text of an option: "Amy · US English · female · warm — not installed on Qwenbot" / "— not installed". */
export function optionText(o: VoiceOption, total: number): string {
  if (o.id === null || !o.missing.length) return o.label;
  if (o.missing.length === total) return `${o.label} — not installed`;
  return `${o.label} — not on ${o.missing.join(", ")}`;
}

/** Under a preview: "Amy", or "Windows voice — Ryan isn't installed there" when the server fell back. */
export function previewText(p: { engine: string | null; voice: string | null; fallback: boolean }, requested: string | null): string {
  const who = p.engine === "piper" ? voiceName(p.voice) : p.engine === "sapi" ? "Windows voice" : (p.engine ?? "?");
  return p.fallback && requested ? `${who} — ${voiceName(requested)} isn't installed there` : who;
}

/** "Piper · Windows voice · espeak-ng" / "none": the engines a server reported (unknown ones left out). */
export function enginesText(e: VoiceEngines | null | undefined): string {
  const parts = [e?.piper ? "Piper" : null, e?.sapi ? "Windows voice" : null, e?.espeak ?? null].filter(Boolean);
  return parts.length ? parts.join(" · ") : "none";
}

export const sameSetting =(a: VoiceSetting | null | undefined, b: VoiceSetting | null | undefined): boolean =>
  !!a && !!b && (a.voice ?? null) === (b.voice ?? null) && clampSpeed(a.speed) === clampSpeed(b.speed) && !!a.chime === !!b.chime;
