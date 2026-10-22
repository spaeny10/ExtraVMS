import { describe, expect, it } from "vitest";
import { type DigestRef, digestBlocks, refEvent, refHref } from "./digestText";

const TEXT = [
  "**Needs your attention**",
  "- World Headquarters › TSS-DT · South Exterior Door, yesterday 4:17 PM: Possible holstered handgun seen on a second look (medium confidence), unconfirmed: needs review [#22909]",
  "- (no Site) › Qwenbot · SW Corner PTZ, yesterday 4:19 PM: Black pistol confirmed by a second look (high confidence) [#46773]",
  "",
  "**World Headquarters**",
  "- TSS-DT: Unusual at Side Yard, yesterday 2:05 PM: Stayed 45 min, the longest visit on record [#501]; 12 person.",
  "- Quiet: T-304",
].join("\n");
const REFS: DigestRef[] = [
  { server_id: "s_tss", event_id: 22909, camera_id: "door", location_id: "l_whq" },
  { server_id: "s_qwen", event_id: 46773, camera_id: "sw", location_id: null },
  { server_id: "s_tss", event_id: 501, camera_id: "yard", location_id: "l_whq" },
];

describe("digest markdown", () => {
  it("renders bold headers and bullets, never raw markup", () => {
    const b = digestBlocks(TEXT, REFS);
    expect(b.map((x) => x.type)).toEqual(["p", "ul", "p", "ul"]);
    expect(b[0]).toEqual({ type: "p", parts: [{ t: "bold", text: "Needs your attention" }] });
    expect(b[3].type === "ul" && b[3].items.length).toBe(2);
    const flat = JSON.stringify(b);
    expect(flat).not.toContain("**");
    expect(flat).not.toMatch(/"- /);
    // HTML in the text stays text (React escapes it): there is no html node type at all
    const html = digestBlocks("<b>hi</b> <script>x()</script> **bold**", []);
    expect(html).toEqual([{ type: "p", parts: [{ t: "text", text: "<b>hi</b> <script>x()</script> " }, { t: "bold", text: "bold" }] }]);
  });

  it("star bullets and an old one-line-per-server digest", () => {
    const b = digestBlocks("*   **Alpha**: quiet\n*   **Bravo**: busy [#3]", null);
    expect(b.length).toBe(1);
    expect(b[0].type === "ul" && b[0].items[0]).toEqual([{ t: "bold", text: "Alpha" }, { t: "text", text: ": quiet" }]);
    // no stored references (an older digest): the citation is plain text
    expect(b[0].type === "ul" && b[0].items[1][2]).toEqual({ t: "cite", ref: "3", footage: false, link: null });
  });
});

describe("event references", () => {
  it("ties each [#id] to its server in order", () => {
    const b = digestBlocks(TEXT, REFS);
    const cites = b.flatMap((x) => (x.type === "p" ? [x.parts] : x.items)).flat().filter((p) => p.t === "cite");
    expect(cites.map((c) => c.t === "cite" && c.link?.server_id)).toEqual(["s_tss", "s_qwen", "s_tss"]);
  });

  it("the same id on two servers goes to the right one", () => {
    const refs: DigestRef[] = [{ server_id: "a", event_id: 7 }, { server_id: "b", event_id: 7 }];
    const b = digestBlocks("- A: [#7]\n- B: [#7]", refs);
    const items = b[0].type === "ul" ? b[0].items : [];
    expect(items.map((it) => it.find((p) => p.t === "cite"))).toEqual([
      { t: "cite", ref: "7", footage: false, link: refs[0] }, { t: "cite", ref: "7", footage: false, link: refs[1] }]);
  });

  it("a reference that doesn't match the text is not linked", () => {
    const b = digestBlocks("seen [#5] and [#6]", [{ server_id: "a", event_id: 9 }, { server_id: "a", event_id: 6 }]);
    const cites = b[0].type === "p" ? b[0].parts.filter((p) => p.t === "cite") : [];
    expect(cites.map((c) => c.t === "cite" && c.link)).toEqual([null, { server_id: "a", event_id: 6 }]);
    expect(digestBlocks("[#5]", [{ server_id: null, event_id: 5 }])[0]).toEqual({ type: "p", parts: [{ t: "cite", ref: "5", footage: false, link: null }] });
  });

  it("opens the event viewer and links to the Timeline", () => {
    expect(refEvent(REFS[0])).toEqual({ server: "s_tss", id: 22909, location: "l_whq" });
    expect(refEvent(REFS[1])).toEqual({ server: "s_qwen", id: 46773, location: null });
    expect(refHref(REFS[0])).toBe("/sites/l_whq/timeline?server=s_tss&cam=door&event=22909");
    expect(refHref(REFS[1])).toBe("/s/s_qwen/#timeline?cam=sw&event=46773");
    expect(refHref({ server_id: "s_x", event_id: 1 })).toBe("/s/s_x/");
  });
});
