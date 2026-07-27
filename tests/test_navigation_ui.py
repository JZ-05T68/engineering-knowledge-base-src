"""Smoke tests for review continuation entries on the home and browser pages."""

from __future__ import annotations

from pathlib import Path

from PIL import Image
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


def _reader_navigation_runtime(
    tmp_path: Path, monkeypatch
) -> tuple[Database, int]:
    database = Database(tmp_path / "database" / "knowledge.db")
    raw_dir = tmp_path / "raw"
    pages_dir = tmp_path / "pages"
    raw_dir.mkdir(parents=True)
    pages_dir.mkdir(parents=True)

    document = database.create_document(
        title="八页导航测试",
        filename="eight-pages.pdf",
        source_path=raw_dir / "eight-pages.pdf",
        sha256="7" * 64,
    )
    document.source_path.write_bytes(b"pdf")
    for page_number in range(1, 9):
        image_path = pages_dir / str(document.id) / f"page_{page_number:04d}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (2, 2), "white").save(image_path)
        database.create_page(
            document_id=document.id,
            page_number=page_number,
            image_path=image_path,
            extracted_text=f"PAGE {page_number} TOKEN NAV-{page_number:04d}",
        )
    database.update_document_page_count(document.id, 8)

    other_document = database.create_document(
        title="其他文档",
        filename="other.pdf",
        source_path=raw_dir / "other.pdf",
        sha256="8" * 64,
    )
    other_document.source_path.write_bytes(b"pdf")
    other_image_path = pages_dir / str(other_document.id) / "page_0001.png"
    other_image_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2, 2), "white").save(other_image_path)
    database.create_page(
        document_id=other_document.id,
        page_number=1,
        image_path=other_image_path,
        extracted_text="OTHER DOCUMENT PAGE 1",
    )
    database.update_document_page_count(other_document.id, 1)

    ordered_list_pages = database.list_pages

    def reversed_list_pages(document_id: int):
        return list(reversed(ordered_list_pages(document_id)))

    monkeypatch.setattr(database, "list_pages", reversed_list_pages)
    service = DocumentService(
        database,
        raw_dir,
        pages_dir,
        tmp_path / "markdown",
    )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_document_service", lambda: service)
    monkeypatch.setattr(
        runtime,
        "application_evidence_basket_service",
        lambda: EvidenceBasketService(database),
    )
    return database, document.id


def _reader_page_selector(app: AppTest):
    return next(selectbox for selectbox in app.selectbox if selectbox.label == "页码")


def _reader_button(app: AppTest, label: str):
    return next(button for button in app.button if button.label == label)


def _has_caption(app: AppTest, text: str) -> bool:
    return any(text in caption.value for caption in app.caption)


def test_reader_page_selection_synchronizes_position_and_ordinary_navigation(
    tmp_path: Path, monkeypatch
) -> None:
    _, document_id = _reader_navigation_runtime(tmp_path, monkeypatch)
    reader_path = next((Path(__file__).parents[1] / "pages").glob("2_*.py"))
    app = AppTest.from_file(str(reader_path))
    app.query_params = {"document": str(document_id)}
    app.run(timeout=10)

    assert not app.exception
    assert _reader_page_selector(app).value == 1
    assert app.query_params["page"] == ["1"]
    assert _has_caption(app, "当前文档记录位置：第 1 / 8 页（PDF 页码 1）")
    assert _reader_button(app, "← 普通上一页").disabled
    assert not _reader_button(app, "普通下一页 →").disabled
    assert _has_caption(app, "普通下一页：第 2 页")

    _reader_page_selector(app).set_value(4).run(timeout=10)

    assert not app.exception
    assert _reader_page_selector(app).value == 4
    assert app.query_params["page"] == ["4"]
    assert _has_caption(app, "当前文档记录位置：第 4 / 8 页（PDF 页码 4）")
    assert not _reader_button(app, "← 普通上一页").disabled
    assert not _reader_button(app, "普通下一页 →").disabled
    assert _has_caption(app, "普通上一页：第 3 页")
    assert _has_caption(app, "普通下一页：第 5 页")

    _reader_button(app, "← 普通上一页").click().run(timeout=10)
    assert _reader_page_selector(app).value == 3
    assert app.query_params["page"] == ["3"]
    assert _has_caption(app, "当前文档记录位置：第 3 / 8 页（PDF 页码 3）")

    _reader_page_selector(app).set_value(4).run(timeout=10)
    _reader_button(app, "普通下一页 →").click().run(timeout=10)
    assert _reader_page_selector(app).value == 5
    assert app.query_params["page"] == ["5"]
    assert _has_caption(app, "当前文档记录位置：第 5 / 8 页（PDF 页码 5）")

    _reader_page_selector(app).set_value(8).run(timeout=10)
    assert _reader_page_selector(app).value == 8
    assert app.query_params["page"] == ["8"]
    assert _has_caption(app, "当前文档记录位置：第 8 / 8 页（PDF 页码 8）")
    assert not _reader_button(app, "← 普通上一页").disabled
    assert _reader_button(app, "普通下一页 →").disabled
    assert _has_caption(app, "普通上一页：第 7 页")
    assert not _has_caption(app, "普通下一页：第 1 页")


def test_reader_query_target_uses_same_sorted_document_navigation(
    tmp_path: Path, monkeypatch
) -> None:
    _, document_id = _reader_navigation_runtime(tmp_path, monkeypatch)
    reader_path = next((Path(__file__).parents[1] / "pages").glob("2_*.py"))
    app = AppTest.from_file(str(reader_path))
    app.query_params = {"document": str(document_id), "page": "4"}
    app.run(timeout=10)

    assert not app.exception
    assert _reader_page_selector(app).value == 4
    assert app.query_params["page"] == ["4"]
    assert _has_caption(app, "当前文档记录位置：第 4 / 8 页（PDF 页码 4）")
    assert _has_caption(app, "普通上一页：第 3 页")
    assert _has_caption(app, "普通下一页：第 5 页")


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
