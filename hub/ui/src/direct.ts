/**
 * Direct-on-LAN: when this browser sits on the same LAN as a server, its video, frames and snapshots come straight
 * from the server instead of through the hub's tunnel (the site uplink is what makes remote playback buffer).
 *
 * Per server, from its card's `direct` (hub/hub/direct.py info):
 *   1. candidates(): the server's own base URLs, `local` ones (http://localhost:8080, only works on that machine) first.
 *      Plain-http LAN addresses are dropped on an https hub page: the browser blocks them as mixed content anyway.
 *   2. probe() every candidate in parallel: GET <url>/api/direct/probe (CORS, 1.5 s). A browser off the LAN times out.
 *   3. The first candidate (in order) that answered: mint a token at the hub (POST /api/servers/{id}/direct-token),
 *      then GET <url>/api/direct/handshake?token= (sets the server's `direct` cookie, proves the token is accepted).
 *      → "direct" {base, token, exp}. Media URLs carry ?direct=<token> (a <video> src can't send a header, and the
 *      cookie is cross-site, which a browser may block as third-party).
 *   4. Nothing answered but an https LAN URL failed fast with a TypeError: that is what an untrusted self-signed
 *      certificate looks like (we can't tell it apart from "refused" precisely) → "cert": the UI offers to open the
 *      probe URL in a tab so the user can accept the certificate once; returning to this window re-probes.
 *   5. Otherwise → "hub" (everything stays on the tunnel).
 * The answer is re-checked every RECHECK_MS (a laptop leaves the LAN), on window focus once it is FOCUS_RECHECK_MS
 * old (always when "cert"), and when the network comes back; the token is re-minted REMINT_LEAD_S before it expires.
 * A re-check keeps the previous state until it has a new answer, so media doesn't flap to the hub and back.
 *
 * Only reads and WHEP go direct (the server refuses other writes on a direct token); hubSource.mediaApi() builds a
 * client whose URL builders point here and whose REST calls stay on the hub.
 */
import { useEffect, useSyncExternalStore } from "react";
import { api, onServerCards, type DirectInfo, type DirectToken, type Server } from "./api";

export const PROBE_TIMEOUT_MS = 1500;
export const HANDSHAKE_TIMEOUT_MS = 4000;
export const RECHECK_MS = 5 * 60_000;
export const FOCUS_RECHECK_MS = 30_000;
/** re-mint this long before the token expires (hub tokens last 15 min) */
export const REMINT_LEAD_S = 60;

export type DirectState = "checking" | "direct" | "hub" | "cert";
export type ProbeResult = "ok" | "unreachable" | "maybe-cert";
export type DirectEntry = {
  state: DirectState;
  /** "direct": the server base URL media goes to */
  base: string | null;
  token: string | null;
  /** token expiry, unix seconds (0 = none) */
  exp: number;
  /** "cert": the https URL whose certificate the user should accept */
  certUrl: string | null;
  fingerprint: string | null;
  /** when this answer was reached (ms) */
  checkedAt: number;
};

const trimSlash = (u: string) => u.replace(/\/+$/, "");

/** Is this URL's host only reachable from the machine itself (and so allowed over http from an https page)? */
export function isLoopback(url: string): boolean {
  try {
    const h = new URL(url).hostname.toLowerCase();
    return h === "localhost" || h.endsWith(".localhost") || h === "[::1]" || h === "::1" || /^127\./.test(h);
  } catch {
    return false;
  }
}

/**
 * The URLs worth probing, in preference order: `local` first (on the server itself it is the fastest path), then the
 * LAN addresses as the server listed them. Duplicates and trailing slashes are dropped; on an https page plain-http
 * non-loopback URLs are dropped too (mixed content: the browser would refuse them without even trying).
 */
export function candidates(info: DirectInfo | null | undefined, pageProtocol: string = typeof location === "undefined" ? "https:" : location.protocol): string[] {
  if (!info?.available || !Array.isArray(info.urls)) return [];
  // the server's heartbeat lists LAN URLs as plain strings and the localhost hint as {url, local}: accept both shapes
  const rows = (info.urls as (string | { url: string; local?: boolean })[])
    .map((u) => (typeof u === "string" ? { url: u } : u))
    .filter((u) => u && typeof u.url === "string" && /^https?:\/\//i.test(u.url));
  const ordered = [...rows.filter((u) => u.local), ...rows.filter((u) => !u.local)].map((u) => trimSlash(u.url));
  const secure = pageProtocol === "https:";
  return [...new Set(ordered)].filter((u) => !secure || u.toLowerCase().startsWith("https://") || isLoopback(u));
}

/**
 * Why a probe failed. A timeout means the address isn't on this network ("unreachable"). A fast TypeError on an
 * https LAN URL is how fetch reports an untrusted certificate — but also a refused connection; we can't tell them
 * apart, so it is only "maybe-cert" and the UI words its offer accordingly.
 */
export function classifyProbeError(url: string, err: unknown): ProbeResult {
  const name = (err as { name?: string } | null)?.name;
  if (name === "AbortError" || name === "TimeoutError") return "unreachable";
  if (err instanceof TypeError && url.toLowerCase().startsWith("https://") && !isLoopback(url)) return "maybe-cert";
  return "unreachable";
}

type Fetch = typeof fetch;

async function withTimeout<T>(ms: number, run: (signal: AbortSignal) => Promise<T>): Promise<T> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), ms);
  try {
    return await run(ctrl.signal);
  } finally {
    clearTimeout(t);
  }
}

/** GET <url>/api/direct/probe: 2xx = this browser can reach the server directly (and CORS allows this hub). */
export async function probe(url: string, fetchImpl: Fetch = fetch, timeoutMs = PROBE_TIMEOUT_MS): Promise<ProbeResult> {
  try {
    const r = await withTimeout(timeoutMs, (signal) => fetchImpl(`${url}/api/direct/probe`, { mode: "cors", credentials: "omit", cache: "no-store", signal }));
    // a server without the route (older version) answers 404: reachable, but no direct mode to use
    return r.ok ? "ok" : "unreachable";
  } catch (e) {
    return classifyProbeError(url, e);
  }
}

export type Handshake = { ok: boolean; user?: string; role?: string; exp?: number };
/** GET <base>/api/direct/handshake?token=: the server checks the token and sets its `direct` cookie. */
export async function handshake(base: string, token: string, fetchImpl: Fetch = fetch, timeoutMs = HANDSHAKE_TIMEOUT_MS): Promise<Handshake | null> {
  try {
    // the token also rides as ?direct= (which the server checks before any cookie): a stale `direct` cookie from an
    // earlier token would otherwise get this very request refused before it can be replaced
    const t = encodeURIComponent(token);
    const r = await withTimeout(timeoutMs, (signal) => fetchImpl(`${base}/api/direct/handshake?token=${t}&direct=${t}`,
      { mode: "cors", credentials: "include", cache: "no-store", signal }));
    if (!r.ok) return null;
    const j = (await r.json()) as Handshake;
    return j && j.ok ? j : null;
  } catch {
    return null;
  }
}

/** What the probes say, before any token: the URL to go direct to, the URL whose certificate to offer, or neither. */
export function decide(urls: string[], results: ProbeResult[]): { ok: string | null; cert: string | null } {
  const ok = urls.find((_, i) => results[i] === "ok") ?? null;
  const cert = ok ? null : urls.find((_, i) => results[i] === "maybe-cert") ?? null;
  return { ok, cert };
}

export type ResolveDeps = { fetch: Fetch; mint: (server: string) => Promise<DirectToken>; now: () => number; probeTimeoutMs?: number };

const entry = (state: DirectState, over: Partial<DirectEntry> = {}, now = Date.now()): DirectEntry =>
  ({ state, base: null, token: null, exp: 0, certUrl: null, fingerprint: null, checkedAt: now, ...over });

/**
 * One full resolution for `server`. `prev` is the current answer: its token is reused while it has more than
 * REMINT_LEAD_S left and the same base still answers (no mint, no handshake), which keeps a 5-minute re-check cheap
 * and well under the hub's mint limit.
 */
export async function resolve(server: string, info: DirectInfo | null | undefined, deps: ResolveDeps, prev?: DirectEntry | null,
  pageProtocol?: string): Promise<DirectEntry> {
  const fingerprint = info?.fingerprint ?? null;
  const urls = candidates(info, pageProtocol);
  if (!urls.length) return entry("hub", { fingerprint }, deps.now());
  const results = await Promise.all(urls.map((u) => probe(u, deps.fetch, deps.probeTimeoutMs)));
  const { ok, cert } = decide(urls, results);
  if (!ok) return entry(cert ? "cert" : "hub", { certUrl: cert, fingerprint }, deps.now());
  const nowS = deps.now() / 1000;
  if (prev?.state === "direct" && prev.base === ok && prev.token && prev.exp - REMINT_LEAD_S > nowS) {
    return { ...prev, checkedAt: deps.now(), fingerprint };
  }
  let tok: DirectToken;
  try {
    tok = await deps.mint(server);
  } catch {
    return entry("hub", { fingerprint }, deps.now());   // not allowed, rate-limited, or the hub is busy: the tunnel still works
  }
  if (!tok?.token) return entry("hub", { fingerprint }, deps.now());
  // the answering URL first, then any other candidate that answered (a server can list two NICs)
  const order = [ok, ...urls.filter((u, i) => u !== ok && results[i] === "ok")];
  for (const base of order) {
    const hs = await handshake(base, tok.token, deps.fetch);
    if (hs) return entry("direct", { base, token: tok.token, exp: tok.exp, fingerprint: tok.fingerprint ?? fingerprint }, deps.now());
  }
  return entry("hub", { fingerprint }, deps.now());
}

// ---------------------------------------------------------------- module cache (one answer per server per tab)

const infos = new Map<string, DirectInfo>();
const entries = new Map<string, DirectEntry>();
const inflight = new Map<string, Promise<void>>();
const timers = new Map<string, ReturnType<typeof setTimeout>>();
const watchers = new Map<string, number>();   // servers some mounted component cares about (refcount)
const listeners = new Set<() => void>();
const notify = () => listeners.forEach((fn) => fn());

let deps: ResolveDeps = {
  fetch: (...a) => fetch(...a),
  mint: (server) => api.directToken(server),
  now: () => Date.now(),
};
/** Tests swap the network out. */
export function setDirectDeps(d: Partial<ResolveDeps>): void { deps = { ...deps, ...d }; }
/** Tests start from an empty cache. */
export function resetDirect(): void {
  infos.clear(); entries.clear(); inflight.clear(); watchers.clear();
  timers.forEach((t) => clearTimeout(t)); timers.clear();
  notify();
}

/** The current answer for a server (undefined = never asked). */
export const directEntry = (server: string): DirectEntry | undefined => entries.get(server);

const sameInfo = (a: DirectInfo | undefined, b: DirectInfo) => JSON.stringify(a ?? null) === JSON.stringify(b);

/** Remember a server's direct addresses (from any card the hub sends); a changed address list re-checks it. */
export function noteServer(s: Pick<Server, "id" | "direct">): void {
  if (!s?.id || !s.direct || sameInfo(infos.get(s.id), s.direct)) return;
  infos.set(s.id, s.direct);
  if (entries.has(s.id) || watchers.get(s.id)) void check(s.id, true);
}
onServerCards((list) => list.forEach(noteServer));

function schedule(server: string, e: DirectEntry): void {
  clearTimeout(timers.get(server));
  let ms = RECHECK_MS;
  if (e.state === "direct" && e.exp) ms = Math.min(ms, Math.max(5_000, (e.exp - REMINT_LEAD_S) * 1000 - deps.now()));
  timers.set(server, setTimeout(() => { timers.delete(server); if (watchers.get(server)) void check(server, true); }, ms));
}

/**
 * Make sure `server` has a fresh answer. `force` re-checks even a recent one. The first check shows "checking";
 * later ones keep the old state until they finish. A server whose card has no direct info is "hub" at once.
 */
export function check(server: string, force = false): Promise<void> {
  const running = inflight.get(server);
  if (running) return running;
  const cur = entries.get(server);
  const info = infos.get(server);
  const fresh = cur && cur.state !== "checking" && deps.now() - cur.checkedAt < RECHECK_MS
    && !(cur.state === "direct" && cur.exp - REMINT_LEAD_S <= deps.now() / 1000);
  if (fresh && !force) return Promise.resolve();
  if (!candidates(info).length) {
    if (cur?.state !== "hub") { entries.set(server, entry("hub", { fingerprint: info?.fingerprint ?? null }, deps.now())); notify(); }
    return Promise.resolve();
  }
  if (!cur) { entries.set(server, entry("checking", {}, deps.now())); notify(); }
  const p = resolve(server, info, deps, cur)
    .catch(() => entry("hub", {}, deps.now()))
    .then((e) => {
      entries.set(server, e);
      inflight.delete(server);
      schedule(server, e);
      notify();
    });
  inflight.set(server, p);
  return p;
}

function watch(server: string): () => void {
  watchers.set(server, (watchers.get(server) ?? 0) + 1);
  return () => {
    const n = (watchers.get(server) ?? 1) - 1;
    if (n > 0) watchers.set(server, n);
    else { watchers.delete(server); clearTimeout(timers.get(server)); timers.delete(server); }
  };
}

// Coming back to the tab (e.g. from accepting the certificate) and the network returning are when the answer most
// likely changed. "cert" re-probes at once; other answers when they're older than FOCUS_RECHECK_MS.
if (typeof window !== "undefined") {
  const recheckAll = (always: boolean) => watchers.forEach((_, server) => {
    const e = entries.get(server);
    if (!e || e.state === "checking") return;
    if (always || e.state === "cert" || deps.now() - e.checkedAt > FOCUS_RECHECK_MS) void check(server, true);
  });
  window.addEventListener("focus", () => recheckAll(false));
  window.addEventListener("online", () => recheckAll(true));
}

// ---------------------------------------------------------------- "don't offer again" (per server, this browser)

const dismissKey = (server: string) => `direct.dismissed.${server}`;
export function certOfferDismissed(server: string): boolean {
  try { return localStorage.getItem(dismissKey(server)) === "1"; } catch { return false; }
}
export function dismissCertOffer(server: string): void {
  try { localStorage.setItem(dismissKey(server), "1"); } catch { /* private mode: offered again next visit */ }
  notify();
}

// ---------------------------------------------------------------- React

const subscribe = (fn: () => void) => { listeners.add(fn); return () => { listeners.delete(fn); }; };

export type UseDirect = {
  state: DirectState;
  base: string | null;
  /** "cert": open the server's probe URL in a new tab so the user can accept its certificate */
  enable: () => void;
  certUrl: string | null;
  fingerprint: string | null;
  /** the user said not to offer the certificate again for this server */
  dismissed: boolean;
  dismiss: () => void;
};

let version = 0;
listeners.add(() => { version++; });
const getVersion = () => version;

/**
 * Watches servers (keeps their answers fresh while mounted) and re-renders when any answer changes. Cards passed
 * here also feed their direct info in (done in the effect: changing module state while rendering would notify other
 * components mid-render).
 */
export function useDirectVersion(servers: (Pick<Server, "id" | "direct"> | string)[]): number {
  const ids = servers.map((s) => (typeof s === "string" ? s : s.id));
  const cards = servers.filter((s): s is Pick<Server, "id" | "direct"> => typeof s !== "string");
  const key = `${ids.join(",")}|${JSON.stringify(cards.map((c) => c.direct ?? null))}`;
  useEffect(() => {
    cards.forEach(noteServer);
    const stops = ids.map(watch);
    ids.forEach((id) => void check(id));
    return () => stops.forEach((s) => s());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  // a counter, bumped on every change, so a hook over many servers re-renders when any of them changes
  return useSyncExternalStore(subscribe, getVersion, getVersion);
}

/** A server's state as the UI should show it right now (before its first check: "checking" when it has addresses). */
export function directStateOf(server: Pick<Server, "id" | "direct"> | string): DirectState {
  const id = typeof server === "string" ? server : server.id;
  const e = entries.get(id);
  if (e) return e.state;
  const info = infos.get(id) ?? (typeof server === "string" ? undefined : server.direct);
  return candidates(info).length ? "checking" : "hub";
}

/** One server's direct state for a chip or a media decision (`server` may be the card or just its id). */
export function useDirect(server: Pick<Server, "id" | "direct"> | string): UseDirect {
  const id = typeof server === "string" ? server : server.id;
  useDirectVersion([server]);
  const e = entries.get(id);
  const state = directStateOf(server);
  const certUrl = e?.certUrl ?? null;
  return {
    state,
    base: e?.state === "direct" ? e.base : null,
    certUrl,
    fingerprint: e?.fingerprint ?? infos.get(id)?.fingerprint ?? null,
    enable: () => { if (certUrl) window.open(`${certUrl}/api/direct/probe`, "_blank", "noopener"); },
    dismissed: certOfferDismissed(id),
    dismiss: () => dismissCertOffer(id),
  };
}
