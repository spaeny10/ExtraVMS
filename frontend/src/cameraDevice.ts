/**
 * Settings → Cameras → Edit → Camera (DeviceSettings.tsx): the camera's own settings over ONVIF (backend camdevice.py,
 * device_api.py). The reply types and the forms' pure logic: what a draft would change, what the camera's options
 * refuse (checked here so Apply is off; the server checks again before anything is written), offsets as text.
 */

export type DeviceRange = { min: number; max: number };
/** a section the camera can't do, or that failed (`error`: the camera refused or didn't answer; `reason` is its words) */
export type Unsupported = { supported: false; reason: string; error?: boolean };
export type Section<T> = (T & { supported: true }) | Unsupported;

export type ZoneCheck = { zone: string; expected_s: number; camera_s: number; ok: boolean };
export type CameraTime = {
  mode: string | null;          // "NTP" | "Manual"
  dst: boolean;
  timezone: string | null;      // POSIX TZ (or the camera's own notation, e.g. Milesight "UTC+06:00:00")
  camera_utc: string | null;
  camera_local: string | null;
  offset_s: number | null;      // the camera's clock minus the server's (positive = ahead)
  local_minus_utc_s: number | null;
  zone_check: ZoneCheck | null;
  ntp: { from_dhcp: boolean; servers: string[]; dhcp_servers: string[] } | null;
  ntp_error?: string;
  ntp_servers: string[];
  warnings?: string[];
};
export type VideoEncodingOptions = {
  encoding: string; encoding_raw: string; resolutions: { width: number; height: number }[];
  fps: DeviceRange | null; fps_list: number[]; bitrate: DeviceRange | null; gov: DeviceRange | null;
};
export type VideoEncoder = {
  token: string; name: string; encoding: string; encoding_raw: string; width: number | null; height: number | null;
  fps: number | null; bitrate_kbps: number | null; gov_length: number | null; gov_seconds: number | null; quality: number | null;
  cbr: boolean | null; profile: string | null; profiles: string[]; profile_name: string | null; source: string | null;
  media: "media" | "media2"; stream: "main" | "sub" | null;
  options: { quality: DeviceRange | null; encodings: VideoEncodingOptions[]; error?: string };
};
export type AudioOption = { encoding: string; encoding_raw: string; bitrates: number[]; sample_rates: number[] };
export type AudioEncoder = {
  token: string; name: string; encoding: string; encoding_raw: string; bitrate: number | null; sample_rate: number | null;
  profiles: string[]; media: "media" | "media2"; options: AudioOption[]; options_error?: string;
};
export type ImagingValue = { value: number | null; range: DeviceRange | null };
export type ImagingField = "brightness" | "contrast" | "saturation" | "sharpness";
export type ImagingSource = { source: string; ir_cut?: { value: string; modes: string[] } } & Partial<Record<ImagingField, ImagingValue>>;
export type AnalyticsRule = { name: string; type: string; type_qname: string; params: Record<string, string>; toggle: string | null; enabled: boolean | null };
export type TimeDefaults = {
  ntp_server: string; timezone: string | null; posix: string | null; server_zone: string | null; gateway: string | null;
  saved: boolean; suggestions: string[];
};
export type CameraDevice = {
  camera_id: string; reachable: boolean; error?: string;
  info: { manufacturer?: string; model?: string; firmware?: string; supported?: boolean; reason?: string };
  time: Section<CameraTime>;
  video: Section<{ media: string; encoders: VideoEncoder[]; sources: string[] }>;
  audio: Section<{ media: string; encoders: AudioEncoder[] }>;
  imaging: Section<{ sources: ImagingSource[] }>;
  analytics: Section<{ configurations: { configuration: string; rules: AnalyticsRule[]; sources?: string[]; error?: string }[] }>;
  reboot: Section<Record<string, never>>;
  capabilities: Partial<Record<"time" | "video" | "audio" | "imaging" | "analytics" | "reboot", boolean>>;
  defaults: TimeDefaults;
};
export type TimeBody = { mode: "ntp" | "manual_sync"; ntp_servers: string[]; timezone: string | null };
export type ClockResult = {
  camera_id: string; name: string; ok: boolean; offset_s?: number | null; mode?: string | null; timezone?: string | null;
  zone_check?: ZoneCheck | null; warnings?: string[]; error?: string;
};

/** A clock this far off (seconds) is shown in red. */
export const OFFSET_BAD_S = 5;

export function offsetBad(s: number | null | undefined): boolean {
  return s != null && Math.abs(s) > OFFSET_BAD_S;
}

/** 2703 → "45 min 3 s ahead", -3600 → "1 h behind", 0.4 → "in step". */
export function offsetText(s: number | null | undefined): string {
  if (s == null) return "unknown";
  const a = Math.round(Math.abs(s));
  if (a < 1) return "in step";
  const h = Math.floor(a / 3600), m = Math.floor((a % 3600) / 60), sec = a % 60;
  const parts = h ? [`${h} h`, m && `${m} min`] : m ? [`${m} min`, sec && `${sec} s`] : [`${sec} s`];
  return `${parts.filter(Boolean).join(" ")} ${s > 0 ? "ahead" : "behind"}`;
}

/** -18000 → "UTC−5:00", 19800 → "UTC+5:30". */
export function utcOffsetText(s: number): string {
  const a = Math.abs(s), h = Math.floor(a / 3600), m = Math.round((a % 3600) / 60);
  return `UTC${s < 0 ? "−" : "+"}${h}:${String(m).padStart(2, "0")}`;
}

/** The zone check as a sentence when it is wrong (null when fine or unknown). */
export function zoneCheckText(z: ZoneCheck | null | undefined): string | null {
  if (!z || z.ok) return null;
  return `The camera's local time is ${utcOffsetText(z.camera_s)}, ${z.zone} is ${utcOffsetText(z.expected_s)} now: its time zone or daylight saving is wrong.`;
}

export function encodingLabel(e: string | null | undefined): string {
  return ({ H264: "H.264", H265: "H.265", JPEG: "MJPEG", MPEG4: "MPEG-4", G711: "G.711", G726: "G.726" } as Record<string, string>)[e ?? ""] ?? (e || "?");
}

/** "Main stream (recorded)", "Sub stream (SD live)", else the camera's profile name; the sensor when it has several. */
export function encoderTitle(v: VideoEncoder, sources: string[], recordStream: "main" | "sub" = "main"): string {
  const base = v.stream === "main" ? `Main stream${recordStream === "main" ? " (recorded)" : ""}`
    : v.stream === "sub" ? `Sub stream${recordStream === "sub" ? " (recorded)" : " (SD live view)"}`
    : v.profile_name || v.name || v.token;
  const n = v.source ? sources.indexOf(v.source) : -1;
  return sources.length > 1 && n >= 0 ? `${base} · sensor ${n + 1}` : base;
}

export type VideoDraft = { encoding: string; resolution: string; fps: string; bitrate_kbps: string; gov_seconds: string };

export const res = (w: number | null | undefined, h: number | null | undefined) => (w && h ? `${w}x${h}` : "");

export function videoDraft(v: VideoEncoder): VideoDraft {
  return { encoding: v.encoding, resolution: res(v.width, v.height), fps: v.fps != null ? String(v.fps) : "",
    bitrate_kbps: v.bitrate_kbps != null ? String(v.bitrate_kbps) : "", gov_seconds: v.gov_seconds != null ? String(v.gov_seconds) : "" };
}

export function videoOptions(v: VideoEncoder, encoding: string): VideoEncodingOptions | null {
  return v.options.encodings.find((o) => o.encoding === encoding) ?? null;
}

/** Keyframe interval seconds → frames, as the server writes it (round(seconds × fps), at least 1). */
export function govFrames(seconds: number, fps: number): number {
  return Math.max(1, Math.round(seconds * fps));
}

const num = (s: string) => (s.trim() === "" ? null : Number(s));
const fmt = (n: number) => String(Math.round(n * 100) / 100);

/** What the draft changes, as the PUT body (only fields that differ from the camera's). */
export function videoChanges(v: VideoEncoder, d: VideoDraft): Record<string, string | number> {
  const out: Record<string, string | number> = {};
  if (d.encoding && d.encoding !== v.encoding) out.encoding = d.encoding;
  if (d.resolution && d.resolution !== res(v.width, v.height)) out.resolution = d.resolution;
  const fps = num(d.fps), br = num(d.bitrate_kbps), gov = num(d.gov_seconds);
  if (fps != null && fps !== v.fps) out.fps = fps;
  if (br != null && br !== v.bitrate_kbps) out.bitrate_kbps = br;
  // seconds are sent when they changed, or with a new frame rate (the server keeps seconds when only fps is sent)
  if (gov != null && (gov !== v.gov_seconds || out.fps != null)) out.gov_seconds = gov;
  return out;
}

/** Why the camera would refuse the draft (empty = fine): the same checks the server makes before writing. */
export function videoProblems(v: VideoEncoder, d: VideoDraft): string[] {
  const o = videoOptions(v, d.encoding || v.encoding);
  const out: string[] = [];
  const fps = num(d.fps), br = num(d.bitrate_kbps), gov = num(d.gov_seconds);
  if ([fps, br, gov].some((x) => x != null && !Number.isFinite(x))) return ["Enter numbers only."];
  if (!o) return v.options.encodings.length ? [`The camera lists no options for ${encodingLabel(d.encoding)}.`] : [];
  if (d.resolution && o.resolutions.length && !o.resolutions.some((r) => res(r.width, r.height) === d.resolution)) {
    out.push(`${d.resolution.replace("x", "×")} is not offered for ${encodingLabel(o.encoding)}.`);
  }
  if (fps != null) {
    if (o.fps_list.length && !o.fps_list.some((f) => Math.abs(f - fps) < 0.01)) out.push(`${fmt(fps)} fps is not offered (${o.fps_list.map(fmt).join(", ")}).`);
    else if (o.fps && (fps < o.fps.min || fps > o.fps.max)) out.push(`Frame rate must be ${fmt(o.fps.min)}–${fmt(o.fps.max)} fps.`);
  }
  if (br != null && o.bitrate && (br < o.bitrate.min || br > o.bitrate.max)) out.push(`Bitrate must be ${fmt(o.bitrate.min)}–${fmt(o.bitrate.max)} kbps.`);
  const rate = fps ?? v.fps;
  if (gov != null) {
    if (gov <= 0) out.push("The keyframe interval must be more than 0 s.");
    else if (rate && o.gov) {
      const frames = govFrames(gov, rate);
      if (frames < o.gov.min || frames > o.gov.max) {
        out.push(`A keyframe every ${fmt(gov)} s is ${frames} frames; the camera allows ${fmt(o.gov.min)}–${fmt(o.gov.max)} (${fmt(o.gov.min / rate)}–${fmt(o.gov.max / rate)} s at ${fmt(rate)} fps).`);
      }
    }
  }
  return out;
}

/** A keyframe interval this long (s) makes Timeline seeking slow: suggest 1–2 s. */
export function slowSeeking(seconds: number | null | undefined): boolean {
  return seconds != null && seconds > 2.5;
}

export type AudioDraft = { encoding: string; bitrate: string; sample_rate: string };

export function audioDraft(a: AudioEncoder): AudioDraft {
  return { encoding: a.encoding, bitrate: a.bitrate != null ? String(a.bitrate) : "", sample_rate: a.sample_rate != null ? String(a.sample_rate) : "" };
}

/** The encodings the camera offers for this encoder (G.711 once, even if it lists PCMU and PCMA). */
export function audioEncodings(a: AudioEncoder): string[] {
  return [...new Set(a.options.map((o) => o.encoding))];
}

/** A new encoding: the bitrate and sample rate move to what it offers (kept when allowed, else the nearest). */
export function audioPick(a: AudioEncoder, d: AudioDraft, encoding: string): AudioDraft {
  const o = a.options.find((x) => x.encoding === encoding);
  if (!o) return { ...d, encoding };
  const near = (cur: string, list: number[]) => {
    if (!list.length) return cur;
    const v = Number(cur);
    return String(list.includes(v) ? v : list.reduce((b, x) => (Math.abs(x - v) < Math.abs(b - v) ? x : b), list[0]));
  };
  return { encoding, bitrate: near(d.bitrate, o.bitrates), sample_rate: near(d.sample_rate, o.sample_rates) };
}

export function audioChanges(a: AudioEncoder, d: AudioDraft): Record<string, string | number> {
  const out: Record<string, string | number> = {};
  if (d.encoding !== a.encoding) out.encoding = d.encoding;
  if (d.bitrate && Number(d.bitrate) !== a.bitrate) out.bitrate = Number(d.bitrate);
  if (d.sample_rate && Number(d.sample_rate) !== a.sample_rate) out.sample_rate = Number(d.sample_rate);
  return out;
}

/** Audio browsers can't play from a recording (playback converts it on the server meanwhile). */
export function browserMute(encoding: string | null | undefined): boolean {
  return encoding === "G711" || encoding === "G726";
}

/** The NTP servers typed in a field: "a, b c" → ["a", "b", "c"]. */
export function splitServers(s: string): string[] {
  return s.split(/[\s,;]+/).map((x) => x.trim()).filter(Boolean);
}

/** The time zones offered: the browser's list (the same IANA names the server converts), with the given ones first. */
export function zoneList(first: (string | null | undefined)[] = []): string[] {
  let all: string[] = [];
  try {
    all = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.("timeZone") ?? [];
  } catch { all = []; }
  if (!all.length) {
    all = ["America/New_York", "America/Chicago", "America/Denver", "America/Phoenix", "America/Los_Angeles", "America/Anchorage",
      "Pacific/Honolulu", "Europe/London", "Europe/Berlin", "Asia/Kolkata", "Asia/Shanghai", "Australia/Sydney"];
  }
  const head = first.filter((z): z is string => Boolean(z) && z!.includes("/"));
  return [...new Set([...head, ...all, "UTC"])];
}

/** "3 fixed · 1 failed" for the Fix clocks results. */
export function clockSummary(results: ClockResult[]): string {
  const ok = results.filter((r) => r.ok).length, bad = results.length - ok;
  return [ok && `${ok} fixed`, bad && `${bad} failed`].filter(Boolean).join(" · ") || "no cameras";
}
