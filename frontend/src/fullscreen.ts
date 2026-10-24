/**
 * Full screen for a player box, shared by every full-screen button (the event player, the hub's Site Live view).
 *
 * The browser's Fullscreen API can't be relied on: a phone's installed app (display-mode standalone), an iframe
 * without allowfullscreen, an iPhone (no element full screen) or a request that never settles all leave the page as
 * it was. So a press always does two things, in this order and without waiting:
 *
 *   1. pseudo full screen: the box gets `data-fs-pseudo` (fullscreen.css: fixed over the whole window, above every
 *      dialog, black, page scroll locked). Its ancestors get `data-fs-host`, which drops anything that would trap a
 *      fixed box (transform, filter, contain) and lifts their stacking contexts to the top. A history entry is pushed,
 *      so the Android back gesture leaves full screen instead of the page.
 *   2. the real thing, as a bonus: requestFullscreen (webkit-prefixed where that is all there is), then, on a touch
 *      device held upright with a landscape picture, screen.orientation.lock("landscape"). Where a page element can't
 *      go full screen (an iPhone in Safari) the <video> goes into the system player instead.
 *
 * Landscape without the lock: when the lock hasn't been granted within LOCK_WAIT_MS (or there is no real full screen
 * to lock in), the `stage` (the box's content, or the part that shows the picture) gets `data-fs-rotated`: turned 90°
 * clockwise with CSS so a 16:9 picture fills the long side of a phone held upright. Turning the phone sideways drops
 * the CSS turn again (the picture is then landscape by itself). VideoZoom maps the pointer through the turn.
 *
 * Leaving: the exit button (stopFullscreen), Esc, the back gesture, or the real full screen ending by the browser's
 * own means (its Esc, its back) all end the session the same way.
 *
 * One session at a time; subscribeFullscreen reports its state (FsInfo) to the hook (useFullscreen.tsx), whose
 * diagnostics overlay shows it (?fsdebug=1, or a long-press on the zoom pill: FS_DEBUG_EVENT).
 */

type FsDoc = Document & {
  webkitFullscreenElement?: Element | null;
  webkitExitFullscreen?: () => void | Promise<void>;
  webkitFullscreenEnabled?: boolean;
};
type FsEl = HTMLElement & { webkitRequestFullscreen?: () => void | Promise<void> };
type IosVideo = HTMLVideoElement & { webkitEnterFullscreen?: () => void; webkitExitFullscreen?: () => void };
type Orientation = EventTarget & { type?: string; angle?: number; lock?: (o: string) => Promise<void>; unlock?: () => void };
export type Picture = HTMLVideoElement | HTMLImageElement | { width: number; height: number };

/** how long the orientation lock may take before the picture is turned with CSS instead */
export const LOCK_WAIT_MS = 400;
/** a requestFullscreen still unsettled after this long is taken as refused (it may still arrive) */
export const REAL_TIMEOUT_MS = 1500;
/** bubbled from inside the box (a long-press on the zoom pill): show or hide the diagnostics overlay */
export const FS_DEBUG_EVENT = "axiom-fsdebug";

const doc = (): FsDoc | null => (typeof document === "undefined" ? null : (document as FsDoc));
const win = (): (Window & typeof globalThis) | null => (typeof window === "undefined" ? null : window);
const hist = (): History | null => (typeof history === "undefined" ? null : history);
const orientation = (): Orientation | null =>
  (typeof screen === "undefined" ? null : ((screen as Screen & { orientation?: Orientation }).orientation ?? null));
const media = (q: string): boolean => {
  try { return typeof matchMedia === "function" && matchMedia(q).matches; } catch { return false; }
};

/** The element in full screen now, if any. */
export function fullscreenElement(): Element | null {
  const d = doc();
  return d ? d.fullscreenElement ?? d.webkitFullscreenElement ?? null : null;
}

/** Can a page element go full screen here (not an iPhone, not an iframe without allowfullscreen)? Unknown: yes. */
export function fullscreenSupported(): boolean {
  const d = doc();
  if (!d) return false;
  if (d.fullscreenEnabled === undefined && d.webkitFullscreenEnabled === undefined) return true;
  return !!(d.fullscreenEnabled || d.webkitFullscreenEnabled);
}

/** The picture's width and height: a video's frames, an image's pixels, else its box on the page (0 × 0: unknown). */
export function pictureSize(p: Picture | null | undefined): { width: number; height: number } {
  if (!p) return { width: 0, height: 0 };
  const v = p as HTMLVideoElement, i = p as HTMLImageElement, el = p as HTMLElement;
  if (v.videoWidth && v.videoHeight) return { width: v.videoWidth, height: v.videoHeight };
  if (i.naturalWidth && i.naturalHeight) return { width: i.naturalWidth, height: i.naturalHeight };
  if (el.clientWidth && el.clientHeight) return { width: el.clientWidth, height: el.clientHeight };
  const s = p as { width: number; height: number };
  return typeof s.width === "number" && typeof s.height === "number" ? { width: s.width, height: s.height } : { width: 0, height: 0 };
}

/** Turn the screen to landscape for this picture? Only a touch device held upright, and only for a picture wider than
 *  tall (a desktop's or a tablet-on-a-stand's mouse pointer: never). */
export function wantsLandscape(o: { touch: boolean; portrait: boolean; picture: { width: number; height: number } }): boolean {
  return o.touch && o.portrait && o.picture.width > o.picture.height && o.picture.height > 0;
}

export const isTouch = () => media("(pointer: coarse)") || media("(hover: none)");
/** Is the window taller than wide? The window's own shape (what the full-screen box takes), not the screen's
 *  orientation: the same on a phone, but right for a phone in split screen or a desktop's device emulation. */
export const isPortrait = () => {
  if (typeof matchMedia === "function") return media("(orientation: portrait)");
  if (typeof innerHeight === "number" && typeof innerWidth === "number") return innerHeight > innerWidth;
  const t = orientation()?.type;
  return !!t && t.startsWith("portrait");
};
/** How the page is shown: an installed app (standalone, fullscreen, minimal-ui) or a browser tab. */
export function displayMode(): string {
  for (const m of ["fullscreen", "standalone", "minimal-ui", "window-controls-overlay"]) if (media(`(display-mode: ${m})`)) return m;
  if (typeof navigator !== "undefined" && (navigator as Navigator & { standalone?: boolean }).standalone) return "standalone";
  return "browser";
}

/** Where the real full screen stands. pending: asked, no answer yet · element: the box is in it · video: an iPhone's
 *  system player · refused: the request failed · unsupported: nothing to ask · timeout: no answer in REAL_TIMEOUT_MS */
export type RealState = "pending" | "element" | "video" | "refused" | "unsupported" | "timeout";
/** The orientation lock. none: not wanted (or not yet asked: no real full screen yet) · pending · granted · refused ·
 *  unsupported (no lock API) */
export type LockState = "none" | "pending" | "granted" | "refused" | "unsupported";

/** `?fsdebug=1` in the URL: every full screen starts with the diagnostics overlay. */
export function debugAsked(search = typeof location === "undefined" ? "" : location.search): boolean {
  try { return new URLSearchParams(search).get("fsdebug") === "1"; } catch { return false; }
}

/** Real full screen to ask for: the element's own, an iPhone's video player, or none (pseudo full screen alone). An
 *  installed iPhone app (standalone) gets no system player: pseudo full screen keeps the page's controls and zoom. */
export function realPlan(o: { elementApi: boolean; videoPlayer: boolean; standalone: boolean }): "element" | "video" | "none" {
  if (o.elementApi) return "element";
  if (o.videoPlayer && !o.standalone) return "video";
  return "none";
}

/**
 * Turn the stage 90° with CSS? Only for a landscape picture on a touch device (`wants`) still held upright, and never
 * once the lock is granted (the screen is landscape then). Lock refused or impossible, or no real full screen to lock
 * in: at once. Otherwise the lock is still on its way: after LOCK_WAIT_MS (`waited`).
 */
export function shouldRotate(o: { wants: boolean; portrait: boolean; real: RealState; lock: LockState; waited: boolean }): boolean {
  if (!o.wants || !o.portrait || o.lock === "granted" || o.real === "video") return false;
  if (o.lock === "refused" || o.lock === "unsupported") return true;
  if (o.real !== "pending" && o.real !== "element") return true;
  return o.waited;
}

/** What the diagnostics overlay shows, and what the hook renders from. */
export type FsInfo = {
  id: number;
  box: HTMLElement;
  /** element Fullscreen API present and enabled */
  api: boolean;
  real: RealState;
  realError?: string;
  lock: LockState;
  lockError?: string;
  /** a landscape picture on a touch device: wants landscape */
  wants: boolean;
  portrait: boolean;
  waited: boolean;
  rotated: boolean;
  display: string;
  picture: { width: number; height: number };
  /** the diagnostics overlay is asked for (FS_DEBUG_EVENT) */
  debug: boolean;
};

type Listener = (info: FsInfo | null) => void;
const listeners = new Set<Listener>();
/** Called with the session's state whenever it changes, and null when it ends; returns the unsubscribe. */
export function subscribeFullscreen(fn: Listener): () => void {
  listeners.add(fn);
  fn(active ? active.snapshot() : null);
  return () => { listeners.delete(fn); };
}
const emit = (info: FsInfo | null) => listeners.forEach((fn) => fn(info));

let locked = false;
/** Give the screen back its own orientation (harmless when nothing was locked). */
export function unlockOrientation(): void {
  if (!locked) return;
  locked = false;
  try { orientation()?.unlock?.(); } catch { /* not supported */ }
}

const errText = (e: unknown) => (e instanceof Error || (e && typeof e === "object" && "name" in e)
  ? `${(e as Error).name}: ${(e as Error).message}` : String(e));

// ---- history: one entry per session, so back leaves full screen; our own history.back() is swallowed
let swallowPop = false;
let swallowTimer: ReturnType<typeof setTimeout> | null = null;
/** How long after a session ends its history entry is left alone, for the back gesture's own pop to arrive. */
export const BACK_SETTLE_MS = 350;
let lastEndedId: number | null = null;
let lastEndedAt = 0;
let popInstalled = false;
function installPop() {
  const w = win();
  if (!w || popInstalled) return;
  popInstalled = true;
  // capture, registered early: runs before the pages' own popstate handlers (Find re-reads its URL on popstate)
  w.addEventListener("popstate", (e: PopStateEvent) => {
    if (swallowPop) {
      swallowPop = false;
      lastEndedId = null;   // our own back(): the gesture didn't pop the entry, so nothing more to swallow
      if (swallowTimer) clearTimeout(swallowTimer);
      e.stopImmediatePropagation();
      return;
    }
    const s = active;
    if (s && s.pushed && (hist()?.state as { axiomFs?: number } | null)?.axiomFs !== s.id) {
      e.stopImmediatePropagation();   // same URL as before: nothing for the page to do
      s.end("back");
      return;
    }
    // the back gesture's pop of an entry whose session already ended (the real full screen ended first): the URL is
    // the one the page shows, so the page (Find re-reading its search) must not react to it
    if (lastEndedId != null && Date.now() - lastEndedAt < BACK_SETTLE_MS * 4) {
      lastEndedId = null;
      e.stopImmediatePropagation();
    }
  }, true);
}

let nextId = 1;
let active: Session | null = null;

export type EndReason = "exit" | "esc" | "back" | "real-exit" | "replaced" | "unmount";

class Session {
  readonly id = nextId++;
  readonly box: HTMLElement;
  private stage: HTMLElement | null;
  private video: IosVideo | null;
  private hosts: HTMLElement[] = [];
  pushed = false;
  private ended = false;
  private timers: ReturnType<typeof setTimeout>[] = [];
  private off: (() => void)[] = [];
  private rotatedEl: HTMLElement | null = null;
  private s: Omit<FsInfo, "id" | "box">;

  constructor(box: HTMLElement, o: { picture?: Picture | null; stage?: HTMLElement | null }) {
    this.box = box;
    this.stage = o.stage ?? null;
    const pic = o.picture ?? null;
    this.video = pic && typeof (pic as IosVideo).webkitEnterFullscreen === "function" ? (pic as IosVideo) : null;
    const size = pictureSize(pic);
    const el = box as FsEl;
    const api = !!(el.requestFullscreen || el.webkitRequestFullscreen) && fullscreenSupported();
    const display = displayMode();
    this.s = {
      api, real: "pending", lock: "none", display, picture: size,
      wants: isTouch() && size.width > size.height && size.height > 0,
      portrait: isPortrait(), waited: false, rotated: false, debug: debugAsked(),
    };
  }

  snapshot(): FsInfo { return { id: this.id, box: this.box, ...this.s }; }

  /** Everything a press does, synchronously (the real request needs the press's user activation). */
  start() {
    this.applyPseudo();
    this.listen();
    this.pushHistory();
    const plan = realPlan({ elementApi: this.s.api, videoPlayer: !!this.video, standalone: this.s.display !== "browser" });
    if (plan === "element") this.requestElement();
    else if (plan === "video") this.requestVideo();
    else this.s.real = "unsupported";
    this.timers.push(setTimeout(() => { this.s.waited = true; this.update(); }, LOCK_WAIT_MS));
    this.update();
  }

  private applyPseudo() {
    this.box.setAttribute("data-fs-pseudo", "");
    const root = doc()?.documentElement ?? null;
    for (let p = this.box.parentElement; p && p !== root; p = p.parentElement) {
      p.setAttribute("data-fs-host", "");
      this.hosts.push(p);
    }
    root?.classList.add("fs-pseudo-on");
  }

  private listen() {
    const d = doc(), w = win(), o = orientation();
    const onChange = () => {
      const el = fullscreenElement();
      if (el === this.box) this.realEntered();
      else if (this.s.real === "element" && !el) this.end("real-exit");   // the browser's Esc or back
    };
    if (d) {
      d.addEventListener("fullscreenchange", onChange);
      d.addEventListener("webkitfullscreenchange", onChange);
      this.off.push(() => { d.removeEventListener("fullscreenchange", onChange); d.removeEventListener("webkitfullscreenchange", onChange); });
    }
    const onTurn = () => {
      const p = isPortrait();
      if (p !== this.s.portrait) { this.s.portrait = p; this.update(); }
    };
    if (o?.addEventListener) { o.addEventListener("change", onTurn); this.off.push(() => o.removeEventListener("change", onTurn)); }
    if (w) {
      const onKey = (e: KeyboardEvent) => {
        if (e.key !== "Escape" || this.s.real === "element") return;   // in real full screen the browser has Esc
        e.preventDefault();
        e.stopImmediatePropagation();   // before the event viewer's own Esc closes it
        this.end("esc");
      };
      w.addEventListener("resize", onTurn);
      w.addEventListener("keydown", onKey, true);
      this.off.push(() => { w.removeEventListener("resize", onTurn); w.removeEventListener("keydown", onKey, true); });
    }
    const onDebug = () => { this.s.debug = !this.s.debug; this.update(); };
    this.box.addEventListener?.(FS_DEBUG_EVENT, onDebug);
    this.off.push(() => this.box.removeEventListener?.(FS_DEBUG_EVENT, onDebug));
  }

  private pushHistory() {
    const h = hist();
    if (!h?.pushState) return;
    installPop();
    try {
      const prev = h.state && typeof h.state === "object" ? h.state : {};
      h.pushState({ ...prev, axiomFs: this.id }, "");
      this.pushed = true;
    } catch { /* sandboxed */ }
  }

  private requestElement() {
    const el = this.box as FsEl;
    try {
      const r = el.requestFullscreen ? el.requestFullscreen({ navigationUI: "hide" }) : el.webkitRequestFullscreen!();
      if (r && typeof (r as Promise<void>).then === "function") {
        (r as Promise<void>).then(() => this.realEntered(), (e) => this.realFailed(e));
      } else if (fullscreenElement() === this.box) this.realEntered();   // an old webkit: no promise
    } catch (e) { this.realFailed(e); }
    this.timers.push(setTimeout(() => {
      if (this.s.real === "pending") { this.s.real = "timeout"; this.update(); }
    }, REAL_TIMEOUT_MS));
  }

  private requestVideo() {
    const v = this.video!;
    try {
      v.webkitEnterFullscreen!();
      this.s.real = "video";
      const done = () => this.end("real-exit");
      v.addEventListener?.("webkitendfullscreen", done);
      this.off.push(() => v.removeEventListener?.("webkitendfullscreen", done));
    } catch (e) {
      this.s.real = "refused";
      this.s.realError = errText(e);
    }
  }

  private realEntered() {
    if (this.ended) {   // it came after the viewer had already left: leave it again
      if (fullscreenElement() === this.box) exitDocFullscreen();
      return;
    }
    if (this.s.real === "element") return;
    this.s.real = "element";
    this.s.realError = undefined;
    this.tryLock();
    this.update();
  }

  private realFailed(e: unknown) {
    if (this.ended || this.s.real === "element") return;
    this.s.real = "refused";
    this.s.realError = errText(e);
    this.update();
  }

  private tryLock() {
    if (!this.s.wants || !isPortrait()) return;
    const o = orientation();
    if (!o?.lock) { this.s.lock = "unsupported"; return; }
    this.s.lock = "pending";
    try {
      o.lock("landscape").then(() => {
        locked = true;
        if (this.ended || fullscreenElement() !== this.box) { unlockOrientation(); return; }
        this.s.lock = "granted";
        this.update();
      }, (e) => {
        if (this.ended) return;
        this.s.lock = "refused";
        this.s.lockError = errText(e);
        this.update();
      });
    } catch (e) {
      this.s.lock = "refused";
      this.s.lockError = errText(e);
    }
  }

  private update() {
    if (this.ended) return;
    const rotate = !!this.stage && shouldRotate(this.s);
    if (rotate !== this.s.rotated) {
      this.s.rotated = rotate;
      if (rotate) { this.stage!.setAttribute("data-fs-rotated", ""); this.rotatedEl = this.stage; }
      else { this.rotatedEl?.removeAttribute("data-fs-rotated"); this.rotatedEl = null; }
    }
    emit(this.snapshot());
  }

  end(reason: EndReason) {
    if (this.ended) return;
    this.ended = true;
    if (active === this) active = null;
    this.timers.forEach(clearTimeout);
    this.off.forEach((f) => f());
    this.rotatedEl?.removeAttribute("data-fs-rotated");
    this.box.removeAttribute("data-fs-pseudo");
    this.hosts.forEach((h) => h.removeAttribute("data-fs-host"));
    doc()?.documentElement?.classList.remove("fs-pseudo-on");
    unlockOrientation();
    if (fullscreenElement() === this.box) exitDocFullscreen();
    if (this.s.real === "video" && reason !== "real-exit") { try { this.video?.webkitExitFullscreen?.(); } catch { /* closed */ } }
    // Our history entry: taken back only if it is still on top a moment later. On Android the back gesture both ends
    // the real full screen (fullscreenchange, "real-exit") and pops our entry, in either order: going back at once here
    // stepped back twice, past the page the viewer was opened on (the event list). The gesture's own pop, arriving
    // after this, still ends nothing (the session is gone) and is swallowed by the popstate handler below.
    const h = hist();
    if (this.pushed && reason !== "back") {
      const id = this.id;
      lastEndedId = id;
      lastEndedAt = Date.now();
      setTimeout(() => {
        if ((h?.state as { axiomFs?: number } | null)?.axiomFs !== id) return;   // already gone (the gesture took it)
        swallowPop = true;
        if (swallowTimer) clearTimeout(swallowTimer);
        swallowTimer = setTimeout(() => { swallowPop = false; }, 1000);
        try { h!.back(); } catch { swallowPop = false; }
      }, BACK_SETTLE_MS);
    }
    lastEnd = reason;
    emit(null);
  }
}

/** How the last session ended (tests, diagnostics). */
export let lastEnd: EndReason | null = null;

function exitDocFullscreen() {
  const d = doc();
  if (!d) return;
  try {
    if (d.exitFullscreen) d.exitFullscreen().catch(() => {});
    else void d.webkitExitFullscreen?.();
  } catch { /* already out */ }
}

/**
 * Full screen for `box`: pseudo at once, the real one tried alongside (see the top of this file). `picture` (the
 * video, or the image shown) decides whether a phone held upright wants landscape; `stage` is what turns when the
 * screen can't (none: no turn). Returns the session's first state.
 */
export function startFullscreen(box: HTMLElement | null | undefined, o: { picture?: Picture | null; stage?: HTMLElement | null } = {}): FsInfo | null {
  if (!box) return null;
  if (active?.box === box) return active.snapshot();
  active?.end("replaced");
  const s = new Session(box, o);
  active = s;
  s.start();
  return s.snapshot();
}

/** Leave full screen however it was entered (the exit button). `box`: only if that box is the one in it. */
export function stopFullscreen(box?: HTMLElement | null, reason: EndReason = "exit"): void {
  if (!active || (box && active.box !== box)) return;
  active.end(reason);
}

/** The box in (pseudo or real) full screen now, if any. */
export function fullscreenBox(): HTMLElement | null {
  return active?.box ?? null;
}

/** Call `fn` with the full-screen element whenever the real full screen starts or ends; returns the unsubscribe. */
export function onFullscreenChange(fn: (el: Element | null) => void): () => void {
  const d = doc();
  if (!d) return () => {};
  const on = () => fn(fullscreenElement());
  d.addEventListener("fullscreenchange", on);
  d.addEventListener("webkitfullscreenchange", on);
  return () => { d.removeEventListener("fullscreenchange", on); d.removeEventListener("webkitfullscreenchange", on); };
}
