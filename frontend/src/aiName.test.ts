import { expect, test } from "vitest";
import { AI_NAME, aiModelLabel } from "./aiName";

test("Qwen model ids read as Otso with their size; other models are left alone", () => {
  expect(AI_NAME).toBe("Otso");
  expect(aiModelLabel("qwen3.8:27b")).toBe("Otso 27B");
  expect(aiModelLabel("qwen3.5:9b")).toBe("Otso 9B");
  expect(aiModelLabel("Qwen/Qwen3.8-27B-FP8")).toBe("Otso 27B");
  expect(aiModelLabel("Qwen/Qwen2.5-VL-32B-Instruct-AWQ")).toBe("Otso 32B");
  expect(aiModelLabel("qwen-vl")).toBe("Otso");
  expect(aiModelLabel("llama3.2-vision:11b")).toBe("llama3.2-vision:11b");
  expect(aiModelLabel(null)).toBe("");
});
