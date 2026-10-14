/**
 * Pure helpers for a Site's camera groups (hub camera_groups.py) and talk-down on the hub: the Settings › Cameras
 * checklist (the Site's cameras by server) and, for Site Live, which speakers talk on each camera tile.
 *
 * A camera's speakers are the union of two kinds of binding: the server's own (a speaker bound to cameras of its
 * server, edited on that server's Settings, reported in its heartbeat summary) and the hub's (a speaker bound to Site
 * camera groups, which may hold cameras of any of the Site's servers). The hub's /api/locations/{id}/speakers computes
 * the same union (camera_groups.speakers_by_camera); Live computes it here from the 15 s Site poll's summaries, so a
 * speaker added on a server shows up without waiting for another fetch. A speaker always plays on its own server: each
 * one carries its server and that server's client, which TalkdownPanel uses whatever server the camera is on.
 */
import type { TalkSpeaker } from "@site/talkdown";
import type { Camera, Server, SiteCameraGroup } from "../api";

/** A speaker on a hub tile: TalkSpeaker with its own server and that server's client (`T`: SiteApi in the app). */
export type HubTalkSpeaker<T> = Omit<TalkSpeaker, "site" | "server"> & { server: string; site: T };
type ServerLike = Pick<Server, "id" | "name"> & { summary?: Server["summary"] | null; retired_at?: number | null };

/**
 * camera key ("<server>/<camera>") -> its speakers, server by server in the Site's order, each server's speakers in
 * its own order, each speaker once. With several servers a speaker's name says which server it is on.
 */
export function talkByCamera<T>(servers: ServerLike[], groups: Pick<SiteCameraGroup, "id" | "cameras" | "speakers">[],
  clientOf: (server: string) => T): Record<string, HubTalkSpeaker<T>[]> {
  const active = servers.filter((s) => !s.retired_at);
  const multi = active.length > 1;
  const byRef = new Map<string, string[]>();   // "<server>/<speaker>" -> camera keys of its groups
  const members = new Map(groups.map((g) => [g.id, g.cameras]));
  for (const g of groups) {
    for (const b of g.speakers) {
      const ref = `${b.server_id}/${b.speaker_id}`;
      byRef.set(ref, [...(byRef.get(ref) ?? []), ...(members.get(g.id) ?? [])]);
    }
  }
  const out: Record<string, HubTalkSpeaker<T>[]> = {};
  for (const srv of active) {
    for (const sp of srv.summary?.speakers ?? []) {
      if (!sp?.id || (sp as { enabled?: boolean }).enabled === false) continue;
      const talk = { id: sp.id, name: multi ? `${sp.name} (${srv.name})` : sp.name, online: sp.online, server: srv.id, site: clientOf(srv.id) };
      const keys = new Set([...(sp.camera_ids ?? []).map((c) => `${srv.id}/${c}`), ...(byRef.get(`${srv.id}/${sp.id}`) ?? [])]);
      for (const k of keys) (out[k] ??= []).push(talk);
    }
  }
  return out;
}

/** One server's cameras in the members checklist. `missing`: gone from the server (kept while a group names it). */
export type CheckCamera = { key: string; name: string; missing: boolean };
export type CheckServer = { id: string; name: string; cameras: CheckCamera[] };

/**
 * The Site's cameras for the checklist, by server (by name) and camera name: the hub registry's, without cameras that
 * are gone from their server unless `keep` (the group's members) names them.
 */
export function camerasByServer(servers: ServerLike[], registry: Pick<Camera, "server_id" | "camera_id" | "name" | "missing_since">[],
  keep: string[] = []): CheckServer[] {
  const held = new Set(keep);
  const by = (a: { name: string }, b: { name: string }) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" });
  return servers.filter((s) => !s.retired_at).map((s) => ({
    id: s.id, name: s.name,
    cameras: registry.filter((c) => c.server_id === s.id)
      .map((c) => ({ key: `${s.id}/${c.camera_id}`, name: c.name || c.camera_id, missing: !!c.missing_since }))
      .filter((c) => !c.missing || held.has(c.key)).sort(by),
  })).filter((s) => s.cameras.length).sort(by);
}

/** The members with these keys turned on or off, keeping the order they were added in. */
export function toggleKeys(members: string[], keys: string[], on: boolean): string[] {
  if (!on) {
    const off = new Set(keys);
    return members.filter((k) => !off.has(k));
  }
  return [...members, ...keys.filter((k) => !members.includes(k))];
}

/** "4 cameras on 2 servers" / "1 camera" / "No cameras". */
export function membersText(keys: string[]): string {
  if (!keys.length) return "No cameras";
  const servers = new Set(keys.map((k) => k.split("/")[0])).size;
  return `${keys.length} camera${keys.length === 1 ? "" : "s"}${servers > 1 ? ` on ${servers} servers` : ""}`;
}

/** A group's camera names, "Server · Camera" when the Site has several servers (for titles and the Speakers list). */
export function cameraNames(keys: string[], servers: CheckServer[], multi: boolean): string[] {
  const names = new Map(servers.flatMap((s) => s.cameras.map((c) => [c.key, multi ? `${s.name} · ${c.name}` : c.name] as const)));
  return keys.map((k) => names.get(k) ?? k);
}
