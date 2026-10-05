/**
 * Tab strips that scroll sideways on a phone (a Site's tabs, Site → Settings, Customer): which edge hides tabs (the
 * strip fades there, hub.css .scroll-tabs) and where to scroll so the current tab is in view. The math is pure
 * (tabStrip.test.ts); useTabStrip wires it to the element.
 */
import { useEffect, useRef, useState } from "react";

export type EdgeFade = { left: boolean; right: boolean };

/** Which edges of a horizontally scrolling strip have tabs beyond them (1 px of slack for fractional widths). */
export function edgeFade(scrollLeft: number, clientWidth: number, scrollWidth: number): EdgeFade {
  return { left: scrollLeft > 1, right: scrollLeft + clientWidth < scrollWidth - 1 };
}

export const fadeClass = (f: EdgeFade) => [f.left ? "fade-l" : "", f.right ? "fade-r" : ""].filter(Boolean).join(" ");

/**
 * The strip's scrollLeft that centres a tab (clamped to what the strip can scroll). `tabLeft` is the tab's offset from
 * the strip's visible left edge (getBoundingClientRect difference), so it already includes the current scroll.
 */
export function centreTab(scrollLeft: number, clientWidth: number, scrollWidth: number, tabLeft: number, tabWidth: number): number {
  const want = scrollLeft + tabLeft - (clientWidth - tabWidth) / 2;
  return Math.round(Math.max(0, Math.min(scrollWidth - clientWidth, want)));
}

/**
 * Pass `ref` to the strip (a callback ref, so a strip that renders after a loading state is picked up) and add
 * `className` to it. The current tab (`.active`) is scrolled into view whenever `active` changes (only the strip
 * scrolls, never the page); the fade classes follow scrolling and resizing.
 */
export function useTabStrip<T extends HTMLElement = HTMLDivElement>(active: string) {
  const [el, ref] = useState<T | null>(null);
  const [fade, setFade] = useState<EdgeFade>({ left: false, right: false });
  useEffect(() => {
    if (!el) return;
    const update = () => {
      const n = edgeFade(el.scrollLeft, el.clientWidth, el.scrollWidth);
      setFade((p) => (p.left === n.left && p.right === n.right ? p : n));
    };
    update();
    el.addEventListener("scroll", update, { passive: true });
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(update) : null;
    ro?.observe(el);
    return () => { el.removeEventListener("scroll", update); ro?.disconnect(); };
  }, [el]);
  const shown = useRef<T | null>(null);   // the strip the current tab was last scrolled into: a new one jumps, no animation
  useEffect(() => {
    const tab = el?.querySelector<HTMLElement>(".active");
    const smooth = shown.current === el;
    shown.current = el;
    if (!el || !tab || el.scrollWidth <= el.clientWidth) return;
    const s = el.getBoundingClientRect(), t = tab.getBoundingClientRect();
    const left = centreTab(el.scrollLeft, el.clientWidth, el.scrollWidth, t.left - s.left, t.width);
    if (Math.abs(left - el.scrollLeft) > 1) el.scrollTo({ left, behavior: smooth ? "smooth" : "auto" });
  }, [el, active]);
  return { ref, className: `scroll-tabs ${fadeClass(fade)}`.trim() };
}
