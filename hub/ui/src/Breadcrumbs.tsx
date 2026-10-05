/**
 * Customer › Site › Server as the page title of a Site: one heading instead of a crumb line above a separate title,
 * so the Site header fits on one row like the server UI's top bar. Each crumb but the last links up the hierarchy
 * (the customer to its Sites list); the last one is the current page.
 */
import type { Org } from "./api";
import { go, serverHref, siteHref } from "./nav";

type Crumb = { label: string; href?: string };

export function crumbsFor(org: Pick<Org, "name">, site?: { id: string; name: string }, server?: { id: string; name: string }): Crumb[] {
  const crumbs: Crumb[] = [{ label: org.name, href: "/sites" }];
  if (site) crumbs.push({ label: site.name, href: siteHref(site.id) });
  if (server && site) crumbs.push({ label: server.name, href: serverHref(site.id, server.id) });
  crumbs[crumbs.length - 1] = { label: crumbs[crumbs.length - 1].label };
  return crumbs;
}

export function Breadcrumbs({ org, site, server, title }: { org: Org; site?: { id: string; name: string }; server?: { id: string; name: string }; title?: string }) {
  const crumbs = crumbsFor(org, site, server);
  // role="navigation", not <nav>: the site toolkit pins every <nav> to the bottom of a phone screen as its tab bar
  return (
    <div className="crumbs-title" role="navigation" aria-label="Breadcrumb" title={title}><h2>
      {crumbs.map((c, i) => (
        <span key={i} className={i < crumbs.length - 1 ? "crumb-up" : "crumb-here"}>
          {i > 0 && <span className="crumb-sep" aria-hidden> › </span>}
          {c.href ? <a href={c.href} onClick={go(c.href)}>{c.label}</a> : <span aria-current="page">{c.label}</span>}
        </span>
      ))}
    </h2></div>
  );
}
