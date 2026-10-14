/**
 * Settings → Cameras → Add / Edit camera (CameraDialog.tsx): pure helpers, kept apart from the component so they are
 * unit-tested: the one-line header summary, which tab each saved field is edited on (the "unsaved changes" dots), and
 * which tab the dialog opens on.
 */
import type { Camera } from "./api";
import { profileSize } from "./cameraStreams";

export type CameraTab = "general" | "connection" | "recording" | "detection" | "journeys" | "ptz" | "rules";

export const CAMERA_TABS: { id: CameraTab; label: string }[] = [
  { id: "general", label: "General" },
  { id: "connection", label: "Connection" },
  { id: "recording", label: "Recording" },
  { id: "detection", label: "Detection" },
  { id: "journeys", label: "Journeys" },
  { id: "ptz", label: "PTZ" },
  { id: "rules", label: "Rules & speakers" },
];

/** The tabs the dialog shows: PTZ only on a camera that has PTZ. */
export function visibleTabs(hasPtz: boolean): CameraTab[] {
  return CAMERA_TABS.map((t) => t.id).filter((t) => t !== "ptz" || hasPtz);
}

/** Where the dialog opens: General for a new camera, else the tab this viewer had open last (if this camera has it). */
export function openingTab(remembered: CameraTab | null, isNew: boolean, tabs: CameraTab[]): CameraTab {
  return !isNew && remembered && tabs.includes(remembered) ? remembered : "general";
}

/** The camera as the form edits it: the saved camera plus a new password ("" / absent = unchanged). */
export type CameraDraft = Camera & { password?: string };

/** The tab each field the form saves is edited on (status, streams and ptz_config are not form fields). */
export const FIELD_TAB: Partial<Record<keyof CameraDraft, CameraTab>> = {
  name: "general", enabled: "general", id: "general", scene_notes: "general",
  host: "connection", onvif_port: "connection", rtsp_port: "connection", username: "connection", password: "connection",
  main_path: "connection", sub_path: "connection",
  public_host: "connection", public_onvif_port: "connection", public_rtsp_port: "connection", public_replay_port: "connection",
  record_stream: "recording", retention_days: "recording", retention_policy: "recording",
  event_source: "detection", motion_events: "detection", synopsis_labels: "detection", zones: "detection",
  policies: "rules",
};

/** A field's value as the form compares it: absent and the default it shows are the same (unticking and ticking a box
 *  again is no change). */
function norm(k: keyof CameraDraft, v: unknown): unknown {
  switch (k) {
    case "password": case "scene_notes": return v || "";
    case "enabled": case "motion_events": return Boolean(v);
    case "record_stream": return v ?? "main";
    case "event_source": return v ?? "auto";
    case "synopsis_labels": return [...((v as string[] | null | undefined) ?? ["person"])].sort();
    case "policies": case "zones": return v ?? [];
    default: return v ?? null;
  }
}

/** The form fields that differ between the camera as the dialog opened and the draft now. */
export function changedFields(before: CameraDraft, after: CameraDraft): (keyof CameraDraft)[] {
  return (Object.keys(FIELD_TAB) as (keyof CameraDraft)[])
    .filter((k) => JSON.stringify(norm(k, before[k])) !== JSON.stringify(norm(k, after[k])));
}

/** The tabs holding unsaved changes. */
export function dirtyTabs(before: CameraDraft, after: CameraDraft): Set<CameraTab> {
  return new Set(changedFields(before, after).map((k) => FIELD_TAB[k]!));
}

export type SummaryPart = { text: string; tone?: "ok" | "bad" | "idle" };

/**
 * The dialog header, from the saved camera: "Side Yard · cam1 · ● streaming · Main 2592×1520 H.264 / Sub 640×480 H.264
 * · recording main · events from camera metadata · PTZ". Parts that are not known are left out; no camera = "New camera".
 */
export function headerSummary(c: Camera | null | undefined): SummaryPart[] {
  if (!c) return [{ text: "New camera" }];
  const parts: SummaryPart[] = [{ text: c.name || c.id }, { text: c.id }];
  const on = Boolean(c.enabled);
  if (!on) parts.push({ text: "disabled", tone: "idle" });
  else if (c.status) parts.push(c.status.stream_ready ? { text: "streaming", tone: "ok" } : { text: "not streaming", tone: "bad" });
  const s = c.streams;
  const main = s ? s.main ?? s.profiles[0] ?? null : null;
  const m = main ? profileSize(main) : "";
  if (s && m) parts.push({ text: s.sd.relay || !s.sub ? `Main ${m} only` : `Main ${m} / Sub ${profileSize(s.sub)}`.trimEnd() });
  if (on) parts.push({ text: c.status?.recording === false ? "not recording" : `recording ${c.record_stream ?? "main"}` });
  const d = c.status?.detections;
  const source = d?.source ?? (c.event_source === "metadata" || c.event_source === "onvif_events" ? c.event_source : null);
  if (source === "metadata") parts.push({ text: "events from camera metadata" });
  else if (source === "onvif_events") {
    parts.push({ text: `events from camera ONVIF events${(d ? d.motion_events : c.motion_events) ? " and motion alarms" : ""}` });
  }
  if (c.status?.ptz?.available) parts.push({ text: "PTZ" });
  return parts;
}
