/**
 * Home dashboard data model, shared by the site UI and the fleet hub (which imports it as @site/dashboard/types).
 * A dashboard is a list of widgets on a 12-column grid; the hub validates the same shape in hub/hub/dashboards.py.
 */
import type { NvrEvent } from "../api";

export type DashboardWidgetType = "camera" | "events" | "briefing" | "alerts" | "health" | "ask";

/** Persisted wire shape (dashboards, groups): `site` is the SERVER id (legacy naming; do not rename). */
export type CameraRef = { site: string; camera: string };
export type CameraProps = { site: string; camera: string; quality?: "sd" | "hd" };
export type EventsProps = { sites?: string[]; cameras?: CameraRef[]; group?: string; classes?: ("person" | "vehicle")[]; limit?: number };
export type BriefingProps = { source: "digest" } | { source: "site"; site: string };
export type AlertsProps = { kinds?: string[]; limit?: number };
export type HealthProps = { sites?: string[] };
export type AskProps = { placeholder?: string };
export type WidgetProps = { camera: CameraProps; events: EventsProps; briefing: BriefingProps; alerts: AlertsProps; health: HealthProps; ask: AskProps };

export type Widget<T extends DashboardWidgetType = DashboardWidgetType> = { id: string; type: T; x: number; y: number; w: number; h: number; props: WidgetProps[T] };
export type AnyWidget = { [K in DashboardWidgetType]: Widget<K> }[DashboardWidgetType];

export type DashboardConfig = { version: 1; cols: 12; rowH: number; widgets: AnyWidget[] };
export type Dashboard = { id: string; org_id: string; owner_user_id: string | null; name: string; shared: boolean; config: DashboardConfig; created_at: number; updated_at: number; updated_by: string | null };
export type DashboardList = { dashboards: Omit<Dashboard, "config">[]; default_id: string | null; generated: DashboardConfig };
export type CameraGroup = { id: string; org_id: string; name: string; members: { site_id: string; camera_id: string }[]; created_at: number; updated_at: number };
export type FleetEvent = NvrEvent & { site_id: string; site_name: string };
export type FleetEvents = { events: FleetEvent[]; offline: string[]; errors: { site_id: string | null; error: string }[] };

export const COLS = 12;
export const DEFAULT_ROW_H = 60;
export const emptyDashboard = (): DashboardConfig => ({ version: 1, cols: COLS, rowH: DEFAULT_ROW_H, widgets: [] });
export const newWidgetId = () => "w_" + Math.random().toString(16).slice(2, 10);
