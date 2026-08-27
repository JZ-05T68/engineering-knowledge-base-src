"""Explicit deployment profile authority, independent of UI and services."""

from __future__ import annotations

import os
from enum import StrEnum

RUNTIME_PROFILE_ENV_VAR = "EKB_RUNTIME_PROFILE"


class RuntimeProfile(StrEnum):
    """The only deployment profiles supported by ADR-007."""

    LOCAL = "local"
    HOSTED = "hosted"


class RuntimeConfigurationErrorCode(StrEnum):
    """Stable, transport-neutral configuration failure codes."""

    INVALID_RUNTIME_PROFILE = "invalid_runtime_profile"
    RUNTIME_PROFILE_MISMATCH = "runtime_profile_mismatch"
    MISSING_DATA_ROOT = "missing_data_root"
    INVALID_DATA_ROOT = "invalid_data_root"
    DATA_ROOT_NOT_USABLE = "data_root_not_usable"
    DATA_ROOT_IN_SOURCE_TREE = "data_root_in_source_tree"
    INVALID_HOSTED_CONFIG = "invalid_hosted_config"


class RuntimeConfigurationError(ValueError):
    """A safe configuration error containing no input values or settings dump."""

    def __init__(self, code: RuntimeConfigurationErrorCode) -> None:
        self.code = code
        super().__init__(f"运行配置校验失败：{code.value}")


def parse_runtime_profile(value: str | None) -> RuntimeProfile:
    """Default only absence to LOCAL; never trim, normalize, or guess a value."""

    if value is None:
        return RuntimeProfile.LOCAL
    if value == "local":
        return RuntimeProfile.LOCAL
    if value == "hosted":
        return RuntimeProfile.HOSTED
    raise RuntimeConfigurationError(RuntimeConfigurationErrorCode.INVALID_RUNTIME_PROFILE)


def get_runtime_profile() -> RuntimeProfile:
    """Read the process environment only, never a repository/CWD dotenv file."""

    return parse_runtime_profile(os.environ.get(RUNTIME_PROFILE_ENV_VAR))


def require_runtime_profile(expected: RuntimeProfile) -> None:
    """Reject the wrong entrypoint before loading that profile's configuration."""

    if get_runtime_profile() is not expected:
        raise RuntimeConfigurationError(RuntimeConfigurationErrorCode.RUNTIME_PROFILE_MISMATCH)
