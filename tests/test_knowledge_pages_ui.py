"""AppTest UI tests for the v0.5.2 knowledge pages (Phase 2B)."""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.database import Database
from src.knowledge_memory_service import KnowledgeMemoryService
from src.knowledge_object_service import KnowledgeObjectService
from src.models import (
    KnowledgeEpistemicBasis,
    KnowledgeMemoryEntryKind,
    KnowledgeObjectKind,
    NoteImportance,
)

KNOWLEDGE_OBJECT_PAGE = str(
    next((Path(__file__).parents[1] / "pages").glob("14_*.py"))
)
KNOWLEDGE_MEMORY_PAGE = str(
    next((Path(__file__).parents[1] / "pages").glob("15_*.py"))
)


def _make_database(tmp_path: Path) -> Database:
    database_dir = tmp_path / "data" / "database"
    database_dir.mkdir(parents=True, exist_ok=True)
    return Database(database_dir / "knowledge.db")


def _seed_document_and_page(database: Database) -> tuple[int, int]:
    document = database.create_document(
        title="测试手册",
        filename="manual.pdf",
        source_path="data/raw/manual.pdf",
        sha256="a" * 64,
        page_count=1,
    )
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path="data/pages/1/page_0001.png",
        extracted_text="页面文本",
    )
    return document.id, page.id


def _build_object_app(tmp_path: Path, monkeypatch) -> tuple[AppTest, Database]:
    database = _make_database(tmp_path)
    service = KnowledgeObjectService(database)
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(
        runtime, "application_knowledge_object_service", lambda: service
    )
    app = AppTest.from_file(KNOWLEDGE_OBJECT_PAGE).run(timeout=30)
    return app, database


def test_object_page_loads_empty(tmp_path: Path, monkeypatch) -> None:
    app, _ = _build_object_app(tmp_path, monkeypatch)
    assert not app.exception
    assert any("还没有知识对象" in info.value for info in app.info)


def test_object_page_lists_and_filters(tmp_path: Path, monkeypatch) -> None:
    database = _make_database(tmp_path)
    service = KnowledgeObjectService(database)
    service.create(
        kind="concept",
        title="概念甲",
        content="内容甲",
        epistemic_basis=KnowledgeEpistemicBasis.PERSONAL_JUDGMENT,
    )
    service.create(
        kind="experience",
        title="经验乙",
        content="内容乙",
        epistemic_basis=KnowledgeEpistemicBasis.PERSONAL_EXPERIENCE,
    )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(
        runtime, "application_knowledge_object_service", lambda: service
    )

    app = AppTest.from_file(KNOWLEDGE_OBJECT_PAGE).run(timeout=30)

    assert not app.exception
    markdown = [item.value for item in app.markdown]
    assert any("概念甲" in value for value in markdown)
    assert any("经验乙" in value for value in markdown)

    app.selectbox(key="ko_filter_kind").set_value(KnowledgeObjectKind.EXPERIENCE).run()

    markdown = [item.value for item in app.markdown]
    assert any("经验乙" in value for value in markdown)
    assert not any("概念甲" in value for value in markdown)


def test_object_create_form_writes_database(tmp_path: Path, monkeypatch) -> None:
    app, database = _build_object_app(tmp_path, monkeypatch)

    app.selectbox(key="ko_new_kind").set_value(KnowledgeObjectKind.CONCEPT)
    app.text_input(key="ko_new_title").set_value("新概念")
    app.text_area(key="ko_new_content").set_value("新内容")
    app.selectbox(key="ko_new_importance").set_value(NoteImportance.PRIMARY)
    app.selectbox(key="ko_new_basis").set_value(KnowledgeEpistemicBasis.PERSONAL_JUDGMENT)
    app.button(key="ko_create").click().run()

    assert not app.exception
    assert database.count_knowledge_objects() == 1
    created = database.list_knowledge_objects()[0]
    assert created.title == "新概念"
    assert created.epistemic_basis is KnowledgeEpistemicBasis.PERSONAL_JUDGMENT
    assert any("已创建知识对象" in value for value in [s.value for s in app.success])


def test_object_confirm_action_marks_confirmed(tmp_path: Path, monkeypatch) -> None:
    database = _make_database(tmp_path)
    service = KnowledgeObjectService(database)
    view = service.create(
        kind="concept",
        title="草稿概念",
        content="内容",
        epistemic_basis=KnowledgeEpistemicBasis.PERSONAL_JUDGMENT,
    )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(
        runtime, "application_knowledge_object_service", lambda: service
    )

    app = AppTest.from_file(KNOWLEDGE_OBJECT_PAGE).run(timeout=30)
    app.button(key=f"ko_confirm_{view.knowledge_object.id}").click().run()

    assert not app.exception
    assert service.get(view.knowledge_object.id).confirmation_status.value == "confirmed"


def test_object_delete_action_removes_object(tmp_path: Path, monkeypatch) -> None:
    database = _make_database(tmp_path)
    service = KnowledgeObjectService(database)
    view = service.create(
        kind="fact",
        title="待删除",
        content="内容",
        epistemic_basis=KnowledgeEpistemicBasis.DIRECT_OBSERVATION,
    )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(
        runtime, "application_knowledge_object_service", lambda: service
    )

    app = AppTest.from_file(KNOWLEDGE_OBJECT_PAGE).run(timeout=30)
    app.button(key=f"ko_delete_{view.knowledge_object.id}").click().run()

    assert not app.exception
    assert database.count_knowledge_objects() == 0


def test_object_source_link_action(tmp_path: Path, monkeypatch) -> None:
    database = _make_database(tmp_path)
    _, page_id = _seed_document_and_page(database)
    service = KnowledgeObjectService(database)
    view = service.create(
        kind="fact",
        title="来源测试",
        content="内容",
        epistemic_basis=KnowledgeEpistemicBasis.SOURCE_DERIVED,
    )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(
        runtime, "application_knowledge_object_service", lambda: service
    )

    app = AppTest.from_file(KNOWLEDGE_OBJECT_PAGE).run(timeout=30)
    app.number_input(key=f"ko_source_id_{view.knowledge_object.id}").set_value(page_id)
    app.button(key=f"ko_link_{view.knowledge_object.id}").click().run()

    assert not app.exception
    sources = database.list_knowledge_object_sources(view.knowledge_object.id)
    assert len(sources) == 1
    assert sources[0].source_id == page_id


def test_memory_page_lists_entries(tmp_path: Path, monkeypatch) -> None:
    database = _make_database(tmp_path)
    memory_service = KnowledgeMemoryService(database)
    document_id, page_id = _seed_document_and_page(database)
    memory_service.create_entry(
        kind=KnowledgeMemoryEntryKind.PROBLEM_SOLVING,
        title="STM32 电机控制异常",
        content="修改 PWM、调整 PID 均无效。",
        root_cause="编码器中断配置错误。",
        lesson="高速控制系统优先检查时序问题。",
        document_id=document_id,
        page_id=page_id,
    )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(
        runtime, "application_knowledge_memory_service", lambda: memory_service
    )

    app = AppTest.from_file(KNOWLEDGE_MEMORY_PAGE).run(timeout=30)

    assert not app.exception
    assert app.title[0].value == "我保存过的内容"
    assert any("删除原资料后，副本仍会保留" in item.value for item in app.caption)
    assert not app.selectbox
    assert not app.number_input
    markdown = [item.value for item in app.markdown]
    assert any("关于 测试手册 的讨论" in value for value in markdown)
    assert any(button.label == "查看" for button in app.button)
    assert any(button.label == "删除" for button in app.button)
    visible_text = "\n".join(markdown)
    assert "问题解决" not in visible_text
    assert "现行" not in visible_text
    assert "ID 1" not in visible_text

    app.button(key="saved_view_1").click().run()
    assert not app.exception
    opened_markdown = "\n".join(item.value for item in app.markdown)
    assert "编码器中断配置错误" in opened_markdown
    assert "以后可以这样做" in opened_markdown


def test_memory_page_deletes_only_after_confirmation(tmp_path: Path, monkeypatch) -> None:
    database = _make_database(tmp_path)
    memory_service = KnowledgeMemoryService(database)
    entry = memory_service.create_entry(
        kind=KnowledgeMemoryEntryKind.EXPERIENCE,
        title="新经验",
        content="经验内容",
    )
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(
        runtime, "application_knowledge_memory_service", lambda: memory_service
    )

    app = AppTest.from_file(KNOWLEDGE_MEMORY_PAGE).run(timeout=30)
    app.button(key=f"saved_delete_{entry.id}").click().run()

    assert not app.exception
    assert database.count_knowledge_memory_entries() == 1
    assert any(button.label == "确认删除" for button in app.button)

    app.button(key=f"saved_confirm_delete_{entry.id}").click().run()
    assert not app.exception
    assert database.count_knowledge_memory_entries() == 0
