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
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"

    # TURN relay for live video through the hub (coturn with use-auth-secret; see hub/coturn/turnserver.conf)
    turn_host: str = ""                       # e.g. hub.axiomvision.ai; empty = no relay (LAN viewing only)
    turn_secret: str = ""
    turn_port: int = 3478
    turn_tls_port: int = 0                    # 5349 once coturn has a certificate; 0 = not advertised
    turn_user_ttl_s: int = 3600
    turn_site_ttl_s: int = 30 * 86400

    # Direct-on-LAN: lifetime of the media token a browser presents straight to a server on its LAN (hub/direct.py)
    direct_token_ttl_s: int = 900

    # Shared AI: an OpenAI-compatible vLLM the hub fronts for every site whose organisation has ai_shared
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


settings = Settings()
if not settings.secret:
    settings.secret = secrets.token_urlsafe(32)
