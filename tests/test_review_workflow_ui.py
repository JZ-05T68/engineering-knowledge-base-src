"""Streamlit interaction tests for the guarded continuous review workflow."""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.database import Database
from src.document_service import DocumentImportError, DocumentService
from src.models import PageStatus


def _build_review_app(
    tmp_path: Path,
    monkeypatch,
    *,
    service_type: type[DocumentService] = DocumentService,
) -> tuple[AppTest, Database, int]:
    database = Database(tmp_path / "database" / "knowledge.db")
    document = database.create_document(
        title="界面复核测试",
        filename="review-ui.pdf",
        source_path=tmp_path / "raw" / "review-ui.pdf",
        sha256="4" * 64,
    )
    for page_number in (1, 2):
        database.create_page(
            document_id=document.id,
            page_number=page_number,
            image_path=tmp_path / "pages" / f"page_{page_number:04d}.png",
        )
    service = service_type(
        database,
        tmp_path / "raw",
        tmp_path / "pages",
        tmp_path / "markdown",
    )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_document_service", lambda: service)
    app_path = next((Path(__file__).parents[1] / "pages").glob("4_*.py"))
    app = AppTest.from_file(str(app_path)).run(timeout=10)
    return app, database, document.id


def _button(app: AppTest, label: str):
    return next(button for button in app.button if button.label == label)


def test_review_ui_saves_draft_then_reviews_and_enters_next_page(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, document_id = _build_review_app(tmp_path, monkeypatch)

    assert not app.exception
    assert {
        "保存草稿",
        "保存并标记已复核",
        "保存、复核并进入下一页",
        "暂时跳过",
        "上一待处理页",
        "下一待处理页",
    } <= {button.label for button in app.button}

    app.text_area(key="review_markdown_1").input("# 第一页草稿").run()
    _button(app, "保存草稿").click().run()
    first_page = database.get_page_by_number(document_id, 1)
    assert first_page is not None
    assert first_page.status is PageStatus.DRAFT
    assert first_page.markdown_content == "# 第一页草稿"

    app.text_area(key="review_markdown_1").input("# 第一页已复核").run()
    _button(app, "保存、复核并进入下一页").click().run()
    first_page = database.get_page_by_number(document_id, 1)
    assert first_page is not None
    assert first_page.status is PageStatus.REVIEWED
    assert app.session_state["review_active_page_id"] == 2
    assert not app.exception


def test_review_ui_warns_before_navigation_with_unsaved_changes(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, document_id = _build_review_app(tmp_path, monkeypatch)

    app.text_area(key="review_markdown_1").input("尚未保存的内容").run()
    _button(app, "下一待处理页").click().run()

    assert app.session_state["review_active_page_id"] == 1
    assert app.session_state["review_markdown_1"] == "尚未保存的内容"
    assert any("放弃修改并切换" in warning.value for warning in app.warning)
    first_page = database.get_page_by_number(document_id, 1)
    assert first_page is not None
    assert first_page.markdown_content == ""
    assert not app.exception


def test_review_ui_keeps_editor_content_when_save_fails(tmp_path: Path, monkeypatch) -> None:
    class FailingReviewService(DocumentService):
        def save_page_markdown(self, *args, **kwargs):
            del args, kwargs
            raise DocumentImportError("模拟只读故障")

    app, database, document_id = _build_review_app(
        tmp_path,
        monkeypatch,
        service_type=FailingReviewService,
    )

    app.text_area(key="review_markdown_1").input("失败后必须保留").run()
    _button(app, "保存草稿").click().run()

    assert app.session_state["review_markdown_1"] == "失败后必须保留"
    assert any("编辑框内容已保留" in error.value for error in app.error)
    first_page = database.get_page_by_number(document_id, 1)
    assert first_page is not None
    assert first_page.markdown_content == ""
    assert not app.exception
