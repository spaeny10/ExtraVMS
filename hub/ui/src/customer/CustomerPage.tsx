/**
 * Customer admin (wire: org): Sites · Servers · Members · Invites · AI · Actions as tabs at /customer/<tab>
 * (old /org links redirect here). Viewers and operators see the read-only tabs; members/invites are admin-only.
 */
import { useCallback, useEffect, useState } from "react";
import { Icon, toast } from "@site/ui";
import { type Fleet, type Me, type Member, type Org, type Server, type Site, api } from "../api";
import { isAdmin } from "../access";
import { GroupsBox } from "../Groups";
import { type CustomerTab, go, navigate } from "../nav";
import { ClaimBox } from "./ClaimBox";
import { CreateOrgBox } from "./CreateOrgBox";
import { FleetActionsPage } from "./FleetActionsPage";
import { MembersBox } from "./MembersBox";
import { ServersBox } from "./ServersBox";
import { SharedAiBox } from "./SharedAiBox";
import { SitesBox } from "./SitesBox";

const TABS: { tab: CustomerTab; label: string; icon: string; admin?: boolean }[] = [
  { tab: "sites", label: "Sites", icon: "grid" }, { tab: "servers", label: "Servers", icon: "settings" },
  { tab: "members", label: "Members", icon: "user", admin: true }, { tab: "invites", label: "Invites (coming)", icon: "link", admin: true },
  { tab: "ai", label: "AI & relay", icon: "sparkle" }, { tab: "actions", label: "Actions", icon: "events" },
];

export function CustomerPage({ org, me, tab, onChanged }: { org: Org; me: Me; tab: CustomerTab; onChanged: () => void }) {
  const admin = isAdmin(org, me);
  const [sites, setSites] = useState<Site[]>([]);
  const [servers, setServers] = useState<Server[]>([]);
  const [members, setMembers] = useState<Member[]>([]);
  const [fleet, setFleet] = useState<Fleet | null>(null);
  const load = useCallback(() => {
    api.locations(org.id, true).then(setSites).catch((e) => toast.error(e));
    api.servers(org.id).then(setServers).catch((e) => toast.error(e));
    api.fleet(org.id).then(setFleet).catch(() => {});
    if (admin) api.members(org.id).then(setMembers).catch(() => {});
  }, [org.id, admin]);
  useEffect(() => { load(); }, [load]);
  const tabs = TABS.filter((t) => admin || !t.admin);
  const shown = tabs.some((t) => t.tab === tab) ? tab : "sites";
  return (
    <>
      <h2>{org.name} <span className="muted small">customer · your role: {org.role}</span></h2>
      <div className="segmented site-tabs" role="tablist">
        {tabs.map((t) => (
          <button key={t.tab} role="tab" aria-selected={shown === t.tab} className={shown === t.tab ? "active" : ""} onClick={() => navigate(`/customer/${t.tab}`)}>
            <Icon name={t.icon} size={16} /> {t.label}
          </button>
        ))}
      </div>
      {shown === "sites" && (
        <>
          <SitesBox org={org} sites={sites} admin={admin} onChanged={load} />
          <GroupsBox org={org} fleet={fleet} canEdit={admin} />
          {me.user.is_super && <CreateOrgBox onDone={onChanged} />}
        </>
      )}
      {shown === "servers" && (
        <>
          {admin && <ClaimBox org={org} sites={sites} onDone={load} />}
          <ServersBox servers={servers} sites={sites} admin={admin} onChanged={load} />
        </>
      )}
      {shown === "members" && <MembersBox org={org} members={members} sites={sites} onChanged={() => { load(); onChanged(); }} />}
      {shown === "invites" && (
        <div className="card">
          <h3>Invites</h3>
          <p className="muted small" style={{ marginBottom: 0 }}>Invite links (choose role and sites, send the link, they set their own password) are coming.
            For now add people under <a href="/customer/members" onClick={go("/customer/members")}>Members</a> with an initial password.</p>
        </div>
      )}
      {shown === "ai" && <SharedAiBox org={org} canEdit={org.role === "owner" || me.user.is_super} />}
      {shown === "actions" && <FleetActionsPage org={org} />}
    </>
  );
}
