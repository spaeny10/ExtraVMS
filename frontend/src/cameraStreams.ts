/**
 * A camera's streams as its ONVIF check found them (backend streams.py), and the live player's first still: pure
 * helpers for Settings → Cameras and the live players, kept apart from the components so they are unit-tested.
 */

/** One ONVIF media profile: a stream the camera serves (path = RTSP path and query, never a URL or password). */
export type StreamProfile = { token: string; encoding: string | null; width: number | null; height: number | null; fps: number | null; path: string };

/** GET /api/cameras `streams` (backend streams.view). */
export type CameraStreams = {
  /** last check (epoch s); null = never asked */
  checked_at: number | null;
  /** why the last check failed (the profiles from before are kept) */
  error: string | null;
  profiles: StreamProfile[];
  /** the main stream's profile and the one SD live view plays (null: none / not known) */
  main: StreamProfile | null;
  sub: StreamProfile | null;
  /** what <id>_sub pulls: a camera path, or relay = the main stream (the camera has no low-resolution stream) */
  sd: { path: string | null; relay: boolean; detected: boolean };
  /** MediaMTX was refused (RTSP 404) on the sub stream path */
  sub_not_found?: boolean;
  /** the check route answered before the camera did; the result is stored when it comes */
  pending?: boolean;
  /** the metadata configuration's Analytics flag (false: no objects in the metadata; detections from ONVIF events) */
  metadata_analytics?: boolean | null;
  /** the configured main path is not one the camera lists: the paths it does list (largest = main), for a one-click fix */
  suggest?: { main_path: string; sub_path: string } | null;
};

/** "Use /Preview_01_main and /Preview_01_sub" for the one-click path fix, or null when there is nothing to offer. */
export function suggestText(s: CameraStreams | null | undefined, current: { main_path: string; sub_path: string }): string | null {
  const g = s?.suggest;
  if (!g || (g.main_path === current.main_path && g.sub_path === current.sub_path)) return null;
  return g.sub_path && g.sub_path !== current.sub_path ? `Use ${g.main_path} and ${g.sub_path}` : `Use ${g.main_path}`;
}

/** Settings → Cameras: where detections come from, in words (null when it's the camera's object metadata). */
export function detectionsText(d: { source: string; reason: string } | null | undefined): string | null {
  if (!d || d.source !== "onvif_events") return null;
  return "Detections: from the camera's ONVIF events (no object positions)";
}

function size(p: StreamProfile): string {
  const res = p.width && p.height ? `${p.width}×${p.height}` : "";
  return [res, p.encoding ?? ""].filter(Boolean).join(" ");
}

/** "Main 3840×2160 H.264 · Sub 1280×720 H.265", "Main 3840×2160 H.264 only", or "" when nothing is known. */
export function streamsText(s: CameraStreams | null | undefined): string {
  if (!s) return "";
  const main = s.main ?? s.profiles[0] ?? null;
  if (!main) return s.sd?.relay ? "Main only" : "";
  const m = size(main);
  if (s.sd.relay || !s.sub) return m ? `Main ${m} only` : "Main only";
  return `Main ${m} · Sub ${size(s.sub)}${s.sd.detected ? ` (${s.sub.path})` : ""}`;
}

/** The tooltip under the Streams line: where SD comes from, and when the camera was last asked. */
export function streamsTitle(s: CameraStreams | null | undefined, now = Date.now() / 1000): string {
  if (!s) return "";
  const parts: string[] = [];
  if (s.sd.relay) parts.push("SD live view plays the main stream (the camera has no low-resolution stream)");
  else if (s.sd.detected) parts.push(`SD live view uses ${s.sd.path}, which the camera lists, instead of the sub stream path`);
  if (s.profiles.length) parts.push(`The camera lists: ${s.profiles.map((p) => `${p.path} ${size(p)}`.trim()).join(", ")}`);
  if (s.error) parts.push(`Last check failed: ${s.error}`);
  if (s.checked_at) {
    const min = Math.max(0, Math.round((now - s.checked_at) / 60));
    parts.push(`Checked ${min < 1 ? "just now" : min < 120 ? `${min} min ago` : `${Math.round(min / 60)} h ago`}`);
  } else parts.push("Not checked yet");
  return parts.join(". ");
}

/** Widths the server's /api/frame accepts (320-1280); stepped so a few sizes cover every player. */
const STILL_WIDTHS = [320, 480, 640, 960, 1280];

/** The still to ask for a player `cssWidth` CSS pixels wide on a `dpr` screen: the smallest step that covers it. */
export function stillWidth(cssWidth: number, dpr = 1): number {
  const want = Math.max(1, cssWidth || 0) * Math.max(1, Math.min(dpr || 1, 2));
  return STILL_WIDTHS.find((w) => w >= want) ?? STILL_WIDTHS[STILL_WIDTHS.length - 1];
}

/** The camera whose recording a MediaMTX live path shows: <id>, <id>_sub and <id>_hd are all <id>. */
export function cameraOfPath(path: string): string {
  return path.replace(/_(sub|hd)$/, "");
}
