import { describe, expect, it } from "vitest";
import type { NvrEvent } from "./api";
import { colorFilterText, colorMatches, colorsText } from "./eventColors";
import { DEFAULT_FILTERS, activeFilterCount, buildQuery, eventQuery, matchesFilters, parseQuery } from "./findViews";

describe("event colors", () => {
  const person = { top: "green", bottom: "black", by: { top: "otso" as const, bottom: "picture" as const } };

  it("matches a color on one part or on any", () => {
    expect(colorMatches(person, "green")).toBe(true);
    expect(colorMatches(person, "green", "top")).toBe(true);
    expect(colorMatches(person, "green", "bottom")).toBe(false);
    expect(colorMatches({ vehicle: "white" }, "white", "vehicle")).toBe(true);
    expect(colorMatches(null, "white")).toBe(false);
    expect(colorMatches(null, "")).toBe(true);
  });

  it("says it in a few words", () => {
    expect(colorsText(person)).toBe("green top · black bottoms");
    expect(colorsText({ vehicle: "red" })).toBe("red vehicle");
    expect(colorsText({ by: {} })).toBeNull();
    expect(colorFilterText("blue", "bottom")).toBe("blue bottoms");
    expect(colorFilterText("blue", "")).toBe("blue");
  });

  it("is a Find filter: query, URL, count and live events", () => {
    const f = { ...DEFAULT_FILTERS, color: "green", colorPart: "top" as const };
    expect(eventQuery(f)).toMatchObject({ color: "green", color_part: "top" });
    expect(eventQuery({ ...DEFAULT_FILTERS, colorPart: "top" })).toMatchObject({ color: undefined, color_part: undefined });
    expect(activeFilterCount(f)).toBe(activeFilterCount(DEFAULT_FILTERS) + 1);
    const q = buildQuery("all", f, DEFAULT_FILTERS, "", "events");
    expect(q).toContain("color=green");
    expect(q).toContain("cpart=top");
    expect(parseQuery(q).patch).toMatchObject({ color: "green", colorPart: "top" });
    expect(parseQuery("?color=chartreuse").patch.color).toBeUndefined();   // not a color we name
    const e = { id: 1, camera_id: "cam3", camera_class: "person", status: "verified", start_ts: Date.now() / 1000, colors: person } as NvrEvent;
    expect(matchesFilters(e, f)).toBe(true);
    expect(matchesFilters(e, { ...f, colorPart: "bottom" })).toBe(false);
    expect(matchesFilters({ ...e, colors: null }, f)).toBe(false);
  });
});
