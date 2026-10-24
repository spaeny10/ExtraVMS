/**
 * Full screen for a player box, shared by every full-screen button (the event player, the hub's Site Live view).
 *
 * enterFullscreen(el, picture): the element's own full screen (webkit-prefixed where that is all there is), then, on a
 * touch device held upright, the screen turned to landscape when the picture is wider than tall: without that a phone
 * shows a camera's 16:9 picture as a thin strip across the middle of a portrait screen. The lock lasts until full screen
 * ends (Esc, the back gesture, the exit button): then the orientation is the device's own again.
 * Where a page element can't go full screen (an iPhone), the <video> alone goes into the system player, which turns
 * with the phone by itself.
 * Every call is best effort: a desktop browser rejects the orientation lock, an old one has no full screen at all.
 */

type FsDoc = Document & {
  webkitFullscreenElement?: Element | null;
  webkitExitFullscreen?: () => void | Promise<void>;
  webkitFullscreenEnabled?: boolean;
};
type FsEl = HTMLElement & { webkitRequestFullscreen?: () => void | Promise<void> };
type IosVideo = HTMLVideoElement & { webkitEnterFullscreen?: () => void };
type Orientation = { type?: string; lock?: (o: string) => Promise<void>; unlock?: () => void };
export type Picture = HTMLVideoElement | HTMLImageElement | { width: number; height: number };

const doc = (): FsDoc | null => (typeof document === "undefined" ? null : (document as FsDoc));
const orientation = (): Orientation | null =>
  (typeof screen === "undefined" ? null : ((screen as Screen & { orientation?: Orientation }).orientation ?? null));
const media = (q: string): boolean => typeof matchMedia === "function" && matchMedia(q).matches;

/** The element in full screen now, if any. */
export function fullscreenElement(): Element | null {
  const d = doc();
  return d ? d.fullscreenElement ?? d.webkitFullscreenElement ?? null : null;
}

/** Can a page element go full screen here (not an iPhone)? */
export function fullscreenSupported(): boolean {
  const d = doc();
  return !!d && !!(d.fullscreenEnabled || d.webkitFullscreenEnabled);
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

const isTouch = () => media("(pointer: coarse)") || media("(hover: none)");
const isPortrait = () => {
  const t = orientation()?.type;
  if (t) return t.startsWith("portrait");
  if (typeof matchMedia === "function") return media("(orientation: portrait)");
  return typeof innerHeight === "number" && typeof innerWidth === "number" && innerHeight > innerWidth;
};

let locked = false;
let watching = false;

/** Give the screen back its own orientation (after full screen; harmless when nothing was locked). */
export function unlockOrientation(): void {
  if (!locked) return;
  locked = false;
  try { orientation()?.unlock?.(); } catch { /* not supported */ }
}

/** Unlock once full screen has ended, however it ended (Esc, back, the browser's own control). */
function watchExit() {
  const d = doc();
  if (!d || watching) return;
  watching = true;
  const on = () => {
    if (fullscreenElement()) return;
    d.removeEventListener("fullscreenchange", on);
    d.removeEventListener("webkitfullscreenchange", on);
    watching = false;
    unlockOrientation();
  };
  d.addEventListener("fullscreenchange", on);
  d.addEventListener("webkitfullscreenchange", on);
}

/**
 * Put `el` in full screen; `picture` (the video, or the image shown) decides whether a phone held upright turns to
 * landscape. Without element full screen (an iPhone) a <video> picture goes into the system player instead.
 * Resolves to what happened: "element", "video" (the iPhone's player) or "none".
 */
export async function enterFullscreen(el: HTMLElement | null | undefined, picture?: Picture | null): Promise<"element" | "video" | "none"> {
  const box = el as FsEl | null | undefined;
  let entered = false;
  try {
    if (box?.requestFullscreen) { await box.requestFullscreen({ navigationUI: "hide" }); entered = true; }
    else if (box?.webkitRequestFullscreen) { await box.webkitRequestFullscreen(); entered = true; }
  } catch { /* refused (no user gesture, an iframe without allowfullscreen) */ }
  if (!entered) {
    const v = picture as IosVideo | null | undefined;
    if (v && typeof v.webkitEnterFullscreen === "function") {
      try { v.webkitEnterFullscreen(); return "video"; } catch { return "none"; }
    }
    return "none";
  }
  const o = orientation();
  if (o?.lock && wantsLandscape({ touch: isTouch(), portrait: isPortrait(), picture: pictureSize(picture) })) {
    try {
      watchExit();
      await o.lock("landscape");
      locked = true;
      // full screen may have ended while the lock was being granted
      if (!fullscreenElement()) unlockOrientation();
    } catch { /* a desktop browser, or the lock refused */ }
  }
  return "element";
}

/** Leave full screen (and the landscape lock). */
export function exitFullscreen(): void {
  unlockOrientation();
  const d = doc();
  if (!d || !fullscreenElement()) return;
  try {
    if (d.exitFullscreen) d.exitFullscreen().catch(() => {});
    else void d.webkitExitFullscreen?.();
  } catch { /* already out */ }
}

/** Call `fn` with the full-screen element whenever full screen starts or ends (both event names); returns the unsubscribe. */
export function onFullscreenChange(fn: (el: Element | null) => void): () => void {
  const d = doc();
  if (!d) return () => {};
  const on = () => fn(fullscreenElement());
  d.addEventListener("fullscreenchange", on);
  d.addEventListener("webkitfullscreenchange", on);
  return () => { d.removeEventListener("fullscreenchange", on); d.removeEventListener("webkitfullscreenchange", on); };
}
