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


settings = Settings()
if not settings.secret:
    settings.secret = secrets.token_urlsafe(32)
