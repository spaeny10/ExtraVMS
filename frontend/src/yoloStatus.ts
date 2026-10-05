/** Settings → System's YOLO row in words, including the Hailo → CPU fallback (backend nvr/detector.py). */
import { fmtTime, type SystemInfo, type YoloFallback } from "./api";

export function yoloState(s: Pick<SystemInfo, "yolo_ready" | "yolo_fallback">): string {
  const fb = s.yolo_fallback;
  if (fb && !fb.using) return "Down: Hailo not found and no CPU model";
  if (fb) return "Running on the CPU (Hailo unavailable)";
  return s.yolo_ready ? "Ready" : "Loading";
}

export function yoloFallbackText(fb: YoloFallback, fmt: (ts: number) => string = fmtTime): string {
  const parts = [`Hailo accelerator not available since ${fmt(fb.since)}`];
  if (fb.error) parts.push(`reason: ${fb.error}`);
  parts.push(fb.using ? "the Hailo is retried every 10 min (NVR_HAILO_RETRY_S) and used again, by itself, once it is back" : "events are not being verified until the Hailo is back");
  return parts.join("; ");
}
