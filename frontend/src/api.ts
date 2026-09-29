import { connection } from "./ui";
import type { DashboardConfig } from "./dashboard/types";
/** include = detect only here; exclude = mask out; area = just a name for a place (never filters) */
export type Zone = { name: string; type?: "include" | "exclude" | "area"; points: [number, number][] };
/** [x, y, class, event_id, status] foot point of a recent detection */
export type DetectionPoint = [number, number, string, number, string];

/** Same rule as backend/nvr/zones.py: in an include zone (if any) and not in any exclude zone. */
export function zoneAllowed(x: number, y: number, zones: Zone[]): boolean {
  const inPoly = (pts: [number, number][]) => {
    let inside = false;
    for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      const [xi, yi] = pts[i], [xj, yj] = pts[j];
      if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi + 1e-12) + xi) inside = !inside;
    }
    return inside;
  };
  const valid = zones.filter((z) => z.points.length >= 3);
  const includes = valid.filter((z) => z.type === "include" || z.type === undefined);
  if (includes.length && !includes.some((z) => inPoly(z.points))) return false;
  return !valid.some((z) => z.type === "exclude" && inPoly(z.points));
}

/** A site rule checked by code after Qwen describes a vehicle (backend policy.py). */
export type SiteRule = { kind: "towing" | "entry"; asset?: string; area?: string; allowed: string[]; priority: "medium" | "high" };
export type BrokenRule = { kind: string; text: string; priority: string };

export type Camera = {
  id: string;
  name: string;
  host: string;
  onvif_port: number;
  rtsp_port: number;
  username: string;
  main_path: string;
  sub_path: string;
  enabled: number | boolean;
  zones: Zone[];
  retention_days: number | null;
  scene_notes: string;
  /** what Qwen describes on this camera (only detections inside its zones); null = site default (people) */
  synopsis_labels?: ("person" | "vehicle")[] | null;
  policies?: SiteRule[];
  retention_policy?: Partial<RetentionPolicy> | null;
  ptz_config?: PtzConfig | null;
  status?: {
    stream_ready: boolean;
    recording: boolean;
    tracks: string[];
    metadata?: boolean;
    metadata_last?: number;
    onvif_events?: boolean;
    health?: StreamHealth;
    ptz?: PtzStatus | null;
  };
};

/** PTZ camera state and settings (backend/nvr/ptz.py). */
export type PtzConfig = { home_token: string | null; home_name: string | null; return_home_min: number; relay_label: string; input_label: string };
export type PtzStatus = {
  available: boolean; pan_tilt?: boolean; at_home: boolean; moving: boolean; preset: string | null; preset_name: string | null;
  position: { x: number; y: number; zoom: number } | null; home_token: string | null; home_name: string | null; last_error: string | null;
  relay: { label: string; state: boolean | null; mode: "bistable" | "monostable"; changed_at: number | null } | null;
  input: { label: string; state: boolean | null; changed_at: number | null } | null;
};
export type PtzPreset = { token: string; name: string; system: boolean; is_home: boolean; known: boolean };
export type PtzInfo = {
  caps: { available: boolean; pan_tilt?: boolean; home_supported?: boolean; max_presets?: number; aux_commands?: string[]; tours?: boolean; relays?: number; inputs?: number } | null;
  status: PtzStatus; presets: PtzPreset[]; config: PtzConfig;
};

/** From MediaMTX metrics, sampled every 10 s (backend/nvr/health.py). */
export type StreamHealth = {
  sampled: boolean; bitrate_mbps: number | null; sub_bitrate_mbps: number | null; gb_per_day: number | null;
  stalled_s: number | null; frames_in_error_1h: number; metadata_reader: boolean | null; problems: string[];
};

export type EventStatus = "open" | "pending" | "verified" | "rejected" | "error" | "masked";
export type Threat = "none" | "low" | "medium" | "high";
/** How unusual an event is for its camera, from the learned baseline (backend/nvr/baseline.py). */
export type Anomaly = { score: number; parts: { time?: number; place?: number; dwell?: number }; reasons: string[]; learning: boolean };
export type BaselineCamera = { camera_id: string; days: number; events: Record<string, number>; learning: boolean; time_active: boolean; built_at: number };
export const UNUSUAL_MIN = 0.75;

export type Detection = {
  ts: number;
  cam_box: number[];
  yolo: { cls: string; conf: number; box: number[] }[];
  match: { cls: string; conf: number; box: number[] } | null;
  iou: number;
};

export type NvrEvent = {
  id: number;
  camera_id: string;
  track_id: string;
  camera_class: "person" | "vehicle";
  camera_conf: number | null;
  start_ts: number;
  end_ts: number | null;
  status: EventStatus;
  yolo_class: string | null;
  yolo_conf: number | null;
  yolo_hits: number | null;
  synopsis_pending?: boolean; // Qwen is queued or writing the synopsis right now
  policy?: BrokenRule | null; // the site rule this event breaks (policy.py)
  snapshot: string | null;
  clip: string | null;
  synopsis: string | null;
  threat: Threat | null;
  /** max(threat, unusualness level); an operator's "none" wins. Null until verified. */
  priority?: Threat | null;
  anomaly?: number | null;
  anomaly_json?: Anomaly | null;
  /** name of the watched person/vehicle this sighting matched */
  watched?: string | null;
  /** named areas (zones of type "area") the object walked into */
  areas?: { name: string; from: number; to: number }[] | null;
  cells?: string | null; // 32x18 grid cells the object crossed (region.ts filter)
  ptz_preset?: string | null; // PTZ camera turned away from home: preset name or "away"
  error: string | null;
  corrected_at?: number | null;
  feedback?: Feedback | null;
  locked?: number;
  lock?: Lock | null;
  journey_id?: number | null;
  journey_cameras?: number | null;
  score?: number;
  // detail only
  path?: number[][];
  rules?: { ts: number; topic: string; rule: string | null }[];
  detections?: { samples: Detection[]; keyframes: { file: string; ts: number; kind: string }[]; needed: number; time_shift_s?: number };
  synopsis_json?: Synopsis | null;
  synopsis_original?: Synopsis | null;
  clip_start?: number | null;
};

export type TimelineEvent = Pick<NvrEvent, "id" | "camera_class" | "yolo_class" | "yolo_conf" | "start_ts" | "end_ts" | "status" | "threat" | "priority" | "anomaly" | "journey_id" | "cells"> & {
  verdict: Verdict | null;
  has_synopsis: number;
};

export type RetentionPolicy = {
  continuous_days: number;
  pad_before_s: number;
  pad_after_s: number;
  keep: { person: boolean; qwen_analyzed: boolean; rule_events: boolean; feedback: boolean; vehicles_in_detect_zones: boolean };
  rule_topics: string[];
  min_free_gb: number;
};

export type RetentionStats = {
  cameras: {
    camera_id: string; name: string; policy: RetentionPolicy; continuous_gb: number; kept_gb: number; locked_gb: number;
    kept_files: number; continuous_days_on_disk: number; gb_per_day: number;
  }[];
  disk: { total_gb: number; free_gb: number };
  alert: { message: string; free_gb: number; floor_gb: number } | null;
  last_pass: Record<string, number | boolean>;
  dry_run: boolean;
  locks: number;
};

export type RetentionPreview = {
  camera_id: string; hours: number; segments: number; deferred: number; kept_minutes: number; deleted_minutes: number;
  freed_gb: number; kept_minutes_by_reason: Record<string, number>;
};

export type Lock = { id: number; camera_id: string; start_ts: number; end_ts: number; event_id: number | null; note: string; created_at: number };
export type KeptSpan = { start_ts: number; end_ts: number; reasons: string[]; score: number };

export type CameraLink = { cam_a: string; cam_b: string; min_s: number; max_s: number; one_way: boolean | number };
export type LinkSuggestion = { cam_a: string; cam_b: string; count: number; median_gap_s: number; min_s: number; max_s: number; configured: boolean };
export type JourneyLink = { id: number; a: number; b: number; gap_s: number; sim: number; status: string; confidence: string | null; reason: string | null };
export type Journey = {
  id: number; first_ts: number; last_ts: number; cameras: string[]; synopsis: string | null; dirty: number;
  events: Pick<NvrEvent, "id" | "camera_id" | "camera_class" | "start_ts" | "end_ts" | "synopsis" | "snapshot" | "status">[];
  links: JourneyLink[];
};

/** order: camera ids in display order (tiles and lanes); cameras not listed follow in their default order */
export type LayoutConfig = { visible: string[] | null; solo: string | null; order?: string[] | null };
export type Layout = { id: number; name: string; config: LayoutConfig; created_at: number; updated_at: number };

export type Synopsis = {
  summary: string;
  objects: { type: string; description: string }[];
  activity: string;
  threat_level: Threat;
  threat_reason?: string;
  tags: string[];
  /** which model wrote it (local 7B or the remote model) */
  model?: string | null;
};

export type Verdict = "correct" | "false_alarm" | "wrong_class";
export type Feedback = {
  rating?: "up" | "down" | null;
  reasons?: string[];
  verdict?: Verdict | null;
  correct_class?: string | null;
  note?: string | null;
  at?: number;
};

export type ChatMessage = {
  id: number;
  event_id: number;
  role: "user" | "assistant";
  content: string;
  frames: { file: string; t: number }[];
  at: number | null;
  saved: number;
  ts: number;
};

export type FeedbackStats = {
  synopses: number;
  corrected: number;
  up: number;
  down: number;
  reasons: Record<string, number>;
  verdicts: Record<string, Partial<Record<Verdict, number>>>;
};

/** A stretch of recorded footage matching a footage search (backend/nvr/footage.py). */
export type FootageMoment = { camera_id: string; ts: number; start: number; end: number; score: number; hits: number; tile: number; box: [number, number, number, number] };
export type FootageMatch = { matches: boolean; confidence: "low" | "medium" | "high"; seen: string };
export type FootageStatus = {
  db_mb: number; model_loaded: boolean;
  cameras: Record<string, { frames: number; oldest: number | null; newest: number | null; cursor: number | null; backlog_s: number }>;
};

/** How Find reads a query (backend assistant.parse_query). */
export type ParsedQuery = { since: number | null; until: number | null; time_label: string | null; text: string; footage_text: string | null; question: boolean };

/* ---- Home */
export type HomeCamera = { id: string; name: string; stream_ready: boolean; metadata: boolean; metadata_last: number | null; onvif_events: boolean; today: Record<string, number>; health: StreamHealth };
export type HomeData = {
  now: number; since: number; new_since: number; attention: NvrEvent[]; recent: NvrEvent[]; cameras: HomeCamera[];
  briefing: { id: number; headline: string; period_start: number; period_end: number; created_at: number } | null;
  disk: { free_gb: number; total_gb: number }; retention_alert: unknown; yolo_ready: boolean; vlm_ready: boolean;
  queues: { verify: number; synopsis: number }; backup: { at: number; path: string } | null; baseline: BaselineCamera[];
};

/* ---- People & vehicles */
export type IdentitySighting = Pick<NvrEvent, "id" | "camera_id" | "start_ts" | "end_ts" | "snapshot" | "synopsis" | "priority" | "anomaly" | "yolo_class">;
export type IdentityCluster = {
  key: string; kind: "person" | "vehicle"; identity_id: number | null; name: string | null; name_sim: number | null; fingerprinted: boolean; watch: boolean;
  sightings: number; first_ts: number; last_ts: number; on_site_s: number; cameras: string[]; cover: number; description: string | null;
  priority: Threat; unusual: boolean; events: IdentitySighting[];
};
export type NamedIdentity = { id: number; name: string; kind: string; notes: string; sightings: number; updated_at: number; watch: number | boolean; watch_note: string; looks?: number };
export type IdentitiesResult = { kind: string; since: number; until: number; clusters: IdentityCluster[]; sightings: number; named: NamedIdentity[] };

/** This site's link to the fleet hub (backend hub_agent.py). */
export type HubStatus = {
  enabled: boolean; hub_url: string; connected: boolean; enrolled: boolean; site_id: string | null; org: string | null;
  claim_code: string | null; claim_expires: number | null; last_error: string | null; last_heartbeat: number | null; vlm_managed: boolean;
};

export type SystemInfo = {
  recordings_disk: { total_gb: number; free_gb: number };
  retention_days: number;
  queues: { verify: number; synopsis: number };
  vlm_ready: boolean;
  vlm_model: string;
  yolo_ready: boolean;
  yolo_model: string;
  events: Record<string, number>;
  webrtc_port: number;
  backup?: { dir: string; last: { at: number; path: string; bytes: number; count: number } | null };
};

export type AdvisorFinding = {
  key: string; area: string; impact: "high" | "medium" | "low"; title: string; why: string; effect: string; steps: string[];
  apply: Record<string, unknown> | null; fingerprint: string; camera_id: string | null; camera: string | null;
};
export type AdvisorReport = {
  generated_at: number; cameras: number; summary: { text: string; model: string | null }; findings: AdvisorFinding[]; hidden: AdvisorFinding[];
  facts: { gb_per_day: number | null; vlm: { size_gb?: number | null; vram_gb?: number | null; queue?: number | null }; median_synopsis_s: number | null };
};
export type SiteDashboard = { id: number; name: string; config: DashboardConfig; created_at: number; updated_at: number };

/** URL prefix when this UI is served through the fleet hub ("/s/<site>"); empty on the site itself. */
export const BASE = /^\/s\/[A-Za-z0-9_-]+/.exec(typeof location === "undefined" ? "" : location.pathname)?.[0] ?? "";

const json = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

const qs = (params: Record<string, string | number | undefined | null>) =>
  new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "") as [string, string][],
  ).toString();

/**
 * All backend calls for one site. `api` is the site this page is served from (prefix BASE); the fleet hub's
 * dashboard builds one per site with makeApi("/s/<site>") so tiles and feeds can mix sites on one page.
 */
export function makeApi(base: string) {
  const req = async <T,>(url: string, init?: RequestInit): Promise<T> => {
    const r = await fetch(base + url, init);
    connection.data();
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  };
  /** NDJSON stream: one parsed object per line to onChunk. */
  const stream = async <C,>(url: string, init: RequestInit, onChunk: (c: C) => void) => {
    const r = await fetch(base + url, init);
    if (!r.ok || !r.body) throw new Error(`${r.status} ${await r.text()}`);
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl: number;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (line) onChunk(JSON.parse(line));
      }
    }
  };
  return {
  base,
  // ---- URLs (media, frames, playback, WebRTC signalling, live socket)
  media: (e: { id: number }, name: string) => `${base}/api/events/${e.id}/media/${name}`,
  frameUrl: (camera: string, t: number, w = 960, exact = false) =>
    `${base}/api/frame/${camera}?${qs({ t: t.toFixed(2), w, exact: exact ? "true" : undefined })}`,
  playbackUrl: (camera: string, start: number, duration = 300) => `${base}/api/playback/${camera}?${qs({ start, duration })}`,
  whepUrl: (path: string) => `${base}/api/whep/${path}`,
  wsUrl: () => `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${base}/api/ws`,
  /** Live updates. onMessage gets the other message types (e.g. {type: "briefing"}). */
  subscribe(onEvent: (e: NvrEvent) => void, onMessage?: (msg: { type: string; [k: string]: unknown }) => void): () => void {
    let ws: WebSocket | null = null;
    let closed = false;
    let retry = 1000;
    const connect = () => {
      ws = new WebSocket(this.wsUrl());
      ws.onmessage = (m) => {
        const msg = JSON.parse(m.data);
        connection.data();
        if (msg.type === "event") onEvent(msg.event);
        else onMessage?.(msg);
      };
      ws.onopen = () => { retry = 1000; connection.ws(true); };
      ws.onclose = () => {
        connection.ws(false);
        if (!closed) setTimeout(connect, (retry = Math.min(retry * 2, 15000)));
      };
    };
    connect();
    return () => {
      closed = true;
      ws?.close();
    };
  },
  /** Ask the NVR assistant; streams AskChunk messages. */
  ask: (threadId: number | null, message: string, onChunk: (c: AskChunk) => void) =>
    stream<AskChunk>("/api/assistant/ask", json("POST", { message, thread_id: threadId }), onChunk),
  /** Ask Qwen about an event clip; calls onChunk for each streamed NDJSON message. */
  askClip: (id: number, message: string, at: number | null, onChunk: (c: ChatChunk) => void) =>
    stream<ChatChunk>(`/api/events/${id}/chat`, json("POST", { message, at }), onChunk),
  // ---- REST
  cameras: () => req<Camera[]>("/api/cameras"),
  saveCamera: (c: Partial<Camera> & { id: string; password?: string }) =>
    req<Camera>(`/api/cameras/${c.id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...c, enabled: Boolean(c.enabled) }),
    }),
  events: (p: { camera?: string; status?: string; label?: string; threat?: string; before_id?: number; limit?: number; since?: number; until?: number; min_yolo?: number }) =>
    req<NvrEvent[]>(`/api/events?${qs(p)}`),
  event: (id: number) => req<NvrEvent>(`/api/events/${id}`),
  reprocess: (id: number) => req(`/api/events/${id}/reprocess`, { method: "POST" }),
  search: (q: string, camera?: string, minYolo?: number, since?: number, until?: number) =>
    req<NvrEvent[]>(`/api/search?${qs({ q, camera, min_yolo: minYolo || undefined, since, until })}`),
  footageSearch: (q: string, camera?: string, since?: number, until?: number) =>
    req<FootageMoment[]>(`/api/footage/search?${qs({ q, camera, since, until })}`),
  parseQuery: (q: string) => req<ParsedQuery>(`/api/query/parse?${qs({ q })}`),
  hub: () => req<HubStatus>("/api/hub"),
  turn: () => req<{ iceServers: RTCIceServer[] }>("/api/turn"),
  setHub: (b: { hub_url?: string; unenrol?: boolean }) => req<HubStatus>("/api/hub", json("PUT", b)),
  // PTZ / relay (ptz.py). Move and stop use keepalive so a Stop still goes out when the tab closes mid-drag.
  ptz: (cam: string) => req<PtzInfo>(`/api/cameras/${cam}/ptz`),
  ptzProbe: (cam: string) => req<PtzInfo>(`/api/cameras/${cam}/ptz/probe`, { method: "POST" }),
  ptzMove: (cam: string, v: { pan: number; tilt: number; zoom: number }) => req(`/api/cameras/${cam}/ptz/move`, { ...json("POST", v), keepalive: true }),
  ptzStop: (cam: string) => req(`/api/cameras/${cam}/ptz/stop`, { method: "POST", keepalive: true }),
  ptzRelative: (cam: string, t: { dx?: number; dy?: number; zoom?: number }) => req(`/api/cameras/${cam}/ptz/relative`, json("POST", { dx: 0, dy: 0, zoom: 0, ...t })),
  ptzHome: (cam: string) => req<PtzInfo>(`/api/cameras/${cam}/ptz/home`, { method: "POST" }),
  ptzGoto: (cam: string, token: string, wait = false) => req<PtzInfo>(`/api/cameras/${cam}/ptz/presets/${encodeURIComponent(token)}/goto${wait ? "?wait=true" : ""}`, { method: "POST" }),
  ptzSavePreset: (cam: string, name: string) => req<PtzInfo & { token: string }>(`/api/cameras/${cam}/ptz/presets`, json("POST", { name })),
  ptzRenamePreset: (cam: string, token: string, name: string) => req<PtzInfo>(`/api/cameras/${cam}/ptz/presets/${encodeURIComponent(token)}`, json("PUT", { name })),
  ptzDeletePreset: (cam: string, token: string) => req<PtzInfo>(`/api/cameras/${cam}/ptz/presets/${encodeURIComponent(token)}`, { method: "DELETE" }),
  ptzSetHome: (cam: string, token: string | null) => req<PtzInfo>(`/api/cameras/${cam}/ptz/home-preset`, json("POST", { token })),
  ptzConfig: (cam: string, cfg: Partial<Pick<PtzConfig, "return_home_min" | "relay_label" | "input_label">>) => req<PtzInfo>(`/api/cameras/${cam}/ptz/config`, json("PUT", cfg)),
  relay: (cam: string, on: boolean) => req<{ state: boolean; mode: string }>(`/api/cameras/${cam}/relay`, json("POST", { on })),
  footageVerify: (b: { camera_id: string; ts: number; tile: number; q: string }) =>
    req<FootageMatch>("/api/footage/verify", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) }),
  footageStatus: () => req<FootageStatus>("/api/footage/status"),
  recordings: (camera: string, start: number, end: number) =>
    req<{ spans: { start: string; duration: number }[]; events: TimelineEvent[]; kept: KeptSpan[]; locks: Lock[] }>(
      `/api/recordings/${camera}?${qs({ start, end })}`,
    ),
  system: () => req<SystemInfo>("/api/system"),
  advisor: (ai = true) => req<AdvisorReport>(`/api/advisor?ai=${ai}`),
  advisorDismiss: (key: string, fingerprint: string) => req("/api/advisor/dismiss", json("POST", { key, fingerprint })),
  advisorUndismiss: (key: string) => req("/api/advisor/undismiss", json("POST", { key })),
  advisorApply: (a: Record<string, unknown>) => req<{ message: string }>("/api/advisor/apply", json("POST", a)),
  correctSynopsis: (id: number, s: Synopsis) => req<NvrEvent>(`/api/events/${id}/synopsis`, json("PUT", s)),
  revertSynopsis: (id: number) => req<NvrEvent>(`/api/events/${id}/synopsis/correction`, { method: "DELETE" }),
  generateSynopsis: (id: number) => req(`/api/events/${id}/synopsis/generate`, { method: "POST" }),
  feedback: (id: number, f: Feedback) => req<NvrEvent>(`/api/events/${id}/feedback`, json("PUT", f)),
  detections: (cam: string, hours = 24) => req<DetectionPoint[]>(`/api/cameras/${cam}/detections?hours=${hours}`),
  previewZones: (cam: string, zones: Zone[]) =>
    req<{ mask: number; restore: number }>(`/api/cameras/${cam}/zones/preview`, json("POST", { zones })),
  applyZones: (cam: string) => req<{ masked: number; restored: number }>(`/api/cameras/${cam}/zones/apply`, { method: "POST" }),
  retentionPolicy: () => req<{ policy: RetentionPolicy; defaults: RetentionPolicy }>("/api/retention/policy"),
  saveRetentionPolicy: (p: RetentionPolicy) => req<{ policy: RetentionPolicy }>("/api/retention/policy", json("PUT", p)),
  retentionStats: () => req<RetentionStats>("/api/retention/stats"),
  retentionPreview: (cam: string, hours = 24) => req<RetentionPreview>(`/api/retention/preview?camera=${cam}&hours=${hours}`),
  createLock: (l: { camera_id: string; start_ts: number; end_ts: number; note?: string }) => req<Lock>("/api/locks", json("POST", l)),
  deleteLock: (id: number) => req(`/api/locks/${id}`, { method: "DELETE" }),
  lockEvent: (id: number, note = "") => req<Lock>(`/api/events/${id}/lock`, json("POST", { note })),
  unlockEvent: (id: number) => req(`/api/events/${id}/lock`, { method: "DELETE" }),
  topology: () => req<CameraLink[]>("/api/topology"),
  saveTopology: (links: CameraLink[]) => req<{ links: CameraLink[]; relinking: number }>("/api/topology", json("PUT", links)),
  topologySuggestions: () => req<LinkSuggestion[]>("/api/topology/suggestions"),
  eventJourney: (id: number) => req<Journey | null>(`/api/events/${id}/journey`),
  rejectLink: (id: number) => req(`/api/links/${id}/reject`, { method: "POST" }),
  regenerateJourney: (id: number) => req(`/api/journeys/${id}/regenerate`, { method: "POST" }),
  dashboards: () => req<SiteDashboard[]>("/api/dashboards"),
  createDashboard: (d: { name: string; config: DashboardConfig }) => req<SiteDashboard>("/api/dashboards", json("POST", d)),
  updateDashboard: (id: number, d: { name: string; config: DashboardConfig }) => req<SiteDashboard>(`/api/dashboards/${id}`, json("PUT", d)),
  deleteDashboard: (id: number) => req(`/api/dashboards/${id}`, { method: "DELETE" }),
  layouts: () => req<Layout[]>("/api/layouts"),
  createLayout: (l: { name: string; config: LayoutConfig }) => req<Layout>("/api/layouts", json("POST", l)),
  updateLayout: (id: number, l: { name: string; config: LayoutConfig }) => req<Layout>(`/api/layouts/${id}`, json("PUT", l)),
  deleteLayout: (id: number) => req(`/api/layouts/${id}`, { method: "DELETE" }),
  feedbackStats: () => req<FeedbackStats>("/api/feedback/stats"),
  baseline: () => req<BaselineCamera[]>("/api/baseline"),
  home: (since: number) => req<HomeData>(`/api/home?${qs({ since })}`),
  identities: (kind: "person" | "vehicle", since: number, camera?: string) => req<IdentitiesResult>(`/api/identities?${qs({ kind, since, camera })}`),
  nameIdentity: (b: { kind: "person" | "vehicle"; name: string; notes: string; event_ids: number[]; watch?: boolean; watch_note?: string }) => req<NamedIdentity>("/api/identities", json("POST", b)),
  watchIdentity: (id: number, watch: boolean, watch_note?: string) => req<NamedIdentity>(`/api/identities/${id}`, json("PUT", { watch, watch_note })),
  eventIdentity: (id: number) => req<(NamedIdentity & { sim: number }) | null>(`/api/events/${id}/identity`),
  deleteIdentity: (id: number) => req(`/api/identities/${id}`, { method: "DELETE" }),
  backupNow: () => req<{ at: number; path: string; bytes: number }>("/api/backup", { method: "POST" }),
  assistantThreads: () => req<AssistantThread[]>("/api/assistant/threads"),
  assistantThread: (id: number) => req<AssistantThread & { messages: AssistantMessage[] }>(`/api/assistant/threads/${id}`),
  deleteThread: (id: number) => req(`/api/assistant/threads/${id}`, { method: "DELETE" }),
  briefings: (limit = 10) => req<{ briefings: Briefing[]; settings: BriefingSettings }>(`/api/briefings?${qs({ limit })}`),
  generateBriefing: () => req<Briefing>("/api/briefings/generate", { method: "POST" }),
  saveBriefingSettings: (s: BriefingSettings) => req<BriefingSettings>("/api/briefings/settings", json("PUT", s)),
  remote: () => req<RemoteStatus>("/api/remote"),
  remoteTasks: (tasks: string[]) => req<RemoteStatus>("/api/remote/tasks", json("PUT", { tasks })),
  remoteTest: () => req<{ ok: boolean; seconds?: number; was?: string; reply?: string; error?: string; status: RemoteStatus }>("/api/remote/test", { method: "POST" }),
  remoteWarm: () => req<{ state: string }>("/api/remote/warm", { method: "POST" }),
  rebuildBaseline: () => req<{ scored: number; cameras: BaselineCamera[] }>("/api/baseline/rebuild", { method: "POST" }),
  chat: (id: number) => req<ChatMessage[]>(`/api/events/${id}/chat`),
  clearChat: (id: number) => req<ChatMessage[]>(`/api/events/${id}/chat`, { method: "DELETE" }),
  saveNote: (id: number, msgId: number, saved: boolean) =>
    req<ChatMessage[]>(`/api/events/${id}/chat/${msgId}/saved?saved=${saved}`, { method: "PUT" }),
  };
}
export type SiteApi = ReturnType<typeof makeApi>;
/** The site this page is served from. */
export const api = makeApi(BASE);

/* ---- Ask the NVR */
export type CiteRefs = {
  events: Record<string, { camera_id: string; camera: string; start_ts: number; label: string; snapshot: boolean }>;
  footage: Record<string, { camera_id: string; camera: string; ts: number; tile: number }>;
};
export type AskCall = { tool: string; label: string; count: number };
export type AskMeta = { calls: AskCall[]; refs: CiteRefs; planner?: string; fallback?: string };
export type AssistantMessage = { id: number; role: "user" | "assistant"; content: string; calls: AskMeta | null; model: string | null; ts: number };
export type AssistantThread = { id: number; title: string; created_at: number; updated_at: number; messages?: AssistantMessage[] | number };
export type AskChunk =
  | { type: "thread"; thread_id: number }
  | { type: "user"; id: number }
  | ({ type: "calls" } & AskMeta)
  | { type: "model"; model: string }
  | { type: "fallback"; reason: string }
  | { type: "delta"; text: string }
  | { type: "done"; id: number }
  | { type: "error"; error: string };
export type Briefing = { id: number; period_start: number; period_end: number; headline: string; text: string; model: string | null;
  created_at: number; stats: { refs?: CiteRefs; free_gb?: number; gaps?: string[] } };
export type BriefingSettings = { enabled: boolean; time: string };
export type RemoteStatus = {
  configured: boolean; model: string | null; local_model: string; state: "off" | "cold" | "warm" | "down"; tasks: string[]; all_tasks: string[];
  down_until: number | null; last_error: string; today: { billed_s: number; requests: number; usd: number }; budget_usd: number;
  rate_usd_per_s: number; last_latency_s: number | null;
};

export const ask: SiteApi["ask"] = (...args) => api.ask(...args);

export type ChatChunk =
  | { type: "user"; id: number }
  | { type: "frames"; frames: { file: string; t: number }[] }
  | { type: "delta"; text: string }
  | { type: "done"; id: number }
  | { type: "error"; error: string };

export const askClip: SiteApi["askClip"] = (...args) => api.askClip(...args);
export const media: SiteApi["media"] = (...args) => api.media(...args);
export const frameUrl: SiteApi["frameUrl"] = (...args) => api.frameUrl(...args);
export const playbackUrl: SiteApi["playbackUrl"] = (...args) => api.playbackUrl(...args);
export const subscribe: SiteApi["subscribe"] = (...args) => api.subscribe(...args);

export const fmtTime = (ts: number) =>
  new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });

export const fmtDuration = (e: Pick<NvrEvent, "start_ts" | "end_ts">) =>
  e.end_ts ? `${Math.max(1, Math.round(e.end_ts - e.start_ts))}s` : "live";
