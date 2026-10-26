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

test("an event below the Otso confidence gate says so instead of 'Writing synopsis…'", () => {
  const note = "No synopsis: YOLO 45% < 60%";
  const e = ev({ status: "verified", camera_class: "person", detections: { samples: [], keyframes: [], needed: 2, synopsis_skipped: note } });
  expect(placeholder(e)).toBe(note);
  expect(placeholder({ ...e, synopsis_pending: true })).toBe("Otso is writing a synopsis…");   // Generate with Otso
  expect(placeholder(ev({ status: "verified", camera_class: "person" }))).toBe("Writing synopsis…");
});

test("outside the Site's AI hours: waiting for an idle Otso, or skipped", () => {
  const e = ev({ status: "verified", camera_class: "person" });
  expect(placeholder({ ...e, synopsis_deferred: true })).toBe("Outside AI hours: described when Otso is free");
  const skipped = { ...e, detections: { samples: [], keyframes: [], needed: 2, synopsis_skipped: "No description (outside AI hours)" } };
  expect(placeholder(skipped)).toBe("No description (outside AI hours)");
});
