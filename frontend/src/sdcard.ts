/**
 * SD card backfill (backend sdbackfill.py): pure helpers for Settings → Cameras and the Timeline, kept apart from the
 * components so they are unit-tested.
 */
import type { Span } from "./playback";

/** A camera's own recording (its SD card), as GET /api/cameras (status.sd) and /api/cameras/{id}/sd report it. */
export type SdStatus = {
  checked_at?: number;
  supported?: boolean;
  has_recording?: boolean;
  earliest?: number | null;
  latest?: number | null;
  recording_now?: boolean;
  error?: string | null;
  text?: string;
};

export type RestoreState = "waiting" | "recovering" | "recovered" | "partly recovered" | "not on the card" | "failed";

/** A recovery job (restored_spans row) as the recordings listing returns it. */
export type RestoredSpan = {
  id: number; from_ts: number; to_ts: number; state: RestoreState; source: string;
  restored_s?: number; done_from?: number | null; done_to?: number | null;
};

/** The span of time the camera's card holds (recordings listing `sd_card`). */
export type CardRange = { from: number; to: number };

/** Holes shorter than this are not offered (a segment boundary, a keyframe's wait); the server uses the same. */
export const GAP_MIN_S = 20;
/** A hole reaching the live edge is only a gap once it is this old (MediaMTX may be reconnecting). */
const TAIL_MARGIN_S = 60;

const day = (t: number) => new Date(t * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" });

/** "SD card: recording, holds Sep 14 → now" / "SD card: no recording on the camera" / … */
export function sdStatusText(sd: SdStatus | null | undefined): string {
  if (!sd) return "SD card: not checked yet";
  if (!sd.supported) return sd.error ? `SD card: can't tell (${sd.error})` : "SD card: the camera can't replay its recording";
  if (!sd.has_recording || sd.earliest == null || sd.latest == null) return "SD card: no recording on the camera";
  return `SD card: ${sd.recording_now ? "recording" : "not recording"}, holds ${day(sd.earliest)} → ${sd.recording_now ? "now" : day(sd.latest)}`;
}

/** Holes of at least GAP_MIN_S between recorded spans (merged, sorted), and after the last one up to `now` minus a
 *  margin; never before the first span (the camera may not have been there). */
export function findGaps(spans: Span[], now: number, minGap = GAP_MIN_S): Span[] {
  const out: Span[] = [];
  for (let i = 1; i < spans.length; i++) {
    const a = spans[i - 1].end, b = spans[i].start;
    if (b - a >= minGap) out.push({ start: a, end: b });
  }
  const last = spans[spans.length - 1];
  if (last && now - TAIL_MARGIN_S - last.end >= minGap) out.push({ start: last.end, end: now - TAIL_MARGIN_S });
  return out;
}

/** The part of a gap the card holds, or null. */
export function onCard(gap: Span, card: CardRange | null | undefined): Span | null {
  if (!card) return null;
  const start = Math.max(gap.start, card.from), end = Math.min(gap.end, card.to);
  return end - start >= 1 ? { start, end } : null;
}

/** Gaps a user may recover: on the card, and not already covered by a job (any state but failed). */
export function recoverableGaps(spans: Span[], card: CardRange | null | undefined, restored: RestoredSpan[], now: number): Span[] {
  const taken = restored.filter((r) => r.state !== "failed");
  const out: Span[] = [];
  for (const g of findGaps(spans, now)) {
    const c = onCard(g, card);
    if (!c) continue;
    let parts: Span[] = [c];
    for (const r of taken) {
      parts = parts.flatMap((p) => (r.to_ts <= p.start || r.from_ts >= p.end ? [p]
        : [...(r.from_ts > p.start ? [{ start: p.start, end: r.from_ts }] : []), ...(r.to_ts < p.end ? [{ start: r.to_ts, end: p.end }] : [])]));
    }
    out.push(...parts.filter((p) => p.end - p.start >= GAP_MIN_S));
  }
  return out;
}

/** Hover text and CSS modifier of a recovery job on the Timeline. */
export function restoredLabel(r: RestoredSpan): { text: string; cls: string } {
  switch (r.state) {
    case "recovered": return { text: "Recovered from the camera's SD card", cls: "done" };
    case "partly recovered": return { text: "Partly recovered from the camera's SD card (the card has holes here)", cls: "done" };
    case "recovering": return { text: "Recovering from the camera's SD card…", cls: "busy" };
    case "waiting": return { text: "Waiting to recover from the camera's SD card", cls: "busy" };
    case "not on the card": return { text: "Not on the camera's SD card", cls: "none" };
    default: return { text: "Recovery from the SD card failed", cls: "none" };
  }
}

/** "4 min 10 s" for a recovery prompt. */
export function fmtLength(s: number): string {
  s = Math.round(s);
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min${s % 60 ? ` ${s % 60} s` : ""}`;
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return `${h} h${m ? ` ${m} min` : ""}`;
}
