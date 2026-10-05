/**
 * The header slot on SOC pages, where the Customer picker sits elsewhere (the SOC works across customers):
 * my role, my presence (PresenceSelector, heartbeated by the stream provider), the roster count, the connection, and
 * the alarm sound (RingerToggle). Reads the SocStreamProvider App mounts around SOC pages.
 */
import type { Me } from "../api";
import { socRole } from "../access";
import { muteActive, ringer } from "./ringer";
import { useRinger, useSoc } from "./useSocStream";
import type { PresenceStatus } from "./types";

export function SocHeader({ me }: { me: Me }) {
  const role = socRole(me);
  const { conn } = useSoc();
  return (
    <div className="soc-header">
      <span className="chip small" title="Your SOC role on this hub">{role === "supervisor" ? "Supervisor" : "Operator"}</span>
      <PresenceSelector />
      <RingerToggle />
      {conn === "refused" ? <span className="chip small warn" role="status" title="The hub closed the SOC feed: you're signed out or no longer SOC staff">SOC feed refused: reload</span>
        : conn !== "live" && <span className="chip small warn" role="status" title="The live queue socket is reconnecting; the queue refreshes every 15 s meanwhile">{conn === "connecting" ? "Connecting…" : "Reconnecting…"}</span>}
    </div>
  );
}

const CHOICES: { id: PresenceStatus; label: string }[] = [{ id: "available", label: "Available" }, { id: "break", label: "On break" }, { id: "offline", label: "Away" }];

/** What I tell the hub about myself. "Engaged" is the hub's to set (I hold a claim), so it shows but isn't a choice. */
export function PresenceSelector() {
  const { myStatus, setStatus, myPresence, queue } = useSoc();
  const engaged = myPresence?.status === "engaged" && myStatus === "available";
  const on = queue.presence.filter((p) => p.status === "available" || p.status === "engaged").length;
  return (
    <label className="soc-presence">
      <span className="muted small">Status</span>
      <select value={myStatus} onChange={(e) => setStatus(e.target.value as PresenceStatus)} aria-label="My SOC status">
        {CHOICES.map((c) => <option key={c.id} value={c.id}>{c.id === "available" && engaged ? `Engaged${myPresence?.incident_id ? ` on #${myPresence.incident_id}` : ""}` : c.label}</option>)}
      </select>
      <span className="muted small soc-roster-count" title={queue.presence.map((p) => `${p.email}: ${p.status}`).join("\n") || "Nobody else is signed in"}>{on} on shift</span>
    </label>
  );
}

/**
 * Sound: "Enable sound" until a gesture unlocks audio; then a mute toggle (m) that lifts itself after 15 minutes.
 * The chip says when another tab is the one ringing, so a silent tab during an alarm isn't mistaken for a fault.
 */
export function RingerToggle() {
  const r = useRinger();
  const muted = muteActive(r.mutedUntil, Date.now());
  if (r.needsGesture) {
    return <button className="small soc-sound-enable" onClick={() => ringer().enable()} title="Browsers play sound only after you interact with the page">🔈 Enable sound</button>;
  }
  const until = muted && r.mutedUntil ? new Date(r.mutedUntil).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : "";
  return (
    <>
      <button className="ghost small" aria-pressed={muted} aria-keyshortcuts="M" onClick={() => ringer().toggleMute()}
        title={muted ? `Muted until ${until} (m to unmute)` : "Mute the alarm for 15 minutes (m)"}>{muted ? "🔕 Unmute" : "🔔 Sound on"}</button>
      {muted && <span className="chip small warn" role="status">Muted until {until}</span>}
      {!muted && r.ringingElsewhere && <span className="muted small" title="Only one tab rings; another tab of this console has the alarm">ringing in another tab</span>}
    </>
  );
}
