import { describe, expect, it } from "vitest";
import { eyeToggle } from "./Timeline";

const ALL = ["a", "b", "c", "d"];

describe("eyeToggle (a lane's 👁)", () => {
  it("in the grid, hides and shows a camera", () => {
    expect(eyeToggle({ visible: null, solo: null }, "b", ALL)).toEqual({ visible: ["a", "c", "d"], solo: null });
    expect(eyeToggle({ visible: ["a", "c"], solo: null }, "b", ALL)).toEqual({ visible: ["a", "c", "b"], solo: null });
  });
  it("isolated, another camera's eye plays it beside the isolated one (it used to flip an entry nobody saw)", () => {
    expect(eyeToggle({ visible: null, solo: "c" }, "a", ALL)).toEqual({ visible: ["a", "c"], solo: null });
    expect(eyeToggle({ visible: ["c"], solo: "c", order: ["d", "c"] }, "d", ALL)).toEqual({ visible: ["c", "d"], solo: null, order: ["d", "c"] });
  });
  it("isolated on a phone, switches to that camera", () => {
    expect(eyeToggle({ visible: null, solo: "c" }, "a", ALL, true)).toEqual({ visible: null, solo: "a" });
    expect(eyeToggle({ visible: ["c"], solo: "c" }, "a", ALL, true)).toEqual({ visible: ["c", "a"], solo: "a" });
  });
  it("hiding the isolated camera ends the solo", () => {
    expect(eyeToggle({ visible: null, solo: "c" }, "c", ALL)).toEqual({ visible: ["a", "b", "d"], solo: null });
  });
  it("a solo on a camera this Timeline no longer has counts as none", () => {
    expect(eyeToggle({ visible: null, solo: "gone" }, "a", ALL)).toEqual({ visible: ["b", "c", "d"], solo: "gone" });
  });
});
