/**
 * One incident on its own page (/soc/incidents/:id): the pop-out window from the workstation (p) and the target of
 * SOC push notifications. Full width: the incident and its Respond/Resolve pane side by side, no queue. The same
 * keys work, except the queue's (j/k/Enter, Shift+C, /). No auto-advance: a pop-out is about this one incident.
 */
import { useEffect, useState } from "react";
import { toast } from "@site/ui";
import type { Me } from "../api";
import { go, socHref } from "../nav";
import type { Command } from "./chords";
import { HelpOverlay } from "./HelpOverlay";
import { IncidentView, type ViewCmd } from "./IncidentView";
import { type PaneTab, RightPane } from "./RightPane";
import { ringer } from "./ringer";
import { socApi } from "./socApi";
import { useChordHint, useChordKeys } from "./SocPage";
import { claimedByMe, useIncident, useIncidentActions, useNow } from "./useIncident";
import { useSoc } from "./useSocStream";

export function IncidentPage({ me, id }: { me: Me; id: number }) {
  const soc = useSoc();
  const now = useNow(1000);
  const { detail, incident, error, reload } = useIncident(id);
  const actions = useIncidentActions(reload);
  const [tab, setTab] = useState<PaneTab>("respond");
  const [cmd, setCmd] = useState<ViewCmd | null>(null);
  const [help, setHelp] = useState(false);
  const { hint, onPending } = useChordHint();
  const mine = claimedByMe(incident, me);
  useEffect(() => { document.title = `#${id} · SOC`; return () => { document.title = "Axiom Vision"; }; }, [id]);

  const run = (c: Command) => {
    const issue = (type: string, extra: Partial<ViewCmd> = {}) => setCmd({ type, nonce: Date.now() + Math.random(), ...extra });
    switch (c.type) {
      case "claim": if (incident?.state === "new") actions.run("claim", () => socApi.claim(id)); return;
      case "release": actions.run("release", () => socApi.release(id), "Released to the queue"); return;
      case "handoff": case "goLive": case "goTimeline": case "goDetails": issue(c.type); return;
      case "resolveTab": setTab("resolve"); return;
      case "goRespond": setTab("respond"); return;
      case "mute": ringer().toggleMute(); return;
      case "help": setHelp(true); return;
      case "escape": setHelp(false); return;
      case "disposition": setTab("resolve"); issue("disposition", { code: c.code }); return;
      case "sop": issue("sop", { index: c.index }); return;
      case "refuse": toast.info(c.reason); return;
      default: return;   // queue keys have no queue here
    }
  };
  useChordKeys(() => ({ tab, hasIncident: !!incident, claimedByMe: mine, groups: soc.groups }), run, onPending);

  if (!incident) return <p className="muted">{error ?? `Loading incident #${id}…`} <a href={socHref()} onClick={go(socHref())}>Queue</a></p>;
  return (
    <>
      {hint && <div className="soc-chord-hint" role="status">{hint}</div>}
      <div className="soc-popout">
        <section className="soc-col soc-col-main" aria-label="Incident">
          <IncidentView me={me} detail={detail} incident={incident} now={now} actions={actions} cmd={cmd} popout />
        </section>
        <aside className="soc-col soc-col-right" aria-label="Respond and resolve">
          <RightPane me={me} incident={incident} detail={detail} actions={actions} tab={tab} setTab={setTab} cmd={cmd} groups={soc.groups}
            onResolved={() => toast.info("Resolved. You can close this window.")} />
        </aside>
      </div>
      {help && <HelpOverlay groups={soc.groups} onClose={() => setHelp(false)} />}
    </>
  );
}
