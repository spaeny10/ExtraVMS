import { useEffect, useState } from "react";
import { stillDelay } from "./liveFallback";
import { nowS } from "./playback";

/**
 * A paused tile's still frame: the time to ask /api/frame for (a few seconds back, so the frame is on disk), moved on
 * every 10 s while `active`, every 60 s once a still failed to load (no recording, server not answering) until one
 * loads again. Wire onLoad / onError to the <img>; keep the <img> rendered (hidden) while `failed` so it can retry.
 */
export function useStill(active: boolean, lagS = 3) {
  const [ts, setTs] = useState(() => nowS() - lagS);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!active) return;
    const t = setTimeout(() => setTs(nowS() - lagS), stillDelay(failed));
    return () => clearTimeout(t);
  }, [active, ts, failed, lagS]);
  return { ts, failed, onLoad: () => setFailed(false), onError: () => setFailed(true) };
}
