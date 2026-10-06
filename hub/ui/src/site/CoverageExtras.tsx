/**
 * Cellular coverage on Site › Settings › General (AddressBox): the rings control for the map (carrier and technology,
 * a legend; the rings are the FCC covered share within 0.5/1/2 km, averages for each circle, never a grid lookup) and
 * "Check cellular coverage" for the picked point before the Site is saved or set up (costs units; cached for the same
 * spot, so asking twice is free).
 */
import { useEffect, useState } from "react";
import { confirmDialog, toast } from "@site/ui";
import { type CoverageCheck, type CoverageData, api, fmtTime } from "../api";
import {
  TECH_LABEL, TYPICAL_CAMERAS, carrierName, costText, fmtScore, medText, needFor, needText, ringsFor, sortCarriers, unitsText, uploadFit,
  type Ring,
} from "../coverage";
import { EvaluationBanner, FitChip } from "./CoverageCard";

export type RingChoice = { on: boolean; carrier: string; tech: string };

/** The rings to draw for a choice (undefined = none). */
export function ringsOf(data: CoverageData | null | undefined, ch: RingChoice): Ring[] | undefined {
  if (!ch.on || !data) return undefined;
  const c = data.carriers.find((x) => x.code === ch.carrier) ?? sortCarriers(data.carriers)[0];
  if (!c) return undefined;
  const tech = c.tech[ch.tech] ? ch.tech : Object.keys(c.tech)[0];
  return ringsFor(c.tech[tech], `${carrierName(c)} ${TECH_LABEL[tech] ?? tech}`);
}

export function RingsControl({ data, choice, setChoice }: { data: CoverageData; choice: RingChoice; setChoice: (c: RingChoice) => void }) {
  const carriers = sortCarriers(data.carriers);
  const cur = carriers.find((c) => c.code === choice.carrier) ?? carriers[0];
  if (!cur) return null;
  const techs = Object.keys(cur.tech);
  return (
    <div className="cov-rings small">
      <label className="row"><input type="checkbox" checked={choice.on} onChange={(e) => setChoice({ ...choice, on: e.target.checked, carrier: cur.code })} /> Coverage rings</label>
      {choice.on && (
        <>
          <select aria-label="Carrier" value={cur.code} onChange={(e) => setChoice({ ...choice, carrier: e.target.value })}>
            {carriers.map((c) => <option key={c.code} value={c.code}>{carrierName(c)}</option>)}
          </select>
          <select aria-label="Technology" value={techs.includes(choice.tech) ? choice.tech : techs[0]} onChange={(e) => setChoice({ ...choice, tech: e.target.value })}>
            {techs.map((t) => <option key={t} value={t}>{TECH_LABEL[t] ?? t}</option>)}
          </select>
          <span className="cov-legend" title="Each ring is the share of the area within 0.5, 1 and 2 km that the carrier covers (FCC data): an average for the whole circle, not where inside it">
            <span className="cov-key good" />90%+ <span className="cov-key fair" />60–90% <span className="cov-key poor" />under 60% <span className="cov-key none" />none
          </span>
        </>
      )}
      {choice.on && <p className="muted small address-note" style={{ margin: "4px 0 0" }}>Rings show the covered share of the area within 0.5, 1 and 2 km (FCC): an average for each circle, not a map of where. Hover a ring for its number.</p>}
    </div>
  );
}

/** "Check cellular coverage" at the form's point (hub POST /api/coverage/check), with a compact per-carrier answer. */
export function CoverageCheckBox({ lat, lon, orgId, cost, evaluation }: { lat: number; lon: number; orgId: string; cost: number; evaluation: boolean }) {
  const [res, setRes] = useState<CoverageCheck | null>(null);
  const [busy, setBusy] = useState(false);
  const [cams, setCams] = useState(String(TYPICAL_CAMERAS));
  useEffect(() => { setRes(null); }, [lat, lon]);
  const run = async () => {
    if (!(await confirmDialog("Check cellular coverage here?", {
      message: `Costs ${costText(cost)} of this month's CoverageMap budget, unless this spot was checked in the last weeks (then it is free).${evaluation ? " Evaluation only." : ""}`,
      confirmLabel: "Check",
    }))) return;
    setBusy(true);
    try { const r = await api.coverageCheck({ lat, lon, org_id: orgId }); setRes(r); if (!r.cached) toast.success(`Checked (${unitsText(r.units)})`); }
    catch (e) { toast.error(e); } finally { setBusy(false); }
  };
  const need = needFor(Number(cams) || 0);
  const carriers = sortCarriers(res?.data.carriers ?? []);
  return (
    <div className="cov-check">
      <div className="row small">
        <button className="ghost small" disabled={busy} onClick={run}>{busy ? "Checking…" : "Check cellular coverage"}</button>
        <span className="muted">{unitsText(cost)} at most · which carrier, and will the upload carry the cameras</span>
      </div>
      {res && (
        <div className="cov-check-result small">
          {res.evaluation && <EvaluationBanner />}
          <div className="row">
            <label className="row">Cameras <input className="cov-cams" inputMode="numeric" value={cams} onChange={(e) => setCams(e.target.value.replace(/[^0-9]/g, "").slice(0, 2))} /></label>
            <span className="muted">{needText(need)}</span>
          </div>
          <table className="hub-table stack cov-check-table">
            <thead><tr><th>Carrier</th>{["lte", "5g"].map((t) => <th key={t}>{TECH_LABEL[t]}</th>)}</tr></thead>
            <tbody>{carriers.map((c) => (
              <tr key={c.code}>
                <td className="lead">{carrierName(c)}</td>
                {["lte", "5g"].map((t) => {
                  const e = c.tech[t];
                  return (
                    <td key={t} data-label={TECH_LABEL[t]}>
                      {e ? <>
                        <strong>{fmtScore(e.summary?.overall)}</strong> <span className="muted">↑ {medText(e.speed?.upload, "Mbit/s")}</span>{" "}
                        <FitChip fit={uploadFit(need.mbps, e.speed?.upload)} />
                      </> : <span className="muted">—</span>}
                    </td>
                  );
                })}
              </tr>))}
            </tbody>
          </table>
          <p className="muted small" style={{ margin: "4px 0 0" }}>
            {res.cached ? `From a check on ${fmtTime(res.fetched_at)} (no units used).` : `Checked now (${unitsText(res.units)}).`} Scores 0–10. {res.source}.
          </p>
        </div>
      )}
    </div>
  );
}
