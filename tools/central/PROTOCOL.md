# Host agent protocol (proto 1)

How a central host's `axiom_host.py run` talks to the hub. The hub side lives in `hub/` (host registry, placement: "Allocate instance" on the Hosts page). This file is the contract between them; both sides implement exactly these frames and field names.

## Connection

- The agent dials `wss://hub.axiomvision.ai/host-agent` with `Authorization: Bearer <host token>` (the token from the hub's host enrollment, kept in `/etc/axiom/host-token`, mode 600). The host opens no port.
- TLS is verified against the `certifi` bundle (system store if certifi is missing). `ws://` is accepted only for `localhost`, unless `--insecure` is given in a lab.
- HTTP 401/403 on the upgrade: the token was refused. The agent retries every 5 minutes.
- Every frame is one JSON text message. Unknown frame types and unknown fields are ignored by both sides.
- Liveness: the agent sends a WebSocket ping every 20 s and closes the connection if no pong arrives within 40 s, so a dead link is dropped within 60 s. The hub may also send app-level `{"t":"ping"}`; the agent answers `{"t":"pong"}`.
- Reconnect: exponential backoff with full jitter (random 0..backoff, backoff doubling 1 s → 60 s), reset after a connection that lasted more than 60 s.

## Agent → hub

### hello (first frame on every connection)
```json
{"t": "hello", "proto": 1, "hostname": "g481-01", "version": "0.1.0", "capacity": { ... }}
```

### heartbeat (every 30 s)
```json
{"t": "heartbeat", "capacity": { ... }, "instances": [ {instance}, ... ]}
```

### result (one per `cmd`, possibly out of order: commands run concurrently)
```json
{"t": "result", "id": 17, "ok": true, "detail": "created acme-gate", "instance": {instance}}
```
- `id`: the command's id, echoed back unchanged.
- `ok`: false when the command was refused or failed; `detail` then says why in plain words (safe to show an admin; never contains secrets).
- `instance`: present for `create_instance`, `set_quota`, `set_camera_network` and `restart_instance` when they succeed.
- `instances`: present (a list of `{instance}`) for `list`.
- `dry_run`: present when the command was sent with `"dry_run": true` (the planned commands, env file with secrets masked, firewall).
- A result that could not be sent because the connection dropped is queued (up to 200) and sent right after the next `hello`. The hub should accept results for ids it no longer tracks (log and drop) and rely on the heartbeat's `instances` as the source of truth.

### capacity object
```json
{
  "cpus": 80,
  "load": [3.1, 2.8, 2.5],
  "ram_gb": {"total": 791.2, "free": 702.4},
  "gpus": [
    {"index": 0, "name": "NVIDIA A40", "mem_total_mb": 46068, "mem_used_mb": 41234, "util": 87, "instances": 0},
    {"index": 1, "name": "NVIDIA A10", "mem_total_mb": 23028, "mem_used_mb": 3120,  "util": 12, "instances": 4}
  ],
  "disks": [{"path": "/srv/axiom", "total_gb": 61440.0, "free_gb": 58210.5, "fs": "zfs", "dataset": "axiom/recordings",
             "project_quota": false, "quota_mode": "zfs"}],
  "instances": 4,
  "allocated": {"quota_gb": 16000, "mem_gb": 32, "cpus": 16}
}
```
- `instances` here is a count; the heartbeat's top-level `instances` is the list of instance objects.
- `gpus` comes from `nvidia-smi --query-gpu=index,name,memory.total,memory.used,utilization.gpu --format=csv,noheader,nounits`; unreadable values are `null`; `util` is percent. `gpus[].instances` = instances assigned to that GPU for YOLO.
- `allocated`: the sum of every instance's storage quota, memory limit and CPU limit (for placement: storage is promised, not used).
- `load` is `null` and `ram_gb` values `null` where the OS doesn't provide them.
- `disks[].quota_mode`: what a new instance would get with `quota_mode: auto` (`zfs`, `xfs` or `none`); `project_quota` is true only for `xfs`. On ZFS, `total_gb` is the pool's usable size (`used` + `available` of its root dataset) and `free_gb` the `available` of `dataset` (the one mounted at `<root>/recordings`); elsewhere both come from `statvfs` on the root.

### instance object
```json
{
  "id": "acme-gate", "location_id": "loc_acme1", "name": "Acme Gate · Central",
  "state": "running", "cameras": 5, "quota_gb": 4000, "used_gb": 812.4, "gpu": 1, "mode": "vpn",
  "subnets": ["10.20.7.0/24", "192.168.105.0/24"], "public_ips": ["203.0.113.7"], "hosts": ["cam1.example.net"],
  "host_ips": {"cam1.example.net": ["203.0.113.21"]}, "quota_mode": "xfs", "enroll_pending": false,
  "mem_gb": 8, "cpus": 4, "image": "axiom/instance:latest", "network": "10.200.0.0/28"
}
```
- `subnets`, `public_ips`, `hosts`: the instance's camera allow-list (see `set_camera_network`). `mode` no longer decides what is allowed; it says how the site connects (the hub's Peplink sheet).
- `host_ips`: each name in `hosts` → the IPv4 addresses it last resolved to (what the firewall allows for it; `[]` = not resolved yet). Re-resolved every 10 minutes with the hub names.
- `state`: Docker's container state (`running`, `restarting`, `exited`, `created`, `paused`, `dead`), or `missing` when the container is gone; `created`/`planned` in a command's own result.
- `cameras`: from the instance's own `/api/cameras`, refreshed every 5 minutes; `null` until known.
- `used_gb`: recordings + database + event media. Exact and free to read on ZFS (`used` of the instance's two datasets) and XFS (the project quota); on other filesystems from `du` every 30 minutes, `null` until the first pass. On ZFS `quota_gb` limits the recordings dataset only (the instance dir has its own, host-wide quota), so `used_gb` can exceed `quota_gb` by up to that amount.
- `gpu`: the host GPU index the instance's YOLO uses, or `null` for CPU.
- `quota_mode`: `zfs` (per-instance datasets with quotas), `xfs` (project hard limit) or `none` (not enforced: see README).
- `enroll_pending`: true until the instance has enrolled into its Site with the one-time token.

## Hub → agent

### cmd
```json
{"t": "cmd", "id": 17, "op": "create_instance", "args": { ... }}
```
`id` is an integer chosen by the hub (unique per connection is enough). `args` names are the CLI flags in snake_case.

| op | args | notes |
|---|---|---|
| `create_instance` | `id`, `location` (alias `location_id`), `name`, `mode` (`vpn`\|`forward`), `subnet` (string or list), `public_ip` (string or list; a DNS name here counts as a `hosts` entry), optional `subnets`, `public_ips`, `hosts` (lists, added to the former), `quota_gb`, `gpu` (int, or `null`/`"none"` for CPU), `enroll_token`, `hub_url`, `vlm_url`; optional `vlm_model`, `mem_gb` (default 8), `cpus` (default 4), `image`, `quota_mode` (`auto`\|`zfs`\|`xfs`\|`none`), `dry_run` | Creates dirs, quota, `instance.env`, network, firewall rules, then starts the container. The camera allow-list is the union of all subnets, public IPs and host names, whatever the `mode`, validated as for `set_camera_network`; at least one entry is required. Refused if the id exists, a camera address is already used by another instance, or leftover data for that id exists. |
| `delete_instance` | `id`, `purge` (default false), `dry_run`; the CLI's `keep_data` is accepted too (`keep_data` wins if both are sent) | Removes the container, network, firewall rules and (XFS) quota limit. On ZFS a purge destroys exactly the instance's two datasets; kept datasets stay with their quotas. Recordings and database **stay on the host** unless `purge` is true; the hub confirms a purge with the admin first. Kept data blocks re-creating the same id. |
| `set_quota` | `id`, `quota_gb`, `force`, `dry_run` | Refused below current usage unless `force`. |
| `set_camera_network` | `id`, `subnets`, `public_ips`, `hosts` (each a list or comma-separated string; `[]` empties it; absent or `null` keeps it), `dry_run` | Replaces the instance's camera allow-list, saves the registry and regenerates and loads the whole firewall (one `nft -f`, as on create/delete). Idempotent. `subnets`: IPv4 networks inside 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 or 100.64.0.0/10, /16 or smaller, no host bits set; any protocol. `public_ips`: IPv4 addresses, not loopback, link-local, multicast, unspecified or reserved; TCP only (ports: `forward_tcp_ports` in host.json, default any). `hosts`: DNS names (letters, digits, `-`, two or more labels, lowercased), resolved to IPv4 and re-resolved every 10 minutes; TCP only like `public_ips`; a resolved address that would be refused as a public IP (or is in the pool, the AI network or a hub address) is left out. Nothing may overlap the instance pool, the AI network (`ai_network`, 10.201.0.0/24) or the hub's addresses; another instance's subnet, public IP or host name is refused; at most 32 entries in all. On an `nft` failure the old rules and registry stay. `dry_run` returns `dry_run.firewall` (the ruleset) and changes nothing. The hub sends all three lists. |
| `restart_instance` | `id`, `recreate` (default false), `image`, `dry_run` | `recreate` or a new `image` removes and re-runs the container (picks up a new image or a scrubbed env file); otherwise `docker restart`. |
| `list` | none | Result carries `instances`. |

Ids: 1-32 lowercase letters, digits, `-` and `_`, starting and ending with a letter or digit (the hub uses `ci_` + 12 hex). The container and its network are both named `axiom-<id>`; the container hostname uses `-` for `_`. Two ids that differ only by `-` versus `_` cannot coexist on one host (they would share a firewall chain name).

### Other frames the agent understands
- `{"t": "ping"}` → `{"t": "pong"}`
- `{"t": "error", "detail": "..."}` → logged.
- anything else (e.g. a `welcome` after hello) → ignored.

## Enrollment of a new instance
1. The hub mints a one-time enrollment token bound to the Site and sends `create_instance` with `enroll_token`.
2. The agent writes it to `instance.env` as `NVR_HUB_ENROLL_TOKEN` (mode 600, root). The token is never written to the registry, logged, or echoed in a result.
3. The instance dials `NVR_HUB_URL` with `Authorization: Enroll <token>` and enrolls itself into that Site (server side: `backend/nvr/hub_agent.py`).
4. The agent probes the instance (`docker exec ... /api/hub`) every minute while `enroll_pending`; once it reports `enrolled`, the token line is removed from `instance.env` and `enroll_pending` turns false in the next heartbeat. The running container keeps the spent token in its environment until its next recreate; the server ignores it once enrolled.
