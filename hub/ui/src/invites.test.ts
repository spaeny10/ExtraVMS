import { describe, expect, it } from "vitest";
import { absoluteUrl, expiresIn, httpStatus, inviteAccessSummary, inviteErrorText } from "./invites";

describe("inviteAccessSummary", () => {
  it("all, named, none", () => {
    expect(inviteAccessSummary({ all_sites: true, locations: [] })).toBe("All sites");
    expect(inviteAccessSummary({ all_sites: false, locations: [{ id: "l_1", name: "HQ" }, { id: "l_2", name: "Yard" }] })).toBe("HQ, Yard");
    expect(inviteAccessSummary({ all_sites: false, locations: [] })).toBe("No sites");
  });
  it("pending rows name ids from the customer's Sites; a deleted Site shows its id", () => {
    expect(inviteAccessSummary({ all_sites: false, location_ids: ["l_2", "l_9"] }, [{ id: "l_2", name: "Yard" }])).toBe("Yard, l_9");
  });
});

describe("expiresIn", () => {
  it("rounds to a readable unit", () => {
    expect(expiresIn(100, 200)).toBe("expired");
    expect(expiresIn(1000 + 20 * 60, 1000)).toBe("in 20 min");
    expect(expiresIn(1000 + 5 * 3600, 1000)).toBe("in 5 h");
    expect(expiresIn(1000 + 6 * 86400, 1000)).toBe("in 6 d");
  });
});

describe("absoluteUrl", () => {
  it("keeps full links and anchors bare paths", () => {
    expect(absoluteUrl("https://hub.example/invite/x", "http://o")).toBe("https://hub.example/invite/x");
    expect(absoluteUrl("/invite/x", "https://o")).toBe("https://o/invite/x");
  });
});

describe("invite errors", () => {
  it("status", () => {
    expect(httpStatus(new Error('404 {"detail":"x"}'))).toBe(404);
    expect(httpStatus(new Error("Failed to fetch"))).toBe(0);
  });
  it("messages", () => {
    expect(inviteErrorText(new Error('404 {"detail":"this invite is unknown, used or expired"}'))).toMatch(/expired/);
    expect(inviteErrorText(new Error('403 {"detail":"this invite is for s***@x.com; sign in as that account"}'))).toBe("This invite is for s***@x.com; sign in as that account.");
    expect(inviteErrorText(new Error('401 {"detail":"wrong code"}'))).toBe("Wrong email, password or code.");
    expect(inviteErrorText(new Error('429 {"detail":"too many"}'))).toMatch(/15 minutes/);
    expect(inviteErrorText(new Error('422 {"detail":"password must be at least 10 characters"}'))).toBe("Password must be at least 10 characters.");
  });
});
