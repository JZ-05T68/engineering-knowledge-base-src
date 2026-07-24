"""Smoke tests for review continuation entries on the home and browser pages."""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.config import Settings
from src.database import Database
from src.document_service import DocumentService
from src.evidence_basket_service import EvidenceBasketService


def _local_runtime(tmp_path: Path, monkeypatch) -> tuple[Database, DocumentService]:
    database = Database(tmp_path / "database" / "knowledge.db")
    document = database.create_document(
        title="导航入口测试",
        filename="navigation.pdf",
        source_path=tmp_path / "raw" / "navigation.pdf",
        sha256="5" * 64,
    )
    database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=tmp_path / "pages" / "page_0001.png",
    )
    service = DocumentService(
        database,
        tmp_path / "raw",
        tmp_path / "pages",
        tmp_path / "markdown",
    )
    settings = Settings(
        data_dir=tmp_path,
        raw_dir=tmp_path / "raw",
        pages_dir=tmp_path / "pages",
        markdown_dir=tmp_path / "markdown",
        database_dir=tmp_path / "database",
        database_path=tmp_path / "database" / "knowledge.db",
        logs_dir=tmp_path / "logs",
        log_path=tmp_path / "logs" / "test.log",
        runtime_dir=tmp_path / "runtime",
        pid_path=tmp_path / "runtime" / "test.pid.json",
    )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_document_service", lambda: service)
    monkeypatch.setattr(
        runtime,
        "application_evidence_basket_service",
        lambda: EvidenceBasketService(database),
    )
    monkeypatch.setattr(runtime, "application_settings", lambda: settings)
    return database, service


def test_home_and_browser_show_review_continuation_entry(tmp_path: Path, monkeypatch) -> None:
    _local_runtime(tmp_path, monkeypatch)
    project_root = Path(__file__).parents[1]

    home = AppTest.from_file(str(project_root / "app.py")).run(timeout=10)
    browser_path = next((project_root / "pages").glob("2_*.py"))
    browser = AppTest.from_file(str(browser_path)).run(timeout=10)

    assert not home.exception
    assert not browser.exception
    assert any(button.label == "继续处理下一待复核页" for button in home.button)
    assert any(button.label == "继续处理下一待复核页" for button in browser.button)


def test_browser_warns_when_database_note_has_no_markdown_file(
    tmp_path: Path, monkeypatch
) -> None:
    database, _ = _local_runtime(tmp_path, monkeypatch)
    page = database.list_pages(1)[0]
    database.update_page_markdown(
        page.id,
        "# 数据库仍有笔记",
        tmp_path / "markdown" / "1" / "page_0001.md",
    )

    browser_path = next((Path(__file__).parents[1] / "pages").glob("2_*.py"))
    browser = AppTest.from_file(str(browser_path)).run(timeout=10)

    assert not browser.exception
    assert any(
        "对应 Markdown 文件缺失或未记录" in warning.value
        for warning in browser.warning
    )
    assert any(metric.label == "导入已处理页" for metric in browser.metric)


def test_browser_disables_markdown_clear_entry_and_keeps_editing_available(
    tmp_path: Path, monkeypatch
) -> None:
    class ClearTrackingDocumentService(DocumentService):
        clear_calls = 0

        def clear_page_markdown(self, document_id: int, page_number: int):
            del document_id, page_number
            self.clear_calls += 1
            raise AssertionError("正常页面交互不得调用 Markdown 清空协议")

    database = Database(tmp_path / "database" / "knowledge.db")
    document = database.create_document(
        title="Markdown 安全入口测试",
        filename="markdown-safety.pdf",
        source_path=tmp_path / "raw" / "markdown-safety.pdf",
        sha256="6" * 64,
    )
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=tmp_path / "pages" / "page_0001.png",
    )
    markdown_path = tmp_path / "markdown" / str(document.id) / "page_0001.md"
    markdown_path.parent.mkdir(parents=True)
    markdown_path.write_text("# 原有笔记", encoding="utf-8")
    database.update_page_markdown(page.id, "# 原有笔记", markdown_path)
    service = ClearTrackingDocumentService(
        database,
        tmp_path / "raw",
        tmp_path / "pages",
        tmp_path / "markdown",
    )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_document_service", lambda: service)
    monkeypatch.setattr(
        runtime,
        "application_evidence_basket_service",
        lambda: EvidenceBasketService(database),
    )

    browser_path = next((Path(__file__).parents[1] / "pages").glob("2_*.py"))
    browser = AppTest.from_file(str(browser_path)).run(timeout=10)

    assert not browser.exception
    assert "确认清空" not in {checkbox.label for checkbox in browser.checkbox}
    assert "清空笔记" not in {button.label for button in browser.button}
    assert any(element.value == "# 原有笔记" for element in browser.markdown)

    browser.text_area(key=f"markdown_editor_{page.id}_current").input(
        "# 更新后的笔记"
    ).run(timeout=10)
    next(button for button in browser.button if button.label == "保存笔记").click().run(
        timeout=10
    )

    updated_page = database.get_page(page.id)
    assert updated_page is not None
    assert updated_page.markdown_content == "# 更新后的笔记"
    assert markdown_path.read_text(encoding="utf-8") == "# 更新后的笔记"
    assert service.clear_calls == 0
    assert not browser.exception
