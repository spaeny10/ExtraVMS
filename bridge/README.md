# Axiom Bridge

A small program for a PC at a camera site that has **no inbound access** (Starlink CGNAT, cellular, no static IP).
It dials the Axiom Vision hub over **outbound TCP 443** and carries the hub's connections to the cameras the hub
assigned to it (RTSP 554, ONVIF/web 80, Milesight SD replay 555). It never listens on any port, so nothing on the
site's network has to be opened or forwarded. Design: [docs/axiom-bridge-plan.md](../docs/axiom-bridge-plan.md).

The bridge only connects where **both** the hub and the PC agree: the address must be assigned by the hub *and* be a
private (RFC 1918) IP address that is not this PC, not loopback, link-local, multicast or broadcast, and not a DNS
name. An owner can narrow it further with `--allow`.

## Requirements

- Windows 10/11 (x64), including IoT Enterprise; or Linux x64/arm64.
- Outbound HTTPS (TCP 443) to `hub.axiomvision.ai`. No inbound ports.
- The cameras on **static IPs** (see below), reachable from the PC.
- The PC **never sleeps** (see below).

## Install (Windows)

1. On the hub, create a **Bridge enrollment code** for the Site (hub admins and the Customer's Site admins can).
   The code (`AXB-XXXX-XXXX-XXXX`) is good for 24 hours and for one bridge.
2. Copy `axiom-bridge.exe` to the PC (for example to `Downloads`).
3. Open PowerShell **as administrator** (right-click → *Run as administrator*) and run, with your code:

   ```powershell
   & "$env:USERPROFILE\Downloads\axiom-bridge.exe" install --code AXB-XXXX-XXXX-XXXX
   ```

   Options: `--allow 192.168.1.50` (only ever reach this camera; repeat or comma-separate for more, prefixes like
   `192.168.1.0/24` work too), `--hub wss://…/bridge` (another hub), `--ca-file hub-ca.pem` (a hub with a private CA).

The installer copies itself to `C:\Program Files\Axiom Bridge\`, writes `C:\ProgramData\Axiom Bridge\config.json`,
enrolls with the hub and prints the bridge's name and Site:

```
Enrolled: bridge br_8f2c…
Bridge: Gate PC   Site: North Ranch   (id br_8f2c…)
Service AxiomBridge is running (automatic start, restarts on failure).
```

It then registers the Windows service **AxiomBridge** (automatic, delayed start, runs as LocalSystem, restarts
after 5 s / 30 s / 60 s on failure) and starts it. If enrollment fails (code mistyped, used or expired, no internet)
it says so and does **not** install the service; make a new code if needed and run the command again.

**Upgrade:** run `install` again with the new exe, without `--code`; the enrollment is kept.

## Status and logs

```powershell
& "C:\Program Files\Axiom Bridge\axiom-bridge.exe" status
```

shows the service state and the last connection: connected since, bridge and Site, open streams, traffic, the last
error and any camera that refused a connection (`bt_…:554: connection refused`). Its exit code is 0 when connected.
`status` needs no elevation.

- Log: `C:\ProgramData\Axiom Bridge\logs\bridge.log` (rotated at 5 MB, 3 files kept). It never contains the token,
  the enrollment code or any camera data. Set `"log_level": "debug"` in `config.json` and restart the service for
  per-stream detail.
- Windows Event Log → *Application*, source **AxiomBridge**: service start/stop and unexpected exits.
- The hub shows the bridge online/offline, its streams and camera errors; `bridge_offline` alerts go to hub admins
  and the Customer.

`Restart-Service AxiomBridge` (elevated) restarts it; a stop finishes within 5 seconds.

## Uninstall

```powershell
& "C:\Program Files\Axiom Bridge\axiom-bridge.exe" uninstall          # keeps config, token and logs
& "C:\Program Files\Axiom Bridge\axiom-bridge.exe" uninstall --purge  # deletes them too
```

When run from the installed exe, the program file is deleted at the next reboot. Also **retire the bridge on the
hub** so its token stops working.

## Linux

```sh
sudo ./axiom-bridge-linux-amd64 run --code AXB-XXXX-XXXX-XXXX   # enrolls, prints name/Site, keeps running; Ctrl-C
sudo install -m 0755 axiom-bridge-linux-amd64 /usr/local/bin/axiom-bridge
```

Config and token live in `/etc/axiom-bridge/` (token `0600`), status and logs in `/var/lib/axiom-bridge/`.
A systemd unit:

```ini
# /etc/systemd/system/axiom-bridge.service
[Unit]
Description=Axiom Bridge
Wants=network-online.target
After=network-online.target

[Service]
ExecStart=/usr/local/bin/axiom-bridge run
Restart=always
RestartSec=5
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
```

`sudo systemctl enable --now axiom-bridge`, then `axiom-bridge status`.

## Site checklist

- **Cameras on static IPs.** The hub reaches a camera by the address it was mapped with. Starlink's router hands out
  DHCP addresses that can change after a power cut, so give each camera a static address outside the router's DHCP
  range (set on the camera itself, or a DHCP reservation if the router offers one), e.g. `192.168.1.50`, and map
  that address on the hub.
- **The PC never sleeps.** Settings → System → Power: *Sleep: Never* (on battery and plugged in), and in the
  network adapter's Power Management tab untick *Allow the computer to turn off this device to save power*. In the
  BIOS, enable *power on after AC loss* so it comes back after an outage. Elevated equivalent:

  ```powershell
  powercfg /change standby-timeout-ac 0; powercfg /change hibernate-timeout-ac 0; powercfg /change monitor-timeout-ac 0
  ```
- Wired Ethernet for the PC and cameras where possible; Wi-Fi works but adds loss.
- Windows Update may reboot the PC; the service starts by itself (delayed start, a minute or two after boot).

## Troubleshooting

- **"Windows protected your PC" (SmartScreen)** when starting the exe: the bridge is not code-signed yet. Click
  *More info* → *Run anyway*. If PowerShell refuses to run a downloaded file, unblock it first:

  ```powershell
  Unblock-File "$env:USERPROFILE\Downloads\axiom-bridge.exe"
  ```
  Check the file against `SHA256SUMS.txt` from the release: `Get-FileHash axiom-bridge.exe`.
- **"this needs an elevated prompt"**: open PowerShell with *Run as administrator*.
- **"the hub refused the enrollment code"**: the code was mistyped, already used or older than 24 h. Create a new one.
- **status shows `waiting` with `HTTP 401`/`403`**: the hub no longer accepts this bridge's token (retired, or a
  restored backup). The bridge retries every 5 minutes; reinstall with a new code to fix it.
- **status shows `waiting` with a network or certificate error**: the PC cannot reach `hub.axiomvision.ai:443`.
  Check internet access, the PC's date and time (TLS fails with a wrong clock), and any proxy or firewall that
  inspects HTTPS. The bridge retries with backoff up to once a minute.
- **status shows `revoked` / `not_enrolled`**: the bridge was retired on the hub; reinstall with a new code.
- **Camera errors (`connection refused`, `timeout`) in status or on the hub**: the camera's IP changed (see static
  IPs), it is off, or the port is wrong. From the PC: `Test-NetConnection 192.168.1.50 -Port 554`.
- **`denied: … is not a private (RFC 1918) address`**: the mapped address is public or CGNAT (100.64/10). Fix the
  mapping, or set `"allow_public": true` in `config.json` if that is really where the camera is.
- **`denied: … not in this bridge's allow list`**: the owner restricted the bridge with `--allow`; add the camera
  with `install --allow …` (run without `--code` to keep the enrollment) or edit `allow` in `config.json`.

## Files

| Windows | Linux | What |
|---|---|---|
| `C:\Program Files\Axiom Bridge\axiom-bridge.exe` | `/usr/local/bin/axiom-bridge` | the program |
| `C:\ProgramData\Axiom Bridge\config.json` | `/etc/axiom-bridge/config.json` | `hub_url`, `allow`, `allow_public`, `ca_file`, `log_level` |
| `C:\ProgramData\Axiom Bridge\token` | `/etc/axiom-bridge/token` | device token (SYSTEM + Administrators only / `0600`) |
| `C:\ProgramData\Axiom Bridge\status.json` | `/var/lib/axiom-bridge/status.json` | what `status` shows |
| `C:\ProgramData\Axiom Bridge\logs\` | `/var/lib/axiom-bridge/logs/` | `bridge.log`, `.1`, `.2` |

`AXIOM_BRIDGE_DIR` (or `run --dir`) keeps all of them in one directory instead.

## Building

```powershell
cd bridge
.\build.ps1 -Test        # go vet + go test, then dist\axiom-bridge.exe, -linux-amd64, -linux-arm64, SHA256SUMS.txt
```

Go 1.26, no cgo; dependencies `github.com/coder/websocket` and `golang.org/x/sys`. Tests use an in-process fake hub
(`internal/fakehub`) and never contact a real hub. `go test -race` needs cgo (a C compiler) on Windows; run it on
Linux or with a toolchain installed. The golden wire vectors in `internal/proto/testdata/golden.json` come from the
Python `tunnelproto` (`testdata/gen_golden.py`; rerun it from the repo root if the protocol changes).
