import { describe, expect, it } from "vitest";
import { citedRefs, formatPlate, otherLabel, plateError, plateOutcome, splitCited } from "./plate";

describe("plates", () => {
  it("formats a plate with its region", () => {
    expect(formatPlate("ABC1234", "TX")).toBe("ABC 1234 · TX");
    expect(formatPlate("7ABC123")).toBe("7 ABC 123");
    expect(formatPlate("abc-1234")).toBe("ABC 1234");
    expect(formatPlate("TOWME")).toBe("TOWME");
    expect(formatPlate(null, "TX")).toBe("");
  });

  it("says what Read plate found", () => {
    expect(plateOutcome({ result: "read", plate: "ABC1234", region: "TX", reason: null })).toBe("Plate ABC 1234 · TX");
    expect(plateOutcome({ result: "unreadable", plate: null, region: null, reason: "not legible" })).toBe("Not readable in this footage");
    expect(plateOutcome({ result: "skipped", plate: null, region: null, reason: "too small: 40 px < 120 px" }))
      .toBe("Not readable in this footage (the vehicle is too small)");
    expect(plateOutcome({ result: "skipped", plate: null, region: null, reason: "no footage" })).toMatch(/no longer there/);
  });

  it("turns errors into a sentence", () => {
    expect(plateError(new Error('429 {"detail":"this plate was just read; try again in 25 s"}'))).toBe("this plate was just read; try again in 25 s");
    expect(plateError(new Error('403 {"detail":"needs operator"}'))).toBe("Reading a plate needs the operator role");
    expect(plateError(new Error("503 Otso is not ready yet"))).toBe("Otso is not ready yet");
  });
});

describe("references: cited first, the rest folded", () => {
  const items = [{ ref: "9" }, { ref: "7b" }, { ref: "4" }, { ref: "F1" }, { ref: undefined }, { ref: "2" }];
  it("orders the cited ones as the answer cites them", () => {
    const text = "The Nissan [#4] came back later [#7b]; again [#4]. Made up: [#99].";
    expect(citedRefs(text)).toEqual(["4", "7b", "99"]);
    const { cited, other } = splitCited(items, (x) => x.ref, text);
    expect(cited.map((x) => x.ref)).toEqual(["4", "7b"]);
    expect(other.map((x) => x.ref)).toEqual(["9", "F1", undefined, "2"]);
    expect(otherLabel(other.length)).toBe("Other events searched (4)");
  });
  it("nothing cited: everything is other", () => {
    const { cited, other } = splitCited(items, (x) => x.ref, "Nothing matched.");
    expect(cited).toEqual([]);
    expect(other.length).toBe(items.length);
  });
});
