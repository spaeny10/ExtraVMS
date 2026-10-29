/**
 * The "Vehicles around this" card's words (pure; VehiclesCard in IncidentView.tsx draws them). The vehicles come
 * from the Site's servers (hub soc_vehicles.py): plates and notes are counted and worded on the server, never by a
 * model; here they are only laid out. Directions are the camera's report ("toward the camera"), never "arrived": a
 * camera can face either way.
 */
import { formatPlate } from "@site/plate";
import type { AroundVehicle, IncidentEvent, IncidentVehicles } from "./types";

const DIRECTION: Record<string, string> = { approach: "toward the camera", away: "going away" };

/** "ABC 1234", "27654 (Otso)", "FTG 4321 (from footage)", or "No plate". */
export function plateLabel(v: Pick<AroundVehicle, "plate" | "plate_by">): string {
  if (!v.plate) return "No plate";
  const by = v.plate_by === "otso" ? " (Otso)" : v.plate_by === "footage" ? " (from footage)" : "";
  return `${formatPlate(v.plate)}${by}`;
}

/** What it is and which way it went: "silver Chevrolet Silverado pickup truck · toward the camera (reported)". */
export function vehicleLabel(v: Pick<AroundVehicle, "vehicle" | "vehicle_by" | "direction">): string {
  const what = v.vehicle ? `${v.vehicle}${v.vehicle_by === "camera" ? " (the camera's guess)" : ""}` : "";
  const way = v.direction && DIRECTION[v.direction] ? `${DIRECTION[v.direction]} (reported)` : "";
  return [what, way].filter(Boolean).join(" · ");
}

/** "3 vehicles, 2 with a plate, from 10:52 PM to 11:19 PM"; on a busy road "20 of 362 vehicles from ... : those with a
 *  plate nearest the incident"; or that none were seen. */
export function vehiclesSummary(d: Pick<IncidentVehicles, "vehicles" | "since" | "until" | "total">, time: (ts: number) => string): string {
  const n = d.vehicles.length;
  const plates = d.vehicles.filter((v) => v.plate).length;
  const span = `from ${time(d.since)} to ${time(d.until)}`;
  if (n === 0) return `No vehicles seen ${span}.`;
  if (d.total && d.total > n) return `${n} of ${d.total} vehicles ${span}: those with a plate nearest the incident.`;
  return `${n} vehicle${n === 1 ? "" : "s"}${plates ? `, ${plates} with a plate` : ", none with a plate"}, ${span}.`;
}

/** Servers that couldn't answer, in a line ("Yard NVR is offline; Gate NVR: no answer within 10 s"), or "". */
export function serversNote(d: Pick<IncidentVehicles, "servers">): string {
  return d.servers.filter((s) => s.status !== "ok").map((s) => (s.status === "offline" ? `${s.server_name} is offline`
    : `${s.server_name}: ${s.error ?? s.status}`)).join("; ");
}

/** The incident's plate alarms (a list hit or a plate pattern: circling, new plate, unusual time), their rule texts. */
export function plateAlarms(events: Pick<IncidentEvent, "detail">[]): string[] {
  const out: string[] = [];
  for (const e of events) {
    const p = (e.detail as { policy?: { kind?: string; text?: string } } | null | undefined)?.policy;
    if (p && (p.kind === "plate_list" || p.kind === "plate_pattern") && p.text && !out.includes(p.text)) out.push(p.text);
  }
  return out;
}
