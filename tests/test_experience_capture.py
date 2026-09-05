"""v0.7.0 Experience Capture: raw Q&A → draft → user edit → confirmed experience.

Covers the service boundary (``promote_raw_qa_to_experience``), the plain-UI
draft flow on the saved-content page (section split, mock AI draft, user
editing, explicit root-cause confirmation gesture) and the traceability chain
(Experience → Raw Q&A → citation snapshot).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.ai.experience_model_service import ExperienceModelService
from src.ai.rag_answer_service import MockCompletionProvider
from src.database import Database
from src.knowledge_memory_service import (
    KnowledgeMemoryEntryNotFoundError,
    KnowledgeMemoryService,
    KnowledgeMemoryValidationError,
)
from src.models import parse_memory_citations


def _database(tmp_path: Path) -> Database:
    database_dir = tmp_path / "data" / "database"
    database_dir.mkdir(parents=True, exist_ok=True)
    return Database(database_dir / "knowledge.db")


def _document_and_page(
    database: Database, *, title: str, sha256: str, text: str
) -> tuple[int, int]:
    document = database.create_document(
        title=title,
        filename=f"{sha256[:8]}.pdf",
        source_path=f"data/raw/{sha256[:8]}.pdf",
        sha256=sha256,
        page_count=1,
    )
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=f"data/pages/{document.id}/page_0001.png",
        extracted_text=text,
    )
    return document.id, page.id


def _raw_qa(service: KnowledgeMemoryService, page_id: int) -> object:
    result = service.create_raw_qa_entry(
        question="变频器报 OC 后应该先检查什么？",
        answer="先检查 A/B 相接线，再检查直流母线电压。",
        cited_page_ids=(page_id,),
    )
    assert result.entry is not None
    return result.entry


def _by_label(widgets, label: str):
    """Return the one AppTest widget with this label."""

    return next(item for item in widgets if item.label == label)


# --------------------------------------------------------------- service layer


def test_promote_copies_traceability_and_keeps_raw_copy_intact(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _, page_id = _document_and_page(
        database, title="变频器手册", sha256="a" * 64, text="接线说明。"
    )
    service = KnowledgeMemoryService(database)
    raw_qa = _raw_qa(service, page_id)

    experience = service.promote_raw_qa_to_experience(
        raw_qa.id,
        title="变频器 OC 排查经验",
        content="遇到的问题：OC 报警。\n\n处理方式：先查 A/B 相接线。",
        root_cause="A/B 相接反",
        lesson="报警后先查相序。",
        outcome="恢复正常运行。",
        context_conditions="380V 三相变频器。",
        root_cause_confirmed=True,
    )

    assert experience.kind.value == "experience"
    assert experience.creation_origin == "agent_assisted"
    assert experience.root_cause_confirmed is True
    assert experience.source_entry_id == raw_qa.id
    assert experience.source_title == raw_qa.title
    assert experience.document_id == raw_qa.document_id
    assert experience.page_id == raw_qa.page_id
    assert experience.citation_snapshot == raw_qa.citation_snapshot
    assert experience.outcome == "恢复正常运行。"
    assert experience.context_conditions == "380V 三相变频器。"

    # The raw copy itself is never modified by promotion.
    unchanged = service.get(raw_qa.id)
    assert unchanged is not None
    assert unchanged.kind.value == "raw_qa"
    assert unchanged.status.value == "active"
    assert unchanged.root_cause_confirmed is False


def test_promote_requires_explicit_root_cause_gesture(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _, page_id = _document_and_page(
        database, title="手册", sha256="b" * 64, text="维护说明。"
    )
    service = KnowledgeMemoryService(database)
    raw_qa = _raw_qa(service, page_id)

    experience = service.promote_raw_qa_to_experience(
        raw_qa.id,
        title="无确认的经验",
        content="遇到的问题：测试。",
        root_cause="AI 推断的根因",
    )

    assert experience.root_cause_confirmed is False


def test_promote_rejects_non_raw_qa_and_non_active_sources(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _, page_id = _document_and_page(
        database, title="手册", sha256="c" * 64, text="说明。"
    )
    service = KnowledgeMemoryService(database)
    raw_qa = _raw_qa(service, page_id)

    experience = service.promote_raw_qa_to_experience(
        raw_qa.id, title="第一次", content="遇到的问题：测试。"
    )
    with pytest.raises(KnowledgeMemoryValidationError, match="保存的问答"):
        service.promote_raw_qa_to_experience(
            experience.id, title="再次", content="遇到的问题：测试。"
        )

    service.delete_entry(raw_qa.id)
    with pytest.raises(KnowledgeMemoryValidationError, match="删除或归档"):
        service.promote_raw_qa_to_experience(
            raw_qa.id, title="墓碑来源", content="遇到的问题：测试。"
        )
    service.restore_entry(raw_qa.id)
    assert service.get(raw_qa.id) is not None


def test_promote_rejects_empty_fields_and_never_truncates(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _, page_id = _document_and_page(
        database, title="手册", sha256="d" * 64, text="说明。"
    )
    service = KnowledgeMemoryService(database)
    raw_qa = _raw_qa(service, page_id)

    with pytest.raises(KnowledgeMemoryValidationError):
        service.promote_raw_qa_to_experience(raw_qa.id, title="  ", content="内容")
    with pytest.raises(KnowledgeMemoryValidationError):
        service.promote_raw_qa_to_experience(raw_qa.id, title="标题", content="   ")
    with pytest.raises(KnowledgeMemoryValidationError):
        service.promote_raw_qa_to_experience(
            raw_qa.id, title="标题", content="超" * 20001
        )
    with pytest.raises(KnowledgeMemoryEntryNotFoundError):
        service.promote_raw_qa_to_experience(9999, title="标题", content="内容")


# ------------------------------------------------------------------- UI layer


def _memory_page_path() -> str:
    return str(next((Path(__file__).parents[1] / "pages").glob("15_*.py")))


def _stub_runtime(monkeypatch: pytest.MonkeyPatch, database: Database) -> None:
    service = KnowledgeMemoryService(database)
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(
        runtime, "application_knowledge_memory_service", lambda: service
    )
    monkeypatch.setattr(
        runtime,
        "application_experience_model_service",
        lambda: ExperienceModelService(MockCompletionProvider()),
    )
    return service


def test_page_splits_sections_and_offers_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    _, page_id = _document_and_page(
        database, title="变频器手册", sha256="e" * 64, text="接线说明。"
    )
    service = _stub_runtime(monkeypatch, database)
    raw_qa = _raw_qa(service, page_id)

    app = AppTest.from_file(_memory_page_path()).run(timeout=30)
    assert not app.exception
    markdown = "\n".join(item.value for item in app.markdown)
    assert "### 保存的问答" in markdown
    assert "### 整理好的经验" in markdown

    app.button(key=f"saved_view_{raw_qa.id}").click().run(timeout=30)
    labels = [button.label for button in app.button]
    assert "整理成经验" in labels


def test_full_capture_flow_edit_confirm_and_traceability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    _, page_id = _document_and_page(
        database, title="变频器手册", sha256="f" * 64, text="接线说明。"
    )
    service = _stub_runtime(monkeypatch, database)
    raw_qa = _raw_qa(service, page_id)

    app = AppTest.from_file(_memory_page_path()).run(timeout=30)
    app.button(key=f"saved_view_{raw_qa.id}").click().run(timeout=30)
    app.button(key=f"saved_promote_{raw_qa.id}").click().run(timeout=30)
    assert not app.exception
    markdown = "\n".join(item.value for item in app.markdown)
    assert "整理成经验（草稿）" in markdown
    # Offline draft is always labelled, never presented as a real AI result.
    assert any("离线演示生成" in item.value for item in app.warning)

    # The user edits the draft before confirming (at least one field).
    _by_label(app.text_input, "标题").set_value("变频器 OC 排查经验").run(timeout=30)
    _by_label(app.text_area, "遇到的问题").set_value("变频器运行中报 OC。").run(timeout=30)
    _by_label(app.text_area, "处理方式").set_value("先断电，再检查 A/B 相接线。").run(
        timeout=30
    )
    _by_label(app.text_area, "结果").set_value("恢复正常运行。").run(timeout=30)
    _by_label(app.text_area, "最终原因").set_value("A/B 相接反。").run(timeout=30)
    _by_label(app.text_area, "经验教训").set_value("OC 先查相序和接线。").run(timeout=30)

    # The AI root-cause judgment only becomes confirmed via an explicit gesture.
    app.checkbox(key=f"saved_exp_draft_{raw_qa.id}_confirmed").check().run(timeout=30)
    # Two-phase confirm: freeze the reviewed snapshot, then commit it.
    app.button(key=f"saved_exp_review_{raw_qa.id}").click().run(timeout=30)
    review_markdown = "\n".join(item.value for item in app.markdown)
    assert "最后确认（内容已锁定）" in review_markdown
    app.button(key=f"saved_exp_confirm_{raw_qa.id}").click().run(timeout=30)
    assert not app.exception

    entries = service.list()
    experiences = [item for item in entries if item.kind.value == "experience"]
    assert len(experiences) == 1
    experience = experiences[0]
    assert experience.title == "变频器 OC 排查经验"
    assert experience.root_cause == "A/B 相接反。"
    assert experience.lesson == "OC 先查相序和接线。"
    assert experience.outcome == "恢复正常运行。"
    assert experience.creation_origin == "agent_assisted"
    assert experience.root_cause_confirmed is True
    assert experience.source_entry_id == raw_qa.id
    assert experience.citation_snapshot == (
        service.get(raw_qa.id).citation_snapshot
    )
    assert "遇到的问题：变频器运行中报 OC。" in experience.content
    assert "处理方式：先断电，再检查 A/B 相接线。" in experience.content

    # The raw copy is untouched and still active.
    unchanged = service.get(raw_qa.id)
    assert unchanged.kind.value == "raw_qa"
    assert unchanged.status.value == "active"

    # The saved list now shows the experience under 整理好的经验 with provenance.
    app = AppTest.from_file(_memory_page_path()).run(timeout=30)
    experience_id = experience.id
    app.button(key=f"saved_view_{experience_id}").click().run(timeout=30)
    visible = "\n".join(
        [item.value for item in app.markdown] + [item.value for item in app.caption]
    )
    assert "来自保存的问答" in visible
    labels = [button.label for button in app.button]
    assert "查看原始问答" in labels

    app.button(key=f"saved_open_source_{experience_id}").click().run(timeout=30)
    markdown = "\n".join(item.value for item in app.markdown)
    assert "先检查 A/B 相接线" in markdown  # 原始问答正文再次可见
    citations = parse_memory_citations(experience.citation_snapshot)
    assert citations and citations[0].document_title == "变频器手册"


def test_capture_without_confirmation_keeps_root_cause_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    _, page_id = _document_and_page(
        database, title="手册", sha256="9" * 64, text="说明。"
    )
    service = _stub_runtime(monkeypatch, database)
    raw_qa = _raw_qa(service, page_id)

    app = AppTest.from_file(_memory_page_path()).run(timeout=30)
    app.button(key=f"saved_view_{raw_qa.id}").click().run(timeout=30)
    app.button(key=f"saved_promote_{raw_qa.id}").click().run(timeout=30)
    _by_label(app.text_area, "最终原因").set_value("AI 推断的根因。").run(timeout=30)
    # No checkbox gesture: save as-is (two-phase confirm).
    app.button(key=f"saved_exp_review_{raw_qa.id}").click().run(timeout=30)
    app.button(key=f"saved_exp_confirm_{raw_qa.id}").click().run(timeout=30)
    assert not app.exception

    experiences = [
        item for item in service.list() if item.kind.value == "experience"
    ]
    assert len(experiences) == 1
    assert experiences[0].root_cause == "AI 推断的根因。"
    assert experiences[0].root_cause_confirmed is False


def test_promote_same_raw_qa_twice_is_rejected(tmp_path: Path) -> None:
    """v0.7.3: one saved Q&A distills into at most one active experience."""

    database = _database(tmp_path)
    _, page_id = _document_and_page(
        database, title="手册", sha256="e" * 64, text="说明。"
    )
    service = KnowledgeMemoryService(database)
    raw_qa = _raw_qa(service, page_id)

    service.promote_raw_qa_to_experience(
        raw_qa.id, title="第一次整理", content="遇到的问题：测试。"
    )
    with pytest.raises(KnowledgeMemoryValidationError, match="已经整理过经验"):
        service.promote_raw_qa_to_experience(
            raw_qa.id, title="重复整理", content="遇到的问题：测试。"
        )

    # After the experience is deleted, re-promotion is allowed again.
    experience = next(
        item
        for item in service.list()
        if item.kind.value == "experience"
    )
    service.delete_entry(experience.id)
    again = service.promote_raw_qa_to_experience(
        raw_qa.id, title="重新整理", content="遇到的问题：测试。"
    )
    assert again.title == "重新整理"
