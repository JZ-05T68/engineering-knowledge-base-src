"""Streamlit interaction tests for the guarded continuous review workflow."""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.database import Database
from src.document_service import DocumentImportError, DocumentService
from src.models import PageStatus
from src.review_shortcuts import review_shortcuts_html


def _build_review_app(
    tmp_path: Path,
    monkeypatch,
    *,
    service_type: type[DocumentService] = DocumentService,
    page_count: int = 2,
) -> tuple[AppTest, Database, int]:
    database = Database(tmp_path / "database" / "knowledge.db")
    document = database.create_document(
        title="界面复核测试",
        filename="review-ui.pdf",
        source_path=tmp_path / "raw" / "review-ui.pdf",
        sha256="4" * 64,
    )
    for page_number in range(1, page_count + 1):
        database.create_page(
            document_id=document.id,
            page_number=page_number,
            image_path=tmp_path / "pages" / f"page_{page_number:04d}.png",
            extracted_text=f"REVIEW PAGE {page_number} TOKEN REVIEW-{page_number:04d}",
        )
    database.update_document_page_count(document.id, page_count)
    service = service_type(
        database,
        tmp_path / "raw",
        tmp_path / "pages",
        tmp_path / "markdown",
    )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_document_service", lambda: service)
    app_path = next((Path(__file__).parents[1] / "pages").glob("5_*.py"))
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
        "继续处理下一待复核页",
        "保存草稿",
        "保存并标记已复核",
        "保存、复核并进入下一页",
        "暂时跳过",
        "上一待处理页",
        "下一待处理页",
    } <= {button.label for button in app.button}
    assert {"当前页", "已处理数", "总页数", "剩余待处理数"} <= {
        metric.label for metric in app.metric
    }

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


def test_shortcut_script_is_guarded_and_keeps_visible_buttons_authoritative() -> None:
    html = review_shortcuts_html()

    assert "保存草稿" in html
    assert "保存、复核并进入下一页" in html
    assert "下一待处理页" in html and "上一待处理页" in html
    assert "isEditing" in html
    assert "target.blur()" in html
    assert "setTimeout(() => clickButton(label), 0)" in html
    assert "event.repeat" in html
    assert "if (label && clickButton(label))" in html


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


def test_review_ui_clears_stale_navigation_warning_after_successful_save(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, document_id = _build_review_app(tmp_path, monkeypatch)

    app.text_area(key="review_markdown_1").input("保存后不应继续警告").run()
    _button(app, "下一待处理页").click().run()
    assert "review_pending_target_id" in app.session_state

    _button(app, "保存草稿并留在当前页").click().run()

    first_page = database.get_page_by_number(document_id, 1)
    assert first_page is not None
    assert first_page.markdown_content == "保存后不应继续警告"
    assert app.session_state["review_pending_target_id"] is None
    assert not any("放弃修改并切换" in warning.value for warning in app.warning)
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


def test_review_direct_jump_shares_state_with_every_navigation_control(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, document_id = _build_review_app(
        tmp_path, monkeypatch, page_count=8
    )
    jump_input_key = f"review_page_jump_{document_id}_input"

    app.text_input(key=jump_input_key).input(" 0005 ").run(timeout=10)
    _button(app, "跳转").click().run(timeout=10)
    page_5 = database.get_page_by_number(document_id, 5)
    assert page_5 is not None
    assert app.session_state["review_active_page_id"] == page_5.id
    assert app.query_params["document"] == [str(document_id)]
    assert app.query_params["page"] == ["5"]
    assert app.query_params["page_id"] == [str(page_5.id)]
    assert next(item for item in app.selectbox if item.label == "选择一页查看").value == page_5.id
    assert next(item for item in app.metric if item.label == "当前页").value == "第 5 页"
    assert any("REVIEW-0005" in item.value for item in app.text)

    _button(app, "下一待处理页").click().run(timeout=10)
    page_6 = database.get_page_by_number(document_id, 6)
    assert page_6 is not None
    assert app.session_state["review_active_page_id"] == page_6.id
    assert app.query_params["page"] == ["6"]

    page_3 = database.get_page_by_number(document_id, 3)
    assert page_3 is not None
    next(
        item for item in app.selectbox if item.label == "选择一页查看"
    ).set_value(page_3.id).run(timeout=10)
    assert app.session_state["review_active_page_id"] == page_3.id
    assert app.query_params["page"] == ["3"]

    _button(app, "上一待处理页").click().run(timeout=10)
    page_2 = database.get_page_by_number(document_id, 2)
    assert page_2 is not None
    assert app.session_state["review_active_page_id"] == page_2.id
    assert app.query_params["page"] == ["2"]

    app.text_input(key=jump_input_key).input("7").run(timeout=10)
    _button(app, "跳转").click().run(timeout=10)
    page_7 = database.get_page_by_number(document_id, 7)
    assert page_7 is not None
    assert app.session_state["review_active_page_id"] == page_7.id
    assert app.query_params["page"] == ["7"]

    app.text_input(key=jump_input_key).input("2.5").run(timeout=10)
    _button(app, "跳转").click().run(timeout=10)
    assert app.session_state["review_active_page_id"] == page_7.id
    assert app.query_params["page"] == ["7"]
    assert any("1 到 8" in warning.value for warning in app.warning)


def test_review_document_filter_and_deep_link_keep_one_canonical_page(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, first_document_id = _build_review_app(
        tmp_path, monkeypatch, page_count=3
    )
    second_document = database.create_document(
        title="第二份资料",
        filename="second.pdf",
        source_path=tmp_path / "raw" / "second.pdf",
        sha256="5" * 64,
    )
    for page_number in range(1, 5):
        database.create_page(
            document_id=second_document.id,
            page_number=page_number,
            image_path=tmp_path / "pages" / f"second_{page_number:04d}.png",
            extracted_text=f"SECOND PAGE {page_number}",
        )
    database.update_document_page_count(second_document.id, 4)
    app.run(timeout=10)

    document_filter = next(
        item for item in app.selectbox if item.label == "选择一份资料"
    )
    document_filter.set_value(second_document.id).run(timeout=10)
    second_page_1 = database.get_page_by_number(second_document.id, 1)
    assert second_page_1 is not None
    assert app.session_state["review_active_page_id"] == second_page_1.id
    assert app.query_params == {
        "page_id": [str(second_page_1.id)],
        "document": [str(second_document.id)],
        "page": ["1"],
    }
    assert next(
        item for item in app.selectbox if item.label == "选择一份资料"
    ).value == second_document.id
    assert any(item.value == "共 4 页" for item in app.markdown)

    second_page_4 = database.get_page_by_number(second_document.id, 4)
    assert second_page_4 is not None
    app_path = next((Path(__file__).parents[1] / "pages").glob("5_*.py"))
    deep_linked = AppTest.from_file(str(app_path))
    deep_linked.query_params = {
        "page_id": str(second_page_4.id),
        "document": str(second_document.id),
        "page": "4",
    }
    deep_linked.run(timeout=10)

    assert not deep_linked.exception
    assert next(
        item for item in deep_linked.selectbox if item.label == "选择一份资料"
    ).value == second_document.id
    assert deep_linked.session_state["review_active_page_id"] == second_page_4.id
    assert deep_linked.query_params == {
        "page_id": [str(second_page_4.id)],
        "document": [str(second_document.id)],
        "page": ["4"],
    }
    assert first_document_id != second_document.id
