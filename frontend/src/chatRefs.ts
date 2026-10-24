/**
 * The sightings under a clip-chat answer that searched beyond the clip (backend clipask.py): chips with the snapshot,
 * the time and the camera; a tap switches the event viewer to that event (EventDetail's ChatPanel).
 */
import type { ChatRef } from "./api";

export const MATCH_LABEL: Record<ChatRef["match"], string> = { this: "This clip", same: "Same", possible: "Possible", found: "Event" };
const ORDER: Record<ChatRef["match"], number> = { this: 0, same: 1, possible: 2, found: 3 };

export type RefChip = {
  key: string;
  ref: ChatRef;
  /** the event the viewer shows now: not tappable */
  current: boolean;
  label: string;
  camera: string;
  time: string;
  title: string;
};

function why(r: ChatRef): string {
  if (r.match === "this") return "the clip you asked about";
  if (r.via === "plate") return "same license plate";
  if (r.via === "journey") return "same person on a confirmed cross-camera journey";
  if (r.score != null) return `${r.match === "same" ? "same" : "possibly the same"} by appearance, similarity ${r.score.toFixed(2)}`;
  return r.match === "found" ? "found by the search" : "";
}

/** Chips for a message's refs, in time order; `currentId` = the event the viewer shows. */
export function refChips(refs: ChatRef[] | null | undefined, currentId: number, cameraName: (id: string) => string,
  fmt: (ts: number) => string): RefChip[] {
  return [...(refs ?? [])]
    .sort((a, b) => a.start_ts - b.start_ts || ORDER[a.match] - ORDER[b.match])
    .map((r) => {
      const camera = cameraName(r.camera_id);
      const time = fmt(r.start_ts);
      const current = r.event_id === currentId;
      const detail = why(r);
      return {
        key: `${r.match}:${r.event_id}`, ref: r, current, label: MATCH_LABEL[r.match], camera, time,
        title: `${MATCH_LABEL[r.match]}: ${camera}, ${time}${detail ? ` (${detail})` : ""} · event #${r.event_id}${current ? " · shown now" : " · tap to open"}`,
      };
    });
}

/** What a tap on a chip opens: the event's id, or null for the event already shown. */
export function refTarget(ref: Pick<ChatRef, "event_id">, currentId: number): number | null {
  return ref.event_id === currentId ? null : ref.event_id;
}

/** "3 same · 2 possible": the head of the chip row (the clip itself and found events are not counted). */
export function refSummary(refs: ChatRef[] | null | undefined): string {
  const n = (m: ChatRef["match"]) => (refs ?? []).filter((r) => r.match === m).length;
  const parts = [n("same") && `${n("same")} same`, n("possible") && `${n("possible")} possible`, n("found") && `${n("found")} found`];
  return parts.filter(Boolean).join(" · ");
}

/** The answer without [#123] handles, should the model copy one from the search results (the chips link the events). */
export function stripHandles(text: string): string {
  return text.replace(/\s*\[#\d+\]/g, "");
}
