/**
 * /invite/<code>: public. Shows what the invite grants (from the public preview), then joins it: a signed-in browser
 * joins with one click (the hub ignores the body and uses the session); otherwise an existing account signs in with
 * its password (and authenticator code when the hub asks for it) or a new account is made with the email and password.
 */
import { useEffect, useState } from "react";
import { toast } from "@site/ui";
import { type InvitePreview, type Me, api } from "./api";
import { expiresIn, httpStatus, inviteAccessSummary, inviteErrorText } from "./invites";

export function InvitePage({ code, me, onJoined, onSignedOut }: { code: string; me: Me | null; onJoined: (orgId: string | undefined) => Promise<void>; onSignedOut: () => void }) {
  const [preview, setPreview] = useState<InvitePreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [needTotp, setNeedTotp] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => { api.invitePreview(code).then(setPreview).catch((e) => setError(inviteErrorText(e))); }, [code]);

  const accept = async (e?: React.FormEvent) => {
    e?.preventDefault();
    setBusy(true);
    try {
      const r = await api.acceptInvite(code, me ? {} : { email: email.trim(), password, totp: needTotp ? totp.trim() : undefined });
      if (r.totp_required) { setNeedTotp(true); return; }
      toast.success(`You joined ${preview?.org_name ?? "the customer"}`);
      await onJoined(r.org_id);
    } catch (err) {
      // 404 means the link is gone for good: replace the page; the rest (wrong password, other address) can be retried
      if (httpStatus(err) === 404) setError(inviteErrorText(err));
      else toast.error(inviteErrorText(err));
    } finally { setBusy(false); }
  };

  return (
    <div className="login-box invite-box">
      <h1>Axiom Vision</h1>
      {error ? (
        <>
          <p>{error}</p>
          <a href="/">Go to sign in</a>
        </>
      ) : !preview ? <p className="muted">Checking the invite…</p> : (
        <>
          <p style={{ marginTop: 0 }}><strong>{preview.org_name}</strong> invited you as <strong>{preview.role}</strong> · {inviteAccessSummary(preview)}</p>
          <p className="muted small">
            {preview.label ? `${preview.label} · ` : ""}{preview.email_hint ? `For ${preview.email_hint} · ` : ""}Expires {expiresIn(preview.expires_at)}
          </p>
          {me ? (
            <>
              <p>You are signed in as <strong>{me.user.email}</strong>.</p>
              <div className="row">
                <button disabled={busy} onClick={() => accept()}>{busy ? "Joining…" : `Join ${preview.org_name}`}</button>
                <button className="ghost small" disabled={busy} onClick={async () => { await api.logout().catch(() => {}); onSignedOut(); }}>Use a different account</button>
              </div>
            </>
          ) : (
            <form onSubmit={accept}>
              <p className="muted small">Already have an account? Use its password. New here? Choose a password of at least 10 characters.</p>
              <label className="field"><span>Email</span><input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus /></label>
              <label className="field"><span>Password</span><input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
              {needTotp && <label className="field"><span>Authenticator code</span><input inputMode="numeric" autoComplete="one-time-code" value={totp} onChange={(e) => setTotp(e.target.value)} autoFocus /></label>}
              <button type="submit" disabled={busy || !email.trim() || !password || (needTotp && !totp.trim())}>{busy ? "Joining…" : `Join ${preview.org_name}`}</button>
            </form>
          )}
        </>
      )}
    </div>
  );
}
