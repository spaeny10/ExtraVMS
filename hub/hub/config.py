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
    public_url: str = "https://cloudvms.bigview.ai"
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
    turn_host: str = ""                       # e.g. cloudvms.bigview.ai; empty = no relay (LAN viewing only)
    turn_secret: str = ""
    turn_port: int = 3478
    turn_tls_port: int = 5349
    turn_user_ttl_s: int = 3600
    turn_site_ttl_s: int = 30 * 86400

    # Shared AI: an OpenAI-compatible vLLM the hub fronts for every site whose organisation has ai_shared
    vllm_url: str = ""                        # e.g. http://vllm:8000/v1
    vllm_key: str = ""
    vllm_model: str = ""                      # e.g. Qwen/Qwen2.5-VL-32B-Instruct-AWQ
    vllm_per_site: int = 2
    vllm_per_org: int = 8


settings = Settings()
if not settings.secret:
    settings.secret = secrets.token_urlsafe(32)
