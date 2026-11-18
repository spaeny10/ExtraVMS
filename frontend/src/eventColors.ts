/** An event's object colors (backend colors.py: events.colors) and Find's color filter. Otso's colors come first,
 * then the camera's own read, then the colors read from the event's crops. */
export const COLOR_NAMES = ["black", "white", "gray", "red", "orange", "yellow", "green", "blue", "purple", "pink", "brown"] as const;
export type ColorName = (typeof COLOR_NAMES)[number];
export type ColorPart = "" | "vehicle" | "top" | "bottom";
export type EventColors = {
  vehicle?: string; top?: string; bottom?: string;
  /** where each part came from */
  by?: Partial<Record<"vehicle" | "top" | "bottom", "otso" | "camera" | "picture">>;
  picture?: Partial<Record<"vehicle" | "top" | "bottom", string>>;
};

/** A swatch for each color (the filter's chips). */
export const SWATCH: Record<ColorName, string> = {
  black: "#1a1a1a", white: "#f4f4f4", gray: "#8a8f98", red: "#d43c3c", orange: "#ec8a2f", yellow: "#e8c930",
  green: "#3f9a4a", blue: "#3a6fd8", purple: "#8a4fc8", pink: "#e47fb0", brown: "#8b5a2b",
};

export const PART_LABELS: [ColorPart, string][] = [["", "Any part"], ["top", "Top"], ["bottom", "Bottoms"], ["vehicle", "Vehicle"]];

/** Same rule as the backend's color filter (db.event_filters): that color on the part, or on any part. */
export function colorMatches(colors: EventColors | null | undefined, color: string, part: ColorPart = ""): boolean {
  if (!color) return true;
  if (!colors) return false;
  return part ? colors[part] === color : colors.vehicle === color || colors.top === color || colors.bottom === color;
}

/** "white vehicle" / "green top · black bottoms", or null. */
export function colorsText(colors: EventColors | null | undefined): string | null {
  if (!colors) return null;
  if (colors.vehicle) return `${colors.vehicle} vehicle`;
  const parts = [colors.top && `${colors.top} top`, colors.bottom && `${colors.bottom} bottoms`].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

/** For the filter's summary line: "green top", "red", "white vehicle". */
export function colorFilterText(color: string, part: ColorPart): string {
  if (!color) return "";
  return part === "top" ? `${color} top` : part === "bottom" ? `${color} bottoms` : part === "vehicle" ? `${color} vehicle` : color;
}
