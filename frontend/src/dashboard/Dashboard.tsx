/**
 * A dashboard: the grid of widgets for one config and one source. Handles the widget chrome (title, edit
 * controls), the live-stream budget shared by camera tiles, and the hero (one widget full width) view.
 */
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { Icon } from "../ui";
import { DashboardGrid } from "./GridView";
import { WIDGET_DEFS } from "./palette";
import type { DashboardSource } from "./source";
import type { AnyWidget, DashboardConfig } from "./types";
import { AlertsWidget } from "./widgets/AlertsWidget";
import { AskWidget } from "./widgets/AskWidget";
import { BriefingWidget } from "./widgets/BriefingWidget";
import { CameraWidget, cameraTitle } from "./widgets/CameraWidget";
import { EventsWidget } from "./widgets/EventsWidget";
import { HealthWidget } from "./widgets/HealthWidget";

export const MAX_LIVE = 9;

/** Which camera tiles may hold a live stream: the first MAX_LIVE visible ones, or any the viewer pressed Play on. */
type Budget = { playing: (id: string) => boolean; visible: (id: string, on: boolean) => void; force: (id: string) => void };
const BudgetCtx = createContext<Budget>({ playing: () => true, visible: () => {}, force: () => {} });
export const useBudget = () => useContext(BudgetCtx);

function useBudgetProvider(): Budget {
  const [order, setOrder] = useState<string[]>([]);
  const visible = useCallback((id: string, on: boolean) => setOrder((o) => (on ? (o.includes(id) ? o : [...o, id]) : o.filter((x) => x !== id))), []);
  const force = useCallback((id: string) => setOrder((o) => [id, ...o.filter((x) => x !== id)]), []);
  const playing = useCallback((id: string) => { const i = order.indexOf(id); return i >= 0 && i < MAX_LIVE; }, [order]);
  return useMemo(() => ({ playing, visible, force }), [playing, visible, force]);
}

export type DashboardProps = {
  source: DashboardSource; config: DashboardConfig; editing: boolean;
  onChange: (c: DashboardConfig) => void;
  onEditWidget: (w: AnyWidget) => void;
};

export function Dashboard({ source, config, editing, onChange, onEditWidget }: DashboardProps) {
  const budget = useBudgetProvider();
  const [hero, setHero] = useState<string | null>(null);
  const remove = (id: string) => onChange({ ...config, widgets: config.widgets.filter((w) => w.id !== id) });
  const patch = (id: string, props: object) => onChange({ ...config, widgets: config.widgets.map((w) => (w.id === id ? { ...w, props: { ...w.props, ...props } } as AnyWidget : w)) });

  const body = (w: AnyWidget, big: boolean): ReactNode => {
    switch (w.type) {
      case "camera": return <CameraWidget widget={w} source={source} editing={editing} big={big} onProps={(p) => patch(w.id, p)} />;
      case "events": return <EventsWidget widget={w} source={source} />;
      case "briefing": return <BriefingWidget widget={w} source={source} />;
      case "alerts": return <AlertsWidget widget={w} source={source} />;
      case "health": return <HealthWidget widget={w} source={source} />;
      case "ask": return <AskWidget widget={w} source={source} />;
    }
  };
  const title = (w: AnyWidget): string => (w.type === "camera" ? cameraTitle(w, source) : WIDGET_DEFS[w.type].label);

  const heroW = hero ? config.widgets.find((w) => w.id === hero) : null;
  if (heroW && !editing) {
    return (
      <BudgetCtx.Provider value={budget}>
        <div className="dash-hero">
          <div className="dash-widget">
            <div className="dash-head">
              <button className="ghost small" onClick={() => setHero(null)} title="Back to the dashboard (Esc)"><Icon name="grid" size={14} /> Back</button>
              <strong>{title(heroW)}</strong>
              <span className="spacer" />
              {heroW.type === "camera" && <a className="small" href={source.liveHref(heroW.props.site)}>Open site →</a>}
            </div>
            <div className="dash-body">{body(heroW, true)}</div>
          </div>
        </div>
      </BudgetCtx.Provider>
    );
  }

  return (
    <BudgetCtx.Provider value={budget}>
      {config.widgets.length === 0 && (
        <div className="empty">This dashboard is empty. {editing ? "Use “Add widget” above." : "Press Edit to add camera tiles, an events feed, briefings and more."}</div>
      )}
      <DashboardGrid config={config} editing={editing} onChange={onChange}
        minSize={(w) => ({ w: WIDGET_DEFS[w.type].minW, h: WIDGET_DEFS[w.type].minH })}
        render={(w, dragHandle) => (
          <div className={`dash-widget kind-${w.type}`}>
            <div className={`dash-head ${editing ? "dash-handle" : ""}`} onPointerDown={dragHandle} title={editing ? "Drag to move" : undefined}>
              {editing && <span className="dash-grip" aria-hidden>⠿</span>}
              {!editing && (w.type === "camera" || w.type === "events")
                ? <button className="linkish dash-title" onClick={() => setHero(w.id)} title="Show large">{title(w)}</button>
                : <span className="dash-title">{title(w)}</span>}
              <span className="spacer" />
              {editing && <button className="ghost small" title="Widget settings" onClick={() => onEditWidget(w)}>⚙</button>}
              {editing && <button className="ghost small" title="Remove widget" onClick={() => remove(w.id)}><Icon name="x" size={14} /></button>}
            </div>
            <div className="dash-body">{body(w, false)}</div>
          </div>
        )} />
    </BudgetCtx.Provider>
  );
}
