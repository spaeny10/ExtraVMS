# Deploying the Axiom Vision hub (Hetzner, Docker Compose)

One small VPS runs Caddy (HTTPS), the hub, Postgres and coturn. Sites dial out to it; nothing is port-forwarded
anywhere. The shared AI runs on a site's GPU and is reached through that site's tunnel, so the VPS needs no GPU.

## 1. Server
- Hetzner Cloud → CX23/CPX21 (2 vCPU, 4 GB) is enough; a CCX23 (4 dedicated vCPU, 16 GB) is what runs hub.axiomvision.ai. Ubuntu 24.04 or newer, your SSH key only. Ashburn is closest to US sites.
- Hetzner Firewall on the server: inbound 22/tcp (your IP), 80/tcp, 443/tcp, 3478/tcp, 3478/udp, 49152-49252/udp.
- Turn on Backups (20 % of the server price) so the volume with Postgres is snapshotted.

## 2. DNS
At the registrar for axiomvision.ai (Namecheap → Advanced DNS, Host = `hub`): `A hub → <server IPv4>`, `AAAA hub → <server IPv6>`.
The IPv6 value is the server's address inside its /64, normally `<prefix>::1`, not the `<prefix>::` block the console lists.
Wait until the name resolves before starting Caddy (Let's Encrypt validates over port 80): `dig +short hub.axiomvision.ai`
on Linux, `Resolve-DnsName hub.axiomvision.ai -Type A` on Windows.

## 3. Install
```bash
apt update && apt install -y docker.io docker-compose-v2 git ufw unattended-upgrades
ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 3478/tcp && ufw allow 3478/udp && ufw allow 49152:49252/udp && ufw enable
git clone https://github.com/spaeny10/ExtraVMS /opt/axiom && cd /opt/axiom
cp hub/.env.example hub/.env
```
Edit `hub/.env`: `HUB_DOMAIN=hub.axiomvision.ai`, `HUB_PUBLIC_URL=https://hub.axiomvision.ai`,
`HUB_SECRET=$(openssl rand -hex 32)`, `POSTGRES_PASSWORD=$(openssl rand -hex 24)`, `HUB_TURN_SECRET=$(openssl rand -hex 24)`,
`HUB_VLLM_SITE=<site id of the GPU site>` (the hub UI shows server ids under Customer → Servers), `HUB_VLLM_MODEL=qwen3.5:9b`.
```bash
docker compose -f hub/docker-compose.yml up -d --build      # first build takes a few minutes (two UI bundles)
docker compose -f hub/docker-compose.yml logs -f caddy hub    # wait for "certificate obtained" and "hub ... up"
```

## 4. Bring the existing data across (skip for a brand-new hub)
On the old hub machine: `scp hub/hub.db root@hub.axiomvision.ai:/opt/axiom/hub/`. Then on the server:
```bash
docker compose -f hub/docker-compose.yml cp hub/hub.db hub:/tmp/hub.db
docker compose -f hub/docker-compose.yml exec hub python /app/hub/tools/migrate_db.py \
  sqlite:////tmp/hub.db "postgresql+psycopg://hub:${POSTGRES_PASSWORD}@postgres:5432/hub"
docker compose -f hub/docker-compose.yml restart hub
rm hub/hub.db
```
Users, org, sites (with their device tokens), dashboards and usage history come across; sites reconnect
without a new claim. Passwords come across too, so change any dev password right away:
`docker compose -f hub/docker-compose.yml exec hub python -m hub setpassword you@example.com` (prompts; ends old sessions).
New hub instead: `docker compose -f hub/docker-compose.yml exec hub python -m hub createsuper you@example.com`.

## 5. Point the sites at the new hub
On each site's own LAN (the hub proxy refuses this endpoint on purpose):
```bash
curl -X PUT http://<site>:8080/api/hub -H 'content-type: application/json' -d '{"hub_url":"wss://hub.axiomvision.ai/agent"}'
```
Do the GPU site first (it serves the shared AI). `GET /api/hub` shows `connected: true` within a few seconds
(sites verify the hub's certificate against certifi's bundle, so a Windows site works too);
the hub's Fleet page shows the site online. Also set `NVR_HUB_URL=wss://hub.axiomvision.ai/agent` in each site's
`.env` (the database setting wins, but a fresh install reads `.env`). New sites: `tools/deploy_site.sh` already
writes that URL; claim them from Customer → Servers → Add server.

## 6. Afterwards
- Remove any router port-forward that pointed at the old hub, and stop the old `python -m hub`.
- Phones: open https://hub.axiomvision.ai, sign in, Add to Home Screen; allow notifications (push works over HTTPS).
- Update: `cd /opt/axiom && git pull && docker compose -f hub/docker-compose.yml up -d --build`.
- Database dump (root's crontab, installed on hub.axiomvision.ai, 14 days kept): `0 4 * * * docker compose -f /opt/axiom/hub/docker-compose.yml exec -T postgres pg_dump -U hub hub | gzip > /srv/backups/hub-$(date +\%F).sql.gz`

## Upgrading to 0.2.0 (Sites, tenancy v2)
Hubs before 0.2.0 grouped cameras straight under servers ("sites"). 0.2.0 adds **Sites** (physical places, each with
one or more servers) between the customer and its servers. Take a dump first, since the first start changes data,
then update as usual:
```bash
docker compose -f hub/docker-compose.yml exec -T postgres pg_dump -U hub hub | gzip > /srv/backups/hub-pre-0.2.0.sql.gz
cd /opt/axiom && git pull && docker compose -f hub/docker-compose.yml up -d --build
```
On its first start the hub, by itself and idempotently (each later start finds nothing to do; a hub already running
a tenancy v2 commit before 0.2.0, like hub.axiomvision.ai at 6af80ae, only does the `site_grants` drop):
- adds the new columns and tables;
- puts every server in its own one-server Site named after it (address from the server's location note), so the fleet
  looks as before; afterwards move servers into shared Sites (Site → Servers → the server) and delete the empty ones;
- turns per-server grants into Site grants once (kv `schema:tenancy_v2`): a member who had server grants gets exactly
  those servers' Sites, everyone else **All sites**. An empty Site list now means nothing, not everything;
- then drops the old `site_grants` table (kv `schema:tenancy_v2_drop_site_grants`; only after the step above).
  Rolling back to code from before tenancy v2 would recreate it empty, i.e. restricted members would see every
  server: restore the dump instead of rolling back that far;
- seeds the cameras registry from each server's last heartbeat.
Check afterwards: `/healthz` says `"version": "0.2.0"`, Sites shows one Site per server, and a restricted member
still sees only their servers.

## Users and invites
- Reset a password from the server (prompts twice; signs the user out everywhere):
  `docker compose -f hub/docker-compose.yml exec hub python -m hub setpassword you@example.com`.
- Add people with **Customer → Invites**: choose the role and All sites or the Sites they may see (optionally lock the
  link to one email, add a label, set the expiry), copy the link and send it yourself; the hub sends no email. The
  person opens `https://hub.axiomvision.ai/invite/<code>`, signs in or creates an account, and joins. Pending links
  can be copied again or revoked. An admin limited to some Sites can only invite to those Sites.
- Customer → Members changes a member's role or Sites, or removes them (their Site grants go with them).
- **Hub administrators** own every customer and see all Sites without being members (so Members never lists them,
  unless they were also added as a member). A hub admin manages them under **Account → Hub administrators**: add by
  the email of an existing account (invite the person to a customer first), remove with a confirm; the hub refuses to
  remove the last one. From the server: `docker compose -f hub/docker-compose.yml exec hub python -m hub setsuper
  someone@example.com` (add `--off` to revoke; also refused for the last one). Changes are audited hub-wide
  (`GET /api/hub/audit`, "Recent changes" in that box), not in any customer's Audit. The header's Customer picker has
  **All customers** for them: every customer's Site cards on one page, grouped by customer.

## Checks
| What | How |
|---|---|
| Certificate and login | https://hub.axiomvision.ai shows the padlock and the login page |
| Servers online | Sites page; `curl -s https://hub.axiomvision.ai/healthz` → `version`, `sites_online` (= servers with a live tunnel) |
| Live video from a phone off Wi-Fi | tile plays; `chrome://webrtc-internals` shows a `relay` candidate (TURN) |
| Shared AI | Customer → AI & relay says "served by the GPU at <site>"; lite sites write synopses |
| No inbound ports at sites | the router forwards nothing; `ss -tlnp` on a site shows 8080 bound to the LAN only |
