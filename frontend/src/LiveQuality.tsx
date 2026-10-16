/**
 * SD / HD for one live tile (Views.tsx LiveTile: the server's Live view and the hub's Site Live), and its control.
 *
 * Mode "auto" (the default: no quality stored for the tile) lets autoQuality.ts pick from the tile's width, Expand,
 * the link's onStats samples and the page's HD slots (dashboard/budget.tsx MAX_HD); "sd" / "hd" are the viewer's own
 * pick (a click on the tile or All SD / All HD, or a quality saved in a layout) and win until changed back to Auto.
 *
 * Before connecting, the server's codec list (GET /api/live/codecs, cached per server) and this browser's decoders
 * (codecs.ts) rule out what can't play: HD greyed out with the reason when its path is H.265 here, and an SD path
 * that is H.265 shows a note instead of a black tile. The player's own verdict (onUnsupported) still counts, per
 * camera, for what the list didn't know. A switch that fails (LivePlayer onSwitchFailed) leaves the old quality on
 * screen; Auto then waits out its cooldown, a manual pick shows the quality that plays until the next click.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { SiteApi } from "./api";
import { type Quality, type TimedSample, decideQuality, pushSample } from "./autoQuality";
import { livePath } from "./cameraStreams";
import { HD_FAILED_TITLE, HD_H265_TITLE, type LiveCodecs, SD_H265_TEXT, canPlayLive, codecCache, pathCodec } from "./codecs";
import { MAX_HD, useBudget } from "./dashboard/budget";
import type { WhepStatsSample } from "./whepTiming";
import { MODE_LABEL, QUALITY_MODES, qualityBadge } from "./qualityBadge";
import { menuKeys, useDismiss } from "./ui";

export type QualityMode = "auto" | Quality;

/** A tile whose SD stream the browser failed to decode, its codec not known to be H.265. */
export const SD_FAILED_TEXT = "This browser couldn't decode this camera's SD stream. If its sub stream is H.265, set it to H.264 in Settings › Cameras › Camera.";

const codecs = codecCache();
const CODECS_REFRESH_MS = 60_000;   // an on-demand path's codec is a guess until it is first pulled

/** The server's live codec list (one request per server per 30 s, shared by its tiles); null until it answers. A
 *  server that can't answer (an older one) keeps whatever was known: everything is tried, as before. */
export function useLiveCodecs(site: SiteApi | null): LiveCodecs | null {
  const [list, setList] = useState<LiveCodecs | null>(() => (site && codecs.last(site.base)) ?? null);
  useEffect(() => {
    if (!site) return;
    let alive = true;
    const pull = () => codecs.get(site.base, () => site.liveCodecs()).then((v) => { if (alive && v) setList(v); });
    pull();
    const t = setInterval(pull, CODECS_REFRESH_MS);
    return () => { alive = false; clearInterval(t); };
  }, [site]);
  return list;
}

/** An element's width in CSS px, kept current (0 until laid out). */
export function useElementWidth(ref: React.RefObject<HTMLElement | null>): number {
  const [w, setW] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setW(el.clientWidth);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([e]) => setW(Math.round(e.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return w;
}

export type LiveQuality = {
  /** what the tile plays now */
  hd: boolean;
  /** why HD can't be picked here (the HD button's title), or null */
  hdBlocked: string | null;
  /** SD is what plays and this browser can't decode it: show this instead of connecting */
  sdNote: string | null;
  /** the note came from the player, not the codec list: it may be worth another try */
  sdRetry: (() => void) | null;
  /** for LivePlayer */
  onStats: (s: WhepStatsSample) => void;
  onSwitchFailed: (path: string) => void;
  onUnsupported: (path: string) => void;
  /** a quality button was clicked: forget a failed switch */
  clicked: () => void;
};

export function useLiveQuality({ cam, site, mode, focused, tileKey, box, hold = false }: {
  cam: { id: string; record_stream?: string | null }; site: SiteApi; mode: QualityMode; focused: boolean;
  /** Auto doesn't switch meanwhile (painting a region, steering PTZ: a switch would remount their overlay) */
  hold?: boolean;
  /** the tile's key in the page's budget (the hub's "server/camera"; the camera id on the server) */
  tileKey: string;
  box: React.RefObject<HTMLElement | null>;
}): LiveQuality {
  const list = useLiveCodecs(site);
  const width = useElementWidth(box);
  const { hdFree, holdHd } = useBudget();
  const sdPath = livePath(cam, false), hdPath = livePath(cam, true);
  // paths the player found this browser can't decode (the tile is keyed by camera: this is per camera)
  const [bad, setBad] = useState<string[]>([]);
  const sdCodec = pathCodec(list, sdPath);
  const hdCodecOk = canPlayLive(pathCodec(list, hdPath));
  const hdOk = hdCodecOk && !bad.includes(hdPath);
  // the player's no-frames check can also trip on a slow first keyframe: an SD path known to be H.264 isn't ruled out
  const sdOk = canPlayLive(sdCodec) && !(bad.includes(sdPath) && sdCodec !== "H264");

  // manual: a switch that failed (LivePlayer onSwitchFailed) shows the quality that plays until the next click
  const [failed, setFailed] = useState<string | null>(null);
  // auto: the quality picked, and what the decision needs between samples
  const [autoQ, setAutoQ] = useState<Quality>("sd");
  const autoRef = useRef({ q: "sd" as Quality, samples: [] as TimedSample[], lastSwitch: null as number | null, lastDrop: null as number | null });
  const shown = useRef(false);   // hd, as last rendered: Auto starts from it

  const hd = mode === "auto" ? autoQ === "hd" && hdOk
    : mode === "hd" ? hdOk && failed !== hdPath
    : failed === sdPath && hdOk;
  shown.current = hd;

  const setAuto = useCallback((q: Quality, now: number, drop: boolean) => {
    const a = autoRef.current;
    a.q = q; a.samples = []; a.lastSwitch = now;
    if (drop) a.lastDrop = now;
    setAutoQ(q);
  }, []);
  // switching to Auto starts from what plays (no switch, and none for MIN_GAP_MS); a manual pick forgets failures
  const firstMode = useRef(true);
  useEffect(() => {
    setFailed(null);
    if (firstMode.current) { firstMode.current = false; return; }
    if (mode === "auto") setAuto(shown.current ? "hd" : "sd", performance.now(), false);
  }, [mode, setAuto]);

  const inputs = useRef({ width, focused, hdOk, sdOk, slot: true, mode, hold });
  inputs.current = { width, focused, hdOk, sdOk, slot: hdFree(tileKey), mode, hold };
  const evaluate = useCallback(() => {
    const i = inputs.current, a = autoRef.current;
    if (i.mode !== "auto" || i.hold) return;
    const now = performance.now();
    const d = decideQuality({ width: i.width, focused: i.focused, hdDecodable: i.hdOk, sdDecodable: i.sdOk, hdSlotFree: i.slot, samples: a.samples,
      now, current: a.q, lastSwitch: a.lastSwitch, lastDrop: a.lastDrop });
    if (d) setAuto(d.quality, now, d.badLink);
  }, [setAuto]);
  useEffect(evaluate, [evaluate, width, focused, hdOk, sdOk, mode, hold]);

  // an HD stream holds one of the page's HD slots (manual ones too: they cost the same), freed when it stops
  useEffect(() => { holdHd(tileKey, hd); }, [holdHd, tileKey, hd]);
  useEffect(() => () => holdHd(tileKey, false), [holdHd, tileKey]);

  const onUnsupported = useCallback((path: string) => setBad((b) => (b.includes(path) ? b : [...b, path])), []);
  const onSwitchFailed = useCallback((path: string) => {
    if (inputs.current.mode === "auto") setAuto(path === hdPath ? "sd" : "hd", performance.now(), path === hdPath);
    else setFailed(path);
  }, [hdPath, setAuto]);
  const onStats = useCallback((s: WhepStatsSample) => {
    autoRef.current.samples = pushSample(autoRef.current.samples, s, performance.now());
    evaluate();
  }, [evaluate]);

  const playerSaidSd = bad.includes(sdPath) && sdCodec !== "H265";
  return {
    hd,
    hdBlocked: !hdCodecOk ? HD_H265_TITLE : !hdOk ? HD_FAILED_TITLE : null,
    sdNote: hd || sdOk ? null : playerSaidSd ? SD_FAILED_TEXT : SD_H265_TEXT,
    sdRetry: !hd && !sdOk && playerSaidSd ? () => setBad((b) => b.filter((p) => p !== sdPath)) : null,
    onStats, onSwitchFailed, onUnsupported,
    clicked: () => setFailed(null),
  };
}

/**
 * The tile's Auto · SD · HD control. The viewer's own pick is filled; under Auto the quality it picked is underlined.
 * `live` false (a paused tile): only the mode shows.
 */
export function QualityButtons({ mode, hd, hdBlocked = null, onPick, live = true }: {
  mode: QualityMode; hd: boolean; hdBlocked?: string | null; onPick: (m: QualityMode) => void; live?: boolean;
}) {
  const auto = mode === "auto";
  const pick = (q: Quality) => (auto ? (live && hd === (q === "hd") ? "auto-pick" : "") : hd === (q === "hd") ? "active" : "");
  // the viewer's HD didn't start (no frame in time): SD plays, HD can be tried again
  const stalled = live && mode === "hd" && !hd && !hdBlocked;
  return (
    <div className="segmented small-seg" title={hdBlocked ?? "Stream quality for this camera"}>
      <button className={auto ? "active" : ""} aria-pressed={auto} onClick={() => onPick("auto")}
        title={`Automatic: HD while the tile is large and the link is clean (at most ${MAX_HD} at once), else SD${auto && live ? ` · now ${hd ? "HD" : "SD"}` : ""}`}>Auto</button>
      <button className={pick("sd")} aria-pressed={mode === "sd"} onClick={() => onPick("sd")} title="Sub stream: low bandwidth">SD</button>
      <button className={pick("hd")} aria-pressed={mode === "hd"} disabled={!!hdBlocked} onClick={() => onPick("hd")} title={hdBlocked ?? (stalled ? "HD didn't start, so SD plays: click to try again" : "Full-resolution main stream")}>HD</button>
    </div>
  );
}

/**
 * The same choice as one small badge (the hub's Site Live tiles): it shows the mode and, live under Auto, what plays
 * ("Auto · SD"); a click opens a tiny Auto / SD / HD menu (Escape or a click outside closes it, arrows move).
 * HD this browser can't play is greyed out with the reason, as on QualityButtons.
 */
export function QualityBadge({ mode, hd, hdBlocked = null, onPick, live = true }: {
  mode: QualityMode; hd: boolean; hdBlocked?: string | null; onPick: (m: QualityMode) => void; live?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLSpanElement>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  useDismiss(box, open, () => setOpen(false), btn);
  // the picked item takes the focus, so the arrows start from it
  useEffect(() => { if (open) menu.current?.querySelector<HTMLElement>("[aria-checked=true]:not(:disabled), [role=menuitemradio]:not(:disabled)")?.focus(); }, [open]);
  const b = qualityBadge(mode, hd, live, hdBlocked);
  const choose = (m: QualityMode) => { setOpen(false); onPick(m); btn.current?.focus(); };
  return (
    <span ref={box} className={`qbadge-box ${open ? "open" : ""}`}>
      <button ref={btn} type="button" className={`qbadge ${mode === "auto" ? "" : "picked"} ${b.stalled ? "stalled" : ""}`} aria-haspopup="menu" aria-expanded={open}
        aria-label={b.label} title={b.title} onClick={() => setOpen((o) => !o)}>{b.text}</button>
      {open && (
        <div ref={menu} className="qbadge-menu" role="menu" aria-label="Stream quality" onKeyDown={menuKeys}>
          {QUALITY_MODES.map((m) => (
            <button key={m} type="button" role="menuitemradio" aria-checked={mode === m} disabled={m === "hd" && !!hdBlocked} onClick={() => choose(m)}
              title={m === "auto" ? `HD while the tile is large and the link is clean (at most ${MAX_HD} at once), else SD` : m === "sd" ? "Sub stream: low bandwidth" : hdBlocked ?? "Full-resolution main stream"}>
              {MODE_LABEL[m]}
            </button>
          ))}
        </div>
      )}
    </span>
  );
}
