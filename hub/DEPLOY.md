# Deploying the Axiom Vision hub (Hetzner, Docker Compose)

One small VPS runs Caddy (HTTPS), the hub, Postgres and coturn. Sites dial out to it; nothing is port-forwarded
anywhere. The shared AI runs on a site's GPU and is reached through that site's tunnel, so the VPS needs no GPU.

## 1. Server
- Hetzner Cloud → CX23/CPX21 (2 vCPU, 4 GB) is enough; a CCX23 (4 dedicated vCPU, 16 GB) is what runs hub.axiomvision.ai. Ubuntu 24.04 or newer, your SSH key only. Ashburn is closest to US sites.
- Hetzner Firewall on the server: inbound 22/tcp (your IP), 80/tcp, 443/tcp, 3478/tcp, 3478/udp, 49152-49252/udp,
  53/udp and 53/tcp (the Direct-on-LAN names, see "Direct-on-LAN names and certificates").
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

## Security hardening (October 2026)
An update past this point changes the following; check these on the server before (or right after) `up -d --build`:
- **Postgres password.** `grep -c '^POSTGRES_PASSWORD=.\+' hub/.env` must print 1. If it prints 0, the database was
  created with the old `change-me` fallback: generate one, then set it in Postgres and in `.env` together:
  ```bash
  NEW=$(openssl rand -hex 24)
  docker compose -f hub/docker-compose.yml exec -T postgres psql -U hub -d hub -c "ALTER USER hub PASSWORD '$NEW'"
  echo "POSTGRES_PASSWORD=$NEW" >> hub/.env && docker compose -f hub/docker-compose.yml up -d hub
  ```
  (`POSTGRES_PASSWORD` only takes effect when the data volume is first created, so changing `.env` alone breaks the
  hub's connection.) Once every install has one, make it required in docker-compose.yml (`${POSTGRES_PASSWORD:?...}`).
- **Shared AI on the hub's own GPU** (`--profile ai`): the vLLM container now refuses to start without `HUB_VLLM_KEY`
  (it used to fall back to the key `internal`). Installs relaying through a site (`HUB_VLLM_SITE`) are unaffected.
- **coturn** has more denied peer ranges (link-local/metadata, CGNAT, multicast, IPv6 ULA/link-local, ...): it reads its
  config only at start. This update recreates the container anyway (its logging settings changed); after any later
  edit of turnserver.conf alone run `docker compose -f hub/docker-compose.yml restart coturn`. Site TURN credentials
  now last 24 h and the hub refreshes them over the tunnel, so sites need no change.
- **Container logs** rotate (json-file, 5 x 50 MB per service): `up -d` recreates the containers to apply it.
- **Trusted proxies**: the hub trusts `X-Forwarded-For/-Proto` only from `HUB_FORWARDED_ALLOW_IPS` (compose default:
  the private ranges of the compose network, where only Caddy reaches port 8000). Check `docker compose logs hub` shows
  "trusting X-Forwarded-* from 172.16.0.0/12, ..." and that the Audit page shows real client IPs, not 172.x.
- **Security headers** come from the hub itself (HSTS, CSP, nosniff, framing, referrer and permissions policies); the
  Caddyfile is unchanged, no Caddy reload needed. After the update: open the hub, a Site's live view, the map and a
  server console (/s/<id>/), with the browser console open: there should be no "Content Security Policy" errors.
- **What users notice**: other devices are signed out when someone changes their password; turning two-factor off or
  moving it to a new phone asks for the password and a current code (setting it up asks for the password); each
  authenticator code works once, so signing in twice within 30 s needs the next code; existing sessions keep working
  (their ids are rehashed on first use); push subscriptions to anything but the browser push services are refused,
  and any such stored row is dropped instead of sent to.

## Camera web pages
Site admins (and hub administrators) open a camera's own settings page from **Site → Settings → Cameras → Camera web
pages**: it opens in a new tab at `https://<label>.cam.axiomvision.ai/`, through the camera's server tunnel (hub
camweb.py, server nvr/camweb.py). Setting it up once:
- **DNS**: `A *.cam → <server IPv4>` (and `AAAA *.cam → <server IPv6>`) at the registrar. `HUB_CAMWEB_DOMAIN`
  (default `cam.axiomvision.ai`) names it for both Caddy and the hub.
- **Caddy**: the Caddyfile has a global `on_demand_tls { ask http://hub:8000/api/camweb/tls-ok }` and a
  `*.{$HUB_CAMWEB_DOMAIN}` site with `tls { on_demand }`: a certificate per camera name, issued the first time it is
  opened (HTTP-01 on port 80 or TLS-ALPN on 443), and only for names the hub says are cameras. After pulling, rebuild the
  hub first (the ask endpoint must exist), then check and reload Caddy:
  `docker compose -f hub/docker-compose.yml exec caddy caddy validate --config /etc/caddy/Caddyfile` and
  `docker compose -f hub/docker-compose.yml up -d caddy` (the new environment variable recreates the container).
- **Servers** need this version (their hello lists `camweb`); an older one answers "update the server first".
- Let's Encrypt allows 50 new certificates per registered domain per week; each camera's name is stable, so a camera
  costs one certificate (renewed by Caddy) the first time anyone opens it. Caddy falls back to ZeroSSL when it is hit.

## Direct-on-LAN names and certificates
A browser on a server's own LAN plays media straight from it (Direct · LAN, hub/ui/src/direct.ts). With only the
server's self-signed certificate the user has to accept it once per server and Chrome shows "Not secure". Instead each
server can hold a Let's Encrypt certificate for its own names:

- **Names**: `<a-b-c-d>.<label>.lan.axiomvision.ai` → A `a.b.c.d`, e.g. `192-168-1-5.s-d6d429b617ca.lan.axiomvision.ai`
  → 192.168.1.5. `<label>` is the server id with `_` → `-` (`s_d6d429b617ca` → `s-d6d429b617ca`; ids never contain `-`,
  so it maps back). Only private addresses are answered (10/8, 172.16/12, 192.168/16, 100.64/10) and only for enrolled,
  non-retired servers; everything else under the zone is NXDOMAIN, other zones REFUSED. The `dns` service
  (`python -m hub dns`, hub/hub/lan_dns.py) is authoritative only, never recursive; 20 queries/s per source address
  (`HUB_LAN_DNS_RATE`), more are dropped. Server ids appear in public Certificate Transparency logs this way (they
  are not secrets: the device token is).
- **Certificates**: `*.<label>.lan.axiomvision.ai`, one per server, so every LAN address it gets (DHCP) is covered. The
  server makes the key and sends a CSR through its tunnel; the hub checks it names exactly that server's wildcard,
  orders it from Let's Encrypt with DNS-01 (the TXT is served by the `dns` service), and sends the chain back
  (hub/hub/lan_certs.py, server backend/nvr/lan_cert.py: `data_dir/tls/lan.crt` + `lan.key`). The server serves it on
  8443 for those names (SNI) and swaps in a renewed one without a restart; by IP address it still serves the
  self-signed one. The ACME account key lives in the hub database (kv `lan_acme:account`).
- **Renewal and limits**: renewed when less than min(30 days, a third of the lifetime) is left, by the server's hourly
  check. One order per server at a time; after an order no new one for that server for 2 days (a server that lost
  its key waits); failures back off 1 h, 2 h, 4 h … 24 h; 10 failed orders in an hour pause all orders for the hour;
  at most `HUB_LAN_CERT_WEEKLY_CAP` (30) certificates per 7 days over all servers (the hub logs "weekly cap" when it
  is reached). Let's Encrypt allows 50 new certificates per registered domain per week and the camera web pages'
  certificates (`*.cam.axiomvision.ai`) count against the same 50; a large initial rollout fills over a few weeks.
- **Fallback**: a customer router with DNS rebinding protection (many consumer routers, Pi-hole, some ISPs' DNS) drops
  answers that point at private addresses. The name then doesn't resolve, the probe fails and the browser keeps using
  the hub tunnel, exactly as off the LAN. Fix per site by allowing `lan.axiomvision.ai` in the router's rebind
  exceptions. Servers without a certificate keep the self-signed behaviour (accept prompt).

Setting it up once:
1. Namecheap → Advanced DNS for axiomvision.ai: `NS lan → ns1.axiomvision.ai` and `A ns1 → <hub server IPv4>`
   (178.156.211.145).
2. Firewall: inbound 53/udp and 53/tcp in the Hetzner firewall and in ufw (`ufw allow 53/udp && ufw allow 53/tcp`).
3. `hub/.env`: `HUB_DNS_BIND=178.156.211.145`. Ubuntu's systemd-resolved already listens on 127.0.0.53:53 and
   127.0.0.54:53, so publishing on 0.0.0.0:53 fails with "address already in use"; binding the public address
   doesn't. (If it fails anyway, only the `dns` container stops; the hub keeps running.)
4. `docker compose -f hub/docker-compose.yml up -d --build`, then check from anywhere:
   `dig +short 192-168-1-5.<label>.lan.axiomvision.ai` → `192.168.1.5`, `dig lan.axiomvision.ai SOA` → `ns1.axiomvision.ai.`,
   `dig @178.156.211.145 google.com` → REFUSED.
5. Then turn ordering on: `HUB_LAN_CERTS=1` in `hub/.env` (optionally `HUB_LAN_ACME_EMAIL=you@...` for expiry mail)
   and `docker compose -f hub/docker-compose.yml up -d hub`. Servers need this version (their hello lists `lan_cert`);
   within a minute of reconnecting a server's heartbeat reports `direct.cert: "le"` and the hub UI uses the names.
   A dry run against Let's Encrypt staging (`HUB_LAN_ACME_DIRECTORY=https://acme-staging-v02.api.letsencrypt.org/directory`)
   gives certificates browsers don't trust; after switching back, delete `data_dir/tls/lan.crt` on those servers so they
   ask again.

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
| Camera web pages | `docker compose -f hub/docker-compose.yml exec caddy wget -qO- 'http://hub:8000/api/camweb/tls-ok?domain=cnotacamera1.cam.axiomvision.ai'` → "404 Not Found" (the ask answers); Site → Settings → Cameras → Open camera web page shows the camera's login with a padlock; the customer's Audit lists "camera web page opened / entered" |
