"""Tests for schema v12: AI ledger tables, memory extension and stable IDs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import src.migrations as migrations_module
from src.ai.provider import AiCallRecord, AiOutputRecord
from src.database import Database
from src.migrations import SCHEMA_VERSION, MigrationError, _read_schema_version, migrate_database
from src.models import (
    EVIDENCE_STABLE_TYPE,
    PAGE_STABLE_TYPE,
    build_stable_id,
)


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[1]
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def test_fresh_database_migrates_to_v13(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")

    assert SCHEMA_VERSION == 13
    assert database.SCHEMA_VERSION == 13
    with sqlite3.connect(database.database_path) as connection:
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        assert versions == list(range(1, 14))
        for table in ("ai_calls", "ai_outputs", "knowledge_project_links"):
            assert connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
        memory_columns = _table_columns(connection, "knowledge_memory_entries")
        assert {"content_revision", "outcome", "context_conditions"} <= memory_columns
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v11_to_v12_migration_preserves_legacy_memory_and_backfills_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "knowledge.db"
    monkeypatch.setattr(
        migrations_module, "_V12_INJECTION_POINT", "v12_ai_calls"
    )
    with pytest.raises(MigrationError, match="v12 迁移失败注入点"):
        migrate_database(database_path)
    monkeypatch.setattr(migrations_module, "_V12_INJECTION_POINT", None)
    assert _read_schema_version(database_path) == 11

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO knowledge_memory_entries(
                kind, title, content, root_cause, lesson,
                knowledge_object_id, document_id, page_id, status,
                created_at, updated_at
            ) VALUES (
                'experience', '旧经验', '旧内容', '旧根因', '旧教训',
                NULL, NULL, NULL, 'active',
                '2026-08-25T00:00:00.000000+00:00',
                '2026-08-25T00:00:00.000000+00:00'
            )
            """
        )
        connection.commit()

    backup_path = migrate_database(database_path)
    assert backup_path is not None
    assert _read_schema_version(database_path) == 13

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT id, kind, title, content, root_cause, lesson,"
            " content_revision, outcome, context_conditions "
            "FROM knowledge_memory_entries"
        ).fetchone()
        assert tuple(row) == (
            1, "experience", "旧经验", "旧内容", "旧根因", "旧教训",
            1, "", "",
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    # Re-running the migration is safe and creates no extra backup.
    assert migrate_database(database_path) is None
    assert _read_schema_version(database_path) == 13


def test_ai_calls_is_append_only_and_stores_no_plaintext(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    assert not hasattr(Database, "update_ai_call")
    assert not hasattr(Database, "delete_ai_call")

    with sqlite3.connect(database.database_path) as connection:
        columns = _table_columns(connection, "ai_calls")
        assert "prompt" not in columns
        assert "response" not in columns
        assert "api_key" not in columns
        assert "prompt_sha256" in columns

    database.insert_ai_call(
        AiCallRecord(
            call_uuid="11111111-1111-1111-1111-111111111111",
            capability="completion",
            model="qwen3.7-plus",
            prompt_sha256="a" * 64,
            input_chars=2,
            status="success",
            source_feature="unit_test",
            target_refs=("kb:page:1",),
            retry_count=1,
            latency_ms=42,
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            finish_reason="stop",
            created_at="2026-08-25T00:00:00.000000+00:00",
        )
    )

    rows = database.list_ai_calls()
    assert len(rows) == 1
    record = rows[0]
    assert record.call_uuid == "11111111-1111-1111-1111-111111111111"
    assert record.capability == "completion"
    assert record.status == "success"
    assert record.retry_count == 1
    assert record.total_tokens == 15
    assert record.target_refs == ("kb:page:1",)
    assert database.total_ai_tokens_since("2026-08-24T00:00:00.000000+00:00") == 15


def test_ai_calls_check_constraints_reject_bad_rows(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")

    with sqlite3.connect(database.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO ai_calls(
                    call_uuid, capability, model, prompt_sha256, input_chars,
                    status, source_feature, created_at
                ) VALUES (?, 'completion', 'm', ?, 1, 'bogus', 'unit_test', ?)
                """,
                ("22222222-2222-2222-2222-222222222222", "b" * 64, "now"),
            )


def test_ai_outputs_stores_only_audit_anchors(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")

    with sqlite3.connect(database.database_path) as connection:
        columns = _table_columns(connection, "ai_outputs")
        assert "output_text" not in columns
        assert "response" not in columns
        assert "output_sha256" in columns

    database.insert_ai_output(
        AiOutputRecord(
            output_uuid="33333333-3333-3333-3333-333333333333",
            model="qwen3.7-plus",
            output_sha256="c" * 64,
            output_kind="imported_answer",
            source_feature="unit_test",
            call_uuid="11111111-1111-1111-1111-111111111111",
            context_package_sha256="d" * 64,
            target_refs=("kb:knowledge_object:1",),
            recheck_path="data/markdown/answer.md",
            created_at="2026-08-25T00:00:00.000000+00:00",
        )
    )

    rows = database.list_ai_outputs()
    assert len(rows) == 1
    assert rows[0].output_sha256 == "c" * 64
    assert rows[0].target_refs == ("kb:knowledge_object:1",)
    # ai_outputs is an audit anchor only: no knowledge asset is created.
    assert database.count_knowledge_objects() == 0


def test_project_links_unique_and_delete_behavior(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")
    project = database.create_project("电机项目")
    memory = database.create_knowledge_memory_entry(
        kind="experience", title="编码器接线经验"
    )
    knowledge_object = database.create_knowledge_object(
        kind="fact", title="PID 原理", content="比例积分微分"
    )

    assert database.link_knowledge_to_project(
        project_id=project.id, target_type="knowledge_memory", target_id=memory.id
    )
    assert not database.link_knowledge_to_project(
        project_id=project.id, target_type="knowledge_memory", target_id=memory.id
    )
    assert database.link_knowledge_to_project(
        project_id=project.id, target_type="knowledge_object", target_id=knowledge_object.id
    )

    links = database.list_project_knowledge_links(project_id=project.id)
    assert {(link.target_type, link.target_id) for link in links} == {
        ("knowledge_memory", memory.id),
        ("knowledge_object", knowledge_object.id),
    }

    # Deleting the memory removes only its link; project and KO survive.
    database.delete_knowledge_memory_entry(memory.id)
    remaining = database.list_project_knowledge_links(project_id=project.id)
    assert [(link.target_type, link.target_id) for link in remaining] == [
        ("knowledge_object", knowledge_object.id)
    ]
    assert any(item.id == project.id for item in database.list_projects())

    # Deleting the KO removes only its link; project survives.
    database.delete_knowledge_object(knowledge_object.id)
    assert database.list_project_knowledge_links(project_id=project.id) == []
    assert any(item.id == project.id for item in database.list_projects())

    # Deleting the project cascades any remaining links, never the knowledge.
    memory_two = database.create_knowledge_memory_entry(
        kind="experience", title="第二条经验"
    )
    database.link_knowledge_to_project(
        project_id=project.id, target_type="knowledge_memory", target_id=memory_two.id
    )
    database.delete_project(project.id)
    assert database.list_project_knowledge_links(project_id=project.id) == []
    assert database.get_knowledge_memory_entry(memory_two.id) is not None


def test_memory_defaults_and_content_revision_increment(tmp_path: Path) -> None:
    database = Database(tmp_path / "knowledge.db")

    entry = database.create_knowledge_memory_entry(
        kind="problem_solving",
        title="STM32 编码器故障",
        root_cause="接线错误",
        lesson="先查接线",
    )
    assert entry.content_revision == 1
    assert entry.outcome == ""
    assert entry.context_conditions == ""

    updated = database.update_knowledge_memory_entry(
        entry.id, outcome="PID 震荡消失", context_conditions="适用于正交编码器"
    )
    assert updated.content_revision == 2
    assert updated.outcome == "PID 震荡消失"
    assert updated.context_conditions == "适用于正交编码器"

    status_only = database.update_knowledge_memory_status(entry.id, status="archived")
    assert status_only.content_revision == 2

    with pytest.raises(ValueError, match="结果不能超过 4000 个字符"):
        database.update_knowledge_memory_entry(entry.id, outcome="x" * 4001)


def test_stable_id_supports_page_and_evidence() -> None:
    kb_uuid = "12345678-1234-1234-1234-123456789abc"

    assert build_stable_id(kb_uuid, PAGE_STABLE_TYPE, 7) == f"{kb_uuid}:page:7"
    assert build_stable_id(kb_uuid, EVIDENCE_STABLE_TYPE, 9) == f"{kb_uuid}:evidence:9"
    with pytest.raises(ValueError, match="非法稳定 ID 对象类型"):
        build_stable_id(kb_uuid, "note", 1)
    with pytest.raises(ValueError, match="非法稳定 ID 对象类型"):
        build_stable_id(kb_uuid, "document", 1)
