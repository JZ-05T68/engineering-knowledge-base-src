"""Transport-neutral Hosted readiness; never migrate, seed, or call AI."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from src.hosted.storage_validation import reject_links, require_regular_file, sidecars
from src.hosted_config import HostedSettings, validate_hosted_paths
from src.migrations import SCHEMA_VERSION
from src.runtime_profile import (
    RuntimeConfigurationError,
    RuntimeProfile,
    require_runtime_profile,
)


class ReadinessReason(StrEnum):
    """Closed public reason vocabulary; no configuration values or exceptions."""

    RUNTIME_INVALID = "runtime_invalid"
    DATABASE_UNAVAILABLE = "database_unavailable"
    SCHEMA_INCOMPATIBLE = "schema_incompatible"
    AI_NOT_CONFIGURED = "ai_not_configured"
    BUDGET_NOT_CONFIGURED = "budget_not_configured"
    COMPOSITION_UNAVAILABLE = "composition_unavailable"
    STORAGE_INVALID = "storage_invalid"


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    reasons: tuple[ReadinessReason, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reasons",
            tuple(dict.fromkeys(ReadinessReason(reason) for reason in self.reasons)),
        )

    @property
    def ready(self) -> bool:
        return not self.reasons


class ReadinessChecker(Protocol):
    def check(self) -> ReadinessResult:
        """Inspect readiness without network or database mutation."""
        ...


def check_hosted_database(path: Path) -> ReadinessReason | None:
    """Observe a quiescent DB without creating SQLite sidecars.

    Schema authority is schema_migrations, not PRAGMA user_version. No Database
    construction, initialization, backup, journal change, or content scan occurs.
    Existing WAL/SHM/journal means this fallback cannot safely observe the file:
    formal WP4 runtime uses its bootstrap-owned live observer connection instead.
    immutable=1 is used only when there is no WAL to ignore. No active runtime
    should use this fallback in place of HostedStorage.readiness_reason.
    """

    try:
        if not path.is_file():
            return ReadinessReason.DATABASE_UNAVAILABLE
        reject_links(path)
        require_regular_file(path)
        if any(item.exists() or item.is_symlink() for item in sidecars(path)):
            return ReadinessReason.DATABASE_UNAVAILABLE
        with closing(
            sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)
        ) as db:
            try:
                version = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            except sqlite3.Error:
                return ReadinessReason.SCHEMA_INCOMPATIBLE
            if type(version) is not int or version != SCHEMA_VERSION:
                return ReadinessReason.SCHEMA_INCOMPATIBLE
            # Existing KB identity is required, but this is not proof that the
            # operator's corpus is public/sanitized. Artifact validation is WP4.
            row = db.execute("SELECT kb_uuid FROM knowledge_base_meta WHERE id = 1").fetchone()
            if row is None:
                return ReadinessReason.DATABASE_UNAVAILABLE
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        return ReadinessReason.DATABASE_UNAVAILABLE
    return None


@dataclass(frozen=True, slots=True)
class HostedReadiness:
    """Compose observation-only path/DB checks with key and finite budget.

    Write probes belong to explicit startup. With formal demo identity settings,
    inject the WP4 storage observer; never silently use a quiescent DB fallback.
    """

    settings: HostedSettings
    database_check: Callable[[Path], ReadinessReason | None] = check_hosted_database

    def check(self) -> ReadinessResult:
        reasons: list[ReadinessReason] = []
        try:
            require_runtime_profile(RuntimeProfile.HOSTED)
            validate_hosted_paths(self.settings)
        except RuntimeConfigurationError:
            reasons.append(ReadinessReason.RUNTIME_INVALID)
        else:
            database_reason = (
                ReadinessReason.STORAGE_INVALID
                if self.settings.demo_kb_uuid is not None
                and self.database_check is check_hosted_database
                else self.database_check(self.settings.database_path)
            )
            if database_reason is not None:
                reasons.append(database_reason)
        if not self.settings.ai_api_key.get_secret_value().strip():
            reasons.append(ReadinessReason.AI_NOT_CONFIGURED)
        if not (
            self.settings.ai_daily_token_budget > 0 or self.settings.ai_monthly_token_budget > 0
        ):
            reasons.append(ReadinessReason.BUDGET_NOT_CONFIGURED)
        return ReadinessResult(tuple(reasons))
