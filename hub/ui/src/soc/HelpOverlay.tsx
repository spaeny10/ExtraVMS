/**
 * `?`: every shortcut, from the same KEYMAP the chord reducer obeys plus the disposition chords from the catalog,
 * so the help can't drift from what the keys do. Focus moves into the dialog and back to where it was on close.
 */
import { useEffect, useRef } from "react";
import { KEYMAP, chordLabel } from "./chords";
import type { DispositionGroup } from "./types";

export function HelpOverlay({ groups, onClose }: { groups: DispositionGroup[]; onClose: () => void }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    box.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" || e.key === "?") { e.preventDefault(); onClose(); } };
    addEventListener("keydown", onKey);
    return () => { removeEventListener("keydown", onKey); prev?.focus?.(); };
  }, [onClose]);
  const sections = [...new Set(KEYMAP.map((k) => k.group))];
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div ref={box} className="modal soc-help" role="dialog" aria-modal="true" aria-labelledby="soc-help-title" tabIndex={-1} onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <h2 id="soc-help-title">Keyboard</h2>
          <button className="ghost" onClick={onClose} aria-label="Close">✕</button>
        </header>
        <div className="soc-help-grid">
          {sections.map((g) => (
            <section key={g}>
              <h3>{g}</h3>
              <dl>{KEYMAP.filter((k) => k.group === g).map((k) => <div key={k.keys} className="soc-help-row"><dt><kbd>{k.keys}</kbd></dt><dd>{k.label}</dd></div>)}</dl>
            </section>
          ))}
          <section>
            <h3>Resolve (claimed incidents)</h3>
            {groups.length === 0 && <p className="muted small">The disposition list hasn't loaded.</p>}
            <dl>{groups.flatMap((g) => g.dispositions.filter((d) => d.selectable && d.key).map((d) => (
              <div key={d.code} className="soc-help-row"><dt><kbd>{chordLabel(g.key, d.key)}</kbd></dt><dd>{g.label}: {d.label}{d.needs_notes ? " (notes)" : ""}</dd></div>
            )))}</dl>
            <p className="muted small">Press the group letter, then the digit within 2.5 s.</p>
          </section>
        </div>
      </div>
    </div>
  );
}
