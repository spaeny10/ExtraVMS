import { describe, expect, it } from "vitest";
import { bestAttr, cameraAttrsDetail, cameraSaw, firmAttr, type CameraAttrs } from "./cameraAttrs";

describe("camera attributes", () => {
  const person: CameraAttrs = {
    top_color: { green: [7, 5.6], blue: [1, 0.4] }, bottom_color: { black: [7, 3.5] },
    helmet: { no: [12, 6], yes: [2, 1] }, hat: { yes: [5, 2.5] },
  };

  it("picks the most weighted value and its share", () => {
    expect(bestAttr(person, "top_color")).toEqual({ value: "green", n: 7, share: 0.93 });
    expect(bestAttr(person, "vehicle_type")).toBeNull();
    expect(bestAttr(null, "helmet")).toBeNull();
  });

  it("is firm only when said often and mostly the same way", () => {
    expect(firmAttr(person, "helmet")).toBe("no");
    expect(firmAttr({ helmet: { yes: [2, 2] } }, "helmet")).toBeNull();          // twice is not enough
    expect(firmAttr({ helmet: { yes: [5, 2.5], no: [5, 2.5] } }, "helmet")).toBeNull();   // split
  });

  it("describes a person and a vehicle in a few words", () => {
    expect(cameraSaw(person, "person")).toBe("green top, black bottoms, wearing a hat");
    expect(cameraSaw({ helmet: { yes: [4, 3.6] } }, "person")).toBe("wearing a hard hat");
    expect(cameraSaw({ vehicle_type: { truck: [3, 2.7] }, vehicle_color: { white: [1, 0.76] } }, "vehicle")).toBe("white truck");
    expect(cameraSaw({ vehicle_color: { red: [2, 1] } }, "vehicle")).toBe("red vehicle");
    expect(cameraSaw({}, "person")).toBeNull();
    expect(cameraSaw(null, "vehicle")).toBeNull();
  });

  it("lists everything with how often it was said", () => {
    expect(cameraAttrsDetail(person)).toBe("top green (7×, 93%) · bottoms black (7×) · hard hat no (12×, 86%) · hat yes (5×)");
  });
});
