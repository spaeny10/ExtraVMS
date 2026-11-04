import { useCallback, useEffect, useState } from "react";
import { confirmDialog, toast } from "@site/ui";
import { type ShareAccess, type ShareLinkRow, type Site, fmtTime, shareLinks } from "./api";

/** Site › Settings › Shared links: the public links made from this Site's exports (hub shares.py), who made them, until
 *  when, how often they were opened and downloaded, the access log, and Revoke. */
export function SharedLinks({ site }: { site: Site }) {
  const [rows, setRows] = useState<ShareLinkRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [log, setLog] = useState<ShareAccess[] | null>(null);
  const load = useCallback(() => {
    shareLinks.list(site.id).then((r) => { setRows(r); setErr(null); }).catch((e) => setErr(String(e)));
  }, [site.id]);
  useEffect(load, [load]);
  useEffect(() => {
    if (!open) { setLog(null); return; }
    shareLinks.access(open).then(setLog).catch((e) => toast.error(e));
  }, [open]);
  const revoke = async (r: ShareLinkRow) => {
    if (!await confirmDialog("Revoke this link?", { message: "It stops working at once, and the hub's copy of the files is deleted.", confirmLabel: "Revoke", danger: true })) return;
    try {
      await shareLinks.revoke(r.id);
      toast.success("Link revoked");
      load();
    } catch (e) {
      toast.error(e);
    }
  };
  if (err) return <p className="small error">{err.startsWith("403") ? "Shared links are for operators and admins." : err}</p>;
  if (!rows) return <p className="muted small">Loading…</p>;
  return (
    <div className="card shared-links">
      <h3>Shared links</h3>
      <p className="muted small">Links made with "Share link…" on an export. Anyone with a link (and its passcode, if it has one) can watch and
        download that export until it expires. The hub keeps its own copy of the files until then.</p>
      {rows.length === 0 ? <p className="muted small">No links yet.</p> : (
        <table className="table small">
          <thead><tr><th>Export</th><th>By</th><th>Status</th><th>Until</th><th>Opened</th><th>Downloads</th><th /></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.title}{r.passcode ? " 🔑" : ""}{r.note ? <div className="muted">{r.note}</div> : null}
                  {r.error ? <div className="error">{r.error}</div> : null}</td>
                <td>{r.created_email}<div className="muted">{fmtTime(r.created_at)}</div></td>
                <td>{r.status}</td>
                <td>{fmtTime(r.revoked_at ?? r.expires_at)}</td>
                <td>{r.views}</td>
                <td>{r.downloads}</td>
                <td className="row">
                  <button className="ghost small" onClick={() => setOpen(open === r.id ? null : r.id)}>{open === r.id ? "Hide log" : "Access log"}</button>
                  {r.status !== "revoked" && r.status !== "expired" && <button className="ghost small danger" onClick={() => void revoke(r)}>Revoke</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {open && (
        <div className="share-log">
          <h4>Access log</h4>
          {!log ? <p className="muted small">Loading…</p> : log.length === 0 ? <p className="muted small">Not opened yet.</p> : (
            <table className="table small">
              <thead><tr><th>When</th><th>What</th><th>From</th></tr></thead>
              <tbody>
                {log.map((a) => (
                  <tr key={a.id}><td>{fmtTime(a.ts)}</td><td>{a.action}{a.name ? ` · ${a.name}` : ""}</td>
                    <td>{a.ip}<div className="muted">{(a.user_agent ?? "").slice(0, 80)}</div></td></tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}
