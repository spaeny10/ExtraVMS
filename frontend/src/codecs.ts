/**
 * Can this browser play a live path's video before it connects? The server lists each live path's codec
 * (GET /api/live/codecs, backend mediamtx.live_codecs); WebRTC decoders come from RTCRtpReceiver.getCapabilities.
 * H.264 plays everywhere; H.265 only where the browser lists video/H265 (Chrome or Edge on Windows with a GPU, recent
 * Safari). Pure, with the capabilities injectable, so vitest covers it; LiveQuality.tsx uses it per camera.
 */

export type LiveCodec = "H264" | "H265" | null;
/** GET /api/live/codecs: every live path (<id>, <id>_sub, <id>_hd) → its video codec (null: unknown) and whether it is being pulled */
export type LiveCodecs = Record<string, { video: LiveCodec; ready: boolean }>;
/** What RTCRtpReceiver.getCapabilities("video") answers (null: the browser can't say) */
export type VideoCaps = { codecs: { mimeType: string }[] } | null;

export function receiverCaps(): VideoCaps {
  try {
    return typeof RTCRtpReceiver !== "undefined" && typeof RTCRtpReceiver.getCapabilities === "function"
      ? RTCRtpReceiver.getCapabilities("video") : null;
  } catch {
    return null;
  }
}

/** Whether a live stream of `codec` can play here. Unknown codecs, and H.265 where the browser can't list its
 *  decoders, are tried: the player's own check (onUnsupported) is still the safety net. */
export function canPlayLive(codec: LiveCodec, caps: VideoCaps = receiverCaps()): boolean {
  if (codec !== "H265" || !caps) return true;
  return caps.codecs.some((c) => /^video\/(h265|hevc)$/i.test(c.mimeType));
}

/** The codec the server reported for a path (null: unknown, or no list yet). */
export const pathCodec = (codecs: LiveCodecs | null | undefined, path: string): LiveCodec => codecs?.[path]?.video ?? null;

/** On the HD button of a camera whose HD stream this browser can't play. */
export const HD_H265_TITLE = "HD is H.265, which this browser can't play live (Chrome or Edge on Windows with a GPU can)";
export const HD_FAILED_TITLE = "This browser couldn't decode this camera's HD stream";
/** On a tile whose SD stream (an H.265 sub stream, or an H.265 main relayed) can't play here: instead of a black box. */
export const SD_H265_TEXT = "This camera's SD stream is H.265, which this browser can't play live. Set its sub stream to H.264 in Settings › Cameras › Camera.";

/**
 * One answer per server, shared by every tile and kept `ttlMs` (the server caches 30 s too): `get(key, load)` returns
 * the cached list, or loads it (one request at a time per key); a failed load is kept as null for the same time, so
 * an older server without the endpoint isn't asked by every tile.
 */
export function codecCache(ttlMs = 30_000, now: () => number = () => Date.now()) {
  const entries = new Map<string, { at: number; value: Promise<LiveCodecs | null> }>();
  const answered = new Map<string, LiveCodecs | null>();
  return {
    get(key: string, load: () => Promise<LiveCodecs>): Promise<LiveCodecs | null> {
      const hit = entries.get(key);
      if (hit && now() - hit.at < ttlMs) return hit.value;
      const value = load().catch(() => null).then((v) => { answered.set(key, v); return v; });
      entries.set(key, { at: now(), value });
      return value;
    },
    /** the last answer for `key` (undefined: none yet), so a tile mounted later starts from it instead of "unknown" */
    last(key: string): LiveCodecs | null | undefined { return answered.get(key); },
  };
}
