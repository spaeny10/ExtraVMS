import { useState } from "react";
import { api, type NvrEvent, type PlateRead, type SiteApi } from "./api";
import { formatPlate, plateError, plateOutcome } from "./plate";

/** A vehicle's plate as a badge ("🔢 ABC 1234 · TX"); nothing when none was read. */
export function PlateBadge({ plate, region, conf }: { plate?: string | null; region?: string | null; conf?: string | null }) {
  if (!plate) return null;
  return (
    <span className="badge plate-badge" title={`License plate, read from the footage${conf ? ` (${conf} confidence)` : ""}`}>
      🔢 {formatPlate(plate, region)}
    </span>
  );
}

/**
 * "Read plate" on a vehicle event (backend POST /api/events/{id}/plate): shows "Reading…", then the plate or "Not
 * readable in this footage". With a plate already known it shows that and offers to read again. Used in the event
 * detail and on the vehicle events an Ask answer cites (this server's Ask and the hub's Site Ask: `site` is the
 * event's server).
 */
export function ReadPlate({ id, plate, region, site = api, onRead, compact }: {
  id: number; plate?: string | null; region?: string | null; site?: SiteApi;
  /** the result, with the updated event when the server sent it */
  onRead?: (r: PlateRead, e?: NvrEvent) => void;
  /** one short line (Ask references) instead of the detail's line */
  compact?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [known, setKnown] = useState<{ plate: string; region?: string | null } | null>(plate ? { plate, region } : null);
  const run = async (ev: React.MouseEvent) => {
    ev.stopPropagation();
    setBusy(true); setErr(null); setSaid(null);
    try {
      const r = await site.readPlate(id);
      setKnown(r.result === "read" && r.plate ? { plate: r.plate, region: r.region } : null);
      setSaid(plateOutcome(r));
      onRead?.(r, r.event);
    } catch (e) {
      setErr(plateError(e));
    }
    setBusy(false);
  };
  return (
    <span className={`read-plate${compact ? " compact" : ""}`} onClick={(ev) => ev.stopPropagation()}>
      {known && !said && <span className="plate-text" title="License plate, read from the footage">Plate {formatPlate(known.plate, known.region)}</span>}
      {said && <span className={known ? "plate-text" : "muted small"}>{said}</span>}
      <button type="button" className="ghost small" disabled={busy} onClick={run}
        title="Qwen looks at the sharpest full-resolution frames of this vehicle and reads the plate only if it is clearly legible">
        {busy ? "Reading…" : known ? "Read again" : "Read plate"}
      </button>
      {err && <span className="error small">{err}</span>}
    </span>
  );
}
