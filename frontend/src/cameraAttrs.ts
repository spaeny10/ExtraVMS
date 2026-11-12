/** What the camera's metadata said about an event's object beyond its box (backend camattrs.py):
 * weighted votes {key: {value: [sightings, weight]}}. Milesight 8477 firmware 63.8 sends vehicle type and color,
 * clothing colours, and whether a face wears a helmet or a hat. */
export type CameraAttrs = Partial<Record<CameraAttrKey, Record<string, [number, number]>>>;
export type CameraAttrKey = "vehicle_type" | "vehicle_color" | "top_color" | "bottom_color" | "helmet" | "hat";

export function bestAttr(votes: CameraAttrs | null | undefined, key: CameraAttrKey): { value: string; n: number; share: number } | null {
  const values = Object.entries(votes?.[key] ?? {});
  if (!values.length) return null;
  const total = values.reduce((s, [, [, w]]) => s + w, 0) || 1;
  const [value, [n, w]] = values.reduce((a, b) => (b[1][1] > a[1][1] || (b[1][1] === a[1][1] && b[1][0] > a[1][0]) ? b : a));
  return { value, n, share: Math.round((w / total) * 100) / 100 };
}

/** The value only when the camera said it at least 3 times and mostly the same way (as camattrs.firm). */
export function firmAttr(votes: CameraAttrs | null | undefined, key: CameraAttrKey): string | null {
  const b = bestAttr(votes, key);
  return b && b.n >= 3 && b.share >= 0.7 ? b.value : null;
}

/** "white truck" / "green top, black bottoms, wearing a hard hat" (as camattrs.describe), or null. */
export function cameraSaw(votes: CameraAttrs | null | undefined, label: string | null | undefined): string | null {
  if (!votes) return null;
  if (label === "vehicle") {
    const color = bestAttr(votes, "vehicle_color"), kind = bestAttr(votes, "vehicle_type");
    return [color?.value, kind?.value ?? (color ? "vehicle" : null)].filter(Boolean).join(" ") || null;
  }
  const parts: string[] = [];
  for (const [key, word] of [["top_color", "top"], ["bottom_color", "bottoms"]] as const) {
    const b = bestAttr(votes, key);
    if (b) parts.push(`${b.value} ${word}`);
  }
  const helmet = firmAttr(votes, "helmet");
  if (helmet === "yes") parts.push("wearing a hard hat");
  else if (helmet === "no" && firmAttr(votes, "hat") === "yes") parts.push("wearing a hat");
  return parts.join(", ") || null;
}

const NAMES: Record<CameraAttrKey, string> = {
  vehicle_type: "type", vehicle_color: "color", top_color: "top", bottom_color: "bottoms", helmet: "hard hat", hat: "hat",
};

/** Every attribute with how often the camera said it: "top green (7×) · hard hat no (12×, 85%)", for Verification. */
export function cameraAttrsDetail(votes: CameraAttrs | null | undefined): string | null {
  const out: string[] = [];
  for (const key of Object.keys(NAMES) as CameraAttrKey[]) {
    const b = bestAttr(votes, key);
    if (!b) continue;
    out.push(`${NAMES[key]} ${b.value} (${b.n}×${b.share < 1 ? `, ${Math.round(b.share * 100)}%` : ""})`);
  }
  return out.join(" · ") || null;
}
