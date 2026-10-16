import { describe, expect, it } from "vitest";
import { RESYNC_HIDDEN_MS, shouldResync } from "./resync";

describe("shouldResync", () => {
  it("reloads only after the page was hidden a while", () => {
    expect(shouldResync(RESYNC_HIDDEN_MS - 1)).toBe(false);
    expect(shouldResync(RESYNC_HIDDEN_MS)).toBe(true);
    expect(shouldResync(7 * 3600_000)).toBe(true);   // a phone asleep since noon
  });
});
