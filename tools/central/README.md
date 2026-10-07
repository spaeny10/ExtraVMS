# Central recording host

One datacenter server records many customer Sites. Each Site gets its own **instance**: the ordinary site server (recording, ONVIF events, YOLO verification, synopses, hub tunnel) in its own container, enrolled into that customer's Site on the hub like any on-site server. The hub drives the host through `axiom_host.py`; the cameras reach the host over SpeedFusion or locked port forwards (`PEPLINK.md`).

```
G481 (Ubuntu 24.04, Docker + NVIDIA Container Toolkit)
├─ axiom-host agent (systemd, root) ── dials wss://hub.axiomvision.ai/host-agent      PROTOCOL.md
├─ axiom-vllm (compose, A40) ── Qwen-VL, OpenAI /v1, on network axiom-ai               ai/compose.yml
├─ FusionHub (KVM VM) ── terminates every VPN-mode site's SpeedFusion tunnel          PEPLINK.md
└─ instances, one per Site: container axiom-<id>, network axiom-<id> (a /28), uid 20000+slot,
   YOLO on the A10 (--gpus device=N), ZFS datasets with quotas, nftables egress allow-list   instance/Dockerfile
   /srv/axiom/instances/<id>/{instance.env,data/}   /srv/axiom/recordings/<id>/   (each a ZFS dataset)
```

Files here:

| File | What |
|---|---|
| `axiom_host.py` | Provisioner and hub agent (CLI + `run`). Python 3.12, stdlib + `websockets` + `certifi`. |
| `PROTOCOL.md` | Agent ↔ hub frames (shared with `hub/`). |
| `instance/Dockerfile`, `instance/Dockerfile.dockerignore` | The instance image. |
| `ai/compose.yml`, `ai/ai.env.example` | The shared vLLM. |
| `systemd/axiom-host.service`, `systemd/axiom-firewall.service` | Units. |
| `host.json.example` | Host settings (`/etc/axiom/host.json`). |
| `PEPLINK.md` | Site router runbook, both modes, data budget. |
| `test_axiom_host.py` | Tests (no Docker needed): `python tools/central/test_axiom_host.py`. |

## 1. Install the host (G481, by hand, once)

Everything below runs as root on the console or over SSH from an admin address.

### Ubuntu and storage

1. Ubuntu Server 24.04 LTS on the boot SSDs. Keep the OS disk separate from recordings.
2. Recordings array: a **ZFS raidz2 pool** named `axiom` (recommended; XFS with project quotas is the alternative below). The data disks go to ZFS directly: no hardware RAID volume underneath (set the controller to HBA/JBOD mode).
   ```bash
   apt install -y zfsutils-linux
   ls -l /dev/disk/by-id/                           # name the disks by id, never sdX (those change between boots)
   zpool create -o ashift=12 \
     -O compression=lz4 -O atime=off -O xattr=sa -O acltype=posixacl -O mountpoint=/srv/axiom \
     axiom raidz2 /dev/disk/by-id/<disk1> /dev/disk/by-id/<disk2> ...      # ALL DATA ON THESE DISKS IS LOST
   zfs create -o recordsize=1M  axiom/recordings    # /srv/axiom/recordings: large sequential video segments
   zfs create -o recordsize=64K axiom/instances     # /srv/axiom/instances: SQLite databases and small files
   zfs create -o recordsize=1M  axiom/ai            # /srv/axiom/ai: the vLLM model cache
   zfs list -o name,mountpoint,recordsize,compression,quota
   findmnt -no FSTYPE,SOURCE --mountpoint /srv/axiom/recordings      # "zfs axiom/recordings"
   ```
   The children inherit their mountpoints (`/srv/axiom/<name>`) and lz4/atime/xattr/acl settings from `axiom`. Do not put anything else in `axiom/recordings` or `axiom/instances`: `axiom_host.py` creates one child dataset per instance in each.

   Why ZFS:
   - **Per-Site datasets with quotas.** `create-instance` creates `axiom/recordings/<id>` (quota = the Site's `quota_gb`) and `axiom/instances/<id>` (quota = `instance_dir_quota_gb` in `host.json`, default 50 GB). Each instance sees its quota as its disk (section 4), `set-quota` is one `zfs set`, usage is exact and free to read, and `delete-instance --purge` is a `zfs destroy` of just those two datasets instead of an `rm -rf` through millions of segment files.
   - **Checksums.** Every block is checksummed; a bad sector or a disk returning wrong data is detected on read and repaired from parity instead of silently corrupting footage or a Site's database. raidz2 survives any two failed disks.
   - **Scrubs.** A scrub reads every block and repairs what it finds before a second disk failure can make it unrecoverable. Ubuntu's `zfsutils-linux` already schedules one: `/etc/cron.d/zfsutils-linux` scrubs every imported pool on the second Sunday of each month. Leave it in place (check the file exists after install), and look at the result with `zpool status axiom` (`scan: scrub repaired 0B ... with 0 errors`). `zpool status -x` prints `all pools are healthy` when nothing needs attention; `zfs-zed` (installed with zfsutils) can mail on disk faults if `ZED_EMAIL_ADDR` is set in `/etc/zfs/zed.d/zed.rc` and the host can send mail.
   - Boot: `zfs-import-cache` and `zfs-mount` (enabled by the package) import and mount the pool before `local-fs.target`, so before `axiom-firewall` and Docker. If the pool ever fails to import, the per-instance directories do not exist and Docker refuses to start the instances (`--mount type=bind` needs its source), so nothing records onto the boot disk; fix the pool (`zpool import axiom`), then `systemctl restart axiom-host`.
   - Snapshots count against a dataset's quota and make `zfs destroy` refuse. Do not snapshot `axiom/recordings/*`; if a purge reports `could not destroy ZFS dataset`, list them with `zfs list -t snapshot -r axiom/recordings/<id>` and remove them by hand. The agent never uses `zfs destroy -r` or `-f`.

   **Alternative: XFS with project quotas.** One large block device (hardware RAID or `mdadm` RAID 6/10) formatted XFS and mounted at `/srv/axiom`:
   ```bash
   apt install -y xfsprogs
   mkfs.xfs -L axiom /dev/md0                       # ALL DATA ON IT IS LOST
   mkdir -p /srv/axiom
   echo 'LABEL=axiom /srv/axiom xfs defaults,noatime,prjquota 0 2' >> /etc/fstab
   mount /srv/axiom
   xfs_quota -x -c state /srv/axiom                 # "Project quota state ... Accounting: ON, Enforcement: ON"
   ```
   It must be its own filesystem: project quotas on the root filesystem need kernel boot flags. XFS has no data checksums and no scrub of footage, and a purge is an `rm -rf`.

   `quota_mode` `auto` (the default for `create-instance`) picks `zfs` when `/srv/axiom/recordings` is the mountpoint of a ZFS dataset, else `xfs` when `/srv/axiom` is XFS mounted with `prjquota`, else `none`. The mode is stored per instance, so a host keeps managing existing instances the way they were created.
3. `apt install -y chrony nftables jq curl` and `systemctl enable --now chrony`. Instances use the host's clock (a container cannot set time), so the host's NTP is the one that matters.

### NVIDIA driver, Docker, Container Toolkit

```bash
ubuntu-drivers list --gpgpu                         # pick the newest *-server driver (570 or later for CUDA 12.8)
apt install -y nvidia-driver-570-server             # the version ubuntu-drivers recommends
reboot
nvidia-smi -L                                       # note which index is the A40 and which the A10
systemctl enable --now nvidia-persistenced

# Docker Engine from Docker's repository (docs.docker.com/engine/install/ubuntu), then:
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' \
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt update && apt install -y nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=docker
```

`/etc/docker/daemon.json` (merge with what `nvidia-ctk` wrote):
```json
{
  "live-restore": true,
  "log-driver": "json-file",
  "log-opts": {"max-size": "50m", "max-file": "5"}
}
```
`live-restore` keeps instances recording while Docker itself restarts or upgrades. Leave Docker's iptables management on (it does the NAT). Then `systemctl restart docker` and check a GPU from a container: `docker run --rm --gpus device=1 ubuntu nvidia-smi -L` shows only that card.

Docker's default address pools (172.17.0.0/12, 192.168.0.0/16) do not overlap the instance pool `10.200.0.0/16`, the AI network `10.201.0.0/24`, FusionHub's `10.19.0.0/24` or site LANs `10.20.0.0/16`. Keep it that way if you change pools.

Do not enable `ufw`: it does not see container traffic and fights Docker's rules. If the host needs its own input rules (SSH from admin addresses only), put them in `/etc/nftables.conf` in a table of your own, never in `inet axiom`.

### Build the instance image

On the host (or a build machine that pushes to a private registry):
```bash
git clone <repo> /opt/axiom-src && cd /opt/axiom-src      # or rsync from the dev PC (never its .env)
# models/ is gitignored: copy yolo11s.pt, yolo11n.pt, ppe_yolov8s.pt, osnet_x0_25_msmt17.pt and open_clip/ into models/
tag=$(git rev-parse --short HEAD)
docker build -f tools/central/instance/Dockerfile -t axiom/instance:$tag .
docker tag axiom/instance:$tag axiom/instance:latest
```

### Shared AI (vLLM on the A40)

```bash
install -d -m 700 /etc/axiom /srv/axiom/ai/hf-cache
install -m 600 tools/central/ai/ai.env.example /etc/axiom/ai.env
sed -i "s|^VLLM_API_KEY=.*|VLLM_API_KEY=$(openssl rand -hex 24)|" /etc/axiom/ai.env
nano /etc/axiom/ai.env                                     # AXIOM_VLM_GPU = the A40's nvidia-smi index
docker compose -f tools/central/ai/compose.yml --env-file /etc/axiom/ai.env up -d
docker logs -f axiom-vllm                                  # first start downloads the model
```

GPU memory: the A40 (48 GB) runs only vLLM. `Qwen/Qwen2.5-VL-32B-Instruct-AWQ` (~20 GB of weights) at `--gpu-memory-utilization 0.90` leaves roughly 18-20 GB of KV cache, about 70k tokens: 15+ concurrent 4-image synopsis requests. A bigger model (e.g. the Qwen 27B that wrote better prose on the PRO 4000) needs an FP8 or AWQ build to fit (BF16 27B = 54 GB). Prefer an **Instruct** (non-thinking) checkpoint.

Check it answers the way the instances will call it, including the `reasoning_effort: "none"` that `backend/nvr/vlmroute.py` always sends:
```bash
KEY=$(grep ^VLLM_API_KEY= /etc/axiom/ai.env | cut -d= -f2); MODEL=$(grep ^AXIOM_VLM_MODEL= /etc/axiom/ai.env | cut -d= -f2)
docker run --rm --network axiom-ai curlimages/curl -s http://axiom-vllm:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"max_tokens\":20,\"reasoning_effort\":\"none\",\"messages\":[{\"role\":\"user\",\"content\":\"Say OK\"}]}"
```
A 400 mentioning `reasoning_effort` means this vLLM build rejects the value `none`: report it (the server would need to omit the field for vLLM) rather than editing the instance.

### Install the agent

```bash
install -d /opt/axiom-host
install -m 755 tools/central/axiom_host.py /opt/axiom-host/
python3 -m venv /opt/axiom-host/venv && /opt/axiom-host/venv/bin/pip install 'websockets>=13' certifi
install -m 600 tools/central/host.json.example /etc/axiom/host.json        # adjust if needed
install -m 644 tools/central/systemd/axiom-firewall.service tools/central/systemd/axiom-host.service /etc/systemd/system/
```

Enroll the host: a hub admin adds the host on the hub (Hosts → Add host) and gets a **host token**. Put it on the host without it touching shell history or a repo:
```bash
install -m 600 /dev/null /etc/axiom/host-token && nano /etc/axiom/host-token   # paste, save
systemctl daemon-reload
systemctl enable --now axiom-firewall axiom-host
journalctl -u axiom-host -f                                                     # "connected to wss://..."
```
The agent runs on the host, not in a container, because it drives `docker`, `nft` and `zfs` (or `xfs_quota`) as root; containerizing it would need a privileged container with the Docker socket, which is the same trust with more moving parts.

## 2. Instances

### Create (hub: Site → Servers → Add central recording; or by hand)

The hub sends `create_instance` with a one-time enrollment token bound to the Site. By hand (Phase 1, before the hub's host pages exist), with the token from the hub saved in a file:
```bash
cd /opt/axiom-host
./venv/bin/python axiom_host.py create-instance --id acme-gate --location <location_id> \
   --name "Acme Gate · Central" --mode vpn --subnet 10.20.7.0/24 --quota-gb 4000 --gpu 1 \
   --enroll-token-file /root/acme-gate.token --dry-run          # read the plan: docker run, env, firewall
./venv/bin/python axiom_host.py create-instance ... (same, without --dry-run) && shred -u /root/acme-gate.token
./venv/bin/python axiom_host.py list
docker logs -f axiom-acme-gate
```
Within a minute the instance enrolls itself into the Site and appears on the hub as "Acme Gate · Central"; add its cameras there (PEPLINK.md). The agent then removes the spent token from `instance.env`.

What `create-instance` does, in order (if a step fails, the earlier ones are rolled back):
1. `/srv/axiom/instances/<id>/` (root, 711), `data/` and `/srv/axiom/recordings/<id>/` (owned by the instance's uid `20000+slot`, 700). On ZFS the two folders are new datasets, created before the folders' owner and mode are set:
   `zfs create -o quota=<bytes> axiom/instances/<id>` (`instance_dir_quota_gb`, default 50 GB) and `zfs create -o quota=<bytes> axiom/recordings/<id>` (`quota_gb`). Quotas are passed in bytes because `quota_gb` is decimal GB and ZFS's `G` suffix means GiB. If `/srv/axiom/instances` is not a ZFS dataset, `instances/<id>` is a plain folder without a quota (the result says so).
2. XFS only: project `70000+slot` covering both `data/` and the recordings folder, hard limit = `quota_gb`.
3. `instance.env` (root, 600): hub URL, enrollment token, `NVR_INSTANCE_NAME`, `NVR_DIRECT_ENABLED=0`, `NVR_HOST=127.0.0.1`, `NVR_LOCAL_VLM_ENABLED=0`, the vLLM URL/key/model, `NVR_YOLO_DEVICE=cuda:0` (or CPU settings), container paths.
4. Docker network `axiom-<id>`: its own /28 from `10.200.0.0/16` (gateway .1, instance .2, vLLM .3), bridge `axb<slot>`.
5. Firewall regenerated and loaded (before the container's first packet).
6. vLLM container connected to the network as `vllm` at .3.
7. `docker run` with: `--read-only` root and a 1 GB `/tmp` tmpfs, `--cap-drop ALL`, `no-new-privileges`, the instance uid, 8 GB memory (no swap), 4 CPUs, 4096 pids, `--gpus device=<N>`, `--restart unless-stopped`, `--init`, log rotation 5 × 50 MB, and **no published ports**. The instance only dials out.

### Day to day

```bash
axiom_host.py list                                   # state, cameras, quota/used, GPU, mode
axiom_host.py capacity                               # what the hub sees for placement
axiom_host.py set-quota --id acme-gate --quota-gb 6000
axiom_host.py restart-instance --id acme-gate        # docker restart
axiom_host.py restart-instance --id acme-gate --image axiom/instance:<tag>    # upgrade one instance
axiom_host.py delete-instance --id acme-gate --keep-data    # or --purge to delete the footage too (one is required)
axiom_host.py delete-instance --id acme-gate --purge --dry-run   # ZFS: prints the two `zfs destroy` commands, runs nothing
axiom_host.py render-firewall                        # print the ruleset; nft list table inet axiom shows counters
axiom_host.py reconcile                              # re-apply firewall/quotas/networks/vLLM links (also at agent start)
```

Upgrading every instance: build and tag the new image, then `restart-instance --image` one instance at a time, checking each comes back on the hub before the next. Each restart is a ~30 s recording gap for that Site (cameras keep recording to their SD cards).

Read-only root: if a library needs to write outside `/tmp`, `/data` or `/recordings`, the instance crash-loops at start (`docker logs`). Set `"read_only": false` in `/etc/axiom/host.json` and `restart-instance --recreate`, then report it so the image can be fixed.

## 3. Isolation design

- **Network**: each instance has its own Docker network; Docker already blocks traffic between different bridge networks, and the vLLM container is the only other member of each instance's network (it never initiates connections). Instances never share a network with each other, not even the AI one: vLLM is attached into each instance network instead.
- **Egress allow-list** (`nft` table `inet axiom`, rendered from `/srv/axiom/registry.json`, replaced atomically by `nft -f` on every create/delete and at boot by `axiom-firewall.service` before Docker starts). Per instance, by its exact address:
  - its site: `10.20.<n>.0/24` (VPN) or the site's public IP over TCP (forwards),
  - the hub's addresses (resolved from the hub host names; re-resolved every 10 minutes): TCP 443 and 3478, UDP 3478 and 49152-49252 (TURN, `hub/coturn`),
  - its own vLLM address, TCP 8000,
  - DNS to the resolvers in `host.json` (`--dns` on the container),
  - everything else dropped, including other instances, other sites, the internet and the host itself (input chain). New connections into the instance pool are dropped too.
  - NTP is not allowed: containers use the host clock; the host runs chrony.
- **Why our own nftables table, not DOCKER-USER**: on Ubuntu 24.04, Docker (27/28) programs iptables through the iptables-nft backend, so its rules and ours live in the same nftables kernel tables and a drop in either is final. A separate `inet axiom` table with base chains at priority `filter - 10` (before Docker's FORWARD) can be replaced in one transaction (`table / delete table / table {...}` in one `nft -f`), is never touched when Docker restarts or rewrites its chains, and keeps working if Docker is switched to its native nftables backend (Docker 29+), which has no DOCKER-USER chain at all. Our `accept` only ends our chain; Docker's chains still run (and masquerade) afterwards. Intra-bridge traffic (instance ↔ its vLLM address) is filtered too because Docker loads `br_netfilter` (`sysctl net.bridge.bridge-nf-call-iptables` = 1).
- **Files**: each instance runs as its own uid and owns only its `data/` and recordings folder (700); `instance.env` is root-only. A process escaping one container still cannot read another instance's footage.
- **Container**: read-only root, no capabilities, no privilege escalation, memory/CPU/pids limits, API bound to loopback inside the container (`NVR_HOST=127.0.0.1`: the hub tunnel is in-process, so nothing needs the port), no Direct-on-LAN.
- **Not isolated**: GPU memory. Instances on the same A10 share its 24 GB with no per-container limit; one runaway instance can starve the others' YOLO (they fall back to fewer verifications, they do not stop recording). Watch `nvidia-smi` during Phase 0 and keep instances per GPU within the measured budget.

## 4. Storage quota

**ZFS** (`quota_mode: zfs`): the instance's `/recordings` is the dataset `axiom/recordings/<id>` and its `/data` lives in `axiom/instances/<id>`, each a filesystem of its own with a ZFS quota, so `statfs` inside the container reports the quota as the disk size. The server's retention works unchanged: its free-space floor (`keep.default_min_free_gb`: 10 % of the disk, at most 200 GB, editable under System → Retention) applies to the recordings quota, and continuous footage is trimmed before the dataset reaches it. Recordings and `/data` are separate filesystems, so `retention.same_volume()` is false and the database/event media side gets the server's own small emergency floor (5-20 GB) inside the instance dataset's quota. `quota_gb` limits the recordings dataset only; each instance also takes up to `instance_dir_quota_gb` (default 50 GB, set in `host.json` before creating; raise it for Sites that keep many event clips). `used_gb` in the heartbeat is the `used` of both datasets (exact, read with `zfs get -Hp`), and `set-quota` refuses a quota below the recordings dataset's own `used` unless forced. A ZFS quota counts snapshots too: another reason not to snapshot recordings.

The agent's ZFS commands, all on exactly `<dataset at /srv/axiom/recordings>/<id>` and `<dataset at /srv/axiom/instances>/<id>` (it refuses any name in the registry that is not that direct child, so it can never act on a parent, a sibling or the pool):

| Operation | Commands |
|---|---|
| detect (`auto`) | `findmnt -n -o FSTYPE,SOURCE --mountpoint /srv/axiom/recordings` (and `.../instances`) |
| `create-instance` | `zfs list -H -o name <ds>` (refuse if it exists), `zfs create -o quota=<instance_dir_quota_gb × 10⁹> axiom/instances/<id>`, `zfs create -o quota=<quota_gb × 10⁹> axiom/recordings/<id>`; rolled back with `zfs destroy <ds>` if a later step fails |
| `set-quota` | `zfs get -Hp -o name,property,value used axiom/recordings/<id>`, then `zfs set quota=<bytes> axiom/recordings/<id>` |
| `delete-instance --purge` | `zfs destroy axiom/recordings/<id>`, `zfs destroy axiom/instances/<id>` (no `-r`, no `-f`; a failure is reported in the result, not forced) |
| `delete-instance --keep-data` | none: both datasets stay, quotas included, and block re-creating the same id |
| `reconcile` | `zfs set quota=...` on both datasets again (repairs hand edits) |
| usage, capacity | `zfs get -Hp -o name,property,value used <both datasets>`; host disk: `zfs get ... used,available axiom axiom/recordings` |

Host capacity on ZFS: `disks[0].total_gb` is the pool's usable size (`used` + `available` of the `axiom` dataset, after raidz2 parity) and `free_gb` is `available` of `axiom/recordings`. `statvfs` on `/srv/axiom` would be wrong there (it leaves out every child dataset's data).

**XFS** (`quota_mode: xfs`): with XFS project quotas, the instance sees its quota as its disk: `statfs` on a project directory reports the project's limit and usage. The server's existing retention therefore works unchanged: its free-space floor (`keep.default_min_free_gb`: 10 % of the disk, at most 200 GB, editable under System → Retention) applies to the quota, and continuous footage is trimmed before the instance reaches the hard limit. Recordings and the database/event media share the one project, so `retention.same_volume()` is true and one floor covers both. `used_gb` in the heartbeat is exact and free to read.

**Without XFS project quotas** (`quota_mode: none`, e.g. ext4): nothing enforces `quota_gb`. The server has no environment setting that caps its usage: the retention floor is a free-space floor stored in each instance's database, and on a shared filesystem every instance sees the same free space, so floors cannot divide the disk. The first instance to write fills it, and then every instance trims together when the shared disk reaches the floor. The agent still reports `used_gb` (from `du`, every 30 minutes) so the hub can flag Sites over their plan, and `set-quota` refuses to go below usage, but it is accounting, not a limit. Use ZFS (or XFS) for production; if a host must run without either, a per-instance loop-mounted XFS image file is the workaround (not automated).

## 5. Capacity (to confirm in the Phase 0 spike)

- CPU is the first limit: an instance with 5 cameras at 4 CPUs/8 GB (defaults; `--cpus`, `--mem-gb` per instance) means ~16-18 instances on 80 threads with room for vLLM, FusionHub and the host. RAM (768 GB) is not the limit.
- A10 (24 GB): YOLO11s at 1280 + CLIP + OSNet + PPE take roughly 1.5-2.5 GB per instance, so ~8-10 instances per A10 by memory; spread across A10s as more are added (`--gpu`).
- A40: one vLLM for every instance on the host; synopsis throughput is the number to measure.
- Storage: plan quotas against usable space; `capacity.allocated.quota_gb` vs `disks[].total_gb` is what the hub uses for placement. On ZFS add `instance_dir_quota_gb` (50 GB) per instance, and keep the pool below about 80 % full: ZFS slows down as a pool fills.

## 6. Adding a second host

Repeat section 1 on the new server (Ubuntu, the `axiom` ZFS pool or XFS `/srv/axiom`, driver, Docker, toolkit, image, vLLM, agent), with its own host token from the hub. Instance networks and the pool are local to each host, so the same `10.200.0.0/16` is reused. For VPN-mode sites, the new host routes `10.20.0.0/16` to the existing FusionHub over the shared `10.19.0.0/24` VLAN (PEPLINK.md); no second FusionHub is needed. Storage stays on each host: moving a Site between hosts uses the existing move/migrate fleet actions.

## 7. Troubleshooting

- Instance not enrolling: `docker logs axiom-<id> | grep hub`; is the hub address in `nft list set inet axiom hub4`? (`axiom_host.py reconcile` re-resolves.)
- Instance can't reach a camera: `nft list table inet axiom` (counter on the instance's final drop rising?), `ip route get 10.20.<n>.11` on the host (via 10.19.0.2?), SpeedFusion status in InControl 2.
- No synopses: `docker inspect -f '{{json .NetworkSettings.Networks}}' axiom-vllm` lists `axiom-<id>`? `docker logs axiom-vllm`; the instance's Settings → System shows the remote model state.
- After a reboot: `systemctl status axiom-firewall axiom-host`; `axiom-firewall` failing keeps Docker from starting on purpose (no instance runs unfiltered): fix the error it logs, or `systemctl start axiom-firewall` once `/srv/axiom` is mounted.
