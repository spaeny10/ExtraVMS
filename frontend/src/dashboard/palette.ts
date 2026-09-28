import type { DashboardWidgetType, WidgetProps } from "./types";

export type WidgetDef<T extends DashboardWidgetType = DashboardWidgetType> = {
  type: T; label: string; hint: string; w: number; h: number; minW: number; minH: number; props: WidgetProps[T];
};

export const WIDGET_DEFS: { [K in DashboardWidgetType]: WidgetDef<K> } = {
  camera: { type: "camera", label: "Camera", hint: "Live picture of one camera from any site", w: 4, h: 4, minW: 2, minH: 2, props: { site: "", camera: "", quality: "sd" } },
  events: { type: "events", label: "Latest events", hint: "Live feed of events from chosen sites, cameras or a group", w: 8, h: 6, minW: 3, minH: 3, props: { limit: 20 } },
  briefing: { type: "briefing", label: "Briefing", hint: "The organisation digest, or one site's daily briefing", w: 6, h: 4, minW: 3, minH: 2, props: { source: "digest" } },
  alerts: { type: "alerts", label: "Alerts", hint: "Open alerts across your sites", w: 4, h: 4, minW: 3, minH: 2, props: { limit: 20 } },
  health: { type: "health", label: "Site health", hint: "Online state, cameras up, disk and alerts per site", w: 4, h: 3, minW: 3, minH: 2, props: {} },
  ask: { type: "ask", label: "Ask", hint: "A question box that opens Find with your question", w: 4, h: 2, minW: 3, minH: 2, props: {} },
};

export const WIDGET_LIST = Object.values(WIDGET_DEFS) as WidgetDef[];
