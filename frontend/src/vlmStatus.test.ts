import { describe, expect, it } from "vitest";
import type { SystemInfo } from "./api";
import { vlmSub, vlmValue } from "./vlmStatus";

const base = { vlm_ready: true, vlm_state: "ready" as const, vlm_model: "qwen3.8:27b", vlm_down_since: null, queues: { verify: 0, synopsis: 0 } };
const two = (p: "ready" | "unresponsive" | "starting", f: "ready" | "unresponsive" | "starting", routed = 0): Partial<SystemInfo> => ({
  ...base, vlm_state: p,
  vlm: { primary: { model: "qwen3.8:27b", gpu: "1", state: p, queue: 0 }, fallback: { model: "qwen3.5:9b", gpu: "0", state: f, queue: 0 }, routed_to_fallback_last_hour: routed },
});

describe("Qwen row", () => {
  it("one model reads as before", () => {
    expect(vlmValue({ ...base, vlm: { primary: { model: "qwen3.8:27b", gpu: "1", state: "ready", queue: 0 }, fallback: null, routed_to_fallback_last_hour: 0 } })).toBe("Ready · qwen3.8:27b");
    expect(vlmSub({ ...base, vlm: null })).toBe("queue empty");
  });
  it("names both models and their states", () => {
    expect(vlmValue(two("ready", "ready") as SystemInfo)).toBe("qwen3.8:27b on GPU 1: Ready · fallback qwen3.5:9b on GPU 0: Ready");
  });
  it("says the fallback is answering while the primary is down", () => {
    const s = { ...two("unresponsive", "ready", 12), vlm_down_since: 100 } as SystemInfo;
    expect(vlmValue(s)).toContain("qwen3.8:27b on GPU 1: Not answering");
    const sub = vlmSub(s, () => "10:00");
    expect(sub).toContain("down since 10:00");
    expect(sub).toContain("the fallback qwen3.5:9b is answering meanwhile");
    expect(sub).toContain("12 answered by the fallback in the last hour");
  });
});
