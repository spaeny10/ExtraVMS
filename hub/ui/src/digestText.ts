/**
 * The customer digest's text (hub/hub/digest.py) as blocks to render: the Site Ask answer renderer's small markdown
 * (parseAnswer: paragraphs, "- " lists, **bold**; never raw HTML) with each event reference [#22909] tied to its
 * server. The hub stores the references in the order they appear in the text ({server_id, event_id, camera_id,
 * location_id}: event ids are per server); the n-th [#id] of the text is the n-th reference when the ids agree, else
 * it stays plain text (an older digest without references, or one edited since).
 */
import type { EventRef } from "./eventOpen";
import { consoleHref, consoleTimelineHref } from "./nav";
import { type Block, type Inline, parseAnswer } from "./site/siteAskData";
import { siteTimelineHref } from "./timelineLink";

export type DigestRef = { server_id: string | null; event_id: number; camera_id?: string | null; location_id?: string | null };
export type DigestInline = Exclude<Inline, { t: "cite" }> | { t: "cite"; ref: string; footage: boolean; link: DigestRef | null };
export type DigestBlock = { type: "p"; parts: DigestInline[] } | { type: "ul"; items: DigestInline[][] };

/** The text's blocks with every [#id] matched to its stored reference (link null: shown as plain "#id"). */
export function digestBlocks(text: string, refs?: readonly DigestRef[] | null): DigestBlock[] {
  let n = 0;
  const tie = (parts: Inline[]): DigestInline[] => parts.map((p) => {
    if (p.t !== "cite") return p;
    if (p.footage || !/^\d+$/.test(p.ref)) return { ...p, link: null };
    const r = refs?.[n++];
    return { ...p, link: r && r.server_id && r.event_id === Number(p.ref) ? r : null };
  });
  return parseAnswer(text ?? "").map((b: Block): DigestBlock => (b.type === "p" ? { type: "p", parts: tie(b.parts) } : { type: "ul", items: b.items.map(tie) }));
}

/** The event viewer's target for a reference. */
export const refEvent = (r: DigestRef): EventRef => ({ server: r.server_id ?? "", id: r.event_id, location: r.location_id ?? null });

/** Where a reference's link points for middle-click / a new tab: the Site Timeline at the event, else the server's console. */
export function refHref(r: DigestRef): string {
  if (!r.server_id) return "#";
  if (!r.camera_id) return consoleHref(r.server_id);
  return r.location_id ? siteTimelineHref(r.location_id, r.server_id, r.camera_id, r.event_id) : consoleTimelineHref(r.server_id, { cam: r.camera_id, event: r.event_id });
}
