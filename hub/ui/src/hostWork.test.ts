import { describe, expect, it } from "vitest";
import {
  fmtNum, fmtRate, gpuRoomText, gpuShareText, gpuTime, inferLine, inferRoomText, instanceWork, queueText, queueTitle, resourcesFormError, resourcesText, seriesMax, sparkPath, vllmLine, vllmTitle, workRows, yoloLine,
} from "./central";
import type { HostCapacity, HostGpu, InstanceWork, VllmWork } from "./api";

const vllm: VllmWork = { ok: true, running: 2, waiting: 0, waiting_capacity: 0, kv_cache_pct: 34.1, gen_tps: 180.4, prompt_tps: 1450.2,
  queue_p50_s: 0.05, e2e_p50_s: 6.2, gpu: 0, model: "Qwen/Qwen3.8-27B-FP8", at: 1 };
const inst = (verify_q: number | null, gpu: number | null, extra: Partial<InstanceWork> = {}): InstanceWork =>
  ({ ok: true, verify_q, synopsis_q: 0, verify_rate_per_min: 0.5, gpu, yolo_ready: true, vlm_ready: true, at: 1, ...extra });
const cap = (work?: HostCapacity["work"], gpus: HostGpu[] = [
  { index: 0, name: "NVIDIA A40", instances: 0, verify_q: 0 },
  { index: 1, name: "NVIDIA A10G", instances: 2, verify_q: 29 },
]): HostCapacity => ({ cpus: 80, gpus, work });

describe("numbers", () => {
  it("rounds like the rest of the page", () => {
    expect(fmtNum(180.4)).toBe("180");
    expect(fmtNum(12.34)).toBe("12.3");
    expect(fmtNum(0.5)).toBe("0.5");
    expect(fmtRate(0.5)).toBe("0.5/min");
    expect(fmtRate(12)).toBe("12/min");
    expect(fmtRate(null)).toBe("—/min");
  });
});

describe("vLLM line", () => {
  it("running, waiting, KV cache and generated tokens per second", () => {
    expect(vllmLine(vllm)).toBe("Qwen (vLLM): 2 running · 0 waiting · KV 34% · 180 tok/s");
    expect(vllmTitle(vllm)).toBe("Qwen/Qwen3.8-27B-FP8 · prompt 1450 tok/s · median wait 0.1 s · median request 6.2 s");
  });
  it("leaves out what an older vLLM doesn't export; says when it does not answer", () => {
    expect(vllmLine({ ok: true, running: 1, waiting: 0, kv_cache_pct: null, gen_tps: null })).toBe("Qwen (vLLM): 1 running · 0 waiting");
    expect(vllmLine({ ok: false, error: "metrics: timed out", running: 2, waiting: 1 }))
      .toBe("Qwen (vLLM): not answering (metrics: timed out) · last 2 running · 1 waiting");
    expect(vllmLine({ ok: false, error: "x".repeat(100) })).toBe(`Qwen (vLLM): not answering (${"x".repeat(59)}…)`);
    expect(vllmLine({ ok: false, error: "no axiom-vllm container" })).toBe("Qwen (vLLM): not answering (no axiom-vllm container)");
    expect(vllmLine(undefined)).toBe("Qwen (vLLM): no data");
    expect(vllmTitle({ ok: true, waiting_capacity: 3 })).toBe("3 waiting for KV cache room");
  });
});

describe("work rows", () => {
  it("the A40's Qwen, then YOLO on the A10G", () => {
    const rows = workRows(cap({ at: 1, vllm, instances: { ci_a: inst(20, 1), ci_b: inst(9, 1) } }))!;
    expect(rows.map((r) => [r.label, r.text, r.state])).toEqual([
      ["GPU 0 · A40", "Qwen (vLLM): 2 running · 0 waiting · KV 34% · 180 tok/s", "ok"],
      ["GPU 1 · A10G", "YOLO: 29 waiting across 2 instances", "ok"],
    ]);
  });
  it("warns when vLLM has waiting requests or an instance's queue grows; bad when vLLM does not answer", () => {
    const trend = { instances: { ci_a: { verify_q: 40, verify_q_before: 10, growing: true, recovered: false } }, vllm_waiting_for_s: 60 };
    const rows = workRows(cap({ at: 1, vllm: { ...vllm, waiting: 3 }, instances: { ci_a: inst(40, 1) } }), trend)!;
    expect(rows.map((r) => r.state)).toEqual(["warn", "warn"]);
    expect(workRows(cap({ at: 1, vllm: { ok: false, error: "x", gpu: 0 }, instances: {} }))![0].state).toBe("bad");
  });
  it("no data from an older agent; CPU instances and a vLLM on no listed GPU get rows of their own", () => {
    expect(workRows(cap(undefined))).toBeNull();
    expect(workRows(null)).toBeNull();
    const rows = workRows(cap({ at: 1, vllm: { ...vllm, gpu: null }, instances: { ci_c: inst(4, null), ci_d: inst(null, null, { ok: false }) } },
      [{ index: 0, name: "NVIDIA A10", instances: 0, verify_q: 0 }]))!;
    expect(rows.map((r) => [r.label, r.text])).toEqual([
      ["Shared Qwen", "Qwen (vLLM): 2 running · 0 waiting · KV 34% · 180 tok/s"],
      ["CPU", "YOLO on the CPU: 4 waiting across 2 instances"],
    ]);
  });
  it("sums the instances itself when the GPU has no verify_q (and says when none reported)", () => {
    const gpus = [{ index: 1, name: "NVIDIA A10G", instances: 1 }];
    expect(workRows(cap({ at: 1, instances: { ci_a: inst(7, 1) } }, gpus))![0].text).toBe("YOLO: 7 waiting across 1 instance");
    expect(workRows(cap({ at: 1, instances: {} }, [{ index: 1, name: "NVIDIA A10G", instances: 1, verify_q: null }]))![0].text)
      .toBe("YOLO: no queue reported by its 1 instance");
    expect(yoloLine(0, 3)).toBe("YOLO: 0 waiting across 3 instances");
  });
});

describe("sparklines", () => {
  it("scales into the box, 0 at the bottom, at least 1 at the top", () => {
    expect(sparkPath([0, 5, 10], 100, 20)).toBe("M0 20 L50 10 L100 0");
    expect(sparkPath([0, 0], 100, 20)).toBe("M0 20 L100 20");      // flat zero stays at the bottom
    expect(sparkPath([1], 100, 20)).toBe("M100 0");
  });
  it("breaks the line on unknown values; nothing to draw = empty", () => {
    expect(sparkPath([2, null, 4, 4], 30, 10)).toBe("M0 5 M20 0 L30 0");
    expect(sparkPath([null, null], 30, 10)).toBe("");
    expect(sparkPath([], 30, 10)).toBe("");
    expect(seriesMax([null, 3, 9, 2])).toBe(9);
    expect(seriesMax([null])).toBeNull();
  });
});

describe("queues column", () => {
  it("YOLO queue, verified per minute, Qwen queue", () => {
    expect(queueText(inst(29, 1))).toBe("YOLO 29 · 0.5/min · Qwen 0");
    expect(queueText(inst(29, 1, { ok: false, verify_rate_per_min: null }))).toBe("YOLO 29 · —/min · Qwen 0 · not answering");
    expect(queueText(null)).toBe("no data");
  });
  it("its tooltip says why", () => {
    expect(queueTitle(inst(40, 1, { yolo_frame_ms: 11.5 }), true)).toBe("YOLO verify queue growing over the last 15 minutes · YOLO 11.5 ms per frame");
    expect(queueTitle(inst(1, 1, { ok: false, error: "container exited", vlm_ready: false, vlm_state: "down" }), false))
      .toBe("not answering: container exited · Qwen not ready (down)");
    expect(queueTitle(inst(1, 1), false)).toBeUndefined();
    expect(queueTitle(null, false)).toMatch(/older than 0\.2\.0/);
  });
  it("finds the instance on its host", () => {
    const hosts = [{ id: "h1", capacity: cap({ at: 1, instances: { ci_a: inst(29, 1) } }),
      work_trend: { instances: { ci_a: { verify_q: 29, verify_q_before: 3, growing: true, recovered: false } }, vllm_waiting_for_s: null } }];
    expect(instanceWork(hosts, { id: "ci_a", host_id: "h1" })).toEqual({ work: inst(29, 1), growing: true });
    expect(instanceWork(hosts, { id: "ci_z", host_id: "h1" })).toEqual({ work: null, growing: false });
    expect(instanceWork(null, { id: "ci_a", host_id: "h1" })).toEqual({ work: null, growing: false });
  });
});

describe("CPU / memory form", () => {
  it("accepts either or both within the ranges and the host's CPUs", () => {
    expect(resourcesFormError("8", "", 80)).toBeNull();
    expect(resourcesFormError("", "16", null)).toBeNull();
    expect(resourcesFormError("1.5", "2", 80)).toBeNull();
    expect(resourcesFormError("", "")).toBe("Enter the CPUs and / or the memory");
    expect(resourcesFormError("0", "")).toBe("CPUs: a number from 1 to 64");
    expect(resourcesFormError("65", "")).toBe("CPUs: a number from 1 to 64");
    expect(resourcesFormError("8", "", 6)).toBe("The host has 6 CPUs");
    expect(resourcesFormError("", "1")).toBe("Memory: 2 to 512 GB");
    expect(resourcesFormError("", "1024")).toBe("Memory: 2 to 512 GB");
    expect(resourcesFormError("x", "")).toBe("CPUs: a number from 1 to 64");
  });
  it("takes a GPU share in GB, or 0 for no cap", () => {
    expect(resourcesFormError("", "", null, "6")).toBeNull();
    expect(resourcesFormError("", "", null, "0")).toBeNull();
    expect(resourcesFormError("", "", null, "0.5")).toBe("GPU share: 1 to 48 GB, or 0 for no cap");
    expect(resourcesFormError("", "", null, "64")).toBe("GPU share: 1 to 48 GB, or 0 for no cap");
    expect(resourcesFormError("", "", null, "")).toBe("Enter the CPUs and / or the memory");
  });
  it("shows the limits", () => {
    expect(resourcesText(4, 8)).toBe("4 CPUs · 8 GB");
    expect(resourcesText(1, 2.5)).toBe("1 CPU · 2.5 GB");
    expect(resourcesText(null, null)).toBe("— CPUs · — GB");
  });
});

describe("GPU shares", () => {
  it("says how much of a card the Sites' shares hold and how many more fit", () => {
    const r = { index: 1, name: "NVIDIA A10G", total_mb: 23028, used_mb: 7923, reserved_mb: 12288, room_mb: 9716, slots: 2 };
    expect(gpuRoomText(r)).toBe("GPU 1 · A10G: 12 GB of 21.5 GB in Site shares · room for 2 more Sites");
    expect(gpuRoomText({ ...r, reserved_mb: 22000, room_mb: 0, slots: 0 })).toBe("GPU 1 · A10G: 21.5 GB of 21.5 GB in Site shares · full: no room for another Site");
    expect(gpuRoomText({ ...r, slots: 1 })).toContain("room for 1 more Site");
  });
  it("shows an instance's share, its peak and out-of-memory errors", () => {
    const work = { ok: true, gpu_mem: { share_mb: 4096, cap_mb: 3584, reserved_mb: 1500, peak_mb: 2970, oom: 0 } };
    expect(gpuShareText({ gpu: 1, gpu_mem_mb: 4096, gpu_share_mb: 4096, work })).toBe("4 GB share · peak 2.9 GB");
    expect(gpuShareText({ gpu: 1, gpu_mem_mb: 4096, gpu_share_mb: 4096, work: { ...work, gpu_mem: { ...work.gpu_mem, oom: 3 } } }))
      .toBe("4 GB share · peak 2.9 GB · 3 out-of-memory errors");
    expect(gpuShareText({ gpu: 1, gpu_mem_mb: null, gpu_share_mb: 4608, work: null })).toBe("no cap (counted as 4.5 GB)");
    expect(gpuShareText({ gpu: null, gpu_mem_mb: null, gpu_share_mb: null, work: null })).toBe("");
    expect(gpuShareText({ gpu: 1, gpu_mem_mb: null, gpu_share_mb: 0, work: null, inference: "shared", infer_gpu_s: 42.4 })).toBe("shared AI service · 42 s GPU lately");
    expect(gpuShareText({ gpu: 1, gpu_mem_mb: null, gpu_share_mb: 0, work: null, inference: "shared", infer_gpu_s: null })).toBe("shared AI service");
  });
});

describe("shared AI service lines", () => {
  const svc = {
    gpu: 1, name: "NVIDIA A10G", state: "running", ok: true, busy_pct: 4.2, verify_p50_ms: 15.2, verify_p95_ms: 1530, verify_wait_p50_ms: 0.1,
    reserved_mb: 1248, peak_mb: 1300, oom: 0, queues: { verify: 0, background: 0, index: 3 }, busy_peak: 17.4, window_s: 1800, sites: 3, max_sites: 30,
    top: [{ instance_id: "ci_a", site: "Yard A", gpu_s: 42.4 }, { instance_id: "ci_b", site: "Yard B", gpu_s: 300 }],
  };
  it("says how the service is doing and who used it", () => {
    const l = inferLine(svc);
    expect(l.text).toBe("AI service · busy 4% · verify 15 ms, p95 1.5 s · 1.2 GB · 3 Sites");
    expect(l.state).toBe("ok");
    expect(l.top).toBe("Yard A 42 s · Yard B 5 min");
    expect(l.title).toContain("busiest 17% in the last 30 min");
    expect(inferLine({ ...svc, queues: { verify: 4 } }).state).toBe("warn");
    expect(inferLine({ ...svc, busy_pct: 75 }).state).toBe("warn");
    const down = inferLine({ ...svc, ok: false, error: "timed out" });
    expect(down.state).toBe("bad");
    expect(down.text).toBe("AI service not answering: timed out · 3 Sites");
    expect(inferLine({ ...svc, state: "restarting" }).text).toBe("AI service restarting · 3 Sites");
  });
  it("says whether a card's service takes another Site", () => {
    const r = { index: 1, name: "NVIDIA A10G", service: "running", sites: 3, max_sites: 30, busy_peak: 12, ok: true, why: null, slots: 27 };
    expect(inferRoomText(r)).toBe("GPU 1 · A10G: AI service, 3 of 30 Sites · busiest 12% lately");
    expect(inferRoomText({ ...r, ok: false, why: "busy 70% lately (limit 60%)", slots: 0, busy_peak: 70 }))
      .toBe("GPU 1 · A10G: AI service, 3 of 30 Sites · busiest 70% lately · takes no more Sites (busy 70% lately (limit 60%))");
    expect(inferRoomText({ ...r, service: null, sites: 0, busy_peak: null })).toBe("GPU 1 · A10G: no AI service yet");
  });
  it("shows GPU time in seconds or minutes", () => {
    expect(gpuTime(0.44)).toBe("0.4 s");
    expect(gpuTime(42.4)).toBe("42 s");
    expect(gpuTime(210)).toBe("3.5 min");
  });
});
