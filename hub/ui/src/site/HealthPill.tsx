/**
 * The Site header's health pill and its panel (SitePage SiteHeader). One pill — "3/3 servers · 13/14 cameras" with a
 * green / amber / red dot (siteHealth.ts) — replaces the row of status chips; a click opens a panel with what they
 * said and more: each server (online or offline, how this browser reaches it, its version), the cameras that are down
 * by name, the cellular score at the address (the old header chip: CoverageMap, linking to the card under the map), the
 * address, and the direct-connection offers ("N servers can connect directly (faster)", each with the Accept… /
 * Don't offer again that used to be a banner per server: direct.ts). A small dot on the pill says an offer is waiting.
 * The panel closes on Escape (focus back on the pill) or a click outside.
 */
import { useEffect, useId, useRef, useState } from "react";
import { Icon, useDismiss } from "@site/ui";
import { type Camera, type Server, type Site, type SiteCoverage, ago, api } from "../api";
import { bestCarrier, fmtScore } from "../coverage";
import { certOfferDismissed, directStateOf, useDirect, useDirectVersion } from "../direct";
import { go, navigate, serverHref, settingsHref } from "../nav";
import { cityState, fmtCoord, hasPoint } from "../place";
import { type ServerHealth, healthLabel, offersText, siteHealth } from "../siteHealth";
import { COVERAGE_ANCHOR } from "./CoverageCard";

const linkDown = (s: Server) => s.summary?.site_link_down === true;

export function HealthPill({ site, servers, cov }: { site: Site; servers: Server[]; cov: SiteCoverage | null }) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const id = useId();
  useDismiss(box, open, () => setOpen(false), btn);
  useEffect(() => { if (open) panel.current?.focus(); }, [open]);
  // keeps every server's route fresh while the header shows, and re-renders when one changes (direct.ts)
  useDirectVersion(servers);
  const best = cov ? bestCarrier(cov.data) : null;
  const rows: ServerHealth[] = servers.map((s) => {
    const route = directStateOf(s);
    return { id: s.id, name: s.name, online: s.online, route, offer: route === "cert" && !certOfferDismissed(s.id), linkDown: linkDown(s) };
  });
  const h = siteHealth({
    servers: rows, serversOnline: site.servers_online, serversTotal: site.servers_total,
    camerasOnline: site.cameras_online, camerasTotal: site.cameras_total, cellularScore: best?.score ?? null,
  });
  const offered = servers.filter((s) => rows.find((r) => r.id === s.id)?.offer && s.online);
  return (
    <div ref={box} className="health-anchor">
      <button ref={btn} type="button" className={`health-pill ${h.level}`} aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? id : undefined}
        aria-label={healthLabel(h)} title={h.reasons.length ? h.reasons.map((r) => r[0].toUpperCase() + r.slice(1)).join(" · ") : "All servers and cameras up"}
        onClick={() => setOpen(!open)}>
        <span className="hp-dot" aria-hidden="true" />
        <span className="hp-long">{h.text}</span>
        <span className="hp-short">{h.short}</span>
        {h.note && <span className="hp-note">· {h.note}</span>}
        {h.offers > 0 && <span className="hp-offer" aria-hidden="true" />}
        <Icon name="chevronDown" size={14} />
      </button>
      {open && (
        <div ref={panel} id={id} className="hub-pop health-pop" role="dialog" aria-label="Site health" tabIndex={-1}>
          <section className="pop-col">
            <span className="pop-h">Servers · {site.servers_total ? `${site.servers_online}/${site.servers_total} online` : "none yet"}</span>
            {servers.length === 0 && <span className="muted small">No servers in this site yet.</span>}
            <ul className="hp-list">
              {servers.map((s) => <ServerLine key={s.id} site={site} s={s} />)}
            </ul>
          </section>
          <CamerasDown site={site} />
          {cov && best && (
            <section className="pop-col">
              <span className="pop-h">Cellular</span>
              <CoverageLine site={site} cov={cov} />
            </section>
          )}
          {offered.length > 0 && (
            <section className="pop-col hp-offers">
              <span className="small hp-offers-h">{offersText(offered.length)}</span>
              <ul className="hp-list">{offered.map((s) => <OfferLine key={s.id} s={s} />)}</ul>
            </section>
          )}
          {site.address && (
            <section className="pop-col">
              <span className="pop-h">Address</span>
              <span className="small">{site.address}{" "}
                <a className="small" href={settingsHref(site.id, "general")} onClick={go(settingsHref(site.id, "general"))}>{hasPoint(site) ? "Map" : "Locate"}</a>
              </span>
            </section>
          )}
        </div>
      )}
    </div>
  );
}

/** "DC1 Server · online · direct on LAN · v1.4.2" (the route's tooltips are the old header chip's). */
function ServerLine({ site, s }: { site: Site; s: Server }) {
  const d = useDirect(s);
  const route = !s.online ? null
    : d.state === "direct" ? { text: "direct on LAN", cls: "ok", title: `Video comes straight from ${s.name} on this network (${d.base}), not through the hub` }
    : d.state === "checking" ? { text: "checking LAN…", cls: "", title: "Checking whether this browser can reach the server on its LAN" }
    : { text: d.state === "cert" && !d.dismissed ? "via hub · direct available" : "via hub", cls: "",
      title: "Video is relayed through the hub (this browser isn't on the server's network, or the server can't be reached directly)" };
  return (
    <li className="hp-row">
      <span className={`dot ${s.online ? "ok" : ""}`} aria-hidden="true" />
      <a href={serverHref(site.id, s.id)} onClick={go(serverHref(site.id, s.id))}>{s.name}</a>
      <span className={`small ${s.online ? "muted" : "error"}`}>{s.online ? "online" : `offline · last seen ${ago(s.last_seen_at)}`}</span>
      {route && <span className={`small hp-route ${route.cls}`} title={route.title}>{route.text}</span>}
      {s.online && linkDown(s) && <span className="small warn-text" title="Every camera is unreachable at once: the link to the site is down">link to the cameras down</span>}
      {s.version && <span className="muted small">v{s.version}</span>}
    </li>
  );
}

/** One offer: the server's name, Accept… (opens the server in a tab to accept its certificate) and Don't offer again. */
function OfferLine({ s }: { s: Server }) {
  const d = useDirect(s);
  if (d.state !== "cert" || d.dismissed) return null;
  return (
    <li className="hp-row" title={d.fingerprint ? `The server's certificate fingerprint (SHA-256): ${d.fingerprint}` : undefined}>
      <span>{s.name}</span>
      <button type="button" className="linkish small" onClick={d.enable}
        title="Opens the server in a new tab: accept the browser's warning there once, then come back to this tab">Accept…</button>
      <button type="button" className="linkish small" onClick={d.dismiss} title="Keep using the hub for this server and stop offering">Don't offer again</button>
    </li>
  );
}

/** The cameras that are down, by name (the hub's registry, read when the panel opens). */
function CamerasDown({ site }: { site: Site }) {
  const [cams, setCams] = useState<Camera[] | null>(null);
  const [failed, setFailed] = useState(false);
  const down = site.cameras_total - site.cameras_online;
  useEffect(() => {
    if (down <= 0) return;
    let live = true;
    api.locationCameras(site.id).then((c) => { if (live) { setCams(c); setFailed(false); } }).catch(() => { if (live) setFailed(true); });
    return () => { live = false; };
  }, [site.id, down, site.servers_online]);
  const multi = site.servers.filter((s) => !s.retired_at).length > 1;
  const list = (cams ?? []).filter((c) => c.enabled && !c.missing_since && !c.online);
  return (
    <section className="pop-col">
      <span className="pop-h">Cameras · {site.cameras_total ? `${site.cameras_online}/${site.cameras_total} up` : "none yet"}</span>
      {down <= 0 ? (site.cameras_total > 0 && <span className="muted small">All cameras up.</span>)
        : failed ? <span className="muted small">{down} down (the list couldn't be read).</span>
        : cams === null ? <span className="muted small">Loading…</span>
        : (
          <ul className="hp-list">
            {list.map((c) => (
              <li key={`${c.server_id}/${c.camera_id}`} className="hp-row">
                <span className="dot" aria-hidden="true" />
                <span>{multi ? `${c.server_name} · ${c.name}` : c.name}</span>
                <span className="muted small">{c.server_online === false ? "server offline" : (c.problems ?? []).join("; ") || "no stream"}</span>
              </li>
            ))}
            {list.length === 0 && <li className="muted small">{down} down.</li>}
          </ul>
        )}
    </section>
  );
}

/** "TMO 10.0 · T-Mobile LTE, overall 10.0 of 10": the best carrier at the address, linking to the coverage card. */
function CoverageLine({ site, cov }: { site: Site; cov: SiteCoverage }) {
  const best = bestCarrier(cov.data);
  if (!best) return null;
  const href = settingsHref(site.id, "general");
  const open = (e: React.MouseEvent) => {
    e.preventDefault();
    navigate(href);
    // the map loads lazily above it: scroll once it has had a moment to take its height
    setTimeout(() => document.getElementById(COVERAGE_ANCHOR)?.scrollIntoView({ behavior: "smooth", block: "start" }), 250);
  };
  return (
    <span className="small">
      <a className={`chip cov-chip ${cov.evaluation ? "eval" : ""}`} href={href} onClick={open}
        title={`Cellular: best here ${best.name} ${best.technology.toUpperCase()}, overall ${best.score.toFixed(1)} of 10 (CoverageMap)${cov.evaluation ? " · evaluation only, hub administrators" : ""}`}>
        {best.code} {fmtScore(best.score)}
      </a>{" "}
      <span className="muted">best here {best.name} {best.technology.toUpperCase()}, {fmtScore(best.score)} of 10</span>
    </span>
  );
}

/** The header's Map button: a pin icon; the address, the place and its coordinates are its tooltip. */
export function MapButton({ site }: { site: Site }) {
  const href = settingsHref(site.id, "general");
  const located = hasPoint(site);
  const title = located ? `${site.address || cityState(site.address_parts)} · ${fmtCoord(site.lat, site.lon)} · map in Settings`
    : site.address ? `${site.address} · not on the map yet: check the address in Settings` : "Not on the map yet: set the address in Settings";
  return (
    <a className={`icon-btn map-btn ${located ? "" : "muted"}`} href={href} onClick={go(href)} title={title} aria-label={located ? `Map: ${site.address || "in Settings"}` : "Locate this site in Settings"}>
      <Icon name="pin" size={18} />
    </a>
  );
}
