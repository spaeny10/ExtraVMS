# Axiom Bridge: plan (agreed 2026-10-18)

A site with cameras and no inbound access (Starlink CGNAT, cellular, no static IP, no Peplink) is recorded by a
central recording instance (fred, or any host running tools/central/axiom_host.py). A small Go program on a PC at the
site, the **Axiom Bridge**, dials the hub over TCP 443 and carries TCP streams to the cameras the hub assigned.

First site: one Milesight camera + a Windows 10 IoT mini PC (Celeron J4125, 4 GB) on Starlink, unlimited plan
(record the main stream).

## Decisions (Shawn, 2026-10-18)

- Our own relay, not Tailscale.
- **Roles:** hub admins AND the Customer's Site admins may create Bridge enrollment codes for their Site, retire
  their Site's bridges and map their Site's cameras to their Site's central instance. Allocation of central instances
  stays hub-admin only (docs/roles.md). Viewers/operators see bridge status read-only.
- **Ports:** 554 (RTSP) + 80 (ONVIF, PTZ, events, camera web page) + 555 (Milesight SD replay, refills gaps).
- **Apply:** after mapping, one click on the hub writes the relay `public_host` / `public_*_port` fields into the
  instance's camera (GET then PUT through the tunnel; never touches passwords). The sheet values are also shown.
- **Alerts:** `bridge_offline` goes to hub admins and to the Customer (like camera_down), not only hub-level.
- Main stream at the camera's bitrate (unlimited Starlink plan). Hetzner bills egress: ~1.3 TB/month per 4 Mbit/s
  camera each way through the hub; watch it as bridges multiply.

## Architecture

TCP port relay that looks like the existing port-forward camera mode, so instances need no change:

instance MediaMTX / ONVIF client → `<instance gateway>:<listen_port>` on the host (listener in axiom_host.py)
→ `WS /host-relay` → hub splices → `WS /bridge` → bridge dials `camera_ip:port`.

The NVR already handles it (backend/nvr/onvif_soap.py `outside()` / `rewrite()`: every http URL → public_onvif_port,
rtsp → public_rtsp_port; mediamtx.camera_url uses outside() and rtspTransport tcp; sdreplay uses public_replay_port;
camweb uses public_web_port or public_onvif_port when web == onvif). Instance camera shape:

```json
{"id":"gate","name":"Gate","host":"192.168.1.50","onvif_port":80,"rtsp_port":554,"username":"…","password":"(instance only)",
 "public_host":"<instance gateway>","public_rtsp_port":47000,"public_onvif_port":47001,"public_replay_port":47002}
```

Known limits: all ONVIF services on one HTTP port (fine for Milesight); camera→speaker conversion (api.py ~804)
copies the LAN host (out of scope, document); no ONVIF scan/discovery over the bridge (add cameras by hand).

Rejected: an L3 virtual network (TUN driver on Windows, NAT per site, whole-LAN reach, subnet clashes).

## Wire protocol (binding for both sides)

tunnelproto framing: JSON text control frames with `"t"` (unknown types/fields ignored); binary frames =
`>I stream_id` + up to CHUNK = 65536 bytes. `end {id}` = half-close (TCP FIN). `abort {id, reason}` = reset.
`credit {id, bytes}` is sent by a receiver only AFTER the bytes were written to the TCP socket (not on arrival).
New `open` / `opened` (no HTTP semantics). Only one side opens streams per connection: the hub on `/bridge`, the host
on `/host-relay`; ids odd. Ordering: `end`/`abort` go through the same ordered queue as the stream's data; only
`credit`, `ping`/`pong`, `heartbeat` may jump ahead. Golden vectors: bytes from Python `tunnelproto.encode` / `chunk`.

### `WS /bridge` (bridge ↔ hub)

Auth header `Authorization: Enroll <code>` → hub waits for `hello` (15 s), consumes the code atomically, sends
`{"t":"enrolled","bridge_id","token"}`, closes (as agents._enroll). `Authorization: Bearer <device token>` = normal.
401/403 at the handshake → the bridge retries every 5 min.

Bridge → hub: `hello {proto:1, version, os, arch, hostname, lan:[{"if","addr":"192.168.1.20/24"}], caps:["tcp","probe"]}`;
`heartbeat {streams, bytes_up, bytes_down, uptime_s, target_errors:{"bt_x:554":"connection refused"}}` every 30 s;
`opened {id}`, chunks, `end`, `abort`, `credit`, `probe_result {id, ok, ms, error}`, `rotated`, `ping`/`pong`.

Hub → bridge: `welcome {bridge_id, name, site, heartbeat_s:30, max_streams:16, window:524288}`;
`targets {rev, items:[{id:"bt_…", host:"192.168.1.50", ports:[554,80,555]}]}` (full list, after welcome and on every
change); `open {id, target, host, port}`; `probe {id, target, host, port}` (TCP connect only); `rotate {token}`;
`revoked {reason}`; chunks, `end`, `abort`, `credit`, `ping`. The hub closes a bridge that sends `open` or exceeds the
advertised window.

### `WS /host-relay` (host agent ↔ hub, same host token as `/host-agent`)

Host → hub: `hello {proto:1, version, listeners:[{instance, port, ok, error}]}`;
`open {id, instance:"ci_…", port:47001, peer:"10.200.0.50"}` (the host names a listener, never a camera address);
chunks, `end`, `abort`, `credit`, `ping`. Hub → host: `opened {id}` or `abort {id, reason}`; chunks, `end`, `credit`, `pong`.

### Host command on `/host-agent`

`set_relay {id, ports:[{port:47001, label:"Gate RTSP"}], dry_run}`: validate, save `rec["relay_ports"]`, regenerate +
load the firewall with one `nft -f`, reload listeners. The instance object (result + heartbeats) gains `gateway` and
`relay: {ip, ports, listening:[…], errors:{…}}`; `capacity` gains `relay: {port_range:[47000,47999], connected}`.

### Hub splice

On `/host-relay` `open`: look up (instance, port) → bridge_targets row; require instance on this host and live,
target enabled, bridge online, under its stream cap and open rate; send `open` to the bridge; `opened` within 10 s →
`opened` to the host. Two pumps per stream: read chunk → take_credit on destination → send → then credit the source.
Memory bounded by one window per direction per stream. Single hub worker.

## Components

1. **Bridge** (`bridge/`, Go 1.26, module `axiomvision.ai/bridge`, deps `github.com/coder/websocket`,
   `golang.org/x/sys`, no cgo): internal/proto (frames + golden tests), internal/tunnel (dial, TLS system roots,
   wss required unless localhost, read limits CHUNK+1024 / 1 MB text, one writer goroutine with ordered data queue +
   priority control queue, ws ping 20 s / dead after 40 s, reconnect full-jitter 1→60 s, 5 min on 401/403),
   internal/relay (dial 5 s, window Take/Grant, bounded write channel, credit after TCP write, end→CloseWrite,
   abort→linger 0), internal/policy (allowed {(host,port)} from targets, replaced atomically, aborts streams to removed
   targets; hard denies: loopback, link-local/metadata, multicast, unspecified, broadcast, own interface addresses,
   non-RFC1918 unless allow_public, DNS names; optional owner `allow` list), internal/config
   (`%ProgramData%\Axiom Bridge\config.json` {hub_url, allow?, ca_file?, log_level}; token file separate; Linux
   /etc/axiom-bridge/), internal/enroll, internal/logx (5 MB × 3, never tokens/payloads), internal/winsvc
   (install: copy to Program Files, config, enroll synchronously, token ACL SYSTEM+Administrators, service AxiomBridge
   auto delayed start, recovery restarts, event log source; uninstall [--purge]), cmd/axiom-bridge
   (install/uninstall/run/status/version). Build script bridge/build.ps1. Unsigned exe for now; auto-update later.
2. **Hub** (hub/hub/bridges.py + db tables bridges, bridge_enroll_codes, bridge_targets; api.py websockets /bridge and
   /host-relay, sweeper, endpoints; central_cameras accepts the relay address only with this instance's listen ports;
   hosts.py MODES += bridge (no subnet/public IP), OPS += set_relay, heartbeat stores gateway/relay; alerts
   bridge_offline (hub admins + Customer); config bridge_max_streams 16, window 512 KB, open 10/s, enroll TTL 24 h;
   UI: bridges list + enrollment code dialog with the install command, "Bridged cameras" per instance with Test
   (probe) and Apply-to-camera, Site page read-only status; audit rows for codes, enrolment, rename/retire, rotate,
   target changes, set_relay).
3. **Host agent** (tools/central/axiom_host.py 0.3.0): MODES += bridge; relay_ports range 47000-47999; set_relay;
   nft input rule per instance before the pool drop: `iifname "axb<slot>" ip saddr <ip> ip daddr <gateway>
   tcp dport { ports } accept`; Relay class (listeners on the gateway, peer check, own /host-relay reconnect loop,
   credit after drain, framing helpers copied in — no tunnelproto dependency on hosts); CLI set-relay; PROTOCOL.md.

## Tests and rollout

Go unit tests with a fake hub; hub pytest with fake bridge + fake host-relay + a TCP echo camera end to end; host agent
tests with fakes. Then LAN end to end (test Site + bridge-mode instance on fred, no Site networks, so only the bridge
can reach the camera): recording, ONVIF events, PTZ, camweb, SD replay, live HD/SD; fault tests (bridge stop, hub
restart, host agent restart, cable pull, fred reboot); CPU/RAM; 24–48 h on a phone hotspot; then Starlink (camera on
a static IP, PC never sleeps). Restarts: the hub and fred's host agent only; instances unchanged.
