"""Env-only Hosted configuration and low-side-effect WP1 startup validation.

This module does not compose services, open SQLite, seed data, configure logging,
or check AI readiness. Those responsibilities belong to later work packages.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
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
    # Same token units as Local. Zero disables one period; readiness rejects
    # both periods being unlimited. No budget accounting is implemented here.
    ai_daily_token_budget: int = Field(default=0, ge=0)
    ai_monthly_token_budget: int = Field(default=0, ge=0)

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


def validate_hosted_startup(settings: HostedSettings) -> None:
    """Validate WP1 paths without mkdir, DB access, AI, or other network I/O.

    The configured root must already exist. Existing database/log directories
    must be usable; absent children are left for WP4. A unique create/write/close
    probe is immediately removed in each existing directory. No persistent file
    is opened or overwritten. This is not a complete readiness check or sandbox.
    """

    if settings.runtime_profile is not RuntimeProfile.HOSTED:
        raise RuntimeConfigurationError(RuntimeConfigurationErrorCode.RUNTIME_PROFILE_MISMATCH)
    try:
        root = settings.data_root
        if not root.is_dir() or root.resolve() != root:
            raise RuntimeConfigurationError(RuntimeConfigurationErrorCode.DATA_ROOT_NOT_USABLE)
        for path in (
            settings.database_dir, settings.database_path, settings.logs_dir, settings.log_path
        ):
            resolved = path.resolve()
            if not resolved.is_relative_to(root) or resolved.is_relative_to(PROJECT_ROOT.resolve()):
                raise RuntimeConfigurationError(RuntimeConfigurationErrorCode.DATA_ROOT_NOT_USABLE)
        for directory in (root, settings.database_dir, settings.logs_dir):
            if directory != root and not directory.exists():
                continue
            if not directory.is_dir():
                raise RuntimeConfigurationError(RuntimeConfigurationErrorCode.DATA_ROOT_NOT_USABLE)
            with NamedTemporaryFile(prefix=".ekb-wp1-", dir=directory) as probe:
                probe.write(b"ekb")
                probe.flush()
    except (OSError, ValueError, RuntimeError):
        raise RuntimeConfigurationError(
            RuntimeConfigurationErrorCode.DATA_ROOT_NOT_USABLE
        ) from None
