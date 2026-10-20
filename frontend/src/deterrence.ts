/**
 * Camera deterrence (backend deterrence.py): a camera's own strobe, siren and white light (its ONVIF auxiliary commands
 * or alarm relays), switched from the live tile, the talk-down panel and the SOC incident view (DeterrencePanel.tsx), and the
 * per-camera automatic rules edited in Settings › Cameras (CameraDialog.tsx); Milesight's own web-API extras on the
 * Camera tab (DeviceSettings.tsx). The reply types and the pure logic, kept apart from the components so they are
 * unit-tested: which buttons a camera gets, the countdowns between polls, the rules' defaults and checks before a PUT,
 * and the Milesight form's changes as a PUT body.
 */

export type DeterAction = "strobe" | "siren" | "light";
export const DETER_ACTIONS: readonly DeterAction[] = ["strobe", "siren", "light"];

/** GET /api/cameras: what the camera can drive (null = nothing, or not asked yet). */
export type DeterrenceBrief = { strobe: boolean; siren: boolean; light: boolean; relays: number };
/**
 * How an action reaches the camera: an auxiliary command, an alarm relay (a monostable one pulses `pulse_s` by itself), or
 * for the siren a talk-down speaker linked to the camera (the server's built-in siren sound; `target` is the speaker).
 */
export type DeterAct = {
  via: "aux" | "relay" | "speaker"; target: string; label: string; timed: boolean; pulse_s: number | null;
  speaker_id?: string; speaker?: string | null; own_speaker?: boolean;
};
/** The camera's setting for the siren: automatic (its own siren only when it can sound, else a linked speaker), or pinned. */
export type SirenSource = "auto" | "relay" | "speaker";
export const SIREN_SOURCES: readonly SirenSource[] = ["auto", "relay", "speaker"];
export type DeterConfig = { siren_source: SirenSource };
/**
 * Which way the siren goes now (backend deterrence.resolve_siren): "relay" = the camera's own siren output, "speaker" = the
 * built-in siren on a linked speaker, null = no siren. `camera_sounds`: a Milesight with a sound file loaded (true), with
 * none (false), or unknown (null: another maker, or its files couldn't be read).
 */
export type SirenRoute = {
  via: "relay" | "speaker" | null; source: SirenSource; speaker_id: string | null; speaker: string | null; own_speaker: boolean;
  camera_sounds: boolean | null; has_output: boolean; speakers: number; reason: string;
};
export type DeterRelay = { token: string; mode: "bistable" | "monostable"; delay_s: number; idle_state: string | null; role: "strobe" | "siren" | null };
/** One output's state; `remaining_s` counts down from the server's `now` (null = on until switched off). */
export type DeterOutput = { on: boolean; until: number | null; remaining_s: number | null; by: string | null; started_at: number | null };
export type DeterLast = {
  ts: number; action: string; on: boolean; seconds: number | null; by: string; source: "manual" | "rule"; result: "ok" | "failed"; error: string | null;
  /** how it went: relay | aux | speaker (a stop lists each, comma-separated) */
  via?: string | null;
};
/** GET / POST /api/cameras/{id}/deterrence. `state` keys: "strobe" | "siren" | "light" | "relay:<token>". */
export type Deterrence = {
  camera_id: string; probed: boolean; available: boolean; error: string | null; milesight: boolean; audio_outputs: number;
  default_s: number; max_s: number; now: number;
  actions: Record<DeterAction, DeterAct | null>;
  relays: DeterRelay[];
  state: Record<string, DeterOutput>;
  last: DeterLast | null;
  /** which way the siren goes (null before the camera answered the probe) */
  siren?: SirenRoute | null;
};
export type DeterRequest = { action: DeterAction | "stop" | "relay"; on?: boolean; token?: string; seconds?: number };

/** Automatic rules: on a verified event of these labels (in `zone`; null = anywhere) switch these outputs on. */
export type DeterLabel = "person" | "vehicle";
export type DeterRule = {
  id: string; name: string; enabled: boolean; labels: DeterLabel[]; zone: string | null;
  /** armed = only while the Site's SOC monitoring is armed (a stand-alone server never is) */
  when: "always" | "armed";
  actions: DeterAction[]; seconds: number;
  /** optionally play this talk-down message on that speaker (one linked to the camera) */
  speaker_id: string | null; message_id: number | null;
  cooldown_s: number;
};
export type DeterRules = {
  rules: DeterRule[]; zones: string[]; speakers: { id: string; name: string }[]; messages: { id: number; name: string }[];
  armed: { known: boolean; armed: boolean | null; reason: string | null };
  caps: Record<DeterAction, boolean>;
  /** the camera's deterrence settings, and which way its siren goes with them */
  config?: DeterConfig;
  siren?: SirenRoute | null;
};

export const DEFAULT_S = 10;
export const MAX_S = 120;
export const MAX_RULES = 8;
export const MAX_COOLDOWN_S = 3600;
/** Re-read the state this often while an output runs (the countdown ticks locally between), and while idle. */
export const RUN_POLL_MS = 2000;
export const IDLE_POLL_MS = 10000;
export const TICK_MS = 250;

/** "409 {"detail": …}" -> 409; 0 when the error carries no status (the network). */
export const errorStatus = (e: unknown): number => Number(/^(?:Error: )?(\d{3})\b/.exec(String(e instanceof Error ? e.message : e))?.[1] ?? 0);

/** Does the camera list's brief say there is something to drive (the tile's button)? */
export function hasDeterrence(b: DeterrenceBrief | null | undefined): boolean {
  return !!b && (b.strobe || b.siren || b.light || b.relays > 0);
}

const NAMES: Record<DeterAction, string> = { strobe: "Strobe", siren: "Siren", light: "Light" };
const ICONS: Record<DeterAction, string> = { strobe: "zap", siren: "siren", light: "bulb" };
export const actionName = (a: DeterAction) => NAMES[a];

export type DeterButton = {
  /** the state key ("strobe", "relay:<token>") */
  key: string; action: DeterAction | "relay"; token?: string; label: string; icon: string; title: string; timed: boolean;
};

/** The buttons a camera gets: strobe, siren, light (those it has), then relays no action uses (by number; token in the title). */
export function deterButtons(d: Pick<Deterrence, "actions" | "relays">): DeterButton[] {
  const out: DeterButton[] = [];
  for (const a of DETER_ACTIONS) {
    const act = d.actions[a];
    if (!act) continue;
    const how = act.pulse_s ? ` (pulses ${act.pulse_s} s by itself)` : act.timed ? "" : " (stays on until switched off)";
    const title = act.via === "speaker" ? sirenSpeakerTitle(act) : `${act.label || NAMES[a]}${how}`;
    out.push({ key: a, action: a, label: NAMES[a], icon: ICONS[a], title, timed: act.timed });
  }
  const used = new Set(DETER_ACTIONS.map((a) => d.actions[a]).filter((x) => x?.via === "relay").map((x) => x!.target));
  d.relays.forEach((r, i) => {
    if (r.role || used.has(r.token)) return;
    const pulse = r.mode === "monostable";
    out.push({ key: `relay:${r.token}`, action: "relay", token: r.token, label: `Output ${i + 1}`, icon: "toggle",
      title: `Alarm output ${r.token}${pulse ? ` (pulses ${r.delay_s} s by itself)` : ""}`, timed: !pulse });
  });
  return out;
}

/** The siren button's title when it sounds through a talk-down speaker: "Siren — through the camera's speaker". */
export function sirenSpeakerTitle(act: Pick<DeterAct, "own_speaker" | "speaker" | "target">): string {
  return `Siren — through ${act.own_speaker ? "the camera's speaker" : `speaker ${act.speaker || act.target}`}`;
}

/** An output now, `sinceFetch` seconds after it was read: on, and the seconds left (null = on until switched off). */
export function outputNow(st: DeterOutput | null | undefined, sinceFetch: number): { on: boolean; left: number | null } {
  if (!st?.on) return { on: false, left: null };
  if (st.remaining_s == null) return { on: true, left: null };
  const left = Math.max(0, st.remaining_s - Math.max(0, sinceFetch));
  return { on: left > 0, left };
}

export const anyOn = (d: Pick<Deterrence, "state"> | null, sinceFetch: number): boolean =>
  !!d && Object.values(d.state).some((st) => outputNow(st, sinceFetch).on);

/** "8 s", "1:05"; "on" for an output with no end. */
export function countdownText(left: number | null): string {
  if (left == null) return "on";
  const s = Math.ceil(left);
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** The seconds picker's choices: a few steps up to the camera's maximum, and its default. */
export function secondsChoices(max = MAX_S, def = DEFAULT_S): number[] {
  return [...new Set([5, 10, 30, 60, 120, def].filter((s) => s >= 1 && s <= max))].sort((a, b) => a - b);
}

/** What a click sends: a running output switches off; otherwise on, for `seconds` when the server times it. */
export function deterRequest(b: Pick<DeterButton, "action" | "token" | "timed">, running: boolean, seconds: number): DeterRequest {
  const base: DeterRequest = { action: b.action, ...(b.token ? { token: b.token } : {}) };
  if (running) return { ...base, on: false };
  return { ...base, on: true, ...(b.timed ? { seconds: clampInt(seconds, 1, MAX_S, DEFAULT_S) } : {}) };
}

/** The last action, as one line: "Siren on for 10 s · rule Yard at night", "Everything off · op@x · failed: …". */
export function lastText(last: DeterLast | null | undefined, buttons: DeterButton[] = []): string {
  if (!last) return "";
  const what = last.action === "stop" ? "Everything off"
    : `${buttons.find((b) => b.key === last.action)?.label ?? (NAMES[last.action as DeterAction] || last.action)} ${last.on ? "on" : "off"}`
      + (last.on && last.seconds ? ` for ${last.seconds} s` : "") + (last.action === "siren" && last.via === "speaker" ? " through the speaker" : "");
  const who = last.source === "rule" ? `rule ${last.by}` : last.by;
  return [what, who, last.result === "failed" ? `failed${last.error ? `: ${last.error}` : ""}` : ""].filter(Boolean).join(" · ");
}

/** Why the control has nothing to offer (null: it has buttons). */
export function unavailableText(d: Pick<Deterrence, "probed" | "available" | "error" | "actions" | "relays">): string | null {
  if (!d.probed) return "The camera hasn't been asked what it can drive yet; try again in a few seconds.";
  if (!d.available) return d.error ? `Not available: ${d.error}` : "This camera has no strobe, siren, light or alarm output.";
  return deterButtons(d).length ? null : "This camera has no strobe, siren, light or alarm output.";
}

function clampInt(v: unknown, min: number, max: number, def: number): number {
  const n = Math.round(Number(v));
  return Number.isFinite(n) && v !== "" && v !== null ? Math.min(max, Math.max(min, n)) : def;
}

/* ------------------------------------------------------------------ the siren's way (the rules editor's setting) */

const SOURCE_NAMES: Record<SirenSource, string> = {
  auto: "Automatic",
  relay: "The camera's own siren",
  speaker: "A linked talk-down speaker",
};
export const sirenSourceName = (s: SirenSource) => SOURCE_NAMES[s];

/** The setting's choices: automatic always; a pinned way only where the camera has it (or it is the saved setting). */
export function sirenSourceChoices(route: Pick<SirenRoute, "has_output" | "speakers"> | null | undefined, saved: SirenSource = "auto"): SirenSource[] {
  return SIREN_SOURCES.filter((s) => s === "auto" || s === saved
    || (s === "relay" ? !!route?.has_output : (route?.speakers ?? 0) > 0));
}

/** One line under the setting: which way the siren goes now, and why. */
export function sirenWayText(route: SirenRoute | null | undefined): string {
  if (!route) return "The camera hasn't been asked what it can drive yet.";
  const why = route.reason ? ` (${route.reason})` : "";
  if (route.via === "relay") return `The siren sounds on the camera's own siren output${why}.`;
  if (route.via === "speaker") {
    return `The siren sounds through ${route.own_speaker ? "the camera's speaker" : `speaker ${route.speaker || route.speaker_id}`}${why}.`;
  }
  return `No siren${why}.`;
}

/* ------------------------------------------------------------------ automatic rules */

/** A rule added in the editor: on, people, only while armed; the outputs are the user's pick. */
export function newRule(existing: Pick<DeterRule, "name">[] = []): DeterRule {
  let n = existing.length + 1;
  while (existing.some((r) => r.name === `Rule ${n}`)) n++;
  return { id: "", name: `Rule ${n}`, enabled: true, labels: ["person"], zone: null, when: "armed", actions: [], seconds: DEFAULT_S,
    speaker_id: null, message_id: null, cooldown_s: 120 };
}

/** One rule as the server takes it: trimmed, ranges clamped, no duplicates, a message only with a speaker. */
export function normalizeRule(r: DeterRule, i = 0): DeterRule {
  const speaker = r.speaker_id || null;
  return {
    id: r.id ?? "", name: r.name.trim() || `Rule ${i + 1}`, enabled: !!r.enabled,
    labels: (["person", "vehicle"] as const).filter((l) => r.labels.includes(l)),
    zone: r.zone || null, when: r.when === "always" ? "always" : "armed",
    actions: DETER_ACTIONS.filter((a) => r.actions.includes(a)),
    seconds: clampInt(r.seconds, 1, MAX_S, DEFAULT_S),
    speaker_id: speaker, message_id: speaker && r.message_id != null ? r.message_id : null,
    cooldown_s: clampInt(r.cooldown_s, 0, MAX_COOLDOWN_S, 120),
  };
}
export const normalizeRules = (rules: DeterRule[]): DeterRule[] => rules.map(normalizeRule);

/** What keeps a rule from saving (empty = fine). `caps`: the outputs the camera has. */
export function ruleProblems(r: DeterRule, caps: Record<DeterAction, boolean>): string[] {
  const out: string[] = [];
  if (!r.labels.length) out.push("pick people, vehicles or both");
  const missing = r.actions.filter((a) => !caps[a]);
  if (missing.length) out.push(`this camera has no ${missing.map((a) => NAMES[a].toLowerCase()).join(" or ")}`);
  if (!!r.speaker_id !== (r.message_id != null)) out.push("pick both a speaker and a message, or neither");
  if (!r.actions.length && !r.speaker_id) out.push("pick an output or a talk-down message");
  const s = Number(r.seconds), c = Number(r.cooldown_s);
  if (!Number.isFinite(s) || s < 1 || s > MAX_S) out.push(`seconds must be 1–${MAX_S}`);
  if (!Number.isFinite(c) || c < 0 || c > MAX_COOLDOWN_S) out.push(`cooldown must be 0–${MAX_COOLDOWN_S} s`);
  return out;
}

/** Every rule's problems, named ("Rule 2: pick an output…"), and the cap on how many. */
export function rulesProblems(rules: DeterRule[], caps: Record<DeterAction, boolean>): string[] {
  const out = rules.flatMap((r, i) => ruleProblems(r, caps).map((p) => `${r.name.trim() || `Rule ${i + 1}`}: ${p}`));
  if (rules.length > MAX_RULES) out.push(`at most ${MAX_RULES} rules per camera`);
  return out;
}

/** Unsaved changes: the rules as they would be sent differ from the saved ones. */
export const rulesDirty = (saved: DeterRule[], edit: DeterRule[]): boolean =>
  JSON.stringify(normalizeRules(saved)) !== JSON.stringify(normalizeRules(edit));

/** The armed line over the editor (rules set to "only while armed" only fire while it says armed). */
export function armedText(a: DeterRules["armed"]): { text: string; tone: "ok" | "idle" | "warn" } {
  if (!a.known) return { text: `No SOC schedule from the hub: rules set to "only while armed" won't fire${a.reason ? ` (${a.reason})` : ""}.`, tone: "warn" };
  return a.armed ? { text: "Site armed now.", tone: "ok" } : { text: `Site not armed now${a.reason ? ` (${a.reason})` : ""}.`, tone: "idle" };
}

/** Nothing a rule could do here: no outputs and no linked speaker (the editor says so instead). */
export const rulesHaveNothing = (r: Pick<DeterRules, "caps" | "speakers">): boolean =>
  !DETER_ACTIONS.some((a) => r.caps[a]) && !r.speakers.length;

/**
 * Offer "Use this camera's speaker for talk-down": the camera has an audio output and no speaker at its address is
 * linked to it yet (a speaker made from it, or a separate one on the same host).
 */
export function offerCameraSpeaker(d: Pick<Deterrence, "audio_outputs"> | null | undefined, speakers: { host: string; camera_ids: string[] }[],
  cam: { id: string; host: string }): boolean {
  if (!d || d.audio_outputs <= 0) return false;
  return !speakers.some((s) => s.host === cam.host && (s.camera_ids ?? []).includes(cam.id));
}

/* ------------------------------------------------------------------ Milesight extras (backend milesight.py) */

type MsPart = { supported: boolean; reason?: string };
export type MsWhiteLightMode = "auto" | "on" | "off" | "schedule";
export type MsWhiteLight = MsPart & { mode: MsWhiteLightMode; sensitivity: number; delay_s: number; start: string; stop: string; brightness: number; writable: boolean };
export type MsRedBlue = MsPart & { mode: "red_blue" | "red" | "blue"; frequency: "low" | "medium" | "high"; writable: boolean };
export type MsSiren = MsPart & { files: { index: number; name: string; present: boolean }[]; play_index: number; speaker_volume: number;
  volume_writable: boolean; file_writable: boolean };
export type MsDisarm = MsPart & { enable: boolean; mutes: Record<string, boolean>; writable: boolean };
/** GET /api/cameras/{id}/device/milesight; the parts are absent when the camera isn't a Milesight that answers. */
export type MilesightExtras = MsPart & { white_light?: MsWhiteLight; red_blue?: MsRedBlue; siren?: MsSiren; disarm?: MsDisarm };
export type MilesightWrite = {
  white_light?: Partial<Pick<MsWhiteLight, "mode" | "sensitivity" | "delay_s" | "start" | "stop" | "brightness">>;
  red_blue?: Partial<Pick<MsRedBlue, "mode" | "frequency">>;
  speaker_volume?: number;
  disarm?: { enable?: boolean; mutes?: Record<string, boolean> };
};

/** The camera's own alarm-linkage actions one-click disarm can mute, in the camera's order. */
export const MUTE_LABELS: Record<string, string> = {
  record: "Recording", snapshot: "Snapshot", output: "Alarm output", audio: "Audio alarm", sip: "SIP call", http: "HTTP notification",
  warningLight: "Warning light", autoTracking: "Auto tracking", appPush: "App push", mqtt: "MQTT",
};

/** The form: numbers as typed text (checked by milesightProblems), the rest as read. */
export type MilesightDraft = {
  white_light: { mode: MsWhiteLightMode; sensitivity: string; delay_s: string; start: string; stop: string; brightness: string };
  red_blue: { mode: MsRedBlue["mode"]; frequency: MsRedBlue["frequency"] };
  speaker_volume: string;
  disarm: { enable: boolean; mutes: Record<string, boolean> };
};

export function milesightDraft(m: MilesightExtras): MilesightDraft {
  const w = m.white_light, rb = m.red_blue;
  return {
    white_light: { mode: w?.mode ?? "auto", sensitivity: String(w?.sensitivity ?? ""), delay_s: String(w?.delay_s ?? ""),
      start: w?.start ?? "", stop: w?.stop ?? "", brightness: String(w?.brightness ?? "") },
    red_blue: { mode: rb?.mode ?? "red_blue", frequency: rb?.frequency ?? "medium" },
    speaker_volume: String(m.siren?.speaker_volume ?? ""),
    disarm: { enable: !!m.disarm?.enable, mutes: { ...(m.disarm?.mutes ?? {}) } },
  };
}

const HHMM = /^([01]\d|2[0-3]):[0-5]\d$/;
const inRange = (s: string, min: number, max: number) => s.trim() !== "" && Number.isInteger(Number(s)) && Number(s) >= min && Number(s) <= max;

/** The Camera tab's Milesight sections, each with its own Apply; a section is also its key in the PUT body. */
export type MsSection = "white_light" | "red_blue" | "speaker_volume" | "disarm";

/** What the camera would refuse, per section (its Apply is off; the server checks again). Only the writable parts count. */
export function milesightProblems(m: MilesightExtras, d: MilesightDraft): Partial<Record<MsSection, string[]>> {
  const w: string[] = [];
  const wl = m.white_light;
  if (wl?.supported && wl.writable) {
    if (d.white_light.mode === "auto") {
      if (!inRange(d.white_light.sensitivity, 1, 5)) w.push("Sensitivity must be 1–5");
      if (!inRange(d.white_light.delay_s, 1, 60)) w.push("Delay must be 1–60 s");
    }
    if (d.white_light.mode === "schedule" && !(HHMM.test(d.white_light.start) && HHMM.test(d.white_light.stop))) w.push("Start and stop are times like 19:30");
    if (d.white_light.mode !== "off" && !inRange(d.white_light.brightness, 1, 100)) w.push("Brightness must be 1–100");
  }
  const out: Partial<Record<MsSection, string[]>> = {};
  if (w.length) out.white_light = w;
  if (m.siren?.supported && m.siren.volume_writable && !inRange(d.speaker_volume, 0, 100)) out.speaker_volume = ["Speaker volume must be 0–100"];
  return out;
}

/** The PUT body: only what changed, only where the camera lets us write. Empty = nothing to apply. */
export function milesightChanges(m: MilesightExtras, d: MilesightDraft): MilesightWrite {
  const out: MilesightWrite = {};
  const w = m.white_light;
  if (w?.supported && w.writable) {
    const c: NonNullable<MilesightWrite["white_light"]> = {};
    if (d.white_light.mode !== w.mode) c.mode = d.white_light.mode;
    for (const k of ["sensitivity", "delay_s", "brightness"] as const) {
      const v = d.white_light[k];
      if (v.trim() !== "" && Number(v) !== w[k]) c[k] = Number(v);
    }
    for (const k of ["start", "stop"] as const) if (d.white_light[k] && d.white_light[k] !== w[k]) c[k] = d.white_light[k];
    if (Object.keys(c).length) out.white_light = c;
  }
  const rb = m.red_blue;
  if (rb?.supported && rb.writable) {
    const c: NonNullable<MilesightWrite["red_blue"]> = {};
    if (d.red_blue.mode !== rb.mode) c.mode = d.red_blue.mode;
    if (d.red_blue.frequency !== rb.frequency) c.frequency = d.red_blue.frequency;
    if (Object.keys(c).length) out.red_blue = c;
  }
  const s = m.siren;
  if (s?.supported && s.volume_writable && d.speaker_volume.trim() !== "" && Number(d.speaker_volume) !== s.speaker_volume) out.speaker_volume = Number(d.speaker_volume);
  const dz = m.disarm;
  if (dz?.supported && dz.writable) {
    const c: NonNullable<MilesightWrite["disarm"]> = {};
    if (d.disarm.enable !== dz.enable) c.enable = d.disarm.enable;
    const mutes = Object.fromEntries(Object.entries(d.disarm.mutes).filter(([k, v]) => v !== !!dz.mutes[k]));
    if (Object.keys(mutes).length) c.mutes = mutes;
    if (Object.keys(c).length) out.disarm = c;
  }
  return out;
}

/** One section's part of the body (each section has its own Apply, as on the rest of the Camera tab). */
export function sectionBody(body: MilesightWrite, part: MsSection): MilesightWrite {
  return body[part] === undefined ? {} : { [part]: body[part] } as MilesightWrite;
}
