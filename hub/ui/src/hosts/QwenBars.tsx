/** Qwen seconds per UTC day as tiny bars, oldest first (the busiest day full height): an instance row, a host's Work. */
import type { QwenDays } from "../api";
import { dayBars } from "../site/aiSchedule";

export function QwenBars({ q }: { q: QwenDays }) {
  const bars = dayBars(q);
  return (
    <span className="qwen-bars" aria-hidden="true">
      {bars.map((h, i) => <span key={q.days[i].day} className={h > 0 ? "" : "zero"} style={{ height: `${Math.max(5, Math.round(h * 100))}%` }} />)}
    </span>
  );
}
