/**
 * The workstation keyboard, pure (chords.test.ts): a reducer from key presses to commands, so the page only wires
 * keydown → chordReducer → run(command), and the help overlay renders the same KEYMAP the reducer obeys.
 *
 * Single keys act at once; `g` starts a go-to (g l live, g t timeline, g r respond, g d full details); a disposition
 * group key (t / f / n from the catalogue) starts a disposition chord finished by its digit (F·1 = false alarm…).
 * Leaders time out after 2.5 s. Chords that change the record (dispositions, SOP digits) refuse unless the incident is
 * claimed by me, so a stray key on someone else's incident does nothing but say why. None of these keys is one the
 * Timeline uses (space, arrows, + -, [ ], 0–9 inside the Timeline): the page ignores keys typed inside it.
 */
import type { DispositionGroup } from "./types";

export const CHORD_TIMEOUT_MS = 2500;

export type ChordState = { mode: "idle" } | { mode: "leader"; key: string; at: number } | { mode: "go"; at: number };
export const IDLE: ChordState = { mode: "idle" };

export type KeyIn = { key: string; shift?: boolean; ctrl?: boolean; alt?: boolean; meta?: boolean; at: number };

export type ChordContext = {
  /** which right-pane tab is showing: digits tick SOP steps only on Respond */
  tab: "respond" | "resolve";
  hasIncident: boolean;
  claimedByMe: boolean;
  /** from the dispositions catalogue */
  groups: Pick<DispositionGroup, "key" | "dispositions" | "label">[];
};

export type Command =
  | { type: "next" } | { type: "prev" } | { type: "open" }
  | { type: "claim" } | { type: "claimNext" } | { type: "release" } | { type: "handoff" }
  | { type: "resolveTab" } | { type: "mute" } | { type: "popout" } | { type: "filter" } | { type: "help" } | { type: "escape" }
  | { type: "goLive" } | { type: "goTimeline" } | { type: "goRespond" } | { type: "goDetails" }
  | { type: "disposition"; code: string }
  | { type: "sop"; index: number }
  | { type: "refuse"; reason: string };

export type ChordResult = { state: ChordState; command: Command | null; /** the page should preventDefault */ handled: boolean };

/** What the help overlay lists (disposition chords are added from the catalogue). `aria` is aria-keyshortcuts syntax. */
export const KEYMAP: { keys: string; aria: string; label: string; group: "Queue" | "Incident" | "Go to" | "Console" }[] = [
  { keys: "j / k", aria: "J K", label: "Next / previous incident in the queue", group: "Queue" },
  { keys: "Enter", aria: "Enter", label: "Open the highlighted incident", group: "Queue" },
  { keys: "/", aria: "/", label: "Filter the queue", group: "Queue" },
  { keys: "Shift+C", aria: "Shift+C", label: "Claim the next ringing incident", group: "Queue" },
  { keys: "c", aria: "C", label: "Claim this incident", group: "Incident" },
  { keys: "l", aria: "L", label: "Release (let go of) this incident", group: "Incident" },
  { keys: "h", aria: "H", label: "Hand off to another operator", group: "Incident" },
  { keys: "r", aria: "R", label: "Resolve tab", group: "Incident" },
  { keys: "1–9", aria: "1 2 3 4 5 6 7 8 9", label: "Tick procedure step N (Respond tab)", group: "Incident" },
  { keys: "g l", aria: "G L", label: "Live video", group: "Go to" },
  { keys: "g t", aria: "G T", label: "Timeline", group: "Go to" },
  { keys: "g r", aria: "G R", label: "Respond tab", group: "Go to" },
  { keys: "g d", aria: "G D", label: "Full event details", group: "Go to" },
  { keys: "m", aria: "M", label: "Mute / unmute the alarm sound (15 min)", group: "Console" },
  { keys: "p", aria: "P", label: "Pop the incident out into its own window", group: "Console" },
  { keys: "?", aria: "Shift+?", label: "This help", group: "Console" },
  { keys: "Esc", aria: "Escape", label: "Cancel a chord, close help", group: "Console" },
];

const SINGLE: Record<string, Command["type"]> = {
  j: "next", k: "prev", Enter: "open", c: "claim", C: "claimNext", l: "release", h: "handoff", r: "resolveTab",
  m: "mute", p: "popout", "/": "filter", "?": "help", Escape: "escape",
};
const GO: Record<string, Command["type"]> = { l: "goLive", t: "goTimeline", r: "goRespond", d: "goDetails" };
const NEEDS_INCIDENT = new Set<Command["type"]>(["claim", "release", "handoff", "resolveTab", "popout", "goLive", "goTimeline", "goRespond", "goDetails"]);

/** The disposition a chord names, by group key and digit (unselectable ones such as "expired" have no key). */
export function dispositionFor(groups: ChordContext["groups"], leader: string, digit: string) {
  const g = groups.find((x) => x.key.toLowerCase() === leader);
  return g?.dispositions.find((d) => d.selectable && d.key === digit) ?? null;
}

/** "F·1": how a disposition's chord is written on its button. */
export const chordLabel = (groupKey: string, key: string | null) => (key ? `${groupKey.toUpperCase()}·${key}` : "");

const done = (command: Command | null, handled = command !== null): ChordResult => ({ state: IDLE, command, handled });

export function chordReducer(state: ChordState, k: KeyIn, ctx: ChordContext): ChordResult {
  // modified keys belong to the browser (Ctrl+R, Cmd+L…); Shift only matters for C and ?
  if (k.ctrl || k.alt || k.meta) return done(null, false);
  const live = state.mode !== "idle" && k.at - state.at <= CHORD_TIMEOUT_MS ? state : IDLE;
  // Shift+C arrives as "C"; any other letter is read lower-case (Caps Lock must not turn j into nothing)
  const shiftC = k.key === "C" && k.shift !== false;
  const key = shiftC ? "C" : k.key.length === 1 ? k.key.toLowerCase() : k.key;
  if (key === "Escape") return done({ type: "escape" });

  if (live.mode === "go") {
    const t = GO[key];
    if (!t) return done(null, false);
    return ctx.hasIncident ? done({ type: t } as Command) : done({ type: "refuse", reason: "Select an incident first" });
  }
  if (live.mode === "leader") {
    if (/^[0-9]$/.test(key)) {
      const d = dispositionFor(ctx.groups, live.key, key);
      if (!d) return done({ type: "refuse", reason: `No disposition ${chordLabel(live.key, key)}` });
      if (!ctx.hasIncident) return done({ type: "refuse", reason: "Select an incident first" });
      if (!ctx.claimedByMe) return done({ type: "refuse", reason: "Claim the incident before resolving it" });
      return done({ type: "disposition", code: d.code });
    }
    // anything else drops the leader and is read as a fresh key (t then j still moves down the queue)
    return chordReducer(IDLE, k, ctx);
  }

  if (key === "g") return { state: { mode: "go", at: k.at }, command: null, handled: true };
  if (ctx.groups.some((g) => g.key.toLowerCase() === key)) return { state: { mode: "leader", key, at: k.at }, command: null, handled: true };
  if (/^[1-9]$/.test(key)) {
    if (ctx.tab !== "respond") return done(null, false);
    if (!ctx.hasIncident) return done({ type: "refuse", reason: "Select an incident first" });
    if (!ctx.claimedByMe) return done({ type: "refuse", reason: "Claim the incident before ticking its procedure" });
    return done({ type: "sop", index: Number(key) - 1 });
  }
  const t = SINGLE[key];
  if (!t) return done(null, false);
  if (NEEDS_INCIDENT.has(t) && !ctx.hasIncident) return done({ type: "refuse", reason: "Select an incident first" });
  if (t === "release" && !ctx.claimedByMe) return done({ type: "refuse", reason: "Only the operator who claimed it can release it" });
  return done({ type: t } as Command);
}

/** Should the workstation read this key at all? Not while typing, inside a dialog, or inside the Timeline (its keys). */
export function ignoreTarget(el: { closest?: (sel: string) => unknown; isContentEditable?: boolean } | null): boolean {
  if (!el || typeof el.closest !== "function") return false;
  if (el.isContentEditable) return true;
  return !!el.closest("input, textarea, select, [contenteditable=true], .timeline-view, .modal-backdrop, .dialog-backdrop");
}
