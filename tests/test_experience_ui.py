"""AppTest UI tests for the read-only experience-model section (Phase 4)."""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.ai.provider import AIExecutionError
from src.database import Database
from src.knowledge_memory_service import KnowledgeMemoryService
from src.models import KNOWLEDGE_MEMORY_STABLE_TYPE, build_stable_id

MEMORY_PAGE = str(next((Path(__file__).parents[1] / "pages").glob("15_*.py")))


def _make_database(tmp_path: Path) -> Database:
    database_dir = tmp_path / "data" / "database"
    database_dir.mkdir(parents=True, exist_ok=True)
    return Database(database_dir / "knowledge.db")


def _memory_entry(database: Database) -> object:
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
        extracted_text="编码器 A/B 相接反导致 PID 震荡。",
    )
    service = KnowledgeMemoryService(database)
    return service.create_entry(
        kind="problem_solving",
        title="编码器接线错误",
        content="PID 震荡，逐步排查后确认 A/B 相接反。",
        root_cause="A/B 相接反",
        lesson="先核对相序",
        document_id=document.id,
        page_id=page.id,
    )


def _stub_runtime(database: Database, monkeypatch, provider_factory) -> None:
    memory_service = KnowledgeMemoryService(database)
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(
        runtime, "application_knowledge_memory_service", lambda: memory_service
    )
    monkeypatch.setattr(runtime, "application_ai_provider", provider_factory)


def _app(tmp_path: Path, monkeypatch, provider_factory) -> AppTest:
    database = _make_database(tmp_path)
    _stub_runtime(database, monkeypatch, provider_factory)
    return AppTest.from_file(MEMORY_PAGE).run(timeout=30)


def test_empty_database_shows_no_context_hint(tmp_path: Path, monkeypatch) -> None:
    app = _app(tmp_path, monkeypatch, lambda: None)

    assert not app.exception
    assert any(
        "没有可选择的现行知识对象或知识记忆" in info.value for info in app.info
    )


def test_generate_button_disabled_without_selection(
    tmp_path: Path, monkeypatch
) -> None:
    database = _make_database(tmp_path)
    _memory_entry(database)
    _stub_runtime(database, monkeypatch, lambda: None)

    app = AppTest.from_file(MEMORY_PAGE).run(timeout=30)

    assert not app.exception
    assert app.button(key="experience_generate").disabled is True


def test_click_generates_once_and_rerun_does_not_repeat(
    tmp_path: Path, monkeypatch
) -> None:
    database = _make_database(tmp_path)
    entry = _memory_entry(database)
    stable_id = build_stable_id(
        database.get_knowledge_base_uuid(), KNOWLEDGE_MEMORY_STABLE_TYPE, entry.id
    )
    calls: list[int] = []

    def provider_factory():
        calls.append(1)
        return None

    _stub_runtime(database, monkeypatch, provider_factory)
    app = AppTest.from_file(MEMORY_PAGE).run(timeout=30)

    option = f"知识记忆｜{entry.title}｜{stable_id}"
    app.multiselect(key="experience_context_selection").set_value([option])
    app.text_area(key="experience_task").set_value("总结编码器经验")
    app.button(key="experience_generate").click().run()

    assert not app.exception
    assert len(calls) == 1
    markdown = [item.value for item in app.markdown]
    warnings = [item.value for item in app.warning]
    captions = [item.value for item in app.caption]
    assert any("离线演示生成" in value for value in warnings)
    assert any("结构化经验候选" in value for value in markdown)
    assert any(stable_id in value for value in captions)

    app.run()

    assert len(calls) == 1
    markdown = [item.value for item in app.markdown]
    assert any("结构化经验候选" in value for value in markdown)


def test_provider_failure_does_not_break_memory_page(
    tmp_path: Path, monkeypatch
) -> None:
    database = _make_database(tmp_path)
    entry = _memory_entry(database)
    stable_id = build_stable_id(
        database.get_knowledge_base_uuid(), KNOWLEDGE_MEMORY_STABLE_TYPE, entry.id
    )

    class _FailingProvider:
        def complete(self, prompt, *, model=None, max_completion_tokens=None):
            raise AIExecutionError("模拟调用失败")

    def provider_factory():
        return _FailingProvider()

    _stub_runtime(database, monkeypatch, provider_factory)
    app = AppTest.from_file(MEMORY_PAGE).run(timeout=30)

    option = f"知识记忆｜{entry.title}｜{stable_id}"
    app.multiselect(key="experience_context_selection").set_value([option])
    app.text_area(key="experience_task").set_value("总结编码器经验")
    app.button(key="experience_generate").click().run()

    assert not app.exception
    assert any("AI 服务调用失败" in error.value for error in app.error)
    markdown = [item.value for item in app.markdown]
    assert any(entry.title in value for value in markdown)


def test_no_automatic_save_entry_exists(tmp_path: Path, monkeypatch) -> None:
    database = _make_database(tmp_path)
    entry = _memory_entry(database)
    stable_id = build_stable_id(
        database.get_knowledge_base_uuid(), KNOWLEDGE_MEMORY_STABLE_TYPE, entry.id
    )
    _stub_runtime(database, monkeypatch, lambda: None)
    app = AppTest.from_file(MEMORY_PAGE).run(timeout=30)

    option = f"知识记忆｜{entry.title}｜{stable_id}"
    app.multiselect(key="experience_context_selection").set_value([option])
    app.button(key="experience_generate").click().run()

    assert not app.exception
    assert all("保存" not in button.label for button in app.button)
    assert all("写入" not in button.label for button in app.button)
    markdown = [item.value for item in app.markdown]
    assert any("只读预览" in value for value in markdown)
