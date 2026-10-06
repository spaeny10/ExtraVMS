# Connecting a site's cameras to its central instance (Peplink BR1 Pro 5G)

A typical site: up to 5 cameras behind a Peplink BR1 Pro 5G, the cameras being the only devices on its LAN, the SIM with a public routable IP. The Site's central instance on the datacenter host pulls RTSP and ONVIF from those cameras in one of two ways, chosen per Site and stored on the hub:

| | **VPN (default)** | **Port forwards** |
|---|---|---|
| Encrypted | Yes (SpeedFusion) | No, unless the camera does RTSPS/HTTPS |
| Exposed on the cellular IP | Nothing | 2 ports per camera, open to the datacenter IP only |
| ONVIF events, metadata, PTZ | Work unchanged | Need the server's ONVIF address rewriting (Phase 2) |
| Central side | FusionHub VM | Nothing |
| Overhead | A few % bandwidth, a few ms | None |
| `create-instance` | `--mode vpn --subnet 10.20.<n>.0/24` | `--mode forward --public-ip <SIM IP>` |

## Site numbering (both modes)

Every Site gets a number `n` (1-254) when its central instance is provisioned; the hub stores it.

- BR1 LAN: `10.20.<n>.0/24`, BR1 at `10.20.<n>.1`. **Never leave the default `192.168.50.0/24`**: every BR1 ships with it, so VPN routes for two sites would collide.
- Cameras: DHCP reservations (or static) `10.20.<n>.11` … `.15` for cameras 1-5.
- DHCP range `10.20.<n>.100-.199` for anything plugged in temporarily.

## Mode 1: SpeedFusion VPN

### Central side: FusionHub as a KVM/libvirt VM

Peplink ships FusionHub as virtual-machine images (KVM/QCOW2, Proxmox, ESXi, Hyper-V, VirtualBox) and cloud images; there is no supported Docker image. Run it as a **KVM VM under libvirt on one G481**: Peplink-supported, managed from InControl 2 like any Peplink device, isolated from Docker's networking, and small (start with 2 vCPU, 2 GB RAM, the image's disk; check Peplink's current sizing for the license tier and number of peers). One FusionHub serves every VPN-mode site, on every host.

Networks (the example addresses are placeholders; pick free ones in the datacenter):

```
            datacenter public network                          internal "fusion" bridge (br-fh), 10.19.0.0/24
 Internet ── [eth0] FusionHub WAN 192.0.2.20 (own public IP)    FusionHub LAN [eth1] 10.19.0.2
                                                                  │
                                                     G481 host br-fh 10.19.0.1  ── route 10.20.0.0/16 via 10.19.0.2
                                                                  │
                                                     instance containers (10.200.x.x, masqueraded to 10.19.0.1)
```

1. Host packages: `apt install qemu-kvm libvirt-daemon-system virtinst bridge-utils`.
2. Bridges in netplan (`/etc/netplan/60-fusionhub.yaml`): `br-wan` over the datacenter uplink (or a second NIC/VLAN that carries FusionHub's public IP) and `br-fh` with no physical port, address `10.19.0.1/24`, plus the route:
   ```yaml
   network:
     version: 2
     bridges:
       br-fh:
         addresses: [10.19.0.1/24]
         routes:
           - to: 10.20.0.0/16
             via: 10.19.0.2
   ```
   (`br-wan`: enslave the uplink and move the host's own public address onto the bridge, per the datacenter's addressing; do this from the console, not over SSH.)
3. Import the FusionHub QCOW2: `virt-install --name fusionhub --memory 2048 --vcpus 2 --import --disk /var/lib/libvirt/images/fusionhub.qcow2,bus=virtio --network bridge=br-wan,model=virtio --network bridge=br-fh,model=virtio --os-variant generic --graphics none --autostart`.
4. On the FusionHub console: WAN = static public IP (preferred: a dedicated address from the datacenter). If only the host's IP is available, DNAT the SpeedFusion ports to the FusionHub WAN instead (defaults: TCP 32015 handshake, UDP 4500 data; use whatever the profile is set to). LAN = `10.19.0.2/24`, mode **routing** (not NAT, not Layer 2 bridging).
5. Add the FusionHub to the organization in InControl 2 and apply its license (FusionHub Solo is free but supports one peer; more VPN-mode sites need a paid FusionHub license: an open item).
6. A second G481 reaches the VPN sites through the same FusionHub: put `br-fh` (10.19.0.0/24) on a private VLAN that all hosts share and give each host the same `10.20.0.0/16 via 10.19.0.2` route.

The central instance's own firewall chain (`axiom_host.py render-firewall`) allows exactly its own `10.20.<n>.0/24` through this route, so instance A can never open site B's cameras even though FusionHub routes to every site.

### SpeedFusion profile (InControl 2)

- Topology: **hub and spoke**, FusionHub = hub, each BR1 = endpoint. Endpoint-to-endpoint traffic: **off** (sites never talk to each other).
- Routing: **Layer 3**. The BR1 advertises `10.20.<n>.0/24`; FusionHub advertises `10.19.0.0/24`. Do not "Send all traffic" through the tunnel: only camera traffic belongs in it.
- **WAN Smoothing: OFF.** It duplicates every packet across links; on one cellular WAN it doubles the data bill and gains nothing for recording.
- Forward Error Correction: off. Traffic distribution: default. Encryption: on (default).
- Data port: default UDP 4500 unless the carrier blocks it; keep the FusionHub WAN forwards in step.

### BR1 Pro 5G settings (each site)

- LAN: `10.20.<n>.1/24`, DHCP reservations for the cameras (above).
- Firewall:
  - Internal/inbound: allow `10.19.0.0/24` (the central side, via SpeedFusion) → `10.20.<n>.0/24` any port; deny everything else into the LAN.
  - Outbound: **deny the camera LAN → Internet** (no camera cloud/P2P phoning home), except what the cameras need from the BR1 itself (DHCP, DNS, NTP).
  - No WAN-side access to the BR1 web admin; manage through InControl 2.
- Time: cameras' NTP server = `10.20.<n>.1` if the BR1 firmware offers its local NTP server (System → Time); otherwise allow UDP 123 out to one public pool for the cameras only. Correct camera clocks keep events and recordings aligned (`NVR_CAMERA_CLOCK_OFFSET` stays 0).
- Cameras: change default passwords, create a dedicated ONVIF/RTSP user for the instance, disable UPnP and P2P/cloud services.

### Adding the cameras

On the hub (Customer › Actions, or the instance's Settings → Cameras through the hub): add each camera by its VPN address (`10.20.<n>.11` …) with the instance user's credentials, exactly as on a LAN server.

## Mode 2: Port forwards locked to the datacenter IP

No VPN, but video, ONVIF and the camera's login exchange cross the cellular network unencrypted unless the camera supports RTSPS/HTTPS. Use it where a VPN isn't possible.

### BR1 forwards (Network → Port Forwarding)

Two forwards per camera on distinct outside ports, **each with Source IP = the datacenter's public IP only**:

| Camera | LAN address | Outside RTSP → 554 | Outside ONVIF/HTTP → 80 (or the camera's ONVIF port) |
|---|---|---|---|
| 1 | 10.20.<n>.11 | 5541 | 8081 |
| 2 | 10.20.<n>.12 | 5542 | 8082 |
| 3 | 10.20.<n>.13 | 5543 | 8083 |
| 4 | 10.20.<n>.14 | 5544 | 8084 |
| 5 | 10.20.<n>.15 | 5545 | 8085 |

- No forward to the camera web UI beyond what ONVIF needs. Most cameras serve ONVIF and the web UI on the same port 80, so the ONVIF forward also exposes the web UI, to the datacenter IP only.
- Where the camera supports them, forward RTSPS (often 322) and HTTPS instead and use those ports in the camera entry.
- The SIM's public IP must be static and the carrier must allow inbound connections: confirm with the carrier before choosing this mode.
- BR1 outbound firewall and camera passwords as in VPN mode.

### Central side

- `create-instance --mode forward --public-ip <SIM IP>`. The instance may reach that IP over TCP only (any port by default). To narrow it to the forwarded ports on a host, set `"forward_tcp_ports": ["5541-5549", "8081-8089"]` in `/etc/axiom/host.json` and run `axiom_host.py apply-firewall`.
- The BR1's source restriction must match the address the host's traffic leaves with. Docker masquerades instance traffic to the host's primary public IP; if the host has several, pin it (an SNAT rule for `10.200.0.0/16`) and use that one in the BR1 rules.
- Each camera is added with host = the SIM's public IP, RTSP port 554x, ONVIF port 808x. ONVIF events, metadata and PTZ need the server's address rewriting (cameras answer with their `10.20.<n>.x:80` URLs), which is Phase 2 of the plan.

## Cellular data budget

Recording is continuous upload from the site, so it decides the data plan. GB/day = Mbit/s × 10.8; TB per 30 days = Mbit/s × 0.324. Figures are for 5 cameras, before the few % SpeedFusion overhead.

| What is pulled continuously | Per camera | 5 cameras | GB/day | TB / 30 days |
|---|---|---|---|---|
| 4K main stream, H.265, busy scene | 6 Mbit/s | 30 Mbit/s | 324 | 9.7 |
| 4K main stream, H.265, typical | 4 Mbit/s | 20 Mbit/s | 216 | 6.5 |
| 1080p main stream, H.265 | 2 Mbit/s | 10 Mbit/s | 108 | 3.2 |
| Sub stream (D1/720p), 1 Mbit/s | 1 Mbit/s | 5 Mbit/s | 54 | 1.6 |
| Sub stream, 512 kbit/s | 0.5 Mbit/s | 2.5 Mbit/s | 27 | 0.8 |
| Sub continuous + main ~2 h/day of events (planned record-stream choice) | ~0.5 + 6 Mbit/s × 2 h | | ~54 | ~1.6 |

- ONVIF metadata and events add only tens of kbit/s per camera.
- Live view through the hub does not add cellular data: viewers watch the instance's copy of the stream, not the camera.
- Set the camera's own encoder (H.265, bitrate cap, I-frame interval, 10-15 fps) before blaming the plan: it is the biggest lever.
