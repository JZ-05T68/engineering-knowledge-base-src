"""Streamlit tests for visible-only batch controls on search and review pages."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.batch_selection import BatchSelectionSource, build_visible_page_scope
from src.batch_service import PageBatchService
from src.classification_metadata import ClassificationMetadataService
from src.database import Database
from src.document_service import DocumentService
from src.evidence_basket_service import EvidenceBasketService
from src.models import PageStatus


class CountingPageBatchService(PageBatchService):
    def __init__(self, database: Database) -> None:
        super().__init__(database)
        self.plan_calls = 0
        self.execute_calls = 0

    def plan_status(self, *args, **kwargs):
        self.plan_calls += 1
        return super().plan_status(*args, **kwargs)

    def plan_add_tags(self, *args, **kwargs):
        self.plan_calls += 1
        return super().plan_add_tags(*args, **kwargs)

    def plan_remove_tags(self, *args, **kwargs):
        self.plan_calls += 1
        return super().plan_remove_tags(*args, **kwargs)

    def plan_add_projects(self, *args, **kwargs):
        self.plan_calls += 1
        return super().plan_add_projects(*args, **kwargs)

    def plan_remove_projects(self, *args, **kwargs):
        self.plan_calls += 1
        return super().plan_remove_projects(*args, **kwargs)

    def execute(self, plan):
        self.execute_calls += 1
        return super().execute(plan)


class RaisingPageBatchService(CountingPageBatchService):
    def execute(self, plan):
        self.execute_calls += 1
        raise RuntimeError("simulated unexpected UI-layer execution failure")


def _database_with_pages(
    tmp_path: Path,
    *,
    page_count: int,
    with_entities: bool = True,
) -> tuple[Database, int, list[int]]:
    database = Database(tmp_path / "database" / "knowledge.db")
    document = database.create_document(
        title="批量界面测试手册",
        filename="visible-batch.pdf",
        source_path=tmp_path / "raw" / "visible-batch.pdf",
        sha256="6" * 64,
    )
    page_ids = [
        database.create_page(
            document_id=document.id,
            page_number=page_number,
            image_path=tmp_path / "pages" / f"page_{page_number:04d}.png",
            extracted_text=f"批量关键词 第 {page_number} 页",
        ).id
        for page_number in range(1, page_count + 1)
    ]
    if with_entities:
        database.create_tag("安全标签")
        database.create_project("安全项目")
    return database, document.id, page_ids


def _search_app(
    tmp_path: Path, monkeypatch, *, page_count: int = 25
) -> tuple[AppTest, Database, list[int], CountingPageBatchService]:
    database, _, page_ids = _database_with_pages(tmp_path, page_count=page_count)
    basket_service = EvidenceBasketService(database)
    batch_service = CountingPageBatchService(database)
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(
        runtime, "application_evidence_basket_service", lambda: basket_service
    )
    monkeypatch.setattr(
        runtime, "application_page_batch_service", lambda: batch_service
    )
    app_path = next((Path(__file__).parents[1] / "pages").glob("4_*.py"))
    app = AppTest.from_file(str(app_path))
    app.query_params = {"q": "批量关键词", "limit": "50"}
    app.run(timeout=15)
    return app, database, page_ids, batch_service


def _review_app(
    tmp_path: Path,
    monkeypatch,
    *,
    page_count: int = 22,
    with_entities: bool = True,
) -> tuple[AppTest, Database, int, list[int], CountingPageBatchService]:
    database, document_id, page_ids = _database_with_pages(
        tmp_path, page_count=page_count, with_entities=with_entities
    )
    document_service = DocumentService(
        database,
        tmp_path / "raw",
        tmp_path / "pages",
        tmp_path / "markdown",
    )
    batch_service = CountingPageBatchService(database)
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(
        runtime, "application_document_service", lambda: document_service
    )
    monkeypatch.setattr(
        runtime, "application_page_batch_service", lambda: batch_service
    )
    app_path = next((Path(__file__).parents[1] / "pages").glob("5_*.py"))
    app = AppTest.from_file(str(app_path)).run(timeout=15)
    return app, database, document_id, page_ids, batch_service


def _button(app: AppTest, label: str):
    return next(button for button in app.button if button.label == label)


def _confirm_checkbox(app: AppTest):
    return next(
        checkbox
        for checkbox in app.checkbox
        if checkbox.label.startswith("我确认对当前选择的")
    )


def _page_checkbox(app: AppTest, page_number: int):
    marker = f"第 {page_number} 页"
    return next(
        checkbox for checkbox in app.checkbox if marker in checkbox.label
    )


def test_search_selection_is_limited_to_current_page_and_cleared_on_pagination(
    tmp_path: Path, monkeypatch
) -> None:
    app, _, page_ids, batch_service = _search_app(tmp_path, monkeypatch)

    _button(app, "选择当前可见 10 项").click().run(timeout=15)
    state = app.session_state["visible_page_batch_state"]
    assert state.selected_page_ids == tuple(page_ids[:10])
    assert batch_service.plan_calls == 0 and batch_service.execute_calls == 0

    _button(app, "下一组 →").click().run(timeout=15)

    state = app.session_state["visible_page_batch_state"]
    assert state.selected_page_ids == ()
    assert any("原批量选择已清除" in info.value for info in app.info)
    assert all(_page_checkbox(app, number) for number in range(11, 21))
    assert batch_service.plan_calls == 0 and batch_service.execute_calls == 0


def test_search_equivalent_filter_order_preserves_visible_selection(
    tmp_path: Path, monkeypatch
) -> None:
    app, _, page_ids, _ = _search_app(tmp_path, monkeypatch, page_count=3)
    app.query_params = {
        "q": "批量关键词",
        "limit": "50",
        "statuses": "pending,draft",
    }
    app.run(timeout=15)
    _button(app, "选择当前可见 3 项").click().run(timeout=15)

    app.query_params = {
        "q": "批量关键词",
        "limit": "50",
        "statuses": "draft,pending",
    }
    app.run(timeout=15)

    state = app.session_state["visible_page_batch_state"]
    assert state.selected_page_ids == tuple(page_ids)
    assert not any("原批量选择已清除" in info.value for info in app.info)


def test_search_document_view_clears_hidden_page_selection(
    tmp_path: Path, monkeypatch
) -> None:
    app, _, _, _ = _search_app(tmp_path, monkeypatch)
    _button(app, "选择当前可见 10 项").click().run(timeout=15)

    app.radio(key="search_view_mode").set_value("document").run(timeout=15)

    state = app.session_state["visible_page_batch_state"]
    assert state.selected_page_ids == ()
    assert state.scope_signature == ""
    assert any("原批量选择已清除" in info.value for info in app.info)
    assert not any(
        "当前可见页面批量操作" in element.value for element in app.markdown
    )


def test_search_target_change_invalidates_preflight_plan(tmp_path: Path, monkeypatch) -> None:
    app, _, _, _ = _search_app(tmp_path, monkeypatch)
    _page_checkbox(app, 1).check().run(timeout=15)
    _button(app, "预检批量操作").click().run(timeout=15)
    assert app.session_state["visible_page_batch_state"].pending_action is not None

    app.selectbox(key="visible_batch_status_search").select("skipped").run(timeout=15)

    assert app.session_state["visible_page_batch_state"].pending_action is None
    assert not any(button.label == "执行批量操作" for button in app.button)


def test_search_preflight_confirm_execute_is_one_shot_and_refreshes_results(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, page_ids, batch_service = _search_app(tmp_path, monkeypatch)

    _button(app, "选择当前可见 10 项").click().run(timeout=15)
    _button(app, "预检批量操作").click().run(timeout=15)
    assert batch_service.plan_calls == 1
    assert any("请求页面：10 页" in element.value for element in app.markdown)

    _confirm_checkbox(app).check().run(timeout=15)
    _button(app, "执行批量操作").click().run(timeout=15)

    assert batch_service.execute_calls == 1
    reviewed = [database.get_page(page_id) for page_id in page_ids[:10]]
    assert all(page is not None and page.status is PageStatus.REVIEWED for page in reviewed)
    assert app.session_state["visible_page_batch_state"].selected_page_ids == ()
    assert any("批量操作已完成" in success.value for success in app.success)
    # The execute click already includes the page's requested rerun.
    assert batch_service.execute_calls == 1


def test_review_protected_plan_has_no_execute_and_empty_entities_are_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, _, page_ids, _ = _review_app(
        tmp_path, monkeypatch, with_entities=False
    )
    database.update_page(page_ids[1], status=PageStatus.DRAFT)
    app.run(timeout=15)

    _button(app, "选择当前可见 20 项").click().run(timeout=15)
    _button(app, "预检批量操作").click().run(timeout=15)

    assert any("草稿状态" in warning.value for warning in app.warning)
    assert not any(button.label == "执行批量操作" for button in app.button)
    assert any("标签管理" in info.value for info in app.info)
    assert any("项目管理" in info.value for info in app.info)
    operation = app.selectbox(key="visible_batch_operation_review_queue")
    assert operation.options == ["批量设置状态"]


def test_review_dirty_selected_page_blocks_preflight_and_batch_change_clears_selection(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, document_id, _, batch_service = _review_app(tmp_path, monkeypatch)
    _button(app, "选择当前可见 20 项").click().run(timeout=15)

    app.text_area(key="review_markdown_1").input("尚未保存的批量保护内容")
    _button(app, "预检批量操作").click().run(timeout=15)

    assert _button(app, "预检批量操作").disabled
    assert any("存在未保存编辑" in warning.value for warning in app.warning)
    assert batch_service.plan_calls == 0
    assert app.session_state["visible_page_batch_state"].pending_action is None
    assert not any("批量计划" in element.value for element in app.markdown)
    app.selectbox(key="review_visible_batch_number").select(2).run(timeout=15)
    assert app.session_state["visible_page_batch_state"].selected_page_ids == ()
    assert app.session_state["review_markdown_1"] == "尚未保存的批量保护内容"
    first = database.get_page_by_number(document_id, 1)
    assert first is not None and first.markdown_content == ""


def test_review_database_pagination_limits_each_visible_batch(
    tmp_path: Path, monkeypatch
) -> None:
    app, _, _, _, _ = _review_app(tmp_path, monkeypatch, page_count=45)

    assert _button(app, "选择当前可见 20 项")
    assert all(_page_checkbox(app, number) for number in range(1, 21))
    app.selectbox(key="review_visible_batch_number").select(2).run(timeout=15)
    assert _button(app, "选择当前可见 20 项")
    assert all(_page_checkbox(app, number) for number in range(21, 41))
    app.selectbox(key="review_visible_batch_number").select(3).run(timeout=15)
    assert _button(app, "选择当前可见 5 项")
    assert all(_page_checkbox(app, number) for number in range(41, 46))


def test_review_pagination_scope_uses_database_page_id_order(
    tmp_path: Path, monkeypatch
) -> None:
    app, _, _, page_ids, _ = _review_app(tmp_path, monkeypatch, page_count=25)
    expected_scope = build_visible_page_scope(
        source=BatchSelectionSource.REVIEW_QUEUE,
        document_id=None,
        filters={
            "review_statuses": (
                PageStatus.PENDING.value,
                PageStatus.DRAFT.value,
                PageStatus.FAILED.value,
            )
        },
        sort="document_page",
        query="",
        batch_number=1,
        visible_page_ids=page_ids[:20],
    )

    state = app.session_state["visible_page_batch_state"]
    assert state.scope_signature == expected_scope.signature


def test_review_document_change_clears_selection_plan_confirmation_and_token(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, _, _, _ = _review_app(tmp_path, monkeypatch, page_count=3)
    second_document = database.create_document(
        title="第二份批量文档",
        filename="second-visible-batch.pdf",
        source_path=tmp_path / "raw" / "second-visible-batch.pdf",
        sha256="7" * 64,
    )
    for page_number in (1, 2):
        database.create_page(
            document_id=second_document.id,
            page_number=page_number,
            image_path=tmp_path / "pages" / f"second-{page_number}.png",
        )
    app.run(timeout=15)
    _button(app, "选择当前可见 5 项").click().run(timeout=15)
    _button(app, "预检批量操作").click().run(timeout=15)
    _confirm_checkbox(app).check().run(timeout=15)
    assert app.session_state["visible_page_batch_state"].confirmed_token is not None

    app.selectbox(key="review_document_filter").select(second_document.id).run(timeout=15)

    state = app.session_state["visible_page_batch_state"]
    assert state.selected_page_ids == ()
    assert state.pending_action is None
    assert state.confirmed_token is None
    assert _button(app, "选择当前可见 2 项")
    assert not app.exception


def test_review_external_range_change_invalidates_unexecuted_plan(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, _, page_ids, batch_service = _review_app(
        tmp_path, monkeypatch, page_count=21
    )
    app.selectbox(key="review_visible_batch_number").select(2).run(timeout=15)
    _button(app, "选择当前可见 1 项").click().run(timeout=15)
    _button(app, "预检批量操作").click().run(timeout=15)
    _confirm_checkbox(app).check().run(timeout=15)
    old_token = app.session_state["visible_page_batch_state"].confirmed_token

    database.update_page(page_ids[-1], status=PageStatus.REVIEWED)
    app.run(timeout=15)

    state = app.session_state["visible_page_batch_state"]
    assert old_token is not None
    assert state.selected_page_ids == ()
    assert state.pending_action is None
    assert state.confirmed_token is None
    assert batch_service.execute_calls == 0
    assert app.session_state["review_visible_batch_number"] == 1
    assert not any(button.label == "执行批量操作" for button in app.button)


def test_review_last_batch_falls_back_after_batch_completion(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, _, page_ids, batch_service = _review_app(
        tmp_path, monkeypatch, page_count=21
    )
    app.selectbox(key="review_visible_batch_number").select(2).run(timeout=15)
    _button(app, "选择当前可见 1 项").click().run(timeout=15)
    _button(app, "预检批量操作").click().run(timeout=15)
    _confirm_checkbox(app).check().run(timeout=15)
    _button(app, "执行批量操作").click().run(timeout=15)

    assert batch_service.execute_calls == 1
    assert database.get_page(page_ids[-1]).status is PageStatus.REVIEWED  # type: ignore[union-attr]
    assert app.session_state["review_visible_batch_number"] == 1
    assert _button(app, "选择当前可见 20 项")
    assert not app.exception


def test_search_and_review_pages_use_one_shared_metadata_entry_per_run(
    tmp_path: Path, monkeypatch
) -> None:
    database, _, _ = _database_with_pages(tmp_path, page_count=3)
    basket_service = EvidenceBasketService(database)
    batch_service = CountingPageBatchService(database)

    class CountingMetadataService(ClassificationMetadataService):
        def __init__(self, database: Database) -> None:
            super().__init__(database)
            self.load_calls = 0

        def load(self, **kwargs):
            self.load_calls += 1
            return super().load(**kwargs)

    metadata_service = CountingMetadataService(database)
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(
        runtime, "application_evidence_basket_service", lambda: basket_service
    )
    monkeypatch.setattr(
        runtime, "application_page_batch_service", lambda: batch_service
    )
    monkeypatch.setattr(
        runtime,
        "application_classification_metadata_service",
        lambda: metadata_service,
    )

    search_path = next((Path(__file__).parents[1] / "pages").glob("4_*.py"))
    search_app = AppTest.from_file(str(search_path))
    search_app.query_params = {"q": "批量关键词", "limit": "50"}
    search_app.run(timeout=15)
    assert metadata_service.load_calls == 1
    assert not search_app.exception

    document_service = DocumentService(
        database,
        tmp_path / "raw",
        tmp_path / "pages",
        tmp_path / "markdown",
    )
    monkeypatch.setattr(
        runtime, "application_document_service", lambda: document_service
    )
    review_path = next((Path(__file__).parents[1] / "pages").glob("5_*.py"))
    review_app = AppTest.from_file(str(review_path)).run(timeout=15)

    assert metadata_service.load_calls == 2
    assert not review_app.exception


def test_review_dirty_unselected_editor_survives_other_page_batch_reruns(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, document_id, page_ids, batch_service = _review_app(
        tmp_path, monkeypatch
    )
    app.text_area(key="review_markdown_1").input("不能丢失也不能自动保存").run(
        timeout=15
    )
    _page_checkbox(app, 2).check().run(timeout=15)

    assert not _button(app, "预检批量操作").disabled
    _button(app, "预检批量操作").click().run(timeout=15)
    _confirm_checkbox(app).check().run(timeout=15)
    _button(app, "执行批量操作").click().run(timeout=15)

    assert batch_service.execute_calls == 1
    assert database.get_page(page_ids[1]).status is PageStatus.REVIEWED  # type: ignore[union-attr]
    first = database.get_page_by_number(document_id, 1)
    assert first is not None and first.markdown_content == ""
    assert app.session_state["review_markdown_1"] == "不能丢失也不能自动保存"


def test_review_feedback_survives_when_last_visible_queue_page_disappears(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, _, page_ids, _ = _review_app(
        tmp_path, monkeypatch, page_count=1
    )
    _button(app, "选择当前可见 1 项").click().run(timeout=15)
    _button(app, "预检批量操作").click().run(timeout=15)
    _confirm_checkbox(app).check().run(timeout=15)
    _button(app, "执行批量操作").click().run(timeout=15)

    page = database.get_page(page_ids[0])
    assert page is not None and page.status is PageStatus.REVIEWED
    assert any("批量操作已完成" in success.value for success in app.success)
    assert not any(
        "当前可见页面批量操作" in element.value for element in app.markdown
    )


def test_stale_plan_preserves_visible_selection_and_is_never_retried(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, page_ids, batch_service = _search_app(tmp_path, monkeypatch)
    _page_checkbox(app, 1).check().run(timeout=15)
    _button(app, "预检批量操作").click().run(timeout=15)
    with sqlite3.connect(database.database_path) as connection:
        connection.execute(
            "UPDATE pages SET updated_at = ? WHERE id = ?",
            ("2099-01-01T00:00:00+00:00", page_ids[0]),
        )

    _confirm_checkbox(app).check().run(timeout=15)
    _button(app, "执行批量操作").click().run(timeout=15)

    state = app.session_state["visible_page_batch_state"]
    assert batch_service.execute_calls == 1
    assert state.selected_page_ids == (page_ids[0],)
    assert state.pending_action is None
    assert any("本次操作已取消" in warning.value for warning in app.warning)
    # The execute click already includes the page's requested rerun.
    assert batch_service.execute_calls == 1


def test_search_stale_relation_refreshes_results_and_clears_hidden_selection(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, page_ids, batch_service = _search_app(
        tmp_path, monkeypatch, page_count=3
    )
    tag = database.list_tags()[0]
    database.set_page_tags(page_ids[0], [tag.id])
    app.query_params = {
        "q": "批量关键词",
        "limit": "50",
        "tags": str(tag.id),
    }
    app.run(timeout=15)
    _page_checkbox(app, 1).check().run(timeout=15)
    app.selectbox(key="visible_batch_operation_search").select(
        "remove_page_tags"
    ).run(timeout=15)
    app.multiselect(key="visible_batch_tags_search").select(tag.id).run(timeout=15)
    _button(app, "预检批量操作").click().run(timeout=15)
    database.set_page_tags(page_ids[0], [])

    _confirm_checkbox(app).check().run(timeout=15)
    _button(app, "执行批量操作").click().run(timeout=15)

    state = app.session_state["visible_page_batch_state"]
    assert batch_service.execute_calls == 1
    assert state.selected_page_ids == ()
    assert state.pending_action is None
    assert app.session_state["knowledge_results"] == []
    assert any("本次操作已取消" in warning.value for warning in app.warning)
    assert not any(
        "当前可见页面批量操作" in element.value for element in app.markdown
    )


def test_execute_exception_consumes_token_and_requires_new_preflight(
    tmp_path: Path, monkeypatch
) -> None:
    app, database, page_ids, _ = _search_app(tmp_path, monkeypatch, page_count=3)
    raising_service = RaisingPageBatchService(database)
    monkeypatch.setattr(
        runtime, "application_page_batch_service", lambda: raising_service
    )
    app.run(timeout=15)
    _page_checkbox(app, 1).check().run(timeout=15)
    _button(app, "预检批量操作").click().run(timeout=15)
    _confirm_checkbox(app).check().run(timeout=15)
    old_token = app.session_state["visible_page_batch_state"].confirmed_token

    _button(app, "执行批量操作").click().run(timeout=15)

    state = app.session_state["visible_page_batch_state"]
    assert old_token is not None and old_token in state.consumed_tokens
    assert state.selected_page_ids == (page_ids[0],)
    assert state.pending_action is None
    assert state.confirmed_token is None
    assert raising_service.execute_calls == 1
    page = database.get_page(page_ids[0])
    assert page is not None and page.status is PageStatus.PENDING
    assert any("确认凭证已失效" in error.value for error in app.error)
    assert not app.exception

    app.run(timeout=15)
    assert raising_service.execute_calls == 1
    assert not any(button.label == "执行批量操作" for button in app.button)
