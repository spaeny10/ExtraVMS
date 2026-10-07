/**
 * Pure helpers for a Site's Ask tab (SiteAsk.tsx, hub/hub/site_ask.py):
 *  - parseAnswer: the answer's text as paragraphs and "- " lists, with **bold** and citations ([#123], [#123a], [F1])
 *    split out so the page renders them as chips;
 *  - citedSource: the merged evidence item a citation points at (its server, event id, camera and time);
 *  - groupSources: the Sources disclosure, by camera (newest sighting first), server only shown there;
 *  - orderThreads: conversations newest first;
 *  - looksLikeQuestion: Find's hint ("Looks like a question — Ask this site");
 *  - sendKey, askQuery: Enter sends (Shift+Enter is a new line); a question prefilled from ?q=.
 */
import type { SiteAskServer, SiteAskSource, SiteAskThread } from "../api";
import { looksLikeInstruction } from "../customer/fleetActions";

export type Inline = { t: "text"; text: string } | { t: "bold"; text: string } | { t: "cite"; ref: string; footage: boolean };
export type Block = { type: "p"; parts: Inline[] } | { type: "ul"; items: Inline[][] };

const CITE = /\[(#\d+[a-z]?|F\d+)\]/g;

/** One line's inline pieces: text, **bold** and citations. */
export function parseInline(line: string): Inline[] {
  const out: Inline[] = [];
  const push = (text: string, bold: boolean) => {
    if (!text) return;
    const last = out[out.length - 1];
    if (!bold && last?.t === "text") last.text += text;
    else out.push(bold ? { t: "bold", text } : { t: "text", text });
  };
  // bold first (a citation inside bold text is still a citation)
  const boldParts = line.split(/(\*\*[^*]+\*\*)/g);
  for (const bp of boldParts) {
    const bold = bp.length > 4 && bp.startsWith("**") && bp.endsWith("**");
    const text = bold ? bp.slice(2, -2) : bp;
    let at = 0;
    for (const m of text.matchAll(CITE)) {
      push(text.slice(at, m.index), bold);
      const ref = m[1].startsWith("#") ? m[1].slice(1) : m[1];
      out.push({ t: "cite", ref, footage: !m[1].startsWith("#") });
      at = (m.index ?? 0) + m[0].length;
    }
    push(text.slice(at), bold);
  }
  return out;
}

/** The answer as blocks: blank lines split paragraphs, "- " / "* " / "• " / "1. " lines make a list. */
export function parseAnswer(text: string): Block[] {
  const blocks: Block[] = [];
  const cur: { para: string[]; list: Inline[][] } = { para: [], list: [] };
  const flushPara = () => { if (cur.para.length) blocks.push({ type: "p", parts: parseInline(cur.para.join(" ")) }); cur.para = []; };
  const flushList = () => { if (cur.list.length) blocks.push({ type: "ul", items: cur.list }); cur.list = []; };
  for (const raw of text.replace(/\r/g, "").split("\n")) {
    const line = raw.trim();
    const item = /^(?:[-*•]|\d+[.)])\s+(.*)$/.exec(line);
    if (!line) { flushPara(); flushList(); continue; }
    if (item) { flushPara(); cur.list.push(parseInline(item[1])); continue; }
    flushList();
    cur.para.push(line.replace(/^#{1,6}\s+/, ""));   // a markdown heading reads as a paragraph
  }
  flushPara(); flushList();
  return blocks;
}

/** Every citation in the answer, in order, once each. */
export function citations(text: string): string[] {
  return [...new Set([...text.matchAll(CITE)].map((m) => (m[1].startsWith("#") ? m[1].slice(1) : m[1])))];
}

/** The evidence item a citation points at (undefined when the answer made one up: shown as plain text). */
export function citedSource(ref: string, items: readonly SiteAskSource[] | undefined): SiteAskSource | undefined {
  return items?.find((x) => x.ref === ref);
}

export type SourceGroup = { camera: string; items: SiteAskSource[] };
const TIMED = new Set(["event", "footage", "journey", "gap"]);

/**
 * The Sources disclosure: sightings grouped by camera (the camera with the newest sighting first, newest first inside),
 * then everything without a camera and time (recording notes, briefings) under "Other".
 */
export function groupSources(items: readonly SiteAskSource[]): SourceGroup[] {
  const groups = new Map<string, SiteAskSource[]>();
  const other: SiteAskSource[] = [];
  for (const x of items) {
    if (!TIMED.has(x.kind) || x.ts == null) { other.push(x); continue; }
    const cam = x.camera || (x.cameras?.length ? x.cameras.join(" → ") : "Unknown camera");
    const g = groups.get(cam);
    if (g) g.push(x); else groups.set(cam, [x]);
  }
  const out = [...groups.entries()].map(([camera, xs]) => ({ camera, items: [...xs].sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0)) }));
  out.sort((a, b) => (b.items[0].ts ?? 0) - (a.items[0].ts ?? 0) || a.camera.localeCompare(b.camera));
  if (other.length) out.push({ camera: "Other", items: other });
  return out;
}

/** "All 3 servers checked" / "2 of 3 servers checked (Charlie offline)": the Sources header. */
export function serversLine(servers: readonly SiteAskServer[]): string {
  const ok = servers.filter((s) => s.status === "ok").length;
  if (!servers.length) return "No servers";
  const missed = servers.filter((s) => s.status !== "ok").map((s) => `${s.server_name} ${s.status === "offline" ? "offline" : "not answering"}`);
  if (!missed.length) return servers.length === 1 ? "1 server checked" : `All ${servers.length} servers checked`;
  return `${ok} of ${servers.length} servers checked (${missed.join(", ")})`;
}

/** Conversations newest first (by last activity, then id). */
export function orderThreads<T extends Pick<SiteAskThread, "id" | "updated_at">>(threads: readonly T[]): T[] {
  return [...threads].sort((a, b) => b.updated_at - a.updated_at || b.id - a.id);
}

const QUESTION_START = /^\s*(how|what|what's|whats|when|where|who|whose|why|which|did|does|do|is|are|was|were|has|have|had|any|anything|anyone|anybody|somebody|someone|can you|could you|tell me|summarize|give me a summary)\b/i;

/**
 * Does Find's search text read as a question for Ask ("Did anyone come in after hours?", "what happened overnight")?
 * Searches ("white pickup truck", "show vans") and instructions (Customer › Actions) do not.
 */
export function looksLikeQuestion(text: string): boolean {
  const t = text.trim();
  if (t.length < 6 || looksLikeInstruction(t)) return false;
  if (t.endsWith("?")) return true;
  return QUESTION_START.test(t) && t.split(/\s+/).length >= 3;
}

/** Enter sends; Shift+Enter (a new line) and IME composition don't. */
export function sendKey(e: { key: string; shiftKey: boolean; isComposing?: boolean }): boolean {
  return e.key === "Enter" && !e.shiftKey && !e.isComposing;
}

/** The question prefilled from the URL (?q=…): placed in the box, never sent by itself. */
export function askQuery(search: string): string {
  return (new URLSearchParams(search).get("q") ?? "").slice(0, 1000);
}

export const SUGGESTIONS = [
  "What happened overnight?",
  "Anything unusual today?",
  "Did anyone come in after hours?",
  "Was any camera offline in the last 24 hours?",
];
