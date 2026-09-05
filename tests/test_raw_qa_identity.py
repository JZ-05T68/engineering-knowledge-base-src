"""Raw Q&A identity, citation snapshot, duplicate and tombstone tests (v0.7 P1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.agent.tools.adapters.knowledge_read import KnowledgeMemoryAdapter
from src.agent.tools.contracts import ToolContext, ToolInput
from src.database import Database
from src.knowledge_memory_service import (
    KnowledgeMemoryService,
    KnowledgeMemoryValidationError,
)
from src.knowledge_memory_ui import _citation_line
from src.knowledge_search_service import KnowledgeSearchService
from src.models import parse_memory_citations


def _memory_page_path() -> str:
    return str(next((Path(__file__).parents[1] / "pages").glob("15_*.py")))


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


def _adapter(
    service: KnowledgeMemoryService, database: Database
) -> KnowledgeMemoryAdapter:
    return KnowledgeMemoryAdapter(
        service, kb_uuid=database.get_knowledge_base_uuid()
    )


def test_save_single_source_raw_qa(tmp_path: Path) -> None:
    database = _database(tmp_path)
    document_id, page_id = _document_and_page(
        database, title="测试手册", sha256="a" * 64, text="编码器接线说明。"
    )
    service = KnowledgeMemoryService(database)

    result = service.create_raw_qa_entry(
        question="这份资料里的唯一验证标记是什么？",
        answer="标记是 EKB-VERIFY-TEST。",
        cited_page_ids=(page_id,),
    )

    assert result.entry is not None and result.skipped_citations == 0
    entry = result.entry
    assert entry.kind.value == "raw_qa"
    assert entry.creation_origin == "human_saved"
    assert entry.status.value == "active"
    assert entry.title == "关于 测试手册 的讨论"
    assert entry.document_id == document_id and entry.page_id == page_id
    assert entry.content == (
        "问题：这份资料里的唯一验证标记是什么？\n\nAgent 回答：\n标记是 EKB-VERIFY-TEST。"
    )
    assert entry.content_fingerprint is not None
    assert len(entry.content_fingerprint) == 64
    citations = parse_memory_citations(entry.citation_snapshot)
    assert len(citations) == 1
    assert citations[0].document_title == "测试手册"
    assert citations[0].document_sha256 == "a" * 64
    assert citations[0].page_number == 1
    assert citations[0].page_id == page_id
    assert citations[0].stable_id.endswith(f":page:{page_id}")


def test_save_multi_source_raw_qa_keeps_all_citations(tmp_path: Path) -> None:
    database = _database(tmp_path)
    first_document, first_page = _document_and_page(
        database, title="旧版手册", sha256="1" * 64, text="旧版额定转速 1379 rpm。"
    )
    second_document, second_page = _document_and_page(
        database, title="修订手册", sha256="2" * 64, text="修订版额定转速 1426 rpm。"
    )
    service = KnowledgeMemoryService(database)

    result = service.create_raw_qa_entry(
        question="两份资料的额定转速是否一致？",
        answer="不一致：旧版 1379 rpm，修订版 1426 rpm，请先确认版本。",
        cited_page_ids=(first_page, second_page),
    )

    entry = result.entry
    assert entry is not None
    assert entry.title == "关于 2 份资料的讨论"
    assert entry.document_id == first_document
    citations = parse_memory_citations(entry.citation_snapshot)
    assert [c.document_title for c in citations] == ["旧版手册", "修订手册"]
    assert {c.document_id for c in citations} == {first_document, second_document}
    assert {c.page_id for c in citations} == {first_page, second_page}


def test_exact_duplicate_save_is_blocked_without_new_row(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _, page_id = _document_and_page(
        database, title="测试手册", sha256="a" * 64, text="内容。"
    )
    service = KnowledgeMemoryService(database)
    first = service.create_raw_qa_entry(
        question="同一个问题？", answer="同一个回答。", cited_page_ids=(page_id,)
    )
    assert first.entry is not None

    repeat = service.create_raw_qa_entry(
        question="  同一个问题？\r\n", answer="同一个回答。\n"
    )

    assert repeat.entry is None
    assert repeat.duplicate_of is not None
    assert repeat.duplicate_of.id == first.entry.id
    assert service.count() == 1


def test_resave_after_delete_is_allowed(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _, page_id = _document_and_page(
        database, title="测试手册", sha256="a" * 64, text="内容。"
    )
    service = KnowledgeMemoryService(database)
    first = service.create_raw_qa_entry(question="问题？", answer="回答。")
    assert first.entry is not None
    service.delete_entry(first.entry.id)

    repeat = service.create_raw_qa_entry(question="问题？", answer="回答。")

    assert repeat.entry is not None
    assert repeat.duplicate_of is None
    assert service.count() == 1
    assert len(service.list_deleted()) == 1


def test_unresolvable_citations_are_counted_not_guessed(tmp_path: Path) -> None:
    database = _database(tmp_path)
    service = KnowledgeMemoryService(database)

    result = service.create_raw_qa_entry(
        question="问题？", answer="回答。", cited_page_ids=(424242,)
    )

    entry = result.entry
    assert entry is not None
    assert result.skipped_citations == 1
    assert parse_memory_citations(entry.citation_snapshot) == ()
    assert entry.document_id is None and entry.page_id is None


def test_long_content_is_refused_not_truncated(tmp_path: Path) -> None:
    database = _database(tmp_path)
    service = KnowledgeMemoryService(database)
    long_answer = "很" * 21000

    with pytest.raises(KnowledgeMemoryValidationError) as excinfo:
        service.create_raw_qa_entry(question="问题？", answer=long_answer)

    message = str(excinfo.value)
    assert "过长" in message
    assert "21018" in message  # question + wrapper + answer, exact size
    assert "未保存" in message
    assert service.count() == 0


def test_soft_delete_restore_and_explicit_purge(tmp_path: Path) -> None:
    database = _database(tmp_path)
    service = KnowledgeMemoryService(database)
    created = service.create_raw_qa_entry(question="问题？", answer="回答。")
    assert created.entry is not None
    entry_id = created.entry.id

    deleted = service.delete_entry(entry_id)
    assert deleted.status.value == "deleted"
    assert service.get(entry_id) is None
    assert service.get(entry_id, include_deleted=True) is not None
    assert [item.id for item in service.list_deleted()] == [entry_id]

    restored = service.restore_entry(entry_id)
    assert restored.status.value == "active"
    assert service.list_deleted() == []

    service.delete_entry(entry_id)
    service.purge_entry(entry_id)
    assert service.get(entry_id, include_deleted=True) is None
    assert database.get_knowledge_memory_entry(entry_id) is None


def test_purge_requires_deleted_state(tmp_path: Path) -> None:
    database = _database(tmp_path)
    service = KnowledgeMemoryService(database)
    created = service.create_raw_qa_entry(question="问题？", answer="回答。")
    assert created.entry is not None

    with pytest.raises(KnowledgeMemoryValidationError, match="先删除"):
        service.purge_entry(created.entry.id)


def test_generic_create_entry_rejects_raw_qa_kind(tmp_path: Path) -> None:
    database = _database(tmp_path)
    service = KnowledgeMemoryService(database)

    with pytest.raises(KnowledgeMemoryValidationError, match="create_raw_qa_entry"):
        service.create_entry(kind="raw_qa", title="绕过专用入口", content="x")


def test_raw_qa_boundary_in_search_and_tool(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _document_and_page(
        database, title="测试手册", sha256="a" * 64, text="编码器接线说明。"
    )
    service = KnowledgeMemoryService(database)
    created = service.create_raw_qa_entry(
        question="这份资料里的唯一验证标记是什么？",
        answer="标记是 EKB-VERIFY-TEST。",
    )
    assert created.entry is not None
    entry_id = created.entry.id
    search = KnowledgeSearchService(database)

    def _memory_hits() -> list:
        return [
            result
            for result in search.search("验证标记")
            if result.result_type.value == "knowledge_memory"
        ]

    active_hits = _memory_hits()
    assert len(active_hits) == 1
    assert active_hits[0].kind == "raw_qa"
    assert active_hits[0].kind_label == "保存的问答"
    assert "经验" not in active_hits[0].kind_label
    stable_id = active_hits[0].stable_id

    service.delete_entry(entry_id)
    assert _memory_hits() == []

    service.restore_entry(entry_id)
    assert len(_memory_hits()) == 1

    adapter = _adapter(service, database)
    ok = adapter(
        ToolInput(tool_name="get_knowledge_memory", arguments={"stable_id": stable_id}),
        ToolContext(run_id="r", request_id="q"),
    )
    assert ok.status.value == "success"
    assert ok.data["kind"] == "raw_qa"
    assert ok.data["kind_label"] == "保存的问答"
    assert ok.data["creation_origin"] == "human_saved"
    assert "不是用户经验" in ok.data["record_type_note"]

    service.delete_entry(entry_id)
    gone = adapter(
        ToolInput(tool_name="get_knowledge_memory", arguments={"stable_id": stable_id}),
        ToolContext(run_id="r", request_id="q"),
    )
    assert gone.status.value == "failed"
    assert gone.error is not None and gone.error.code.value == "not_found"


def test_citation_history_survives_source_deletion(tmp_path: Path) -> None:
    database = _database(tmp_path)
    document_id, page_id = _document_and_page(
        database, title="测试手册", sha256="a" * 64, text="内容。"
    )
    service = KnowledgeMemoryService(database)
    created = service.create_raw_qa_entry(
        question="问题？", answer="回答。", cited_page_ids=(page_id,)
    )
    entry = created.entry
    assert entry is not None

    # The material is deleted afterwards; the FK is nulled but the frozen
    # snapshot must keep describing what the answer once cited.
    with database._connection() as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DELETE FROM pages WHERE id = ?", (page_id,))
        connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))

    assert database.get_document(document_id) is None
    assert database.get_page(page_id) is None
    citations = parse_memory_citations(entry.citation_snapshot)
    assert len(citations) == 1
    assert citations[0].document_title == "测试手册"
    line = _citation_line(citations[0], database)
    assert "测试手册" in line
    assert "原资料现已不可用" in line


def test_saved_content_page_shows_raw_qa_label_and_recycle_bin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    _, page_id = _document_and_page(
        database, title="测试手册", sha256="a" * 64, text="编码器接线说明。"
    )
    service = KnowledgeMemoryService(database)
    created = service.create_raw_qa_entry(
        question="这份资料里的唯一验证标记是什么？",
        answer="标记是 EKB-VERIFY-TEST。",
        cited_page_ids=(page_id,),
    )
    entry_id = created.entry.id
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(
        runtime, "application_knowledge_memory_service", lambda: service
    )
    page_path = _memory_page_path()

    app = AppTest.from_file(page_path).run(timeout=30)
    assert not app.exception
    captions = "\n".join(item.value for item in app.caption)
    assert "保存的问答" in captions
    # The type chip reads "保存的问答", never the experience label.
    assert "· 经验" not in captions

    app.button(key=f"saved_view_{entry_id}").click().run(timeout=30)
    markdown = "\n".join(item.value for item in app.markdown)
    assert "《测试手册》第 1 页" in markdown

    app.button(key=f"saved_delete_{entry_id}").click().run(timeout=30)
    app.button(key=f"saved_confirm_delete_{entry_id}").click().run(timeout=30)
    assert service.get(entry_id) is None

    app = AppTest.from_file(page_path).run(timeout=30)
    labels = [button.label for button in app.button]
    assert "恢复" in labels and "永久删除" in labels
    app.button(key=f"saved_restore_{entry_id}").click().run(timeout=30)
    assert not app.exception
    assert service.get(entry_id) is not None
    assert service.get(entry_id).status.value == "active"
