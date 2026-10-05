/** Customer › Site › Server, each crumb a link up the hierarchy (the last one is the current page, not a link). */
import type { Org } from "./api";
import { go, serverHref, siteHref } from "./nav";

type Crumb = { label: string; href?: string };

export function Breadcrumbs({ org, site, server }: { org: Org; site?: { id: string; name: string }; server?: { id: string; name: string } }) {
  const crumbs: Crumb[] = [{ label: org.name, href: "/sites" }];
  if (site) crumbs.push({ label: site.name, href: siteHref(site.id) });
  if (server && site) crumbs.push({ label: server.name, href: serverHref(site.id, server.id) });
  crumbs[crumbs.length - 1] = { label: crumbs[crumbs.length - 1].label };
  return (
    <div className="crumbs small" aria-label="Breadcrumb">
      {crumbs.map((c, i) => (
        <span key={i}>
          {i > 0 && <span className="muted" aria-hidden> › </span>}
          {c.href ? <a href={c.href} onClick={go(c.href)}>{c.label}</a> : <span aria-current="page">{c.label}</span>}
        </span>
      ))}
    </div>
  );
}
