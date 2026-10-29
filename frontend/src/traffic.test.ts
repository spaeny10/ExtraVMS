import { describe, expect, it } from "vitest";
import {
  type PushSlot, confBand, confText, countBars, countsSummary, dateFromExpiry, directionText, expiryFromDate, expiryText, filterOptions,
  isSilent, listBadge, listError, nearChip, normPlate, onList, periodSince, plateProblem, plateText, pushText, slotText, stateText, timeAgo,
  vehicleText, knownPlate, otsoText, otsoWhen, isDescription, NO_ENTRY_DRAFT, conflictText, csvToRows, daysText, draftToBody, effectText,
  entriesToCsv, entryToDraft, expiryOf, filterEntries, normEntryPlate, scheduleText, seenText, type ListEntry,
} from "./traffic";

describe("one read", () => {
  it("shows the plate, or that there is none", () => {
    expect(plateText({ plate: "1691AJN" })).toBe("1691 AJN");
    expect(plateText({ plate: null })).toBe("No plate");
  });
  it("confidence as a percentage and a band", () => {
    expect(confText(0.894)).toBe("89 %");
    expect(confText(null)).toBe("");
    expect(confBand(0.8)).toBe("high");
    expect(confBand(0.6)).toBe("medium");
    expect(confBand(0.59)).toBe("low");
    expect(confBand(null)).toBeNull();
  });
  it("state and direction are only ever reported", () => {
    expect(stateText("KS")).toBe("state reported KS");
    expect(stateText(null)).toBe("");
    expect(directionText("away")).toBe("going away (reported)");
    expect(directionText("approach")).toBe("approaching (reported)");
    expect(directionText(null)).toBe("");
  });
  it("the vehicle: colour, brand, type", () => {
    expect(vehicleText({ vehicle_color: "White", vehicle_brand: "GMC", vehicle_type: "Truck" })).toBe("White GMC Truck");
    expect(vehicleText({ vehicle_color: "Black", vehicle_brand: null, vehicle_type: "SUV" })).toBe("Black SUV");
    expect(vehicleText({ vehicle_color: null, vehicle_brand: "-", vehicle_type: null })).toBe("");
  });
  it("list badges: deny, watch, allow, and possible for a near match", () => {
    expect(listBadge({ list: null, list_match: null })).toBeNull();
    expect(listBadge({ list: "deny", list_match: "exact" })).toMatchObject({ text: "Deny list", className: "list-badge deny" });
    expect(listBadge({ list: "watch", list_match: "near" })).toMatchObject({ text: "Possible · watch list", className: "list-badge watch near" });
    expect(listBadge({ list: "allow", list_match: "exact" })?.text).toBe("Allowed");
    expect(listBadge({ list: "deny", list_match: "near" })?.title).toMatch(/One character off/);
  });
  it("near plates as chips", () => {
    expect(nearChip("ABC1284")).toBe("Possible: ABC 1284");
  });
});

describe("time", () => {
  it("time ago", () => {
    const now = 1_000_000;
    expect(timeAgo(null, now)).toBe("never");
    expect(timeAgo(now - 10, now)).toBe("just now");
    expect(timeAgo(now - 120, now)).toBe("2 min ago");
    expect(timeAgo(now - 3 * 3600, now)).toBe("3 h ago");
    expect(timeAgo(now - 2 * 86400, now)).toBe("2 days ago");
    expect(timeAgo(now + 30, now)).toBe("just now");   // a camera clock a little ahead
  });
  it("a period's start", () => {
    const now = new Date(2026, 9, 28, 15, 30).getTime();
    expect(periodSince("all", now)).toBeUndefined();
    expect(periodSince("today", now)).toBe(new Date(2026, 9, 28).getTime() / 1000);
    expect(periodSince("24h", now)).toBe(now / 1000 - 86400);
    expect(periodSince("7d", now)).toBe(now / 1000 - 7 * 86400);
  });
});

describe("search", () => {
  it("plates as the server matches them", () => {
    expect(normPlate("abc-1234 ")).toBe("ABC1234");
  });
  it("filter options: the usual words first, then others seen, once each", () => {
    expect(filterOptions(["Car", "SUV"], ["suv", "Pickup", null, "-", "pickup", "Bus"])).toEqual(["Car", "SUV", "Pickup", "Bus"]);
  });
});

describe("counts", () => {
  it("fills the empty hours and keeps the server's", () => {
    const since = new Date(2026, 9, 28, 10, 20).getTime() / 1000;
    const until = new Date(2026, 9, 28, 14, 5).getTime() / 1000;
    const h11 = new Date(2026, 9, 28, 11).getTime() / 1000;
    const bars = countBars({ bucket: "hour", since, until, buckets: [{ t: h11, total: 12, plates: 5, direction: { away: 12 }, type: { car: 12 } }] });
    expect(bars.map((b) => b.t)).toEqual([10, 11, 12, 13, 14].map((h) => new Date(2026, 9, 28, h).getTime() / 1000));
    expect(bars.map((b) => b.total)).toEqual([0, 12, 0, 0, 0]);
    expect(bars[1].plates).toBe(5);
    expect(countsSummary(bars)).toBe("12 vehicles · 5 with a plate (42 %)");
    expect(countsSummary([])).toBe("No vehicles");
  });
  it("days start at local midnight", () => {
    const since = new Date(2026, 9, 22, 15).getTime() / 1000;
    const until = new Date(2026, 9, 28, 15).getTime() / 1000;
    const bars = countBars({ bucket: "day", since, until, buckets: [] });
    expect(bars).toHaveLength(7);
    expect(bars[0].t).toBe(new Date(2026, 9, 22).getTime() / 1000);
    expect(bars[6].t).toBe(new Date(2026, 9, 28).getTime() / 1000);
  });
});

describe("push health", () => {
  const now = 10_000;
  it("silent: set up and nothing for longer than the limit", () => {
    expect(isSilent({ enabled: true, last_push: now - 1000 }, 900, now)).toBe(true);
    expect(isSilent({ enabled: true, last_push: now - 100 }, 900, now)).toBe(false);
    expect(isSilent({ enabled: true, last_push: null }, 900, now)).toBe(true);
    expect(isSilent({ enabled: false, last_push: null }, 900, now)).toBe(false);
  });
  it("the push line", () => {
    expect(pushText({ enabled: true, last_push: now - 120 }, now)).toBe("Last push 2 min ago");
    expect(pushText({ enabled: true, last_push: null }, now)).toBe("No push yet");
    expect(pushText({ enabled: false, last_push: now }, now)).toBe("Not set up");
  });
});

describe("lists", () => {
  it("an expiry date is the end of that day, and back", () => {
    const ts = expiryFromDate("2026-11-01")!;
    expect(new Date(ts * 1000).getDate()).toBe(1);
    expect(new Date(ts * 1000).getHours()).toBe(23);
    expect(dateFromExpiry(ts)).toBe("2026-11-01");
    expect(expiryFromDate("")).toBeNull();
    expect(dateFromExpiry(null)).toBe("");
  });
  it("expiry text", () => {
    expect(expiryText({ expires_at: null, expired: false })).toBe("never");
    const ts = new Date(2026, 10, 1, 23, 59).getTime() / 1000;
    expect(expiryText({ expires_at: ts, expired: false })).toMatch(/^until /);
    expect(expiryText({ expires_at: ts, expired: true })).toMatch(/^expired /);
  });
  it("checks a typed plate", () => {
    expect(plateProblem("")).toMatch(/Type a plate/);
    expect(plateProblem("--")).toMatch(/Type a plate/);
    expect(plateProblem("A")).toMatch(/at least 2/);
    expect(plateProblem("ABCDEFGHIJK")).toMatch(/at most 10/);
    expect(plateProblem("abc 1234")).toBeNull();
  });
  it("takes a partial plate", () => {
    expect(normEntryPlate("abc 12?")).toBe("ABC12?");
    expect(normEntryPlate("34**")).toBe("34*");
    expect(plateProblem("ABC12?")).toBeNull();
    expect(plateProblem("34*")).toBeNull();
    expect(plateProblem("?*")).toMatch(/2 known characters/);
    expect(plateProblem("A?")).toMatch(/2 known characters/);
  });
  it("one entry per plate and list", () => {
    const entries = [{ plate: "ABC1234", list: "deny" as const }];
    expect(onList(entries, "abc-1234", "deny")).toBe(true);
    expect(onList(entries, "ABC1234", "watch")).toBe(false);
  });
  it("a viewer's 403 says who may", () => {
    const detail = (e: unknown) => String(e instanceof Error ? e.message : e).replace(/^\d{3}\s*/, "");
    expect(listError(new Error('403 {"detail":"needs operator"}'), detail)).toBe("Changing plate lists needs the operator role");
    expect(listError(new Error("422 not a plate"), detail)).toBe("not a plate");
  });
});

describe("list entry options", () => {
  const entry = (over: Partial<ListEntry> = {}): ListEntry => ({
    id: 1, plate: "ABC1234", list: "watch", note: "", expires_at: null, created_at: 0, by: null, expired: false, last_seen: null, last_camera: null, ...over,
  });
  it("says when an entry applies", () => {
    expect(scheduleText(null)).toBe("Always");
    expect(scheduleText({ when: "soc" })).toBe("During SOC hours");
    expect(scheduleText({ when: "hours", days: [0, 1, 2, 3, 4], from: "22:00", to: "06:00" })).toBe("Mon-Fri, 10:00 PM-6:00 AM");
    expect(daysText([5, 6])).toBe("Sat, Sun");
    expect(daysText([0, 2, 4])).toBe("Mon, Wed, Fri");
    expect(daysText([0, 1, 2, 3, 4, 5, 6])).toBe("Every day");
  });
  it("says what a hit does", () => {
    expect(effectText({ list: "deny" })).toBe("High alarm");
    expect(effectText({ list: "watch" })).toBe("Medium alarm");
    expect(effectText({ list: "allow" })).toBe("Marks the read");
    expect(effectText({ list: "allow", action: "alarm" })).toBe("Low alarm");
    expect(effectText({ list: "watch", priority: "high" })).toBe("High alarm");
    expect(effectText({ list: "deny", action: "mark" })).toBe("Marks the read");
  });
  it("warns when another list wins", () => {
    expect(conflictText({ list: "allow", overridden_by: "watch", conflicts: [{ id: 2, list: "watch", plate: "PR700809" }] }))
      .toBe("Does nothing: this plate is also on the watch list, which applies instead");
    expect(conflictText({ list: "watch", overridden_by: null, conflicts: [{ id: 1, list: "allow", plate: "PR700809" }] }))
      .toBe("Also on the allow list (this entry applies)");
    expect(conflictText({ list: "watch", overridden_by: null, conflicts: [] })).toBe("");
    expect(seenText({ seen_7d: 3, seen_30d: 12 })).toBe("3 this week · 12 in 30 days");
    expect(seenText({ seen_7d: 0, seen_30d: 0 })).toBe("not seen in 30 days");
  });
  it("turns the form into a request and back", () => {
    const now = 1_000_000;
    const body = draftToBody({ ...NO_ENTRY_DRAFT, plate: "abc 12?", list: "watch", note: " BOLO ", category: "police", expiry: "7d", when: "hours",
      days: [5, 6], from: "08:00", to: "12:00", action: "alarm", priority: "high", cameras: ["cam8"], expect: { color: "white", make: " ", vehicle_type: "van" } }, now);
    expect(body).toEqual({ plate: "ABC12?", list: "watch", note: "BOLO", expires_at: now + 7 * 86400, category: "police",
      schedule: { when: "hours", days: [5, 6], from: "08:00", to: "12:00" }, action: "alarm", priority: "high", cameras: ["cam8"],
      expect: { color: "white", vehicle_type: "van" } });
    const plain = draftToBody({ ...NO_ENTRY_DRAFT, plate: "XYZ1", action: "mark", priority: "high" }, now);
    expect(plain).toMatchObject({ schedule: null, action: "mark", priority: null, cameras: null, expect: null, category: null, expires_at: null });
    const back = entryToDraft(entry({ category: "police", schedule: { when: "soc" }, expect: { make: "Ford" }, expires_at: new Date(2026, 10, 1, 23, 59, 59).getTime() / 1000 }));
    expect(back).toMatchObject({ category: "police", when: "soc", expect: { make: "Ford" }, expiry: "date", date: "2026-11-01" });
    expect(expiryOf({ expiry: "", date: "" }, now)).toBeNull();
    expect(expiryOf({ expiry: "24h", date: "" }, now)).toBe(now + 86400);
  });
  it("searches plate, note, category and vehicle", () => {
    const es = [entry({ id: 1, plate: "ABC1234", note: "Dana", category: "employee", look: "white Ford F-150 pickup truck" }),
      entry({ id: 2, plate: "PR700809", note: "Shawn", list: "allow" })];
    expect(filterEntries(es, "abc 1234").map((e) => e.id)).toEqual([1]);
    expect(filterEntries(es, "employee").map((e) => e.id)).toEqual([1]);
    expect(filterEntries(es, "ford pickup").map((e) => e.id)).toEqual([1]);
    expect(filterEntries(es, "allowed").map((e) => e.id)).toEqual([2]);
    expect(filterEntries(es, "").length).toBe(2);
  });
  it("exports CSV that imports back", () => {
    const csv = entriesToCsv([entry({ plate: "ABC1234", list: "deny", category: "trespass", note: 'banned, "again"', expires_at: new Date(2026, 10, 1, 23, 59, 59).getTime() / 1000 })]);
    expect(csv.split("\n")[0]).toBe("plate,list,category,note,expires,applies,on_a_hit,last_seen");
    const rows = csvToRows(csv);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ plate: "ABC1234", list: "deny", category: "trespass", note: 'banned, "again"' });
    expect(new Date(rows[0].expires_at! * 1000).getDate()).toBe(1);
    // no header: plate, list, category, note, expires
    expect(csvToRows("XYZ999,Watch,,blue van\r\n\n")).toEqual([{ plate: "XYZ999", list: "watch", category: undefined, note: "blue van", expires_at: null }]);
    expect(csvToRows("")).toEqual([]);
  });
});

describe("camera setup", () => {
  const slot = (index: number, enabled: boolean, url: string, ours = false): PushSlot => ({ index, enabled, ours, url, event_mask: 765, status_report: ours });
  it("our slot, a free one, or none", () => {
    expect(slotText([slot(0, true, "http://x/other"), slot(1, true, "http://nvr/api/traffic/push/cam8/•••", true)]))
      .toBe("Set up: push slot 2 sends every read here, with a status report");
    expect(slotText([slot(0, true, "http://x/other"), slot(1, false, "")])).toBe("Not set up (a push slot is free)");
    expect(slotText([slot(0, true, "http://x/a"), slot(1, false, "http://x/b")])).toBe("Not set up (all push slots are in use)");
    expect(slotText([{ ...slot(2, false, "http://nvr/api/traffic/push/cam8/•••", true) }])).toMatch(/switched off on the camera/);
  });
});

describe("Otso's Read plate", () => {
  it("shows the camera's plate first, else Otso's", () => {
    expect(knownPlate({ plate: "1691AJN", otso_plate: "407MFL" })).toBe("1691AJN");
    expect(knownPlate({ plate: null, otso_plate: "407MFL" })).toBe("407MFL");
    expect(knownPlate({ plate: null })).toBeNull();
  });
  it("says what Otso read, or that it couldn't", () => {
    expect(otsoText({ at: 1, result: "read", plate: "407MFL", region: "KS", confidence: "high" })).toBe("Otso: 407 MFL · KS");
    expect(otsoText({ at: 1, result: "read", plate: "3416AFR", confidence: "medium" })).toBe("Otso: probably 3416 AFR");
    expect(otsoText({ at: 1, result: "unreadable", reason: "not legible" })).toBe("Otso couldn't read it");
    expect(otsoText({ at: 1, result: "skipped", reason: "no picture of the vehicle" })).toBe("Otso had no picture to read");
    expect(otsoText(null)).toBe("");
  });
  it("says whether a person asked or Otso looked on its own", () => {
    const t = () => "3:04 PM";
    expect(otsoWhen({ at: 1, result: "read", by: "otso" }, t)).toBe("on its own, 3:04 PM");
    expect(otsoWhen({ at: 1, result: "read", by: "person" }, t)).toBe("asked, 3:04 PM");
    expect(otsoWhen({ at: 1, result: "read" }, t)).toBe("asked, 3:04 PM");   // before Otso looked on its own
  });
});

describe("search by description", () => {
  it("tells a description from a plate", () => {
    for (const d of ["white pickup ladder rack", "Ladder", "trucks", "red f150", "box truck with logo", "Ford"]) expect(isDescription(d), d).toBe(true);
    for (const p of ["1691AJN", "1691", "ABC 123", "1691 AJN", "ABC-1234", "", "  ", "LADDER1"]) expect(isDescription(p), p).toBe(false);
  });
});
