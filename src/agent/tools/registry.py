"""Tool Registry and Phase 1 read-only execution policy (v0.6.0 Phase 1A).

The registry's job is intentionally narrow:

- know which ToolDefinitions exist;
- find a definition by stable name;
- reject duplicate registration (fail-closed);
- reject unknown tools with a typed error;
- validate a definition against the active execution policy when resolved.

It is NOT a runtime container: it holds no model client, no database
connection, no Streamlit state, no Agent planner, and no retry loop.

Policy note (ADR-006 decision 1): Phase 1 allows only ``READ_ONLY`` tools.
Write-side definitions may be registered for future extension / tests, but
``ToolRegistry.resolve`` rejects them through :class:`Phase1ReadOnlyPolicy`
so they are unreachable in Phase 1 execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from src.agent.tools.contracts import (
    ToolDefinition,
    ToolSideEffect,
)


class ToolRegistryError(RuntimeError):
    """Base error of the Tool Registry boundary."""


class DuplicateToolError(ToolRegistryError):
    """Raised when a Tool name is registered a second time."""


class UnknownToolError(ToolRegistryError):
    """Raised when a Tool name cannot be resolved."""


class ToolNotAllowedError(ToolRegistryError):
    """Raised when a definition is rejected by the execution policy."""


@dataclass(frozen=True, slots=True)
class Phase1ReadOnlyPolicy:
    """Phase 1 execution policy: READ_ONLY tools only.

    ``allowed_side_effects`` is kept as a frozen set so the same policy object
    can be reused by tests and later execution layers without mutation.
    """

    allowed_side_effects: frozenset[ToolSideEffect] = field(
        default_factory=lambda: frozenset({ToolSideEffect.READ_ONLY})
    )

    def is_allowed(self, side_effect: ToolSideEffect) -> bool:
        """Return whether ``side_effect`` may execute in Phase 1."""
        return ToolSideEffect(side_effect) in self.allowed_side_effects

    def validate(self, definition: ToolDefinition) -> None:
        """Fail closed when ``definition`` is not READ_ONLY."""
        if not self.is_allowed(definition.side_effect):
            raise ToolNotAllowedError(
                f"Phase 1 只允许 READ_ONLY Tool，"
                f"'{definition.name}' 的 side_effect="
                f"{definition.side_effect.value} 被拒绝"
            )


@runtime_checkable
class ToolExecutionPolicy(Protocol):
    """Minimal policy boundary consumed by the registry."""

    def validate(self, definition: ToolDefinition) -> None:
        """Raise a typed error when ``definition`` is not executable."""
        ...


class ToolRegistry:
    """Register, resolve, and policy-check Tool definitions.

    Names are case-sensitive and whitespace-stripped on lookup. Listing is
    deterministic (sorted by name), never dependent on insertion order.
    """

    def __init__(self, policy: ToolExecutionPolicy | None = None) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._policy = policy or Phase1ReadOnlyPolicy()

    def register(self, definition: ToolDefinition) -> None:
        """Register ``definition``; raise on duplicate names (fail-closed)."""
        if not isinstance(definition, ToolDefinition):
            raise TypeError("ToolRegistry 只能注册 ToolDefinition")
        key = self._normalize_lookup(definition.name)
        if key in self._definitions:
            raise DuplicateToolError(
                f"Tool 已注册，禁止覆盖：{definition.name}"
            )
        self._definitions[key] = definition

    def get(self, name: str) -> ToolDefinition:
        """Return the definition by name without execution-policy checks."""
        return self._resolve_definition(name)

    def contains(self, name: str) -> bool:
        """Return whether a definition with this name is registered."""
        try:
            key = self._normalize_lookup(name)
        except TypeError:
            return False
        return key in self._definitions

    def resolve(self, name: str) -> ToolDefinition:
        """Resolve ``name`` and validate it against the execution policy.

        Unknown names raise :class:`UnknownToolError`; non-READ_ONLY
        definitions raise :class:`ToolNotAllowedError` in Phase 1.
        """
        definition = self._resolve_definition(name)
        self._policy.validate(definition)
        return definition

    def list_definitions(self) -> tuple[ToolDefinition, ...]:
        """Return all registered definitions sorted by name (deterministic)."""
        return tuple(sorted(self._definitions.values(), key=lambda item: item.name))

    def _resolve_definition(self, name: str) -> ToolDefinition:
        key = self._normalize_lookup(name)
        try:
            return self._definitions[key]
        except KeyError:
            raise UnknownToolError(f"Tool 未注册：{name}") from None

    @staticmethod
    def _normalize_lookup(name: str) -> str:
        if not isinstance(name, str):
            raise TypeError("Tool name 必须是字符串")
        return name.strip()


__all__ = [
    "DuplicateToolError",
    "Phase1ReadOnlyPolicy",
    "ToolExecutionPolicy",
    "ToolNotAllowedError",
    "ToolRegistry",
    "ToolRegistryError",
    "UnknownToolError",
]
