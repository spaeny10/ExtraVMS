/**
 * The SOC console's shared state: the queue as the SOC socket keeps it, the roster, my presence, the SLA policy and
 * the disposition catalogue, plus the alarm sound and the screen-reader announcements that follow from them.
 * App mounts one provider around the SOC pages (header and page both read it), so one socket and one ringer serve
 * the header's toggles and the workstation.
 *
 * Resync: every (re)connect refetches the open incidents over REST (the snapshot frame should cover it, but a REST
 * list also drops incidents closed while we were away), and while the socket is down the list is polled every 15 s
 * so a console behind a broken proxy still shows new incidents, just later.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef, useState, useSyncExternalStore } from "react";
import type { Me } from "../api";
import { EMPTY_QUEUE, type QueueState, applyStreamMessage, mergePresence, ringPriority, upsertIncident } from "./queue";
import { ringer } from "./ringer";
import { socApi, subscribeSoc } from "./socApi";
import type { DispositionGroup, Incident, Presence, PresenceStatus, SlaPolicy, SocMessage } from "./types";

export type Conn = "connecting" | "live" | "down" | "refused";
type Action = { type: "msg"; m: SocMessage } | { type: "put"; i: Incident } | { type: "list"; list: Incident[] } | { type: "presence"; p: Presence | Presence[] };

function reducer(s: QueueState, a: Action): QueueState {
  switch (a.type) {
    case "msg": return applyStreamMessage(s, a.m);
    case "put": return { ...s, incidents: upsertIncident(s.incidents, a.i), rev: s.rev + 1 };
    case "presence": return { ...s, presence: mergePresence(s.presence, a.p) };
    case "list": {
      // the REST list is authoritative for which incidents are open; a row the socket already updated past it wins
      const have = new Map(s.incidents.map((i) => [i.id, i]));
      const incidents = a.list.filter((i) => i.state !== "closed").map((i) => {
        const o = have.get(i.id);
        return o && o.updated_at > i.updated_at ? o : o && !i.events && o.events ? { ...i, events: o.events } : i;
      });
      return { ...s, incidents, rev: s.rev + 1 };
    }
  }
}

export type SocStream = {
  enabled: boolean; me: Me; queue: QueueState; conn: Conn;
  myPresence: Presence | null; myStatus: PresenceStatus; setStatus: (s: PresenceStatus) => void;
  sla: SlaPolicy | null; groups: DispositionGroup[];
  /** refetch the open incidents now */
  refresh: () => void;
  /** an action's response row, applied at once (the socket frame that follows is then a no-op) */
  put: (i: Incident) => void;
  /** polite announcement for screen readers (new incidents) */
  announcement: string;
  /** open incidents at escalation level 2 or above (supervisors paged): the alert banner */
  escalated: Incident[];
};

const Ctx = createContext<SocStream | null>(null);

export function useSoc(): SocStream {
  const v = useContext(Ctx);
  if (!v) throw new Error("useSoc outside SocStreamProvider");
  return v;
}

export const useRinger = () => useSyncExternalStore(ringer().subscribe, ringer().get);

const HEARTBEAT_MS = 30000;
const isIncident = (x: unknown): x is Incident => !!x && typeof x === "object" && "id" in x && "state" in x;

export function SocStreamProvider({ me, enabled, children }: { me: Me; enabled: boolean; children: React.ReactNode }) {
  const [queue, dispatch] = useReducer(reducer, EMPTY_QUEUE);
  const [conn, setConn] = useState<Conn>("connecting");
  const [sla, setSla] = useState<SlaPolicy | null>(null);
  const [groups, setGroups] = useState<DispositionGroup[]>([]);
  const [announcement, setAnnouncement] = useState("");
  const repeatRef = useRef<number | null>(null);
  const levels = useRef(new Map<number, number>());

  const refresh = useCallback(() => {
    socApi.incidents({ limit: 500 }).then((list) => dispatch({ type: "list", list })).catch(() => {});
    socApi.presence().then((p) => dispatch({ type: "presence", p })).catch(() => {});
  }, []);
  const put = useCallback((i: Incident) => { if (isIncident(i)) dispatch({ type: "put", i }); }, []);

  useEffect(() => {
    if (!enabled) return;
    socApi.sla().then(setSla).catch(() => {});
    socApi.dispositions().then((d) => setGroups(d.groups ?? [])).catch(() => {});
    let first = true;
    return subscribeSoc((m) => { dispatch({ type: "msg", m }); onFrame(m); }, (up, fatal) => {
      setConn(up ? "live" : fatal ? "refused" : "down");
      // the first open's snapshot is enough; after a drop, also refetch (frames sent meanwhile are lost)
      if (up && !first) refresh();
      if (up) first = false;
    });
    // onFrame only touches refs and state setters, so the first render's copy is the same as any later one
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, refresh]);

  // REST fallback while the socket is down (and the first load if it never opens); not after a refusal (signed out
  // or no longer SOC staff), where every request would fail the same way
  useEffect(() => {
    if (!enabled || conn === "live" || conn === "refused") return;
    refresh();
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
  }, [enabled, conn, refresh]);

  function onFrame(m: SocMessage) {
    // every frame carries the hub's sound policy; its repeat interval is kept for the ringer (see below)
    if (m.sound?.ring && m.sound.repeat_s) repeatRef.current = m.sound.repeat_s;
    if (m.type === "snapshot") {
      for (const i of m.incidents) levels.current.set(i.id, i.escalation_level ?? 0);
      return;
    }
    if (!("incident" in m)) return;
    const i = m.incident;
    const before = levels.current.get(i.id) ?? 0;
    levels.current.set(i.id, i.escalation_level ?? 0);
    // level 2 = supervisors paged: a distinct chime, once per incident crossing it
    if ((i.escalation_level ?? 0) >= 2 && before < 2 && i.state !== "closed") ringer().chime();
    if (m.type === "incident_opened" && i.lane === "ring") {
      setAnnouncement(`New ${i.priority} priority incident at ${i.org_name} › ${i.location_name}${i.title ? `: ${i.title}` : ""}`);
    }
  }

  // the alarm: the most urgent unclaimed ringing incident's pattern, silence when there is none. Whether to ring is
  // read off the queue itself (the same rows the hub's ring_count counts, and it stops the instant a claim frame
  // lands); the hub's repeat_s is a ceiling, so a high-priority pattern can repeat faster but never slower
  const want = enabled ? ringPriority(queue.incidents) : null;
  useEffect(() => { ringer().want(want, repeatRef.current); }, [want]);
  useEffect(() => () => ringer().stop(), []);
  // any click or key on the console counts as the gesture browsers require before audio
  useEffect(() => {
    if (!enabled) return;
    const on = () => { if (ringer().get().needsGesture) ringer().enable(); };
    addEventListener("pointerdown", on, { capture: true });
    addEventListener("keydown", on, { capture: true });
    return () => { removeEventListener("pointerdown", on, { capture: true }); removeEventListener("keydown", on, { capture: true }); };
  }, [enabled]);

  // ---- presence: what I chose, resent every 30 s while the console is visible (the hub ages out silent users after
  // 2 minutes; the open socket also keeps me on shift, the heartbeat covers a console whose socket is down).
  // Opening the socket makes me available (or keeps engaged / on break), so nothing is sent until the roster has
  // been seen: a heartbeat of the default "available" must not undo the break I was on before a reload.
  const myPresence = queue.presence.find((p) => p.user_id === me.user.id) ?? null;
  const [myStatus, setMyStatus] = useState<PresenceStatus>("available");
  const statusRef = useRef(myStatus);
  statusRef.current = myStatus;
  const adopted = useRef(false);
  useEffect(() => {
    // after a reload, keep the break/offline the roster still has for me rather than flipping back to available
    if (adopted.current || !myPresence) return;
    adopted.current = true;
    if (myPresence.status === "break" || myPresence.status === "offline") setMyStatus(myPresence.status);
  }, [myPresence]);
  const serverStatus = useRef<PresenceStatus | null>(null);
  serverStatus.current = myPresence?.status ?? null;
  const beat = useCallback((s: PresenceStatus) => {
    socApi.setPresence(s).then((r) => dispatch({ type: "presence", p: r })).catch(() => {});
  }, []);
  // "engaged" is the hub's (I hold a claim): while I'm available by choice, the heartbeat repeats engaged so it
  // doesn't knock me back to available mid-incident
  const heartbeat = useCallback(() => {
    if (!adopted.current || document.visibilityState !== "visible") return;
    beat(statusRef.current === "available" && serverStatus.current === "engaged" ? "engaged" : statusRef.current);
  }, [beat]);
  const setStatus = useCallback((s: PresenceStatus) => { adopted.current = true; setMyStatus(s); beat(s); }, [beat]);
  useEffect(() => {
    if (!enabled) return;
    const t = setInterval(heartbeat, HEARTBEAT_MS);
    document.addEventListener("visibilitychange", heartbeat);
    return () => { clearInterval(t); document.removeEventListener("visibilitychange", heartbeat); };
  }, [enabled, heartbeat]);

  const escalated = useMemo(() => queue.incidents.filter((i) => (i.escalation_level ?? 0) >= 2 && (i.state === "new" || i.state === "claimed")), [queue.incidents]);
  const value = useMemo<SocStream>(() => ({
    enabled, me, queue, conn, myPresence, myStatus, setStatus, sla, groups, refresh, put, announcement, escalated,
  }), [enabled, me, queue, conn, myPresence, myStatus, setStatus, sla, groups, refresh, put, announcement, escalated]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
