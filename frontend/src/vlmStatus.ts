/** Settings → System's Qwen row in words: the primary local model and, when configured, the fallback model
 * (backend nvr/vlmroute.py Router.vlm_status). */
import { fmtTime, type SystemInfo, type VlmInstance } from "./api";

export function vlmStateWord(state: VlmInstance["state"] | undefined): string {
  if (state === "ready") return "Ready";
  if (state === "unresponsive") return "Not answering · restarting Ollama";
  return "Starting";
}

const gpu = (m: VlmInstance) => (m.gpu ? ` on GPU ${m.gpu}` : "");

/** The row's value: "Ready · qwen3.8:27b" with one model, both models and states with a fallback. */
export function vlmValue(s: Pick<SystemInfo, "vlm" | "vlm_ready" | "vlm_state" | "vlm_model">): string {
  const v = s.vlm;
  if (!v || !v.fallback) return `${s.vlm_ready ? "Ready" : vlmStateWord(s.vlm_state)} · ${s.vlm_model}`;
  return `${v.primary.model}${gpu(v.primary)}: ${vlmStateWord(v.primary.state)} · fallback ${v.fallback.model}${gpu(v.fallback)}: ${vlmStateWord(v.fallback.state)}`;
}

/** The row's second line: why it is down, who is answering, queues. */
export function vlmSub(s: Pick<SystemInfo, "vlm" | "vlm_state" | "vlm_down_since" | "queues">, fmt: (ts: number) => string = fmtTime): string {
  const v = s.vlm;
  const parts: string[] = [];
  if (s.vlm_state === "unresponsive") {
    parts.push(`down since ${s.vlm_down_since ? fmt(s.vlm_down_since) : "?"} · if nvidia-smi says the GPU is lost, reboot`);
  }
  if (v?.fallback) {
    if (v.primary.state !== "ready" && v.fallback.state === "ready") parts.push(`the fallback ${v.fallback.model} is answering meanwhile`);
    if (v.fallback.state === "unresponsive") parts.push(`the fallback has not been answering since ${v.fallback.down_since ? fmt(v.fallback.down_since) : "?"}`);
    if (v.routed_to_fallback_last_hour) parts.push(`${v.routed_to_fallback_last_hour} answered by the fallback in the last hour`);
    if (v.primary.queue || v.fallback.queue) parts.push(`working on ${v.primary.queue} + ${v.fallback.queue}`);
  }
  if (s.vlm_state !== "unresponsive") parts.push(s.queues.synopsis ? `${s.queues.synopsis} waiting` : "queue empty");
  return parts.join(" · ");
}
