import { describe, expect, it } from "vitest";
import { CHORD_TIMEOUT_MS, type ChordContext, type ChordState, IDLE, KEYMAP, chordLabel, chordReducer, dispositionFor, ignoreTarget } from "./chords";

const groups: ChordContext["groups"] = [
  { key: "t", label: "True alarm", dispositions: [{ code: "true_alarm_dispatched", label: "Dispatched", needs_notes: true, selectable: true, four_eyes: ["high"], key: "1" }] },
  { key: "f", label: "False alarm", dispositions: [{ code: "false_alarm", label: "False alarm", needs_notes: false, selectable: true, four_eyes: [], key: "1" },
    { code: "nuisance", label: "Nuisance", needs_notes: false, selectable: true, four_eyes: [], key: "2" }] },
  { key: "n", label: "Not actionable", dispositions: [{ code: "expired", label: "Expired", needs_notes: false, selectable: false, four_eyes: [], key: null }] },
];
const ctx: ChordContext = { tab: "respond", hasIncident: true, claimedByMe: true, groups };

/** Feed keys one after another (1 ms apart unless `gap`), returning every command. */
function run(keys: (string | { key: string; shift?: boolean; ctrl?: boolean; gap?: number })[], c: ChordContext = ctx) {
  let s: ChordState = IDLE, at = 0;
  const out: unknown[] = [];
  for (const k of keys) {
    const o = typeof k === "string" ? { key: k } : k;
    at += o.gap ?? 1;
    const r = chordReducer(s, { key: o.key, shift: o.shift, ctrl: o.ctrl, at }, c);
    s = r.state;
    if (r.command) out.push(r.command);
  }
  return { out, state: s };
}

describe("single keys", () => {
  it("map to commands", () => {
    expect(run(["j", "k", "Enter", "c", "l", "h", "r", "m", "p", "/", "?", "Escape"]).out.map((c) => (c as { type: string }).type))
      .toEqual(["next", "prev", "open", "claim", "release", "handoff", "resolveTab", "mute", "popout", "filter", "help", "escape"]);
  });
  it("Shift+C claims the next ringing incident; Caps Lock C is a plain claim", () => {
    expect(run([{ key: "C", shift: true }]).out).toEqual([{ type: "claimNext" }]);
    expect(run([{ key: "C", shift: false }]).out).toEqual([{ type: "claim" }]);
  });
  it("modified keys are the browser's", () => {
    const r = chordReducer(IDLE, { key: "r", ctrl: true, at: 1 }, ctx);
    expect(r).toEqual({ state: IDLE, command: null, handled: false });
  });
  it("incident commands need an incident; release needs my claim", () => {
    expect(run(["c"], { ...ctx, hasIncident: false }).out).toEqual([{ type: "refuse", reason: "Select an incident first" }]);
    expect(run(["l"], { ...ctx, claimedByMe: false }).out[0]).toMatchObject({ type: "refuse" });
    expect(run(["j"], { ...ctx, hasIncident: false }).out).toEqual([{ type: "next" }]);
  });
  it("unknown keys are not handled", () => {
    expect(chordReducer(IDLE, { key: "x", at: 1 }, ctx).handled).toBe(false);
  });
});

describe("go prefix", () => {
  it("g l / t / r / d", () => {
    expect(run(["g", "l", "g", "t", "g", "r", "g", "d"]).out.map((c) => (c as { type: string }).type)).toEqual(["goLive", "goTimeline", "goRespond", "goDetails"]);
  });
  it("times out after 2.5 s", () => {
    expect(run(["g", { key: "l", gap: CHORD_TIMEOUT_MS + 1 }]).out).toEqual([{ type: "release" }]);
  });
  it("g then an unknown key does nothing", () => {
    expect(run(["g", "z"])).toEqual({ out: [], state: IDLE });
  });
});

describe("disposition chords", () => {
  it("leader + digit from the catalog", () => {
    expect(run(["f", "1"]).out).toEqual([{ type: "disposition", code: "false_alarm" }]);
    expect(run(["f", "2"]).out).toEqual([{ type: "disposition", code: "nuisance" }]);
    expect(run(["t", "1"]).out).toEqual([{ type: "disposition", code: "true_alarm_dispatched" }]);
  });
  it("refuses unless claimed by me", () => {
    expect(run(["f", "1"], { ...ctx, claimedByMe: false }).out).toEqual([{ type: "refuse", reason: "Claim the incident before resolving it" }]);
  });
  it("unknown or unselectable digits say so", () => {
    expect(run(["f", "9"]).out).toEqual([{ type: "refuse", reason: "No disposition F·9" }]);
    expect(dispositionFor(groups, "n", "1")).toBe(null);
  });
  it("a leader followed by another key reads that key fresh", () => {
    expect(run(["t", "j"]).out).toEqual([{ type: "next" }]);
  });
  it("leaders time out", () => {
    expect(run(["f", { key: "1", gap: CHORD_TIMEOUT_MS + 1 }]).out).toEqual([{ type: "sop", index: 0 }]);
  });
  it("chord labels", () => {
    expect(chordLabel("f", "1")).toBe("F·1");
    expect(chordLabel("n", null)).toBe("");
  });
});

describe("SOP digits", () => {
  it("only on the Respond tab, only when claimed by me", () => {
    expect(run(["3"]).out).toEqual([{ type: "sop", index: 2 }]);
    expect(chordReducer(IDLE, { key: "3", at: 1 }, { ...ctx, tab: "resolve" }).handled).toBe(false);
    expect(run(["3"], { ...ctx, claimedByMe: false }).out[0]).toMatchObject({ type: "refuse" });
    expect(chordReducer(IDLE, { key: "0", at: 1 }, ctx).handled).toBe(false);
  });
});

it("ignores keys typed in fields, dialogs and the Timeline", () => {
  const el = (match: boolean) => ({ closest: () => (match ? {} : null) });
  expect(ignoreTarget(el(true))).toBe(true);
  expect(ignoreTarget(el(false))).toBe(false);
  expect(ignoreTarget({ closest: () => null, isContentEditable: true })).toBe(true);
  expect(ignoreTarget(null)).toBe(false);
});

it("the help table covers every single key the reducer knows", () => {
  const listed = KEYMAP.map((k) => k.keys).join(" ");
  for (const k of ["j", "k", "Enter", "/", "Shift+C", "c", "l", "h", "r", "1–9", "g l", "g t", "g r", "g d", "m", "p", "?", "Esc"]) expect(listed).toContain(k);
});
