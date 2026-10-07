import { describe, expect, it } from "vitest";
import type { SiteAskSource } from "../api";
import { askQuery, citations, citedSource, groupSources, looksLikeQuestion, orderThreads, parseAnswer, parseInline, sendKey, serversLine } from "./siteAskData";

const src = (o: Partial<SiteAskSource>): SiteAskSource => ({ kind: "event", server_id: "s1", server_name: "Alpha", ...o });

describe("citations become chips", () => {
  it("splits text, bold and citations, including per-server refs and footage", () => {
    expect(parseInline("Yes: the Lobby at 10:03 AM [#123], and **twice** at the gate [#7b] [F2].")).toEqual([
      { t: "text", text: "Yes: the Lobby at 10:03 AM " },
      { t: "cite", ref: "123", footage: false },
      { t: "text", text: ", and " },
      { t: "bold", text: "twice" },
      { t: "text", text: " at the gate " },
      { t: "cite", ref: "7b", footage: false },
      { t: "text", text: " " },
      { t: "cite", ref: "F2", footage: true },
      { t: "text", text: "." },
    ]);
    expect(parseInline("no handles [#] [x1] here")).toEqual([{ t: "text", text: "no handles [#] [x1] here" }]);
    expect(citations("[#1] then [F1] and [#1] again, [#12a]")).toEqual(["1", "F1", "12a"]);
  });

  it("paragraphs and lists", () => {
    const blocks = parseAnswer("Two visits today.\n\n- [#5] Lobby, 9:00 AM\n- [#9] Gate, 6:10 PM\nThat's all.\n## Note\nquiet night");
    expect(blocks.map((b) => b.type)).toEqual(["p", "ul", "p"]);
    expect(blocks[1].type === "ul" && blocks[1].items.length).toBe(2);
    expect(blocks[2].type === "p" && blocks[2].parts).toEqual([{ t: "text", text: "That's all. Note quiet night" }]);
    expect(parseAnswer("")).toEqual([]);
    expect(parseAnswer("1. first\n2) second").map((b) => b.type)).toEqual(["ul"]);
  });

  it("finds the cited evidence; made-up handles find nothing", () => {
    const items = [src({ ref: "7a", event_id: 7 }), src({ ref: "7b", event_id: 7, server_id: "s2", server_name: "Bravo" }), src({ ref: "F1", kind: "footage" })];
    expect(citedSource("7b", items)?.server_id).toBe("s2");
    expect(citedSource("F1", items)?.kind).toBe("footage");
    expect(citedSource("99", items)).toBeUndefined();
    expect(citedSource("7a", undefined)).toBeUndefined();
  });
});

describe("Sources", () => {
  it("groups by camera, newest sighting first, the rest under Other", () => {
    const groups = groupSources([
      src({ ref: "1", camera: "Lobby", ts: 100 }),
      src({ ref: "2", camera: "Gate", ts: 300, server_name: "Bravo" }),
      src({ ref: "3", camera: "Lobby", ts: 200 }),
      src({ kind: "note", text: "recorded continuously: Lobby" }),
      src({ ref: "4", kind: "journey", cameras: ["Lobby", "Gate"], ts: 50 }),
    ]);
    expect(groups.map((g) => g.camera)).toEqual(["Gate", "Lobby", "Lobby → Gate", "Other"]);
    expect(groups[1].items.map((x) => x.ref)).toEqual(["3", "1"]);
    expect(groups[3].items[0].kind).toBe("note");
    expect(groupSources([])).toEqual([]);
  });

  it("says which servers were checked", () => {
    expect(serversLine([{ server_id: "a", server_name: "Alpha", status: "ok" }, { server_id: "b", server_name: "Bravo", status: "ok" }])).toBe("All 2 servers checked");
    expect(serversLine([{ server_id: "a", server_name: "Alpha", status: "ok" }])).toBe("1 server checked");
    expect(serversLine([{ server_id: "a", server_name: "Alpha", status: "ok" }, { server_id: "c", server_name: "Charlie", status: "offline" },
      { server_id: "d", server_name: "Delta", status: "timeout" }])).toBe("1 of 3 servers checked (Charlie offline, Delta not answering)");
  });
});

describe("conversations", () => {
  it("newest activity first, then the newer id", () => {
    const t = (id: number, updated_at: number) => ({ id, updated_at });
    expect(orderThreads([t(1, 10), t(2, 30), t(3, 30), t(4, 20)]).map((x) => x.id)).toEqual([3, 2, 4, 1]);
  });

  it("Enter sends, Shift+Enter is a new line", () => {
    expect(sendKey({ key: "Enter", shiftKey: false })).toBe(true);
    expect(sendKey({ key: "Enter", shiftKey: true })).toBe(false);
    expect(sendKey({ key: "Enter", shiftKey: false, isComposing: true })).toBe(false);
    expect(sendKey({ key: "a", shiftKey: false })).toBe(false);
  });

  it("a question prefilled from ?q=", () => {
    expect(askQuery("?q=Was%20anyone%20here%3F")).toBe("Was anyone here?");
    expect(askQuery("")).toBe("");
  });
});

describe("Find's question hint", () => {
  it("spots questions", () => {
    for (const t of ["Did anyone come in after hours?", "what happened overnight", "Was any camera offline today", "white van at the gate?",
      "anyone at the back door last night"]) expect(looksLikeQuestion(t), t).toBe(true);
  });
  it("not searches, short text or instructions", () => {
    for (const t of ["white pickup truck", "person at the back door", "show vans", "who?", "", "Quiet alerts tonight", "Rename cam3 to Dock",
      "is it"]) expect(looksLikeQuestion(t), t).toBe(false);
  });
});
