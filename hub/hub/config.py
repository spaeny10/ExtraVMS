"""Hub settings (env prefix HUB_, .env next to this package)."""
from __future__ import annotations

import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

HERE = Path(__file__).resolve().parent          # hub/hub
ROOT = HERE.parents[1]                          # repo root


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=HERE.parent / ".env", env_prefix="HUB_", extra="ignore")

    database_url: str = f"sqlite:///{(HERE.parent / 'hub.db').as_posix()}"   # Postgres in production
    secret: str = ""                          # session signing; generated per process when empty (dev only)
    public_url: str = "https://hub.axiomvision.ai"
    site_ui_dir: Path = ROOT / "frontend" / "dist"   # the site UI bundle, served under /s/<site>/
    ui_dir: Path = HERE.parent / "ui" / "dist"        # the hub's own pages
    cookie_secure: bool = True
    session_days: int = 14
    offline_after_s: float = 90.0             # three missed heartbeats
    heartbeat_s: float = 30.0
    max_streams_per_site: int = 32
    first_byte_timeout_s: float = 30.0
    scan_timeout_s: float = 120.0        # POST /api/cameras/scan through the tunnel
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"
    # Who may set X-Forwarded-For / -Proto (the client address and https behind Caddy): comma-separated IPs or CIDRs.
    # Only 127.0.0.1 by default; docker-compose.yml sets the compose network's private ranges, where only Caddy can
    # reach the hub (port 8000 is exposed to the network, never published). Never "*": then anyone reaching the port
    # could forge their address (sign-in rate limit, audit IPs) and the scheme.
    forwarded_allow_ips: str = "127.0.0.1"

    # TURN relay for live video through the hub (coturn with use-auth-secret; see hub/coturn/turnserver.conf)
    turn_host: str = ""                       # e.g. hub.axiomvision.ai; empty = no relay (LAN viewing only)
    turn_secret: str = ""
    turn_port: int = 3478
    turn_tls_port: int = 0                    # 5349 once coturn has a certificate; 0 = not advertised
    turn_user_ttl_s: int = 3600
    turn_site_ttl_s: int = 86400              # a site's credential (MediaMTX relays with it); refreshed over the tunnel
    turn_site_refresh_s: int = 8 * 3600       # ... once it has less than this left (checked on every heartbeat)

    # Direct-on-LAN: lifetime of the media token a browser presents straight to a server on its LAN (hub/direct.py)
    direct_token_ttl_s: int = 900

    # Shared AI: an OpenAI-compatible vLLM the hub fronts for every site whose organization has ai_shared
    vllm_url: str = ""                        # e.g. http://vllm:8000/v1 (a vLLM/Ollama the hub can reach directly)
    vllm_site: str = ""                       # or: the site id whose local Qwen serves the fleet, reached down its tunnel
    vllm_key: str = ""
    vllm_model: str = ""                      # e.g. Qwen/Qwen2.5-VL-32B-Instruct-AWQ
    vllm_per_site: int = 2
    vllm_per_org: int = 8

    digest_hour: int = 7                      # org digest at HH:30 hub-local
    backup_hour: int = 3                      # nightly site config backups
    push_contact: str = ""                    # mailto for VAPID claims (defaults to admin@<public host>)

    # SOC escalation (soc.escalate_once) and reports (soc_reports.py)
    soc_escalate_step_s: int = 120            # unclaimed past SLA: level 1, then one level per step (2 supervisors, 3 customer contact)
    soc_quiet_ttl_s: int = 86400              # an unclaimed quiet-lane incident with no new event for this long closes as `expired`
    soc_feedback_retry_s: int = 300           # how often failed false-alarm feedback is retried once its server is back online
    soc_shift_ends: str = "06:00,14:00,22:00" # hub-local shift boundaries: a shift report is stored at each one
    soc_loops: bool = True                    # run the escalation and report loops (tests drive escalate_once themselves)

    # Site addresses (geocode.py): a Nominatim-compatible geocoder, and the map tiles the hub UI draws Sites on
    geocoder_url: str = ""                    # empty = https://nominatim.openstreetmap.org (1 request/s, cached 30 days)
    geocoder_key: str = ""                    # sent as key= for hosted Nominatim-compatible services that want one
    census_url: str = ""                      # US Census Bureau geocoder; empty = the public one, "off" = never asked
    geocoder_country: str = "US"              # where the Sites are: US = every street address asks Census first
    geocode_backfill: bool = True             # at start, locate Sites that have an address but no coordinates
    map_tiles: str = ""                       # Leaflet URL template; empty = MAP_TILES below (OpenStreetMap)
    map_attribution: str = ""                 # HTML; empty = OpenStreetMap's (required with its tiles)


    # Central recording (hosts.py): datacenter hosts running axiom-host, one server instance per customer Site
    central_vlm_url: str = "http://vllm:8000/v1"   # the shared vLLM every central instance uses (handed to the host)
    datacenter_ip: str = ""                   # the datacenter's public IP: the only allowed source of port-forward rules
    fusionhub_address: str = ""               # SpeedFusion peer for VPN-mode Sites when the host has none of its own
    central_gpu_prefer: str = "A10"           # an instance gets the host GPU whose name has this model (least used first) ...
    central_gpu_avoid: str = "A40"            # ... else the least used GPU that isn't this one (the A40 runs vLLM)
    central_enroll_ttl_s: int = 86400         # how long an instance's single-use enrollment token stays valid
    host_create_timeout_s: float = 600.0      # create_instance may pull images and set up quotas: minutes
    host_command_timeout_s: float = 120.0     # every other host command

    # Cellular coverage per Site (coverage.py): the CoverageMap API (FCC coverage, crowdsourced speed tests, summary scores)
    coveragemap_key: str = ""                 # empty = the feature is off everywhere (stored data is kept: `python -m hub coverage purge`)
    coveragemap_plan: str = "trial"           # trial = evaluation only (hub administrators see it) | paid = customers see their Sites'
    coveragemap_url: str = "https://enterprise.coveragemap.com/api/v1"
    coveragemap_refresh_days: int = 30        # stored data older than this is refetched (also: the Site moved > 100 m)
    coveragemap_monthly_units: int = 400      # budget cap per calendar month (UTC); 0 = no cap
    coveragemap_datasets: str = "summary,fcc-coverage,speed-tests"

settings = Settings()
if not settings.secret:
    settings.secret = secrets.token_urlsafe(32)
