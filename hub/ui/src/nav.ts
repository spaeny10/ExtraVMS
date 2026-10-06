import { useEffect, useState } from "react";

/** Path routing without a router library. */
export function navigate(path: string, replace = false) {
  if (replace) history.replaceState(null, "", path);
  else history.pushState(null, "", path);
  dispatchEvent(new PopStateEvent("popstate"));
}

export function usePath() {
  const [p, setP] = useState(location.pathname);
  useEffect(() => { const on = () => setP(location.pathname); addEventListener("popstate", on); return () => removeEventListener("popstate", on); }, []);
  return p;
}

/**
 * An <a> whose plain click does something in place (opens the event viewer) while its href still serves middle-click
 * and open-in-new-tab (e.g. the event's Timeline link).
 */
export const inPlace = (fn: () => void) => (e: React.MouseEvent) => {
  if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  fn();
};

/** An <a> click that stays in the app (plain href kept so middle-click / open-in-new-tab still work). */
export const go = (path: string) => inPlace(() => navigate(path));

export const SITE_TABS = ["live", "timeline", "find", "alerts", "servers", "settings"] as const;
export type SiteTab = (typeof SITE_TABS)[number];
export const CUSTOMER_TABS = ["sites", "servers", "members", "invites", "ai", "actions"] as const;
export type CustomerTab = (typeof CUSTOMER_TABS)[number];

/** Site → Settings sub-tabs. General is the Site's own form; the others configure SOC monitoring of the Site. */
export const SETTINGS_SECTIONS = ["general", "monitoring", "contacts", "procedures"] as const;
export type SettingsSection = (typeof SETTINGS_SECTIONS)[number];
/** The SOC area: the operator queue (and one incident, for pop-outs and phones), the supervisor view, reports. */
export type SocTab = "queue" | "incident" | "supervisor" | "reports";
export const SOC_REPORTS = ["operators", "false-alarms", "shifts", "customers"] as const;
export type SocReport = (typeof SOC_REPORTS)[number];

export type Page = "home" | "sites" | "site" | "server" | "find" | "alerts" | "customer" | "audit" | "account" | "invite" | "soc" | "hosts";
/**
 * The header's top-level items: path, label, icon (phone tab bar), the pages that light it up, an optional tooltip,
 * `soc` (shown to SOC staff only), `hubAdmin` (hub administrators only) and `siteOwned` (hidden inside a Site, see topNav).
 */
export type TopNavItem = { path: string; label: string; icon: string; pages: Page[]; title?: string; soc?: boolean; hubAdmin?: boolean; siteOwned?: boolean };
export const TOP_NAV: TopNavItem[] = [
  { path: "/", label: "Home", icon: "home", pages: ["home"] },
  // customer-wide Find and Alerts (/find, /alerts) are reached from the Sites page's "Find across all sites" and
  // "All alerts" buttons, so Sites stays lit there; a Site's own Find and Alerts are its tabs
  { path: "/sites", label: "Sites", icon: "grid", pages: ["sites", "site", "server", "find", "alerts"] },
  { path: "/soc", label: "SOC", icon: "lock", pages: ["soc"], soc: true },
  { path: "/customer", label: "Customer", icon: "settings", pages: ["customer"] },
  { path: "/audit", label: "Audit", icon: "events", pages: ["audit"] },
  // central recording hosts in the datacenter (hub/hub/hosts.py): hub administrators only
  { path: "/hub/hosts", label: "Hosts", icon: "timeline", pages: ["hosts"], title: "Central recording hosts", hubAdmin: true },
  { path: "/account", label: "Account", icon: "user", pages: ["account"] },
];

/** A page inside one Site (/sites/:id/*): its tabs are the navigation there. */
export const inSite = (page: Page) => page === "site" || page === "server";

/**
 * The top items to show on `page`. Find and Alerts are not top items: customer-wide search and alerts open from the
 * Sites page, and a Site's own Find and Alerts are its tabs (two same-named links going to different places
 * confused customers). `siteOwned` items, if any are added again, are hidden inside a Site.
 */
export function topNav(page: Page, socUser: boolean, hubAdmin = false): TopNavItem[] {
  return TOP_NAV.filter((n) => (!n.soc || socUser) && (!n.hubAdmin || hubAdmin) && !(n.siteOwned && inSite(page)));
}

/**
 * Pages laid out full-bleed like the server's own UI (no centered column): a Site's tabs (the Live grid and Timeline
 * want every pixel) and the SOC console. Lists and forms (Sites, Customer, Audit, Account…) stay centered.
 */
export const fullBleed = (page: Page) => inSite(page) || page === "soc";

/**
 * Where a path points. `redirect` is the canonical path when the one asked for is an alias or incomplete
 * (/sites/:id → /sites/:id/live, /org/… → /customer/…); the app replaces the URL with it so links and Back stay clean.
 */
export type Route = {
  page: Page; siteId?: string; tab?: string; serverId?: string; code?: string; redirect?: string;
  section?: SettingsSection; socTab?: SocTab; incidentId?: string; report?: SocReport;
};

const isOneOf = <T extends string>(list: readonly T[], v: string | undefined): v is T => !!v && (list as readonly string[]).includes(v);

export function matchRoute(path: string): Route {
  const parts = path.split("/").filter(Boolean).map((p) => decodeURIComponent(p));
  const [head, a, b, c] = parts;
  switch (head) {
    case undefined: return { page: "home" };
    case "sites": {
      if (!a) return { page: "sites" };
      if (b === "servers" && c) return { page: "server", siteId: a, serverId: c, tab: "servers" };
      // bare /settings is General (the form that was the whole tab before the SOC sub-tabs, so old links still land there)
      if (b === "settings") {
        if (!c) return { page: "site", siteId: a, tab: "settings", section: "general" };
        if (isOneOf(SETTINGS_SECTIONS, c)) return { page: "site", siteId: a, tab: "settings", section: c };
        return { page: "site", siteId: a, tab: "settings", section: "general", redirect: settingsHref(a, "general") };
      }
      if (isOneOf(SITE_TABS, b)) return { page: "site", siteId: a, tab: b };
      return { page: "site", siteId: a, tab: "live", redirect: `/sites/${encodeURIComponent(a)}/live` };
    }
    case "customer":
    case "org": {
      // /org was the old name; /org/actions keeps working for links in old notifications and bookmarks
      const tab = isOneOf(CUSTOMER_TABS, a) ? a : "sites";
      const canonical = a && tab === a ? `/customer/${tab}` : "/customer";
      return { page: "customer", tab, ...(head === "org" || (a && tab !== a) ? { redirect: canonical } : {}) };
    }
    // the old flat server list: Sites replaced it (the server cards live on under Customer → Servers)
    case "fleet": return { page: "sites", redirect: "/sites" };
    case "soc": {
      if (!a) return { page: "soc", socTab: "queue" };
      if (a === "incidents" && b) return { page: "soc", socTab: "incident", incidentId: b };
      if (a === "supervisor") return { page: "soc", socTab: "supervisor" };
      if (a === "reports") {
        if (!b) return { page: "soc", socTab: "reports", report: "operators" };
        if (isOneOf(SOC_REPORTS, b)) return { page: "soc", socTab: "reports", report: b };
        return { page: "soc", socTab: "reports", report: "operators", redirect: reportHref() };
      }
      // /soc/incidents without an id, or anything unknown: the queue
      return { page: "soc", socTab: "queue", redirect: socHref() };
    }
    case "find": return { page: "find" };
    case "alerts": return { page: "alerts" };
    case "audit": return { page: "audit" };
    case "account": return { page: "account" };
    // hub-wide administration (hub administrators only; App shows a notice to anyone else)
    case "hub": return a === "hosts" ? { page: "hosts" } : { page: "hosts", redirect: "/hub/hosts" };
    // public: the accept page works signed out (App renders it before the sign-in check)
    case "invite": return a ? { page: "invite", code: a } : { page: "home", redirect: "/" };
    default: return { page: "home" };
  }
}

export const siteHref = (siteId: string, tab: SiteTab = "live") => `/sites/${encodeURIComponent(siteId)}/${tab}`;
export const serverHref = (siteId: string, serverId: string) => `/sites/${encodeURIComponent(siteId)}/servers/${encodeURIComponent(serverId)}`;
/** Site → Settings → section; General is the bare /settings path. */
export const settingsHref = (siteId: string, section: SettingsSection = "general") =>
  `/sites/${encodeURIComponent(siteId)}/settings${section === "general" ? "" : `/${section}`}`;
/** SOC pages. The queue is /soc; one incident has its own path so it can be popped out or opened from a push. */
export const socHref = (tab: "queue" | "supervisor" | "reports" = "queue") => (tab === "queue" ? "/soc" : `/soc/${tab}`);
export const incidentHref = (id: number | string) => `/soc/incidents/${encodeURIComponent(String(id))}`;
export const reportHref = (report: SocReport = "operators") => (report === "operators" ? "/soc/reports" : `/soc/reports/${report}`);
/** The server's own UI through its tunnel (hash = its tab). */
export const consoleHref = (serverId: string, hash = "") => `/s/${serverId}/${hash ? `#${hash}` : ""}`;

/**
 * A moment on the server's own Timeline (its UI routes on the hash): the "open on server" fallback next to links into
 * the Site's combined Timeline (timelineLink.ts siteTimelineHref, same parameter names).
 */
export function consoleTimelineHref(serverId: string, at: { cam: string; event?: number; t?: number }): string {
  const q = new URLSearchParams({ cam: at.cam });
  if (at.event) q.set("event", String(at.event));
  else if (at.t) q.set("t", String(Math.round(at.t)));
  return consoleHref(serverId, `timeline?${q}`);
}
