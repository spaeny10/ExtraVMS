import { describe, expect, it } from "vitest";
import { EXPORT_MAX_S, GRID_MAX_S, exportProblem } from "./RangeDialog";

describe("exportProblem (the export dialog's limits, the server's too)", () => {
  const base = { keys: ["cam1"], start: 1000, end: 1090, grid: false };
  it("a camera and a range up to an hour is fine", () => {
    expect(exportProblem(base)).toBeNull();
    expect(exportProblem({ ...base, end: base.start + EXPORT_MAX_S })).toBeNull();
  });
  it("needs a camera and at least a second", () => {
    expect(exportProblem({ ...base, keys: [] })).toMatch(/camera/);
    expect(exportProblem({ ...base, end: base.start + 0.5 })).toMatch(/second/);
  });
  it("at most an hour, and the grid at most 30 minutes", () => {
    expect(exportProblem({ ...base, end: base.start + EXPORT_MAX_S + 1 })).toMatch(/60 minutes/);
    expect(exportProblem({ ...base, grid: true, end: base.start + GRID_MAX_S })).toBeNull();
    expect(exportProblem({ ...base, grid: true, end: base.start + GRID_MAX_S + 1 })).toMatch(/grid/);
  });
});
