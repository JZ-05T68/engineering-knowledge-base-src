"""Suite-wide pytest guards.

AppTest fixtures must stub every runtime service factory they trigger. The
real ``application_settings()`` creates checkout data directories and attaches
a log handler to the checkout's own ``logs/engineering-kb.log``; tests must
never initialize it.

Ordinary tests must also stay independent of the developer's environment
(audit R-02): the real ``.env`` carries AI credentials and the OS session may
carry ``EKB_*``/``DASHSCOPE_*`` values, so the isolation fixture removes those
variables and neutralizes ``Settings``' implicit dotenv source. Tests that
deliberately exercise environment behavior opt in explicitly (see the fixture
docstring); ``tests/test_test_environment_isolation.py`` locks this contract.
"""

from __future__ import annotations

import os

import pytest

import src.runtime as runtime
from src.config import Settings

#: Environment namespaces that carry developer credentials or runtime intent.
_SENSITIVE_ENV_PREFIXES: tuple[str, ...] = ("EKB_", "DASHSCOPE_")


@pytest.fixture(autouse=True)
def _block_real_application_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if any test path reaches the real application runtime."""

    def _forbidden() -> None:
        raise AssertionError(
            "测试触发了真实 application_settings()：请在该测试的 AppTest fixture 中"
            " stub 所需的 runtime 服务（含 application_document_deletion_service），"
            "不得触碰 checkout 下的正式运行时。"
        )

    monkeypatch.setattr(runtime, "application_settings", _forbidden)


@pytest.fixture(autouse=True)
def _isolate_developer_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ordinary tests deterministic and free of developer secrets.

    Two sources could otherwise leak into any ``Settings()`` construction:

    1. ``os.environ`` — ambient ``EKB_*``/``DASHSCOPE_*`` variables (including
       real credentials) are removed for the duration of each test; spawned
       subprocesses therefore inherit a sanitized environment as well.
    2. ``PROJECT_ROOT/.env`` — ``Settings.model_config`` points at the real
       checkout dotenv file; the attribute is neutralized here so implicit
       constructions cannot read it. Production behavior is unchanged: the
       fixture only exists inside the test process.

    Opt-in remains fully supported and is the documented extension point:

    - a test may set its own variables with ``monkeypatch.setenv`` (applied
      after this fixture, so they survive) — see ``tests/test_config.py``;
    - a test may pass ``_env_file=<path>`` to ``Settings``, which pydantic-settings
      resolves with higher precedence than ``model_config`` — used by tests
      that deliberately verify dotenv loading.
    """

    for name in tuple(os.environ):
        if name.startswith(_SENSITIVE_ENV_PREFIXES):
            monkeypatch.delenv(name, raising=False)

    monkeypatch.setattr(
        Settings,
        "model_config",
        {**Settings.model_config, "env_file": None},
    )
