# Deploying the Axiom Vision hub (Hetzner, Docker Compose)

One small VPS runs Caddy (HTTPS), the hub, Postgres and coturn. Sites dial out to it; nothing is port-forwarded
anywhere. The shared AI runs on a site's GPU and is reached through that site's tunnel, so the VPS needs no GPU.

## 1. Server
- Hetzner Cloud → CX23 or CPX21 (2 vCPU, 4 GB, 40 GB), Ubuntu 24.04, your SSH key only. Ashburn is closest to US sites.
- Hetzner Firewall on the server: inbound 22/tcp (your IP), 80/tcp, 443/tcp, 3478/tcp, 3478/udp, 49152-49252/udp.
- Turn on Backups (20 % of the server price) so the volume with Postgres is snapshotted.

## 2. DNS
At the registrar for axiomvision.ai: `A hub → <server IPv4>`, `AAAA hub → <server IPv6>`. Wait until
`dig +short hub.axiomvision.ai` answers before starting Caddy (Let's Encrypt validates over port 80).

## 3. Install
```bash
apt update && apt install -y docker.io docker-compose-v2 git ufw unattended-upgrades
ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 3478/tcp && ufw allow 3478/udp && ufw allow 49152:49252/udp && ufw enable
git clone https://github.com/spaeny10/ExtraVMS /opt/axiom && cd /opt/axiom
cp hub/.env.example hub/.env
```
Edit `hub/.env`: `HUB_DOMAIN=hub.axiomvision.ai`, `HUB_PUBLIC_URL=https://hub.axiomvision.ai`,
`HUB_SECRET=$(openssl rand -hex 32)`, `POSTGRES_PASSWORD=$(openssl rand -hex 24)`, `HUB_TURN_SECRET=$(openssl rand -hex 24)`,
`HUB_VLLM_SITE=<site id of the GPU site>` (the hub UI shows site ids under Organisation → Sites), `HUB_VLLM_MODEL=qwen2.5vl:7b`.
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
without a new claim. New hub instead: `docker compose -f hub/docker-compose.yml exec hub python -m hub createsuper you@example.com`.

## 5. Point the sites at the new hub
On each site's own LAN (the hub proxy refuses this endpoint on purpose):
```bash
curl -X PUT http://<site>:8080/api/hub -H 'content-type: application/json' -d '{"hub_url":"wss://hub.axiomvision.ai/agent"}'
```
Do the GPU site first (it serves the shared AI). `GET /api/hub` shows `connected: true` within a few seconds;
the hub's Fleet page shows the site online. Also set `NVR_HUB_URL=wss://hub.axiomvision.ai/agent` in each site's
`.env` (the database setting wins, but a fresh install reads `.env`). New sites: `tools/deploy_site.sh` already
writes that URL; claim them from Organisation → Add site.

## 6. Afterwards
- Remove any router port-forward that pointed at the old hub, and stop the old `python -m hub`.
- Phones: open https://hub.axiomvision.ai, sign in, Add to Home Screen; allow notifications (push works over HTTPS).
- Update: `cd /opt/axiom && git pull && docker compose -f hub/docker-compose.yml up -d --build`.
- Database dump (add to root's crontab): `0 4 * * * docker compose -f /opt/axiom/hub/docker-compose.yml exec -T postgres pg_dump -U hub hub | gzip > /srv/backups/hub-$(date +\%F).sql.gz`

## Checks
| What | How |
|---|---|
| Certificate and login | https://hub.axiomvision.ai shows the padlock and the login page |
| Sites online | Fleet page; `curl -s https://hub.axiomvision.ai/api/health` → `sites_online` |
| Live video from a phone off Wi-Fi | tile plays; `chrome://webrtc-internals` shows a `relay` candidate (TURN) |
| Shared AI | Organisation → Shared AI card says "served by the GPU at <site>"; lite sites write synopses |
| No inbound ports at sites | the router forwards nothing; `ss -tlnp` on a site shows 8080 bound to the LAN only |
