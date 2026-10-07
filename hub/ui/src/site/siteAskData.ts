/**
 * Pure helpers for a Site's Ask tab (SiteAsk.tsx, hub/hub/site_ask.py):
 *  - parseAnswer: the answer's text as paragraphs and "- " lists, with **bold** and citations ([#123], [#123a], [F1])
 *    split out so the page renders them as chips;
 *  - citedSource: the merged evidence item a citation points at (its server, event id, camera and time);
 *  - groupSources: the Sources disclosure, by camera (newest sighting first), server only shown there;
 *  - orderThreads: conversations newest first;
 *  - looksLikeQuestion: Find's hint ("Looks like a question — Ask this site");
 *  - sendKey, askQuery: Enter sends (Shift+Enter is a new line); a question prefilled from ?q=;
 *  - pickAnswer, buildReferences, refCountsLine, clockIn: the References panel (the evidence of the selected answer,
 *    cited first) and its times in the Site's time zone.
 */
import type { SiteAskMessage, SiteAskServer, SiteAskSource, SiteAskThread } from "../api";
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

// ---------------------------------------------------------------- the References panel

/** An answer on the page: a stored message's id, or the one being written. */
export type AnswerKey = number | "pending";

/**
 * The answer the References panel follows: the one the user picked (while it is still on the page), else the one being
 * written, else the latest answer of the conversation; null when there is no answer yet.
 */
export function pickAnswer(messages: readonly Pick<SiteAskMessage, "id" | "role">[], pick: AnswerKey | null, pending: boolean): AnswerKey | null {
  if (pick === "pending" && pending) return "pending";
  if (typeof pick === "number" && messages.some((m) => m.id === pick && m.role === "assistant")) return pick;
  if (pending) return "pending";
  for (let i = messages.length - 1; i >= 0; i--) if (messages[i].role === "assistant") return messages[i].id;
  return null;
}

export type References = {
  /** events (with an id) and footage moments: thumbnail cards */
  cards: SiteAskSource[];
  /** journeys, recording gaps and briefings: one-line rows below the cards */
  rows: SiteAskSource[];
  /** the refs the answer cites that exist in its evidence */
  cited: Set<string>;
  events: number; footage: number; journeys: number; gaps: number;
};

/**
 * The selected answer's evidence for the References panel: what the answer cites first (in the order it cites them),
 * then the rest newest first. Recording notes ("recorded continuously: Lobby") are left out: they add nothing there.
 */
export function buildReferences(items: readonly SiteAskSource[] | undefined, text: string): References {
  const list = items ?? [];
  const refs = new Set(list.map((x) => x.ref).filter((r): r is string => !!r));
  const order = new Map(citations(text).filter((r) => refs.has(r)).map((r, i) => [r, i]));
  const rank = (x: SiteAskSource) => (x.ref != null ? order.get(x.ref) ?? Infinity : Infinity);
  const sorted = [...list].sort((a, b) => {
    const ra = rank(a), rb = rank(b);
    if (ra !== rb) return ra < rb ? -1 : 1;
    return (b.ts ?? -1) - (a.ts ?? -1);
  });
  const isCard = (x: SiteAskSource) => (x.kind === "event" && x.event_id != null) || (x.kind === "footage" && !!x.camera_id && x.ts != null);
  const cards = sorted.filter(isCard);
  const rows = sorted.filter((x) => !isCard(x) && x.kind !== "note");
  const n = (k: SiteAskSource["kind"]) => list.filter((x) => x.kind === k).length;
  return { cards, rows, cited: new Set(order.keys()), events: cards.filter((x) => x.kind === "event").length, footage: n("footage"),
    journeys: n("journey"), gaps: n("gap") };
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** "6 events · 2 footage moments · 1 journey"; "Nothing matched" when there is no evidence. */
export function refCountsLine(r: Pick<References, "events" | "footage" | "journeys" | "gaps">): string {
  const parts: string[] = [];
  if (r.events) parts.push(plural(r.events, "event"));
  if (r.footage) parts.push(plural(r.footage, "footage moment"));
  if (r.journeys) parts.push(plural(r.journeys, "journey"));
  if (r.gaps) parts.push(plural(r.gaps, "recording gap"));
  return parts.length ? parts.join(" · ") : "Nothing matched";
}

const dtf = new Map<string, Intl.DateTimeFormat>();
function fmt(tz: string | null | undefined, opts: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${tz ?? ""}|${JSON.stringify(opts)}`;
  let f = dtf.get(key);
  if (!f) {
    try { f = new Intl.DateTimeFormat("en-US", tz ? { ...opts, timeZone: tz } : opts); }
    catch { f = new Intl.DateTimeFormat("en-US", opts); }   // an unknown zone: the browser's own
    dtf.set(key, f);
  }
  return f;
}

/** "9:10 AM" today, "Oct 6 9:10 AM" on other days, in the Site's time zone (the browser's when the Site has none). */
export function clockIn(ts: number, tz: string | null | undefined, now = Date.now() / 1000): string {
  const day = fmt(tz, { year: "numeric", month: "numeric", day: "numeric" });
  const d = new Date(ts * 1000);
  const time = fmt(tz, { hour: "numeric", minute: "2-digit" }).format(d).replace(/ /g, " ");
  return day.format(d) === day.format(new Date(now * 1000)) ? time : `${fmt(tz, { month: "short", day: "numeric" }).format(d)} ${time}`;
}
