"""Current schema v12 backup/restore roundtrip over new knowledge data (Phase 6)."""

from __future__ import annotations

from pathlib import Path

from src.ai.provider import AiCallRecord
from src.backup_service import (
    BackupService,
    read_database_summary,
    sha256_file,
    validate_backup,
)
from src.database import Database
from src.knowledge_object_service import KnowledgeObjectService
from src.migrations import SCHEMA_VERSION
from src.models import KnowledgeRelationType


def _build_data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    (data / "raw").mkdir(parents=True)
    (data / "pages" / "1").mkdir(parents=True)
    (data / "markdown").mkdir(parents=True)
    (data / "database").mkdir()
    pdf = data / "raw" / "manual.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    png = data / "pages" / "1" / "page_0001.png"
    png.write_bytes(b"\x89PNG")
    database = Database(data / "database" / "knowledge.db")
    document = database.create_document(
        title="手册",
        filename="manual.pdf",
        source_path=pdf,
        sha256=sha256_file(pdf),
        page_count=1,
    )
    page = database.create_page(
        document_id=document.id,
        page_number=1,
        image_path=png,
        extracted_text="编码器 A/B 相接反。",
    )
    service = KnowledgeObjectService(database)
    first = service.create(
        kind="experience", title="经验甲", content="内容甲",
        epistemic_basis="personal_experience",
    ).knowledge_object
    second = service.create(
        kind="fact", title="事实乙", content="内容乙",
        epistemic_basis="source_derived",
    ).knowledge_object
    service.link_source(first.id, source_type="page", source_id=page.id)
    service.add_relation(
        first.id, second.id, relation_type=KnowledgeRelationType.SUPPORTS
    )
    database.create_knowledge_memory_entry(
        kind="experience", title="记忆甲", content="记忆内容",
        document_id=document.id, page_id=page.id,
    )
    database.insert_ai_call(
        AiCallRecord(
            call_uuid="1" * 32,
            capability="completion",
            model="qwen3.7-plus",
            prompt_sha256="a" * 64,
            input_chars=10,
            status="success",
            source_feature="rag_answer",
            target_refs=(database.knowledge_object_stable_id(first.id),),
            created_at="2026-08-25T10:00:01",
        )
    )
    database.insert_ai_call(
        AiCallRecord(
            call_uuid="2" * 32,
            capability="completion",
            model="qwen3.7-plus",
            prompt_sha256="b" * 64,
            input_chars=10,
            status="error",
            source_feature="experience_model",
            target_refs=(database.knowledge_object_stable_id(second.id),),
            error_class="http_500",
            created_at="2026-08-25T10:00:02",
        )
    )
    return data


def test_v12_backup_roundtrip_preserves_new_knowledge_data(tmp_path: Path) -> None:
    data = _build_data_dir(tmp_path)
    database_path = data / "database" / "knowledge.db"
    service = BackupService(
        app_version="0.5.2",
        data_dir=data,
        raw_dir=data / "raw",
        pages_dir=data / "pages",
        markdown_dir=data / "markdown",
        database_path=database_path,
        backups_dir=tmp_path / "backups",
    )

    backup = service.create_backup().backup_path
    validation = validate_backup(
        backup, expected_app_version="0.5.2", expected_schema_version=SCHEMA_VERSION
    )
    assert validation.valid

    restored_data = tmp_path / "restored" / "data"
    (restored_data / "raw").mkdir(parents=True)
    (restored_data / "pages").mkdir(parents=True)
    (restored_data / "markdown").mkdir(parents=True)
    (restored_data / "database").mkdir()
    restore_service = BackupService(
        app_version="0.5.2",
        data_dir=restored_data,
        raw_dir=restored_data / "raw",
        pages_dir=restored_data / "pages",
        markdown_dir=restored_data / "markdown",
        database_path=restored_data / "database" / "knowledge.db",
        backups_dir=tmp_path / "restored-backups",
    )
    result = restore_service.restore_backup(backup, require_existing_target=False)

    summary = result.database_summary
    assert summary.schema_version == SCHEMA_VERSION
    assert summary.integrity_check == "ok"
    assert summary.foreign_key_violations == 0

    restored_db = Database(restored_data / "database" / "knowledge.db")
    objects = restored_db.list_knowledge_objects()
    assert len(objects) == 2
    memories = restored_db.list_knowledge_memory_entries()
    assert len(memories) == 1
    calls = restored_db.list_ai_calls()
    assert len(calls) == 2
    assert {call.source_feature for call in calls} == {
        "rag_answer",
        "experience_model",
    }
    assert sorted(call.status for call in calls) == ["error", "success"]
    restored_summary = read_database_summary(
        restored_data / "database" / "knowledge.db"
    )
    assert restored_summary.schema_version == SCHEMA_VERSION
