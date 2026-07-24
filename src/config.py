"""Application configuration for the local engineering knowledge base."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_HOST: Final[str] = "127.0.0.1"
OFFICIAL_PORT: Final[int] = 8501


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

    app_title: str = "工程知识库 v0.2.0"
    app_version: str = "0.2.0"
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
def get_settings() -> Settings:
    """Return one cached, official-endpoint settings instance for the process."""

    try:
        settings = Settings()
    except ValidationError as exc:
        raise OfficialEndpointError(
            f"正式服务端点必须为 {OFFICIAL_HOST}:{OFFICIAL_PORT}；配置值无效。"
        ) from exc
    require_official_endpoint(settings.host, settings.port)
    return settings
