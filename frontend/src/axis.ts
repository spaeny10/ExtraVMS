/* An AXIS camera's VAPIX side (backend axis.py, GET /api/cameras/{id}/device/axis): who it is, its firmware, its scene
   metadata producer, its AXIS Object Analytics scenarios and whether this server's stream paths use VAPIX with the
   metadata. The Camera tab's AXIS section (DeviceSettings.tsx) shows it with the "Set up for Axiom" button. */

export type AxisUnsupported = { supported: false; reason?: string; error?: boolean };

export type AxisScenario = { id: number; name: string; type: string; labels: string[]; counting: boolean };

export type AxisDevice = {
  supported: true;
  identity: { brand: string; model: string; full_name?: string | null; firmware?: string | null; serial?: string | null; apis: Record<string, string> };
  firmware: { version?: string | null; old: boolean; baseline: string };
  metadata: ({ supported: true; producer: string; producer_name: string; enabled: boolean } | AxisUnsupported);
  analytics: ({ supported: true; scenarios: AxisScenario[] } | AxisUnsupported);
  streams: { main_path: string; sub_path: string; vapix: boolean; analytics: boolean };
  setup_needed: boolean;
  setup?: { producer: string | null; enabled_now: boolean; main_path: string; sub_path: string; sd: string | null; written: string[]; fallback?: string };
};

export type AxisInfo = AxisDevice | AxisUnsupported;

const TYPE_NAMES: Record<string, string> = {
  motion: "Object in area", fence: "Line crossing", crosslinecounting: "Crossline counting", occupancyinarea: "Occupancy in area",
};

/** "Object in area · people, vehicles" */
export function scenarioText(s: AxisScenario): string {
  const kind = TYPE_NAMES[s.type.toLowerCase()] ?? s.type;
  const what = s.counting ? "counts only, opens no events" : s.labels.map((l) => (l === "person" ? "people" : l === "vehicle" ? "vehicles" : l)).join(", ");
  return `${kind} · ${what}`;
}

/** One line per part of the setup: what is on, and what the button would change. */
export function setupState(a: AxisDevice): { ok: boolean; text: string }[] {
  const rows: { ok: boolean; text: string }[] = [];
  const m = a.metadata;
  if (m.supported) {
    rows.push(m.enabled
      ? { ok: true, text: `Scene metadata (${m.producer_name}) is on in the camera` }
      : { ok: false, text: `Scene metadata (${m.producer_name}) is off in the camera: its people and vehicles aren't sent` });
    rows.push(a.streams.analytics
      ? { ok: true, text: "This server reads the camera's objects from its stream" }
      : { ok: false, text: "This server's stream doesn't ask for the camera's objects" });
  } else {
    rows.push({ ok: false, text: `No scene metadata${m.reason ? ` (${m.reason})` : ""}: detections come from motion and YOLO` });
  }
  rows.push(a.streams.vapix
    ? { ok: true, text: "Stream paths: AXIS's own (SD is the same picture, smaller)" }
    : { ok: false, text: "Stream paths: ONVIF profiles (SD may show another view than the recording)" });
  return rows;
}

/** What the confirm dialog says the button will do. */
export function setupMessage(a: AxisDevice): string {
  const steps = [];
  if (a.metadata.supported && !a.metadata.enabled) steps.push(`turns on the camera's scene metadata (${a.metadata.producer_name}), a setting on the camera`);
  steps.push("switches this server's stream paths for it to AXIS's own (main with the camera's objects, SD the same view smaller)");
  return `This ${steps.join(", and ")}. Recording restarts for a few seconds.`;
}
