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
- `instance`: present for `create_instance`, `set_quota`, `set_instance_quota`, `set_camera_network`, `restart_instance` and `set_resources` when they succeed.
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
  "allocated": {"quota_gb": 16000, "mem_gb": 32, "cpus": 16},
  "relay": {"port_range": [47000, 47999], "connected": true},
  "image": "axiom/instance:latest", "image_id": "sha256:4f1c…",
  "work": {
    "at": 1791530000.0,
    "vllm": {"ok": true, "model": "Qwen/Qwen3.8-27B-FP8", "running": 2, "waiting": 0, "waiting_capacity": 0,
             "kv_cache_pct": 34.1, "prompt_tps": 1450.2, "gen_tps": 180.4, "queue_p50_s": 0.05, "e2e_p50_s": 6.2,
             "gpu": 0, "at": 1791530000.0},
    "instances": {
      "acme-gate": {"verify_q": 29, "synopsis_q": 0, "yolo_ready": true, "vlm_ready": true, "vlm_state": "ready",
                    "yolo_frame_ms": 11.5, "verify_rate_per_min": 0.5, "gpu": 1, "ok": true, "at": 1791529999.2,
                    "gpu_mem": {"share_mb": 4096, "cap_mb": 3584, "reserved_mb": 1800, "peak_mb": 2900, "oom": 0}}
    }
  }
}
```
- `instances` here is a count; the heartbeat's top-level `instances` is the list of instance objects.
- `gpus` comes from `nvidia-smi --query-gpu=index,name,memory.total,memory.used,utilization.gpu --format=csv,noheader,nounits`; unreadable values are `null`; `util` is percent. `gpus[].instances` = instances assigned to that GPU for YOLO.
- `allocated`: the sum of every instance's storage quota, memory limit and CPU limit (for placement: storage is promised, not used).
- `load` is `null` and `ram_gb` values `null` where the OS doesn't provide them.
- `work` (agent 0.2.0+; absent from older agents and until the first collection after the agent starts): what the GPUs are working through, collected beside the heartbeat (never inside it) in at most `work_budget_s` (host.json, default 5 s):
  - `vllm`: one GET of the shared vLLM's Prometheus `/metrics` (no key needed). The address is `vllm_metrics_url` from host.json, else the `vllm_container`'s address on `ai_network` from `docker inspect` (10.201.0.10 with ai/compose.yml; the host is that bridge's gateway) and the port of `vlm_url`. `running` / `waiting` = `vllm:num_requests_running` / `_waiting`; `waiting_capacity` = `vllm:num_requests_waiting_by_reason{reason="capacity"}`; `kv_cache_pct` = `vllm:kv_cache_usage_perc` (older builds: `vllm:gpu_cache_usage_perc`) × 100. `prompt_tps` / `gen_tps` = the increase of `vllm:prompt_tokens_total` / `vllm:generation_tokens_total` per second against the oldest read of the last 2 minutes; `queue_p50_s` / `e2e_p50_s` = the median of the requests that finished in that time, interpolated from the bucket deltas of `vllm:request_queue_time_seconds` (older: `vllm:time_in_queue_requests`) and `vllm:e2e_request_latency_seconds`. Any metric this vLLM does not export, a first read, a counter that went backwards (restart) or no finished request is `null`. `gpu` = `AXIOM_VLM_GPU` from ai.env. `ok` false + `error` when it could not be read; the last good numbers stay, with their `at`.
  - `instances`: per running instance, one `docker exec axiom-<id> python -c ...` that reads its own `GET http://127.0.0.1:8080/api/system` (concurrent across instances, each at most `work_exec_timeout_s`, default 4 s). `verify_q` / `synopsis_q` = its `queues`; `verify_rate_per_min` = events that left verification (every status but `open` / `pending` in its `events` counts) per minute over up to the last 5 minutes (`null` on the first read; a count that went down, as merges and retention delete events, counts as no progress since the read before: agents older than 0.3.1 restarted the rate there instead, so a busy instance showed none); `gpu` = its assigned GPU. An instance that fails, is not running or does not answer in time keeps its last good values with `ok` false, `error` and the `at` of those values; a hung read is not started again until it ends.
  - `gpus[].verify_q`: the sum of `verify_q` over the instances assigned to that GPU (`0` for a GPU with none, `null` when none of its instances has reported).
- `image` / `image_id` (agent 0.3.1+): host.json `image`, what new instances are created from, and its Docker image ID (`docker image inspect --format {{.Id}}`, read at most every 5 minutes; `null` when Docker can't say). The hub's Hosts page flags an instance whose container runs another image ID. Tags are not compared: instances run git-rev tags (`axiom/instance:2096ed5`) while `latest` is retagged to the same build after every rebuild.
- `relay` (agent 0.3.0+): the Axiom Bridge relay's listen port range (host.json `relay_port_range`; the hub allocates 47000-47999 per instance, so the range must contain it) and whether the agent's `/host-relay` socket is up (see below).
- `disks[].quota_mode`: what a new instance would get with `quota_mode: auto` (`zfs`, `xfs` or `none`); `project_quota` is true only for `xfs`. On ZFS, `total_gb` is the pool's usable size (`used` + `available` of its root dataset) and `free_gb` the `available` of `dataset` (the one mounted at `<root>/recordings`); elsewhere both come from `statvfs` on the root.

### instance object
```json
{
  "id": "acme-gate", "location_id": "loc_acme1", "name": "Acme Gate · Central",
  "state": "running", "cameras": 5, "quota_gb": 4000, "used_gb": 812.4,
  "instance_used_gb": 12.3, "instance_quota_gb": 50.0, "instance_free_gb": 37.7, "gpu": 1, "mode": "vpn",
  "subnets": ["10.20.7.0/24", "192.168.105.0/24"], "public_ips": ["203.0.113.7"], "hosts": ["cam1.example.net"],
  "host_ips": {"cam1.example.net": ["203.0.113.21"]}, "quota_mode": "xfs", "enroll_pending": false,
  "mem_gb": 8, "cpus": 4, "image": "axiom/instance:2096ed5", "image_id": "sha256:4f1c…", "network": "10.200.0.0/28",
  "gateway": "10.200.0.1",
  "relay": {"ip": "10.200.0.1", "ports": [47000, 47001, 47002], "listening": [47000, 47001], "errors": {"47002": "Address already in use"}}
}
```
- `subnets`, `public_ips`, `hosts`: the instance's camera allow-list (see `set_camera_network`). `mode` no longer decides what is allowed; it says how the site connects (the hub's Peplink sheet).
- `host_ips`: each name in `hosts` → the IPv4 addresses it last resolved to (what the firewall allows for it; `[]` = not resolved yet). Re-resolved every 10 minutes with the hub names.
- `image`: the tag the container was created from (host.json `image`, or `restart-instance --image`); `image_id` (0.3.1+): the image ID the container runs (`.Image` of one batched `docker inspect` per heartbeat), `null` when unreadable.
- `state`: Docker's container state (`running`, `restarting`, `exited`, `created`, `paused`, `dead`), or `missing` when the container is gone; `created`/`planned` in a command's own result.
- `cameras`: from the instance's own `/api/cameras`, refreshed every 5 minutes; `null` until known.
- `used_gb`: recordings + database + event media. Exact and free to read on ZFS (`used` of the instance's two datasets) and XFS (the project quota); on other filesystems from `du` every 30 minutes, `null` until the first pass. On ZFS `quota_gb` limits the recordings dataset only (the instance dir has its own, host-wide quota), so `used_gb` can exceed `quota_gb` by up to that amount.
- `instance_used_gb`, `instance_quota_gb`, `instance_free_gb` (agent 0.3.2+; absent from older agents): the instance's data area, the container's `/data` (`nvr.db`, `events/`, backups). When it fills up the server's database writes fail, so the hub alerts on it (`instance_data_full`) and `preflight` checks it before restarts. ZFS with a data dataset of its own (`instances/<id>`): its `used` and `quota` (`null` when the quota was cleared), and free = `quota` − `used`, capped by the dataset's `available` (the pool's free space); read with the recordings dataset in one `zfs get -Hp used,quota,available` per instance per heartbeat. Everywhere else (XFS, `none`, or a ZFS host whose `<root>/instances` is not a dataset) the data shares the recordings' quota or filesystem: `instance_quota_gb` is `null`, `instance_used_gb` comes from the 30-minute `du` pass (`null` until the first) and `instance_free_gb` is the filesystem's free space there (`statvfs`; on XFS, the project's). Any figure is `null` when unreadable.
- `gpu`: the host GPU index the instance's YOLO uses, or `null` for CPU.
- `quota_mode`: `zfs` (per-instance datasets with quotas), `xfs` (project hard limit) or `none` (not enforced: see README).
- `enroll_pending`: true until the instance has enrolled into its Site with the one-time token.
- `gateway` (0.3.0+): the instance network's gateway, the host's own address on the instance's bridge `axb<slot>`: where the Axiom Bridge relay listens for it. Its cameras behind a bridge use it as their `public_host`.
- `relay` (0.3.0+): its relay ports (`set_relay`), the ones the agent's listeners hold now (`listening`) and why the others are not (`errors`, by port). The hub compares `ports` with what it maps and sends `set_relay` again when they differ (at most once a minute).
- `mode`: `vpn`, `forward` or `bridge` (an Axiom Bridge at the Site: no camera address needed, see `/host-relay`).

## Hub → agent

### cmd
```json
{"t": "cmd", "id": 17, "op": "create_instance", "args": { ... }}
```
`id` is an integer chosen by the hub (unique per connection is enough). `args` names are the CLI flags in snake_case.

| op | args | notes |
|---|---|---|
| `create_instance` | `id`, `location` (alias `location_id`), `name`, `mode` (`vpn`\|`forward`\|`bridge`), `subnet` (string or list), `public_ip` (string or list; a DNS name here counts as a `hosts` entry), optional `subnets`, `public_ips`, `hosts` (lists, added to the former), `quota_gb`, `gpu` (int, or `null`/`"none"` for CPU), `enroll_token`, `hub_url`, `vlm_url`; optional `vlm_model`, `mem_gb` (default 8), `cpus` (default 4), `gpu_mem_mb` (agent 0.3.3+: its share of the GPU in MB, 1024-49152 or 0 = no cap; default host.json `gpu_mem_mb`, 4096; ignored without a GPU), `image`, `quota_mode` (`auto`\|`zfs`\|`xfs`\|`none`), `dry_run` | Creates dirs, quota, `instance.env`, network, firewall rules, then starts the container. The camera allow-list is the union of all subnets, public IPs and host names, whatever the `mode`, validated as for `set_camera_network`; at least one entry is required, except in `bridge` mode (its cameras come through `set_relay`; the hub sends none). Refused if the id exists, a camera address is already used by another instance, or leftover data for that id exists. |
| `delete_instance` | `id`, `purge` (default false), `dry_run`; the CLI's `keep_data` is accepted too (`keep_data` wins if both are sent) | Removes the container, network, firewall rules and (XFS) quota limit. On ZFS a purge destroys exactly the instance's two datasets; kept datasets stay with their quotas. Recordings and database **stay on the host** unless `purge` is true; the hub confirms a purge with the admin first. Kept data blocks re-creating the same id. |
| `set_quota` | `id`, `quota_gb`, `force`, `dry_run` | Refused below current usage unless `force`. |
| `set_instance_quota` | `id`, `quota_gb` (1-10,000,000), `force`, `dry_run` | Agent 0.3.2+ (older agents answer `unknown op`). The quota of the instance's data dataset (ZFS `instances/<id>`: the container's `/data`; created with host.json `instance_dir_quota_gb`, default 50): `zfs set quota=<bytes>` on it, saved as `instance_quota_gb` in the registry (reconcile re-applies it). Refused below the current data use unless `force`. Refused, changing nothing, for an instance without a data dataset of its own (XFS, `none`, or `<root>/instances` not a ZFS dataset): its data shares the recordings' quota there, which `set_quota` sets. Result: `detail` (`"acme-gate data quota 150 GB"`, plus `"; only 2.5 GB free under it (preflight wants 4.5 GB)"` when the new quota leaves less than `preflight` wants) and `instance` with the new figures. `dry_run` returns the planned `zfs set`. |
| `set_camera_network` | `id`, `subnets`, `public_ips`, `hosts` (each a list or comma-separated string; `[]` empties it; absent or `null` keeps it), `dry_run` | Replaces the instance's camera allow-list, saves the registry and regenerates and loads the whole firewall (one `nft -f`, as on create/delete). Idempotent. `subnets`: IPv4 networks inside 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 or 100.64.0.0/10, /16 or smaller, no host bits set; any protocol. `public_ips`: IPv4 addresses, not loopback, link-local, multicast, unspecified or reserved; TCP only (ports: `forward_tcp_ports` in host.json, default any). `hosts`: DNS names (letters, digits, `-`, two or more labels, lowercased), resolved to IPv4 and re-resolved every 10 minutes; TCP only like `public_ips`; a resolved address that would be refused as a public IP (or is in the pool, the AI network or a hub address) is left out. Nothing may overlap the instance pool, the AI network (`ai_network`, 10.201.0.0/24) or the hub's addresses; another instance's subnet, public IP or host name is refused; at most 32 entries in all. On an `nft` failure the old rules and registry stay. `dry_run` returns `dry_run.firewall` (the ruleset) and changes nothing. The hub sends all three lists. |
| `restart_instance` | `id`, `recreate` (default false), `image`, `gpu_mem_mb` (agent 0.3.3+), `dry_run` | `recreate`, a new `image` or a new `gpu_mem_mb` removes and re-runs the container (picks up a new image, GPU share or a scrubbed env file); otherwise `docker restart`. A GPU share with the image upgrade costs one recording gap instead of two. |
| `set_resources` | `id`, `cpus` (1-64, at most the host's CPU count), `mem_gb` (2-512), `gpu_mem_mb` (agent 0.3.3+: 1024-49152, 0 = no cap; GPU instances only), `dry_run`; any may be left out (kept) | Saves the new CPU / memory limits and GPU share in the registry. CPU and memory are applied live with `docker update --cpus --memory --memory-swap` (no recording gap); a new GPU share (an environment variable, `NVR_GPU_MEM_MB`) always recreates the container, and so does a live update that fails (`docker rm -f` + `docker run`, the same path as `restart_instance` `recreate`: ~30 s without recording). The same values again change nothing. If the new container does not start, the old limits are restored and the container re-run with them, and the result is not ok. `dry_run` returns the planned commands. |
| `set_relay` | `id`, `ports` (a list of `{port, label}` or bare port numbers; `[]` closes them all), `dry_run` | Replaces the instance's Axiom Bridge listen ports (agent 0.3.0+; older agents answer `unknown op`). Each port inside `relay_port_range` (default 47000-47999), no duplicates; `label` (for logs and views only, never written into the firewall) one line of at most 80 characters. Saves `relay_ports` in the registry, regenerates and loads the whole firewall with one `nft -f`: in the `input` chain, before the pool's drop, `iifname "axb<slot>" ip saddr <instance ip> ip daddr <gateway> tcp dport { ports } accept` (the instance reaches exactly these ports of its own gateway, nothing else of the host), then reloads the listeners. On an `nft` failure the old rules and registry stay. Idempotent. Result: `instance` (with `gateway` and `relay`). The CLI `set-relay` does the same; the running agent picks it up within a minute. |
| `list` | none | Result carries `instances`. |

Ids: 1-32 lowercase letters, digits, `-` and `_`, starting and ending with a letter or digit (the hub uses `ci_` + 12 hex). The container and its network are both named `axiom-<id>`; the container hostname uses `-` for `_`. Two ids that differ only by `-` versus `_` cannot coexist on one host (they would share a firewall chain name).

### Other frames the agent understands
- `{"t": "ping"}` → `{"t": "pong"}`
- `{"t": "error", "detail": "..."}` → logged.
- anything else (e.g. a `welcome` after hello) → ignored.

## Axiom Bridge relay: `WS /host-relay` (agent 0.3.0+)

A Site with no inbound access runs the Axiom Bridge on a PC there (docs/axiom-bridge-plan.md, the hub's `bridges.py`). Its central instance reaches each camera as through port forwards: the camera's `public_host` is the instance's `gateway` and each `public_*_port` one of the instance's relay ports, which the hub allocates and sends with `set_relay`. The path:

instance → `<gateway>:<relay port>` (a listener of this agent) → `WS /host-relay` → hub → `WS /bridge` → bridge → `camera_ip:port`

- The agent keeps a second WebSocket, `wss://<hub>/host-relay` (the `/host-agent` URL with its last part replaced), with the same `Authorization: Bearer <host token>`, its own reconnect loop (the same backoff, 5 minutes after a 401/403), and ws pings every 20 s. `max_size` is one chunk plus 1 KB.
- Framing (the hub's tunnelproto, copied into axiom_host.py so a host needs nothing else): JSON text control frames with `"t"`, unknown types and fields ignored; binary frames = 4-byte big-endian stream id + up to 65536 bytes. `end {id}` = half-close (a TCP FIN), `abort {id, reason}` = reset. `credit {id, bytes}` is sent by a receiver only **after** the bytes were written to the TCP socket (drained), never on arrival. Each side may have at most 524288 bytes (the hub's `bridge_window`) unacknowledged per stream. Only the host opens streams here; ids are odd. `end` / `abort` go through the same ordered queue as the stream's data; only `credit` and `ping` / `pong` may jump ahead.
- Listeners: one per relay port of each instance, bound on that instance's gateway. A connection from any address but the instance's own is closed at once; so is every new connection while the relay socket is down (the instance retries as with any unreachable camera). A port that cannot be bound (the gateway's bridge not up yet at boot) is retried every minute and reported in `relay.errors`.

Host → hub:
```json
{"t": "hello", "proto": 1, "version": "0.3.0", "listeners": [{"instance": "ci_1a2b3c", "port": 47000, "ok": true, "error": null}]}
{"t": "open", "id": 1, "instance": "ci_1a2b3c", "port": 47001, "peer": "10.200.0.2"}
```
then chunks, `end`, `abort`, `credit`, `ping`. The host names a listener, never a camera address: the hub looks up which camera (instance, port) relays to, and refuses (`abort`) a listener of another host's instance, an unmapped port, a target that is switched off, a bridge that is offline, at its stream limit (16) or opening too fast (10 per second).

Hub → host: `{"t": "opened", "id": 1}` once the bridge connected to the camera (within 10 s), or `{"t": "abort", "id": 1, "reason": "..."}`; then chunks, `end`, `credit`, `pong`. The agent waits up to 15 s for the answer and reads nothing from the instance's socket before `opened`. On `abort` the connection is closed with a reset (SO_LINGER 0). When the relay socket drops, every open stream is reset.

## Enrollment of a new instance
1. The hub mints a one-time enrollment token bound to the Site and sends `create_instance` with `enroll_token`.
2. The agent writes it to `instance.env` as `NVR_HUB_ENROLL_TOKEN` (mode 600, root). The token is never written to the registry, logged, or echoed in a result.
3. The instance dials `NVR_HUB_URL` with `Authorization: Enroll <token>` and enrolls itself into that Site (server side: `backend/nvr/hub_agent.py`).
4. The agent probes the instance (`docker exec ... /api/hub`) every minute while `enroll_pending`; once it reports `enrolled`, the token line is removed from `instance.env` and `enroll_pending` turns false in the next heartbeat. The running container keeps the spent token in its environment until its next recreate; the server ignores it once enrolled.
