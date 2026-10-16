import { afterEach, describe, expect, it, vi } from "vitest";
import { type LiveReport, inboundVideo, pathQuality, reportLive, setLiveReporter, statsSample, whepRoute, whepTiming } from "./whepTiming";

/** A clock the test moves by hand. */
const clock = (t0 = 1000) => { let t = t0; return { now: () => t, at: (v: number) => { t = v; } }; };

describe("whepTiming", () => {
  it("times every stage of a connection that plays, and TTFF from start", () => {
    const c = clock(1000);
    const t = whepTiming(1, c.now);
    for (const [m, at] of [["offer", 1010], ["iceGathered", 1510.4], ["posted", 1600], ["answer", 1605], ["connected", 1800], ["firstFrame", 2100]] as const) {
      c.at(at);
      expect(t.mark(m)).toBe(true);
    }
    expect(t.snapshot()).toEqual({
      attempt: 1, ttff_ms: 1100, ice_timed_out: false,
      stages: { offer: 10, ice: 500, post: 90, answer: 5, connected: 195, first_frame: 300 },
    });
    expect(t.has("firstFrame")).toBe(true);
  });

  it("keeps each mark the first time only", () => {
    const c = clock(0);
    const t = whepTiming(1, c.now);
    c.at(50);
    expect(t.mark("offer")).toBe(true);
    c.at(90);
    expect(t.mark("offer")).toBe(false);
    expect(t.snapshot().stages.offer).toBe(50);
  });

  it("leaves out stages whose marks are missing, and has no TTFF before the first frame", () => {
    const c = clock(0);
    const t = whepTiming(2, c.now);
    c.at(20); t.mark("offer");
    c.at(1520); t.iceTimedOut(); t.mark("iceGathered");
    // the POST never answered: no post / answer / connected / first_frame
    expect(t.snapshot()).toEqual({ attempt: 2, stages: { offer: 20, ice: 1500 }, ttff_ms: null, ice_timed_out: true });
    expect(t.has("posted")).toBe(false);
  });

  it("a stage whose marks came out of order counts as 0, not negative", () => {
    const c = clock(0);
    const t = whepTiming(1, c.now);
    c.at(300); t.mark("firstFrame");
    c.at(310); t.mark("connected");
    expect(t.snapshot().stages.first_frame).toBe(0);
    expect(t.snapshot().ttff_ms).toBe(300);
  });

  it("a retry is a new recorder with its own attempt number and clock", () => {
    const c = clock(0);
    const first = whepTiming(1, c.now);
    c.at(4000);
    const second = whepTiming(2, c.now);
    c.at(4700); second.mark("firstFrame");
    expect(first.snapshot().ttff_ms).toBeNull();
    expect(second.snapshot()).toMatchObject({ attempt: 2, ttff_ms: 700 });
  });

  it("elapsed seconds since start, to a tenth", () => {
    const c = clock(0);
    const t = whepTiming(1, c.now);
    expect(t.elapsedS(12_345)).toBe(12.3);
    c.at(500);
    expect(t.elapsedS()).toBe(0.5);
  });
});

describe("whepRoute", () => {
  const page = "https://hub.axiomvision.ai";
  it("is direct when the URL is on another origin (the hub's LAN client)", () => {
    expect(whepRoute("https://192.168.1.5:8443/api/whep/cam1_sub?direct=tok", page)).toBe("direct");
    expect(whepRoute("http://hub.axiomvision.ai/s/s_abc/api/whep/cam1", page)).toBe("direct");   // another scheme is another origin
  });
  it("is hub under this page's /s/<id>/ prefix", () => {
    expect(whepRoute("/s/s_abc/api/whep/cam1_sub", page)).toBe("hub");
    expect(whepRoute("https://hub.axiomvision.ai/s/s_abc/api/whep/cam1_hd", page)).toBe("hub");
  });
  it("is local on the NVR's own UI", () => {
    expect(whepRoute("/api/whep/cam1_sub", "https://192.168.1.5:8443")).toBe("local");
    expect(whepRoute("/api/whep/cam1", "http://localhost:8080")).toBe("local");
  });
});

describe("pathQuality", () => {
  it("SD is the _sub path, everything else HD", () => {
    expect(pathQuality("cam1_sub")).toBe("sd");
    expect(pathQuality("cam1")).toBe("hd");
    expect(pathQuality("cam1_hd")).toBe("hd");
  });
});

describe("reportLive", () => {
  afterEach(() => setLiveReporter(null));
  const r: LiveReport = {
    attempt: 1, stages: {}, ttff_ms: 900, ice_timed_out: false, event: "first_frame", base: "/s/s_1", camera: "cam1", path: "cam1_sub",
    quality: "sd", route: "hub", duration_s: null,
  };
  it("does nothing without a reporter (the NVR's own UI)", () => {
    expect(() => reportLive(r)).not.toThrow();
  });
  it("hands reports to the registered reporter, and a reporter that throws never breaks the caller", () => {
    const fn = vi.fn();
    setLiveReporter(fn);
    reportLive(r);
    expect(fn).toHaveBeenCalledWith(r);
    setLiveReporter(() => { throw new Error("offline"); });
    expect(() => reportLive(r)).not.toThrow();
  });
});

describe("statsSample", () => {
  it("counts from zero on the first read", () => {
    expect(statsSample(null, { packetsLost: 2, packetsReceived: 98, framesDecoded: 50, framesDropped: 0, freezeCount: 0, frameWidth: 640, frameHeight: 360, jitter: 0.01 }))
      .toEqual({ loss: 0.02, packetsLost: 2, packetsReceived: 98, freezes: 0, framesDropped: 0, framesDecoded: 50, dropped: 0, width: 640, height: 360, jitter: 0.01 });
  });
  it("works on the change since the last read", () => {
    const prev = { packetsLost: 2, packetsReceived: 98, framesDecoded: 50, framesDropped: 1, freezeCount: 1 };
    const s = statsSample(prev, { packetsLost: 12, packetsReceived: 188, framesDecoded: 95, framesDropped: 6, freezeCount: 3, frameWidth: 1920, frameHeight: 1080 });
    expect(s.loss).toBe(0.1);
    expect(s.dropped).toBe(0.1);
    expect(s.freezes).toBe(2);
    expect([s.width, s.height, s.jitter]).toEqual([1920, 1080, null]);
  });
  it("nothing moved (or counters went back): 0, never negative or NaN", () => {
    const same = { packetsLost: 5, packetsReceived: 100, framesDecoded: 10 };
    expect(statsSample(same, same)).toMatchObject({ loss: 0, dropped: 0, freezes: 0 });
    expect(statsSample({ packetsReceived: 500, framesDecoded: 300 }, { packetsReceived: 10, framesDecoded: 5 })).toMatchObject({ packetsReceived: 0, framesDecoded: 0, loss: 0 });
  });
});

describe("inboundVideo", () => {
  it("picks the video inbound-rtp entry", () => {
    const stats = [{ type: "inbound-rtp", kind: "audio", packetsReceived: 1 }, { type: "transport" }, { type: "inbound-rtp", kind: "video", framesDecoded: 3 }];
    expect(inboundVideo(stats)).toEqual({ type: "inbound-rtp", kind: "video", framesDecoded: 3 });
    expect(inboundVideo([{ type: "inbound-rtp", mediaType: "video", framesDecoded: 1 }])).toMatchObject({ framesDecoded: 1 });
    expect(inboundVideo([{ type: "outbound-rtp", kind: "video" }])).toBeNull();
  });
});
