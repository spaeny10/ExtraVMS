/**
 * The header slot on SOC pages, where the Customer picker sits elsewhere (the SOC works across customers).
 * Stage-1 stubs: presence (Available / On break / Away, heartbeated to /api/soc/presence) and the alarm-sound mute
 * (soc/ringer.ts) arrive with the operator workstation; they are shown now so the header's shape doesn't jump later.
 */
import type { Me } from "../api";
import { socRole } from "../access";

export function SocHeader({ me }: { me: Me }) {
  const role = socRole(me);
  return (
    <div className="soc-header">
      <span className="chip small" title="Your SOC role on this hub">{role === "supervisor" ? "Supervisor" : "Operator"}</span>
      <label className="soc-presence"><span className="muted small">Status</span>
        <select disabled value="available" title="Presence arrives with the operator workstation">
          <option value="available">Available</option>
        </select>
      </label>
      <button className="ghost small" disabled title="Alarm sound arrives with the operator workstation" aria-pressed={false}>🔔 Sound on</button>
    </div>
  );
}
