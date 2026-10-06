import { describe, expect, it } from "vitest";
import { actionText, actionsHref, instructionKey, looksLikeInstruction, outcomeText, parserLabel, textFromSearch, whereText } from "./fleetActions";

describe("looksLikeInstruction", () => {
  it("spots fleet instructions, with or without polite padding", () => {
    for (const t of ["Migrate Ironsight to Hailo T1", "Quiet alerts tonight", "Lock Side Yard footage 3-4 pm today",
      "please move the gate camera to Qwenbot", "Can you retire Old Barn?", "Stop describing vehicles on cam2", "Set Qwenbot to 7 days of recording"]) {
      expect(looksLikeInstruction(t), t).toBe(true);
    }
  });
  it("never takes a question or a search for one", () => {
    for (const t of ["how many people today?", "Did anyone move the ladder?", "white pickup truck", "person at the back door last night",
      "moving cars in the lot", "locked gate", "Quiet night?", "describe the man at the gate", ""]) {
      expect(looksLikeInstruction(t), t).toBe(false);
    }
  });
});

describe("Actions page links", () => {
  it("prefills the instruction box, encoded, and reads it back", () => {
    const href = actionsHref("Quiet alerts at Main & 5th for 2 hours");
    expect(href).toBe("/customer/actions?text=Quiet%20alerts%20at%20Main%20%26%205th%20for%202%20hours");
    expect(textFromSearch(href.slice(href.indexOf("?")))).toBe("Quiet alerts at Main & 5th for 2 hours");
    expect(actionsHref("  ")).toBe("/customer/actions");
    expect(textFromSearch("")).toBe("");
  });
});

describe("instruction box keys", () => {
  it("Enter plans, Shift+Enter and composition do not", () => {
    expect(instructionKey({ key: "Enter", shiftKey: false })).toBe("plan");
    expect(instructionKey({ key: "Enter", shiftKey: true })).toBeNull();
    expect(instructionKey({ key: "Enter", shiftKey: false, isComposing: true })).toBeNull();
    expect(instructionKey({ key: "a", shiftKey: false })).toBeNull();
  });
});

describe("card and log text", () => {
  const fmt = (ts: number) => `T${ts}`;
  it("names the parser", () => {
    expect(parserLabel("ai")).toBe("Read by the AI");
    expect(parserLabel("rules")).toBe("Read by the rule parser");
    expect(parserLabel(undefined)).toBeNull();
  });
  it("words each outcome", () => {
    expect(outcomeText({ outcome: "done", status: 200 }, fmt)).toBe("Done");
    expect(outcomeText({ outcome: "failed", status: 500, reason: "Echo is offline" }, fmt)).toBe("Failed: Echo is offline");
    expect(outcomeText({ outcome: "refused", status: 403, reason: "needs admin in this organization" }, fmt)).toBe("Refused: needs admin in this organization");
    expect(outcomeText({ outcome: "done", status: 200, undone_by: "sam@example.com", undone_at: 5 }, fmt)).toBe("Undone by sam@example.com at T5");
    // rows written before outcomes were recorded
    expect(outcomeText({ status: 200 }, fmt)).toBe("Done");
    expect(outcomeText({ status: 500 }, fmt)).toBe("Failed: see the details");
  });
  it("says where and what", () => {
    expect(whereText({ servers: ["Echo", "Delta"], location: "Harbor" })).toBe("Harbor · Echo, Delta");
    expect(whereText({ servers: ["Echo"], location: "Echo" })).toBe("Echo");
    expect(whereText({ servers: [] })).toBe("—");
    expect(actionText({ action: "fleet action: Undo: Retire Echo" })).toBe("Undo: Retire Echo");
  });
});
