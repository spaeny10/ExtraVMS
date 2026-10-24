import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type Mod = typeof import("./fullscreen");

/** Listeners by type, capture ones first (as at the target), honouring stopImmediatePropagation. */
function target() {
  const ls: { type: string; fn: (e: any) => void; capture: boolean }[] = [];
  return {
    addEventListener: (type: string, fn: (e: any) => void, opt?: boolean | AddEventListenerOptions) => {
      ls.push({ type, fn, capture: opt === true || (typeof opt === "object" && !!opt?.capture) });
    },
    removeEventListener: (type: string, fn: (e: any) => void, opt?: boolean | EventListenerOptions) => {
      const capture = opt === true || (typeof opt === "object" && !!opt?.capture);
      const i = ls.findIndex((l) => l.type === type && l.fn === fn && l.capture === capture);
      if (i >= 0) ls.splice(i, 1);
    },
    fire(type: string, extra: Record<string, unknown> = {}) {
      let stopped = false;
      const e = { type, ...extra, preventDefault: vi.fn(), stopImmediatePropagation: vi.fn(() => { stopped = true; }) };
      for (const l of [...ls.filter((x) => x.type === type && x.capture), ...ls.filter((x) => x.type === type && !x.capture)]) {
        if (stopped) break;
        l.fn(e);
      }
      return e;
    },
    count: (type: string) => ls.filter((l) => l.type === type).length,
  };
}

/** An element: attributes, a parent, events. */
function element(parent: any = null, extra: Record<string, unknown> = {}) {
  const attrs = new Set<string>();
  return Object.assign(target(), {
    parentElement: parent,
    setAttribute: (n: string) => { attrs.add(n); },
    removeAttribute: (n: string) => { attrs.delete(n); },
    has: (n: string) => attrs.has(n),
    dispatchEvent: () => true,
  }, extra);
}

type PageOpts = {
  touch?: boolean; portrait?: boolean; lock?: "ok" | "fail" | "never" | "none"; unlockThrows?: boolean; noOrientation?: boolean;
  /** what requestFullscreen does: enter, reject, never settle, or the element has none (an iPhone) */
  request?: "enter" | "reject" | "never" | "none" | "webkit"; enabled?: boolean; standalone?: boolean; search?: string;
};

/** A stand-in for the page: document, window (events, history), screen.orientation and matchMedia. */
function page(o: PageOpts = {}) {
  const docEvents = target();
  const rootClasses = new Set<string>();
  const root = element(null, { classList: { add: (c: string) => rootClasses.add(c), remove: (c: string) => rootClasses.delete(c) } });
  const doc = Object.assign(docEvents, {
    documentElement: root,
    fullscreenElement: null as unknown,
    fullscreenEnabled: o.enabled ?? true,
    exitFullscreen: vi.fn(async () => { doc.fullscreenElement = null; docEvents.fire("fullscreenchange"); }),
  });
  const w = target();
  // history: a stack; back() pops and fires popstate a moment later (as browsers do)
  const stack: unknown[] = [null];
  const hist = {
    get state() { return stack[stack.length - 1]; },
    pushState: vi.fn((s: unknown) => { stack.push(s); }),
    back: vi.fn(() => { setTimeout(() => { if (stack.length > 1) stack.pop(); w.fire("popstate"); }, 0); }),
  };
  /** the viewer's back gesture: the browser pops the entry */
  const back = () => { if (stack.length > 1) stack.pop(); return w.fire("popstate"); };
  const orientation = Object.assign(target(), {
    type: o.portrait === false ? "landscape-primary" : "portrait-primary",
    lock: o.lock === "none" ? undefined : vi.fn((_o: string) => (o.lock === "fail" ? Promise.reject(new DOMException("not supported", "NotSupportedError"))
      : o.lock === "never" ? new Promise<void>(() => {}) : Promise.resolve())),
    unlock: vi.fn(() => { if (o.unlockThrows) throw new Error("no"); }),
  });
  vi.stubGlobal("document", doc);
  vi.stubGlobal("window", w);
  vi.stubGlobal("history", hist);
  vi.stubGlobal("location", { search: o.search ?? "" });
  vi.stubGlobal("screen", o.noOrientation ? {} : { orientation });
  vi.stubGlobal("matchMedia", (q: string) => ({
    matches: q.includes("coarse") || q.includes("hover: none") ? !!o.touch
      : q.includes("display-mode: standalone") ? !!o.standalone
      : q.includes("portrait") ? orientation.type.startsWith("portrait") : false,
  }));
  const host = element(element(root));   // the dialog around the player, and its parent
  const req = o.request ?? "enter";
  /** a request that hasn't settled yet ("never"), granted now */
  let grant = () => {};
  const el: any = element(host, req === "none" ? {} : req === "webkit" ? {
    webkitRequestFullscreen: vi.fn(() => { doc.fullscreenElement = el; docEvents.fire("fullscreenchange"); }),
  } : {
    requestFullscreen: vi.fn(() => req === "enter" ? Promise.resolve().then(() => { doc.fullscreenElement = el; docEvents.fire("fullscreenchange"); })
      : req === "reject" ? Promise.reject(new TypeError("Permissions check failed"))
      : new Promise<void>((ok) => { grant = () => { doc.fullscreenElement = el; docEvents.fire("fullscreenchange"); ok(); }; })),
  });
  const stage = element(el);
  /** the viewer leaves the real full screen by the browser's means (its Esc, its back) */
  const leave = () => { doc.fullscreenElement = null; docEvents.fire("fullscreenchange"); };
  return { doc, w, hist, stack, back, orientation, el, host, stage, leave, rootClasses, grant: () => grant() };
}
const wide = { videoWidth: 2592, videoHeight: 1520 } as unknown as HTMLVideoElement;
const tall = { videoWidth: 1080, videoHeight: 1920 } as unknown as HTMLVideoElement;
const flush = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); };

let fs: Mod;
let seen: (import("./fullscreen").FsInfo | null)[];
beforeEach(async () => {
  vi.useFakeTimers();
  vi.resetModules();
  fs = await import("./fullscreen");
  seen = [];
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });
const last = () => seen[seen.length - 1];
const start = (p: ReturnType<typeof page>, picture: unknown = wide, stage: unknown = p.stage) => {
  fs.subscribeFullscreen((i) => seen.push(i));
  return fs.startFullscreen(p.el as HTMLElement, { picture: picture as HTMLVideoElement, stage: stage as HTMLElement });
};

describe("wantsLandscape", () => {
  it("only a touch device held upright with a picture wider than tall", () => {
    const w = { width: 1920, height: 1080 };
    expect(fs.wantsLandscape({ touch: true, portrait: true, picture: w })).toBe(true);
    expect(fs.wantsLandscape({ touch: false, portrait: true, picture: w })).toBe(false);
    expect(fs.wantsLandscape({ touch: true, portrait: false, picture: w })).toBe(false);
    expect(fs.wantsLandscape({ touch: true, portrait: true, picture: { width: 1080, height: 1920 } })).toBe(false);
    expect(fs.wantsLandscape({ touch: true, portrait: true, picture: { width: 0, height: 0 } })).toBe(false);   // unknown
  });
});

describe("pictureSize", () => {
  it("a video's frames, an image's pixels, else its box", () => {
    expect(fs.pictureSize(wide)).toEqual({ width: 2592, height: 1520 });
    expect(fs.pictureSize({ naturalWidth: 960, naturalHeight: 563 } as unknown as HTMLImageElement)).toEqual({ width: 960, height: 563 });
    expect(fs.pictureSize({ videoWidth: 0, videoHeight: 0, clientWidth: 360, clientHeight: 203 } as unknown as HTMLVideoElement)).toEqual({ width: 360, height: 203 });
    expect(fs.pictureSize(null)).toEqual({ width: 0, height: 0 });
  });
});

describe("realPlan", () => {
  it("the element's own full screen where there is one; else an iPhone's video player, but not in the installed app", () => {
    expect(fs.realPlan({ elementApi: true, videoPlayer: true, standalone: false })).toBe("element");
    expect(fs.realPlan({ elementApi: true, videoPlayer: false, standalone: true })).toBe("element");
    expect(fs.realPlan({ elementApi: false, videoPlayer: true, standalone: false })).toBe("video");
    expect(fs.realPlan({ elementApi: false, videoPlayer: true, standalone: true })).toBe("none");
    expect(fs.realPlan({ elementApi: false, videoPlayer: false, standalone: false })).toBe("none");
  });
});

describe("shouldRotate", () => {
  const base = { wants: true, portrait: true, real: "element" as const, lock: "pending" as const, waited: false };
  it("never without a landscape picture on a touch device, held upright", () => {
    expect(fs.shouldRotate({ ...base, wants: false, lock: "refused" })).toBe(false);
    expect(fs.shouldRotate({ ...base, portrait: false, lock: "refused" })).toBe(false);
  });
  it("never once the lock is granted, nor under an iPhone's system player", () => {
    expect(fs.shouldRotate({ ...base, lock: "granted", waited: true })).toBe(false);
    expect(fs.shouldRotate({ ...base, real: "video", lock: "none", waited: true })).toBe(false);
  });
  it("at once when the lock is refused or impossible, or there is no real full screen to lock in", () => {
    expect(fs.shouldRotate({ ...base, lock: "refused" })).toBe(true);
    expect(fs.shouldRotate({ ...base, lock: "unsupported" })).toBe(true);
    for (const real of ["refused", "unsupported", "timeout"] as const) expect(fs.shouldRotate({ ...base, real, lock: "none" })).toBe(true);
  });
  it("a lock (or the full screen) still on its way: only after the wait", () => {
    expect(fs.shouldRotate({ ...base })).toBe(false);
    expect(fs.shouldRotate({ ...base, waited: true })).toBe(true);
    expect(fs.shouldRotate({ ...base, real: "pending", lock: "none" })).toBe(false);
    expect(fs.shouldRotate({ ...base, real: "pending", lock: "none", waited: true })).toBe(true);
  });
});

describe("debugAsked", () => {
  it("?fsdebug=1", () => {
    expect(fs.debugAsked("?fsdebug=1")).toBe(true);
    expect(fs.debugAsked("?view=x&fsdebug=1")).toBe(true);
    expect(fs.debugAsked("?fsdebug=0")).toBe(false);
    expect(fs.debugAsked("")).toBe(false);
  });
});

describe("startFullscreen: pseudo full screen first", () => {
  it("fills the window at once, before the real request answers, and locks the page's scroll", () => {
    const p = page({ request: "never" });
    const info = start(p);
    expect(p.el.has("data-fs-pseudo")).toBe(true);
    expect(p.host.has("data-fs-host")).toBe(true);
    expect(p.host.parentElement.has("data-fs-host")).toBe(true);
    expect(p.rootClasses.has("fs-pseudo-on")).toBe(true);
    expect(p.el.requestFullscreen).toHaveBeenCalledWith({ navigationUI: "hide" });
    expect(info?.real).toBe("pending");
    expect(fs.fullscreenBox()).toBe(p.el);
  });
  it("a request that never settles: timeout, and the picture turned after the lock wait", () => {
    const p = page({ touch: true, request: "never" });
    start(p);
    expect(p.stage.has("data-fs-rotated")).toBe(false);
    vi.advanceTimersByTime(fs.LOCK_WAIT_MS);
    expect(p.stage.has("data-fs-rotated")).toBe(true);
    expect(last()?.rotated).toBe(true);
    vi.advanceTimersByTime(fs.REAL_TIMEOUT_MS);
    expect(last()?.real).toBe("timeout");
  });
  it("a refused request: turned at once, the error kept for the diagnostics", async () => {
    const p = page({ touch: true, request: "reject" });
    start(p);
    await flush();
    expect(last()?.real).toBe("refused");
    expect(last()?.realError).toContain("Permissions check failed");
    expect(p.stage.has("data-fs-rotated")).toBe(true);
    expect(p.orientation.lock).not.toHaveBeenCalled();
  });
  it("no Fullscreen API enabled (an iframe, an installed app): pseudo alone, turned at once", () => {
    const p = page({ touch: true, enabled: false });
    start(p);
    expect(p.el.requestFullscreen).not.toHaveBeenCalled();
    expect(last()?.real).toBe("unsupported");
    expect(p.stage.has("data-fs-rotated")).toBe(true);
  });
  it("no turn on a desktop, a phone already sideways, a portrait picture, or without a stage", async () => {
    for (const [o, pic, withStage] of [[{ touch: false }, wide, true], [{ touch: true, portrait: false }, wide, true], [{ touch: true }, tall, true], [{ touch: true }, wide, false]] as const) {
      vi.resetModules();
      fs = await import("./fullscreen");
      const p = page({ ...o, request: "reject" });
      start(p, pic, withStage ? p.stage : null);
      await flush();
      vi.advanceTimersByTime(fs.REAL_TIMEOUT_MS);
      expect(p.stage.has("data-fs-rotated")).toBe(false);
      expect(last()?.rotated).toBe(false);
      vi.unstubAllGlobals();
    }
  });
});

describe("real full screen and the orientation lock", () => {
  it("entered and the lock granted: landscape by the screen, no CSS turn", async () => {
    const p = page({ touch: true });
    start(p);
    await flush();
    expect(last()?.real).toBe("element");
    expect(p.orientation.lock).toHaveBeenCalledWith("landscape");
    expect(last()?.lock).toBe("granted");
    vi.advanceTimersByTime(fs.LOCK_WAIT_MS);
    expect(p.stage.has("data-fs-rotated")).toBe(false);
  });
  it("entered but the lock refused: turned with CSS at once", async () => {
    const p = page({ touch: true, lock: "fail" });
    start(p);
    await flush();
    expect(last()?.lock).toBe("refused");
    expect(last()?.lockError).toContain("NotSupportedError");
    expect(p.stage.has("data-fs-rotated")).toBe(true);
  });
  it("entered, no lock API: turned at once", async () => {
    const p = page({ touch: true, lock: "none" });
    start(p);
    await flush();
    expect(last()?.lock).toBe("unsupported");
    expect(p.stage.has("data-fs-rotated")).toBe(true);
  });
  it("a lock that takes too long: turned after the wait", async () => {
    const p = page({ touch: true, lock: "never" });
    start(p);
    await flush();
    expect(p.stage.has("data-fs-rotated")).toBe(false);
    vi.advanceTimersByTime(fs.LOCK_WAIT_MS);
    expect(p.stage.has("data-fs-rotated")).toBe(true);
  });
  it("no lock on a desktop, a phone already sideways or a portrait picture", async () => {
    for (const [o, pic] of [[{ touch: false }, wide], [{ touch: true, portrait: false }, wide], [{ touch: true }, tall]] as const) {
      vi.resetModules();
      fs = await import("./fullscreen");
      const p = page(o);
      start(p, pic);
      await flush();
      expect(last()?.real).toBe("element");
      expect(p.orientation.lock).not.toHaveBeenCalled();
      vi.unstubAllGlobals();
    }
  });
  it("the webkit-prefixed request where that is all there is", async () => {
    const p = page({ touch: true, request: "webkit" });
    start(p);
    await flush();
    expect(p.el.webkitRequestFullscreen).toHaveBeenCalledOnce();
    expect(last()?.real).toBe("element");
    expect(p.orientation.lock).toHaveBeenCalledWith("landscape");
  });
  it("a request that answers late, after a timeout: still taken, and the lock asked then", async () => {
    const p = page({ touch: true, request: "never" });
    start(p);
    vi.advanceTimersByTime(fs.REAL_TIMEOUT_MS);
    expect(last()?.real).toBe("timeout");
    expect(p.stage.has("data-fs-rotated")).toBe(true);
    p.grant();
    await flush();
    expect(last()?.real).toBe("element");
    expect(last()?.lock).toBe("granted");
    expect(p.stage.has("data-fs-rotated")).toBe(false);
  });
});

describe("turning the phone", () => {
  it("sideways drops the CSS turn; upright again brings it back", () => {
    const p = page({ touch: true, enabled: false });
    start(p);
    expect(p.stage.has("data-fs-rotated")).toBe(true);
    p.orientation.type = "landscape-primary";
    p.orientation.fire("change");
    expect(p.stage.has("data-fs-rotated")).toBe(false);
    p.orientation.type = "portrait-primary";
    p.w.fire("resize");
    expect(p.stage.has("data-fs-rotated")).toBe(true);
  });
});

describe("iPhone", () => {
  it("no element full screen in Safari: the video's system player, over the pseudo full screen", () => {
    const p = page({ touch: true, request: "none", enabled: false });
    const v: any = Object.assign(element(), { videoWidth: 2592, videoHeight: 1520, webkitEnterFullscreen: vi.fn() });
    start(p, v);
    expect(v.webkitEnterFullscreen).toHaveBeenCalledOnce();
    expect(last()?.real).toBe("video");
    expect(p.el.has("data-fs-pseudo")).toBe(true);
    vi.advanceTimersByTime(fs.LOCK_WAIT_MS);
    expect(p.stage.has("data-fs-rotated")).toBe(false);   // the system player turns with the phone
    v.fire("webkitendfullscreen");                         // its Done: the whole thing ends
    expect(fs.fullscreenBox()).toBeNull();
    expect(p.el.has("data-fs-pseudo")).toBe(false);
  });
  it("the system player refused, or the installed app: pseudo full screen, turned", () => {
    const p = page({ touch: true, request: "none", enabled: false });
    const v: any = Object.assign(element(), { videoWidth: 2592, videoHeight: 1520, webkitEnterFullscreen: vi.fn(() => { throw new DOMException("no", "InvalidStateError"); }) });
    start(p, v);
    expect(last()?.real).toBe("refused");
    expect(p.stage.has("data-fs-rotated")).toBe(true);
  });
  it("the installed app (standalone): no system player at all", () => {
    const p = page({ touch: true, request: "none", enabled: false, standalone: true });
    const v: any = Object.assign(element(), { videoWidth: 2592, videoHeight: 1520, webkitEnterFullscreen: vi.fn() });
    start(p, v);
    expect(v.webkitEnterFullscreen).not.toHaveBeenCalled();
    expect(last()?.display).toBe("standalone");
    expect(last()?.real).toBe("unsupported");
    expect(p.stage.has("data-fs-rotated")).toBe(true);
  });
});

describe("leaving", () => {
  const cleared = (p: ReturnType<typeof page>) => {
    expect(fs.fullscreenBox()).toBeNull();
    expect(p.el.has("data-fs-pseudo")).toBe(false);
    expect(p.host.has("data-fs-host")).toBe(false);
    expect(p.stage.has("data-fs-rotated")).toBe(false);
    expect(p.rootClasses.has("fs-pseudo-on")).toBe(false);
    expect(last()).toBeNull();
  };
  it("pushes one history entry on entering", () => {
    const p = page();
    start(p);
    expect(p.hist.pushState).toHaveBeenCalledOnce();
    expect((p.hist.state as { axiomFs: number }).axiomFs).toBeTypeOf("number");
  });
  it("the exit button: everything undone, out of the real full screen, our history entry taken back quietly", async () => {
    const p = page({ touch: true });
    const page_ = vi.fn();
    p.w.addEventListener("popstate", page_);   // the page's own handler (Find re-reads its URL)
    start(p);
    await flush();
    fs.stopFullscreen(p.el as HTMLElement);
    cleared(p);
    expect(p.doc.exitFullscreen).toHaveBeenCalledOnce();
    expect(p.orientation.unlock).toHaveBeenCalledOnce();
    expect(p.hist.back).not.toHaveBeenCalled();   // not at once: the back gesture's own pop may still be coming
    vi.advanceTimersByTime(fs.BACK_SETTLE_MS);
    expect(p.hist.back).toHaveBeenCalledOnce();
    vi.advanceTimersByTime(1);
    expect(p.stack).toEqual([null]);
    expect(page_).not.toHaveBeenCalled();
  });
  it("the back gesture: leaves full screen, not the page", () => {
    const p = page({ touch: true, enabled: false });
    const page_ = vi.fn();
    p.w.addEventListener("popstate", page_);
    start(p);
    const e = p.back();
    expect(e.stopImmediatePropagation).toHaveBeenCalled();
    expect(page_).not.toHaveBeenCalled();
    cleared(p);
    expect(p.hist.back).not.toHaveBeenCalled();   // the browser already took the entry
    expect(fs.lastEnd).toBe("back");
  });
  it("Esc: leaves full screen, before the event viewer's own Esc closes the viewer", () => {
    const p = page({ request: "never" });
    const viewer = vi.fn();
    p.w.addEventListener("keydown", viewer);
    start(p);
    const e = p.w.fire("keydown", { key: "Escape" });
    expect(e.preventDefault).toHaveBeenCalled();
    expect(viewer).not.toHaveBeenCalled();
    cleared(p);
    expect(fs.lastEnd).toBe("esc");
    p.w.fire("keydown", { key: "Escape" });   // afterwards Esc is the viewer's again
    expect(viewer).toHaveBeenCalledOnce();
  });
  it("other keys pass", () => {
    const p = page({ request: "never" });
    start(p);
    p.w.fire("keydown", { key: "f" });
    expect(fs.fullscreenBox()).toBe(p.el);
  });
  it("the real full screen ending by the browser's means (its Esc, its back): the pseudo one ends too", async () => {
    const p = page({ touch: true });
    start(p);
    await flush();
    p.leave();
    cleared(p);
    expect(fs.lastEnd).toBe("real-exit");
    expect(p.orientation.unlock).toHaveBeenCalledOnce();
    vi.advanceTimersByTime(fs.BACK_SETTLE_MS);
    expect(p.hist.back).toHaveBeenCalledOnce();
  });
  it("Android's back gesture: the real full screen ends first, then the gesture pops our entry: one step back, not two", async () => {
    const p = page({ touch: true });
    const page_ = vi.fn();
    p.w.addEventListener("popstate", page_);   // the page's own handler (Find re-reads its URL and closes the viewer)
    start(p);
    await flush();
    p.leave();                                 // fullscreenchange: the session ends ("real-exit")
    cleared(p);
    const e = p.back();                        // then the gesture's own pop of our entry
    expect(e.stopImmediatePropagation).toHaveBeenCalled();
    expect(page_).not.toHaveBeenCalled();
    vi.advanceTimersByTime(fs.BACK_SETTLE_MS);
    expect(p.hist.back).not.toHaveBeenCalled();   // the entry is already gone: no second step back
    expect(p.stack).toEqual([null]);
  });
  it("a real full screen granted after the viewer already left: left again", async () => {
    const p = page({ request: "never" });
    start(p);
    fs.stopFullscreen();
    expect(p.doc.exitFullscreen).not.toHaveBeenCalled();
    p.grant();
    await flush();
    expect(p.doc.exitFullscreen).toHaveBeenCalledOnce();
    expect(p.doc.fullscreenElement).toBeNull();
  });
  it("an unlock that throws is swallowed", async () => {
    const p = page({ touch: true, unlockThrows: true });
    start(p);
    await flush();
    expect(() => fs.stopFullscreen()).not.toThrow();
    expect(p.orientation.unlock).toHaveBeenCalled();
  });
  it("stopFullscreen for another box leaves this one alone; a second start for the same box is the same session", () => {
    const p = page({ request: "never" });
    const a = start(p);
    expect(fs.startFullscreen(p.el as HTMLElement)?.id).toBe(a?.id);
    fs.stopFullscreen(element() as unknown as HTMLElement);
    expect(fs.fullscreenBox()).toBe(p.el);
  });
});

describe("diagnostics", () => {
  it("?fsdebug=1 starts with the overlay; the long-press event toggles it", () => {
    const p = page({ request: "never", search: "?fsdebug=1" });
    start(p);
    expect(last()?.debug).toBe(true);
    p.el.fire(fs.FS_DEBUG_EVENT);
    expect(last()?.debug).toBe(false);
    p.el.fire(fs.FS_DEBUG_EVENT);
    expect(last()?.debug).toBe(true);
  });
});

describe("onFullscreenChange", () => {
  it("reports the element, and stops when unsubscribed", () => {
    const p = page();
    const got: unknown[] = [];
    const off = fs.onFullscreenChange((el) => got.push(el));
    p.doc.fullscreenElement = p.el;
    p.doc.fire("fullscreenchange");
    p.leave();
    off();
    p.doc.fullscreenElement = p.el;
    p.doc.fire("fullscreenchange");
    expect(got).toEqual([p.el, null]);
  });
});
