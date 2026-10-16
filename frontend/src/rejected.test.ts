import { expect, test } from "vitest";
import type { NvrEvent } from "./api";
import { placeholder, rejectedReason } from "./Events";

const ev = (over: Partial<NvrEvent>) => ({ status: "rejected", camera_class: "vehicle", ...over }) as NvrEvent;

test("a parked-vehicle rejection shows its reason on the card", () => {
  const e = ev({ detections: { samples: [], keyframes: [], needed: 2, rejected: "parked vehicle, motion elsewhere" } });
  expect(rejectedReason(e)).toBe("Parked vehicle · motion elsewhere");
  expect(placeholder(e)).toBe("Parked vehicle · motion elsewhere");
});

test("a plain rejection keeps the YOLO text", () => {
  expect(rejectedReason(ev({}))).toBeNull();
  expect(placeholder(ev({}))).toBe("YOLO did not confirm the camera detection.");
});

test("an event below the Qwen confidence gate says so instead of 'Writing synopsis…'", () => {
  const note = "No synopsis: YOLO 45% < 60%";
  const e = ev({ status: "verified", camera_class: "person", detections: { samples: [], keyframes: [], needed: 2, synopsis_skipped: note } });
  expect(placeholder(e)).toBe(note);
  expect(placeholder({ ...e, synopsis_pending: true })).toBe("Qwen is writing a synopsis…");   // Generate with Qwen
  expect(placeholder(ev({ status: "verified", camera_class: "person" }))).toBe("Writing synopsis…");
});
