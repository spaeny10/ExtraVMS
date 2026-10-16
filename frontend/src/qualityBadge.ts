/**
 * The live tile's one-badge quality control (LiveQuality.tsx QualityBadge, the hub's Site Live tiles): what the badge
 * says for the viewer's mode and what plays, and the order of its menu (Auto, SD, HD). Pure, so it is unit-tested (qualityBadge.test.ts).
 */
export type BadgeMode = "auto" | "sd" | "hd";

export const QUALITY_MODES: readonly BadgeMode[] = ["auto", "sd", "hd"];
export const MODE_LABEL: Record<BadgeMode, string> = { auto: "Auto", sd: "SD", hd: "HD" };

export type QualityBadgeText = {
  /** on the badge: "Auto · HD", "Auto · SD", "Auto" (paused), "SD", "HD", "HD · SD" (HD picked, SD plays) */
  text: string;
  /** the badge's tooltip */
  title: string;
  /** read out: "Stream quality: Auto, now HD" */
  label: string;
  /** the viewer picked HD and it didn't start: SD plays, a click on HD tries again */
  stalled: boolean;
};

/**
 * `hd`: what plays now (LiveQuality's lq.hd); `live` false: a paused tile (only the mode is known); `hdBlocked`: why HD
 * can't play in this browser (null = it can).
 */
export function qualityBadge(mode: BadgeMode, hd: boolean, live = true, hdBlocked: string | null = null): QualityBadgeText {
  const now = hd ? "HD" : "SD";
  if (mode === "auto") {
    const text = live ? `Auto · ${now}` : "Auto";
    return {
      text, stalled: false,
      label: `Stream quality: Auto${live ? `, now ${now}` : ""}`,
      title: `Automatic: HD while the tile is large and the link is clean, else SD${live ? ` · now ${now}` : ""}${hdBlocked ? ` · ${hdBlocked}` : ""} · click to change`,
    };
  }
  if (mode === "sd") return { text: "SD", stalled: false, label: "Stream quality: SD", title: "Sub stream: low bandwidth · click to change" };
  const stalled = live && !hd && !hdBlocked;
  if (stalled) return { text: "HD · SD", stalled, label: "Stream quality: HD, playing SD", title: "HD didn't start, so SD plays: pick HD again to retry · click to change" };
  return { text: "HD", stalled: false, label: "Stream quality: HD", title: `${hdBlocked ?? "Full-resolution main stream"} · click to change` };
}

