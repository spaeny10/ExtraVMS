/**
 * "Paint a region" quick filter: per camera, a 32x18 grid of cells the user painted over the picture.
 * Events carry `cells` (backend cells.py: which grid cells the object's feet crossed, as a 72-byte bitmap in
 * base64url); an event passes the filter when any painted cell is set in it — a bytewise AND, no request.
 * Regions live in localStorage (regionFilter.<cam>) and are shared by the Live and Timeline views.
 */
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { api, type Camera, type SiteApi, type Zone } from "./api";
import { promptDialog, toast } from "./ui";

export const GRID_W = 32, GRID_H = 18, NBYTES = (GRID_W * GRID_H) / 8;

export function decodeCells(s: string): Uint8Array {
  const out = new Uint8Array(NBYTES);
  try {
    const bin = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4));
    for (let i = 0; i < Math.min(NBYTES, bin.length); i++) out[i] = bin.charCodeAt(i);
  } catch { /* malformed: empty */ }
  return out;
}

export function encodeCells(b: Uint8Array): string {
  let bin = "";
  for (let i = 0; i < b.length; i++) bin += String.fromCharCode(b[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export const cellIndex = (col: number, row: number) => row * GRID_W + col;
export const hasCell = (b: Uint8Array, i: number) => (b[i >> 3] & (1 << (i & 7))) !== 0;
export function setCell(b: Uint8Array, i: number, on: boolean) {
  if (on) b[i >> 3] |= 1 << (i & 7); else b[i >> 3] &= ~(1 << (i & 7));
}
export const isEmpty = (b: Uint8Array) => b.every((x) => x === 0);
export const countCells = (b: Uint8Array) => { let n = 0; for (let i = 0; i < GRID_W * GRID_H; i++) if (hasCell(b, i)) n++; return n; };

export function regionMatches(eventCells: string | null | undefined, region: Uint8Array): boolean {
  if (!eventCells) return false;
  const e = decodeCells(eventCells);
  for (let i = 0; i < NBYTES; i++) if (e[i] & region[i]) return true;
  return false;
}

/** Open events have only a partial path yet: keep them visible until they close. */
export function regionPass(e: { status: string; cells?: string | null; ptz_preset?: string | null }, region: Uint8Array | undefined): boolean {
  // a region is painted on the home view: an event while a PTZ camera was turned away can't match it
  return !region || e.status === "open" || (!e.ptz_preset && regionMatches(e.cells, region));
}

// ---------------------------------------------------------------- store (localStorage-backed, shared by views)

const KEY = "regionFilter.";
let state: Record<string, Uint8Array> = load();
const listeners = new Set<() => void>();

function load(): Record<string, Uint8Array> {
  const out: Record<string, Uint8Array> = {};
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k?.startsWith(KEY)) {
        const cam = k.slice(KEY.length);
        const b = decodeCells(localStorage.getItem(k) ?? "");
        if (!isEmpty(b)) out[cam] = b;
      }
    }
  } catch { /* private mode */ }
  return out;
}

export const regions = {
  get: () => state,
  set(cam: string, bits: Uint8Array | null) {
    const next = { ...state };
    if (bits && !isEmpty(bits)) next[cam] = bits; else delete next[cam];
    state = next;
    try {
      if (next[cam]) localStorage.setItem(KEY + cam, encodeCells(next[cam])); else localStorage.removeItem(KEY + cam);
    } catch { /* private mode */ }
    listeners.forEach((l) => l());
  },
  subscribe(l: () => void) { listeners.add(l); return () => { listeners.delete(l); }; },
};

export const useRegions = () => useSyncExternalStore(regions.subscribe, regions.get);
export const useRegion = (cam: string) => useRegions()[cam];

// ---------------------------------------------------------------- region-scoped fetches, one per camera

/** "cam=<encoded region>,…" over the painted cameras (sorted by the caller): what the feed fetches, per camera. */
export const regionFetchKey = (keys: readonly string[], map: Record<string, Uint8Array>) =>
  keys.filter((k) => map[k]).map((k) => `${k}=${encodeCells(map[k])}`).join(",");

/** Per camera: the region its events were fetched for, and those events. */
export type RegionResults<T> = ReadonlyMap<string, { region: string; items: T[] }>;

/** Which cameras of `fetchKey` need a fetch (none yet, or fetched for another region) and the results still worth
 *  keeping (cameras still painted; a repainted one keeps its old events until the new ones arrive, filtered on the
 *  client meanwhile). Repainting one camera refetches only that camera. */
export function regionFetchPlan<T>(have: RegionResults<T>, fetchKey: string): { keep: RegionResults<T>; stale: [string, string][] } {
  const wanted = fetchKey ? fetchKey.split(",").map((kv) => kv.split("=") as [string, string]) : [];
  const cams = new Set(wanted.map(([cam]) => cam));
  const keep = new Map([...have].filter(([cam]) => cams.has(cam)));
  return { keep: keep.size === have.size ? have : keep, stale: wanted.filter(([cam, region]) => have.get(cam)?.region !== region) };
}

/** The events of every painted camera, each fetched by `fetchOne(cam, region)` once its stroke settles (400 ms), and
 *  again only when that camera's region changes. `fetchOne` should not throw (catch to []). */
export function useRegionResults<T>(fetchKey: string, fetchOne: (cam: string, region: string) => Promise<T[]>): T[] {
  const [have, setHave] = useState<RegionResults<T>>(() => new Map());
  const haveRef = useRef(have);
  haveRef.current = have;
  const fetchRef = useRef(fetchOne);
  fetchRef.current = fetchOne;
  useEffect(() => {
    const { keep, stale } = regionFetchPlan(haveRef.current, fetchKey);
    if (keep !== haveRef.current) setHave(keep);   // a cleared region drops its events now
    if (!stale.length) return;
    let alive = true;
    const t = setTimeout(() => {   // painting changes the region cell by cell: fetch once the stroke settles
      Promise.all(stale.map(([cam, region]) => fetchRef.current(cam, region).then((items) => [cam, region, items] as const)))
        .then((got) => {
          if (!alive) return;
          setHave((prev) => {
            const next = new Map(prev);
            for (const [cam, region, items] of got) next.set(cam, { region, items });
            return next;
          });
        });
    }, 400);
    return () => { alive = false; clearTimeout(t); };
  }, [fetchKey]);
  return useMemo(() => [...have.values()].flatMap((v) => v.items), [have]);
}

// ---------------------------------------------------------------- painted cells -> a zone polygon

/** Orthogonal outline of the largest 4-connected painted component (holes ignored). Points in 0..1. */
export function cellsToPolygon(b: Uint8Array): { points: [number, number][]; components: number } {
  const seen = new Uint8Array(GRID_W * GRID_H);
  const comps: number[][] = [];
  for (let i = 0; i < GRID_W * GRID_H; i++) {
    if (!hasCell(b, i) || seen[i]) continue;
    const comp: number[] = []; const stack = [i]; seen[i] = 1;
    while (stack.length) {
      const c = stack.pop()!; comp.push(c);
      const r = Math.floor(c / GRID_W), k = c % GRID_W;
      for (const [dr, dk] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
        const rr = r + dr, kk = k + dk;
        if (rr < 0 || rr >= GRID_H || kk < 0 || kk >= GRID_W) continue;
        const j = rr * GRID_W + kk;
        if (hasCell(b, j) && !seen[j]) { seen[j] = 1; stack.push(j); }
      }
    }
    comps.push(comp);
  }
  if (!comps.length) return { points: [], components: 0 };
  const comp = new Set(comps.sort((x, y) => y.length - x.length)[0]);
  const inComp = (r: number, k: number) => r >= 0 && r < GRID_H && k >= 0 && k < GRID_W && comp.has(r * GRID_W + k);
  // boundary edges as directed segments (clockwise around the filled cells), then chain them into a loop
  const edges = new Map<string, [number, number]>(); // "x,y" -> next corner
  for (const c of comp) {
    const r = Math.floor(c / GRID_W), k = c % GRID_W;
    if (!inComp(r - 1, k)) edges.set(`${k},${r}`, [k + 1, r]);           // top edge, left -> right
    if (!inComp(r, k + 1)) edges.set(`${k + 1},${r}`, [k + 1, r + 1]);   // right edge, top -> bottom
    if (!inComp(r + 1, k)) edges.set(`${k + 1},${r + 1}`, [k, r + 1]);   // bottom edge, right -> left
    if (!inComp(r, k - 1)) edges.set(`${k},${r + 1}`, [k, r]);           // left edge, bottom -> top
  }
  // start at the top-left corner of the top-left cell (guaranteed on the outer boundary)
  const first = [...comp].reduce((a, c) => (c < a ? c : a));
  let cur: [number, number] = [first % GRID_W, Math.floor(first / GRID_W)];
  const loop: [number, number][] = [];
  const startKey = `${cur[0]},${cur[1]}`;
  for (let guard = 0; guard < edges.size + 1; guard++) {
    loop.push(cur);
    const nxt = edges.get(`${cur[0]},${cur[1]}`);
    if (!nxt) break;
    cur = nxt;
    if (`${cur[0]},${cur[1]}` === startKey) break;
  }
  // drop collinear points
  const pts = loop.filter((p, i) => {
    const a = loop[(i - 1 + loop.length) % loop.length], c = loop[(i + 1) % loop.length];
    return !((a[0] === p[0] && p[0] === c[0]) || (a[1] === p[1] && p[1] === c[1]));
  });
  return { points: pts.map(([x, y]) => [x / GRID_W, y / GRID_H] as [number, number]), components: comps.length };
}

/** Promote the painted region to a named place (zone type "area") on the camera; `site` = the camera's server. */
export async function saveAsPlace(camera: Camera, bits: Uint8Array, site: SiteApi = api): Promise<boolean> {
  const { points, components } = cellsToPolygon(bits);
  if (points.length < 3) { toast.error("Paint an area first"); return false; }
  const name = await promptDialog("Name this place", { message: components > 1 ? `Only the largest painted patch is used (${components} separate patches painted).` : "Events that walk into it will be tagged with this name, and Ask/Find will understand it.", label: "Name", confirmLabel: "Save place" });
  if (!name?.trim()) return false;
  const zone: Zone = { name: name.trim(), type: "area", points };
  try {
    await site.saveCamera({ ...camera, zones: [...(camera.zones ?? []), zone] });
    await site.applyZones(camera.id);
    toast.success(`Saved "${zone.name}" as a named place on ${camera.name}`);
    return true;
  } catch (e) { toast.error(e); return false; }
}
