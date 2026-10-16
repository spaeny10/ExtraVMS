/**
 * Time to first frame (TTFF) of a WHEP connection, and the link-quality samples the live player reads while it plays:
 * pure, so vitest covers them; WhepPlayer.tsx feeds them performance.now() and RTCPeerConnection.getStats().
 *
 * One recorder per connection attempt (a retry is a new attempt with the next number). Marks, in the order they
 * normally come: start, offer (created and set locally: ICE gathering begins), iceGathered (or the wait for it timed
 * out: iceTimedOut), posted (the WHEP POST's answer came back), answer (applied), connected, firstFrame (a decoded
 * video frame). Each mark is kept the first time only. A stage needs both of its marks; one that would be negative
 * (marks out of order) counts as 0.
 *
 * Reporting: the fleet hub's UI registers a reporter (setLiveReporter) and gets one report when the first frame lands
 * and one when the attempt ends; the NVR's own UI registers none, so nothing is sent from it.
 */

export type WhepMark = "start" | "offer" | "iceGathered" | "posted" | "answer" | "connected" | "firstFrame";
export type WhepStage = "offer" | "ice" | "post" | "answer" | "connected" | "first_frame";

/** stage → (from mark, to mark) */
export const STAGES: readonly [WhepStage, WhepMark, WhepMark][] = [
  ["offer", "start", "offer"],
  ["ice", "offer", "iceGathered"],
  ["post", "iceGathered", "posted"],
  ["answer", "posted", "answer"],
  ["connected", "answer", "connected"],
  ["first_frame", "connected", "firstFrame"],
];

/** What onFirstFrame receives and the reports carry: ms, rounded; ttff_ms null until the first frame. */
export type WhepTimingSnapshot = {
  attempt: number; stages: Partial<Record<WhepStage, number>>; ttff_ms: number | null; ice_timed_out: boolean;
};

export type WhepTiming = {
  readonly attempt: number;
  /** record a mark (first time only); false when it was already set */
  mark: (m: WhepMark, t?: number) => boolean;
  has: (m: WhepMark) => boolean;
  /** the ICE gathering wait hit its timeout (the offer went out with the candidates found so far) */
  iceTimedOut: () => void;
  snapshot: () => WhepTimingSnapshot;
  /** seconds since start (the attempt's viewing time at teardown) */
  elapsedS: (t?: number) => number;
};

export function whepTiming(attempt: number, now: () => number = () => performance.now()): WhepTiming {
  const marks: Partial<Record<WhepMark, number>> = { start: now() };
  let iceTimedOut = false;
  return {
    attempt,
    mark: (m, t = now()) => {
      if (marks[m] !== undefined) return false;
      marks[m] = t;
      return true;
    },
    has: (m) => marks[m] !== undefined,
    iceTimedOut: () => { iceTimedOut = true; },
    snapshot: () => {
      const stages: Partial<Record<WhepStage, number>> = {};
      for (const [s, a, b] of STAGES) {
        const ta = marks[a], tb = marks[b];
        if (ta !== undefined && tb !== undefined) stages[s] = Math.max(0, Math.round(tb - ta));
      }
      const ff = marks.firstFrame;
      return { attempt, stages, ttff_ms: ff === undefined ? null : Math.max(0, Math.round(ff - marks.start!)), ice_timed_out: iceTimedOut };
    },
    elapsedS: (t = now()) => Math.max(0, Math.round((t - marks.start!) / 100) / 10),
  };
}

// ---------------------------------------------------------------- route and quality

/** How the WHEP offer reaches the server: through the hub's /s/<id> proxy, straight to the server on its LAN (the hub's
 *  Direct-on-LAN client), or the NVR's own UI talking to itself. */
export type LiveRoute = "hub" | "direct" | "local";

/** "direct" when the WHEP URL is on another origin than this page (the hub's hybrid client sent it to the LAN base),
 *  "hub" when it is under this page's /s/<id>/ prefix, else "local". */
export function whepRoute(whepUrl: string, pageOrigin: string = typeof location === "undefined" ? "http://localhost" : location.origin): LiveRoute {
  let u: URL;
  try { u = new URL(whepUrl, pageOrigin); } catch { return "local"; }
  if (u.origin !== new URL(pageOrigin).origin) return "direct";
  return /^\/s\/[A-Za-z0-9_-]+\//.test(u.pathname) ? "hub" : "local";
}

/** SD is always <id>_sub; anything else (<id>, <id>_hd) is the main stream. */
export const pathQuality = (path: string): "sd" | "hd" => (path.endsWith("_sub") ? "sd" : "hd");

// ---------------------------------------------------------------- reports

/**
 * One report: `event` "first_frame" when the picture appears, "end" when the attempt is torn down (a retry, the tile
 * closing, the page leaving; ttff_ms null = it never showed a frame). `base` is the camera's server client's base
 * ("/s/<id>" on the hub, the direct client included); the reporter turns it into a server id.
 */
export type LiveReport = WhepTimingSnapshot & {
  event: "first_frame" | "end"; base: string; camera: string; path: string; quality: "sd" | "hd"; route: LiveRoute;
  /** seconds since the attempt started; only on "end" */
  duration_s: number | null;
};
export type LiveReporter = (r: LiveReport) => void;

let reporter: LiveReporter | null = null;
/** The hub UI registers its beacon here at startup; null turns reporting off. */
export function setLiveReporter(fn: LiveReporter | null): void { reporter = fn; }
/** Hand a report to the registered reporter, if any (a reporter that throws never breaks the player). */
export function reportLive(r: LiveReport): void {
  if (!reporter) return;
  try { reporter(r); } catch { /* telemetry is best effort */ }
}

// ---------------------------------------------------------------- link quality

/** The inbound-rtp video counters WhepPlayer reads from getStats (all optional: browsers differ). */
export type InboundVideo = {
  packetsLost?: number; packetsReceived?: number; freezeCount?: number; framesDropped?: number; framesDecoded?: number;
  frameWidth?: number; frameHeight?: number; jitter?: number;
};

/**
 * What onStats receives every 2 s while connected: the change since the last sample. loss = lost / (lost + received)
 * packets, dropped = dropped / (decoded + dropped) frames (0 when nothing moved), freezes = new freezes; width, height
 * and jitter (s) as the browser reports them now.
 */
export type WhepStatsSample = {
  loss: number; packetsLost: number; packetsReceived: number; freezes: number; framesDropped: number; framesDecoded: number;
  dropped: number; width: number | null; height: number | null; jitter: number | null;
};

/** The sample between two reads (`prev` null: the first read, counted from zero). Counters that went backwards (a new
 *  receiver) count as 0. */
export function statsSample(prev: InboundVideo | null, cur: InboundVideo): WhepStatsSample {
  const d = (k: keyof InboundVideo) => Math.max(0, (cur[k] ?? 0) - (prev?.[k] ?? 0));
  const packetsLost = d("packetsLost"), packetsReceived = d("packetsReceived");
  const framesDropped = d("framesDropped"), framesDecoded = d("framesDecoded");
  return {
    loss: packetsLost + packetsReceived ? packetsLost / (packetsLost + packetsReceived) : 0,
    packetsLost, packetsReceived, freezes: d("freezeCount"), framesDropped, framesDecoded,
    dropped: framesDropped + framesDecoded ? framesDropped / (framesDropped + framesDecoded) : 0,
    width: cur.frameWidth ?? null, height: cur.frameHeight ?? null, jitter: cur.jitter ?? null,
  };
}

/** The video inbound-rtp entry of a getStats() report, or null. */
export function inboundVideo(stats: Iterable<unknown>): InboundVideo | null {
  for (const s of stats) {
    const r = s as { type?: string; kind?: string; mediaType?: string };
    if (r.type === "inbound-rtp" && (r.kind ?? r.mediaType) === "video") return s as InboundVideo;
  }
  return null;
}
