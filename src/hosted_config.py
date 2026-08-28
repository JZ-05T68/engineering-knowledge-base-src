"""Env-only Hosted configuration and low-side-effect WP1 startup validation.

This module does not compose services, open SQLite, seed data, configure logging,
or check AI readiness. Those responsibilities belong to later work packages.
"""

from __future__ import annotations

import os
import re
from ipaddress import ip_address, ip_network
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    SettingsError,
)

from src.config import PROJECT_ROOT
from src.runtime_profile import (
    RuntimeConfigurationError,
    RuntimeConfigurationErrorCode,
    RuntimeProfile,
    parse_runtime_profile,
    require_runtime_profile,
)


class HostedSettings(BaseSettings):
    """Immutable server configuration from explicit injection or process env.

    Unlike Local Settings, this type has no private asset/storage defaults.
    Even an explicit ``_env_file`` argument cannot enable dotenv loading.
    Direct construction supports synthetic/injected configuration; the formal
    loader additionally requires an explicit Hosted process profile.
    """

    model_config = SettingsConfigDict(
        env_prefix="EKB_",
        env_file=None,
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    runtime_profile: Literal[RuntimeProfile.HOSTED]
    data_root: Path
    ai_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    # Server authority only. Match Local model metadata without loading Local settings.
    ai_llm_model: str = "qwen3.7-plus"
    ai_llm_model_hard: str = "qwen3.8-max"
    ai_embedding_model: str = "qwen3.7-text-embedding"
    ai_rerank_model: str = "qwen3-rerank"
    ai_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    ai_max_extra_attempts: int = Field(default=0, ge=0, le=2)
    hosted_port: int = Field(default=8000, ge=1, le=65535)
    # Same token units as Local. Zero disables one period; readiness rejects
    # both periods being unlimited. No budget accounting is implemented here.
    ai_daily_token_budget: int = Field(default=0, ge=0)
    ai_monthly_token_budget: int = Field(default=0, ge=0)
    agent_rate_limit_per_minute: int = Field(default=10, ge=1)
    source_rate_limit_per_minute: int = Field(default=60, ge=1)
    max_active_agent_runs: int = Field(default=4, ge=1, le=8)
    cors_allowed_origins: Annotated[tuple[str, ...], NoDecode] = ()
    trusted_proxy_cidrs: Annotated[tuple[str, ...], NoDecode] = ()
    # Optional for transport-only DI. Explicit storage bootstrap requires identity.
    demo_db_artifact: Path | None = Field(default=None, repr=False)
    demo_db_sha256: str | None = Field(default=None, repr=False)
    demo_kb_uuid: str | None = None

    def __init__(self, **values: Any) -> None:
        # Force this before BaseSettings instantiates its sources: excluding a
        # dotenv source alone does not prevent its constructor reading a file.
        values["_env_file"] = None
        values["_secrets_dir"] = None
        try:
            super().__init__(**values)
        except ValidationError as exc:
            code = RuntimeConfigurationErrorCode.INVALID_HOSTED_CONFIG
            for error in exc.errors(include_input=False, include_url=False):
                cause = error.get("ctx", {}).get("error")
                if isinstance(cause, RuntimeConfigurationError):
                    code = cause.code
                    break
                if error["loc"] == ("runtime_profile",):
                    code = RuntimeConfigurationErrorCode.RUNTIME_PROFILE_MISMATCH
                    break
                if error["loc"] == ("data_root",):
                    code = (
                        RuntimeConfigurationErrorCode.MISSING_DATA_ROOT
                        if error["type"] == "missing"
                        else RuntimeConfigurationErrorCode.INVALID_DATA_ROOT
                    )
                    break
            raise RuntimeConfigurationError(code) from None
        except SettingsError:
            raise RuntimeConfigurationError(
                RuntimeConfigurationErrorCode.INVALID_HOSTED_CONFIG
            ) from None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Explicit server injection outranks env; no dotenv/file-secret source."""

        return init_settings, env_settings

    @field_validator("runtime_profile", mode="before")
    @classmethod
    def _hosted_only(cls, value: str | None) -> RuntimeProfile:
        profile = parse_runtime_profile(value)
        if profile is not RuntimeProfile.HOSTED:
            raise RuntimeConfigurationError(RuntimeConfigurationErrorCode.RUNTIME_PROFILE_MISMATCH)
        return profile

    @field_validator("demo_db_artifact", mode="before")
    @classmethod
    def _artifact_path(cls, value: object) -> Path | None:
        if value is None:
            return None
        if not isinstance(value, (str, Path)) or not str(value).strip() or "\0" in str(value):
            raise ValueError("Invalid demo artifact path")
        # Keep symlink spelling for the storage validator; never resolve it away.
        return Path(value).absolute()

    @field_validator("demo_db_sha256", mode="before")
    @classmethod
    def _artifact_digest(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError("Demo digest must be a complete SHA-256")
        return value.lower()

    @field_validator("demo_kb_uuid", mode="before")
    @classmethod
    def _artifact_uuid(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("Demo identity must be a canonical UUID")
        return value

    @field_validator(
        "agent_rate_limit_per_minute",
        "source_rate_limit_per_minute",
        "max_active_agent_runs",
        "ai_max_extra_attempts",
        "hosted_port",
        mode="before",
    )
    @classmethod
    def _security_integer(cls, value: object) -> int:
        if type(value) is int:
            return value
        if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
            return int(value)
        raise ValueError("Security limits must be positive integers")

    @field_validator("cors_allowed_origins", "trusted_proxy_cidrs", mode="before")
    @classmethod
    def _security_list(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str):
            if not value.strip():
                return ()
            value = tuple(item.strip() for item in value.split(","))
        if not isinstance(value, (tuple, list)) or any(
            not isinstance(item, str) or not item for item in value
        ):
            raise ValueError("Security allowlist must contain explicit entries")
        return tuple(dict.fromkeys(value))

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def _proxy_networks(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any("%" in value for value in values):
            raise ValueError("Scoped proxy networks are not supported")
        return tuple(dict.fromkeys(str(ip_network(value)) for value in values))

    @field_validator("cors_allowed_origins")
    @classmethod
    def _explicit_origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if any(ord(char) <= 32 or ord(char) >= 127 for char in value) or any(
                marker in value for marker in ("*", "?", "#", "\\", "%")
            ):
                raise ValueError("CORS requires explicit HTTP origins")
            parsed = urlsplit(value)
            host = parsed.hostname
            if (
                parsed.scheme not in {"http", "https"}
                or not host
                or parsed.path
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("CORS requires an origin without credentials or path")
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError("Invalid origin port")
            if parsed.netloc.endswith(":"):
                raise ValueError("Invalid origin port")
            try:
                address = ip_address(host)
            except ValueError:
                if len(host) > 253 or not all(
                    re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                    for label in host.split(".")
                ):
                    raise ValueError("Invalid origin hostname") from None
                loopback = host == "localhost"
            else:
                loopback = address.is_loopback
            if parsed.scheme == "http" and not loopback:
                raise ValueError("HTTP origins are restricted to explicit loopback development")
        return values

    @field_validator("data_root", mode="before")
    @classmethod
    def _resolve_data_root(cls, value: object) -> Path:
        if not isinstance(value, (str, Path)) or (
            isinstance(value, str) and (not value or value.isspace() or "\0" in value)
        ):
            raise RuntimeConfigurationError(RuntimeConfigurationErrorCode.INVALID_DATA_ROOT)
        try:
            root = Path(value).resolve()
            source_root = PROJECT_ROOT.resolve()
        except (OSError, ValueError, RuntimeError):
            raise RuntimeConfigurationError(
                RuntimeConfigurationErrorCode.INVALID_DATA_ROOT
            ) from None
        if root.is_relative_to(source_root):
            raise RuntimeConfigurationError(RuntimeConfigurationErrorCode.DATA_ROOT_IN_SOURCE_TREE)
        return root

    @property
    def database_dir(self) -> Path:
        return self.data_root / "database"

    @property
    def database_path(self) -> Path:
        return self.database_dir / "knowledge.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_root / "logs"

    @property
    def log_path(self) -> Path:
        return self.logs_dir / "engineering-kb.log"


def load_hosted_settings() -> HostedSettings:
    """Require explicit Hosted opt-in before reading any Hosted configuration."""

    require_runtime_profile(RuntimeProfile.HOSTED)
    return HostedSettings()


def validate_hosted_paths(settings: HostedSettings) -> None:
    """Observe WP1 containment/type policy without writing a readiness probe."""
    if settings.runtime_profile is not RuntimeProfile.HOSTED:
        raise RuntimeConfigurationError(RuntimeConfigurationErrorCode.RUNTIME_PROFILE_MISMATCH)
    try:
        root = settings.data_root
        if not root.is_dir() or root.resolve() != root:
            raise RuntimeConfigurationError(RuntimeConfigurationErrorCode.DATA_ROOT_NOT_USABLE)
        for path in (
            settings.database_dir,
            settings.database_path,
            settings.logs_dir,
            settings.log_path,
        ):
            resolved = path.resolve()
            if not resolved.is_relative_to(root) or resolved.is_relative_to(PROJECT_ROOT.resolve()):
                raise RuntimeConfigurationError(RuntimeConfigurationErrorCode.DATA_ROOT_NOT_USABLE)
        for directory in (root, settings.database_dir, settings.logs_dir):
            if directory != root and not directory.exists():
                continue
            if not directory.is_dir():
                raise RuntimeConfigurationError(RuntimeConfigurationErrorCode.DATA_ROOT_NOT_USABLE)
            if not os.access(directory, os.R_OK | os.W_OK | os.X_OK):
                raise RuntimeConfigurationError(RuntimeConfigurationErrorCode.DATA_ROOT_NOT_USABLE)
    except (OSError, ValueError, RuntimeError):
        raise RuntimeConfigurationError(
            RuntimeConfigurationErrorCode.DATA_ROOT_NOT_USABLE
        ) from None


def validate_hosted_startup(settings: HostedSettings) -> None:
    """Explicit WP1 startup write probes; never called by HTTP readiness.

    Root must exist; absent children remain absent. WP4 creates only database/logs.
    No DB connection, migration or persistent file write occurs here.
    """
    validate_hosted_paths(settings)
    try:
        for directory in (settings.data_root, settings.database_dir, settings.logs_dir):
            if not directory.exists():
                continue
            with NamedTemporaryFile(prefix=".ekb-wp1-", dir=directory) as probe:
                probe.write(b"ekb")
                probe.flush()
    except (OSError, ValueError, RuntimeError):
        raise RuntimeConfigurationError(
            RuntimeConfigurationErrorCode.DATA_ROOT_NOT_USABLE
        ) from None
