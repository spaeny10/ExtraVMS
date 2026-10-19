/**
 * The Hosts page at fleet scale (hosts/HostsPage.tsx, hosts/InstancesTable.tsx), the pure parts: the tab and filters
 * kept in the URL's query, the API parameters they become (the hub filters, sorts and pages: hub hostlist.py), the
 * attention strip's lines, a host row's mini bars, and which selected instances a bulk change applies to. vitest
 * covers it (fleetList.test.ts).
 */
import type { CentralInstance, CentralListParams, FleetSummary, HostCapacity, HostListParams, HostProblem, InstanceProblem } from "../api";
import { capacityBars, fmtGB } from "../central";

export const PAGE_SIZE = 50;
export const HOSTS_TABS = ["hosts", "instances"] as const;
export type HostsTab = (typeof HOSTS_TABS)[number];
export const HOST_STATUSES = ["online", "offline", "agent", "attention"] as const;
export const HOST_SORTS = ["name", "load", "free", "instances"] as const;
export const INSTANCE_STATES = ["running", "provisioning", "failed", "deleting"] as const;
export const INSTANCE_MODES = ["vpn", "forward", "bridge"] as const;
export const INSTANCE_PROBLEMS = ["any", "down", "storage", "queue", "image", "cameras"] as const;
export const INSTANCE_SORTS = ["created", "site", "customer", "host", "storage", "cameras", "queue"] as const;

export const HOST_STATUS_LABEL: Record<(typeof HOST_STATUSES)[number], string> = { online: "Online", offline: "Offline", agent: "Older agent", attention: "Needs attention" };
export const HOST_SORT_LABEL: Record<(typeof HOST_SORTS)[number], string> = { name: "Name", load: "Load", free: "Free storage", instances: "Instances" };
export const STATE_LABEL: Record<(typeof INSTANCE_STATES)[number], string> = { running: "Running", provisioning: "Provisioning", failed: "Failed", deleting: "Removing" };
export const MODE_LABEL: Record<(typeof INSTANCE_MODES)[number], string> = { vpn: "SpeedFusion VPN", forward: "Port forwarding", bridge: "Axiom Bridge" };
export const PROBLEM_LABEL: Record<(typeof INSTANCE_PROBLEMS)[number], string> = {
  any: "Any problem", down: "Not running", storage: "Storage ≥ 90 %", queue: "Queue backing up", image: "Other image", cameras: "Cameras down",
};
export const HOST_PROBLEM_LABEL: Record<HostProblem, string> = { offline: "Offline", agent: "Older agent", queue: "Queue alert" };
export const INSTANCE_SORT_LABEL: Record<(typeof INSTANCE_SORTS)[number], string> = {
  created: "Newest", site: "Site", customer: "Customer", host: "Host", storage: "Storage %", cameras: "Cameras", queue: "Queue",
};
/** What a sort starts with when no order is picked: the biggest first for numbers, A-Z for names (as the hub does). */
const DESC_FIRST = new Set(["created", "storage", "cameras", "queue", "load", "free", "instances"]);
export const defaultOrder = (sort: string) => (DESC_FIRST.has(sort) ? "desc" : "asc");

/** The page's state in the query string: ?tab=instances&problem=storage&sort=storage&page=2 (blank = default). */
export type ListQuery = {
  tab: HostsTab; q: string; page: number; sort: string; order: "" | "asc" | "desc";
  /** Hosts tab */
  status: string;
  /** Instances tab */
  state: string; host: string; mode: string; problem: string;
};
export const NO_QUERY: ListQuery = { tab: "hosts", q: "", page: 1, sort: "", order: "", status: "", state: "", host: "", mode: "", problem: "" };

const oneOf = (list: readonly string[], v: string | null) => (v && list.includes(v) ? v : "");

/**
 * The query string → the page's state; unknown values fall back to the defaults (a stale link still opens).
 * `fixedTab`: a page with one list only (a host's own page lists its instances): the query's tab is ignored.
 */
export function parseQuery(search: string, fixedTab?: HostsTab): ListQuery {
  const p = new URLSearchParams(search);
  const tab = fixedTab ?? ((oneOf(HOSTS_TABS, p.get("tab")) || "hosts") as HostsTab);
  const page = Math.floor(Number(p.get("page")));
  return {
    tab, q: (p.get("q") ?? "").slice(0, 200), page: Number.isFinite(page) && page > 1 ? page : 1,
    sort: oneOf(tab === "hosts" ? HOST_SORTS : INSTANCE_SORTS, p.get("sort")), order: oneOf(["asc", "desc"], p.get("order")) as ListQuery["order"],
    status: tab === "hosts" ? oneOf(HOST_STATUSES, p.get("status")) : "",
    state: tab === "instances" ? oneOf(INSTANCE_STATES, p.get("state")) : "",
    host: tab === "instances" ? (p.get("host") ?? "").slice(0, 24) : "",
    mode: tab === "instances" ? oneOf(INSTANCE_MODES, p.get("mode")) : "",
    problem: tab === "instances" ? oneOf(INSTANCE_PROBLEMS, p.get("problem")) : "",
  };
}

/** The page's state → "?tab=instances&problem=down" ("" for the defaults); the inverse of parseQuery (same `fixedTab`). */
export function querySearch(q: ListQuery, fixedTab?: HostsTab): string {
  const p = new URLSearchParams();
  if (q.tab !== "hosts" && !fixedTab) p.set("tab", q.tab);
  for (const k of ["q", "status", "state", "host", "mode", "problem", "sort", "order"] as const) if (q[k].trim()) p.set(k, q[k].trim());
  if (q.page > 1) p.set("page", String(q.page));
  const s = p.toString();
  return s ? `?${s}` : "";
}

/** A change to the filters goes back to page 1 (unless it is the page itself that changes). */
export const withQuery = (q: ListQuery, change: Partial<ListQuery>): ListQuery => ({ ...q, page: 1, ...change });

/** A tab with nothing filtered (switching tabs drops the other tab's filters). */
export const tabQuery = (tab: HostsTab): ListQuery => ({ ...NO_QUERY, tab });

const offset = (q: ListQuery) => (q.page - 1) * PAGE_SIZE;
const opt = (v: string) => v.trim() || undefined;

export function hostParams(q: ListQuery): HostListParams {
  return { q: opt(q.q), status: opt(q.status), sort: opt(q.sort), order: opt(q.order), limit: PAGE_SIZE, offset: offset(q) };
}

/** `fixedHost`: one host's page lists only its instances, whatever the query says. Always paged (limit set). */
export function instanceParams(q: ListQuery, fixedHost?: string): CentralListParams {
  return { q: opt(q.q), state: opt(q.state), host: fixedHost ?? opt(q.host), mode: opt(q.mode), problem: opt(q.problem),
    sort: opt(q.sort), order: opt(q.order), limit: PAGE_SIZE, offset: offset(q) };
}

/** "51–100 of 120" ("None" when nothing matches). */
export function pageText(page: number, shown: number, total: number): string {
  if (!total || !shown) return total ? `None on page ${page} of ${total}` : "None";
  const from = (page - 1) * PAGE_SIZE + 1;
  return `${from}–${from + shown - 1} of ${total}`;
}
export const lastPage = (total: number) => Math.max(1, Math.ceil(total / PAGE_SIZE));

// ---- the attention strip

export type AttentionItem = { key: string; count: number; text: string; tone: "bad" | "warn"; title?: string; to: ListQuery };

const n = (count: number, one: string, many = `${one}s`) => `${count} ${count === 1 ? one : many}`;

/**
 * The strip's lines, worst first, only those with something to count; [] = all well. Each opens the list it counts
 * (a tab and a filter): parseQuery(querySearch(to)) gives the same filter back, so activeAttention can light it up.
 */
export function attentionItems(s: FleetSummary | null | undefined): AttentionItem[] {
  if (!s) return [];
  const h = s.hosts, i = s.instances;
  const items: AttentionItem[] = [];
  const add = (key: string, count: number, text: string, tone: AttentionItem["tone"], to: Partial<ListQuery>, title?: string) => {
    if (count > 0) items.push({ key, count, text, tone, title, to: { ...NO_QUERY, ...to } });
  };
  add("offline", h.offline, `${n(h.offline, "host")} offline`, "bad", { tab: "hosts", status: "offline" });
  add("down", i.down, `${n(i.down, "instance")} not running`, "bad", { tab: "instances", problem: "down" },
    "Failed, its container stopped on the host, or its server not connected to the hub");
  add("storage", i.storage, `${n(i.storage, "instance")} at 90 % storage or more`, "warn", { tab: "instances", problem: "storage", sort: "storage" });
  add("queue", i.queue, `${n(i.queue, "detection queue")} backing up`, "warn", { tab: "instances", problem: "queue", sort: "queue" },
    "YOLO verify queue above 20 events, or growing over the last 15 minutes");
  add("agent", h.agent, `${n(h.agent, "host")} on an older agent`, "warn", { tab: "hosts", status: "agent" },
    s.newest_agent ? `Older than ${s.newest_agent}, the newest agent in the fleet` : undefined);
  add("image", i.image, `${n(i.image, "instance")} on another image`, "warn", { tab: "instances", problem: "image" },
    "Its container runs another build (Docker image ID) than its host creates new instances from");
  add("cameras", i.cameras, `${n(s.cameras_down, "camera")} down at ${n(i.cameras, "instance")}`, "warn", { tab: "instances", problem: "cameras" });
  return items;
}

/** The strip's line for the filter shown now (lit up; a second click clears it). */
export function activeAttention(item: AttentionItem, q: ListQuery): boolean {
  const t = item.to;
  return q.tab === t.tab && (t.tab === "hosts" ? q.status === t.status : q.problem === t.problem);
}

// ---- a host row

export type MiniBar = { pct: number; text: string };

/** CPU, RAM, the busiest GPU and the fullest disk, as one bar each (null = not reported); the text lists every card / disk. */
export function hostMiniBars(c: HostCapacity | null | undefined): Record<"cpu" | "ram" | "gpu" | "disk", MiniBar | null> {
  const bars = capacityBars(c);
  const top = (prefix: string): MiniBar | null => {
    const mine = bars.filter((b) => b.label.startsWith(prefix));
    if (!mine.length) return null;
    const worst = mine.reduce((a, b) => (b.pct > a.pct ? b : a));
    return { pct: worst.pct, text: mine.map((b) => (prefix === "GPU" || prefix === "Disk" ? `${b.label}: ${b.text}` : b.text)).join("\n") };
  };
  return { cpu: top("CPU"), ram: top("RAM"), gpu: top("GPU"), disk: top("Disk") };
}

export const barTone = (pct: number) => (pct >= 90 ? "hot" : pct >= 75 ? "warm" : "");

/** How many of the instance's cameras are not streaming while its server is online (0 otherwise). */
export function camerasDown(c: Pick<CentralInstance, "camera_count" | "cameras_online" | "server_online" | "problems">): number {
  return c.problems?.includes("cameras") ? Math.max(0, (c.camera_count ?? 0) - (c.cameras_online ?? 0)) : 0;
}

/**
 * Why a "down" instance is down, for its row's badge: its container's state on the host, or its server not connected.
 * null when its State already says it (failed) or it is not down.
 */
export function downText(c: Pick<CentralInstance, "state" | "host_state" | "problems">): string | null {
  if (!c.problems?.includes("down") || c.state === "failed") return null;
  return c.host_state && c.host_state !== "running" ? `Container ${c.host_state}` : "Server not connected";
}

/** "sha256:4f1c2a9e…" → "4f1c2a9e" (null stays null). */
export const shortImageId = (id: string | null | undefined) => (id ? id.replace(/^sha256:/, "").slice(0, 12) : null);

/** The image badge's tooltip: the tags (what was asked for) and the IDs (what runs) of the instance and its host. */
export function imageTitle(c: Pick<CentralInstance, "image" | "image_id" | "host_image" | "host_image_id">): string {
  const one = (tag?: string | null, id?: string | null) => `${tag ?? "?"}${id ? ` (${shortImageId(id)})` : ""}`;
  return `Runs ${one(c.image, c.image_id)}; its host creates ${one(c.host_image, c.host_image_id)}`;
}

/** The problems worth a badge on the row (its Storage and Cameras cells already say full and "N down" in their own words). */
export const rowProblems = (c: Pick<CentralInstance, "problems">): InstanceProblem[] => (c.problems ?? []).filter((p) => p !== "storage" && p !== "cameras");

// ---- bulk changes (the Instances table's checkboxes)

export type BulkAction = "limit" | "storage";
export type BulkPlan = { apply: CentralInstance[]; skip: { ci: CentralInstance; why: string }[] };

const gone = (c: CentralInstance) => c.state === "deleting" || c.state === "deleted";

/**
 * Which of the selected instances a bulk change reaches, and why the rest are left alone. Camera limit: any live one
 * (hub only, no host round trip). Storage only ever goes up: an instance at or above `gb` already, or on an offline
 * host, is skipped (a lower quota deletes recordings, so it stays a one-instance change).
 */
export function bulkPlan(action: BulkAction, selected: CentralInstance[], gb?: number | null): BulkPlan {
  const plan: BulkPlan = { apply: [], skip: [] };
  for (const c of selected) {
    const why = gone(c) ? "being removed"
      : action === "storage" && c.host_online === false ? "its host is offline"
      : action === "storage" && gb != null && c.quota_gb >= gb ? `already ${fmtGB(c.quota_gb)}`
      : null;
    if (why) plan.skip.push({ ci: c, why });
    else plan.apply.push(c);
  }
  return plan;
}

/** The bulk dialog's value check: a camera limit (blank = none) or a storage quota in whole GB, at least 10. */
export function bulkValueError(action: BulkAction, v: string): string | null {
  const t = v.trim();
  if (action === "storage") return /^\d+$/.test(t) && Number(t) >= 10 ? null : "A whole number of GB, at least 10";
  return null;   // the camera limit is checked by parseCameraLimit (central.ts)
}
