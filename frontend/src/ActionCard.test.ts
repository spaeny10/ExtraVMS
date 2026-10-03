import { describe, expect, it } from "vitest";
import { blockedReason, canUndo, type ActionPlanCore } from "./ActionCard";

const plan = (card: Partial<ActionPlanCore["card"]> = {}, allowed = true): ActionPlanCore => ({
  id: "p1", action: "migrate_site", allowed,
  card: { title: "t", moves: [], stays: [], warnings: [], blockers: [], needs: [], can_execute: true, ...card },
});
const none = { options: {}, inputs: {} };

describe("blockedReason", () => {
  it("lets a plain card through", () => expect(blockedReason(plan(), none)).toBeNull());
  it("needs the role", () => expect(blockedReason(plan({ role: "admin" }, false), none)).toMatch(/admin/));
  it("waits for questions", () => expect(blockedReason(plan({ can_execute: false, needs: ["Which site?"] }), none)).toMatch(/questions/));
  it("wants the site name typed, ignoring case and spaces", () => {
    const p = plan({ confirm_name: "Hailo T1" });
    expect(blockedReason(p, none)).toMatch(/Type "Hailo T1"/);
    expect(blockedReason(p, { ...none, confirm_name: "Hailo" })).not.toBeNull();
    expect(blockedReason(p, { ...none, confirm_name: "  hailo   t1 " })).toBeNull();
  });
  it("wants required fields filled", () => {
    const p = plan({ inputs: [{ key: "password", label: "Camera password", type: "password" }, { key: "username", label: "User", type: "text", optional: true }] });
    expect(blockedReason(p, none)).toMatch(/camera password/);
    expect(blockedReason(p, { ...none, inputs: { password: "x" } })).toBeNull();
  });
});

describe("canUndo", () => {
  it("only for a successful action still inside its window", () => {
    expect(canUndo({ ok: true, lines: [], summary: "", audit_id: 3, undo_until: 200 }, 100)).toBe(true);
    expect(canUndo({ ok: true, lines: [], summary: "", audit_id: 3, undo_until: 50 }, 100)).toBe(false);
    expect(canUndo({ ok: false, lines: [], summary: "", audit_id: 3, undo_until: 200 }, 100)).toBe(false);
    expect(canUndo({ ok: true, lines: [], summary: "", audit_id: null, undo_until: null }, 100)).toBe(false);
    expect(canUndo(null)).toBe(false);
  });
});
