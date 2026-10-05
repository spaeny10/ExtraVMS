import { describe, expect, it } from "vitest";
import { consoleHref, matchRoute, serverHref, siteHref } from "./nav";

describe("matchRoute", () => {
  it("top-level pages", () => {
    expect(matchRoute("/")).toEqual({ page: "home" });
    expect(matchRoute("/sites")).toEqual({ page: "sites" });
    expect(matchRoute("/sites/")).toEqual({ page: "sites" });
    expect(matchRoute("/find")).toEqual({ page: "find" });
    expect(matchRoute("/alerts")).toEqual({ page: "alerts" });
    expect(matchRoute("/audit")).toEqual({ page: "audit" });
    expect(matchRoute("/account")).toEqual({ page: "account" });
    expect(matchRoute("/nowhere")).toEqual({ page: "home" });
  });

  it("a site defaults to its Live tab", () => {
    expect(matchRoute("/sites/l_abc")).toEqual({ page: "site", siteId: "l_abc", tab: "live", redirect: "/sites/l_abc/live" });
    expect(matchRoute("/sites/l_abc/bogus")).toMatchObject({ page: "site", tab: "live", redirect: "/sites/l_abc/live" });
  });

  it("site tabs", () => {
    for (const tab of ["live", "timeline", "find", "alerts", "servers", "settings"])
      expect(matchRoute(`/sites/l_abc/${tab}`)).toEqual({ page: "site", siteId: "l_abc", tab });
  });

  it("a server inside a site", () => {
    expect(matchRoute("/sites/l_abc/servers/s_123")).toEqual({ page: "server", siteId: "l_abc", serverId: "s_123", tab: "servers" });
  });

  it("customer tabs, default and unknown", () => {
    expect(matchRoute("/customer")).toEqual({ page: "customer", tab: "sites" });
    for (const tab of ["sites", "servers", "members", "invites", "ai", "actions"])
      expect(matchRoute(`/customer/${tab}`)).toEqual({ page: "customer", tab });
    expect(matchRoute("/customer/nope")).toEqual({ page: "customer", tab: "sites", redirect: "/customer" });
  });

  it("/org aliases redirect to /customer", () => {
    expect(matchRoute("/org")).toEqual({ page: "customer", tab: "sites", redirect: "/customer" });
    expect(matchRoute("/org/actions")).toEqual({ page: "customer", tab: "actions", redirect: "/customer/actions" });
    expect(matchRoute("/org/members")).toEqual({ page: "customer", tab: "members", redirect: "/customer/members" });
  });

  it("decodes path segments", () => {
    expect(matchRoute("/sites/a%20b/live").siteId).toBe("a b");
  });
});

it("/fleet redirects to /sites", () => {
  expect(matchRoute("/fleet")).toEqual({ page: "sites", redirect: "/sites" });
});

describe("hrefs", () => {
  it("round-trip through matchRoute", () => {
    expect(matchRoute(siteHref("l_1", "servers"))).toEqual({ page: "site", siteId: "l_1", tab: "servers" });
    expect(matchRoute(serverHref("l_1", "s_2"))).toMatchObject({ page: "server", siteId: "l_1", serverId: "s_2" });
  });
  it("server console", () => {
    expect(consoleHref("s_2")).toBe("/s/s_2/");
    expect(consoleHref("s_2", "timeline")).toBe("/s/s_2/#timeline");
  });
});
