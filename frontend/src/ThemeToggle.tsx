import { useEffect, useState } from "react";
import { Icon } from "./ui";

/** auto = follow the OS setting; light / dark = forced (saved per browser). */
type Theme = "auto" | "light" | "dark";
const ORDER: Theme[] = ["auto", "light", "dark"];
const ICON: Record<Theme, string> = { auto: "auto", light: "sun", dark: "moon" };
const LABEL: Record<Theme, string> = { auto: "Theme: follows your system", light: "Theme: light", dark: "Theme: dark" };

function load(): Theme {
  try {
    const t = localStorage.getItem("theme");
    return t === "light" || t === "dark" ? t : "auto";
  } catch {
    return "auto";
  }
}

/** Also applied by the inline script in index.html before first paint, so there's no flash. */
export function applyTheme(t: Theme) {
  if (t === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(load);
  useEffect(() => applyTheme(theme), [theme]);
  const next = () => {
    const t = ORDER[(ORDER.indexOf(theme) + 1) % ORDER.length];
    try {
      if (t === "auto") localStorage.removeItem("theme");
      else localStorage.setItem("theme", t);
    } catch {
      /* private mode: still switches for this visit */
    }
    setTheme(t);
  };
  return (
    <button className="ghost small theme-toggle" onClick={next} title={`${LABEL[theme]} (click to change)`} aria-label={LABEL[theme]}>
      <Icon name={ICON[theme]} size={16} />
    </button>
  );
}
