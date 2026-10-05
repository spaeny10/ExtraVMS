/**
 * The "how is video reaching me" chip per server (Site header, SOC incident header): green "Direct · LAN" when this
 * browser fetches the server's media straight from it, grey "via hub" otherwise. When the only thing in the way is
 * (probably) the server's self-signed certificate, it offers to open the server's probe URL in a tab so the user can
 * accept it once; coming back to this window re-probes (direct.ts). "Don't offer again" is kept per server.
 */
import type { Server } from "./api";
import { useDirect } from "./direct";

export function DirectChip({ server, named = false }: { server: Pick<Server, "id" | "name" | "direct">; named?: boolean }) {
  const d = useDirect(server);
  const who = named ? `${server.name}: ` : "";
  if (d.state === "direct") {
    return <span className="chip direct-chip on" title={`Video comes straight from ${server.name} on this network (${d.base}), not through the hub`}>{who}Direct · LAN</span>;
  }
  if (d.state === "cert" && !d.dismissed) {
    return (
      <span className="chip direct-chip offer" title={d.fingerprint ? `The server's certificate fingerprint (SHA-256): ${d.fingerprint}` : undefined}>
        {who}Direct connection available — accept the server's certificate
        <button className="linkish small" onClick={d.enable}
          title="Opens the server in a new tab: accept the browser's warning there once, then come back to this tab">Accept…</button>
        <button className="linkish small" onClick={d.dismiss} title="Keep using the hub for this server and stop offering">Don't offer again</button>
      </span>
    );
  }
  if (d.state === "checking") return <span className="chip direct-chip" title="Checking whether this browser can reach the server on its LAN">{who}Checking LAN…</span>;
  return <span className="chip direct-chip" title="Video is relayed through the hub (this browser isn't on the server's network, or the server can't be reached directly)">{who}via hub</span>;
}

/** One chip per server; prefixed with the server's name when there is more than one. */
export function DirectChips({ servers }: { servers: Pick<Server, "id" | "name" | "direct" | "online">[] }) {
  const on = servers.filter((s) => s.online);
  if (!on.length) return null;
  return <>{on.map((s) => <DirectChip key={s.id} server={s} named={on.length > 1} />)}</>;
}
