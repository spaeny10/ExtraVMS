/* Settings → Cameras → Edit → Camera: the camera's own settings over ONVIF (backend camdevice.py), changed from here
   instead of the camera's web page: its clock, video and audio encoders, imaging, analytics rules, reboot. Each section
   has its own Apply that writes to the camera at once (not with the dialog's Save) and shows what the camera reads
   back. "Fix clocks…" (Settings › Cameras) sets several cameras' clocks at once. Pure logic: cameraDevice.ts.
   A Milesight camera that answers its web API also gets its lights, siren volume and one-click disarm here
   (backend milesight.py; the form's logic is deterrence.ts), and a Milesight traffic camera its plate reader's push
   to this server (TrafficSetup.tsx). */
import { useEffect, useId, useState } from "react";
import { BASE, api, type Camera } from "./api";
import { cameraWebUrl } from "./cameraEdit";
import {
  audioChanges, audioDraft, audioEncodings, audioPick, browserMute, clockSummary, encoderTitle, encodingLabel, govFrames,
  offsetBad, offsetText, res, slowSeeking, splitServers, videoChanges, videoDraft, videoOptions, videoProblems, zoneCheckText, zoneList,
  type AnalyticsRule, type AudioEncoder, type CameraDevice, type CameraTime, type ClockResult, type ImagingField, type ImagingSource,
  type TimeDefaults, type Unsupported, type VideoEncoder,
} from "./cameraDevice";
import { MUTE_LABELS, type MilesightDraft, type MilesightExtras, type MilesightWrite, type MsSection, milesightChanges, milesightDraft,
  milesightProblems, sectionBody } from "./deterrence";
import { TrafficSetupSection } from "./TrafficSetup";
import { confirmDialog, errorText, toast } from "./ui";

export function CameraDevicePanel({ camera, active }: { camera: Camera; active: boolean }) {
  const [dev, setDev] = useState<CameraDevice | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const load = async () => {
    setLoading(true); setErr("");
    try { setDev(await api.cameraDevice(camera.id)); }
    catch (e) { setErr(errorText(e)); }
    setLoading(false);
  };
  // asked when the tab is first shown (every request goes to the camera): not on every dialog open
  useEffect(() => { if (active && !dev && !loading && !err) load(); }, [active]); // eslint-disable-line react-hooks/exhaustive-deps

  const patch = (p: Partial<CameraDevice>) => setDev((d) => (d ? { ...d, ...p } : d));
  const info = dev?.info;
  return (
    <>
      <p className="saves-now small">Each section's Apply changes the camera at once, not with Save below. The camera's answer is shown as it reads back.</p>
      <div className="row device-head">
        <span className="small">
          {loading ? "Asking the camera…"
            : info?.model ? <><strong>{[info.manufacturer, info.model].filter(Boolean).join(" ")}</strong>{info.firmware && <span className="muted"> · firmware {info.firmware}</span>}</>
            : dev && !dev.reachable ? <span className="error">Can't reach the camera over ONVIF: {dev.error}</span> : null}
        </span>
        <span className="spacer" />
        <button className="ghost small" disabled={loading} onClick={load}>{loading ? "…" : "Read again"}</button>
      </div>
      {err && <p className="error small">{err}</p>}
      {loading && !dev && <p className="muted small">Reading the camera's settings (a slow camera takes a few seconds)…</p>}
      {dev && dev.reachable && <>
        <h3 className="spaced">Time</h3>
        <Supported s={dev.time}>{(t) => <TimeSection camera={camera} t={t} defaults={dev.defaults} onTime={(time) => patch({ time: { ...time, supported: true } })} />}</Supported>

        <h3 className="spaced">Video</h3>
        <Supported s={dev.video}>{(v) => <>
          <p className="hint small">Keyframes every 1–2 s make Timeline seeking faster; longer intervals save a little bandwidth.</p>
          {v.encoders.map((e) => (
            <VideoCard key={e.token} camera={camera} v={e} sources={v.sources}
              onSaved={(n) => patch({ video: { ...v, supported: true, encoders: v.encoders.map((x) => (x.token === n.token ? n : x)) } })} />
          ))}
        </>}</Supported>

        <h3 className="spaced">Audio</h3>
        <Supported s={dev.audio}>{(a) => <>
          <p className="hint small">AAC plays in browsers without conversion; G.711 is converted on this server for every playback.</p>
          {a.encoders.map((e) => (
            <AudioCard key={e.token} camera={camera} a={e}
              onSaved={(n) => patch({ audio: { ...a, supported: true, encoders: a.encoders.map((x) => (x.token === n.token ? n : x)) } })} />
          ))}
        </>}</Supported>

        <h3 className="spaced">Imaging</h3>
        <Supported s={dev.imaging}>{(im) => <>
          {im.sources.map((s, i) => (
            <ImagingCard key={s.source} camera={camera} s={s} title={im.sources.length > 1 ? `Sensor ${i + 1}` : null}
              onSaved={(n) => patch({ imaging: { ...im, supported: true, sources: im.sources.map((x) => (x.source === n.source ? n : x)) } })} />
          ))}
        </>}</Supported>

        <h3 className="spaced">Analytics rules</h3>
        <Supported s={dev.analytics}>{(an) => <AnalyticsSection camera={camera} configs={an.configurations}
          onRule={(cfg, r) => patch({ analytics: { ...an, supported: true, configurations: an.configurations.map((c) => (c.configuration === cfg
            ? { ...c, rules: c.rules.map((x) => (x.name === r.name ? r : x)) } : c)) } })} />}</Supported>
        <WebPageNote camera={camera} />

        <MilesightSection camera={camera} />
        <TrafficSetupSection camera={camera} />

        <h3 className="spaced">Restart</h3>
        <RebootSection camera={camera} />
      </>}
    </>
  );
}

/** A section's body, or "Not supported by this camera" (the camera's own words in red when it refused). */
function Supported<T>({ s, children }: { s: ({ supported: true } & T) | Unsupported; children: (v: { supported: true } & T) => React.ReactNode }) {
  if (s.supported) return <>{children(s)}</>;
  return s.error ? <p className="small error">The camera answered: {s.reason}</p>
    : <p className="small muted">Not supported by this camera{s.reason ? ` (${s.reason})` : ""}.</p>;
}

function Labeled({ label, hint, children }: { label: string; hint?: React.ReactNode; children: React.ReactNode }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint && <span className="hint small">{hint}</span>}
    </label>
  );
}

/** Apply's state: busy while the camera answers, its error verbatim. */
function useApply() {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const run = async (fn: () => Promise<void>) => {
    setBusy(true); setErr("");
    try { await fn(); } catch (e) { setErr(errorText(e)); }
    setBusy(false);
  };
  return { busy, err, run };
}

function OffsetBadge({ s }: { s: number | null | undefined }) {
  return <span className={`badge clock-badge ${offsetBad(s) ? "bad" : "ok"}`}>{offsetText(s)}</span>;
}

/* ------------------------------------------------------------------ time */

function TimeSection({ camera, t, defaults, onTime }: { camera: Camera; t: CameraTime; defaults: TimeDefaults; onTime: (t: CameraTime) => void }) {
  const [mode, setMode] = useState<"ntp" | "manual_sync">("ntp");
  const [servers, setServers] = useState(defaults.ntp_server);
  const [zone, setZone] = useState(defaults.timezone ?? "");
  const [warnings, setWarnings] = useState<string[]>([]);
  const { busy, err, run } = useApply();
  const list = useId();
  const zoneWarn = zoneCheckText(t.zone_check);
  const done = (r: CameraTime, what: string) => {
    onTime(r); setWarnings(r.warnings ?? []);
    toast.success(`${camera.name}: ${what}; the clock is ${offsetText(r.offset_s)}`);
  };
  const apply = () => run(async () => {
    const r = await api.setCameraTime(camera.id, { mode, ntp_servers: mode === "ntp" ? splitServers(servers) : [], timezone: zone || null });
    done(r, mode === "ntp" ? "time from NTP" : "time set from this server");
  });
  const sync = () => run(async () => done(await api.syncCameraTime(camera.id), "clock set from this server"));
  return (
    <div className="device-card">
      <p className="small">
        Camera clock: {t.camera_utc ? new Date(t.camera_utc).toLocaleString() : "unknown"} <OffsetBadge s={t.offset_s} />
        {offsetBad(t.offset_s) && <span className="muted"> compared with this server</span>}
      </p>
      <p className="small muted">
        {t.mode === "NTP" ? `NTP${t.ntp_servers.length ? ` (${t.ntp_servers.join(", ")})` : ""}` : t.mode === "Manual" ? "Set by hand (no NTP)" : t.mode ?? "?"}
        {" · "}time zone {t.timezone || "not set"}{t.dst ? " · daylight saving on" : ""}
      </p>
      {zoneWarn && <p className="small warn-text">{zoneWarn}</p>}
      {t.ntp_error && <p className="small muted">NTP servers not readable: {t.ntp_error}</p>}
      <div className="row">
        <Labeled label="Keep time by">
          <select value={mode} onChange={(e) => setMode(e.target.value as "ntp" | "manual_sync")}>
            <option value="ntp">NTP server</option>
            <option value="manual_sync">This server's clock (no NTP)</option>
          </select>
        </Labeled>
        {mode === "ntp" && (
          <Labeled label="NTP servers" hint={defaults.gateway ? `${defaults.gateway} is this server's gateway: only if the router answers NTP.` : undefined}>
            <input value={servers} list={list} placeholder="pool.ntp.org" onChange={(e) => setServers(e.target.value)} />
            <datalist id={list}>{defaults.suggestions.map((s) => <option key={s} value={s} />)}</datalist>
          </Labeled>
        )}
        <Labeled label="Time zone">
          <select value={zone} onChange={(e) => setZone(e.target.value)}>
            <option value="">Keep the camera's ({t.timezone || "none"})</option>
            {[...new Set([zone, ...zoneList([defaults.timezone, defaults.server_zone])].filter(Boolean))].map((z) => <option key={z} value={z}>{z}</option>)}
          </select>
        </Labeled>
      </div>
      <div className="row">
        <button disabled={busy || (mode === "ntp" && !splitServers(servers).length)} onClick={apply}>{busy ? "Applying…" : "Apply"}</button>
        <button className="ghost" disabled={busy} onClick={sync} title="Set the camera's clock from this server's now; a camera on NTP stays on NTP">Sync now</button>
        <span className="muted small">Sync now: for a camera that can't reach an NTP server, or to fix it at once.</span>
      </div>
      {warnings.map((w, i) => <p key={i} className="small warn-text">{w}</p>)}
      {err && <p className="small error">{err}</p>}
    </div>
  );
}

/* ------------------------------------------------------------------ video */

function VideoCard({ camera, v, sources, onSaved }: { camera: Camera; v: VideoEncoder; sources: string[]; onSaved: (v: VideoEncoder) => void }) {
  const [d, setD] = useState(() => videoDraft(v));
  useEffect(() => setD(videoDraft(v)), [v]);
  const { busy, err, run } = useApply();
  const o = videoOptions(v, d.encoding);
  const changes = videoChanges(v, d);
  const problems = videoProblems(v, d);
  const rate = Number(d.fps) || v.fps || 0;
  const gov = Number(d.gov_seconds);
  const encodings = v.options.encodings.map((x) => x.encoding);
  const apply = () => run(async () => {
    const n = await api.setVideoEncoder(camera.id, v.token, changes);
    onSaved(n);
    toast.success(`${camera.name}: ${encoderTitle(n, sources, camera.record_stream)} saved on the camera`);
  });
  const resolutions = o?.resolutions.length ? o.resolutions.map((r) => res(r.width, r.height)) : [res(v.width, v.height)];
  return (
    <div className="device-card">
      <div className="row"><strong className="small">{encoderTitle(v, sources, camera.record_stream)}</strong>
        <span className="muted small">{[encodingLabel(v.encoding), v.width && `${v.width}×${v.height}`, v.fps && `${v.fps} fps`,
          v.bitrate_kbps && `${v.bitrate_kbps} kbps`, v.gov_seconds != null && `keyframe every ${v.gov_seconds} s`].filter(Boolean).join(" · ")}</span></div>
      <div className="row">
        {v.media === "media2" && encodings.length > 1 && (
          <Labeled label="Encoding">
            <select value={d.encoding} onChange={(e) => setD({ ...d, encoding: e.target.value })}>
              {encodings.map((x) => <option key={x} value={x}>{encodingLabel(x)}</option>)}
            </select>
          </Labeled>
        )}
        <Labeled label="Resolution">
          <select value={d.resolution} onChange={(e) => setD({ ...d, resolution: e.target.value })}>
            {[...new Set([d.resolution, ...resolutions].filter(Boolean))].map((r) => <option key={r} value={r}>{r.replace("x", "×")}</option>)}
          </select>
        </Labeled>
        <Labeled label="Frame rate (fps)" hint={o?.fps ? `${o.fps.min}–${o.fps.max}` : undefined}>
          {o?.fps_list.length
            ? <select value={d.fps} onChange={(e) => setD({ ...d, fps: e.target.value })}>
                {[...new Set([d.fps, ...o.fps_list.map(String)].filter(Boolean))].map((f) => <option key={f} value={f}>{f}</option>)}
              </select>
            : <input type="number" min={o?.fps?.min} max={o?.fps?.max} value={d.fps} onChange={(e) => setD({ ...d, fps: e.target.value })} />}
        </Labeled>
        <Labeled label="Bitrate (kbps)" hint={o?.bitrate ? `${o.bitrate.min}–${o.bitrate.max}` : undefined}>
          <input type="number" min={o?.bitrate?.min} max={o?.bitrate?.max} step={128} value={d.bitrate_kbps} onChange={(e) => setD({ ...d, bitrate_kbps: e.target.value })} />
        </Labeled>
        <Labeled label="Keyframe every (s)" hint={rate && gov > 0 ? `= ${govFrames(gov, rate)} frames` : undefined}>
          <input type="number" min={0.5} step={0.5} value={d.gov_seconds} onChange={(e) => setD({ ...d, gov_seconds: e.target.value })} />
        </Labeled>
      </div>
      {slowSeeking(v.gov_seconds) && <p className="small warn-text">A keyframe only every {v.gov_seconds} s: Timeline seeking waits for one. 1–2 s is faster.</p>}
      {v.options.error && <p className="small muted">The camera didn't list its options ({v.options.error}); it checks the values itself.</p>}
      {problems.map((p, i) => <p key={i} className="small error">{p}</p>)}
      <div className="row">
        <button disabled={busy || !Object.keys(changes).length || problems.length > 0} onClick={apply}>{busy ? "Applying…" : "Apply"}</button>
        {Object.keys(changes).length > 0 && <button className="ghost small" disabled={busy} onClick={() => setD(videoDraft(v))}>Undo</button>}
        {v.stream && Object.keys(changes).length > 0 && <span className="muted small">The live view and recording reconnect for a few seconds.</span>}
      </div>
      {err && <p className="small error">{err}</p>}
    </div>
  );
}

/* ------------------------------------------------------------------ audio */

function AudioCard({ camera, a, onSaved }: { camera: Camera; a: AudioEncoder; onSaved: (a: AudioEncoder) => void }) {
  const [d, setD] = useState(() => audioDraft(a));
  useEffect(() => setD(audioDraft(a)), [a]);
  const { busy, err, run } = useApply();
  const o = a.options.find((x) => x.encoding === d.encoding);
  const changes = audioChanges(a, d);
  const apply = () => run(async () => {
    const n = await api.setAudioEncoder(camera.id, a.token, changes);
    onSaved(n);
    toast.success(`${camera.name}: audio is now ${encodingLabel(n.encoding)}`);
  });
  return (
    <div className="device-card">
      <div className="row"><strong className="small">{a.name || a.token}</strong>
        <span className="muted small">{[encodingLabel(a.encoding), a.bitrate && `${a.bitrate} kbps`, a.sample_rate && `${a.sample_rate} kHz`].filter(Boolean).join(" · ")}</span>
        {browserMute(a.encoding) && <span className="badge">converted for browsers</span>}</div>
      {a.options.length ? (
        <div className="row">
          <Labeled label="Codec">
            <select value={d.encoding} onChange={(e) => setD(audioPick(a, d, e.target.value))}>
              {audioEncodings(a).map((x) => <option key={x} value={x}>{encodingLabel(x)}</option>)}
            </select>
          </Labeled>
          {o && o.bitrates.length > 0 && (
            <Labeled label="Bitrate (kbps)">
              <select value={d.bitrate} onChange={(e) => setD({ ...d, bitrate: e.target.value })}>
                {o.bitrates.map((b) => <option key={b} value={String(b)}>{b}</option>)}
              </select>
            </Labeled>
          )}
          {o && o.sample_rates.length > 0 && (
            <Labeled label="Sample rate (kHz)">
              <select value={d.sample_rate} onChange={(e) => setD({ ...d, sample_rate: e.target.value })}>
                {o.sample_rates.map((r) => <option key={r} value={String(r)}>{r}</option>)}
              </select>
            </Labeled>
          )}
        </div>
      ) : <p className="small muted">The camera lists no audio options{a.options_error ? ` (${a.options_error})` : ""}.</p>}
      <div className="row">
        <button disabled={busy || !Object.keys(changes).length} onClick={apply}>{busy ? "Applying…" : "Apply"}</button>
        {d.encoding === "AAC" && browserMute(a.encoding) && <span className="muted small">Footage recorded before the change stays {encodingLabel(a.encoding)}.</span>}
      </div>
      {err && <p className="small error">{err}</p>}
    </div>
  );
}

/* ------------------------------------------------------------------ imaging */

const IMAGING_LABELS: Record<ImagingField, string> = { brightness: "Brightness", contrast: "Contrast", saturation: "Saturation", sharpness: "Sharpness" };
const IR_LABELS: Record<string, string> = { AUTO: "Automatic", ON: "Day (colour)", OFF: "Night (black & white)" };

function ImagingCard({ camera, s, title, onSaved }: { camera: Camera; s: ImagingSource; title: string | null; onSaved: (s: ImagingSource) => void }) {
  const initial = () => ({
    ...Object.fromEntries((Object.keys(IMAGING_LABELS) as ImagingField[]).map((k) => [k, s[k]?.value != null ? String(s[k]!.value) : ""])),
    ir_cut: s.ir_cut?.value ?? "",
  }) as Record<ImagingField | "ir_cut", string>;
  const [d, setD] = useState(initial);
  useEffect(() => setD(initial()), [s]); // eslint-disable-line react-hooks/exhaustive-deps
  const { busy, err, run } = useApply();
  const fields = (Object.keys(IMAGING_LABELS) as ImagingField[]).filter((k) => s[k]);
  const changes: Record<string, string | number> = {};
  for (const k of fields) if (d[k] !== "" && Number(d[k]) !== s[k]!.value) changes[k] = Number(d[k]);
  if (s.ir_cut && d.ir_cut && d.ir_cut !== s.ir_cut.value) changes.ir_cut = d.ir_cut;
  const bad = fields.filter((k) => {
    const r = s[k]!.range, v = Number(d[k]);
    return d[k] !== "" && (!Number.isFinite(v) || (r != null && (v < r.min || v > r.max)));
  });
  if (!fields.length && !s.ir_cut) return <p className="small muted">{title ? `${title}: ` : ""}No imaging settings over ONVIF.</p>;
  const apply = () => run(async () => {
    onSaved(await api.setImaging(camera.id, s.source, changes));
    toast.success(`${camera.name}: imaging saved on the camera`);
  });
  return (
    <div className="device-card">
      {title && <strong className="small">{title}</strong>}
      <div className="row">
        {fields.map((k) => (
          <Labeled key={k} label={IMAGING_LABELS[k]} hint={s[k]!.range ? `${s[k]!.range!.min}–${s[k]!.range!.max}` : undefined}>
            <input type="number" min={s[k]!.range?.min} max={s[k]!.range?.max} value={d[k]} onChange={(e) => setD({ ...d, [k]: e.target.value })} />
          </Labeled>
        ))}
        {s.ir_cut && (
          <Labeled label="Day / night">
            <select value={d.ir_cut} onChange={(e) => setD({ ...d, ir_cut: e.target.value })}>
              {[...new Set([s.ir_cut.value, ...s.ir_cut.modes])].filter(Boolean).map((m) => <option key={m} value={m}>{IR_LABELS[m] ?? m}</option>)}
            </select>
          </Labeled>
        )}
      </div>
      {bad.length > 0 && <p className="small error">Outside the camera's range: {bad.map((k) => IMAGING_LABELS[k]).join(", ")}.</p>}
      <button disabled={busy || !Object.keys(changes).length || bad.length > 0} onClick={apply}>{busy ? "Applying…" : "Apply"}</button>
      {err && <p className="small error">{err}</p>}
    </div>
  );
}

/* ------------------------------------------------------------------ analytics, reboot */

function AnalyticsSection({ camera, configs, onRule }: {
  camera: Camera; configs: { configuration: string; rules: AnalyticsRule[]; error?: string }[]; onRule: (cfg: string, r: AnalyticsRule) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState("");
  const toggle = async (cfg: string, r: AnalyticsRule) => {
    setBusy(r.name); setErr("");
    try {
      const n = await api.setAnalyticsRule(camera.id, { configuration: cfg, rule: r.name, enabled: !r.enabled });
      onRule(cfg, n);
      toast.success(`${camera.name}: ${r.name} ${n.enabled ? "on" : "off"}`);
    } catch (e) { setErr(errorText(e)); }
    setBusy(null);
  };
  const shown = (r: AnalyticsRule) => Object.entries(r.params).filter(([k, v]) => k !== r.toggle && v.length <= 24).map(([k, v]) => `${k} ${v}`).join(", ");
  return (
    <div className="device-card">
      {configs.map((c) => (
        <div key={c.configuration}>
          {configs.length > 1 && <strong className="small">{c.configuration}</strong>}
          {c.error && <p className="small error">The camera answered: {c.error}</p>}
          {!c.error && !c.rules.length && <p className="small muted">No rules.</p>}
          <ul className="device-rules">
            {c.rules.map((r) => (
              <li key={r.name} className="row small">
                <span><strong>{r.name}</strong> <span className="muted">{r.type}{shown(r) ? ` · ${shown(r)}` : ""}</span></span>
                <span className="spacer" />
                {r.toggle
                  ? <button className={r.enabled ? "small" : "ghost small"} disabled={busy !== null} onClick={() => toggle(c.configuration, r)}
                      title={`Turn ${r.enabled ? "off" : "on"} on the camera (its ${r.toggle} parameter)`}>{busy === r.name ? "…" : r.enabled ? "On" : "Off"}</button>
                  : <span className="muted">read-only</span>}
              </li>
            ))}
          </ul>
        </div>
      ))}
      {err && <p className="small error">{err}</p>}
    </div>
  );
}

/** Rule areas, sensitivity, schedules and the camera's smart detection: on its own web page. */
function WebPageNote({ camera }: { camera: Camera }) {
  const url = BASE ? null : cameraWebUrl(camera);
  return (
    <p className="small muted">Detection areas, sensitivity and the camera's smart detection are set on its own web page
      {url ? <>: <a href={url} target="_blank" rel="noopener noreferrer">open camera web page ↗</a></> : " (from the hub: Site › Settings › Cameras › Camera web pages)"}.</p>
  );
}

function RebootSection({ camera }: { camera: Camera }) {
  const { busy, err, run } = useApply();
  const reboot = async () => {
    if (!(await confirmDialog(`Restart ${camera.name}?`, {
      message: "The camera stops streaming and recording for about a minute while it restarts.", confirmLabel: "Restart", danger: true,
    }))) return;
    await run(async () => {
      const r = await api.rebootCamera(camera.id);
      toast.success(`${camera.name}: ${r.message || "restarting"}`);
    });
  };
  return (
    <div className="row">
      <button className="ghost" disabled={busy} onClick={reboot}>{busy ? "…" : "Restart camera…"}</button>
      <span className="muted small">Recording stops for about a minute.</span>
      {err && <span className="small error">{err}</span>}
    </div>
  );
}

/* ------------------------------------------------------------------ Milesight extras */

const WHITE_MODES: Record<MilesightDraft["white_light"]["mode"], string> = { auto: "Automatic (on detection)", on: "Always on", off: "Off", schedule: "On a schedule" };
const RB_MODES: Record<MilesightDraft["red_blue"]["mode"], string> = { red_blue: "Red and blue", red: "Red", blue: "Blue" };
const FREQS: Record<MilesightDraft["red_blue"]["frequency"], string> = { low: "Slow", medium: "Medium", high: "Fast" };

/** A Milesight part the camera doesn't have, or that its web API refused (its reason). */
function MsUnsupported({ part }: { part: { supported: boolean; reason?: string } | undefined }) {
  return <p className="small muted">Not supported by this camera{part?.reason ? ` (${part.reason})` : ""}.</p>;
}

/** Shown only when the camera answers Milesight's web API; each part has its own Apply, and a part it won't let us
 *  write is shown read-only. */
function MilesightSection({ camera }: { camera: Camera }) {
  const [m, setM] = useState<MilesightExtras | null>(null);
  const [d, setD] = useState<MilesightDraft | null>(null);
  useEffect(() => {
    let alive = true;
    api.milesight(camera.id).then((x) => { if (alive) { setM(x); setD(milesightDraft(x)); } }).catch(() => {});   // not a Milesight: nothing shown
    return () => { alive = false; };
  }, [camera.id]);
  if (!m?.supported || !d) return null;
  const body = milesightChanges(m, d);
  const problems = milesightProblems(m, d);
  /** the camera's answer: what it reads back replaces that part of the form (the other parts keep what was typed) */
  const saved = (part: MsSection) => (r: MilesightExtras) => {
    setM(r);
    const fresh = milesightDraft(r);
    setD((cur) => (cur ? { ...cur, [part]: fresh[part] } : fresh));
  };
  const apply = (part: MsSection, label: string) => (
    <MsApply camera={camera} body={sectionBody(body, part)} problems={problems[part] ?? []} label={label} onSaved={saved(part)} />
  );
  const wl = m.white_light, rb = m.red_blue, siren = m.siren, dz = m.disarm;
  const w = d.white_light;
  const mutes = dz ? [...Object.keys(MUTE_LABELS).filter((k) => k in dz.mutes), ...Object.keys(dz.mutes).filter((k) => !(k in MUTE_LABELS))] : [];
  return <>
    <h3 className="spaced">White light</h3>
    {!wl?.supported ? <MsUnsupported part={wl} /> : (
      <div className="device-card">
        <fieldset disabled={!wl.writable} className="row ms-fields">
          <Labeled label="Mode">
            <select value={w.mode} onChange={(e) => setD({ ...d, white_light: { ...w, mode: e.target.value as typeof w.mode } })}>
              {Object.entries(WHITE_MODES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </Labeled>
          {w.mode === "auto" && <>
            <Labeled label="Sensitivity" hint="1–5"><input type="number" min={1} max={5} value={w.sensitivity} onChange={(e) => setD({ ...d, white_light: { ...w, sensitivity: e.target.value } })} /></Labeled>
            <Labeled label="Stays on (s)" hint="1–60 after the detection"><input type="number" min={1} max={60} value={w.delay_s} onChange={(e) => setD({ ...d, white_light: { ...w, delay_s: e.target.value } })} /></Labeled>
          </>}
          {w.mode === "schedule" && <>
            <Labeled label="On at"><input type="time" value={w.start} onChange={(e) => setD({ ...d, white_light: { ...w, start: e.target.value } })} /></Labeled>
            <Labeled label="Off at"><input type="time" value={w.stop} onChange={(e) => setD({ ...d, white_light: { ...w, stop: e.target.value } })} /></Labeled>
          </>}
          {w.mode !== "off" && (
            <Labeled label="Brightness" hint="1–100"><input type="number" min={1} max={100} value={w.brightness} onChange={(e) => setD({ ...d, white_light: { ...w, brightness: e.target.value } })} /></Labeled>
          )}
        </fieldset>
        {wl.writable ? apply("white_light", "white light") : <p className="small muted">Read-only on this camera.</p>}
      </div>
    )}

    <h3 className="spaced">Red and blue light</h3>
    {!rb?.supported ? <MsUnsupported part={rb} /> : (
      <div className="device-card">
        <fieldset disabled={!rb.writable} className="row ms-fields">
          <Labeled label="Colours">
            <select value={d.red_blue.mode} onChange={(e) => setD({ ...d, red_blue: { ...d.red_blue, mode: e.target.value as typeof d.red_blue.mode } })}>
              {Object.entries(RB_MODES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </Labeled>
          <Labeled label="Flashing">
            <select value={d.red_blue.frequency} onChange={(e) => setD({ ...d, red_blue: { ...d.red_blue, frequency: e.target.value as typeof d.red_blue.frequency } })}>
              {Object.entries(FREQS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </Labeled>
        </fieldset>
        {rb.writable ? apply("red_blue", "red and blue light") : <p className="small muted">Read-only on this camera.</p>}
      </div>
    )}

    <h3 className="spaced">Siren</h3>
    {!siren?.supported ? <MsUnsupported part={siren} /> : (
      <div className="device-card">
        <p className="small">
          Sound: {siren.files.find((f) => f.index === siren.play_index)?.name ?? `file ${siren.play_index}`}
          {siren.files.length > 1 && <span className="muted"> · on the camera: {siren.files.filter((f) => f.present).map((f) => f.name).join(", ") || "none"}</span>}
        </p>
        {!siren.file_writable && <p className="small muted">The sound is picked on the camera's own web page.</p>}
        {siren.volume_writable ? <>
          <div className="row">
            <Labeled label="Speaker volume" hint="0–100">
              <input type="number" min={0} max={100} value={d.speaker_volume} onChange={(e) => setD({ ...d, speaker_volume: e.target.value })} />
            </Labeled>
          </div>
          {apply("speaker_volume", "speaker volume")}
        </> : <p className="small muted">Speaker volume {siren.speaker_volume} (read-only on this camera).</p>}
      </div>
    )}

    <h3 className="spaced">One-click disarm</h3>
    {!dz?.supported ? <MsUnsupported part={dz} /> : (
      <div className="device-card">
        <p className="hint small">While disarm is on, the camera's own alarm actions ticked below are muted. Deterrence switched from this server is not affected.</p>
        <fieldset disabled={!dz.writable} className="ms-fields">
          <label className="row small">
            <input type="checkbox" checked={d.disarm.enable} onChange={(e) => setD({ ...d, disarm: { ...d.disarm, enable: e.target.checked } })} /> Disarm on
          </label>
          <div className="row">
            {mutes.map((k) => (
              <label key={k} className="row small">
                <input type="checkbox" checked={!!d.disarm.mutes[k]} onChange={(e) => setD({ ...d, disarm: { ...d.disarm, mutes: { ...d.disarm.mutes, [k]: e.target.checked } } })} />
                {MUTE_LABELS[k] ?? k}
              </label>
            ))}
          </div>
        </fieldset>
        {dz.writable ? apply("disarm", "disarm") : <p className="small muted">Read-only on this camera.</p>}
      </div>
    )}
  </>;
}

function MsApply({ camera, body, problems, label, onSaved }: {
  camera: Camera; body: MilesightWrite; problems: string[]; label: string; onSaved: (r: MilesightExtras) => void;
}) {
  const { busy, err, run } = useApply();
  const go = () => run(async () => {
    onSaved(await api.setMilesight(camera.id, body));
    toast.success(`${camera.name}: ${label} saved on the camera`);
  });
  return <>
    {problems.map((p) => <p key={p} className="small error">{p}</p>)}
    <button disabled={busy || !Object.keys(body).length || problems.length > 0} onClick={go}>{busy ? "Applying…" : "Apply"}</button>
    {err && <p className="small error">{err}</p>}
  </>;
}

/* ------------------------------------------------------------------ Fix clocks… */

/** Settings › Cameras › "Fix clocks…": the same NTP server and time zone on the chosen cameras, with each one's result. */
export function FixClocksDialog({ cameras, onClose }: { cameras: Camera[]; onClose: () => void }) {
  const [defaults, setDefaults] = useState<TimeDefaults | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(() => new Set(cameras.filter((c) => c.enabled).map((c) => c.id)));
  const [mode, setMode] = useState<"ntp" | "manual_sync">("ntp");
  const [servers, setServers] = useState("");
  const [zone, setZone] = useState("");
  const [results, setResults] = useState<ClockResult[] | null>(null);
  const { busy, err, run } = useApply();
  const list = useId();
  useEffect(() => {
    api.cameraTimeDefaults().then((d) => { setDefaults(d); setServers(d.ntp_server); setZone(d.timezone ?? ""); })
      .catch(() => setServers("pool.ntp.org"));
  }, []);
  const apply = () => run(async () => {
    const r = await api.fixClocks({ camera_ids: [...chosen], mode, ntp_servers: mode === "ntp" ? splitServers(servers) : [], timezone: zone || null, remember: true });
    setResults(r.results);
    toast.success(`Clocks: ${clockSummary(r.results)}`);
  });
  const flip = (id: string) => setChosen((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  // what the metadata reader measured (arrival minus camera time): a hint of which clocks are off, before asking them
  const measured = (c: Camera) => (c.status as { clock_offset?: number | null } | undefined)?.clock_offset;
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal fix-clocks" role="dialog" aria-modal="true" aria-label="Fix camera clocks" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <h2>Fix camera clocks</h2>
          <button className="ghost" onClick={onClose} aria-label="Close">✕</button>
        </header>
        <div className="form">
          <p className="small muted">Sets each camera's NTP server and time zone over ONVIF, and a camera still off is set from this server's clock at once.
            Cameras whose clocks are off show detections at the wrong time on the Timeline.</p>
          <div className="row">
            <Labeled label="Keep time by">
              <select value={mode} onChange={(e) => setMode(e.target.value as "ntp" | "manual_sync")}>
                <option value="ntp">NTP server</option>
                <option value="manual_sync">This server's clock (no NTP)</option>
              </select>
            </Labeled>
            {mode === "ntp" && (
              <Labeled label="NTP server" hint={defaults?.gateway ? `Gateway ${defaults.gateway}: only if the router answers NTP.` : undefined}>
                <input value={servers} list={list} onChange={(e) => setServers(e.target.value)} />
                <datalist id={list}>{(defaults?.suggestions ?? []).map((s) => <option key={s} value={s} />)}</datalist>
              </Labeled>
            )}
            <Labeled label="Time zone" hint={defaults?.posix && zone === defaults.timezone ? `sent as ${defaults.posix}` : undefined}>
              <select value={zone} onChange={(e) => setZone(e.target.value)}>
                <option value="">Keep each camera's</option>
                {[...new Set([zone, ...zoneList([defaults?.timezone, defaults?.server_zone])].filter(Boolean))].map((z) => <option key={z} value={z}>{z}</option>)}
              </select>
            </Labeled>
          </div>
          <table className="table small">
            <thead><tr><th /><th>Camera</th><th>Clock (measured)</th><th>Result</th></tr></thead>
            <tbody>
              {cameras.map((c) => {
                const r = results?.find((x) => x.camera_id === c.id);
                const m = measured(c);
                return (
                  <tr key={c.id} className={c.enabled ? "" : "muted"}>
                    <td><input type="checkbox" checked={chosen.has(c.id)} onChange={() => flip(c.id)} aria-label={`Fix ${c.name}`} /></td>
                    <td>{c.name} <span className="muted">{c.id}</span></td>
                    <td>{m != null ? <OffsetBadge s={-m} /> : <span className="muted">—</span>}</td>
                    <td>{!r ? null : r.ok
                      ? <><OffsetBadge s={r.offset_s} /> <span className="muted">{r.mode}{r.timezone ? ` · ${r.timezone}` : ""}</span>
                          {zoneCheckText(r.zone_check) && <div className="warn-text">{zoneCheckText(r.zone_check)}</div>}
                          {(r.warnings ?? []).map((w, i) => <div key={i} className="muted">{w}</div>)}</>
                      : <span className="error">{r.error}</span>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {err && <p className="small error">{err}</p>}
          <div className="row">
            <span className="muted small">{results ? clockSummary(results) : `${chosen.size} camera${chosen.size === 1 ? "" : "s"} chosen`}</span>
            <span className="spacer" />
            <button className="ghost" onClick={onClose}>Close</button>
            <button disabled={busy || !chosen.size || (mode === "ntp" && !splitServers(servers).length)} onClick={apply}>{busy ? "Applying…" : "Apply"}</button>
          </div>
        </div>
      </div>
    </div>
  );
}
