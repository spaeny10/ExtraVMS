/**
 * Traffic cameras (backend traffic.py, traffic_api.py): Milesight traffic cameras read plates and vehicle attributes on
 * the camera and push each read here. The reply types and the pure logic of the Plates page (Plates.tsx) and the Camera
 * tab's Traffic section (TrafficSetup.tsx), kept apart from the components so they are unit-tested.
 *
 * Kansas issues rear plates only, so most reads have no plate; the camera's state (`region`) and direction are weak
 * hints (a Kansas plate read as Ohio, a rear view called "Approach"), so they are always shown as "reported", never as
 * fact. A near match (one character off) on a list is "possible".
 */
import { formatPlate } from "./plate";

export type PlateList = "allow" | "deny" | "watch";
export const PLATE_LISTS: readonly PlateList[] = ["deny", "watch", "allow"];
export type ImageKind = "plate" | "vehicle" | "full" | "violation";
export type Direction = "approach" | "away";

/** One read the camera pushed (traffic.public). `best`: the pass's most confident read (a vehicle is one pass). */
export type Read = {
  id: number; camera_id: string; ts: number; received_at: number; kind: string;
  plate: string | null; plate_raw: string | null; confidence: number | null; region: string | null;
  plate_type: string | null; plate_color: string | null;
  vehicle_type: string | null; vehicle_color: string | null; vehicle_brand: string | null;
  direction: Direction | null; speed: number | null; alarm: string | null; detection_region: string | null;
  box: [number, number, number, number] | null; plate_box: [number, number, number, number] | null;
  images: ImageKind[]; pass_id: number | null; best: boolean;
  list: PlateList | null; list_match: "exact" | "near" | null; list_entry: number | null; event_id: number | null;
  /** where the vehicle picture came from: the camera's push, or cut from the recording (the camera sent none) */
  vehicle_source?: "camera" | "recording" | null;
  /** Otso's own look at the vehicle crop (Ask Otso, or on its own in the background), kept beside the camera's read;
   *  otso_plate when it read one; otso_look what the vehicle is in Otso's words ("white Ford F-150 pickup truck, ladder rack") */
  otso?: OtsoRead | null; otso_plate?: string | null; otso_look?: string | null;
};
export type OtsoRead = {
  at: number; result: "read" | "unreadable" | "skipped"; plate?: string; plate_raw?: string; region?: string;
  confidence?: "medium" | "high"; reason?: string; model?: string;
  /** Ask Otso's notes on the plate's history (counted on the server, traffic.plate_notes), and the plate they are about */
  about?: string; notes?: string[];
  /** who asked: a person (Ask Otso) or Otso on its own (it looks at each vehicle when it is free); older answers have none */
  by?: "person" | "otso";
  /** what the vehicle is, in Otso's words */
  vehicle?: { vehicle_type?: string; color?: string; make?: string; model?: string; details?: string };
};
/** POST /api/traffic/reads/{id}/plate: Otso's answer, the read as now stored, and the list the plate is on (if any). */
export type ReadPlateResult = {
  result: OtsoRead["result"]; plate: string | null; plate_raw: string | null; region: string | null;
  confidence: "medium" | "high" | null; reason: string | null; read: Read;
  list: { list: PlateList; match: "exact" | "near"; plate: string; note: string } | null;
  about: string | null; notes: string[];
};
/** GET /api/traffic/reads/{id}: the read and every read of its pass, oldest first. */
export type ReadDetail = Read & { pass?: Read[] };
/** GET /api/traffic/reads: newest first; `near` = plates one character off the search, seen in the same window. */
export type ReadsPage = { reads: Read[]; near: string[]; /** a description search: the words matched */ look?: string[] };
export type ReadsQuery = {
  camera?: string; since?: number; until?: number; q?: string; type?: string; color?: string; direction?: Direction;
  list?: PlateList | "any"; min_conf?: number; plates?: boolean; all?: boolean; before_id?: number; limit?: number;
  /** also reads that aren't a vehicle (no vehicle type and no or a weak plate: a sign read at night); a search shows them anyway */
  noise?: boolean;
  /** a description ("white pickup ladder rack"): every word in Otso's description or the camera's colour, type or make */
  look?: string;
};

export type CountBucket = {
  /** the local hour's (or day's) start, epoch seconds */
  t: number; total: number; plates: number;
  direction: Partial<Record<Direction | "unknown", number>>; type: Record<string, number>;
};
/** GET /api/traffic/counts: only buckets with vehicles in them (one vehicle = one pass). */
export type Counts = { bucket: "hour" | "day"; since: number; until: number; buckets: CountBucket[] };
export type CountsQuery = { camera?: string; since?: number; until?: number; bucket?: "hour" | "day" };

export type TrafficCameraStatus = {
  camera_id: string; name: string; enabled: boolean; last_push: number | null; last_read: number | null; last_status: number | null;
  reads_today: number; plates_today: number; zone_offset_s: number | null;
};
/** GET /api/traffic/status: each camera with a traffic setup; a set-up camera silent for silent_after_s raises an alert. */
export type TrafficStatus = { cameras: TrafficCameraStatus[]; silent_after_s: number };

export type Category = "employee" | "contractor" | "visitor" | "resident" | "delivery" | "trespass" | "police" | "other";
/** When an entry applies: always (null), while the Site's SOC monitoring is armed, or set hours in the Site's zone
 *  (days 0-6, Monday 0; `to` <= `from` runs past midnight). */
export type EntrySchedule = { when: "soc" } | { when: "hours"; days: number[]; from: string; to: string };
/** The vehicle a listed plate belongs on: another one raises the cloned-plate alarm. */
export type ExpectedVehicle = { vehicle_type?: string; color?: string; make?: string; model?: string };
export type ListEntry = {
  id: number; plate: string; list: PlateList; note: string; expires_at: number | null; created_at: number; by: string | null;
  expired: boolean; last_seen: number | null; last_camera: string | null;
  /** options (older servers: absent) */
  category?: Category | null; schedule?: EntrySchedule | null; action?: "alarm" | "mark" | null; priority?: "low" | "medium" | "high" | null;
  cameras?: string[] | null; expect?: ExpectedVehicle | null; updated_at?: number | null; updated_by?: string | null;
  /** what the page shows: a partial plate (? and *), the latest read, passes in 7 / 30 days, Otso's latest description,
   *  other entries for the same plate and the list that applies instead of this one, in force now, what a hit does */
  pattern?: boolean; last_read_id?: number | null; seen_7d?: number; seen_30d?: number; look?: string | null;
  conflicts?: { id: number; list: PlateList; plate: string }[]; overridden_by?: PlateList | null; active?: boolean;
  effect?: { alarm: boolean; priority: "low" | "medium" | "high" };
};
/** GET /api/traffic/lists/{id}: one entry opened. */
export type ListEntryDetail = ListEntry & {
  sightings: Read[]; notes: string[];
  alarms: { id: number; ts: number; priority: string | null; camera_id: string; text: string | null }[];
  /** the vehicle Otso saw last on this plate (to save as the one it belongs on) */
  vehicle: (ExpectedVehicle & { details?: string; read_id: number; ts: number }) | null;
};
type EntryOptions = {
  category?: Category | null; schedule?: EntrySchedule | null; action?: "alarm" | "mark" | null;
  priority?: "low" | "medium" | "high" | null; cameras?: string[] | null; expect?: ExpectedVehicle | null;
};
export type ListEntryIn = { plate: string; list: PlateList; note?: string; expires_at?: number | null } & EntryOptions;
/** Only the keys sent change; null clears. */
export type ListEntryEdit = { note?: string; expires_at?: number | null; clear_expiry?: boolean } & EntryOptions;
export type ImportResult = { results: { line: number; plate: string; list: string; ok: boolean; error?: string }[]; added: number };

/** A push slot on the camera (it has three); `ours` = pointed at this server. The URL's secret is hidden. */
export type PushSlot = { index: number; enabled: boolean; ours: boolean; url: string; event_mask: number | null; status_report: boolean };
export type CameraTrafficServer = {
  enabled: boolean; set_at: number | null; last_push: number | null; last_read: number | null; last_status: number | null;
  zone_offset_s: number | null; silent_after_s: number;
  /** plate pattern alerts (older servers: none) */
  alerts?: TrafficAlerts; learn_days?: number; circling?: { passes: number; minutes: number };
};
/** A camera's plate pattern alerts (backend traffic.check_patterns): new and unusual_time only while the Site's SOC
 *  monitoring is armed, unless `when` is "always". */
export type TrafficAlerts = { circling: boolean; new: boolean; unusual_time: boolean; when: "armed" | "always" };
/** GET / PUT /api/cameras/{id}/traffic. sd_card null = the camera didn't say. */
export type CameraTraffic =
  | { supported: false; reason: string; server: CameraTrafficServer }
  | {
    supported: true;
    camera: { model: string | null; firmware: string | null; licensed: boolean; lpr_enabled: boolean; country: string | null;
      slots: PushSlot[]; sd_card: boolean | null; resend: boolean };
    server: CameraTrafficServer;
  };

// ---------------------------------------------------------------- one read

/** "1691 AJN", or "No plate" (most reads: an approaching car in a rear-plate state). */
export const plateText = (r: Pick<Read, "plate">): string => (r.plate ? formatPlate(r.plate) : "No plate");

/** The plate to show and act on: the camera's, else the one Otso read when a person asked (Ask Otso). */
export const knownPlate = (r: Pick<Read, "plate" | "otso_plate">): string | null => r.plate ?? r.otso_plate ?? null;

/** When Otso answered, and who asked: "asked, 3:04 PM" or "on its own, 3:04 PM". */
export function otsoWhen(o: OtsoRead, time = (t: number) => new Date(t * 1000).toLocaleTimeString()): string {
  return `${o.by === "otso" ? "on its own" : "asked"}, ${time(o.at)}`;
}

// words that make a search a description, not a plate ("FORD" alone could be a vanity plate, so one word is a
// description only when it is one of these)
const DESCRIBE_WORDS = new Set([
  "car", "suv", "truck", "van", "bus", "motorcycle", "pickup", "sedan", "semi", "trailer", "ladder", "rack", "toolbox", "logo",
  "lettering", "flatbed", "white", "black", "gray", "grey", "silver", "red", "blue", "green", "brown", "yellow", "orange", "gold",
  "beige", "tan", "ford", "chevy", "chevrolet", "toyota", "honda", "nissan", "dodge", "ram", "gmc", "jeep", "tesla", "hyundai",
  "kia", "subaru",
]);

/** Is the search box text a vehicle description ("white pickup ladder rack", "red f150") rather than a plate ("1691AJN",
 *  "ABC 123")? A known description word, or two or more words not like a plate typed with a space. */
export function isDescription(text: string): boolean {
  const words = text.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (words.some((w) => DESCRIBE_WORDS.has(w.replace(/s$/, "")))) return true;
  if (words.length < 2) return false;
  // "ABC 123" / "1691 AJN": short pieces of letters and digits, one with a digit
  if (words.length === 2 && words.every((w) => /^[a-z0-9]{1,5}$/.test(w)) && words.some((w) => /\d/.test(w))) return false;
  return words.some((w) => !/\d/.test(w));
}

/** Otso's answer as one line: "Otso: 407 MFL · KS", "Otso: probably 3416 AFR", or why it couldn't ("Otso couldn't read it"). */
export function otsoText(o: OtsoRead | null | undefined): string {
  if (!o) return "";
  // medium: a soft crop read character by character, one may be off (087 MAB came back "087 MAU"): "probably"
  if (o.result === "read" && o.plate) return o.confidence === "high" ? `Otso: ${formatPlate(o.plate, o.region)}` : `Otso: probably ${formatPlate(o.plate, o.region)}`;
  if (o.result === "skipped") return "Otso had no picture to read";
  return "Otso couldn't read it";
}

/** Confidence bands: 0.6 puts a plate on the vehicle's event (traffic_attach_min_conf), 0.8 is the camera's "high". */
export function confBand(c: number | null | undefined): "high" | "medium" | "low" | null {
  if (c == null) return null;
  return c >= 0.8 ? "high" : c >= 0.6 ? "medium" : "low";
}
/** "89 %" (empty without a plate). */
export const confText = (c: number | null | undefined): string => (c == null ? "" : `${Math.round(c * 100)} %`);

/** "state reported KS": the camera's guess, never a fact (KS, CA and OK all came up on one Kansas road). */
export const stateText = (region: string | null | undefined): string => (region ? `state reported ${region}` : "");
/** "going away (reported)": the camera called rear views "Approach", so this is a hint too. */
export function directionText(d: Direction | null | undefined): string {
  return d === "away" ? "going away (reported)" : d === "approach" ? "approaching (reported)" : "";
}

/** "White GMC Truck": colour, brand, type, whichever the camera gave. */
export function vehicleText(r: Pick<Read, "vehicle_color" | "vehicle_brand" | "vehicle_type">): string {
  return [r.vehicle_color, r.vehicle_brand, r.vehicle_type].filter((x) => x && x !== "-").join(" ");
}

const LIST_NAME: Record<PlateList, string> = { deny: "Deny list", watch: "Watch list", allow: "Allowed" };
export const listName = (l: PlateList) => LIST_NAME[l];

/** The read's list badge: deny red, watch amber, allow green; a near match is "possible" (one character off). */
export function listBadge(r: Pick<Read, "list" | "list_match">): { text: string; className: string; title: string } | null {
  if (!r.list) return null;
  const near = r.list_match === "near";
  const what = r.list === "allow" ? "the allow list" : `the ${r.list} list`;
  return {
    text: near ? `Possible · ${LIST_NAME[r.list].toLowerCase()}` : LIST_NAME[r.list],
    className: `list-badge ${r.list}${near ? " near" : ""}`,
    title: near ? `One character off a plate on ${what}: check the plate crop` : `On ${what}`,
  };
}

/** A near plate from a search, as the chip that searches it: "Possible: ABC 1284". */
export const nearChip = (p: string) => `Possible: ${formatPlate(p)}`;

/** "just now", "2 min ago", "3 h ago", "2 days ago"; "never" for none. */
export function timeAgo(ts: number | null | undefined, now = Date.now() / 1000): string {
  if (!ts) return "never";
  const s = Math.max(0, now - ts);
  if (s < 45) return "just now";
  if (s < 5400) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400 * 1.5) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

// ---------------------------------------------------------------- search

export type Period = "today" | "24h" | "7d" | "30d" | "all";
export const PERIODS: [Period, string][] = [["today", "Today"], ["24h", "24 h"], ["7d", "7 days"], ["30d", "30 days"], ["all", "All"]];
/** The search's `since` for a period (local midnight for today); undefined = everything kept. */
export function periodSince(p: Period, now = Date.now()): number | undefined {
  if (p === "all") return undefined;
  if (p === "today") { const d = new Date(now); d.setHours(0, 0, 0, 0); return d.getTime() / 1000; }
  return now / 1000 - { "24h": 1, "7d": 7, "30d": 30 }[p] * 86400;
}

/** Letters and digits, upper case: what the server matches on ("abc-1234" -> "ABC1234"). */
export const normPlate = (s: string) => s.toUpperCase().replace(/[^A-Z0-9]/g, "");

/** The vehicle types and colours to filter by: the camera's usual words, then any others seen (as the camera wrote them). */
export const VEHICLE_TYPES = ["Car", "SUV", "Truck", "Van", "Bus", "Motorcycle"];
export const VEHICLE_COLORS = ["White", "Black", "Gray", "Silver", "Red", "Blue", "Green", "Brown", "Yellow", "Orange"];
export function filterOptions(base: readonly string[], seen: (string | null | undefined)[]): string[] {
  const out = [...base];
  const have = new Set(base.map((x) => x.toLowerCase()));
  for (const s of seen) {
    if (!s || s === "-" || have.has(s.toLowerCase())) continue;
    have.add(s.toLowerCase());
    out.push(s);
  }
  return out;
}

// ---------------------------------------------------------------- counts

export type Bar = { t: number; total: number; plates: number; label: string };

/**
 * Every hour (or day) from `since` to `until` as a bar, the empty ones too (the server sends only hours with vehicles).
 * Steps in local time like the server's buckets, so a daylight-saving day has 23 or 25 hours.
 */
export function countBars(c: Pick<Counts, "bucket" | "since" | "until" | "buckets">): Bar[] {
  const have = new Map(c.buckets.map((b) => [b.t, b]));
  const d = new Date(c.since * 1000);
  d.setMinutes(0, 0, 0);
  if (c.bucket === "day") d.setHours(0);
  const out: Bar[] = [];
  for (let n = 0; d.getTime() / 1000 < c.until && n < 1000; n++) {
    const t = d.getTime() / 1000;
    const b = have.get(t);
    out.push({ t, total: b?.total ?? 0, plates: b?.plates ?? 0, label: barLabel(t, c.bucket) });
    if (c.bucket === "day") d.setDate(d.getDate() + 1); else d.setHours(d.getHours() + 1);
  }
  return out;
}

/** "3 PM" for an hour, "Tue 28" for a day. */
export function barLabel(t: number, bucket: "hour" | "day"): string {
  const d = new Date(t * 1000);
  return bucket === "day" ? d.toLocaleDateString(undefined, { weekday: "short", day: "numeric" }) : d.toLocaleTimeString(undefined, { hour: "numeric" });
}

/** "1,204 vehicles · 512 with a plate (43 %)" over the bars. */
export function countsSummary(bars: Pick<Bar, "total" | "plates">[]): string {
  const total = bars.reduce((s, b) => s + b.total, 0);
  const plates = bars.reduce((s, b) => s + b.plates, 0);
  if (!total) return "No vehicles";
  return `${total.toLocaleString()} vehicle${total === 1 ? "" : "s"} · ${plates.toLocaleString()} with a plate (${Math.round((plates / total) * 100)} %)`;
}

// ---------------------------------------------------------------- push health

/** A camera set up whose last push (a read or a status report) is older than silent_after_s, or that never pushed. */
export function isSilent(c: { enabled: boolean; last_push: number | null }, silentAfterS: number, now = Date.now() / 1000): boolean {
  return c.enabled && (!c.last_push || now - c.last_push > silentAfterS);
}

/** "Last push 2 min ago", or why there is none. */
export function pushText(c: { enabled: boolean; last_push: number | null }, now = Date.now() / 1000): string {
  if (!c.enabled) return "Not set up";
  return c.last_push ? `Last push ${timeAgo(c.last_push, now)}` : "No push yet";
}

// ---------------------------------------------------------------- lists

/** An expiry date field ("2026-11-01") -> the end of that local day; "" -> null (never). */
export function expiryFromDate(s: string): number | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (!m) return null;
  return new Date(+m[1], +m[2] - 1, +m[3], 23, 59, 59).getTime() / 1000;
}
/** An expiry back to the date field's value. */
export function dateFromExpiry(ts: number | null | undefined): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
/** "never", "until Nov 1", "expired Nov 1". */
export function expiryText(e: Pick<ListEntry, "expires_at" | "expired">): string {
  if (!e.expires_at) return "never";
  const day = new Date(e.expires_at * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return e.expired ? `expired ${day}` : `until ${day}`;
}

/** A plate typed for a list, as the server keeps it: letters and digits, and ? (one character) and * (any run) for a
 *  partial plate. */
export const normEntryPlate = (s: string) => s.toUpperCase().replace(/[^A-Z0-9?*]/g, "").replace(/\*+/g, "*");
export const isPartial = (s: string) => /[?*]/.test(s);

/** What is wrong with a plate typed into the add form (the server says 422 for anything else that isn't a plate). */
export function plateProblem(s: string): string | null {
  const n = normEntryPlate(s);
  if (!n) return "Type a plate: letters and digits (? for one unknown character, * for any)";
  const known = n.replace(/[?*]/g, "").length;
  if (isPartial(n)) return known < 2 ? "A partial plate needs at least 2 known characters" : n.length > 12 ? "Too long" : null;
  if (n.length < 2) return "A plate has at least 2 characters";
  if (n.length > 10) return "A plate has at most 10 characters";
  return null;
}

/** Is this plate already on that list (the server keeps one entry per plate and list)? */
export const onList = (entries: Pick<ListEntry, "plate" | "list">[], plate: string, list: PlateList) =>
  entries.some((e) => e.plate === normEntryPlate(plate) && e.list === list);

export const CATEGORIES: readonly Category[] = ["employee", "contractor", "visitor", "resident", "delivery", "trespass", "police", "other"];
const CATEGORY_NAME: Record<Category, string> = {
  employee: "Employee", contractor: "Contractor", visitor: "Visitor", resident: "Resident", delivery: "Delivery",
  trespass: "Trespass notice", police: "Police lookout", other: "Other",
};
export const categoryName = (c: Category) => CATEGORY_NAME[c] ?? c;
export const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** "13:30" -> "1:30 PM". */
export function clock12(hhmm: string): string {
  const [h, m] = hhmm.split(":").map(Number);
  return `${h % 12 || 12}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
}

/** "Mon-Fri", "Sat, Sun", "Every day", "Mon, Wed, Fri". */
export function daysText(days: number[]): string {
  const d = [...new Set(days)].filter((x) => x >= 0 && x <= 6).sort();
  if (d.length === 7) return "Every day";
  if (d.length >= 3 && d.every((x, i) => i === 0 || x === d[i - 1] + 1)) return `${DAY_NAMES[d[0]]}-${DAY_NAMES[d[d.length - 1]]}`;
  return d.map((x) => DAY_NAMES[x]).join(", ");
}

/** "Always", "During SOC hours", "Mon-Fri, 10:00 PM-6:00 AM". */
export function scheduleText(s: EntrySchedule | null | undefined): string {
  if (!s) return "Always";
  if (s.when === "soc") return "During SOC hours";
  return `${daysText(s.days)}, ${clock12(s.from)}-${clock12(s.to)}`;
}

/** What a hit does: "High alarm", "Medium alarm", "Marks the read". */
export function effectText(e: Pick<ListEntry, "list" | "action" | "priority">): string {
  const alarm = (e.action ?? (e.list === "allow" ? "mark" : "alarm")) === "alarm";
  if (!alarm) return "Marks the read";
  const p = e.priority ?? (e.list === "deny" ? "high" : e.list === "watch" ? "medium" : "low");
  return `${p[0].toUpperCase()}${p.slice(1)} alarm`;
}

/** "white Ford F-150 pickup truck". */
export const expectText = (v: ExpectedVehicle | null | undefined) =>
  v ? [v.color, v.make, v.model, v.vehicle_type].filter(Boolean).join(" ") : "";

/** Why an entry may do nothing ("Does nothing: ... the watch list applies instead"), or that another list has it too. */
export function conflictText(e: Pick<ListEntry, "overridden_by" | "conflicts" | "list">): string {
  if (e.overridden_by) return `Does nothing: this plate is also on the ${e.overridden_by} list, which applies instead`;
  const others = (e.conflicts ?? []).map((c) => c.list).filter((l) => l !== e.list);
  return others.length ? `Also on the ${[...new Set(others)].join(" and ")} list (this entry applies)` : "";
}

/** Passes seen: "3 this week · 12 in 30 days", "not seen in 30 days". */
export function seenText(e: Pick<ListEntry, "seen_7d" | "seen_30d">): string {
  if (!e.seen_30d) return "not seen in 30 days";
  return `${e.seen_7d ?? 0} this week · ${e.seen_30d} in 30 days`;
}

// ---- the entry form (add and edit)

export type ExpiryChoice = "" | "24h" | "7d" | "30d" | "date";
export type EntryDraft = {
  plate: string; list: PlateList; note: string; category: "" | Category; expiry: ExpiryChoice; date: string;
  when: "always" | "soc" | "hours"; days: number[]; from: string; to: string;
  action: "" | "alarm" | "mark"; priority: "" | "low" | "medium" | "high"; cameras: string[]; expect: ExpectedVehicle;
};
export const NO_ENTRY_DRAFT: EntryDraft = {
  plate: "", list: "deny", note: "", category: "", expiry: "", date: "", when: "always", days: [0, 1, 2, 3, 4], from: "22:00", to: "06:00",
  action: "", priority: "", cameras: [], expect: {},
};

/** The expiry chosen: none, 24 hours / 7 / 30 days from now, or the end of a picked day. */
export function expiryOf(d: Pick<EntryDraft, "expiry" | "date">, now = Date.now() / 1000): number | null {
  if (d.expiry === "24h") return now + 86400;
  if (d.expiry === "7d") return now + 7 * 86400;
  if (d.expiry === "30d") return now + 30 * 86400;
  if (d.expiry === "date") return expiryFromDate(d.date);
  return null;
}

/** The form -> the request body (every option sent, null for the defaults, so an edit clears what was unset). */
export function draftToBody(d: EntryDraft, now = Date.now() / 1000): ListEntryIn {
  const expect = Object.fromEntries(Object.entries(d.expect).filter(([, v]) => v && String(v).trim())) as ExpectedVehicle;
  return {
    plate: normEntryPlate(d.plate), list: d.list, note: d.note.trim(), expires_at: expiryOf(d, now),
    category: d.category || null,
    schedule: d.when === "always" ? null : d.when === "soc" ? { when: "soc" } : { when: "hours", days: d.days, from: d.from, to: d.to },
    action: d.action || null, priority: d.action === "mark" ? null : d.priority || null,
    cameras: d.cameras.length ? d.cameras : null, expect: Object.keys(expect).length ? expect : null,
  };
}

/** An entry -> the form, to edit it (a set expiry shows as its date). */
export function entryToDraft(e: ListEntry): EntryDraft {
  const s = e.schedule;
  return {
    plate: e.plate, list: e.list, note: e.note, category: e.category ?? "", expiry: e.expires_at ? "date" : "", date: dateFromExpiry(e.expires_at),
    when: s ? s.when : "always", days: s?.when === "hours" ? s.days : NO_ENTRY_DRAFT.days,
    from: s?.when === "hours" ? s.from : NO_ENTRY_DRAFT.from, to: s?.when === "hours" ? s.to : NO_ENTRY_DRAFT.to,
    action: e.action ?? "", priority: e.priority ?? "", cameras: e.cameras ?? [], expect: e.expect ?? {},
  };
}

/** Search the lists: plate (or part of it), note, category, list, Otso's description, the expected vehicle. */
export function filterEntries<T extends ListEntry>(entries: T[], q: string): T[] {
  const words = q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return entries;
  return entries.filter((e) => {
    const hay = [e.plate, e.note, e.category ? categoryName(e.category) : "", listName(e.list), e.look, expectText(e.expect)]
      .filter(Boolean).join(" ").toLowerCase();
    return words.every((w) => hay.includes(w) || (normPlate(w) !== "" && hay.includes(normPlate(w).toLowerCase())));
  });
}

// ---- CSV (export and import): plate, list, category, note, expires (YYYY-MM-DD)

const CSV_HEAD = ["plate", "list", "category", "note", "expires", "applies", "on_a_hit", "last_seen"];
const csvCell = (v: string) => (/[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);

export function entriesToCsv(entries: ListEntry[]): string {
  const day = (ts: number | null | undefined) => (ts ? dateFromExpiry(ts) : "");
  const rows = entries.map((e) => [e.plate, e.list, e.category ?? "", e.note, day(e.expires_at), scheduleText(e.schedule), effectText(e), day(e.last_seen)]);
  return [CSV_HEAD, ...rows].map((r) => r.map((c) => csvCell(String(c ?? ""))).join(",")).join("\n") + "\n";
}

/** One CSV line into cells (quotes and doubled quotes). */
function csvCells(line: string): string[] {
  const out: string[] = [];
  let cur = "", q = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (q) {
      if (c === '"' && line[i + 1] === '"') { cur += '"'; i++; } else if (c === '"') q = false; else cur += c;
    } else if (c === '"') q = true;
    else if (c === ",") { out.push(cur); cur = ""; } else cur += c;
  }
  out.push(cur);
  return out.map((x) => x.trim());
}

/** A CSV file -> rows to import ({plate, list, category?, note?, expires_at?}): a header row is used when it names a
 *  "plate" column (an export reads back), else the columns are plate, list, category, note, expires. */
export function csvToRows(text: string): { plate: string; list: string; category?: string; note?: string; expires_at?: number | null }[] {
  const lines = text.replace(/^﻿/, "").split(/\r?\n/).filter((l) => l.trim());
  if (!lines.length) return [];
  const first = csvCells(lines[0]).map((c) => c.toLowerCase());
  const head = first.includes("plate") ? first : null;
  const col = (name: string, fallback: number) => (head ? head.indexOf(name) : fallback);
  const idx = { plate: col("plate", 0), list: col("list", 1), category: col("category", 2), note: col("note", 3), expires: col("expires", 4) };
  return (head ? lines.slice(1) : lines).map((l) => {
    const c = csvCells(l);
    const get = (i: number) => (i >= 0 ? c[i] ?? "" : "");
    return {
      plate: get(idx.plate), list: get(idx.list).toLowerCase(), category: get(idx.category).toLowerCase() || undefined,
      note: get(idx.note) || undefined, expires_at: expiryFromDate(get(idx.expires)),
    };
  });
}

/** A list change's error: a viewer's 403 says who may; anything else is the server's own words. */
export function listError(e: unknown, detail: (e: unknown) => string): string {
  if (/^(?:Error: )?403\b/.test(String(e instanceof Error ? e.message : e))) return "Changing plate lists needs the operator role";
  return detail(e);
}

// ---------------------------------------------------------------- camera setup

/** Our push slot on the camera (pointed at this server), if any. */
export const ourSlot = (slots: PushSlot[]) => slots.find((s) => s.ours) ?? null;

/** The Traffic section's status line for our push: set up and enabled, set up but off on the camera, or not set up. */
export function slotText(slots: PushSlot[]): string {
  const s = ourSlot(slots);
  // a free slot is off and empty (traffic.configure takes only such a one)
  if (!s) return slots.some((x) => !x.enabled && !x.url) ? "Not set up (a push slot is free)" : "Not set up (all push slots are in use)";
  if (!s.enabled) return `Push slot ${s.index + 1} points here but is switched off on the camera`;
  return `Set up: push slot ${s.index + 1} sends every read here${s.status_report ? ", with a status report" : ""}`;
}
