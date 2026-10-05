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

/** An <a> click that stays in the app (plain href kept so middle-click / open-in-new-tab still work). */
export const go = (path: string) => (e: React.MouseEvent) => {
  if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  navigate(path);
};

export const SITE_TABS = ["live", "timeline", "find", "alerts", "servers", "settings"] as const;
export type SiteTab = (typeof SITE_TABS)[number];
export const CUSTOMER_TABS = ["sites", "servers", "members", "invites", "ai", "actions"] as const;
export type CustomerTab = (typeof CUSTOMER_TABS)[number];

export type Page = "home" | "sites" | "site" | "server" | "find" | "alerts" | "customer" | "audit" | "account";
/**
 * Where a path points. `redirect` is the canonical path when the one asked for is an alias or incomplete
 * (/sites/:id → /sites/:id/live, /org/… → /customer/…); the app replaces the URL with it so links and Back stay clean.
 */
export type Route = { page: Page; siteId?: string; tab?: string; serverId?: string; redirect?: string };

const isOneOf = <T extends string>(list: readonly T[], v: string | undefined): v is T => !!v && (list as readonly string[]).includes(v);

export function matchRoute(path: string): Route {
  const parts = path.split("/").filter(Boolean).map((p) => decodeURIComponent(p));
  const [head, a, b, c] = parts;
  switch (head) {
    case undefined: return { page: "home" };
    case "sites": {
      if (!a) return { page: "sites" };
      if (b === "servers" && c) return { page: "server", siteId: a, serverId: c, tab: "servers" };
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
    case "find": return { page: "find" };
    case "alerts": return { page: "alerts" };
    case "audit": return { page: "audit" };
    case "account": return { page: "account" };
    default: return { page: "home" };
  }
}

export const siteHref = (siteId: string, tab: SiteTab = "live") => `/sites/${encodeURIComponent(siteId)}/${tab}`;
export const serverHref = (siteId: string, serverId: string) => `/sites/${encodeURIComponent(siteId)}/servers/${encodeURIComponent(serverId)}`;
/** The server's own UI through its tunnel (hash = its tab). */
export const consoleHref = (serverId: string, hash = "") => `/s/${serverId}/${hash ? `#${hash}` : ""}`;
