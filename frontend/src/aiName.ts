/** The AI's name wherever people read it: the model underneath (Qwen) is never shown to customers. */
export const AI_NAME = "Otso";

/** A model id for people to read: any Qwen ("qwen3.8:27b", "Qwen/Qwen3.8-27B-FP8") is "Otso 27B"; others unchanged. */
export function aiModelLabel(model: string | null | undefined): string {
  if (!model) return "";
  if (!/qwen/i.test(model)) return model;
  const m = /(?<![\d.])(\d+(?:\.\d+)?)b\b/i.exec(model);
  return m ? `${AI_NAME} ${m[1]}B` : AI_NAME;
}
