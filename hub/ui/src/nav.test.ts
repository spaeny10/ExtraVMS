import { describe, expect, it } from "vitest";
import { consoleHref, consoleTimelineHref, fullBleed, incidentHref, matchRoute, reportHref, serverHref, settingsHref, siteHref, socHref, topNav } from "./nav";

describe("invite route", () => {
  it("is public and carries the code", () => {
    expect(matchRoute("/invite/AbC_123")).toEqual({ page: "invite", code: "AbC_123" });
    expect(matchRoute("/invite")).toEqual({ page: "home", redirect: "/" });
  });
});

describe("consoleTimelineHref", () => {
  it("an event or a moment on the server's own Timeline", () => {
    expect(consoleTimelineHref("s_2", { cam: "c 1", event: 5 })).toBe("/s/s_2/#timeline?cam=c+1&event=5");
    expect(consoleTimelineHref("s_2", { cam: "c", t: 10.4 })).toBe("/s/s_2/#timeline?cam=c&t=10");
    expect(consoleTimelineHref("s_2", { cam: "c", event: 5, t: 9 })).toBe("/s/s_2/#timeline?cam=c&event=5");
  });
});

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
    for (const tab of ["live", "timeline", "find", "alerts", "servers"])
      expect(matchRoute(`/sites/l_abc/${tab}`)).toEqual({ page: "site", siteId: "l_abc", tab });
  });

  it("settings sections; bare /settings is General", () => {
    expect(matchRoute("/sites/l_abc/settings")).toEqual({ page: "site", siteId: "l_abc", tab: "settings", section: "general" });
    for (const section of ["general", "monitoring", "contacts", "procedures"])
      expect(matchRoute(`/sites/l_abc/settings/${section}`)).toEqual({ page: "site", siteId: "l_abc", tab: "settings", section });
    expect(matchRoute("/sites/l_abc/settings/bogus")).toEqual({ page: "site", siteId: "l_abc", tab: "settings", section: "general", redirect: "/sites/l_abc/settings" });
  });

  it("SOC pages", () => {
    expect(matchRoute("/soc")).toEqual({ page: "soc", socTab: "queue" });
    expect(matchRoute("/soc/incidents/42")).toEqual({ page: "soc", socTab: "incident", incidentId: "42" });
    expect(matchRoute("/soc/incidents")).toEqual({ page: "soc", socTab: "queue", redirect: "/soc" });
    expect(matchRoute("/soc/supervisor")).toEqual({ page: "soc", socTab: "supervisor" });
    expect(matchRoute("/soc/reports")).toEqual({ page: "soc", socTab: "reports", report: "operators" });
    expect(matchRoute("/soc/reports/false-alarms")).toEqual({ page: "soc", socTab: "reports", report: "false-alarms" });
    expect(matchRoute("/soc/reports/nope")).toEqual({ page: "soc", socTab: "reports", report: "operators", redirect: "/soc/reports" });
    expect(matchRoute("/soc/whatever")).toEqual({ page: "soc", socTab: "queue", redirect: "/soc" });
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
  it("settings and SOC round-trip through matchRoute", () => {
    expect(settingsHref("l 1")).toBe("/sites/l%201/settings");
    expect(matchRoute(settingsHref("l_1", "contacts"))).toEqual({ page: "site", siteId: "l_1", tab: "settings", section: "contacts" });
    expect(matchRoute(settingsHref("l_1", "general")).section).toBe("general");
    expect(socHref()).toBe("/soc");
    expect(matchRoute(socHref("supervisor")).socTab).toBe("supervisor");
    expect(matchRoute(socHref("reports"))).toMatchObject({ socTab: "reports", report: "operators" });
    expect(matchRoute(incidentHref(7))).toEqual({ page: "soc", socTab: "incident", incidentId: "7" });
    expect(reportHref()).toBe("/soc/reports");
    expect(matchRoute(reportHref("shifts"))).toMatchObject({ socTab: "reports", report: "shifts" });
  });
  it("server console", () => {
    expect(consoleHref("s_2")).toBe("/s/s_2/");
    expect(consoleHref("s_2", "timeline")).toBe("/s/s_2/#timeline");
  });
});

describe("topNav", () => {
  const labels = (path: string, soc = false) => topNav(matchRoute(path).page, soc).map((n) => n.label);

  it("Find and Alerts are not in the top menu; their customer-wide pages still open and light up Sites", () => {
    for (const p of ["/", "/sites", "/find", "/alerts", "/customer/members"])
      expect(labels(p)).toEqual(["Home", "Sites", "Customer", "Audit", "Account"]);
    const sites = topNav("home", false).find((n) => n.label === "Sites")!;
    expect(sites.pages).toContain(matchRoute("/find").page);
    expect(sites.pages).toContain(matchRoute("/alerts").page);
  });

  it("inside a Site (every tab and the server panel): no Find or Alerts, the Site's tabs own them", () => {
    for (const p of ["/sites/l_1/live", "/sites/l_1/timeline", "/sites/l_1/find", "/sites/l_1/alerts", "/sites/l_1/servers",
      "/sites/l_1/settings/contacts", "/sites/l_1/servers/s_1"]) {
      expect(labels(p)).toEqual(["Home", "Sites", "Customer", "Audit", "Account"]);
      expect(labels(p, true)).toEqual(["Home", "Sites", "SOC", "Customer", "Audit", "Account"]);
    }
  });

  it("Hosts only for hub administrators", () => {
    expect(topNav("home", false, true).map((n) => n.label)).toEqual(["Home", "Sites", "Customer", "Audit", "Hosts", "Account"]);
    expect(labels("/")).not.toContain("Hosts");
    expect(matchRoute("/hub/hosts")).toEqual({ page: "hosts" });
    expect(matchRoute("/hub")).toEqual({ page: "hosts", redirect: "/hub/hosts" });
    expect(topNav("hosts", false, true).find((n) => n.label === "Hosts")!.pages).toContain("hosts");
  });

  it("SOC only for SOC staff", () => {
    expect(labels("/", true)).toEqual(["Home", "Sites", "SOC", "Customer", "Audit", "Account"]);
    expect(labels("/soc", false)).not.toContain("SOC");
  });

  it("Sites stays lit inside a Site", () => {
    const sites = topNav("site", false).find((n) => n.label === "Sites")!;
    expect(sites.pages).toContain(matchRoute("/sites/l_1/live").page);
    expect(sites.pages).toContain(matchRoute("/sites/l_1/servers/s_1").page);
  });
});

describe("fullBleed", () => {
  it("Site pages and the SOC fill the window; lists and forms stay centered", () => {
    for (const p of ["/sites/l_1/live", "/sites/l_1/timeline", "/sites/l_1/servers/s_1", "/soc"]) expect(fullBleed(matchRoute(p).page)).toBe(true);
    for (const p of ["/", "/sites", "/find", "/alerts", "/customer", "/audit", "/account"]) expect(fullBleed(matchRoute(p).page)).toBe(false);
  });
});
