/**
 * One incident's detail (events, log, contacts, procedures, SOP progress) and the actions on it. The queue row comes
 * from the socket; the detail is refetched whenever that row changes (someone claimed, logged a call, another event
 * joined), so the log a teammate writes shows up without a reload.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "@site/ui";
import type { Me, Org } from "../api";
import { socApi } from "./socApi";
import { useSoc } from "./useSocStream";
import type { Incident, IncidentDetail } from "./types";

export function useNow(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => { const t = setInterval(() => setNow(Date.now() / 1000), ms); return () => clearInterval(t); }, [ms]);
  return now;
}

export function useIncident(id: number | null) {
  const soc = useSoc();
  const [detail, setDetail] = useState<IncidentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const row = id == null ? undefined : soc.queue.incidents.find((i) => i.id === id);
  const seq = useRef(0);
  const reload = useCallback(() => {
    if (id == null) return;
    const n = ++seq.current;
    socApi.incident(id).then((d) => { if (n === seq.current) { setDetail(d); setError(null); } })
      .catch((e: Error) => { if (n === seq.current) setError(e.message.startsWith("404") ? "This incident doesn't exist." : e.message.startsWith("403") ? "You can't see this incident." : e.message); });
  }, [id]);
  useEffect(() => { setDetail(null); setError(null); reload(); }, [reload]);
  // the socket row moved on: refetch (row.updated_at is the hub's own change marker)
  const stamp = row ? `${row.updated_at}:${row.event_count}:${row.state}:${row.claimed_by}` : "";
  useEffect(() => { if (stamp && detail && detail.incident.id === id) reload(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [stamp]);
  // the freshest row: the socket's when it is newer than the detail's
  const incident = detail && row && row.updated_at > detail.incident.updated_at ? { ...detail.incident, ...row } : detail?.incident ?? row ?? null;
  return { detail, incident, error, reload, setDetail };
}

const is409 = (e: unknown) => /^(Error: )?409\b/.test(String(e instanceof Error ? e.message : e));

/**
 * Runs an action with one in-flight guard (double clicks and a held key send one request). A 409 (someone claimed it
 * first, it was already resolved) shows the hub's own words, which name the claimer and how long ago, then refreshes
 * both the detail and the queue so the screen shows who has it now.
 */
export function useIncidentActions(reload: () => void) {
  const soc = useSoc();
  const [busy, setBusy] = useState<string | null>(null);
  const inFlight = useRef(false);
  const run = useCallback(async <T,>(name: string, fn: () => Promise<T>, ok?: string): Promise<T | null> => {
    if (inFlight.current) return null;
    inFlight.current = true;
    setBusy(name);
    try {
      const r = await fn();
      if (r && typeof r === "object" && "id" in r && "state" in r) soc.put(r as unknown as Incident);
      if (ok) toast.success(ok);
      reload();
      return r;
    } catch (e) {
      toast.error(e);
      if (is409(e)) { reload(); soc.refresh(); }
      return null;
    } finally {
      inFlight.current = false;
      setBusy(null);
    }
  }, [reload, soc]);
  return { busy, run };
}

/** The customer an incident belongs to, as SiteLive/SiteTimeline want it (SOC staff see it through the SOC). */
export function incidentOrg(me: Me, i: Pick<Incident, "org_id" | "org_name">): Org {
  return me.orgs.find((o) => o.id === i.org_id) ?? { id: i.org_id, name: i.org_name, slug: "", role: "operator", soc: true };
}

export const claimedByMe = (i: Pick<Incident, "claimed_by" | "state"> | null, me: Me) => !!i && i.state === "claimed" && i.claimed_by === me.user.id;

/**
 * React to a keyboard command handed down by the page. Only commands issued after this component mounted count:
 * a pane that appears later (another incident opened) must not replay the last chord, say a disposition.
 */
export function useCommand<C extends { nonce: number }>(cmd: C | null | undefined, fn: (c: C) => void) {
  const seen = useRef(cmd?.nonce);
  const latest = useRef(fn);
  latest.current = fn;
  useEffect(() => {
    if (!cmd || cmd.nonce === seen.current) return;
    seen.current = cmd.nonce;
    latest.current(cmd);
  }, [cmd]);
}
