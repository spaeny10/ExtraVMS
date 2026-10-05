import { describe, expect, it } from "vitest";
import { localFindSource, nextLocalCursor } from "./findSource";
import { DEFAULT_VIEW_KEY } from "./findViews";

describe("nextLocalCursor", () => {
  const page = (ids: number[]) => ids.map((id) => ({ id }));
  it("pages newest-first by the last id and stops on a short page", () => {
    expect(nextLocalCursor("id", page([9, 8, 7]), null, 3)).toEqual({ before_id: 7 });
    expect(nextLocalCursor("id", page([6, 5]), { before_id: 7 }, 3)).toBeNull();
    expect(nextLocalCursor("id", [], null, 3)).toBeNull();
  });
  it("pages by offset, adding what came back", () => {
    expect(nextLocalCursor("offset", page([1, 2, 3]), null, 3)).toEqual({ offset: 3 });
    expect(nextLocalCursor("offset", page([4, 5, 6]), { offset: 3 }, 3)).toEqual({ offset: 6 });
    expect(nextLocalCursor("offset", page([7]), { offset: 6 }, 3)).toBeNull();
  });
});

describe("localFindSource", () => {
  it("is this server: events keyed by id, every feature on, the shared default-view key", () => {
    const s = localFindSource();
    const e = { id: 12, camera_id: "cam3" } as Parameters<typeof s.eventKey>[0];
    expect(s.eventKey(e)).toBe("12");
    expect(s.cameraKey(e)).toBe("cam3");
    expect(s.features).toEqual({ assistant: true, footage: true, identities: true, summary: true });
    expect(s.views.defaultKey).toBe(DEFAULT_VIEW_KEY);
    expect(s.views.canEdit).toBe(true);
    expect(s.renderDetail).toBeUndefined();
  });
});
