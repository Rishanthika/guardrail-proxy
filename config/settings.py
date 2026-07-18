"""
Application-wide configuration.

Values here can be overridden by environment variables (or a .env file) so
the same codebase behaves correctly across local dev, CI, and cloud
deployment (e.g. ECS/App Runner env vars) without code changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    app_name: str = "Action Guardrail Proxy"
    environment: str = "development"

    # Path to the declarative policy file consumed by the PolicyEngine.
    policy_path: Path = BASE_DIR / "config" / "policy.yaml"

    # Async SQLAlchemy connection string. Defaults to a local SQLite file so
    # the service runs with zero external dependencies out of the box; swap
    # for a real Postgres DSN in production (e.g. via env var DATABASE_URL).
    database_url: str = f"sqlite+aiosqlite:///{BASE_DIR / 'guardrail.db'}"

    # If set (True/False), overrides the `dry_run` flag from policy.yaml.
    # Leave unset (None) to let policy.yaml be the source of truth.
    dry_run_override: Optional[bool] = None

    log_level: str = "INFO"

    # How long (seconds) a HITL request may sit PENDING before it is
    # considered stale by /v1/hitl/pending reporting (informational only —
    # does not auto-reject).
    hitl_stale_after_seconds: int = 3600


settings = Settings()
