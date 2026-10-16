import { afterEach, describe, expect, it, vi } from "vitest";
import type { DirectInfo, DirectToken } from "./api";
import { REMINT_LEAD_S, candidates, certOfferPending, check, classifyProbeError, decide, directEntry, handshake, isLanName, isLoopback, isPrivateIPv4,
  lanNameUrl, lanSuffix, noteServer, probe, resetDirect, resolve,
  setDirectDeps, type DirectEntry, type ResolveDeps } from "./direct";
import { hybridApi, mediaApi, siteApi } from "./hubSource";

const info = (urls: { url: string; local?: boolean }[], available = true): DirectInfo => ({ available, urls, fingerprint: "AB:CD" });
const LAN = "https://192.168.1.10:8443";
const LOCAL = "http://localhost:8080";

/** A fake fetch: url prefix → response status, or an error to throw (TypeError = cert/refused, "abort" = timeout). */
function fakeFetch(routes: Record<string, number | "type" | "abort" | { json: unknown }>) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const hit = Object.entries(routes).find(([k]) => url.startsWith(k));
    if (!hit) throw new TypeError("Failed to fetch");
    const r = hit[1];
    if (r === "type") throw new TypeError("Failed to fetch");
    if (r === "abort") {
      // behave like a hung connection: reject only when the caller's timeout aborts
      return new Promise<Response>((_, rej) => init?.signal?.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError"))));
    }
    if (typeof r === "number") return new Response(r === 204 ? null : "x", { status: r });
    return new Response(JSON.stringify(r.json), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as unknown as typeof fetch;
}
const token = (over: Partial<DirectToken> = {}): DirectToken => ({ token: "tok1", exp: 10_000, role: "viewer", available: true, urls: [], fingerprint: "AB:CD", ...over });
const deps = (f: typeof fetch, mint = vi.fn(async () => token()), now = 1_000_000): ResolveDeps => ({ fetch: f, mint, now: () => now, probeTimeoutMs: 20 });

describe("candidates", () => {
  it("puts local URLs first, keeps the server's order, drops duplicates and trailing slashes", () => {
    expect(candidates(info([{ url: `${LAN}/` }, { url: "https://10.0.0.5:8443" }, { url: LOCAL, local: true }, { url: LAN }]), "https:"))
      .toEqual([LOCAL, LAN, "https://10.0.0.5:8443"]);
  });
  it("drops plain-http LAN URLs on an https page (mixed content) but keeps loopback", () => {
    expect(candidates(info([{ url: "http://192.168.1.10:8080" }, { url: LOCAL, local: true }]), "https:")).toEqual([LOCAL]);
    expect(candidates(info([{ url: "http://192.168.1.10:8080" }]), "http:")).toEqual(["http://192.168.1.10:8080"]);
  });
  it("is empty when the server reports nothing usable", () => {
    expect(candidates(undefined)).toEqual([]);
    expect(candidates(info([{ url: LAN }], false))).toEqual([]);
    expect(candidates(info([{ url: "javascript:alert(1)" }]))).toEqual([]);
  });
  it("accepts the heartbeat's bare-string LAN URLs alongside {url, local} objects", () => {
    const mixed = { available: true, urls: [LAN, { url: LOCAL, local: true }], fingerprint: null } as unknown as DirectInfo;
    expect(candidates(mixed, "https:")).toEqual([LOCAL, LAN]);
  });
  it("knows loopback hosts", () => {
    expect(isLoopback("http://localhost:8080")).toBe(true);
    expect(isLoopback("http://127.0.0.1:8080")).toBe(true);
    expect(isLoopback("http://nvr.localhost")).toBe(true);
    expect(isLoopback(LAN)).toBe(false);
  });
});

describe("probe classification", () => {
  it("a timeout is unreachable; a fast TypeError on an https LAN URL may be the certificate", () => {
    expect(classifyProbeError(LAN, new DOMException("x", "AbortError"))).toBe("unreachable");
    expect(classifyProbeError(LAN, new TypeError("Failed to fetch"))).toBe("maybe-cert");
    expect(classifyProbeError(LOCAL, new TypeError("Failed to fetch"))).toBe("unreachable");
    expect(classifyProbeError("https://localhost:8443", new TypeError("x"))).toBe("unreachable");
  });
  it("probes <url>/api/direct/probe with CORS and maps the answer", async () => {
    const f = fakeFetch({ [`${LAN}/api/direct/probe`]: 204 });
    expect(await probe(LAN, f)).toBe("ok");
    expect((f as unknown as ReturnType<typeof vi.fn>).mock.calls[0][1]).toMatchObject({ mode: "cors" });
    expect(await probe(LAN, fakeFetch({ [LAN]: 404 }))).toBe("unreachable");
    expect(await probe(LAN, fakeFetch({ [LAN]: "type" }))).toBe("maybe-cert");
    expect(await probe(LAN, fakeFetch({ [LAN]: "abort" }), 20)).toBe("unreachable");
  });
  it("decide: the first answering URL in order wins; else the first maybe-cert one is offered", () => {
    expect(decide(["a", "b"], ["unreachable", "ok"])).toEqual({ ok: "b", cert: null });
    expect(decide(["a", "b"], ["maybe-cert", "unreachable"])).toEqual({ ok: null, cert: "a" });
    expect(decide(["a"], ["unreachable"])).toEqual({ ok: null, cert: null });
  });
  it("handshake sends the token with credentials and needs ok:true", async () => {
    const f = fakeFetch({ [`${LAN}/api/direct/handshake`]: { json: { ok: true, user: "u", role: "viewer", exp: 9 } } });
    expect(await handshake(LAN, "a b", f)).toMatchObject({ ok: true });
    const [url, init] = (f as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe(`${LAN}/api/direct/handshake?token=a%20b&direct=a%20b`);
    expect(init).toMatchObject({ credentials: "include", mode: "cors" });
    expect(await handshake(LAN, "t", fakeFetch({ [LAN]: { json: { ok: false } } }))).toBeNull();
    expect(await handshake(LAN, "t", fakeFetch({ [LAN]: 401 }))).toBeNull();
  });
});

describe("resolve", () => {
  const both = info([{ url: LOCAL, local: true }, { url: LAN }]);
  it("goes direct on the first answering candidate after minting and a handshake", async () => {
    const f = fakeFetch({ [`${LAN}/api/direct/probe`]: 204, [`${LAN}/api/direct/handshake`]: { json: { ok: true } } });
    const d = deps(f);
    const e = await resolve("srv", both, d, null, "https:");
    expect(e).toMatchObject({ state: "direct", base: LAN, token: "tok1", exp: 10_000 });
    expect(d.mint).toHaveBeenCalledWith("srv");
  });
  it("stays on the hub without minting when nothing answers (a browser off the LAN)", async () => {
    const d = deps(fakeFetch({ [LAN]: "abort", [LOCAL]: "abort" }));
    const e = await resolve("srv", both, { ...d, fetch: fakeFetch({}) }, null, "https:");
    expect(e.state).toBe("cert");   // a fast TypeError on the https LAN URL
    const off = await resolve("srv", info([{ url: LOCAL, local: true }]), d, null, "https:");
    expect(off.state).toBe("hub");
    expect(d.mint).not.toHaveBeenCalled();
  });
  it("offers the certificate URL when the https LAN URL fails fast", async () => {
    const e = await resolve("srv", both, deps(fakeFetch({ [LAN]: "type" })), null, "https:");
    expect(e).toMatchObject({ state: "cert", certUrl: LAN, fingerprint: "AB:CD" });
  });
  it("falls back to the hub when minting or the handshake fails", async () => {
    const f = fakeFetch({ [`${LAN}/api/direct/probe`]: 204, [`${LAN}/api/direct/handshake`]: 403 });
    expect((await resolve("srv", both, deps(f), null, "https:")).state).toBe("hub");
    const noMint = deps(fakeFetch({ [`${LAN}/api/direct/probe`]: 204 }), vi.fn(async () => { throw new Error("429"); }));
    expect((await resolve("srv", both, noMint, null, "https:")).state).toBe("hub");
  });
  it("reuses a token with time left (no mint) and re-mints near expiry", async () => {
    const f = fakeFetch({ [`${LAN}/api/direct/probe`]: 204, [`${LAN}/api/direct/handshake`]: { json: { ok: true } } });
    const prev: DirectEntry = { state: "direct", base: LAN, token: "old", exp: 2000, certUrl: null, fingerprint: null, checkedAt: 0 };
    const d = deps(f, vi.fn(async () => token({ token: "new", exp: 3000 })), 1_000_000);   // now = 1000 s
    expect((await resolve("srv", both, d, prev, "https:")).token).toBe("old");
    expect(d.mint).not.toHaveBeenCalled();
    const late = deps(f, vi.fn(async () => token({ token: "new", exp: 3000 })), (2000 - REMINT_LEAD_S + 1) * 1000);
    expect((await resolve("srv", both, late, prev, "https:")).token).toBe("new");
  });
});

describe("module cache and mediaApi", () => {
  afterEach(() => resetDirect());
  it("mediaApi is the hub client until the server is direct, then swaps only media URLs", async () => {
    const f = fakeFetch({ [`${LAN}/api/direct/probe`]: 204, [`${LAN}/api/direct/handshake`]: { json: { ok: true } } });
    setDirectDeps({ fetch: f, mint: vi.fn(async () => token({ token: "T1", exp: Date.now() / 1000 + 900 })), now: () => Date.now() });
    expect(mediaApi("srv")).toBe(siteApi("srv"));
    noteServer({ id: "srv", direct: info([{ url: LAN }]) });
    await check("srv");
    expect(directEntry("srv")?.state).toBe("direct");
    const m = mediaApi("srv");
    expect(m).toBe(mediaApi("srv"));   // stable identity: tiles don't reconnect on every render
    expect(m.playbackUrl("cam1", 100, 20, "sd")).toBe(`${LAN}/api/playback/cam1?start=100&duration=20&q=sd&direct=T1`);
    expect(m.media({ id: 7 }, "clip.mp4")).toBe(`${LAN}/api/events/7/media/clip.mp4?direct=T1`);
    expect(m.whepUrl("cam1_sub")).toBe(`${LAN}/api/whep/cam1_sub?direct=T1`);
    expect(m.base).toBe("/s/srv");   // REST (and every write) still goes through the hub
    expect(m.lockEvent).toBe(siteApi("srv").lockEvent);
  });
  it("a server without direct info is the hub at once", async () => {
    await check("other");
    expect(directEntry("other")?.state).toBe("hub");
  });
  it("hybridApi reads the token at call time", () => {
    let t = "a";
    const c = hybridApi(siteApi("x"), LAN, () => t);
    expect(c.frameUrl("c", 1, 320)).toContain("direct=a");
    t = "b";
    expect(c.frameUrl("c", 1, 320)).toContain("direct=b");
  });
});

describe("trusted LAN names (servers with a Let's Encrypt certificate)", () => {
  const SUFFIX = "s-d6d429b617ca.lan.axiomvision.ai";
  const le = (urls: { url: string; local?: boolean }[], over: Partial<DirectInfo> = {}): DirectInfo =>
    ({ ...info(urls), cert: "le", lan_name_suffix: SUFFIX, not_after: 2_000_000_000, ...over });
  const NAMED = `https://192-168-1-10.${SUFFIX}:8443`;

  it("knows the private ranges the hub's DNS answers for", () => {
    for (const ip of ["10.0.0.1", "172.16.0.1", "172.31.255.255", "192.168.1.5", "100.64.0.1", "100.127.1.1"]) expect(isPrivateIPv4(ip)).toBe(true);
    for (const ip of ["8.8.8.8", "172.32.0.1", "100.128.0.1", "127.0.0.1", "169.254.1.1", "192.169.1.1", "300.1.1.1", "fe80::1", "nvr.local"])
      expect(isPrivateIPv4(ip)).toBe(false);
  });
  it("turns each private https address into its name, keeping the port; local and other URLs stay as they are", () => {
    const got = candidates(le([{ url: LOCAL, local: true }, { url: LAN }, { url: "https://10.0.0.5:8443/" }, { url: "https://203.0.113.9:8443" },
      { url: "http://192.168.1.10:8080" }]), "https:");
    expect(got).toEqual([LOCAL, NAMED, `https://10-0-0-5.${SUFFIX}:8443`, "https://203.0.113.9:8443"]);
    expect(lanNameUrl("https://192.168.1.10", SUFFIX)).toBe(`https://192-168-1-10.${SUFFIX}`);
    expect(lanNameUrl("https://8.8.8.8:8443", SUFFIX)).toBeNull();
  });
  it("servers without a trusted certificate keep the bare addresses exactly as before", () => {
    const urls = [{ url: LOCAL, local: true }, { url: LAN }];
    const before = candidates(info(urls), "https:");
    expect(candidates(le(urls, { cert: "self" }), "https:")).toEqual(before);
    expect(candidates(le(urls, { lan_name_suffix: null }), "https:")).toEqual(before);
    expect(candidates(le(urls, { lan_name_suffix: "evil.example/path" }), "https:")).toEqual(before);   // not a DNS name
    expect(candidates(le(urls, { cert: undefined, lan_name_suffix: undefined }), "https:")).toEqual(before);   // an older hub
    expect(lanSuffix(le(urls, { lan_name_suffix: SUFFIX.toUpperCase() }))).toBe(SUFFIX);
  });
  it("goes direct through the name with the same probe, mint and handshake", async () => {
    const f = fakeFetch({ [`${NAMED}/api/direct/probe`]: 204, [`${NAMED}/api/direct/handshake`]: { json: { ok: true } } });
    const d = deps(f);
    const e = await resolve("srv", le([{ url: LAN }]), d, null, "https:");
    expect(e).toMatchObject({ state: "direct", base: NAMED, token: "tok1" });
    expect(d.mint).toHaveBeenCalledWith("srv");
    expect(String((f as unknown as { mock: { calls: unknown[][] } }).mock.calls[0][0])).toBe(`${NAMED}/api/direct/probe`);
  });
  it("never offers a certificate for a name: a failed name (rebind protection) means the hub", async () => {
    const d = deps(fakeFetch({ [NAMED]: "type" }));
    const e = await resolve("srv", le([{ url: LAN }]), d, null, "https:");
    expect(e).toMatchObject({ state: "hub", certUrl: null });
    expect(d.mint).not.toHaveBeenCalled();
    // a self-signed server in the same situation still gets the offer
    expect((await resolve("srv", info([{ url: LAN }]), deps(fakeFetch({ [LAN]: "type" })), null, "https:")).state).toBe("cert");
    expect(isLanName(NAMED, SUFFIX) && !isLanName(LAN, SUFFIX) && !isLanName(NAMED, null)).toBe(true);
  });
  it("certOfferPending counts servers still waiting for an accepted certificate", async () => {
    resetDirect();
    setDirectDeps({ fetch: fakeFetch({ [LAN]: "type", [NAMED]: "type" }), now: () => 1_000_000, probeTimeoutMs: 20 });
    noteServer({ id: "self1", direct: info([{ url: LAN }]) });
    noteServer({ id: "le1", direct: le([{ url: LAN }]) });
    await Promise.all([check("self1"), check("le1")]);
    expect(certOfferPending("self1")).toBe(true);
    expect(certOfferPending("le1")).toBe(false);
    resetDirect();
  });
});
