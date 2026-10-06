import { describe, expect, it } from "vitest";
import { livePath } from "./Views";

describe("livePath", () => {
  it("SD is always the _sub path", () => {
    expect(livePath({ id: "gate" }, false)).toBe("gate_sub");
    expect(livePath({ id: "gate", record_stream: "sub" }, false)).toBe("gate_sub");
  });
  it("HD is the recorded path, or _hd when the camera records its sub stream", () => {
    expect(livePath({ id: "gate" }, true)).toBe("gate");
    expect(livePath({ id: "gate", record_stream: "main" }, true)).toBe("gate");
    expect(livePath({ id: "gate", record_stream: "sub" }, true)).toBe("gate_hd");
  });
});
