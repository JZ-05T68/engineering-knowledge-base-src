"""Suite-wide pytest guards.

AppTest fixtures must stub every runtime service factory they trigger. The
real ``application_settings()`` creates checkout data directories and attaches
a log handler to the checkout's own ``logs/engineering-kb.log``; tests must
never initialize it.
"""

from __future__ import annotations

import pytest

import src.runtime as runtime


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
