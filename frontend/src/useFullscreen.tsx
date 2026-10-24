/**
 * The React side of fullscreen.ts: a full-screen toggle for one box (the event player's, the hub's Site Live view),
 * its state, and the diagnostics overlay.
 *
 *   const fs = useFullscreen(boxRef, { picture: () => video, stage: () => stageEl });
 *   fs.on · fs.rotated · fs.toggle() · fs.exit() · {fs.debugView} (render it inside the stage, so it turns with it)
 */
import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import {
  FS_DEBUG_EVENT, displayMode, fullscreenBox, fullscreenSupported, startFullscreen, stopFullscreen, subscribeFullscreen, type FsInfo, type Picture,
} from "./fullscreen";
import "./fullscreen.css";

export function useFullscreen(box: RefObject<HTMLElement | null>, opts: { picture?: () => Picture | null | undefined; stage?: () => HTMLElement | null | undefined } = {}) {
  const [info, setInfo] = useState<FsInfo | null>(null);
  const optsRef = useRef(opts);
  optsRef.current = opts;
  useEffect(() => subscribeFullscreen((i) => setInfo(i && i.box === box.current ? i : null)), [box]);
  // the player goes away (the viewer closed) while it is full screen: leave it (the box it went full screen with: the
  // ref is already cleared when this runs)
  const mine = useRef<HTMLElement | null>(null);
  useEffect(() => () => { if (mine.current && fullscreenBox() === mine.current) stopFullscreen(mine.current, "unmount"); }, []);
  const enter = useCallback(() => {
    const o = optsRef.current;
    mine.current = box.current;
    startFullscreen(box.current, { picture: o.picture?.() ?? null, stage: o.stage?.() ?? null });
  }, [box]);
  const exit = useCallback(() => stopFullscreen(box.current), [box]);
  const toggle = useCallback(() => {
    if (box.current && fullscreenBox() === box.current) stopFullscreen(box.current);
    else enter();
  }, [box, enter]);
  return {
    on: !!info,
    rotated: !!info?.rotated,
    info,
    enter, exit, toggle,
    debugView: info?.debug ? <FullscreenDebug info={info} /> : null,
  };
}

const yes = (q: string) => { try { return matchMedia(q).matches; } catch { return false; } };

/** The overlay a long-press on the zoom pill (or ?fsdebug=1) shows in full screen: what the browser allowed. A tap
 *  hides it. */
export function FullscreenDebug({ info }: { info: FsInfo }) {
  const [, tick] = useState(0);
  useEffect(() => {   // orientation and viewport change without a session update
    const on = () => tick((n) => n + 1);
    addEventListener("resize", on);
    return () => removeEventListener("resize", on);
  }, []);
  const o = typeof screen !== "undefined" ? (screen as Screen & { orientation?: { type?: string; angle?: number } }).orientation : undefined;
  const rows: [string, string][] = [
    ["Fullscreen API", `${fullscreenSupported() ? "enabled" : "not enabled"}${info.api ? "" : " (no element API)"}`],
    ["Real full screen", info.real + (info.realError ? ` (${info.realError})` : "")],
    ["Orientation lock", info.lock + (info.lockError ? ` (${info.lockError})` : "")],
    ["Display mode", displayMode()],
    ["Pointer", `${yes("(pointer: coarse)") ? "coarse" : yes("(pointer: fine)") ? "fine" : "none"} · hover ${yes("(hover: none)") ? "none" : "yes"}`],
    ["Orientation", `${o?.type ?? "?"} ${o?.angle ?? ""}° · ${innerWidth}×${innerHeight}`],
    ["Picture", `${info.picture.width}×${info.picture.height}${info.wants ? " (wants landscape)" : ""}`],
    ["CSS rotation", info.rotated ? "on" : "off"],
  ];
  return (
    <div className="fs-debug" role="status" onClick={(e) => { e.stopPropagation(); info.box.dispatchEvent(new CustomEvent(FS_DEBUG_EVENT)); }}
      onPointerDown={(e) => e.stopPropagation()}>
      {rows.map(([k, v]) => <div key={k}><b>{k}</b> {v}</div>)}
      <div className="fs-debug-hint">tap to hide</div>
    </div>
  );
}
