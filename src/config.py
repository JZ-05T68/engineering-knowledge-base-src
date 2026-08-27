"""Application configuration for the local engineering knowledge base."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.runtime_profile import RuntimeProfile, require_runtime_profile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_HOST: Final[str] = "127.0.0.1"
OFFICIAL_PORT: Final[int] = 8501
STAGING_PORT: Final[int] = 8502
DEFAULT_STAGING_ROOT: Final[Path] = PROJECT_ROOT / "staging-data"
#: Environment flag selecting the staging instance inside an app process.
STAGING_ENV_VAR: Final[str] = "EKB_STAGING_INSTANCE"


class OfficialEndpointError(ValueError):
    """Raised when a formal runtime attempts to use a non-official endpoint."""


def require_official_endpoint(host: str, port: int) -> None:
    """Require the one supported endpoint for formal local runtime paths.

    ``Settings`` remains directly constructible with a temporary port so tests
    can isolate their listeners.  Formal entry points use ``get_settings()``,
    which calls this guard before returning configuration.
    """

    if host != OFFICIAL_HOST:
        raise OfficialEndpointError(
            f"正式服务端点必须为 {OFFICIAL_HOST}:{OFFICIAL_PORT}；收到地址 {host}。"
        )
    if port != OFFICIAL_PORT:
        raise OfficialEndpointError(
            f"正式服务端点必须为 {OFFICIAL_HOST}:{OFFICIAL_PORT}；收到端口 {port}。"
        )


class Settings(BaseSettings):
    """Settings loaded from environment variables and an optional local ``.env``.

    Every default points to the local project directory.  In particular, the
    application listens only on the loopback interface and does not need an API
    key in manual AI mode.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="EKB_",
        extra="ignore",
    )

    app_title: str = "工程知识库 v0.5.3"
    app_version: str = "0.5.3"
    host: Literal["127.0.0.1"] = OFFICIAL_HOST
    port: int = Field(default=OFFICIAL_PORT, ge=1, le=65535)

    data_dir: Path = PROJECT_ROOT / "data"
    raw_dir: Path = PROJECT_ROOT / "data" / "raw"
    pages_dir: Path = PROJECT_ROOT / "data" / "pages"
    markdown_dir: Path = PROJECT_ROOT / "data" / "markdown"
    database_dir: Path = PROJECT_ROOT / "data" / "database"
    database_path: Path = PROJECT_ROOT / "data" / "database" / "knowledge.db"
    backups_dir: Path = PROJECT_ROOT / "backups"
    logs_dir: Path = PROJECT_ROOT / "logs"
    log_path: Path = PROJECT_ROOT / "logs" / "engineering-kb.log"
    runtime_dir: Path = PROJECT_ROOT / "runtime"
    pid_path: Path = PROJECT_ROOT / "runtime" / "engineering-kb.pid.json"

    minimum_text_length: int = Field(default=20, ge=0)
    pdf_render_dpi: int = Field(default=150, ge=72, le=600)

    # Optional AI layer (v0.5.0). Manual by default: without an API key the
    # application starts and every existing offline feature keeps working.
    ai_mode: Literal["manual", "api"] = "manual"
    ai_provider: Literal["qwen"] = "qwen"
    ai_api_key: SecretStr = SecretStr("")
    ai_llm_model: str = "qwen3.7-plus"
    ai_llm_model_hard: str = "qwen3.8-max"
    ai_embedding_model: str = "qwen3.7-text-embedding"
    ai_rerank_model: str = "qwen3-rerank"
    ai_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    # Bounded retry and token budgets (v0.5.3). Budget unit is tokens, never
    # currency; 0 means unlimited. Retry ceiling is capped at 2 extra attempts.
    ai_max_extra_attempts: int = Field(default=2, ge=0, le=2)
    ai_daily_token_budget: int = Field(default=0, ge=0)
    ai_monthly_token_budget: int = Field(default=0, ge=0)

    @property
    def runtime_profile(self) -> RuntimeProfile:
        """Local configuration remains distinct from Hosted server settings."""

        return RuntimeProfile.LOCAL

    def ensure_directories(self) -> None:
        """Create all writable local directories without removing existing data."""

        for directory in (
            self.data_dir,
            self.raw_dir,
            self.pages_dir,
            self.markdown_dir,
            self.database_dir,
            self.backups_dir,
            self.logs_dir,
            self.runtime_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def _get_local_settings() -> Settings:
    """Cache the existing Local configuration after the entrypoint guard."""

    try:
        settings = Settings()
    except ValidationError as exc:
        raise OfficialEndpointError(
            f"正式服务端点必须为 {OFFICIAL_HOST}:{OFFICIAL_PORT}；配置值无效。"
        ) from exc
    require_official_endpoint(settings.host, settings.port)
    return settings


def get_settings() -> Settings:
    """Guard the Local entrypoint even on cache hits, preserving Local defaults."""

    require_runtime_profile(RuntimeProfile.LOCAL)
    return _get_local_settings()


# Preserve the existing public cache reset hook used by developer/test workflows.
get_settings.cache_clear = _get_local_settings.cache_clear


def staging_settings(root: Path | None = None) -> Settings:
    """Build fully isolated AI-staging settings under one separate root.

    Every writable path (data / raw / pages / markdown / database / backups /
    logs / runtime) is derived under ``root`` — never the production
    locations — and the staging endpoint is loopback ``STAGING_PORT`` (8502).
    Explicit constructor arguments outrank any ``EKB_*`` value in ``.env``,
    so a stray path override in the environment cannot break isolation; the
    AI credentials and model settings still resolve from ``.env`` as usual.

    The formal-runtime guard (``require_official_endpoint``) deliberately
    does not apply: staging is a non-formal, explicitly separate instance,
    and direct ``Settings`` construction is the existing sanctioned
    extension point. Staging data can be deleted or rebuilt freely without
    affecting production.
    """

    require_runtime_profile(RuntimeProfile.LOCAL)
    staging_root = Path(root) if root is not None else DEFAULT_STAGING_ROOT
    data_dir = staging_root / "data"
    database_dir = data_dir / "database"
    logs_dir = staging_root / "logs"
    runtime_dir = staging_root / "runtime"
    return Settings(
        port=STAGING_PORT,
        data_dir=data_dir,
        raw_dir=data_dir / "raw",
        pages_dir=data_dir / "pages",
        markdown_dir=data_dir / "markdown",
        database_dir=database_dir,
        database_path=database_dir / "knowledge.db",
        backups_dir=staging_root / "backups",
        logs_dir=logs_dir,
        log_path=logs_dir / "engineering-kb-staging.log",
        runtime_dir=runtime_dir,
        pid_path=runtime_dir / "engineering-kb-staging.pid.json",
    )


def runtime_settings() -> Settings:
    """Resolve the settings for one application process.

    A process launched with ``STAGING_ENV_VAR=1`` (the staging service
    manager path) runs entirely on :func:`staging_settings`; every other
    process gets the guarded formal settings exactly as before. Production
    behavior is unchanged when the flag is absent.
    """

    require_runtime_profile(RuntimeProfile.LOCAL)
    if os.environ.get(STAGING_ENV_VAR) == "1":
        return staging_settings()
    return get_settings()
