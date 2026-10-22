/**
 * A digest's (or a SOC shift report's) text as paragraphs, bullets and bold instead of raw "*" / "**" (digestText.ts;
 * no raw HTML: everything is React text). With `onOpen`, each event reference [#22909] the hub tied to its server is a
 * link that opens the event viewer in place; its href (middle-click) is the Site Timeline at the event.
 */
import type { EventRef } from "./eventOpen";
import { type DigestInline, type DigestRef, digestBlocks, refEvent, refHref } from "./digestText";
import { inPlace } from "./nav";

export function DigestText({ text, refs, onOpen, className }: {
  text: string; refs?: readonly DigestRef[] | null; onOpen?: (ev: EventRef) => void; className?: string;
}) {
  const blocks = digestBlocks(text, refs);
  const inline = (parts: DigestInline[]) => parts.map((p, i) => {
    if (p.t === "text") return <span key={i}>{p.text}</span>;
    if (p.t === "bold") return <strong key={i}>{p.text}</strong>;
    const label = p.footage ? `[${p.ref}]` : `#${p.ref}`;
    if (!p.link || !onOpen) return <span key={i} className="muted">{label}</span>;
    const link = p.link;
    return <a key={i} className="digest-ref" href={refHref(link)} onClick={inPlace(() => onOpen(refEvent(link)))} title="Show the clip and synopsis">{label}</a>;
  });
  return (
    <div className={`digest-text${className ? ` ${className}` : ""}`}>
      {blocks.map((b, i) => (b.type === "p" ? <p key={i}>{inline(b.parts)}</p>
        : <ul key={i}>{b.items.map((it, j) => <li key={j}>{inline(it)}</li>)}</ul>))}
    </div>
  );
}
