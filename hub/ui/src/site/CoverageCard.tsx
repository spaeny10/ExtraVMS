/**
 * Site › Settings › General, next to the address and map: cellular coverage at the Site (hub coverage.py, the CoverageMap
 * API). Which carrier, and will the upload carry the cameras: carriers best first, each with LTE and 5G rows (overall
 * score bar 0–10 with coverage, reliability and performance; FCC signal with its band and the covered share at
 * 0.5/1/2 km; nearby speed-test medians with test and failure counts; measured vs estimated; the upload fit for the
 * cameras' need). Hub administrators and the Site's admins can look it up again (it costs units; confirmed first). On
 * the trial plan it is for hub administrators only and says so. Beside the Settings card on wide screens, below it on
 * narrower ones (it lays itself out by its own width: hub.css container query).
 */
import { useState } from "react";
import { confirmDialog, toast } from "@site/ui";
import { type CoverageCarrier, type CoverageEntry, type Site, type SiteCoverage, type UploadFit, api, fmtTime } from "../api";
import {
  BAND_LABEL, FIT_LABEL, TECH_LABEL, carrierName, costText, fmtScore, medText, needText, pctText, radiusText, scoreClass, signalBand,
  sortCarriers, testsText, unitsText, uploadFit,
} from "../coverage";

export const COVERAGE_ANCHOR = "cellular-coverage";

export function EvaluationBanner() {
  return (
    <p className="cov-eval small" role="note">
      <strong>Evaluation only — hub administrators.</strong> CoverageMap's free trial may not be shown to customers; they see nothing until the hub is on a paid plan and the data has been looked up again.
    </p>
  );
}

export function FitChip({ fit }: { fit: UploadFit }) {
  return <span className={`chip small fit-chip fit-${fit.fit}`} title={fit.reason}>{fit.fit === "unknown" ? "Upload: no tests" : `Upload: ${FIT_LABEL[fit.fit]}`}</span>;
}

export function ScoreBar({ value }: { value: number | null | undefined }) {
  const pct = value == null ? 0 : Math.max(0, Math.min(100, value * 10));
  return (
    <span className="cov-score">
      <span className={`cov-bar ${scoreClass(value)}`} role="meter" aria-valuemin={0} aria-valuemax={10} aria-valuenow={value ?? undefined} aria-label="Overall score">
        <span style={{ width: `${pct}%` }} />
      </span>
      <strong>{fmtScore(value)}</strong>
    </span>
  );
}

function TechRow({ e, fit }: { e: CoverageEntry; fit: UploadFit }) {
  const s = e.summary, f = e.fcc, sp = e.speed;
  const band = signalBand(f?.signal.point ?? f?.signal.r05);
  const dbm = f?.signal.point ?? f?.signal.r05;
  const up = sp?.upload, down = sp?.download, lat = sp?.latency;
  return (
    <div className="cov-row">
      <div className="cov-tech">
        <strong>{TECH_LABEL[e.technology] ?? e.technology_name ?? e.technology}</strong>
        {s?.source && <span className={`chip small src-${s.source === "measured" ? "measured" : "estimated"}`} title={s.source === "measured" ? "Scores use nearby speed tests" : "Scores from FCC data alone (nothing measured nearby): performance and reliability are discounted"}>{s.source === "measured" ? "measured" : "estimated"}</span>}
      </div>
      <div className="cov-cell">
        <ScoreBar value={s?.overall} />
        <span className="muted small">coverage {fmtScore(s?.coverage)} · reliability {fmtScore(s?.reliability)} · performance {fmtScore(s?.performance)}</span>
      </div>
      <div className="cov-cell small">
        {f ? (
          <>
            <span><span className={`sig-band ${band}`}>{BAND_LABEL[band]}</span> {dbm != null ? `${Math.round(dbm)} dBm` : ""}</span>
            <span className="muted" title="Share of the area within each radius the carrier covers (FCC Broadband Data Collection)">covered 0.5 km {pctText(f.coverage.r05)} · 1 km {pctText(f.coverage.r1)} · 2 km {pctText(f.coverage.r2)}</span>
          </>
        ) : <span className="muted">No FCC coverage reported</span>}
      </div>
      <div className="cov-cell small">
        {up || down ? (
          <>
            <span>↓ {medText(down, "Mbit/s")} · ↑ {medText(up, "Mbit/s")} · {medText(lat, "ms")}</span>
            <span className="muted">{testsText(up ?? down)}{(up ?? down) ? ` · ${radiusText(up ?? down)}` : ""}</span>
          </>
        ) : <span className="muted">No speed tests nearby</span>}
      </div>
      <div className="cov-fit"><FitChip fit={fit} /></div>
    </div>
  );
}

function Carrier({ c, need, best }: { c: CoverageCarrier; need: number; best: boolean }) {
  const techs = Object.keys(c.tech).sort((a, b) => (a === "lte" ? -1 : b === "lte" ? 1 : a.localeCompare(b)));
  return (
    <div className={`cov-carrier ${best ? "best" : ""}`}>
      <div className="row cov-carrier-head">
        <strong>{carrierName(c)}</strong>
        <span className="muted small">{c.code}</span>
        {best && <span className="chip small cov-best">best here</span>}
      </div>
      {techs.map((t) => <TechRow key={t} e={c.tech[t]} fit={uploadFit(need, c.tech[t].speed?.upload)} />)}
    </div>
  );
}

export function CoverageCard({ site, cov, onChanged }: { site: Site; cov: SiteCoverage; onChanged: (c: SiteCoverage) => void }) {
  const [busy, setBusy] = useState(false);
  const refresh = async () => {
    const first = !cov.data && !cov.hidden;
    if (!(await confirmDialog(first ? `Look up cellular coverage at ${site.name}?` : `Look up ${site.name} again?`, {
      message: `Costs ${costText(cov.refresh_cost)} of this month's CoverageMap budget.${cov.evaluation ? " Evaluation only: customers will not see it." : ""}`,
      confirmLabel: "Look up",
    }))) return;
    setBusy(true);
    try { const c = await api.refreshCoverage(site.id); onChanged(c); toast.success(`Cellular coverage updated (${unitsText(c.units ?? 0)})`); }
    catch (e) { toast.error(e); } finally { setBusy(false); }
  };
  const carriers = sortCarriers(cov.data?.carriers ?? []);
  const wait = cov.refresh_wait_s ?? 0;
  return (
    <div className="card cov-card" id={COVERAGE_ANCHOR}>
      <div className="row">
        <h3 style={{ margin: 0 }}>Cellular coverage</h3>
        <span className="muted small">which carrier, and will the upload carry the cameras</span>
        <span className="spacer" />
        {cov.can_refresh && cov.locatable && (
          <button className="small ghost" disabled={busy || wait > 0} onClick={refresh}
            title={wait > 0 ? `Looked up moments ago: again in ${Math.ceil(wait / 60)} min` : `Costs ${costText(cov.refresh_cost)}`}>
            {busy ? "Looking up…" : cov.data ? "Refresh" : "Look up"} <span className="muted">({unitsText(cov.refresh_cost)})</span>
          </button>
        )}
      </div>
      {cov.evaluation && <EvaluationBanner />}
      {cov.error && cov.can_refresh && <p className="small cov-error">Last lookup failed{cov.attempted_at ? ` (${fmtTime(cov.attempted_at)})` : ""}: {cov.error}{cov.data ? " The data below is from before." : ""}</p>}
      {!cov.data ? (
        <p className="muted small" style={{ marginBottom: 0 }}>
          {cov.hidden ? "This Site's coverage was looked up during the evaluation and is being looked up again; it shows here once that is done."
            : !cov.locatable ? "Put the Site on the map (its address, then Save) to look up its cellular coverage."
            : cov.can_refresh ? "Not looked up yet. Look up shows each carrier's LTE and 5G scores, signal, nearby speed tests and whether the upload carries the cameras."
            : "Not looked up yet."}
        </p>
      ) : (
        <>
          <p className="small cov-need"><span className="muted">Upload the cameras need:</span> {needText(cov.need)}</p>
          {carriers.length === 0 ? <p className="muted small">No carrier data at this place.</p>
            : carriers.map((c, i) => <Carrier key={c.code} c={c} need={cov.need.mbps} best={i === 0 && c.best != null} />)}
          <p className="muted small cov-foot">
            Looked up {cov.fetched_at ? fmtTime(cov.fetched_at) : "—"}{cov.basis === "address" ? " (by address: set a map pin for a precise lookup)" : ""}
            {cov.stale ? ` · due for a refresh (${cov.stale_reason === "moved" ? "the pin moved" : cov.stale_reason === "plan" ? "plan changed" : "older than the refresh period"})` : ""}
            {cov.can_refresh && cov.units != null ? ` · ${unitsText(cov.units)}` : ""}
          </p>
          {cov.information.length > 0 && <p className="muted small">{cov.information.join(" ")}</p>}
        </>
      )}
      <p className="muted small cov-source">{cov.source}. Speeds are medians of crowdsourced tests near the Site; a fit is a guide, not a guarantee.</p>
    </div>
  );
}
