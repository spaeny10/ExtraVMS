/**
 * Deterrence on a camera (backend deterrence.py): its own strobe, siren and white light (and alarm outputs no action
 * uses), each one tap on and a tap again off, with the seconds left counting down on the button, and Stop, which turns
 * everything off, always there. An action on a running output replaces it (a new countdown). The state is re-read every
 * couple of seconds while something runs (rules switch outputs too) and the countdown ticks locally between reads.
 * DeterrencePanel docks it under a live tile like the talk-down panel; TalkdownPanel and the SOC incident view embed
 * DeterrenceControl. `site` is the camera's server (on the hub its calls go through the hub, which needs operator).
 * The logic is deterrence.ts.
 */
import { useEffect, useState } from "react";
import { api, fmtWhen, type SiteApi } from "./api";
import { IDLE_POLL_MS, RUN_POLL_MS, TICK_MS, type DeterButton, type Deterrence, anyOn, countdownText, deterButtons, deterRequest,
  errorStatus, lastText, outputNow, secondsChoices, unavailableText } from "./deterrence";
import { Icon, errorText, toast } from "./ui";

/** A camera's deterrence state, read now and re-read while the control is open; `gone`: the server has none for it. */
function useDeterrence(cameraId: string, site: SiteApi) {
  const [d, setD] = useState<Deterrence | null>(null);
  const [at, setAt] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());
  const [err, setErr] = useState<{ status: number; text: string } | null>(null);
  const [seq, setSeq] = useState(0);   // bumped to read again at once (after an action failed)
  const take = (x: Deterrence) => { const t = Date.now(); setD(x); setAt(t); setNow(t); setErr(null); };
  const running = anyOn(d, (now - at) / 1000);
  const gone = err?.status === 404 || err?.status === 409;

  useEffect(() => { setD(null); setErr(null); }, [site, cameraId]);
  useEffect(() => {
    if (gone) return;
    let alive = true;
    const pull = () => site.deterrence(cameraId)
      .then((x) => { if (alive) take(x); })
      .catch((e) => { if (alive) setErr({ status: errorStatus(e), text: errorText(e) }); });
    pull();
    const t = setInterval(pull, running ? RUN_POLL_MS : IDLE_POLL_MS);
    return () => { alive = false; clearInterval(t); };
  }, [site, cameraId, running, gone, seq]);
  useEffect(() => {   // the countdowns between reads
    if (!running) return;
    const t = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(t);
  }, [running]);
  return { d, err, gone, since: Math.max(0, (now - at) / 1000), take, reread: () => setSeq((n) => n + 1) };
}

export function DeterrenceControl({ cameraId, site = api, embedded = false }: {
  cameraId: string;
  /** the camera's server (default: this server) */
  site?: SiteApi;
  /** inside another panel (talk-down, the SOC): a small heading, and nothing at all when the camera has no outputs */
  embedded?: boolean;
}) {
  const { d, err, since, take, reread } = useDeterrence(cameraId, site);
  const [busy, setBusy] = useState<string | null>(null);
  const [seconds, setSeconds] = useState<number | null>(null);
  const buttons = d ? deterButtons(d) : [];
  const secs = seconds ?? d?.default_s ?? 10;
  const none = d ? unavailableText(d) : null;

  const send = async (key: string, label: string, run: () => ReturnType<SiteApi["deter"]>) => {
    setBusy(key);
    try { take(await run()); }
    catch (e) { toast.error(`${label}: ${errorText(e)}`); reread(); }
    finally { setBusy(null); }
  };
  const press = (b: DeterButton) => {
    const on = outputNow(d?.state[b.key], since).on;
    return send(b.key, b.label, () => site.deter(cameraId, deterRequest(b, on, secs)));
  };
  const stop = () => send("stop", "Stop", () => site.deter(cameraId, { action: "stop" }));

  if (embedded && (!d || none || !buttons.length)) return null;
  const head = embedded && <div className="talk-hw-title">Deterrence</div>;
  if (!d) {
    return err ? <div className={err.status === 503 ? "talk-dim" : "talk-error"}>
      {err.status === 503 ? "The camera hasn't been asked what it can drive yet; trying again…" : err.text}</div>
      : <div className="talk-dim">Reading the camera's outputs…</div>;
  }
  if (none) return <div className="talk-dim">{none}</div>;
  const last = lastText(d.last, buttons);
  const running = buttons.some((b) => outputNow(d.state[b.key], since).on);
  return (
    <div className={`deter ${embedded ? "embedded" : ""}`} role="group" aria-label="Deterrence">
      {head}
      <div className="deter-btns">
        {buttons.map((b) => {
          const st = outputNow(d.state[b.key], since);
          return (
            <button key={b.key} type="button" className={`talk-act deter-btn ${st.on ? "on" : ""}`} aria-pressed={st.on} disabled={busy !== null && busy !== "stop"}
              title={st.on ? `${b.title}: on${st.left != null ? `, ${countdownText(st.left)} left` : ""}; tap to switch off`
                : `${b.title}${b.timed ? `: on for ${secs} s` : ""}`}
              onClick={() => press(b)}>
              <Icon name={b.icon} size={18} />
              <span>{b.label}</span>
              {st.on && <span className="deter-left">{countdownText(st.left)}</span>}
            </button>
          );
        })}
        <button type="button" className="deter-stop" onClick={stop} disabled={busy === "stop"} aria-label="Stop: turn every output off"
          title="Turn every output of this camera off"><Icon name="stop" size={16} /> STOP</button>
      </div>
      <div className="deter-foot">
        {buttons.some((b) => b.timed) && (
          <label className="deter-secs" title="How long an output stays on (tap it again to switch it off sooner)">
            For <select value={secs} onChange={(e) => setSeconds(Number(e.target.value))} aria-label="Seconds on">
              {secondsChoices(d.max_s, d.default_s).map((s) => <option key={s} value={s}>{s} s</option>)}
            </select>
          </label>
        )}
        {/* announced when what runs changes, not every tick */}
        <span className="sr-only" aria-live="polite">{running ? "Deterrence on" : ""}</span>
        {last && <span className={`deter-last ${d.last?.result === "failed" ? "talk-error" : "talk-dim"}`}>
          Last: {last}{d.last ? ` · ${fmtWhen(d.last.ts)}` : ""}</span>}
      </div>
    </div>
  );
}

/** Docked under a live tile (never over the picture: the operator watches the person while the siren sounds). */
export function DeterrencePanel({ cameraId, site = api, onClose }: { cameraId: string; site?: SiteApi; onClose: () => void }) {
  const halt = (e: React.SyntheticEvent) => e.stopPropagation();   // not a paint stroke, zoom or PTZ drag on the picture
  return (
    <div className="talk-panel deter-panel" role="region" aria-label="Deterrence" onPointerDown={halt} onClick={halt} onDoubleClick={halt} onWheel={halt}
      onKeyDown={(e) => { if (e.key === "Escape") onClose(); }}>
      <div className="talk-head">
        <span className="talk-title"><Icon name="siren" size={18} /> Deterrence</span>
        <span className="spacer" />
        <button className="ghost talk-close" onClick={onClose} aria-label="Close deterrence" title="Close (Esc); outputs that are on stay on until their time is up">
          <Icon name="x" size={18} />
        </button>
      </div>
      <DeterrenceControl cameraId={cameraId} site={site} />
    </div>
  );
}
