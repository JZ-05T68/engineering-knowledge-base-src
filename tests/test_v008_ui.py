"""v0.0.8 first-use empty states and maintenance-page integration tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.backup_service import BackupService
from src.config import Settings
from src.database import Database
from src.diagnostic_service import DiagnosticService
from src.document_service import DocumentService
from src.evidence_basket_service import EvidenceBasketService


def _button(app: AppTest, label: str):
    return next(button for button in app.button if button.label == label)


@pytest.fixture
def empty_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data = tmp_path / "data"
    settings = Settings(
        data_dir=data,
        raw_dir=data / "raw",
        pages_dir=data / "pages",
        markdown_dir=data / "markdown",
        database_dir=data / "database",
        database_path=data / "database" / "knowledge.db",
        backups_dir=tmp_path / "backups",
        logs_dir=tmp_path / "logs",
        log_path=tmp_path / "logs" / "engineering-kb.log",
        runtime_dir=tmp_path / "runtime",
        pid_path=tmp_path / "runtime" / "engineering-kb.pid.json",
        port=49341,
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    document_service = DocumentService(
        database,
        settings.raw_dir,
        settings.pages_dir,
        settings.markdown_dir,
    )
    evidence_service = EvidenceBasketService(database)
    backup_service = BackupService(
        app_version=settings.app_version,
        data_dir=settings.data_dir,
        raw_dir=settings.raw_dir,
        pages_dir=settings.pages_dir,
        markdown_dir=settings.markdown_dir,
        database_path=settings.database_path,
        backups_dir=settings.backups_dir,
        host=settings.host,
        port=settings.port,
    )
    diagnostic_service = DiagnosticService(
        app_version=settings.app_version,
        project_root=tmp_path,
        data_dir=settings.data_dir,
        raw_dir=settings.raw_dir,
        pages_dir=settings.pages_dir,
        markdown_dir=settings.markdown_dir,
        database_path=settings.database_path,
        backups_dir=settings.backups_dir,
        logs_dir=settings.logs_dir,
        log_path=settings.log_path,
        host=settings.host,
        port=settings.port,
        listener_addresses=lambda port: (),
        health_check=lambda port: False,
        port_is_open=lambda port: False,
    )

    import src.runtime as runtime

    monkeypatch.setattr(runtime, "application_settings", lambda: settings)
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_document_service", lambda: document_service)
    monkeypatch.setattr(
        runtime,
        "application_evidence_basket_service",
        lambda: evidence_service,
    )
    monkeypatch.setattr(runtime, "application_backup_service", lambda: backup_service)
    monkeypatch.setattr(
        runtime,
        "application_diagnostic_service",
        lambda: diagnostic_service,
    )
    return settings


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        ("app.py", "导入第一份 PDF"),
        ("pages/1_导入资料.py", "选择第一份 PDF"),
        ("pages/2_浏览资料.py", "还没有可浏览的文档"),
        ("pages/3_检索资料.py", "暂无可检索内容"),
        ("pages/4_待整理页面.py", "还没有文档或待复核页面"),
        ("pages/5_标签管理.py", "创建第一个标签"),
        ("pages/6_项目管理.py", "创建第一个本地项目"),
        ("pages/9_证据篮.py", "证据篮为空"),
        ("pages/10_系统维护.py", "还没有 v0.1.0 完整备份"),
    ],
)
def test_major_pages_have_actionable_empty_states(
    empty_runtime: Settings, page: str, expected: str
) -> None:
    del empty_runtime
    app = AppTest.from_file(page).run(timeout=10)

    messages = [element.value for element in (*app.info, *app.success, *app.warning)]
    assert any(expected in message for message in messages)
    assert not app.exception


def test_maintenance_page_creates_verified_backup_and_runs_read_only_diagnostics(
    empty_runtime: Settings,
) -> None:
    app = AppTest.from_file("pages/10_系统维护.py").run(timeout=10)

    _button(app, "创建并验证完整备份").click().run(timeout=20)
    assert list(empty_runtime.backups_dir.glob("*/manifest.json"))
    assert not app.exception

    _button(app, "运行完整只读诊断").click().run(timeout=20)
    assert any("诊断完成" in element.value for element in (*app.warning, *app.error))
    assert not app.exception
