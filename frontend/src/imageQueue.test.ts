import { describe, expect, it } from "vitest";
import { Slots, retryUrl } from "./imageQueue";

describe("pictures a few at a time", () => {
  it("hands out at most `limit` slots and the rest in order as they free", async () => {
    const s = new Slots(2);
    const order: number[] = [];
    const rels = await Promise.all([s.take(), s.take()]);
    const third = s.take().then((r) => { order.push(3); return r; });
    const fourth = s.take().then((r) => { order.push(4); return r; });
    expect(s.busy).toBe(2);
    expect(s.queued).toBe(2);
    rels[0]();
    const r3 = await third;
    expect(order).toEqual([3]);
    rels[0]();   // releasing twice frees only once
    expect(s.busy).toBe(2);
    rels[1]();
    const r4 = await fourth;
    expect(order).toEqual([3, 4]);
    r3(); r4();
    expect(s.busy).toBe(0);
  });
  it("asks again with a try number", () => {
    expect(retryUrl("/api/traffic/reads/5/image/vehicle", 0)).toBe("/api/traffic/reads/5/image/vehicle");
    expect(retryUrl("/api/traffic/reads/5/image/vehicle", 2)).toBe("/api/traffic/reads/5/image/vehicle?try=2");
    expect(retryUrl("https://a/x?token=t", 1)).toBe("https://a/x?token=t&try=1");
  });
});
